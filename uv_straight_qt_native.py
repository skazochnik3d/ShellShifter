"""Owned transparent windows over Maya's native UV viewport (Maya 2025)."""
from pathlib import Path
import json
import math
import time
import uuid
from array import array
from copy import copy
from collections import deque
from types import SimpleNamespace
import maya.cmds as cmds
import maya.api.OpenMaya as om
import maya.api.OpenMayaRender as omr
import maya.OpenMaya as om1
import maya.OpenMayaUI as omui1
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid, wrapInstance
from uv_straight_overlay import ColorOverlay, ViewportEdgeOverlay
import uv_straight_bounds_state as bounds_state
from uv_straight_changes import ChangeTracker
from uv_straight_distortion import DistortionMesh, VectorGeometry, shell_neutral_scale
from uv_straight_colors import completion_tint
from uv_straight_live_preview import LiveViewportPreview


def triangle_scales(a, b, c, uv_a, uv_b, uv_c):
    """Principal UV/3D length ratios, including equal-area shear/stretch."""
    ab, ac = b-a, c-a
    length = ab.length()
    area2 = (ab ^ ac).length()
    if length <= 1e-15 or area2 <= 1e-15:
        return None
    x, y = (ab * ac) / length, area2 / length
    p = (uv_b[0]-uv_a[0]) / length
    r = (uv_b[1]-uv_a[1]) / length
    q = (uv_c[0]-uv_a[0]-p*x) / y
    s = (uv_c[1]-uv_a[1]-r*x) / y
    trace = p*p+q*q+r*r+s*s
    determinant = abs(p*s-q*r)
    high = math.sqrt(max(0., .5*(trace+math.sqrt(max(0., trace*trace-4*determinant*determinant)))))
    low = determinant / max(high, 1e-30)
    return max(low, 1e-15), max(high, 1e-15), area2*.5


class LivePayload:
    """Cache guide connectivity; read current UV positions without Mesh rebuilds."""
    def __init__(self, meshes, distortion_reference=None, geometry_reference=None):
        from uv_straight_native import build_payload
        import uv_straight as uv
        self.entries = []
        self.handles = [(om.MObjectHandle(mesh.path.node()),
                         om.MObjectHandle(mesh.path.transform())) for mesh in meshes]
        self.distortion_cache = {}
        self.distortion_face_indices = {}
        self.vector_geometry = {}
        self.shell_faces = []
        self.distortion_reference = (distortion_reference
                                     if distortion_reference is not None else {})
        self.capture_geometry = geometry_reference is not None
        self.geometry_reference = (geometry_reference
                                   if geometry_reference is not None else {})
        self.mesh_reference_keys = []
        self.reference_positions = []
        self.distortion_computes = 0
        self.distortion_hits = 0
        self.selected_ids = []
        self.selected_points = []
        self.viewport_face_colors = []
        for mesh in meshes:
            uuids = cmds.ls(mesh.name, uuid=True) or []
            self.mesh_reference_keys.append(
                ((uuids[0] if uuids else mesh.name), mesh.fn.numPolygons,
                 mesh.fn.numVertices, mesh.fn.numEdges, len(mesh.points)))
            indexed = None
            if not hasattr(mesh, 'distortion_faces'):
                indexed = copy(mesh)
                indexed.points = {i: i for i in mesh.points}
            counts, ids = mesh.fn.getAssignedUVs(mesh.uv_set)
            faces = getattr(mesh, 'distortion_faces', None)
            if faces is None:
                faces = []
                iterator = om.MItMeshPolygon(mesh.path)
                while not iterator.isDone():
                    try:
                        vertices = tuple(iterator.getVertices())
                        uv_ids = tuple(iterator.getUVIndex(i, mesh.uv_set)
                                       for i in range(len(vertices)))
                        _, triangle_ids = iterator.getTriangles()
                        local = {vertex: i for i, vertex in enumerate(vertices)}
                        triangles = tuple(tuple(local[triangle_ids[j+k]] for k in range(3))
                                          for j in range(0, len(triangle_ids), 3))
                        faces.append((iterator.index(), mesh.shell_ids[uv_ids[0]],
                                      uv_ids, vertices, triangles))
                    except RuntimeError:
                        pass  # An unmapped polygon must not hide the mapped shells.
                    iterator.next()
            by_shell = {}
            for face in faces:
                by_shell.setdefault(face[1], []).append(face)
            self.shell_faces.append(by_shell)
            if self.capture_geometry:
                geometry_key = (self.mesh_reference_keys[-1][0],
                                mesh.fn.numPolygons, mesh.fn.numVertices, mesh.fn.numEdges,
                                len(mesh.points), tuple(counts), tuple(ids),
                                hash(tuple(mesh.face_vertices)))
                reference = self.geometry_reference.get(mesh.name)
                if reference is None or reference[0] != geometry_key:
                    reference = (geometry_key, om.MPointArray(
                        mesh.fn.getPoints(om.MSpace.kWorld)))
                    self.geometry_reference[mesh.name] = reference
                self.reference_positions.append(reference[1])
            else:
                self.reference_positions.append(None)
            self.entries.append((mesh, tuple(counts), tuple(ids),
                                 uv._mark_data(mesh), build_payload([indexed]) if indexed else [], faces))
            marked = {i for group in uv._marks(mesh) for i in group['ids']}
            self.selected_ids.append(tuple(sorted(marked & mesh.explicit_selected)))

    def is_valid(self):
        return all(handle.isAlive() and handle.isValid()
                   for pair in self.handles for handle in pair)

    def read(self):
        if not self.is_valid():
            return None
        import uv_straight as uv
        result = []
        selected_points = []
        for entry_index, (mesh, counts, ids, marks, layout, _) in enumerate(self.entries):
            # History evaluation/CPV writes may replace the internal mesh data
            # while the DAG node stays alive. Never retain that function set.
            fn = mesh.fn = om.MFnMesh(mesh.path)
            if fn.currentUVSetName() != mesh.uv_set or uv._mark_data(mesh) != marks:
                return None
            current_counts, current_ids = fn.getAssignedUVs(mesh.uv_set)
            u, v = fn.getUVs(mesh.uv_set)
            if (len(u) != len(mesh.points) or tuple(current_counts) != counts or
                    tuple(current_ids) != ids):
                return None
            point = lambda i: (u[i], v[i])
            selected_points.extend(point(i) for i in self.selected_ids[entry_index])
            for color, segments, points in layout:
                result.append((color, [(point(a), point(b)) for a, b in segments],
                               [point(i) for i in points]))
        self.selected_points = selected_points
        return result

    def distortion(self, color_range=2.):
        result = self.distortion_payloads(color_range, include_triangles=False)
        return None if result is None else result[0]

    def distortion_payloads(self, color_range=2., include_triangles=True):
        if not self.is_valid():
            return None
        uv_result, viewport_result, face_colors = [], [], []
        for index, entry in enumerate(self.entries):
            mesh, counts, ids, marks, _, _ = entry
            fn = mesh.fn = om.MFnMesh(mesh.path)
            if fn.currentUVSetName() != mesh.uv_set or uv_marks(mesh) != marks:
                return None
            current_counts, current_ids = fn.getAssignedUVs(mesh.uv_set)
            u, v = fn.getUVs(mesh.uv_set)
            reference_key = self.mesh_reference_keys[index]
            if (fn.numPolygons != reference_key[1] or
                    fn.numVertices != reference_key[2] or
                    fn.numEdges != reference_key[3] or
                    len(u) != len(mesh.points) or tuple(current_counts) != counts or
                    tuple(current_ids) != ids):
                return None
            # Distortion describes the UV layout. Keep the mesh-space reference
            # fixed while vertices or transforms are edited, and only invalidate
            # this result when the UV coordinates themselves change.
            signature = (float(color_range), bool(include_triangles),
                         array('f', u).tobytes(), array('f', v).tobytes())
            cached = self.distortion_cache.get(index)
            if cached is not None and cached[0] == signature:
                result = cached[1]
                self.distortion_hits += 1
            else:
                positions = self.reference_positions[index]
                if positions is None:
                    positions = fn.getPoints(om.MSpace.kWorld)
                # A UV edit normally touches one shell. Keep other shells'
                # polygons/colors and their neutral references untouched.
                partial = (cached is not None and not include_triangles and
                           cached[0][:2] == signature[:2] and self.capture_geometry)
                if partial:
                    old_u, old_v = array('f'), array('f')
                    old_u.frombytes(cached[0][2])
                    old_v.frombytes(cached[0][3])
                    changed_shells = {mesh.shell_ids[i]
                                      for i, (a, b, c, d) in enumerate(zip(old_u, old_v, u, v))
                                      if a != c or b != d}
                    faces = [face for shell in sorted(changed_shells)
                             for face in self.shell_faces[index].get(shell, ())]
                    changed = self._calculate_distortion(
                        color_range, [(index, entry[:-1] + (faces,))], False,
                        (u, v, positions))
                    if changed is None:
                        return None
                    result = cached[1]
                    indices = self.distortion_face_indices[index]
                    for polygon, color in zip(changed[0], changed[2]):
                        offset = indices[color[1]]
                        if result[0][offset] != polygon:
                            result[0][offset] = polygon
                        if result[2][offset] != color:
                            result[2][offset] = color
                else:
                    result = self._calculate_distortion(color_range, [(index, entry)],
                                                        include_triangles,
                                                        (u, v, positions))
                if result is None:
                    return None
                if not partial:
                    self.distortion_face_indices[index] = {
                        item[1]: offset for offset, item in enumerate(result[2])}
                self.distortion_cache[index] = (signature, result)
                self.distortion_computes += 1
            uv_result.extend(result[0])
            viewport_result.extend(result[1])
            face_colors.extend(result[2])
        self.viewport_face_colors = face_colors
        return uv_result, viewport_result

    def _calculate_distortion(self, color_range, entries, include_triangles=True,
                              coordinates=None):
        """Return matching UV polygons and world-space VP2 triangles."""
        if not include_triangles and self.capture_geometry and coordinates is not None:
            uv_result, face_colors = [], []
            for mesh_index, entry in entries:
                if mesh_index not in self.vector_geometry:
                    self.vector_geometry[mesh_index] = VectorGeometry(
                        self.entries[mesh_index][-1], self.reference_positions[mesh_index])
                result = self.vector_geometry[mesh_index].calculate(
                    color_range, entry[-1], coordinates, self.distortion_reference,
                    self.mesh_reference_keys[mesh_index], entry[0].name)
                uv_result.extend(result[0])
                face_colors.extend(result[2])
            return uv_result, [], face_colors
        samples = []
        for mesh_index, (mesh, counts, ids, marks, _, faces) in entries:
            fn = mesh.fn
            if fn.currentUVSetName() != mesh.uv_set or uv_marks(mesh) != marks:
                return None
            if coordinates is None:
                current_counts, current_ids = fn.getAssignedUVs(mesh.uv_set)
                u, v = fn.getUVs(mesh.uv_set)
                if (len(u) != len(mesh.points) or tuple(current_counts) != counts or
                        tuple(current_ids) != ids):
                    return None
                positions = self.reference_positions[mesh_index]
            else:
                u, v, positions = coordinates
            for face_index, shell, face, vertices, triangle_indices in faces:
                world_area = 0.
                uv_area = 0.
                scales = []
                for a, b, c in triangle_indices:
                    tri_uv = [(u[face[i]], v[face[i]]) for i in (a, b, c)]
                    scale = triangle_scales(*(positions[vertices[i]] for i in (a, b, c)), *tri_uv)
                    if scale is None:
                        continue
                    low, high, area = scale
                    world_area += area
                    uv_area += area*low*high
                    scales.append((low, high))
                if world_area <= 1e-15:
                    continue
                density = math.sqrt(max(uv_area, 1e-30) / world_area)
                samples.append(((self.mesh_reference_keys[mesh_index], shell),
                                mesh.name, face_index,
                                [(u[i], v[i]) for i in face], vertices,
                                positions, density, world_area, scales, triangle_indices))
        if not samples:
            return [], [], []
        # Use the same current-shell baseline as the vector path. Historical
        # UV size must not tint a repaired shell, and a local edit should not
        # move the baseline set by the unchanged majority of its surface.
        shell_samples = {}
        for key, _, _, _, _, _, density, weight, _, _ in samples:
            shell_samples.setdefault(key, []).append((density, weight))
        for key, values in shell_samples.items():
            self.distortion_reference[key] = {'neutral': shell_neutral_scale(
                [value[0] for value in values], [value[1] for value in values])}
        intensity = max(.01, float(color_range))
        dead_zone = math.log(1.02)
        full_distortion = math.log(2.)-dead_zone
        uv_result = []
        viewport_result = []
        face_colors = []
        for key, shape, face_index, polygon, vertices, positions, density, _, scales, triangle_indices in samples:
            neutral = self.distortion_reference[key]['neutral']
            deviations = [math.log(value/neutral) for pair in scales for value in pair]
            # Equal stretch/compression (pure skew) is shown as stretch/red.
            signed = max(deviations,
                         key=lambda value: (round(abs(value), 10), -value))
            deviation = max(0., abs(signed)-dead_zone)
            strength = min(1., deviation/full_distortion*intensity)
            neutral_rgb = (.48, .48, .48)
            target = (.08, .32, 1.) if signed > 0 else (1., .08, .03)
            rgb = tuple(neutral_rgb[i]*(1-strength)+target[i]*strength for i in range(3))
            # Zero distortion is fully transparent so Maya's own standard UV
            # face color remains exact, rather than being tinted by the layer.
            rgba = rgb + (.65*strength,)
            uv_result.append((rgba, polygon))
            face_colors.append((shape, face_index, rgb + (1.,)))
            if include_triangles and strength > 0.:
                triangles = []
                for ia, ib, ic in triangle_indices:
                    anchor, b, c = (positions[vertices[i]] for i in (ia, ib, ic))
                    normal = (b-anchor) ^ (c-anchor)
                    if normal.length() > 1e-15:
                        normal.normalize()
                    # A tiny outward offset avoids z-fighting with the shaded
                    # mesh while depth testing still hides back surfaces.
                    size = max((b-anchor).length(), (c-anchor).length(),
                               (c-b).length(), 1.)
                    offset = normal * (size * 2e-5)
                    triangles.extend(tuple(p + offset) for p in (anchor, b, c))
                if triangles:
                    viewport_result.append((rgba, triangles))
        return uv_result, viewport_result, face_colors


