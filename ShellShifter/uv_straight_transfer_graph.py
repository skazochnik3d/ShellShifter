"""Material-scoped texture discovery and isolated, undoable reconnection."""
import hashlib
import json
import os
from pathlib import Path
import re
import maya.cmds as cmds
import maya.api.OpenMaya as om
from uv_straight_materials import mesh,source_material
from uv_straight_transfer_udim import discover_udim_paths


def assignments(path):
    dag,fn=mesh(path);groups,indices=fn.getConnectedShaders(dag.instanceNumber());materials={}
    for slot,group in enumerate(groups):
        material=source_material(group)
        if material is None:continue
        key=material.uuid().asString()
        record=materials.setdefault(key,dict(name=material.name(),uuid=key,faces=[]))
        record['faces'].extend(i for i,index in enumerate(indices) if index==slot)
    return sorted(materials.values(),key=lambda r:r['name'].casefold())


def identity(node):
    return str(cmds.ls(node,uuid=True)[0])


def _dependency(node):
    selection=om.MSelectionList();selection.add(node)
    return om.MFnDependencyNode(selection.getDependNode(0))


def _authored_attribute(fn,attribute):
    """A render sample and a file's evaluation cache are not shader edits."""
    name=attribute.rsplit('.',1)[-1].split('[',1)[0]
    try:
        attr=fn.attribute(name)
        while not attr.isNull():
            info=om.MFnAttribute(attr)
            if info.name in ('vertexCameraOne','vertexCameraTwo','vertexCameraThree',
                              'vertexUvOne','vertexUvTwo','vertexUvThree','pixelCenter'):return False
            if not(info.writable and info.storable):return False
            attr=info.parent
        return True
    except RuntimeError:
        # A deleted authored attribute in an older snapshot must still differ.
        return True


def _inputs(node,root_group=False):
    pairs=cmds.listConnections(node,source=True,destination=False,connections=True,plugs=True) or []
    result=[]
    for destination,source in zip(pairs[::2],pairs[1::2]):
        if root_group and destination.split('.',1)[1] not in ('displacementShader','volumeShader'):continue
        try:
            if cmds.getAttr(destination,type=True)=='message':continue
        except RuntimeError:continue
        result.append((source,destination))
    return sorted(result)


def _constants(node):
    values={};fn=_dependency(node)
    for attr in cmds.listAttr(node,settable=True) or []:
        try:
            if not _authored_attribute(fn,attr):continue
            plug=node+'.'+attr
            if cmds.connectionInfo(plug,isDestination=True) or cmds.listConnections(plug,source=True,destination=False):continue
            value=cmds.getAttr(plug)
            if isinstance(value,(str,bool,int,float,list,tuple)) or value is None:
                json.dumps(value);values[attr]=value
        except (RuntimeError,TypeError,ValueError):pass
    return values


def _path(file_node):
    path=str(cmds.getAttr(file_node+'.fileTextureName') or '')
    path=os.path.expandvars(path)
    if path and not Path(path).is_absolute():path=cmds.workspace(expandName=path)
    mode=int(cmds.getAttr(file_node+'.uvTilingMode') or 0)
    if mode not in (0,3):raise ValueError('Only regular textures or UDIMs are supported: '+file_node)
    path=path.replace('<udim>','<UDIM>')
    if mode==3 and '<UDIM>' not in path:path=re.sub(r'(?<!\d)(1\d{3})(?=[^\\/]*$)','<UDIM>',path,count=1)
    return path,mode


def image_stats(path):
    try:paths=discover_udim_paths(path)
    except Exception:return None
    return {str(tile):[str(Path(name).resolve()),Path(name).stat().st_size,Path(name).stat().st_mtime_ns]
            for tile,name in paths.items()}


