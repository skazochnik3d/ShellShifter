"""Native UV editor annotation manager. Explicit enable/disable; restores editors."""
from pathlib import Path
from contextlib import contextmanager
import maya.cmds as cmds
import maya.api.OpenMaya as om
import uv_straight_draw_state as draw_state
try:
    from PySide6 import QtCore
except ImportError:
    from PySide2 import QtCore

_manager = globals().get('_manager')  # Reload must not orphan active timers.


class DisplayState(QtCore.QObject):
    changed = QtCore.Signal(dict)


_display_state = globals().get('_display_state') or DisplayState()
_wireframe_mode = globals().get('_wireframe_mode', 'full')


def wireframe_visible():
    return cmds.displayPref(query=True, wireframeOnShadedActive=True) != 'none'


def set_wireframe(enabled):
    """Toggle Maya's active shaded-object wires without changing mesh data."""
    global _wireframe_mode
    current = cmds.displayPref(query=True, wireframeOnShadedActive=True)
    if enabled:
        target = current if current != 'none' else _wireframe_mode
    else:
        if current != 'none':
            _wireframe_mode = current
        target = 'none'
    if target != current:
        # Display preferences are not a UV edit; retain the artist's Undo/Redo.
        with _no_undo():
            cmds.displayPref(wireframeOnShadedActive=target)
    _notify_display()


def display_state():
    """Shared settings, independent of which palette is currently visible."""
    return dict(
        wireframe_enabled=wireframe_visible(),
        pins_enabled=bool(_manager is not None and _manager.pins_enabled),
        distortion_enabled=(bool(_manager.distortion_enabled) if _manager is not None
                            else bool(cmds.optionVar(query='uvStraightDistortion'))
                            if cmds.optionVar(exists='uvStraightDistortion') else False),
        distortion_range=(float(_manager.distortion_range) if _manager is not None
                          else float(cmds.optionVar(query='uvStraightDistortionRange'))
                          if cmds.optionVar(exists='uvStraightDistortionRange') else 2.))


def _notify_display():
    _display_state.changed.emit(display_state())


def ensure_viewport_colors():
    """Repair display attributes after UI/material changes; reuse cached colors."""
    if _manager is not None and not _manager.viewport_display_ready():
        _manager.force_viewport_color = True
        _manager._safe_publish_viewport()

COLORS = {'H': (1.0, 0.59, 0.23), 'V': (0.22, 0.79, 1.0)}


@contextmanager
def _no_undo():
    state = cmds.undoInfo(query=True, state=True)
    modified = cmds.file(query=True, modified=True)
    cmds.undoInfo(stateWithoutFlush=False)
    try:
        yield
    finally:
        cmds.undoInfo(stateWithoutFlush=state)
        if not modified and cmds.file(query=True, modified=True):
            cmds.file(modified=False)


def build_payload(meshes):
    import uv_straight as uv
    result = []
    for mesh in meshes:
        membership = {}
        groups = uv._marks(mesh)
        for group in groups:
            ids = group['ids']
            segments = [(mesh.points[a], mesh.points[b])
                        for a, b in uv._marked_pairs(mesh, group)]
            result.append((COLORS[group['axis']], segments, [mesh.points[i] for i in ids]))
            for i in ids:
                membership.setdefault(i, set()).add(group['axis'])
        corners = [mesh.points[i] for i, axes in membership.items() if len(axes) > 1]
        if corners:
            result.append(((1.0, 1.0, 1.0), [], corners))
    return result


def _find_editors():
    """UV Editor is a scripted panel, not an lsUI(editors=True) entry."""
    try:
        panels = cmds.getPanel(scriptType='polyTexturePlacementPanel') or []
    except RuntimeError:
        return []
    editors = []
    for panel in panels:
        try:
            if cmds.textureWindow(panel, exists=True):
                editors.append(panel)
        except RuntimeError:
            continue  # A scripted panel can disappear while its window closes.
    return editors


