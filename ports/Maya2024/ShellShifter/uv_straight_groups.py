"""Scene-owned UV groups. Membership uses polygon IDs, never transient shell IDs."""
from collections import defaultdict
import copy
import hashlib
import json
import math
import uuid
import maya.cmds as cmds
import maya.api.OpenMaya as om

ATTR = 'shellShifterUVGroups'
PALETTE = ((.25,.72,.93),(.95,.55,.23),(.43,.81,.48),(.76,.49,.91),(.95,.78,.27),(.91,.39,.57))
DEFAULT_DENSITY=500.
_snapshot_state=None
_snapshot_groups=()


def _nodes():
    return sorted(cmds.ls('*.'+ATTR,objectsOnly=True,long=True) or [])


def read():
    return copy.deepcopy(list(snapshot()))


def snapshot(state=None):
    """Shared read-only metadata cache; scene edits/Undo are validated by strings."""
    global _snapshot_state,_snapshot_groups
    state=fingerprint() if state is None else state
    if state==_snapshot_state:return _snapshot_groups
    groups=[]
    for node,source in state:
        try:
            data=json.loads(source or '{}')
        except (ValueError,RuntimeError):
            raise ValueError('Cannot read saved UV groups: '+node)
        if data.get('format')!=1:raise ValueError('Unsupported UV group format: '+node)
        for group in data.get('groups',[]):
            item=dict(group);item.pop('enabled',None)
            if item['kind']=='texel':
                item.setdefault('density',DEFAULT_DENSITY*item.get('factor',1.))
                item['factor']=item['density']/DEFAULT_DENSITY
            else:
                item.setdefault('mode','relative')
                item['strength']=1. if item['mode']=='absolute' else .85
            item.setdefault('visible',True);item['_node']=node;groups.append(item)
    _snapshot_state=state;_snapshot_groups=tuple(groups)
    return _snapshot_groups


def fingerprint():
    return tuple((node,cmds.getAttr(node+'.'+ATTR)) for node in _nodes())


def _save(groups,node=None):
    if node is None:
        node=cmds.createNode('network',name='shellShifterUVGroups#',skipSelect=True)
        cmds.addAttr(node,longName=ATTR,dataType='string')
    clean=[{key:value for key,value in group.items() if not key.startswith('_')}
           for group in groups]
    cmds.setAttr(node+'.'+ATTR,json.dumps(dict(format=1,groups=clean),sort_keys=True),type='string')
    return node


def get(group_id):
    matches=[g for g in read() if g['id']==group_id]
    if len(matches)!=1:raise ValueError('Select an existing UV group.')
    return matches[0]


def topology(fn):
    counts,vertices=fn.getVertices()
    return hashlib.sha256((str(tuple(counts))+str(tuple(vertices))).encode('ascii')).hexdigest()


def face_shells(mesh):
    counts,assigned=mesh.fn.getAssignedUVs(mesh.uv_set)
    faces=defaultdict(set);offset=0
    for index,count in enumerate(counts):
        ids=assigned[offset:offset+count];offset+=count
        if ids:faces[int(mesh.shell_ids[ids[0]])].add(index)
    return faces


def selection_members():
    import uv_straight as uv
    _,meshes=uv._selection();members=[];unique={}
    for mesh in meshes:
        key=(mesh.fn.uuid().asString(),mesh.uv_set)
        if key in unique:unique[key].selected.update(mesh.selected)
        else:unique[key]=mesh
    for key,mesh in unique.items():
        owners={int(mesh.shell_ids[i]) for i in mesh.selected}
        faces=sorted({face for owner,values in face_shells(mesh).items() if owner in owners for face in values})
        if faces:members.append(dict(mesh=key[0],uv_set=key[1],topology=topology(mesh.fn),faces=faces))
    if not members:raise ValueError('Select UV shells to add to the group.')
    return members


def next_name(kind):
    prefix='G' if kind=='pack' else 'T'
    names={g['name'].casefold() for g in read() if g['kind']==kind}
    index=1
    while (prefix+format(index,'02d')).casefold() in names:index+=1
    return prefix+format(index,'02d')


