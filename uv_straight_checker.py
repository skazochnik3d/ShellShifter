"""Reversible per-mesh checker assignments with scene-persistent backups."""
import json
import math
import weakref
from pathlib import Path
from collections import defaultdict
import maya.cmds as cmds
import maya.api.OpenMaya as om

STATE = 'uvStraightCheckerState'
NODES = 'uvStraightCheckerNodes'
ORIGINAL = 'uvStraightOriginalMaterial'
# Scene-linear greys: softer contrast than pure black/white in the viewport.
CHECKER_DARK = (.22, .22, .22)
CHECKER_LIGHT = (.55, .55, .55)
STYLES = (('gray', 'Gray Squares'), ('color', 'Color Labels'))
STYLE_OPTION = 'uvStraightCheckerStyle'
COLOR_TEXTURE = Path(__file__).resolve().parent / 'assets' / 'shellshifter_checker_color.png'
_style_listeners = globals().get('_style_listeners', [])


def preferred_style():
    value = cmds.optionVar(query=STYLE_OPTION) if cmds.optionVar(exists=STYLE_OPTION) else 'gray'
    return value if value in dict(STYLES) else 'gray'


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
    if style not in dict(STYLES):
        raise ValueError('Unknown checker style: ' + str(style))
    if style == 'color' and not COLOR_TEXTURE.is_file():
        raise ValueError('Color checker texture is missing: ' + str(COLOR_TEXTURE))
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
            'uvSet': fn.currentUVSetName()}


def _helpers(shape):
    nodes = set(cmds.listConnections(shape+'.'+NODES, source=True, destination=False) or []) if cmds.attributeQuery(NODES, node=shape, exists=True) else set()
    # Duplicate copies metadata and SG memberships, but not message connections.
    for sg in cmds.listConnections(shape,type='shadingEngine') or []:
        if not cmds.attributeQuery(ORIGINAL,node=sg,exists=True):
            continue
        nodes.add(sg)
        for shader in cmds.listConnections(sg+'.surfaceShader',source=True,destination=False) or []:
            nodes.add(shader)
            for kind in ('checker', 'file'):
                for texture in cmds.listConnections(shader+'.outColor',source=True,destination=False,type=kind) or []:
                    nodes.add(texture)
                    nodes.update(cmds.listConnections(texture,source=True,destination=False,type='place2dTexture') or [])
    return nodes


def _node(shape, kind):
    nodes = [node for node in _helpers(shape) if cmds.nodeType(node)==kind]
    if len(nodes) != 1:
        raise ValueError('Checker helper is missing; remove the checker and apply it again.')
    return nodes[0]


def _tiling(value):
    value = float(value)
    if not math.isfinite(value) or not 1 <= value <= 4096:
        raise ValueError('Checker tiles must be between 1 and 4096.')
    return value


def _texture(shape):
    shader = _node(shape, 'surfaceShader')
    textures = cmds.listConnections(shader+'.outColor', source=True, destination=False) or []
    textures = [node for node in textures if cmds.nodeType(node) in ('checker', 'file')]
    if len(textures) != 1:
        raise ValueError('Checker texture is missing; remove the checker and apply it again.')
    return textures[0]


def _squares_per_repeat(shape):
    return 8. if cmds.nodeType(_texture(shape)) == 'file' else 2.


def _current_tiles(shape):
    return cmds.getAttr(_node(shape, 'place2dTexture')+'.repeatU') * _squares_per_repeat(shape)


def _set_tiles(shape, tiles):
    # Preserve the square density: gray has 2 cells, labeled image has 8.
    place = _node(shape, 'place2dTexture')
    repeat = tiles / _squares_per_repeat(shape)
    cmds.setAttr(place + '.repeatU', repeat)
    cmds.setAttr(place + '.repeatV', repeat)


def _make_texture(style, place):
    kind = 'file' if style == 'color' else 'checker'
    texture = cmds.shadingNode(kind, asTexture=True, name='uvStraightCheckerTexture#')
    if style == 'gray':
        _set_palette(texture)
    else:
        cmds.setAttr(texture+'.fileTextureName', COLOR_TEXTURE.as_posix(), type='string')
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
    old_texture = _texture(shape)
    style = 'color' if cmds.nodeType(old_texture) == 'file' else 'gray'
    shader = cmds.shadingNode('surfaceShader', asShader=True, name='uvStraightCheckerMaterial#')
    place = cmds.duplicate(_node(shape, 'place2dTexture'))[0]
    texture = _make_texture(style, place)
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
        cmds.sets(['{}.f[{}]'.format(shape, i) for i in assignment['faces']], edit=True, forceElement=sg)
        helpers.append(sg)
    if cmds.attributeQuery(NODES, node=shape, exists=True):
        cmds.deleteAttr(shape+'.'+NODES)
    cmds.addAttr(shape, longName=NODES, attributeType='message', multi=True)
    for index, node in enumerate(helpers):
        cmds.connectAttr(node+'.message', '{}.{}[{}]'.format(shape, NODES, index))
    _link_uv_set(shape, texture, _state(shape)['uvSet'])
    cmds.select(selection, replace=True)


