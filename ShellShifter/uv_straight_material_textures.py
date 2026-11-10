"""Material texture slots, scene-persisted UI settings and undoable connections."""
from pathlib import Path
from contextlib import nullcontext
import json,re,uuid,os,glob
import maya.cmds as cmds

SLOTS=(('normal','Normal'),('base_color','Base Color'),('ao','AO'),('id_map','ID Map'),
       ('roughness','Roughness'),('glossiness','Glossiness'),('metallic','Metallic'),
       ('specular','Specular'),('emissive','Emissive'),('opacity','Opacity'))
DATA_ATTR='ssMaterialTextureData'
EXTENSIONS={'.png','.tif','.tiff','.tga','.jpg','.jpeg','.bmp','.exr','.hdr','.dds','.tx','.iff'}
PREVIEW_TAG='ssTexturePreview'
CHANNELS=(('rgb','RGB'),('r','R'),('g','G'),('b','B'),('alpha','Alpha'))
CHANNEL_OUTPUTS={'r':'outColorR','g':'outColorG','b':'outColorB','alpha':'outAlpha'}


def source_channel(key,channel=None):
    channel=channel or ('alpha' if key=='opacity' else 'rgb')
    if channel not in ('rgb', *CHANNEL_OUTPUTS):raise ValueError('Choose RGB, R, G, B or Alpha.')
    return channel


def input_connections(plug):
    if not cmds.objExists(plug):return ()
    pairs=cmds.listConnections(plug,source=True,destination=False,plugs=True,connections=True) or []
    if pairs:return tuple((pairs[index+1],pairs[index]) for index in range(0,len(pairs),2))
    node,attr=plug.split('.',1)
    # Maya does not report individually connected children when querying
    # their unconnected float3 parent (opacity, transparency, reverse.input).
    children=cmds.attributeQuery(attr,node=node,listChildren=True) or []
    return tuple(pair for child in children for pair in input_connections(node+'.'+child))


def disconnect_input(plug):
    for source,target in input_connections(plug):
        if cmds.isConnected(source,target):cmds.disconnectAttr(source,target)


def connected_channel(plug,file):
    pending=[source for source,_ in input_connections(plug)]
    seen=set()
    sources=[]
    while pending:
        source=pending.pop()
        if source in seen:continue
        seen.add(source)
        node=source.split('.',1)[0]
        kind=cmds.nodeType(node)
        if kind in ('reverse','unitConversion'):
            pending.extend(origin for origin,_ in input_connections(node+'.input'))
        elif kind=='bump2d':
            if cmds.getAttr(node+'.bumpInterp'):
                return 'rgb'
            pending.extend(origin for origin,_ in input_connections(node+'.bumpValue'))
        else:
            sources.append(source)
    return next((channel for channel,output in CHANNEL_OUTPUTS.items()
                 if set(sources)=={file+'.'+output}), 'rgb')


def is_scalar_input(plug):
    return cmds.getAttr(plug,type=True) not in ('float3','double3','reflectanceRGB','spectrumRGB')


def record_scalar_rgb(material, key, file, channel, plug):
    data = settings(material)
    records = data.get('scalar_rgb', {})
    if channel == 'rgb' and is_scalar_input(plug):
        identity = cmds.ls(file, uuid=True)[0]
        if records.get(key) != identity:
            records[key] = identity
            data['scalar_rgb'] = records
            save_settings(material, data)
    elif key in records:
        del records[key]
        save_settings(material, data)


def connect_file_output(file,plug,channel='rgb',inverse=False,stem='Texture'):
    """Route a scalar channel to all color components when needed."""
    disconnect_input(plug)
    scalar=is_scalar_input(plug)
    output=file+'.'+CHANNEL_OUTPUTS.get(channel,'outColorR' if scalar else 'outColor')
    if inverse:
        reverse=cmds.shadingNode('reverse',asUtility=True,name='SS_'+stem+'_invert',skipSelect=True)
        if scalar:cmds.connectAttr(output,reverse+'.inputX')
        elif channel=='rgb':cmds.connectAttr(output,reverse+'.input')
        else:
            for axis in 'XYZ':cmds.connectAttr(output,reverse+'.input'+axis)
        cmds.connectAttr(reverse+('.outputX' if scalar else '.output'),plug,force=True)
    elif channel!='rgb' and not scalar:
        for child in input_children(plug):cmds.connectAttr(output,child,force=True)
    else:cmds.connectAttr(output,plug,force=True)


