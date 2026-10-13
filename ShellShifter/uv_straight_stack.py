"""Stack selected isometric UV shells onto the least distorted current layout.

Scene reads/commits stay on Maya's thread. Matching consumes plain arrays and
UV topology, so the palette can run it on a cancellable background worker.
"""
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
import math
import numpy as np
import maya.api.OpenMaya as om
import maya.cmds as cmds
from uv_straight_distortion import shell_neutral_scale


@dataclass
class Shell:
    mesh_index: int
    shell_id: int
    uv_ids: np.ndarray
    uv: np.ndarray
    xyz: np.ndarray
    faces: tuple
    labels: tuple
    edge_faces: dict
    face_keys: tuple
    key: tuple
    edge_lengths: np.ndarray
    scale: float
    score: tuple
    normalized_uv: np.ndarray


@dataclass
class Plan:
    groups: list
    unmatched: list
    total: int

    @property
    def stacked(self):
        return sum(len(targets) for keeper, targets in self.groups)


def _quality(faces, points, uv):
    """Unclipped principal stretch plus fold area; independent of display state."""
    scales, weights, signs, face_indices = [], [], [], []
    for face_index, (ids, vertices, triangles) in enumerate(faces):
        if not triangles:
            continue
        tri = np.asarray(triangles, dtype=np.int32)
        xyz = points[np.asarray(vertices)[tri]]
        coords = uv[np.asarray(ids)[tri]]
        ab, ac = xyz[:,1]-xyz[:,0], xyz[:,2]-xyz[:,0]
        length = np.linalg.norm(ab, axis=1)
        area2 = np.linalg.norm(np.cross(ab,ac), axis=1)
        valid = (length>1e-15) & (area2>1e-15)
        if not np.any(valid):
            continue
        ab, ac, length, area2, coords = ab[valid],ac[valid],length[valid],area2[valid],coords[valid]
        x = np.einsum('ij,ij->i',ab,ac)/length
        y = area2/length
        p = (coords[:,1]-coords[:,0])/length[:,None]
        q = ((coords[:,2]-coords[:,0])-p*x[:,None])/y[:,None]
        det = p[:,0]*q[:,1]-q[:,0]*p[:,1]
        trace = np.sum(p*p+q*q,axis=1)
        high = np.sqrt(np.maximum(0.,.5*(trace+np.sqrt(np.maximum(0.,trace*trace-4*det*det)))))
        low = np.abs(det)/np.maximum(high,1e-30)
        scales.extend(zip(low,high))
        weights.extend(area2*.5)
        signs.extend(det)
        face_indices.extend([face_index]*len(det))
    if not weights:
        return (1.,float('inf')), 0.
    scales,weights,signs = np.asarray(scales),np.asarray(weights),np.asarray(signs)
    world = float(weights.sum())
    # A collapsed UV region cannot be the preferred template.
    if not np.isfinite(scales).all() or np.any(scales<=1e-15):
        return (1.,float('inf')), math.sqrt(world)
    face_indices = np.asarray(face_indices,dtype=np.int32)
    area = np.bincount(face_indices,weights=weights,minlength=len(faces))
    mapped = np.bincount(face_indices,weights=weights*scales[:,0]*scales[:,1],minlength=len(faces))
    density = np.sqrt(mapped/np.maximum(area,1e-30))
    neutral = shell_neutral_scale(density,area)
    logs = np.log(scales/neutral)
    rms = math.sqrt(float(np.sum(weights[:,None]*logs*logs)/(2*world)))
    flipped = min(float(weights[signs<0].sum()),float(weights[signs>0].sum()))/world
    return (flipped,rms), math.sqrt(world)


