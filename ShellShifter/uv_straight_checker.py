"""Reversible per-mesh checker assignments with scene-persistent backups."""
import json
import math
import weakref
import hashlib
import shutil
from pathlib import Path
from collections import defaultdict
import maya.cmds as cmds
import maya.api.OpenMaya as om

STATE = 'uvStraightCheckerState'
NODES = 'uvStraightCheckerNodes'
ORIGINAL = 'uvStraightOriginalMaterial'
PREVIEW_SHADER = 'uvStraightPreviewShader'
VISIBILITY_OPTION = 'shellShifterDisplayChecker'
# Scene-linear greys: softer contrast than pure black/white in the viewport.
CHECKER_DARK = (.22, .22, .22)
CHECKER_LIGHT = (.55, .55, .55)
STYLES = (('gray', 'Gray Squares'), ('color', 'Color Labels'), ('direction', 'Direction'))
STYLE_OPTION = 'uvStraightCheckerStyle'
COLOR_TEXTURE = Path(__file__).resolve().parent / 'assets' / 'shellshifter_checker_color.png'
DIRECTION_TEXTURE = Path(__file__).resolve().parent / 'assets' / 'shellshifter_checker_direction.png'
LIBRARY_OPTION = 'uvStraightCheckerLibrary'
TEXTURE_STYLE = 'uvStraightCheckerPattern'
TEXTURE_CELLS = 'uvStraightCheckerCells'
_style_listeners = globals().get('_style_listeners', [])


def _custom_styles():
    try:
        records=json.loads(cmds.optionVar(query=LIBRARY_OPTION)) if cmds.optionVar(exists=LIBRARY_OPTION) else []
        return [r for r in records if isinstance(r,dict) and isinstance(r.get('id'),str) and
                r['id'].startswith('custom_') and isinstance(r.get('path'),str) and isinstance(r.get('label'),str)]
    except (ValueError,TypeError):return []


def styles():
    return STYLES+tuple((r['id'],r['label']) for r in _custom_styles())


def style_texture(style):
    if style=='color':return COLOR_TEXTURE
    if style=='direction':return DIRECTION_TEXTURE
    return next((Path(r['path']) for r in _custom_styles() if r['id']==style),None)


def add_texture(filename):
    """Keep imported patterns in user prefs, outside replaceable plugin files."""
    source=Path(filename).expanduser().resolve()
    if not source.is_file():raise ValueError('Checker texture is missing: '+str(source))
    try:
        image=om.MImage();image.readFromFile(str(source))
        if min(image.getSize())<1:raise ValueError('Empty checker texture.')
        digest=hashlib.sha256()
        with source.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
        identifier='custom_'+digest.hexdigest()
        existing=next((r for r in _custom_styles() if r['id']==identifier),None)
        folder=Path(cmds.internalVar(userPrefDir=True))/'ShellShifter'/'checkers'
        folder.mkdir(parents=True,exist_ok=True)
        destination=folder/(digest.hexdigest()+source.suffix.lower())
        if source!=destination:shutil.copy2(source,destination)
        records=[r for r in _custom_styles() if r['id']!=identifier]
        records.append(dict(id=identifier,label=existing['label'] if existing else source.stem,path=destination.as_posix()))
        cmds.optionVar(stringValue=(LIBRARY_OPTION,json.dumps(records,ensure_ascii=False)))
        return identifier
    except (OSError,RuntimeError) as error:
        raise ValueError('Cannot import checker texture: '+str(error)) from error


def preferred_style():
    value = cmds.optionVar(query=STYLE_OPTION) if cmds.optionVar(exists=STYLE_OPTION) else 'gray'
    return value if value in dict(styles()) else 'gray'


def subscribe_style(callback):
    _style_listeners.append(weakref.ref(callback))


def _remember_style(style):
    cmds.optionVar(stringValue=(STYLE_OPTION, style))
    alive = []
    for reference in _style_listeners:
        callback = reference()
        if callback is not None:
            try:
                callback(style)
                alive.append(reference)
            except RuntimeError:
                pass  # Its Qt window was destroyed.
    _style_listeners[:] = alive


