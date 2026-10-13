"""Short-lived Qt snapshots animate section dragging without changing scene data."""
try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets


class SectionDragOverlay(QtWidgets.QWidget):
    DURATION = 320

    def __init__(self, window, contents, stack):
        super().__init__(contents)
        self.owner = window
        self.contents = contents
        self.stack = stack
        self.source = None
        self.settling = False
        self.snapshots = {}
        self.positions = {}
        self.floating = QtCore.QPoint()
        self.placeholder = QtCore.QRect()
        self.group = QtCore.QParallelAnimationGroup(self)
        self.group.finished.connect(self.animation_finished)
        self.setObjectName('sectionDragOverlay')
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.hide()

    def begin(self, section):
        if self.settling:
            self.clear()
        self.source = section.objectName().replace('section_', '', 1)
        self.original_order = list(self.owner.pinned_order)
        self.snapshots = {key: widget.grab() for key, widget in self.owner.sections.items()
                          if not widget.isHidden()}
        self.positions = {key: self.owner.sections[key].pos() for key in self.snapshots}
        start = section.header.drag_start or QtCore.QPoint(15, 15)
        self.offset = section.header.mapTo(section, start)
        self.floating = section.pos()
        self.placeholder = section.geometry()
        self.owner.drag_source_key = self.source
        self.owner.drag_target_index = None
        self.setGeometry(self.contents.rect())
        self.show()
        self.raise_()
        QtWidgets.QApplication.instance().installEventFilter(self)

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

    def set_position(self, key, value):
        self.positions[key] = value
        self.update()

    def set_placeholder(self, value):
        self.placeholder = value
        self.update()

    def set_floating(self, value):
        self.floating = value
        self.update()

    def move_section(self, section, global_position):
        if not section.pin.isChecked():
            return
        if self.source is None or self.settling:
            self.begin(section)
        local = self.contents.mapFromGlobal(global_position)
        self.floating = QtCore.QPoint(section.x(), local.y() - self.offset.y())
        pinned = [key for key in self.original_order
                  if self.owner.sections[key].pin.isChecked() and key != self.source]
        center = self.floating.y() + section.height() / 2
        index = next((i for i, key in enumerate(pinned)
                      if center < self.owner.sections[key].geometry().center().y()), len(pinned))
        if index != self.owner.drag_target_index:
            self.owner.drag_target_index = index
            pinned.insert(index, self.source)
            order = pinned + [key for key in self.original_order if key not in pinned]
            self.owner.apply_section_order(order)
            self.stack.activate()
            self.group.stop()
            self.group.clear()
            for key in self.snapshots:
                if key != self.source:
                    self.animate(self.positions[key], self.owner.sections[key].pos(),
                                 lambda value, key=key: self.set_position(key, value))
            self.animate(self.placeholder, section.geometry(), self.set_placeholder)
            self.group.start()
        self.update()

    def finish(self):
        if self.source is None or self.settling:
            return
        self.settling = True
        key = self.source
        self.owner.reorder_pinned(key, self.owner.drag_target_index)
        self.stack.activate()
        self.group.stop()
        self.group.clear()
        for other in self.snapshots:
            if other != key:
                self.animate(self.positions[other], self.owner.sections[other].pos(),
                             lambda value, other=other: self.set_position(other, value))
        self.animate(self.placeholder, self.owner.sections[key].geometry(), self.set_placeholder)
        self.animate(self.floating, self.owner.sections[key].pos(), self.set_floating)
        self.owner.drag_source_key = None
        self.owner.drag_target_index = None
        if self.group.animationCount():
            self.group.start()
        else:
            self.clear()

    def animation_finished(self):
        if self.settling:
            self.clear()

    def clear(self):
        self.group.stop()
        self.group.clear()
        self.hide()
        self.source = None
        self.settling = False
        self.snapshots.clear()
        self.positions.clear()
        QtWidgets.QApplication.instance().removeEventFilter(self)

    def cancel(self):
        if self.source is not None and not self.settling:
            header = self.owner.sections[self.source].header
            header.dragging = False
            header.drag_start = None
            header.setDown(False)
            header.setCursor(QtCore.Qt.OpenHandCursor)
            self.owner.apply_section_order()
            self.stack.activate()
        self.owner.drag_source_key = None
        self.owner.drag_target_index = None
        self.clear()

    def eventFilter(self, watched, event):
        if (event.type() in (QtCore.QEvent.ShortcutOverride, QtCore.QEvent.KeyPress)
                and event.key() == QtCore.Qt.Key_Escape):
            event.accept()
            if event.type() == QtCore.QEvent.KeyPress:
                self.cancel()
            return True
        return False

    def paintEvent(self, event):
        if self.source is None:
            return
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor('#303030'))
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor('#727272'), 1, QtCore.Qt.DashLine))
        painter.setBrush(QtGui.QColor('#404040'))
        painter.drawRoundedRect(QtCore.QRectF(self.placeholder).adjusted(1, 1, -1, -1), 14, 14)
        for key, snapshot in self.snapshots.items():
            if key != self.source:
                painter.drawPixmap(self.positions[key], snapshot)
        painter.setOpacity(.92 if not self.settling else 1.)
        painter.drawPixmap(self.floating, self.snapshots[self.source])
        painter.setOpacity(1.)
        painter.setPen(QtGui.QPen(QtGui.QColor('#a0a0a0'), 1))
        painter.setBrush(QtCore.Qt.NoBrush)
        source = self.owner.sections[self.source]
        painter.drawRoundedRect(QtCore.QRectF(self.floating.x(), self.floating.y(),
                                             source.width(), source.height()).adjusted(1, 1, -1, -1), 14, 14)
        painter.end()
