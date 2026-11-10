"""Adaptive marking menus: Alt+Shift+RMB, scoped by Maya's UV/model bindings."""
from functools import partial
import json
import maya.cmds as cmds
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import wrapInstance, isValid

_manager = globals().get('_manager')


def restore_tools_callback(menu, original):
    """Restore MEL script callbacks through MEL, Python callables through cmds.

    menu has no sourceType flag. Passing a saved MEL script back through cmds
    makes Maya parse it as Python and report SyntaxError during uninstall.
    """
    if callable(original) or not original:
        cmds.menu(menu, edit=True, postMenuCommand=original or '')
    else:
        import maya.mel as mel
        mel.eval('menu -edit -postMenuCommand {} {};'.format(
            json.dumps(original,ensure_ascii=False),json.dumps(menu,ensure_ascii=False)))


def tools_menus():
    from uv_straight_native import _find_editors
    names = {editor + 'ToolsMenu' for editor in _find_editors()}
    if not names:
        return set()
    # A retired and a recreated UV Editor can temporarily share short names.
    return {path for path in cmds.lsUI(menus=True, long=True) or []
            if path.rsplit('|', 1)[-1] in names and cmds.menu(path, exists=True)}


def tools_item(menu):
    return menu + '|' + menu.rsplit('|', 1)[-1] + '_uvStraightOpen'


def text_input_widget(widget):
    while isinstance(widget, QtWidgets.QWidget):
        if isinstance(widget, (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit,
                               QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox)):
            return True
        widget = widget.parentWidget()
    return False


def menu_interaction_active():
    """Do not mutate Maya menus inside a popup's nested event loop."""
    app = QtWidgets.QApplication.instance()
    return bool(app is not None and
                (app.activePopupWidget() is not None or
                 app.mouseButtons() != QtCore.Qt.NoButton))


def after_menu_interaction(function):
    """Keep destructive UI work out of the native menu selection callback."""
    def when_ready():
        if menu_interaction_active():
            QtCore.QTimer.singleShot(50, when_ready)
        else:
            function()
    QtCore.QTimer.singleShot(0, when_ready)


def angle():
    return float(cmds.optionVar(query='uvStraightAutoAngle')) if cmds.optionVar(exists='uvStraightAutoAngle') else 15.


def picker_windows():
    """Return every live tool window that owns a segment picker."""
    import uv_straight as uv
    from shiboken6 import isValid
    windows = [uv._window]
    try:
        import uv_straight_preview as preview
        windows.append(preview._popup)
    except (ImportError, AttributeError):
        pass
    windows.extend(widget for widget in QtWidgets.QApplication.allWidgets()
                   if hasattr(widget, 'pin_picker'))
    result = []
    seen = set()
    for window in windows:
        if (window is None or id(window) in seen or not isValid(window) or
                not hasattr(window, 'pin_picker') or window.pin_picker.stopped):
            continue
        seen.add(id(window))
        result.append(window)
    return result


def picker_window(open_window=False):
    """Return the active picker window, optionally opening the tool first."""
    import uv_straight as uv
    valid = picker_windows()
    active = next((window for window in valid
                   if window.pin_picker.active), None)
    visible = next((window for window in valid if window.isVisible()), None)
    window = active or visible or (valid[0] if valid else None)
    if window is None and open_window:
        uv.show()
        window = uv._window
    return window


def picker_button(open_window=False):
    """Legacy helper: current palettes have no separate picker button."""
    window = picker_window(open_window)
    return window.pin_picker.button if window is not None else None


def picker_active():
    from uv_straight_pick import session
    return session().active


def toggle_picker():
    from uv_straight_pick import session
    state = session()
    state.set_active(not state.active)


def picker_axis():
    from uv_straight_pick import session
    return session().axis


def picker_axes():
    from uv_straight_pick import session
    return session().axes


def toggle_picker_auto_axes():
    from uv_straight_pick import session
    state=session()
    state.set_auto_axes(not state.auto_axes)


def set_picker_mode(axis=None):
    """Toggle H/V controls; no separate activation state is required."""
    if axis not in (None, 'H', 'V'):
        raise ValueError("Picker axis must be H, V or automatic.")
    from uv_straight_pick import session
    state = session()
    if axis is None:
        state.set_active(not state.active)
    else:
        state.toggle_axis(axis,axis not in state.axes)