def _placement(file_node):
    uv=cmds.listConnections(file_node+'.uvCoord',source=True,destination=False,plugs=True) or []
    if not uv:return
    node=uv[0].split('.',1)[0]
    if cmds.nodeType(node)!='place2dTexture':
        raise ValueError('Nonstandard UV connection on '+file_node+' before packing; first convert its placement to place2dTexture.')
    defaults={'coverage':(1.,1.),'translateFrame':(0.,0.),'rotateFrame':0.,'mirrorU':False,'mirrorV':False,
              'stagger':False,'wrapU':True,'wrapV':True,'repeatUV':(1.,1.),'offset':(0.,0.),'rotateUV':0.,'noiseUV':(0.,0.)}
    for attr,expected in defaults.items():
        if cmds.listConnections(node+'.'+attr,source=True,destination=False):
            raise ValueError('Animated or procedural UV parameter: '+node+'.'+attr)
        value=cmds.getAttr(node+'.'+attr)
        if isinstance(value,list) and len(value)==1:value=value[0]
        a=value if isinstance(value,(tuple,list)) else (value,);b=expected if isinstance(expected,tuple) else (expected,)
        if len(a)!=len(b) or any(abs(float(x)-float(y))>1e-7 for x,y in zip(a,b)):
            raise ValueError('Transfer requires standard UV placement without repeats or offsets: '+node+'.'+attr)


def describe(material,*,tolerant=False):
    roots={material}
    for group in cmds.listConnections(material,source=False,destination=True,type='shadingEngine') or []:
        if _inputs(group,True):roots.add(group)
    pending=list(roots);nodes={};edges=[];external=[];warnings=[]
    while pending:
        node=pending.pop()
        if node in nodes:continue
        if len(nodes)>1024:raise ValueError('Material network is too large: '+material)
        nodes[node]=dict(uuid=identity(node),type=cmds.nodeType(node),values=_constants(node))
        if nodes[node]['type']!='file' and any('texture/2d' in item for item in cmds.getClassification(nodes[node]['type']) or []):
            reason='Bake the procedural 2D texture to a File first: '+node
            if not tolerant:raise ValueError(reason)
            warnings.append(dict(texture=node,reason=reason))
        for source,destination in _inputs(node,cmds.nodeType(node)=='shadingEngine'):
            upstream=source.split('.',1)[0]
            if cmds.nodeType(upstream) in ('time','colorManagementGlobals') or cmds.objectType(upstream,isAType='dagNode'):
                external.append((source,destination));continue
            edges.append((source,destination));pending.append(upstream)
    files=[]
    for node,info in sorted(nodes.items()):
        if info['type']!='file':continue
        try:files.append(_file_descriptor(node,info,nodes,edges,roots))
        except (ValueError,RuntimeError,OSError) as error:
            if not tolerant:raise
            files.append(dict(node=node,uuid=info['uuid'],path=str(cmds.getAttr(node+'.fileTextureName') or ''),
                              tiling=int(cmds.getAttr(node+'.uvTilingMode')),is_normal=False,
                              color_space=str(cmds.getAttr(node+'.colorSpace') or ''),stats=None,error=str(error)))
    contract=dict(material=material,material_uuid=identity(material),roots=sorted(roots),nodes=nodes,
                  edges=sorted(set(edges)),external=sorted(set(external)),files=files,warnings=warnings)
    contract['signature']=hashlib.sha256(_canonical(contract).encode()).hexdigest()
    return contract


def _file_descriptor(node,info,nodes,edges,roots):
    if cmds.getAttr(node+'.useFrameExtension'):
        raise ValueError('Image sequences are not supported yet: '+node)
    _placement(node)
    path,mode=_path(node);roles=set();seen=set();queue=[(node,False)]
    while queue:
        current,normal=queue.pop()
        if (current,normal) in seen:continue
        seen.add((current,normal))
        for source,destination in edges:
            if source.split('.',1)[0]!=current:continue
            downstream=destination.split('.',1)[0];kind=nodes[downstream]['type'];is_normal=normal
            if kind in ('bump2d','bump3d'):
                interp=int(cmds.getAttr(downstream+'.bumpInterp'))
                if interp==2:raise ValueError('Object-space normal maps are not supported yet: '+node)
                is_normal=normal or interp==1
            elif kind=='aiNormalMap':
                if cmds.attributeQuery('tangentSpace',node=downstream,exists=True) and not cmds.getAttr(downstream+'.tangentSpace'):
                    raise ValueError('Object-space normal maps are not supported yet: '+node)
                is_normal=True
            if downstream in roots:roles.add(is_normal)
            else:queue.append((downstream,is_normal))
    if len(roles)>1:raise ValueError('One File node is used for both normals and color/masks: '+node)
    return dict(node=node,uuid=info['uuid'],path=path,tiling=mode,is_normal=roles=={True},
                      color_space=str(cmds.getAttr(node+'.colorSpace') or ''),stats=image_stats(path))