def _shell(mesh_index, shell_id, uv_ids, source_faces, uv, world):
    ids = np.asarray(sorted(uv_ids),dtype=np.int32)
    offsets = {int(value):i for i,value in enumerate(ids)}
    vertices = {}
    faces, quality_faces = [], []
    for face, geometric, triangles in source_faces:
        local = tuple(offsets[i] for i in face)
        if len(set(local)) != len(local) or len(local)<3:
            return None
        for index,vertex in zip(face,geometric):
            if index in vertices and vertices[index] != vertex:
                return None
            vertices[index]=vertex
        faces.append(local)
        quality_faces.append((face,geometric,triangles))
    if not faces or len(vertices)!=len(ids):
        return None
    score, scale = _quality(quality_faces,world,uv)
    if scale<=1e-15:
        return None
    xyz = world[[vertices[int(i)] for i in ids]]/scale
    values = uv[ids].copy()
    neighbors, incident, edges = defaultdict(set),defaultdict(list),defaultdict(list)
    for index,face in enumerate(faces):
        for vertex in face:
            incident[vertex].append(len(face))
        for a,b in zip(face,face[1:]+face[:1]):
            neighbors[a].add(b);neighbors[b].add(a)
            edges[tuple(sorted((a,b)))].append(index)
    # Nonmanifold UV surfaces have no unambiguous face-to-face propagation.
    if any(len(users)>2 for users in edges.values()):
        return None
    boundary = Counter(v for edge,users in edges.items() if len(users)==1 for v in edge)
    labels = tuple((len(neighbors[i]),boundary[i],tuple(sorted(incident[i]))) for i in range(len(ids)))
    keys = tuple((len(face),tuple(sorted(labels[i] for i in face))) for face in faces)
    key = (len(ids),len(faces),tuple(sorted(Counter(keys).items())))
    lengths = np.sort([np.linalg.norm(xyz[a]-xyz[b]) for a,b in edges])
    centered = values-values.mean(axis=0)
    norm = float(np.linalg.norm(centered))
    normalized = centered/max(norm,1e-15)
    return Shell(mesh_index,shell_id,ids,values,xyz,tuple(faces),labels,dict(edges),keys,
                 key,lengths,scale,score,normalized)


def prepare():
    """Snapshot complete selected shells in each mesh's current UV set."""
    import uv_straight as uv_tool
    selection, meshes = uv_tool._selection()
    # Instances share one UV array. Merge their component selections and
    # commit that underlying mesh only once.
    unique = {}
    for mesh in meshes:
        key = (om.MObjectHandle(mesh.fn.object()).hashCode(),mesh.uv_set)
        if key in unique:
            unique[key].selected.update(mesh.selected)
        else:
            unique[key]=mesh
    meshes=list(unique.values())
    records, snapshots, skipped = [], [], 0
    for mesh_index,mesh in enumerate(meshes):
        u,v = mesh.fn.getUVs(mesh.uv_set)
        values = np.column_stack((u,v))
        world = np.asarray([(p.x,p.y,p.z) for p in mesh.fn.getPoints(om.MSpace.kWorld)])
        face_data = defaultdict(list)
        iterator = om.MItMeshPolygon(mesh.path)
        while not iterator.isDone():
            try:
                geometric = tuple(iterator.getVertices())
                face = tuple(iterator.getUVIndex(i,mesh.uv_set) for i in range(len(geometric)))
                _, triangles = iterator.getTriangles()
                local = {vertex:i for i,vertex in enumerate(geometric)}
                triples = tuple(tuple(local[triangles[i+k]] for k in range(3))
                                for i in range(0,len(triangles),3))
                if face:
                    face_data[mesh.shell_ids[face[0]]].append((face,geometric,triples))
            except RuntimeError:
                pass
            iterator.next()
        counts,assigned = mesh.fn.getAssignedUVs(mesh.uv_set)
        snapshots.append(dict(mesh=mesh,handle=om.MObjectHandle(mesh.fn.object()),values=values,
                              counts=tuple(counts),assigned=tuple(assigned),world=world,
                              vertices=tuple(mesh.fn.getVertices()[1])))
        for ids in mesh.shells():
            shell_id = int(mesh.shell_ids[min(ids)])
            record = _shell(mesh_index,shell_id,ids,face_data[shell_id],values,world)
            if record is None:
                skipped+=1
            else:
                records.append(record)
    if len(records)<2:
        raise ValueError('Select at least two UV shells with matching surface topology.')
    return dict(selection=tuple(selection),meshes=snapshots,records=records,skipped=skipped)