def create(kind,name=None,members=None,density=DEFAULT_DENSITY,mode='relative'):
    if kind not in ('texel','pack'):raise ValueError('Unknown UV group type.')
    if name is None:name=next_name(kind)
    if not name.strip():raise ValueError('Enter a group name.')
    members=selection_members() if members is None else members
    groups=read();node=_nodes()[0] if _nodes() else None
    group=dict(id=uuid.uuid4().hex,kind=kind,name=name.strip(),color=list(PALETTE[len(groups)%len(PALETTE)]),
               visible=True,density=float(density),factor=float(density)/DEFAULT_DENSITY,
               mode=mode,strength=1. if mode=='absolute' else .85,members=[])
    if not math.isfinite(density) or not .0001<=density<=1e9:raise ValueError('Texel density must be from 0.0001 to 1000000000 px/unit.')
    if mode not in ('absolute','relative'):raise ValueError('Unknown Shell Group mode.')
    import uv_straight as uv
    with uv._undo('ShellShifter Create UV Group'):
        node=_save([g for g in groups if g['_node']==node]+[group],node)
        assign(group['id'],members=members,undo=False)
    return group['id']


def update(group_id,**changes):
    group=get(group_id)
    allowed={'name','color','visible','density','mode','factor','strength'}
    if set(changes)-allowed:raise ValueError('Unknown group setting.')
    if 'name' in changes:
        changes['name']=changes['name'].strip()
        if not changes['name']:raise ValueError('Enter a group name.')
    if 'factor' in changes and (not math.isfinite(changes['factor']) or not .01<=changes['factor']<=100):
        raise ValueError('Texel multiplier must be from 0.01 to 100.')
    if 'factor' in changes:changes['density']=changes['factor']*DEFAULT_DENSITY
    if 'density' in changes:
        if not math.isfinite(changes['density']) or not .0001<=changes['density']<=1e9:
            raise ValueError('Texel density must be from 0.0001 to 1000000000 px/unit.')
        changes['factor']=changes['density']/DEFAULT_DENSITY
    if 'strength' in changes and (not math.isfinite(changes['strength']) or not 0<=changes['strength']<=1):
        raise ValueError('Group strength must be from 0 to 100%.')
    if 'strength' in changes:changes['mode']='relative'
    if 'mode' in changes:
        if changes['mode'] not in ('absolute','relative'):raise ValueError('Unknown Shell Group mode.')
        changes['strength']=1. if changes['mode']=='absolute' else .85
    if 'visible' in changes:changes['visible']=bool(changes['visible'])
    if 'color' in changes and (len(changes['color'])!=3 or any(not math.isfinite(c) or not 0<=c<=1 for c in changes['color'])):
        raise ValueError('Invalid group color.')
    changes={key:value for key,value in changes.items() if group.get(key)!=value}
    if not changes:return
    groups=[g for g in read() if g['_node']==group['_node']]
    for item in groups:
        if item['id']==group_id:item.update(changes)
    import uv_straight as uv
    with uv._undo('ShellShifter Edit UV Group'):_save(groups,group['_node'])


def delete(group_id):
    group=get(group_id)
    import uv_straight as uv
    with uv._undo('ShellShifter Delete UV Group'):
        _save([g for g in read() if g['_node']==group['_node'] and g['id']!=group_id],group['_node'])


def assign(group_id,members=None,remove=False,undo=True):
    from contextlib import nullcontext
    import uv_straight as uv
    group=get(group_id);members=selection_members() if members is None else members
    incoming={(m['mesh'],m['uv_set']):m for m in members}
    groups=read()
    for item in groups:
        if item['kind']!=group['kind']:continue
        updated=[]
        for member in item['members']:
            addition=incoming.get((member['mesh'],member['uv_set']))
            faces=set(member['faces'])
            if addition is not None:faces-=set(addition['faces'])
            if faces:updated.append(dict(member,faces=sorted(faces)))
        if item['id']==group_id and not remove:
            for member in members:
                existing=next((m for m in updated if (m['mesh'],m['uv_set'])==(member['mesh'],member['uv_set'])),None)
                if existing is None:updated.append(copy.deepcopy(member))
                else:
                    if existing['topology']!=member['topology']:
                        raise ValueError('Topology changed. Remove stale group members before adding shells.')
                    existing['faces']=sorted(set(existing['faces'])|set(member['faces']))
        # Remove only affects the chosen group; Add transfers membership within this type.
        if not remove or item['id']==group_id:item['members']=updated
    with uv._undo('ShellShifter Assign UV Group') if undo else nullcontext():
        for node in dict.fromkeys(g['_node'] for g in groups):
            _save([g for g in groups if g['_node']==node],node)