def _semantic(contract,live_nodes=None):
    """Rebuild old snapshots too: legacy hashes included non-storable caches."""
    nodes=contract['nodes'];names=live_nodes or {data['uuid']:name for name,data in nodes.items()}
    def plug_key(plug):
        node,attribute=plug.split('.',1)
        return (nodes[node]['uuid'] if node in nodes else identity(node))+'.'+attribute
    result={}
    for name,data in nodes.items():
        live=names.get(data['uuid'],name)
        fn=_dependency(live) if cmds.objExists(live) else None
        values={key:value for key,value in data['values'].items()
                if fn is None or _authored_attribute(fn,key)}
        # File names are compared through the resolved path/tile descriptors,
        # so Maya's slash or relative-path normalization is harmless.
        if data['type']=='file':values.pop('fileTextureName',None)
        result[data['uuid']]={'type':data['type'],'values':values}
    files=[]
    for texture in sorted(contract['files'],key=lambda row:row['uuid']):
        item={key:value for key,value in texture.items() if key!='node'}
        item['path']=os.path.normcase(os.path.normpath(item['path']))
        if item['stats']:
            item['stats']={tile:[os.path.normcase(os.path.normpath(stat[0])),*stat[1:]]
                           for tile,stat in item['stats'].items()}
        files.append(item)
    return dict(material_uuid=contract['material_uuid'],roots=sorted(nodes[root]['uuid'] for root in contract['roots']),
                nodes=result,edges=sorted((plug_key(a),plug_key(b)) for a,b in contract['edges']+contract['external']),files=files)


def _canonical(contract,live_nodes=None):
    return json.dumps(_semantic(contract,live_nodes),sort_keys=True,separators=(',',':'))


def graph_change(previous,current,ignore_files=()):
    """Compare authored state, with a useful reason and legacy-source support."""
    names={data['uuid']:name for name,data in current['nodes'].items()}
    if ignore_files:
        previous=dict(previous,files=[f for f in previous['files'] if f['uuid'] not in ignore_files])
        current=dict(current,files=[f for f in current['files'] if f['uuid'] not in ignore_files])
    try:
        a=json.loads(_canonical(previous,names));b=json.loads(_canonical(current,names))
    except (RuntimeError,ValueError,KeyError):return 'Could not match saved material nodes'
    if a==b:return ''
    if a['files']!=b['files']:return 'The texture file, its contents or read settings changed'
    if a['edges']!=b['edges'] or a['roots']!=b['roots']:return 'Material node connections changed'
    if set(a['nodes'])!=set(b['nodes']):return 'A material node was added, removed or replaced'
    for key,node in a['nodes'].items():
        other=b['nodes'][key]
        if node['type']!=other['type']:return 'Material node type changed'
        for attr in sorted(set(node['values'])|set(other['values'])):
            if node['values'].get(attr)!=other['values'].get(attr):
                return 'Parameter changed: '+names.get(key,key)+'.'+attr
    return 'Material data changed'


def _source_uv_plugs(node):
    plugs=cmds.uvLink(query=True,texture=node) or []
    # Maya omits maps on auxiliary shader inputs from uvLink's report.
    # A connected uvChooser takes precedence over that report.
    placements=cmds.listConnections(node+'.uvCoord',source=True,destination=False,type='place2dTexture') or []
    choosers={chooser for placement in placements for chooser in
              cmds.listConnections(placement+'.uvCoord',source=True,destination=False,type='uvChooser') or []}
    if choosers:
        plugs=[plug for chooser in choosers for plug in cmds.listConnections(
            chooser+'.uvSets',source=True,destination=False,plugs=True) or []]
    return plugs


