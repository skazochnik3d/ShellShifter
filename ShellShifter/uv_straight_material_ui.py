"""Compact material picker, cooperative scene reads and quick UV selection."""
import sys
import time
from PySide6 import QtCore, QtGui, QtWidgets
import maya.api.OpenMaya as om
import maya.cmds as cmds
from uv_straight_materials import scan, select
from uv_straight_material_groups import sort_key
from uv_straight_icons import icon

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
        section = window.sections['materials']
        outer = section.layout()
        header_row = outer.takeAt(0).layout()
        header_row.setParent(None)
        outer.removeWidget(section.body)
        card = QtWidgets.QFrame(section)
        card.setObjectName('materialsCard')
        section.setStyleSheet(
            'QFrame#section_materials { background: transparent; border: 0; }'
            'QFrame#materialsCard { background: #363636; border: 0; border-radius: 12px; }')
        font = QtGui.QFont('Segoe UI')
        font.setPixelSize(12)
        section.setFont(font)
        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(0, 8, 0, 0)
        card_layout.setSpacing(0)
        card_layout.addLayout(header_row)
        card_layout.addWidget(section.body)
        outer.setSpacing(5)
        outer.addWidget(card)
        layout.setContentsMargins(8, 3, 8, 8)
        header_row.setContentsMargins(56, 0, 8, 0)
        header_row.setSpacing(0)
        section.header.setFixedHeight(24)
        section.header.setStyleSheet('QToolButton#categoryHeader { text-align: center; padding: 0; color: #f0f0f0; }')
        section.pin.setIconSize(QtCore.QSize(14, 14))
        def set_pin_icon(pinned):
            section.pin.setIcon(icon('material_pinned' if pinned else 'material_pin'))
        set_pin_icon(section.pin.isChecked())
        section.pin.toggled.connect(set_pin_icon)
        self.refresh_button = QtWidgets.QToolButton()
        self.refresh_button.setObjectName('materialRefresh')
        self.refresh_button.setIcon(icon('material_refresh'))
        self.refresh_button.setIconSize(QtCore.QSize(14, 14))
        self.refresh_button.setFixedSize(24, 24)
        self.refresh_button.setToolTip('Refresh assigned materials')
        self.refresh_button.setAccessibleName('Refresh assigned materials')
        self.refresh_button.clicked.connect(self.refresh)
        header_row.insertWidget(header_row.indexOf(section.pin), self.refresh_button)
        self.refresh_button.setStyleSheet('QToolButton { padding: 0; border-radius: 5px; } '
                                         'QToolButton:hover { background: #525252; }')
        self.list = QtWidgets.QListWidget()
        self.list.setObjectName('materialPicker')
        self.list.setAccessibleName('Assigned materials')
        self.list.setToolTip('Choose a material to select its UVs and view its textures.')
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.list.setStyleSheet(
            'QListWidget {background:#292929;color:#eee;border:1px solid #555;border-radius:5px;}'
            'QListWidget::item {padding:4px 8px;}'
            'QListWidget::item:selected {background:#cfe1dc;color:#17292b;}')
        self.drop_viewport = self.list.viewport()
        self.list.currentItemChanged.connect(self.queue_selection)
        self.list.itemClicked.connect(self.queue_selection)
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
        self.resize_timer=QtCore.QTimer(self)
        self.resize_timer.setSingleShot(True)
        self.resize_timer.setInterval(16)
        self.resize_timer.timeout.connect(lambda:self.window.adjust_popup_size() if hasattr(self.window,'adjust_popup_size') else None)
        from uv_straight_material_texture_ui import TexturePanel
        self.textures=TexturePanel(self,body,outer)
        # Catch external files on the material buttons and the empty panel too.
        self.view.setAcceptDrops(True)
        self.drop_viewport.setAcceptDrops(True)
        self.drop_viewport.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.view or watched is self.drop_viewport:
            if event.type() in (QtCore.QEvent.DragEnter,QtCore.QEvent.DragMove,QtCore.QEvent.Drop):
                from uv_straight_texture_import_ui import mime_paths
                paths=mime_paths(event.mimeData())
                if paths:
                    if event.type()==QtCore.QEvent.Drop:self.textures.queue_import(paths)
                    event.acceptProposedAction();return True
                event.ignore();return True
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
        self.textures.invalidate()

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
        self.resize_timer.stop()
        self.cancel_scan()
        self.remove_callbacks(self.callbacks)
        self.remove_callbacks(self.rename_callbacks)
        self.textures.pause()

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
        elif self.active and not self.scene_busy():
            self.textures.check_dirty()

    def scene_busy(self):
        packing = sys.modules.get('uv_straight_pack_progress')
        native = sys.modules.get('uv_straight_native')
        transfer = sys.modules.get('uv_straight_transfer_ui')
        return bool((packing is not None and packing.is_running()) or
                    (transfer is not None and transfer.is_running()) or
                    (native is not None and native.uv_edit_busy()))

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
        current = self.current_record()
        selected = current.uuid if current is not None else None
        self.records = records = tuple(sorted(records, key=lambda record: (sort_key(record.name), record.uuid)))
        with QtCore.QSignalBlocker(self.list):
            self.list.clear()
            for record in records:
                item = QtWidgets.QListWidgetItem(record.name)
                item.setData(QtCore.Qt.UserRole, record)
                item.setToolTip('{}\nObjects: {} · UV sets: {}'.format(
                    record.name, record.objects, ', '.join(record.uv_sets)))
                self.list.addItem(item)
            self.list.setCurrentRow(next((i for i, record in enumerate(records) if record.uuid == selected), -1))
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
        self.update_textures()

    def current_record(self):
        item = self.list.currentItem()
        return item.data(QtCore.Qt.UserRole) if item is not None else None

    def resize_list(self):
        row_height = max(22, self.list.sizeHintForRow(0))
        self.list.setFixedHeight(min(9, max(3, self.list.count())) * row_height + 2)
        self.adjust_size()

    def adjust_size(self):
        if hasattr(self.window, 'adjust_popup_size'):
            # Coalesce requests after Qt propagates size hints through the
            # scroll area. A zero-delay callback can still see the old height.
            self.resize_timer.start()

    def queue_selection(self, index=None, *unused):
        # Only user activation selects UVs; scans and scene refreshes never do.
        self.selection_request += 1
        revision = self.selection_request
        record = self.current_record()
        records = (record,) if record is not None else ()
        def apply():
            if not self.stopped and revision == self.selection_request:
                if self.scene_busy():
                    QtCore.QTimer.singleShot(50, apply)
                else:
                    self.update_textures()
                    self.select_uvs(records)
        QtCore.QTimer.singleShot(0, apply)

    def update_textures(self, *unused):
        self.textures.set_material(self.current_record())

    def select_uvs(self, records):
        try:
            uvs, objects = select(records)
            self.status.setText('Selected: {} UVs · {} object{}'.format(uvs, objects, '' if objects == 1 else 's'))
            self.status.hide()
        except (RuntimeError, ValueError, TypeError) as error:
            self.status.setText(str(error))
            self.status.show()
            self.dirty = True
