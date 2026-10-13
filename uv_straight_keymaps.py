"""Persistent mouse bindings shared by UI and event handlers."""
import maya.cmds as cmds
import weakref
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid
DEFAULTS = {'menu': ('Alt+Shift', 'RMB'), 'wheel': ('Shift', 'Wheel'),
            'popup': ('Shift', 'Space'), 'pick': ('None', 'LMB')}
MODIFIERS = ['None', 'Shift', 'Ctrl', 'Alt', 'Ctrl+Shift', 'Alt+Shift', 'Ctrl+Alt', 'Ctrl+Alt+Shift']
_listeners = globals().get('_listeners',weakref.WeakSet())


def enabled(action):
    name = 'uvStraightKeymap_' + action + 'Enabled'
    return bool(cmds.optionVar(query=name)) if cmds.optionVar(exists=name) else True


def register(listener):
    _listeners.add(listener)


def unregister(listener):
    _listeners.discard(listener)


def set_enabled(action,value):
    previous = enabled(action)
    value = bool(value)
    cmds.optionVar(intValue=('uvStraightKeymap_' + action + 'Enabled',int(value)))
    if previous != value:
        for listener in list(_listeners):
            if isValid(listener):
                listener.keymap_state_changed(action,value)
            else:
                _listeners.discard(listener)

def binding(action):
    default = DEFAULTS[action]
    result = []
    for suffix, fallback in zip(('Mods', 'Input'), default):
        name = 'uvStraightKeymap_' + action + suffix
        result.append(cmds.optionVar(query=name) if cmds.optionVar(exists=name) else fallback)
    return tuple(result)

def modifiers(action):
    value = QtCore.Qt.NoModifier
    for name in binding(action)[0].split('+'):
        value |= {'Ctrl': QtCore.Qt.ControlModifier, 'Alt': QtCore.Qt.AltModifier,
                  'Shift': QtCore.Qt.ShiftModifier}.get(name, QtCore.Qt.NoModifier)
    return value

def mouse_button(action):
    return {'LMB': QtCore.Qt.LeftButton, 'MMB': QtCore.Qt.MiddleButton,
            'RMB': QtCore.Qt.RightButton}[binding(action)[1]]

def keyboard_key(action):
    sequence = QtGui.QKeySequence.fromString(
        binding(action)[1], QtGui.QKeySequence.PortableText)
    if sequence.isEmpty():
        return QtCore.Qt.Key_unknown
    return sequence[0].key()

def text(action):
    mods, key = binding(action)
    return (mods + ' + ' if mods != 'None' else '') + key

def menu_flags():
    mods, key = binding('menu')
    return dict(button={'LMB': 1, 'MMB': 2, 'RMB': 3}[key],
                altModifier='Alt' in mods, shiftModifier='Shift' in mods,
                ctrlModifier='Ctrl' in mods)

