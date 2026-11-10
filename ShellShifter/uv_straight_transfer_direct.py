"""Explicit old/new UV endpoints; no packing or persistent source required."""
from copy import deepcopy
from pathlib import Path
import json
import time
import uuid
import numpy as np
import maya.cmds as cmds
import maya.api.OpenMaya as om
from uv_straight_transfer_snapshot import mesh_fn, capture, restrict, combine, compatible_geometry, transfer_parts, frame_error
from uv_straight_transfer_graph import assignments, describe, identity, reconnect, graph_change, file_errors, validate_files, validate_source_uv
from uv_straight_transfer import _signature


def pick():
    nodes=list(dict.fromkeys(item.split('.',1)[0] for item in cmds.ls(selection=True,long=True) or []))
    if len(nodes)!=1:raise ValueError('Select one mesh or group, then click Pick.')
    root=nodes[0]
    endpoint=dict(uuid=identity(root),name=root)
    paths=meshes(endpoint)
    common=set(mesh_fn(paths[0]).getUVSetNames())
    for path in paths[1:]:common.intersection_update(mesh_fn(path).getUVSetNames())
    if not common:raise ValueError('All meshes in this selection must share a UV-set name.')
    current=mesh_fn(paths[0]).currentUVSetName()
    return endpoint,sorted(common),current if current in common else sorted(common)[0]


def meshes(endpoint):
    roots=cmds.ls(endpoint['uuid'],long=True) or []
    if len(roots)!=1:raise ValueError('The selected source or target no longer exists or is instanced. Pick it again.')
    root=roots[0]
    paths=([root] if cmds.nodeType(root)=='mesh' else cmds.ls(root,dag=True,type='mesh',long=True,noIntermediate=True) or [])
    paths=sorted(set(paths))
    if not paths:raise ValueError('Select a polygon mesh or a group containing polygon meshes.')
    ids=[identity(path) for path in paths]
    if len(set(ids))!=len(ids):raise ValueError('Select only one instance of each mesh.')
    return paths


def pairs(source,target):
    a,b=meshes(source),meshes(target)
    if len(a)!=len(b):raise ValueError('Source and Target must contain the same number of meshes.')
    if len(a)==1:return list(zip(a,b))
    def relative(path,root):
        return '|'.join(token.split(':')[-1] for token in path[len(root):].strip('|').split('|'))
    source_root=(cmds.ls(source['uuid'],long=True) or [''])[0]
    target_root=(cmds.ls(target['uuid'],long=True) or [''])[0]
    aa={relative(path,source_root):path for path in a};bb={relative(path,target_root):path for path in b}
    if len(aa)==len(a) and aa.keys()==bb.keys():return [(aa[key],bb[key]) for key in sorted(aa)]
    # Unique identical object-space geometry is a safe fallback for renamed meshes.
    def key(path):
        fn=mesh_fn(path);counts,vertices=fn.getVertices()
        xyz=np.round([(p.x,p.y,p.z) for p in fn.getPoints()],6)
        return tuple(counts),tuple(vertices),xyz.tobytes()
    targets={}
    for path in b:targets.setdefault(key(path),[]).append(path)
    result=[]
    for path in a:
        candidates=targets.get(key(path),[])
        if len(candidates)!=1:raise ValueError('Mesh matching is ambiguous. Keep matching child names in Source and Target groups, or transfer meshes separately.')
        result.append((path,candidates.pop()))
    return result


def _material_rows(source,target,source_uv,target_uv,progress=None,selected=None,source_assignments=None):
    grouped={}
    if not source or not target:return grouped
    mesh_pairs=pairs(source,target)
    for index,(first,last) in enumerate(mesh_pairs):
        if progress:progress('Reading source materials...',index,len(mesh_pairs))
        if identity(first)==identity(last) and source_uv==target_uv:
            raise ValueError('Choose different Source and Target UV sets, or different meshes.')
        for path,uv in ((first,source_uv),(last,target_uv)):
            if uv not in mesh_fn(path).getUVSetNames():raise ValueError('UV set not found: '+path+' / '+uv)
        assigned=assignments(first)
        if source_assignments is not None:source_assignments[first]=assigned
        for material in assigned:
            if selected is not None and material['uuid'] not in selected:continue
            row=grouped.setdefault(material['uuid'],dict(uuid=material['uuid'],name=material['name'],bindings=[]))
            row['bindings'].append(dict(source_path=first,path=last,faces=material['faces']))
    return grouped


