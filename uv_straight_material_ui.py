"""Two-column material buttons, cooperative scene reads and quick UV selection."""
import sys
import time
from PySide6 import QtCore, QtGui, QtWidgets
import maya.api.OpenMaya as om
import maya.cmds as cmds
from uv_straight_materials import scan, select
from uv_straight_material_groups import sort_key

CELL_HEIGHT = 38


class MaterialDelegate(QtWidgets.QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QtCore.QSize(120, CELL_HEIGHT)

    def paint(self, painter, option, index):
        if index.data(QtCore.Qt.UserRole) is None:
            return
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QtCore.QRectF(option.rect).adjusted(3.5, 3.5, -3.5, -3.5)
        selected = bool(option.state & QtWidgets.QStyle.State_Selected)
        hovered = bool(option.state & QtWidgets.QStyle.State_MouseOver)
        enabled = bool(option.state & QtWidgets.QStyle.State_Enabled)
        painter.setBrush(QtGui.QColor('#575d63' if selected else '#505050' if hovered else '#484848'))
        painter.setPen(QtGui.QPen(QtGui.QColor('#8c9297'), 1) if selected else QtCore.Qt.NoPen)
        painter.drawRoundedRect(rect, 7, 7)
        painter.setFont(option.font)
        painter.setPen(QtGui.QColor('#eeeeee' if enabled else '#888888'))
        text_rect = rect.adjusted(9, 0, -9, 0)
        title = QtGui.QFontMetrics(option.font).elidedText(
            str(index.data(QtCore.Qt.DisplayRole)), QtCore.Qt.ElideRight, max(0, int(text_rect.width())))
        painter.drawText(text_rect, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, title)
        painter.restore()


class MaterialGrid(QtWidgets.QTableWidget):
    """Equal-width columns with selection ranges following the reading order."""
    keyboard_selected = QtCore.Signal()

    def __init__(self):
        super().__init__(0, 2)
        self.anchor = None
        self.horizontalHeader().hide()
        self.verticalHeader().hide()
        self.horizontalHeader().setMinimumSectionSize(0)
        self.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.verticalHeader().setDefaultSectionSize(CELL_HEIGHT)
        self.verticalHeader().setMinimumSectionSize(CELL_HEIGHT)
        self.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Fixed)
        self.setShowGrid(False)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectItems)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.setDragDropMode(QtWidgets.QAbstractItemView.NoDragDrop)

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if event.button() != QtCore.Qt.LeftButton or item is None or item.data(QtCore.Qt.UserRole) is None:
            super().mousePressEvent(event)
            return
        index = self.row(item)*2+self.column(item)
        modifiers = event.modifiers()
        toggle = bool(modifiers & (QtCore.Qt.ControlModifier | QtCore.Qt.MetaModifier))
        previous = tuple(self.selectedItems())
        was_selected = item.isSelected()
        # Let Qt finish its normal press/release path before Maya selection is queued.
        super().mousePressEvent(event)
        self.clearSelection()
        if toggle:
            for current in previous:
                current.setSelected(True)
        if modifiers & QtCore.Qt.ShiftModifier:
            first = self.anchor if self.anchor is not None else index
            for position in range(min(first, index), max(first, index)+1):
                current = self.item(position//2, position%2)
                if current is not None and current.data(QtCore.Qt.UserRole) is not None:
                    current.setSelected(True)
        else:
            item.setSelected(not was_selected if toggle else True)
            self.anchor = index

    def keyPressEvent(self, event):
        super().keyPressEvent(event)
        if event.key() in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Right, QtCore.Qt.Key_Up,
                           QtCore.Qt.Key_Down, QtCore.Qt.Key_Home, QtCore.Qt.Key_End,
                           QtCore.Qt.Key_PageUp, QtCore.Qt.Key_PageDown, QtCore.Qt.Key_Space,
                           QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            item = self.currentItem()
            if item is not None:
                self.anchor = self.row(item)*2+self.column(item)
            self.keyboard_selected.emit()


class MaterialController(QtCore.QObject):
    def __init__(self, window, layout):
        super().__init__(window)
        self.window = window
        self.active = self.stopped = False
        self.dirty = True
        self.steps = None
        self.records = ()
        self.callbacks = []
        self.rename_callbacks = []
        self.selection_request = 0
        self.view = QtWidgets.QWidget()
        self.view.setObjectName('materialBrowser')
        self.view.setStyleSheet(
            'QWidget#materialBrowser { background: transparent; }'
            'QTableWidget#materialRows { border: 0; background: transparent; outline: 0; color: #dedede; }'
            'QTableWidget#materialRows::item { border: 0; padding: 0; background: transparent; }'
            'QTableWidget#materialRows::item:selected { background: transparent; }'
            'QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }'
            'QScrollBar::handle:vertical { background: #666666; border-radius: 4px; min-height: 24px; }'
            'QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }'
            'QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }')
        body = QtWidgets.QVBoxLayout(self.view)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        self.refresh_button = QtWidgets.QToolButton()
        self.refresh_button.setObjectName('materialRefresh')
        refresh_pixmap = QtGui.QPixmap(32, 32)
        refresh_pixmap.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(refresh_pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        pen = QtGui.QPen(QtGui.QColor('#dedede'), 2.)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawArc(QtCore.QRectF(6, 6, 20, 20), 35 * 16, 290 * 16)
        painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(24, 5),
            QtCore.QPointF(25, 12), QtCore.QPointF(18, 11)]))
        painter.end()
        self.refresh_button.setIcon(QtGui.QIcon(refresh_pixmap))
        self.refresh_button.setIconSize(QtCore.QSize(18, 18))
        self.refresh_button.setFixedSize(24, 24)
        self.refresh_button.setToolTip('Refresh assigned materials')
        self.refresh_button.setAccessibleName('Refresh assigned materials')
        self.refresh_button.clicked.connect(self.refresh)
        section = window.sections['materials']
        header_row = section.layout().itemAt(0).layout()
        header_row.insertWidget(header_row.indexOf(section.pin), self.refresh_button)
        self.refresh_button.setStyleSheet('QToolButton { padding: 0; border-radius: 10px; } '
                                         'QToolButton:hover { background: #525252; }')
        self.list = MaterialGrid()
        self.list.setObjectName('materialRows')
        self.list.setMouseTracking(True)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.list.setItemDelegate(MaterialDelegate(self.list))
        self.list.setFixedHeight(CELL_HEIGHT)
        self.list.setToolTip('Click to select the material’s UVs. Ctrl adds materials; Shift selects a range. Uses each object’s current UV set.')
        self.list.itemClicked.connect(self.queue_selection)
        self.list.keyboard_selected.connect(self.queue_selection)
        body.addWidget(self.list)
        self.empty = QtWidgets.QLabel('No materials with UVs found.')
        self.empty.setWordWrap(True)
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.setMinimumHeight(40)
        self.empty.hide()
        body.addWidget(self.empty)
        self.status = QtWidgets.QLabel('Open this section to read assigned materials.')
        self.status.setWordWrap(True)
        self.status.setStyleSheet('color: #aaaaaa;')
        body.addWidget(self.status)
        self.status.hide()
        layout.addWidget(self.view)
        self.view.installEventFilter(self)
        window.installEventFilter(self)
        window.destroyed.connect(self.stop)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.advance)
        self.poll = QtCore.QTimer(self)
        self.poll.setInterval(150)
        self.poll.timeout.connect(self.check_dirty)

    def eventFilter(self, watched, event):
        if watched is self.view:
            if event.type() == QtCore.QEvent.Show:
                self.activate()
            elif event.type() == QtCore.QEvent.Hide and not getattr(self.window, 'popup_flag_change', False):
                self.pause()
        if watched is self.window:
            if event.type() == QtCore.QEvent.Close:
                self.stop()
            elif event.type() == QtCore.QEvent.Hide and not getattr(self.window, 'popup_flag_change', False):
                self.pause()
        return False

    def invalidate(self, *unused):
        # DG and scene callbacks only mark stale data. All Maya reads happen
        # later on the GUI thread, outside the triggering Maya operation.
        self.dirty = True

    def activate(self):
        if self.stopped or self.active:
            return
        self.active = True
        self.callbacks = [om.MSceneMessage.addCallback(event, self.invalidate) for event in
                          (om.MSceneMessage.kAfterNew, om.MSceneMessage.kAfterOpen, om.MSceneMessage.kAfterImport)]
        self.callbacks.append(om.MDGMessage.addConnectionCallback(self.invalidate))
        for kind in ('mesh', 'shadingEngine'):
            self.callbacks.append(om.MDGMessage.addNodeAddedCallback(self.invalidate, kind))
            self.callbacks.append(om.MDGMessage.addNodeRemovedCallback(self.invalidate, kind))
        events = set(om.MEventMessage.getEventNames())
        for name in ('Undo', 'Redo', 'NameChanged', 'PolyUVSetChanged', 'PolyUVSetDeleted'):
            if name in events:
                self.callbacks.append(om.MEventMessage.addEventCallback(name, self.invalidate))
        self.poll.start()
        self.refresh()

    @staticmethod
    def remove_callbacks(callbacks):
        for callback in callbacks:
            try:
                om.MMessage.removeCallback(callback)
            except RuntimeError:
                pass
        callbacks.clear()

    def cancel_scan(self):
        self.timer.stop()
        if self.steps is not None:
            self.steps.close()
            self.steps = None

    def pause(self):
        self.active = False
        self.dirty = True
        self.poll.stop()
        self.cancel_scan()
        self.remove_callbacks(self.callbacks)
        self.remove_callbacks(self.rename_callbacks)

    def stop(self, *unused):
        self.stopped = True
        try:
            self.pause()
        except RuntimeError:
            self.remove_callbacks(self.callbacks)
            self.remove_callbacks(self.rename_callbacks)

    def check_dirty(self):
        if self.dirty and self.steps is None:
            self.refresh()

    def scene_busy(self):
        packing = sys.modules.get('uv_straight_pack_progress')
        return bool(packing is not None and packing.is_running())

    def refresh(self, *unused):
        if not self.active or self.stopped:
            self.dirty = True
            return
        self.cancel_scan()
        self.remove_callbacks(self.rename_callbacks)
        self.dirty = False
        self.steps = scan()
        self.list.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.status.setText('Reading materials…')
        self.status.show()
        self.timer.start(0)

    def advance(self):
        if not self.active or self.steps is None:
            return
        if self.scene_busy():
            self.timer.start(100)
            return
        deadline = time.perf_counter() + .010
        try:
            while time.perf_counter() < deadline:
                next(self.steps)
        except StopIteration as finished:
            self.steps = None
            self.replace_records(finished.value)
            return
        except Exception as error:
            self.cancel_scan()
            self.list.setEnabled(True)
            self.refresh_button.setEnabled(True)
            self.status.setText(str(error))
            self.status.show()
            return
        self.timer.start(1)

    def replace_records(self, records):
        selected = {item.data(QtCore.Qt.UserRole).uuid for item in self.list.selectedItems()
                    if item.data(QtCore.Qt.UserRole) is not None}
        self.records = records = tuple(sorted(records, key=lambda record: (sort_key(record.name), record.uuid)))
        with QtCore.QSignalBlocker(self.list):
            self.list.clearContents()
            self.list.setRowCount((len(records)+1)//2)
            self.list.anchor = None
            for index, record in enumerate(records):
                item = QtWidgets.QTableWidgetItem(record.name)
                self.list.setItem(index//2, index%2, item)
                item.setData(QtCore.Qt.UserRole, record)
                item.setToolTip('{}\n{} objects · UV sets: {}\nClick to select UVs; Ctrl/Shift for multiple materials.'.format(
                    record.name, record.objects, ', '.join(record.uv_sets)))
                item.setSelected(record.uuid in selected)
            if len(records)%2:
                empty = QtWidgets.QTableWidgetItem()
                empty.setFlags(QtCore.Qt.NoItemFlags)
                self.list.setItem(len(records)//2, 1, empty)
        self.list.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.resize_list()
        for record in records:
            try:
                nodes = om.MSelectionList()
                nodes.add(record.name)
                self.rename_callbacks.append(om.MNodeMessage.addNameChangedCallback(nodes.getDependNode(0), self.invalidate))
            except RuntimeError:
                pass
        self.list.setVisible(bool(records))
        self.empty.setVisible(not records)
        self.status.setText('{} material{}'.format(len(records), '' if len(records) == 1 else 's'))
        self.status.hide()
        self.adjust_size()

    def material_items(self):
        return tuple(self.list.item(index//2, index%2) for index in range(len(self.records)))

    def resize_list(self):
        rows = self.list.rowCount()
        self.list.setFixedHeight(min(280, max(CELL_HEIGHT, rows*CELL_HEIGHT)))
        self.adjust_size()

    def adjust_size(self):
        if hasattr(self.window, 'adjust_popup_size'):
            self.window.adjust_popup_size()

    def queue_selection(self, item=None, *unused):
        # Finish the mouse event before changing Maya's component selection.
        # Keep the clicked records even if the list refreshes before applying.
        self.selection_request += 1
        revision = self.selection_request
        records = tuple(item.data(QtCore.Qt.UserRole) for item in self.list.selectedItems()
                        if item.data(QtCore.Qt.UserRole) is not None)
        def apply():
            if not self.stopped and revision == self.selection_request:
                self.select_uvs(records)
        QtCore.QTimer.singleShot(0, apply)

    def select_uvs(self, records):
        try:
            uvs, objects = select(records)
            self.status.setText('Selected: {} UVs · {} object{}'.format(uvs, objects, '' if objects == 1 else 's'))
            self.status.hide()
        except (RuntimeError, ValueError, TypeError) as error:
            self.status.setText(str(error))
            self.status.show()
            self.dirty = True
