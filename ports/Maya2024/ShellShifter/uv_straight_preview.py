"""Tool UI, color preview and optional native UV-editor annotations."""
from pathlib import Path

from PySide2 import QtCore, QtGui, QtWidgets
from uv_straight_qt5 import signals_blocked
import maya.cmds as cmds
import math
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
from uv_straight_changes import ChangeTracker
from uv_straight_actions import tooltip_label
import uv_straight_language as ui_language

COLORS = {'H': '#ff963b', 'V': '#38c9ff'}
CONTROL_SPACING = 6
DOCK_CONTROL = 'uvStraightQtWindowWorkspaceControl'
UV_TOOLKIT_CONTROL = 'UVToolkitDockControl'
POPUP_OBJECT = 'uvStraightQtPopup'
_popup = globals().get('_popup')
_startup_defaults_applied = globals().get('_startup_defaults_applied', False)
SECTION_DEFINITIONS = (
    ('unwrap', 'Unwrap', True), ('packing', 'Packing', False),
    ('materials', 'Materials', False),
    ('display', 'Display', False),
    ('checks', 'UV Checks', False),
    ('overlaps', 'Move Overlaps', False),
    ('settings', 'Keymaps', False), ('updates', 'Updates', False))


class CheckerPatternComboBox(QtWidgets.QComboBox):
    """Keep the selector's dropdown affordance visible in Maya's dark theme."""
    def paintEvent(self, event):
        super().paintEvent(event)
        option = QtWidgets.QStyleOptionComboBox()
        self.initStyleOption(option)
        rect = self.style().subControlRect(
            QtWidgets.QStyle.CC_ComboBox, option,
            QtWidgets.QStyle.SC_ComboBoxArrow, self)
        center = QtCore.QPointF(rect.center())
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        color = '#9edbff' if self.underMouse() else '#cbd2da'
        pen = QtGui.QPen(QtGui.QColor(color if self.isEnabled() else '#888888'), 1.8)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline(QtGui.QPolygonF([
            center + QtCore.QPointF(-4, -2),
            center + QtCore.QPointF(0, 2),
            center + QtCore.QPointF(4, -2)]))


class DisplayDisclosureButton(QtWidgets.QPushButton):
    def __init__(self, text, action):
        super().__init__(text)
        from uv_straight_icons import icon
        self.setCheckable(True)
        self.setIconSize(QtCore.QSize(16, 16))
        self.setIcon(icon(action))
        self.setStyleSheet(
            'QPushButton {background:#4b4b4b;border-radius:7px;padding:4px 23px;}'
            'QPushButton:hover {background:#585858;}'
            'QPushButton:checked {background:#cfe1dc;color:#17292b;}'
            'QPushButton:checked:hover {background:#dcebe7;}')

    def paintEvent(self, event):
        super().paintEvent(event)
        from uv_straight_icons import paint_icon
        painter = QtGui.QPainter(self)
        expanded = self.isChecked()
        paint_icon(painter, QtCore.QRectF(self.width()-22, (self.height()-14)*.5, 14, 14),
                   'material_chevron_up' if expanded else 'material_chevron')
        painter.end()


class DistortionScale(QtWidgets.QWidget):
    """Two mirrored handles edit one intensity; closer to the edges means stronger."""
    valueChanged=QtCore.Signal(float)
    sliderReleased=QtCore.Signal()
    MINIMUM=.1;MAXIMUM=4.;DEFAULT=2.
    ACCESSIBLE_NAME='Distortion intensity'
    LABELS=('Stretch','Neutral','Compress')
    GRADIENT=((0,'#ff1408'),(.5,'#777777'),(1,'#1452ff'))
    HANDLE_COLORS=('#f45749','#477bff')
    def __init__(self,parent=None):
        super().__init__(parent)
        self._value=self.DEFAULT;self._drag_side=None;self._keyboard_side=-1
        self.setFixedHeight(34);self.setMinimumWidth(120)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Fixed)
        self.setFocusPolicy(QtCore.Qt.StrongFocus);self.setMouseTracking(True)
        self.setAccessibleName(self.ACCESSIBLE_NAME)
        self.setValue(self.DEFAULT)
    def value(self):return self._value
    def setValue(self,value):
        value=round(max(self.MINIMUM,min(self.MAXIMUM,float(value))),2)
        changed=value!=self._value;self._value=value
        self.setToolTip(self.tooltip_text(value))
        self.setAccessibleDescription('{} {:g}x; mirrored handles'.format(self.ACCESSIBLE_NAME,value))
        self.update()
        if changed:self.valueChanged.emit(value)
    def tooltip_text(self,value):
        return ('Intensity: {:g}×. Drag either marker; the other mirrors it. Toward the ends is stronger. Arrow keys fine-tune.'.format(value))
    def step_value(self,direction):return self._value+direction*.05
    def bar_rect(self):return QtCore.QRectF(1,1,max(1,self.width()-2),20)
    def handle_positions(self):
        bar=self.bar_rect()
        fraction=math.log(self._value/self.MINIMUM)/math.log(self.MAXIMUM/self.MINIMUM)
        distance=bar.width()*.48*fraction
        return (QtCore.QPointF(bar.center().x()-distance,25.),
                QtCore.QPointF(bar.center().x()+distance,25.))
    def _value_at(self,x,side):
        bar=self.bar_rect()
        fraction=max(0.,min(1.,side*(x-bar.center().x())/(bar.width()*.48)))
        return self.MINIMUM*(self.MAXIMUM/self.MINIMUM)**fraction
    def paintEvent(self,event):
        painter=QtGui.QPainter(self);painter.setRenderHint(QtGui.QPainter.Antialiasing)
        bar=self.bar_rect();gradient=QtGui.QLinearGradient(bar.topLeft(),bar.topRight())
        for position,color in self.GRADIENT:gradient.setColorAt(position,QtGui.QColor(color))
        painter.setPen(QtGui.QColor('#555555'));painter.setBrush(gradient);painter.drawRect(bar)
        font=painter.font();font.setPixelSize(11);painter.setFont(font);painter.setPen(QtGui.QColor('white'))
        for n,text in enumerate(self.LABELS):
            painter.drawText(QtCore.QRectF(bar.left()+n*bar.width()/len(self.LABELS),bar.top(),bar.width()/len(self.LABELS),bar.height()),QtCore.Qt.AlignCenter,text)
        for index,point in enumerate(self.handle_positions()):
            painter.setPen(QtGui.QPen(QtGui.QColor('#f4f4f4'),1.2))
            painter.setBrush(QtGui.QColor(self.HANDLE_COLORS[index]))
            painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(point.x(),18),
                QtCore.QPointF(point.x()-5.5,30),QtCore.QPointF(point.x()+5.5,30)]))
        if self.hasFocus():
            painter.setPen(QtGui.QPen(QtGui.QColor('#b0c8d8'),1,QtCore.Qt.DotLine));painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRect(QtCore.QRectF(self.rect()).adjusted(0,0,-1,-1))
        painter.end()
    def mousePressEvent(self,event):
        if event.button()!=QtCore.Qt.LeftButton:return super().mousePressEvent(event)
        self.setFocus(QtCore.Qt.MouseFocusReason)
        self._drag_side=-1 if event.localPos().x()<self.bar_rect().center().x() else 1
        self._keyboard_side=self._drag_side
        self.setValue(self._value_at(event.localPos().x(),self._drag_side));event.accept()
    def mouseMoveEvent(self,event):
        if self._drag_side is not None:
            self.setValue(self._value_at(event.localPos().x(),self._drag_side));event.accept()
        else:self.setCursor(QtCore.Qt.SizeHorCursor)
    def mouseReleaseEvent(self,event):
        if event.button()==QtCore.Qt.LeftButton and self._drag_side is not None:
            self.setValue(self._value_at(event.localPos().x(),self._drag_side));self._drag_side=None
            self.sliderReleased.emit();event.accept()
        else:super().mouseReleaseEvent(event)
    def keyPressEvent(self,event):
        if event.key() in (QtCore.Qt.Key_Left,QtCore.Qt.Key_Right,QtCore.Qt.Key_Up,QtCore.Qt.Key_Down):
            step=(1 if event.key() in (QtCore.Qt.Key_Right,QtCore.Qt.Key_Up) else -1)
            if event.key() in (QtCore.Qt.Key_Left,QtCore.Qt.Key_Right):step*=self._keyboard_side
            self.setValue(self.step_value(step));event.accept()
        elif event.key() in (QtCore.Qt.Key_Home,QtCore.Qt.Key_End):
            self.setValue(self.MINIMUM if event.key()==QtCore.Qt.Key_Home else self.MAXIMUM);event.accept()
        else:super().keyPressEvent(event)


class TexelDensityScale(DistortionScale):
    """The same mirrored control, with logarithmic density-color sensitivity."""
    MAXIMUM=100.;DEFAULT=1.
    ACCESSIBLE_NAME='Texel density contrast'
    LABELS=('Low density','High density')
    GRADIENT=((0,'#123cff'),(.25,'#00dfff'),(.5,'#35ed42'),(.75,'#ffe100'),(1,'#ff3b0a'))
    HANDLE_COLORS=('#477bff','#f45749')
    _reference_density=None
    def tooltip_text(self,value):
        reference=('Green: {:.4f} px/unit at the start of analysis. '.format(self._reference_density)
                   if self._reference_density is not None else 'Green: reference density. ')
        return ('Contrast: {:g}×. Drag either marker; the other mirrors it. Farther from the center increases color differences. Arrow keys fine-tune. {}Blue/cyan: below reference. Yellow/red: above. Only colors change; density values and UVs are preserved.'.format(value,reference))
    def setReferenceDensity(self,value):
        self._reference_density=value;self.setToolTip(self.tooltip_text(self._value))
    def handle_positions(self):
        bar=self.bar_rect();distance=bar.width()*(.1+.12*(math.log10(self._value)+1.))
        return (QtCore.QPointF(bar.center().x()-distance,25.),QtCore.QPointF(bar.center().x()+distance,25.))
    def _value_at(self,x,side):
        bar=self.bar_rect();distance=max(.1,min(.46,side*(x-bar.center().x())/bar.width()))
        return 10.**((distance-.1)/.12-1.)
    def step_value(self,direction):return self._value*10.**(direction*.025)


class MapSizeSpinBox(QtWidgets.QSpinBox):
    """Power-of-two texture sizes, with short K labels and editable pixel values."""
    def __init__(self,parent=None):
        super().__init__(parent);self.setRange(2,32768)
    def nearest(self,value):
        value=max(self.minimum(),min(self.maximum(),int(value)))
        lower=1 << (value.bit_length()-1)
        return lower if value-lower<lower*2-value else min(self.maximum(),lower*2)
    def setValue(self,value):super().setValue(self.nearest(value))
    def textFromValue(self,value):return str(value//1024)+'K' if value>=1024 and value%1024==0 else str(value)
    def valueFromText(self,text):
        text=text.strip().upper()
        try:return self.nearest(float(text[:-1])*1024 if text.endswith('K') else float(text))
        except ValueError:return self.value()
    def validate(self,text,position):
        import re
        valid=bool(re.fullmatch(r'\d+(?:\.\d+)?[kK]?',text.strip()))
        return (QtGui.QValidator.Acceptable if valid else QtGui.QValidator.Intermediate,text,position)
    def stepBy(self,steps):self.setValue(1 << max(1,min(15,self.value().bit_length()-1+steps)))


class CompactDensitySpinBox(QtWidgets.QDoubleSpinBox):
    def textFromValue(self,value):
        text=self.locale().toString(value,'f',self.decimals())
        return text.rstrip('0').rstrip(self.locale().decimalPoint()) if self.decimals() else text


class VisibleCheckBox(QtWidgets.QCheckBox):
    """Draw a clear checkbox indicator that remains legible in Maya themes."""
    def paintEvent(self, event):
        super().paintEvent(event)
        option = QtWidgets.QStyleOptionButton()
        self.initStyleOption(option)
        rect = self.style().subElementRect(
            QtWidgets.QStyle.SE_CheckBoxIndicator, option, self)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        checked = self.isChecked()
        enabled = self.isEnabled()
        fill = ('#4f8dce' if checked else '#202020') if enabled else '#3d3d3d'
        border = ('#e3f1ff' if checked else
                  ('#b8b8b8' if not self.underMouse() else '#eeeeee'))
        painter.setPen(QtGui.QPen(QtGui.QColor(border), 1.5))
        painter.setBrush(QtGui.QColor(fill))
        painter.drawRoundedRect(QtCore.QRectF(rect).adjusted(.75, .75, -.75, -.75), 3., 3.)
        if checked:
            pen = QtGui.QPen(QtGui.QColor('#ffffff'), 2.1)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            pen.setJoinStyle(QtCore.Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.NoBrush)
            path = QtGui.QPainterPath()
            path.moveTo(rect.left()+4., rect.center().y())
            path.lineTo(rect.left()+7.5, rect.bottom()-4.)
            path.lineTo(rect.right()-3.5, rect.top()+4.)
            painter.drawPath(path)
        painter.end()


class SectionHeader(QtWidgets.QToolButton):
    """Clickable section title that also drags pinned cards."""
    def __init__(self, section):
        super().__init__()
        self.section = section
        self.drag_start = None
        self.dragging = False

    @staticmethod
    def _global_pos(event):
        return event.globalPos()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.section.pin.isChecked():
            self.drag_start = event.pos()
            self.dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self.drag_start is not None and
                event.buttons() & QtCore.Qt.LeftButton and
                self.section.pin.isChecked()):
            distance = (event.pos()-self.drag_start).manhattanLength()
            if self.dragging or distance >= QtWidgets.QApplication.startDragDistance():
                self.dragging = True
                self.setCursor(QtCore.Qt.ClosedHandCursor)
                owner = getattr(self.section, 'owner_window', None)
                if hasattr(owner, 'move_pinned_section'):
                    owner.move_pinned_section(self.section, self._global_pos(event))
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.dragging:
            owner = getattr(self.section, 'owner_window', None)
            if hasattr(owner, 'finish_section_drag'):
                owner.finish_section_drag()
            self.dragging = False
            self.drag_start = None
            self.setDown(False)
            self.setCursor(QtCore.Qt.OpenHandCursor)
            event.accept()
            return
        self.drag_start = None
        super().mouseReleaseEvent(event)


