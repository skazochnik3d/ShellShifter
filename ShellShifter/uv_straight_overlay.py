"""Qt drawing layer for a UV viewport. No scene nodes or selection changes.

The caller must supply verified UV bounds for the exact parent viewport.
The native manager supplies bounds from a registered Maya UV context.
"""
import math
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from uv_straight_raster import RasterSignals, RasterTicket, RasterTask


def project_uv(point, bounds, width, height):
    left, right, bottom, top = bounds
    if (not all(math.isfinite(value) for value in bounds)
            or right <= left or top <= bottom):
        raise ValueError('UV viewport bounds are invalid')
    return QtCore.QPointF((point[0] - left) * width / (right - left),
                         (top - point[1]) * height / (top - bottom))


class ColorOverlay(QtWidgets.QWidget):
    def __init__(self, viewport, bounds_provider):
        # A child QWidget clears QmayaGLWidget's framebuffer to black. An
        # owned translucent tool window is composited by Windows instead.
        super().__init__(viewport, QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint |
                         QtCore.Qt.WindowTransparentForInput |
                         QtCore.Qt.WindowDoesNotAcceptFocus)
        self.viewport = viewport
        self.setObjectName('uvStraightColorOverlay')
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_QuitOnClose, False)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.bounds_provider = bounds_provider
        self.payload = []
        self.selected_points = []
        self.distortion = []
        self.distortion_polygons = []
        self.distortion_polygon_cache = {}
        self.distortion_groups = {}
        self.group_polygons = {}
        self.group_rectangles = {}
        self.group_caches = {}
        self.raster_append_polygons = []
        self.distortion_rectangles = np.empty((0, 4))
        self.distortion_revision = 0
        self.distortion_dirty_rect = None
        self.distortion_raster = None
        self.raster_bounds = None
        self.raster_revision = -1
        self.raster_timer = QtCore.QTimer(self)
        self.raster_timer.setSingleShot(True)
        self.raster_timer.timeout.connect(self._settle_raster)
        self.projection_timer = QtCore.QTimer(self)
        self.projection_timer.setSingleShot(True)
        self.projection_timer.timeout.connect(self._sync_projection_after_event)
        self.projection_changed = None
        self.async_polygon_limit = 8000
        self.raster_signals = RasterSignals(self)
        self.raster_signals.finished.connect(self._raster_finished, QtCore.Qt.QueuedConnection)
        self.raster_ticket = None
        self.overview_raster = None
        self.detail_raster = None
        self.async_requested = None
        self.async_exact = False
        self.draw_count = 0
        self.error = ''
        self.closing = False
        viewport.installEventFilter(self)
        viewport.window().installEventFilter(self)
        self.sync_geometry()

    def sync_geometry(self):
        if self.closing or self.viewport is None:
            return
        viewport = self.viewport
        geometry = QtCore.QRect(viewport.mapToGlobal(QtCore.QPoint(0, 0)), viewport.size())
        if self.geometry() != geometry:
            self.setGeometry(geometry)
        visible = bool(self.payload or self.distortion or self.selected_points) and viewport.isVisible() and not viewport.window().isMinimized()
        if self.isVisible() != visible:
            self.setVisible(visible)

    def eventFilter(self, watched, event):
        if self.closing:
            return False
        if event.type() in (QtCore.QEvent.Close, QtCore.QEvent.Destroy):
            self.closing = True
            self.raster_timer.stop()
            self.projection_timer.stop()
            self.projection_changed = None
            self._cancel_async_raster()
            self.setUpdatesEnabled(False)
            self.payload = []
            self.selected_points = []
            self.distortion = []
            self.distortion_polygons = []
            self.distortion_polygon_cache.clear()
            self.distortion_groups.clear()
            self.group_caches.clear()
            self.group_polygons.clear()
            self.group_rectangles.clear()
            self.distortion_raster = None
            return False
        if event.type() in (QtCore.QEvent.MouseMove, QtCore.QEvent.MouseButtonPress,
                            QtCore.QEvent.MouseButtonRelease, QtCore.QEvent.Wheel,
                            QtCore.QEvent.KeyRelease, QtCore.QEvent.Resize,
                            QtCore.QEvent.Show):
            input_callback = getattr(self, 'interaction_changed', None)
            if input_callback is not None and watched is self.viewport:
                input_callback(event)
            callback = getattr(self, 'view_changed', None)
            if callback is not None:
                callback()
            self.schedule_projection_sync()
        if event.type() in (
                QtCore.QEvent.Paint, QtCore.QEvent.UpdateRequest):
            # The filter runs BEFORE Maya updates its view. Read the native
            # bounds on the next event-loop turn, after the host paint/input.
            # The overlay's own paint never schedules this timer.
            self.schedule_projection_sync()
        if event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Move,
                            QtCore.QEvent.Show, QtCore.QEvent.Hide,
                            QtCore.QEvent.WindowStateChange):
            self.sync_geometry()
        return False

    def schedule_projection_sync(self):
        if not self.closing and not self.projection_timer.isActive():
            self.projection_timer.start(0)

    def _sync_projection_after_event(self):
        if not self.closing and self.projection_changed is not None:
            self.projection_changed(self)

    def set_payload(self, payload):
        changed = payload != self.payload
        self.payload = payload
        self.sync_geometry()
        if changed:
            self.update()

    def set_selected_points(self, points):
        changed = points != self.selected_points
        self.selected_points = list(points)
        self.sync_geometry()
        if changed:
            self.update()

    def set_distortion(self, payload, groups=None):
        grouped = {'_': payload} if groups is None else groups
        changed_groups = {name for name, data in grouped.items()
                          if name not in self.distortion_groups or
                          (data is not self.distortion_groups[name] and
                           (groups is not None or data != self.distortion_groups[name]))}
        removed_groups = self.distortion_groups.keys()-grouped.keys()
        changed = bool(changed_groups or removed_groups)
        self.distortion = payload
        if changed:
            dirty = self.distortion_dirty_rect
            old_names, new_names = sorted(self.distortion_groups), sorted(grouped)
            append = not removed_groups and not (changed_groups & self.distortion_groups.keys()) and new_names[:len(old_names)] == old_names
            if not append:
                self.raster_append_polygons = None
            for name in removed_groups:
                for item in self.group_caches.pop(name).values():
                    dirty = QtCore.QRectF(item[4]) if dirty is None else dirty.united(item[4])
                self.group_polygons.pop(name, None)
                self.group_rectangles.pop(name, None)
            for name in sorted(changed_groups):
                old_cache = self.group_caches.get(name, {})
                cache, polygons, rectangles = {}, [], []
                for color, points in grouped[name]:
                    if color[3] <= 0 or len(points) < 3:
                        continue
                    key = id(points)
                    previous = old_cache.get(key)
                    if previous is not None and previous[0] is points and previous[1] == color:
                        item = previous
                    else:
                        polygon = QtGui.QPolygonF([QtCore.QPointF(*p) for p in points])
                        rectangle = polygon.boundingRect()
                        item = (points, color, QtGui.QColor.fromRgbF(*color), polygon, rectangle,
                                (rectangle.left(), rectangle.bottom(), rectangle.right(), rectangle.top()))
                        dirty = QtCore.QRectF(rectangle) if dirty is None else dirty.united(rectangle)
                        if previous is not None:
                            dirty = dirty.united(previous[4])
                    cache[key] = item
                    polygons.append(item[2:5])
                    rectangles.append(item[5])
                for key in old_cache.keys()-cache.keys():
                    rectangle = old_cache[key][4]
                    dirty = QtCore.QRectF(rectangle) if dirty is None else dirty.united(rectangle)
                self.group_caches[name] = cache
                self.group_polygons[name] = polygons
                self.group_rectangles[name] = np.asarray(rectangles, dtype=np.float64).reshape(-1, 4)
                if append and self.raster_append_polygons is not None:
                    self.raster_append_polygons.extend(polygons)
            self.distortion_groups = dict(grouped)
            self.distortion_polygons = [item for name in new_names for item in self.group_polygons[name]]
            self.distortion_rectangles = np.concatenate([self.group_rectangles[name] for name in new_names]) if new_names else np.empty((0, 4))
            self.distortion_dirty_rect = dirty
            self.distortion_revision += 1
            self._cancel_async_raster()
        self.sync_geometry()
        if changed:
            self.update()

    def _settle_raster(self):
        if not self.closing:
            if len(self.distortion_polygons) > self.async_polygon_limit:
                self.async_exact = True
            else:
                self.raster_bounds = None
            self.update()

    def _cancel_async_raster(self):
        if self.raster_ticket is not None:
            self.raster_ticket.cancelled.set()
        if self.overview_raster is not None or self.detail_raster is not None:
            self.distortion_raster = None
            self.raster_bounds = None
        self.overview_raster = None
        self.detail_raster = None
        self.async_requested = None
        self.async_exact = False

    def _raster_finished(self, ticket):
        if ticket is not self.raster_ticket:
            return
        self.raster_ticket = None
        if self.closing:
            return
        if ticket.error:
            self.error = ticket.error
        elif not ticket.cancelled.is_set() and ticket.revision == self.distortion_revision:
            data = (QtGui.QImage(ticket.image), ticket.bounds, ticket.view_bounds, ticket.size)
            if ticket.overview:
                self.overview_raster = data
            else:
                self.detail_raster = data
            self.distortion_raster = data[0]
            self.raster_bounds = data[1]
            self.raster_view_bounds = data[2]
            self.raster_view_size = data[3]
            self.raster_revision = ticket.revision
            self.distortion_dirty_rect = None
            self.raster_append_polygons = []
        self.update()

    def _draw_async_distortion(self, painter, bounds):
        width, height = self.width(), self.height()
        request = (bounds, (width, height), self.distortion_revision)
        if request != self.async_requested:
            self.async_requested = request
            self.raster_timer.start(100)
        detail = self.detail_raster
        needs_detail = detail is None
        if detail is not None:
            _, (rl,rr,rb,rt), (ol,or_,ob,ot), size = detail
            left,right,bottom,top = bounds
            scale = max((or_-ol)/(right-left), (ot-ob)/(top-bottom))
            needs_detail = (size != (width,height) or left < rl or right > rr or
                            bottom < rb or top > rt or scale > 1.3 or scale < .77 or
                            (self.async_exact and detail[2] != bounds))
        if self.raster_ticket is None and (self.overview_raster is None or needs_detail):
            ticket = RasterTicket(self.distortion_polygons, self.distortion_rectangles,
                bounds, (width,height), self.distortion_revision,
                self.overview_raster is None, self.raster_signals)
            self.raster_ticket = ticket
            self.async_exact = False
            QtCore.QThreadPool.globalInstance().start(RasterTask(ticket))
        left,right,bottom,top = bounds
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        for data in (self.overview_raster, self.detail_raster):
            if data is None:
                continue
            image, (rl,rr,rb,rt), _, _ = data
            target = QtCore.QRectF((rl-left)*width/(right-left), (top-rt)*height/(top-bottom),
                                  (rr-rl)*width/(right-left), (rt-rb)*height/(top-bottom))
            # Replace, rather than blend twice, inside detailed image coverage.
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
            painter.drawImage(target, image)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)

    def _draw_distortion(self, painter, bounds):
        if not self.distortion_polygons:
            self.distortion_raster = None
            return
        if len(self.distortion_polygons) > self.async_polygon_limit:
            self._draw_async_distortion(painter, bounds)
            return
        left, right, bottom, top = bounds
        width, height = self.width(), self.height()
        full = (self.distortion_raster is None or self.raster_bounds is None or
                getattr(self, 'raster_view_size', None) != (width, height))
        if not full:
            rl, rr, rb, rt = self.raster_bounds
            covered = left >= rl and right <= rr and bottom >= rb and top <= rt
            old_left, old_right, old_bottom, old_top = self.raster_view_bounds
            scale = max((old_right-old_left)/(right-left), (old_top-old_bottom)/(top-bottom))
            # An affine image reuse is cheap and stays in the current bounds.
            # Refresh resolution/coverage DURING a large zoom or pan rather
            # than waiting for 100 ms of silence (often only mouse-up).
            full = not covered or scale > 1.3 or scale < .77
        if (self.distortion_raster is not None and self.raster_bounds is not None and
                not full and
                self.raster_revision == self.distortion_revision and
                getattr(self, 'raster_view_bounds', None) != bounds and
                getattr(self, 'raster_view_size', None) == (width, height)):
            self.raster_timer.start(100)
        else:
            self.raster_timer.stop()
            if full:
                du, dv = (right-left)*.2, (top-bottom)*.2
                self.raster_bounds = (left-du, right+du, bottom-dv, top+dv)
                self.raster_view_bounds = bounds
                self.raster_view_size = (width, height)
                self.distortion_raster = QtGui.QImage(max(1, round(width*1.4)), max(1, round(height*1.4)),
                                                     QtGui.QImage.Format_ARGB32_Premultiplied)
                self.distortion_raster.fill(QtCore.Qt.transparent)
            if full or self.raster_revision != self.distortion_revision:
                rl, rr, rb, rt = self.raster_bounds
                sx = self.distortion_raster.width()/(rr-rl)
                sy = self.distortion_raster.height()/(rt-rb)
                visible = QtCore.QRectF(rl, rb, rr-rl, rt-rb)
                region = visible if full or self.distortion_dirty_rect is None else self.distortion_dirty_rect.adjusted(-2/sx, -2/sy, 2/sx, 2/sy).intersected(visible)
                raster = QtGui.QPainter(self.distortion_raster)
                try:
                    pixel_region = QtCore.QRectF((region.left()-rl)*sx, (rt-region.bottom())*sy,
                                                region.width()*sx, region.height()*sy).toAlignedRect()
                    region = QtCore.QRectF(rl+pixel_region.left()/sx,
                                          rt-(pixel_region.top()+pixel_region.height())/sy,
                                          pixel_region.width()/sx, pixel_region.height()/sy)
                    append = not full and self.raster_append_polygons is not None
                    if not append:
                        raster.setClipRect(pixel_region)
                        raster.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
                        raster.fillRect(pixel_region, QtCore.Qt.transparent)
                    raster.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
                    raster.setRenderHint(QtGui.QPainter.Antialiasing)
                    raster.setPen(QtCore.Qt.NoPen)
                    raster.setTransform(QtGui.QTransform(sx, 0., 0., -sy, -rl*sx, rt*sy))
                    if append:
                        polygons = self.raster_append_polygons
                    else:
                        rectangles = self.distortion_rectangles
                        visible_ids = np.flatnonzero((rectangles[:, 0] < region.right()) &
                                                     (rectangles[:, 2] > region.left()) &
                                                     (rectangles[:, 3] < region.bottom()) &
                                                     (rectangles[:, 1] > region.top()))
                        polygons = (self.distortion_polygons[index] for index in visible_ids)
                    for color, polygon, _ in polygons:
                        raster.setBrush(color)
                        raster.drawPolygon(polygon)
                finally:
                    raster.end()
                self.raster_revision = self.distortion_revision
                self.distortion_dirty_rect = None
                self.raster_append_polygons = []
        rl, rr, rb, rt = self.raster_bounds
        target = QtCore.QRectF((rl-left)*width/(right-left), (top-rt)*height/(top-bottom),
                              (rr-rl)*width/(right-left), (rt-rb)*height/(top-bottom))
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        painter.drawImage(target, self.distortion_raster)

    def paintEvent(self, event):
        if self.closing or not self.isVisible():
            return
        painter = None
        try:
            # Explicitly erase the translucent backing store: old segments
            # must not survive a payload/selection change on Windows.
            painter = QtGui.QPainter(self)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
            painter.fillRect(self.rect(), QtCore.Qt.transparent)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
            if not self.payload and not self.distortion and not self.selected_points:
                return
            bounds = self.bounds_provider()
            # Validate before painting, including when payload groups are empty.
            project_uv((0., 0.), bounds, self.width(), self.height())
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setClipRect(self.rect())
            painter.setPen(QtCore.Qt.NoPen)
            self._draw_distortion(painter, bounds)
            for color, segments, points in self.payload:
                qcolor = QtGui.QColor.fromRgbF(*color)
                pen = QtGui.QPen(qcolor, 3.)
                pen.setCapStyle(QtCore.Qt.RoundCap)
                painter.setPen(pen)
                project = lambda point: project_uv(point, bounds, self.width(), self.height())
                for a, b in segments:
                    painter.drawLine(project(a), project(b))
                painter.setPen(QtCore.Qt.NoPen)
                painter.setBrush(qcolor)
                for point in points:
                    painter.drawEllipse(project(point), 3.5, 3.5)
            if self.selected_points:
                project = lambda point: project_uv(point, bounds, self.width(), self.height())
                painter.setPen(QtCore.Qt.NoPen)
                painter.setBrush(QtGui.QColor(20, 28, 20, 220))
                for point in self.selected_points:
                    painter.drawEllipse(project(point), 6., 6.)
                painter.setBrush(QtGui.QColor(92, 255, 72))
                for point in self.selected_points:
                    painter.drawEllipse(project(point), 4.4, 4.4)
            self.draw_count += 1
            self.error = ''
        except Exception as error:
            self.error = '{}: {}'.format(type(error).__name__, error)
        finally:
            if painter is not None:
                painter.end()

    def shutdown(self):
        """Stop native painting before Maya replaces a mesh or UV widget."""
        if self.closing:
            return
        self.closing = True
        self.raster_timer.stop()
        self.projection_timer.stop()
        self.projection_changed = None
        self._cancel_async_raster()
        self.setUpdatesEnabled(False)
        self.payload = []
        self.selected_points = []
        self.distortion = []
        self.distortion_polygons = []
        self.distortion_polygon_cache.clear()
        self.distortion_groups.clear()
        self.group_caches.clear()
        self.group_polygons.clear()
        self.group_rectangles.clear()
        self.distortion_raster = None
        self.view_changed = None
        viewport = self.viewport
        if viewport is not None:
            try:
                viewport.removeEventFilter(self)
                viewport.window().removeEventFilter(self)
            except RuntimeError:
                pass
        self.hide()
        self.close()
        self.viewport = None
        self.editor_root = None