def _active_source(editor):
    """Keep an implicit active-list connection parented to the UV control."""
    control = cmds.textureWindow(editor, query=True, control=True)
    if control:
        return cmds.selectionConnection(activeList=True, parent=control)
    return cmds.selectionConnection(activeList=True)


def _source_objects(connection, selected, meshes):
    """Use the editor list when queryable; Maya may hide activeList contents."""
    try:
        objects = cmds.selectionConnection(connection, query=True, object=True)
        if isinstance(objects, (list, tuple)):
            return list(objects)
    except RuntimeError:
        pass
    # The active-list connection can be valid yet refuse an object query in
    # some Maya UI states. These are the same selected objects as that list.
    return list(dict.fromkeys(selected or [mesh.name for mesh in meshes]))


class NativeAnnotations(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.editors = {}
        self.helper = None
        self.transform = None
        self.helper_uuid = None
        self.error = ''
        self.step = 'idle'
        self._fingerprint = None
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh)
        self.plugin = None
        self.callbacks = []

    def start(self):
        path = str(Path(__file__).with_name('uv_straight_draw_plugin.py'))
        loaded = cmds.pluginInfo('uv_straight_draw_plugin', query=True, loaded=True)
        if loaded and cmds.pluginInfo('uv_straight_draw_plugin', query=True, version=True) != '1.4.37':
            if cmds.ls(type='uvStraightAnnotationShape'):
                raise RuntimeError('Disable the old UV annotation helper before upgrading.')
            cmds.unloadPlugin('uv_straight_draw_plugin')
            loaded = False
        if not loaded:
            cmds.loadPlugin(path, quiet=True)
        self.plugin = draw_state
        self.plugin.DRAW_COUNT = 0
        self.plugin.CAN_DRAW_COUNT = 0
        self.plugin.UV_CALL_COUNT = 0
        self.plugin.DRAW_ERROR = ''
        self._install_callbacks()
        if not _find_editors():
            import maya.mel as mel
            mel.eval('texturePanelShow()')
        self.refresh()
        self.timer.start()

    def _install_callbacks(self):
        # Even a doNotWrite node can cause a Maya "requires plugin" line.
        # Remove runtime shapes before serialization and recreate afterwards.
        for before, after in ((om.MSceneMessage.kBeforeSave, om.MSceneMessage.kAfterSave),
                              (om.MSceneMessage.kBeforeExport, om.MSceneMessage.kAfterExport),
                              (om.MSceneMessage.kBeforeNew, om.MSceneMessage.kAfterNew),
                              (om.MSceneMessage.kBeforeOpen, om.MSceneMessage.kAfterOpen)):
            self.callbacks.append(om.MSceneMessage.addCallback(before, self._suspend))
            self.callbacks.append(om.MSceneMessage.addCallback(after, self._resume))

    def _resume(self, *unused):
        self.refresh()
        self.timer.start()

    def _create_helper(self):
        if self._find_helper():
            return
        with _no_undo():
            self.helper = cmds.createNode(self.plugin.TYPE_NAME, name='uvStraightAnnotationsShape#', skipSelect=True)
            self.helper_uuid = cmds.ls(self.helper, uuid=True)[0]
            self.transform = cmds.listRelatives(self.helper, parent=True, fullPath=True)[0]
            cmds.setAttr(self.transform + '.hiddenInOutliner', True)
            cmds.setAttr(self.helper + '.hiddenInOutliner', True)
            for node in (self.helper, self.transform):
                selection = om.MSelectionList()
                selection.add(node)
                om.MFnDependencyNode(selection.getDependNode(0)).setDoNotWrite(True)

    def _find_helper(self):
        matches = cmds.ls(self.helper_uuid, long=True) if self.helper_uuid else []
        if matches and cmds.nodeType(matches[0]) == self.plugin.TYPE_NAME:
            self.helper = matches[0]
            self.transform = cmds.listRelatives(self.helper, parent=True, fullPath=True)[0]
            return self.helper
        return None

    def _restore(self, editor):
        state = self.editors.pop(editor, None)
        if not state:
            return
        original, connection = state
        self.step = 'restore editor connection'
        if cmds.textureWindow(editor, exists=True):
            current = cmds.textureWindow(editor, query=True, mainListConnection=True)
            if current == connection:
                if not original or not cmds.selectionConnection(original, exists=True):
                    original = _active_source(editor)
                cmds.textureWindow(editor, edit=True, forceMainConnection=original)
        if cmds.selectionConnection(connection, exists=True):
            cmds.deleteUI(connection)

    def refresh(self):
        import uv_straight as uv
        try:
            self.step = 'find UV editors'
            editors = _find_editors()
            for editor in list(self.editors):
                if editor not in editors:
                    self._restore(editor)
            if not editors:
                return
            # Source the same selected meshes as the tool. The helper is never
            # added to Maya's active selection, so marking and transforms stay normal.
            try:
                self.step = 'read selected meshes'
                selected, meshes = uv._selection()
            except ValueError:
                selected = []
                meshes = []
            self.step = 'build color data'
            payload = build_payload(meshes)
            signature = repr(payload)
            self.plugin.PAYLOAD = payload
            with _no_undo():
                if payload:
                    self.step = 'create annotation shape'
                    self._create_helper()
                    for editor in editors:
                        if editor not in self.editors:
                            self.step = 'query main editor connection'
                            original = cmds.textureWindow(editor, query=True, mainListConnection=True)
                            if not original or not cmds.selectionConnection(original, exists=True):
                                # The native UV panel may use Maya's implicit active
                                # list and report no named connection. Materialize
                                # an equivalent live list so it can be queried and
                                # restored after annotations are disabled.
                                self.step = 'create active selection source'
                                original = _active_source(editor)
                            self.step = 'create selection connection'
                            connection = cmds.selectionConnection()
                            self.editors[editor] = (original, connection)
                            self.step = 'connect annotation to editor'
                            cmds.textureWindow(editor, edit=True, forceMainConnection=connection)
                        original, connection = self.editors[editor]
                        if cmds.textureWindow(editor, query=True, mainListConnection=True) != connection:
                            self._restore(editor)
                            continue
                        self.step = 'read original editor objects'
                        source = _source_objects(original, selected, meshes)
                        desired = list(dict.fromkeys(source + [self.helper]))
                        self.step = 'read annotation editor objects'
                        current = cmds.selectionConnection(connection, query=True, object=True) or []
                        if current != desired:
                            self.step = 'update annotation editor objects'
                            cmds.selectionConnection(connection, edit=True, clear=True)
                            for item in desired:
                                cmds.selectionConnection(connection, edit=True, select=item)
                            signature += repr(desired)
                else:
                    for editor in list(self.editors):
                        self._restore(editor)
                if signature != self._fingerprint:
                    self.step = 'redraw UV editor'
                    for editor in editors:
                        cmds.textureWindow(editor, edit=True, refresh=True)
                    self._fingerprint = signature
            self.error = ''
            self.step = 'ready'
        except (RuntimeError, ValueError) as error:
            self.error = '{}: {}'.format(self.step, error)
            self.plugin.PAYLOAD = []
            with _no_undo():
                for editor in list(self.editors):
                    self._restore(editor)

    def _suspend(self, *unused):
        self.timer.stop()
        with _no_undo():
            for editor in list(self.editors):
                self._restore(editor)
            if self._find_helper():
                cmds.delete(self.transform)
        if self.plugin:
            self.plugin.PAYLOAD = []
        self.helper = self.transform = None
        self.helper_uuid = None

    def stop(self):
        self._suspend()
        for callback in self.callbacks:
            om.MMessage.removeCallback(callback)
        self.callbacks = []