def connect_normal(file, plug, channel, stem):
    bump = cmds.shadingNode('bump2d', asUtility=True, name='SS_'+stem+'_normal', skipSelect=True)
    cmds.setAttr(bump+'.bumpInterp', int(channel == 'rgb'))
    cmds.connectAttr(file+'.'+CHANNEL_OUTPUTS.get(channel, 'outAlpha'), bump+'.bumpValue')
    disconnect_input(plug)
    cmds.connectAttr(bump+'.outNormal', plug, force=True)


def resolve_material(identity):
    names=cmds.ls(identity) or []
    if not names:raise ValueError('The material no longer exists.')
    return names[0]


def settings_node(material):
    for node in cmds.listConnections(material+'.message',source=False,destination=True,type='network') or []:
        if cmds.attributeQuery(DATA_ATTR,node=node,exists=True):return node
    return None


def settings(material):
    node=settings_node(material)
    if not node:return dict(custom=[],colors={})
    try:
        value=json.loads(cmds.getAttr(node+'.'+DATA_ATTR) or '{}')
        if not isinstance(value,dict):raise ValueError()
        value.setdefault('custom',[]);value.setdefault('colors',{});return value
    except (ValueError,TypeError):return dict(custom=[],colors={})


def save_settings(material,value):
    node=settings_node(material)
    if node is None:
        node=cmds.createNode('network',name='SS_MaterialTextures#',skipSelect=True)
        cmds.addAttr(node,longName='material',attributeType='message')
        cmds.addAttr(node,longName=DATA_ATTR,dataType='string')
        cmds.connectAttr(material+'.message',node+'.material')
    cmds.setAttr(node+'.'+DATA_ATTR,json.dumps(value,ensure_ascii=False),type='string')


def slot_definitions(material):
    data = settings(material)
    hidden = set(data.get('hidden_slots', ()))
    return [dict(key=key, label=label) for key, label in SLOTS if key not in hidden] + data['custom']


def input_spec(material,key):
    """Return a real shader input when its meaning is known, else an auxiliary map."""
    kind=cmds.nodeType(material);attrs={};inverse=False
    if kind in ('standardSurface','aiStandardSurface'):
        attrs=dict(normal='normalCamera',base_color='baseColor',roughness='specularRoughness',
                   glossiness='specularRoughness',metallic='metalness',specular='specularColor',
                   emissive='emissionColor',opacity='opacity')
        inverse=key=='glossiness'
    elif kind in ('lambert','blinn','phong','phongE','anisotropic'):
        attrs=dict(normal='normalCamera',base_color='color',ao='diffuse',specular='specularColor',emissive='incandescence',opacity='transparency')
        inverse=key=='opacity'
    elif kind=='surfaceShader':attrs=dict(base_color='outColor',opacity='outTransparency');inverse=key=='opacity'
    attr=attrs.get(key)
    if attr and cmds.attributeQuery(attr,node=material,exists=True):return material+'.'+attr,inverse,False
    return material+'.ssTexture_'+re.sub('[^a-zA-Z0-9_]','_',key),False,True


def parked_input(plug, key):
    node, attr = plug.split('.', 1)
    return '{}.ssMuted_{}_{}'.format(node, key, attr)


def input_children(plug):
    node, attr = plug.split('.', 1)
    return [node+'.'+child for child in cmds.attributeQuery(attr, node=node, listChildren=True) or []]


