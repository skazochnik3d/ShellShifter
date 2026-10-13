"""Read-only shell overlap quality: symmetric UV area agreement, not vertex count.

All Maya reads and result selection run on the main thread. Triangle clipping
and spatial indexing use immutable snapshots in the cancellable scan worker.
"""
from collections import defaultdict
from dataclasses import dataclass
import math
import numpy as np
import maya.api.OpenMaya as om
import maya.cmds as cmds
from uv_straight_error_checks import Progress, MeshErrors, BoxIndex, _intersects
from uv_straight_error_checks import read_steps as _read_steps

MATCH_THRESHOLD = .99
RATIO_EPSILON = 1.e-7  # Inclusive 99%, allowing float32 UV roundoff.
AREA_EPSILON = 1.e-10  # Relative area threshold; shared borders are not overlaps.


@dataclass(frozen=True)
class QualitySnapshot:
    source: object
    triangles: tuple


@dataclass(frozen=True)
class Report:
    checked: int
    issues: tuple
    skipped: tuple
    matching_pairs: int = 0
    overlapping_pairs: int = 0


@dataclass(frozen=True)
class Triangle:
    points: tuple
    face: int
    bbox: tuple
    area: float


class Shell:
    def __init__(self, shape, triangles):
        self.shape = shape
        self.triangles = triangles
        self.faces = tuple(sorted({triangle.face for triangle in triangles}))
        self.area = math.fsum(triangle.area for triangle in triangles)
        self.bbox = (min(t.bbox[0] for t in triangles), min(t.bbox[1] for t in triangles),
                     max(t.bbox[2] for t in triangles), max(t.bbox[3] for t in triangles))
        self.index = None
        self.folded = False


def _cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def _area(points):
    return abs(math.fsum(_cross(points[0],points[i],points[i+1])
                         for i in range(1,len(points)-1)))*.5 if len(points)>=3 else 0.


def _intersection_area(first, second):
    """Convex triangle clipping in local coordinates, stable for tiny/UDIM UVs."""
    origin = first.points[0]
    extent = max(*(abs(value-origin[axis]) for triangle in (first,second)
                   for point in triangle.points for axis,value in enumerate(point)),1.e-300)
    a = [((p[0]-origin[0])/extent,(p[1]-origin[1])/extent) for p in first.points]
    b = [((p[0]-origin[0])/extent,(p[1]-origin[1])/extent) for p in second.points]
    if _cross(*b)<0.:b.reverse()
    polygon = a
    for index, start in enumerate(b):
        end = b[(index+1)%3]
        if not polygon:return 0.
        result = []
        previous = polygon[-1]
        previous_side = _cross(start,end,previous)
        for current in polygon:
            current_side = _cross(start,end,current)
            previous_inside, current_inside = previous_side>=0., current_side>=0.
            if previous_inside != current_inside:
                factor = previous_side/(previous_side-current_side)
                result.append((previous[0]+(current[0]-previous[0])*factor,
                               previous[1]+(current[1]-previous[1])*factor))
            if current_inside:result.append(current)
            previous,previous_side = current,current_side
        polygon = result
    return _area(polygon)*extent*extent


def _query(index, bounds):
    keys = index.keys(bounds)
    if keys is None:
        possible = range(len(index.items))
    else:
        possible = set(index.wide)
        for key in keys:possible.update(index.cells.get(key,()))
    for item in possible:
        if _intersects(bounds,index.items[item].bbox,True):
            yield index.items[item]


def _prepare_index(shell, progress):
    if shell.index is None:
        shell.index = BoxIndex(shell.triangles)
        for index in range(len(shell.triangles)):
            shell.index.add(index)
            if index%256==255:yield progress


def _folded(shell, progress):
    yield from _prepare_index(shell,progress)
    comparisons = 0
    for index,first in enumerate(shell.triangles):
        for other in shell.index.candidates(index,strict=True):
            second = shell.triangles[other]
            if _intersection_area(first,second)>min(first.area,second.area)*AREA_EPSILON:
                return True
            comparisons += 1
            if comparisons%128==0:yield progress
        if index%128==127:yield progress
    return False


def _coverage(first, second, progress):
    # Query the larger indexed shell using the smaller set of triangles.
    if len(first.triangles)>len(second.triangles):first,second=second,first
    yield from _prepare_index(second,progress)
    intersection,comparisons = 0.,0
    for index,triangle in enumerate(first.triangles):
        for other in _query(second.index,triangle.bbox):
            intersection += _intersection_area(triangle,other)
            comparisons += 1
            if comparisons%128==0:yield progress
        if index%128==127:yield progress
    intersection = min(intersection,first.area,second.area)
    union = first.area+second.area-intersection
    return intersection,intersection/union if union>0. else 0.


