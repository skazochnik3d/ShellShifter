"""Compact texture cards and asynchronous image thumbnails for Materials."""
import ctypes
from contextlib import contextmanager
from html import escape
from pathlib import Path
from collections import OrderedDict
from PySide2 import QtCore,QtGui,QtWidgets
from shiboken2 import isValid
import maya.cmds as cmds
import maya.api.OpenMaya as om
from uv_straight_icons import icon, menu_image
import uv_straight_material_textures as textures
import uv_straight_texture_reload as texture_reload
from uv_straight_texture_import_ui import ImportComboBox, TextureImportDialog, mime_paths
CARD_BORDER = '#5b5b5b'
BUTTON_STYLE = (
    'QPushButton { background:#515151; color:#f0f0f0; border:0; border-radius:6px;'
    'padding:0 7px; min-height:26px; max-height:26px; }'
    'QPushButton:hover, QPushButton:checked { background:#5c5c5c; }'
    'QPushButton:focus { border:1px solid #8098b4; }'
    'QPushButton:disabled { background:#444444; color:#979797; }')
PRIMARY_STYLE = BUTTON_STYLE + (
    'QPushButton { background:#ccdfda; color:#233633; }'
    'QPushButton:hover { background:#d9e9e5; }'
    'QPushButton:disabled { background:#82918c; color:#273735; }')
ICON_STYLE = (
    'QToolButton { background:transparent; border:0; padding:0; }'
    'QToolButton:hover { background:#5c5c5c; border-radius:5px; }'
    'QToolButton:focus { border:1px solid #8098b4; border-radius:5px; }')


def combo_style(compact=False):
    arrow = menu_image('material_chevron_down')
    height = 20 if compact else 25
    return (
        'QComboBox { background:#434343; color:#f0f0f0; border:1px solid #5b5b5b;'
        f'border-radius:5px; min-height:{height}px; max-height:{height}px; padding:0 16px 0 5px; font-size:'
        + ('11px' if compact else '12px') + '; }'
        'QComboBox::drop-down { border:0; width:16px; }'
        f'QComboBox::down-arrow {{ image:url("{arrow}"); width:13px; height:13px; }}'
    )


def texture_dialog_directory(texture_path=''):
    scene = cmds.file(query=True, sceneName=True)
    if scene:
        scene_path = Path(scene)
        if scene_path.is_absolute():
            return str(scene_path.parent)
    if texture_path:
        return str(Path(texture_path).parent)
    if cmds.optionVar(exists='ssMaterialTextureDirectory'):
        return str(cmds.optionVar(query='ssMaterialTextureDirectory'))
    return ''


def maya_thumbnail(path):
    """Fallback for formats missing from Maya's Qt image plugins."""
    source = om.MImage()
    try:
        source.readFromFile(path, om.MImage.kByte)
        width, height = source.getSize()
        depth = source.depth()
        formats = {3: QtGui.QImage.Format_RGB888, 4: QtGui.QImage.Format_RGBA8888}
        if not width or not height or depth not in formats or not source.pixels():
            return QtGui.QImage()
        data = ctypes.string_at(source.pixels(), width * height * depth)
        image = QtGui.QImage(data, width, height, width * depth, formats[depth])
        return image.scaled(64, 64, QtCore.Qt.KeepAspectRatio,
                            QtCore.Qt.SmoothTransformation).mirrored(False, True).copy()
    except (RuntimeError, ValueError, OSError):
        return QtGui.QImage()


class ThumbnailSignals(QtCore.QObject):
    ready=QtCore.Signal(object,object,object)


class ThumbnailTask(QtCore.QRunnable):
    def __init__(self,key,revision):
        super().__init__();self.key=key;self.revision=revision;self.signals=ThumbnailSignals()
    def run(self):
        image=QtGui.QImage()
        try:
            reader=QtGui.QImageReader(self.key[0]);reader.setAutoTransform(True)
            size=reader.size()
            if size.isValid():reader.setScaledSize(size.scaled(96,96,QtCore.Qt.KeepAspectRatio))
            image=reader.read()
            if not image.isNull():image=image.scaled(64,64,QtCore.Qt.KeepAspectRatio,QtCore.Qt.SmoothTransformation)
        except Exception:image=QtGui.QImage()
        finally:
            try:self.signals.ready.emit(self.key,self.revision,image)
            except RuntimeError:pass


class ConversionSignals(QtCore.QObject):
    finished = QtCore.Signal(int, object)


class ConversionTask(QtCore.QRunnable):
    def __init__(self, paths, directory, size, fmt, depth):
        super().__init__()
        self.paths = paths
        self.settings = directory, size, fmt, depth
        self.signals = ConversionSignals()

    def run(self):
        try:
            from uv_straight_texture_convert import convert_batch
            count, errors = convert_batch(self.paths, *self.settings)
        except ImportError as error:
            count, errors = 0, [str(error)]
        self.signals.finished.emit(count, errors)


class TypeNameEdit(QtWidgets.QLineEdit):
    cancelled=QtCore.Signal()
    def keyPressEvent(self,event):
        if event.key()==QtCore.Qt.Key_Escape:self.clear();self.cancelled.emit();event.accept()
        else:super().keyPressEvent(event)


