"""Collapsed material-based transfer controls embedded below Packing."""
from pathlib import Path
import re
import math
import threading
import time
from PySide6 import QtCore,QtGui,QtWidgets
from shiboken6 import isValid
import maya.cmds as cmds
from uv_straight_transfer_errors import CancelledError
from uv_straight_transfer_models import TransferSettings

PREFIX='uvStraightTransfer'
_active=None
ACTIVE_ROLE=QtCore.Qt.UserRole+3
COLOR_ROLE=QtCore.Qt.UserRole+4
INDICATOR_COLORS={'yellow':'#d9b75e','red':'#df7373','green':'#66c58c','gray':'#888888'}


def indicator_icon(color):
    pixmap=QtGui.QPixmap(16,16);pixmap.fill(QtCore.Qt.transparent)
    painter=QtGui.QPainter(pixmap);painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setPen(QtCore.Qt.NoPen);painter.setBrush(QtGui.QColor(INDICATOR_COLORS[color]))
    painter.drawEllipse(QtCore.QRectF(3,3,10,10));painter.end()
    icon=QtGui.QIcon()
    for mode in (QtGui.QIcon.Normal,QtGui.QIcon.Selected,QtGui.QIcon.Active,QtGui.QIcon.Disabled):
        icon.addPixmap(pixmap,mode,QtGui.QIcon.Off);icon.addPixmap(pixmap,mode,QtGui.QIcon.On)
    return icon


class TransferMaterialTree(QtWidgets.QTreeWidget):
    def keyPressEvent(self,event):
        if event.key() in (QtCore.Qt.Key_Space,QtCore.Qt.Key_Return,QtCore.Qt.Key_Enter):
            if self.currentItem() is not None:self.itemClicked.emit(self.currentItem(),0)
            event.accept();return
        super().keyPressEvent(event)


class WrappingLabel(QtWidgets.QLabel):
    """Status text must never widen a palette with horizontal scrolling off."""
    def __init__(self,text='',parent=None):
        super().__init__(parent)
        self._plain_text=''
        self.setTextFormat(QtCore.Qt.PlainText)
        self.setWordWrap(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Preferred)
        self.setText(text)

    def setText(self,text):
        self._plain_text=str(text).replace('\u200b','')
        # Backslashes and long asset names are not Qt word boundaries. Add
        # invisible break opportunities, retaining the original text for callers.
        wrapped=re.sub(r'([\\/_:.\-]|[^\s\\/_:.\-]{24})',lambda match:match[0]+'\u200b',self._plain_text)
        super().setText(wrapped)
        self.setToolTip(self._plain_text)

    def text(self):return self._plain_text


def saved(key,default):
    return cmds.optionVar(query=PREFIX+key) if cmds.optionVar(exists=PREFIX+key) else default


def preparation_progress(phase,done=None,total=None):
    if _active is not None and _active.worker is None:
        _active.read_progress(phase,done,total)


class TransferWorker(QtCore.QThread):
    progress=QtCore.Signal(dict)
    outcome=QtCore.Signal(object,object)
    def __init__(self,prepared,cancel,parent):
        super().__init__(parent);self.prepared=prepared;self.cancel=cancel
    def run(self):
        from uv_straight_transfer import plan
        try:self.outcome.emit(plan(self.prepared,self.progress.emit,self.cancel),None)
        except Exception as error:self.outcome.emit(None,error)