def _validate_style(style):
    if style not in dict(styles()):
        raise ValueError('Unknown checker style: ' + str(style))
    path=style_texture(style)
    if style!='gray' and (path is None or not path.is_file()):
        raise ValueError('Checker texture is missing: ' + str(path))
    return style


def _set_palette(texture):
    cmds.setAttr(texture+'.color1', *CHECKER_DARK, type='double3')
    cmds.setAttr(texture+'.color2', *CHECKER_LIGHT, type='double3')


def refresh_palette():
    """Update existing owned checker networks, retaining material backups."""
    textures = set()
    for sg in cmds.ls(type='shadingEngine') or []:
        if not cmds.attributeQuery(ORIGINAL, node=sg, exists=True):
            continue
        for shader in cmds.listConnections(sg+'.surfaceShader', source=True,
                                           destination=False) or []:
            textures.update(cmds.listConnections(shader+'.outColor', source=True,
                                                 destination=False, type='checker') or [])
    changed = [texture for texture in textures
               if any(any(abs(a-b) > 1e-6 for a, b in zip(cmds.getAttr(texture+'.'+attr)[0], color))
                      for attr, color in (('color1', CHECKER_DARK), ('color2', CHECKER_LIGHT)))]
    if changed:
        import uv_straight as uv
        with uv._undo('ShellShifter Muted Checker'):
            for texture in changed:
                _set_palette(texture)
    return len(changed)


def _meshes():
    selected = cmds.ls(selection=True, long=True, objectsOnly=True) or []
    shapes = set()
    for node in selected:
        if cmds.nodeType(node) == 'mesh':
            shapes.add(node)
        else:
            shapes.update(cmds.listRelatives(node, allDescendents=True,
                                            type='mesh', fullPath=True) or [])
    result = []
    for shape in sorted(shapes):
        if cmds.getAttr(shape + '.intermediateObject'):
            continue
        if len(cmds.ls(shape, long=True, allPaths=True) or []) > 1:
            raise ValueError('Checker: instanced meshes are not supported. Make the mesh unique first.')
        result.append(shape)
    if not result:
        raise ValueError('Select polygon meshes or their components.')
    return result


def _state(shape):
    if cmds.attributeQuery(STATE, node=shape, exists=True):
        return json.loads(cmds.getAttr(shape + '.' + STATE))
    return None


def _face_components(shape, faces):
    """Keep material assignments compact when Maya receives large face sets."""
    from itertools import groupby
    result=[]
    for _,run in groupby(enumerate(sorted(set(faces))),lambda item:item[1]-item[0]):
        _,first=next(run)
        last=first
        for _,last in run:pass
        span=str(first) if first==last else '{}:{}'.format(first,last)
        result.append('{}.f[{}]'.format(shape,span))
    return result


def _snapshot(shape):
    sel = om.MSelectionList()
    sel.add(shape)
    path = sel.getDagPath(0)
    fn = om.MFnMesh(path)
    if fn.numUVs(fn.currentUVSetName()) == 0:
        raise ValueError('Checker needs UVs: ' + shape)
    shaders, indices = fn.getConnectedShaders(path.instanceNumber())
    if any(i < 0 for i in indices):
        raise ValueError('Checker needs a material on every face: ' + shape)
    groups = defaultdict(list)
    for face, index in enumerate(indices):
        groups[index].append(face)
    assignments = []
    for index, faces in groups.items():
        name = om.MFnDependencyNode(shaders[index]).name()
        assignments.append({'uuid': cmds.ls(name, uuid=True)[0],
                            'name': name, 'faces': faces})
    return {'assignments': assignments, 'faces': fn.numPolygons,
            'displayColors': cmds.getAttr(shape + '.displayColors'),
            'uvSet': fn.currentUVSetName(),
            'defaultUV': fn.getUVSetNames()[0] == fn.currentUVSetName() == 'map1'}


