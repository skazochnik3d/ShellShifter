"""Move stacked UV shells out of a baking tile in Autodesk Maya 2025.

Usage in Maya's Python tab::

    import move_overlaps
    move_overlaps.show()

The tool deliberately handles *stacked* shells (the same UV polygons at the same
coordinates).  Merely intersecting, unrelated shells are reported neither as a
stack nor moved.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple
import json
import math

import maya.api.OpenMaya as om
import maya.cmds as cmds


WINDOW = "moveOverlapsForBakeWindow"
TITLE = "Move UV Overlaps for Bake"
VERSION = "1.3.3"
OVERLAP_THRESHOLD = 0.5
GEOMETRY_EPSILON = 1.0e-10
TILE_EPSILON = 1.0e-6
ORIGINAL_COLOR_OPTION = "uvStraightMoveOverlapOriginalColor"
ORIGINAL_COLOR_OWNER = "move_overlaps_originals"
ORIGINAL_RGBA = (1.0, 0.72, 0.04, 1.0)
NON_ORIGINAL_RGBA = (0.30, 0.30, 0.30, 1.0)
ORIGINAL_ATTR = "uvStraightMoveOverlapOriginals"


@dataclass(frozen=True)
class Shell:
    shape: str
    transform: str
    owner: str
    shell_id: int
    uv_ids: Tuple[int, ...]
    face_ids: Tuple[int, ...]
    triangles: Tuple[Tuple[Tuple[float, float], ...], ...]
    bbox: Tuple[float, float, float, float]
    area: float
    signed_area: float

    @property
    def is_blue(self) -> bool:
        """Positive UV winding (shown blue with Maya UV orientation shading)."""
        return self.signed_area >= 0.0

    @property
    def label(self) -> str:
        return "{}.uvShell[{}]".format(self.transform, self.shell_id)


@dataclass
class Plan:
    shells: List[Shell]
    groups: List[List[Shell]]
    keepers: List[Shell]
    movers: List[Shell]
    anchors: List[str]
    preferred_keepers: List[Shell]
    ignored_outside_tile: int
    skipped_without_anchor: int


# Keep the session choice across importlib.reload() when the panel is reopened.
# UUID keys cannot leak into another scene because Maya assigns new node UUIDs.
if "_preferred_originals" not in globals():
    _preferred_originals = set()
if "_legacy_originals_migrated" not in globals():
    _legacy_originals_migrated = False


def _selected_mesh_shapes() -> List[str]:
    """Return selected polygon shapes, preserving selection order."""
    selected = cmds.ls(selection=True, long=True, objectsOnly=True) or []
    shapes: List[str] = []
    seen_shapes = set()

    for node in selected:
        if not cmds.objExists(node):
            continue
        node_type = cmds.nodeType(node)
        if node_type == "mesh":
            candidates = [node]
        else:
            candidates = cmds.listRelatives(
                node, allDescendents=True, fullPath=True, type="mesh"
            ) or []
        for shape in reversed(candidates):
            if cmds.getAttr(shape + ".intermediateObject"):
                continue
            long_name = (cmds.ls(shape, long=True) or [shape])[0]
            if long_name not in seen_shapes:
                shapes.append(long_name)
                seen_shapes.add(long_name)

    if not shapes:
        raise RuntimeError("Select one or more polygon meshes.")
    return shapes


def _mesh_fn(shape: str) -> om.MFnMesh:
    selection = om.MSelectionList()
    selection.add(shape)
    return om.MFnMesh(selection.getDagPath(0))


def _shell_key(shell: Shell) -> Tuple[str, Tuple[int, ...]]:
    uuids = cmds.ls(shell.shape, uuid=True) or []
    return (uuids[0] if uuids else shell.shape, shell.face_ids)


def _original_data(shape: str) -> dict:
    plug = shape + "." + ORIGINAL_ATTR
    if not cmds.objExists(plug):
        return {}
    try:
        value = cmds.getAttr(plug)
        data = json.loads(value) if value else {}
        return data if isinstance(data, dict) else {}
    except (RuntimeError, TypeError, ValueError):
        return {}


def _original_face_sets(shape: str) -> set:
    uv_set = _mesh_fn(shape).currentUVSetName()
    return {tuple(sorted(int(face) for face in faces))
            for faces in _original_data(shape).get(uv_set, [])}


def _write_original_face_sets(shape: str, face_sets) -> None:
    data = _original_data(shape)
    uv_set = _mesh_fn(shape).currentUVSetName()
    normalized = sorted({tuple(sorted(int(face) for face in faces))
                         for faces in face_sets})
    if normalized:
        data[uv_set] = [list(faces) for faces in normalized]
    else:
        data.pop(uv_set, None)
    plug = shape + "." + ORIGINAL_ATTR
    if not data:
        if cmds.objExists(plug):
            cmds.deleteAttr(plug)
        return
    if not cmds.attributeQuery(ORIGINAL_ATTR, node=shape, exists=True):
        cmds.addAttr(shape, longName=ORIGINAL_ATTR, dataType="string", hidden=True)
    cmds.setAttr(plug, json.dumps(data, sort_keys=True), type="string")


def _original_shapes() -> List[str]:
    return sorted(set(cmds.ls("*.{}".format(ORIGINAL_ATTR),
                              objectsOnly=True, long=True, recursive=True) or []))


def _migrate_legacy_originals() -> None:
    """Persist originals assigned by 1.4.83 before its in-memory list is lost."""
    global _preferred_originals, _legacy_originals_migrated
    if _legacy_originals_migrated:
        return
    _legacy_originals_migrated = True
    legacy = set(_preferred_originals)
    _preferred_originals = set()
    if not legacy:
        return
    grouped = defaultdict(set)
    for node_uuid, face_ids in legacy:
        for node in cmds.ls(node_uuid, long=True) or []:
            if cmds.objExists(node) and cmds.nodeType(node) == "mesh":
                grouped[node].add(tuple(face_ids))
                break
    if not grouped:
        return
    undo_enabled = cmds.undoInfo(query=True, state=True)
    cmds.undoInfo(stateWithoutFlush=False)
    try:
        for shape, face_sets in grouped.items():
            _write_original_face_sets(shape, _original_face_sets(shape) | face_sets)
    finally:
        cmds.undoInfo(stateWithoutFlush=undo_enabled)


def _stored_original_keys() -> set:
    _migrate_legacy_originals()
    result = set()
    for shape in _original_shapes():
        uuids = cmds.ls(shape, uuid=True) or []
        identity = uuids[0] if uuids else shape
        result.update((identity, faces) for faces in _original_face_sets(shape))
    return result


def _shells_from_selection() -> List[Shell]:
    selected_uvs = _selected_uv_ids_by_shape()
    if not selected_uvs:
        raise RuntimeError("Select one or more polygon meshes or UV shells.")
    chosen = []
    for shape, uv_ids in selected_uvs.items():
        shells, _ = _extract_shells(shape, (0, 0), False)
        chosen.extend(shell for shell in shells
                      if any(uv_id in uv_ids for uv_id in shell.uv_ids))
    if not chosen:
        raise RuntimeError("The selection has no measurable UV shells.")
    return chosen


def original_color_visible() -> bool:
    return (bool(cmds.optionVar(query=ORIGINAL_COLOR_OPTION))
            if cmds.optionVar(exists=ORIGINAL_COLOR_OPTION) else True)


def original_shells() -> List[Shell]:
    """Resolve scene-persistent originals for their current shape paths."""
    _migrate_legacy_originals()
    result = []
    for shape in _original_shapes():
        face_sets = _original_face_sets(shape)
        shells, _ = _extract_shells(shape, (0, 0), False)
        result.extend(shell for shell in shells if shell.face_ids in face_sets)
    return result


def original_color_payload() -> list:
    """Yellow originals plus neutral faces on their meshes for clear contrast."""
    shells = original_shells()
    original_faces = defaultdict(set)
    for shell in shells:
        original_faces[shell.shape].update(shell.face_ids)
    payload = []
    for shape, yellow_faces in original_faces.items():
        polygon_count = _mesh_fn(shape).numPolygons
        payload.extend((shape, face_id,
                        ORIGINAL_RGBA if face_id in yellow_faces else NON_ORIGINAL_RGBA)
                       for face_id in range(polygon_count))
    return payload


def refresh_original_colors() -> list:
    """Update the transient viewport color layer and return its payload."""
    payload = original_color_payload() if original_color_visible() else []
    if cmds.about(batch=True):
        return payload
    import uv_straight_native as native
    if payload:
        native.set_viewport_overlay(payload, ORIGINAL_COLOR_OWNER)
    else:
        native.clear_viewport_overlay(ORIGINAL_COLOR_OWNER)
    return payload


def set_original_color_visible(enabled: bool) -> bool:
    cmds.optionVar(intValue=(ORIGINAL_COLOR_OPTION, int(bool(enabled))))
    refresh_original_colors()
    return bool(enabled)


def set_originals_from_selection() -> int:
    """Add the selected meshes/UV shells to the preferred originals."""
    global _last_analysis
    _migrate_legacy_originals()
    selected = _shells_from_selection()
    before = _stored_original_keys()
    by_shape = defaultdict(set)
    for shell in selected:
        by_shape[shell.shape].add(shell.face_ids)
    import uv_straight as uv
    with uv._undo('ShellShifter Add Overlap Originals'):
        for shape, face_sets in by_shape.items():
            _write_original_face_sets(shape, _original_face_sets(shape) | face_sets)
    added = _stored_original_keys() - before
    _last_analysis = None
    refresh_original_colors()
    return len(added)


def clear_originals_from_selection() -> int:
    """Remove only selected meshes/UV shells from the preferred originals."""
    global _last_analysis
    _migrate_legacy_originals()
    selected = _shells_from_selection()
    before = _stored_original_keys()
    by_shape = defaultdict(set)
    for shell in selected:
        by_shape[shell.shape].add(shell.face_ids)
    import uv_straight as uv
    with uv._undo('ShellShifter Remove Overlap Originals'):
        for shape, face_sets in by_shape.items():
            _write_original_face_sets(shape, _original_face_sets(shape) - face_sets)
    removed = before - _stored_original_keys()
    _last_analysis = None
    refresh_original_colors()
    return len(removed)


def clear_originals() -> int:
    """Clear every preferred original (used by scene/tool reset code)."""
    global _preferred_originals, _legacy_originals_migrated, _last_analysis
    _migrate_legacy_originals()
    count = len(_stored_original_keys())
    shapes = _original_shapes()
    if shapes:
        import uv_straight as uv
        with uv._undo('ShellShifter Clear Overlap Originals'):
            for shape in shapes:
                if cmds.attributeQuery(ORIGINAL_ATTR, node=shape, exists=True):
                    cmds.deleteAttr(shape + "." + ORIGINAL_ATTR)
    _preferred_originals = set()
    _legacy_originals_migrated = True
    _last_analysis = None
    refresh_original_colors()
    return count


def original_count() -> int:
    return len(_stored_original_keys())


def _signed_area(points: Sequence[Tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    twice_area = 0.0
    for index, (u_value, v_value) in enumerate(points):
        next_u, next_v = points[(index + 1) % len(points)]
        twice_area += u_value * next_v - next_u * v_value
    return 0.5 * twice_area


def _cross(first, second, third) -> float:
    return (
        (second[0] - first[0]) * (third[1] - first[1])
        - (second[1] - first[1]) * (third[0] - first[0])
    )


def _point_in_triangle(point, first, second, third) -> bool:
    return (
        _cross(first, second, point) >= -GEOMETRY_EPSILON
        and _cross(second, third, point) >= -GEOMETRY_EPSILON
        and _cross(third, first, point) >= -GEOMETRY_EPSILON
    )


def _triangulate_polygon(points):
    """Triangulate a simple UV polygon with an ear-clipping pass."""
    clean = []
    for point in points:
        point = (float(point[0]), float(point[1]))
        if not clean or point != clean[-1]:
            clean.append(point)
    if len(clean) > 1 and clean[0] == clean[-1]:
        clean.pop()
    if len(clean) < 3:
        return []
    if _signed_area(clean) < 0.0:
        clean.reverse()

    indices = list(range(len(clean)))
    triangles = []
    guard = len(indices) * len(indices)
    while len(indices) > 3 and guard:
        guard -= 1
        ear_found = False
        for offset, current in enumerate(indices):
            previous = indices[offset - 1]
            following = indices[(offset + 1) % len(indices)]
            first, second, third = clean[previous], clean[current], clean[following]
            if _cross(first, second, third) <= GEOMETRY_EPSILON:
                continue
            if any(
                _point_in_triangle(clean[index], first, second, third)
                for index in indices
                if index not in (previous, current, following)
            ):
                continue
            triangles.append((first, second, third))
            del indices[offset]
            ear_found = True
            break
        if not ear_found:
            break

    if len(indices) == 3:
        triangle = tuple(clean[index] for index in indices)
        if abs(_signed_area(triangle)) > GEOMETRY_EPSILON:
            triangles.append(triangle)
    elif not triangles:
        # Degenerate/non-simple fallback. Maya production faces are normally
        # triangles or quads, but a filtered fan is safer than dropping the face.
        for index in range(1, len(clean) - 1):
            triangle = (clean[0], clean[index], clean[index + 1])
            if abs(_signed_area(triangle)) > GEOMETRY_EPSILON:
                if _signed_area(triangle) < 0.0:
                    triangle = (triangle[0], triangle[2], triangle[1])
                triangles.append(triangle)
    return triangles


def _line_intersection(start, end, clip_start, clip_end):
    start_side = _cross(clip_start, clip_end, start)
    end_side = _cross(clip_start, clip_end, end)
    denominator = start_side - end_side
    if abs(denominator) <= GEOMETRY_EPSILON:
        return end
    amount = start_side / denominator
    return (
        start[0] + amount * (end[0] - start[0]),
        start[1] + amount * (end[1] - start[1]),
    )


def _triangle_intersection_area(first, second) -> float:
    """Area of two CCW triangles using convex polygon clipping."""
    polygon = list(first)
    for index, clip_start in enumerate(second):
        clip_end = second[(index + 1) % 3]
        if not polygon:
            return 0.0
        output = []
        start = polygon[-1]
        start_inside = _cross(clip_start, clip_end, start) >= -GEOMETRY_EPSILON
        for end in polygon:
            end_inside = _cross(clip_start, clip_end, end) >= -GEOMETRY_EPSILON
            if end_inside:
                if not start_inside:
                    output.append(_line_intersection(start, end, clip_start, clip_end))
                output.append(end)
            elif start_inside:
                output.append(_line_intersection(start, end, clip_start, clip_end))
            start, start_inside = end, end_inside
        polygon = output
    return abs(_signed_area(polygon))


def _triangle_bbox(triangle):
    u_values = [point[0] for point in triangle]
    v_values = [point[1] for point in triangle]
    return min(u_values), min(v_values), max(u_values), max(v_values)


def _boxes_intersect(first, second) -> bool:
    return not (
        first[2] <= second[0] + GEOMETRY_EPSILON
        or second[2] <= first[0] + GEOMETRY_EPSILON
        or first[3] <= second[1] + GEOMETRY_EPSILON
        or second[3] <= first[1] + GEOMETRY_EPSILON
    )


def _overlap_ratio(first: Shell, second: Shell) -> float:
    smaller_area = min(first.area, second.area)
    if smaller_area <= GEOMETRY_EPSILON or not _boxes_intersect(first.bbox, second.bbox):
        return 0.0
    overlap_area = 0.0
    first_triangles = [(triangle, _triangle_bbox(triangle)) for triangle in first.triangles]
    second_triangles = [(triangle, _triangle_bbox(triangle)) for triangle in second.triangles]
    for first_triangle, first_box in first_triangles:
        for second_triangle, second_box in second_triangles:
            if _boxes_intersect(first_box, second_box):
                overlap_area += _triangle_intersection_area(first_triangle, second_triangle)
    return min(1.0, overlap_area / smaller_area)


def _overlap_groups(shells: Sequence[Shell]) -> List[List[Shell]]:
    """Find connected stacks whose pairwise coverage reaches 50 percent."""
    parents = list(range(len(shells)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first, second):
        first_root, second_root = find(first), find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    ordered = sorted(range(len(shells)), key=lambda index: shells[index].bbox[0])
    for position, first_index in enumerate(ordered):
        first = shells[first_index]
        for second_index in ordered[position + 1 :]:
            second = shells[second_index]
            if second.bbox[0] >= first.bbox[2] - GEOMETRY_EPSILON:
                break
            if _overlap_ratio(first, second) + GEOMETRY_EPSILON >= OVERLAP_THRESHOLD:
                union(first_index, second_index)

    by_root = defaultdict(list)
    for index, shell in enumerate(shells):
        by_root[find(index)].append(shell)
    groups = [group for group in by_root.values() if len(group) > 1]
    groups.sort(key=lambda group: min(shell.label for shell in group))
    return groups


def _selected_uv_ids_by_shape() -> Dict[str, set]:
    """Convert the current Maya selection to UV ids without changing it."""
    result = defaultdict(set)
    current = cmds.ls(selection=True, long=True) or []
    if not current:
        return result

    converted = cmds.polyListComponentConversion(current, toUV=True) or []
    if not converted:
        return result

    selection = om.MSelectionList()
    for component_name in converted:
        try:
            selection.add(component_name)
        except RuntimeError:
            continue

    for index in range(selection.length()):
        try:
            dag_path, component = selection.getComponent(index)
        except RuntimeError:
            continue
        if component.isNull() or component.apiType() != om.MFn.kMeshMapComponent:
            continue
        if dag_path.node().hasFn(om.MFn.kTransform):
            dag_path.extendToShape()
        component_fn = om.MFnSingleIndexedComponent(component)
        result[dag_path.fullPathName()].update(int(value) for value in component_fn.getElements())
    return result


def _shells_with_any_overlap(shells: Sequence[Shell]) -> List[Shell]:
    """Return selected shells participating in any positive-area intersection."""
    matched_indices = set()
    ordered = sorted(range(len(shells)), key=lambda index: shells[index].bbox[0])
    for position, first_index in enumerate(ordered):
        first = shells[first_index]
        for second_index in ordered[position + 1 :]:
            second = shells[second_index]
            if second.bbox[0] >= first.bbox[2] - GEOMETRY_EPSILON:
                break
            if _overlap_ratio(first, second) > GEOMETRY_EPSILON:
                matched_indices.update((first_index, second_index))
    return [shell for index, shell in enumerate(shells) if index in matched_indices]


def _uv_ids_for_shells(shells: Sequence[Shell]) -> Dict[str, set]:
    result = defaultdict(set)
    for shell in shells:
        result[shell.shape].update(shell.uv_ids)
    return result


def _select_uv_shells(shells: Sequence[Shell]) -> None:
    uv_ids_by_shape = _uv_ids_for_shells(shells)
    components = [
        "{}.map[{}]".format(shape, uv_id)
        for shape, uv_ids in uv_ids_by_shape.items()
        for uv_id in sorted(uv_ids)
    ]
    import uv_straight as uv
    uv.select_uv_shell_components(components)


def check_overlaps() -> List[Shell]:
    """Select whole UV shells that overlap any other currently selected shell."""
    selected_uvs = _selected_uv_ids_by_shape()
    if not selected_uvs:
        raise RuntimeError("Select UV shells or UV components first.")

    selected_shells = []
    for shape, uv_ids in selected_uvs.items():
        extracted, _ = _extract_shells(shape, (0, 0), False)
        selected_shells.extend(
            shell for shell in extracted if any(uv_id in uv_ids for uv_id in shell.uv_ids)
        )

    overlaps = _shells_with_any_overlap(selected_shells)
    if overlaps:
        _select_uv_shells(overlaps)
        om.MGlobal.displayInfo(
            "Select Overlaps: selected {} of {} UV shells.".format(
                len(overlaps), len(selected_shells)
            )
        )
    else:
        cmds.select(clear=True)
        om.MGlobal.displayInfo(
            "Select Overlaps: no intersections among {} selected UV shells.".format(
                len(selected_shells)
            )
        )
    return overlaps


def _face_geometry_shell_ids(fn_mesh: om.MFnMesh) -> List[int]:
    """Return a welded-geometry component id for every polygon face."""
    face_counts, face_vertices = fn_mesh.getVertices()
    face_count = len(face_counts)
    parents = list(range(face_count))

    def find(face_id: int) -> int:
        while parents[face_id] != face_id:
            parents[face_id] = parents[parents[face_id]]
            face_id = parents[face_id]
        return face_id

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    first_face_by_vertex = {}
    cursor = 0
    for face_id, count in enumerate(face_counts):
        for vertex_id in face_vertices[cursor : cursor + count]:
            vertex_id = int(vertex_id)
            previous_face = first_face_by_vertex.get(vertex_id)
            if previous_face is None:
                first_face_by_vertex[vertex_id] = face_id
            else:
                union(face_id, previous_face)
        cursor += count

    root_to_shell = {}
    result = []
    for face_id in range(face_count):
        root = find(face_id)
        root_to_shell.setdefault(root, len(root_to_shell))
        result.append(root_to_shell[root])
    return result


def _extract_shells(
    shape: str,
    source_tile: Tuple[int, int],
    source_tile_only: bool,
) -> Tuple[List[Shell], int]:
    fn_mesh = _mesh_fn(shape)
    uv_set = fn_mesh.currentUVSetName()
    u_values, v_values = fn_mesh.getUVs(uv_set)
    if len(u_values) == 0:
        return [], 0

    # Python API 2.0 returns (number_of_shells, shell_id_per_uv).
    shell_count, shell_ids = fn_mesh.getUvShellsIds(uv_set)
    uv_counts, assigned_uvs = fn_mesh.getAssignedUVs(uv_set)
    transform = (cmds.listRelatives(shape, parent=True, fullPath=True) or [shape])[0]
    geometry_shell_ids = _face_geometry_shell_ids(fn_mesh)

    shell_uvs: Dict[int, List[int]] = defaultdict(list)
    for uv_id, shell_id in enumerate(shell_ids):
        shell_uvs[int(shell_id)].append(uv_id)

    shell_faces: Dict[int, List[Tuple[int, Tuple[int, ...]]]] = defaultdict(list)
    cursor = 0
    for face_id, count in enumerate(uv_counts):
        face_uvs = tuple(int(value) for value in assigned_uvs[cursor : cursor + count])
        cursor += count
        if not face_uvs:
            continue
        face_shells = {int(shell_ids[uv_id]) for uv_id in face_uvs}
        if len(face_shells) == 1:
            shell_faces[face_shells.pop()].append((face_id, face_uvs))

    tile_u, tile_v = source_tile
    minimum_u, maximum_u = float(tile_u), float(tile_u + 1)
    minimum_v, maximum_v = float(tile_v), float(tile_v + 1)
    result: List[Shell] = []
    ignored = 0

    for shell_id in range(int(shell_count)):
        uv_ids = tuple(sorted(shell_uvs.get(shell_id, ())))
        faces = shell_faces.get(shell_id, ())
        if not uv_ids or not faces:
            continue

        if source_tile_only:
            inside = all(
                minimum_u - TILE_EPSILON <= u_values[uv_id] <= maximum_u + TILE_EPSILON
                and minimum_v - TILE_EPSILON <= v_values[uv_id] <= maximum_v + TILE_EPSILON
                for uv_id in uv_ids
            )
            if not inside:
                ignored += 1
                continue

        triangles = []
        total_area = 0.0
        face_ids: List[int] = []
        for face_id, face_uvs in faces:
            float_points = [(float(u_values[i]), float(v_values[i])) for i in face_uvs]
            triangles.extend(_triangulate_polygon(float_points))
            total_area += _signed_area(float_points)
            face_ids.append(face_id)

        geometry_ids = tuple(sorted({geometry_shell_ids[face_id] for face_id in face_ids}))
        geometry_label = ",".join(str(value) for value in geometry_ids)
        owner = "{}.meshShell[{}]".format(transform, geometry_label)
        all_points = [point for triangle in triangles for point in triangle]
        if not all_points:
            continue
        bbox = (
            min(point[0] for point in all_points),
            min(point[1] for point in all_points),
            max(point[0] for point in all_points),
            max(point[1] for point in all_points),
        )
        area = sum(abs(_signed_area(triangle)) for triangle in triangles)

        result.append(
            Shell(
                shape=shape,
                transform=transform,
                owner=owner,
                shell_id=shell_id,
                uv_ids=uv_ids,
                face_ids=tuple(sorted(face_ids)),
                triangles=tuple(triangles),
                bbox=bbox,
                area=area,
                signed_area=total_area,
            )
        )
    return result, ignored


def _object_components(groups: Sequence[Sequence[Shell]]) -> List[List[str]]:
    graph: Dict[str, set] = defaultdict(set)
    for group in groups:
        objects = {shell.owner for shell in group}
        for object_name in objects:
            graph[object_name].update(objects - {object_name})

    components: List[List[str]] = []
    unseen = set(graph)
    while unseen:
        first = min(unseen)
        stack = [first]
        component = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(graph[current] - component)
        unseen -= component
        components.append(sorted(component))
    return components


def build_plan(
    source_tile: Tuple[int, int] = (0, 0),
    source_tile_only: bool = True,
) -> Plan:
    shells: List[Shell] = []
    ignored = 0
    for shape in _selected_mesh_shapes():
        extracted, skipped = _extract_shells(
            shape, source_tile, source_tile_only
        )
        shells.extend(extracted)
        ignored += skipped

    groups = _overlap_groups(shells)

    # Maya preserves selection order here. It is the most useful final tie-breaker:
    # artists can select the intended original first when copies are otherwise equal.
    object_rank: Dict[str, int] = {}
    for shell in shells:
        object_rank.setdefault(shell.owner, len(object_rank))

    keepers: List[Shell] = []
    movers: List[Shell] = []
    anchors: List[str] = []
    preferred_keepers: List[Shell] = []
    skipped_without_anchor = 0

    preferred_originals = _stored_original_keys()
    automatic_groups = []
    for group in groups:
        preferred = [shell for shell in group
                     if _shell_key(shell) in preferred_originals]
        if preferred:
            preferred_keepers.extend(preferred)
            keepers.extend(preferred)
            movers.extend(shell for shell in group if shell not in preferred)
        else:
            automatic_groups.append(group)

    pending = [
        (component, [group for group in automatic_groups
                     if any(shell.owner in component for shell in group)])
        for component in _object_components(automatic_groups)
    ]
    while pending:
        component, related = pending.pop(0)
        component_has_blue = any(
            shell.owner in component and shell.is_blue
            for group in related
            for shell in group
        )

        def anchor_score(object_name: str) -> Tuple[int, int, int, int, str]:
            coverage = sum(any(shell.owner == object_name for shell in group) for group in related)
            blue_shells = sum(
                shell.owner == object_name and shell.is_blue
                for group in related
                for shell in group
            )
            red_shells = sum(
                shell.owner == object_name and not shell.is_blue
                for group in related
                for shell in group
            )
            if not component_has_blue:
                # In an all-red family there is no orientation preference. Keep
                # the mesh represented in the greatest number of overlap groups;
                # selection order resolves otherwise identical copies.
                return (
                    -coverage,
                    -red_shells,
                    0,
                    object_rank.get(object_name, len(object_rank)),
                    object_name,
                )
            # Prefer the mesh contributing the most blue overlap shells. This
            # distinguishes a blue original from its red mirrored copies even
            # when a mesh also contains a few red manual overlaps.
            return (
                -blue_shells,
                red_shells,
                -coverage,
                object_rank.get(object_name, len(object_rank)),
                object_name,
            )

        anchor = min(component, key=anchor_score)
        anchors.append(anchor)

        unresolved = []
        for group in related:
            if any(shell in keepers for shell in group):
                continue
            local = [shell for shell in group if shell.owner == anchor]
            # A mesh may participate in only part of a connected overlap family.
            # Resolve the remaining stacks in this same analysis, not on the next
            # user click. Already resolved stacks and their keepers stay fixed.
            if not local:
                unresolved.append(group)
                continue
            if any(shell.is_blue for shell in group):
                # In a mixed family keep the anchor's blue shells. Its red
                # overlaps move together with the other copies.
                keepers.extend(shell for shell in local if shell.is_blue)
                movers.extend(
                    shell
                    for shell in group
                    if shell.owner != anchor or not shell.is_blue
                )
            else:
                # With no blue candidate, one complete red mesh becomes the
                # original and only the other red copies move.
                keepers.extend(local)
                movers.extend(shell for shell in group if shell.owner != anchor)

        # At least one group is consumed per iteration: the anchor comes from
        # related. Repartition only the unresolved groups to preserve coherent
        # choices across all shells that can share an original.
        for remainder in _object_components(unresolved):
            pending.append((remainder, [
                group for group in unresolved
                if any(shell.owner in remainder for shell in group)
            ]))

    return Plan(
        shells=shells,
        groups=groups,
        keepers=keepers,
        movers=movers,
        anchors=anchors,
        preferred_keepers=preferred_keepers,
        ignored_outside_tile=ignored,
        skipped_without_anchor=skipped_without_anchor,
    )


def _summary(plan: Plan) -> str:
    blue_keepers = sum(shell.is_blue for shell in plan.keepers)
    lines = [
        "Meshes analysed: {}".format(len({shell.shape for shell in plan.shells})),
        "UV shells analysed: {}".format(len(plan.shells)),
        "Overlap stacks: {}".format(len(plan.groups)),
        "Shells kept in source tile: {} (blue: {})".format(len(plan.keepers), blue_keepers),
        "Shells to move: {}".format(len(plan.movers)),
    ]
    if plan.preferred_keepers:
        lines.append("User originals kept: {}".format(len(plan.preferred_keepers)))
    if plan.anchors:
        lines.append("Automatic originals: {}".format(len(plan.anchors)))
    if plan.ignored_outside_tile:
        lines.append("Ignored outside source tile: {}".format(plan.ignored_outside_tile))
    if plan.skipped_without_anchor:
        lines.append(
            "Stacks left unchanged (not present on anchor): {}".format(
                plan.skipped_without_anchor
            )
        )
    if not plan.groups:
        lines.append("No UV shell overlaps reached 50% coverage.")
    return "\n".join(lines)


_last_analysis = None


def analyse(
    source_tile: Tuple[int, int] = (0, 0),
    source_tile_only: bool = True,
) -> Plan:
    global _last_analysis
    current_uvs = _selected_uv_ids_by_shape()
    if (
        _last_analysis is not None
        and _last_analysis[1] == source_tile
        and _last_analysis[2] == source_tile_only
        and current_uvs == _last_analysis[3]
    ):
        original_selection = _last_analysis[4]
        # Analyse always reads fresh UVs, even after manual edits or Undo.
        previous_selection = cmds.ls(selection=True, long=True) or []
        try:
            cmds.select(original_selection, replace=True)
            plan = build_plan(source_tile, source_tile_only)
        finally:
            if previous_selection:
                cmds.select(previous_selection, replace=True)
            else:
                cmds.select(clear=True)
    else:
        original_selection = cmds.ls(selection=True, long=True) or []
        plan = build_plan(source_tile, source_tile_only)
    message = _summary(plan)
    print("\n{} {}\n{}".format(TITLE, VERSION, message))
    om.MGlobal.displayInfo("Move Overlaps: {} stacks, {} shells to move.".format(
        len(plan.groups), len(plan.movers)
    ))
    _select_uv_shells(plan.movers)
    _last_analysis = (
        plan,
        source_tile,
        source_tile_only,
        _uv_ids_for_shells(plan.movers),
        original_selection,
    )
    return plan


def move(
    offset: Tuple[float, float] = (1.0, 0.0),
    source_tile: Tuple[int, int] = (0, 0),
    source_tile_only: bool = True,
) -> Plan:
    global _last_analysis
    current_uvs = _selected_uv_ids_by_shape()
    original_selection = None
    if (
        _last_analysis is not None
        and _last_analysis[1] == source_tile
        and _last_analysis[2] == source_tile_only
        and current_uvs == _last_analysis[3]
    ):
        plan = _last_analysis[0]
        original_selection = _last_analysis[4]
    else:
        plan = build_plan(source_tile, source_tile_only)
    _last_analysis = None
    if not plan.movers:
        om.MGlobal.displayWarning("Move Overlaps: no stacked shells to move.")
        if original_selection is not None:
            if original_selection:
                cmds.select(original_selection, replace=True)
            else:
                cmds.select(clear=True)
        return plan

    move_by_shape: Dict[str, set] = defaultdict(set)
    for shell in plan.movers:
        move_by_shape[shell.shape].update(shell.uv_ids)

    cmds.undoInfo(openChunk=True, chunkName=TITLE)
    try:
        for shape, uv_ids in move_by_shape.items():
            components = ["{}.map[{}]".format(shape, uv_id) for uv_id in sorted(uv_ids)]
            cmds.polyEditUV(
                components,
                relative=True,
                uValue=float(offset[0]),
                vValue=float(offset[1]),
            )
    finally:
        cmds.undoInfo(closeChunk=True)

    om.MGlobal.displayInfo(
        "Move Overlaps: moved {} shells by U {}, V {}. Undo is one step.".format(
            len(plan.movers), offset[0], offset[1]
        )
    )
    if original_selection is not None:
        if original_selection:
            cmds.select(original_selection, replace=True)
        else:
            cmds.select(clear=True)
    return plan


def flip(axis):
    """Flip selected UV shells around their individual bounding-box centers."""
    import uv_straight as uv
    axis = str(axis).upper()
    if axis not in ('U', 'V'):
        raise ValueError("Flip axis must be 'U' or 'V'.")
    _, meshes = uv._selection()
    plans = []
    coordinate = 0 if axis == 'U' else 1
    for mesh in meshes:
        for shell in mesh.shells():
            values = [mesh.points[index][coordinate] for index in shell]
            pivot = (min(values)+max(values))*.5
            positions = {}
            for index in shell:
                point = list(mesh.points[index])
                point[coordinate] = 2.*pivot-point[coordinate]
                positions[index] = tuple(point)
            plans.append((mesh, positions))
    if not plans:
        raise ValueError('Select one or more UV shells to flip.')
    with uv._undo('ShellShifter Flip ' + axis):
        for mesh, positions in plans:
            mesh.write(positions)
    return len(plans)


def move_selected_shells(offset=(1.0, 0.0)):
    """Move every UV shell touched by the current selection."""
    import uv_straight as uv
    try:
        u_value, v_value = (float(value) for value in offset)
    except (TypeError, ValueError):
        raise ValueError('Move offset must contain U and V values.')
    if not all(map(math.isfinite, (u_value, v_value))):
        raise ValueError('Move offset must contain finite U and V values.')

    selection, meshes = uv._selection()
    plans = []
    shell_count = 0
    for mesh in meshes:
        shells = mesh.shells()
        if not shells:
            continue
        uv_ids = set().union(*shells)
        positions = {
            index: (mesh.points[index][0] + u_value,
                    mesh.points[index][1] + v_value)
            for index in uv_ids
        }
        plans.append((mesh, positions))
        shell_count += len(shells)
    if not plans:
        raise ValueError('Select one or more UV shells to move.')

    with uv._undo('ShellShifter Move Selected Shells'):
        try:
            for mesh, positions in plans:
                mesh.write(positions)
        finally:
            if selection:
                cmds.select(selection, replace=True)
            else:
                cmds.select(clear=True)
    om.MGlobal.displayInfo(
        'ShellShifter: moved {} selected shell{} by U {}, V {}.'.format(
            shell_count, '' if shell_count == 1 else 's', u_value, v_value))
    return shell_count


class _Window:
    def __init__(self) -> None:
        self.output = None
        self.source_u = None
        self.source_v = None
        self.tile_only = None
        self.offset_u = None
        self.offset_v = None

    def values(self):
        return dict(
            source_tile=(
                cmds.intField(self.source_u, query=True, value=True),
                cmds.intField(self.source_v, query=True, value=True),
            ),
            source_tile_only=cmds.checkBox(self.tile_only, query=True, value=True),
        )

    def on_analyse(self, *_):
        try:
            plan = analyse(**self.values())
            cmds.scrollField(self.output, edit=True, text=_summary(plan))
        except Exception as error:
            cmds.scrollField(self.output, edit=True, text="ERROR: {}".format(error))
            om.MGlobal.displayError(str(error))

    def on_move(self, *_):
        try:
            offset = (
                cmds.floatField(self.offset_u, query=True, value=True),
                cmds.floatField(self.offset_v, query=True, value=True),
            )
            plan = move(offset=offset, **self.values())
            cmds.scrollField(self.output, edit=True, text=_summary(plan))
        except Exception as error:
            cmds.scrollField(self.output, edit=True, text="ERROR: {}".format(error))
            om.MGlobal.displayError(str(error))

    def on_check(self, *_):
        try:
            overlaps = check_overlaps()
            cmds.scrollField(
                self.output,
                edit=True,
                text="Check Overlaps\nOverlapping UV shells selected: {}".format(
                    len(overlaps)
                ),
            )
        except Exception as error:
            cmds.scrollField(self.output, edit=True, text="ERROR: {}".format(error))
            om.MGlobal.displayError(str(error))

    def create(self):
        if cmds.window(WINDOW, exists=True):
            cmds.deleteUI(WINDOW)
        cmds.window(WINDOW, title="{}  {}".format(TITLE, VERSION), sizeable=False)
        cmds.columnLayout(adjustableColumn=True, rowSpacing=6, width=440)
        cmds.text(
            label="Select meshes/groups. Welded geometry is one original.",
            align="left",
        )
        cmds.separator(style="in", height=8)

        cmds.rowLayout(numberOfColumns=5, adjustableColumn=5)
        cmds.text(label="Source tile  U")
        self.source_u = cmds.intField(value=0, width=45)
        cmds.text(label="  V")
        self.source_v = cmds.intField(value=0, width=45)
        self.tile_only = cmds.checkBox(label="Only this tile", value=True)
        cmds.setParent("..")

        cmds.rowLayout(numberOfColumns=5, adjustableColumn=5)
        cmds.text(label="Move by       U")
        self.offset_u = cmds.floatField(value=1.0, precision=3, width=65)
        cmds.text(label="  V")
        self.offset_v = cmds.floatField(value=0.0, precision=3, width=65)
        cmds.text(label="  (next tile = U 1, V 0)", align="left")
        cmds.setParent("..")

        cmds.rowLayout(
            numberOfColumns=3,
            adjustableColumn=3,
            columnWidth3=(143, 143, 144),
        )
        cmds.button(label="Analyse", command=self.on_analyse, height=32)
        cmds.button(label="Check Overlaps", command=self.on_check, height=32)
        cmds.button(label="Move overlaps", command=self.on_move, height=32)
        cmds.setParent("..")

        self.output = cmds.scrollField(
            editable=False,
            wordWrap=False,
            height=150,
            text="Ready. Analyse does not modify UVs.",
        )
        cmds.text(
            label="All-red stacks keep one complete welded mesh as original.",
            align="left",
        )
        cmds.showWindow(WINDOW)
        return WINDOW


_window = None


def show():
    global _window
    _window = _Window()
    return _window.create()


if __name__ == "__main__":
    show()
