"""Switch between saved UV snapshots and explicit Source/Target transfer."""
import math
from PySide2 import QtCore,QtWidgets
import maya.cmds as cmds
from shiboken2 import isValid
from uv_straight_transfer_ui import TransferControls,run,PREFIX,preparation_progress
from uv_straight_transfer_models import TransferSettings
import uv_straight_transfer_direct as direct


class DirectTransferControls(TransferControls):
    def __init__(self,window,layout,resolution,enabled,spacing,mode='duplicate'):
        self.mode=mode
        self.endpoints={};self.endpoint_controls={}
        self.endpoint_rows=[]
        super().__init__(window,layout,resolution,enabled,spacing)
        content=self.panel.layout()
        for index,key in enumerate(('source','target'),1):
            box=QtWidgets.QWidget()
            row=QtWidgets.QHBoxLayout(box);row.setContentsMargins(0,0,0,0);row.setSpacing(4)
            row.addWidget(QtWidgets.QLabel(key.title()))
            name=QtWidgets.QLineEdit();name.setReadOnly(True);name.setPlaceholderText('Select a mesh or group');name.setMinimumWidth(0)
            row.addWidget(name,1)
            uv=QtWidgets.QComboBox();uv.setMinimumWidth(60);uv.setMaximumWidth(92);uv.setToolTip(key.title()+' UV set')
            uv.hide();row.addWidget(uv);uv.currentIndexChanged.connect(self.invalidate)
            pick=QtWidgets.QPushButton('Pick');pick.setFixedWidth(42);pick.setToolTip('Use the selected mesh or group as '+key.title())
            row.addWidget(pick);self.endpoint_controls[key]=(name,uv,pick)
            pick.clicked.connect(lambda checked=False,key=key:self.pick_endpoint(key))
            self.endpoint_rows.append(box)
            content.insertWidget(index,box)
        self.update_mode()

    def set_mode(self,mode):
        if mode==self.mode:return
        self.mode=mode
        self.excluded.clear()
        self.update_mode()

    def update_mode(self):
        same=self.mode=='same'
        for row in self.endpoint_rows:row.setVisible(not same)
        self.controls['materials'].setToolTip(
            'Materials are ready after their source UVs have been saved by packing.' if same else
            'Materials and maps are read from Source. Transfer connects new material copies only to Target.')
        self.button.setToolTip(
            'Rebuild textures from saved UVs and connect them to the same mesh.' if same else
            'Transfer textures from the selected Source UVs to Target UVs. No packing is required. Source and unrelated objects keep their materials.')
        self.status.setText('Pack UVs in this mode to save the original UVs, then transfer textures.' if same else '')
        self.status.setToolTip('')
        self.refresh()

    def pick_endpoint(self,key):
        try:
            endpoint,sets,current=direct.pick();self.endpoints[key]=endpoint
            name,uv,_=self.endpoint_controls[key]
            name.setText(endpoint['name'].rsplit('|',1)[-1]);name.setToolTip(endpoint['name'])
            blocker=QtCore.QSignalBlocker(uv);uv.clear();uv.addItems(sets);uv.setCurrentText(current);del blocker
            uv.setVisible(len(sets)>1)
            self.excluded.clear();self.refresh()
        except Exception as error:self.status.setText(str(error))

    def refresh(self):
        if not self.enabled.isChecked() or len(self.endpoint_controls)!=2:return
        if self.mode=='same':return super().refresh()
        tree=self.controls['materials'];self.dirty=False;tree.clear();self.rows=[]
        try:
            if len(self.endpoints)!=2:
                self.status.setText('Pick Source with the original textures, then Target with the new UVs.')
            else:
                self.rows=direct.materials(self.endpoints['source'],self.endpoints['target'],
                                          self.endpoint_controls['source'][1].currentText(),
                                          self.endpoint_controls['target'][1].currentText())
                self.status.setText('Matching geometry and face order required. Packing is not needed.')
            self.excluded.intersection_update(row['uuid'] for row in self.rows)
            for row in self.rows:
                item=QtWidgets.QTreeWidgetItem([row['name'],''])
                item.setData(0,QtCore.Qt.UserRole,row['uuid']);item.setData(0,QtCore.Qt.UserRole+1,row['ready'])
                item.setData(0,QtCore.Qt.UserRole+2,row['reason'] or '{} source maps. Click to enable or exclude.'.format(row['textures']))
                item.setData(0,QtCore.Qt.UserRole+5,row['state']);self.paint_state(item);tree.addTopLevelItem(item)
            tree.setFixedHeight(min(160,max(58,len(self.rows)*24+tree.header().sizeHint().height()+8)))
            if self.callbacks:self.watch_nodes()
        except Exception as error:self.status.setText(str(error))
        self.update_button()

    def perform(self):
        if self.mode=='same':return super().perform()
        c=self.controls;tree=c['materials']
        from uv_straight_transfer_ui import ACTIVE_ROLE
        selected=[tree.topLevelItem(i).data(0,QtCore.Qt.UserRole) for i in range(tree.topLevelItemCount())
                  if tree.topLevelItem(i).data(0,ACTIVE_ROLE)]
        try:
            if len(self.endpoints)!=2:raise ValueError('Pick Source and Target first.')
            settings=TransferSettings(c['Width'].value(),c['Height'].value(),c['InputSign'].currentData(),c['OutputSign'].currentData(),
                                      bool(c['InputRG'].currentData()),bool(c['OutputRG'].currentData()),math.ceil(self.spacing.value()),4,output_bit_depth=0)
            directory=c['Directory'].text();cmds.optionVar(stringValue=(PREFIX+'Directory',directory))
            source,target=self.endpoints['source'].copy(),self.endpoints['target'].copy()
            source_uv=self.endpoint_controls['source'][1].currentText();target_uv=self.endpoint_controls['target'][1].currentText()
            result=run(lambda:direct.prepare(source,target,source_uv,target_uv,selected,settings,directory,progress=preparation_progress))
            if isValid(self.panel) and result:
                count=sum(row.get('connected',False) for row in result['materials'])
                self.refresh()
                self.status.setText('Transferred {} materials to Target.{}\n{}'.format(count,' Check warnings.' if result.get('warnings') else '',result['directory']))
            return result
        except Exception as error:
            if isValid(self.panel):self.status.setText(str(error))
            return None
