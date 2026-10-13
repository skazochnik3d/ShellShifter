"""Texel-density analysis and transient viewport visualization."""
from dataclasses import dataclass
import math

import maya.api.OpenMaya as om
import maya.cmds as cmds


@dataclass
class ShellDensity:
    mesh: object
    shell_id: int
    uv_ids: tuple
    density: float
    triangles: tuple
    face_ids: tuple


@dataclass
class DensityGroup:
    minimum: float
    maximum: float
    color: tuple
    shells: list
    color_reference: float = None


STOPS = ((0.00, (0.05, 0.22, 1.00)),
         (0.25, (0.00, 0.92, 1.00)),
         (0.50, (0.15, 0.95, 0.24)),
         (0.75, (1.00, 0.92, 0.00)),
         (1.00, (1.00, 0.20, 0.04)))


def gradient(value):
    value = min(1., max(0., float(value)))
    for (left, a), (right, b) in zip(STOPS, STOPS[1:]):
        if value <= right:
            factor = (value-left)/(right-left)
            return tuple(a[i]+(b[i]-a[i])*factor for i in range(3))
    return STOPS[-1][1]


def _triangle_area_2d(a, b, c):
    return abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))*.5


def _measure(meshes, resolution):
    shells = []
    for mesh in meshes:
        positions = mesh.fn.getPoints(om.MSpace.kWorld)
        accum = {}
        iterator = om.MItMeshPolygon(mesh.path)
        while not iterator.isDone():
            try:
                vertices = tuple(iterator.getVertices())
                uv_ids = tuple(iterator.getUVIndex(i, mesh.uv_set)
                               for i in range(len(vertices)))
                _, triangle_vertices = iterator.getTriangles(om.MSpace.kWorld)
                local = {vertex: index for index, vertex in enumerate(vertices)}
                shell_id = mesh.shell_ids[uv_ids[0]]
                entry = accum.setdefault(shell_id, [0., 0., [], set(), []])
                entry[3].update(uv_ids)
                entry[4].append(iterator.index())
                for offset in range(0, len(triangle_vertices), 3):
                    indices = tuple(local[triangle_vertices[offset+i]] for i in range(3))
                    vertex_ids = tuple(vertices[i] for i in indices)
                    tri_uvs = tuple(mesh.points[uv_ids[i]] for i in indices)
                    a, b, c = (positions[i] for i in vertex_ids)
                    world_area = ((b-a) ^ (c-a)).length()*.5
                    uv_area = _triangle_area_2d(*tri_uvs)
                    entry[0] += world_area
                    entry[1] += uv_area
                    entry[2].extend((tuple(a), tuple(b), tuple(c)))
            except (RuntimeError, KeyError):
                pass
            iterator.next()
        for shell_id, (world_area, uv_area, triangles, uv_ids, face_ids) in accum.items():
            if world_area > 1e-20 and uv_area > 1e-20:
                density = float(resolution)*math.sqrt(uv_area/world_area)
                shells.append(ShellDensity(mesh, shell_id, tuple(sorted(uv_ids)),
                                           density, tuple(triangles), tuple(face_ids)))
    if not shells:
        raise ValueError('Selected meshes have no measurable UV shells.')
    return shells


# UV coordinates are float32. Do not amplify their roundoff into a heatmap.
DENSITY_REL_TOLERANCE = 1.e-4
DENSITY_ABS_TOLERANCE = 1.e-8
MINIMUM_COLOR_SPAN = .02


def _color_scale(shells):
    low = min(shell.density for shell in shells)
    high = max(shell.density for shell in shells)
    if math.isclose(low, high, rel_tol=DENSITY_REL_TOLERANCE,
                    abs_tol=DENSITY_ABS_TOLERANCE):
        return None
    center = (low + high) * .5
    span = max(high - low, center * MINIMUM_COLOR_SPAN)
    return center - span * .5, span


def _density_color(density, scale, reference=None):
    if reference is not None:
        # Fixed analysis reference: monotonic for every positive density, with
        # no min/max clipping or recoloring of unchanged shells after an edit.
        return gradient(density / (density + reference))
    return gradient(.5 if scale is None else (density - scale[0]) / scale[1])


def _group(shells, group_count, color_reference=None):
    low = min(shell.density for shell in shells)
    high = max(shell.density for shell in shells)
    count = max(1, min(int(group_count), len(shells)))
    if math.isclose(low, high, rel_tol=DENSITY_REL_TOLERANCE,
                    abs_tol=DENSITY_ABS_TOLERANCE):
        representative = math.fsum(shell.density for shell in shells) / len(shells)
        return [DensityGroup(low, high, _density_color(representative, None, color_reference),
                             shells, color_reference)]
    width = (high-low)/count
    scale = _color_scale(shells)
    buckets = [[] for _ in range(count)]
    for shell in shells:
        index = min(count-1, int((shell.density-low)/width))
        buckets[index].append(shell)
    result = []
    for index, bucket in enumerate(buckets):
        if bucket:
            representative = math.fsum(s.density for s in bucket) / len(bucket)
            result.append(DensityGroup(min(s.density for s in bucket),
                                       max(s.density for s in bucket),
                                       _density_color(representative, scale, color_reference),
                                       bucket, color_reference))
    return result


