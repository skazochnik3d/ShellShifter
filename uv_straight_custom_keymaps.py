"""Persistent user shortcuts, synchronized editors and one scoped event filter."""
import json
import time
import weakref
import maya.cmds as cmds
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid, wrapInstance
from uv_straight_actions import CATALOG, BY_ID

OPTION = 'shellShifterCustomKeymaps'
_clients = globals().get('_clients', weakref.WeakSet())
if globals().get('_manager') is not None:
    _manager.stop()
_manager = None


def normalize(value):
    if not value or not value[1]:
        return None
    mods, key = value
    names = set(mods.split('+')) - {'None'}
    if names - {'Ctrl', 'Alt', 'Shift'}:
        raise ValueError('Use Ctrl, Alt or Shift for shortcut modifiers.')
    mods = '+'.join(name for name in ('Ctrl', 'Alt', 'Shift') if name in names) or 'None'
    if key not in ('LMB', 'MMB', 'RMB', 'Wheel'):
        sequence = QtGui.QKeySequence.fromString(key, QtGui.QKeySequence.PortableText)
        if sequence.isEmpty() or sequence.count() != 1 or sequence[0].key() == QtCore.Qt.Key_unknown:
            raise ValueError('Record a single key, mouse button or wheel shortcut.')
        key = sequence.toString(QtGui.QKeySequence.PortableText)
    return mods, key


def load_records():
    result = []
    try:
        raw = json.loads(cmds.optionVar(query=OPTION)) if cmds.optionVar(exists=OPTION) else []
        seen = set()
        for record in raw:
            action = record['action']
            if action not in BY_ID or action in seen:
                continue
            seen.add(action)
            value = normalize(record.get('binding'))
            result.append(dict(action=action, binding=value, enabled=bool(record.get('enabled', True))))
    except (ValueError, TypeError, KeyError, IndexError):
        pass
    return result


_records = load_records()
_draft = {record['action']: record['binding'] for record in _records}


def records():
    return [dict(record) for record in _records]


def notify():
    for client in tuple(_clients):
        if isValid(client):
            client.refresh()
        else:
            _clients.discard(client)


def save():
    cmds.optionVar(stringValue=(OPTION, json.dumps(_records)))
    notify()


def add(action):
    if action not in BY_ID:
        raise ValueError('Choose an available ShellShifter function.')
    if any(record['action'] == action for record in _records):
        return
    _records.append(dict(action=action, binding=None, enabled=True))
    _draft[action] = None
    save()


def remove(action):
    _records[:] = [record for record in _records if record['action'] != action]
    _draft.pop(action, None)
    save()


def set_enabled(action, value):
    for record in _records:
        if record['action'] == action:
            record['enabled'] = bool(value)
    save()


def set_draft(action, value):
    _draft[action] = normalize(value)
    notify()


def apply(primary):
    """Validate all bindings before writing anything; leave drafts on failure."""
    import uv_straight_keymaps as main
    taken = {normalize(value): name for name, value in dict(primary, pick=main.binding('pick')).items()}
    for record in _records:
        action = record['action']
        value = _draft.get(action)
        if value is None:
            continue
        if value[1] in ('LMB', 'MMB', 'RMB', 'Wheel') and value[0] == 'None':
            raise ValueError('Custom mouse and wheel shortcuts require Ctrl, Alt or Shift.')
        if value in taken:
            other = BY_ID[taken[value]][1] if taken[value] in BY_ID else {
                'menu': 'Marking menu', 'wheel': 'Auto H/V angle', 'popup': 'Popup window',
                'pick': 'Segment picking'}[taken[value]]
            raise ValueError('{} conflicts with {}. Record a different shortcut.'.format(BY_ID[action][1], other))
        taken[value] = action
    for record in _records:
        record['binding'] = _draft.get(record['action'])
    save()


