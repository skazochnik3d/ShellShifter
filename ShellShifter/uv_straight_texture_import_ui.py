"""Review filename matches and import many material textures with one Undo."""
from collections import defaultdict
from os.path import commonprefix
from pathlib import Path
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid
import maya.cmds as cmds
import uv_straight_material_textures as textures
import uv_straight_texture_match as matching


def mime_paths(mime):
    if not mime.hasUrls():
        return ()
    urls = mime.urls()
    if not urls or any(not url.isLocalFile() for url in urls):
        return ()
    paths = tuple(url.toLocalFile() for url in urls)
    return paths if any(Path(path).is_dir() or Path(path).suffix.lower() in matching.EXTENSIONS
                        for path in paths) else ()


def has_alpha_channel(path):
    """Inspect stored channels, including opaque alpha; never infer from RGB."""
    path=Path(path)
    try:
        if path.suffix.lower()=='.tga':
            with path.open('rb') as stream:header=stream.read(18)
            if len(header)!=18:return False
            return (header[2] in (2,10) and header[16]==32 or
                    header[2] in (3,11) and header[16]==16) and bool(header[17]&15)
        reader=QtGui.QImageReader(str(path))
        if not reader.canRead():return False
        reader.setScaledSize(QtCore.QSize(1,1))
        image=reader.read()
        return not image.isNull() and image.hasAlphaChannel()
    except OSError:return False


class DialogDragHeader(QtWidgets.QWidget):
    """Move the frameless Assign dialog from the header and its labels."""
    def __init__(self,dialog):
        super().__init__(dialog)
        self.dialog=dialog;self.drag_offset=None
        self.setObjectName('assignTexturesDragHeader')
    def mousePressEvent(self,event):
        if event.button()==QtCore.Qt.LeftButton:
            self.drag_offset=event.globalPosition().toPoint()-self.dialog.pos()
            event.accept()
        else:super().mousePressEvent(event)
    def mouseMoveEvent(self,event):
        if self.drag_offset is not None and event.buttons()&QtCore.Qt.LeftButton:
            self.dialog.move(event.globalPosition().toPoint()-self.drag_offset)
            event.accept()
        else:super().mouseMoveEvent(event)
    def mouseReleaseEvent(self,event):
        if event.button()==QtCore.Qt.LeftButton and self.drag_offset is not None:
            self.drag_offset=None;event.accept()
        else:super().mouseReleaseEvent(event)


class ImportComboBox(QtWidgets.QComboBox):
    def __init__(self, scroll_view=None):
        super().__init__()
        self.scroll_view = scroll_view

    def wheelEvent(self, event):
        if self.scroll_view is not None:
            # Cell widgets receive the wheel before the table's viewport.
            position = event.globalPosition()
            forwarded = QtGui.QWheelEvent(
                QtCore.QPointF(self.scroll_view.mapFromGlobal(position.toPoint())),
                position, event.pixelDelta(), event.angleDelta(), event.buttons(),
                event.modifiers(), event.phase(), event.inverted(),
                event.source(), event.pointingDevice())
            QtWidgets.QApplication.sendEvent(self.scroll_view, forwarded)
        event.accept()

    def width_for_text(self, text_width):
        option = QtWidgets.QStyleOptionComboBox()
        self.initStyleOption(option)
        size = QtCore.QSize(text_width + 4, self.fontMetrics().height())
        return self.style().sizeFromContents(QtWidgets.QStyle.CT_ComboBox, option, size, self).width()