def enable(open_uv_editor=True, pins_enabled=True):
    global _manager
    if cmds.about(batch=True):
        raise RuntimeError('Native UV annotations require the Maya GUI.')
    if _manager is not None:
        return _manager
    from uv_straight_qt_native import QtAnnotations
    _manager = QtAnnotations()
    _manager.pins_enabled = bool(pins_enabled)
    try:
        _manager.start(open_uv_editor=open_uv_editor)
    except Exception:
        _manager.stop()
        _manager = None
        raise
    _notify_display()
    return _manager


def disable():
    global _manager
    if _manager is not None:
        _manager.stop()
        _manager = None
    _notify_display()


def set_distortion(enabled, color_range=None):
    if _manager is None:
        if not enabled:
            cmds.optionVar(intValue=('uvStraightDistortion', 0))
            if color_range is not None:
                cmds.optionVar(floatValue=('uvStraightDistortionRange', float(color_range)))
            _notify_display()
            return
        enable(pins_enabled=False)
    _manager.set_distortion(enabled, color_range)
    _notify_display()


def set_pins(enabled):
    if _manager is None:
        if not enabled:
            _notify_display()
            return
        enable(pins_enabled=True)
    _manager.set_pins(enabled)
    _notify_display()


def set_viewport_overlay(payload, owner):
    global _manager
    if _manager is None:
        enable(open_uv_editor=False, pins_enabled=False)
    _manager.set_viewport_override(payload, owner)