class CustomBindingsUI(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.rows = {}
        self.function_picker = None
        self.window.installEventFilter(self)
        self.layout = QtWidgets.QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(6)
        from uv_straight_pie_preview import PieFooterButton
        self.plus = PieFooterButton(self)
        self.plus.setAccessibleName('Add Hotkey')
        self.plus.setObjectName('addFunctionShortcut')
        self.plus.setToolTip('Add a shortcut for a ShellShifter function')
        self.plus.clicked.connect(self.choose_function)
        self.layout.addWidget(self.plus,0,QtCore.Qt.AlignHCenter)
        self.plus.show()
        _clients.add(self)
        self.refresh()
        ensure_manager()

    def refresh(self):
        from uv_straight_preview import VisibleCheckBox
        from uv_straight_keymaps import BindingField
        wanted = [record['action'] for record in _records]
        for action in list(self.rows):
            if action not in wanted:
                widget = self.rows.pop(action)
                self.layout.removeWidget(widget)
                widget.hide()
                widget.deleteLater()
        for index, record in enumerate(_records):
            action = record['action']
            if action not in self.rows:
                widget = QtWidgets.QWidget(self)
                row = QtWidgets.QHBoxLayout(widget)
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(6)
                title = BY_ID[action][1]
                widget.checkbox = VisibleCheckBox()
                widget.checkbox.setText(QtGui.QFontMetrics(widget.checkbox.font()).elidedText(
                    title, QtCore.Qt.ElideRight, 110))
                widget.checkbox.setToolTip(BY_ID[action][0]+' · '+BY_ID[action][1])
                widget.checkbox.setStyleSheet('QCheckBox:unchecked { color: #888888; }')
                widget.checkbox.toggled.connect(lambda value, action=action: set_enabled(action, value))
                widget.field = BindingField('custom')
                widget.field.setMinimumWidth(70)
                widget.field.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
                # Capture edits into a shared draft. Apply Keymaps activates them.
                widget.field.recorded.connect(lambda value, action=action: set_draft(action, value))
                widget.field.setToolTip('Click to record a shortcut, then Apply Keymaps. Custom mouse/wheel shortcuts require a modifier.')
                delete = QtWidgets.QToolButton()
                delete.setText('×')
                delete.setObjectName('removeFunctionShortcut_'+action)
                delete.setFixedSize(24, 24)
                delete.setStyleSheet('QToolButton { color: #bcbcbc; font-size: 16px; } QToolButton:hover { color: #ff8e83; }')
                delete.setToolTip('Remove '+BY_ID[action][1]+' shortcut')
                delete.clicked.connect(lambda checked=False, action=action: remove(action))
                row.addWidget(widget.checkbox)
                row.addWidget(widget.field, 1)
                row.addWidget(delete)
                self.rows[action] = widget
                self.layout.insertWidget(index, widget)
            widget = self.rows[action]
            with QtCore.QSignalBlocker(widget.checkbox):
                widget.checkbox.setChecked(record['enabled'])
            widget.field.setEnabled(record['enabled'])
            widget.field.set_value(_draft.get(action) or ('None', ''))
        self.updateGeometry()
        if hasattr(self.window, 'adjust_popup_size'):
            self.window.adjust_popup_size()

    def choose_function(self):
        from uv_straight_pie_catalog import FunctionPicker
        self.close_functions()
        editor = self.window.pie_preview
        editor.close_functions()
        picker = FunctionPicker(editor, add, draggable=False, title='Shortcut Functions',
                                catalog=CATALOG, parent=self)
        self.function_picker = picker
        added = {record['action'] for record in _records}
        for button in picker.buttons:
            if button.action in added:
                button.setEnabled(False)
                button.setToolTip('Already added to Keymaps')
        anchor = QtCore.QRect(self.window.mapToGlobal(QtCore.QPoint()), self.window.size())
        picker.open_at(anchor)
        return picker

    def close_functions(self):
        if self.function_picker is not None and isValid(self.function_picker):
            self.function_picker.close()

    def hideEvent(self, event):
        self.close_functions()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() in (QtCore.QEvent.Hide, QtCore.QEvent.HideToParent, QtCore.QEvent.Close):
            self.close_functions()
        return super().eventFilter(watched, event)


class CustomShortcutManager(QtCore.QObject):
    def __init__(self):
        super().__init__(QtWidgets.QApplication.instance())
        self.stopped = False
        self.mouse_pending = {}
        self.last_wheel = 0.
        self.generation = 0
        self.app = QtWidgets.QApplication.instance()
        self.app.installEventFilter(self)
        self.app.aboutToQuit.connect(self.stop)

    def context_allowed(self, watched):
        popup = self.app.activePopupWidget()
        if self.app.activeModalWidget() or (popup and not hasattr(popup, 'shortcut_actions')):
            return False
        current = watched if isinstance(watched, QtWidgets.QWidget) else self.app.focusWidget()
        widget = current
        while widget:
            if isinstance(widget, (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit,
                                   QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox)):
                return False
            if hasattr(widget, 'shortcut_actions'):
                return True
            widget = widget.parentWidget()
        if current is None:
            return False
        import maya.OpenMayaUI as omui
        from uv_straight_native import _find_editors
        controls = [cmds.modelPanel(panel, query=True, control=True)
                    for panel in cmds.getPanel(type='modelPanel') or []]
        controls += [cmds.textureWindow(editor, query=True, control=True) for editor in _find_editors()]
        for control in controls:
            pointer = omui.MQtUtil.findControl(control) if control else None
            if pointer:
                root = wrapInstance(int(pointer), QtWidgets.QWidget)
                if current == root or root.isAncestorOf(current):
                    return True
        return False

    def queue(self, record):
        action, binding, generation = record['action'], record['binding'], self.generation
        def execute_if_current():
            if self.stopped or generation != self.generation:
                return
            if any(entry['action'] == action and entry['enabled'] and entry['binding'] == binding for entry in _records):
                from uv_straight_actions import execute
                execute(action)
        QtCore.QTimer.singleShot(0, execute_if_current)

    def eventFilter(self, watched, event):
        kind = event.type()
        if self.stopped or kind not in (QtCore.QEvent.ShortcutOverride, QtCore.QEvent.KeyPress,
                                      QtCore.QEvent.KeyRelease, QtCore.QEvent.MouseButtonPress,
                                      QtCore.QEvent.MouseButtonRelease, QtCore.QEvent.Wheel):
            return False
        if not _records and not self.mouse_pending:
            return False
        if kind == QtCore.QEvent.MouseButtonRelease and event.button() in self.mouse_pending:
            record = self.mouse_pending.pop(event.button())
            self.queue(record)
            event.accept()
            return True
        from uv_straight_keymaps import BindingCapture
        mods = BindingCapture.modifier_text(event.modifiers())
        if kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease, QtCore.QEvent.ShortcutOverride):
            key = QtGui.QKeySequence(event.key()).toString(QtGui.QKeySequence.PortableText)
        elif kind in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonRelease):
            key = {QtCore.Qt.LeftButton: 'LMB', QtCore.Qt.MiddleButton: 'MMB', QtCore.Qt.RightButton: 'RMB'}.get(event.button())
        else:
            if not (event.angleDelta().x() or event.angleDelta().y() or event.pixelDelta().y()):
                return False
            key = 'Wheel'
        record = next((entry for entry in _records if entry['enabled'] and entry['binding'] == (mods, key)), None)
        if record is None:
            return False
        try:
            if not self.context_allowed(watched):
                return False
        except RuntimeError:
            return False  # The editor may have been closed during input delivery.
        event.accept()
        if kind == QtCore.QEvent.KeyPress and not event.isAutoRepeat():
            self.queue(record)
        elif kind == QtCore.QEvent.MouseButtonPress:
            self.mouse_pending[event.button()] = dict(record)
        elif kind == QtCore.QEvent.Wheel:
            now = time.perf_counter()
            if now - self.last_wheel >= .1:
                self.last_wheel = now
                self.queue(record)
        return True

    def stop(self):
        self.stopped = True
        self.generation += 1
        self.mouse_pending.clear()
        self.app.removeEventFilter(self)
        try:
            self.app.aboutToQuit.disconnect(self.stop)
        except (RuntimeError, TypeError):
            pass


def ensure_manager():
    global _manager
    if QtWidgets.QApplication.instance() is None:
        return None
    if _manager is None or _manager.stopped:
        _manager = CustomShortcutManager()
    return _manager
