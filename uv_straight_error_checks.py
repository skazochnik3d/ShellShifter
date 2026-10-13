"""UV Manager's overlap check, adapted to immutable UV snapshots.

Face containment, asymmetric 90% vertex matching and 100U twin offsets
follow the supplied uv_manager.py. Maya's native overlap detector runs on
temporary hidden snapshot meshes, never on modified source UVs.
"""
from collections import defaultdict
from dataclasses import dataclass
import math
import numpy as np
import maya.api.OpenMaya as om
import maya.cmds as cmds
from uv_straight_move_overlaps import _selected_mesh_shapes

TOLERANCE = .003
VERTEX_MATCH_THRESHOLD = .9


@dataclass(frozen=True)
class Progress:
    stage: str
    done: int
    total: int


@dataclass(frozen=True)
class MeshErrors:
    shape: str
    uuid: str
    face_count: int
    faces: tuple


@dataclass(frozen=True)
class Report:
    checked: int
    issues: tuple
    skipped: tuple


@dataclass(frozen=True)
class MeshSnapshot:
    shape: str
    identity: tuple
    points: object
    shell_ids: object
    counts: object
    ids: object
    vertex_counts: object
    vertices: object
    geometry_points: object


@dataclass(frozen=True)
class Snapshot:
    meshes: tuple
    skipped: tuple


@dataclass(frozen=True)
class PreparedCheck:
    snapshot: Snapshot
    report: Report
    shifted_points: tuple
    native_batches: tuple


class FaceData:
    def __init__(self, mesh_path, f_idx, shell_id, uv_ids, uvs):
        self.mesh_path, self.f_idx, self.shell_id = mesh_path, f_idx, shell_id
        self.uv_ids, self.uvs = uv_ids, uvs
        self.update_bounds()

    def update_bounds(self):
        self.bbox = (min(u for u,v in self.uvs), min(v for u,v in self.uvs),
                     max(u for u,v in self.uvs), max(v for u,v in self.uvs))

    def is_strictly_inside(self, pu, pv, tol_sq):
        # From UnifiedUVManager.FaceData in the supplied script.
        inside = False
        j = len(self.uvs)-1
        for i in range(len(self.uvs)):
            xi,yi = self.uvs[i]
            xj,yj = self.uvs[j]
            if ((yi > pv) != (yj > pv)) and (pu < (xj-xi)*(pv-yi)/(yj-yi)+xi):
                inside = not inside
            j = i
        if not inside:
            return False
        j = len(self.uvs)-1
        for i in range(len(self.uvs)):
            xi,yi = self.uvs[i]
            xj,yj = self.uvs[j]
            l2 = (xi-xj)**2+(yi-yj)**2
            if l2 == 0:
                d2 = (pu-xi)**2+(pv-yi)**2
            else:
                t = max(0.,min(1.,((pu-xi)*(xj-xi)+(pv-yi)*(yj-yi))/l2))
                d2 = (pu-(xi+t*(xj-xi)))**2+(pv-(yi+t*(yj-yi)))**2
            if d2 <= tol_sq:
                return False
            j = i
        return True


class ShellExt:
    def __init__(self, shell_id, faces, mesh_path, points, uv_indices):
        self.id, self.faces, self.mesh_path, self.points = shell_id, faces, mesh_path, points
        self.uv_indices = uv_indices
        self.point_grid = None
        self.bbox = (min(f.bbox[0] for f in faces), min(f.bbox[1] for f in faces),
                     max(f.bbox[2] for f in faces), max(f.bbox[3] for f in faces))


def _intersects(first, second, strict=False):
    if strict:
        return first[0] < second[2] and first[2] > second[0] and first[1] < second[3] and first[3] > second[1]
    return first[0] <= second[2] and first[2] >= second[0] and first[1] <= second[3] and first[3] >= second[1]


