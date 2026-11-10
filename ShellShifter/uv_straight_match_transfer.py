"""Ordered reference-to-mesh UV transfer, independent of destination UV seams."""
from collections import defaultdict
import json
import numpy as np
import maya.api.OpenMaya as om
import maya.cmds as cmds


def _snapshot(path):
    fn=om.MFnMesh(path);uv_set=fn.currentUVSetName()
    u,v=fn.getUVs(uv_set);counts,assigned=fn.getAssignedUVs(uv_set)
    sizes,vertices=fn.getVertices();offset=0;faces=[]
    for size in sizes:
        faces.append(tuple(vertices[offset:offset+size]));offset+=size
    tri_counts,tri_vertices=fn.getTriangles();offset=0;triples=[]
    for face,count in zip(faces,tri_counts):
        lookup={vertex:i for i,vertex in enumerate(face)}
        triples.append(tuple(tuple(lookup[tri_vertices[offset+i+k]] for k in range(3))
                             for i in range(0,count*3,3)))
        offset+=count*3
    return dict(name=path.fullPathName(),path=om.MDagPath(path),handle=om.MObjectHandle(fn.object()),
                uv_set=uv_set,values=np.asarray([u,v],dtype=float).T,
                counts=tuple(counts),assigned=tuple(assigned),faces=tuple(faces),triples=tuple(triples),
                xyz=np.asarray([(p.x,p.y,p.z) for p in fn.getPoints(om.MSpace.kWorld)]),
                owners=tuple(fn.getUvShellsIds(uv_set)[1]))


def prepare():
    """Capture object order without expanding selected parents into a scene scope."""
    # Read the chronological list directly, then remove component suffixes
    # without sorting or collapsing distinct DAG instance paths.
    nodes=list(dict.fromkeys(item.split('.',1)[0]
                            for item in cmds.ls(orderedSelection=True,long=True) or []))
    if len(nodes)<2:raise ValueError('Select the reference mesh first, then one or more target meshes.')
    snapshots=[];failed=[];seen=set()
    for index,node in enumerate(nodes):
        try:
            shapes=([node] if cmds.nodeType(node)=='mesh' else
                    cmds.listRelatives(node,shapes=True,noIntermediate=True,fullPath=True,type='mesh') or [])
            if len(shapes)!=1:raise ValueError('Select a mesh, not an Outliner group.')
            selection=om.MSelectionList();selection.add(shapes[0]);path=selection.getDagPath(0)
            fn=om.MFnMesh(path);key=fn.uuid().asString()
            if key in seen:continue  # Instances share UV arrays; never overwrite the reference.
            seen.add(key)
            if index and cmds.lockNode(shapes[0],query=True,lock=True)[0]:
                raise ValueError('The target mesh is locked.')
            snapshot=_snapshot(path)
            if index==0:
                if not len(snapshot['values']) or snapshot['counts']!=tuple(map(len,snapshot['faces'])):
                    raise ValueError('The reference mesh must have UVs on every face.')
            snapshots.append(snapshot)
        except (RuntimeError,ValueError) as error:
            if index==0:raise ValueError('Reference: '+str(error)) from error
            failed.append((node,str(error)))
    if len(snapshots)<2 and not failed:
        raise ValueError('Select at least one target mesh with its own UV arrays.')
    numeric=[{key:value for key,value in snapshot.items() if key not in ('path','handle','name','uv_set')}
             for snapshot in snapshots]
    return dict(meshes=snapshots,records=numeric,failed=failed,
                selection=tuple(cmds.ls(selection=True,long=True,flatten=True) or []))


def _geometry_record(data):
    from uv_straight_stack import _shell
    xyz=data['xyz'];centered=xyz-xyz.mean(axis=0)
    _,_,axes=np.linalg.svd(centered,full_matrices=False)
    projected=centered@axes[:2].T
    faces=[(face,face,triangles) for face,triangles in zip(data['faces'],data['triples'])]
    return _shell(0,0,range(len(xyz)),faces,projected,xyz)


def _same_index_positions(source,target):
    a,b=source['xyz'],target['xyz']
    if a.shape!=b.shape or not len(a):return False
    a=a-a.mean(axis=0);b=b-b.mean(axis=0)
    na,nb=np.linalg.norm(a),np.linalg.norm(b)
    if min(na,nb)<1e-15:return False
    a=a/na;b=b/nb
    u,_,v=np.linalg.svd(a.T@b)
    return float(np.linalg.norm(a@(u@v)-b,axis=1).max())<=1e-4/max(1.,np.sqrt(len(a)))