class TransferProgress(QtWidgets.QDialog):
    def __init__(self,prepare):
        super().__init__(None);self.prepare=prepare;self.cancel=threading.Event();self.worker=None
        self.result=self.error=None;self.started=time.monotonic()
        self.setWindowTitle('Texture Transfer');self.setWindowModality(QtCore.Qt.ApplicationModal);self.setMinimumWidth(360)
        layout=QtWidgets.QVBoxLayout(self);self.phase=WrappingLabel('Preparing materials…')
        self.bar=QtWidgets.QProgressBar();self.bar.setRange(0,0)
        self.elapsed=QtWidgets.QLabel();self.stop=QtWidgets.QPushButton('Cancel');self.stop.clicked.connect(self.request_stop)
        for widget in (self.phase,self.bar,self.elapsed,self.stop):layout.addWidget(widget)
        self.timer=QtCore.QTimer(self);self.timer.setInterval(100)
        self.timer.timeout.connect(lambda:self.elapsed.setText('Elapsed {:.1f} s'.format(time.monotonic()-self.started)))
        from uv_straight_language import apply
        apply(self)
        self.timer.start()
    def start(self):
        try:
            self.repaint()
            QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ExcludeUserInputEvents)
            if self.cancel.is_set():raise ValueError('Transfer cancelled.')
            from uv_straight_native import uv_edit
            with uv_edit():self.prepared=self.prepare()
            if self.cancel.is_set():raise ValueError('Transfer cancelled.')
            self.worker=TransferWorker(self.prepared,self.cancel,self)
            self.worker.progress.connect(self.update_progress);self.worker.outcome.connect(self.completed);self.worker.start()
        except Exception as error:self.failed(error)
    def read_progress(self,phase,done=None,total=None):
        now=time.monotonic()
        if self.phase.text()!=phase or now-getattr(self,'last_read_paint',0.)>=.05:
            self.last_read_paint=now
            self.phase.setText(phase)
            self.bar.setRange(0,total or 0);self.bar.setValue(done or 0)
            QtWidgets.QApplication.processEvents(QtCore.QEventLoop.AllEvents,5)
        if self.cancel.is_set():raise CancelledError('Transfer cancelled. Materials were not changed.')

    @QtCore.Slot(dict)
    def update_progress(self,state):
        if self.cancel.is_set():return
        self.phase.setText(state.get('material','')+'\n'+state.get('phase',''))
        total=state.get('total',0);self.bar.setRange(0,max(0,total));self.bar.setValue(state.get('done',0))
    @QtCore.Slot(object,object)
    def completed(self,result,error):
        try:
            if error is not None:raise error
            if self.cancel.is_set():raise ValueError('Transfer cancelled. Materials were not changed.')
            if self.prepared.get('direct'):
                from uv_straight_transfer_direct import apply
            else:
                from uv_straight_transfer import apply
            self.phase.setText('Connecting new textures…');self.stop.setEnabled(False)
            self.result=apply(self.prepared,result)
        except Exception as failure:self.failed(failure);return
        warnings=self.result.get('warnings',[])
        if warnings or self.result.get('report_warning'):
            self.timer.stop();self.setWindowTitle('Transfer Warnings')
            connected=sum(row.get('connected',False) for row in self.result['materials'])
            self.phase.setText('Completed with warnings. Materials connected: {}.\nSuccessful textures have been kept.'.format(connected))
            detail=QtWidgets.QPlainTextEdit();detail.setReadOnly(True)
            text='\n\n'.join(item['material']+(' / '+item['texture'] if item.get('texture') else '')+':\n'+item['reason'] for item in warnings)
            if self.result.get('report_warning'):text+='\n\nReport: '+self.result['report_warning']
            detail.setPlainText(text);detail.setMinimumHeight(150)
            self.layout().insertWidget(1,detail)
            self.bar.setRange(0,1);self.bar.setValue(1)
            self.stop.setEnabled(True);self.stop.setText('Close')
            self.stop.clicked.disconnect();self.stop.clicked.connect(self.finish)
            return
        self.finish()
    def failed(self,error):
        self.error=error;self.timer.stop()
        if self.cancel.is_set():self.finish();return
        self.setWindowTitle('Texture transfer error')
        self.phase.setText('Transfer failed.\n'+str(error))
        self.bar.setRange(0,1);self.bar.setValue(0)
        self.stop.setEnabled(True);self.stop.setText('Close')
        self.stop.clicked.disconnect();self.stop.clicked.connect(self.finish)
    def finish(self):self.timer.stop();self.accept()
    def request_stop(self):self.cancel.set();self.stop.setEnabled(False);self.phase.setText('Cancelling transfer…')
    def reject(self):
        if self.worker is not None and self.worker.isRunning():self.request_stop()
        elif self.result is None and self.error is None:self.request_stop()
        else:super().reject()
    def closeEvent(self,event):
        if self.result is None and self.error is None:self.request_stop();event.ignore()
        else:super().closeEvent(event)


def is_running():return _active is not None