class BindingCapture(QtWidgets.QDialog):
    """Modal recorder: consume the full mouse click before closing."""
    def __init__(self, action, parent=None):
        super().__init__(parent)
        self.action = action
        self.value = None
        self.pending = None
        self.setWindowTitle('Assign shortcut')
        self.setMinimumWidth(300)
        self.setContextMenuPolicy(QtCore.Qt.NoContextMenu)
        layout = QtWidgets.QVBoxLayout(self)
        self.prompt = QtWidgets.QLabel(
            'Hold Ctrl / Alt / Shift, then ' +
            ('press a key, click a mouse button or scroll the wheel.' if action == 'custom' else
             'press a keyboard key.' if action == 'popup' else
             'scroll the wheel.' if action == 'wheel' else 'press a mouse button.') +
            '\nEsc — cancel')
        self.prompt.setWordWrap(True)
        self.prompt.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        layout.addWidget(self.prompt)

    @staticmethod
    def modifier_text(mods):
        return '+'.join(name for name, flag in (
            ('Ctrl', QtCore.Qt.ControlModifier), ('Alt', QtCore.Qt.AltModifier),
            ('Shift', QtCore.Qt.ShiftModifier)) if mods & flag) or 'None'

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Escape:
            self.reject()
        elif (self.action in ('popup', 'custom') and event.key() not in
              (QtCore.Qt.Key_Control, QtCore.Qt.Key_Alt, QtCore.Qt.Key_Shift,
               QtCore.Qt.Key_Meta)):
            key = QtGui.QKeySequence(event.key()).toString(
                QtGui.QKeySequence.PortableText)
            if key:
                self.value = (self.modifier_text(event.modifiers()), key)
                self.accept()
        elif event.key() not in (QtCore.Qt.Key_Control, QtCore.Qt.Key_Alt, QtCore.Qt.Key_Shift):
            self.prompt.setText('This action uses ' +
                                ('a keyboard key' if self.action == 'popup' else
                                 'the wheel' if self.action == 'wheel' else 'a mouse button') +
                                ' with optional Ctrl / Alt / Shift.\nEsc — cancel')
        event.accept()

    def keyReleaseEvent(self, event):
        event.accept()

    def mousePressEvent(self, event):
        buttons = {QtCore.Qt.LeftButton: 'LMB', QtCore.Qt.MiddleButton: 'MMB', QtCore.Qt.RightButton: 'RMB'}
        if self.action == 'custom' and self.modifier_text(event.modifiers()) == 'None':
            self.prompt.setText('Mouse shortcuts require Ctrl, Alt or Shift.\nEsc — cancel')
        elif self.action != 'wheel' and event.button() in buttons:
            self.pending = (self.modifier_text(event.modifiers()), buttons[event.button()])
        event.accept()

    def mouseReleaseEvent(self, event):
        buttons = {QtCore.Qt.LeftButton: 'LMB', QtCore.Qt.MiddleButton: 'MMB', QtCore.Qt.RightButton: 'RMB'}
        if self.pending and self.pending[1] == buttons.get(event.button()):
            self.value = self.pending
            self.accept()
        event.accept()

    def wheelEvent(self, event):
        if self.action == 'custom' and self.modifier_text(event.modifiers()) == 'None':
            self.prompt.setText('Wheel shortcuts require Ctrl, Alt or Shift.\nEsc — cancel')
        elif self.action in ('wheel', 'custom') and (event.angleDelta().y() or event.pixelDelta().y()):
            self.value = (self.modifier_text(event.modifiers()), 'Wheel')
            self.accept()
        event.accept()


class BindingField(QtWidgets.QPushButton):
    recorded = QtCore.Signal(object)
    def __init__(self, action, parent=None):
        super().__init__(parent)
        self.action = action
        self.setToolTip('Click to record a new mouse shortcut. Esc cancels.')
        self.set_value(binding(action) if action in DEFAULTS else ('None', ''))
        self.clicked.connect(self.record)

    def set_value(self, value):
        self.value = tuple(value)
        mods, key = self.value
        self.setText((mods.replace('+', ' + ') + ' + ' if mods != 'None' else '') + key if key else 'Assign shortcut')

    def record(self):
        owner = self.window()
        popup = owner.windowType() == QtCore.Qt.Popup
        flags, position = owner.windowFlags(), owner.pos()
        if popup:
            # Qt 6.8 dismisses a native popup when its modal child opens,
            # deleting both the field and recorder. Keep ownership stable
            # while capturing, then restore ordinary popup dismissal.
            owner.setWindowFlags((flags & ~QtCore.Qt.WindowType_Mask) | QtCore.Qt.Tool)
            owner.move(position)
            owner.show()
        dialog = BindingCapture(self.action, self)
        try:
            if dialog.exec() == QtWidgets.QDialog.Accepted and isValid(self):
                self.set_value(dialog.value)
                self.recorded.emit(dialog.value)
        finally:
            if isValid(dialog):
                dialog.deleteLater()
            if popup and isValid(owner):
                owner.setWindowFlags(flags)
                owner.move(position)
                owner.show()


