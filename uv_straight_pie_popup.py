"""Adaptive Qt marking popup with the visual editor's continuous radial layout."""
import math
from PySide6 import QtCore, QtGui, QtWidgets
import maya.cmds as cmds
from uv_straight_pie_preview import NativePieCanvas
import uv_straight_pie as pie


class RuntimePieCanvas(NativePieCanvas):
    def __init__(self, popup):
        self._runtime_reflow = False
        super().__init__(popup)
        self.background = None
        self.setObjectName('shellShifterRuntimePieCanvas')
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setAutoFillBackground(False)

    def resizeEvent(self, event):
        # Geometry is set explicitly for each page. Reflowing from resize can
        # reset a submenu's width/offset while a frame is being presented.
        QtWidgets.QWidget.resizeEvent(self, event)

    def showEvent(self, event):
        # open_at already refreshed and positioned the complete runtime page.
        QtWidgets.QWidget.showEvent(self, event)

    def reflow(self):
        if self._runtime_reflow:
            return
        self._runtime_reflow = True
        try:
            self.setFixedWidth(480)
            page = self.editor.pages[-1] if self.editor.pages else None
            if not page or page['owner'] is None:
                super().reflow()
                return
            children = page['owner']['children']
            radial = [node for node in children if node['kind'] != 'separator'][:8]
            radial_ids = {node['id'] for node in radial}
            lower = [node for node in children if node['id'] not in radial_ids]
            metrics = QtGui.QFontMetrics(self.menu_font())
            # Maya's small nested menus use the forward/right and bottom slots.
            angles = ([0.] if len(radial) == 1 else [0., math.pi*.5] if len(radial) == 2 else
                      [-math.pi*.5, 0., math.pi*.5] if len(radial) == 3 else None)
            self.pie_center, self.regions, y = pie.ring_geometry(
                480, metrics, [('child'+str(index), node) for index, node in enumerate(radial)], angles)
            self.panels = []
            self.radial_bottom = y
            if any(node['kind'] != 'separator' for node in lower):
                y = self.add_panel(lower, y, page['owner']['id'], None, metrics)
            # Keep previous centers visible even across a chain of upward moves.
            trail = [self.pie_center+QtCore.QPointF(previous['center']-page['center'])
                     for previous in self.editor.pages[:-1]]
            bounds = QtCore.QRectF(0, 0, 480, y+20)
            for point in trail:
                bounds = bounds.united(QtCore.QRectF(point.x()-6, point.y()-6, 12, 12))
            offset = QtCore.QPointF(max(0., 12-bounds.left()), max(0., 12-bounds.top()))
            self.pie_center += offset
            self.radial_bottom += offset.y()
            for region in self.regions:
                region['rect'].translate(offset)
                if 'anchor' in region:
                    region['anchor'] += offset
            for panel in self.panels:
                panel['rect'].translate(offset)
            self.setFixedSize(int(bounds.width()+24), int(bounds.height()+24))
            if hasattr(self, 'drag'):
                self.drag.setGeometry(self.rect())
            self.update()
        finally:
            self._runtime_reflow = False

    def paintEvent(self, event):
        # Repaint alpha as well as the entries so cursor lines leave no trails.
        painter = QtGui.QPainter(self)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(), QtCore.Qt.transparent)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
        if self.editor.transition_image is not None:
            painter.setOpacity(1.-self.editor.page_opacity)
            painter.drawPixmap(self.mapFromGlobal(self.editor.transition_origin), self.editor.transition_image)
        painter.setOpacity(self.editor.page_opacity)
        self.paint_contents(painter)
        painter.end()

    def guide_pen(self):
        from uv_straight_native import COLORS
        pen = QtGui.QPen(QtGui.QColor.fromRgbF(*COLORS['V']), 2.)
        pen.setCosmetic(True)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        return pen

    def paint_underlay(self, painter):
        centers = self.editor.trail_points()
        painter.setPen(self.guide_pen())
        painter.setBrush(QtGui.QColor('#777777'))
        for index, center in enumerate(centers[:-1]):
            painter.drawLine(QtCore.QLineF(center, centers[index+1]))
            painter.drawEllipse(center, 5, 5)
        point = self.editor.cursor_point
        if point is None:
            return
        vector = point-self.pie_center
        length = math.hypot(vector.x(), vector.y())
        if length <= 6:
            return
        painter.setPen(self.guide_pen())
        # Start outside the center dot; entries paint over the line's end.
        painter.drawLine(QtCore.QLineF(self.pie_center+vector*(6./length), point))

    def entry_enabled(self, node):
        return self.editor.selectable(node)

    def mouseMoveEvent(self, event):
        self.editor.track(event.position())
        event.accept()

    def mousePressEvent(self, event):
        self.editor.record_press(event.position(), event.button())
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == self.editor.button:
            self.editor.finish_gesture(event.position())
            event.accept()
            return
        region = self.hit(event.position())
        if region is None or region['node']['kind'] == 'separator':
            self.editor.close()
        else:
            self.editor.activate(region)
        event.accept()

    def contextMenuEvent(self, event):
        event.accept()

    def mouseDoubleClickEvent(self, event):
        event.accept()