def execute(action, *unused):
    import uv_straight as uv
    import uv_straight_checker as checker
    import uv_straight_native as native
    commands = {
        'H': partial(uv.mark, 'H'), 'V': partial(uv.mark, 'V'),
        'orient': uv.orient_selected_shells, 'unpin': uv.unpin,
        'unfold': uv.unfold, 'relax': uv.relax, 'pick': toggle_picker,
        'relax_h':partial(uv.relax_axis,'H'), 'relax_v':partial(uv.relax_axis,'V'),
        'pick_auto': partial(set_picker_mode, None),
        'pick_H': partial(set_picker_mode, 'H'),
        'pick_V': partial(set_picker_mode, 'V'),
        'mesh': lambda: uv.unwrap_from_edges(angle(),
            resolution=int(cmds.optionVar(query='uvStraightPackResolution')) if cmds.optionVar(exists='uvStraightPackResolution') else 2048,
            spacing_px=int(cmds.optionVar(query='uvStraightPackSpacing')) if cmds.optionVar(exists='uvStraightPackSpacing') else 40),
        'quad': lambda: uv.quick_straighten_quads('H', angle()),
        'checker': checker.apply, 'checker_off': checker.remove,
        'checker_gray': partial(checker.apply, tiles=None, style='gray'),
        'checker_color': partial(checker.apply, tiles=None, style='color'),
        'tiles_up': partial(checker.resize, factor=2.),
        'tiles_down': partial(checker.resize, factor=.5),
        'distortion': lambda: native.set_distortion(not native.status().get('distortion_enabled', False)),
        'done': partial(uv.mark_done, True), 'done_off': partial(uv.mark_done, False),
        'window': uv.show,
    }
    if action == 'window':
        # Unwind the native selection callback before opening the palette.
        after_menu_interaction(partial(uv._run, commands[action]))
    else:
        uv._run(commands[action])


def populate(menu, *unused):
    from uv_straight_pie import populate as populate_layout
    populate_layout(menu)