def validate_input(plug):
    if cmds.objExists(plug) and any(cmds.getAttr(target, lock=True) for target in (plug, *input_children(plug))):
        raise ValueError('Material input is locked: '+plug)


def discard_parked_input(plug, key):
    parked = parked_input(plug, key)
    if cmds.objExists(parked):
        cmds.deleteAttr(parked)


def remember_untextured_value(plug):
    # Maya may reset the authored shader value when the texture is disconnected.
    node, attr = plug.split('.', 1)
    value = cmds.getAttr(plug)
    values = list(value[0]) if isinstance(value, list) else [value]
    default = cmds.attributeQuery(attr, node=node, listDefault=True)
    data = settings(node)
    stored = data.get('untextured_values', {})
    previous = stored.get(attr)
    if values == default:
        if previous is not None:
            del stored[attr]
            save_settings(node, data)
    elif values != previous:
        data.setdefault('untextured_values', {})[attr] = values
        save_settings(node, data)


def restore_untextured_value(plug, default=None):
    node, attr = plug.split('.', 1)
    values = settings(node).get('untextured_values', {}).get(attr, default)
    if values:
        if len(values) == 1:
            cmds.setAttr(plug, values[0])
        else:
            cmds.setAttr(plug, *values, type=cmds.getAttr(plug, type=True))


def disconnect_slot(row):
    if not row['muted']:
        disconnect_input(row['plug'])
        restore_untextured_value(row['plug'])
    discard_parked_input(row['plug'], row['key'])


def upstream_file(plug):
    """Follow utility inputs, without guessing among multiple texture sources."""
    pending=[source.split('.',1)[0] for source,target in input_connections(plug)];seen=set();files=[]
    while pending and len(seen)<256:
        node=pending.pop()
        if node in seen:continue
        seen.add(node)
        if cmds.nodeType(node)=='file':files.append(node);continue
        if cmds.objectType(node,isAType='dagNode'):continue
        pending.extend(cmds.listConnections(node,source=True,destination=False) or [])
    return files[0] if len(files)==1 else None,tuple(sorted(files))


def read(material):
    material=resolve_material(material);data=settings(material);rows=[]
    shown=preview_key(material)
    for definition in slot_definitions(material):
        key=definition['key'];plug,inverse,aux=input_spec(material,key)
        # Older releases kept AO on an auxiliary input. Keep it visible until
        # the artist explicitly replaces it; reading must not change shading
        # or invalidate a saved texture-transfer source.
        legacy=material+'.ssTexture_ao'
        parked = parked_input(plug, key)
        inputs = input_connections(plug)
        if key=='ao' and not aux and not inputs and not input_connections(parked):
            legacy_parked = parked_input(legacy, key)
            if input_connections(legacy) or input_connections(legacy_parked):
                plug, parked, aux = legacy, legacy_parked, True
                inputs = input_connections(plug)
        # Roughness and Glossiness share a shader socket. A reverse node
        # identifies an explicitly connected glossiness map.
        if inputs and key in ('roughness','glossiness') and not aux:
            reverse = cmds.nodeType(inputs[0][0].split('.', 1)[0]) == 'reverse'
            if reverse != (key == 'glossiness'):
                inputs = ()
        muted = not inputs and bool(input_connections(parked))
        connection_plug = parked if muted else plug
        connected = bool(inputs) or muted
        file, files = upstream_file(connection_plug) if connected else (None, ())
        path=str(cmds.getAttr(file+'.fileTextureName') or '') if file else ''
        channel=connected_channel(connection_plug,file) if file else 'rgb'
        if channel=='r' and data.get('scalar_rgb',{}).get(key) and is_scalar_input(connection_plug):
            if data['scalar_rgb'][key] == cmds.ls(file,uuid=True)[0]:channel='rgb'
        rows.append(dict(definition,plug=plug,auxiliary=aux,file=file,files=files,path=path,
                         connected=connected,muted=muted,connection_plug=connection_plug,
                         color=data['colors'].get(key),custom=key not in dict(SLOTS),previewing=shown==key,
                         channel=channel))
    return rows


