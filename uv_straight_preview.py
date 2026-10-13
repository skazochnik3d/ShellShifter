"""Tool UI, color preview and optional native UV-editor annotations."""
try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets
import maya.cmds as cmds
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
from uv_straight_changes import ChangeTracker

COLORS = {'H': '#ff963b', 'V': '#38c9ff'}
CONTROL_SPACING = 6
DOCK_CONTROL = 'uvStraightQtWindowWorkspaceControl'
UV_TOOLKIT_CONTROL = 'UVToolkitDockControl'
POPUP_OBJECT = 'uvStraightQtPopup'
_popup = globals().get('_popup')
SECTION_DEFINITIONS = (
    ('unwrap', 'Unwrap', True), ('packing', 'Packing', False), ('pins', 'H / V Pins', True),
    ('align', 'Orient && Straighten', False), ('checker', 'Checker', False),
    ('materials', 'Materials', False),
    ('display', 'Display', False), ('texel', 'Texel Density', False),
    ('checks', 'UV Checks', False), ('done', 'Finished Meshes', False),
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
        return (event.globalPosition().toPoint()
                if hasattr(event, 'globalPosition') else event.globalPos())

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
        self.setToolTip('H: orange | V: cyan | shared corner: white\nWheel: zoom. Drag: pan. Refresh: frame all.')
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
        return icon('pack' if key == 'packing' else 'wrench')
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


def flip_axis_icon(axis):
    """Solid and dashed halves around the axis being mirrored."""
    pixmap = QtGui.QPixmap(48, 48)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.scale(2., 2.)
    painter.translate(12., 12.)
    if axis == 'V':
        painter.rotate(90.)
    pen = QtGui.QPen(QtGui.QColor('#9bc7e5'), 1.5)
    pen.setCapStyle(QtCore.Qt.RoundCap)
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    painter.setPen(pen)
    painter.drawRoundedRect(QtCore.QRectF(-8., -7., 6., 14.), 1., 1.)
    pen.setStyle(QtCore.Qt.DashLine)
    pen.setDashPattern([2., 2.])
    painter.setPen(pen)
    painter.drawRoundedRect(QtCore.QRectF(2., -7., 6., 14.), 1., 1.)
    painter.drawLine(QtCore.QPointF(0., -9.), QtCore.QPointF(0., 9.))
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
        self.pin.setToolTip('Pin section open')
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
        self.pin.setToolTip('Unpin section' if pinned else 'Pin section open')
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
    def __init__(self, window, pins, distortion, intensity, wireframe):
        super().__init__(window)
        import uv_straight_native as native
        self.pins, self.distortion, self.intensity = pins, distortion, intensity
        self.wireframe = wireframe
        self.state = native._display_state
        self.state.changed.connect(self.sync)
        import maya.api.OpenMaya as om
        self.callbacks = [om.MEventMessage.addEventCallback(event, self.refresh)
                          for event in ('DisplayPreferenceChanged',)]
        window.destroyed.connect(self.stop)
        self.sync(native.display_state())

    def refresh(self, *unused):
        import uv_straight_native as native
        self.sync(native.display_state())

    @QtCore.Slot(dict)
    def sync(self, state):
        for widget, value in ((self.wireframe, state['wireframe_enabled']),
                              (self.pins, state['pins_enabled']),
                              (self.distortion, state['distortion_enabled']),
                              (self.intensity, state['distortion_range'])):
            blocker = QtCore.QSignalBlocker(widget)
            if widget is self.intensity:
                widget.setValue(value)
            else:
                widget.setChecked(value)
            del blocker

    def stop(self, *unused):
        import maya.api.OpenMaya as om
        for callback in self.callbacks:
            om.MMessage.removeCallback(callback)
        self.callbacks = []
        if getattr(self, '_state_connected', True):
            self._state_connected = False
            try:
                self.state.changed.disconnect(self.sync)
            except (RuntimeError, TypeError):
                pass


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


class PopupTitleBar(QtWidgets.QWidget):
    """Move the palette directly, without a nested native drag loop."""
    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self.origin = self.window_origin = None
        self.dragging = False
        self.setObjectName('popupTitleBar')
        self.setFixedHeight(26)
        self.setCursor(QtCore.Qt.OpenHandCursor)
        self.setToolTip('Drag to move the window')
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(8, 0, 0, 0)
        row.setSpacing(2)
        title = QtWidgets.QLabel('ShellShifter')
        title.setStyleSheet('color: #aaaaaa; background: transparent;')
        title.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        row.addWidget(title, 1)
        window.popup_pin = QtWidgets.QToolButton(self)
        window.popup_pin.setObjectName('popupWindowPin')
        window.popup_pin.setCheckable(True)
        window.popup_pin.setFixedSize(24, 24)
        window.popup_pin.setIconSize(QtCore.QSize(18, 18))
        window.popup_pin.setIcon(section_pin_icon(False))
        window.popup_pin.setToolTip('Pin window: keep it open while working in Maya')
        window.popup_pin.setAccessibleName('Pin popup window')
        window.popup_pin.setCursor(QtCore.Qt.PointingHandCursor)
        window.popup_pin.toggled.connect(window.set_popup_pinned)
        row.addWidget(window.popup_pin)
        window.popup_close = QtWidgets.QToolButton(self)
        window.popup_close.setObjectName('popupWindowClose')
        window.popup_close.setText('×')
        window.popup_close.setFixedSize(24, 24)
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
        self.transient_popup_type = QtCore.Qt.Tool if cmds.about(batch=True) else QtCore.Qt.Popup

    def set_popup_pinned(self, pinned):
        pinned = bool(pinned)
        if pinned == self.popup_pinned:
            return
        position, size, visible = self.pos(), self.size(), self.isVisible()
        self.popup_pinned = pinned
        with QtCore.QSignalBlocker(self.popup_pin):
            self.popup_pin.setChecked(pinned)
        self.popup_pin.setIcon(section_pin_icon(pinned))
        self.popup_pin.setToolTip('Unpin window: close on outside click' if pinned else
                                  'Pin window: keep it open while working in Maya')
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
        if self.popup_pinned and event.key() == QtCore.Qt.Key_Escape:
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


def show_window(popup=False):
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
        from shiboken6 import wrapInstance
        pointer = omui.MQtUtil.mainWindow()
        if pointer:
            parent = wrapInstance(int(pointer), QtWidgets.QWidget)
    window = (PopupWindow if popup else ToolWindow)(parent=parent)
    window.popup_mode = bool(popup)
    window.setObjectName(object_name)
    window.setWindowTitle('ShellShifter ' + uv.VERSION + ' | Maya ' + cmds.about(version=True))
    window.resize(340, 720)
    window.setMinimumWidth(300)
    window.setStyleSheet(
        'QDialog, QWidget#categoryContents { background: #303030; }'
        'QFrame[categoryCard="true"] { background: #383838; border: 0; border-radius: 14px; }'
        'QFrame[categoryCard="true"][dragSource="true"] { background: #414b55; border: 1px solid #a7cbed; }'
        'QWidget#sectionBody { background: transparent; }'
        'QDoubleSpinBox, QSpinBox, QComboBox, QLineEdit { background: #474747; color: #eeeeee; '
        'border: 1px solid #555555; border-radius: 7px; padding: 4px 6px; min-height: 18px; }'
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
        icon.setToolTip(title.replace('&&', '&'))
        icon.setAccessibleName(title.replace('&&', '&'))
        rail.addWidget(icon)
        window.category_buttons[key] = icon
    rail.addStretch()
    stack.addStretch()
    default_order = [item[0] for item in SECTION_DEFINITIONS]
    saved_order = (str(cmds.optionVar(query='uvStraightPinnedOrder')).split(',')
                   if cmds.optionVar(exists='uvStraightPinnedOrder') else [])
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
    def select_category(key):
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
    select_category(saved_category if saved_category in window.sections or saved_category == '' else 'unwrap')
    layout = window.sections['done'].content
    from uv_straight_actions import register_button
    window.shortcut_actions = {}
    def label(text):
        item = QtWidgets.QLabel(text)
        item.setWordWrap(True)
        layout.addWidget(item)
    def button(text, callback, target=None, color=None):
        from uv_straight_icons import BUTTONS, ActionButton
        target = layout if target is None else target
        item = (ActionButton(text, BUTTONS[text], dark=bool(color))
                if text in BUTTONS else QtWidgets.QPushButton(text))
        item.clicked.connect(partial(uv._run, callback))
        register_button(window, text, callback)
        if color:
            item.setStyleSheet(item.styleSheet() + 'QPushButton { background: ' + color + '; color: #111820; font-weight: bold; }')
        target.addWidget(item)
        return item
    done_row = QtWidgets.QHBoxLayout()
    layout.addLayout(done_row)
    button('Mark Done', partial(uv.mark_done, True), done_row, '#42cd70')
    button('Clear Done', partial(uv.mark_done, False), done_row)
    button('Restore Scene Colors', uv.restore_done_colors)
    import uv_straight_checker as checker
    layout = window.sections['checker'].content
    style_row = QtWidgets.QHBoxLayout()
    layout.addLayout(style_row)
    style_row.addWidget(QtWidgets.QLabel('Pattern'))
    checker_style = CheckerPatternComboBox()
    checker_style.setObjectName('checkerStyle')
    checker_style.setStyleSheet(
        'QComboBox#checkerStyle { padding-right: 28px; }'
        'QComboBox#checkerStyle::drop-down { width: 24px; }'
        'QComboBox#checkerStyle::down-arrow { image: none; }')
    checker_style.setIconSize(QtCore.QSize(24,24))
    for style, label in checker.STYLES:
        icon = (category_icon('checker') if style == 'gray'
                else QtGui.QIcon(str(checker.COLOR_TEXTURE)))
        checker_style.addItem(icon, label, style)
    style_row.addWidget(checker_style, 1)
    checker_style.setToolTip('Switch the selected checker while preserving its square density.')
    def sync_checker_style(style):
        blocker = QtCore.QSignalBlocker(checker_style)
        checker_style.setCurrentIndex(checker_style.findData(style))
    window.checker_style_sync = sync_checker_style
    window.checker_style = checker_style
    checker.subscribe_style(sync_checker_style)
    sync_checker_style(checker.preferred_style())
    def change_checker_style(*unused):
        uv._run(lambda: checker.set_style(checker_style.currentData()))
        sync_checker_style(checker.preferred_style())
    checker_style.currentIndexChanged.connect(change_checker_style)
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
    button('Apply Checker', lambda: checker.apply(tiles.value(), checker_style.currentData()), checker_buttons)
    button('Remove Checker', checker.remove, checker_buttons)
    layout = window.sections['pins'].content
    row = QtWidgets.QHBoxLayout()
    layout.addLayout(row)
    button('Mark H', partial(uv.mark, 'H'), row, COLORS['H'])
    button('Mark V', partial(uv.mark, 'V'), row, COLORS['V'])
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
    if not popup:
        window.angle_wheel = uv_straight_wheel.AngleWheel(window, angle, wheel_feedback)
    layout = window.sections['align'].content
    quads_button = button('Straighten Quads',
                          lambda: uv.quick_straighten_quads('H', angle.value()))
    quads_button.setToolTip(
        'Straighten selected quad grids and suitable ribbons with triangles or n-gons.\n'
        'Ribbons preserve UV area and get H/V guides along their long side(s).\n'
        'Ribbons run constrained Unfold after assigning guides. Quad grids do not.')
    orient_button = button('Orient Shells', uv.orient_selected_shells)
    orient_button.setToolTip(
        'Rotate the whole shell to the nearest U or V axis, preserving its shape and scale.\n'
        'Two selected UV points define a line; more points fit the selected contour directions.\n'
        'Rotation is at most 45 degrees. A single selected UV uses the shell border.')
    layout = window.sections['unwrap'].content
    resolution = int(cmds.optionVar(query='uvStraightPackResolution')) if cmds.optionVar(exists='uvStraightPackResolution') else 2048
    spacing = int(cmds.optionVar(query='uvStraightPackSpacing')) if cmds.optionVar(exists='uvStraightPackSpacing') else 40
    unwrap_button = button('Unwrap Mesh',
                           lambda: uv.unwrap_from_edges(angle.value(), resolution=resolution, spacing_px=spacing))
    unwrap_button.setToolTip(
        'Cut selected edges and hard edges, unfold without pins, then Orient Shells and pack.\n'
        'H/V pins are not assigned automatically. Existing pins are cleared.')
    button('Unfold', uv.unfold)
    button('Relax', uv.relax)
    layout = window.sections['packing'].content
    from uv_straight_pack_ui import build_packing_controls
    build_packing_controls(window, layout, button)
    layout = window.sections['display'].content
    import uv_straight_native as native
    wireframe_toggle = VisibleCheckBox('Wireframe')
    wireframe_toggle.setObjectName('wireframeToggle')
    wireframe_toggle.setToolTip('Show or hide the wireframe on selected shaded meshes in 3D views.\nKeeps UVs, selection and preview colors unchanged.')
    wireframe_toggle.setChecked(native.wireframe_visible())
    layout.addWidget(wireframe_toggle)
    native_toggle = VisibleCheckBox('Show H/V Pins')
    native_toggle.setToolTip('Show H/V pins in the UV Editor.')
    native_toggle.setChecked(native.status().get('pins_enabled', False))
    layout.addWidget(native_toggle)
    distortion_toggle = VisibleCheckBox('Show Distortion')
    distortion_toggle.setToolTip('Live distortion on selected visible objects. UV Editors show selected meshes or shells. Deselect to clear the display.')
    distortion_toggle.setChecked(bool(cmds.optionVar(query='uvStraightDistortion'))
                                 if cmds.optionVar(exists='uvStraightDistortion') else False)
    layout.addWidget(distortion_toggle)
    distortion_legend = QtWidgets.QLabel('Stretch     Neutral     Compression')
    distortion_legend.setAlignment(QtCore.Qt.AlignCenter)
    distortion_legend.setFixedHeight(20)
    distortion_legend.setStyleSheet(
        'QLabel { color: white; border: 1px solid #555; '
        'background: qlineargradient(x1:0,y1:0,x2:1,y2:0, '
        'stop:0 #ff1408, stop:0.5 #777777, stop:1 #1452ff); }')
    layout.addWidget(distortion_legend)
    distortion_row = QtWidgets.QHBoxLayout()
    layout.addLayout(distortion_row)
    distortion_label = QtWidgets.QLabel('Color intensity')
    distortion_label.setToolTip('Display brightness only. The distortion scale is identical for every mesh and scene.')
    distortion_row.addWidget(distortion_label)
    distortion_range = QtWidgets.QDoubleSpinBox()
    distortion_range.setRange(.1, 4.)
    distortion_range.setDecimals(1)
    distortion_range.setSingleStep(.25)
    distortion_range.setSuffix('×')
    distortion_range.setValue(float(cmds.optionVar(query='uvStraightDistortionRange'))
                              if cmds.optionVar(exists='uvStraightDistortionRange') else 2.)
    distortion_row.addWidget(distortion_range)
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
            native.set_distortion(native.display_state()['distortion_enabled'], value)
        except (RuntimeError, ValueError) as error:
            native_status.setText(str(error))
    distortion_toggle.toggled.connect(update_distortion)
    distortion_range.valueChanged.connect(update_intensity)
    if distortion_toggle.isChecked() and not native.status().get('enabled'):
        update_distortion()
    window.display_controls = DisplayControls(window, native_toggle,
                                               distortion_toggle, distortion_range, wireframe_toggle)
    if not popup:
        window.status_timer = QtCore.QTimer(window)
        window.status_timer.setInterval(1000)
        window.status_timer.timeout.connect(native_state)
        window.status_timer.start()
    native_state()
    layout = window.sections['pins'].content
    button('Unpin', uv.unpin)
    from uv_straight_icons import ActionButton
    pick_button = ActionButton('Edit Pins', 'pick')
    pick_button.setCheckable(True)
    pick_button.setStyleSheet(pick_button.styleSheet() + 'QPushButton:checked { background: #84502c; }')
    layout.addWidget(pick_button)
    pin_axis_row = QtWidgets.QHBoxLayout()
    pin_axis_row.setSpacing(6)
    layout.addLayout(pin_axis_row)
    window.pin_axis_buttons = {}
    for axis in ('H', 'V'):
        axis_button = QtWidgets.QPushButton(axis)
        axis_button.setObjectName('pinAxis' + axis)
        axis_button.setCheckable(True)
        axis_button.setToolTip(
            'Force clicked segments to be added as {} pins. Click again for automatic direction.'.format(axis))
        axis_button.setStyleSheet(
            'QPushButton { background: #4b4b4b; border-radius: 9px; }'
            'QPushButton:checked { background: ' + COLORS[axis] +
            '; color: #172027; font-weight: bold; }')
        pin_axis_row.addWidget(axis_button, 1)
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
        window, pick_button, pick_feedback, window.pin_axis_buttons)
    layout = window.sections['texel'].content
    import uv_straight_texel as texel
    importlib.reload(texel)
    density_row = QtWidgets.QHBoxLayout()
    layout.addLayout(density_row)
    density_get = button('Get', lambda: density_value.setValue(
        texel.get_density(texel_resolution.value())), density_row)
    density_get.setToolTip('Get the average texel density of selected shells (equal weight per shell)')
    density_value = QtWidgets.QDoubleSpinBox()
    density_value.setRange(.0001, 1000000000.)
    density_value.setDecimals(4)
    density_value.setValue(10.24)
    density_value.setSuffix(' px/unit')
    density_row.addWidget(density_value, 1)
    def set_texel_density():
        texel.set_density(density_value.value(), texel_resolution.value())
        window.texel_display.refresh()
    button('Set', set_texel_density, density_row)
    texel_row = QtWidgets.QHBoxLayout()
    layout.addLayout(texel_row)
    texel_row.addWidget(QtWidgets.QLabel('Map Size'))
    texel_resolution = QtWidgets.QSpinBox()
    texel_resolution.setRange(16, 32768)
    texel_resolution.setSingleStep(512)
    saved_texel_resolution = (int(cmds.optionVar(query='uvStraightTexelResolution'))
                              if cmds.optionVar(exists='uvStraightTexelResolution') else 2048)
    texel_resolution.setValue(saved_texel_resolution)
    texel_resolution.valueChanged.connect(
        lambda value: cmds.optionVar(intValue=('uvStraightTexelResolution', value)))
    texel_row.addWidget(texel_resolution, 1)
    texel_legend = QtWidgets.QLabel('Low density                                      High density')
    texel_legend.setAlignment(QtCore.Qt.AlignCenter)
    texel_legend.setFixedHeight(20)
    texel_legend.setStyleSheet(
        'QLabel { color: white; border-radius: 4px; '
        'background: qlineargradient(x1:0,y1:0,x2:1,y2:0, '
        'stop:0 #123cff, stop:0.25 #00dfff, stop:0.5 #35ed42, '
        'stop:0.75 #ffe100, stop:1 #ff3b0a); }')
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
    analyse_button = button('Analyse && Show',window.texel_display.analyse,texel_buttons)
    analyse_button.setToolTip(
        'Show texel density on selected meshes. Colors and groups refresh after UV edits, '
        'geometry changes and Undo/Redo until Clear is pressed.')
    button('Clear',window.texel_display.clear,texel_buttons)
    layout = window.sections['checks'].content
    import uv_straight_check_ui
    importlib.reload(uv_straight_check_ui)
    from uv_straight_check_ui import ErrorCheckController, ResultLabel
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
    checks_group('Shells & Edges')
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
    checks_resolution = QtWidgets.QSpinBox()
    checks_resolution.setObjectName('uvCheckMapSize')
    checks_resolution.setAccessibleName('UV checks map resolution')
    checks_resolution.setRange(16, 32768)
    checks_resolution.setSingleStep(512)
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
        'Select whole shells whose overall side or local polygon-strip width is below the limit.')
    layout.addWidget(checks_status)
    hard_edge_status = ResultLabel()
    def check_hard_edges():
        found = checks.show_hard_edges_without_seams()
        if found:
            hard_edge_status.setText(
                '{} hard edge{} without UV cuts · selected.'.format(
                    len(found), '' if len(found) == 1 else 's'))
        else:
            hard_edge_status.setText('No hard edges without UV cuts.')
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
        'Select internal hard edges without UV cuts in the current UV set.')
    layout.addWidget(hard_edge_status)
    checks_group('Overlaps')
    overlap_checks_row = QtWidgets.QHBoxLayout()
    overlap_checks_row.setSpacing(5)
    layout.addLayout(overlap_checks_row)
    check_overlap_button = button('Select Overlaps', check_uv_overlaps, overlap_checks_row)
    check_overlap_button.setText('Select')
    check_overlap_button.setObjectName('selectOverlaps')
    check_overlap_button.setAccessibleName('Select Overlaps')
    check_overlap_button.setToolTip('Select overlapping UV shells without moving UVs.')
    layout.addWidget(overlap_status)
    window.error_checks = ErrorCheckController(window, layout)
    layout.removeWidget(window.error_checks.button)
    overlap_checks_row.addWidget(window.error_checks.button)
    overlap_checks_row.setStretch(0, 1)
    overlap_checks_row.setStretch(1, 1)
    window.error_checks.button.setText('Check 99%')
    window.error_checks.button.setAccessibleName('Check Overlaps')
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
        'Restore sewn seams where adjacent UV faces fold onto the same side. '
        'Cut and select shell borders on selected meshes or whole selected shells. '
        'UVs stay in place; current UV sets are used. Supports Undo.')
    layout.addWidget(seam_status)
    checks_group('UV Sets')
    layout = window.sections['overlaps'].content
    import uv_straight_move_overlaps as overlaps
    importlib.reload(overlaps)
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
    move_step.setToolTip('Distance moved by one arrow click in UV tiles.')
    move_step.setAccessibleName('UV movement step')
    left_controls.addWidget(move_step, 0, 1, 1, 2)
    window.overlap_move_step = move_step
    window.overlap_flip_buttons = {}
    for column, (axis, hint) in enumerate((('U', 'horizontally'), ('V', 'vertically')), 1):
        flip_button = button(axis, partial(overlaps.flip, axis), left_controls)
        left_controls.removeWidget(flip_button)
        left_controls.addWidget(flip_button, 1, column)
        flip_button.setObjectName('overlapFlipButton')
        flip_button.setIcon(flip_axis_icon(axis))
        flip_button.setIconSize(QtCore.QSize(18, 18))
        flip_button.setFixedHeight(28)
        flip_button.setMinimumWidth(40)
        flip_button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        flip_button.setAccessibleName('Flip UV shells along ' + axis)
        flip_button.setToolTip('Flip selected UV shells {} along the {} axis.'.format(hint, axis))
        window.overlap_flip_buttons[axis] = flip_button
    move_grid = QtWidgets.QGridLayout()
    move_grid.setContentsMargins(0, 0, 0, 0)
    move_grid.setHorizontalSpacing(4)
    move_grid.setVerticalSpacing(6)
    move_row.addLayout(move_grid)
    layout.addWidget(move_pad)
    from uv_straight_stack_ui import StackController
    window.stack_shells = StackController(window, layout)
    window.shortcut_actions['stack_shells'] = window.stack_shells.start_or_cancel
    separator = QtWidgets.QFrame()
    separator.setObjectName('overlapSeparator')
    separator.setFixedHeight(1)
    separator.setStyleSheet('QFrame#overlapSeparator { background: #515151; border: 0; }')
    layout.addWidget(separator)
    original_status = QtWidgets.QLabel()
    original_status.setObjectName('overlapOriginalCount')
    original_status.setMinimumWidth(0)
    original_status.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
    original_buttons = QtWidgets.QHBoxLayout()
    original_buttons.setContentsMargins(0, 0, 0, 0)
    original_buttons.setSpacing(6)
    layout.addLayout(original_buttons)
    original_buttons.addWidget(original_status, 1)
    def update_original_status():
        count = overlaps.original_count()
        original_status.setText('Originals {}'.format(count))
        original_status.setToolTip('Assigned originals: {} shell{}'.format(count, '' if count == 1 else 's'))
        with QtCore.QSignalBlocker(show_original_color):
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
            values = dict(source_tile=(0, 0), source_tile_only=True)
            plan = overlaps.analyse(**values)
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
        arrow.setToolTip('Move all selected UV shells {} by the Step value.'.format(direction))
        arrow.setProperty('moveDirection', direction)
        arrow.clicked.connect(lambda checked=False, unit=unit: uv._run(lambda: move_selected(unit)))
        window.shortcut_actions['move_' + direction] = partial(move_selected, unit)
        move_grid.addWidget(arrow, row_index, column_index, QtCore.Qt.AlignCenter)
        window.overlap_move_arrows[direction] = arrow
    analyse_button = button('Analyse', analyse_overlaps)
    analyse_button.setObjectName('overlapAnalyse')
    analyse_button.setIcon(overlap_action_icon('analyse'))
    analyse_button.setIconSize(QtCore.QSize(16, 16))
    analyse_button.setFixedHeight(28)
    analyse_button.setToolTip('Select meshes or groups. Analyse selects copies without moving UVs.')
    layout.addWidget(output)
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
        'auto_hv': lambda: uv.auto_mark(plugin_menu.angle(), whole_shells=True),
        'pick_toggle': plugin_menu.toggle_picker,
        'pick_auto': partial(plugin_menu.set_picker_mode, None),
        'pick_h': partial(plugin_menu.set_picker_mode, 'H'),
        'pick_v': partial(plugin_menu.set_picker_mode, 'V'),
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
    keymaps.build_ui(window.sections['settings'].content, window)
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
            from shiboken6 import isValid
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
    if _popup is not None:
        try:
            if _popup.isVisible():
                _popup.close()
                _popup = None
                return None
        except RuntimeError:
            pass
    _popup = show_window(popup=True)
    return _popup
