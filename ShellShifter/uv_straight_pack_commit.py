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
        fn.setUVs(values[0],values[1],entry['uv_set'])
        fn.updateSurface()

    def create_history(self, entry, incoming, destination):
        # Retain upstream UV edits: one native tweak node for the whole mesh.
        # Direct output-data writes would be lost when a UV history node cooks.
        entry['modifiers']=[]
        mod=om.MDGModifier()
        node=mod.createNode('polyTweakUV')
        mod.renameNode(node,'shellShifterPackedUVs#')
        entry['modifiers'].append(mod);mod.doIt()
        dep=om.MFnDependencyNode(node)
        mod=om.MDGModifier()
        mod.newPlugValueString(dep.findPlug('uvSetName',False),entry['uv_set'])
        tweaks=dep.findPlug('uvTweak',False)
        before,after=entry['before'],entry['after']
        for index in entry['ids']:
            delta=(after[0][index]-before[0][index],after[1][index]-before[1][index])
            if delta==(0.,0.):continue
            plug=tweaks.elementByLogicalIndex(int(index))
            mod.newPlugValueFloat(plug.child(0),delta[0])
            mod.newPlugValueFloat(plug.child(1),delta[1])
        mod.disconnect(incoming,destination)
        mod.connect(incoming,dep.findPlug('inputPolymesh',False))
        mod.connect(dep.findPlug('output',False),destination)
        entry['modifiers'].append(mod);mod.doIt()
        self.mesh_fn(entry).updateSurface()

    def redoIt(self):
        self.applied=[];done=0
        try:
            for entry in self.entries:
                self.applied.append(entry)
                if 'modifiers' in entry:
                    for mod in entry['modifiers']:mod.doIt()
                else:
                    fn=self.mesh_fn(entry)
                    destination=om.MFnDependencyNode(fn.object()).findPlug('inMesh',False)
                    incoming=destination.connectedTo(True,False)
                    if incoming:self.create_history(entry,incoming[0],destination)
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