def set_slot_visible(material, key, visible):
    set_slots_visible(((material, key),), visible)


def set_slots_visible(slots, visible):
    """Toggle all requested slots in one undoable action."""
    plans = []
    for identity, key in slots:
        material = resolve_material(identity)
        row = next((item for item in read(material) if item['key'] == key), None)
        if row is None or not row['connected']:
            raise ValueError('Connect a texture to this slot first.')
        if row['muted'] == (not visible):
            continue
        plug = row['plug']
        validate_input(plug)
        validate_input(parked_input(plug, key))
        if visible and input_connections(plug):
            raise ValueError('This material input already has a connection.')
        plans.append((material, row))
    if not plans:
        return
    from uv_straight import _undo
    with _undo('ShellShifter Show Texture' if visible else 'ShellShifter Hide Texture'):
        for material in dict.fromkeys(material for material, _ in plans):
            _end_preview(preview_groups(material))
        for material, row in plans:
            move_slot_connection(material, row, visible)


def move_slot_connection(material, row, visible):
    key = row['key']
    plug = row['plug']
    parked = parked_input(plug, key)
    if not visible:
        discard_parked_input(plug, key)
        attr = parked.split('.', 1)[1]
        cmds.addAttr(material, longName=attr, attributeType=cmds.getAttr(plug,type=True), hidden=True)
        for index, child in enumerate(input_children(plug)):
            cmds.addAttr(material, longName=attr+'_'+str(index), parent=attr,
                         attributeType=cmds.getAttr(child,type=True), hidden=True)
    elif not row['auxiliary']:
        remember_untextured_value(plug)
    original = (plug, *input_children(plug))
    storage = (parked, *input_children(parked))
    source, target = (storage, original) if visible else (original, storage)
    destinations = dict(zip(source, target))
    for output, old_input in input_connections(source[0]):
        cmds.connectAttr(output, destinations[old_input])
    if visible:
        cmds.deleteAttr(parked)
    else:
        disconnect_input(plug)
        default = (0., 0., 0.) if key == 'emissive' and plug.endswith('.emissionColor') else None
        restore_untextured_value(plug, default)


def add_slot(material,label):
    material=resolve_material(material);label=str(label).strip()
    if not label or len(label)>40 or any(ord(c)<32 for c in label):raise ValueError('Enter a texture type name: 1 to 40 characters.')
    if label.casefold() in {row['label'].casefold() for row in slot_definitions(material)}:raise ValueError('This texture type already exists.')
    data = settings(material)
    standard = next(((key, name) for key, name in SLOTS if name.casefold() == label.casefold()), None)
    if standard:
        key, name = standard
        data['hidden_slots'] = [hidden for hidden in data.get('hidden_slots', ()) if hidden != key]
        entry = dict(key=key, label=name)
    else:
        entry = dict(key='custom_'+uuid.uuid4().hex[:12], label=label)
        data['custom'].append(entry)
    from uv_straight import _undo
    with _undo('ShellShifter Add Texture Slot'):
        save_settings(material, data)
    return entry


def delete_slot(material, key):
    material = resolve_material(material)
    row = next((row for row in read(material) if row['key'] == key), None)
    if row is None:
        raise ValueError('The texture slot no longer exists.')
    if row['connected']:
        validate_input(row['connection_plug'])
    data = settings(material)
    if row['custom']:
        data['custom'] = [entry for entry in data['custom'] if entry['key'] != key]
    else:
        data.setdefault('hidden_slots', []).append(key)
    data['colors'].pop(key, None)
    from uv_straight import _undo
    with _undo('ShellShifter Delete Texture Slot'):
        # Roughness and Glossiness may share a shader input. An empty
        # card must leave the other slot's connection intact.
        if row['connected']:
            _end_preview(preview_groups(material))
            disconnect_slot(row)
        save_settings(material, data)


