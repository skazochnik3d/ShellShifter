"""Background QImage painting only. No QWidget, Maya or scene API calls."""
import threading
import numpy as np
from PySide6 import QtCore, QtGui


class RasterSignals(QtCore.QObject):
    finished = QtCore.Signal(object)


class RasterTicket:
    def __init__(self, polygons, rectangles, bounds, size, revision, overview, signals):
        # The caller replaces these containers; published polygons are never
        # modified. Worker uses its own Qt value copies and a private QImage.
        self.polygons, self.rectangles = polygons, rectangles
        self.view_bounds, self.size = bounds, size
        self.revision, self.overview, self.signals = revision, overview, signals
        self.cancelled = threading.Event()
        self.bounds = bounds
        self.image = None
        self.error = ''


class RasterTask(QtCore.QRunnable):
    def __init__(self, ticket):
        super().__init__()
        self.ticket = ticket

    def run(self):
        ticket = self.ticket
        painter = None
        try:
            if ticket.cancelled.is_set():
                return
            left, right, bottom, top = ticket.view_bounds
            width, height = ticket.size
            du, dv = (right-left)*.2, (top-bottom)*.2
            bounds = (left-du, right+du, bottom-dv, top+dv)
            if ticket.overview and len(ticket.rectangles):
                rects = ticket.rectangles
                bounds = (min(bounds[0], float(rects[:,0].min())),
                          max(bounds[1], float(rects[:,2].max())),
                          min(bounds[2], float(rects[:,3].min())),
                          max(bounds[3], float(rects[:,1].max())))
            rl, rr, rb, rt = bounds
            sx, sy = width/(right-left), height/(top-bottom)
            image_width, image_height = (rr-rl)*sx, (rt-rb)*sy
            if ticket.overview:
                factor = min(1., 2048/max(image_width, image_height))
                image_width, image_height = image_width*factor, image_height*factor
            image = QtGui.QImage(max(1, round(image_width)), max(1, round(image_height)),
                                 QtGui.QImage.Format_ARGB32_Premultiplied)
            image.fill(QtCore.Qt.transparent)
            painter = QtGui.QPainter(image)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setPen(QtCore.Qt.NoPen)
            sx, sy = image.width()/(rr-rl), image.height()/(rt-rb)
            painter.setTransform(QtGui.QTransform(sx, 0., 0., -sy, -rl*sx, rt*sy))
            rects = ticket.rectangles
            visible = np.flatnonzero((rects[:,0] < rr) & (rects[:,2] > rl) &
                                      (rects[:,3] < rt) & (rects[:,1] > rb))
            for index in visible:
                if ticket.cancelled.is_set():
                    return
                color, polygon, _ = ticket.polygons[index]
                painter.setBrush(QtGui.QColor(color))
                painter.drawPolygon(QtGui.QPolygonF(polygon))
            painter.end()
            painter = None
            ticket.bounds, ticket.image = bounds, image
        except Exception as error:
            ticket.error = '{}: {}'.format(type(error).__name__, error)
        finally:
            if painter is not None:
                painter.end()
            try:
                ticket.signals.finished.emit(ticket)
            except RuntimeError:
                pass  # The owning layer was destroyed after cancellation.