def uv_marks(mesh):
    import uv_straight as uv
    return uv._mark_data(mesh)


def selected_mesh_shapes(with_paths=False):
    """Resolve only selected DAG branches; never enumerate the scene's meshes."""
    selection = om.MGlobal.getActiveSelectionList()
    roots = set()
    shapes = set()
    paths = []
    seen = set()
    for index in range(selection.length()):
        try:
            path = selection.getDagPath(index)
            name = path.fullPathName()
            if name in seen:
                continue
            seen.add(name)
            if path.node().hasFn(om.MFn.kMesh):
                if om.MFnDagNode(path).isIntermediateObject:
                    continue
                visible = path.isVisible()
                paths.append((path, visible))
                if visible:
                    shapes.add(name)
            else:
                roots.add(name)
        except (RuntimeError, TypeError):
            continue
    if roots:
        for shape in cmds.ls(list(roots), dag=True, type='mesh', long=True,
                             noIntermediate=True) or []:
            try:
                item = om.MSelectionList()
                item.add(shape)
                path = item.getDagPath(0)
                visible = path.isVisible()
                paths.append((path, visible))
                if visible:
                    shapes.add(shape)
            except RuntimeError:
                continue
    result = tuple(sorted(shapes))
    return (result, paths) if with_paths else result


def selected_guide_meshes(shapes):
    """Build guides only on marked meshes; avoid flattening the scene's UVs."""
    import uv_straight as uv
    meshes = []
    for shape in shapes:
        path = om.MSelectionList().add(shape).getDagPath(0)
        mesh = uv.Mesh(path)
        mesh.selected = set(mesh.points)
        meshes.append(mesh)
    selection = om.MGlobal.getActiveSelectionList()
    for index in range(selection.length()):
        try:
            path, component = selection.getComponent(index)
            if component.isNull() or not component.hasFn(om.MFn.kMeshMapComponent):
                continue
            indices = om.MFnSingleIndexedComponent(component)
            for mesh in meshes:
                if mesh.path.node() == path.node():
                    mesh.explicit_selected.update(mesh.points if indices.isComplete else indices.getElements())
        except (RuntimeError, TypeError):
            continue
    return meshes


