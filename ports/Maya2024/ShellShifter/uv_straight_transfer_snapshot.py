"""Scene-persistent pre-pack UVs and normal frames; Maya-thread access only."""
import base64
from dataclasses import replace
import hashlib
import io
import json
import uuid
import zipfile
from uv_straight_deps import np
import maya.cmds as cmds
import maya.api.OpenMaya as om
from uv_straight_transfer_models import MeshSnapshot,UvSetSnapshot
from uv_straight_transfer_basis import frames_from_axes

ATTR='uvStraightTextureTransfer'
SOURCE_PREFIX='SS_Transfer_Source'


def mesh_fn(path):
    selection=om.MSelectionList();selection.add(path)
    return om.MFnMesh(selection.getDagPath(0))


def digest(*arrays):
    h=hashlib.sha256()
    for array in arrays:
        a=np.ascontiguousarray(array);h.update(str((a.shape,a.dtype)).encode());h.update(a.tobytes())
    return h.hexdigest()


def compatible_geometry(before,after):
    # Native UV/history nodes can renormalize float32 normals by one ULP.
    # UV edits must not be mistaken for a changed surface in that case.
    return ((before.topology_hash,before.geometry_hash,before.triangulation_hash)==
            (after.topology_hash,after.geometry_hash,after.triangulation_hash) and
            before.normals.shape==after.normals.shape and
            np.allclose(before.normals,after.normals,rtol=0,atol=5e-7))


def metadata(path):
    if not cmds.attributeQuery(ATTR,node=path,exists=True):return {'schema':1,'sets':{}}
    raw=cmds.getAttr(path+'.'+ATTR) or ''
    data=json.loads(raw) if raw else {'schema':1,'sets':{}}
    if data.get('schema')!=1:raise ValueError('Unknown saved source version: '+path)
    return data


def set_metadata(path,data):
    if not cmds.attributeQuery(ATTR,node=path,exists=True):cmds.addAttr(path,longName=ATTR,dataType='string')
    cmds.setAttr(path+'.'+ATTR,json.dumps(data,separators=(',',':'),ensure_ascii=False),type='string')


def capture(path,uv_set):
    fn=mesh_fn(path)
    counts,vertices=fn.getVertices();counts=np.asarray(counts,dtype=np.int32);vertices=np.asarray(vertices,dtype=np.int32)
    points=np.asarray([(p.x,p.y,p.z) for p in fn.getPoints(om.MSpace.kObject)],dtype=np.float64)
    u,v=fn.getUVs(uv_set);uvs=np.column_stack((u,v)).astype(np.float64)
    assigned,ids=fn.getAssignedUVs(uv_set);assigned=np.asarray(assigned,dtype=np.int32)
    offsets=np.r_[0,np.cumsum(counts)];corners=np.full(len(vertices),-1,dtype=np.int32);cursor=0
    for face,n in enumerate(assigned):
        if n not in (0,counts[face]):raise ValueError('Incomplete UVs on face {}: {}'.format(face,path))
        if n:corners[offsets[face]:offsets[face+1]]=ids[cursor:cursor+n];cursor+=n
    tri_counts,tri_vertices=fn.getTriangles();tri_faces=[];tri_corners=[];cursor=0
    for face,n in enumerate(tri_counts):
        start,end=offsets[face:face+2];lookup={int(vertex):int(start+i) for i,vertex in enumerate(vertices[start:end])}
        if len(lookup)!=end-start:raise ValueError('Duplicate vertex on face {}: {}'.format(face,path))
        for _ in range(n):
            tri_corners.append(tuple(lookup[int(i)] for i in tri_vertices[cursor:cursor+3]));tri_faces.append(face);cursor+=3
    tri_corners=np.asarray(tri_corners,dtype=np.int32).reshape(-1,3);tri_faces=np.asarray(tri_faces,dtype=np.int32)
    normals=np.zeros((len(vertices),3));tangents=np.zeros_like(normals);bitangents=np.zeros_like(normals)
    vectors=lambda items:np.asarray([(p.x,p.y,p.z) for p in items],dtype=np.float64)
    for face in range(len(counts)):
        start,end=offsets[face:face+2]
        normals[start:end]=vectors(fn.getFaceVertexNormals(face,om.MSpace.kObject))
        if assigned[face]:
            try:
                tangents[start:end]=vectors(fn.getFaceVertexTangents(face,om.MSpace.kObject,uv_set))
                bitangents[start:end]=vectors(fn.getFaceVertexBinormals(face,om.MSpace.kObject,uv_set))
            except RuntimeError:pass
    # Only missing/degenerate Maya axes use per-triangle UV derivatives. Other
    # materials may contain unmapped faces; they cannot invalidate this source.
    bad=(np.linalg.norm(tangents,axis=1)<1e-10)|(np.linalg.norm(bitangents,axis=1)<1e-10)
    for triangle in tri_corners[np.any(bad[tri_corners],axis=1)]:
        uv_ids=corners[triangle]
        if np.any(uv_ids<0):continue
        xyz=points[vertices[triangle]];tex=uvs[uv_ids];du,dv=tex[1:]-tex[0]
        determinant=du[0]*dv[1]-du[1]*dv[0]
        if abs(determinant)<1e-14:continue
        a,b=xyz[1:]-xyz[0]
        tangent=(a*dv[1]-b*du[1])/determinant;bitangent=(b*du[0]-a*dv[0])/determinant
        for corner in triangle:
            if bad[corner]:tangents[corner]+=tangent;bitangents[corner]+=bitangent
    nlength=np.linalg.norm(normals,axis=1);valid_n=nlength>1e-10
    normals[~valid_n]=(0,0,1);normals/=np.maximum(nlength[:,None],1e-10);normals[~valid_n]=(0,0,1)
    projected=tangents-normals*np.sum(normals*tangents,axis=1,keepdims=True)
    invalid=(np.linalg.norm(projected,axis=1)<1e-10)|(np.linalg.norm(bitangents,axis=1)<1e-10)
    for corner in np.flatnonzero(invalid):
        n=normals[corner];axis=np.eye(3)[int(np.argmin(np.abs(n)))];tangents[corner]=np.cross(axis,n)
        bitangents[corner]=np.cross(n,tangents[corner])
    frames=frames_from_axes(tangents,bitangents,normals)
    shells,shell_ids=fn.getUvShellsIds(uv_set)
    uv=UvSetSnapshot(uv_set,uvs,assigned,corners,np.asarray(shell_ids,dtype=np.int32),shells)
    return MeshSnapshot(path,fn.uuid().asString(),points,counts,vertices,tri_faces,tri_corners,
                        normals.astype(np.float32),frames,uv,digest(counts,vertices),digest(points),
                        digest(tri_faces,tri_corners),digest(normals.astype(np.float32)),
                        {'invalid_frame_corners':np.flatnonzero(invalid).tolist()})


