"""Collapsible UV group editors and a shared transient color display."""
from PySide6 import QtCore,QtGui,QtWidgets
import maya.cmds as cmds
import uv_straight_groups as model

_display=globals().get('_display')
KEEP_TEXEL_OPTION = 'shellShifterKeepTexelGroups'


def keep_texel_enabled():
    return bool(cmds.optionVar(query=KEEP_TEXEL_OPTION)) if cmds.optionVar(exists=KEEP_TEXEL_OPTION) else False


class GroupDisplay(QtCore.QObject):
    changed=QtCore.Signal(str)

    def __init__(self):
        super().__init__(QtWidgets.QApplication.instance())
        self.kind='';self.signature=None
        self.changes=None;self.callbacks=[];self.state=None;self.context=None
        self.face_payload=[];self.uv_payload=[];self.rebuilds=0;self.publishing=False
        self.timer=QtCore.QTimer(self);self.timer.setInterval(600);self.timer.timeout.connect(self.refresh)

    def show(self,kind):
        if kind==self.kind:self.clear();return
        from uv_straight_texel_ui import display
        display().clear()
        from uv_straight_changes import ChangeTracker
        self.remove_callbacks()
        if self.changes is not None:self.changes.stop()
        self.changes=ChangeTracker()
        self.state=self.context=None
        self.kind=kind;self.signature=None;self.refresh();self.timer.start();self.changed.emit(kind)

    def clear(self):
        import uv_straight_native as native
        self.kind='';self.signature=None;self.timer.stop()
        if self.changes is not None:self.changes.stop();self.changes=None
        self.remove_callbacks()
        self.state=self.context=None;self.face_payload=[];self.uv_payload=[]
        native.clear_group_colors();self.changed.emit('')

    def remove_callbacks(self):
        import maya.api.OpenMaya as om
        for callback in self.callbacks:
            try:om.MMessage.removeCallback(callback)
            except RuntimeError:pass
        self.callbacks=[]

    def watch_visibility(self,meshes):
        import maya.api.OpenMaya as om
        self.remove_callbacks();seen=set()
        def changed(message,plug,other,unused=None):
            if self.publishing or self.changes is None:return
            if plug.partialName(useLongNames=True).startswith(('visibility','lodVisibility','override',
                    'displayFacesWithGroupId','instObjGroups','invisibleFaces','intermediateObject')):
                self.changes.invalidate()
        def renamed(*unused):
            if self.changes is not None:self.changes.invalidate()
        for mesh in meshes:
            path=om.MDagPath(mesh.path)
            while path.length():
                node=path.node();key=om.MObjectHandle(node).hashCode()
                if key not in seen:
                    seen.add(key)
                    self.callbacks.append(om.MNodeMessage.addAttributeChangedCallback(node,changed))
                    self.callbacks.append(om.MNodeMessage.addNameChangedCallback(node,renamed))
                path.pop()

    def publish(self):
        import uv_straight_native as native
        self.publishing=True
        if self.changes is not None:self.changes.previewing=True
        try:native.set_group_colors(self.face_payload,self.uv_payload)
        finally:
            if self.changes is not None:self.changes.previewing=False
            self.publishing=False

    def refresh(self):
        if not self.kind:return
        if QtWidgets.QApplication.mouseButtons()!=QtCore.Qt.NoButton:return
        import uv_straight_native as native
        if cmds.about(batch=True):return
        if native.uv_edit_busy():return
        manager=native._manager
        if manager is not None and manager.viewport_override_owner not in (None,'uv_groups'):
            self.clear();return
        try:
            from uv_straight_pack import _uv_face_group_mode
            context=_uv_face_group_mode();state=model.fingerprint()
            if self.changes is not None and self.changes.history_settling():return
            dirty=(self.changes is None or self.changes.dirty or
                   any(not handle.isValid() or not handle.isAlive() for handle,callbacks in self.changes.nodes.values()))
            if not dirty and state==self.state and context==self.context:
                # Camera motion uses the cached UV polygons. No topology/UV reads while idle.
                if manager is not None and manager.viewport_override_owner is None:self.publish()
                return
            saved=model.snapshot(state);meshes=[];seen=set();watched={}
            from types import SimpleNamespace
            from uv_straight_texel_ui import DensityMesh
            for group in saved:
                if group['kind']!=self.kind:continue
                for path,member in model.paths(group):
                    watched[path.fullPathName()]=SimpleNamespace(name=path.fullPathName(),path=path)
                    if not path.isVisible():continue
                    mesh=DensityMesh(path);key=(mesh.fn.uuid().asString(),mesh.uv_set)
                    if mesh.uv_set!=member['uv_set'] or key in seen:continue
                    meshes.append(mesh);seen.add(key)
            resolved=model.resolve(meshes,strict=False)
            from uv_straight_qt_native import selected_mesh_shapes
            from uv_straight_pack import _visible_faces,_uv_face_group_mode
            face_group=_uv_face_group_mode()
            selected=set(selected_mesh_shapes());face_colors=[];uv_colors=[]
            signature=[self.kind,state]
            for mesh in meshes:
                visible=_visible_faces(mesh.path,False,include_groups=False)
                uv_visible=_visible_faces(mesh.path,face_group) if mesh.name in selected else set()
                counts,assigned=mesh.fn.getAssignedUVs(mesh.uv_set);offset=0
                signature.append((mesh.name,mesh.uv_set,tuple(mesh.points.items()),tuple(counts),tuple(assigned),
                                  tuple(sorted(visible)),tuple(sorted(uv_visible))))
                for face,count in enumerate(counts):
                    ids=assigned[offset:offset+count];offset+=count
                    if not ids or face not in visible:continue
                    group=resolved[id(mesh)][self.kind].get(int(mesh.shell_ids[ids[0]]))
                    if group is None:continue
                    rgb=tuple(group['color']);face_colors.append((mesh.name,face,rgb+(1.,)))
                    if face in uv_visible:
                        uv_colors.append((rgb+(.48,),tuple(mesh.points[i] for i in ids)))
            self.rebuilds+=1
            if self.changes is not None:self.changes.watch(list(watched.values()))
            self.watch_visibility(list(watched.values()))
            if signature!=self.signature or (manager is not None and manager.viewport_override_owner is None):
                self.face_payload=face_colors;self.uv_payload=uv_colors;self.publish()
                self.signature=signature
            self.state=state;self.context=context
            if self.changes is not None:self.changes.clean()
        except (ValueError,RuntimeError):
            # Scene replacement may invalidate DAG paths between timer turns.
            self.clear()


