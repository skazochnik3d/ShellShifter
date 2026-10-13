"""Scoped Shift-wheel gesture, with bounded update rate and no idle polling."""
import uv_straight_keymaps as keymaps
import math
import maya.cmds as cmds
import maya.OpenMayaUI as omui
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import wrapInstance


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

    def in_uv_editor(self, widget):
        if not isinstance(widget, QtWidgets.QWidget):
            return False
        from uv_straight_native import _find_editors
        for editor in _find_editors():
            control = cmds.textureWindow(editor, query=True, control=True)
            pointer = omui.MQtUtil.findControl(control) if control else None
            if pointer:
                root = wrapInstance(int(pointer), QtWidgets.QWidget)
                if widget == root or root.isAncestorOf(widget):
                    return True
        return False

    def eventFilter(self, watched, event):
        if (self.stopped or event.type() != QtCore.QEvent.Wheel or not keymaps.enabled('wheel') or
                event.modifiers() != keymaps.modifiers('wheel')):
            return False
        try:
            if not self.in_uv_editor(watched):
                return False
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
            self.feedback.setText(str(error))
            return False

    def apply(self):
        if self.stopped or not keymaps.enabled('wheel'):
            return
        # Never apply a queued wheel operation to a newly selected shell.
        if selection_key() != self.selection:
            return
        try:
            import uv_straight as uv
            if self.angle.value() == 0:
                uv.unpin(whole_shell=True)
                self.feedback.setText('Auto H/V: 0° · Pins removed from selected shells')
            else:
                count = uv.auto_mark(self.angle.value(), whole_shells=True)
                self.feedback.setText('Auto H/V: {:.0f}° · {} groups'.format(self.angle.value(), count))
            import uv_straight_native as native
            if native._manager is not None:
                native._manager.changes.invalidate()
                native._manager.refresh()
        except (RuntimeError, ValueError) as error:
            self.feedback.setText(str(error))

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