class PiePopup(QtWidgets.QWidget):
    """Hold to navigate radial levels; release the bound button to finish."""
    TRANSITION_DURATION = 320

    def __init__(self, parent=None, button=QtCore.Qt.RightButton):
        super().__init__(parent, QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint | QtCore.Qt.NoDropShadowWindowHint)
        self.setObjectName('shellShifterAdaptivePie')
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WA_NoMouseReplay)
        self.setAutoFillBackground(False)
        self.setStyleSheet('QWidget#shellShifterAdaptivePie, QWidget#shellShifterRuntimePieCanvas {'
                          ' background: transparent; border: none; }')
        self.setMouseTracking(True)
        self.editing = False
        self.direction = self.identifier = self.container = None
        self.selected = bool(cmds.ls(selection=True))
        self.button = button
        self.holding = True
        self.executed = False
        self.submenu_id = None
        self.pages = []
        self.hover_id = None
        self.cursor_point = None
        self.cancel_button = None
        self.page_opacity = 1.
        self.transition_image = None
        self.transition_origin = QtCore.QPoint()
        self.transition_cursor = None
        self.frame_bounds = QtCore.QRect()
        self.page_animation = QtCore.QVariantAnimation(self)
        self.page_animation.setDuration(self.TRANSITION_DURATION)
        self.page_animation.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        self.page_animation.setStartValue(0.)
        self.page_animation.setEndValue(1.)
        self.page_animation.valueChanged.connect(self.animate_page)
        self.page_animation.finished.connect(self.stop_transition)
        self.submenu_timer = QtCore.QTimer(self)
        self.submenu_timer.setSingleShot(True)
        self.submenu_timer.setInterval(400)
        self.submenu_timer.timeout.connect(self.open_hover_submenu)
        self.back_timer = QtCore.QTimer(self)
        self.back_timer.setSingleShot(True)
        self.back_timer.setInterval(400)
        self.back_timer.timeout.connect(self.back)
        self.canvas = RuntimePieCanvas(self)
        self.canvas.setFixedWidth(480)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        layout.addWidget(self.canvas)
        self.adjustSize()

    def selectable(self, node):
        return (node['kind'] == 'separator' or self.selected or node.get('action') in pie.NO_SELECTION or
                any(self.selectable(child) for child in node.get('children', [])))

    def open_at(self, global_point):
        self.stop_transition()
        self.frame_bounds = QtCore.QRect()
        self.transition_cursor = None
        self.pages = [dict(owner=None, center=QtCore.QPoint(global_point))]
        self.canvas.refresh()
        self.position_page()
        self.show()
        self.pages[0]['center'] = self.canvas.mapToGlobal(self.canvas.pie_center.toPoint())
        self.raise_()
        QtWidgets.QApplication.instance().installEventFilter(self)

    def capture_page(self):
        """Capture the currently visible blend for a smooth interrupted change."""
        bounds = QtCore.QRect(self.canvas.mapToGlobal(QtCore.QPoint()), self.canvas.size())
        return self.canvas.grab(), bounds

    def prepare_transition(self):
        snapshot = self.capture_page() if self.isVisible() else None
        cursor = self.canvas.mapToGlobal(self.cursor_point.toPoint()) if self.cursor_point is not None else None
        self.stop_transition()
        self.transition_cursor = cursor
        if snapshot is not None:
            self.transition_image, bounds = snapshot
            self.transition_origin = bounds.topLeft()
            self.page_opacity = 0.
        return snapshot

    def start_transition(self, snapshot):
        if snapshot is None:return
        self.page_animation.start()
        self.canvas.update()

    def animate_page(self, value):
        self.page_opacity = float(value)
        self.canvas.update()

    def stop_transition(self):
        self.page_animation.stop()
        self.transition_image = None
        self.page_opacity = 1.
        if hasattr(self, 'canvas'):self.canvas.update()

    def position_page(self):
        global_point = self.pages[-1]['center']
        position = global_point-self.canvas.pie_center.toPoint()
        target_size = self.canvas.size()
        screen = QtGui.QGuiApplication.screenAt(global_point) or QtGui.QGuiApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            position.setX(max(available.left(), min(position.x(), available.right()-target_size.width()+1)))
            position.setY(max(available.top(), min(position.y(), available.bottom()-target_size.height()+1)))
        target_bounds = QtCore.QRect(position, target_size)
        # Retain the frame extent for this gesture, so finishing a fade never
        # resizes or moves the native window. Both pages paint into this canvas.
        bounds = self.frame_bounds.united(target_bounds) if not self.frame_bounds.isEmpty() else target_bounds
        offset = QtCore.QPointF(position-bounds.topLeft())
        self.canvas.pie_center += offset
        self.canvas.radial_bottom += offset.y()
        for region in self.canvas.regions:
            region['rect'].translate(offset)
            if 'anchor' in region:region['anchor'] += offset
        for panel in self.canvas.panels:panel['rect'].translate(offset)
        self.frame_bounds = bounds
        self.canvas.setFixedSize(bounds.size())
        self.setFixedSize(bounds.size())
        self.layout().activate()
        self.move(bounds.topLeft())
        self.pages[-1]['center'] = self.canvas.mapToGlobal(self.canvas.pie_center.toPoint())
        self.cursor_point = (QtCore.QPointF(self.canvas.mapFromGlobal(self.transition_cursor))
                             if self.transition_cursor is not None else QtCore.QPointF(self.canvas.pie_center))

    def trail_points(self):
        if not self.pages:
            return [self.canvas.pie_center]
        return [QtCore.QPointF(self.canvas.mapFromGlobal(page['center'])) for page in self.pages]

    def eventFilter(self, watched, event):
        if not self.isVisible():
            return False
        kind = event.type()
        if kind in (QtCore.QEvent.ShortcutOverride, QtCore.QEvent.KeyPress) and event.key() == QtCore.Qt.Key_Escape:
            event.accept()
            if kind == QtCore.QEvent.KeyPress:
                self.close()
            return True
        if kind == QtCore.QEvent.MouseButtonRelease and event.button() == self.cancel_button:
            self.close()
            event.accept()
            return True
        if self.cancel_button is not None and kind == QtCore.QEvent.MouseMove:
            event.accept()
            return True
        if kind == QtCore.QEvent.MouseButtonPress:
            global_point = event.globalPosition().toPoint()
            point = QtCore.QPointF(self.canvas.mapFromGlobal(global_point))
            if self.record_press(point, event.button()):
                event.accept()
                return True
        if kind == QtCore.QEvent.MouseMove and self.holding:
            self.track(QtCore.QPointF(self.canvas.mapFromGlobal(event.globalPosition().toPoint())))
            event.accept()
            return True
        if kind == QtCore.QEvent.MouseButtonRelease and event.button() == self.button:
            point = QtCore.QPointF(self.canvas.mapFromGlobal(event.globalPosition().toPoint()))
            self.finish_gesture(point)
            event.accept()
            return True
        return False

    def record_press(self, point, button):
        region = self.canvas.hit(point)
        if region is None or region['node']['kind'] == 'separator':
            self.cancel_button = button
            return True
        return False

    def track(self, point):
        global_point = QtCore.QPointF(point)+QtCore.QPointF(self.canvas.mapToGlobal(QtCore.QPoint()))
        self.ensure_cursor_frame(global_point)
        point = global_point-QtCore.QPointF(self.canvas.mapToGlobal(QtCore.QPoint()))
        self.cursor_point = point
        if len(self.pages) > 1 and self.canvas.hit(point) is None:
            previous = self.trail_points()[-2]
            if (point-previous).manhattanLength() < 14:
                self.submenu_timer.stop()
                self.hover_id = None
                self.canvas.hover = None
                if not self.back_timer.isActive():
                    self.back_timer.start()
                self.canvas.update()
                return
        self.back_timer.stop()
        region = self.region_at(point, gesture=self.holding)
        self.canvas.hover = region
        node = region['node'] if region else None
        self.canvas.setToolTip(node.get('label', '') if node else '')
        identifier = node['id'] if node else None
        if identifier != self.hover_id:
            self.hover_id = identifier
            self.submenu_timer.stop()
            if node and node['kind'] == 'submenu' and self.selectable(node):
                self.submenu_timer.start()
        self.canvas.update()

    def ensure_cursor_frame(self, global_point):
        """Make room for the complete ray without moving menu hit targets."""
        if self.frame_bounds.isEmpty():return
        target = QtCore.QRectF(global_point-QtCore.QPointF(4,4), QtCore.QSizeF(8,8)).toAlignedRect()
        bounds = QtCore.QRect(self.frame_bounds)
        # Grow in chunks rather than moving/resizing the popup on every pixel.
        step = 128
        if target.left() < bounds.left():
            bounds.setLeft(bounds.left()-math.ceil((bounds.left()-target.left())/step)*step)
        if target.top() < bounds.top():
            bounds.setTop(bounds.top()-math.ceil((bounds.top()-target.top())/step)*step)
        if target.right() > bounds.right():
            bounds.setRight(bounds.right()+math.ceil((target.right()-bounds.right())/step)*step)
        if target.bottom() > bounds.bottom():
            bounds.setBottom(bounds.bottom()+math.ceil((target.bottom()-bounds.bottom())/step)*step)
        if bounds == self.frame_bounds:return
        offset = QtCore.QPointF(self.frame_bounds.topLeft()-bounds.topLeft())
        self.canvas.pie_center += offset
        self.canvas.radial_bottom += offset.y()
        for region in self.canvas.regions:
            region['rect'].translate(offset)
            if 'anchor' in region:region['anchor'] += offset
        for panel in self.canvas.panels:panel['rect'].translate(offset)
        if self.cursor_point is not None:self.cursor_point += offset
        self.frame_bounds = bounds
        self.canvas.setFixedSize(bounds.size())
        self.setFixedSize(bounds.size())
        self.layout().activate()
        self.move(bounds.topLeft())

    def finish_gesture(self, point):
        self.holding = False
        self.submenu_timer.stop()
        self.back_timer.stop()
        region = self.region_at(point, gesture=True)
        node = region['node'] if region else None
        if node and node['kind'] == 'action' and self.selectable(node):
            self.run(node['action'])
        else:
            self.close()

    def back(self):
        self.back_timer.stop()
        if len(self.pages) < 2:
            return
        snapshot = self.prepare_transition()
        self.pages.pop()
        self.hover_id = None
        self.canvas.hover = None
        self.submenu_timer.stop()
        self.submenu_id = self.pages[-1]['owner']['id'] if self.pages[-1]['owner'] else None
        self.canvas.reflow()
        self.position_page()
        self.start_transition(snapshot)

    def region_at(self, point, gesture=False):
        vector = point-self.canvas.pie_center
        if gesture and math.hypot(vector.x(), vector.y()) < 18:
            return None
        region = self.canvas.hit(point)
        if region or not gesture or point.y() >= self.canvas.radial_bottom:
            return region
        radial = [region for region in self.canvas.regions if region['direction'] and region['node']]
        angle = math.atan2(vector.y(), vector.x())
        return min(radial, key=lambda region: abs((region['angle']-angle+math.pi) % math.tau-math.pi)) if radial else None

    def open_hover_submenu(self):
        region = next((region for region in self.canvas.regions if region['node'] and
                       region['node']['id'] == self.hover_id), None)
        if self.isVisible() and region and region['node']['kind'] == 'submenu':
            self.open_submenu(region)

    def activate(self, region):
        if not region or not region['node']:
            return
        node = region['node']
        if not self.selectable(node) or node['kind'] == 'separator':
            return
        if node['kind'] == 'submenu':
            self.open_submenu(region)
        else:
            self.run(node['action'])

    def open_submenu(self, region):
        node = region['node']
        if self.submenu_id == node['id']:
            return
        self.submenu_id = node['id']
        point = self.cursor_point if self.hover_id == node['id'] and self.cursor_point is not None else region['rect'].center()
        anchor = self.canvas.mapToGlobal(point.toPoint())
        snapshot = self.prepare_transition()
        self.submenu_timer.stop()
        self.pages.append(dict(owner=node, center=anchor))
        self.back_timer.stop()
        self.hover_id = None
        self.canvas.hover = None
        self.canvas.reflow()
        self.position_page()
        self.start_transition(snapshot)

    def run(self, action):
        if self.executed:
            return
        self.executed = True
        self.close()
        from uv_straight_menu import after_menu_interaction
        from uv_straight_actions import execute
        after_menu_interaction(lambda: execute(action))

    def hideEvent(self, event):
        self.stop_transition()
        self.submenu_timer.stop()
        self.back_timer.stop()
        self.cursor_point = None
        self.cancel_button = None
        QtWidgets.QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Escape:
            self.close()
        event.accept()
