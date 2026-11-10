"""Read-only viewport previews. Maya reads happen before VP2 draw callbacks."""
from collections import defaultdict
from types import SimpleNamespace
import maya.api.OpenMaya as om
import maya.api.OpenMayaUI as omui
import uv_straight_live_draw_state as state
from uv_straight_colors import DISTORTION_NEUTRAL, completion_tint, finished_visible


def offset_triangle(points, a, b, c):
    normal = (b-a) ^ (c-a)
    if normal.length() <= 1e-15:
        return
    normal.normalize()
    offset = normal * max((b-a).length(), (c-a).length(), (c-b).length(), 1.) * 2e-5
    points.extend(tuple(p + offset) for p in (a, b, c))


class LiveViewportPreview:
    def __init__(self):
        self.geometry = {}
        self.groups = {}
        self.source = None
        self.panel_shapes = {}
        self.face_payload = None
        self.face_paths = {}
        self.face_watches = []
        self.faces_dirty = False

    def _invalidate_faces(self, *unused):
        self.faces_dirty = True

    def _dirty_face_geometry(self, node, plug, *unused):
        if plug.partialName(useLongNames=True).startswith(('outMesh', 'inMesh', 'pnts', 'visibility')):
            self.faces_dirty = True

    def set_faces(self, payload):
        """Draw sparse faces without changing any shape or shader attributes."""
        if payload is not self.face_payload:
            self.clear()
            self.face_payload = payload
            self.faces_dirty = True
            for shape in dict.fromkeys(shape for shape, _, _ in payload):
                selection = om.MSelectionList()
                try:
                    selection.add(shape)
                    path = selection.getDagPath(0)
                except RuntimeError:
                    continue
                self.face_paths[shape] = path
                self.face_watches.append(om.MNodeMessage.addNodeDirtyPlugCallback(path.node(), self._dirty_face_geometry))
                self.face_watches.append(om.MDagMessage.addWorldMatrixModifiedCallback(path, self._invalidate_faces))
        if not self.faces_dirty and all(
                (path.isValid() and path.isVisible()) == (shape in self.groups)
                for shape, path in self.face_paths.items()):
            return
        self.faces_dirty = False
        by_shape = defaultdict(dict)
        for shape, face, rgba in payload:
            by_shape[shape][face] = rgba
        self.groups.clear()
        for shape, path in self.face_paths.items():
            if not path.isValid() or not path.isVisible():
                continue
            points, values = [], []
            faces = by_shape[shape]
            polygons = om.MItMeshPolygon(path)
            count = polygons.count()
            for face, rgba in faces.items():
                if not 0 <= face < count:
                    continue
                polygons.setIndex(face)
                triangles, _ = polygons.getTriangles(om.MSpace.kWorld)
                first = len(points)
                for index in range(0, len(triangles), 3):
                    offset_triangle(points, triangles[index], triangles[index+1], triangles[index+2])
                values.extend([rgba] * (len(points)-first))
            self.groups[shape] = (faces, points, values)
        self._publish()

    def set_panel_shapes(self, panels):
        if panels == self.panel_shapes:
            return
        self.panel_shapes = panels
        if self.source is not None:
            self._publish()

    def update(self, scene, baseline, shapes):
        changed = False
        show_finished = finished_visible()
        for shape in shapes:
            cached = scene.meshes.get(shape)
            if cached is None or not cached.is_valid():
                changed |= self.groups.pop(shape, None) is not None
                continue
            mesh = cached.entries[0][0]
            if not mesh.path.isVisible():
                changed |= self.groups.pop(shape, None) is not None
                continue
            done = show_finished and om.MFnDependencyNode(mesh.path.node()).hasAttribute('uvStraightDoneFillBackup')
            colors = {}
            base = baseline.get(shape, {})
            for _, face, rgba in scene.face_colors_by_shape.get(shape, ()):
                if rgba == DISTORTION_NEUTRAL or base.get(face) == (completion_tint(rgba) if done else rgba):
                    continue
                # The live draw layer is alpha blended over the material.
                # Recover tint and opacity from the neutral-centred CPV value.
                strength = 2.*max(abs(value-.5) for value in rgba[:3])
                colors[face] = tuple(.5+(value-.5)/strength for value in rgba[:3]) + (.65*strength,)
            previous = self.groups.get(shape)
            if previous is not None and previous[0] == colors:
                continue
            changed = True
            if not colors:
                self.groups.pop(shape, None)
                continue
            geometry = self.geometry.get(shape)
            if geometry is None or geometry[0] is not cached:
                # Current positions are for drawing; the distortion metric
                # continues to use its fixed geometry reference.
                positions = om.MFnMesh(mesh.path).getPoints(om.MSpace.kWorld)
                geometry = (cached, positions,
                            {face[0]: face for face in cached.entries[0][-1]}, {})
                self.geometry[shape] = geometry
            _, positions, faces, triangles = geometry
            points, values = [], []
            for face, rgba in colors.items():
                if face not in triangles:
                    data = faces.get(face)
                    if data is None:
                        continue
                    face_points = []
                    vertices = data[3]
                    for triangle in data[4]:
                        a, b, c = (positions[vertices[i]] for i in triangle)
                        offset_triangle(face_points, a, b, c)
                    triangles[face] = face_points
                points.extend(triangles[face])
                values.extend([rgba] * len(triangles[face]))
            self.groups[shape] = (colors, points, values)
        active = set(scene.shapes)
        for shape in set(self.groups) - active:
            del self.groups[shape]
            changed = True
        if not changed:
            return False
        self._publish()
        return True

    def _publish(self):
        def pack(groups):
            points = om.MPointArray([p for _, data, _ in groups for p in data])
            colors = om.MColorArray([om.MColor(c) for _, _, data in groups for c in data])
            return SimpleNamespace(points=points, colors=colors)
        self.source = pack(list(self.groups.values()))
        self.source.by_panel = {
            panel: pack([data for shape, data in self.groups.items() if shape in allowed])
            for panel, allowed in self.panel_shapes.items()}
        state.VIEWPORT_PAYLOAD = self.source
        omui.M3dView.scheduleRefreshAllViews()

    def clear(self):
        active = self.source is not None
        for callback in self.face_watches:
            try:
                om.MMessage.removeCallback(callback)
            except RuntimeError:
                pass
        self.face_watches.clear()
        self.face_paths.clear()
        self.face_payload = None
        self.faces_dirty = False
        self.geometry.clear()
        self.groups.clear()
        self.source = None
        state.VIEWPORT_PAYLOAD = None
        if active:
            omui.M3dView.scheduleRefreshAllViews()
