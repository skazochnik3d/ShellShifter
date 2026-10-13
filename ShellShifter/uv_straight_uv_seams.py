"""Restore UV borders at sewn foldovers without moving UV coordinates.

A sewn polygon edge is a fold when the triangles bordering it on both faces
lie on the same UV side. Compare UV IDs at matching mesh vertices, so an
intentional stack of separate shells is never mistaken for a sewn edge.
"""
from dataclasses import dataclass
import math
import re

import maya.api.OpenMaya as om
import maya.cmds as cmds


_COMPONENT = re.compile(r'\.(?:map|e|f|vtx|vtxFace)\[')
_SIDE_EPSILON = 1.e-10


@dataclass(frozen=True)
class SeamPlan:
    shape: str
    uv_set: str
    border_edges: tuple
    folded_edges: tuple
    sewn_border_edges: tuple

    @property
    def edges(self):
        return tuple(sorted(set(self.border_edges) | set(self.folded_edges)))

    @property
    def needs_cut(self):
        return bool(self.folded_edges or self.sewn_border_edges)

    @property
    def components(self):
        return ['{}.e[{}]'.format(self.shape, edge) for edge in self.edges]


@dataclass(frozen=True)
class SeamResult:
    plans: tuple
    cut_meshes: int

    @property
    def restored_edges(self):
        return sum(len(plan.folded_edges) for plan in self.plans)

    @property
    def selected_edges(self):
        return sum(len(plan.edges) for plan in self.plans)


def _uuid(path):
    return om.MFnDependencyNode(path.node()).uuid().asString()


def _mesh_paths(owner):
    if cmds.nodeType(owner) == 'mesh':
        names = [owner]
    else:
        names = cmds.listRelatives(owner, allDescendents=True,
                                   fullPath=True, type='mesh') or []
    for name in names:
        if cmds.getAttr(name + '.intermediateObject'):
            continue
        yield om.MSelectionList().add(name).getDagPath(0)


def _selection_scope():
    """UUID -> (mesh path, selected UV IDs or None for the whole mesh)."""
    selected = cmds.ls(selection=True, long=True) or []
    if not selected:
        raise ValueError('Select meshes, groups or UV shells to restore their seams.')
    scopes = {}
    # Object and group selection wins over a component selection of that mesh.
    for item in selected:
        if _COMPONENT.search(item):
            continue
        for path in _mesh_paths(item):
            scopes[_uuid(path)] = (path, None)
    components = [item for item in selected if _COMPONENT.search(item)]
    if components:
        converted = cmds.polyListComponentConversion(components, toUV=True) or []
        uvs = cmds.ls(converted, flatten=True, long=True) or []
        owners = {}
        for component in uvs:
            owner, index = component.rsplit('.map[', 1)
            if owner not in owners:
                path = om.MSelectionList().add(owner).getDagPath(0)
                if path.node().hasFn(om.MFn.kTransform):
                    path.extendToShape()
                owners[owner] = (path, _uuid(path))
            path, key = owners[owner]
            if key not in scopes:
                scopes[key] = (path, set())
            ids = scopes[key][1]
            if ids is not None:
                ids.add(int(index[:-1]))
    if not scopes:
        raise ValueError('The selection has no polygon UVs in the current UV set.')
    return tuple(scopes.values())


def _side(a, b, third):
    dx, dy = b[0] - a[0], b[1] - a[1]
    tx, ty = third[0] - a[0], third[1] - a[1]
    cross = dx * ty - dy * tx
    tolerance = _SIDE_EPSILON * math.hypot(dx, dy) * math.hypot(tx, ty)
    if not math.isfinite(cross) or abs(cross) <= tolerance:
        return 0
    return 1 if cross > 0 else -1


