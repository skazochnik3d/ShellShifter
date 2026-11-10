"""Read-only shell overlap quality: symmetric UV area agreement, not vertex count.

All Maya reads and result selection run on the main thread. Triangle clipping
and spatial indexing use immutable snapshots in the cancellable scan worker.
"""
from collections import defaultdict
from dataclasses import dataclass
import math
from uv_straight_deps import np
import maya.api.OpenMaya as om
import maya.cmds as cmds
from uv_straight_error_checks import Progress, MeshErrors, BoxIndex, _intersects
from uv_straight_error_checks import read_steps as _read_steps

MATCH_THRESHOLD = .95
RATIO_EPSILON = 1.e-7  # Allow float32 UV roundoff at the inclusive threshold.
AREA_EPSILON = 1.e-10  # Relative area threshold; shared borders are not overlaps.


@dataclass(frozen=True)
class QualitySnapshot:
    source: object
    triangles: tuple


@dataclass(frozen=True)
class Finding:
    kind: str
    shells: tuple
    agreement: float | None = None

    @property
    def key(self):
        return self.kind, frozenset(self.shells)


@dataclass(frozen=True)
class Report:
    checked: int
    issues: tuple
    skipped: tuple
    matching_pairs: int = 0
    overlapping_pairs: int = 0
    scope: tuple = ()
    reviewed: tuple = ()
    findings: tuple = ()


@dataclass(frozen=True)
class Triangle:
    points: tuple
    face: int
    bbox: tuple
    area: float


class Shell:
    def __init__(self, shape, triangles, faces=None):
        self.shape = shape
        self.triangles = triangles
        self.faces = faces if faces is not None else tuple(sorted({triangle.face for triangle in triangles}))
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


def _focused_pairs(index,focused):
    """Visit only pairs touching the reviewed islands, in either index order."""
    positions = {id(shell):i for i,shell in enumerate(index.items)}
    visited = set()
    for first in sorted(focused):
        for shell in _query(index,index.items[first].bbox):
            second = positions[id(shell)]
            if first==second:continue
            pair = tuple(sorted((first,second)))
            if pair not in visited:
                visited.add(pair)
                yield pair


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


def analyse_steps(snapshot,focus=None):
    """Check each actual overlapping pair; a good twin cannot hide a third shell."""
    shells,identities = [],{}
    for mesh_index,(mesh,entries) in enumerate(zip(snapshot.source.meshes,snapshot.triangles)):
        by_shell = defaultdict(list)
        shell_faces = defaultdict(set)
        uv_points = [tuple(map(float, point)) for point in mesh.points]
        progress = Progress('Preparing shells',mesh_index+1,len(snapshot.source.meshes))
        for index,(face,a,b,c) in enumerate(entries):
            owner = int(mesh.shell_ids[a])
            shell_faces[owner].add(int(face))
            points = (uv_points[a], uv_points[b], uv_points[c])
            if all(math.isfinite(value) for p in points for value in p):
                area = _area(points)
                if area>0.:
                    bounds = (min(p[0] for p in points),min(p[1] for p in points),
                              max(p[0] for p in points),max(p[1] for p in points))
                    by_shell[owner].append(Triangle(points,int(face),bounds,area))
            if index%256==255:yield progress
        shells.extend(Shell(mesh.shape, triangles, tuple(sorted(shell_faces[owner])))
                      for owner, triangles in by_shell.items())
        identities[mesh.shape] = mesh.identity
        yield progress
    shell_results = [MeshErrors(shell.shape, *identities[shell.shape], shell.faces) for shell in shells]
    index = BoxIndex(shells)
    bad = defaultdict(set)
    findings = []
    focus_faces = defaultdict(set)
    if focus is not None:
        for target in (focus,) if isinstance(focus, MeshErrors) else focus:
            focus_faces[target.uuid].update(target.faces)
    focused = {i for i, result in enumerate(shell_results) if focus is None or
               (result.uuid in focus_faces and focus_faces[result.uuid].intersection(result.faces))}
    folded = set()
    representatives, geometry, repeated = {}, [], set()
    fold_cache = {}

    def check_fold(representative, progress):
        if representative not in fold_cache:
            fold_cache[representative] = yield from _folded(representative, progress)
        return fold_cache[representative]

    for shell_index,shell in enumerate(shells):
        progress = Progress('Checking folded shells',shell_index+1,len(shells))
        # Only share identical, ordered triangles. Different tessellations
        # and nearly coincident UVs still use the normal geometric test.
        key = tuple(triangle.points for triangle in shell.triangles)
        representative = representatives.setdefault(key, shell)
        geometry.append(representative)
        if representative is not shell:
            repeated.add(representative)
        if shell_index in focused:
            shell.folded = yield from check_fold(representative, progress)
            folded.add(shell_index)
            if shell.folded:
                bad[shell.shape].update(shell.faces)
                findings.append(Finding('fold', (shell_results[shell_index],)))
        index.add(shell_index)
        yield progress
    matching,overlapping = 0,0
    coverage_cache = {}
    pairs = (((i,j) for i in range(len(shells)) for j in index.candidates(i,strict=True))
             if focus is None else _focused_pairs(index,focused))
    for shell_index,other in pairs:
        first,second = shells[shell_index],shells[other]
        progress = Progress('Checking overlap quality',shell_index+1,len(shells))
        first_geometry, second_geometry = geometry[shell_index], geometry[other]
        # Keep the order so the clipping and accumulation order stay identical.
        key = (first_geometry, second_geometry)
        coverage = coverage_cache.get(key)
        if coverage is None:
            coverage = yield from _coverage(first_geometry, second_geometry, progress)
            if len(coverage_cache) < 65536 and (first_geometry in repeated or second_geometry in repeated):
                coverage_cache[key] = coverage
        intersection,ratio = coverage
        if intersection<=min(first.area,second.area)*AREA_EPSILON:continue
        overlapping += 1
        for candidate in (shell_index,other):
            if candidate not in folded:
                shells[candidate].folded = yield from check_fold(geometry[candidate], progress)
                folded.add(candidate)
        if ratio+RATIO_EPSILON>=MATCH_THRESHOLD and not first.folded and not second.folded:
            matching += 1
        else:
            bad[first.shape].update(first.faces)
            bad[second.shape].update(second.faces)
            pair = (shell_results[shell_index], shell_results[other])
            if first.folded or second.folded:
                findings.append(Finding('fold_overlap', pair))
            else:
                findings.append(Finding('mismatch', pair, ratio))
        yield progress
    scope = tuple(MeshErrors(shape,identity[0],identity[1],()) for shape,identity in identities.items())
    reviewed = ()
    if focus is not None:
        selected_faces = defaultdict(set)
        for i in focused:
            selected_faces[shells[i].shape].update(shells[i].faces)
        for shape in list(bad):
            bad[shape].intersection_update(selected_faces.get(shape, ()))
        reviewed = tuple(MeshErrors(shape, *identity, tuple(sorted(selected_faces[shape])))
                         for shape,identity in identities.items() if identity[0] in focus_faces)
    issues = tuple(MeshErrors(shape,identity[0],identity[1],tuple(sorted(bad[shape])))
                   for shape,identity in identities.items() if bad.get(shape))
    return Report(len(identities),issues,snapshot.source.skipped,matching,overlapping,scope,reviewed,tuple(findings))


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