def paths(group):
    result=[]
    for member in group['members']:
        for name in cmds.ls(member['mesh'],long=True) or []:
            selection=om.MSelectionList();selection.add(name)
            try:
                path=selection.getDagPath(0);fn=om.MFnMesh(path)
                if member['uv_set'] in fn.getUVSetNames() and topology(fn)==member['topology']:
                    result.append((path,member))
            except RuntimeError:pass
    return result


def resolve(meshes,strict=True):
    """Resolve full current shells; reject a sewn chart spanning group boundaries."""
    groups=snapshot();result={id(mesh):{'texel':{},'pack':{}} for mesh in meshes}
    for mesh in meshes:
        key=(mesh.fn.uuid().asString(),mesh.uv_set);shells=face_shells(mesh);topo=topology(mesh.fn)
        for kind in ('texel','pack'):
            for group in (g for g in groups if g['kind']==kind):
                members=[m for m in group['members'] if (m['mesh'],m['uv_set'])==key]
                if not members:continue
                if any(m['topology']!=topo for m in members):
                    if strict:raise ValueError('Topology changed for group "'+group['name']+'". Reassign its shells.')
                    continue
                faces={f for m in members for f in m['faces']}
                for owner,current in shells.items():
                    if not current & faces:continue
                    if not current<=faces or owner in result[id(mesh)][kind]:
                        if strict:raise ValueError('A UV shell crosses group boundaries. Reassign the complete shell: '+group['name'])
                        result[id(mesh)][kind].pop(owner,None);continue
                    result[id(mesh)][kind][owner]=group
    return result


def hidden_faces(path,groups=None):
    """UV-only exclusions; native 3D face/transform visibility stays unchanged."""
    hidden=[g for g in (snapshot() if groups is None else groups) if not g.get('visible',True)]
    if not hidden:return frozenset()
    fn=om.MFnMesh(path);key=(fn.uuid().asString(),fn.currentUVSetName());topo=None;faces=set()
    for group in hidden:
        for member in group['members']:
            if (member['mesh'],member['uv_set'])!=key:continue
            if topo is None:topo=topology(fn)
            if member['topology']==topo:faces.update(member['faces'])
    return frozenset(faces)


def select(group_id):
    import uv_straight as uv
    group=get(group_id);components=[]
    for path,member in paths(group):
        mesh=uv.Mesh(path)
        if mesh.uv_set!=member['uv_set']:continue
        wanted=set(member['faces']);owners={o for o,f in face_shells(mesh).items() if f<=wanted}
        components.extend(mesh.components(i for i,o in enumerate(mesh.shell_ids) if o in owners))
    if not components:raise ValueError('No group shells in the current UV sets. Check topology and UV set.')
    uv.select_uv_shell_components(components)
    return len(components)


def apply_texel(group_id,resolution=4096,base_density=None):
    """Explicit group density edit, independent of the transient Maya selection."""
    import uv_straight as uv
    from uv_straight_texel import _measure
    from uv_straight_pack_commit import commit
    from uv_straight_deps import np
    group=get(group_id);meshes=[]
    for path,member in paths(group):
        mesh=uv.Mesh(path)
        if mesh.uv_set==member['uv_set']:meshes.append(mesh)
    resolved=resolve(meshes);entries=[];count=0
    for mesh in meshes:
        before=np.asarray(list(mesh.points.values()));after=before.copy();ids=[];shell_count=0
        for shell in _measure([mesh],resolution):
            assignment=resolved[id(mesh)]['texel'].get(shell.shell_id)
            if assignment is None or assignment['id']!=group_id:continue
            values=before[list(shell.uv_ids)];center=values.mean(axis=0)
            after[list(shell.uv_ids)]=(values-center)*(group['density']/shell.density)+center
            ids.extend(shell.uv_ids);shell_count+=1
        if ids:
            entries.append(dict(handle=om.MObjectHandle(mesh.fn.object()),uv_set=mesh.uv_set,
                                before=before.T.tolist(),after=after.T.tolist(),ids=ids,shells=shell_count))
            count+=shell_count
    if not entries:raise ValueError('No measurable group shells in the current UV sets.')
    with uv._undo('ShellShifter Apply Texel Group'):commit(entries)
    return count
