"""Scoped Shift-wheel gesture, with bounded update rate and no idle polling."""
import uv_straight_keymaps as keymaps
import math
import maya.cmds as cmds
import maya.OpenMayaUI as omui
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import wrapInstance, isValid
_service = globals().get('_service')


def selection_key():
    # Maya may reorder/compress a multi-shell selection between wheel events.
    # Compare the actual UV membership, not the textual component ranges.
    selected = cmds.ls(selection=True, long=True) or []
    if not selected:
        return ()
    components = cmds.polyListComponentConversion(selected, toUV=True) or []
    return tuple(sorted(set(cmds.ls(components, long=True, flatten=True) or [])))


class AngleHud(QtWidgets.QWidget):
    """Brief, input-transparent angle readout; all timers stop when hidden."""
    def __init__(self, parent):
        flags = (QtCore.Qt.ToolTip | QtCore.Qt.FramelessWindowHint |
                 QtCore.Qt.NoDropShadowWindowHint | QtCore.Qt.WindowTransparentForInput |
                 QtCore.Qt.WindowDoesNotAcceptFocus)
        super().__init__(parent, flags)
        self.setObjectName('uvStraightAngleHud')
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
        self.setAttribute(QtCore.Qt.WA_QuitOnClose, False)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        font = self.font()
        font.setPointSizeF(11.)
        font.setBold(True)
        self.setFont(font)
        self.text = ''
        self._opacity = 1.
        self.hold = QtCore.QTimer(self)
        self.hold.setSingleShot(True)
        self.hold.setInterval(550)
        self.fade = QtCore.QPropertyAnimation(self, b'opacity', self)
        self.fade.setDuration(450)
        self.fade.setStartValue(1.)
        self.fade.setEndValue(0.)
        self.fade.setEasingCurve(QtCore.QEasingCurve.InOutQuad)
        self.hold.timeout.connect(self.fade.start)
        self.fade.finished.connect(self.hide)
        self.hide()

    def _set_opacity(self, value):
        self._opacity = float(value)
        self.update()

    opacity = QtCore.Property(float, lambda self: self._opacity, _set_opacity)

    @staticmethod
    def position(point, size, bounds):
        # Qt screen geometry and wheel positions are both in logical pixels.
        x = point.x() - size.width() // 2
        y = point.y() - size.height() - 18
        if y < bounds.top() + 4:
            y = point.y() + 18
        x = max(bounds.left() + 4, min(x, bounds.right() - size.width() - 3))
        y = max(bounds.top() + 4, min(y, bounds.bottom() - size.height() - 3))
        return QtCore.QPoint(x, y)

    def display(self, angle, point):
        self.hold.stop()
        self.fade.stop()
        self.opacity = 1.
        self.text = '{:g}°'.format(angle)
        metrics = QtGui.QFontMetrics(self.font())
        self.setFixedSize(max(54, metrics.horizontalAdvance(self.text) + 24),
                          max(30, metrics.height() + 12))
        screen = QtGui.QGuiApplication.screenAt(point) or QtGui.QGuiApplication.primaryScreen()
        if screen is not None:
            self.move(self.position(point, self.size(), screen.availableGeometry()))
        else:
            self.move(point - QtCore.QPoint(self.width() // 2, self.height() + 18))
        self.show()
        self.raise_()
        self.update()
        self.hold.start()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(), QtCore.Qt.transparent)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setOpacity(self.opacity)
        painter.setPen(QtGui.QPen(QtGui.QColor('#686868'), 1.))
        painter.setBrush(QtGui.QColor('#333333'))
        painter.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 9., 9.)
        painter.setPen(QtGui.QColor('#eeeeee'))
        painter.drawText(self.rect(), QtCore.Qt.AlignCenter, self.text)
        painter.end()

    def stop(self):
        self.hold.stop()
        self.fade.stop()
        self.hide()