def validate_source_uv(spec,path,uv_set,link_cache=None):
    """The saved pre-pack UV must actually drive each original File node."""
    key=identity(path)
    for texture in spec['files']:
        texture_node=texture['node']
        if link_cache is None:
            linked=[]
            for plug in _source_uv_plugs(texture_node):
                node=plug.split('.',1)[0]
                if cmds.objExists(node) and identity(node)==key:linked.append(cmds.getAttr(plug))
        else:
            if texture_node not in link_cache:
                by_mesh={}
                for plug in _source_uv_plugs(texture_node):
                    node=plug.split('.',1)[0]
                    if cmds.objExists(node):by_mesh.setdefault(identity(node),set()).add(cmds.getAttr(plug))
                link_cache[texture_node]=by_mesh
            linked=link_cache[texture_node].get(key,())
        if uv_set not in linked:
            raise ValueError('Source texture '+texture['node']+' uses a different UV set on '+path+
                             '. Connect it to '+uv_set+'.')


def validate_files(spec):
    if not spec['files']:raise ValueError('The material has no connected File textures: '+spec['material'])
    for texture in spec['files']:
        if texture.get('error'):raise ValueError(texture['error'])
        if not texture['path'] or not texture['stats']:raise ValueError('Source texture not found: '+texture['path'])
        if image_stats(texture['path'])!=texture['stats']:raise ValueError('The source image changed after packing: '+texture['path'])
        for info in texture['stats'].values():
            from uv_straight_transfer_images import image_bit_depth
            image_bit_depth(info[0])


def file_errors(spec):
    """A bad image must not make other File maps of the shader unavailable."""
    errors={}
    for texture in spec['files']:
        try:validate_files(dict(spec,files=[texture]))
        except (ValueError,RuntimeError,OSError) as error:errors[texture['uuid']]=str(error)
    return errors


def reconnect(spec,outputs,bindings):
    """Duplicate upstream nodes; original materials retain their identity."""
    roots=set(spec['roots']);clones={}
    for node in spec['nodes']:
        if node in roots:continue
        clones[node]=cmds.duplicate(node,name='SS_Transferred_'+node.split(':')[-1]+'#',
                                     upstreamNodes=False,inputConnections=False,returnRootsOnly=False)[0]
    remap=lambda plug:clones.get(plug.split('.',1)[0],plug.split('.',1)[0])+'.'+plug.split('.',1)[1]
    for source,destination in spec['edges']+spec['external']:
        if destination.split('.',1)[0] in roots:continue
        if not cmds.isConnected(remap(source),remap(destination)):
            cmds.connectAttr(remap(source),remap(destination),force=True)
    for texture in spec['files']:
        if texture['uuid'] not in outputs:continue
        node=clones[texture['node']];output=outputs[texture['uuid']]
        cmds.setAttr(node+'.fileTextureName',output['path'],type='string')
        cmds.setAttr(node+'.uvTilingMode',3 if output['udim'] else 0)
        # Maya can assign a color space from filename rules when its path changes.
        cmds.setAttr(node+'.colorSpace',texture['color_space'],type='string')
        if cmds.attributeQuery('ignoreColorSpaceFileRules',node=node,exists=True):cmds.setAttr(node+'.ignoreColorSpaceFileRules',True)
        for binding in bindings:
            path,uv_set=binding['path'],binding['uv_set']
            for index in cmds.getAttr(path+'.uvSet',multiIndices=True) or []:
                plug=path+'.uvSet[{}].uvSetName'.format(index)
                if cmds.getAttr(plug)==uv_set:cmds.uvLink(texture=node,uvSet=plug,make=True)
    for source,destination in spec['edges']+spec['external']:
        if destination.split('.',1)[0] in roots:cmds.connectAttr(remap(source),destination,force=True)
    return clones