def visible():
    return not cmds.optionVar(exists=VISIBILITY_OPTION) or bool(cmds.optionVar(query=VISIBILITY_OPTION))


def _checker_shader(group):
    saved = group + '.' + PREVIEW_SHADER
    if cmds.objExists(saved):
        return cmds.listConnections(saved, source=True, destination=False) or []
    return cmds.listConnections(group + '.surfaceShader', source=True, destination=False) or []


def _set_group_visibility(value):
    for group in cmds.ls(type='shadingEngine') or []:
        if not cmds.attributeQuery(ORIGINAL, node=group, exists=True):
            continue
        original = cmds.listConnections(group + '.' + ORIGINAL, source=True, destination=False) or []
        shaders = _checker_shader(group)
        if not original or not shaders:
            continue
        shader = shaders[0]
        saved = group + '.' + PREVIEW_SHADER
        if value and not cmds.objExists(saved):
            continue
        if not value and not cmds.objExists(saved):
            cmds.addAttr(group, longName=PREVIEW_SHADER, attributeType='message', hidden=True)
            cmds.connectAttr(shader + '.message', saved)
        for slot in ('surfaceShader', 'volumeShader', 'displacementShader'):
            target = group + '.' + slot
            old = cmds.connectionInfo(target, sourceFromDestination=True)
            if value:
                new = shader + '.outColor' if slot == 'surfaceShader' else ''
            else:
                new = cmds.connectionInfo(original[0] + '.' + slot, sourceFromDestination=True)
            if old == new:
                continue
            if new:
                cmds.connectAttr(new, target, force=True)
            elif old:
                cmds.disconnectAttr(old, target)


def set_visible(value):
    from uv_straight_native import _no_undo
    with _no_undo():
        _set_group_visibility(value)
    cmds.optionVar(intValue=(VISIBILITY_OPTION, int(value)))


def _helpers(shape, group_cache=None):
    nodes = set(cmds.listConnections(shape+'.'+NODES, source=True, destination=False) or []) if cmds.attributeQuery(NODES, node=shape, exists=True) else set()
    # Duplicates may share SG memberships without copying message connections.
    group_cache = {} if group_cache is None else group_cache
    for sg in cmds.listConnections(shape,type='shadingEngine') or []:
        if sg not in group_cache:
            graph = set()
            if cmds.attributeQuery(ORIGINAL,node=sg,exists=True):
                graph.add(sg)
                for shader in _checker_shader(sg):
                    graph.add(shader)
                    for kind in ('checker', 'file'):
                        for texture in cmds.listConnections(shader+'.outColor',source=True,destination=False,type=kind) or []:
                            graph.add(texture)
                            graph.update(cmds.listConnections(texture,source=True,destination=False,type='place2dTexture') or [])
            group_cache[sg] = graph
        nodes.update(group_cache[sg])
    return nodes


def _preview_network(shape, cache=None):
    """Resolve the live material graph, even when copied metadata is stale."""
    selection = om.MSelectionList()
    selection.add(shape)
    path = selection.getDagPath(0)
    shaders, _ = om.MFnMesh(path).getConnectedShaders(path.instanceNumber())
    groups = tuple(sorted(om.MFnDependencyNode(node).name() for node in shaders))
    if cache is not None and groups in cache:
        return cache[groups]
    preview_groups = [sg for sg in groups if cmds.attributeQuery(ORIGINAL, node=sg, exists=True)]
    materials = {node for sg in preview_groups for node in
                 _checker_shader(sg)}
    if len(materials) == 1:
        shader = materials.pop()
        textures = [node for node in (cmds.listConnections(shader+'.outColor', source=True, destination=False) or [])
                    if cmds.nodeType(node) in ('checker', 'file')]
        if len(textures) == 1:
            texture = textures[0]
            places = cmds.listConnections(texture+'.uvCoord', source=True, destination=False,
                                          type='place2dTexture') or []
            if len(places) == 1:
                result = shader, texture, places[0]
                if cache is not None:
                    cache[groups] = result
                return result
    raise ValueError('Checker preview is disconnected on {}. Remove Checker and apply it again.'.format(shape))