class AngleWheel(QtCore.QObject):
    feedback_changed = QtCore.Signal(str)
    def __init__(self, window, angle, feedback):
        super().__init__(window)
        self.angle = angle
        self.feedback = feedback
        self.hud = AngleHud(window)
        self.selection = None
        self.remainder = 0.
        self.stopped = False
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.apply)
        app = QtWidgets.QApplication.instance()
        app.installEventFilter(self)
        app.aboutToQuit.connect(self.stop)
        keymaps.register(self)

    def keymap_state_changed(self,action,value):
        if action == 'wheel' and not value:
            self.timer.stop()
            self.hud.stop()
            self.remainder = 0.
            self.selection = None

    def in_uv_editor(self, widget, point=None):
        # Qt 6.8 also sends wheel input to the native render QWindow. Resolve
        # its position back to the editor rather than dropping those events.
        native_window = isinstance(widget, QtGui.QWindow)
        if native_window and point is not None:
            widget = QtWidgets.QApplication.widgetAt(point)
        if not isinstance(widget, QtWidgets.QWidget) and not native_window:
            return False
        from uv_straight_native import _find_editors
        for editor in _find_editors():
            control = cmds.textureWindow(editor, query=True, control=True)
            pointer = omui.MQtUtil.findControl(control) if control else None
            if pointer:
                root = wrapInstance(int(pointer), QtWidgets.QWidget)
                if isinstance(widget, QtWidgets.QWidget) and (widget == root or root.isAncestorOf(widget)):
                    return True
                if (native_window and widget is None and point is not None and root.isVisible()
                        and root.rect().contains(root.mapFromGlobal(point))):
                    return True
        return False

    def eventFilter(self, watched, event):
        logical_modifiers = (QtCore.Qt.ShiftModifier | QtCore.Qt.ControlModifier |
                             QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier)
        if (self.stopped or event.type() != QtCore.QEvent.Wheel or not keymaps.enabled('wheel') or
                event.modifiers() & logical_modifiers != keymaps.modifiers('wheel') or
                QtWidgets.QApplication.activeModalWidget() is not None):
            return False
        try:
            inside = (self.in_uv_editor(watched, event.globalPosition().toPoint())
                      if isinstance(watched, QtGui.QWindow) else self.in_uv_editor(watched))
            if not inside:
                return False
            from uv_straight_native import uv_edit_busy
            if uv_edit_busy():
                event.accept()
                return True
            selected = selection_key()
            if not selected:
                return False
            if selected != self.selection:
                self.remainder = 0.
                self.selection = selected
            delta = event.angleDelta().y() or event.angleDelta().x()
            if not delta:
                delta = event.pixelDelta().y() * 3
            self.remainder += delta / 120.
            steps = math.trunc(self.remainder)
            self.remainder -= steps
            previous = self.angle.value()
            self.angle.setValue(previous + steps)
            if self.angle.value() != previous:
                self.hud.display(self.angle.value(), event.globalPosition().toPoint())
            if (self.angle.value() != previous or (steps < 0 and self.angle.value() == 0)) and not self.timer.isActive():
                self.timer.start()
            event.accept()
            return True
        except (RuntimeError, ValueError) as error:
            self.notify(str(error))
            return False

    def apply(self):
        if self.stopped or not keymaps.enabled('wheel'):
            return
        from uv_straight_native import uv_edit_busy
        if uv_edit_busy():
            return
        # Never apply a queued wheel operation to a newly selected shell.
        if selection_key() != self.selection:
            return
        try:
            import uv_straight as uv
            if self.angle.value() == 0:
                uv.unpin(whole_shell=True)
                self.notify('Auto H/V: 0° · Pins removed from selected shells')
            else:
                count = uv.auto_mark(self.angle.value(), whole_shells=True)
                self.notify('Auto H/V: {:.0f}° · {} groups'.format(self.angle.value(), count))
            import uv_straight_native as native
            if native._manager is not None:
                native._manager.changes.invalidate()
                native._manager.refresh()
        except (RuntimeError, ValueError) as error:
            self.notify(str(error))

    def notify(self, text):
        self.feedback.setText(text)
        self.feedback_changed.emit(text)

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        keymaps.unregister(self)
        self.timer.stop()
        self.hud.stop()
        app = QtWidgets.QApplication.instance()
        app.removeEventFilter(self)
        try:
            app.aboutToQuit.disconnect(self.stop)
        except (RuntimeError, TypeError):
            pass


class PaletteWheelBinding:
    """A palette follows the shared angle; closing it keeps the gesture alive."""
    def __init__(self, window, angle, feedback, service):
        self.service, self.angle, self.feedback = service, angle, feedback
        self.stopped = False
        self.changed = lambda value: service.angle.setValue(value)
        self.updated = lambda value: angle.setValue(value) if isValid(angle) else None
        self.message = lambda text: feedback.setText(text) if isValid(feedback) else None
        angle.valueChanged.connect(self.changed)
        service.angle.valueChanged.connect(self.updated)
        service.feedback_changed.connect(self.message)
        window.destroyed.connect(self.stop)

    def __getattr__(self, name):
        return getattr(self.service, name)

    def stop(self, *unused):
        if self.stopped:
            return
        self.stopped = True
        connections = []
        if isValid(self.angle):
            connections.append((self.angle.valueChanged, self.changed))
        if isValid(self.service):
            connections.extend(((self.service.angle.valueChanged, self.updated),
                                (self.service.feedback_changed, self.message)))
        for signal, slot in connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass


def ensure_service():
    global _service
    if _service is not None and isValid(_service) and not _service.stopped:
        return _service
    pointer = omui.MQtUtil.mainWindow()
    parent = wrapInstance(int(pointer), QtWidgets.QWidget) if pointer else None
    host = QtWidgets.QWidget(parent, QtCore.Qt.Tool | QtCore.Qt.WindowDoesNotAcceptFocus)
    host.setObjectName('uvStraightWheelHost')
    host.setAttribute(QtCore.Qt.WA_QuitOnClose, False)
    angle = QtWidgets.QDoubleSpinBox(host)
    angle.setRange(0., 45.)
    angle.setValue(float(cmds.optionVar(query='uvStraightAutoAngle'))
                   if cmds.optionVar(exists='uvStraightAutoAngle') else 15.)
    angle.valueChanged.connect(lambda value: cmds.optionVar(floatValue=('uvStraightAutoAngle', value)))
    feedback = QtWidgets.QLabel(host)
    host.hide()
    _service = AngleWheel(host, angle, feedback)
    _service.host = host  # Retain the hidden QWidget when Maya has no main window.
    return _service


def bind_palette(window, angle, feedback):
    service = ensure_service()
    service.angle.setValue(angle.value())
    return PaletteWheelBinding(window, angle, feedback, service)


def shutdown_service():
    global _service
    if _service is not None and isValid(_service):
        _service.stop()
        _service.parent().deleteLater()
    _service = None