def _face_data(fn, uv_set, u, v):
    """Read mappings in bulk and use edge-adjacent triangles for concave faces."""
    counts, vertices = fn.getVertices()
    uv_counts, uv_ids = fn.getAssignedUVs(uv_set)
    triangle_counts, triangle_vertices = fn.getTriangles()
    faces = []
    vertex_offset = uv_offset = triangle_offset = 0
    for face_id, count in enumerate(counts):
        face_vertices = tuple(vertices[vertex_offset:vertex_offset + count])
        mapped_count = uv_counts[face_id]
        mapped_ids = tuple(uv_ids[uv_offset:uv_offset + mapped_count])
        triangles = triangle_vertices[triangle_offset:
                                      triangle_offset + 3 * triangle_counts[face_id]]
        vertex_offset += count
        uv_offset += mapped_count
        triangle_offset += 3 * triangle_counts[face_id]
        if mapped_count != count or len(set(face_vertices)) != count:
            faces.append(None)
            continue
        mapping = dict(zip(face_vertices, mapped_ids))
        boundary = {tuple(sorted((a, b))) for a, b in
                    zip(face_vertices, face_vertices[1:] + face_vertices[:1])}
        sides = {}
        for offset in range(0, len(triangles), 3):
            triangle = tuple(triangles[offset:offset + 3])
            for corner in range(3):
                first, second = sorted((triangle[corner], triangle[(corner + 1) % 3]))
                key = (first, second)
                if key not in boundary:
                    continue
                third = triangle[(corner + 2) % 3]
                a_id, b_id, third_id = mapping[first], mapping[second], mapping[third]
                sign = _side((u[a_id], v[a_id]), (u[b_id], v[b_id]),
                             (u[third_id], v[third_id]))
                if sign:
                    sides.setdefault(key, set()).add(sign)
        faces.append((mapping, sides))
    return faces


def _plan(path, selected_uvs):
    fn = om.MFnMesh(path)
    uv_set = fn.currentUVSetName()
    u, v = fn.getUVs(uv_set)
    if not len(u):
        return None
    _, shell_ids = fn.getUvShellsIds(uv_set)
    selected_shells = (None if selected_uvs is None else
                       {shell_ids[index] for index in selected_uvs
                        if 0 <= index < len(shell_ids)})
    faces = _face_data(fn, uv_set, u, v)
    border, folded, sewn_border = [], [], []
    edges = om.MItMeshEdge(path)
    while not edges.isDone():
        edge_id = edges.index()
        first, second = sorted((edges.vertexId(0), edges.vertexId(1)))
        adjacent = list(edges.getConnectedFaces())
        edges.next()
        mapped = [faces[face] for face in adjacent if faces[face] is not None]
        if not mapped:
            continue
        pairs = [(face[0][first], face[0][second]) for face in mapped]
        if selected_shells is not None and not any(
                shell_ids[pair[0]] in selected_shells or
                shell_ids[pair[1]] in selected_shells for pair in pairs):
            continue
        is_border = len(adjacent) == 1 or len(mapped) != len(adjacent) or len(set(pairs)) > 1
        if is_border:
            border.append(edge_id)
            # A seam can still share an endpoint at a UV pinch; cut it as well.
            if any(a[0] == b[0] or a[1] == b[1]
                   for i, a in enumerate(pairs) for b in pairs[i + 1:]):
                sewn_border.append(edge_id)
            continue
        if len(adjacent) != 2:
            continue
        key = (first, second)
        side_a, side_b = (face[1].get(key, set()) for face in mapped)
        if len(side_a) == len(side_b) == 1 and side_a == side_b:
            folded.append(edge_id)
    return SeamPlan(path.fullPathName(), uv_set, tuple(border),
                    tuple(folded), tuple(sewn_border))


def find_butterfly_seams():
    """Inspect the current selection without changing UVs or selection."""
    plans = tuple(plan for path, ids in _selection_scope()
                  if (plan := _plan(path, ids)) is not None)
    if not plans:
        raise ValueError('The selected meshes have no UVs in their current UV sets.')
    return plans


def fix_butterfly_overlaps():
    """Cut sewn fold boundaries and select shell borders, in a single Undo step.

    Meshes/groups use their current UV set. Component selections expand to
    complete UV shells. Separate coincident stacks and UV coordinates are kept.
    """
    import uv_straight as uv
    plans = find_butterfly_seams()
    cut_meshes = 0
    with uv._undo('Restore Butterfly UV Seams'):
        for plan in plans:
            if not plan.needs_cut:
                continue
            # polyMapCut uses each mesh's current set; it has no uvSetName flag.
            # Include all contour edges, but avoid adding no-op history on repeat.
            cmds.polyMapCut(plan.components, constructionHistory=True)
            cut_meshes += 1
        components = [component for plan in plans for component in plan.components]
        if components:
            cmds.selectMode(component=True)
            cmds.selectType(allComponents=False)
            cmds.selectType(polymeshEdge=True)
            cmds.hilite([plan.shape for plan in plans if plan.edges], replace=True)
            cmds.select(components, replace=True)
    return SeamResult(plans, cut_meshes)