def display():
    global _display
    if _display is None:_display=GroupDisplay()
    return _display


def stop_display():
    global _display
    if _display is not None:
        _display.clear();_display.deleteLater();_display=None


class TexelDensityField(QtWidgets.QDoubleSpinBox):
    def __init__(self,panel,group_id):
        super().__init__();self.panel=panel;self.group_id=group_id
        self.lineEdit().installEventFilter(self)
    def textFromValue(self,value):
        text=self.locale().toString(value,'f',self.decimals())
        return text.rstrip('0').rstrip(self.locale().decimalPoint()) if self.decimals() else text
    def eventFilter(self,watched,event):
        if watched is self.lineEdit() and event.type()==QtCore.QEvent.ToolTip:
            text=self.panel.density_tooltip(self.group_id);self.setToolTip(text);watched.setToolTip(text)
        return super().eventFilter(watched,event)
    def event(self,event):
        if event.type()==QtCore.QEvent.ToolTip:
            self.setToolTip(self.panel.density_tooltip(self.group_id))
        return super().event(event)


class GroupRow(QtWidgets.QWidget):
    def __init__(self,panel,group):
        super().__init__(panel.list)
        self.panel=panel;self.group_id=group['id'];self.loading=False
        self.setObjectName(panel.kind+'GroupRow')
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos:panel.context_menu(self.group_id,self.mapToGlobal(pos)))
        row=QtWidgets.QHBoxLayout(self);row.setContentsMargins(3,0,3,0);row.setSpacing(4)
        self.eye=QtWidgets.QToolButton();self.eye.setCheckable(True);self.eye.setFixedSize(21,24)
        self.eye.setAutoRaise(True);self.eye.setIconSize(QtCore.QSize(18,18))
        self.eye.setObjectName('groupVisibility')
        self.eye.setStyleSheet('QToolButton { border: 0; padding: 0; background: transparent; }')
        self.eye.setToolTip("Show or hide this group's shells only in the UV Editor. The 3D model remains visible.")
        self.eye.clicked.connect(self.visibility);row.addWidget(self.eye)
        self.label=QtWidgets.QLabel();self.label.setMinimumWidth(0)
        self.label.setSizePolicy(QtWidgets.QSizePolicy.Ignored,QtWidgets.QSizePolicy.Preferred)
        row.addWidget(self.label,1)
        self.swatch=QtWidgets.QPushButton();self.swatch.setFixedSize(18,18)
        self.swatch.setObjectName('groupColor');self.swatch.setToolTip('Change the group color.')
        self.swatch.clicked.connect(lambda:panel.color(self.group_id));row.addWidget(self.swatch)
        if panel.kind=='texel':
            self.editor=TexelDensityField(panel,self.group_id);self.editor.setDecimals(4);self.editor.setRange(.0001,1e9)
            self.editor.setSingleStep(10.)
            self.editor.setObjectName('texelGroupDensity')
            self.editor.setToolTip('Target density in px/unit for both Density modes. Packing fits the layout with a shared scale. Hover to see current density.')
            self.editor.valueChanged.connect(lambda value:self.setting(density=value))
            self.editor.setKeyboardTracking(False)
        else:
            self.editor=QtWidgets.QWidget();modes=QtWidgets.QHBoxLayout(self.editor);modes.setContentsMargins(0,0,0,0);modes.setSpacing(3)
            self.mode_buttons={};self.mode_group=QtWidgets.QButtonGroup(self);self.mode_group.setExclusive(True)
            for mode,title,hint in (
                    ('absolute','A',"Absolute: pack the group's selected visible shells as one unit. Relative positions, rotations and scale remain fixed. The entire group may scale and rotate together."),
                    ('relative','R','Relative: keep shells close together, similar to Cohesion 85%. Shells may move and rotate independently for tighter packing.')):
                button=QtWidgets.QToolButton();button.setText(title);button.setCheckable(True);button.setToolTip(hint)
                button.setObjectName('shellGroup'+title);button.setSizePolicy(QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Expanding)
                button.setStyleSheet('QToolButton {background:#505050;border:0;border-radius:7px;color:#dedede;} QToolButton:checked {background:#ff963b;color:#172027;font-weight:bold;}')
                # Maya 2025 can dispatch clicked() without a checked argument.
                button.clicked.connect(lambda *unused,mode=mode:self.setting(mode=mode))
                self.mode_group.addButton(button);modes.addWidget(button);self.mode_buttons[mode]=button
        self.editor.setFixedSize(76,24);row.addWidget(self.editor)
        self.sync(group)

    def setting(self,**changes):
        if not self.loading:self.panel.update_group(self.group_id,**changes)

    def visibility(self,visible):
        self.panel.update_group(self.group_id,visible=visible)
        from uv_straight_group_visibility import service
        self.panel.run(service().start)

    def sync(self,group):
        self.loading=True
        try:
            from uv_straight_icons import icon
            self.eye.setChecked(group.get('visible',True))
            self.eye.setIcon(icon('eye' if group.get('visible',True) else 'eye_closed'))
            self.label.setText(group['name']);self.label.setToolTip(group['name']+'\nRight-click → Rename to rename the group.')
            self.label.setStyleSheet('color: '+('#e3e3e3' if group.get('visible',True) else '#929292')+';')
            rgb=','.join(str(round(c*255)) for c in group['color'])
            self.swatch.setStyleSheet('QPushButton { background: rgb('+rgb+'); border: 1px solid #b0b0b0; border-radius: 9px; padding: 0; }')
            if self.panel.kind=='texel':self.editor.setValue(group['density'])
            else:
                for mode,button in self.mode_buttons.items():button.setChecked(group.get('mode','relative')==mode)
        finally:self.loading=False

    def mousePressEvent(self,event):
        self.panel.select_row(self.group_id);super().mousePressEvent(event)

    def mouseDoubleClickEvent(self,event):
        if event.button()==QtCore.Qt.LeftButton:
            self.panel.run(lambda:model.select(self.group_id));event.accept();return
        super().mouseDoubleClickEvent(event)


