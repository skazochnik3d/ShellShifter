"""Compact material picker, cooperative scene reads and quick UV selection."""
from collections import OrderedDict
from pathlib import Path
import sys
import time
from PySide2 import QtCore, QtGui, QtWidgets
from uv_straight_qt5 import signals_blocked
import maya.api.OpenMaya as om
import maya.cmds as cmds
from uv_straight_materials import scan, select
from uv_straight_material_groups import sort_key
from uv_straight_icons import icon

_VIEW_OPTION = 'shellShifterMaterialsView'


def _material_sphere(material, texture=None):
    color = QtGui.QColor('#8b9299')
    for attribute in ('baseColor', 'color', 'outColor'):
        plug = material + '.' + attribute
        if not cmds.objExists(plug):
            continue
        try:
            channels = cmds.getAttr(plug)[0]
            color = QtGui.QColor.fromRgbF(*(max(0., min(1., value)) for value in channels[:3]))
            break
        except (RuntimeError, TypeError, ValueError, IndexError):
            continue
    if texture is not None and not texture.isNull():
        color = QtGui.QColor(255, 255, 255, 0)
    elif color.value() < 40:
        color = QtGui.QColor('#42474d')
    pixmap = QtGui.QPixmap(48, 48)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    gradient = QtGui.QRadialGradient(24, 24, 24, 16, 13)
    gradient.setColorAt(0., color.lighter(175))
    gradient.setColorAt(.55, color)
    gradient.setColorAt(.92, color.darker(240))
    gradient.setColorAt(1., QtGui.QColor('#25282a'))
    if texture is not None and not texture.isNull():
        circle = QtGui.QPainterPath()
        circle.addEllipse(2, 2, 44, 44)
        painter.setClipPath(circle)
        scaled = texture.scaled(44, 44, QtCore.Qt.KeepAspectRatioByExpanding,
                                QtCore.Qt.SmoothTransformation)
        painter.drawImage((48 - scaled.width()) // 2, (48 - scaled.height()) // 2, scaled)
        gradient.setColorAt(0., QtGui.QColor(255, 255, 255, 64))
        gradient.setColorAt(.55, QtGui.QColor(0, 0, 0, 0))
        gradient.setColorAt(.92, QtGui.QColor(0, 0, 0, 120))
        gradient.setColorAt(1., QtGui.QColor(0, 0, 0, 180))
    painter.setPen(QtCore.Qt.NoPen)
    painter.setBrush(gradient)
    painter.drawEllipse(2, 2, 44, 44)
    painter.end()
    return QtGui.QIcon(pixmap)


class MaterialSphereGrid(QtWidgets.QListWidget):
    CELL_HEIGHT = 78

    def __init__(self):
        super().__init__()
        self.setObjectName('materialSphereGrid')
        self.setAccessibleName('Material spheres')
        self.setViewMode(QtWidgets.QListView.IconMode)
        self.setMovement(QtWidgets.QListView.Static)
        self.setFlow(QtWidgets.QListView.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QtWidgets.QListView.Adjust)
        self.setIconSize(QtCore.QSize(48, 48))
        self.setUniformItemSizes(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setStyleSheet(
            'QListWidget {background:#292929;color:#eee;border:1px solid #515151;border-radius:6px;}'
            'QListWidget::item {padding:4px 2px;border-radius:5px;}'
            'QListWidget::item:hover {background:#474747;}'
            'QListWidget::item:selected {background:#cfe1dc;color:#17292b;}')

    def resizeEvent(self, event):
        self.setGridSize(QtCore.QSize(max(1, self.viewport().width() // 3), self.CELL_HEIGHT))
        super().resizeEvent(event)

    def fit_rows(self):
        rows = (self.count() + 2) // 3
        self.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarAlwaysOn if rows > 3 else QtCore.Qt.ScrollBarAlwaysOff)
        self.setFixedHeight(max(1, min(3, rows)) * self.CELL_HEIGHT + 2)


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
        header_row.setContentsMargins(8, 0, 8, 0)
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
        self.sphere_mode = (cmds.optionVar(query=_VIEW_OPTION) == 'spheres'
                            if cmds.optionVar(exists=_VIEW_OPTION) else False)
        self.mode_buttons = QtWidgets.QButtonGroup(self)
        self.mode_buttons.setExclusive(True)
        for label, sphere_mode in (('List', False), ('Spheres', True)):
            control = QtWidgets.QToolButton()
            control.setText(label)
            control.setCheckable(True)
            control.setChecked(sphere_mode == self.sphere_mode)
            control.setToolTip('Show material spheres' if sphere_mode else 'Show the material dropdown')
            control.setStyleSheet(
                'QToolButton {padding:3px 5px;border-radius:4px;color:#eee;background:#444;}'
                'QToolButton:checked {color:#17292b;background:#cfe1dc;}')
            control.clicked.connect(lambda checked=False, spheres=sphere_mode: self.set_material_view(spheres))
            self.mode_buttons.addButton(control, int(sphere_mode))
            header_row.insertWidget(header_row.indexOf(self.refresh_button), control)
        self.list = QtWidgets.QComboBox()
        self.list.setObjectName('materialPicker')
        self.list.setMinimumWidth(0)
        self.list.setFixedHeight(29)
        self.list.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.list.setMinimumContentsLength(1)
        self.list.setMaxVisibleItems(16)
        self.list.setPlaceholderText('Select material…')
        self.list.setAccessibleName('Assigned materials')
        self.list.setToolTip("Choose a material to select its UVs and view its textures.")
        self.list.setStyleSheet(
            'QComboBox {background:#292929;color:#f0f0f0;border:1px solid #5b5b5b;'
            'border-radius:5px;padding:0 8px;min-height:27px;max-height:27px;}'
            'QComboBox::drop-down {border:0;width:24px;}'
            'QComboBox QAbstractItemView {background:#292929;color:#eeeeee;'
            'border:1px solid #666666;selection-background-color:#cfe1dc;selection-color:#17292b;}')
        self.drop_viewport = self.list
        self.list.activated.connect(self.queue_selection)
        body.addWidget(self.list)
        self.spheres = MaterialSphereGrid()
        self.spheres.itemClicked.connect(self.select_sphere)
        self.spheres.itemActivated.connect(self.select_sphere)
        self.spheres.hide()
        self.list.setVisible(not self.sphere_mode)
        body.addWidget(self.spheres)
        sphere_viewport = self.spheres.viewport()
        sphere_viewport.setAcceptDrops(True)
        sphere_viewport.installEventFilter(self)
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
        self.sphere_revision = 0
        self.sphere_jobs = {}
        self.sphere_cache = OrderedDict()
        self.sphere_pool = QtCore.QThreadPool(self)
        self.sphere_pool.setMaxThreadCount(1)
        from uv_straight_texture_reload import start
        if not cmds.about(batch=True):
            start().changed.connect(self.sphere_files_changed)
        # Catch external files on the material buttons and the empty panel too.
        self.view.setAcceptDrops(True)
        self.drop_viewport.setAcceptDrops(True)
        self.drop_viewport.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched in (self.view, self.drop_viewport, self.spheres.viewport()):
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
        self.cancel_sphere_previews()

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
            self.status.setText(str(error))
            self.status.show()
            return
        self.timer.start(1)

    def replace_records(self, records):
        current = self.list.currentData()
        selected = current.uuid if current is not None else None
        records = tuple(sorted(records, key=lambda record: (sort_key(record.name), record.uuid)))
        same_items = tuple((record.uuid, record.name) for record in self.records) == tuple(
            (record.uuid, record.name) for record in records)
        self.records = records
        with signals_blocked(self.list):
            if not same_items:
                self.list.clear()
            for index, record in enumerate(records):
                if same_items:
                    self.list.setItemData(index, record)
                else:
                    self.list.addItem(record.name, record)
                self.list.setItemData(index,
                    '{}\nObjects: {} · UV sets: {}'.format(record.name, record.objects, ', '.join(record.uv_sets)),
                    QtCore.Qt.ToolTipRole)
            self.list.setCurrentIndex(next((i for i, record in enumerate(records) if record.uuid == selected), -1))
        for record in records:
            try:
                nodes = om.MSelectionList()
                nodes.add(record.name)
                self.rename_callbacks.append(om.MNodeMessage.addNameChangedCallback(nodes.getDependNode(0), self.invalidate))
            except RuntimeError:
                pass
        self.refresh_material_view()
        self.empty.setVisible(not records)
        self.status.setText('{} material{}'.format(len(records), '' if len(records) == 1 else 's'))
        self.status.hide()
        if same_items:
            self.textures.check_dirty()
        else:
            self.adjust_size()
            self.update_textures()

    def set_material_view(self, spheres):
        if self.sphere_mode == spheres:
            return
        self.sphere_mode = spheres
        cmds.optionVar(stringValue=(_VIEW_OPTION, 'spheres' if spheres else 'list'))
        self.refresh_material_view()
        self.adjust_size()

    def sphere_files_changed(self, paths):
        from uv_straight_texture_reload import path_key
        for key in tuple(self.sphere_cache):
            if path_key(key[0]) in paths:
                del self.sphere_cache[key]
        if self.active and self.sphere_mode:
            self.invalidate()

    def cancel_sphere_previews(self):
        self.sphere_revision += 1
        self.sphere_pool.clear()
        self.sphere_jobs.clear()

    def refresh_material_view(self, *unused):
        self.cancel_sphere_previews()
        self.list.setVisible(bool(self.records) and not self.sphere_mode)
        self.spheres.setVisible(bool(self.records) and self.sphere_mode)
        if not self.sphere_mode:
            return
        if tuple(self.spheres.item(index).data(QtCore.Qt.UserRole) for index in range(self.spheres.count())) != tuple(
                (record.uuid, record.name) for record in self.records):
            self.spheres.clear()
            for record in self.records:
                item = QtWidgets.QListWidgetItem(record.name)
                item.setData(QtCore.Qt.UserRole, (record.uuid, record.name))
                item.setToolTip(record.name)
                self.spheres.addItem(item)
            self.spheres.fit_rows()
        self.sync_sphere_selection()
        from uv_straight_material_texture_ui import ThumbnailTask
        from uv_straight_material_textures import input_spec, parked_input, preview_path, upstream_file

        for index, record in enumerate(self.records):
            item = self.spheres.item(index)
            item.setIcon(_material_sphere(record.name))
            try:
                plug, _, _ = input_spec(record.name, 'base_color')
                file, candidates = upstream_file(plug)
                if not candidates:
                    file, _ = upstream_file(parked_input(plug, 'base_color'))
                path = preview_path(cmds.getAttr(file + '.fileTextureName')) if file else ''
                if not path:
                    continue
                stat = Path(path).stat()
            except (RuntimeError, ValueError, OSError):
                continue
            key = (path, stat.st_mtime_ns, stat.st_size)
            if key in self.sphere_cache:
                item.setIcon(_material_sphere(record.name, self.sphere_cache[key]))
                self.sphere_cache.move_to_end(key)
                continue
            if path in self.sphere_jobs:
                self.sphere_jobs[path][1].append(index)
                continue
            task = ThumbnailTask(key, self.sphere_revision)
            self.sphere_jobs[path] = (task, [index])
            task.signals.ready.connect(self.sphere_preview_ready)
            self.sphere_pool.start(task)

    @QtCore.Slot(object, object, object)
    def sphere_preview_ready(self, key, revision, image):
        if revision != self.sphere_revision:
            return
        job = self.sphere_jobs.pop(key[0], None)
        if job is None:
            return
        if image.isNull():
            from uv_straight_material_texture_ui import maya_thumbnail
            image = maya_thumbnail(key[0])
        self.sphere_cache[key] = image
        while len(self.sphere_cache) > 80:
            self.sphere_cache.popitem(last=False)
        if image.isNull():
            return
        for index in job[1]:
            item = self.spheres.item(index)
            item.setIcon(_material_sphere(self.records[index].name, image))

    def sync_sphere_selection(self):
        if self.sphere_mode:
            self.spheres.setCurrentRow(self.list.currentIndex())

    def select_sphere(self, item):
        self.list.setCurrentIndex(self.spheres.row(item))
        self.queue_selection()

    def adjust_size(self):
        if hasattr(self.window, 'adjust_popup_size'):
            # Coalesce requests after Qt propagates size hints through the
            # scroll area. A zero-delay callback can still see the old height.
            self.resize_timer.start()

    def queue_selection(self, index=None, *unused):
        # Only user activation selects UVs; scans and scene refreshes never do.
        self.sync_sphere_selection()
        self.update_textures()
        self.selection_request += 1
        revision = self.selection_request
        record = self.list.currentData()
        records = (record,) if record is not None else ()
        def apply():
            if not self.stopped and revision == self.selection_request:
                if self.scene_busy():
                    QtCore.QTimer.singleShot(50, apply)
                else:
                    self.select_uvs(records)
        QtCore.QTimer.singleShot(0, apply)

    def update_textures(self, *unused):
        self.textures.set_material(self.list.currentData())

    def select_uvs(self, records):
        try:
            uvs, objects = select(records)
            self.status.setText('Selected: {} UVs · {} object{}'.format(uvs, objects, '' if objects == 1 else 's'))
            self.status.hide()
        except (RuntimeError, ValueError, TypeError) as error:
            self.status.setText(str(error))
            self.status.show()
            self.dirty = True
