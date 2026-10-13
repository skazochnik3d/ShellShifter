"""Read-only drag preview. All Maya reads happen before VP2 draw callbacks."""
from types import SimpleNamespace
import maya.api.OpenMaya as om
import maya.api.OpenMayaUI as omui
import uv_straight_live_draw_state as state
from uv_straight_colors import completion_tint


class LiveViewportPreview:
    def __init__(self):
        self.geometry = {}
        self.groups = {}
        self.source = None
        self.panel_shapes = {}

    def update(self, scene, baseline, shapes):
        changed = False
        for shape in shapes:
            cached = scene.meshes.get(shape)
            if cached is None or not cached.is_valid():
                changed |= self.groups.pop(shape, None) is not None
                continue
            mesh = cached.entries[0][0]
            if not mesh.path.isVisible():
                changed |= self.groups.pop(shape, None) is not None
                continue
            done = om.MFnDependencyNode(mesh.path.node()).hasAttribute('uvStraightDoneFillBackup')
            colors = {face: (completion_tint(rgba) if done else rgba)
                      for _, face, rgba in scene.face_colors_by_shape.get(shape, ())}
            colors = {face: rgba for face, rgba in colors.items()
                      if baseline.get(shape, {}).get(face) != rgba}
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
                        normal = (b-a) ^ (c-a)
                        if normal.length() <= 1e-15:
                            continue
                        normal.normalize()
                        offset = normal * max((b-a).length(), (c-a).length(),
                                              (c-b).length(), 1.) * 2e-5
                        face_points.extend(tuple(p + offset) for p in (a, b, c))
                    triangles[face] = face_points
                points.extend(triangles[face])
                # Opaque changed faces also erase the old CPV tint when a
                # distorted area returns towards neutral during the drag.
                values.extend([rgba] * len(triangles[face]))
            self.groups[shape] = (colors, points, values)
        active = set(scene.shapes)
        for shape in set(self.groups) - active:
            del self.groups[shape]
            changed = True
        if not changed:
            return False
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
        return True

    def clear(self):
        active = self.source is not None
        self.geometry.clear()
        self.groups.clear()
        self.source = None
        state.VIEWPORT_PAYLOAD = None
        if active:
            omui.M3dView.scheduleRefreshAllViews()