def run(prepare):
    global _active
    from uv_straight_pack_progress import is_running as packing_running
    if _active is not None or packing_running():raise ValueError('Wait for the current operation to finish.')
    dialog=TransferProgress(prepare);_active=dialog
    try:
        QtCore.QTimer.singleShot(0,dialog.start);dialog.exec()
        if dialog.error is not None:raise dialog.error
        return dialog.result
    finally:
        if dialog.worker is not None and dialog.worker.isRunning():dialog.cancel.set();dialog.worker.wait()
        _active=None;dialog.deleteLater()


class TransferControls:
    def __init__(self,window,layout,resolution,enabled,spacing):
        from uv_straight_preview import VisibleCheckBox
        from uv_straight_icons import ActionButton
        self.window=window;self.enabled=enabled;self.spacing=spacing;self.controls={};self.rows=[]
        self.excluded=set();self.callbacks=[];self.node_callbacks=[];self.dirty=False
        self.icons={color:indicator_icon(color) for color in INDICATOR_COLORS}
        self.panel=QtWidgets.QWidget();self.panel.setObjectName('ssTextureTransferControls')
        self.panel.setSizePolicy(QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Maximum)
        content=QtWidgets.QVBoxLayout(self.panel);content.setContentsMargins(0,5,0,0);content.setSpacing(6)
        content.setAlignment(QtCore.Qt.AlignTop)
        separator=QtWidgets.QFrame();separator.setFixedHeight(1)
        separator.setStyleSheet('background: #4b4b4b; border: 0;');content.addWidget(separator)
        tree=TransferMaterialTree();tree.setColumnCount(2);tree.setHeaderLabels(['Material','Status'])
        tree.headerItem().setToolTip(0,'Material for texture transfer')
        tree.headerItem().setToolTip(1,'Material readiness for transfer')
        tree.setStyleSheet('QHeaderView::section { background: #414141; color: #dedede; border: 0; padding: 3px; }')
        tree.header().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        tree.setRootIsDecorated(False);tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        tree.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        tree.setToolTip('Yellow: UVs unchanged; red: error; green: transfer enabled. Click a ready material to disable or re-enable its transfer. Gray: transfer cancelled or completed.')
        tree.setMinimumHeight(58);tree.setMaximumHeight(160);content.addWidget(tree);self.controls['materials']=tree
        tree.itemClicked.connect(self.toggle_material)
        def spin(key,default,low,high):
            from uv_straight_pack_ui import PowerOfTwoSpinBox
            widget=PowerOfTwoSpinBox();widget.setRange(low,high);widget.setKeyboardTracking(False);widget.setValue(int(saved(key,default)))
            widget.valueChanged.connect(lambda value,key=key:cmds.optionVar(intValue=(PREFIX+key,value)))
            self.controls[key]=widget;return widget
        width=spin('Width',resolution.value(),2,8192);height=spin('Height',resolution.value(),2,8192)
        width.setToolTip('Output texture width in pixels. Arrows double or halve it.')
        height.setToolTip('Output texture height in pixels. Arrows double or halve it.')
        linked=VisibleCheckBox('Link');linked.setChecked(bool(saved('Linked',True)))
        linked.setToolTip('Link output texture width and height.')
        linked.toggled.connect(lambda value:cmds.optionVar(intValue=(PREFIX+'Linked',int(value))))
        self.controls['Linked']=linked;row=QtWidgets.QHBoxLayout();row.addWidget(width,1);row.addWidget(QtWidgets.QLabel('×'));row.addWidget(height,1);row.addWidget(linked)
        content.addWidget(QtWidgets.QLabel('Resolution'));content.addLayout(row)
        width.valueChanged.connect(lambda value:height.setValue(value) if linked.isChecked() else None)
        height.valueChanged.connect(lambda value:width.setValue(value) if linked.isChecked() else None)
        linked.toggled.connect(lambda checked:height.setValue(width.value()) if checked else None)
        resolution.valueChanged.connect(width.setValue)
        advanced=QtWidgets.QToolButton();advanced.setText('Normal Map Settings');advanced.setCheckable(True)
        advanced.setToolTip('Configure green-channel direction and storage format for input and output normal maps.')
        advanced.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon);advanced.setArrowType(QtCore.Qt.RightArrow)
        content.addWidget(advanced);options=QtWidgets.QWidget();options_layout=QtWidgets.QGridLayout(options);options_layout.setContentsMargins(0,0,0,0)
        for line,(title,sign_key,packing_key) in enumerate((('Input','InputSign','InputRG'),('Output','OutputSign','OutputRG'))):
            options_layout.addWidget(QtWidgets.QLabel(title),line,0)
            system=QtWidgets.QComboBox();system.addItem('+Y (OpenGL)',1);system.addItem('−Y (DirectX)',-1)
            system.setToolTip('Normal-map green-channel direction: +Y (OpenGL) or −Y (DirectX).')
            system.setCurrentIndex(max(0,system.findData(int(saved(sign_key,1)))))
            system.currentIndexChanged.connect(lambda index,key=sign_key,box=system:cmds.optionVar(intValue=(PREFIX+key,box.currentData())))
            codec=QtWidgets.QComboBox();codec.addItem('RGB',False);codec.addItem('BC5 / RG',True);codec.setCurrentIndex(int(bool(saved(packing_key,False))))
            codec.setToolTip('Normal-map format: three RGB channels or two RG channels with blue reconstruction.')
            codec.currentIndexChanged.connect(lambda index,key=packing_key,box=codec:cmds.optionVar(intValue=(PREFIX+key,int(box.currentData()))))
            options_layout.addWidget(system,line,1);options_layout.addWidget(codec,line,2)
            self.controls[sign_key]=system;self.controls[packing_key]=codec
        note=WrappingLabel('Changing the normal map format creates a compatible material copy.')
        note.setToolTip("Changing a material's normal-map format creates a compatible copy.")
        options_layout.addWidget(note,2,0,1,3)
        options.hide();advanced.toggled.connect(options.setVisible)
        advanced.toggled.connect(lambda checked:advanced.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow))
        content.addWidget(options)
        content.addWidget(QtWidgets.QLabel('Save Path'));row=QtWidgets.QHBoxLayout()
        default=str(Path(cmds.workspace(query=True,rootDirectory=True))/'sourceimages'/'ShellShifter_Transfer')
        folder=QtWidgets.QLineEdit(str(saved('Directory',default)));folder.editingFinished.connect(lambda:cmds.optionVar(stringValue=(PREFIX+'Directory',folder.text())))
        folder.setToolTip('Output folder for transferred textures.')
        browse=QtWidgets.QPushButton('…');browse.setMaximumWidth(28)
        browse.setToolTip('Choose an output folder for textures.')
        def choose():
            selected=QtWidgets.QFileDialog.getExistingDirectory(self.panel,'Save Path',folder.text())
            if selected:folder.setText(selected);cmds.optionVar(stringValue=(PREFIX+'Directory',selected))
        browse.clicked.connect(choose);row.addWidget(folder,1);row.addWidget(browse);content.addLayout(row);self.controls['Directory']=folder
        open_folder=QtWidgets.QPushButton('Open Output Folder')
        open_folder.setToolTip('Open the selected folder. Output PNGs are saved directly into it with unique names.')
        open_folder.clicked.connect(lambda:QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(Path(folder.text()).expanduser().resolve()))))
        content.addWidget(open_folder)
        hint=WrappingLabel('GPU (OpenCL) · PNG: source bit depth · Gap padding · 4× sampling.')
        hint.setToolTip('Transfer runs on the GPU. If OpenCL is unavailable, CPU is used and the report explains why. Bit depth is preserved per source map; mixed UDIMs use the highest depth. Padding rounds up to whole pixels. Input: 8/16-bit PNG/TIFF, TGA and JPEG. TIFF without alpha or float channels.')
        content.addWidget(hint)
        self.button=ActionButton('Transfer && Connect Textures','texture_transfer');self.button.clicked.connect(self.perform)
        self.button.setToolTip('Transfer available maps of enabled materials from source UVs to repacked UVs and connect the new files. Individual map errors produce warnings; other maps are saved. Yellow Ready with Warnings indicates partial availability. Gray materials are excluded. Other materials are preserved.')
        content.addWidget(self.button);self.status=WrappingLabel();content.addWidget(self.status)
        layout.addWidget(self.panel);self.panel.setVisible(enabled.isChecked())
        self.poll=QtCore.QTimer(self.panel);self.poll.setInterval(300);self.poll.timeout.connect(self.poll_refresh)
        self.panel.destroyed.connect(lambda *args:self.unwatch())
        enabled.toggled.connect(self.set_enabled)
        if enabled.isChecked():self.set_enabled(True)
    def set_enabled(self,value):
        self.panel.setVisible(value)
        if value:self.watch();self.refresh()
        else:self.unwatch()
    def paint_state(self,item):
        state=item.data(0,QtCore.Qt.UserRole+5);ready=bool(item.data(0,QtCore.Qt.UserRole+1))
        active=ready and item.data(0,QtCore.Qt.UserRole) not in self.excluded
        color=(('yellow' if state=='partial' else 'green') if active else 'gray') if ready else 'yellow' if state=='no_source' else 'gray' if state=='completed' else 'red'
        states={'ready':'Ready','no_source':'UV Unchanged','incomplete':'Incomplete Source',
                'completed':'Transferred','partial':'Ready with Warnings','no_textures':'No File Textures','changed':'Material Changed','material_error':'Material Error'}
        item.setData(0,ACTIVE_ROLE,active);item.setData(0,COLOR_ROLE,color)
        item.setIcon(0,self.icons[color]);item.setText(1,'Transfer Disabled' if ready and not active else states.get(state,'Material Error'))
        detail=item.data(0,QtCore.Qt.UserRole+2) or ''
        hint=('Click to cancel transfer.\n' if active else 'Transfer cancelled. Click to enable.\n') if ready else ''
        item.setToolTip(0,hint+detail);item.setToolTip(1,hint+detail)
    def toggle_material(self,item,column=0):
        if not item.data(0,QtCore.Qt.UserRole+1):return
        key=item.data(0,QtCore.Qt.UserRole)
        if key in self.excluded:self.excluded.remove(key)
        else:self.excluded.add(key)
        self.paint_state(item);self.update_button()
    def refresh(self):
        if not self.enabled.isChecked():return
        from uv_straight_transfer import materials
        self.dirty=False
        tree=self.controls['materials']
        previous=tree.currentItem().data(0,QtCore.Qt.UserRole) if tree.currentItem() else None
        try:self.rows=materials()
        except Exception as error:
            self.rows=[];tree.clear()
            self.status.setText(str(error));self.update_button();return
        tree.clear()
        tree.setFixedHeight(min(160,max(68,len(self.rows)*24+tree.header().sizeHint().height()+8)))
        current=None;first_ready=None
        self.excluded.intersection_update(row['uuid'] for row in self.rows)
        for row in self.rows:
            item=QtWidgets.QTreeWidgetItem([row['name'],''])
            item.setData(0,QtCore.Qt.UserRole,row['uuid']);item.setData(0,QtCore.Qt.UserRole+1,row['ready'])
            item.setData(0,QtCore.Qt.UserRole+5,row['state'])
            detail='{} · {} maps · {} meshes\n{}'.format(row['name'],row['textures'],len(row['bindings']),
                                                       row['reason'] or 'Ready to transfer. Source UVs are saved.')
            item.setData(0,QtCore.Qt.UserRole+2,detail)
            item.setToolTip(0,detail);item.setToolTip(1,detail)
            item.setFlags(item.flags()&~QtCore.Qt.ItemIsUserCheckable)
            self.paint_state(item)
            if row['ready'] and first_ready is None:first_ready=item
            if row['uuid']==previous:current=item
            tree.addTopLevelItem(item)
        tree.setCurrentItem(current or first_ready or tree.topLevelItem(0))
        self.update_button()
        if self.callbacks:self.watch_nodes()
    def update_button(self):
        tree=self.controls['materials']
        self.button.setEnabled(any(tree.topLevelItem(i).data(0,ACTIVE_ROLE)
                                   for i in range(tree.topLevelItemCount())))
    def perform(self):
        from uv_straight_transfer import prepare
        c=self.controls;tree=c['materials']
        selected=[tree.topLevelItem(i).data(0,QtCore.Qt.UserRole) for i in range(tree.topLevelItemCount())
                  if tree.topLevelItem(i).data(0,ACTIVE_ROLE)]
        settings=TransferSettings(c['Width'].value(),c['Height'].value(),c['InputSign'].currentData(),c['OutputSign'].currentData(),
                                  bool(c['InputRG'].currentData()),bool(c['OutputRG'].currentData()),math.ceil(self.spacing.value()),4,
                                  output_bit_depth=0)
        directory=c['Directory'].text();cmds.optionVar(stringValue=(PREFIX+'Directory',directory))
        try:
            result=run(lambda:prepare(selected,settings,directory,progress=preparation_progress))
            if isValid(self.panel):
                count=sum(len(output['files']) for row in result['materials'] for output in row['outputs'].values())
                connected=sum(row.get('connected',False) for row in result['materials'])
                label='Transfer completed with warnings' if result.get('warnings') else 'Transfer complete'
                self.status.setText(label+': {} materials, {} PNG files.\n{}\n{}'.format(connected,count,
                                    result.get('compute',{}).get('device','CPU'),result['directory']))
                details='\n'.join(item['material']+': '+item['reason'] for item in result.get('warnings',()))
                self.status.setToolTip('Connected materials: {}, PNG files: {}.\n{}\n{}'.format(connected,count,result['directory'],details))
                self.refresh()
            return result
        except Exception as error:
            if isValid(self.panel):self.status.setText(str(error))
            return None
    def stop(self):
        self.unwatch()
        if _active is not None:_active.request_stop()

    def invalidate(self,*args):self.dirty=True

    def attribute_changed(self,message,*args):
        import maya.api.OpenMaya as om
        if message&(om.MNodeMessage.kAttributeSet|om.MNodeMessage.kConnectionMade|om.MNodeMessage.kConnectionBroken|om.MNodeMessage.kAttributeArrayAdded|om.MNodeMessage.kAttributeArrayRemoved):self.invalidate()

    def watch(self):
        if self.callbacks:return
        import maya.api.OpenMaya as om
        self.callbacks=[om.MSceneMessage.addCallback(event,self.invalidate) for event in
                        (om.MSceneMessage.kAfterNew,om.MSceneMessage.kAfterOpen,om.MSceneMessage.kAfterImport)]
        self.callbacks.append(om.MDGMessage.addConnectionCallback(self.invalidate))
        for kind in ('mesh','file','shadingEngine'):
            self.callbacks.append(om.MDGMessage.addNodeAddedCallback(self.invalidate,kind))
            self.callbacks.append(om.MDGMessage.addNodeRemovedCallback(self.invalidate,kind))
        events=set(om.MEventMessage.getEventNames())
        for name in ('Undo','Redo','NameChanged','PolyUVSetChanged','PolyUVSetDeleted'):
            if name in events:self.callbacks.append(om.MEventMessage.addEventCallback(name,self.invalidate))
        self.poll.start()

    def watch_nodes(self):
        import maya.api.OpenMaya as om
        self.remove_callbacks(self.node_callbacks)
        nodes={row['name'] for row in self.rows}
        nodes.update(binding['path'] for row in self.rows for binding in row['bindings'] if 'path' in binding)
        for row in self.rows:
            if cmds.objExists(row['name']):nodes.update(cmds.listHistory(row['name'],pruneDagObjects=True) or [])
        for node in sorted(nodes):
            try:
                selection=om.MSelectionList();selection.add(node);obj=selection.getDependNode(0)
                self.node_callbacks.append(om.MNodeMessage.addAttributeChangedCallback(obj,self.attribute_changed))
            except (RuntimeError,ValueError):pass

    def poll_refresh(self):
        from uv_straight_pack_progress import is_running as packing_running
        if self.dirty and self.enabled.isChecked() and self.panel.isVisible() and not is_running() and not packing_running():self.refresh()

    @staticmethod
    def remove_callbacks(callbacks):
        import maya.api.OpenMaya as om
        for callback in callbacks:
            try:om.MMessage.removeCallback(callback)
            except RuntimeError:pass
        callbacks.clear()

    def unwatch(self):
        if isValid(self.poll):self.poll.stop()
        self.remove_callbacks(self.node_callbacks);self.remove_callbacks(self.callbacks)