class Preview(QtWidgets.QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setMinimumHeight(200)
        self.setBackgroundBrush(QtGui.QColor('#20252b'))
        self.setRenderHint(QtGui.QPainter.Antialiasing)
        self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setToolTip('H: orange | V: cyan | shared angle: white\nWheel: zoom. Drag: pan. Refresh: frame all.')
        self._key = None
        self._snapshot = None
        self.changes = ChangeTracker()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(600)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.stop)

    def stop(self):
        self.timer.stop()
        self.changes.stop()
        try:
            QtWidgets.QApplication.instance().aboutToQuit.disconnect(self.stop)
        except (RuntimeError, TypeError):
            pass

    def wheelEvent(self, event):
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        scale = self.transform().m11() * factor
        if 0.01 < scale < 10000:
            self.scale(factor, factor)
        event.accept()

    def pen(self, color, width=1):
        pen = QtGui.QPen(QtGui.QColor(color))
        pen.setWidthF(width)
        pen.setCosmetic(True)
        return pen

    def refresh(self, frame=False):
        import uv_straight as uv
        if not self.isVisible() and not frame:
            return
        if not frame and not self.changes.dirty:
            return
        try:
            _, meshes = uv._selection()
            snapshots = [(m, uv._marks(m)) for m in meshes]
            self.changes.watch(meshes)
            key = tuple((m.name, m.uv_set) for m in meshes)
            fingerprint = repr([(m.name, m.points, groups) for m, groups in snapshots])
            if fingerprint == self._snapshot and not frame:
                return
            self._snapshot = fingerprint
            self.scene().clear()
            for mesh, groups in snapshots:
                path = QtGui.QPainterPath()
                edges = set().union(*mesh.edge_uvs.values()) if mesh.edge_uvs else set()
                for a, b in edges:
                    x, y = mesh.points[a]
                    path.moveTo(x * 100, -y * 100)
                    x, y = mesh.points[b]
                    path.lineTo(x * 100, -y * 100)
                self.scene().addPath(path, self.pen('#6d7885'))
                membership = {}
                for group in groups:
                    ids = group['ids']
                    color = COLORS[group['axis']]
                    group_path = QtGui.QPainterPath()
                    for a, b in uv._marked_pairs(mesh, group):
                        x, y = mesh.points[a]
                        group_path.moveTo(x * 100, -y * 100)
                        x, y = mesh.points[b]
                        group_path.lineTo(x * 100, -y * 100)
                    self.scene().addPath(group_path, self.pen(color, 3))
                    for i in ids:
                        membership.setdefault(i, set()).add(group['axis'])
                for i, axes in membership.items():
                    x, y = mesh.points[i]
                    color = '#ffffff' if len(axes) > 1 else COLORS[next(iter(axes))]
                    dot = self.scene().addEllipse(-3, -3, 6, 6, self.pen(color), QtGui.QBrush(QtGui.QColor(color)))
                    dot.setFlag(QtWidgets.QGraphicsItem.ItemIgnoresTransformations)
                    dot.setPos(x * 100, -y * 100)
            bounds = self.scene().itemsBoundingRect().adjusted(-12, -12, 12, 12)
            self.scene().setSceneRect(bounds)
            if frame or key != self._key:
                self.fitInView(bounds, QtCore.Qt.KeepAspectRatio)
            self._key = key
        except (ValueError, RuntimeError) as error:
            self.changes.watch([])
            self.scene().clear()
            item = self.scene().addText(str(error))
            item.setDefaultTextColor(QtGui.QColor('#b9c5d2'))
            item.setTextWidth(320)
            self.resetTransform()
            self.scene().setSceneRect(item.boundingRect())
            self._snapshot = None
            self._key = None


def category_icon(key):
    """Small vector-drawn icons, independent of Maya icon search paths."""
    if key in ('packing', 'updates'):
        from uv_straight_icons import icon
        return icon('pack' if key == 'packing' else 'updates')
    pixmap = QtGui.QPixmap(48, 48)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.scale(2., 2.)
    colors = dict(unwrap='#e4ad60', pins='#41c8ee', align='#91b6ef',
                  checker='#b8b8b8', materials='#8cd1c1', display='#cf91df', texel='#37d6a3',
                  checks='#efbd55', done='#65cf89', settings='#b9b9b9',
                  overlaps='#dba775')
    painter.setPen(QtGui.QPen(QtGui.QColor(colors[key]), 1.6))
    if key == 'unwrap':
        painter.drawRect(3, 4, 7, 7)
        painter.drawRect(13, 4, 7, 7)
        painter.drawRect(3, 14, 17, 6)
    elif key == 'pins':
        painter.setPen(QtGui.QPen(QtGui.QColor(COLORS['H']), 1.6))
        painter.drawLine(4, 4, 20, 4)
        painter.drawLine(4, 20, 20, 20)
        painter.setPen(QtGui.QPen(QtGui.QColor(COLORS['V']), 1.6))
        painter.drawLine(4, 4, 4, 20)
        painter.drawLine(20, 4, 20, 20)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor('#ffffff'))
        for x, y in ((4, 4), (20, 4), (4, 20), (20, 20)):
            painter.drawEllipse(QtCore.QPointF(x, y), 1.5, 1.5)
    elif key == 'align':
        polygon = QtGui.QPolygonF([
            QtCore.QPointF(4, 17), QtCore.QPointF(8, 4),
            QtCore.QPointF(21, 8), QtCore.QPointF(16, 21)])
        painter.setBrush(QtGui.QColor(145, 182, 239, 34))
        painter.drawPolygon(polygon)
        painter.drawLine(QtCore.QPointF(4, 17), QtCore.QPointF(21, 8))
        painter.setBrush(QtGui.QColor(colors[key]))
        for point in polygon:
            painter.drawEllipse(point, 1.35, 1.35)
    elif key == 'checker':
        painter.drawRect(3, 3, 18, 18)
        for x in range(3):
            for y in range(3):
                if (x+y)%2 == 0:
                    painter.fillRect(3+x*6, 3+y*6, 6, 6, QtGui.QColor(colors[key]))
    elif key == 'materials':
        center = QtCore.QPointF(12, 12)
        gradient = QtGui.QRadialGradient(center, 10, QtCore.QPointF(8, 7))
        gradient.setColorAt(0, QtGui.QColor('#e3fff4'))
        gradient.setColorAt(.45, QtGui.QColor('#8cd1c1'))
        gradient.setColorAt(1, QtGui.QColor('#366e69'))
        painter.setBrush(gradient)
        painter.drawEllipse(QtCore.QRectF(3, 3, 18, 18))
        painter.setPen(QtGui.QPen(QtGui.QColor('#daf8f0'), 1.2))
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawArc(QtCore.QRectF(5.5, 5.5, 13, 13), 75 * 16, 70 * 16)
    elif key == 'display':
        painter.setBrush(QtGui.QColor('#252a30'))
        painter.drawRoundedRect(QtCore.QRectF(3, 6, 18, 13), 2, 2)
        painter.save()
        screen = QtCore.QRectF(5, 8, 14, 9)
        painter.setClipRect(screen)
        bars = ('#f04b4b', '#f2d34f', '#54cf6a',
                '#45d4d8', '#568bea', '#c16add')
        width = screen.width()/len(bars)
        painter.setPen(QtCore.Qt.NoPen)
        for index, color in enumerate(bars):
            painter.fillRect(QtCore.QRectF(screen.left()+index*width,
                                          screen.top(), width+.2,
                                          screen.height()), QtGui.QColor(color))
        painter.restore()
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.setPen(QtGui.QPen(QtGui.QColor(colors[key]), 1.5))
        painter.drawRoundedRect(QtCore.QRectF(3, 6, 18, 13), 2, 2)
        painter.drawLine(QtCore.QPointF(9, 6), QtCore.QPointF(6, 2.5))
        painter.drawLine(QtCore.QPointF(15, 6), QtCore.QPointF(18, 2.5))
        painter.drawLine(QtCore.QPointF(7, 21), QtCore.QPointF(9, 19))
        painter.drawLine(QtCore.QPointF(17, 21), QtCore.QPointF(15, 19))
    elif key == 'texel':
        gradient = QtGui.QLinearGradient(3, 0, 21, 0)
        for position, color in ((0., '#123cff'), (.25, '#00dfff'),
                                (.5, '#35ed42'), (.75, '#ffe100'), (1., '#ff3b0a')):
            gradient.setColorAt(position, QtGui.QColor(color))
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(QtCore.QRectF(3, 5, 18, 14), 3, 3)
        painter.setPen(QtGui.QColor('#101010'))
        font = QtGui.QFont('Arial')
        font.setPixelSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QtCore.QRectF(3, 5, 18, 14), QtCore.Qt.AlignCenter, 'TD')
    elif key == 'overlaps':
        painter.setPen(QtGui.QPen(QtGui.QColor('#ef6464'), 1.6))
        painter.drawRect(3, 4, 10, 10)
        painter.setPen(QtGui.QPen(QtGui.QColor('#549aee'), 1.6))
        painter.drawRect(10, 11, 10, 10)
    elif key == 'checks':
        painter.setBrush(QtGui.QColor(239, 189, 85, 35))
        warning = QtGui.QPolygonF([
            QtCore.QPointF(12, 2.5), QtCore.QPointF(22, 20.5),
            QtCore.QPointF(2, 20.5)])
        painter.drawPolygon(warning)
        painter.setBrush(QtGui.QColor(colors[key]))
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(QtCore.QRectF(11, 7, 2, 7), 1, 1)
        painter.drawEllipse(QtCore.QPointF(12, 17.5), 1.2, 1.2)
    elif key == 'settings':
        # Bright flames frame an ivory keycap; draw a dark K for contrast.
        flame = QtGui.QPainterPath()
        flame.moveTo(4, 20)
        flame.cubicTo(1, 16, 1.3, 9, 4.6, 3.2)
        flame.cubicTo(4.8, 6.8, 7.1, 8.1, 8.7, 5.7)
        flame.cubicTo(9.4, 3.5, 12.5, 3, 13.6, .3)
        flame.cubicTo(13.2, 4.5, 17.6, 6.5, 17.2, 9.5)
        flame.cubicTo(18.8, 9, 20.8, 8.2, 20.6, 6.3)
        flame.cubicTo(23.5, 11.3, 24, 17, 20, 21)
        flame.cubicTo(16, 24, 7, 24, 4, 20)
        flame.closeSubpath()
        gradient = QtGui.QLinearGradient(0, 1, 0, 24)
        gradient.setColorAt(0, QtGui.QColor('#fff3a6'))
        gradient.setColorAt(.45, QtGui.QColor('#ffbb38'))
        gradient.setColorAt(1, QtGui.QColor('#ff7435'))
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QBrush(gradient))
        painter.drawPath(flame)
        heat = QtGui.QPainterPath()
        heat.moveTo(6, 18)
        heat.cubicTo(5, 12, 8.2, 9, 8.6, 5.3)
        heat.cubicTo(8.9, 8, 12, 9, 14.2, 4.6)
        heat.cubicTo(13.1, 10, 18, 13, 18, 19)
        heat.closeSubpath()
        painter.setBrush(QtGui.QColor('#ffe6a1'))
        painter.drawPath(heat)
        # Angled keycap with a deep front skirt and shaded right side.
        skirt = QtGui.QPainterPath()
        skirt.moveTo(6.5, 7)
        skirt.lineTo(19, 8)
        skirt.lineTo(21, 17)
        skirt.lineTo(19.5, 21.5)
        skirt.quadTo(19.2, 23, 17.5, 22.8)
        skirt.lineTo(4.5, 21.5)
        skirt.quadTo(2.5, 21.2, 3, 19)
        skirt.lineTo(5, 9)
        skirt.quadTo(5.3, 7, 6.5, 7)
        skirt.closeSubpath()
        front = QtGui.QLinearGradient(0, 9, 0, 23)
        front.setColorAt(0, QtGui.QColor('#c6ced5'))
        front.setColorAt(1, QtGui.QColor('#8594a3'))
        painter.setPen(QtGui.QPen(QtGui.QColor('#ffe2a4'), .85))
        painter.setBrush(QtGui.QBrush(front))
        painter.drawPath(skirt)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor('#637587'))
        painter.drawPolygon(QtGui.QPolygonF([
            QtCore.QPointF(18.6, 8.5), QtCore.QPointF(21, 17),
            QtCore.QPointF(19.5, 21.5), QtCore.QPointF(18, 22.4),
            QtCore.QPointF(16.7, 14.8)]))
        # Raise the face and letter together to give the skirt more depth.
        painter.save()
        painter.translate(0, -3)
        top = QtGui.QPainterPath()
        top.moveTo(7.5, 9.5)
        top.lineTo(17.5, 10.4)
        top.quadTo(19.3, 10.6, 18.8, 12.2)
        top.lineTo(17.5, 16.3)
        top.quadTo(17.1, 18, 15.5, 17.8)
        top.lineTo(6, 16.8)
        top.quadTo(4.5, 16.6, 5, 15)
        top.lineTo(6, 11)
        top.quadTo(6.3, 9.4, 7.5, 9.5)
        top.closeSubpath()
        face = QtGui.QLinearGradient(0, 9, 0, 18)
        face.setColorAt(0, QtGui.QColor('#fff8e8'))
        face.setColorAt(1, QtGui.QColor('#dfd8c7'))
        painter.setPen(QtGui.QPen(QtGui.QColor('#fff1cd'), 1.05))
        painter.setBrush(QtGui.QBrush(face))
        painter.drawPath(top)
        pen = QtGui.QPen(QtGui.QColor('#253344'), 1.75)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(QtCore.QPointF(9, 11.9), QtCore.QPointF(8, 15.6))
        painter.drawPolyline(QtGui.QPolygonF([
            QtCore.QPointF(14, 12.4), QtCore.QPointF(8.7, 13.9),
            QtCore.QPointF(13, 16.1)]))
        painter.restore()
    elif key == 'done':
        painter.drawRect(3, 3, 18, 18)
        painter.drawLine(6, 12, 10, 16)
        painter.drawLine(10, 16, 18, 7)
    painter.end()
    return QtGui.QIcon(pixmap)