def read_steps(shapes=None):
    """Snapshot native mesh tessellation, preserving concavity and folded quads."""
    source = yield from _read_steps(shapes)
    meshes,triangles,skipped = [],[],list(source.skipped)
    for mesh_index,mesh in enumerate(source.meshes):
        progress = Progress('Reading triangles',mesh_index+1,len(source.meshes))
        yield progress
        try:
            fn = om.MFnMesh(om.MSelectionList().add(mesh.shape).getDagPath(0))
            triangle_counts,triangle_vertices = fn.getTriangles()
            uv_offset,vertex_offset,triangle_offset = 0,0,0
            entries = []
            for face,(uv_count,vertex_count,triangle_count) in enumerate(
                    zip(mesh.counts,mesh.vertex_counts,triangle_counts)):
                if uv_count==vertex_count and uv_count>=3:
                    mapping = dict(zip(mesh.vertices[vertex_offset:vertex_offset+vertex_count],
                                       mesh.ids[uv_offset:uv_offset+uv_count]))
                    for offset in range(triangle_offset,triangle_offset+int(triangle_count)*3,3):
                        entries.append((face,*(int(mapping[v]) for v in triangle_vertices[offset:offset+3])))
                uv_offset+=int(uv_count);vertex_offset+=int(vertex_count)
                triangle_offset+=int(triangle_count)*3
                if face%128==127:yield progress
            array = np.array(entries,dtype=np.int64).reshape(-1,4).copy()
            array.setflags(write=False)
            meshes.append(mesh);triangles.append(array)
        except (RuntimeError,ValueError,KeyError) as error:
            skipped.append((mesh.shape,str(error)))
    from uv_straight_error_checks import Snapshot
    return QualitySnapshot(Snapshot(tuple(meshes),tuple(skipped)),tuple(triangles))


def analyse_steps(snapshot):
    """Check each actual overlapping pair; a good twin cannot hide a third shell."""
    shells,identities = [],{}
    for mesh_index,(mesh,entries) in enumerate(zip(snapshot.source.meshes,snapshot.triangles)):
        by_shell = defaultdict(list)
        progress = Progress('Preparing shells',mesh_index+1,len(snapshot.source.meshes))
        for index,(face,a,b,c) in enumerate(entries):
            points = tuple(tuple(mesh.points[i]) for i in (a,b,c))
            if all(math.isfinite(value) for p in points for value in p):
                area = _area(points)
                if area>0.:
                    bounds = (min(p[0] for p in points),min(p[1] for p in points),
                              max(p[0] for p in points),max(p[1] for p in points))
                    by_shell[int(mesh.shell_ids[a])].append(Triangle(points,int(face),bounds,area))
            if index%256==255:yield progress
        shells.extend(Shell(mesh.shape,triangles) for triangles in by_shell.values())
        identities[mesh.shape] = mesh.identity
        yield progress
    index = BoxIndex(shells)
    bad = defaultdict(set)
    for shell_index,shell in enumerate(shells):
        progress = Progress('Checking folded shells',shell_index+1,len(shells))
        shell.folded = yield from _folded(shell,progress)
        if shell.folded:bad[shell.shape].update(shell.faces)
        index.add(shell_index)
        yield progress
    matching,overlapping = 0,0
    for shell_index,first in enumerate(shells):
        progress = Progress('Checking overlap quality',shell_index+1,len(shells))
        for other in index.candidates(shell_index,strict=True):
            second = shells[other]
            intersection,ratio = yield from _coverage(first,second,progress)
            if intersection<=min(first.area,second.area)*AREA_EPSILON:continue
            overlapping += 1
            if ratio+RATIO_EPSILON>=MATCH_THRESHOLD and not first.folded and not second.folded:
                matching += 1
            else:
                bad[first.shape].update(first.faces)
                bad[second.shape].update(second.faces)
            yield progress
        yield progress
    issues = tuple(MeshErrors(shape,identity[0],identity[1],tuple(sorted(bad[shape])))
                   for shape,identity in identities.items() if bad.get(shape))
    return Report(len(identities),issues,snapshot.source.skipped,matching,overlapping)


def native_steps(report):
    # Matches the cooperative controller's API; this scan creates no helper nodes.
    if False:yield Progress('Selecting results',0,0)
    return report


def scan(shapes=None):
    snapshot = _drain(read_steps(shapes))
    return _drain(analyse_steps(snapshot))


def _drain(steps):
    while True:
        try:next(steps)
        except StopIteration as done:return done.value


def select_issues(issues, faces=False, isolate=False):
    """Select bad whole shells as UVs; matching shells in the same mesh stay clear."""
    components = []
    for issue in issues:
        matches = cmds.ls(issue.uuid,long=True,allPaths=True) or []
        if not matches:continue
        shape = issue.shape if issue.shape in matches else matches[0]
        if cmds.polyEvaluate(shape,face=True)!=issue.face_count:
            raise ValueError('Topology changed. Run the check again.')
        components.extend('{}.f[{}]'.format(shape,face) for face in issue.faces)
    if components:
        import uv_straight as uv
        converted = cmds.polyListComponentConversion(components,toUV=True) or []
        uv.select_uv_shell_components(cmds.ls(converted,flatten=True,long=True) or [])
    else:
        cmds.select(clear=True)