class EnabledKeymapRow(QtWidgets.QWidget):
    def __init__(self,action,title,parent):
        super().__init__(parent)
        from uv_straight_preview import VisibleCheckBox
        self.action = action
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0,0,0,0)
        self.checkbox = VisibleCheckBox(title)
        self.checkbox.setObjectName('keymapEnabled_'+action)
        self.checkbox.setAccessibleName('Enable '+title+' shortcut')
        self.checkbox.setToolTip('Enable or disable this shortcut immediately.')
        self.checkbox.setStyleSheet('QCheckBox:unchecked { color: #888888; }')
        self.field = BindingField(action)
        self.field.setStyleSheet('QPushButton:disabled { background: #414141; color: #888888; }')
        row.addWidget(self.checkbox)
        row.addWidget(self.field,1)
        self.keymap_state_changed(action,enabled(action))
        self.checkbox.toggled.connect(lambda value:set_enabled(action,value))
        register(self)

    def keymap_state_changed(self,action,value):
        if action != self.action:
            return
        with QtCore.QSignalBlocker(self.checkbox):
            self.checkbox.setChecked(value)
        self.field.setEnabled(value)


def build_ui(layout, window):
    rows = {}
    toggles = {}
    enabled_rows = {}
    for action, title in [('menu', 'Marking menu'), ('wheel', 'Auto H/V angle'),
                          ('popup', 'Popup window')]:
        row = EnabledKeymapRow(action,title,window)
        layout.addWidget(row)
        rows[action] = row.field
        toggles[action] = row.checkbox
        enabled_rows[action] = row
    window.keymap_fields = rows
    window.keymap_toggles = toggles
    window.keymap_rows = enabled_rows
    import uv_straight_custom_keymaps as custom
    window.custom_keymaps = custom.CustomBindingsUI(window)
    layout.addWidget(window.custom_keymaps)
    info = QtWidgets.QLabel('')
    info.setWordWrap(True)
    layout.addWidget(info)
    info.hide()
    def fill(values):
        for action, field in rows.items():
            field.set_value(values[action])
    fill({action: binding(action) for action in rows})
    def apply():
        values = {action: field.value for action, field in rows.items()}
        if values['menu'] == binding('pick'):
            info.setText('Menu and segment picking cannot use the same binding.')
            info.show()
            return
        try:
            custom.apply(values)
        except ValueError as error:
            info.setText(str(error))
            info.show()
            return
        for action, value in values.items():
            for suffix, part in zip(('Mods', 'Input'), value):
                cmds.optionVar(stringValue=('uvStraightKeymap_' + action + suffix, part))
        for listener in list(_listeners):
            if isValid(listener) and hasattr(listener, 'field') and listener.action in values:
                listener.field.set_value(values[listener.action])
        import uv_straight_menu as menu
        menu.uninstall(); menu.install()
        window.wheel_feedback.setText('UV Editor: ' + text('wheel') + ' = Auto H/V angle ±1°')
        info.hide()
    apply_button = QtWidgets.QPushButton('Apply Keymaps')
    apply_button.clicked.connect(apply)
    layout.addWidget(apply_button)
    reset = QtWidgets.QPushButton('Restore Defaults')
    def restore_defaults():
        fill(DEFAULTS)
        for action in rows:
            set_enabled(action,True)
    reset.clicked.connect(restore_defaults)
    layout.addWidget(reset)
    from uv_straight_pie_preview import InlinePieEditor
    edit_menu = QtWidgets.QPushButton('Edit Pie Menu')
    edit_menu.setObjectName('editPieMenu')
    edit_menu.setCheckable(True)
    edit_menu.setStyleSheet('QPushButton:checked { background: #84502c; }')
    layout.addWidget(edit_menu)
    window.pie_preview = InlinePieEditor(window)
    edit_menu.toggled.connect(window.pie_preview.set_editing)
    layout.addWidget(window.pie_preview)