def move_arrow_icon(direction):
    """Crisp vector arrow for the Move Overlaps direction pad."""
    pixmap = QtGui.QPixmap(48, 48)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.scale(2., 2.)
    painter.translate(12., 12.)
    angles = {'up': 0., 'right': 90., 'down': 180., 'left': 270.}
    painter.rotate(angles[direction])
    color = QtGui.QColor('#e8ba78')
    painter.setPen(QtGui.QPen(color, 1.8, QtCore.Qt.SolidLine,
                              QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
    painter.drawLine(QtCore.QPointF(0, 7), QtCore.QPointF(0, -5))
    painter.drawLine(QtCore.QPointF(0, -5), QtCore.QPointF(-4.5, -.5))
    painter.drawLine(QtCore.QPointF(0, -5), QtCore.QPointF(4.5, -.5))
    painter.end()
    return QtGui.QIcon(pixmap)


def overlap_action_icon(action):
    """Neutral eye and scan icons for the compact overlap controls."""
    pixmap = QtGui.QPixmap(48, 48)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.scale(2., 2.)
    pen = QtGui.QPen(QtGui.QColor('#dedede'), 1.7)
    pen.setCapStyle(QtCore.Qt.RoundCap)
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    painter.setPen(pen)
    if action == 'eye':
        eye = QtGui.QPainterPath(QtCore.QPointF(2., 12.))
        eye.cubicTo(7., 3., 17., 3., 22., 12.)
        eye.cubicTo(17., 21., 7., 21., 2., 12.)
        painter.drawPath(eye)
        painter.drawEllipse(QtCore.QRectF(9., 9., 6., 6.))
    elif action == 'analyse':
        for x, y, dx, dy in ((3., 3., 1., 1.), (21., 3., -1., 1.),
                              (3., 21., 1., -1.), (21., 21., -1., -1.)):
            painter.drawLine(QtCore.QPointF(x + dx*4., y), QtCore.QPointF(x, y))
            painter.drawLine(QtCore.QPointF(x, y), QtCore.QPointF(x, y + dy*4.))
        painter.drawEllipse(QtCore.QRectF(7., 7., 8., 8.))
        painter.drawLine(QtCore.QPointF(14., 14.), QtCore.QPointF(18., 18.))
    painter.end()
    return QtGui.QIcon(pixmap)


def section_pin_icon(pinned):
    pixmap = QtGui.QPixmap(40, 40)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.scale(2., 2.)
    color = QtGui.QColor('#ffffff' if pinned else '#939393')
    painter.setPen(QtGui.QPen(color, 1.2))
    path = QtGui.QPainterPath(QtCore.QPointF(11, 3))
    for x, y in ((17, 9), (14, 9), (11, 13), (7, 9), (11, 6), (11, 3)):
        path.lineTo(x, y)
    if pinned:
        # A pinned section uses a solid silhouette so its state stays obvious
        # even at Maya's small toolbar icon size.
        painter.fillPath(path, color)
    painter.drawPath(path)
    stem_pen = QtGui.QPen(color, 1.8 if pinned else 1.2)
    stem_pen.setCapStyle(QtCore.Qt.RoundCap)
    painter.setPen(stem_pen)
    painter.drawLine(8, 12, 3, 17)
    painter.end()
    return QtGui.QIcon(pixmap)


class CollapsibleSection(QtWidgets.QFrame):
    """Compact category with persisted expansion state."""
    def __init__(self, title, key, expanded=True, parent=None):
        super().__init__(parent)
        self.setObjectName('section_' + key)
        self.setProperty('categoryCard', True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Maximum)
        self.option = 'uvStraightSection_' + key
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.header = SectionHeader(self)
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.header.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.header.setMinimumHeight(28)
        self.header.setObjectName('categoryHeader')
        self.body = QtWidgets.QWidget()
        self.body.setObjectName('sectionBody')
        self.content = QtWidgets.QVBoxLayout(self.body)
        self.content.setContentsMargins(6, 6, 6, 16)
        self.content.setSpacing(CONTROL_SPACING)
        header_row = QtWidgets.QHBoxLayout()
        header_row.setContentsMargins(0, 0, 6, 0)
        header_row.addWidget(self.header, 1)
        self.pin = QtWidgets.QToolButton()
        self.pin.setCheckable(True)
        self.pin.setFixedSize(24, 24)
        self.pin.setIconSize(QtCore.QSize(18, 18))
        self.pin.setToolTip('Pin this section open')
        self.pin.setAccessibleName('Pin ' + title.replace('&&', '&'))
        self.pin_option = 'uvStraightPinnedSection_' + key
        self.pin.setChecked(bool(cmds.optionVar(query=self.pin_option))
                            if cmds.optionVar(exists=self.pin_option) else False)
        self.pin.setIcon(section_pin_icon(self.pin.isChecked()))
        self.pin.toggled.connect(self.set_pinned)
        self.header.setCursor(QtCore.Qt.OpenHandCursor if self.pin.isChecked()
                              else QtCore.Qt.ArrowCursor)
        header_row.addWidget(self.pin)
        outer.addLayout(header_row)
        outer.addWidget(self.body)
        value = bool(cmds.optionVar(query=self.option)) if cmds.optionVar(exists=self.option) else expanded
        self.header.setChecked(value)
        self.set_expanded(value, save=False)
        self.header.toggled.connect(self.set_expanded)

    def set_pinned(self, pinned):
        self.pin.setIcon(section_pin_icon(pinned))
        self.pin.setToolTip('Unpin section' if pinned else 'Pin this section open')
        cmds.optionVar(intValue=(self.pin_option, int(pinned)))
        self.header.setCursor(QtCore.Qt.OpenHandCursor if pinned
                              else QtCore.Qt.ArrowCursor)

    def set_expanded(self, expanded, save=True):
        self.body.setVisible(expanded)
        self.header.setArrowType(QtCore.Qt.NoArrow)
        if save:
            cmds.optionVar(intValue=(self.option, int(expanded)))


class DisplayControls(QtCore.QObject):
    """Synchronize both palettes immediately after any menu changes display."""
    def __init__(self, window, pins, distortion, intensity, wireframe, finished, update_timer=None):
        super().__init__(window)
        import uv_straight_native as native
        self.pins, self.distortion, self.intensity = pins, distortion, intensity
        self.wireframe = wireframe
        self.finished = finished
        self.update_timer=update_timer
        self.state = native._display_state
        self.state_slot = self.sync
        self.state.changed.connect(self.state_slot)
        import maya.api.OpenMaya as om
        self.callbacks = [om.MEventMessage.addEventCallback(event, self.refresh)
                          for event in ('DisplayPreferenceChanged',)]
        self.callbacks.extend(om.MEventMessage.addEventCallback(event, self.refresh_finished)
                              for event in ('Undo', 'Redo'))
        self.callbacks.extend(om.MSceneMessage.addCallback(event, self.refresh_finished)
                              for event in (om.MSceneMessage.kAfterNew, om.MSceneMessage.kAfterOpen))
        window.destroyed.connect(self.stop)
        self.sync(native.display_state())

    def refresh(self, *unused):
        import uv_straight_native as native
        self.sync(native.display_state())

    def refresh_finished(self, *unused):
        from uv_straight_colors import finished_visible
        with signals_blocked(self.finished):
            self.finished.setChecked(finished_visible())

    @QtCore.Slot(dict)
    def sync(self, state):
        for widget, value in ((self.wireframe, state['wireframe_enabled']),
                              (self.pins, state['pins_enabled']),
                              (self.distortion, state['distortion_enabled']),
                              (self.finished, state['finished_unwrap_visible']),
                              (self.intensity, state['distortion_range'])):
            blocker = QtCore.QSignalBlocker(widget)
            if widget is self.intensity:
                widget.setValue(value)
            else:
                widget.setChecked(value)
            del blocker

    def stop(self, *unused):
        import maya.api.OpenMaya as om
        if self.update_timer is not None:self.update_timer.stop()
        for callback in self.callbacks:
            om.MMessage.removeCallback(callback)
        self.callbacks = []
        if self.state_slot is not None:
            slot, self.state_slot = self.state_slot, None
            try:
                self.state.changed.disconnect(slot)
            except RuntimeError:
                pass  # Qt can disconnect automatically when the parent is destroyed.


class ToolWindow(MayaQWidgetDockableMixin, QtWidgets.QDialog):
    def stop(self, *unused):
        if hasattr(self, 'updates'):
            self.updates.stop()
        if hasattr(self, 'texel_display'):
            self.texel_display.stop()
        if hasattr(self, 'materials'):
            self.materials.stop()
        if hasattr(self, 'uv_sets'):
            self.uv_sets.stop()
        if hasattr(self, 'section_drag'):
            self.section_drag.cancel()
        if hasattr(self, 'stack_shells'):
            self.stack_shells.stop()
        if hasattr(self,'match_uvs'):self.match_uvs.stop()
        for panel in getattr(self,'uv_group_panels',{}).values():panel.timer.stop()
        if hasattr(self, 'error_checks'):
            self.error_checks.stop()
        if hasattr(self, 'display_controls'):
            self.display_controls.stop()
        if hasattr(self, 'pin_picker'):
            self.pin_picker.stop()
        if hasattr(self, 'angle_wheel'):
            self.angle_wheel.stop()
        if hasattr(self, 'status_timer'):
            self.status_timer.stop()

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)


def header_logo(parent):
    title = QtWidgets.QLabel('ShellShifter', parent)
    title.setObjectName('shellshifterHeaderLogo')
    title.setStyleSheet('color: #aaaaaa; background: transparent;')
    title.setAccessibleName('ShellShifter')
    title.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
    logo_path = Path(__file__).with_name('assets') / 'shellshifter_logo.png'
    reader = QtGui.QImageReader(str(logo_path))
    # Frame the artwork inside the supplied 2170 × 725 logo canvas.
    reader.setClipRect(QtCore.QRect(156, 200, 1900, 286))
    logo = reader.read()
    if not logo.isNull():
        ratio = parent.devicePixelRatioF()
        matte = QtGui.QImage(logo.size(), QtGui.QImage.Format_RGB32)
        matte.fill(QtGui.QColor('#303030'))
        painter = QtGui.QPainter(matte)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_Lighten)
        painter.drawImage(0, 0, logo)
        painter.end()
        pixmap = QtGui.QPixmap.fromImage(matte.scaledToHeight(
            round(21 * ratio), QtCore.Qt.SmoothTransformation))
        pixmap.setDevicePixelRatio(ratio)
        title.setPixmap(pixmap)
    return title


class LanguageSelector(CheckerPatternComboBox):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName('shellshifterLanguage')
        self.setFixedSize(86, 24)
        self.setToolTip('Tooltip language')
        self.setAccessibleName('Tooltip language')
        self.setStyleSheet('QComboBox { background:#3c3c3c; padding:1px 6px; min-height:0; }'
                           'QComboBox::down-arrow { image:none; }')
        self.addItem('English', 'en')
        self.addItem('Русский', 'ru')
        self.setCurrentIndex(self.findData(ui_language.language()))
        self.activated.connect(self.select_language)

    def select_language(self, index):
        ui_language.change(self.itemData(index))

    def wheelEvent(self, event):
        event.ignore()