def _tiling(value):
    value = float(value)
    if not math.isfinite(value) or not 1 <= value <= 4096:
        raise ValueError('Checker tiles must be between 1 and 4096.')
    return value


def _texture_cells(texture):
    if cmds.nodeType(texture) != 'file':
        return 2.
    return (cmds.getAttr(texture+'.'+TEXTURE_CELLS)
            if cmds.attributeQuery(TEXTURE_CELLS,node=texture,exists=True) else 8.)


def _current_tiles(shape):
    _, texture, place = _preview_network(shape)
    return cmds.getAttr(place+'.repeatU') * _texture_cells(texture)


def _set_tiles(shape, tiles):
    _, texture, place = _preview_network(shape)
    repeat = tiles / _texture_cells(texture)
    cmds.setAttr(place+'.repeatUV', repeat, repeat, type='double2')


def _file_spec(texture):
    return dict(path=cmds.getAttr(texture+'.fileTextureName'),
                cells=cmds.getAttr(texture+'.'+TEXTURE_CELLS) if cmds.attributeQuery(TEXTURE_CELLS,node=texture,exists=True) else 8.,
                style=cmds.getAttr(texture+'.'+TEXTURE_STYLE) if cmds.attributeQuery(TEXTURE_STYLE,node=texture,exists=True) else 'color')


def _set_file_pattern(texture,style,spec=None):
    spec=spec or dict(path=style_texture(style).as_posix(),cells=24. if style=='direction' else 8. if style=='color' else 1.,style=style)
    cmds.setAttr(texture+'.fileTextureName',spec['path'],type='string')
    if not cmds.attributeQuery(TEXTURE_STYLE,node=texture,exists=True):cmds.addAttr(texture,longName=TEXTURE_STYLE,dataType='string',hidden=True)
    if not cmds.attributeQuery(TEXTURE_CELLS,node=texture,exists=True):cmds.addAttr(texture,longName=TEXTURE_CELLS,attributeType='double',hidden=True)
    cmds.setAttr(texture+'.'+TEXTURE_STYLE,spec['style'],type='string')
    cmds.setAttr(texture+'.'+TEXTURE_CELLS,spec['cells'])


def _make_texture(style, place, spec=None):
    kind = 'checker' if style == 'gray' else 'file'
    texture = cmds.shadingNode(kind, asTexture=True, name='uvStraightCheckerTexture#')
    if style == 'gray':
        _set_palette(texture)
    else:
        _set_file_pattern(texture,style,spec)
        cmds.setAttr(texture+'.colorSpace', 'sRGB', type='string')
        cmds.setAttr(texture+'.ignoreColorSpaceFileRules', True)
        # File textures require the full place2d mapping, including vertex UVs.
        for attr in ('coverage', 'translateFrame', 'rotateFrame', 'mirrorU', 'mirrorV',
                     'stagger', 'wrapU', 'wrapV', 'repeatUV', 'offset', 'rotateUV',
                     'noiseUV', 'vertexUvOne', 'vertexUvTwo', 'vertexUvThree', 'vertexCameraOne'):
            cmds.connectAttr(place+'.'+attr, texture+'.'+attr)
    cmds.connectAttr(place+'.outUV', texture+'.uvCoord')
    cmds.connectAttr(place+'.outUvFilterSize', texture+'.uvFilterSize')
    return texture


def _link_uv_set(shape, texture, uv_set):
    for index in cmds.getAttr(shape+'.uvSet', multiIndices=True) or []:
        plug = '{}.uvSet[{}].uvSetName'.format(shape, index)
        if cmds.getAttr(plug) == uv_set:
            cmds.uvLink(make=True, texture=texture, uvSet=plug)
            break