def set_color(material,key,color):
    material=resolve_material(material)
    if key not in {row['key'] for row in slot_definitions(material)}:raise ValueError('The texture type no longer exists.')
    if color is not None and not re.fullmatch('#[0-9a-fA-F]{6}',color):raise ValueError('Invalid background color.')
    from uv_straight import _undo
    with _undo('ShellShifter Texture Background'):
        data=settings(material)
        if color is None:data['colors'].pop(key,None)
        else:data['colors'][key]=color
        save_settings(material,data)


def validate_path(path):
    path=Path(path).expanduser()
    if not path.is_file():raise ValueError('Texture file not found: '+str(path))
    if path.suffix.lower() not in EXTENSIONS:raise ValueError('Choose an image file.')
    return str(path.resolve())


def texture_pattern(path):
    """Recognize an actual multi-file UDIM set, not any four-digit filename."""
    file=Path(path);match=re.search(r'(?<!\d)(1\d{3})(?=\.[^.]+$)',file.name)
    if match:
        prefix,suffix=file.name[:match.start()],file.name[match.end():]
        pattern=glob.escape(str(file.parent/prefix))+'1[0-9][0-9][0-9]'+glob.escape(suffix)
        if len(glob.glob(pattern))>1:return str(file.parent/(prefix+'<UDIM>'+suffix)),True
    return path,False


def link_current_uv_sets(material,file,records=None):
    """Explicit links also cover hidden auxiliary inputs and checker materials."""
    from uv_straight_materials import read as material_records
    identity=(cmds.ls(material,uuid=True) or [None])[0]
    plugs=[]
    for record in material_records() if records is None else records:
        if record.uuid!=identity:continue
        for binding in record.bindings:
            for index in cmds.getAttr(binding.path+'.uvSet',multiIndices=True) or []:
                plug=binding.path+'.uvSet[{}].uvSetName'.format(index)
                if cmds.getAttr(plug)==binding.uv_set:
                    plugs.append(plug);break
    if not plugs:return
    place=(cmds.listConnections(file+'.uvCoord',source=True,destination=False) or [None])[0]
    chooser=cmds.shadingNode('uvChooser',asUtility=True,name='SS_TextureUV#',skipSelect=True)
    for index,plug in enumerate(sorted(set(plugs))):cmds.connectAttr(plug,chooser+'.uvSets[{}]'.format(index))
    cmds.connectAttr(chooser+'.outUv',place+'.uvCoord')
    for attribute in ('vertexUvOne','vertexUvTwo','vertexUvThree','vertexCameraOne'):
        cmds.connectAttr(chooser+'.out'+attribute[0].upper()+attribute[1:],place+'.'+attribute)


def validate_attachment(material,key,path):
    material=resolve_material(material);path=validate_path(path)
    if key not in {row['key'] for row in slot_definitions(material)}:raise ValueError('The texture type no longer exists.')
    plug,inverse,aux=input_spec(material,key)
    validate_input(plug)
    validate_input(parked_input(plug, key))
    return material,path,plug,inverse,aux