def analyse(resolution=2048, group_count=5):
    """Measure every UV shell belonging to the selected mesh objects."""
    import uv_straight as uv
    _, meshes = uv._selection()
    return _group(_measure(meshes, resolution), group_count)


def reanalyse(groups, resolution=2048, group_count=5):
    """Remeasure the meshes captured by a previous analysis.

    Group selection changes Maya's active selection to UV components.  Keeping
    the mesh paths in the analysis result lets Set refresh the complete color
    map without depending on that temporary selection.
    """
    import uv_straight as uv
    names = list(dict.fromkeys(shell.mesh.name
                               for group in groups for shell in group.shells))
    meshes = []
    for name in names:
        if not cmds.objExists(name):
            continue
        selection = om.MSelectionList()
        try:
            selection.add(name)
            path = selection.getDagPath(0)
            if path.node().hasFn(om.MFn.kTransform):
                path.extendToShape()
            meshes.append(uv.Mesh(path))
        except RuntimeError:
            continue
    if not meshes:
        raise ValueError('The meshes from the texel-density analysis no longer exist.')
    return _group(_measure(meshes, resolution), group_count)


def selected_shells(resolution=2048):
    """Return measured shells touched by the current UV/component selection."""
    import uv_straight as uv
    _, meshes = uv._selection()
    selected = {(mesh.name, mesh.shell_ids[uv_id])
                for mesh in meshes for uv_id in mesh.selected}
    return [shell for shell in _measure(meshes, resolution)
            if (shell.mesh.name, shell.shell_id) in selected]


def get_density(resolution=2048):
    """Arithmetic mean of the selected shells, with equal weight per shell."""
    shells = selected_shells(resolution)
    if not shells:
        raise ValueError('Select one or more measurable UV shells.')
    return math.fsum(shell.density for shell in shells)/len(shells)


def set_density(target, resolution=2048):
    """Uniformly scale selected UV shells to the requested px/unit value."""
    import uv_straight as uv
    target = float(target)
    if target <= 0:
        raise ValueError('Texel density must be greater than zero.')
    shells = selected_shells(resolution)
    if not shells:
        raise ValueError('Select one or more UV shells.')
    plans = []
    for shell in shells:
        scale = target/shell.density
        points = [shell.mesh.points[index] for index in shell.uv_ids]
        center = (sum(point[0] for point in points)/len(points),
                  sum(point[1] for point in points)/len(points))
        positions = {index: (center[0]+(shell.mesh.points[index][0]-center[0])*scale,
                             center[1]+(shell.mesh.points[index][1]-center[1])*scale)
                     for index in shell.uv_ids}
        plans.append((shell.mesh, positions))
    with uv._undo('ShellShifter Set Texel Density'):
        for mesh, positions in plans:
            mesh.write(positions)
    return len(shells)


def _colored_shells(groups):
    shells = [shell for group in groups for shell in group.shells]
    if not shells:
        return []
    scale = _color_scale(shells)
    result = []
    for group in groups:
        uniform = math.isclose(group.minimum, group.maximum,
                               rel_tol=DENSITY_REL_TOLERANCE,
                               abs_tol=DENSITY_ABS_TOLERANCE)
        representative = math.fsum(shell.density for shell in group.shells) / len(group.shells)
        for shell in group.shells:
            # Equal densities, including float32 roundoff, have EXACTLY one color.
            density = representative if uniform else shell.density
            result.append((shell, _density_color(density, scale, group.color_reference)))
    return result


def viewport_payload(groups, alpha=.72):
    """Give every shell its exact position on the continuous color scale.

    UI groups intentionally remain broad, but the viewport does not quantize
    all shells in a group to the same representative color.
    """
    return [(rgb + (float(alpha),), list(shell.triangles))
            for shell, rgb in _colored_shells(groups)]


def viewport_color_payload(groups):
    """Return Maya-native per-face colors without a custom VP2 draw node."""
    payload = []
    for shell, rgb in _colored_shells(groups):
        payload.extend((shell.mesh.name, face, tuple(rgb)+(1.,))
                       for face in shell.face_ids)
    return payload


def show(groups):
    import uv_straight_native as native
    payload = viewport_color_payload(groups)
    if payload:
        native.set_viewport_overlay(payload, 'texel_density')
    else:
        native.clear_viewport_overlay('texel_density')


def clear():
    import uv_straight_native as native
    native.clear_viewport_overlay('texel_density')


def select_group(group):
    components = []
    for shell in group.shells:
        components.extend(shell.mesh.components(shell.uv_ids))
    if components:
        import uv_straight as uv
        uv.select_uv_shell_components(components)
    return len(group.shells)