def clear_viewport_overlay(owner=None):
    if _manager is not None:
        _manager.clear_viewport_override(owner)


def set_check_overlay(uv_payload, world_segments, owner):
    """Show validation edges in UV and model viewports without scene edits."""
    global _manager
    if _manager is None:
        enable(open_uv_editor=True, pins_enabled=False)
    _manager.set_check_overlay(uv_payload, world_segments, owner)


def clear_check_overlay(owner=None):
    if _manager is not None:
        _manager.clear_check_overlay(owner)


def restore_viewport_colors(only=None):
    """Restore underlying mesh colors for all previews or selected shapes."""
    if _manager is not None:
        _manager.live_viewport_preview.clear()
        _manager._restore_viewport_colors(only)
        return
    from uv_straight_qt_native import QtAnnotations
    temporary = QtAnnotations()
    if only is not None:
        temporary._restore_viewport_colors(only)
        temporary.viewport_recovery_pending = False
    temporary.stop()


def refresh_viewport_colors():
    """Reapply an active native color layer after a material assignment."""
    if _manager is None:
        return
    _manager.force_viewport_color = True
    _manager._safe_publish_viewport()


def unload_legacy_viewport_plugin():
    """Unload the retired VP2 provider when no old live node owns it."""
    name = 'uv_straight_draw_plugin'
    if not cmds.pluginInfo(name, query=True, loaded=True):
        return True
    if cmds.ls(type='uvStraightAnnotationShape'):
        return False
    cmds.pluginInfo(name, edit=True, writeRequires=False)
    cmds.unloadPlugin(name)
    return True


def recover(previous_manager):
    """Stop a manager loaded from an older file, then start this version."""
    import types
    global _manager
    if hasattr(previous_manager, 'snapshot'):
        previous_manager.stop()
        _manager = None
        return enable()
    previous_manager._restore = types.MethodType(NativeAnnotations._restore, previous_manager)
    # The old _restore removed its saved state before forceMainConnection
    # failed. Find any editor still using that stranded annotation list.
    helper = previous_manager.helper
    if helper:
        helper_name = helper.rsplit('|', 1)[-1]
        for editor in _find_editors():
            try:
                connection = cmds.textureWindow(editor, query=True, mainListConnection=True)
                if not connection or not cmds.selectionConnection(connection, exists=True):
                    continue
                objects = cmds.selectionConnection(connection, query=True, object=True) or []
            except RuntimeError:
                continue
            if any(item.rsplit('|', 1)[-1] == helper_name for item in objects):
                cmds.textureWindow(editor, edit=True, forceMainConnection=_active_source(editor))
                cmds.deleteUI(connection)
    _manager = previous_manager
    disable()
    return enable()


