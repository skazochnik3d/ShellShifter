"""UV-only group hiding through Maya's native face-group display filter.

A private facets set supplies visible faces. Original editor modes and mesh
group IDs are restored on show-all, reload and scene replacement. The temporary
set is removed before saving; only the authored group visibility is saved.
"""
from PySide2 import QtCore,QtWidgets
import maya.cmds as cmds
import maya.api.OpenMaya as om
import uv_straight_groups as groups

_service=globals().get('_service')


class Visibility(QtCore.QObject):
    def __init__(self):
        super().__init__(QtWidgets.QApplication.instance())
        self.helper=None;self.meshes={};self.editors={};self.signature=None
        self.input_stamp=None
        from uv_straight_changes import ChangeTracker
        self.changes=ChangeTracker()
        self.saving=self.busy=self.stopped=False;self.callbacks=[]
        self.timer=QtCore.QTimer(self);self.timer.setInterval(350);self.timer.timeout.connect(self.poll)

    def start(self):
        self.stopped=False
        if self.changes is None:
            from uv_straight_changes import ChangeTracker
            self.changes=ChangeTracker()
        if not self.callbacks:
            self.callbacks=[om.MSceneMessage.addCallback(event,callback) for event,callback in (
                (om.MSceneMessage.kBeforeSave,self.before_save),(om.MSceneMessage.kAfterSave,self.after_save),
                (om.MSceneMessage.kBeforeNew,self.before_scene),(om.MSceneMessage.kBeforeOpen,self.before_scene))]
        self.timer.start()
        self.sync()

    def before_save(self,*unused):
        self.saving=True;self.restore()

    def after_save(self,*unused):
        self.saving=False;QtCore.QTimer.singleShot(0,self.poll)

    def before_scene(self,*unused):
        self.restore();self.saving=False

    def poll(self):
        if self.stopped or self.saving or self.busy or QtWidgets.QApplication.mouseButtons()!=QtCore.Qt.NoButton:return
        import uv_straight_native as native
        if native.uv_edit_busy():return
        try:self.sync()
        except (RuntimeError,ValueError):self.restore()

    def restore(self):
        from uv_straight_native import _no_undo
        with _no_undo():
            for record in self.meshes.values():
                name=record['path']
                try:
                    if cmds.objExists(name) and cmds.getAttr(name+'.displayFacesWithGroupId')==record.get('ours'):
                        cmds.setAttr(name+'.displayFacesWithGroupId',record['original'])
                except RuntimeError:pass
            for editor,mode in self.editors.items():
                try:
                    if cmds.textureWindow(editor,exists=True) and cmds.textureWindow(editor,query=True,useFaceGroup=True):
                        cmds.textureWindow(editor,edit=True,useFaceGroup=mode)
                except RuntimeError:pass
            if self.helper and cmds.objExists(self.helper):cmds.delete(self.helper)
        self.helper=None;self.meshes={};self.editors={};self.signature=None
        self.input_stamp=None
        if self.changes is not None:self.changes.watch([]);self.changes.clean()

    def stop(self):
        self.stopped=True
        self.timer.stop();self.restore()
        if self.changes is not None:self.changes.stop();self.changes=None
        for callback in self.callbacks:
            try:om.MMessage.removeCallback(callback)
            except RuntimeError:pass
        self.callbacks=[]

    def stamp(self,state,editors):
        # No UV arrays, polygon walks or temporary attribute writes in the idle path.
        meshes=[]
        for record in self.meshes.values():
            name=record['path'];handle=record.get('handle')
            if handle is None or not handle.isValid() or not handle.isAlive() or not cmds.objExists(name):
                meshes.append((name,None));continue
            fn=om.MFnMesh(handle.object())
            meshes.append((name,fn.currentUVSetName(),fn.numPolygons,
                           cmds.getAttr(name+'.displayFacesWithGroupId')))
        return (state,tuple(cmds.ls(selection=True,long=True) or []),
                tuple((editor,bool(cmds.textureWindow(editor,query=True,useFaceGroup=True))) for editor in editors),tuple(meshes))

    def sync(self,editor_names=None):
        if self.stopped or self.busy or self.saving:return
        from uv_straight_native import _find_editors,_no_undo
        from uv_straight_qt_native import selected_mesh_shapes
        from uv_straight_pack import _visible_faces
        state=groups.fingerprint()
        hidden=[g for g in groups.snapshot(state) if not g.get('visible',True)]
        if not hidden:
            if self.helper or self.editors:self.restore()
            return
        editors=_find_editors() if editor_names is None else editor_names
        if not editors:return
        if (self.changes is not None and not self.changes.dirty and
                self.helper and cmds.objExists(self.helper) and self.stamp(state,editors)==self.input_stamp):return
        self.busy=True
        if self.changes is not None:self.changes.previewing=True
        try:
            with _no_undo():
                modes={editor:bool(cmds.textureWindow(editor,query=True,useFaceGroup=True)) for editor in editors}
                for editor,mode in modes.items():self.editors.setdefault(editor,mode)
                original_mode=self.editors[editors[0]]
                if self.helper and not modes[editors[0]]:
                    # The user switched off native isolation while our filter was active.
                    for editor,mode in modes.items():self.editors[editor]=mode
                    for record in self.meshes.values():record['mode']=False
                    original_mode=False
                names=set(selected_mesh_shapes())
                # Retain hidden members when selection moves away, so returning
                # to them never briefly resurrects the hidden UV charts.
                for group in hidden:names.update(path.fullPathName() for path,member in groups.paths(group))
                state=[groups.fingerprint(),tuple(sorted(names)),tuple(sorted(self.editors.items()))]
                desired={}
                for name in sorted(names):
                    if not cmds.objExists(name):continue
                    selection=om.MSelectionList();selection.add(name);path=selection.getDagPath(0)
                    fn=om.MFnMesh(path);key=(fn.uuid().asString(),path.instanceNumber())
                    current=cmds.getAttr(name+'.displayFacesWithGroupId')
                    record=self.meshes.get(key)
                    if record is None:
                        record=dict(path=name,original=current,ours=None,mode=original_mode,
                                    handle=om.MObjectHandle(fn.object()),dag=om.MDagPath(path))
                        self.meshes[key]=record
                    record['path']=name
                    if current!=record.get('ours'):
                        record['original']=current
                        if record.get('ours') is not None:
                            record['mode']=modes[editors[0]]
                            for editor,mode in modes.items():self.editors[editor]=mode
                    # Compose with the user's original shader/isolate view.
                    mode=record['mode']
                    if mode:cmds.setAttr(name+'.displayFacesWithGroupId',record['original'])
                    base=_visible_faces(path,mode,include_groups=False)
                    if mode and record.get('ours') is not None:cmds.setAttr(name+'.displayFacesWithGroupId',record['ours'])
                    wanted=base-groups.hidden_faces(path)
                    desired[name]=wanted
                    state.append((key,fn.currentUVSetName(),tuple(sorted(wanted)),record['original']))
                if state==self.signature and all(modes.values()) and self.helper and cmds.objExists(self.helper):return
                if not self.helper or not cmds.objExists(self.helper):
                    self.helper=cmds.sets(empty=True,facets=True,name='shellShifterUVVisibleFaces#')
                    cmds.setAttr(self.helper+'.hiddenInOutliner',True)
                else:cmds.sets(clear=self.helper)
                from uv_straight_materials import component_ranges
                components=[component for name,faces in desired.items() for component in component_ranges(name,faces)]
                # component_ranges formats UV components; convert only the suffix.
                components=[component.replace('.map[','.f[') for component in components]
                if components:cmds.sets(components,addElement=self.helper)
                set_ids=set(cmds.listConnections(self.helper,type='groupId') or [])
                for record in self.meshes.values():
                    name=record['path']
                    if name not in desired:continue
                    common=sorted(set(cmds.listConnections(name,type='groupId') or [])&set_ids)
                    group_id=cmds.getAttr(common[0]+'.groupId') if common else -1
                    cmds.setAttr(name+'.displayFacesWithGroupId',group_id);record['ours']=group_id
                for editor in editors:
                    cmds.textureWindow(editor,edit=True,useFaceGroup=True)
                    cmds.textureWindow(editor,edit=True,refresh=True)
                self.signature=state
                if self.changes is not None:
                    from types import SimpleNamespace
                    self.changes.watch([SimpleNamespace(name=r['path'],path=r['dag']) for r in self.meshes.values()
                                        if r['handle'].isValid() and r['handle'].isAlive()])
                self.input_stamp=self.stamp(groups.fingerprint(),editors)
        except Exception:
            self.restore();raise
        finally:
            if self.changes is not None:self.changes.previewing=False;self.changes.clean()
            self.busy=False


def service():
    global _service
    if _service is None:_service=Visibility()
    return _service


def stop():
    global _service
    if _service is not None:
        _service.stop();_service.deleteLater();_service=None
