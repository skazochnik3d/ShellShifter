"""One-click UV transfer: first selected mesh is the reference."""
import html
import time
from PySide2 import QtCore
import maya.cmds as cmds
import maya.api.OpenMaya as om
import uv_straight_groups as groups
from uv_straight_stack_ui import StackController,Ticket
import uv_straight_match_transfer as transfer


def prepare_scope(kind,value,use_selected=True):
    import uv_straight as uv
    import uv_straight_stack as stack
    _,selected=uv._selection()
    selected_ids={}
    for m in selected:selected_ids.setdefault((m.fn.uuid().asString(),m.uv_set),set()).update(m.selected)
    if kind!='dag':raise ValueError('Choose an Outliner hierarchy for Transfer UVs.')
    if not cmds.objExists(value):raise ValueError('The search hierarchy no longer exists.')
    names=cmds.listRelatives(value,allDescendents=True,fullPath=True,type='mesh') or []
    if cmds.nodeType(value)=='mesh':names.append(value)
    else:names+=cmds.listRelatives(value,shapes=True,noIntermediate=True,fullPath=True,type='mesh') or []
    meshes=[];seen=set()
    for name in dict.fromkeys(names):
        selection=om.MSelectionList();selection.add(name);path=selection.getDagPath(0)
        fn=om.MFnMesh(path)
        if not path.isVisible() or fn.findPlug('intermediateObject',False).asBool():continue
        key=(fn.uuid().asString(),fn.currentUVSetName())
        if key in seen:continue
        seen.add(key);mesh=uv.Mesh(path)
        from uv_straight_pack import _visible_faces
        faces=_visible_faces(path,False)
        owners={owner for owner,ids in groups.face_shells(mesh).items() if ids<=faces}
        mesh.selected={i for i,o in enumerate(mesh.shell_ids) if o in owners}
        if mesh.selected:meshes.append(mesh)
    prepared=stack.prepare(meshes)
    if use_selected:
        sources={i for i,record in enumerate(prepared['records'])
                 if set(record.uv_ids)&selected_ids.get((prepared['meshes'][record.mesh_index]['mesh'].fn.uuid().asString(),
                                                        prepared['meshes'][record.mesh_index]['mesh'].uv_set),set())}
        if not sources:raise ValueError('Selected source shells are outside the search group.')
        if len(sources)==len(prepared['records']):raise ValueError('Select source shells only, or choose Best in Group.')
        prepared['sources']=sources
    return prepared


class TransferTask(QtCore.QRunnable):
    def __init__(self,ticket):
        super().__init__();self.ticket=ticket

    def run(self):
        ticket=self.ticket;steps=transfer.analyse_steps(ticket.records);last=0.
        try:
            while not ticket.cancelled.is_set():
                progress=next(steps);now=time.perf_counter()
                if now-last>=.05:
                    ticket.signals.progress.emit(ticket,progress);last=now
        except StopIteration as done:ticket.plan=done.value
        except Exception as error:ticket.error=str(error)
        finally:
            steps.close();ticket.records=None
            try:ticket.signals.finished.emit(ticket)
            except RuntimeError:pass


class MatchController(StackController):
    TITLE='Transfer UVs'

    def __init__(self,window,layout):
        super().__init__(window,layout)
        # Maya must track the order before artists start selecting references.
        cmds.selectPref(trackSelectionOrder=True)
        self.button.setObjectName('matchUVs')
        self.status.setObjectName('matchUVsResult')
        self.button.setToolTip('Select the source mesh first, then the target meshes, and click Transfer UVs.\nUV coordinates and seams from the first mesh are copied to the current UV sets of the others.\nIncompatible meshes are skipped with a notification.\nThe source stays unchanged. All changes are one Undo. Click again to cancel the search.')

    def notify(self,text):
        self.status.setText(text)
        if not cmds.about(batch=True):
            try:cmds.inViewMessage(amg=html.escape(text),pos='topCenter',fade=True,fadeStayTime=3500)
            except RuntimeError:pass
        if hasattr(self.window,'adjust_popup_size'):
            QtCore.QTimer.singleShot(0,self.window.adjust_popup_size)

    def prepare(self):
        return transfer.prepare()

    def start_or_cancel(self):
        if self.ticket is not None:
            self.stop();self.notify('UV transfer cancelled. UVs were not changed.');return
        try:
            self.prepared=self.prepare()
            self.ticket=Ticket(self.prepared['records'],self.signals)
            self.button.setText('Stop Matching')
            self.status.setText('Matching {} target meshes…'.format(len(self.prepared['meshes'])-1))
            QtCore.QThreadPool.globalInstance().start(TransferTask(self.ticket))
        except Exception as error:
            self.stop();self.notify('UV transfer is not possible. '+str(error))

    def progress(self,ticket,progress):
        if ticket is self.ticket:self.status.setText('Matching meshes: {} / {}'.format(*progress))

    def finished(self,ticket):
        if ticket is not self.ticket or ticket.cancelled.is_set():return
        prepared=self.prepared;self.ticket=self.prepared=None;self.button.setText(self.TITLE)
        try:
            if ticket.error:raise ValueError(ticket.error)
            if ticket.plan is None:raise ValueError('Matching did not finish.')
            result=transfer.apply(prepared,ticket.plan)
            text='UVs transferred to {} mesh{}.'.format(result['matched'],'' if result['matched']==1 else 'es')
            if result['failed']:
                text+=' Transfer not possible for {} mesh{}.'.format(len(result['failed']),'' if len(result['failed'])==1 else 'es')
                for name,reason in result['failed']:print('Transfer UVs: {}: {}'.format(name,reason))
            self.notify(text)
        except Exception as error:self.notify('UV transfer is not possible. '+str(error))