class ViewportEdgeOverlay(QtWidgets.QWidget):
    """Transparent model-viewport layer for non-selectable validation edges."""
    def __init__(self, viewport):
        super().__init__(viewport, QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint |
                         QtCore.Qt.WindowTransparentForInput |
                         QtCore.Qt.WindowDoesNotAcceptFocus)
        self.viewport = viewport
        self.setObjectName('uvStraightViewportEdgeOverlay')
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_QuitOnClose, False)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.segments = []
        self.closing = False
        self.view_changed = None
        viewport.installEventFilter(self)
        viewport.window().installEventFilter(self)
        self.sync_geometry()

    def sync_geometry(self):
        if self.closing or self.viewport is None:
            return
        geometry = QtCore.QRect(self.viewport.mapToGlobal(QtCore.QPoint(0, 0)),
                                self.viewport.size())
        if self.geometry() != geometry:
            self.setGeometry(geometry)
        visible = bool(self.segments) and self.viewport.isVisible()
        if self.isVisible() != visible:
            self.setVisible(visible)

    def eventFilter(self, watched, event):
        if self.closing:
            return False
        if event.type() in (QtCore.QEvent.Close, QtCore.QEvent.Destroy):
            self.closing = True
            self.segments = []
            return False
        if event.type() in (QtCore.QEvent.MouseMove, QtCore.QEvent.MouseButtonPress,
                            QtCore.QEvent.MouseButtonRelease, QtCore.QEvent.Wheel,
                            QtCore.QEvent.Resize, QtCore.QEvent.Show,
                            QtCore.QEvent.Move):
            if self.view_changed is not None:
                self.view_changed()
        if event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Move,
                            QtCore.QEvent.Show, QtCore.QEvent.Hide,
                            QtCore.QEvent.WindowStateChange):
            self.sync_geometry()
        return False

    def set_segments(self, segments):
        changed = list(segments) != self.segments
        self.segments = list(segments)
        self.sync_geometry()
        if changed:
            self.update()

    def paintEvent(self, event):
        if self.closing or not self.segments:
            return
        painter = QtGui.QPainter(self)
        try:
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
            painter.fillRect(self.rect(), QtCore.Qt.transparent)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setClipRect(self.rect())
            shadow = QtGui.QPen(QtGui.QColor(35, 0, 0, 210), 5.5)
            shadow.setCapStyle(QtCore.Qt.RoundCap)
            painter.setPen(shadow)
            for first, second in self.segments:
                painter.drawLine(first, second)
            red = QtGui.QPen(QtGui.QColor(255, 35, 18), 3.2)
            red.setCapStyle(QtCore.Qt.RoundCap)
            painter.setPen(red)
            for first, second in self.segments:
                painter.drawLine(first, second)
        finally:
            painter.end()

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.segments = []
        self.view_changed = None
        viewport = self.viewport
        if viewport is not None:
            try:
                viewport.removeEventFilter(self)
                viewport.window().removeEventFilter(self)
            except RuntimeError:
                pass
        self.hide()
        self.close()
        self.viewport = None