def attach(material,key,path,*,channel=None,uv_records=None,undo=True,file_cache=None):
    material,path,plug,inverse,aux=validate_attachment(material,key,path)
    channel=source_channel(key,channel)
    from uv_straight import _undo
    with _undo('ShellShifter Connect Material Texture') if undo else nullcontext():
        _end_preview(preview_groups(material))
        parked = parked_input(plug, key)
        if not aux and key in ('roughness','glossiness'):
            discard_parked_input(plug, 'roughness' if key == 'glossiness' else 'glossiness')
        if input_connections(parked) and not input_connections(plug):
            plug = parked
        else:
            discard_parked_input(plug, key)
            if not aux and not input_connections(plug):
                remember_untextured_value(plug)
        if not cmds.objExists(plug):
            attr=plug.split('.',1)[1];cmds.addAttr(material,longName=attr,attributeType='float3',hidden=True)
            for component in 'RGB':cmds.addAttr(material,longName=attr+component,attributeType='float',parent=attr,hidden=True)
        stem=re.sub('[^a-zA-Z0-9_]','_',material.split(':')[-1]+'_'+key)
        space='sRGB' if key in ('base_color','emissive','specular') else 'Raw'
        normal_rgb=key=='normal' and not aux and channel=='rgb'
        file_key=(material,path,space,normal_rgb)
        file=file_cache.get(file_key) if file_cache is not None else None
        if file is None:
            file=create_texture_file(material,path,space,normal_rgb,stem,uv_records)
            if file_cache is not None:file_cache[file_key]=file
        if key=='normal' and not aux:
            connect_normal(file,plug,channel,stem)
        else:
            connect_file_output(file,plug,channel,inverse,stem)
        record_scalar_rgb(material,key,file,channel,plug)
        if key=='ao' and not aux:
            legacy=material+'.ssTexture_ao'
            discard_parked_input(legacy, key)
            if cmds.objExists(legacy):
                for old in cmds.listConnections(legacy,source=True,destination=False,plugs=True) or []:cmds.disconnectAttr(old,legacy)
        if key=='emissive' and cmds.nodeType(material) in ('standardSurface','aiStandardSurface'):
            weight=material+'.emission'
            if not cmds.listConnections(weight,source=True,destination=False) and not cmds.getAttr(weight,lock=True) and cmds.getAttr(weight)==0:cmds.setAttr(weight,1.)
    return file


def create_texture_file(material,path,space,normal_rgb,stem,uv_records):
    file=cmds.shadingNode('file',asTexture=True,name='SS_'+stem+'_file',skipSelect=True)
    place=cmds.shadingNode('place2dTexture',asUtility=True,name='SS_'+stem+'_place',skipSelect=True)
    cmds.connectAttr(place+'.outUV',file+'.uvCoord');cmds.connectAttr(place+'.outUvFilterSize',file+'.uvFilterSize')
    for attribute in ('coverage','translateFrame','rotateFrame','mirrorU','mirrorV','stagger','wrapU','wrapV',
                      'repeatUV','offset','rotateUV','noiseUV','vertexUvOne','vertexUvTwo','vertexUvThree','vertexCameraOne'):
        cmds.connectAttr(place+'.'+attribute,file+'.'+attribute)
    pattern,tiled=texture_pattern(path)
    cmds.setAttr(file+'.fileTextureName',pattern,type='string')
    if tiled:cmds.setAttr(file+'.uvTilingMode',3)
    cmds.setAttr(file+'.ignoreColorSpaceFileRules',True)
    spaces=cmds.colorManagementPrefs(query=True,inputSpaceNames=True) or []
    if space not in spaces:
        if space=='sRGB':space=next((s for s in spaces if 'srgb' in s.lower() and 'texture' in s.lower()),space)
        else:space=next((s for s in spaces if s.lower().endswith('raw')),space)
    if space in spaces:cmds.setAttr(file+'.colorSpace',space,type='string')
    cmds.setAttr(file+'.alphaIsLuminance',normal_rgb)
    link_current_uv_sets(material,file,uv_records)
    return file


def attach_batch(entries,*,replace=False):
    """Validate the entire batch first, then connect it in one undoable action."""
    prepared=[];destinations=set()
    for entry in entries:
        material,path,plug,_,_=validate_attachment(entry['material'],entry['key'],entry['path'])
        channel=source_channel(entry['key'],entry.get('channel'))
        if plug in destinations:raise ValueError('Multiple maps target the same input: '+plug)
        destinations.add(plug)
        rows=read(material)
        # A Roughness map also replaces Glossiness, since they share a socket.
        connected=any(other['connected'] and other['plug']==plug for other in rows)
        if connected and not replace:raise ValueError('Input is already connected: '+plug+'. Enable texture replacement.')
        if entry.get('signature') is not None and entry['signature']!=connection_signature(material,entry['key']):
            raise ValueError('Connections changed after validation. Reopen the import: '+material)
        prepared.append((material,entry['key'],path,channel))
    if not prepared:return []
    from uv_straight_materials import read as material_records
    records=material_records()
    from uv_straight import _undo
    file_cache={}
    with _undo('ShellShifter Import Material Textures'):
        files = [attach(material,key,path,channel=channel,uv_records=records,undo=False,file_cache=file_cache)
                 for material,key,path,channel in prepared]
        for material in dict.fromkeys(entry[0] for entry in prepared):
            kind = cmds.nodeType(material)
            if kind in ('standardSurface', 'aiStandardSurface'):
                attr = 'base'
            elif kind in ('lambert', 'blinn', 'phong', 'phongE', 'anisotropic'):
                attr = 'diffuse'
            else:
                continue
            plug = material + '.' + attr
            if (cmds.objExists(plug) and cmds.getAttr(plug, settable=True)
                    and not input_connections(plug) and cmds.getAttr(plug) != 1.):
                cmds.setAttr(plug, 1.)
        return files