def _output(source,target,mapping):
    """Rebuild destination face UVs, including cuts absent from its old layout."""
    from uv_straight_shell_match import cycle,same_surface
    by_face=defaultdict(list);charts=defaultdict(list);chart_vertices=defaultdict(lambda:defaultdict(set))
    vertex_charts=defaultdict(set);offset=0
    for face,count in zip(source['faces'],source['counts']):
        ids=source['assigned'][offset:offset+count];offset+=count
        owner=source['owners'][ids[0]]
        by_face[cycle(face)].append((face,ids));charts[owner].append(tuple(ids))
        for vertex,index in zip(face,ids):
            chart_vertices[owner][vertex].add(index);vertex_charts[vertex].add(owner)
    output=[];receiving=defaultdict(list)
    for face in target['faces']:
        mapped=tuple(int(mapping[i]) for i in face);exact=by_face.get(cycle(mapped),())
        if len(exact)==1:
            vertices,ids=exact[0];lookup=dict(zip(vertices,ids));assigned=tuple(lookup[i] for i in mapped)
        else:
            owners=set.intersection(*(vertex_charts[i] for i in mapped))
            if len(owners)!=1:raise ValueError('Destination polygons cross reference UV seams.')
            owner=next(iter(owners));choices=[chart_vertices[owner][i] for i in mapped]
            if any(len(ids)!=1 for ids in choices):raise ValueError('UV correspondence is ambiguous.')
            assigned=tuple(next(iter(ids)) for ids in choices)
        receiving[source['owners'][assigned[0]]].append(assigned);output.extend(assigned)
    # Check entire UV surfaces. Matching vertices alone must never transfer to
    # unrelated connectivity, missing polygons, overlapping faces or changed holes.
    if set(receiving)!=set(charts):raise ValueError('The mesh surfaces do not match.')
    for owner,faces in charts.items():
        ids=sorted({i for face in faces for i in face});local={value:i for i,value in enumerate(ids)}
        a=[tuple(local[i] for i in face) for face in faces]
        b=[tuple(local[i] for i in face) for face in receiving[owner]]
        if not same_surface(a,b,np.arange(len(ids)),source['values'][ids]):
            raise ValueError('The mesh surfaces or UV seams do not match.')
    return (tuple(map(len,target['faces'])),tuple(output))


def analyse_steps(records):
    """Numeric worker: match only against the first mesh, never other targets."""
    from uv_straight_stack import match_steps
    source=records[0];reference=None;matched=[];failed=[]
    for index,target in enumerate(records[1:],1):
        try:
            mapping=None
            if _same_index_positions(source,target):mapping=np.arange(len(source['xyz']))
            else:
                if reference is None:reference=_geometry_record(source)
                record=_geometry_record(target)
                if reference is not None and record is not None:
                    steps=match_steps(reference,record)
                    try:
                        while True:
                            next(steps);yield (index,len(records)-1)
                    except StopIteration as done:mapping=done.value
                    finally:steps.close()
            if mapping is None:raise ValueError('The mesh geometry does not match the reference.')
            matched.append((index,_output(source,target,mapping)))
        except (ValueError,IndexError,KeyError,np.linalg.LinAlgError) as error:
            failed.append((index,str(error)))
        yield (index,len(records)-1)
    return dict(matched=matched,failed=failed)


def _validate(snapshot):
    handle=snapshot['handle']
    if not handle.isValid() or not handle.isAlive():raise ValueError('A mesh no longer exists.')
    current=_snapshot(snapshot['path'])
    if (current['uv_set']!=snapshot['uv_set'] or current['faces']!=snapshot['faces'] or
        current['counts']!=snapshot['counts'] or current['assigned']!=snapshot['assigned'] or
        not np.array_equal(current['xyz'],snapshot['xyz']) or
        not np.array_equal(current['values'],snapshot['values'])):
        raise ValueError('Geometry or UVs changed during matching. Try again.')


def apply(prepared,plan):
    """Compatible meshes commit immediately; a rejected target leaves others intact."""
    import uv_straight as uv
    from uv_straight_pack_commit import commit
    source=prepared['meshes'][0];_validate(source)
    if tuple(cmds.ls(selection=True,long=True,flatten=True) or [])!=prepared['selection']:
        raise ValueError('Selection changed during matching. Select the reference first and try again.')
    failed=list(prepared['failed'])+[(prepared['meshes'][i]['name'],reason) for i,reason in plan['failed']]
    changed=0;matched=0
    with uv._undo('ShellShifter Transfer UVs'):
        for index,assigned in plan['matched']:
            target=prepared['meshes'][index]
            try:
                _validate(target)
                if (target['counts'],target['assigned'])==assigned and np.array_equal(target['values'],source['values']):
                    matched+=1;continue
                entry=dict(handle=target['handle'],uv_set=target['uv_set'],before=target['values'].T.tolist(),
                           after=source['values'].T.tolist(),before_assigned=(target['counts'],target['assigned']),
                           after_assigned=assigned,shells=1)
                # Guides refer to UV IDs. Replacing their connectivity invalidates
                # only this set's old guides, as part of the same undo command.
                if (target['counts'],target['assigned'])!=assigned:
                    plug=target['name']+'.'+uv.MARK_ATTR
                    if cmds.objExists(plug):
                        data=json.loads(cmds.getAttr(plug) or '{}');data.pop(target['uv_set'],None)
                        entry['attributes']={uv.MARK_ATTR:json.dumps(data)}
                commit([entry]);changed+=1;matched+=1
            except (RuntimeError,ValueError) as error:failed.append((target['name'],str(error)))
    return dict(matched=matched,changed=changed,failed=failed)