def _switch_texture(shape, style):
    texture = _texture(shape)
    wanted = 'file' if style == 'color' else 'checker'
    if cmds.nodeType(texture) == wanted:
        if style == 'gray':
            _set_palette(texture)
        return
    shader, place = _node(shape, 'surfaceShader'), _node(shape, 'place2dTexture')
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
    style = _validate_style(preferred_style() if style is None else style)
    if tiles is not None:
        tiles = _tiling(tiles)
    shapes = _meshes()
    if existing_only:
        shapes = [shape for shape in shapes if _state(shape) is not None]
        if not shapes:
            _remember_style(style)
            return 0
    plans = [(shape, _snapshot(shape) if _state(shape) is None else None)
             for shape in shapes]
    selection = cmds.ls(selection=True, long=True) or []
    with uv._undo('ShellShifter Checker'):
        try:
            for shape, state in plans:
                value = tiles if tiles is not None else (_current_tiles(shape) if state is None else 16.)
                if state is not None:
                    shader = cmds.shadingNode('surfaceShader', asShader=True, name='uvStraightCheckerMaterial#')
                    place = cmds.shadingNode('place2dTexture', asUtility=True, name='uvStraightCheckerPlacement#')
                    texture = _make_texture(style, place)
                    cmds.connectAttr(texture+'.outColor', shader+'.outColor')
                    _link_uv_set(shape, texture, state['uvSet'])
                    cmds.addAttr(shape, longName=NODES, attributeType='message', multi=True)
                    helpers = [shader, texture, place]
                    for assignment in state['assignments']:
                        sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name='uvStraightCheckerSG#')
                        cmds.connectAttr(shader+'.outColor', sg+'.surfaceShader')
                        cmds.addAttr(sg, longName=ORIGINAL, attributeType='message')
                        cmds.connectAttr(assignment['name']+'.message', sg+'.'+ORIGINAL)
                        cmds.sets(['{}.f[{}]'.format(shape, i) for i in assignment['faces']], edit=True, forceElement=sg)
                        helpers.append(sg)
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
                else:
                    if _shared_network(shape):
                        _detach_network(shape)
                    _switch_texture(shape, style)
                _set_tiles(shape, value)
                if not cmds.attributeQuery('uvStraightViewportPreviewState',
                                           node=shape, exists=True):
                    if cmds.attributeQuery('uvStraightDoneFillBackup', node=shape, exists=True):
                        uv._done_fill(shape, True)
                    else:
                        cmds.setAttr(shape+'.displayColors', False)
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


def resize(tiles=None, factor=None):
    import uv_straight as uv
    shapes = [s for s in _meshes() if _state(s) is not None]
    if not shapes:
        return 0
    plans=[]
    for shape in shapes:
        current=_current_tiles(shape)
        value=_tiling(tiles if tiles is not None else min(4096,max(1,current*factor)))
        plans.append((shape,value))
    with uv._undo('ShellShifter Checker Tiling'):
        for shape,value in plans:
            if _shared_network(shape):
                _detach_network(shape)
            _set_tiles(shape,value)
    return len(shapes)


def remove():
    import uv_straight as uv
    plans=[]
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
            for index, shader in enumerate(shaders):
                sg=om.MFnDependencyNode(shader).name()
                if not cmds.attributeQuery(ORIGINAL,node=sg,exists=True):
                    continue  # Preserve materials assigned manually during preview.
                originals=cmds.listConnections(sg+'.'+ORIGINAL,source=True,destination=False) or []
                if not originals:
                    raise ValueError('Original material group was deleted for '+sg)
                faces=[i for i,j in enumerate(indices) if j==index]
                if faces:
                    restored.append((originals[0],faces))
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
        for shape,state,assignments in plans:
            nodes=_helpers(shape)
            if (state.get('format',1)<2 and len(state['assignments'])>1 and
                    cmds.polyEvaluate(shape,face=True)!=state['faces']):
                backup=shape+'.uvStraightLegacyCheckerBackup'
                if not cmds.objExists(backup):
                    cmds.addAttr(shape,longName='uvStraightLegacyCheckerBackup',dataType='string',hidden=True)
                cmds.setAttr(backup,json.dumps(state),type='string')
            for sg,faces in assignments:
                cmds.sets(['{}.f[{}]'.format(shape,i) for i in faces],edit=True,forceElement=sg)
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
            # Duplicated meshes may share the preview network. Keep it alive
            # until its last owner has removed the checker.
            shared=any(any('.'+NODES+'[' in plug for plug in
                (cmds.listConnections(node+'.message',source=False,destination=True,plugs=True) or []))
                for node in nodes if cmds.objExists(node))
            shared = shared or any(cmds.sets(node,query=True) for node in nodes
                                   if cmds.nodeType(node)=='shadingEngine')
            if nodes and not shared:
                cmds.delete(list(set(nodes)))
            if cmds.attributeQuery('uvStraightDoneFillBackup',node=shape,exists=True):
                uv._done_fill(shape, True)
    import uv_straight_native as native
    native.refresh_viewport_colors()
    return len(plans)