class GroupPanel(QtWidgets.QWidget):
    def __init__(self,kind,window):
        super().__init__(window)
        self.kind=kind;self.window=window;self.state=None;self.row_controls={}
        self.setObjectName(kind+'GroupsPanel')
        layout=QtWidgets.QVBoxLayout(self);layout.setContentsMargins(0,6,0,4);layout.setSpacing(4)
        self.list=QtWidgets.QListWidget();self.list.setObjectName(kind+'GroupsList')
        self.list.setMinimumWidth(0);self.list.setFixedHeight(100)
        self.list.setStyleSheet('QListWidget { background: #292929; color: #dedede; border: 1px solid #505050; border-radius: 5px; outline: 0; }'
                                'QListWidget::item { padding: 0; }'
                                'QListWidget::item:selected { background: #465b70; }')
        self.list.setToolTip('Eye: shell visibility in the UV Editor. Color and value on the right: group settings. Right-click → Rename.')
        self.list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.list_context_menu)
        layout.addWidget(self.list)
        row=QtWidgets.QHBoxLayout();row.setSpacing(4);layout.addLayout(row)
        for title,hint,callback in (
                ('New','Create a group from selected whole shells. A name is assigned automatically.',self.create),
                ('Add','Add selected shells to the current group. They will be removed from another group of the same type.',lambda:self.edit_members(False)),
                ('Remove','Remove selected shells only from the current group.',lambda:self.edit_members(True)),
                ('Delete','Delete the group. UVs and geometry are preserved.',self.delete)):
            button=QtWidgets.QPushButton(title);button.setToolTip(hint);button.setMinimumWidth(0)
            button.setObjectName(kind+'Group'+title);button.clicked.connect(callback);row.addWidget(button)
        row=QtWidgets.QHBoxLayout();layout.addLayout(row)
        select=QtWidgets.QPushButton('Select Group');select.setToolTip("Select the group's shells in the current UV sets.")
        select.clicked.connect(lambda:self.run(lambda:model.select(self.current_id())));row.addWidget(select)
        self.colors=QtWidgets.QPushButton('Show Colors');self.colors.setCheckable(True)
        self.colors.setToolTip('Toggle shell colors by group. This is independent of shell visibility (the eye in each row).')
        self.colors.clicked.connect(lambda:self.run(lambda:display().show(kind)));row.addWidget(self.colors)
        display().changed.connect(self.color_state);self.colors.setChecked(display().kind==kind)
        if kind == 'texel':
            from uv_straight_preview import VisibleCheckBox
            self.keep_texel = VisibleCheckBox('Keep Texel Groups')
            self.keep_texel.setObjectName('keepTexelGroups')
            self.keep_texel.setToolTip('Pack at the exact Texel Group densities. If the shells do not fit, UVs stay unchanged.')
            self.keep_texel.setChecked(keep_texel_enabled())
            self.keep_texel.toggled.connect(
                lambda checked: cmds.optionVar(intValue=(KEEP_TEXEL_OPTION, int(checked))))
            layout.addWidget(self.keep_texel)
        self.status=QtWidgets.QLabel();self.status.setWordWrap(True);self.status.hide();layout.addWidget(self.status)
        self.timer=QtCore.QTimer(self);self.timer.setInterval(600);self.timer.timeout.connect(self.refresh)
        self.refresh(force=True)

    @QtCore.Slot(str)
    def color_state(self,active):self.colors.setChecked(active==self.kind)

    def showEvent(self,event):
        super().showEvent(event);self.refresh(force=True);self.timer.start()

    def hideEvent(self,event):self.timer.stop();super().hideEvent(event)

    def current_id(self):
        item=self.list.currentItem()
        if item is None:raise ValueError('Select a group in the list.')
        return item.data(QtCore.Qt.UserRole)

    def select_row(self,group_id):
        for i in range(self.list.count()):
            if self.list.item(i).data(QtCore.Qt.UserRole)==group_id:self.list.setCurrentRow(i);break

    def run(self,callback):
        try:
            result=callback();self.status.hide();self.refresh(force=True)
            import uv_straight_group_visibility as visibility
            if visibility._service is not None:visibility._service.poll()
            if display().kind:display().signature=None;display().refresh()
            return result
        except (ValueError,RuntimeError) as error:
            self.status.setText(str(error));self.status.show()

    def create(self):
        density=self.window.texel_density_value.value() if hasattr(self.window,'texel_density_value') else model.DEFAULT_DENSITY
        group_id=self.run(lambda:model.create(self.kind,density=density))
        if group_id:self.select_row(group_id)

    def density_tooltip(self,group_id):
        group=model.get(group_id)
        resolution=self.window.packing_controls['resolution'].value() if hasattr(self.window,'packing_controls') else 4096
        text='Target: {:g} px/unit. Map Size: {}.\n'.format(group['density'],resolution)
        try:
            from uv_straight_texel_ui import DensityMesh
            from uv_straight_texel import _measure
            values=[]
            for path,member in model.paths(group):
                mesh=DensityMesh(path)
                if mesh.uv_set!=member['uv_set']:continue
                faces=set(member['faces'])
                values.extend(shell.density for shell in _measure([mesh],resolution) if set(shell.face_ids)<=faces)
            if values:
                average=sum(values)/len(values)
                text+='Current density: {:.2f} px/unit'.format(average)
                if max(values)-min(values)>.01:text+=' (range {:.2f}–{:.2f})'.format(min(values),max(values))
                text+='; {} shells.\n'.format(len(values))
            else:text+='Current density is unavailable in the active UV set.\n'
        except (ValueError,RuntimeError) as error:text+='Current density is unavailable: '+str(error)+'\n'
        return text+'Group targets apply with Density on or off. Packing adjusts the overall scale to the available space.'

    def edit_members(self,remove):self.run(lambda:model.assign(self.current_id(),remove=remove))
    def delete(self):self.run(lambda:model.delete(self.current_id()))

    def update_group(self,group_id,**changes):
        self.select_row(group_id)
        self.run(lambda:model.update(group_id,**changes))

    def rename(self,group_id):
        def choose():
            group=model.get(group_id)
            name,ok=QtWidgets.QInputDialog.getText(self,'Rename Group','Name',text=group['name'])
            if ok:model.update(group_id,name=name)
        self.run(choose)

    def make_context_menu(self,group_id):
        menu=QtWidgets.QMenu(self)
        action=menu.addAction('Rename');action.setToolTip('Rename the group.')
        action.triggered.connect(lambda:self.rename(group_id))
        return menu

    def context_menu(self,group_id,position):
        self.select_row(group_id);menu=self.make_context_menu(group_id)
        menu.exec(position);menu.deleteLater()

    def list_context_menu(self,position):
        item=self.list.itemAt(position)
        if item:self.context_menu(item.data(QtCore.Qt.UserRole),self.list.viewport().mapToGlobal(position))

    def color(self,group_id):
        def choose():
            group=model.get(group_id)
            color=QtWidgets.QColorDialog.getColor(QtGui.QColor.fromRgbF(*group['color']),self,'Group Color')
            if color.isValid():model.update(group_id,color=[color.redF(),color.greenF(),color.blueF()])
        self.select_row(group_id);self.run(choose)

    def refresh(self,force=False):
        if self.kind == 'texel':
            self.keep_texel.setChecked(keep_texel_enabled())
        try:
            state=model.fingerprint()
            if not force and state==self.state:return
            groups=[group for group in model.read() if group['kind']==self.kind]
            current=self.list.currentItem();selected=current.data(QtCore.Qt.UserRole) if current else None
            ids=[self.list.item(i).data(QtCore.Qt.UserRole) for i in range(self.list.count())]
            # Keep inline editors alive while values, colors and eye states change.
            if ids==[group['id'] for group in groups]:
                for group in groups:self.row_controls[group['id']].sync(group)
            else:
                with QtCore.QSignalBlocker(self.list):
                    self.list.clear();self.row_controls={}
                    for group in groups:
                        item=QtWidgets.QListWidgetItem();item.setData(QtCore.Qt.UserRole,group['id'])
                        item.setSizeHint(QtCore.QSize(0,28));self.list.addItem(item)
                        widget=GroupRow(self,group);self.list.setItemWidget(item,widget)
                        self.row_controls[group['id']]=widget
                        if group['id']==selected:self.list.setCurrentItem(item)
                    if self.list.currentRow()<0 and self.list.count():self.list.setCurrentRow(0)
            self.state=state
        except (ValueError,RuntimeError) as error:
            self.status.setText(str(error));self.status.show()


def build(window,layout):
    row=QtWidgets.QHBoxLayout();row.setSpacing(4);layout.addLayout(row)
    window.uv_group_panels={};window.uv_group_buttons={}
    for kind,title in (('texel','Texel Groups'),('pack','Shell Groups')):
        panel=GroupPanel(kind,window);panel.hide();layout.addWidget(panel)
        button=QtWidgets.QPushButton(title);button.setCheckable(True);button.setMinimumWidth(0)
        button.setObjectName(kind+'GroupsToggle')
        button.setStyleSheet('QPushButton:checked { background: #4a6374; }')
        button.setToolTip('Expand '+('texel density groups.' if kind=='texel' else 'packing groups.'))
        def toggle(checked,kind=kind,panel=panel):
            if checked:
                for other,control in window.uv_group_buttons.items():
                    if other!=kind:control.setChecked(False)
            elif display().kind==kind:
                display().clear()
            panel.setVisible(checked)
            if hasattr(window,'adjust_popup_size'):QtCore.QTimer.singleShot(0,window.adjust_popup_size)
        button.toggled.connect(toggle)
        row.addWidget(button);window.uv_group_panels[kind]=panel;window.uv_group_buttons[kind]=button
    from uv_straight_group_visibility import service
    service().start()