def status():
    if _manager is None:
        return {'enabled': False}
    if hasattr(_manager, 'snapshot'):
        return _manager.snapshot()
    panels = cmds.getPanel(scriptType='polyTexturePlacementPanel') or []
    windows = {panel: bool(cmds.textureWindow(panel, exists=True)) for panel in panels}
    try:
        import uv_straight as uv
        _, meshes = uv._selection()
        marked_groups = sum(len(uv._marks(mesh)) for mesh in meshes)
    except (ValueError, RuntimeError):
        marked_groups = 0
    connections = {}
    for editor, (_original, connection) in _manager.editors.items():
        try:
            connections[editor] = {
                'main': cmds.textureWindow(editor, query=True, mainListConnection=True),
                'objects': cmds.selectionConnection(connection, query=True, object=True) or []}
        except RuntimeError as error:
            connections[editor] = {'error': str(error)}
    return {'enabled': True, 'editors': list(_manager.editors),
            'draw_calls': _manager.plugin.DRAW_COUNT if _manager.plugin else 0,
            'can_draw_calls': _manager.plugin.CAN_DRAW_COUNT if _manager.plugin else 0,
            'uv_calls': _manager.plugin.UV_CALL_COUNT if _manager.plugin else 0,
            'draw_error': _manager.plugin.DRAW_ERROR if _manager.plugin else '',
            'uv_panels': windows, 'selected_mark_groups': marked_groups,
            'payload_groups': len(_manager.plugin.PAYLOAD) if _manager.plugin else 0,
            'helper': _manager._find_helper(), 'connections': connections,
            'error': _manager.error}


def probe_selected_helper():
    """Temporarily activate the helper to test Maya's drawUV selection gate.

    Restores the exact Maya API selection list and the annotation timer, even
    when the UV editor or its rendering callback raises an exception.
    """
    if _manager is not None and hasattr(_manager, 'snapshot'):
        return _manager.snapshot()
    if _manager is None or not _manager._find_helper():
        raise RuntimeError('Enable native colors and select a marked mesh first.')
    try:
        from PySide6 import QtWidgets
    except ImportError:
        from PySide2 import QtWidgets

    original = om.MGlobal.getActiveSelectionList()
    test_selection = om.MGlobal.getActiveSelectionList()
    test_selection.add(_manager.helper)
    was_running = _manager.timer.isActive()
    before = status()
    selection_before = cmds.ls(selection=True, long=True, flatten=True) or []
    _manager.timer.stop()
    try:
        with _no_undo():
            om.MGlobal.setActiveSelectionList(test_selection)
            for editor in _manager.editors:
                cmds.textureWindow(editor, edit=True, refresh=True)
            cmds.refresh(force=True)
            QtWidgets.QApplication.processEvents()
            QtWidgets.QApplication.processEvents()
            during = status()
            helper_active = any(item == _manager.helper for item in
                                (cmds.ls(selection=True, long=True) or []))
    finally:
        try:
            with _no_undo():
                om.MGlobal.setActiveSelectionList(original)
                for editor in _manager.editors:
                    if cmds.textureWindow(editor, exists=True):
                        cmds.textureWindow(editor, edit=True, refresh=True)
        finally:
            if was_running:
                _manager.timer.start()
    result = {name: {'before': before[name], 'with_helper_selected': during[name]}
              for name in ('can_draw_calls', 'uv_calls', 'draw_calls', 'draw_error')}
    result['helper_active_during_probe'] = helper_active
    result['selection_restored'] = selection_before == (
        cmds.ls(selection=True, long=True, flatten=True) or [])
    return result