class PopupTitleBar(QtWidgets.QWidget):
    """Move the palette directly, without a nested native drag loop."""
    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self.origin = self.window_origin = None
        self.dragging = False
        self.setObjectName('popupTitleBar')
        self.setFixedHeight(28)
        self.setCursor(QtCore.Qt.OpenHandCursor)
        self.setToolTip('Drag to move the window')
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(8, 0, 0, 0)
        row.setSpacing(2)
        row.addWidget(header_logo(self), 1)
        row.addWidget(LanguageSelector(self))
        window.popup_pin = QtWidgets.QToolButton(self)
        window.popup_pin.setObjectName('popupWindowPin')
        window.popup_pin.setCheckable(True)
        window.popup_pin.setFixedSize(24, 24)
        window.popup_pin.setIconSize(QtCore.QSize(18, 18))
        window.popup_pin.setIcon(section_pin_icon(False))
        window.popup_pin.setToolTip('Pin the window to keep it open while working in Maya')
        window.popup_pin.setAccessibleName('Pin popup window')
        window.popup_pin.setCursor(QtCore.Qt.PointingHandCursor)
        window.popup_pin.toggled.connect(window.set_popup_pinned)
        row.addWidget(window.popup_pin)
        window.popup_close = QtWidgets.QToolButton(self)
        window.popup_close.setObjectName('popupWindowClose')
        window.popup_close.setText('×')
        window.popup_close.setFixedSize(28, 28)
        window.popup_close.setToolTip('Close window')
        window.popup_close.setAccessibleName('Close popup window')
        window.popup_close.setCursor(QtCore.Qt.PointingHandCursor)
        window.popup_close.clicked.connect(window.close)
        row.addWidget(window.popup_close)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.origin = SectionHeader._global_pos(event)
            self.window_origin = self.owner.pos()
            self.dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.origin is not None and event.buttons() & QtCore.Qt.LeftButton:
            point = SectionHeader._global_pos(event)
            delta = point - self.origin
            if self.dragging or delta.manhattanLength() >= QtWidgets.QApplication.startDragDistance():
                self.dragging = True
                self.setCursor(QtCore.Qt.ClosedHandCursor)
                self.owner.move_popup(self.window_origin + delta, point)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.origin is not None:
            self.origin = self.window_origin = None
            self.dragging = False
            self.setCursor(QtCore.Qt.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class PopupWindow(QtWidgets.QDialog):
    """Lightweight popup shell; it must never create a Maya workspace control."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WA_NoMouseReplay)
        self.setAutoFillBackground(False)
        self.popup_pinned = False
        self.popup_flag_change = False
        # Qt 5 can immediately dismiss a native popup opened from Maya's
        # viewport shortcut. Keep a tool window and dismiss on outside clicks.
        self.transient_popup_type = QtCore.Qt.Tool
        QtWidgets.QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event):
        if (event.type() == QtCore.QEvent.MouseButtonPress and self.isVisible()
                and not self.popup_pinned
                and QtWidgets.QApplication.activePopupWidget() is None
                and QtWidgets.QApplication.activeModalWidget() is None
                and not self.rect().contains(self.mapFromGlobal(event.globalPos()))):
            self.close()
        return False

    def set_popup_pinned(self, pinned):
        pinned = bool(pinned)
        if pinned == self.popup_pinned:
            return
        position, size, visible = self.pos(), self.size(), self.isVisible()
        self.popup_pinned = pinned
        with signals_blocked(self.popup_pin):
            self.popup_pin.setChecked(pinned)
        self.popup_pin.setIcon(section_pin_icon(pinned))
        self.popup_pin.setToolTip('Unpin the window to close it when clicking outside' if pinned else
                                  'Pin the window to keep it open while working in Maya')
        # Changing native window type temporarily hides it. This is not a close:
        # cooperative checks must keep running through the hide/show sequence.
        self.popup_flag_change = True
        try:
            kind = QtCore.Qt.Tool if pinned else self.transient_popup_type
            self.setWindowFlags(kind | QtCore.Qt.FramelessWindowHint |
                                QtCore.Qt.NoDropShadowWindowHint)
            self.resize(size)
            self.move(position)
            if visible:
                self.show()
                self.raise_()
                self.activateWindow()
        finally:
            self.popup_flag_change = False

    def move_popup(self, position, cursor=None):
        screen = QtGui.QGuiApplication.screenAt(cursor if cursor is not None else position)
        screen = screen or self.screen() or QtGui.QGuiApplication.primaryScreen()
        available = screen.availableGeometry()
        x = max(available.left(), min(position.x(), available.right()-self.width()+1))
        y = max(available.top(), min(position.y(), available.bottom()-self.height()+1))
        self.move(x, y)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Escape:
            if not self.popup_pinned:
                self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        painter=QtGui.QPainter(self)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(),QtCore.Qt.transparent)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtGui.QColor('#303030'))
        painter.drawRoundedRect(QtCore.QRectF(self.rect()),14,14)
        painter.end()

    def stop(self, *unused):
        if hasattr(self, 'updates'):
            self.updates.stop()
        if hasattr(self, 'texel_display'):
            self.texel_display.stop()
        if hasattr(self, 'materials'):
            self.materials.stop()
        if hasattr(self, 'uv_sets'):
            self.uv_sets.stop()
        if hasattr(self, 'section_drag'):
            self.section_drag.cancel()
        if hasattr(self, 'stack_shells'):
            self.stack_shells.stop()
        if hasattr(self,'match_uvs'):self.match_uvs.stop()
        for panel in getattr(self,'uv_group_panels',{}).values():panel.timer.stop()
        if hasattr(self, 'error_checks'):
            self.error_checks.stop()
        if hasattr(self, 'display_controls'):
            self.display_controls.stop()
        if hasattr(self, 'pin_picker'):
            self.pin_picker.stop()
        if hasattr(self, 'angle_wheel'):
            self.angle_wheel.stop()
        if hasattr(self, 'status_timer'):
            self.status_timer.stop()

    def closeEvent(self, event):
        global _popup
        if _popup is self:
            _popup = None
        QtWidgets.QApplication.instance().removeEventFilter(self)
        self.stop()
        super().closeEvent(event)


def show_window(popup=False):
    global _startup_defaults_applied
    import uv_straight as uv
    from functools import partial
    object_name = POPUP_OBJECT if popup else 'uvStraightQtWindow'
    for widget in QtWidgets.QApplication.allWidgets():
        if widget.objectName() == object_name:
            if hasattr(widget, 'updates'):
                widget.updates.stop()
            if hasattr(widget, 'texel_display'):
                widget.texel_display.stop()
            if hasattr(widget, 'materials'):
                widget.materials.stop()
            if hasattr(widget, 'uv_sets'):
                widget.uv_sets.stop()
            if hasattr(widget, 'section_drag'):
                widget.section_drag.cancel()
            if hasattr(widget, 'stack_shells'):
                widget.stack_shells.stop()
            if hasattr(widget,'match_uvs'):widget.match_uvs.stop()
            if hasattr(widget, 'error_checks'):
                widget.error_checks.stop()
            if hasattr(widget, 'display_controls'):
                widget.display_controls.stop()
            if hasattr(widget, 'pin_picker'):
                widget.pin_picker.stop()
            if hasattr(widget,'angle_wheel'):
                widget.angle_wheel.stop()
            if hasattr(widget,'preview'):
                widget.preview.stop()
            if hasattr(widget,'status_timer'):
                widget.status_timer.stop()
    if not popup and cmds.workspaceControl(DOCK_CONTROL, query=True, exists=True):
        cmds.deleteUI(DOCK_CONTROL)
    for widget in QtWidgets.QApplication.topLevelWidgets():
        try:
            if widget.objectName() == object_name:
                widget.close()
                widget.deleteLater()
        except RuntimeError:
            pass
    parent = None
    if popup and not cmds.about(batch=True):
        import maya.OpenMayaUI as omui
        from shiboken2 import wrapInstance
        pointer = omui.MQtUtil.mainWindow()
        if pointer:
            parent = wrapInstance(int(pointer), QtWidgets.QWidget)
    window = (PopupWindow if popup else ToolWindow)(parent=parent)
    window.popup_mode = bool(popup)
    window.setObjectName(object_name)
    window.setWindowTitle('ShellShifter ' + uv.VERSION + ' | Maya ' + cmds.about(version=True))
    window.resize(340, 720)
    window.setMinimumWidth(300)
    from uv_straight_icons import menu_image
    spin_up_image=menu_image('spin_up');spin_down_image=menu_image('spin_down')
    window.setStyleSheet(
        'QDialog, QWidget#categoryContents { background: #303030; }'
        'QFrame[categoryCard="true"] { background: #383838; border: 0; border-radius: 14px; }'
        'QFrame[categoryCard="true"][dragSource="true"] { background: #414b55; border: 1px solid #a7cbed; }'
        'QWidget#sectionBody { background: transparent; }'
        'QDoubleSpinBox, QSpinBox, QComboBox, QLineEdit { background: #474747; color: #eeeeee; '
        'border: 1px solid #555555; border-radius: 7px; padding: 4px 6px; min-height: 18px; }'
        'QSpinBox, QDoubleSpinBox { padding-right: 4px; }'
        'QSpinBox::up-button, QDoubleSpinBox::up-button {'
        'subcontrol-origin:border;subcontrol-position:top right;width:16px;height:10px;'
        'margin-top:2px;margin-right:2px;background:#565656;border:0;border-radius:5px;}'
        'QSpinBox::down-button, QDoubleSpinBox::down-button {'
        'subcontrol-origin:border;subcontrol-position:bottom right;width:16px;height:10px;'
        'margin-bottom:2px;margin-right:2px;background:#565656;border:0;border-radius:5px;}'
        'QSpinBox::up-button:hover,QSpinBox::down-button:hover,'
        'QDoubleSpinBox::up-button:hover,QDoubleSpinBox::down-button:hover {background:#686868;}'
        'QSpinBox::up-button:pressed,QSpinBox::down-button:pressed,'
        'QDoubleSpinBox::up-button:pressed,QDoubleSpinBox::down-button:pressed {background:#424242;}'
        'QSpinBox::up-button:disabled,QSpinBox::down-button:disabled,'
        'QDoubleSpinBox::up-button:disabled,QDoubleSpinBox::down-button:disabled {background:#484848;}'
        'QSpinBox::up-arrow,QDoubleSpinBox::up-arrow,'
        'QSpinBox::down-arrow,QDoubleSpinBox::down-arrow {width:10px;height:8px;}'
        f'QSpinBox::up-arrow,QDoubleSpinBox::up-arrow {{image:url("{spin_up_image}");}}'
        f'QSpinBox::down-arrow,QDoubleSpinBox::down-arrow {{image:url("{spin_down_image}");}}'
        'QComboBox::drop-down { border: 0; width: 18px; }'
        'QLabel, QCheckBox { color: #dedede; background: transparent; }'
        'QCheckBox { spacing: 8px; padding: 4px 0; }'
        'QCheckBox::indicator { width: 18px; height: 18px; }'
        'QPushButton { background: #525252; color: #eeeeee; padding: 5px 8px; '
        'min-height: 16px; border: 0; border-radius: 12px; }'
        'QPushButton:hover { background: #626262; }'
        'QPushButton:pressed { background: #424242; }'
        'QPushButton:focus { border: 1px solid #8098b4; }'
        'QPushButton:disabled { background: #414141; color: #888888; }'
        'QToolButton { background: transparent; color: #eeeeee; border: 0; padding: 4px; }'
        'QToolButton#categoryHeader { text-align: left; padding-left: 12px; }'
        'QToolButton#popupWindowPin, QToolButton#popupWindowClose { border-radius: 12px; padding: 0; }'
        'QToolButton#popupWindowClose { border-radius: 14px; font-size: 22px; }'
        'QToolButton#popupWindowPin:hover, QToolButton#popupWindowClose:hover { background: #525252; }'
        'QToolButton#popupWindowPin:checked { background: #494949; }'
        'QToolButton#categoryIcon { border-radius: 5px; }'
        'QToolButton#categoryIcon:hover { background: #454545; }'
        'QToolButton#categoryIcon:checked { background: #486080; }'
        'QScrollArea { border: 0; background: transparent; }')
    root = QtWidgets.QVBoxLayout(window)
    root.setContentsMargins(6, 6, 6, 6)
    if popup:
        window.popup_titlebar = PopupTitleBar(window)
        root.addWidget(window.popup_titlebar)
    else:
        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(3, 0, 1, 0)
        header.addWidget(header_logo(window), 1)
        header.addWidget(LanguageSelector(window))
        root.addLayout(header)
    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    category_row = QtWidgets.QHBoxLayout()
    category_row.setSpacing(5)
    root.addLayout(category_row, 1)
    rail = QtWidgets.QVBoxLayout()
    rail.setSpacing(3)
    category_row.addLayout(rail)
    category_row.addWidget(scroll, 1)
    contents = QtWidgets.QWidget()
    contents.setObjectName('categoryContents')
    scroll.setWidget(contents)
    stack = QtWidgets.QVBoxLayout(contents)
    stack.setContentsMargins(0, 0, 0, 0)
    stack.setSpacing(4)
    window.sections = {}
    window.category_buttons = {}
    legacy_categories={'align':'unwrap','pins':'unwrap','checker':'display','texel':'display','done':'display'}
    for old,new in legacy_categories.items():
        legacy_pin='uvStraightPinnedSection_'+old
        if cmds.optionVar(exists=legacy_pin):
            if cmds.optionVar(query=legacy_pin):cmds.optionVar(intValue=('uvStraightPinnedSection_'+new,1))
            cmds.optionVar(remove=legacy_pin)
    for key, title, expanded in SECTION_DEFINITIONS:
        section = CollapsibleSection(title, key, expanded)
        # Keep a direct owner reference: after Maya docks the tool,
        # QWidget.window() points to workspaceControl instead of ToolWindow.
        section.owner_window = window
        window.sections[key] = section
        stack.addWidget(section)
        icon = QtWidgets.QToolButton()
        icon.setObjectName('categoryIcon')
        icon.setIcon(category_icon(key))
        icon.setIconSize(QtCore.QSize(22, 22))
        icon.setFixedSize(32, 32)
        icon.setCheckable(True)
        icon.setToolTip(tooltip_label(title))
        icon.setAccessibleName(title.replace('&&', '&'))
        rail.addWidget(icon)
        window.category_buttons[key] = icon
    rail.addStretch()
    stack.addStretch()
    default_order = [item[0] for item in SECTION_DEFINITIONS]
    saved_order = (str(cmds.optionVar(query='uvStraightPinnedOrder')).split(',')
                   if cmds.optionVar(exists='uvStraightPinnedOrder') else [])
    saved_order=list(dict.fromkeys(legacy_categories.get(key,key) for key in saved_order))
    window.pinned_order = ([key for key in saved_order if key in default_order] +
                           [key for key in default_order if key not in saved_order])
    window.drag_source_key = None
    window.drag_target_index = None
    def apply_section_order(preview_order=None):
        requested_order = preview_order if preview_order is not None else window.pinned_order
        pinned = [key for key in requested_order
                  if window.sections[key].pin.isChecked()]
        active = ([window.active_category]
                  if getattr(window, 'active_category', '') in window.sections and
                  not window.sections[window.active_category].pin.isChecked() else [])
        order = pinned + active + [key for key in requested_order
                                   if key not in pinned and key not in active]
        for index, key in enumerate(order):
            stack.insertWidget(index, window.sections[key])
    window.apply_section_order = apply_section_order
    def reorder_pinned(source_key, target_index):
        pinned = [key for key in window.pinned_order
                  if window.sections[key].pin.isChecked()]
        if source_key not in pinned:
            return
        pinned.remove(source_key)
        target_index = max(0, min(int(target_index), len(pinned)))
        pinned.insert(target_index, source_key)
        window.pinned_order = pinned + [key for key in window.pinned_order
                                        if key not in pinned]
        cmds.optionVar(stringValue=('uvStraightPinnedOrder',
                                    ','.join(window.pinned_order)))
        apply_section_order()
    window.reorder_pinned = reorder_pinned
    from uv_straight_section_drag import SectionDragOverlay
    window.section_drag = SectionDragOverlay(window, contents, stack)
    window.move_pinned_section = window.section_drag.move_section
    window.finish_section_drag = window.section_drag.finish
    def clear_menu_messages():
        navigator = getattr(window, 'hard_edge_navigation', None)
        if navigator is not None:
            navigator.clear()
        # Class identity can change when another palette reloads the UI modules.
        for label in window.findChildren(QtWidgets.QLabel):
            if any(base.__name__=='ResultLabel' for base in type(label).__mro__):
                label.setText('');label.hide()
        materials=getattr(window,'materials',None)
        for owner in (materials,getattr(materials,'textures',None)):
            status=getattr(owner,'status',None)
            if status is not None:status.setText('');status.hide()
    window.clear_menu_messages=clear_menu_messages
    def select_category(key):
        key=legacy_categories.get(key,key)  # Existing shortcuts/preferences.
        if key!=getattr(window,'active_category',None):clear_menu_messages()
        window.section_drag.cancel()
        if key == 'packing' and hasattr(window, 'packing_refresh'):
            window.packing_refresh()
        if key == 'overlaps' and hasattr(window, 'overlap_refresh'):
            window.overlap_refresh()
        for name, section in window.sections.items():
            selected = name == key or section.pin.isChecked()
            section.setVisible(selected)
            section.header.blockSignals(True)
            section.header.setChecked(selected)
            section.header.blockSignals(False)
            section.set_expanded(selected, save=False)
            window.category_buttons[name].setChecked(selected)
        window.active_category = key
        cmds.optionVar(stringValue=('uvStraightActiveCategory', key))
        apply_section_order()
        import uv_straight_native as native
        native.ensure_viewport_colors()
        if popup and hasattr(window, 'adjust_popup_size'):
            QtCore.QTimer.singleShot(0, window.adjust_popup_size)
    window.select_category = select_category
    for key, icon in window.category_buttons.items():
        window.sections[key].pin.toggled.connect(
            lambda checked=False: select_category(window.active_category))
        icon.clicked.connect(lambda checked=False, key=key: select_category(key if window.category_buttons[key].isChecked() else ''))
        window.sections[key].header.clicked.connect(lambda checked=False, key=key: select_category(key if window.sections[key].header.isChecked() else ''))
    saved_category = cmds.optionVar(query='uvStraightActiveCategory') if cmds.optionVar(exists='uvStraightActiveCategory') else 'unwrap'
    select_category(saved_category if saved_category in window.sections or saved_category in ('',*legacy_categories) else 'unwrap')
    done_group = QtWidgets.QWidget(window)
    done_group.setObjectName('displayFinishedGroup')
    layout = QtWidgets.QVBoxLayout(done_group)
    layout.setContentsMargins(0,0,0,0)
    layout.setSpacing(CONTROL_SPACING)
    from uv_straight_actions import register_button
    window.shortcut_actions = {}
    def label(text):
        item = QtWidgets.QLabel(text)
        item.setWordWrap(True)
        layout.addWidget(item)
    def button(text, callback, target=None, color=None,compact=False):
        from uv_straight_icons import BUTTONS, ActionButton
        target = layout if target is None else target
        item = (ActionButton(text, BUTTONS[text], dark=bool(color))
                if text in BUTTONS and not compact else QtWidgets.QPushButton(text))
        def invoke():
            # Let Qt finish button delivery before Maya replaces UV/DG data.
            # Native Unfold may pump paint events while it is solving.
            from uv_straight_menu import after_menu_interaction
            after_menu_interaction(partial(uv._run, callback))
        item.clicked.connect(invoke)
        register_button(window, text, callback)
        if color:
            item.setStyleSheet(item.styleSheet() + 'QPushButton { background: ' + color + '; color: #111820; font-weight: bold; }')
        target.addWidget(item)
        return item
    done_row = QtWidgets.QHBoxLayout()
    layout.addLayout(done_row)
    from uv_straight_icons import icon as action_icon
    mark_done_button = button('Mark Done', partial(uv.mark_done, True), done_row, compact=True)
    mark_done_button.setToolTip(
        'Mark selected meshes as done. Show Done displays them in green. The mark is saved in the scene.')
    clear_done_button = button('Clear Done', partial(uv.mark_done, False), done_row, compact=True)
    clear_done_button.setToolTip(
        'Clear done marks and hide the yellow Originals highlight on selected meshes. This is saved with the scene.')
    for control, action in ((mark_done_button, 'done'), (clear_done_button, 'done_clear')):
        control.setIcon(action_icon(action))
        control.setIconSize(QtCore.QSize(16, 16))
    import uv_straight_checker as checker
    checker_group=QtWidgets.QWidget(window)
    checker_group.setObjectName('displayCheckerGroup')
    layout=QtWidgets.QVBoxLayout(checker_group);layout.setContentsMargins(0,0,0,0);layout.setSpacing(CONTROL_SPACING)
    layout.setAlignment(QtCore.Qt.AlignTop)
    style_row = QtWidgets.QHBoxLayout()
    layout.addLayout(style_row)
    style_row.addWidget(QtWidgets.QLabel('Pattern'))
    checker_style = CheckerPatternComboBox()
    checker_style.setObjectName('checkerStyle')
    checker_style.setMinimumWidth(0)
    checker_style.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Fixed)
    checker_style.setStyleSheet(
        'QComboBox#checkerStyle { padding-right: 28px; }'
        'QComboBox#checkerStyle::drop-down { width: 24px; }'
        'QComboBox#checkerStyle::down-arrow { image: none; }')
    checker_style.setIconSize(QtCore.QSize(24,24))
    style_row.addWidget(checker_style, 1)
    checker_style.setToolTip('Change the checker texture while preserving checker density.')
    def sync_checker_style(style):
        blocker = QtCore.QSignalBlocker(checker_style)
        checker_style.clear()
        for key,label in checker.styles():
            image=checker.style_texture(key)
            icon=QtGui.QIcon(str(image)) if image else category_icon('checker')
            checker_style.addItem(icon,label,key)
        checker_style.setCurrentIndex(checker_style.findData(style))
    window.checker_style_sync = sync_checker_style
    window.checker_style = checker_style
    checker.subscribe_style(sync_checker_style)
    sync_checker_style(checker.preferred_style())
    def change_checker_style(*unused):
        uv._run(lambda: checker.set_style(checker_style.currentData()))
        sync_checker_style(checker.preferred_style())
    checker_style.currentIndexChanged.connect(change_checker_style)
    checker_add=QtWidgets.QPushButton('+');checker_add.setObjectName('checkerAddTexture')
    checker_add.setFixedWidth(28);checker_add.setToolTip('Add a custom texture to the checker library')
    style_row.addWidget(checker_add)
    def add_checker_texture(path):
        style=checker.add_texture(path)
        checker.set_style(style)
        sync_checker_style(style)
        return style
    def choose_checker_texture():
        path,_=QtWidgets.QFileDialog.getOpenFileName(window,'Add Checker','',
            'Textures (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.tga *.exr *.hdr);;All files (*)')
        if path:uv._run(lambda:add_checker_texture(path))
    checker_add.clicked.connect(choose_checker_texture)
    window.add_checker_texture=add_checker_texture
    window.checker_add_button=checker_add
    checker_row = QtWidgets.QHBoxLayout()
    layout.addLayout(checker_row)
    checker_row.addWidget(QtWidgets.QLabel('Tiling'))
    tiles = QtWidgets.QDoubleSpinBox()
    tiles.setRange(1.,4096.)
    tiles.setDecimals(1)
    tiles.setValue(16.)
    checker_row.addWidget(tiles)
    def set_tiles():
        uv._run(lambda: checker.resize(tiles=tiles.value()))
    tiles.editingFinished.connect(set_tiles)
    def step_tiles(factor):
        tiles.setValue(min(4096.,max(1.,tiles.value()*factor)))
        checker.resize(tiles=tiles.value())
    button('÷2', partial(step_tiles,.5), checker_row)
    button('×2', partial(step_tiles,2.), checker_row)
    checker_buttons = QtWidgets.QHBoxLayout()
    layout.addLayout(checker_buttons)
    button('Apply Checker', lambda: checker.apply(tiles.value(), checker_style.currentData()), checker_buttons,compact=True)
    button('Remove Checker', checker.remove, checker_buttons,compact=True)
    pins_group=QtWidgets.QWidget(window)
    pins_group.setObjectName('unwrapPinsGroup')
    pins_layout=QtWidgets.QVBoxLayout(pins_group)
    pins_layout.setContentsMargins(0,0,0,0);pins_layout.setSpacing(CONTROL_SPACING)
    pins_heading=QtWidgets.QHBoxLayout();pins_heading.setSpacing(8)
    pins_heading_label=QtWidgets.QLabel('H / V Pins')
    pins_heading_label.setStyleSheet('color:#b9b9b9;font-size:11px;')
    pins_heading.addWidget(pins_heading_label)
    pins_heading_line=QtWidgets.QFrame();pins_heading_line.setFrameShape(QtWidgets.QFrame.HLine)
    pins_heading_line.setStyleSheet('color:#505050;');pins_heading.addWidget(pins_heading_line,1)
    pins_layout.addLayout(pins_heading)
    layout = pins_layout
    # The wheel handler and unwrap actions still share the saved angle value.
    # It has no visible control in the compact Pins section.
    angle = QtWidgets.QDoubleSpinBox(window)
    angle.setRange(0.0, 45.0)
    angle.setDecimals(1)
    angle.setSingleStep(5.0)
    angle.setSuffix('°')
    angle.setValue(cmds.optionVar(query='uvStraightAutoAngle') if cmds.optionVar(exists='uvStraightAutoAngle') else 15.)
    angle.valueChanged.connect(lambda value: cmds.optionVar(floatValue=('uvStraightAutoAngle', value)))
    angle.hide()
    wheel_feedback = QtWidgets.QLabel('UV Editor: Shift + wheel = Auto H/V angle ±1° on selected shells')
    wheel_feedback.setWordWrap(True)
    # Kept as a hidden message target for the wheel/keymap handlers.
    wheel_feedback.setParent(window)
    wheel_feedback.hide()
    import importlib
    import uv_straight_wheel
    importlib.reload(uv_straight_wheel)
    window.wheel_feedback = wheel_feedback
    import uv_straight_keymaps as keymaps
    importlib.reload(keymaps)
    wheel_feedback.setText('UV Editor: ' + keymaps.text('wheel') + ' = Auto H/V angle ±1°')
    window.angle_wheel = uv_straight_wheel.bind_palette(window, angle, wheel_feedback)
    layout = window.sections['unwrap'].content
    unwrap_rows = [QtWidgets.QHBoxLayout() for _ in range(4)]
    for row in unwrap_rows:layout.addLayout(row)
    quads_button = button('Straighten Quads',
                          lambda: uv.quick_straighten_quads('H', angle.value()), unwrap_rows[0])
    quads_font=quads_button.font()
    quads_font.setPixelSize(10)
    quads_button.setFont(quads_font)
    quads_button.setStyleSheet(quads_button.styleSheet()+'QPushButton { padding-right: 4px; }')
    quads_button.setToolTip(
        'Straighten selected quad grids and suitable strips with triangles or n-gons.\nStrips preserve UV area and receive H/V guides along their long sides.\nStrips then run constrained Unfold; quad grids do not.')
    orient_button = button('Orient Shells', uv.orient_selected_shells, unwrap_rows[1])
    orient_button.setToolTip(
        'Rotate the whole shell to the nearest U or V axis, preserving shape and scale.\nTwo selected UV points define a line; larger selections use selected boundary directions.\nRotation is at most 45 degrees. A single selected UV uses the shell boundary.')
    layout = window.sections['unwrap'].content
    resolution = int(cmds.optionVar(query='uvStraightPackResolution')) if cmds.optionVar(exists='uvStraightPackResolution') else 2048
    spacing = int(cmds.optionVar(query='uvStraightPackSpacing')) if cmds.optionVar(exists='uvStraightPackSpacing') else 40
    unwrap_button = button('Unwrap Mesh',
                           lambda: uv.unwrap_from_edges(angle.value(), resolution=resolution, spacing_px=spacing), unwrap_rows[0])
    unwrap_rows[0].insertWidget(0,unwrap_button)
    unwrap_button.setToolTip(
        'Preserve existing UV seams, cut selected and hard edges, Unfold without pins, then Orient Shells and pack.\nH/V pins are not assigned automatically. Existing pins are removed.')
    unfold_button = button('Unfold', uv.unfold, unwrap_rows[1])
    unfold_button.setToolTip('Unfold the selection. Shift + wheel pins make Unfold affect the entire shell.')
    unwrap_rows[1].insertWidget(0,unfold_button)
    relax_group=QtWidgets.QWidget();relax_group.setObjectName('relaxAxisGroup')
    relax_row=QtWidgets.QHBoxLayout(relax_group);relax_row.setContentsMargins(0,0,0,0);relax_row.setSpacing(2)
    unwrap_rows[2].addWidget(relax_group)
    relax_button=button('Relax', uv.relax, relax_row,compact=True)
    from uv_straight_icons import icon as vector_icon
    relax_button.setIcon(vector_icon('relax'));relax_button.setIconSize(QtCore.QSize(18,18))
    relax_button.setToolTip('Reduce distortion in the selection. Shift + wheel pins make Relax affect the entire shell.')
    window.relax_axis_buttons={}
    for axis in ('H','V'):
        axis_button=button('Relax '+axis,partial(uv.relax_axis,axis),relax_row,compact=True)
        axis_button.setText(axis);axis_button.setObjectName('relaxAxis'+axis);axis_button.setFixedWidth(23)
        axis_button.setToolTip('Relax selected UVs only along '+('U (horizontal)' if axis=='H' else 'V (vertical)')+
                              '. The other coordinate stays fixed. Shift + wheel pins process the entire affected shell; otherwise only selected UVs change.')
        window.relax_axis_buttons[axis]=axis_button
    relax_row.setStretch(0,1)
    for index in range(relax_row.count()):
        control=relax_row.itemAt(index).widget();control.setFixedHeight(26)

        control.setStyleSheet('QPushButton { padding: 0 2px; min-height: 26px; max-height: 26px; border-radius: 8px; }')
        control.setSizePolicy(QtWidgets.QSizePolicy.MinimumExpanding if index==0 else QtWidgets.QSizePolicy.Fixed,
                              QtWidgets.QSizePolicy.Fixed)
    window.relax_group=relax_group
    import uv_straight_move_overlaps as overlaps
    # Refresh before Unwrap binds rotation helpers from an older Maya session.
    importlib.reload(overlaps)
    flip_group=QtWidgets.QWidget();flip_group.setObjectName('unwrapFlipGroup')
    flip_row=QtWidgets.QHBoxLayout(flip_group);flip_row.setContentsMargins(0,0,0,0);flip_row.setSpacing(CONTROL_SPACING)
    unwrap_rows[3].addWidget(flip_group);window.unwrap_flip_buttons={}
    for axis in ('U','V'):
        control=button(axis,partial(overlaps.flip,axis),flip_row,compact=True)
        control.setIcon(vector_icon('flip_'+axis.lower()));control.setIconSize(QtCore.QSize(18,18))
        control.setToolTip('Flip each selected shell along '+axis+' around its center.')
        control.setMinimumWidth(0);control.setFixedHeight(26);control.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Fixed)
        control.setStyleSheet('QPushButton { padding: 0 3px; border-radius: 8px; }')
        flip_row.setStretch(flip_row.count()-1,1);window.unwrap_flip_buttons[axis]=control
    rotation_group=QtWidgets.QWidget();rotation_group.setObjectName('unwrapRotationGroup')
    rotation_row=QtWidgets.QHBoxLayout(rotation_group);rotation_row.setContentsMargins(0,0,0,0);rotation_row.setSpacing(2)
    unwrap_rows[3].addWidget(rotation_group)
    rotation_angle=CompactDensitySpinBox();rotation_angle.setObjectName('shellRotationAngle')
    rotation_angle.setRange(0.,360.);rotation_angle.setDecimals(1);rotation_angle.setSingleStep(15.)
    rotation_angle.setSuffix('°');rotation_angle.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
    rotation_angle.setAlignment(QtCore.Qt.AlignCenter);rotation_angle.setValue(overlaps.rotation_angle())
    rotation_angle.setMinimumWidth(0);rotation_angle.setFixedHeight(26)
    rotation_angle.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Fixed)
    rotation_angle.setToolTip('Shared rotation angle for selected shells about their combined UV-vertex center.')
    rotation_angle.valueChanged.connect(overlaps.set_rotation_angle);rotation_row.addWidget(rotation_angle,1)
    window.shell_rotation_angle=rotation_angle;window.shell_rotation_buttons={}
    for action,sign,title in (('rotate_ccw',1,'Rotate Counterclockwise'),('rotate_cw',-1,'Rotate Clockwise')):
        control=button(title,lambda sign=sign:overlaps.rotate_selected_shells(sign*overlaps.rotation_angle()),rotation_row,compact=True)
        control.setText('');control.setIcon(vector_icon(action));control.setIconSize(QtCore.QSize(22,22))
        control.setFixedSize(26,26);control.setStyleSheet('QPushButton { padding: 0; border-radius: 8px; }')
        control.setToolTip(('Counterclockwise' if sign>0 else 'Clockwise')+' by the specified angle around the combined UV-vertex center of the selected shells.')
        window.shell_rotation_buttons[action]=control
    def edit_rotation_angle():
        value,accepted=QtWidgets.QInputDialog.getDouble(window,'Rotation Angle','Angle (°)',overlaps.rotation_angle(),0.,360.,1)
        if accepted:rotation_angle.setValue(value)
    window.shortcut_actions['rotate_angle']=edit_rotation_angle
    window.shortcut_actions['rotate_shells']=window.shortcut_actions['rotate_cw']
    window.shortcut_actions['flip_shells']=partial(overlaps.flip,'U')
    from uv_straight_stack_ui import StackController
    window.stack_shells=StackController(window,layout,button_layout=unwrap_rows[2])
    window.shortcut_actions['stack_shells']=window.stack_shells.start_or_cancel
    from uv_straight_match_ui import MatchController
    window.match_uvs=MatchController(window,layout)
    window.shortcut_actions['match_uvs']=window.match_uvs.start_or_cancel
    for row in unwrap_rows:
        row.setSpacing(CONTROL_SPACING)
        row.setStretch(0,1);row.setStretch(1,1)
        for index in range(row.count()):
            item=row.itemAt(index).widget()
            if item is not None:
                item.setMinimumWidth(0);item.setFixedHeight(26)
                if isinstance(item,QtWidgets.QPushButton):
                    item.setStyleSheet(item.styleSheet()+'QPushButton { min-height: 16px; max-height: 16px; }')
                item.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Fixed)
    # Group the existing controls without replacing their actions/controllers.
    layout = window.sections['unwrap'].content
    def unwrap_task(title,name):
        group=QtWidgets.QWidget(window);group.setObjectName(name)
        group.setStyleSheet('QWidget#'+name+' {background:#3e3e3e;border-radius:10px;}')
        content=QtWidgets.QVBoxLayout(group)
        content.setContentsMargins(8,7,8,8);content.setSpacing(CONTROL_SPACING)
        heading_row=QtWidgets.QHBoxLayout();heading_row.setSpacing(8)
        heading=QtWidgets.QLabel(title);heading.setStyleSheet('color:#bbbbbb;font-size:11px;')
        heading_row.addWidget(heading)
        line=QtWidgets.QFrame();line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setStyleSheet('color:#505050;');heading_row.addWidget(line,1)
        content.addLayout(heading_row)
        return group,content
    shape_group,shape_layout=unwrap_task('Shape','unwrapShapeGroup')
    shape_grid=QtWidgets.QGridLayout();shape_grid.setSpacing(CONTROL_SPACING)
    shape_grid.setColumnStretch(0,1);shape_grid.setColumnStretch(1,1)
    shape_layout.addLayout(shape_grid)
    for control,row,column in ((unfold_button,0,0),(relax_group,0,1),
                               (quads_button,1,0),(orient_button,1,1)):
        shape_grid.addWidget(control,row,column)
    transform_group,transform_layout=unwrap_task('Transform','unwrapTransformGroup')
    transform_row=QtWidgets.QHBoxLayout();transform_row.setSpacing(CONTROL_SPACING)
    transform_layout.addLayout(transform_row)
    transform_row.addWidget(flip_group,1);transform_row.addWidget(rotation_group,1)
    utilities=QtWidgets.QWidget(window);utilities.setObjectName('unwrapUtilitiesGroup')
    utilities_row=QtWidgets.QHBoxLayout(utilities)
    utilities_row.setContentsMargins(0,0,0,0);utilities_row.setSpacing(CONTROL_SPACING)
    utilities_row.addWidget(window.stack_shells.button,1)
    utilities_row.addWidget(window.match_uvs.button,1)
    for control in (window.stack_shells.button,window.match_uvs.button):
        control.setMinimumWidth(0)
        control.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Fixed)
    # All controls have new owners before the empty old row layouts are removed.
    for row in unwrap_rows:
        layout.removeItem(row)
    layout.insertWidget(0,unwrap_button)
    unwrap_button.setFixedHeight(28)
    layout.insertWidget(1,shape_group)
    layout.insertWidget(2,transform_group)
    layout.insertWidget(3,utilities)
    for row in unwrap_rows:row.deleteLater()
    section=window.sections['unwrap']
    section.content.setContentsMargins(12,10,12,16)
    section.content.setSpacing(8)
    section.pin.setIcon(vector_icon('task_pin'))
    section.pin.toggled.connect(lambda checked=False:section.pin.setIcon(vector_icon('task_pin')))
    def style_unwrap_button(control,action=None):
        if action is not None:
            if hasattr(control,'icon_action'):control.icon_action=action
            else:control.setIcon(vector_icon(action));control.setIconSize(QtCore.QSize(16,16))
        if hasattr(control,'centered_icon'):control.centered_icon=True
        font=control.font();font.setPixelSize(11);control.setFont(font)
        control.setFixedHeight(29)
        control.setStyleSheet(control.styleSheet()+'QPushButton {background:#505050;padding:0 4px;border-radius:8px;min-height:29px;max-height:29px;} QPushButton:hover {background:#606060;}')
        control.update()
    for control,action in ((unwrap_button,None),(unfold_button,'task_unfold'),
            (quads_button,'task_quad'),(orient_button,'task_orient'),
            (relax_button,'task_relax'),(window.stack_shells.button,'task_stack'),
            (window.match_uvs.button,'task_transfer')):
        style_unwrap_button(control,action)
    relax_group.setFixedHeight(29);flip_group.setFixedHeight(29);rotation_group.setFixedHeight(29)
    for control in window.relax_axis_buttons.values():
        style_unwrap_button(control);control.setFixedWidth(27)
    # The original rows ignore minimum sizes. The Shape grid must reserve
    # room for all three Relax controls after their final styling.
    relax_button.ensurePolished()
    relax_button.setMinimumWidth(relax_button.sizeHint().width())
    relax_row.invalidate()
    relax_group.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
    relax_group.setMinimumWidth(relax_row.minimumSize().width())
    for axis,control in window.unwrap_flip_buttons.items():style_unwrap_button(control,'task_flip_'+axis.lower())
    for action,control in window.shell_rotation_buttons.items():
        style_unwrap_button(control,'task_'+action);control.setFixedWidth(27)
    rotation_angle.setFixedHeight(29)
    rotation_angle.setStyleSheet('QDoubleSpinBox {background:#494949;border:1px solid #606060;border-radius:8px;padding:0 4px;min-height:27px;max-height:27px;}')
    layout = window.sections['packing'].content
    from uv_straight_pack_ui import PowerOfTwoSpinBox, build_packing_controls
    build_packing_controls(window, layout, button)
    layout = window.sections['display'].content
    display_pages = {}
    for key in ('view', 'texel', 'checker'):
        page = QtWidgets.QWidget()
        page.setObjectName('displayPage_'+key)
        page_layout = QtWidgets.QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(CONTROL_SPACING)
        page_layout.setAlignment(QtCore.Qt.AlignTop)
        display_pages[key] = page
    layout.addWidget(display_pages['view'])
    divider = QtWidgets.QFrame()
    divider.setFixedHeight(1)
    divider.setStyleSheet('background:#292929;')
    layout.addWidget(divider)
    layout.addWidget(done_group)
    disclosure_row = QtWidgets.QHBoxLayout()
    layout.addLayout(disclosure_row)
    disclosures = {}
    for key, title, action in (('texel', 'Texel', 'density_analyse'), ('checker', 'Checker', 'checker')):
        control = DisplayDisclosureButton(title, action)
        control.setObjectName('displayDisclosure_'+key)
        disclosure_row.addWidget(control, 1)
        disclosures[key] = control
        layout.addWidget(display_pages[key])

    def show_display_details(active):
        for key, control in disclosures.items():
            control.setChecked(key == active)
            display_pages[key].setVisible(key == active)

    def toggle_display_details(checked, key):
        active = key if checked else 'view'
        clear_menu_messages()
        show_display_details(active)
        cmds.optionVar(stringValue=('ssDisplayTab', active))
        if popup and hasattr(window, 'adjust_popup_size'):
            QtCore.QTimer.singleShot(0, window.adjust_popup_size)

    for key, control in disclosures.items():
        control.clicked[bool].connect(partial(toggle_display_details, key=key))
    saved_tab = cmds.optionVar(query='ssDisplayTab') if cmds.optionVar(exists='ssDisplayTab') else 'view'
    show_display_details(saved_tab)
    layout = display_pages['view'].layout()
    display_grid=QtWidgets.QGridLayout();display_grid.setSpacing(CONTROL_SPACING)
    display_grid.setColumnStretch(0,1);display_grid.setColumnStretch(1,1);layout.addLayout(display_grid)
    import uv_straight_native as native
    if not _startup_defaults_applied:
        _startup_defaults_applied = True
        cmds.optionVar(floatValue=('uvStraightSmallShellPixels', 1.))
        cmds.optionVar(intValue=('uvStraightChecksResolution', 4096))
        native.set_wireframe(True)
        native.set_distortion(False)
        if not cmds.about(batch=True):
            try:
                manager = native.enable(open_uv_editor=False)
                if not manager.pins_enabled:
                    native.set_pins(True)
            except (RuntimeError, ValueError) as error:
                cmds.warning('ShellShifter: could not start pin display: ' + str(error))
    wireframe_toggle = VisibleCheckBox('Wireframe')
    wireframe_toggle.setObjectName('wireframeToggle')
    wireframe_toggle.setToolTip('Show or hide wireframe on selected shaded meshes in 3D views.\nUVs, selection and preview colors are preserved.')
    wireframe_toggle.setChecked(native.wireframe_visible())
    display_grid.addWidget(wireframe_toggle,0,0)
    native_toggle = VisibleCheckBox('Pins');native_toggle.setObjectName('showPinsToggle')
    native_toggle.setToolTip('Show H/V pins in the UV Editor.')
    native_toggle.setChecked(native.status().get('pins_enabled', False))
    display_grid.addWidget(native_toggle,0,1)
    distortion_toggle = VisibleCheckBox('Distortion');distortion_toggle.setObjectName('distortionToggle')
    distortion_toggle.setToolTip('Show distortion on selected visible objects in real time. The UV Editor shows selected meshes or shells. Deselect to clear the display.')
    distortion_toggle.setChecked(bool(cmds.optionVar(query='uvStraightDistortion'))
                                 if cmds.optionVar(exists='uvStraightDistortion') else False)
    display_grid.addWidget(distortion_toggle,1,0)
    finished_toggle = VisibleCheckBox('Show Done')
    finished_toggle.setObjectName('finishedUnwrapMeshesToggle')
    finished_toggle.setToolTip('Show green completion colors on marked meshes. Turning this off keeps their marks.')
    display_grid.addWidget(finished_toggle,1,1)

    distortion_legend = DistortionScale()
    distortion_legend.setObjectName('distortionLegend')
    layout.addWidget(distortion_legend)
    distortion_range = distortion_legend
    distortion_range.setValue(float(cmds.optionVar(query='uvStraightDistortionRange'))
                              if cmds.optionVar(exists='uvStraightDistortionRange') else 2.)
    window.distortion_scale=distortion_range
    native_status = QtWidgets.QLabel('Native annotations are off')
    native_status.setWordWrap(True)
    native_status.setParent(window)
    native_status.hide()
    def toggle_wireframe(checked):
        try:
            native.set_wireframe(checked)
        except (RuntimeError, ValueError) as error:
            blocker = QtCore.QSignalBlocker(wireframe_toggle)
            wireframe_toggle.setChecked(native.wireframe_visible())
            del blocker
            native_status.setText(str(error))
    wireframe_toggle.toggled.connect(toggle_wireframe)
    def native_state():
        state = native.status()
        if state.get('error'):
            native_status.setText('Native annotations: ' + state['error'])
        elif state.get('draw_error'):
            native_status.setText('UV drawing error: ' + state['draw_error'])
        elif state.get('enabled') and not state.get('uv_panels'):
            native_status.setText('Open Maya UV Editor to show native colors')
        elif (state.get('enabled') and not state.get('selected_mark_groups') and
              not state.get('distortion_enabled')):
            native_status.setText('Select a mesh with H/V marks')
        elif state.get('draw_calls', 0):
            native_status.setText('Colors are drawing in the native UV Editor')
        elif state.get('uv_calls', 0):
            native_status.setText('UV drawing callback reached; waiting for visible pass')
        elif state.get('can_draw_calls', 0):
            native_status.setText('UV helper detected; waiting for editor redraw')
        elif state['enabled']:
            if not state.get('uv_panels'):
                native_status.setText('Open Maya UV Editor to show native colors')
            elif not state.get('selected_mark_groups') and not state.get('distortion_enabled'):
                native_status.setText('Select a mesh with H/V marks')
            else:
                native_status.setText('Waiting for UV Editor drawing')
        else:
            native_status.setText('Native annotations are off')
    def toggle_native(checked):
        try:
            native.set_pins(checked)
        except Exception as error:
            native_toggle.blockSignals(True)
            native_toggle.setChecked(False)
            native_toggle.blockSignals(False)
            native_status.setText(str(error))
            return
        native_state()
    native_toggle.toggled.connect(toggle_native)
    def update_distortion(*unused):
        try:
            native.set_distortion(distortion_toggle.isChecked(), distortion_range.value())
        except (RuntimeError, ValueError) as error:
            native_status.setText(str(error))
    def update_intensity(value):
        try:
            state=native.display_state()
            if abs(state['distortion_range']-value)>1e-6:
                native.set_distortion(state['distortion_enabled'], value)
        except (RuntimeError, ValueError) as error:
            native_status.setText(str(error))
    distortion_toggle.toggled.connect(update_distortion)
    intensity_timer=QtCore.QTimer(window);intensity_timer.setSingleShot(True);intensity_timer.setInterval(40)
    intensity_timer.timeout.connect(lambda:update_intensity(distortion_range.value()))
    def queue_intensity(*unused):
        if not intensity_timer.isActive():intensity_timer.start()
    def commit_intensity():
        intensity_timer.stop();update_intensity(distortion_range.value())
    distortion_range.valueChanged.connect(queue_intensity)
    distortion_range.sliderReleased.connect(commit_intensity)
    if distortion_toggle.isChecked() and not native.status().get('enabled'):
        update_distortion()
    window.display_controls = DisplayControls(window, native_toggle,
                                               distortion_toggle, distortion_range, wireframe_toggle,
                                               finished_toggle, intensity_timer)

    def toggle_finished(visible):
        uv._run(partial(uv.set_finished_unwrap_visible, visible))
        window.display_controls.refresh_finished()

    finished_toggle.toggled.connect(toggle_finished)
    from uv_straight_colors import finished_visible
    window.shortcut_actions['scene_colors'] = lambda: uv.set_finished_unwrap_visible(not finished_visible())
    if not popup:
        window.status_timer = QtCore.QTimer(window)
        window.status_timer.setInterval(1000)
        window.status_timer.timeout.connect(native_state)
        window.status_timer.start()
    native_state()
    layout = pins_layout
    pin_axis_row = QtWidgets.QHBoxLayout()
    pin_axis_row.setSpacing(6)
    layout.addLayout(pin_axis_row)
    unpin_button=button('Unpin',uv.unpin,pin_axis_row)
    unpin_button.setToolTip('Remove pins from the selection.')
    unpin_button.setMinimumWidth(0)
    unpin_button.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Fixed)
    style_unwrap_button(unpin_button,'task_unpin')
    pin_axis_row.setStretch(0,14)
    window.pin_axis_buttons = {}
    for axis in ('H', 'V'):
        axis_button = QtWidgets.QPushButton(axis)
        axis_button.setObjectName('pinAxis' + axis)
        axis_button.setCheckable(True)
        axis_button.setToolTip(
            'Click an edge to toggle pins.\nH/V: choose axis. Both: auto. Neither: off.\nShift + wheel: pin by angle.')
        axis_button.setStyleSheet(
            'QPushButton { background: #4b4b4b; border-radius: 9px; padding:0; min-height:29px; max-height:29px; }'
            'QPushButton:checked { background: ' + COLORS[axis] +
            '; color: #172027; font-weight: bold; }')
        axis_button.setFixedHeight(29)
        pin_axis_row.addWidget(axis_button, 10)
        window.pin_axis_buttons[axis] = axis_button
    pick_feedback = QtWidgets.QLabel(
        'Click an edge to add/remove it and save the start. Shift + click applies through the end.')
    pick_feedback.setWordWrap(True)
    # Retain the picker's feedback target without adding another visible row.
    pick_feedback.setParent(window)
    pick_feedback.hide()
    import uv_straight_pick
    importlib.reload(uv_straight_pick)
    window.pin_picker = uv_straight_pick.PinSegmentPicker(
        window, None, pick_feedback, window.pin_axis_buttons)
    window.sections['unwrap'].content.addWidget(pins_group)
    layout = display_pages['texel'].layout()
    import uv_straight_texel as texel
    importlib.reload(texel)
    saved_texel_resolution = (int(cmds.optionVar(query='uvStraightTexelResolution'))
                              if cmds.optionVar(exists='uvStraightTexelResolution') else 2048)

    def get_texel_density():
        density_value.setValue(texel.get_density(texel_resolution.value()))

    def set_texel_density():
        texel.set_density(density_value.value(), texel_resolution.value())
        window.texel_display.refresh()

    def build_texel_row():
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(QtWidgets.QLabel('Map Size'))
        resolution = MapSizeSpinBox()
        resolution.setObjectName('texelMapSize')
        resolution.setFixedWidth(64)
        resolution.setValue(saved_texel_resolution)
        row.addWidget(resolution)

        segment = QtWidgets.QHBoxLayout()
        segment.setContentsMargins(0, 0, 0, 0)
        segment.setSpacing(0)
        row.addLayout(segment, 1)
        get = button('Get', get_texel_density, segment)
        get.setToolTip('Get average texel density of selected shells, with equal weight per shell')
        value = CompactDensitySpinBox()
        value.setObjectName('texelDensityValue')
        value.setRange(.0001, 1000000000.)
        value.setDecimals(4)
        value.setValue(500.)
        value.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        value.setAlignment(QtCore.Qt.AlignCenter)
        value.setStyleSheet(
            'QDoubleSpinBox {background:#3d3d3d;border:1px solid #555555;'
            'border-left:0;border-right:0;border-radius:0;padding:0 4px;}'
            'QDoubleSpinBox QLineEdit {background:transparent;border:0;padding:0;}')
        value.setMinimumWidth(0)
        value.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        value.setFixedHeight(28)
        segment.addWidget(value, 1)
        set_button = button('Set', set_texel_density, segment)
        set_button.setToolTip('Set the texel density of selected shells at the current Map Size.')
        for action, side in ((get, 'left'), (set_button, 'right')):
            action.setFixedSize(34, 28)
            action.setStyleSheet(
                'QPushButton {background:#cfe1dc;color:#17292b;border:0;border-radius:0;padding:0;'
                f'border-top-{side}-radius:8px;border-bottom-{side}-radius:8px;'
                '} QPushButton:hover {background:#dfeee9;}'
                'QPushButton:pressed {background:#aecbc2;}'
                'QPushButton:disabled {background:#505754;color:#9baca6;}')
        return row, resolution, value

    unwrap_row, texel_resolution, density_value = build_texel_row()
    display_row, display_resolution, display_value = build_texel_row()
    unwrap_content = window.sections['unwrap'].content
    unwrap_content.insertLayout(unwrap_content.indexOf(utilities), unwrap_row)
    layout.addLayout(display_row)
    texel_resolution.valueChanged.connect(display_resolution.setValue)
    display_resolution.valueChanged.connect(texel_resolution.setValue)
    density_value.valueChanged.connect(display_value.setValue)
    display_value.valueChanged.connect(density_value.setValue)
    saved_texel_resolution = texel_resolution.value()
    cmds.optionVar(intValue=('uvStraightTexelResolution', saved_texel_resolution))
    texel_resolution.valueChanged.connect(
        lambda value: cmds.optionVar(intValue=('uvStraightTexelResolution', value)))
    window.texel_resolution = texel_resolution
    window.texel_density_value = density_value
    texel_legend = TexelDensityScale();texel_legend.setObjectName('texelDensityContrast')
    window.texel_contrast=texel_legend
    layout.addWidget(texel_legend)
    texel_groups_widget = QtWidgets.QWidget()
    texel_groups_layout = QtWidgets.QVBoxLayout(texel_groups_widget)
    texel_groups_layout.setContentsMargins(0, 0, 0, 0)
    texel_groups_layout.setSpacing(4)
    layout.addWidget(texel_groups_widget)
    from uv_straight_texel_ui import TexelDensityController
    window.texel_display = TexelDensityController(window,texel_resolution,texel_groups_layout,texel_legend)
    texel_buttons = QtWidgets.QHBoxLayout()
    layout.addLayout(texel_buttons)
    analyse_button = button('Show Color',window.texel_display.analyse,texel_buttons)
    analyse_button.setToolTip(
        'Show texel density on selected meshes. Colors and groups update after UV edits, geometry changes and Undo/Redo until Clear Color is clicked.')
    button('Clear Color',window.texel_display.clear,texel_buttons).setToolTip(
        'Clear texel-density colors from the viewport.')
    display_pages['checker'].layout().addWidget(checker_group)
    layout = window.sections['checks'].content
    import uv_straight_check_ui
    importlib.reload(uv_straight_check_ui)
    from uv_straight_check_ui import ErrorCheckController, HardEdgeNavigator, ResultLabel
    import uv_straight_checks as checks
    importlib.reload(checks)
    checks_parameters = QtWidgets.QWidget()
    checks_parameters.setObjectName('uvCheckParameters')
    parameters_grid = QtWidgets.QGridLayout(checks_parameters)
    parameters_grid.setContentsMargins(0, 0, 0, 0)
    parameters_grid.setHorizontalSpacing(8)
    parameters_grid.setVerticalSpacing(4)
    parameters_grid.setColumnStretch(0, 1)
    parameters_grid.setColumnStretch(1, 1)
    parameters_grid.addWidget(QtWidgets.QLabel('Min feature size'), 0, 0)
    parameters_grid.addWidget(QtWidgets.QLabel('Map Size'), 0, 1)
    def checks_group(title):
        heading = QtWidgets.QLabel(title)
        heading.setObjectName('uvCheckGroup')
        heading.setContentsMargins(0, 6, 0, 0)
        heading.setStyleSheet('QLabel { color: #aaaaaa; }')
        layout.addWidget(heading)
        return heading
    layout.addWidget(checks_parameters)
    minimum_pixels = QtWidgets.QDoubleSpinBox()
    minimum_pixels.setObjectName('uvCheckMinimumPixels')
    minimum_pixels.setAccessibleName('Minimum feature size in pixels')
    minimum_pixels.setRange(.01, 4096.)
    minimum_pixels.setDecimals(2)
    minimum_pixels.setSingleStep(.5)
    minimum_pixels.setSuffix(' px')
    saved_minimum = (float(cmds.optionVar(query='uvStraightSmallShellPixels'))
                     if cmds.optionVar(exists='uvStraightSmallShellPixels') else 1.)
    minimum_pixels.setValue(saved_minimum)
    minimum_pixels.valueChanged.connect(
        lambda value: cmds.optionVar(floatValue=('uvStraightSmallShellPixels', value)))
    parameters_grid.addWidget(minimum_pixels, 1, 0)
    checks_resolution = PowerOfTwoSpinBox()
    checks_resolution.setObjectName('uvCheckMapSize')
    checks_resolution.setAccessibleName('UV checks map resolution')
    checks_resolution.setRange(512, 4096)
    checks_resolution.setKeyboardTracking(False)
    saved_checks_resolution = (
        int(cmds.optionVar(query='uvStraightChecksResolution'))
        if cmds.optionVar(exists='uvStraightChecksResolution') else saved_texel_resolution)
    checks_resolution.setValue(saved_checks_resolution)
    checks_resolution.valueChanged.connect(
        lambda value: cmds.optionVar(intValue=('uvStraightChecksResolution', value)))
    parameters_grid.addWidget(checks_resolution, 1, 1)
    window.checks_minimum_pixels = minimum_pixels
    window.checks_resolution = checks_resolution
    checks_status = ResultLabel()
    def find_small_shells():
        found = checks.select_small_shells(minimum_pixels.value(),
                                           checks_resolution.value())
        if found:
            checks_status.setText(
                'Selected {} small/thin shell{} · limit {:.2f} px at {} px.'.format(
                    len(found), '' if len(found) == 1 else 's',
                    minimum_pixels.value(), checks_resolution.value()))
        else:
            checks_status.setText(
                'No small or thin shells below {:.2f} px at {} px.'.format(
                    minimum_pixels.value(), checks_resolution.value()))
    small_shell_button = button('Find Small / Thin Shells', find_small_shells)
    small_shell_button.setToolTip(
        'Select whole shells whose overall dimension or local polygon-strip width is below the threshold.')
    layout.addWidget(checks_status)
    zero_area_status = ResultLabel()
    def select_zero_area_shells():
        found = checks.select_zero_area_shells()
        if found:
            zero_area_status.setText('Selected {} zero-area shell{}.'.format(
                len(found), '' if len(found) == 1 else 's'))
        else:
            zero_area_status.setText('No zero-area shells found.')
    zero_area_button = button('Zero-Area Shells', select_zero_area_shells)
    zero_area_button.setToolTip(
        'Select whole UV shells collapsed to a line or point in the current UV set.')
    layout.addWidget(zero_area_status)
    hard_edge_status = ResultLabel()
    window.hard_edge_navigation = HardEdgeNavigator()
    def check_hard_edges():
        window.hard_edge_navigation.clear()
        hard_edge_status.setText('')
        found = checks.show_hard_edges_without_seams(select_edges=False)
        if not found:
            hard_edge_status.setText('No hard edges without UV cuts.')
        window.hard_edge_navigation.load(found)
        if hasattr(window, 'adjust_popup_size'):
            QtCore.QTimer.singleShot(0, window.adjust_popup_size)
    overlap_status = ResultLabel()
    def check_uv_overlaps():
        from uv_straight_move_overlaps import check_overlaps
        try:
            found = check_overlaps()
            overlap_status.setText('Overlapping shells selected: {}'.format(len(found)))
        except Exception as error:
            overlap_status.setText(str(error))
            raise
    hard_edge_button = button('Check Hard Edges', check_hard_edges)
    hard_edge_button.setToolTip(
        'Find hard edges without UV cuts. Use the arrows to frame one edge at a time in the viewport.')
    layout.addWidget(hard_edge_status)
    layout.addWidget(window.hard_edge_navigation)
    checks_group('Overlaps')
    register_button(window,'Select Overlaps',check_uv_overlaps)
    layout.addWidget(overlap_status)
    window.error_checks = ErrorCheckController(window, layout)
    window.error_checks.button.setAccessibleName('Bad Overlaps')
    seam_status = ResultLabel()
    seam_status.setObjectName('butterflySeamResult')
    def fix_butterfly_overlaps():
        from uv_straight_uv_seams import fix_butterfly_overlaps as restore_seams
        try:
            result = restore_seams()
            seam_status.setText(
                ('Restored {} folded edge{} · {} border edges selected.'.format(
                    result.restored_edges, '' if result.restored_edges == 1 else 's',
                    result.selected_edges) if result.restored_edges else
                 'No sewn folds found · {} border edges selected.'.format(result.selected_edges)))
        except Exception as error:
            seam_status.setText(str(error))
            raise
        finally:
            if hasattr(window, 'adjust_popup_size'):
                QtCore.QTimer.singleShot(0, window.adjust_popup_size)
    seam_button = button('Fix Butterfly Overlaps', fix_butterfly_overlaps)
    seam_button.setText('Fix Butterfly')
    seam_button.setAccessibleName('Fix Butterfly Overlaps')
    seam_button.setObjectName('fixButterflyOverlaps')
    seam_button.setToolTip(
        'Restore sewn seams where neighboring UV faces fold onto the same side. Cut and select shell boundaries on selected meshes or fully selected shells. UVs stay in place; current UV sets are used. Supports Undo.')
    layout.addWidget(seam_status)
    checks_group('UV Sets')
    layout = window.sections['overlaps'].content
    move_pad = QtWidgets.QWidget()
    move_pad.setObjectName('overlapMovePad')
    move_pad.setStyleSheet(
        'QWidget#overlapMovePad { background: transparent; border: 0; }'
        'QToolButton#overlapMoveArrow { background: #525252; border: 0; '
        'border-radius: 10px; padding: 0; }'
        'QToolButton#overlapMoveArrow:hover { background: #626262; }'
        'QToolButton#overlapMoveArrow:pressed { background: #424242; }'
        'QPushButton#overlapFlipButton { background: #525252; '
        'border-radius: 10px; padding: 0 5px; }'
        'QPushButton#overlapFlipButton:hover { background: #626262; }'
        'QPushButton#overlapFlipButton:pressed { background: #424242; }')
    move_row = QtWidgets.QHBoxLayout(move_pad)
    move_row.setContentsMargins(0, 0, 0, 0)
    move_row.setSpacing(10)
    left_controls = QtWidgets.QGridLayout()
    left_controls.setContentsMargins(0, 0, 0, 0)
    left_controls.setHorizontalSpacing(6)
    left_controls.setVerticalSpacing(6)
    left_controls.setColumnStretch(1, 1)
    left_controls.setColumnStretch(2, 1)
    for row_index, title in enumerate(('Step', 'Flip')):
        control_label = QtWidgets.QLabel(title)
        control_label.setFixedWidth(28)
        left_controls.addWidget(control_label, row_index, 0)
    move_row.addLayout(left_controls, 1)
    move_step = QtWidgets.QDoubleSpinBox()
    move_step.setObjectName('overlapMoveStep')
    move_step.setRange(.001, 10000.)
    move_step.setDecimals(4)
    move_step.setSingleStep(1.)
    move_step.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
    move_step.setAlignment(QtCore.Qt.AlignCenter)
    move_step.setFixedHeight(28)
    move_step.setMinimumWidth(60)
    move_step.setStyleSheet('QDoubleSpinBox#overlapMoveStep { padding: 0 5px; min-height: 0; }')
    saved_move_step = (float(cmds.optionVar(query='uvStraightOverlapMoveStep'))
                       if cmds.optionVar(exists='uvStraightOverlapMoveStep') else 1.)
    move_step.setValue(saved_move_step)
    move_step.valueChanged.connect(
        lambda value: cmds.optionVar(floatValue=('uvStraightOverlapMoveStep', value)))
    move_step.setToolTip('Distance moved per arrow click, in UV tiles.')
    move_step.setAccessibleName('UV movement step')
    left_controls.addWidget(move_step, 0, 1, 1, 2)
    window.overlap_move_step = move_step
    window.overlap_flip_buttons = {}
    for column, (axis, hint) in enumerate((('U', 'horizontally'), ('V', 'vertically')), 1):
        flip_button = button(axis, partial(overlaps.flip, axis), left_controls)
        left_controls.removeWidget(flip_button)
        left_controls.addWidget(flip_button, 1, column)
        flip_button.setObjectName('overlapFlipButton')
        flip_button.setIcon(vector_icon('flip_'+axis.lower()))
        flip_button.setIconSize(QtCore.QSize(18, 18))
        flip_button.setFixedHeight(28)
        flip_button.setMinimumWidth(40)
        flip_button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        flip_button.setAccessibleName('Flip UV shells along ' + axis)
        flip_button.setToolTip('Flip selected UV shells {} along {}.'.format(hint, axis))
        window.overlap_flip_buttons[axis] = flip_button
    move_grid = QtWidgets.QGridLayout()
    move_grid.setContentsMargins(0, 0, 0, 0)
    move_grid.setHorizontalSpacing(4)
    move_grid.setVerticalSpacing(6)
    move_row.addLayout(move_grid)
    layout.addWidget(move_pad)
    separator = QtWidgets.QFrame()
    separator.setObjectName('overlapSeparator')
    separator.setFixedHeight(1)
    separator.setStyleSheet('QFrame#overlapSeparator { background: #515151; border: 0; }')
    original_status = QtWidgets.QLabel()
    original_status.setObjectName('overlapOriginalCount')
    original_status.setMinimumWidth(0)
    original_status.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
    original_buttons = QtWidgets.QHBoxLayout()
    original_buttons.setContentsMargins(0, 0, 0, 0)
    original_buttons.setSpacing(6)
    original_buttons.addWidget(original_status, 1)
    def update_original_status():
        count = overlaps.original_count()
        original_status.setText('Originals {}'.format(count))
        original_status.setToolTip('Assigned originals: {} UV shells'.format(count))
        with signals_blocked(show_original_color):
            show_original_color.setChecked(overlaps.original_color_visible())
    def set_overlap_originals():
        overlaps.set_originals_from_selection()
        update_original_status()
    def clear_overlap_originals():
        overlaps.clear_originals_from_selection()
        update_original_status()
    set_original_button = button('Set Selection as Original', set_overlap_originals, original_buttons)
    set_original_button.setText('Set Selected')
    set_original_button.setObjectName('overlapSetOriginals')
    set_original_button.setToolTip('Add selected meshes or UV shells to the originals.')
    clear_original_button = button('Clear Originals', clear_overlap_originals, original_buttons)
    clear_original_button.setText('Clear')
    clear_original_button.setObjectName('overlapClearOriginals')
    clear_original_button.setToolTip('Remove only selected meshes or UV shells from the originals.')
    for control in (set_original_button, clear_original_button):
        control.setFixedHeight(28)
        control.setStyleSheet('QPushButton { padding: 0 6px; min-height: 0; border-radius: 10px; }')
    show_original_color = QtWidgets.QToolButton()
    show_original_color.setObjectName('overlapShowOriginals')
    show_original_color.setCheckable(True)
    show_original_color.setIcon(overlap_action_icon('eye'))
    show_original_color.setIconSize(QtCore.QSize(18, 18))
    show_original_color.setFixedSize(28, 28)
    show_original_color.setAccessibleName('Show Originals Color')
    show_original_color.setToolTip('Show assigned originals in yellow in the model viewport.')
    show_original_color.setStyleSheet(
        'QToolButton#overlapShowOriginals { background: #525252; border-radius: 10px; padding: 0; }'
        'QToolButton#overlapShowOriginals:hover { background: #626262; }'
        'QToolButton#overlapShowOriginals:checked { background: #707070; }')
    original_buttons.addWidget(show_original_color)
    def set_overlap_original_color(enabled):
        try:
            overlaps.set_original_color_visible(enabled)
        finally:
            update_original_status()
    show_original_color.clicked.connect(lambda checked: uv._run(lambda: set_overlap_original_color(checked)))
    window.overlap_show_original_color = show_original_color
    window.overlap_refresh = update_original_status
    update_original_status()
    # Constructing another palette must not replace a running display layer.
    if not native.status().get('enabled'):
        overlaps.refresh_original_colors()
    output = QtWidgets.QLabel()
    output.setObjectName('overlapResult')
    output.setWordWrap(True)
    output.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    output.hide()
    def show_overlap_result(text):
        output.setText(text)
        output.setVisible(bool(text))
        if hasattr(window, 'adjust_popup_size'):
            QtCore.QTimer.singleShot(0, window.adjust_popup_size)
    def analyse_overlaps():
        try:
            plan = overlaps.analyse()
            show_overlap_result(overlaps._summary(plan))
        except Exception as error:
            show_overlap_result(str(error))
            raise
    def move_selected(unit):
        offset = (unit[0]*move_step.value(), unit[1]*move_step.value())
        count = overlaps.move_selected_shells(offset)
        show_overlap_result('Moved {} selected shell{} by U {:.4f}, V {:.4f}.'.format(
            count, '' if count == 1 else 's', offset[0], offset[1]))
    arrow_specs = {
        'up': ((0., 1.), 0, 1), 'left': ((-1., 0.), 1, 0),
        'down': ((0., -1.), 1, 1), 'right': ((1., 0.), 1, 2)}
    window.overlap_move_arrows = {}
    for direction, (unit, row_index, column_index) in arrow_specs.items():
        arrow = QtWidgets.QToolButton()
        arrow.setObjectName('overlapMoveArrow')
        arrow.setIcon(move_arrow_icon(direction))
        arrow.setIconSize(QtCore.QSize(18, 18))
        arrow.setFixedSize(28, 28)
        arrow.setAccessibleName('Move selected shells ' + direction)
        arrow.setToolTip('Move all selected UV shells {} by Step.'.format(
            {'up': 'up', 'down': 'down', 'left': 'left', 'right': 'right'}[direction]))
        arrow.setProperty('moveDirection', direction)
        arrow.clicked.connect(lambda checked=False, unit=unit: uv._run(lambda: move_selected(unit)))
        window.shortcut_actions['move_' + direction] = partial(move_selected, unit)
        move_grid.addWidget(arrow, row_index, column_index, QtCore.Qt.AlignCenter)
        window.overlap_move_arrows[direction] = arrow
    analyse_button = button('Analyse Select', analyse_overlaps)
    analyse_button.setObjectName('overlapAnalyse')
    analyse_button.setIcon(overlap_action_icon('analyse'))
    analyse_button.setIconSize(QtCore.QSize(16, 16))
    analyse_button.setFixedHeight(28)
    analyse_button.setToolTip('Find overlapping copies only among selected UV shells, in any tiles.\nOne source remains in each stack; flipped copies are selected first.\nSelecting a mesh or group checks all its shells. UVs are not moved.')
    def pick_selection_tile():
        import uv_straight_pack_target as target
        shapes = overlaps._selected_mesh_shapes()
        def select_tile(u,v):
            count = overlaps.select_tile((u,v),shapes)
            show_overlap_result('Selected {} shell{} in tile ({}, {}).'.format(
                count,'' if count==1 else 's',u,v))
        target.choose_tile(lambda u,v:uv._run(lambda:select_tile(u,v)))
    tile_button = button('Select in Tile',pick_selection_tile,compact=True)
    tile_button.setObjectName('overlapSelectTile')
    tile_button.setToolTip('Select whole visible shells inside a tile on selected meshes or in a group.\nPoint at a tile in the UV Editor and left-click. Esc or right-click cancels.\nShells crossing tile boundaries are not selected.')
    layout.addWidget(output)
    layout.addWidget(separator)
    layout.addLayout(original_buttons)
    import uv_straight_uv_merge as uv_sets
    import uv_straight_uv_merge_command as uv_merge_command
    import uv_straight_uv_set_ui as uv_set_ui
    importlib.reload(uv_sets)
    importlib.reload(uv_merge_command)
    importlib.reload(uv_set_ui)
    window.uv_sets = uv_set_ui.UVSetController(
        window, window.sections['checks'].content, button_text='Cleanup')
    import uv_straight_menu as plugin_menu
    window.shortcut_actions.update({
        # Keep saved command IDs callable after removing their palette buttons.
        'mark_h': partial(uv.mark,'H'), 'mark_v': partial(uv.mark,'V'),
        'auto_hv': lambda: uv.auto_mark(plugin_menu.angle(), whole_shells=True),
        'pick_toggle': plugin_menu.toggle_picker,
        'pick_auto': partial(plugin_menu.set_picker_mode, None),
        'pick_h': partial(plugin_menu.set_picker_mode, 'H'),
        'pick_v': partial(plugin_menu.set_picker_mode, 'V'),
        'pick_axes_auto': plugin_menu.toggle_picker_auto_axes,
        'checker_gray': lambda: checker.apply(tiles.value(), 'gray'),
        'checker_color': lambda: checker.apply(tiles.value(), 'color'),
        'wireframe': lambda: native.set_wireframe(not native.wireframe_visible()),
        'show_pins': lambda: native.set_pins(not native.display_state()['pins_enabled']),
        'distortion': lambda: native.set_distortion(not native.display_state()['distortion_enabled'],
                                                   native.display_state()['distortion_range']),
        'original_color': lambda: set_overlap_original_color(not overlaps.original_color_visible()),
        'bad_overlaps': window.error_checks.start_or_cancel,
        'uv_cleanup': window.uv_sets.start_or_cancel,
    })
    from uv_straight_material_ui import MaterialController
    window.materials = MaterialController(window, window.sections['materials'].content)
    from uv_straight_updates import UpdateController
    window.updates = UpdateController(window, window.sections['updates'].content)
    def section_action(section, callback):
        if not window.isVisible():
            if not popup and cmds.workspaceControl(DOCK_CONTROL, query=True, exists=True):
                cmds.workspaceControl(DOCK_CONTROL, edit=True, restore=True)
            else:
                QtWidgets.QWidget.setVisible(window, True)
        select_category(section)
        callback()

    transfer_checkbox = window.packing_controls['transfer']
    textures = window.materials.textures
    window.shortcut_actions.update({
        'pin_axis_h': window.pin_axis_buttons['H'].click,
        'pin_axis_v': window.pin_axis_buttons['V'].click,
        'texel_groups': partial(section_action, 'packing', window.uv_group_buttons['texel'].click),
        'shell_groups': partial(section_action, 'packing', window.uv_group_buttons['pack'].click),
        'texture_transfer': partial(section_action, 'packing', transfer_checkbox.click),
        'transfer_connect': partial(section_action, 'packing', window.texture_transfer.button.click),
        'display_texel': partial(section_action, 'display', disclosures['texel'].click),
        'display_checker': partial(section_action, 'display', disclosures['checker'].click),
        'material_assign': partial(section_action, 'materials', textures.import_button.click),
        'material_clear': partial(section_action, 'materials', textures.clear_button.click),
        'material_convert': partial(section_action, 'materials', textures.convert_button.click),
        'material_view': partial(section_action, 'materials', textures.view_button.click),
        'material_refresh': partial(section_action, 'materials', window.materials.refresh_button.click),
        'check_updates': window.updates.button.click,
    })
    keymaps.build_ui(window.sections['settings'].content, window)
    ui_language.apply(window)
    if popup:
        window.setWindowFlags(window.transient_popup_type | QtCore.Qt.FramelessWindowHint |
                              QtCore.Qt.NoDropShadowWindowHint)
        window.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        cursor = QtGui.QCursor.pos()
        screen = QtGui.QGuiApplication.screenAt(cursor)
        if screen is None:
            screen = QtGui.QGuiApplication.primaryScreen()
        width = 340
        anchor = cursor + QtCore.QPoint(10, 10)
        window.popup_positioned = False
        def adjust_popup_size():
            from shiboken2 import isValid
            if not isValid(window):
                return
            visible = [section for section in window.sections.values()
                       if not section.isHidden()]
            for section in visible:
                section.ensurePolished()
                section.layout().activate()
                section.body.layout().activate()
            content_height = sum(section.sizeHint().height()
                                 for section in visible)
            if len(visible) > 1:
                content_height += stack.spacing() * (len(visible)-1)
            buttons = list(window.category_buttons.values())
            rail_height = (sum(button.height() for button in buttons) +
                           rail.spacing() * max(0, len(buttons)-1))
            position = window.pos() if window.popup_positioned else anchor
            current_screen = (window.screen() if window.popup_positioned else screen)
            available = current_screen.availableGeometry()
            margins = root.contentsMargins()
            desired = (max(content_height, rail_height) + margins.top() + margins.bottom() +
                       window.popup_titlebar.height() + root.spacing())
            height = min(max(220, desired), max(220, available.height()-24))
            window.resize(width, height)
            window.move_popup(position, available.center())
            window.popup_positioned = True
        window.adjust_popup_size = adjust_popup_size
        adjust_popup_size()
        window.show()
        window.raise_()
        window.activateWindow()
        QtCore.QTimer.singleShot(0, adjust_popup_size)
    elif cmds.about(batch=True):
        # Maya standalone has no main-window pointer for the mixin to parent to.
        QtWidgets.QWidget.setVisible(window, True)
    else:
        window.show(dockable=True, area='right', floating=False, retain=False,
                    width=340, minWidth=300, height=720, closeCallback=window.stop)
        if cmds.workspaceControl(UV_TOOLKIT_CONTROL, query=True, exists=True):
            cmds.workspaceControl(UV_TOOLKIT_CONTROL, edit=True, visible=True,
                                  collapse=False)
            cmds.workspaceControl(DOCK_CONTROL, edit=True,
                                  tabToControl=(UV_TOOLKIT_CONTROL, -1),
                                  label='ShellShifter')
        cmds.workspaceControl(DOCK_CONTROL, edit=True, restore=True)
    return window


def toggle_popup():
    """Toggle the complete tool palette next to the mouse cursor."""
    global _popup
    from shiboken2 import isValid
    if _popup is not None and isValid(_popup) and _popup.isVisible():
        _popup.close()
        _popup = None
        return None
    _popup = show_window(popup=True)
    return _popup