def _shared_network(shape):
    # Maya duplicates may share SGs even when the message connections weren't copied.
    own = {shape, (cmds.listRelatives(shape, parent=True, fullPath=True) or [''])[0]}
    for node in _helpers(shape):
        if cmds.nodeType(node) == 'shadingEngine':
            for member in cmds.sets(node, query=True) or []:
                if any(owner not in own for owner in (cmds.ls(member, long=True, objectsOnly=True) or [])):
                    return True
        for plug in cmds.listConnections(node+'.message', source=False, destination=True, plugs=True) or []:
            if '.'+NODES+'[' in plug and (cmds.ls(plug.split('.')[0], long=True) or [''])[0] != shape:
                return True
    return False


def _detach_network(shape):
    """Copy a shared preview graph without replacing original material backups."""
    selection = cmds.ls(selection=True, long=True) or []
    # An upstream duplicate may traverse uvChooser into the mesh itself.
    # Copy placement values only and rebuild the small owned texture graph.
    _, old_texture, old_place = _preview_network(shape)
    spec=_file_spec(old_texture) if cmds.nodeType(old_texture)=='file' else None
    style=spec['style'] if spec else 'gray'
    shader = cmds.shadingNode('surfaceShader', asShader=True, name='uvStraightCheckerMaterial#')
    place = cmds.duplicate(old_place)[0]
    texture = _make_texture(style, place,spec)
    cmds.connectAttr(texture+'.outColor', shader+'.outColor')
    helpers = [shader, texture, place]
    snapshot = _snapshot(shape)
    for assignment in snapshot['assignments']:
        previous = assignment['name']
        if not cmds.attributeQuery(ORIGINAL, node=previous, exists=True):
            continue  # Keep manual material assignments made during preview.
        original = (cmds.listConnections(previous+'.'+ORIGINAL, source=True, destination=False) or [])[0]
        sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name='uvStraightCheckerSG#')
        cmds.connectAttr(shader+'.outColor', sg+'.surfaceShader')
        cmds.addAttr(sg, longName=ORIGINAL, attributeType='message')
        cmds.connectAttr(original+'.message', sg+'.'+ORIGINAL)
        cmds.sets(_face_components(shape,assignment['faces']), edit=True, forceElement=sg)
        helpers.append(sg)
    if cmds.attributeQuery(NODES, node=shape, exists=True):
        cmds.deleteAttr(shape+'.'+NODES)
    cmds.addAttr(shape, longName=NODES, attributeType='message', multi=True)
    for index, node in enumerate(helpers):
        cmds.connectAttr(node+'.message', '{}.{}[{}]'.format(shape, NODES, index))
    _link_uv_set(shape, texture, _state(shape)['uvSet'])
    if not visible():
        _set_group_visibility(False)
    cmds.select(selection, replace=True)


def _switch_texture(shape, style):
    shader, texture, place = _preview_network(shape)
    wanted = 'checker' if style == 'gray' else 'file'
    if cmds.nodeType(texture) == wanted:
        if style == 'gray':
            _set_palette(texture)
        else:
            _set_file_pattern(texture,style)
        return
    replacement = _make_texture(style, place)
    cmds.connectAttr(replacement+'.outColor', shader+'.outColor', force=True)
    _link_uv_set(shape, replacement, _state(shape)['uvSet'])
    cmds.delete(texture)
    used = cmds.getAttr(shape+'.'+NODES, multiIndices=True) or []
    index = max(used, default=-1)+1
    cmds.connectAttr(replacement+'.message', '{}.{}[{}]'.format(shape, NODES, index))


def _active_preview_blend(shape):
    """Return the CPV blend needed by an active ShellShifter viewport layer."""
    if not cmds.attributeQuery('uvStraightViewportPreviewState',
                               node=shape, exists=True):
        return None
    try:
        import uv_straight_native as native
        status = native.status()
        if status.get('distortion_enabled') and not status.get('viewport_override'):
            return 6  # Modulate2X: checker remains visible below distortion.
    except (ImportError, RuntimeError):
        pass
    return 0  # Texel Density and other explicit layers keep exact colors.