class TexturesToggle(QtWidgets.QToolButton):
    def paintEvent(self, event):
        option = QtWidgets.QStyleOptionToolButton()
        self.initStyleOption(option)
        option.text = ''
        option.icon = QtGui.QIcon()
        painter = QtWidgets.QStylePainter(self)
        painter.drawComplexControl(QtWidgets.QStyle.CC_ToolButton, option)
        self.icon().paint(painter, QtCore.QRect(4, (self.height()-13)//2, 13, 13))
        title, separator, count = self.text().partition(' · ')
        painter.setPen(QtGui.QColor('#f0f0f0' if self.isEnabled() else '#979797'))
        text_rect = self.rect().adjusted(22, 0, 0, 0)
        painter.drawText(text_rect, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, title)
        if separator:
            text_rect.setLeft(text_rect.left() + painter.fontMetrics().horizontalAdvance(title) + 4)
            font = painter.font()
            font.setPixelSize(11)
            painter.setFont(font)
            painter.setPen(QtGui.QColor('#bdbdbd'))
            painter.drawText(text_rect, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, '· ' + count)


class TextureCard(QtWidgets.QWidget):
    def __init__(self,panel,row):
        super().__init__(panel);self.panel=panel;self.row=row;self.hovered=False;self.dragged=False
        self.preview_key = None
        self.setFixedHeight(27 if row['connected'] else 26);self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Fixed)
        self.setAcceptDrops(True);self.setMouseTracking(True);self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setCursor(QtCore.Qt.PointingHandCursor);self.setAccessibleName(row['label'])
        detail=row['path'] or ('Composite network: '+', '.join(row['files']) if row['connected'] else 'No texture connected')
        if row['auxiliary']:detail+='\nAuxiliary material map; does not affect shading.'
        if not row['auxiliary']:detail+='\nMaya shader input.'
        if row['key']=='base_color':detail+='\nBase Color / Albedo'
        if row['connected']:detail+='\nFile channel: '+dict(textures.CHANNELS)[row['channel']]
        self.base_tooltip=row['label']+'\n'+detail+'\nLeft-click to choose a file, or drag and drop a file.'
        self.setToolTip(self.base_tooltip)
        self.eye_button=None
        self.remove_button=None
        if row['connected']:
            self.eye_button = QtWidgets.QToolButton(self)
            self.eye_button.setObjectName('textureVisibility')
            self.eye_button.setIcon(icon('material_eye_off' if row['muted'] else 'material_eye'))
            self.eye_button.setIconSize(QtCore.QSize(14,14))
            action = 'Show ' if row['muted'] else 'Hide '
            self.eye_button.setAccessibleName(action+row['label'])
            self.eye_button.setToolTip(action+row['label']+' on the material')
            self.eye_button.setCursor(QtCore.Qt.PointingHandCursor)
            self.eye_button.setStyleSheet(ICON_STYLE)
            self.eye_button.clicked.connect(
                lambda: self.panel.mutate(lambda material: textures.set_slot_visible(material, row['key'], row['muted']))
            )
            self.remove_button=QtWidgets.QToolButton(self)
            self.remove_button.setObjectName('removeTextureSlot')
            self.remove_button.setIcon(icon('material_close'))
            self.remove_button.setIconSize(QtCore.QSize(13,13))
            self.remove_button.setAccessibleName('Remove '+row['label'])
            self.remove_button.setToolTip('Disconnect '+row['label']+' from this slot. One Undo restores the map.')
            self.remove_button.setStyleSheet(ICON_STYLE)
            self.remove_button.clicked.connect(lambda:self.panel.mutate(lambda material:textures.detach(material,row['key'])))
        self.channel_combo=None
        if row['file']:
            self.channel_combo=ImportComboBox()
            self.channel_combo.setParent(self)
            self.channel_combo.setObjectName('textureSourceChannel')
            for value,label in textures.CHANNELS:
                self.channel_combo.addItem(label,value)
            self.channel_combo.setCurrentIndex(self.channel_combo.findData(row['channel']))
            self.channel_combo.setToolTip('RGB uses the full normal map. A single channel is interpreted as bump height.'
                                         if row['key']=='normal' else 'Choose the source channel of this file.')
            self.channel_combo.setAccessibleName(row['label']+' source channel')
            self.channel_combo.setStyleSheet(combo_style(compact=True))
            self.channel_combo.activated.connect(self.change_channel)

    def change_channel(self,index):
        channel=self.channel_combo.itemData(index);key=self.row['key']
        self.panel.mutate(lambda material:textures.set_source_channel(material,key,channel))

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if self.eye_button is not None:
            self.eye_button.setGeometry(3,(self.height()-24)//2,22,24)
        if self.channel_combo is not None:
            self.channel_combo.setGeometry(self.width()-94,(self.height()-22)//2,65,22)
        if self.remove_button is not None:
            self.remove_button.setGeometry(self.width()-25,(self.height()-24)//2,22,24)

    def set_preview(self,image):
        if self.preview_key == image.cacheKey():
            return
        self.preview_key = image.cacheKey()
        if image.isNull():
            self.setToolTip(self.base_tooltip+'\nCould not read the preview. Check the file path and image format.')
            return
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.WriteOnly)
        if image.save(buffer, 'PNG'):
            data = bytes(buffer.data().toBase64()).decode('ascii')
            description = escape(self.base_tooltip).replace('\n', '<br>')
            self.setToolTip(f'<div><img src="data:image/png;base64,{data}"><br>{description}</div>')

    def paintEvent(self,event):
        painter=QtGui.QPainter(self);painter.setRenderHint(QtGui.QPainter.Antialiasing)
        rect=QtCore.QRectF(self.rect())
        connected = self.row['connected']
        muted = self.row['muted']
        color=QtGui.QColor('#434343' if connected else '#292929')
        if self.hovered:color=QtGui.QColor('#5c5c5c')
        border=QtGui.QColor(CARD_BORDER)
        if self.dragged or self.row.get('previewing'):
            painter.setPen(QtGui.QPen(border.lighter(125),1.5))
        else:
            painter.setPen(QtCore.Qt.NoPen if connected else QtGui.QPen(border,1.))
        painter.setBrush(color);painter.drawRoundedRect(rect.adjusted(.5,.5,-.5,-.5) if not connected else rect,5,5)
        if connected:
            swatch = self.row.get('color') or ('#a5a69a' if self.row['key']=='base_color' else '#8172c6')
            painter.setOpacity(.5 if muted else 1.)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(swatch))
            painter.drawRoundedRect(QtCore.QRectF(29, (self.height()-14)//2+1, 14, 14), 3, 3)
            painter.setOpacity(1.)
            first_control = self.channel_combo if self.channel_combo is not None else self.remove_button
            text_rect = QtCore.QRectF(47,0,max(0,first_control.x()-51),self.height())
        else:
            painter.setPen(QtGui.QPen(border,1.))
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRoundedRect(QtCore.QRectF(self.width()-23,6,14,14),4,4)
            text_rect = rect.adjusted(8,0,-27,0)
        painter.setPen(QtGui.QColor('#bdbdbd' if muted else '#f0f0f0'))
        text=QtGui.QFontMetrics(self.font()).elidedText(self.row['label'],QtCore.Qt.ElideRight,max(0,int(text_rect.width())))
        painter.drawText(text_rect,QtCore.Qt.AlignVCenter|QtCore.Qt.AlignLeft,text)

    def enterEvent(self,event):self.hovered=True;self.update();super().enterEvent(event)
    def leaveEvent(self,event):self.hovered=False;self.update();super().leaveEvent(event)
    def mouseReleaseEvent(self,event):
        if event.button()==QtCore.Qt.LeftButton and self.rect().contains(event.localPos().toPoint()):self.panel.choose_file(self.row['key']);event.accept()
        else:super().mouseReleaseEvent(event)
    def keyPressEvent(self,event):
        if event.key() in (QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter,QtCore.Qt.Key_Space):self.panel.choose_file(self.row['key']);event.accept()
        else:super().keyPressEvent(event)
    @staticmethod
    def dropped_file(mime):
        urls=mime.urls() if mime.hasUrls() else []
        if len(urls)!=1 or not urls[0].isLocalFile():return None
        path=urls[0].toLocalFile()
        return path if Path(path).suffix.lower() in textures.EXTENSIONS and Path(path).is_file() else None
    def dragEnterEvent(self,event):
        if mime_paths(event.mimeData()):self.dragged=True;self.update();event.acceptProposedAction()
        else:event.ignore()
    def dragLeaveEvent(self,event):self.dragged=False;self.update();event.accept()
    def dropEvent(self,event):
        self.dragged=False;self.update();path=self.dropped_file(event.mimeData())
        if path:self.panel.queue_attach(self.row['key'],path);event.acceptProposedAction()
        elif mime_paths(event.mimeData()):self.panel.queue_import(mime_paths(event.mimeData()));event.acceptProposedAction()
        else:event.ignore()
    def contextMenuEvent(self, event):
        event.accept()
        self.panel.open_slot_menu(event.globalPos(), self.row['key'])


class TexturePanel(QtWidgets.QWidget):
    def __init__(self,controller,layout,tray_layout=None):
        super().__init__();self.controller=controller;self.material_uuid=None;self.cards={};self.signature=None
        if not cmds.optionVar(exists='ssMaterialTextureLayout') or cmds.optionVar(query='ssMaterialTextureLayout')!='connected':
            cmds.optionVar(stringValue=('ssMaterialTextureLayout','connected'))
            cmds.optionVar(intValue=('ssMaterialTexturesCollapsed',0))
        self.collapsed=bool(cmds.optionVar(query='ssMaterialTexturesCollapsed')) if cmds.optionVar(exists='ssMaterialTexturesCollapsed') else False
        self.show_all=bool(cmds.optionVar(query='ssMaterialTextureShowAll')) if cmds.optionVar(exists='ssMaterialTextureShowAll') else False
        self.import_open=False
        self.dirty=True;self.callbacks=[];self.revision=0;self.pending={};self.cache=OrderedDict()
        self.pool=QtCore.QThreadPool(self);self.pool.setMaxThreadCount(2)
        self.setObjectName('materialTexturePanel')
        self.setAcceptDrops(True)
        body=QtWidgets.QVBoxLayout(self);body.setContentsMargins(0,1,0,0);body.setSpacing(5)
        header=QtWidgets.QHBoxLayout();header.setContentsMargins(0,0,0,0);header.setSpacing(5)
        self.toggle=TexturesToggle()
        self.toggle.setSizePolicy(QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Fixed)
        self.toggle.setMinimumWidth(0);self.toggle.setFixedHeight(28);self.toggle.clicked.connect(self.toggle_collapsed)
        self.toggle.setStyleSheet(ICON_STYLE)
        self.import_button=QtWidgets.QPushButton('Assign…');self.import_button.setFixedSize(57,26)
        self.import_button.setToolTip('Assign multiple textures by set name and map type. Drop files or a folder onto Assign or Materials. Only files directly inside a folder are read.')
        self.import_button.setStyleSheet(PRIMARY_STYLE)
        self.import_button.clicked.connect(self.choose_import)
        self.import_button.setAcceptDrops(True)
        self.import_button.installEventFilter(self)
        self.clear_button=QtWidgets.QPushButton('Clear');self.clear_button.setFixedSize(41,26)
        self.clear_button.setStyleSheet(BUTTON_STYLE)
        self.clear_button.setToolTip('Disconnect all textures from the selected material. One Undo restores them.')
        self.clear_button.clicked.connect(lambda:self.mutate(textures.clear_textures))
        header.addWidget(self.toggle,1);header.addWidget(self.import_button);header.addWidget(self.clear_button);body.addLayout(header)
        self.content=QtWidgets.QWidget();content_body=QtWidgets.QVBoxLayout(self.content);content_body.setContentsMargins(0,0,0,0);content_body.setSpacing(3)
        self.grid=QtWidgets.QGridLayout();self.grid.setHorizontalSpacing(5);self.grid.setVerticalSpacing(3);self.grid.setColumnStretch(0,1);self.grid.setColumnStretch(1,1);content_body.addLayout(self.grid)
        self.empty=QtWidgets.QLabel('No connected maps');self.empty.setAlignment(QtCore.Qt.AlignCenter)
        self.empty.setStyleSheet('color:#aaaaaa;padding:6px 0;');self.empty.hide();content_body.addWidget(self.empty)
        self.all_button=QtWidgets.QToolButton();self.all_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.all_button.setSizePolicy(QtWidgets.QSizePolicy.Preferred,QtWidgets.QSizePolicy.Fixed)
        self.all_button.setFixedHeight(26);self.all_button.setMinimumWidth(0)
        self.all_button.setIconSize(QtCore.QSize(13,13))
        self.all_button.setStyleSheet(ICON_STYLE + 'QToolButton {color:#bdbdbd;font-size:11px;padding:0 7px;}')
        self.all_button.clicked.connect(self.toggle_all_maps)
        body.addWidget(self.content)
        self.entry=TypeNameEdit();self.entry.setPlaceholderText('Slot name');self.entry.setMaxLength(40);self.entry.setMinimumWidth(0)
        self.entry.setAccessibleName('Texture slot name')
        self.entry.returnPressed.connect(self.add_type)
        self.entry.cancelled.connect(self.cancel_add_type)
        self.entry.hide()
        content_body.addWidget(self.entry)
        content_body.addWidget(self.all_button, 0, QtCore.Qt.AlignHCenter)
        self.status=QtWidgets.QLabel();self.status.setWordWrap(True);self.status.hide();body.addWidget(self.status)
        if not cmds.about(batch=True):
            texture_reload.start().changed.connect(self.texture_files_changed)
        layout.addWidget(self);self.hide()
        self.add_texture_tools(layout, tray_layout if tray_layout is not None else layout)
        self.update_header()

    def add_texture_tools(self, layout, tray_layout):
        self.tool_mode = ''
        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(5)
        self.convert_button = QtWidgets.QPushButton('Convert')
        self.view_button = QtWidgets.QPushButton('View')
        self.view_button.setIcon(icon('material_eye'))
        self.convert_button.setIcon(icon('material_convert'))
        for mode, button in (('convert',self.convert_button), ('view',self.view_button)):
            button.setCheckable(True)
            button.setFixedHeight(26)
            button.setIconSize(QtCore.QSize(14,14))
            button.setStyleSheet(BUTTON_STYLE)
            button.clicked.connect(lambda checked=False, mode=mode: self.toggle_tools(mode))
            buttons.addWidget(button,1)
        self.convert_button.setToolTip('Choose image files, conversion settings, and an output folder.')
        self.view_button.setToolTip('Show or hide each map type across all texture sets.')
        layout.addLayout(buttons)
        self.view_widget = QtWidgets.QFrame()
        self.view_widget.setStyleSheet('QFrame#textureVisibilityPanel {background:#363636;border:1px solid #5b5b5b;border-radius:12px;}')
        self.view_widget.setObjectName('textureVisibilityPanel')
        self.view_layout = QtWidgets.QVBoxLayout(self.view_widget)
        self.view_layout.setContentsMargins(8,7,8,8)
        self.view_layout.setSpacing(3)
        self.add_tool_heading(self.view_layout,'Map visibility','view','All texture sets')
        self.view_signature = None
        self.view_widget.hide()
        tray_layout.addWidget(self.view_widget)
        self.conversion_widget = QtWidgets.QFrame()
        self.conversion_widget.setObjectName('textureConversionPanel')
        self.conversion_widget.setStyleSheet('QFrame#textureConversionPanel {background:#363636;border:1px solid #5b5b5b;border-radius:12px;}')
        self.conversion_widget.hide()
        tray_layout.addWidget(self.conversion_widget)
        content = QtWidgets.QVBoxLayout(self.conversion_widget)
        content.setContentsMargins(8,7,8,8)
        content.setSpacing(0)
        self.add_tool_heading(content,'Convert textures','convert')
        content.addSpacing(4)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(5)
        content.addLayout(row)
        self.convert_size = QtWidgets.QComboBox()
        self.convert_size.setToolTip('Output size on the longest side. A 2:1 map at 2K becomes 2048 × 1024.')
        for label, size in (('Source', 0), ('512', 512), ('1K', 1024), ('2K', 2048),
                            ('4K', 4096), ('8K', 8192)):
            self.convert_size.addItem(label, size)
        self.convert_size.setCurrentIndex(self.convert_size.findData(2048))
        self.convert_format = QtWidgets.QComboBox()
        self.convert_format.setToolTip('Output file format')
        for fmt in ('TGA', 'TIFF', 'PNG'):
            self.convert_format.addItem(fmt, 'tif' if fmt == 'TIFF' else fmt.lower())
        self.convert_depth = QtWidgets.QComboBox()
        self.convert_depth.setToolTip('Bits per channel. TGA supports 8 bit.')
        for depth in (8, 16):
            self.convert_depth.addItem('{} bit'.format(depth), depth)
        self.convert_format.currentIndexChanged.connect(self.update_conversion_depth)
        self.update_conversion_depth()
        for label, control, stretch in (('Size',self.convert_size,85), ('Format',self.convert_format,115),
                                        ('Depth',self.convert_depth,100)):
            column = QtWidgets.QVBoxLayout()
            column.setSpacing(3)
            caption = QtWidgets.QLabel(label)
            caption.setStyleSheet('color:#bdbdbd;font-size:11px;')
            column.addWidget(caption)
            control.setStyleSheet(combo_style())
            control.setMinimumWidth(0)
            control.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Fixed)
            control.setFixedHeight(27)
            column.addWidget(control)
            row.addLayout(column,stretch)
        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(5)
        content.addSpacing(7)
        self.convert_files_button = QtWidgets.QPushButton('Select files…')
        self.convert_files_button.setIcon(icon('material_folder'))
        self.convert_files_button.setIconSize(QtCore.QSize(14,14))
        self.convert_files_button.setFixedSize(98,26)
        self.convert_files_button.setStyleSheet(BUTTON_STYLE)
        self.convert_save_button = QtWidgets.QPushButton('Save…')
        self.convert_save_button.setFixedHeight(26)
        self.convert_save_button.setStyleSheet(PRIMARY_STYLE)
        self.convert_save_button.setToolTip('Choose the output folder and convert the selected files.')
        self.convert_save_button.setEnabled(False)
        self.convert_files_button.clicked.connect(self.choose_conversion_files)
        self.convert_save_button.clicked.connect(self.save_conversion)
        self.conversion_files = ()
        self.conversion_selection = QtWidgets.QLabel('No files')
        self.conversion_selection.setStyleSheet('color:#bdbdbd;font-size:11px;')
        self.conversion_selection.setAlignment(QtCore.Qt.AlignCenter)
        actions.addWidget(self.convert_files_button)
        actions.addWidget(self.conversion_selection,1)
        actions.addWidget(self.convert_save_button)
        content.addLayout(actions)
        self.conversion_task = None
        self.conversion_status = QtWidgets.QLabel()
        self.conversion_status.setContentsMargins(0,5,0,0)
        self.conversion_status.setWordWrap(True)
        self.conversion_status.hide()
        content.addWidget(self.conversion_status)

    def add_tool_heading(self, layout, title, mode, context=''):
        row = QtWidgets.QHBoxLayout()
        caption = QtWidgets.QLabel(title)
        font = caption.font()
        font.setWeight(QtGui.QFont.DemiBold)
        caption.setFont(font)
        caption.setStyleSheet('color:#f0f0f0;')
        row.addWidget(caption,1)
        if context:
            label = QtWidgets.QLabel(context)
            label.setStyleSheet('color:#bdbdbd;font-size:11px;')
            row.addWidget(label)
        close = QtWidgets.QToolButton()
        close.setIcon(icon('material_close'))
        close.setIconSize(QtCore.QSize(13,13))
        close.setAccessibleName('Close '+title)
        close.setFixedSize(22,24)
        close.setStyleSheet(ICON_STYLE)
        close.clicked.connect(lambda:self.toggle_tools(mode))
        row.addWidget(close)
        layout.addLayout(row)

    def toggle_tools(self, mode):
        self.tool_mode = '' if self.tool_mode == mode else mode
        self.convert_button.setChecked(self.tool_mode == 'convert')
        self.view_button.setChecked(self.tool_mode == 'view')
        self.conversion_widget.setVisible(self.tool_mode == 'convert')
        self.view_widget.setVisible(self.tool_mode == 'view')
        self.invalidate()
        if self.tool_mode == 'view':
            self.refresh()
        self.controller.adjust_size()

    def refresh_view(self, current_rows):
        nodes = set()
        groups = {}
        for record in self.controller.records:
            try:
                material = textures.resolve_material(record.uuid)
                rows = current_rows if record.uuid == self.material_uuid else textures.read(material)
            except (ValueError, RuntimeError):
                continue
            nodes.add(material)
            for row in rows:
                if not row['connected']:
                    continue
                key = row['label'].casefold() if row['custom'] else row['key']
                group = groups.setdefault(key,(row['label'],[]))
                group[1].append((record.uuid,row['key'],row['muted']))
        primary = {'base_color':0,'normal':1,'opacity':2}
        ordered = sorted(groups.items(),key=lambda item:(primary.get(item[0],3),item[1][0].casefold()))
        total = len(nodes)
        signature = (total,tuple((key,label,tuple(slots)) for key,(label,slots) in ordered))
        if signature == self.view_signature:
            return nodes
        self.view_signature = signature
        while self.view_layout.count() > 1:
            widget = self.view_layout.takeAt(1).widget()
            widget.hide()
            widget.deleteLater()
        if not ordered:
            self.view_layout.addWidget(QtWidgets.QLabel('No assigned maps'))
        for key,(label,slots) in ordered:
            line = QtWidgets.QFrame()
            line.setObjectName('mapVisibilityRow')
            line.setStyleSheet('QFrame#mapVisibilityRow {background:#434343;border-radius:5px;}')
            line.setFixedHeight(27)
            row = QtWidgets.QHBoxLayout(line)
            row.setContentsMargins(7,0,3,0)
            row.setSpacing(6)
            row.addWidget(QtWidgets.QLabel(label),1)
            visible = sum(not muted for _,_,muted in slots)
            count = len({identity for identity,_,_ in slots})
            coverage = QtWidgets.QLabel('{}/{} sets'.format(count,total))
            coverage.setStyleSheet('color:#bdbdbd;font-size:11px;')
            coverage.setToolTip('{} of {} assigned maps visible'.format(visible,len(slots)))
            row.addWidget(coverage)
            eye = QtWidgets.QToolButton()
            eye.setObjectName('mapTypeVisibility')
            eye.setFixedSize(22,24)
            eye.setIconSize(QtCore.QSize(14,14))
            eye.setIcon(icon('material_eye' if visible else 'material_eye_off',highlight=0<visible<len(slots)))
            eye.setStyleSheet(ICON_STYLE)
            action = 'Hide ' if visible else 'Show '
            eye.setAccessibleName(action+label+' in all texture sets')
            eye.setToolTip(action+label+' in all texture sets\n'+coverage.toolTip())
            eye.clicked.connect(lambda checked=False, slots=slots, show=not visible:
                                self.mutate_all_visibility(slots,show))
            row.addWidget(eye)
            self.view_layout.addWidget(line)
        self.controller.adjust_size()
        return nodes

    def mutate_all_visibility(self, slots, visible):
        def apply():
            if self.controller.stopped:
                return
            try:
                if self.controller.scene_busy():
                    raise ValueError('Wait for the current operation to finish.')
                textures.set_slots_visible(((identity,key) for identity,key,_ in slots), visible)
                self.status.hide()
            except (ValueError,RuntimeError) as error:
                self.show_error(error)
            self.refresh()
        QtCore.QTimer.singleShot(0,apply)

    def update_conversion_depth(self):
        is_tga = self.convert_format.currentData() == 'tga'
        if is_tga:
            self.convert_depth.setCurrentIndex(0)
        self.convert_depth.model().item(1).setEnabled(not is_tga)

    def choose_conversion_files(self):
        if self.conversion_task is not None:
            return
        directory = str(Path(self.conversion_files[0]).parent) if self.conversion_files else texture_dialog_directory()
        with self.dialog_guard():
            paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
                self.conversion_widget, 'Convert Textures', directory, 'Textures (*.tif *.tiff *.tga *.png)')
        if paths:
            self.conversion_files = tuple(paths)
            self.conversion_selection.setText('{} selected'.format(len(paths)))
            self.conversion_selection.setToolTip('\n'.join(paths))
            self.conversion_status.hide()
            self.convert_save_button.setEnabled(True)
            self.controller.adjust_size()

    def save_conversion(self):
        if self.conversion_task is not None or not self.conversion_files:
            return
        paths = self.conversion_files
        with self.dialog_guard():
            destination = QtWidgets.QFileDialog.getExistingDirectory(
                self.conversion_widget, 'Choose Output Folder', str(Path(paths[0]).parent))
        if not destination:
            return
        self.convert_files_button.setEnabled(False)
        self.convert_save_button.setEnabled(False)
        self.conversion_status.setToolTip('')
        self.show_conversion_status('Converting {} textures…'.format(len(paths)))
        self.conversion_task = ConversionTask(paths, destination, self.convert_size.currentData(),
                                              self.convert_format.currentData(), self.convert_depth.currentData())
        self.conversion_task.signals.finished.connect(self.conversion_finished)
        QtCore.QThreadPool.globalInstance().start(self.conversion_task)

    @QtCore.Slot(int, object)
    def conversion_finished(self, count, errors):
        self.conversion_task = None
        self.convert_files_button.setEnabled(True)
        self.convert_save_button.setEnabled(bool(self.conversion_files))
        message = 'Converted {} texture{}.'.format(count, '' if count == 1 else 's')
        if errors:
            message += ' {} failed. {}'.format(len(errors), errors[0])
        self.conversion_status.setToolTip('\n'.join(errors))
        self.show_conversion_status(message)

    def show_conversion_status(self, message):
        self.conversion_status.setText(message)
        self.conversion_status.show()
        self.controller.adjust_size()

    def invalidate(self,*args):self.dirty=True
    def remove_callbacks(self):
        for callback in self.callbacks:
            try:om.MMessage.removeCallback(callback)
            except RuntimeError:pass
        self.callbacks.clear()
    def attribute_changed(self,message,*args):
        if message&(om.MNodeMessage.kAttributeSet|om.MNodeMessage.kConnectionMade|om.MNodeMessage.kConnectionBroken):self.invalidate()
    def pause(self):
        if self.material_uuid:
            try:textures.end_preview(self.material_uuid)
            except (RuntimeError,ValueError):pass
        self.remove_callbacks();self.dirty=True;self.revision+=1;self.pool.clear();self.pending.clear()
    def stop(self):
        self.pause()
    def toggle_collapsed(self):
        if not self.material_uuid:return
        self.collapsed=not self.collapsed
        cmds.optionVar(intValue=('ssMaterialTexturesCollapsed',int(self.collapsed)))
        if self.collapsed:self.pause()
        self.refresh()
        self.update_header()
        self.controller.adjust_size()
    def toggle_all_maps(self):
        self.set_show_all(not self.show_all)
        self.refresh();self.controller.adjust_size()

    def set_show_all(self, enabled):
        self.show_all = enabled
        cmds.optionVar(intValue=('ssMaterialTextureShowAll', int(enabled)))

    def update_header(self,rows=None):
        if rows is not None:self.connected_count=sum(bool(row['connected']) for row in rows);self.slot_count=len(rows)
        count=' · {}/{}'.format(getattr(self,'connected_count',0),getattr(self,'slot_count',len(textures.SLOTS))) if self.material_uuid else ''
        self.toggle.setText('Textures'+count)
        self.toggle.setIcon(icon('material_chevron_right' if self.collapsed or not self.material_uuid else 'material_chevron'))
        self.toggle.setEnabled(bool(self.material_uuid))
        self.clear_button.setEnabled(bool(self.material_uuid) and bool(getattr(self,'connected_count',0)))
        self.view_button.setEnabled(bool(self.controller.records) or self.tool_mode=='view')
        self.toggle.setToolTip('Expand or collapse maps for the selected material. The state is remembered.' if self.material_uuid else 'Select a material to show its maps.')
        self.content.setVisible(bool(self.material_uuid) and not self.collapsed)
        self.all_button.setText('Connected Only' if self.show_all else 'All Maps · {}'.format(getattr(self,'slot_count',len(textures.SLOTS))))
        self.all_button.setIcon(icon('material_chevron_up' if self.show_all else 'material_chevron'))
        self.all_button.setToolTip('Return to connected maps' if self.show_all else 'Show all texture slots')
        self.all_button.setVisible(getattr(self,'connected_count',0)<getattr(self,'slot_count',len(textures.SLOTS)))
    def set_material(self,record):
        identity=record.uuid if record else None
        if identity!=self.material_uuid:
            self.pause();self.material_uuid=identity;self.signature=None;self.connected_count=0;self.entry.clear();self.entry.hide();self.status.hide()
        self.setVisible(bool(self.controller.records));self.update_header()
        if identity or self.tool_mode=='view':self.refresh()
        self.controller.adjust_size()
    def check_dirty(self):
        if self.dirty and (self.material_uuid or self.tool_mode=='view'):self.refresh()
    def refresh(self):
        try:
            material = textures.resolve_material(self.material_uuid) if self.material_uuid else None
            rows = textures.read(material) if material else []
            nodes = {material} if material else set()
            self.update_header(rows)
            if self.tool_mode=='view':
                nodes.update(self.refresh_view(rows))
            signature=repr((rows,self.show_all))
            if material and not self.collapsed and signature!=self.signature:
                self.revision+=1;self.pool.clear();self.pending.clear();self.cards.clear()
                while self.grid.count():
                    item=self.grid.takeAt(0);widget=item.widget()
                    if widget is not None:widget.hide();widget.deleteLater()
                connected_rows = sorted((row for row in rows if row['connected']),
                                        key=lambda row: row['key'] != 'base_color')
                for index, row in enumerate(connected_rows):
                    card = TextureCard(self, row)
                    self.cards[row['key']] = card
                    self.grid.addWidget(card,index,0,1,2)
                empty_rows = [row for row in rows if not row['connected']] if self.show_all else []
                for index, row in enumerate(empty_rows):
                    card = TextureCard(self, row)
                    self.cards[row['key']] = card
                    self.grid.addWidget(card,len(connected_rows)+index//2,index%2)
                self.empty.setText('No connected maps' if rows else 'No texture slots')
                self.empty.setVisible(not connected_rows and (not self.show_all or not rows))
                self.signature=signature
                self.controller.adjust_size()
            if material and not self.collapsed:
                for card in self.cards.values():self.load_preview(card)
                nodes.update(cmds.listHistory(material,pruneDagObjects=True) or [])
                meta=textures.settings_node(material)
                if meta:nodes.add(meta)
            self.remove_callbacks()
            for node in nodes:
                try:
                    selection=om.MSelectionList();selection.add(node)
                    self.callbacks.append(om.MNodeMessage.addAttributeChangedCallback(selection.getDependNode(0),self.attribute_changed))
                except RuntimeError:pass
            self.dirty=False
        except (RuntimeError,ValueError) as error:self.show_error(error);self.dirty=False

    def load_preview(self,card):
        path=textures.preview_path(card.row['path'])
        if not path:return
        try:stat=Path(path).stat();key=(path,stat.st_mtime_ns,stat.st_size)
        except OSError:card.set_preview(QtGui.QImage());return
        if key in self.cache:card.set_preview(self.cache[key]);self.cache.move_to_end(key);return
        token=(key,self.revision)
        if token in self.pending:return
        task=ThumbnailTask(key,self.revision);self.pending[token]=task
        task.signals.ready.connect(self.preview_ready);self.pool.start(task)

    def texture_files_changed(self, paths):
        for key in tuple(self.cache):
            if texture_reload.path_key(key[0]) in paths:
                del self.cache[key]
        if not self.isVisible() or self.collapsed:
            return
        if any(path and texture_reload.path_key(path) in paths
               for path in (textures.preview_path(card.row['path']) for card in self.cards.values())):
            self.revision += 1
            self.pool.clear()
            self.pending.clear()
            for card in self.cards.values():
                self.load_preview(card)

    @QtCore.Slot(object,object,object)
    def preview_ready(self,key,revision,image):
        self.pending.pop((key,revision),None)
        if revision!=self.revision:return
        if image.isNull():
            image = maya_thumbnail(key[0])
        self.cache[key]=image
        while len(self.cache)>80:self.cache.popitem(last=False)
        for card in self.cards.values():
            if textures.preview_path(card.row['path'])==key[0]:
                card.set_preview(image)

    @contextmanager
    def dialog_guard(self):
        window=self.controller.window;pin=hasattr(window,'set_popup_pinned') and not window.popup_pinned
        if pin:window.set_popup_pinned(True)
        try:yield
        finally:
            if pin and isValid(window):window.set_popup_pinned(False)
    def choose_file(self,key):
        identity=self.material_uuid
        card=self.cards.get(key);path=card.row['path'] if card else ''
        directory=texture_dialog_directory(path)
        with self.dialog_guard():selected,_=QtWidgets.QFileDialog.getOpenFileName(self,'Texture',directory,'Images (*.png *.tif *.tiff *.tga *.jpg *.jpeg *.bmp *.exr *.hdr *.dds *.tx *.iff);;All Files (*.*)')
        if selected:
            cmds.optionVar(stringValue=('ssMaterialTextureDirectory',str(Path(selected).parent)))
            self.queue_attach(key,selected,identity)
    def queue_attach(self,key,path,identity=None):
        card = self.cards.get(key)
        channel = card.row['channel'] if card is not None and card.row['file'] else None
        self.mutate(lambda material:textures.attach(material,key,path,channel=channel),identity)
    def mutate(self,operation,identity=None):
        identity=identity or self.material_uuid
        def apply():
            if self.controller.stopped:return
            try:
                if self.controller.scene_busy():raise ValueError('Wait for the current operation to finish.')
                operation(textures.resolve_material(identity));self.status.hide();self.invalidate();self.refresh()
            except Exception as error:self.show_error(error)
        QtCore.QTimer.singleShot(0,apply)
    def choose_color(self,key,current):
        with self.dialog_guard():color=QtWidgets.QColorDialog.getColor(QtGui.QColor(current),self,'Background Color')
        if color.isValid():self.mutate(lambda material:textures.set_color(material,key,color.name()))
    def contextMenuEvent(self, event):
        event.accept()
        self.open_slot_menu(event.globalPos())

    def open_material_editor(self, identity=None):
        identity = identity or self.material_uuid
        if not identity or self.controller.stopped:
            return
        try:
            import json
            import maya.mel as mel
            material = textures.resolve_material(identity)
            with self.dialog_guard():
                if not mel.eval('exists "showEditorExact"'):
                    mel.eval('source "showEditor.mel";')
                mel.eval('showEditorExact ' + json.dumps(material, ensure_ascii=False) + ';')
            self.status.hide()
        except Exception as error:
            self.show_error(error)

    def open_slot_menu(self, position, key=None):
        identity = self.material_uuid
        if not identity:
            return
        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(
            'QMenu {background:#383838;color:#eeeeee;border:1px solid #666666;}'
            'QMenu::item {padding:5px 16px;} QMenu::item:selected {background:#486080;}'
            'QMenu::item:disabled {color:#888888;}'
        )
        editor = menu.addAction('Material Attributes')
        menu.addSeparator()
        add = menu.addAction('Add slot')
        delete = menu.addAction('Delete slot')
        delete.setEnabled(key in self.cards)
        with self.dialog_guard():
            action = menu.exec_(position)
        if isValid(menu):
            menu.deleteLater()
        if not isValid(self) or identity != self.material_uuid:
            return
        if action is editor:
            self.open_material_editor(identity)
        elif action is add:
            if self.collapsed:
                self.toggle_collapsed()
            self.entry.show()
            self.entry.setFocus()
            self.controller.adjust_size()
        elif action is delete:
            self.mutate(lambda material: textures.delete_slot(material, key), identity)

    def cancel_add_type(self):
        self.entry.hide()
        self.controller.adjust_size()

    def add_type(self):
        label = self.entry.text()
        def add(material):
            textures.add_slot(material, label)
            self.entry.clear()
            self.entry.hide()
            if not self.show_all:
                self.set_show_all(True)
        self.mutate(add)
    def show_error(self,error):
        if isValid(self.status):self.status.setText(str(error));self.status.show();self.controller.adjust_size()
    def eventFilter(self, watched, event):
        if watched is self.import_button and event.type() in (
                QtCore.QEvent.DragEnter, QtCore.QEvent.DragMove, QtCore.QEvent.Drop):
            paths = mime_paths(event.mimeData())
            if paths:
                if event.type() == QtCore.QEvent.Drop:
                    self.queue_import(paths)
                event.acceptProposedAction()
            else:
                event.ignore()
            return True
        return super().eventFilter(watched, event)

    def choose_import(self):
        directory=texture_dialog_directory()
        with self.dialog_guard():
            paths,_=QtWidgets.QFileDialog.getOpenFileNames(self,'Assign Textures by Name',directory,
                'Images (*.png *.tif *.tiff *.tga *.jpg *.jpeg *.bmp *.exr *.hdr *.dds *.tx *.iff);;All Files (*.*)')
        if paths:self.queue_import(paths)
    def queue_import(self,paths):
        paths=tuple(paths)
        def apply():
            if self.controller.stopped or self.import_open:return
            if self.controller.scene_busy():self.show_error('Wait for the current operation to finish.');return
            self.import_open=True
            dialog=None
            try:
                with self.dialog_guard():
                    dialog=TextureImportDialog(self.controller,paths,self.controller.window)
                    if dialog.exec_()==QtWidgets.QDialog.Accepted:
                        cmds.optionVar(stringValue=('ssMaterialTextureDirectory',str(Path(paths[0]).parent)))
                        self.status.setText('Assigned {} maps. Undo restores all assignments.'.format(dialog.assigned_count));self.status.show()
                        self.invalidate()
                        if self.material_uuid:self.refresh()
                        self.controller.adjust_size()
            except Exception as error:self.show_error(error)
            finally:
                self.import_open=False
                if dialog is not None and isValid(dialog):dialog.deleteLater()
        QtCore.QTimer.singleShot(0,apply)
    def dragEnterEvent(self,event):
        if mime_paths(event.mimeData()):event.acceptProposedAction()
        else:event.ignore()
    def dragMoveEvent(self,event):self.dragEnterEvent(event)
    def dropEvent(self,event):
        paths=mime_paths(event.mimeData())
        if paths:self.queue_import(paths);event.acceptProposedAction()
        else:event.ignore()