class MenuManager(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.menus = {}
        self.tools_hooks = {}
        self.conflicts = set()
        self.radial_popup = None
        self.popup_request = 0
        self.stopping = False
        self.stopped = False
        self.cleanup_timer = QtCore.QTimer(self)
        self.cleanup_timer.setInterval(50)
        self.cleanup_timer.timeout.connect(self._finish_stop)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        app = QtWidgets.QApplication.instance()
        app.installEventFilter(self)
        app.aboutToQuit.connect(self.stop)
        import uv_straight_keymaps as keymaps
        keymaps.register(self)

    def keymap_state_changed(self,action,value):
        if action == 'menu':
            self.popup_request += 1
            if not value and self.radial_popup is not None and isValid(self.radial_popup):
                self.radial_popup.close()
            if menu_interaction_active():
                after_menu_interaction(self.refresh)
            else:
                self.refresh()

    def eventFilter(self, watched, event):
        if self.stopping or self.stopped:
            return False
        if event.type() in (QtCore.QEvent.ToolTip, QtCore.QEvent.ToolTipChange):
            from uv_straight_language import refresh_hover
            refresh_hover(watched)
            return False
        from uv_straight_keymaps import keyboard_key, modifiers, enabled, mouse_button
        if (event.type() == QtCore.QEvent.MouseButtonPress and enabled('menu') and
                event.button() == mouse_button('menu') and event.modifiers() == modifiers('menu')):
            target = watched if isinstance(watched, QtWidgets.QWidget) else QtWidgets.QApplication.widgetAt(
                event.globalPosition().toPoint())
            if self.panel_allowed(target):
                self.popup_request += 1
                self.open_radial(event.globalPosition().toPoint(), target.window(), event.button())
                event.accept()
                return True
        if (event.type() in (QtCore.QEvent.ShortcutOverride,
                            QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease) and enabled('popup') and
                event.key() == keyboard_key('popup') and
                event.modifiers() == modifiers('popup')):
            event.accept()
            if event.type() == QtCore.QEvent.KeyPress and not event.isAutoRepeat():
                import uv_straight_preview as preview
                if menu_interaction_active():
                    def toggle_if_enabled():
                        if enabled('popup'):
                            preview.toggle_popup()
                    after_menu_interaction(toggle_if_enabled)
                else:
                    preview.toggle_popup()
            return True
        return False

    def open_radial(self, point, parent=None, button=None, holding=True):
        """Both Qt input and Maya's native posting hook open this one popup."""
        from uv_straight_keymaps import enabled, mouse_button
        if self.stopping or self.stopped or not enabled('menu'):
            return
        if self.radial_popup is not None and isValid(self.radial_popup):
            if self.radial_popup.isVisible():
                return  # A native posting hook and Qt press can share one gesture.
            self.radial_popup.deleteLater()
        from uv_straight_pie_popup import PiePopup
        if parent is None:
            import maya.OpenMayaUI as omui
            pointer = omui.MQtUtil.mainWindow()
            parent = wrapInstance(int(pointer), QtWidgets.QWidget) if pointer else None
        self.radial_popup = PiePopup(parent, mouse_button('menu') if button is None else button)
        self.radial_popup.holding = holding
        self.radial_popup.open_at(point)

    def post_native_menu(self, menu, *unused):
        """Use Maya's authoritative binding when its viewport bypasses Qt input.

        Never populate fixed compass positions here. Clear the native trigger
        menu and unwind its posting callback before opening the adaptive popup.
        """
        if self.stopping or self.stopped or menu not in self.menus.values():
            return
        from uv_straight_keymaps import enabled, mouse_button
        from uv_straight_native import _no_undo
        with _no_undo():
            cmds.popupMenu(menu, edit=True, deleteAllItems=True)
        if not enabled('menu'):
            return
        app = QtWidgets.QApplication.instance()
        if app.activeModalWidget() or text_input_widget(app.widgetAt(QtGui.QCursor.pos())):
            return
        self.popup_request += 1
        request = self.popup_request
        point = QtGui.QCursor.pos()
        button = mouse_button('menu')
        def show_when_posted():
            if (self.stopping or self.stopped or request != self.popup_request or
                    menu not in self.menus.values() or not enabled('menu')):
                return
            app = QtWidgets.QApplication.instance()
            if app.activeModalWidget():
                return
            native_popup = app.activePopupWidget()
            if native_popup is not None:
                own = self.radial_popup
                if own is not None and isValid(own) and (native_popup == own or own.isAncestorOf(native_popup)):
                    return
                native_popup.close()
            if app.mouseButtons() & button:
                self.open_radial(point, button=button, holding=True)
        QtCore.QTimer.singleShot(0, show_when_posted)

    def panel_allowed(self, watched):
        """Honor the same panel bindings and conflicts as the native fallback."""
        app = QtWidgets.QApplication.instance()
        if app.activeModalWidget() or app.activePopupWidget() or not isinstance(watched, QtWidgets.QWidget):
            return False
        if text_input_widget(watched):
            return False
        import maya.OpenMayaUI as omui
        for control in self.menus:
            pointer = omui.MQtUtil.findControl(control)
            if pointer:
                root = wrapInstance(int(pointer), QtWidgets.QWidget)
                if watched == root or root.isAncestorOf(watched):
                    return True
        return False

    def refresh(self):
        if self.stopping or self.stopped or menu_interaction_active():
            return
        from uv_straight_native import _find_editors, _no_undo
        from uv_straight_keymaps import menu_flags, enabled
        flags = menu_flags()
        controls = set()
        if enabled('menu'):
            for panel in cmds.getPanel(type='modelPanel') or []:
                control = cmds.modelPanel(panel, query=True, control=True)
                if control:
                    controls.add(control)
            for editor in _find_editors():
                control = cmds.textureWindow(editor, query=True, control=True)
                if control:
                    controls.add(control)
        self.refresh_tools_menus()
        with _no_undo():
            for control, menu in list(self.menus.items()):
                if control not in controls or not cmds.popupMenu(menu, exists=True):
                    if cmds.popupMenu(menu, exists=True):
                        cmds.deleteUI(menu)
                    self.menus.pop(control)
            for control in controls - self.menus.keys():
                try:
                    attached = cmds.control(control, query=True, popupMenuArray=True) or []
                    conflict = any(all(cmds.popupMenu(m, query=True, **{flag: True}) == value
                                       for flag, value in flags.items()) for m in attached)
                    if conflict:
                        if control not in self.conflicts:
                            cmds.warning('ShellShifter: menu binding already assigned on ' + control)
                            self.conflicts.add(control)
                        continue
                    # This native menu only owns Maya's scoped mouse binding.
                    # Its posting hook displays the same adaptive ring as Qt input.
                    menu = cmds.popupMenu(parent=control, markingMenu=False, **flags)
                    cmds.popupMenu(menu, edit=True, postMenuCommand=partial(self.post_native_menu, menu))
                    self.menus[control] = menu
                except RuntimeError as error:
                    if control not in self.conflicts:
                        cmds.warning('ShellShifter menu: ' + str(error))
                        self.conflicts.add(control)

    def refresh_tools_menus(self):
        # Maya can rebuild the menu under the same name. Check the live
        # callback rather than assuming that a name still owns our hook.
        active = tools_menus()
        for menu in active | self.tools_hooks.keys():
            if not cmds.menu(menu, exists=True):
                self.tools_hooks.pop(menu, None)
                continue
            if menu not in active:
                continue
            original = cmds.menu(menu, query=True, postMenuCommand=True)
            registered = self.tools_hooks.get(menu)
            if not isinstance(registered, tuple) or original is not registered[1]:
                def populate_tools(*unused, menu=menu, original=original):
                    try:
                        if callable(original):
                            original(*unused)
                        elif original:
                            import maya.mel as mel
                            mel.eval(original)
                    finally:
                        self.add_tools_item(menu)
                cmds.menu(menu, edit=True, postMenuCommand=populate_tools)
                self.tools_hooks[menu] = (original, populate_tools)
            self.add_tools_item(menu)

    @staticmethod
    def add_tools_item(menu):
        item = tools_item(menu)
        if cmds.menu(menu, exists=True) and not cmds.menuItem(item, exists=True):
            cmds.menuItem(item.rsplit('|', 1)[-1], parent=menu, label='ShellShifter',
                          insertAfter='', command=partial(execute, 'window'))

    def stop(self):
        if self.stopping or self.stopped:
            return
        self.stopping = True
        # A module reload can leave an older QObject alive. Its timer still
        # points at the old cleanup implementation; replace that owned binding.
        self.cleanup_timer.timeout.disconnect()
        self.cleanup_timer.timeout.connect(partial(MenuManager._finish_stop,self))
        self.popup_request += 1
        if self.radial_popup is not None and isValid(self.radial_popup):
            self.radial_popup.close()
            self.radial_popup.deleteLater()
        self.radial_popup = None
        import uv_straight_keymaps as keymaps
        keymaps.unregister(self)
        self.timer.stop()
        QtWidgets.QApplication.instance().removeEventFilter(self)
        try:
            QtWidgets.QApplication.instance().aboutToQuit.disconnect(self.stop)
        except (RuntimeError, TypeError):
            pass
        MenuManager._finish_stop(self)

    def _finish_stop(self):
        if self.stopped:
            return
        if menu_interaction_active():
            if not self.cleanup_timer.isActive():
                self.cleanup_timer.start()
            return
        self.cleanup_timer.stop()
        from uv_straight_native import _no_undo
        with _no_undo():
            for menu in self.menus.values():
                if cmds.popupMenu(menu, exists=True):
                    cmds.deleteUI(menu)
        self.menus.clear()
        for menu, registered in self.tools_hooks.items():
            if cmds.menu(menu, exists=True):
                # During a live reload the old manager may still have the
                # pre-hook-pair representation.
                if not isinstance(registered, tuple):
                    restore_tools_callback(menu, registered)
                else:
                    original, hook = registered
                    if cmds.menu(menu, query=True, postMenuCommand=True) is hook:
                        restore_tools_callback(menu, original)
            item = tools_item(menu) if isinstance(registered, tuple) else menu + '_uvStraightOpen'
            if cmds.menuItem(item, exists=True):
                cmds.deleteUI(item)
        self.tools_hooks.clear()
        self.stopped = True


def install():
    global _manager
    import uv_straight_custom_keymaps as custom
    custom.ensure_manager()
    if cmds.about(batch=True):
        return
    if _manager is not None and _manager.stopping:
        # Hold the retiring manager alive until its menus can be removed.
        MenuManager._finish_stop(_manager)
        if not _manager.stopped:
            after_menu_interaction(install)
            return
        _manager = None
    if _manager is None:
        _manager = MenuManager()
    _manager.refresh()
    _manager.timer.start()


def uninstall():
    global _manager
    if _manager is not None:
        # Use the current cleanup even when the live object predates this module.
        MenuManager.stop(_manager)
        if _manager.stopped:
            _manager = None