def apply(tiles=16, style=None, *, existing_only=False):
    import uv_straight as uv
    from uv_straight_colors import finished_visible
    show_finished = finished_visible()
    style = _validate_style(preferred_style() if style is None else style)
    if tiles is not None:
        tiles = _tiling(tiles)
    shapes = _meshes()
    states = {shape: _state(shape) for shape in shapes}
    if existing_only:
        shapes = [shape for shape in shapes if states[shape] is not None]
        if not shapes:
            _remember_style(style)
            return 0
    plans = [(shape, _snapshot(shape) if states[shape] is None else None)
             for shape in shapes]
    selection = cmds.ls(selection=True, long=True) or []
    existing_networks = defaultdict(list)
    graph_cache = {}
    for shape, snapshot in plans:
        if snapshot is None:
            existing_networks[_preview_network(shape, graph_cache)].append(shape)
    shared_updates = {}
    for (shader, texture, place), members in existing_networks.items():
        # A texture type change on a non-default UV set needs per-mesh UV links.
        same_kind = (cmds.nodeType(texture) == ('checker' if style == 'gray' else 'file'))
        if _owns_preview(shader, members) and (same_kind or all(states[s].get('defaultUV') for s in members)):
            representative = members[0]
            shared_updates.update(dict.fromkeys(members, representative))

    with uv._undo('ShellShifter Checker'):
        try:
            networks = {}
            memberships = defaultdict(list)
            for shape, state in plans:
                representative = shared_updates.get(shape)
                value = tiles if tiles is not None else (
                    _current_tiles(shape) if state is None and representative in (None, shape) else 16.)
                if state is not None:
                    originals = tuple(sorted(item['name'] for item in state['assignments']))
                    key = (value, originals, state['defaultUV'])
                    if key not in networks:
                        shader = cmds.shadingNode('surfaceShader', asShader=True, name='uvStraightCheckerMaterial#')
                        place = cmds.shadingNode('place2dTexture', asUtility=True, name='uvStraightCheckerPlacement#')
                        texture = _make_texture(style, place)
                        cmds.connectAttr(texture+'.outColor', shader+'.outColor')
                        cells = 2 if style == 'gray' else cmds.getAttr(texture+'.'+TEXTURE_CELLS)
                        repeat = value / cells
                        cmds.setAttr(place+'.repeatUV', repeat, repeat, type='double2')
                        groups = {}
                        for original in originals:
                            sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name='uvStraightCheckerSG#')
                            cmds.connectAttr(shader+'.outColor', sg+'.surfaceShader')
                            cmds.addAttr(sg, longName=ORIGINAL, attributeType='message')
                            cmds.connectAttr(original+'.message', sg+'.'+ORIGINAL)
                            groups[original] = sg
                        networks[key] = (texture, [shader, texture, place]+list(groups.values()), groups)
                    texture, helpers, groups = networks[key]
                    if not state['defaultUV']:_link_uv_set(shape, texture, state['uvSet'])
                    cmds.addAttr(shape, longName=NODES, attributeType='message', multi=True)
                    for assignment in state['assignments']:
                        members = memberships[groups[assignment['name']]]
                        if len(assignment['faces']) == state['faces']:
                            members.append(shape)
                        else:
                            members.extend(_face_components(shape, assignment['faces']))
                    state['format'] = 2
                    for i,node in enumerate(helpers):
                        cmds.connectAttr(node+'.message', '{}.{}[{}]'.format(shape,NODES,i))
                    cmds.addAttr(shape, longName=STATE, dataType='string', hidden=True)
                    cmds.setAttr(shape+'.'+STATE, json.dumps(state), type='string')
                    preview_blend = _active_preview_blend(shape)
                    if preview_blend is None:
                        cmds.setAttr(shape+'.displayColors', False)
                    else:
                        cmds.setAttr(shape+'.displayColors', True)
                        cmds.setAttr(shape+'.materialBlend', preview_blend)
                elif representative in (None, shape):
                    if representative is None and _shared_network(shape):
                        _detach_network(shape)
                    _switch_texture(shape, style)
                    _set_tiles(shape, value)
                if not cmds.attributeQuery('uvStraightViewportPreviewState',
                                           node=shape, exists=True):
                    if show_finished and cmds.attributeQuery('uvStraightDoneFillBackup', node=shape, exists=True):
                        uv._done_fill(shape, True)
                    else:
                        cmds.setAttr(shape+'.displayColors', False)
            for sg, components in memberships.items():
                cmds.sets(components, edit=True, forceElement=sg)
            if not visible():
                _set_group_visibility(False)
        finally:
            cmds.select(selection, replace=True)
    if not cmds.about(batch=True):
        for panel in cmds.getPanel(visiblePanels=True) or []:
            if cmds.getPanel(typeOf=panel) == 'modelPanel':
                cmds.modelEditor(panel, edit=True, displayTextures=True)
    import uv_straight_native as native
    native.refresh_viewport_colors()
    _remember_style(style)
    return len(shapes)