class BoxIndex:
    """Bounded cell occupancy with a fallback for unusually large polygons."""
    def __init__(self, items):
        self.items = items
        # Only cell sizing is approximate; bbox tests remain exact. Avoid a
        # full 140k-face Python traversal before the next UI checkpoint.
        sample = items[::max(1,len(items)//1024)]
        sides = [max(item.bbox[2]-item.bbox[0],item.bbox[3]-item.bbox[1]) for item in sample]
        self.cell_size = max(float(np.median(sides)) if sides else 1.,1e-6)
        self.cells = defaultdict(list)
        self.wide = []

    def keys(self, rect):
        a,b,c,d = (math.floor(value/self.cell_size) for value in rect)
        if (c-a+1)*(d-b+1) > 128:
            return None
        return ((x,y) for x in range(a,c+1) for y in range(b,d+1))

    def add(self, index):
        keys = self.keys(self.items[index].bbox)
        if keys is None:
            self.wide.append(index)
        else:
            for key in keys:
                self.cells[key].append(index)

    def candidates(self, index, strict=False):
        rect = self.items[index].bbox
        keys = self.keys(rect)
        if keys is None:
            possible = range(index+1,len(self.items))
        else:
            possible = set(self.wide)
            for key in keys:
                possible.update(self.cells.get(key,()))
            possible = sorted(i for i in possible if i > index)
        for other in possible:
            if _intersects(rect,self.items[other].bbox,strict):
                yield other


def _matches(first, second, progress):
    """Same any-nearby-vertex predicate as UV Manager, using a point grid."""
    a,b = len(first.points),len(second.points)
    if not a or not b or min(a,b)/max(a,b) < .5:
        return False
    if second.point_grid is None:
        grid = defaultdict(list)
        for index,(u,v) in enumerate(second.points):
            grid[(math.floor(u/TOLERANCE),math.floor(v/TOLERANCE))].append((u,v))
            if index % 256 == 255:
                yield progress
        second.point_grid = grid
    matches = 0
    required = math.ceil(VERTEX_MATCH_THRESHOLD*a)
    tol_sq = TOLERANCE**2
    for index,(u,v) in enumerate(first.points):
        x,y = math.floor(u/TOLERANCE),math.floor(v/TOLERANCE)
        if any((u-pu)**2+(v-pv)**2 <= tol_sq
               for dx in (-1,0,1) for dy in (-1,0,1)
               for pu,pv in second.point_grid.get((x+dx,y+dy),())):
            matches += 1
        if matches >= required:
            return True
        if matches+(a-index-1) < required:
            return False
        if index % 128 == 127:
            yield progress
    return False


def _face_overlap(first, second):
    tol_sq = TOLERANCE**2
    if any(second.is_strictly_inside(u,v,tol_sq) for u,v in first.uvs):
        return True
    if any(first.is_strictly_inside(u,v,tol_sq) for u,v in second.uvs):
        return True
    return False  # Edge crossings/folded faces are handled by polyUVOverlap.


def _native_batches(faces,limit=512):
    """Exact broad phase: every intersecting bbox pair shares a native batch.

    Faces crossing a partition are included on both sides. A small bbox
    padding only adds candidates; Maya still decides whether they overlap.
    No face-size, overlap-area, or point-match filter is applied here.
    """
    bounds = []
    for face in faces:
        a,b,c,d = face.bbox
        pad = max(1e-6,max(abs(a),abs(b),abs(c),abs(d))*1e-6)
        bounds.append((a-pad,b-pad,c+pad,d+pad))
    pending = [(tuple(range(len(faces))),0)]
    batches = []
    seen = set()
    while pending:
        indices,depth = pending.pop()
        yield Progress('Preparing native batches',len(batches),len(faces))
        if not indices:
            continue
        split = None
        if len(indices)>limit and depth<24:
            centers = [[(bounds[i][axis]+bounds[i][axis+2])*.5 for i in indices] for axis in (0,1)]
            axes = sorted((0,1),key=lambda axis:max(centers[axis])-min(centers[axis]),reverse=True)
            for axis in axes:
                values = sorted(centers[axis])
                cut = values[len(values)//2]
                left = tuple(i for i in indices if bounds[i][axis]<=cut)
                right = tuple(i for i in indices if bounds[i][axis+2]>=cut)
                if max(len(left),len(right)) < len(indices)*.98:
                    split = left,right
                    break
        if split is not None:
            pending.extend((part,depth+1) for part in split)
        elif indices not in seen:
            seen.add(indices)
            grouped = defaultdict(list)
            for i in indices:
                grouped[faces[i].mesh_path].append(faces[i].f_idx)
            batches.append(tuple((shape,tuple(sorted(face_ids))) for shape,face_ids in grouped.items()))
    return tuple(batches)


def read_steps(shapes=None):
    """Main-thread Maya reads only; return immutable NumPy arrays, no API handles."""
    shapes = _selected_mesh_shapes() if shapes is None else list(shapes)
    if not shapes:
        raise ValueError('Select polygon meshes or a group.')
    meshes,skipped = [],[]
    for mesh_index,shape in enumerate(shapes):
        progress = Progress('Reading UVs',mesh_index+1,len(shapes))
        yield progress
        try:
            path = om.MSelectionList().add(shape).getDagPath(0)
            if not path.isVisible():
                skipped.append((shape,'Hidden mesh'))
                continue
            fn = om.MFnMesh(path)
            identity = (cmds.ls(shape,uuid=True)[0],fn.numPolygons)
            uv_set = fn.currentUVSetName()
            u,v = fn.getUVs(uv_set)
            if not len(u):
                skipped.append((shape,'No UVs in the current UV set'))
                continue
            _,shell_ids = fn.getUvShellsIds(uv_set)
            counts,ids = fn.getAssignedUVs(uv_set)
            vertex_counts,vertices = fn.getVertices()
            points = np.column_stack((u,v))
            arrays = [points]+[np.array(values,dtype=np.int64,copy=True) for values in
                (shell_ids,counts,ids,vertex_counts,vertices)]
            arrays.append(np.array(fn.getPoints(om.MSpace.kObject),dtype=np.float64,copy=True)[:,:3].copy())
            for array in arrays:
                array.setflags(write=False)
            meshes.append(MeshSnapshot(shape,identity,*arrays))
        except (RuntimeError, ValueError, IndexError) as error:
            skipped.append((shape,str(error)))
    return Snapshot(tuple(meshes),tuple(skipped))


def analyse_steps(snapshot):
    """Pure snapshot maths; safe to run in a worker without Maya/Qt UI calls."""
    faces,shells,meshes = [],[],{}
    for mesh_index,mesh in enumerate(snapshot.meshes):
        shape,identity = mesh.shape,mesh.identity
        points,shell_ids,counts,ids = mesh.points,mesh.shell_ids,mesh.counts,mesh.ids
        python_points = points.tolist()
        progress = Progress('Preparing shells',mesh_index+1,len(snapshot.meshes))
        mesh_faces = defaultdict(list)
        shell_points = defaultdict(list)
        for uv_id,shell_id in enumerate(shell_ids):
            shell_points[int(shell_id)].append(uv_id)
            if uv_id % 1024 == 1023:
                yield progress
        offset = 0
        for face_id,count in enumerate(counts):
            face_uvs = tuple(ids[offset:offset+count])
            offset += count
            if count >= 3:
                polygon = [tuple(python_points[uv_id]) for uv_id in face_uvs]
                if all(math.isfinite(value) for point in polygon for value in point):
                    shell_id = int(shell_ids[face_uvs[0]])
                    mesh_faces[shell_id].append((face_id,face_uvs,polygon))
            if face_id % 256 == 255:
                yield progress
        for local_id in shell_points:
            entries = mesh_faces.get(local_id)
            if not entries:
                continue
            global_id = len(shells)
            current = []
            for entry_index,(face_id,uvs,polygon) in enumerate(entries):
                current.append(FaceData(shape,face_id,global_id,uvs,polygon))
                if entry_index % 128 == 127:
                    yield progress
            shells.append(ShellExt(global_id,current,shape,[python_points[i] for i in shell_points[local_id]],shell_points[local_id]))
            faces.extend(current)
            yield progress
        meshes[shape] = identity

    index = BoxIndex(shells)
    for i in range(len(shells)):
        index.add(i)
        if i % 256 == 255:
            yield Progress('Indexing stacks',i+1,len(shells))
    processed, shifts = set(), {}
    comparisons = 0
    for i,first in enumerate(shells):
        progress = Progress('Matching stacks',i+1,len(shells))
        if i not in processed:
            processed.add(i)
            group = [i]
            for j in index.candidates(i):
                if j not in processed and (yield from _matches(first,shells[j],progress)):
                    processed.add(j)
                    group.append(j)
                comparisons += 1
                if comparisons % 128 == 0:
                    yield progress
            for step,shell_id in enumerate(group[1:],1):
                shifts[shell_id] = 100.*step
        if i % 64 == 63:
            yield progress
    # The original keeps Python-double shifted coordinates for containment,
    # while MFnMesh.setUVs rounds its native pass's coordinates to float32.
    shifted_points = {mesh.shape:np.array(mesh.points,dtype=np.float64,copy=True) for mesh in snapshot.meshes}
    for shell in shells:
        shift = shifts.get(shell.id,0.)
        if shift:
            shifted_points[shell.mesh_path][shell.uv_indices,0] += shift
    python_shifted = {shape:points.tolist() for shape,points in shifted_points.items()}
    for i,face in enumerate(faces):
        face.uvs = [tuple(python_shifted[face.mesh_path][uv_id]) for uv_id in face.uv_ids]
        face.update_bounds()
        if i % 512 == 511:
            yield Progress('Preparing overlaps',i+1,len(faces))
    index = BoxIndex(faces)
    for i in range(len(faces)):
        index.add(i)
        if i % 256 == 255:
            yield Progress('Indexing faces',i+1,len(faces))
    bad,comparisons = defaultdict(set),0
    for i,first in enumerate(faces):
        for j in index.candidates(i,strict=True):
            second = faces[j]
            if first.shell_id == second.shell_id:
                continue  # The original containment pass compares distinct shells.
            comparisons += 1
            if comparisons % 64 == 0:
                yield Progress('Checking overlaps',i+1,len(faces))
            if first.f_idx in bad[first.mesh_path] and second.f_idx in bad[second.mesh_path]:
                continue  # Only error-face membership is needed, not every pair.
            if _face_overlap(first,second):
                bad[first.mesh_path].add(first.f_idx)
                bad[second.mesh_path].add(second.f_idx)
        if i % 64 == 63:
            yield Progress('Checking overlaps',i+1,len(faces))
    issues = tuple(MeshErrors(shape,meshes[shape][0],meshes[shape][1],tuple(sorted(bad[shape])))
                   for shape in meshes if shape in bad and bad[shape])
    shifted_points = {shape:np.array(points,dtype=np.float32,copy=True) for shape,points in shifted_points.items()}
    python_native = {shape:points.tolist() for shape,points in shifted_points.items()}
    for i,face in enumerate(faces):
        face.uvs = [tuple(python_native[face.mesh_path][uv_id]) for uv_id in face.uv_ids]
        face.update_bounds()
        if i % 512 == 511:
            yield Progress('Preparing native bounds',i+1,len(faces))
    for array in shifted_points.values():
        array.setflags(write=False)
    return PreparedCheck(snapshot,Report(len(meshes),issues,snapshot.skipped),
                         tuple(shifted_points[mesh.shape] for mesh in snapshot.meshes),
                         (yield from _native_batches(faces)))


def native_steps(prepared):
    """Main-thread native pass, with cancellable preparation and guaranteed cleanup.

    API-created helpers are hidden, excluded from saves, and never added to
    Undo. Each atomic operation preserves the scene's existing modified flag.
    No Maya state is held across a worker thread or changed on source meshes.
    """
    root_handle = None
    mapping, paths = {}, {}
    bad = {issue.shape:set(issue.faces) for issue in prepared.report.issues}
    try:
        for index,(mesh,points) in enumerate(zip(prepared.snapshot.meshes,prepared.shifted_points)):
            yield Progress('Preparing Maya overlap check',index+1,len(prepared.snapshot.meshes))
            modified = cmds.file(query=True,modified=True)
            try:
                if root_handle is None:
                    root = om.MFnTransform().create()
                    root_handle = om.MObjectHandle(root)
                    root_fn = om.MFnDependencyNode(root)
                    root_fn.setName('shellShifterUVCheckTmp#')
                    root_fn.setDoNotWrite(True)
                    root_fn.findPlug('visibility',False).setBool(False)
                    root_fn.findPlug('hiddenInOutliner',False).setBool(True)
                fn = om.MFnMesh()
                parent = om.MFnTransform().create(parent=root_handle.object())
                om.MFnDependencyNode(parent).setDoNotWrite(True)
                fn.create([om.MPoint(*point) for point in mesh.geometry_points],
                          mesh.vertex_counts.tolist(),mesh.vertices.tolist(),parent=parent)
                fn.setDoNotWrite(True)
                fn.setUVs(points[:,0].tolist(),points[:,1].tolist())
                fn.assignUVs(mesh.counts.tolist(),mesh.ids.tolist())
                path = om.MDagPath.getAPathTo(fn.object()).fullPathName()
                mapping[path] = mesh.shape
                mapping[om.MDagPath.getAPathTo(parent).fullPathName()] = mesh.shape
                paths[mesh.shape] = path
            finally:
                cmds.file(modified=modified)
        for index,batch in enumerate(prepared.native_batches):
            yield Progress('Maya overlap check',index+1,len(prepared.native_batches))
            components = []
            for shape,face_ids in batch:
                start = end = face_ids[0]
                for face_id in face_ids[1:]+(None,):
                    if face_id == end+1:
                        end = face_id
                        continue
                    span = str(start) if start == end else '{}:{}'.format(start,end)
                    components.append('{}.f[{}]'.format(paths[shape],span))
                    start = end = face_id
            # Same native command as the original; spatial batching prevents
            # an all-scene native call from monopolizing Maya's main thread.
            raw = cmds.polyUVOverlap(components,overlappingComponents=True)
            for component in cmds.ls(raw,flatten=True,long=True) if raw else []:
                path,face = component.rsplit('.f[',1)
                bad.setdefault(mapping[path],set()).add(int(face[:-1]))
        issues = tuple(MeshErrors(mesh.shape,mesh.identity[0],mesh.identity[1],tuple(sorted(bad[mesh.shape])))
                       for mesh in prepared.snapshot.meshes if bad.get(mesh.shape))
        return Report(prepared.report.checked,issues,prepared.report.skipped)
    finally:
        if root_handle is not None and root_handle.isValid() and root_handle.isAlive():
            modified = cmds.file(query=True,modified=True)
            try:
                modifier = om.MDagModifier()
                modifier.deleteNode(root_handle.object())
                modifier.doIt()
            finally:
                cmds.file(modified=modified)


def scan_steps(shapes=None):
    snapshot = yield from read_steps(shapes)
    prepared = yield from analyse_steps(snapshot)
    return (yield from native_steps(prepared))


def scan(shapes=None):
    steps = scan_steps(shapes)
    while True:
        try:
            next(steps)
        except StopIteration as finished:
            return finished.value


def _isolate_meshes(shapes):
    """Replace native isolation with whole meshes in visible modeling views."""
    visible = set(cmds.getPanel(visiblePanels=True) or [])
    transforms = list(dict.fromkeys((cmds.listRelatives(shape,parent=True,fullPath=True) or [shape])[0]
                                    for shape in shapes))
    for panel in cmds.getPanel(type='modelPanel') or []:
        if panel not in visible:
            continue
        if not cmds.modelEditor(panel,query=True,mainListConnection=True):
            cmds.modelEditor(panel,edit=True,mainListConnection='activeList',lockMainConnection=True)
        cmds.isolateSelect(panel,state=True)
        group = cmds.isolateSelect(panel,query=True,viewObjects=True)
        if group:
            cmds.sets(clear=group)
            cmds.sets(transforms,add=group)
            cmds.isolateSelect(panel,update=True)


def select_issues(issues, faces=False, isolate=False):
    """Resolve UUIDs after renames; reject face results after topology changes."""
    targets,owners = [],[]
    for issue in issues:
        matches = cmds.ls(issue.uuid,long=True,allPaths=True) or []
        if not matches:
            continue
        shape = issue.shape if issue.shape in matches else matches[0]
        if faces:
            if cmds.polyEvaluate(shape,face=True) != issue.face_count:
                raise ValueError('Topology changed. Run the check again.')
            owners.append(shape)
            targets.extend('{}.f[{}]'.format(shape,i) for i in issue.faces)
        else:
            targets.append((cmds.listRelatives(shape,parent=True,fullPath=True) or [shape])[0])
    if not targets:
        raise ValueError('Result meshes were deleted. Run the check again.')
    if isolate and owners:
        _isolate_meshes(owners)
    if faces:
        cmds.selectMode(component=True)
        cmds.selectType(allComponents=False)
        cmds.selectType(polymeshFace=True)
        cmds.hilite(owners,replace=True)
    else:
        cmds.selectMode(object=True)
    cmds.select(list(dict.fromkeys(targets)),replace=True)