def encode(snapshot):
    buffer=io.BytesIO()
    fields=('points','face_counts','face_vertices','triangle_faces','triangle_corners','normals','frames')
    arrays={name:getattr(snapshot,name) for name in fields}
    arrays.update({name:getattr(snapshot.uv_set,name) for name in ('uvs','face_uv_counts','corner_uv_ids','uv_shell_ids')})
    np.savez_compressed(buffer,**arrays)
    return dict(blob=base64.b64encode(buffer.getvalue()).decode('ascii'),name=snapshot.name,uuid=snapshot.uuid,
                uv_set=snapshot.uv_set.name,shell_count=snapshot.uv_set.shell_count,
                topology_hash=snapshot.topology_hash,geometry_hash=snapshot.geometry_hash,
                triangulation_hash=snapshot.triangulation_hash,normal_hash=snapshot.normal_hash,
                metadata=dict(snapshot.metadata))


def decode(data):
    payload=base64.b64decode(data['blob'],validate=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        if sum(item.file_size for item in archive.infolist())>256*1024*1024:
            raise ValueError('The saved source exceeds the allowed size.')
    with np.load(io.BytesIO(payload),allow_pickle=False) as arrays:
        uv=UvSetSnapshot(data['uv_set'],arrays['uvs'],arrays['face_uv_counts'],arrays['corner_uv_ids'],arrays['uv_shell_ids'],data['shell_count'])
        return MeshSnapshot(data['name'],data['uuid'],*(arrays[name] for name in
            ('points','face_counts','face_vertices','triangle_faces','triangle_corners','normals','frames')),uv,
            *(data[name] for name in ('topology_hash','geometry_hash','triangulation_hash','normal_hash')),data.get('metadata',{}))


def source(path,state,entry):
    """Read the immutable source, independent of its editable UV-set mirror.

    The blob contains original coordinates, corner assignments and tangent
    frames as one consistent snapshot. Maya's extra UV set is a convenient
    scene copy; edits, reindexing or deletion of that copy cannot invalidate
    the original data or justify recapturing the already packed working UV.
    Geometry, assignments and textures are checked separately by the caller.
    """
    stored=state['sources'][entry['source']];snapshot=decode(stored)
    if snapshot.uuid!=mesh_fn(path).uuid().asString():raise ValueError('The source belongs to a different mesh: '+path)
    return snapshot


def capture_uv(path,uv_set):
    fn=mesh_fn(path)
    if uv_set not in fn.getUVSetNames():raise ValueError('The source UV set was deleted: '+path+' / '+uv_set)
    u,v=fn.getUVs(uv_set);counts,ids=fn.getAssignedUVs(uv_set);faces=fn.getVertices()[0]
    corners=np.full(sum(faces),-1,dtype=np.int32);cursor=offset=0
    for count,assigned in zip(faces,counts):
        if assigned:corners[offset:offset+count]=ids[cursor:cursor+assigned];cursor+=assigned
        offset+=count
    return np.column_stack((u,v)),np.asarray(counts,dtype=np.int32),corners


def preserve(prepared, progress=None):
    """Called inside the successful packing Undo chunk, before the UV commit."""
    from uv_straight_transfer_graph import assignments,describe,validate_source_uv
    touched={}
    for mesh,ids,*_ in prepared['records']:
        record=touched.setdefault(mesh.name,dict(mesh=mesh,ids=set()))
        record['ids'].update(ids)
    affected=set()
    graph_cache={}
    link_cache={}
    touched_assignments={}
    for path,record in touched.items():
        fn=mesh_fn(path);changed_faces=set();uv_counts,uv_ids=fn.getAssignedUVs(record['mesh'].uv_set);cursor=0
        for face,n in enumerate(uv_counts):
            if any(int(i) in record['ids'] for i in uv_ids[cursor:cursor+n]):changed_faces.add(face)
            cursor+=n
        touched_assignments[path]=assignments(path)
        affected.update(material['uuid'] for material in touched_assignments[path]
                        if changed_faces.intersection(material['faces']))
    # An assigned material is one texture atlas across all its meshes. Capture
    # unchanged recipients too, so reconnecting the material remains correct.
    roster=sorted(set(cmds.ls(dag=True,type='mesh',long=True,allPaths=True,noIntermediate=True) or []))
    for index, path in enumerate(roster):
        if progress: progress(index, len(roster))
        assigned=touched_assignments[path] if path in touched_assignments else assignments(path)
        materials=[m for m in assigned if m['uuid'] in affected]
        if not materials:continue
        fn=mesh_fn(path);uv_set=fn.currentUVSetName();data=metadata(path)
        if path in touched:uv_set=touched[path]['mesh'].uv_set
        state=data['sets'].setdefault(uv_set,{'sources':{},'materials':{}});fresh=[];updated=False
        for material in materials:
            key=material['uuid'];previous=state['materials'].get(key)
            if previous and previous['pending']:
                old=source(path,state,previous);current=capture(path,uv_set)
                if not compatible_geometry(old,current):
                    raise ValueError('Geometry changed after saving the source: '+path)
                if previous['faces']!=material['faces']:raise ValueError('Material assignment changed: '+material['name'])
                if previous.get('transferred_files'):
                    previous['transferred_files']=[];updated=True
                # Keep the original images/graph through any number of repacks.
                continue
            fresh.append(material)
        if fresh:
            for material in fresh:
                key=material['uuid']
                if key not in graph_cache:graph_cache[key]=describe(material['name'],tolerant=True)
            graphs={material['uuid']:graph_cache[material['uuid']] for material in fresh}
            fresh=[material for material in fresh if graphs[material['uuid']]['files']]
            if not fresh:continue
            graphs={material['uuid']:graphs[material['uuid']] for material in fresh}
            source_errors={}
            for key,graph in graphs.items():
                source_errors[key]={}
                for texture in graph['files']:
                    try:validate_source_uv(dict(graph,files=[texture]),path,uv_set,link_cache=link_cache)
                    except (ValueError,RuntimeError) as error:source_errors[key][texture['uuid']]=str(error)
            # The immutable blob is the authoritative source (see source()).
            # An extra UV-set mirror is not consumed by transfer, but switching
            # sets here can stall Maya's UV Editor across a large selection.
            snapshot=capture(path,uv_set)
            epoch=uuid.uuid4().hex;stored=encode(snapshot)
            state['sources'][epoch]=stored
            for material in fresh:
                state['materials'][material['uuid']]=dict(source=epoch,pending=True,faces=material['faces'],
                    material_name=material['name'],graph=graphs[material['uuid']],
                    source_errors=source_errors[material['uuid']])
        if fresh or updated:set_metadata(path,data)
    if progress: progress(len(roster), len(roster))


def restrict(snapshot,faces):
    mask=np.isin(snapshot.triangle_faces,np.asarray(faces,dtype=np.int32))
    triangles=snapshot.triangle_corners[mask];face_ids=snapshot.triangle_faces[mask]
    if np.any(snapshot.uv_set.corner_uv_ids[triangles]<0):raise ValueError('The selected material has faces without UVs: '+snapshot.name)
    xyz=snapshot.points[snapshot.face_vertices[triangles]]
    nonzero=np.linalg.norm(np.cross(xyz[:,1]-xyz[:,0],xyz[:,2]-xyz[:,0]),axis=1)>1e-14
    triangles=triangles[nonzero];face_ids=face_ids[nonzero]
    if not len(triangles):raise ValueError('The material has no triangles to transfer: '+snapshot.name)
    return replace(snapshot,triangle_corners=triangles,triangle_faces=face_ids)


def transfer_parts(original,target,faces):
    """Keep identical corner correspondence while discarding unrasterizable UVs.

    A zero-area target triangle owns no texture surface. It must not abort
    unrelated triangles, nor contribute invalid tangent corners to their maps.
    Never discard a positive-area target because its source frame is invalid.
    """
    first,last=restrict(original,faces),restrict(target,faces)
    if not np.array_equal(first.triangle_corners,last.triangle_corners):
        raise ValueError('Source and Target triangulation do not match: '+target.name)
    source_uv=first.uv_set.uvs[first.uv_set.corner_uv_ids[first.triangle_corners]]
    if not np.isfinite(source_uv).all():raise ValueError('Source UVs are not finite: '+original.name)
    uv=last.uv_set.uvs[last.uv_set.corner_uv_ids[last.triangle_corners]]
    if not np.isfinite(uv).all():raise ValueError('Target UVs are not finite: '+target.name)
    a,b=uv[:,1]-uv[:,0],uv[:,2]-uv[:,0]
    valid=np.abs(a[:,0]*b[:,1]-a[:,1]*b[:,0])>1e-14
    skipped=last.triangle_faces[~valid]
    info=dict(triangles=int((~valid).sum()),faces=np.unique(skipped).tolist(),
              kept_faces=np.unique(last.triangle_faces[valid]).tolist())
    if not np.any(valid):return None,None,info
    if np.all(valid):return first,last,info
    return (replace(first,triangle_corners=first.triangle_corners[valid],triangle_faces=first.triangle_faces[valid]),
            replace(last,triangle_corners=last.triangle_corners[valid],triangle_faces=last.triangle_faces[valid]),info)


def frame_error(part,role):
    invalid=part.metadata.get('invalid_frame_corners',())
    affected=np.isin(part.triangle_corners,invalid).any(axis=1)
    uv=part.uv_set.uvs[part.uv_set.corner_uv_ids[part.triangle_corners]]
    a,b=uv[:,1]-uv[:,0],uv[:,2]-uv[:,0]
    area=a[:,0]*b[:,1]-a[:,1]*b[:,0]
    # Some Maya versions return arbitrary finite axes even for collapsed UVs.
    affected|=(~np.isfinite(area))|(np.abs(area)<=1e-14)
    if not np.any(affected):return ''
    faces=np.unique(part.triangle_faces[affected]).tolist()
    return ('{} UV tangent frame is invalid on {}: faces {}{}. Repair the UVs on these faces before transferring normals.'
            .format(role,part.name,', '.join(map(str,faces[:12])),'...' if len(faces)>12 else ''))


def combine(parts):
    """Combine corresponding source/target parts in identical corner order."""
    if len(parts)==1:return parts[0]
    vertices=[];faces=[];triangles=[];ids=[];shell_ids=[];vo=fo=co=uo=so=0
    for part in parts:
        vertices.append(part.face_vertices+vo);faces.append(part.triangle_faces+fo);triangles.append(part.triangle_corners+co)
        assigned=part.uv_set.corner_uv_ids.copy();assigned[assigned>=0]+=uo;ids.append(assigned)
        shell_ids.append(part.uv_set.uv_shell_ids+so)
        vo+=len(part.points);fo+=len(part.face_counts);co+=len(part.face_vertices);uo+=len(part.uv_set.uvs);so+=part.uv_set.shell_count
    cat=lambda name:np.concatenate([getattr(part,name) for part in parts])
    uv=UvSetSnapshot('material',np.concatenate([part.uv_set.uvs for part in parts]),
        np.concatenate([part.uv_set.face_uv_counts for part in parts]),np.concatenate(ids),np.concatenate(shell_ids),so)
    return MeshSnapshot('material','',cat('points'),cat('face_counts'),np.concatenate(vertices),np.concatenate(faces),
        np.concatenate(triangles),cat('normals'),cat('frames'),uv,'','','','',{})
