"""Stable pie drag targets and crisp labels share pinned cards' silhouette/easing."""
from PySide6 import QtCore, QtGui, QtWidgets
from uv_straight_section_drag import SectionDragOverlay
import uv_straight_pie as pie


class PieDragOverlay(QtWidgets.QWidget):
    cleared = QtCore.Signal()
    DURATION = SectionDragOverlay.DURATION

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.source = None
        self.external = None
        self.pointer_owner = None
        self.settling = False
        self.proposal = None
        self.spec = None
        self.entries = {}
        self.hit_regions = []
        self.hit_panels = []
        self.positions = {}
        self.placeholder = QtCore.QRectF()
        self.floating = QtCore.QRectF()
        self.footer_rect = QtCore.QRectF()
        self.original_height = 0
        self.floating_opacity = 1.
        self.group = QtCore.QParallelAnimationGroup(self)
        self.group.finished.connect(self.animation_finished)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setObjectName('pieDragOverlay')
        self.hide()

    def begin(self, region, point):
        self.cancel()
        self.positions = {r['node']['id']: QtCore.QRectF(r['rect']) for r in self.canvas.regions if r['node']}
        self.hit_regions = [dict(r, rect=QtCore.QRectF(r['rect'])) for r in self.canvas.regions]
        self.hit_panels = [dict(panel, rect=QtCore.QRectF(panel['rect'])) for panel in self.canvas.panels]
        self.entries = {r['node']['id']: dict(r) for r in self.hit_regions if r['node']}
        self.source = region['node']['id']
        self.offset = point-region['rect'].topLeft()
        self.floating = QtCore.QRectF(region['rect'])
        self.placeholder = QtCore.QRectF(region['rect'])
        self.original = pie.layout()
        self.footer_rect = QtCore.QRectF(self.canvas.footer.geometry())
        self.original_height = self.canvas.height()
        self.setGeometry(self.canvas.rect())
        self.show()
        self.raise_()
        QtWidgets.QApplication.instance().installEventFilter(self)
        self.canvas.setCursor(QtCore.Qt.ClosedHandCursor)
        self.canvas.update_footer()
        self.canvas.update()

    def begin_external(self, action, point, pointer_owner=None):
        node=pie.entry(action)
        metrics=QtGui.QFontMetrics(self.canvas.menu_font())
        width=min(290,max(90,metrics.horizontalAdvance(node['label'])+38))
        rect=QtCore.QRectF(point.x()-width*.5,point.y()-14,width,28)
        region=dict(node=node,rect=rect,direction=None,parent=None,before=None)
        self.begin(region,point)
        self.external=node
        self.pointer_owner=pointer_owner
        self.entries[node['id']]=region
        self.positions[node['id']]=QtCore.QRectF(rect)
        self.move_entry(point)

    def animate(self, start, end, setter):
        if start == end:
            setter(end)
            return
        animation = QtCore.QVariantAnimation()
        animation.setDuration(self.DURATION)
        animation.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.valueChanged.connect(setter)
        self.group.addAnimation(animation)

    def set_position(self, identifier, value):
        self.positions[identifier] = value
        self.update()

    def set_placeholder(self, value):
        self.placeholder = value
        self.update()

    def set_floating(self, value):
        self.floating = value
        self.update()

    def set_floating_opacity(self, value):
        self.floating_opacity = value
        self.update()

    def move_entry(self, point):
        if not self.source or self.settling:
            return
        self.floating.moveTopLeft(point-self.offset)
        spec = self.canvas.drop_spec(point)
        target = self.hit(point)
        if spec != self.spec:
            self.spec = spec
            try:
                payload=dict(action=self.external['action'],id=self.source) if self.external else dict(id=self.source)
                self.proposal = self.canvas.propose_drop(payload, spec) if spec else None
            except ValueError:
                self.proposal = None
            self.canvas.reflow()
            self.entries.update({r['node']['id']: dict(r) for r in self.canvas.regions if r['node']})
            target_rects = {r['node']['id']: r['rect'] for r in self.canvas.regions if r['node']}
            destination = target_rects.get(self.source, target['rect'] if target else self.positions[self.source])
            self.group.stop()
            self.group.clear()
            for identifier in self.positions:
                if identifier != self.source:
                    self.animate(self.positions[identifier], target_rects.get(identifier, self.positions[identifier]),
                                 lambda value, identifier=identifier: self.set_position(identifier, value))
            self.animate(self.placeholder, QtCore.QRectF(destination), self.set_placeholder)
            self.group.start()
        self.canvas.update_footer(bool(spec and spec[0]=='remove'))
        self.update()

    def finish(self):
        if not self.source or self.settling:
            return
        if self.proposal is None or self.proposal == self.original:
            self.cancel()
            return
        # Publishing a proposal is a single editor history step, never a mesh edit.
        value = self.proposal
        removing = self.spec and self.spec[0]=='remove'
        self.settling = True
        self.proposal = None
        pie.publish(value)
        self.entries.update({r['node']['id']: dict(r) for r in self.canvas.regions if r['node']})
        target = next((r['rect'] for r in self.canvas.regions if r['node'] and r['node']['id'] == self.source),
                      self.placeholder)
        if removing:
            center=self.footer_rect.center()
            target=QtCore.QRectF(center.x()-3,center.y()-3,6,6)
        self.group.stop()
        self.group.clear()
        for region in self.canvas.regions:
            node = region['node']
            if node and node['id'] in self.positions and node['id'] != self.source:
                identifier = node['id']
                self.animate(self.positions[identifier], region['rect'],
                             lambda value, identifier=identifier: self.set_position(identifier, value))
        if not removing:
            self.animate(self.placeholder, QtCore.QRectF(target), self.set_placeholder)
        else:
            self.animate(self.floating_opacity,0.,self.set_floating_opacity)
        self.animate(self.floating, QtCore.QRectF(target), self.set_floating)
        if self.group.animationCount():
            self.group.start()
        else:
            self.clear()

    def animation_finished(self):
        if self.settling:
            self.clear()

    def clear(self):
        had_source=bool(self.source)
        self.group.stop()
        self.group.clear()
        self.source = None
        self.external = None
        self.pointer_owner = None
        self.settling = False
        self.proposal = self.spec = None
        self.footer_rect = QtCore.QRectF()
        self.original_height = 0
        self.floating_opacity = 1.
        self.positions.clear()
        self.entries.clear()
        self.hit_regions.clear()
        self.hit_panels.clear()
        self.hide()
        QtWidgets.QApplication.instance().removeEventFilter(self)
        self.canvas.setCursor(QtCore.Qt.OpenHandCursor)
        self.canvas.origin = self.canvas.pressed = None
        self.canvas.reflow()
        self.canvas.editor.updateGeometry()
        if hasattr(self.canvas.editor.window, 'adjust_popup_size'):
            self.canvas.editor.window.adjust_popup_size()
        if had_source:self.cleared.emit()

    def cancel(self):
        self.clear()

    def hit(self, point):
        return next((region for region in self.hit_regions if region['rect'].contains(point)), None)

    def panel_at(self, point):
        return next((panel for panel in self.hit_panels if panel['rect'].contains(point)), None)

    def eventFilter(self, watched, event):
        if self.pointer_owner is not None:
            return False  # The catalog owns pointer/keyboard cleanup for this drag.
        if self.source and not self.settling and watched is self.canvas.footer:
            if event.type()==QtCore.QEvent.MouseMove:
                point=QtCore.QPointF(self.canvas.mapFromGlobal(event.globalPosition().toPoint()))
                self.move_entry(point)
                event.accept()
                return True
            if event.type()==QtCore.QEvent.MouseButtonRelease and event.button()==QtCore.Qt.LeftButton:
                point=QtCore.QPointF(self.canvas.mapFromGlobal(event.globalPosition().toPoint()))
                self.move_entry(point)
                self.canvas.editor.edit(self.finish)
                event.accept()
                return True
        if event.type() in (QtCore.QEvent.ShortcutOverride, QtCore.QEvent.KeyPress) and event.key() == QtCore.Qt.Key_Escape:
            event.accept()
            if event.type() == QtCore.QEvent.KeyPress:
                external=bool(self.external)
                self.cancel()
                if external:return False  # Let the native QDrag loop cancel too.
            return True
        return False

    def paintEvent(self, event):
        if not self.source:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        # The same gray dashed silhouette and floating opacity as pinned cards.
        removing = self.spec and self.spec[0]=='remove'
        if not removing:
            painter.setPen(QtGui.QPen(QtGui.QColor('#727272'), 1, QtCore.Qt.DashLine))
            painter.setBrush(QtGui.QColor('#404040'))
            rect=self.placeholder.adjusted(1,1,-1,-1)
            painter.drawRoundedRect(rect,self.canvas.entry_radius(rect),self.canvas.entry_radius(rect))
        for identifier, region in self.entries.items():
            if identifier != self.source:
                self.canvas.paint_entry(painter, dict(region, rect=self.positions[identifier]))
        if self.external and not self.settling:
            painter.end()
            return  # The catalog ghost paints the pill across window boundaries.
        painter.setOpacity((1. if self.settling else .92)*self.floating_opacity)
        self.canvas.paint_entry(painter, dict(self.entries[self.source], rect=self.floating), hovered=True)
        painter.setOpacity(self.floating_opacity if removing else 1.)
        painter.setPen(QtGui.QPen(QtGui.QColor('#a0a0a0'), 1))
        painter.setBrush(QtCore.Qt.NoBrush)
        rect=self.floating.adjusted(1,1,-1,-1)
        painter.drawRoundedRect(rect,self.canvas.entry_radius(rect),self.canvas.entry_radius(rect))
        painter.end()