def _same_face(source, target, face_index, target_order, tolerance):
    a = source.xyz[list(source.faces[face_index])]
    b = target.xyz[list(target_order)]
    if len(a)<=16:
        # All distances, including diagonals: equal edge lengths alone can
        # incorrectly match a square and a rhombus.
        da = np.linalg.norm(a[:,None]-a[None,:],axis=2)
        db = np.linalg.norm(b[:,None]-b[None,:],axis=2)
        return np.allclose(da,db,rtol=tolerance,atol=tolerance*1e-3)
    # Large ngons need O(n) memory; rigid alignment admits reflections too.
    a,b = a-a.mean(axis=0),b-b.mean(axis=0)
    u,_,v = np.linalg.svd(a.T@b)
    error = np.linalg.norm(a@(u@v)-b,axis=1)
    extent = max(float(np.linalg.norm(a,axis=1).max()),1e-8)
    return float(error.max())<=tolerance*extent


def _propagate(source,target,seed,target_face,target_order,tolerance):
    mapping, inverse, face_map, used_faces = {},{}, {},set()
    queue = deque([(seed,target_face,target_order)])
    while queue:
        face_index,other,order = queue.popleft()
        if face_index in face_map:
            if face_map[face_index]!=other:
                return None
            continue
        if other in used_faces or len(source.faces[face_index])!=len(order):
            return None
        for a,b in zip(source.faces[face_index],order):
            if (source.labels[a]!=target.labels[b] or
                    (a in mapping and mapping[a]!=b) or (b in inverse and inverse[b]!=a)):
                return None
        if not _same_face(source,target,face_index,order,tolerance):
            return None
        for a,b in zip(source.faces[face_index],order):
            mapping[a],inverse[b]=b,a
        face_map[face_index]=other;used_faces.add(other)
        face = source.faces[face_index]
        for a,b in zip(face,face[1:]+face[:1]):
            users = source.edge_faces[tuple(sorted((a,b)))]
            other_users = target.edge_faces.get(tuple(sorted((mapping[a],mapping[b]))),())
            if len(users)!=len(other_users):
                return None
            if len(users)==1:
                continue
            next_face = next(i for i in users if i!=face_index)
            next_other = next(i for i in other_users if i!=other)
            if next_face in face_map:
                if face_map[next_face]!=next_other:
                    return None
                continue
            source_loop, target_loop = source.faces[next_face], target.faces[next_other]
            if len(source_loop)!=len(target_loop):
                return None
            pivot = source_loop.index(a);target_pivot = target_loop.index(mapping[a])
            choices = []
            for direction in (1,-1):
                aligned = tuple(target_loop[(target_pivot+direction*(i-pivot))%len(target_loop)]
                                for i in range(len(source_loop)))
                if all(i not in mapping or mapping[i]==j for i,j in zip(source_loop,aligned)):
                    choices.append(aligned)
            if len(choices)!=1:
                return None
            queue.append((next_face,next_other,choices[0]))
    if len(mapping)!=len(source.uv_ids) or len(face_map)!=len(source.faces):
        return None
    # Inverse permutation: every destination UV receives its corresponding
    # template UV, including internal vertices, rather than only the outline.
    return np.asarray([inverse[i] for i in range(len(target.uv_ids))],dtype=np.int32)


def match_steps(source,target,tolerance=1e-4):
    if source.key!=target.key or len(source.edge_lengths)!=len(target.edge_lengths):
        return None
    if not np.allclose(source.edge_lengths,target.edge_lengths,rtol=tolerance,atol=tolerance*1e-3):
        return None
    counts = Counter(source.face_keys)
    seed = min(range(len(source.faces)),key=lambda i:(counts[source.face_keys[i]],len(source.faces[i]),i))
    first = source.faces[seed]
    candidates=[]
    for other,key in enumerate(target.face_keys):
        if key!=source.face_keys[seed]:
            continue
        face = target.faces[other]
        for direction in (1,-1):
            # Rank seed correspondences by current UV orientation. Symmetric
            # copies retain the closest orientation instead of rotating at random.
            for offset in range(len(face)):
                order=tuple(face[(offset+direction*i)%len(face)] for i in range(len(face)))
                if all(source.labels[a]==target.labels[b] for a,b in zip(first,order)):
                    merit=float(np.sum((source.normalized_uv[list(first)]-target.normalized_uv[list(order)])**2))
                    candidates.append((merit,other,order))
    for _,other,order in sorted(candidates):
        yield None
        mapping = _propagate(source,target,seed,other,order,tolerance)
        if mapping is not None:
            return mapping
    return None