class SceneDistortion:
    """Live selected-mesh cache, with bounded inactive entries for reselection."""
    def __init__(self, reference, geometry_reference=None, cache_mesh_limit=2048,
                 cache_face_limit=250000):
        self.reference = reference
        self.geometry_reference = (geometry_reference
                                   if geometry_reference is not None else {})
        self.meshes = {}
        self.shapes = ()
        self.viewport_face_colors = []
        self.face_colors_by_shape = {}
        self.distortion_computes = 0
        self.distortion_hits = 0
        self.uv_polygons = {}
        self.errors = {}
        self.rebuilt = False
        self.rebuilt_shapes = set()
        self.updated_shapes = set()
        self.processing = False
        self.pending_shapes = deque()
        self.pending_set = set()
        self.ready_shapes = set()
        self.cache_mesh_limit = max(1, int(cache_mesh_limit))
        self.cache_face_limit = max(1, int(cache_face_limit))

    def update_scope(self, shapes=None):
        shapes = selected_mesh_shapes() if shapes is None else tuple(shapes)
        changed = shapes != self.shapes
        self.shapes = shapes
        if changed:
            active = set(shapes)
            self.ready_shapes.intersection_update(active)
            self.pending_shapes = deque(shape for shape in self.pending_shapes if shape in active)
            self.pending_set.intersection_update(active)
        self._prune_cache()
        return changed

    def invalidate_history(self):
        # Undo callbacks must not query the DG. Retain the bounded CPU cache,
        # but require fresh handle/topology/UV validation before publishing it.
        self.ready_shapes.clear()
        self.pending_shapes.clear()
        self.pending_set.clear()
        self.processing = False
        self.viewport_face_colors = []

    def _drop(self, shape):
        cached = self.meshes.pop(shape, None)
        self.uv_polygons.pop(shape, None)
        self.face_colors_by_shape.pop(shape, None)
        self.geometry_reference.pop(shape, None)
        self.ready_shapes.discard(shape)
        if cached is not None:
            # Each shell reference belongs to this payload. Avoid scanning
            # every scene reference once for every mesh being evicted.
            for mesh_key, shells in zip(cached.mesh_reference_keys, cached.shell_faces):
                for shell in shells:
                    self.reference.pop((mesh_key, shell), None)

    def _prune_cache(self):
        active = set(self.shapes)
        face_count = sum(len(cached.entries[0][-1]) for cached in self.meshes.values())
        for shape in list(self.meshes):
            cached = self.meshes[shape]
            if shape not in active and (not cached.is_valid() or
                    len(self.meshes) > self.cache_mesh_limit or
                    face_count > self.cache_face_limit):
                face_count -= len(cached.entries[0][-1])
                self._drop(shape)

    def _build(self, shape):
        selection = om.MSelectionList()
        selection.add(shape)
        mesh = DistortionMesh(selection.getDagPath(0))
        return LivePayload([mesh], self.reference, self.geometry_reference)

    def distortion_payloads(self, color_range, dirty_shapes=None, budget_ms=None):
        self.rebuilt = False
        self.rebuilt_shapes.clear()
        self.updated_shapes.clear()
        self.errors = {}
        for shape in self.shapes:
            if (dirty_shapes is None or shape in dirty_shapes or shape not in self.meshes) and shape not in self.pending_set:
                self.pending_shapes.append(shape)
                self.pending_set.add(shape)
        started = time.perf_counter()
        processed = 0
        while self.pending_shapes:
            shape = self.pending_shapes.popleft()
            self.pending_set.discard(shape)
            try:
                cached = self.meshes.get(shape)
                if cached is None:
                    cached = self._build(shape)
                    self.meshes[shape] = cached
                    self.rebuilt = True
                    self.rebuilt_shapes.add(shape)
                before_computes, before_hits = cached.distortion_computes, cached.distortion_hits
                result = cached.distortion_payloads(color_range, include_triangles=False)
                if result is None:
                    # Topology, UV sets or replaced DG objects invalidate only
                    # their own mesh, without rebuilding unrelated geometry.
                    cached = self._build(shape)
                    self.meshes[shape] = cached
                    self.rebuilt = True
                    self.rebuilt_shapes.add(shape)
                    before_computes = before_hits = 0
                    result = cached.distortion_payloads(color_range, include_triangles=False)
                computed = cached.distortion_computes > before_computes
                self.distortion_computes += cached.distortion_computes - before_computes
                self.distortion_hits += cached.distortion_hits - before_hits
                if result is not None:
                    if computed or shape not in self.uv_polygons:
                        self.uv_polygons[shape] = result[0]
                        self.face_colors_by_shape[shape] = list(cached.viewport_face_colors)
                        self.updated_shapes.add(shape)
                    if shape not in self.ready_shapes:
                        self.updated_shapes.add(shape)
                        self.ready_shapes.add(shape)
                # Moving the entry to the end keeps recently used meshes warm.
                self.meshes.pop(shape, None)
                self.meshes[shape] = cached
                self.errors.pop(shape, None)
            except (RuntimeError, ValueError) as error:
                # An unmapped/deleted mesh must not hide other scene meshes.
                self._drop(shape)
                self.errors[shape] = str(error)
            processed += 1
            # Warm validation can be very cheap. Limit the following native
            # color-set creation/writes as well as the calculation time.
            if budget_ms is not None and (processed >= 32 or
                    (time.perf_counter()-started)*1000 >= budget_ms):
                break
        self.processing = bool(self.pending_shapes)
        self._prune_cache()
        polygons, face_colors = [], []
        for shape in self.shapes:
            if shape not in self.ready_shapes:
                continue
            polygons.extend(self.uv_polygons.get(shape, ()))
            face_colors.extend(self.face_colors_by_shape.get(shape, ()))
        self.viewport_face_colors = face_colors
        return polygons, []

    def selected_uv_payload(self):
        """UV preview follows visible selected meshes/shells, never scene scope."""
        selection = om.MGlobal.getActiveSelectionList()
        scopes = {}
        object_roots = set()
        for index in range(selection.length()):
            try:
                path, component = selection.getComponent(index)
            except (RuntimeError, TypeError):
                try:
                    path, component = selection.getDagPath(index), om.MObject.kNullObj
                except (RuntimeError, TypeError):
                    continue  # Materials and other non-DAG selections.
            owner = path.fullPathName()
            if component.isNull():
                object_roots.add(owner)
                continue
            cached = self.meshes.get(owner)
            if cached is None:
                # Maya may select a different DAG instance of the same mesh.
                owner = next((shape for shape, current in self.meshes.items()
                              if current.is_valid() and
                              current.entries[0][0].path.node() == path.node()), None)
                cached = self.meshes.get(owner)
            if not path.isVisible():
                continue
            if owner not in self.ready_shapes or cached is None or (owner in scopes and scopes[owner] is None):
                continue
            mesh = cached.entries[0][0]
            try:
                indices = om.MFnSingleIndexedComponent(component)
                if indices.isComplete:
                    scopes[owner] = None
                    continue
                ids = indices.getElements()
                chosen = scopes.setdefault(owner, set())
                if component.hasFn(om.MFn.kMeshMapComponent):
                    chosen.update(mesh.shell_ids[i] for i in ids if i < len(mesh.shell_ids))
                elif component.hasFn(om.MFn.kMeshEdgeComponent):
                    faces = self._face_shells(cached)
                    iterator = om.MItMeshEdge(mesh.path)
                    for edge in ids:
                        iterator.setIndex(edge)
                        chosen.update(faces[face] for face in iterator.getConnectedFaces()
                                      if face in faces)
                elif component.hasFn(om.MFn.kMeshPolygonComponent):
                    faces = self._face_shells(cached)
                    chosen.update(faces[i] for i in ids if i in faces)
                elif component.hasFn(om.MFn.kMeshVertComponent):
                    if not hasattr(cached, 'vertex_shells'):
                        cached.vertex_shells = {}
                    faces = self._face_shells(cached)
                    iterator = om.MItMeshVertex(mesh.path)
                    for vertex in ids:
                        if vertex not in cached.vertex_shells:
                            iterator.setIndex(vertex)
                            cached.vertex_shells[vertex] = {
                                faces[face] for face in iterator.getConnectedFaces()
                                if face in faces}
                    chosen.update(shell for vertex in ids
                                  for shell in cached.vertex_shells.get(vertex, ()))
            except (RuntimeError, TypeError, IndexError):
                continue
        # Resolve groups once per active mesh. The former selection x cache
        # walk was quadratic when thousands of objects were selected.
        if object_roots:
            for shape in self.shapes:
                if shape not in self.ready_shapes:
                    continue
                cached = self.meshes.get(shape)
                if cached is None or not cached.is_valid():
                    continue
                mesh_path = cached.entries[0][0].path
                paths = (om.MDagPath.getAllPathsTo(mesh_path.node())
                         if mesh_path.isInstanced() else (mesh_path,))
                for candidate in paths:
                    if not candidate.isVisible():
                        continue
                    ancestor = candidate.fullPathName()
                    while ancestor:
                        if ancestor in object_roots:
                            scopes[shape] = None
                            break
                        ancestor = ancestor.rsplit('|', 1)[0]
                    if shape in scopes and scopes[shape] is None:
                        break
        result = []
        self.selected_uv_groups = {}
        for shape, shells in scopes.items():
            cached = self.meshes[shape]
            if not cached.is_valid():
                continue
            polygons = self.uv_polygons.get(shape, ())
            if shells is None:
                result.extend(polygons)
                self.selected_uv_groups[shape] = polygons
            elif shells:
                key = (id(polygons), cached.distortion_computes, frozenset(shells))
                saved = getattr(cached, 'uv_selection_cache', None)
                if shells.issuperset(cached.shell_faces[0]):
                    chosen = polygons
                elif saved is not None and saved[0] == key:
                    chosen = saved[1]
                else:
                    faces = self._face_shells(cached)
                    chosen = [polygon for polygon, (_, face, _) in
                              zip(polygons, cached.viewport_face_colors) if faces.get(face) in shells]
                    cached.uv_selection_cache = (key, chosen)
                result.extend(chosen)
                self.selected_uv_groups[shape] = chosen
        return result

    @staticmethod
    def _face_shells(cached):
        if not hasattr(cached, 'face_shells'):
            cached.face_shells = {face: shell for face, shell, *_ in cached.entries[0][-1]}
        return cached.face_shells