def connection_signature(material,key):
    material=resolve_material(material)
    plug,_,_=input_spec(material,key)
    return tuple(sorted(input_connections(plug) + input_connections(parked_input(plug, key))))


def set_source_channel(material,key,channel):
    material=resolve_material(material);channel=source_channel(key,channel)
    row=next((row for row in read(material) if row['key']==key),None)
    if not row or not row['file']:raise ValueError('Connect a single File texture first.')
    if row['channel']==channel:return
    plug=row['connection_plug'];inverse=input_spec(material,key)[1]
    validate_input(plug)
    from uv_straight import _undo
    with _undo('ShellShifter Texture Source Channel'):
        _end_preview(preview_groups(material))
        file=row['file']
        normal=key=='normal' and not row['auxiliary']
        normal_rgb=normal and channel=='rgb'
        if (normal or channel=='alpha') and bool(cmds.getAttr(file+'.alphaIsLuminance')) != normal_rgb:
            # Preserve file settings for other consumers of the same image.
            file=cmds.duplicate(file,inputConnections=True,name=file+'_channel')[0]
            cmds.setAttr(file+'.alphaIsLuminance',normal_rgb)
        stem=re.sub('[^a-zA-Z0-9_]','_',material+'_'+key)
        if normal:connect_normal(file,plug,channel,stem)
        else:connect_file_output(file,plug,channel,inverse,stem)
        record_scalar_rgb(material,key,file,channel,plug)
    return file


def detach(material,key):
    material=resolve_material(material)
    row=next((row for row in read(material) if row['key']==key),None)
    if not row or not row['connected']:return
    from uv_straight import _undo
    with _undo('ShellShifter Disconnect Material Texture'):
        _end_preview(preview_groups(material))
        disconnect_slot(row)


def clear_textures(material):
    """Disconnect all texture slots in one Undo, preserving shared source nodes."""
    material=resolve_material(material)
    rows = [row for row in read(material) if row['connected']]
    if not rows:return
    for row in rows:
        validate_input(row['connection_plug'])
    from uv_straight import _undo
    with _undo('ShellShifter Clear Material Textures'):
        _end_preview(preview_groups(material))
        for row in rows:
            disconnect_slot(row)


def absolute_texture_path(path):
    if not path:return ''
    path=os.path.expandvars(str(path))
    if not Path(path).is_absolute():path=cmds.workspace(expandName=path)
    return path


def preview_path(path):
    path=absolute_texture_path(path)
    if '<UDIM>' in path.upper():
        files=sorted(glob.glob(re.sub('<udim>','[0-9][0-9][0-9][0-9]',path,flags=re.I)))
        return files[0] if files else ''
    return path


def preview_groups(material=None):
    identity=(cmds.ls(material,uuid=True) or [None])[0] if material else None
    groups=[]
    for group in cmds.ls(type='shadingEngine') or []:
        if not cmds.attributeQuery(PREVIEW_TAG,node=group,exists=True):continue
        if not cmds.getAttr(group+'.'+PREVIEW_TAG):continue
        owners=cmds.listConnections(group+'.ssPreviewMaterial',source=True,destination=False) or []
        if identity is None or any((cmds.ls(owner,uuid=True) or [None])[0]==identity for owner in owners):groups.append(group)
    return groups