def analyse_steps(records):
    buckets=defaultdict(list)
    for index,shell in enumerate(records):
        buckets[shell.key].append(index)
    groups,unmatched=[],[]
    processed=0
    for indices in buckets.values():
        remaining=sorted(indices,key=lambda i:(records[i].score,i))
        while remaining:
            keeper=remaining.pop(0)
            matched=[];left=[]
            for other in remaining:
                steps=match_steps(records[keeper],records[other])
                try:
                    while True:
                        next(steps)
                        yield (processed,len(records))
                except StopIteration as done:
                    mapping=done.value
                if mapping is None:
                    left.append(other)
                else:
                    matched.append((other,mapping))
            if matched and math.isfinite(records[keeper].score[1]):
                groups.append((keeper,matched))
                processed+=1+len(matched)
            else:
                unmatched.extend([keeper]+[other for other,_ in matched])
                processed+=1+len(matched)
            remaining=left
            yield (processed,len(records))
    return Plan(groups,unmatched,len(records))


def apply(prepared,plan):
    """Validate the snapshot and commit all destination arrays as one Undo."""
    import uv_straight as uv_tool
    from uv_straight_pack_commit import commit
    if tuple(cmds.ls(selection=True,long=True,flatten=True) or [])!=prepared['selection']:
        raise ValueError('The selection changed during stacking. Select the shells and try again.')
    entries={}
    for index,snapshot in enumerate(prepared['meshes']):
        handle=snapshot['handle']
        if not handle.isValid() or not handle.isAlive():
            raise ValueError('A selected mesh no longer exists.')
        mesh=snapshot['mesh'];fn=om.MFnMesh(mesh.path)
        u,v=fn.getUVs(mesh.uv_set)
        counts,assigned=fn.getAssignedUVs(mesh.uv_set)
        world=np.asarray([(p.x,p.y,p.z) for p in fn.getPoints(om.MSpace.kWorld)])
        if (fn.currentUVSetName()!=mesh.uv_set or tuple(counts)!=snapshot['counts'] or
                tuple(assigned)!=snapshot['assigned'] or tuple(fn.getVertices()[1])!=snapshot['vertices'] or
                not np.array_equal(np.column_stack((u,v)),snapshot['values']) or
                not np.array_equal(world,snapshot['world'])):
            raise ValueError('Geometry or UVs changed during stacking. Run stacking again.')
    for keeper,targets in plan.groups:
        source=prepared['records'][keeper]
        for other,mapping in targets:
            target=prepared['records'][other];index=target.mesh_index
            after=source.uv[mapping]
            if np.array_equal(after,target.uv):
                continue
            if index not in entries:
                snapshot=prepared['meshes'][index]
                entries[index]=dict(handle=snapshot['handle'],uv_set=snapshot['mesh'].uv_set,
                                    before=snapshot['values'].copy(),after=snapshot['values'].copy(),ids=[],shells=0)
            entry=entries[index]
            entry['after'][target.uv_ids]=after
            entry['ids'].extend(int(i) for i in target.uv_ids);entry['shells']+=1
    if entries:
        with uv_tool._undo('ShellShifter Stack Similar Shells'):
            for index,entry in entries.items():
                mesh=prepared['meshes'][index]['mesh']
                current=uv_tool._marks(mesh)
                moved=set(entry['ids']);groups=[]
                for original in current:
                    group=dict(original)
                    if set(group['ids'])<=moved:
                        points=entry['after'][group['ids']]
                        span=np.ptp(points,axis=0)
                        group['axis']='H' if span[0]>=span[1] else 'V'
                    groups.append(group)
                if groups!=current:
                    import json
                    data=uv_tool._mark_data(mesh)
                    data[mesh.uv_set]={'topology':uv_tool._topology(mesh),'groups':groups,'format':2}
                    entry['attributes']={uv_tool.MARK_ATTR:json.dumps(data)}
                for name in ('before','after'):
                    entry[name]=entry[name].T.tolist()
            commit(list(entries.values()))
    return dict(groups=len(plan.groups),stacked=plan.stacked,
                changed=sum(entry['shells'] for entry in entries.values()),
                unmatched=len(plan.unmatched)+prepared['skipped'],selected=plan.total+prepared['skipped'])


def stack_selected_shells():
    prepared=prepare();steps=analyse_steps(prepared['records'])
    try:
        while True:
            next(steps)
    except StopIteration as done:
        return apply(prepared,done.value)