class QtAnnotations(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.editors = {}
        self.context_name = None
        self.context = None
        self.editor_contexts = {}
        self.retired_contexts = set()
        self.bound_viewport = None
        self.error = ''
        self.payload = []
        self.selected_pin_points = []
        self.pins_enabled = True
        import sys
        pick_module = sys.modules.get('uv_straight_pick')
        pick_session = getattr(pick_module, '_session', None)
        self.picker_active = bool(pick_session is not None and pick_session.active)
        self.distortion_payload = []
        self.viewport_distortion_payload = []
        self.viewport_override_payload = None
        self.viewport_override_owner = None
        self.check_uv_payload = []
        self.check_world_segments = []
        self.check_overlay_owner = None
        self.model_overlays = {}
        self.model_view_dirty = False
        self.viewport_helper = None
        self.viewport_helper_uuid = None
        self.viewport_ready = False
        self.scene_callbacks = []
        self.marked_groups = 0
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.poll)
        self.view_timer = QtCore.QTimer(self)
        self.view_timer.setInterval(16)
        self.view_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.view_timer.timeout.connect(self.update_views)
        self.last_bounds = None
        self.view_dirty = True
        self.changes = ChangeTracker(self._retire_layers_for_history)
        self.history_revision = self.changes.history_revision
        self.stopped = False
        self.refreshing = False
        self.scene_suspended = False
        self.resume_pending = False
        self.payload_builds = 0
        self.selection_key = None
        self.scope_shapes = ()
        self.scope_paths = []
        self.guide_presence = {}
        self.live_payload = None
        self.coordinate_updates = 0
        self.distortion_enabled = (bool(cmds.optionVar(query='uvStraightDistortion'))
                                   if cmds.optionVar(exists='uvStraightDistortion') else False)
        self.distortion_range = (float(cmds.optionVar(query='uvStraightDistortionRange'))
                                 if cmds.optionVar(exists='uvStraightDistortionRange') else 2.)
        self.distortion_reference = {}
        self.distortion_geometry_reference = {}
        self.scene_distortion = SceneDistortion(self.distortion_reference,
                                                self.distortion_geometry_reference)
        self.distortion_clock = QtCore.QElapsedTimer()
        self.distortion_clock.start()
        self.force_distortion = True
        self.distortion_pending = False
        self.distortion_interval = 16
        self.distortion_last_ms = 0.
        self.viewport_error = ''
        self.viewport_color_fingerprint = None
        self.viewport_dirty_shapes = set()
        self.viewport_display_plugs = {}
        self.distortion_invalid_shapes = set()
        self.distortion_all_dirty = True
        self.viewport_color_shapes = set()
        self.viewport_display = {}
        self.viewport_face_cache = {}
        self.viewport_recovery_pending = True
        self.uv_dragging = False
        self.live_viewport_preview = LiveViewportPreview()
        self.uv_drag_clock = QtCore.QElapsedTimer()
        self.uv_drag_clock.start()
        self.uv_drag_interval = 33
        self.uv_input_revision = 0
        self.uv_read_revision = 0
        self.viewport_color_pending = False
        self.viewport_publish_after = 0.
        self.viewport_display_clock = QtCore.QElapsedTimer()
        self.viewport_display_clock.start()
        self.uv_error = ''
        self.editor_key = ()
        self.force_viewport_color = True
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.shutdown)

    def start(self, open_uv_editor=True):
        from uv_straight_native import _find_editors
        if open_uv_editor and not _find_editors():
            import maya.mel as mel
            mel.eval('texturePanelShow()')
        self.refresh()
        self.timer.start()
        self.view_timer.start()

    def _install_scene_callbacks(self):
        # QTimer work runs on Maya's main event loop and is naturally paused by
        # file I/O. Python scene callbacks are intentionally forbidden here:
        # Maya autosave can restore model-editor DG state while callbacks run.
        return

    def _suspend_viewport(self, *unused):
        self.scene_suspended = True
        self.live_payload = None
        self.viewport_override_payload = None
        self.viewport_override_owner = None
        self.timer.stop()
        self.view_timer.stop()
        # Keep both the draw provider and its runtime node alive during save.
        # The node can be a member of Maya's view-selected set. Deleting it
        # from kBeforeSave leaves that set restoring a dead DG member and can
        # crash in TmodelEditor::restoreViewSelectedSetMembers.
        self.live_viewport_preview.clear()
        self.clear_check_overlay()

    def _resume_viewport(self, *unused):
        if self.stopped or self.resume_pending:
            return
        self.resume_pending = True
        QtCore.QTimer.singleShot(0, self._finish_scene_change)

    def _finish_scene_change(self):
        self.resume_pending = False
        self.scene_suspended = False
        if self.stopped:
            return
        self.force_distortion = True
        self.changes.invalidate()
        self.view_dirty = True
        self.refresh()
        self.timer.start()
        self.view_timer.start()

    def _load_viewport_plugin(self):
        name = 'uv_straight_live_draw_plugin'
        expected = '1.4.114'
        from uv_straight_native import _no_undo
        loaded = cmds.pluginInfo(name, query=True, loaded=True)
        if loaded and cmds.pluginInfo(name, query=True, version=True) != expected:
            with _no_undo():
                for shape in cmds.ls(type='uvStraightLivePreviewShape', long=True) or []:
                    parent = cmds.listRelatives(shape, parent=True, fullPath=True) or []
                    cmds.delete(parent or shape)
            cmds.unloadPlugin(name)
            loaded = False
        if not loaded:
            cmds.loadPlugin(str(Path(__file__).with_name(name + '.py')), quiet=True)
        # The draw node is runtime-only and setDoNotWrite. Keep its provider
        # out of the scene's `requires` list as well.
        try:
            cmds.pluginInfo(name, edit=True, writeRequires=False)
        except RuntimeError:
            pass
        import uv_straight_live_draw_state as draw_state
        draw_state.VIEWPORT_DRAW_ERROR = ''
        self.viewport_ready = True

    def _ensure_viewport_helper(self):
        if self.viewport_helper and cmds.objExists(self.viewport_helper):
            return
        # A stopped/reloaded manager deliberately leaves the empty transient
        # node alive. Reuse it instead of creating another VP2 provider.
        existing = cmds.ls(type='uvStraightLivePreviewShape', long=True) or []
        if existing:
            self.viewport_helper = existing[0]
            uuids = cmds.ls(self.viewport_helper, uuid=True) or []
            self.viewport_helper_uuid = uuids[0] if uuids else None
            return
        from uv_straight_native import _no_undo
        with _no_undo():
            shape = cmds.createNode('uvStraightLivePreviewShape',
                                    name='shellShifterLivePreviewShape#', skipSelect=True)
            transform = (cmds.listRelatives(shape, parent=True, fullPath=True) or [None])[0]
            if transform:
                cmds.setAttr(transform + '.hiddenInOutliner', True)
            for node in (shape, transform):
                if node:
                    selection = om.MSelectionList()
                    selection.add(node)
                    om.MFnDependencyNode(selection.getDependNode(0)).setDoNotWrite(True)
            self.viewport_helper = shape
            uuids = cmds.ls(shape, uuid=True) or []
            self.viewport_helper_uuid = uuids[0] if uuids else None

    def _remove_viewport_helper(self):
        self.live_viewport_preview.clear()
        shape = self.viewport_helper
        expected_uuid = self.viewport_helper_uuid
        self.viewport_helper = None
        self.viewport_helper_uuid = None
        if not shape or not cmds.objExists(shape):
            return
        current = cmds.ls(shape, uuid=True) or []
        if expected_uuid and current and current[0] != expected_uuid:
            return
        from uv_straight_native import _no_undo
        with _no_undo():
            parent = cmds.listRelatives(shape, parent=True, fullPath=True) or []
            cmds.delete(parent or shape)

    def _publish_viewport_distortion(self):
        desired = self.viewport_override_payload
        if desired is None and self.distortion_enabled:
            desired = self.viewport_distortion_payload
        if QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton:
            self.viewport_color_pending = True
            self.viewport_publish_after = time.monotonic() + .016
            if (self.uv_dragging and self.viewport_ready and self.distortion_enabled and
                    self.viewport_override_payload is None):
                self.live_viewport_preview.update(self.scene_distortion,
                    self.viewport_face_cache, self.scene_distortion.updated_shapes)
            return
        if time.monotonic() < self.viewport_publish_after:
            self.viewport_color_pending = True
            return
        if self.viewport_recovery_pending:
            self._restore_viewport_colors()
        if desired is None:
            self.viewport_color_pending = False
            self._restore_viewport_colors()
            self.live_viewport_preview.clear()
            return
        if not desired:
            self.viewport_color_pending = False
            self._restore_viewport_colors()
            self.live_viewport_preview.clear()
            return
        check_display = self.force_viewport_color or self.viewport_display_clock.elapsed() >= 1000
        display_ready = self.viewport_display_ready() if check_display else True
        if (self.viewport_override_payload is None and self.distortion_enabled and
                not self.force_viewport_color and self.viewport_color_fingerprint is not None and
                display_ready):
            active = {shape for shape in self.scene_distortion.ready_shapes
                      if self.scene_distortion.face_colors_by_shape.get(shape)}
            missing = self.viewport_color_shapes-active
            if missing:
                self._restore_viewport_colors(missing)
            delta = [item for shape in self.viewport_dirty_shapes
                     for item in self.scene_distortion.face_colors_by_shape.get(shape, ())]
            if delta:
                self._apply_viewport_colors(delta, replace=False)
            self.viewport_dirty_shapes.clear()
            self.viewport_color_pending = False
            self.live_viewport_preview.clear()
            if check_display:
                self.viewport_display_clock.restart()
            return
        # Publish live values after the manipulator finishes. Writing mesh CPV
        # while dragging can force upstream evaluation and revert a UV edit.
        fingerprint = tuple(desired)
        if check_display and display_ready:
            self.viewport_display_clock.restart()
        if (fingerprint == self.viewport_color_fingerprint and
                display_ready):
            self.force_viewport_color = False
            self.viewport_color_pending = False
            self.live_viewport_preview.clear()
            return
        if check_display and not display_ready:
            self.force_viewport_color = True
        if fingerprint == self.viewport_color_fingerprint:
            self.force_viewport_color = True  # Display switches need repair.
        self._apply_viewport_colors(desired)
        self.viewport_color_fingerprint = fingerprint
        self.viewport_dirty_shapes.clear()
        self.force_viewport_color = False
        self.viewport_color_pending = False
        self.live_viewport_preview.clear()
        self.viewport_display_clock.restart()

    @staticmethod
    def _color_attrs():
        return ('displayColors', 'materialBlend', 'displayColorChannel',
                'displayColorAsGreyScale', 'displayRedColorChannel',
                'displayGreenColorChannel', 'displayBlueColorChannel')

    def viewport_display_ready(self):
        """Check display switches only; never walk mesh faces or recalculate UVs."""
        for shape, (color_set, blend) in self.viewport_display.items():
            try:
                cached = self._viewport_plugs(shape)
                handle, path, plugs = cached
                if (not handle.isValid() or not handle.isAlive() or
                        not plugs['displayColors'].asBool() or
                        plugs['materialBlend'].asInt() != blend or
                        plugs['displayColorChannel'].asString() != 'Diffuse' or
                        plugs['displayColorAsGreyScale'].asBool() or
                        any(not plugs['display'+channel+'ColorChannel'].asBool()
                            for channel in ('Red', 'Green', 'Blue')) or
                        om.MFnMesh(path).currentColorSetName() != color_set):
                    self.viewport_display_plugs.pop(shape, None)
                    return False
            except RuntimeError:
                return False
        return True

    def _viewport_plugs(self, shape):
        cached = self.viewport_display_plugs.get(shape)
        if (cached is None or not cached[0].isValid() or not cached[0].isAlive() or
                cached[1].fullPathName() != shape):
            selection = om.MSelectionList()
            selection.add(shape)
            path = selection.getDagPath(0)
            node = path.node()
            dependency = om.MFnDependencyNode(node)
            cached = (om.MObjectHandle(node), path,
                      {name: dependency.findPlug(name, False) for name in self._color_attrs()})
            self.viewport_display_plugs[shape] = cached
        return cached

    def _restore_viewport_colors(self, only=None):
        """Restore Maya mesh display state without any renderer callbacks."""
        attr = 'uvStraightViewportPreviewState'
        shapes = set(only or [])
        if only is None:
            if self.viewport_recovery_pending:
                shapes.update(cmds.ls('*.{}'.format(attr), objectsOnly=True, long=True) or [])
                self.viewport_recovery_pending = False
            shapes.update(self.viewport_color_shapes)
        if not shapes:
            self.viewport_color_fingerprint = None
            return
        from uv_straight_native import _no_undo
        with _no_undo():
            for shape in sorted(shapes):
                try:
                    _, path, plugs = self._viewport_plugs(shape)
                    dependency = om.MFnDependencyNode(path.node())
                    if not dependency.hasAttribute(attr):
                        continue
                    state = json.loads(dependency.findPlug(attr, False).asString())
                    sets = list(om.MFnMesh(path).getColorSetNames())
                    if state['set'] in sets:
                        cmds.polyColorSet(shape, delete=True, colorSet=state['set'])
                        sets.remove(state['set'])
                    if state.get('current') in sets:
                        cmds.polyColorSet(shape, currentColorSet=True,
                                          colorSet=state['current'])
                    for name in self._color_attrs():
                        value = state[name]
                        if isinstance(value, str):
                            plugs[name].setString(value)
                        elif isinstance(value, bool):
                            plugs[name].setBool(value)
                        else:
                            plugs[name].setInt(value)
                    cmds.deleteAttr(shape + '.' + attr)
                    if cmds.attributeQuery('uvStraightDoneFillBackup', node=shape, exists=True):
                        import uv_straight as uv
                        uv._done_fill(shape, True)
                    elif dependency.hasAttribute('uvStraightCheckerState'):
                        plugs['displayColors'].setBool(False)
                except (RuntimeError, ValueError, KeyError, TypeError):
                    pass
        self.viewport_color_shapes.difference_update(shapes)
        for shape in shapes:
            self.viewport_display.pop(shape, None)
            self.viewport_display_plugs.pop(shape, None)
            self.viewport_face_cache.pop(shape, None)
        self.viewport_color_fingerprint = None

    def _apply_viewport_colors(self, payload, replace=True):
        """Display preview colors through Maya's native mesh color sets."""
        grouped = {}
        for shape, face, rgba in payload:
            grouped.setdefault(shape, {})[int(face)] = tuple(float(v) for v in rgba)
        missing = self.viewport_color_shapes.difference(grouped) if replace else set()
        if missing:
            self._restore_viewport_colors(missing)
        attr = 'uvStraightViewportPreviewState'
        from uv_straight_native import _no_undo
        with _no_undo():
            for shape, faces in grouped.items():
                try:
                    _, path, plugs = self._viewport_plugs(shape)
                    fn = om.MFnMesh(path)
                    dependency = om.MFnDependencyNode(path.node())
                except (RuntimeError, ValueError):
                    continue
                if (self.viewport_override_owner is None and
                        dependency.hasAttribute('uvStraightDoneFillBackup')):
                    faces = {face: completion_tint(rgba) for face, rgba in faces.items()}
                if (not self.force_viewport_color and
                        self.viewport_face_cache.get(shape) == faces):
                    continue
                sets = list(fn.getColorSetNames())
                if not dependency.hasAttribute(attr):
                    state = {name: (plugs[name].asString() if name == 'displayColorChannel' else
                                    plugs[name].asInt() if name == 'materialBlend' else
                                    plugs[name].asBool()) for name in self._color_attrs()}
                    state['current'] = fn.currentColorSetName() if sets else None
                    state['set'] = 'uvStraightPreview_' + uuid.uuid4().hex[:12]
                    cmds.addAttr(shape, longName=attr, dataType='string', hidden=True)
                    cmds.setAttr(shape + '.' + attr, json.dumps(state), type='string')
                else:
                    state = json.loads(dependency.findPlug(attr, False).asString())
                write_colors = (self.viewport_face_cache.get(shape) != faces or
                                state['set'] not in sets)
                if state['set'] not in sets:
                    cmds.polyColorSet(shape, create=True, colorSet=state['set'],
                                      representation='RGBA')
                cmds.polyColorSet(shape, currentColorSet=True, colorSet=state['set'])
                if write_colors:
                    selection = om.MSelectionList()
                    selection.add(shape)
                    fn = om.MFnMesh(selection.getDagPath(0))
                    colors, face_ids = om.MColorArray(), om.MIntArray()
                    previous = (self.viewport_face_cache.get(shape, {})
                                if state['set'] in sets else {})
                    changed_faces = {face: rgba for face, rgba in faces.items()
                                     if previous.get(face) != rgba}
                    for face in sorted(changed_faces):
                        if face < 0 or face >= fn.numPolygons:
                            continue
                        rgba = changed_faces[face]
                        colors.append(om.MColor(rgba))
                        face_ids.append(face)
                    if colors:
                        fn.setFaceColors(colors, face_ids)
                    self.viewport_face_cache[shape] = dict(faces)
                plugs['displayColors'].setBool(True)
                # Modulate2X keeps the UV checker visible below distortion:
                # a neutral .5 CPV leaves the material unchanged while red or
                # blue CPV tints it. Explicit overlays such as Texel Density
                # continue to replace the material with their exact colors.
                checker = dependency.hasAttribute('uvStraightCheckerState')
                blend = 6 if checker and self.viewport_override_owner is None else 0
                plugs['materialBlend'].setInt(blend)
                self.viewport_display[shape] = (state['set'], blend)
                plugs['displayColorChannel'].setString('Diffuse')
                plugs['displayColorAsGreyScale'].setBool(False)
                for channel in ('Red', 'Green', 'Blue'):
                    plugs['display' + channel + 'ColorChannel'].setBool(True)
        if replace:
            self.viewport_color_shapes = set(grouped)
        else:
            self.viewport_color_shapes.update(grouped)

    def set_viewport_override(self, payload, owner):
        self.live_viewport_preview.clear()
        self.viewport_override_payload = list(payload)
        self.viewport_override_owner = str(owner)
        self.force_viewport_color = True
        self._safe_publish_viewport()

    def clear_viewport_override(self, owner=None):
        if owner is not None and owner != self.viewport_override_owner:
            return
        self.viewport_override_payload = None
        self.viewport_override_owner = None
        self.force_viewport_color = True
        self._safe_publish_viewport()

    def set_check_overlay(self, uv_payload, world_segments, owner):
        self.check_uv_payload = list(uv_payload)
        self.check_world_segments = list(world_segments)
        self.check_overlay_owner = str(owner)
        self.model_view_dirty = True
        self.changes.invalidate()
        self.refresh()
        self._sync_check_viewports()

    def clear_check_overlay(self, owner=None):
        if owner is not None and owner != self.check_overlay_owner:
            return
        self.check_uv_payload = []
        self.check_world_segments = []
        self.check_overlay_owner = None
        for panel in list(self.model_overlays):
            self._remove_model_overlay(panel)
        for layer in self.editors.values():
            if isValid(layer):
                layer.set_payload(self.payload)

    def _sync_viewport_isolation(self):
        # The empty runtime provider stays alive during file I/O; never
        # delete a DAG member from a save/Undo notification.
        if not self.viewport_ready or not self.viewport_helper:
            return
        if QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton:
            return
        if not cmds.objExists(self.viewport_helper):
            self.viewport_ready = False
            return
        isolated = {}
        for panel in cmds.getPanel(type='modelPanel') or []:
            if cmds.isolateSelect(panel, query=True, state=True):
                group = cmds.isolateSelect(panel, query=True, viewObjects=True)
                members = (cmds.sets(group, query=True) or []) if group else []
                roots = {item.split('.', 1)[0] for item in
                         (cmds.ls(members, long=True) or [])} if members else set()
                allowed = set()
                for shape in self.scope_shapes:
                    owner = shape
                    while owner:
                        if owner in roots:
                            allowed.add(shape)
                            break
                        owner = owner.rsplit('|', 1)[0]
                isolated[panel] = allowed
                if group and not cmds.sets(self.viewport_helper, isMember=group):
                    from uv_straight_native import _no_undo
                    with _no_undo():
                        cmds.sets(self.viewport_helper, add=group)
        self.live_viewport_preview.panel_shapes = isolated

    def _safe_publish_viewport(self):
        # A VP2 failure must not erase the independent UV-editor overlay.
        self.changes.previewing = True
        try:
            self._publish_viewport_distortion()
            self._sync_viewport_isolation()
            self.viewport_error = ''
        except (RuntimeError, ValueError) as error:
            self.viewport_error = str(error)
        finally:
            self.changes.previewing = False

    def bounds(self):
        if self.scene_suspended or self.stopped:
            raise ValueError('UV overlay suspended during scene changes')
        # Only a registered, once-activated context is safe to query. A raw
        # MPxTexContext() has no backing Maya tool and can crash viewRect().
        if self.bound_viewport is None or not isValid(self.bound_viewport):
            raise ValueError('UV viewport was closed; waiting for a live editor')
        values = [om1.MScriptUtil() for _ in range(4)]
        for value in values:
            value.createFromDouble(0.)
        pointers = [value.asDoublePtr() for value in values]
        self.context.viewRect(*pointers)
        bounds = tuple(om1.MScriptUtil.getDouble(p) for p in pointers)
        left, right, bottom, top = bounds
        if not all(math.isfinite(v) for v in bounds) or right <= left or top <= bottom:
            raise ValueError('UV viewport projection is not ready')
        return bounds

    def _bind(self, editor):
        previous_tool = cmds.currentCtx()
        previous_focus = cmds.getPanel(withFocus=True)
        selection = cmds.ls(selection=True, long=True) or []
        highlighted = cmds.ls(hilite=True, long=True) or []
        modes = {mode: cmds.selectMode(query=True, **{mode: True})
                 for mode in ('object', 'component', 'hierarchical')}
        mask_flags = ('meshComponents', 'polymeshVertex', 'polymeshEdge',
                      'polymeshFace', 'polymeshUV', 'polymeshVtxFace',
                      'polymeshFreeEdge', 'meshUVShell')
        masks = [(object_mask, flag, cmds.selectType(
                    query=True, **dict({flag: True}, **({'objectComponent': True} if object_mask else {}))))
                 for object_mask in (False, True) for flag in mask_flags]
        # A viewport-only texel preview does not need a UV tool context.
        stored_context = self.editor_contexts.get(editor)
        if stored_context is not None:
            self.context_name, self.context = stored_context
        elif (self.context_name is None or self.editor_contexts or
              self.context_name in self.retired_contexts):
            if not cmds.pluginInfo('uv_straight_bounds_plugin', query=True, loaded=True):
                cmds.loadPlugin(str(Path(__file__).with_name('uv_straight_bounds_plugin.py')), quiet=True)
            self.context_name = cmds.uvStraightBoundsContext()
            self.context = bounds_state.CONTEXT
        self.editor_contexts[editor] = (self.context_name, self.context)
        # Maya initializes the context's UV editor reference on activation.
        # Afterwards viewRect continues to follow pan/zoom while standard
        # Maya tools remain active. Never add a helper to active selection.
        from uv_straight_native import _no_undo
        with _no_undo():
            try:
                cmds.setFocus(editor)
                cmds.setToolTo(self.context_name)
            finally:
                cmds.setToolTo(previous_tool)
                if previous_focus:
                    try:
                        cmds.setFocus(previous_focus)
                    except RuntimeError:
                        pass
                # MPxTexContext activation can change component masks even
                # after Maya restores the previous tool (notably UV Shell).
                for mode, active in modes.items():
                    if active:
                        if not cmds.selectMode(query=True, **{mode: True}):
                            cmds.selectMode(**{mode: True})
                        break
                for object_mask, flag, active in masks:
                    if active is not None:
                        options = {flag: bool(active)}
                        if object_mask:
                            options['objectComponent'] = True
                        # Setting an unchanged mask still broadcasts native
                        # selection/UI updates. On a cold UV Editor this can
                        # repeat expensive editor/viewport initialization.
                        current = cmds.selectType(query=True, **dict(
                            {flag: True}, **({'objectComponent': True} if object_mask else {})))
                        if current != active:
                            cmds.selectType(**options)
                if (cmds.ls(hilite=True, long=True) or []) != highlighted:
                    current = cmds.ls(hilite=True, long=True) or []
                    if current:
                        cmds.hilite(current, unHilite=True)
                    if highlighted:
                        cmds.hilite(highlighted, replace=True)
                if (cmds.ls(selection=True, long=True) or []) != selection:
                    if selection:
                        cmds.select(selection, replace=True)
                    else:
                        cmds.select(clear=True)
        self.last_bounds = self.bounds()

    def _attach(self, editor):
        control = cmds.textureWindow(editor, query=True, control=True)
        pointer = omui1.MQtUtil.findControl(control) if control else None
        if not pointer:
            raise ValueError('UV viewport widget is not ready')
        root = wrapInstance(int(pointer), QtWidgets.QWidget)
        widgets = [root] + root.findChildren(QtWidgets.QWidget)
        ports = [w for w in widgets if w.metaObject().className() == 'QmayaGLWidget']
        if len(ports) != 1:
            raise ValueError('Expected one native UV viewport; found {}'.format(len(ports)))
        self.bound_viewport = ports[0]
        self._bind(editor)
        layer = ColorOverlay(ports[0], lambda: layer.uv_bounds)
        layer.uv_bounds = self.last_bounds
        layer.view_changed = self.invalidate_view
        layer.interaction_changed = self.uv_input
        layer.projection_changed = self.sync_uv_projection
        layer.editor_name = editor
        # Keep the wrapped root alive: PySide can invalidate child wrappers
        # when their temporary Python parent wrapper is collected.
        layer.editor_root = root
        self.editors[editor] = layer
        return layer

    def _remove(self, editor):
        layer = self.editors.pop(editor, None)
        context = self.editor_contexts.pop(editor, None)
        if context is not None:
            # May run inside Maya's Undo callback: defer native context
            # deletion to stop(), rather than issuing Maya UI commands here.
            self.retired_contexts.add(context[0])
        if layer is not None and isValid(layer):
            try:
                layer.shutdown()
                layer.deleteLater()
            except RuntimeError:
                pass  # Maya may already have destroyed the owning UV widget.

    def _attach_model_overlay(self, panel):
        control = cmds.modelPanel(panel, query=True, control=True)
        pointer = omui1.MQtUtil.findControl(control) if control else None
        if not pointer:
            raise ValueError('Model viewport widget is not ready')
        root = wrapInstance(int(pointer), QtWidgets.QWidget)
        widgets = [root] + root.findChildren(QtWidgets.QWidget)
        ports = [widget for widget in widgets
                 if widget.metaObject().className() == 'QmayaGLWidget']
        if len(ports) != 1:
            raise ValueError('Expected one model viewport; found {}'.format(len(ports)))
        overlay = ViewportEdgeOverlay(ports[0])
        overlay.editor_root = root
        overlay.view_changed = self.invalidate_model_view
        self.model_overlays[panel] = overlay
        return overlay

    def _remove_model_overlay(self, panel):
        overlay = self.model_overlays.pop(panel, None)
        if overlay is not None and isValid(overlay):
            overlay.shutdown()
            overlay.deleteLater()

    def invalidate_model_view(self):
        self.model_view_dirty = True

    def _project_model_segments(self, panel, overlay):
        view = omui1.M3dView()
        omui1.M3dView.getM3dViewFromModelPanel(panel, view)
        matrix = view.modelViewMatrix()
        view_width = max(1, view.portWidth())
        view_height = max(1, view.portHeight())
        sx = float(overlay.width()) / view_width
        sy = float(overlay.height()) / view_height
        result = []
        for first, second in self.check_world_segments:
            points = [om1.MPoint(*first), om1.MPoint(*second)]
            camera_points = [point * matrix for point in points]
            if any(point.z >= -1e-6 for point in camera_points):
                continue
            projected = []
            for point in points:
                x_value, y_value = om1.MScriptUtil(), om1.MScriptUtil()
                x_value.createFromInt(0)
                y_value.createFromInt(0)
                x_pointer, y_pointer = x_value.asShortPtr(), y_value.asShortPtr()
                view.worldToView(point, x_pointer, y_pointer)
                x = om1.MScriptUtil.getShort(x_pointer) * sx
                y = overlay.height() - om1.MScriptUtil.getShort(y_pointer) * sy
                projected.append(QtCore.QPointF(x, y))
            result.append(tuple(projected))
        overlay.set_segments(result)

    def _sync_check_viewports(self):
        if not self.check_world_segments:
            for panel in list(self.model_overlays):
                self._remove_model_overlay(panel)
            return
        panels = set(cmds.getPanel(type='modelPanel') or [])
        for panel in list(self.model_overlays):
            overlay = self.model_overlays[panel]
            if panel not in panels or not isValid(overlay) or not isValid(overlay.viewport):
                self._remove_model_overlay(panel)
        for panel in panels:
            try:
                overlay = self.model_overlays.get(panel)
                if overlay is None:
                    overlay = self._attach_model_overlay(panel)
                    self.model_view_dirty = True
                overlay.sync_geometry()
                if self.model_view_dirty:
                    self._project_model_segments(panel, overlay)
            except (RuntimeError, ValueError):
                self._remove_model_overlay(panel)
        self.model_view_dirty = False

    def _retire_layers_for_history(self):
        """Stop all native paint immediately when Maya reports Undo/Redo."""
        self.live_viewport_preview.clear()
        self.live_payload = None
        self.scene_distortion.invalidate_history()
        self.payload = []
        self.selected_pin_points = []
        self.distortion_payload = []
        self.viewport_distortion_payload = []
        self.viewport_color_fingerprint = None
        self.viewport_face_cache.clear()
        self.force_viewport_color = True
        self.check_uv_payload = []
        self.check_world_segments = []
        self.check_overlay_owner = None
        for panel in list(self.model_overlays):
            self._remove_model_overlay(panel)
        for editor in list(self.editors):
            self._remove(editor)

    def poll(self):
        if self.stopped or self.scene_suspended or self.refreshing:
            return
        if QtWidgets.QApplication.activePopupWidget() is not None:
            return
        if not self._history_ready():
            return
        # Maya camera navigation and component editing share the main UI
        # thread. Do not walk UV data or publish color sets until the gesture
        # ends; the next idle poll will pick up any UV edits.
        if QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton:
            return
        try:
            if any(not path.isValid() or path.isVisible() != visible for path, visible in self.scope_paths):
                self.changes.invalidate()
        except RuntimeError:
            self.changes.invalidate()
        if self.pins_enabled and self.scope_shapes and not self.changes.nodes:
            self.changes.invalidate()  # Discover the first newly assigned guide.
        started = time.perf_counter()
        self.refresh()
        if self.viewport_display_clock.elapsed() >= 1000:
            self._safe_publish_viewport()
        # Keep idle polling sparse; UV drags and explicit ShellShifter actions
        # schedule their own live refreshes.
        self.timer.setInterval(max(250, min(1200, int((time.perf_counter()-started)*3000))))

    def refresh(self):
        # Maya/Qt can deliver timer and paint events inside commands below.
        if self.stopped or self.scene_suspended or self.refreshing:
            return
        if QtWidgets.QApplication.activePopupWidget() is not None:
            return
        if not self._history_ready():
            return
        self.refreshing = True
        try:
            self._refresh_impl()
        finally:
            self.refreshing = False

    def _refresh_impl(self):
        if self.stopped:
            return
        from uv_straight_native import _find_editors
        import uv_straight as uv
        try:
            self.uv_error = ''
            try:
                editors = _find_editors()
            except (RuntimeError, ValueError) as error:
                editors = []
                self.uv_error = str(error)
            editor_key = tuple(editors)
            if editor_key != self.editor_key:
                self.editor_key = editor_key
                self.force_viewport_color = True
                self.changes.invalidate()
            for editor, layer in list(self.editors.items()):
                if editor not in editors or not isValid(layer) or not isValid(layer.viewport):
                    self._remove(editor)
            # Cheap fallback for selection changes missed by Maya callbacks.
            # Do not flatten components or walk mesh topology while idle.
            selection_key = tuple(cmds.ls(selection=True, long=True) or [])
            if selection_key != self.selection_key:
                self.selection_key = selection_key
                self.live_payload = None
                self.changes.selection_changed()
                self.payload = []
                self.selected_pin_points = []
            if self.changes.dirty or (self.distortion_pending and
                                      self.distortion_clock.elapsed() >= self.distortion_interval):
                if self.changes.scope_dirty:
                    self.scope_shapes, self.scope_paths = selected_mesh_shapes(with_paths=True) if (
                        self.distortion_enabled or self.pins_enabled or self.picker_active) else ((), [])
                selected_shapes = self.scope_shapes
                selected_shape_set = set(selected_shapes)
                if self.pins_enabled or self.picker_active:
                    check_marks = (selected_shapes if self.changes.all_dirty else
                                   (selected_shape_set.difference(self.guide_presence) |
                                    self.changes.dirty_shapes))
                    for shape in check_marks:
                        if shape in selected_shape_set:
                            self.guide_presence[shape] = cmds.attributeQuery(uv.MARK_ATTR, node=shape, exists=True)
                    self.guide_presence = {shape: self.guide_presence.get(shape, False) for shape in selected_shapes}
                guide_shapes = [shape for shape in selected_shapes if self.guide_presence.get(shape)]
                needs_guides = (self.pins_enabled or self.picker_active) and bool(guide_shapes)
                if needs_guides and self.live_payload is not None and not self.live_payload.entries:
                    self.live_payload = None
                current = self.live_payload.read() if self.live_payload is not None else None
                if current is None:
                    try:
                        meshes = selected_guide_meshes(guide_shapes) if needs_guides else []
                    except (ValueError, RuntimeError):
                        meshes = []
                    self.marked_groups = sum(len(uv._marks(mesh)) for mesh in meshes)
                    self.live_payload = LivePayload(meshes, self.distortion_reference)
                    current = self.live_payload.read()
                    self.payload_builds += 1
                self.payload = (current or []) if self.pins_enabled else []
                self.selected_pin_points = (list(self.live_payload.selected_points)
                                            if self.pins_enabled and current is not None else [])
                if not self.distortion_enabled:
                    self.distortion_payload = []
                    self.viewport_distortion_payload = []
                    self.distortion_pending = False
                else:
                    previous_shapes = set(self.scene_distortion.shapes)
                    scope_changed = self.scene_distortion.update_scope(selected_shapes)
                    self.distortion_all_dirty |= self.changes.all_dirty
                    self.distortion_invalid_shapes.update(selected_shape_set-previous_shapes)
                    self.distortion_invalid_shapes.update(self.changes.dirty_shapes)
                    if scope_changed:
                        self.force_viewport_color = True
                if self.distortion_enabled and (scope_changed or self.force_distortion or
                        self.distortion_clock.elapsed() >= self.distortion_interval):
                    started = time.perf_counter()
                    dirty_shapes = (None if self.distortion_all_dirty else
                                    set(self.distortion_invalid_shapes))
                    self.scene_distortion.distortion_payloads(
                        self.distortion_range, dirty_shapes,
                        budget_ms=6 if len(selected_shapes) > 16 else None)
                    self.viewport_dirty_shapes.update(self.scene_distortion.updated_shapes)
                    self.distortion_all_dirty = False
                    self.distortion_invalid_shapes.clear()
                    if self.scene_distortion.rebuilt:
                        for shape in self.scene_distortion.rebuilt_shapes:
                            self.viewport_face_cache.pop(shape, None)
                    self.distortion_last_ms = (time.perf_counter()-started)*1000.
                    self.viewport_distortion_payload = list(
                        self.scene_distortion.viewport_face_colors)
                    self.distortion_clock.restart()
                    # A slow initial build must not delay later cheap UV edits
                    # by 3x its duration (up to one second). Queue work already
                    # has a budget and camera gestures pause it independently.
                    self.distortion_interval = 16
                    self.force_distortion = False
                    self.distortion_pending = self.scene_distortion.processing
                elif self.distortion_enabled:
                    self.distortion_pending = True
                if self.distortion_enabled:
                    # Apply the selection filter even inside the calculation
                    # throttle so deselection clears UV ghosts immediately.
                    self.distortion_payload = self.scene_distortion.selected_uv_payload() if editors else []
                watched = {entry[0].name: entry[0] for entry in self.live_payload.entries}
                if self.pins_enabled or self.picker_active:
                    watched.update({path.fullPathName(): SimpleNamespace(path=path, name=path.fullPathName())
                                    for path, visible in self.scope_paths if visible})
                if self.distortion_enabled:
                    watched.update({shape: cached.entries[0][0] for shape, cached in
                                    self.scene_distortion.meshes.items()
                                    if shape in self.scene_distortion.ready_shapes})
                self.changes.watch(list(watched.values()))
                self.changes.clean()
                self.coordinate_updates += 1
                self._safe_publish_viewport()
            try:
                self._sync_viewport_isolation()
            except (RuntimeError, ValueError) as error:
                self.viewport_error = str(error)
            for editor in editors:
                try:
                    layer = self.editors.get(editor)
                    combined_payload = self.payload + self.check_uv_payload
                    if layer is None and (combined_payload or self.distortion_payload or
                                          self.picker_active):
                        layer = self._attach(editor)
                    if layer is not None:
                        layer.set_payload(combined_payload)
                        layer.set_selected_points(self.selected_pin_points)
                        layer.set_distortion(self.distortion_payload, groups=(
                            getattr(self.scene_distortion, 'selected_uv_groups', {})
                            if self.distortion_enabled else {}))
                        if not (combined_payload or self.distortion_payload or
                                self.picker_active):
                            self._remove(editor)
                except (RuntimeError, ValueError) as error:
                    self.uv_error = str(error)
                    self._remove(editor)
            if (self.distortion_enabled and self.editors and not self.viewport_ready and
                    not cmds.about(batch=True) and
                    QtWidgets.QApplication.mouseButtons() == QtCore.Qt.NoButton):
                # Prepare outside a mouse gesture. Rendering only receives
                # immutable buffers and never writes mesh colors during edits.
                try:
                    self._load_viewport_plugin()
                    self._ensure_viewport_helper()
                    self._sync_viewport_isolation()
                except (RuntimeError, ValueError) as error:
                    self.viewport_error = str(error)
                    self.viewport_ready = False
            self.error = ''
        except (RuntimeError, ValueError) as error:
            self.error = str(error)
            self.live_payload = None
            self.payload = []
            self.selected_pin_points = []
            self.distortion_payload = []
            self.marked_groups = 0
            for layer in self.editors.values():
                if isValid(layer):
                    layer.set_payload(self.check_uv_payload)
                    layer.set_selected_points([])
                    layer.set_distortion([])
            self._safe_publish_viewport()

    def set_pins(self, enabled):
        if self.pins_enabled != bool(enabled):
            self.live_payload = None
        self.pins_enabled = bool(enabled)
        self.changes.invalidate()
        self.refresh()

    def set_picker_active(self, enabled):
        """Keep an invisible UV viewport layer available for segment picking."""
        if self.picker_active != bool(enabled):
            self.live_payload = None
        self.picker_active = bool(enabled)
        self.changes.invalidate()
        self.refresh()

    def prepare_picker_view(self, layer):
        """Read current UV bounds before picking, including an empty overlay."""
        if self.stopped or self.scene_suspended or not self._history_ready():
            raise ValueError('UV Editor is updating after Undo/Redo. Try the click again.')
        if self.bound_viewport is not layer.viewport:
            editor = next((name for name, current in self.editors.items()
                           if current is layer), None)
            if editor is None:
                raise ValueError('UV viewport was replaced; waiting for the current editor.')
            self.bound_viewport = layer.viewport
            self._bind(editor)
        self.last_bounds = self.bounds()
        self.view_dirty = False

    def set_distortion(self, enabled, color_range=None):
        was_enabled = self.distortion_enabled
        self.distortion_enabled = bool(enabled)
        self.uv_dragging = False
        self.live_viewport_preview.clear()
        if not self.distortion_enabled or not was_enabled:
            self.distortion_reference.clear()
            self.distortion_geometry_reference.clear()
            self.scene_distortion = SceneDistortion(self.distortion_reference,
                                                    self.distortion_geometry_reference)
        if color_range is not None:
            self.distortion_range = max(.01, float(color_range))
        self.force_distortion = True
        self.force_viewport_color = True
        cmds.optionVar(intValue=('uvStraightDistortion', int(self.distortion_enabled)))
        cmds.optionVar(floatValue=('uvStraightDistortionRange', self.distortion_range))
        self.changes.invalidate()
        self.refresh()

    def invalidate_view(self):
        self.view_dirty = True

    def uv_input(self, event):
        if event.type() == QtCore.QEvent.MouseMove:
            if (event.buttons() & (QtCore.Qt.LeftButton | QtCore.Qt.MiddleButton) and
                    not event.modifiers() & QtCore.Qt.AltModifier):
                self.uv_input_revision += 1

    def _history_ready(self):
        if self.changes.history_revision != self.history_revision:
            self.history_revision = self.changes.history_revision
            # Retained payloads validate handles and UV topology with a fresh
            # MFnMesh before reuse, even when selection text did not change.
            self.live_payload = None
            self.scene_distortion.invalidate_history()
            self.payload = []
            self.selected_pin_points = []
            self.distortion_payload = []
            self.viewport_distortion_payload = []
            self.viewport_override_payload = None
            self.viewport_override_owner = None
            self.check_uv_payload = []
            self.check_world_segments = []
            self.check_overlay_owner = None
            for panel in list(self.model_overlays):
                self._remove_model_overlay(panel)
            for editor in list(self.editors):
                self._remove(editor)
            self.force_distortion = True
            self.view_dirty = True
        return not self.changes.history_settling()

    def update_views(self):
        if self.stopped or self.scene_suspended or self.refreshing:
            return
        if QtWidgets.QApplication.activePopupWidget() is not None:
            return
        if not self._history_ready():
            return
        try:
            mouse_buttons = QtWidgets.QApplication.mouseButtons()
            mouse_busy = mouse_buttons != QtCore.Qt.NoButton
            if self.check_world_segments and not mouse_busy:
                self._sync_check_viewports()
            # Poll during a drag even when Maya postpones dirty notifications.
            # Never schedule refresh from Paint: that creates a redraw loop.
            modifiers = QtWidgets.QApplication.keyboardModifiers()
            dragging = (bool(mouse_buttons & (QtCore.Qt.LeftButton | QtCore.Qt.MiddleButton)) and
                        not mouse_buttons & QtCore.Qt.RightButton and
                        not modifiers & QtCore.Qt.AltModifier)
            if dragging and mouse_buttons == QtCore.Qt.MiddleButton and not self.uv_dragging:
                tool = (cmds.currentCtx() or '').lower()
                dragging = any(name in tool for name in ('move', 'scale', 'rotate'))
            over_uv = False
            if mouse_busy:
                # A toolbar icon may leave focus in the UV Editor. Use the
                # widget under the pointer so palette clicks still stay cheap.
                hovered = QtWidgets.QApplication.widgetAt(QtGui.QCursor.pos())
                over_uv = any(isValid(layer) and isValid(layer.viewport) and
                               (hovered is layer.viewport or
                                (hovered is not None and layer.viewport.isAncestorOf(hovered)))
                               for layer in self.editors.values())
                dragging = dragging and (over_uv or self.uv_dragging)
            # Let UV drags update live, but never put scene-wide work in the
            # middle of model-view navigation or other mouse gestures.
            if mouse_busy and not dragging:
                if over_uv:
                    self._update_uv_projection(False)
                return
            if dragging:
                self.uv_dragging |= (self.uv_input_revision != self.uv_read_revision or
                                     bool(self.changes.dirty_shapes))
                # Maya can postpone mesh dirty callbacks until mouse-up.
                # Validate only active owners, with a frame-rate cap, without
                # invalidating topology or scanning all meshes in the scene.
                if (self.distortion_enabled and self.uv_dragging and
                        self.uv_drag_clock.elapsed() >= (
                            self.uv_drag_interval if self.uv_read_revision != self.uv_input_revision else 100)):
                    self.changes.dirty = True
                    self.changes.dirty_shapes.update(self.scope_shapes)
                    self.force_distortion = True
                    self.uv_read_revision = self.uv_input_revision
                    self.uv_drag_clock.restart()
            elif self.uv_dragging and not mouse_busy:
                self.uv_dragging = False
                if self.distortion_enabled:
                    # Catch the final mouse position even when the last
                    # movement arrived inside the frame-rate throttle.
                    self.changes.dirty_shapes.update(self.scope_shapes)
                    self.force_distortion = True
                    self.changes.dirty = True
            pending_due = (self.distortion_pending and
                           self.distortion_clock.elapsed() >= self.distortion_interval)
            if (self.viewport_color_pending and self.distortion_pending and not mouse_busy and
                    time.monotonic() >= self.viewport_publish_after):
                # Finish the final throttled UV edit before publishing colors,
                # including with no UV Editor and on large, slower selections.
                self.force_distortion = True
                self.changes.dirty = True
            changed = self.changes.dirty or pending_due
            if self.changes.dirty or pending_due:
                self.changes.dirty = True
                self.refresh()
            if self.viewport_color_pending and not mouse_busy:
                self._safe_publish_viewport()
            self._update_uv_projection(changed)
        except (RuntimeError, ValueError) as error:
            self.error = str(error)

    def _update_uv_projection(self, changed):
        # No context queries after a viewport's C++ widget was destroyed.
        layers = [layer for layer in self.editors.values()
                  if isValid(layer) and isValid(layer.viewport)
                  and layer.viewport.isVisible() and
                  (layer.payload or layer.distortion or self.picker_active)]
        if not layers:
            return
        # Never query MPxTexContext on an idle timer or from paintEvent.
        # Qt input/resize events invalidate the cached projection.
        if not (self.view_dirty or changed):
            return
        for layer in layers:
            self._sync_uv_layer_projection(layer, immediate=False)
        self.view_dirty = False

    def sync_uv_projection(self, layer):
        """Post-input/post-host-paint sync, without mesh reads or refresh()."""
        if (self.stopped or self.scene_suspended or self.refreshing or
                QtWidgets.QApplication.activePopupWidget() is not None):
            self.view_dirty = True
            return
        try:
            if self._history_ready():
                self._sync_uv_layer_projection(layer, immediate=True)
        except (RuntimeError, ValueError) as error:
            self.uv_error = str(error)

    def _sync_uv_layer_projection(self, layer, immediate=False):
        if (not isValid(layer) or layer.closing or not isValid(layer.viewport) or
                not layer.viewport.isVisible()):
            return
        editor = getattr(layer, 'editor_name', None)
        if editor is None:
            editor = next((name for name, current in self.editors.items() if current is layer), None)
        if editor is None or self.editors.get(editor) is not layer:
            return
        stored_context = self.editor_contexts.get(editor)
        if stored_context is None:
            return  # Never query an unregistered/unbound tex context.
        self.context_name, self.context = stored_context
        self.bound_viewport = layer.viewport
        bounds = self.bounds()
        previous = getattr(layer, 'uv_bounds', None)
        layer.uv_bounds = bounds  # Assign before synchronous painting.
        self.last_bounds = bounds
        layer.sync_geometry()
        if bounds != previous:
            if immediate:
                layer.repaint()  # Only cached coordinates/raster; no Maya reads.
            else:
                layer.update()

    def shutdown(self):
        self.stopped = True
        self.live_viewport_preview.clear()
        self.timer.stop()
        self.view_timer.stop()
        self.changes.stop()
        self._restore_viewport_colors()
        for panel in list(self.model_overlays):
            self._remove_model_overlay(panel)
        for callback in self.scene_callbacks:
            try:
                om.MMessage.removeCallback(callback)
            except RuntimeError:
                pass
        self.scene_callbacks = []
        for editor in list(self.editors):
            self._remove(editor)

    def stop(self):
        self.shutdown()
        try:
            QtWidgets.QApplication.instance().aboutToQuit.disconnect(self.shutdown)
        except (RuntimeError, TypeError):
            pass
        contexts = self.retired_contexts | {name for name, _ in self.editor_contexts.values()}
        if self.context_name:
            contexts.add(self.context_name)
        for name in contexts:
            if cmds.contextInfo(name, exists=True):
                if cmds.currentCtx() == name:
                    cmds.setToolTo('selectSuperContext')
                cmds.deleteUI(name)
        self.retired_contexts.clear()
        self.editor_contexts.clear()
        self.context_name = None
        self.context = None

    def snapshot(self):
        from uv_straight_native import _find_editors
        import uv_straight_live_draw_state as draw_state
        import uv_straight as uv
        layers = [layer for layer in self.editors.values() if isValid(layer)]
        return {'enabled': True, 'version': uv.VERSION, 'backend': 'qt_overlay',
                'pins_enabled': self.pins_enabled,
                'editors': list(self.editors),
                'draw_calls': sum(layer.draw_count for layer in layers),
                'draw_error': '; '.join(layer.error for layer in layers if layer.error),
                'uv_panels': {name: True for name in _find_editors()},
                'selected_mark_groups': self.marked_groups,
                'payload_groups': len(self.payload), 'bounds': self.last_bounds,
                'selected_pin_points': len(self.selected_pin_points),
                'payload_builds': self.payload_builds,
                'coordinate_updates': self.coordinate_updates,
                'distortion_enabled': self.distortion_enabled,
                'distortion_faces': len(self.distortion_payload),
                'viewport_distortion_faces': len(self.viewport_distortion_payload),
                'viewport_helper': self.viewport_helper or '',
                'viewport_plugin_loaded': bool(draw_state.PLUGIN_LOADED),
                'viewport_draw_calls': draw_state.VIEWPORT_DRAW_COUNT,
                'viewport_prepare_calls': draw_state.VIEWPORT_PREPARE_COUNT,
                'viewport_callback_calls': draw_state.VIEWPORT_CALLBACK_COUNT,
                'viewport_draw_error': draw_state.VIEWPORT_DRAW_ERROR,
                'viewport_error': self.viewport_error,
                'uv_error': self.uv_error,
                'viewport_color_pending': self.viewport_color_pending,
                'uv_drag_active': self.uv_dragging,
                'live_preview_faces': sum(len(item[0]) for item in self.live_viewport_preview.groups.values()),
                'distortion_range': self.distortion_range,
                'viewport_override': self.viewport_override_owner,
                'check_overlay': self.check_overlay_owner,
                'check_uv_groups': len(self.check_uv_payload),
                'check_world_edges': len(self.check_world_segments),
                'distortion_last_ms': self.distortion_last_ms,
                'distortion_meshes': len(self.scene_distortion.shapes),
                'distortion_cached_meshes': len(self.scene_distortion.meshes),
                'distortion_scope': 'selected',
                'distortion_mesh_computes': self.scene_distortion.distortion_computes,
                'distortion_cache_hits': self.scene_distortion.distortion_hits,
                'distortion_mesh_errors': dict(self.scene_distortion.errors),
                'active_tool': cmds.currentCtx(), 'error': self.error}