def preview_key(material):
    groups=preview_groups(material)
    return cmds.getAttr(groups[0]+'.ssPreviewKey') if groups else None


def _end_preview(groups):
    shaders=set()
    for group in groups:
        originals=cmds.listConnections(group+'.uvStraightOriginalMaterial',source=True,destination=False,type='shadingEngine') or []
        if not originals:
            owners=cmds.listConnections(group+'.ssPreviewMaterial',source=True,destination=False) or []
            originals=[sg for owner in owners for sg in cmds.listConnections(owner,source=False,destination=True,type='shadingEngine') or []
                       if sg not in groups]
        members=cmds.sets(group,query=True) or []
        if members and not originals:raise ValueError('The original material could not be found to end the preview.')
        if members:cmds.sets(members,edit=True,forceElement=originals[0])
        for shader in cmds.listConnections(group+'.surfaceShader',source=True,destination=False) or []:
            if cmds.attributeQuery(PREVIEW_TAG,node=shader,exists=True) and cmds.getAttr(shader+'.'+PREVIEW_TAG):shaders.add(shader)
        cmds.delete(group)
    for shader in shaders:
        if cmds.objExists(shader) and not cmds.listConnections(shader,source=False,destination=True,type='shadingEngine'):cmds.delete(shader)


def end_preview(material=None):
    groups=preview_groups(resolve_material(material) if material else None)
    if not groups:return
    from uv_straight import _undo
    with _undo('ShellShifter Restore Material Preview'):_end_preview(groups)


def show_in_base_color(material,key):
    """Inspect a map; Base Color or the active map restores original shading."""
    material=resolve_material(material)
    if key=='base_color' or preview_key(material)==key:
        end_preview(material);return
    row=next((r for r in read(material) if r['key']==key),None)
    if not row or not row['file']:raise ValueError('Preview requires one connected File texture.')
    from uv_straight_materials import source_material
    import maya.api.OpenMaya as om
    identity=(cmds.ls(material,uuid=True) or [None])[0]
    from uv_straight import _undo
    with _undo('ShellShifter Preview Texture in Base Color'):
        _end_preview(preview_groups(material));targets=[]
        for group in cmds.ls(type='shadingEngine') or []:
            selection=om.MSelectionList();selection.add(group);source=source_material(selection.getDependNode(0))
            if source is None or source.uuid().asString()!=identity:continue
            members=cmds.sets(group,query=True) or []
            if members:targets.append((group,members))
        if not targets:raise ValueError('The material is not assigned to any objects.')
        shader=cmds.shadingNode('surfaceShader',asShader=True,name='SS_TexturePreview#',skipSelect=True)
        cmds.addAttr(shader,longName=PREVIEW_TAG,attributeType='bool',defaultValue=True,hidden=True)
        connect_file_output(row['file'],shader+'.outColor',row['channel'])
        for original,members in targets:
            group=cmds.sets(renderable=True,noSurfaceShader=True,empty=True,name='SS_TexturePreviewSG#')
            cmds.addAttr(group,longName=PREVIEW_TAG,attributeType='bool',defaultValue=True,hidden=True)
            cmds.addAttr(group,longName='uvStraightOriginalMaterial',attributeType='message')
            cmds.connectAttr(original+'.message',group+'.uvStraightOriginalMaterial')
            cmds.addAttr(group,longName='ssPreviewMaterial',attributeType='message')
            cmds.connectAttr(material+'.message',group+'.ssPreviewMaterial')
            cmds.addAttr(group,longName='ssPreviewKey',dataType='string');cmds.setAttr(group+'.ssPreviewKey',key,type='string')
            cmds.connectAttr(shader+'.outColor',group+'.surfaceShader')
            cmds.sets(members,edit=True,forceElement=group)
