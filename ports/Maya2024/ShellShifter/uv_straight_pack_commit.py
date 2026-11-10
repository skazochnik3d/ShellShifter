"""One undoable UV-array commit per packing result, on the Maya thread."""
from pathlib import Path
import sys
import uuid
import maya.cmds as cmds
import maya.api.OpenMaya as om

_pending = {}


class UVCommit:
    def __init__(self, entries, callback=None):
        self.entries=entries
        self.callback=callback
        self.applied=[]
        self.total=sum(entry['shells'] for entry in entries)

    def mesh_fn(self, entry):
        handle=entry['handle']
        if not handle.isValid() or not handle.isAlive():
            raise ValueError('A packed mesh no longer exists.')
        return om.MFnMesh(handle.object())

    def write(self, entry, values):
        fn=self.mesh_fn(entry)
        assigned=entry.get('after_assigned' if values is entry['after'] else 'before_assigned')
        if assigned is not None:fn.clearUVs(entry['uv_set'])
        fn.setUVs(values[0],values[1],entry['uv_set'])
        if assigned is not None:fn.assignUVs(*assigned,entry['uv_set'])
        fn.updateSurface()

    def create_history(self, entry, destination):
        # Retain upstream UV edits: one native history node for the whole mesh.
        # Direct output-data writes would be lost when a UV history node cooks.
        entry['modifiers']=[]
        mod=om.MDGModifier()
        offset=entry.get('offset')
        assignments=entry.get('after_assigned')
        bulk_transfer=assignments is not None or entry.get('bulk_history',False)
        node=mod.createNode('polyTransfer' if bulk_transfer else
                            'polyMoveUV' if offset is not None else 'polyTweakUV')
        mod.renameNode(node,'shellShifterMovedUVs#' if offset is not None else 'shellShifterPackedUVs#')
        entry['modifiers'].append(mod);mod.doIt()
        dep=om.MFnDependencyNode(node)
        # The node owns these values and retains them when recreated on redo.
        if bulk_transfer:
            fn=self.mesh_fn(entry);data=om.MFnMeshData().create()
            snapshot=om.MFnMesh(om.MFnMesh().copy(fn.object(),data))
            if assignments is not None:snapshot.clearUVs(entry['uv_set'])
            snapshot.setUVs(*entry['after'],entry['uv_set'])
            if assignments is not None:snapshot.assignUVs(*assignments,entry['uv_set'])
            dep.findPlug('otherPoly',False).setMObject(data)
        else:dep.findPlug('uvSetName',False).setString(entry['uv_set'])
        if bulk_transfer:pass
        elif offset is not None:
            # Translation needs two values and one component list, not two
            # delta plugs per UV. polyMoveUV also preserves native pin weights.
            component_fn=om.MFnSingleIndexedComponent()
            component=component_fn.create(om.MFn.kMeshMapComponent)
            component_fn.addElements(entry['ids'])
            data_fn=om.MFnComponentListData();data=data_fn.create();data_fn.add(component)
            dep.findPlug('inputComponents',False).setMObject(data)
            dep.findPlug('translateU',False).setFloat(offset[0])
            dep.findPlug('translateV',False).setFloat(offset[1])
        else:
            tweaks=dep.findPlug('uvTweak',False)
            before,after=entry['before'],entry['after']
            for index in entry['ids']:
                delta=(after[0][index]-before[0][index],after[1][index]-before[1][index])
                if delta==(0.,0.):continue
                plug=tweaks.elementByLogicalIndex(int(index))
                plug.child(0).setFloat(delta[0])
                plug.child(1).setFloat(delta[1])
        entry['history_destinations']=[destination]
        entry['history_input']=dep.findPlug('inputPolymesh',False)
        entry['history_output']=dep.findPlug('output',False)
        self.connect_history(entry, True)
        self.mesh_fn(entry).updateSurface()

    def connect_history(self, entry, connected):
        history_input=entry['history_input']
        history_output=entry['history_output']
        modifier=om.MDGModifier()
        if connected:
            destinations=entry['history_destinations']
            incoming=destinations[0].source()
            modifier.connect(incoming,history_input)
            for destination in destinations:
                modifier.disconnect(destination.source(),destination)
                modifier.connect(history_output,destination)
        else:
            # Live color previews can insert a node downstream of our UV edit
            # between redo and undo. Bypass the UV node at its current location.
            destinations=history_output.destinations()
            incoming=history_input.source()
            modifier.disconnect(incoming,history_input)
            for destination in destinations:
                modifier.disconnect(history_output,destination)
                modifier.connect(incoming,destination)
        modifier.doIt()
        entry['history_destinations']=destinations
        entry['history_connected']=connected

    def redoIt(self):
        self.applied=[];done=0
        try:
            for entry in self.entries:
                self.applied.append(entry)
                if 'modifiers' in entry:
                    for mod in entry['modifiers']:mod.doIt()
                    self.connect_history(entry, True)
                else:
                    fn=self.mesh_fn(entry)
                    destination=om.MFnDependencyNode(fn.object()).findPlug('inMesh',False)
                    incoming=destination.connectedTo(True,False)
                    if incoming:self.create_history(entry,destination)
                    else:self.write(entry,entry['after'])
                # Stack guide metadata belongs to this same Maya command:
                # it must undo together with every mesh's UV array.
                attributes=entry.get('attributes')
                if attributes:
                    modifier=entry.get('attribute_modifier')
                    if modifier is None:
                        dep=om.MFnDependencyNode(self.mesh_fn(entry).object())
                        modifier=om.MDGModifier()
                        for name,value in attributes.items():
                            plug=dep.findPlug(name,False)
                            if plug.isLocked:
                                raise ValueError('Unlock {}.{} before stacking.'.format(dep.name(),name))
                            modifier.newPlugValueString(plug,value)
                        entry['attribute_modifier']=modifier
                    modifier.doIt()
                    entry['attributes_applied']=True
                done+=entry['shells']
                if self.callback:self.callback(done,self.total)
        except Exception:
            self.undoIt()
            raise
        finally:
            # Undo history must never retain a destroyed progress dialog.
            self.callback=None

    def undoIt(self):
        for entry in reversed(self.applied):
            if entry.pop('attributes_applied',False):
                entry['attribute_modifier'].undoIt()
            if 'modifiers' in entry:
                if entry.get('history_connected'):
                    self.connect_history(entry, False)
                for mod in reversed(entry['modifiers']):mod.undoIt()
            else:self.write(entry,entry['before'])
        self.applied=[]


def commit(entries, callback=None):
    if not cmds.undoInfo(query=True,state=True):
        raise ValueError('Enable Undo in Maya preferences before using ShellShifter.')
    plugin='uv_straight_pack_commit_plugin'
    if not cmds.pluginInfo(plugin,query=True,loaded=True):
        cmds.loadPlugin(str(Path(__file__).with_name(plugin+'.py')),quiet=True)
        cmds.pluginInfo(plugin,edit=True,writeRequires=False)
    key=uuid.uuid4().hex
    _pending[key]=(entries,callback)
    try:cmds.shellShifterApplyPackedUVs(key)
    finally:_pending.pop(key,None)
    native=sys.modules.get('uv_straight_native')
    changes=getattr(getattr(native,'_manager',None),'changes',None)
    if changes is not None:changes.invalidate()