def _check_material_row(row):
    graph=describe(row['name'],tolerant=True);errors=file_errors(graph)
    row.update(textures=len(graph['files']),ready=any(t['uuid'] not in errors for t in graph['files']),reason='; '.join(errors.values()))
    if not graph['files']:row['reason']='Source material has no File textures.'
    row['state']='partial' if row['ready'] and errors else 'ready' if row['ready'] else 'material_error' if graph['files'] else 'no_textures'
    return graph,errors


def materials(source,target,source_uv,target_uv,progress=None):
    rows=_material_rows(source,target,source_uv,target_uv,progress).values()
    for row in rows:_check_material_row(row)
    return sorted(rows,key=lambda row:row['name'].casefold())


def prepare(source,target,source_uv,target_uv,selected,settings,directory,progress=None):
    from uv_straight_material_textures import end_preview
    end_preview()
    if not (1<=settings.width<=8192 and 1<=settings.height<=8192):raise ValueError('Resolution must be between 1 and 8192.')
    if settings.supersampling not in (1,2,4) or settings.output_bit_depth not in (0,8,16):raise ValueError('Unsupported sampling or bit depth.')
    if not 0<=settings.padding<=4096:raise ValueError('Padding must be between 0 and 4096 pixels.')
    if settings.input_green_sign not in (-1,1) or settings.output_green_sign not in (-1,1):raise ValueError('Unknown normal coordinate system.')
    if not str(directory).strip():raise ValueError('Choose an output folder.')
    selected=tuple(selected or ())
    if not selected:raise ValueError('No source materials with File textures are enabled.')
    source_assignments={}
    rows=_material_rows(source,target,source_uv,target_uv,progress,set(selected),source_assignments)
    snapshots={};validation={};jobs=[];assignment_state={};warnings=[]
    link_cache={}
    def snapshot(path,uv):
        if (path,uv) not in snapshots:
            snapshots[path,uv]=capture(path,uv)
            validation[path,uv]=dict(path=path,uv_set=uv,signature=_signature(snapshots[path,uv]))
        if path not in assignment_state:
            assignment_state[path]=source_assignments[path] if path in source_assignments else assignments(path)
        return snapshots[path,uv]
    for key in selected:
        if key not in rows:raise ValueError('The source material is no longer ready. Pick the endpoints again.')
        row=rows[key]
        if progress:progress('Checking source material: '+row['name'])
        graph,errors=_check_material_row(row)
        if not row['ready']:raise ValueError('The source material is no longer ready. Pick the endpoints again.')
        src=[];dst=[];bindings=[]
        for index,binding in enumerate(row['bindings']):
            if progress:progress('Reading Source and Target UVs...',index,len(row['bindings']))
            a,b=snapshot(binding['source_path'],source_uv),snapshot(binding['path'],target_uv)
            if not compatible_geometry(a,b):raise ValueError('Source and Target geometry, face order, normals or triangulation do not match: '+binding['path'])
            first,last,skipped=transfer_parts(a,b,binding['faces'])
            if skipped['triangles']:
                warnings.append(dict(stage='prepare',material=row['name'],code='ZERO_AREA_TARGET_UV',
                    reason='Skipped {} zero-area Target UV triangles on {} (first faces {}). Existing materials on fully collapsed faces are preserved.'.format(
                        skipped['triangles'],binding['path'],', '.join(map(str,skipped['faces'][:12]))),
                    context=dict(mesh=binding['path'],triangle_count=skipped['triangles'],faces=skipped['faces'])))
            if first is None:continue
            src.append(first);dst.append(last)
            path=om.MSelectionList().add(binding['source_path']).getDagPath(0)
            groups,slots=mesh_fn(binding['source_path']).getConnectedShaders(path.instanceNumber())
            names=[om.MFnDependencyNode(group).name() for group in groups]
            face_groups={}
            for face in skipped['kept_faces']:
                group=names[slots[face]]
                face_groups.setdefault(group,[]).append(face)
            bindings.append(dict(path=binding['path'],uv_set=target_uv,faces=skipped['kept_faces'],groups=face_groups))
            for texture in graph['files']:
                try:validate_source_uv(dict(graph,files=[texture]),binding['source_path'],source_uv,link_cache=link_cache)
                except (ValueError,RuntimeError) as error:errors[texture['uuid']]=str(error)
        if any(t['is_normal'] for t in graph['files']):
            for role,parts in (('Source',src),('Target',dst)):
                for part in parts:
                    reason=frame_error(part,role)
                    if reason:
                        for texture in graph['files']:
                            if texture['is_normal']:errors[texture['uuid']]=reason
        if not src:continue
        jobs.append(dict(uuid=key,name=row['name'],source=combine(src),target=combine(dst),graph=graph,bindings=bindings,
                         textures=graph['files'],texture_errors=errors))
    if not jobs:raise ValueError('All selected Target UV triangles have zero area. Repair the Target UVs first. '+ '; '.join(w['reason'] for w in warnings[:3]))
    return dict(direct=True,jobs=jobs,validation=list(validation.values()),assignment_state=assignment_state,
                settings=settings,directory=str(Path(directory).expanduser().resolve()),
                run_id=time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8],warnings=warnings)