def set_style(style):
    """Remember choice and switch existing selected previews, preserving tiling."""
    style = _validate_style(style)
    if not cmds.ls(selection=True):
        _remember_style(style)
        return 0
    # Keep bare selected meshes unassigned until Apply is pressed.
    return apply(tiles=None, style=style, existing_only=True)


def _owns_preview(shader, shapes):
    """Include actual SG owners, even when duplicate meshes lack message links."""
    groups = cmds.listConnections(shader+'.outColor', source=False, destination=True,
                                  type='shadingEngine') or []
    groups += [plug.split('.', 1)[0] for plug in
               (cmds.listConnections(shader + '.message', source=False, destination=True, plugs=True) or [])
               if plug.endswith('.' + PREVIEW_SHADER)]
    assigned = [member for sg in set(groups) for member in (cmds.sets(sg, query=True) or [])]
    owners = set(cmds.ls(assigned, objectsOnly=True, long=True) or []) if assigned else set()
    mesh_owners = set(cmds.ls(list(owners), dag=True, type='mesh', long=True,
                              noIntermediate=True) or []) if owners else set()
    return bool(mesh_owners) and mesh_owners <= set(shapes)

def resize(tiles=None, factor=None):
    import uv_straight as uv
    shapes = [s for s in _meshes() if _state(s) is not None]
    if not shapes:
        return 0
    networks = defaultdict(list)
    cache = {}
    for shape in shapes:
        networks[_preview_network(shape, cache)].append(shape)
    plans = []
    for (shader, texture, place), members in networks.items():
        cells = _texture_cells(texture)
        current = cmds.getAttr(place+'.repeatU') * cells
        value = _tiling(tiles if tiles is not None else min(4096, max(1, current*factor)))
        if value == current:
            continue
        shared_selected = _owns_preview(shader, members)
        plans.append((members, place, value/cells, shared_selected, value))
    with uv._undo('ShellShifter Checker Tiling'):
        for members, place, repeat, shared_selected, value in plans:
            if shared_selected:
                cmds.setAttr(place+'.repeatUV', repeat, repeat, type='double2')
            else:
                for shape in members:
                    _detach_network(shape)
                    _set_tiles(shape, value)
    return len(shapes)