class TextureImportDialog(QtWidgets.QDialog):
    def __init__(self, controller, paths, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setAcceptDrops(True)
        self.setWindowFlag(QtCore.Qt.FramelessWindowHint,True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setWindowTitle('ShellShifter · Assign Textures')
        self.setStyleSheet(
            'QDialog {background:transparent;color:#eeeeee;}'
            'QFrame#assignTextureSurface {background:#363636;border:1px solid #666666;border-radius:14px;}'
            'QLabel,QCheckBox {color:#dedede;background:transparent;}'
            'QTableWidget {background:#363636;alternate-background-color:#3b3b3b;color:#eeeeee;border:0;}'
            'QHeaderView::section {background:#424242;color:#cccccc;border:0;padding:4px 6px;}'
            'QComboBox {background:#484848;color:#eeeeee;border:1px solid #606060;'
            'border-radius:4px;padding:2px 4px;}'
            'QComboBox QAbstractItemView {background:#383838;color:#eeeeee;selection-background-color:#59636b;}'
            'QPushButton {background:#505050;color:#eeeeee;border:0;border-radius:8px;padding:7px 14px;}'
            'QPushButton:hover {background:#626262;} QPushButton:disabled {color:#888888;}'
            'QPushButton#assignTextures {background:#426957;}')
        outer=QtWidgets.QVBoxLayout(self);outer.setContentsMargins(0,0,0,0)
        surface=QtWidgets.QFrame();surface.setObjectName('assignTextureSurface');outer.addWidget(surface)
        self.surface=surface
        layout=QtWidgets.QVBoxLayout(surface);layout.setContentsMargins(0,0,0,14);layout.setSpacing(0)
        self.drag_header=DialogDragHeader(self)
        header_layout=QtWidgets.QVBoxLayout(self.drag_header)
        header_layout.setContentsMargins(14,14,14,8);header_layout.setSpacing(8)
        layout.addWidget(self.drag_header)
        content=QtWidgets.QWidget(surface);layout.addWidget(content,1)
        body=QtWidgets.QVBoxLayout(content);body.setContentsMargins(14,0,14,0);body.setSpacing(8)
        title_row=QtWidgets.QHBoxLayout()
        self.drag_title=QtWidgets.QLabel('Assign Textures')
        self.drag_title.setObjectName('assignTexturesDragTitle')
        self.drag_title.setMinimumHeight(24)
        self.drag_title.setToolTip('Drag to move the window')
        title_row.addWidget(self.drag_title)
        self.target_label=QtWidgets.QLabel()
        self.target_label.setStyleSheet('color:#bdbdbd;')
        self.target_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Preferred)
        self.target_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        title_row.addWidget(self.target_label,1)
        close=QtWidgets.QToolButton();close.setText('×');close.setFixedSize(24,24);close.setToolTip('Close')
        close.setStyleSheet('QToolButton {color:#dddddd;background:transparent;border:0;font-size:18px;}')
        close.clicked.connect(self.reject);title_row.addWidget(close);header_layout.addLayout(title_row)
        header_layout.addWidget(QtWidgets.QLabel('AO routing:'))
        materials = [dict(name=record.name, uuid=record.uuid) for record in controller.records]
        if not materials:
            raise ValueError('No assigned materials with UVs were found. Refresh Materials.')
        self.paths = matching.collect_paths(paths)
        self.original_plan = matching.plan(self.paths, materials)
        self.alpha_cache={}
        self.ao_route = ImportComboBox()
        self.ao_route.addItem('AO → AO', 'ao')
        self.ao_route.addItem('AO → Base Color', 'base_color')
        self.ao_route.addItem('AO → Base Color + Opacity (Alpha)', 'base_color_alpha')
        self.ao_route.setMinimumWidth(0)
        self.ao_route.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Fixed)
        mode=cmds.optionVar(query='ssMaterialTextureAORouting') if cmds.optionVar(exists='ssMaterialTextureAORouting') else 'ao'
        self.ao_route.setCurrentIndex(max(0,self.ao_route.findData(mode)))
        self.ao_route.setToolTip('AO is added to Opacity only when every UDIM tile has an actual alpha channel.')
        body.addWidget(self.ao_route)
        self.details_button=QtWidgets.QToolButton()
        self.details_button.setText('Configure assignments');self.details_button.setCheckable(True)
        self.details_button.setArrowType(QtCore.Qt.RightArrow)
        self.details_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.details_button.setStyleSheet('QToolButton {color:#bdbdbd;background:transparent;border:0;padding:3px 0;}')
        self.details_button.toggled.connect(self.set_details_visible)
        body.addWidget(self.details_button)
        self.rows = []
        self.materials = materials
        self.existing = {}
        self.definitions = {}
        for material in materials:
            name = textures.resolve_material(material['uuid'])
            self.existing[material['uuid']] = textures.read(name)
            self.definitions[material['uuid']] = textures.slot_definitions(name)
        self.table = QtWidgets.QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(('File', 'Texture set', 'Map type', 'Channel', 'Status'))
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setFixedHeight(0)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Fixed)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.table.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        header.setSectionResizeMode(QtWidgets.QHeaderView.Fixed)
        body.addWidget(self.table, 1)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        body.addWidget(self.summary)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch()
        cancel = QtWidgets.QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        self.assign = QtWidgets.QPushButton('Assign')
        self.assign.setObjectName('assignTextures')
        self.assign.clicked.connect(self.apply)
        buttons.addWidget(cancel)
        buttons.addWidget(self.assign)
        body.addLayout(buttons)
        self.size_timer=QtCore.QTimer(self);self.size_timer.setSingleShot(True);self.size_timer.setInterval(16)
        self.size_timer.timeout.connect(self.resize_for_content)
        self.table.itemChanged.connect(lambda item: self.update_rows() if item.column() == 0 else None)
        self.ao_route.currentIndexChanged.connect(self.rebuild_rows)
        self.rebuild_rows()
        if any(entry.get('packed') for entry in self.original_plan):
            self.details_button.setChecked(True)
        from uv_straight_language import apply
        apply(self)
        self.resize_for_content()

    def dragEnterEvent(self, event):
        if mime_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        paths = mime_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        try:
            merged = matching.collect_paths((*self.paths, *paths))
            if merged != self.paths:
                previous = {(row['entry']['path'], row['entry']['key']): (
                    row['material'].currentData(), row['key'].currentData(),
                    row['channel'].currentData(), row['file'].checkState())
                    for row in self.rows}
                self.original_plan = matching.plan(merged, self.materials)
                self.paths = merged
                self.rebuild_rows()
                with QtCore.QSignalBlocker(self.table):
                    for row in self.rows:
                        old = previous.get((row['entry']['path'], row['entry']['key']))
                        if old is None:
                            continue
                        material, key, channel, checked = old
                        with QtCore.QSignalBlocker(row['material']), QtCore.QSignalBlocker(row['channel']):
                            row['material'].setCurrentIndex(max(0, row['material'].findData(material)))
                            self.populate_keys(row['key'], material, key)
                            row['channel'].setCurrentIndex(max(0, row['channel'].findData(channel)))
                            row['file'].setCheckState(checked)
                self.update_rows()
                if any(entry.get('packed') and (entry['path'],entry['key']) not in previous for entry in self.original_plan):
                    self.details_button.setChecked(True)
            event.acceptProposedAction()
        except (OSError, ValueError) as error:
            self.summary.setText(str(error))
            self.summary.show()
            event.ignore()

    def set_details_visible(self,visible):
        self.table.setVisible(visible)
        self.details_button.setArrowType(QtCore.Qt.DownArrow if visible else QtCore.Qt.RightArrow)
        self.summary.setVisible(bool(self.summary.text()))
        self.size_timer.start()

    def resize_for_content(self):
        if not isValid(self):return
        self.ensurePolished()
        available = self.screen().availableGeometry()
        max_width = available.width() - 24
        padding = 28 + 2 * self.surface.frameWidth()
        # Include the AO selector, which is also visible in the collapsed dialog.
        width = self.ao_route.sizeHint().width() + padding
        if self.details_button.isChecked():
            header = self.table.horizontalHeader()
            widths = [header.sectionSizeHint(column) for column in range(self.table.columnCount())]
            if self.rows:
                metrics = self.table.fontMetrics()
                for column, field in ((0, 'file'), (4, 'state')):
                    index = max(range(len(self.rows)), key=lambda i: metrics.horizontalAdvance(self.rows[i][field].text()))
                    item = self.table.model().index(index, column)
                    option = QtWidgets.QStyleOptionViewItem()
                    option.initFrom(self.table)
                    widths[column] = max(widths[column], self.table.itemDelegate().sizeHint(option, item).width() + 4)
                for column, field in ((1, 'material'), (2, 'key'), (3, 'channel')):
                    combo = self.rows[0][field]
                    metrics = combo.fontMetrics()
                    longest = max(metrics.horizontalAdvance(row[field].currentText()) for row in self.rows)
                    widths[column] = max(widths[column], combo.width_for_text(longest))
            for column, value in enumerate(widths):
                self.table.setColumnWidth(column, value)
            header_width = header.length()
            frame = 2 * self.table.frameWidth()
            row_height = self.ao_route.sizeHint().height() + 2
            self.table.verticalHeader().setDefaultSectionSize(row_height)
            base_height = header.sizeHint().height() + frame
            full_height = base_height + self.table.verticalHeader().length()
            self.layout().activate()
            chrome_height = self.sizeHint().height() - self.table.minimumHeight()
            table_height = min(full_height, base_height + row_height * 8,
                               max(base_height + row_height, available.height() - chrome_height - 24))
            scroll_width = self.table.verticalScrollBar().sizeHint().width()
            width = max(width, header_width + frame + padding + (scroll_width if full_height > table_height else 0))
            if width > max_width:
                table_height += self.table.horizontalScrollBar().sizeHint().height()
            self.table.setFixedHeight(table_height)
        self.layout().activate()
        width = min(max_width, max(width, self.minimumSizeHint().width()))
        height = self.layout().totalHeightForWidth(width)
        before = self.size()
        self.resize(width, height if height >= 0 else self.sizeHint().height())
        if self.isVisible() and (self.width() > before.width() or self.height() > before.height()):
            x = min(self.x(), available.x() + available.width() - self.width())
            y = min(self.y(), available.y() + available.height() - self.height())
            self.move(max(available.x(), x), max(available.y(), y))
        meta = self.target_name
        if self.details_button.isChecked() and self.file_prefix:
            meta = '{} · {}'.format(self.file_prefix, meta)
        self.target_label.setText(self.target_label.fontMetrics().elidedText(
            meta, QtCore.Qt.ElideMiddle, self.target_label.width()))

    def rebuild_rows(self,*unused):
        blocker=QtCore.QSignalBlocker(self.table)
        self.table.setRowCount(0)
        self.rows=[];self.plan=[]
        mode=self.ao_route.currentData()
        for entry in self.original_plan:
            if entry['key']=='ao' and mode!='ao' and not entry.get('packed'):
                self.plan.append(dict(entry,key='base_color',channel='rgb'))
                if mode=='base_color_alpha':
                    paths=entry['paths'] or (entry['path'],)
                    for path in paths:
                        if path not in self.alpha_cache:self.alpha_cache[path]=has_alpha_channel(path)
                    if all(self.alpha_cache[path] for path in paths):
                        self.plan.append(dict(entry,key='opacity',channel='alpha'))
            else:self.plan.append(dict(entry,channel=textures.source_channel(entry['key'],entry.get('channel'))))
        self.table.setRowCount(len(self.plan))
        viewport = self.table.viewport()
        stems = {Path(entry['path']).stem for entry in self.plan}
        shared = commonprefix(sorted(stems)) if len(stems) > 1 else ''
        end = max(shared.rfind(separator) for separator in '_- .') + 1
        prefix = shared[:end] if all(len(stem) > end for stem in stems) else ''
        self.file_prefix = prefix.rstrip('_- .')
        for index, entry in enumerate(self.plan):
            path = Path(entry['path'])
            suffix = ' · {} UDIM'.format(len(entry['paths'])) if len(entry['paths']) > 1 else ''
            file = QtWidgets.QTableWidgetItem(path.name[len(prefix):] + suffix)
            file.setToolTip('\n'.join(entry['paths']))
            file.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsUserCheckable)
            file.setCheckState(QtCore.Qt.Checked if entry['material'] and entry['key'] else QtCore.Qt.Unchecked)
            self.table.setItem(index, 0, file)
            material = ImportComboBox(viewport)
            material.setMinimumWidth(0)
            material.addItem('— choose set —', None)
            for record in self.materials:
                material.addItem(record['name'], record['uuid'])
            material.setCurrentIndex(max(0, material.findData(entry['material'])))
            self.table.setCellWidget(index, 1, material)
            key = ImportComboBox(viewport)
            key.setMinimumWidth(0)
            self.populate_keys(key, entry['material'], entry['key'])
            self.table.setCellWidget(index, 2, key)
            channel = ImportComboBox(viewport)
            for value, label in textures.CHANNELS:
                channel.addItem(label,value)
            channel.setCurrentIndex(max(0,channel.findData(entry['channel'])))
            channel.setToolTip('Choose RGB, an individual color channel, or the image alpha channel.')
            self.table.setCellWidget(index,3,channel)
            state = QtWidgets.QTableWidgetItem()
            self.table.setItem(index, 4, state)
            row = dict(file=file, material=material, key=key, channel=channel, state=state, entry=entry)
            self.rows.append(row)
            material.currentIndexChanged.connect(lambda unused, row=row: self.material_changed(row))
            key.currentIndexChanged.connect(lambda unused, row=row: self.key_changed(row))
            channel.currentIndexChanged.connect(lambda unused,row=row:self.choice_changed(row))
        del blocker
        self.update_rows()

    def populate_keys(self, combo, identity, selected):
        definitions = self.definitions.get(identity, [dict(key=key, label=label) for key, label in textures.SLOTS])
        with QtCore.QSignalBlocker(combo):
            combo.clear()
            combo.addItem('— choose type —', None)
            for definition in definitions:
                combo.addItem(definition['label'], definition['key'])
            combo.setCurrentIndex(max(0, combo.findData(selected)))

    def material_changed(self, row):
        self.populate_keys(row['key'], row['material'].currentData(), row['key'].currentData())
        self.choice_changed(row)

    def choice_changed(self, row):
        with QtCore.QSignalBlocker(self.table):
            row['file'].setCheckState(QtCore.Qt.Checked if row['material'].currentData() and row['key'].currentData()
                                     else QtCore.Qt.Unchecked)
        self.update_rows()

    def key_changed(self,row):
        key=row['key'].currentData()
        with QtCore.QSignalBlocker(row['channel']):
            if not row['entry'].get('packed'):
                row['channel'].setCurrentIndex(row['channel'].findData(textures.source_channel(key)))
        self.choice_changed(row)

    def update_rows(self, *unused):
        selected = defaultdict(list)
        self.entries = []
        for row in self.rows:
            identity, key = row['material'].currentData(), row['key'].currentData()
            row['destination'] = None
            row['signature'] = None
            rows = self.existing.get(identity, ())
            target = next((item for item in rows if item['key'] == key), None)
            row['target'] = target
            if target is not None:
                row['destination'] = (identity, target['plug'])
                if row['file'].checkState() == QtCore.Qt.Checked:
                    selected[row['destination']].append(row)
        unresolved=0;identities=set()
        for row in self.rows:
            identity, key = row['material'].currentData(), row['key'].currentData()
            checked = row['file'].checkState() == QtCore.Qt.Checked
            target = row['target']
            if identity:identities.add(identity)
            error = None
            replacing = False
            if not identity:
                error = 'Choose a set' if len(row['entry']['candidates']) < 2 else 'Ambiguous name'
                unresolved+=1
            elif not key or target is None:
                error = 'Choose a map type'
                unresolved+=1
            elif len(selected[row['destination']]) > 1 and checked:
                error = 'Duplicate: keep one'
                unresolved+=1
            else:
                replacing = any(item['connected'] and item['plug'] == target['plug']
                                for item in self.existing[identity])
            if error:
                state, color = error, '#e4b26c'
            elif checked:
                state, color = ('Replace' if replacing else 'Assign'), '#8fc9a3'
                material = textures.resolve_material(identity)
                # Cache the live connection snapshot when this row becomes eligible.
                row['signature'] = textures.connection_signature(material, key)
                self.entries.append(dict(material=identity, key=key, path=row['entry']['path'],
                                         signature=row['signature'],channel=row['channel'].currentData()))
            else:
                state, color = 'Skip', '#a0a0a0'
            row['state'].setText('Duplicate' if state == 'Duplicate: keep one' else state)
            row['state'].setToolTip(state)
            row['state'].setForeground(QtGui.QColor(color))
        count = len(self.entries)
        self.assign.setText('Assign {}'.format(count))
        self.assign.setEnabled(count > 0)
        names=[material['name'] for material in self.materials if material['uuid'] in identities]
        self.target_name = names[0] if len(names)==1 else '{} sets'.format(len(names)) if names else 'Not detected'
        self.target_label.setToolTip('\n'.join(name for name in (self.file_prefix, *names) if name))
        self.summary.setText('{} {} to review'.format(unresolved, 'map' if unresolved == 1 else 'maps') if unresolved else '')
        self.summary.setStyleSheet('color:#bdbdbd;')
        # Unknown filenames no longer expand the dialog automatically.
        self.details_button.setVisible(bool(unresolved) or self.details_button.isChecked() or
                                       any(entry.get('packed') for entry in self.original_plan))
        self.set_details_visible(self.details_button.isChecked())

    def apply(self):
        if not self.entries:
            return
        try:
            if self.controller.stopped or self.controller.scene_busy():
                raise ValueError('Wait for the current operation to finish.')
            # Do not rebuild snapshots here: stale connections must fail preflight.
            textures.attach_batch(self.entries, replace=True)
        except Exception as error:
            self.summary.setText(str(error))
            self.summary.setStyleSheet('color:#eca18b;')
            self.summary.show();self.size_timer.start()
            return
        self.assigned_count = len(self.entries)
        cmds.optionVar(stringValue=('ssMaterialTextureAORouting',self.ao_route.currentData()))
        self.controller.invalidate()
        self.accept()