def validate(prepared,outputs_by_material=None):
    for record in prepared['validation']:
        if _signature(capture(record['path'],record['uv_set']))!=record['signature']:
            raise ValueError('Source or Target changed during transfer. New textures were not connected.')
    for path,state in prepared['assignment_state'].items():
        if assignments(path)!=state:raise ValueError('Material assignments changed during transfer: '+path)
    for job in prepared['jobs']:
        outputs=outputs_by_material[job['uuid']]['outputs'] if outputs_by_material is not None else None
        ignored={t['uuid'] for t in job['graph']['files'] if outputs is not None and t['uuid'] not in outputs}
        if graph_change(job['graph'],describe(job['name'],tolerant=True),ignored):
            raise ValueError('The source material changed during transfer: '+job['name'])
        available=dict(job['graph'],files=[t for t in job['graph']['files'] if t['uuid'] not in ignored])
        if available['files']:validate_files(available)


def apply(prepared,result):
    from uv_straight import _undo
    from uv_straight_native import uv_edit
    by_uuid={row['uuid']:row for row in result['materials']}
    validate(prepared,by_uuid)
    for row in by_uuid.values():
        for output in row['outputs'].values():
            if any(not Path(path).is_file() for path in output['files']):raise ValueError('An output texture is missing.')
    with uv_edit(),_undo('ShellShifter Transfer Textures'):
        for job in prepared['jobs']:
            row=by_uuid[job['uuid']];row['connected']=False
            if not row['outputs']:continue
            spec=deepcopy(job['graph']);root_map={}
            # Preserve the source shader and every unrelated recipient. Clone
            # just the material roots, then use the existing graph reconnection.
            for root in spec['roots']:
                root_map[root]=(cmds.sets(renderable=True,noSurfaceShader=True,empty=True,name='SS_TransferredSG#')
                                if cmds.nodeType(root)=='shadingEngine' else cmds.duplicate(root,name='SS_Transferred_'+root.split(':')[-1]+'#',
                                  upstreamNodes=False,inputConnections=False,returnRootsOnly=False)[0])
            remap=lambda plug:root_map.get(plug.split('.',1)[0],plug.split('.',1)[0])+'.'+plug.split('.',1)[1]
            spec['nodes']={root_map.get(name,name):value for name,value in spec['nodes'].items()}
            spec['roots']=[root_map[name] for name in spec['roots']]
            for kind in ('edges','external'):spec[kind]=[(remap(a),remap(b)) for a,b in spec[kind]]
            spec['material']=root_map[job['name']]
            for binding in job['bindings']:
                for group in binding['groups']:
                    if group not in root_map:
                        root_map[group]=cmds.sets(renderable=True,noSurfaceShader=True,empty=True,name='SS_TransferredSG#')
            groups=[root_map[group] for binding in job['bindings'] for group in binding['groups']]
            reconnect(spec,row['outputs'],job['bindings'])
            for group in groups:cmds.connectAttr(spec['material']+'.outColor',group+'.surfaceShader',force=True)
            for binding in job['bindings']:
                for group,ids in binding['groups'].items():
                    faces=['{}.f[{}]'.format(binding['path'],i) for i in ids]
                    cmds.sets(faces,edit=True,forceElement=root_map[group])
            row['connected']=True;row['target_material']=spec['material']
    result['connected']=any(row.get('connected') for row in result['materials'])
    result['partial']=bool(result.get('warnings'));result['direct']=True
    try:Path(result['report_path']).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    except OSError as error:result['report_warning']=str(error)
    return result