def remove():
    import uv_straight as uv
    from uv_straight_colors import finished_visible
    show_finished = finished_visible()
    plans=[]
    original_groups = {}
    for shape in _meshes():
        state=_state(shape)
        if state is None:
            # Combine/separate or transferred assignments can retain checker SGs
            # without the custom attributes from the original mesh.
            groups = cmds.listConnections(shape, type='shadingEngine') or []
            if not any(cmds.attributeQuery(ORIGINAL, node=sg, exists=True) for sg in groups):
                continue
            state = {'format': 2, 'displayColors': cmds.getAttr(shape+'.displayColors')}
        restored=[]
        if state.get('format', 1) >= 2:
            # Maya carries SG membership through extrusion, deletion and subdivision.
            # Restore current memberships, never stale polygon numbers.
            sel=om.MSelectionList(); sel.add(shape)
            path=sel.getDagPath(0)
            shaders,indices=om.MFnMesh(path).getConnectedShaders(path.instanceNumber())
            face_groups = defaultdict(list)
            for face, index in enumerate(indices):
                if index >= 0:
                    face_groups[index].append(face)
            for index, faces in face_groups.items():
                sg = om.MFnDependencyNode(shaders[index]).name()
                if sg not in original_groups:
                    original_groups[sg] = None
                    if cmds.attributeQuery(ORIGINAL, node=sg, exists=True):
                        originals = cmds.listConnections(sg+'.'+ORIGINAL,source=True,destination=False) or []
                        if not originals:
                            raise ValueError('Original material group was deleted for '+sg)
                        original_groups[sg] = originals[0]
                if original_groups[sg]:
                    restored.append((original_groups[sg], None if len(faces) == len(indices) else faces))
        else:
            count=cmds.polyEvaluate(shape, face=True)
            changed=count != state['faces']
            saved=state['assignments']
            if changed:
                # Older versions discarded live material membership. A single
                # original material can still be restored exactly on all faces.
                saved=[max(saved,key=lambda item: len(item['faces']))]
                if len(state['assignments'])>1:
                    cmds.warning('Legacy checker: topology changed; restoring the main original material. Old per-face assignments are unavailable.')
            for assignment in saved:
                matches=cmds.ls(assignment['uuid']) or []
                if len(matches)!=1 or cmds.nodeType(matches[0])!='shadingEngine':
                    raise ValueError('Original material group was deleted: '+assignment['name'])
                restored.append((matches[0],list(range(count)) if changed else assignment['faces']))
        plans.append((shape,state,restored))
    if not plans:
        raise ValueError('No ShellShifter checker found on selected meshes. The visible checker may belong to another material or the UV Editor preview.')
    with uv._undo('ShellShifter Remove Checker'):
        group_cache = {}
        cleanup = {frozenset(_helpers(shape, group_cache)) for shape, _, _ in plans}
        memberships = defaultdict(list)
        for shape, state, assignments in plans:
            for sg, faces in assignments:
                memberships[sg].extend([shape] if faces is None else _face_components(shape, faces))
        for sg, components in memberships.items():
            cmds.sets(components, edit=True, forceElement=sg)
        for shape,state,assignments in plans:
            if (state.get('format',1)<2 and len(state['assignments'])>1 and
                    cmds.polyEvaluate(shape,face=True)!=state['faces']):
                backup=shape+'.uvStraightLegacyCheckerBackup'
                if not cmds.objExists(backup):
                    cmds.addAttr(shape,longName='uvStraightLegacyCheckerBackup',dataType='string',hidden=True)
                cmds.setAttr(backup,json.dumps(state),type='string')
            preview_active = cmds.attributeQuery('uvStraightViewportPreviewState',
                                                 node=shape, exists=True)
            if preview_active:
                # Keep the active preview visible; without the checker its
                # colors return to the normal overwrite behavior.
                cmds.setAttr(shape+'.displayColors', True)
                cmds.setAttr(shape+'.materialBlend', 0)
            else:
                cmds.setAttr(shape+'.displayColors',state['displayColors'])
            for attribute in (STATE, NODES):
                if cmds.attributeQuery(attribute, node=shape, exists=True):
                    cmds.deleteAttr(shape+'.'+attribute)
            if show_finished and cmds.attributeQuery('uvStraightDoneFillBackup',node=shape,exists=True):
                uv._done_fill(shape, True)
        for network in cleanup:
            nodes = [node for node in network if cmds.objExists(node)]
            shared = any(any('.'+NODES+'[' in plug for plug in
                (cmds.listConnections(node+'.message', source=False, destination=True, plugs=True) or []))
                for node in nodes)
            shared = shared or any(cmds.sets(node, query=True) for node in nodes
                                   if cmds.nodeType(node)=='shadingEngine')
            if nodes and not shared:
                cmds.delete(nodes)
    import uv_straight_native as native
    native.refresh_viewport_colors()
    return len(plans)
