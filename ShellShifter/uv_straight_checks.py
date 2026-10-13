"""UV validation helpers that select complete shells needing attention."""
from dataclasses import dataclass
import math
import maya.api.OpenMaya as om
import maya.cmds as cmds


HARD_EDGE_OWNER = 'uv_checks_hard_edges'


@dataclass(frozen=True)
class SmallShell:
    mesh: object
    uv_ids: tuple
    width_pixels: float
    height_pixels: float
    local_thickness_pixels: float

    @property
    def limiting_size_pixels(self):
        return min(self.width_pixels, self.height_pixels,
                   self.local_thickness_pixels)


@dataclass(frozen=True)
class HardEdgeWithoutSeam:
    mesh: object
    edge_id: int
    uv_pair: tuple
    world_segment: tuple


@dataclass
class HardEdgeMesh:
    name: str
    fn: object
    uv_set: str
    points: dict


def _shell_thickness(mesh, shell):
    """Tessellation-independent band thickness estimate: 2 * area / perimeter."""
    edge_counts = {}
    area = 0.
    for face in mesh.faces:
        if not set(face) <= shell:
            continue
        points = [mesh.points[index] for index in face]
        twice_area = sum(first[0]*second[1]-second[0]*first[1]
                         for first, second in zip(points, points[1:]+points[:1]))
        area += abs(twice_area)*.5
        for first, second in zip(face, face[1:]+face[:1]):
            pair = tuple(sorted((first, second)))
            edge_counts[pair] = edge_counts.get(pair, 0)+1
    perimeter = 0.
    for (first, second), count in edge_counts.items():
        if count == 1:
            a, b = mesh.points[first], mesh.points[second]
            perimeter += math.hypot(b[0]-a[0], b[1]-a[1])
    return 2.*area/perimeter if perimeter > 1e-12 else 0.


def find_small_shells(minimum_pixels=1.0, texture_size=2048):
    """Find shells with a small bounding side or a locally thin UV face."""
    import uv_straight as uv
    minimum_pixels = float(minimum_pixels)
    texture_size = int(texture_size)
    if minimum_pixels <= 0:
        raise ValueError('Minimum shell size must be greater than zero pixels.')
    if texture_size <= 0:
        raise ValueError('Texture size must be greater than zero.')
    _, meshes = uv._selection()
    result = []
    for mesh in meshes:
        for shell in mesh.shells():
            if not shell:
                continue
            u_values = [mesh.points[index][0] for index in shell]
            v_values = [mesh.points[index][1] for index in shell]
            width = (max(u_values)-min(u_values))*texture_size
            height = (max(v_values)-min(v_values))*texture_size
            local_width = _shell_thickness(mesh, shell)*texture_size
            if min(width, height, local_width) < minimum_pixels:
                result.append(SmallShell(mesh, tuple(sorted(shell)), width, height,
                                         local_width))
    return result


def select_small_shells(minimum_pixels=1.0, texture_size=2048):
    """Select complete UV shells smaller than the requested pixel threshold."""
    import uv_straight as uv
    shells = find_small_shells(minimum_pixels, texture_size)
    components = [shell.mesh.component(index)
                  for shell in shells for index in shell.uv_ids]
    uv.select_uv_shell_components(components)
    return shells


def find_hard_edges_without_seams():
    """Find selected meshes' internal hard edges that still share one UV edge."""
    from uv_straight_move_overlaps import _selected_mesh_shapes
    result = []
    for shape in _selected_mesh_shapes():
        path = om.MSelectionList().add(shape).getDagPath(0)
        if not path.isVisible():
            continue
        fn = om.MFnMesh(path)
        uv_set = fn.currentUVSetName()
        u,v = fn.getUVs(uv_set)
        if not len(u):
            continue
        mesh = HardEdgeMesh(shape,fn,uv_set,{})
        edges = om.MItMeshEdge(path)
        polygons = om.MItMeshPolygon(path)
        face_uvs = {}
        world = None
        while not edges.isDone():
            edge_id = edges.index()
            adjacent = list(edges.getConnectedFaces())
            edges.next()
            if len(adjacent) != 2 or fn.isEdgeSmooth(edge_id):
                continue
            first,second = fn.getEdgeVertices(edge_id)
            pairs = []
            for face_id in adjacent:
                if face_id not in face_uvs:
                    polygons.setIndex(face_id)
                    try:
                        face_uvs[face_id] = {vertex:polygons.getUVIndex(local,uv_set)
                                            for local,vertex in enumerate(polygons.getVertices())}
                    except RuntimeError:
                        face_uvs[face_id] = None
                mapping = face_uvs[face_id]
                if mapping is None:
                    break
                # Compare UV IDs at the SAME mesh vertices on both faces.
                # Separate IDs are a seam even if their coordinates coincide.
                pairs.append((mapping[first],mapping[second]))
            if len(pairs) != 2 or pairs[0] != pairs[1]:
                continue
            uv_pair = pairs[0]
            for uv_id in uv_pair:
                mesh.points[uv_id] = (u[uv_id],v[uv_id])
            if world is None:
                world = fn.getPoints(om.MSpace.kWorld)
            segment = (tuple(world[first])[:3], tuple(world[second])[:3])
            result.append(HardEdgeWithoutSeam(mesh, edge_id, uv_pair, segment))
    return result


def show_hard_edges_without_seams(select_edges=True):
    """Select hard edges missing UV cuts, without adding a color overlay."""
    clear_hard_edge_highlights()
    issues = find_hard_edges_without_seams()
    if issues and select_edges:
        owners = list(dict.fromkeys(issue.mesh.name for issue in issues))
        cmds.selectMode(component=True)
        cmds.selectType(allComponents=False)
        cmds.selectType(polymeshEdge=True)
        cmds.hilite(owners,replace=True)
        cmds.select(['{}.e[{}]'.format(issue.mesh.name,issue.edge_id) for issue in issues],replace=True)
    return issues


def clear_hard_edge_highlights():
    import uv_straight_native as native
    native.clear_check_overlay(HARD_EDGE_OWNER)
