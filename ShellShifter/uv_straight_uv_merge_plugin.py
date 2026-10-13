"""One undoable bulk UV merge command. No custom nodes are saved in scenes."""
import json
import maya.api.OpenMaya as om
import maya.cmds as cmds


def maya_useNewAPI():
    pass


def mesh_fn(shape):
    selection = om.MSelectionList()
    selection.add(shape)
    return om.MFnMesh(selection.getDagPath(0))


def write_uvs(shape, arrays):
    uv, counts, ids = arrays
    mesh_fn(shape).clearUVs('map1')
    fn = mesh_fn(shape)
    if len(uv):
        fn.setUVs(uv[:, 0].tolist(), uv[:, 1].tolist(), 'map1')
    fn.assignUVs(counts.tolist(), ids.tolist(), 'map1')
    fn.updateSurface()


class MergeCommand(om.MPxCommand):
    def __init__(self):
        super().__init__()
        self.modifiers = []
        self.before = []
        self.results = []
        self.selection = None

    @staticmethod
    def creator():
        return MergeCommand()

    def isUndoable(self):
        return True

    def doIt(self, args):
        import uv_straight_uv_merge as merge
        self.results = merge._pending.pop(args.asString(0))
        self.selection = om.MGlobal.getActiveSelectionList()
        from uv_straight_uv_sets import uv_data
        for result in self.results:
            fn = mesh_fn(result.shape)
            self.before.append((result.shape, {name: uv_data(fn, name) for name in fn.getUVSetNames()},
                                fn.currentUVSetName()))
        self.redoIt()

    def execute(self, modifier):
        # Keep the modifier before executing it so failures can undo partial work.
        self.modifiers.append(modifier)
        modifier.doIt()

    def redoIt(self):
        self.modifiers = []
        try:
            for result in self.results:
                self.apply(result)
        except Exception:
            self.undoIt()
            raise
        finally:
            om.MGlobal.setActiveSelectionList(self.selection)

    def apply(self, result):
        shape = result.shape
        fn = mesh_fn(shape)
        dep = om.MFnDependencyNode(fn.object())
        incoming = dep.findPlug('inMesh', False).connectedTo(True, False)
        # Capture UV chooser destinations before changing the set names.
        uv_sets = dep.findPlug('uvSet', False)
        name_attr = dep.attribute('uvSetName')
        destinations = []
        for index in uv_sets.getExistingArrayAttributeIndices():
            name_plug = uv_sets.elementByLogicalIndex(index).child(name_attr)
            destinations.extend((name_plug, dest) for dest in name_plug.connectedTo(False, True))
        first = list(fn.getUVSetNames())[0] if fn.numUVSets else None
        # Keep map1 in the primary slot so other sets can be removed in one batch.
        if first != 'map1':
            if 'map1' in fn.getUVSetNames():
                mod = om.MDGModifier()
                fn.renameUVSet('map1', '__shellShifterOldMap1', mod)
                self.execute(mod)
            mod = om.MDGModifier()
            if first:
                mesh_fn(shape).renameUVSet(first, 'map1', mod)
            else:
                mesh_fn(shape).createUVSet('map1', mod)
            self.execute(mod)
        if incoming:
            # Native polyTransfer retains upstream geometry/history. Its UV
            # source is storable mesh data, so saving needs no ShellShifter node.
            fn = mesh_fn(shape)
            data = om.MFnMeshData().create()
            snapshot = om.MFnMesh(om.MFnMesh().copy(fn.object(), data))
            snapshot.clearUVs('map1')
            uv, counts, ids = result.output
            if len(uv):
                snapshot.setUVs(uv[:, 0].tolist(), uv[:, 1].tolist(), 'map1')
            snapshot.assignUVs(counts.tolist(), ids.tolist(), 'map1')
            mod = om.MDGModifier()
            node = mod.createNode('polyTransfer')
            mod.renameNode(node, 'shellShifterUVTransfer#')
            node_fn = om.MFnDependencyNode(node)
            mod.newPlugValue(node_fn.findPlug('otherPoly', False), data)
            mod.disconnect(incoming[0], dep.findPlug('inMesh', False))
            mod.connect(incoming[0], node_fn.findPlug('inputPolymesh', False))
            mod.connect(node_fn.findPlug('output', False), dep.findPlug('inMesh', False))
            self.execute(mod)
        else:
            write_uvs(shape, result.output)
        # Redirect all old-set UV chooser inputs to map1, undoably.
        dep = om.MFnDependencyNode(mesh_fn(shape).object())
        uv_sets = dep.findPlug('uvSet', False)
        target = next(uv_sets.elementByLogicalIndex(index).child(name_attr)
                      for index in uv_sets.getExistingArrayAttributeIndices()
                      if uv_sets.elementByLogicalIndex(index).child(name_attr).asString() == 'map1')
        mod = om.MDGModifier()
        for original, destination in destinations:
            if original != target:
                mod.disconnect(original, destination)
                mod.connect(target, destination)
        for attr, value in result.metadata.items():
            mod.newPlugValueString(dep.findPlug(attr, False), json.dumps(value))
        self.execute(mod)
        # Validate complete merged arrays before deleting any extra set.
        self.verify(result)
        for name in list(mesh_fn(shape).getUVSetNames()):
            if name != 'map1':
                mod = om.MDGModifier()
                mesh_fn(shape).deleteUVSet(name, mod)
                self.execute(mod)
        mod = om.MDGModifier()
        mesh_fn(shape).setCurrentUVSetName('map1', mod)
        self.execute(mod)
        self.verify(result)
        if list(mesh_fn(shape).getUVSetNames()) != ['map1']:
            raise RuntimeError('Extra UV sets remain on '+shape)

    def verify(self, result):
        from uv_straight_uv_merge import verify_output
        verify_output(result.shape, result.output)

    def undoIt(self):
        for mod in reversed(self.modifiers):
            mod.undoIt()
        for shape, arrays, current in self.before:
            fn = mesh_fn(shape)
            dep = om.MFnDependencyNode(fn.object())
            # History changes were undone by the modifiers. Restore direct UV
            # data only on shapes whose UVs were edited without history.
            if not dep.findPlug('inMesh', False).isDestination:
                for name, data in arrays.items():
                    fn = mesh_fn(shape)
                    fn.clearUVs(name)
                    fn = mesh_fn(shape)
                    uv, counts, ids = data
                    if len(uv):
                        fn.setUVs(uv[:, 0].tolist(), uv[:, 1].tolist(), name)
                    fn.assignUVs(counts.tolist(), ids.tolist(), name)
                mesh_fn(shape).setCurrentUVSetName(current)
                mesh_fn(shape).updateSurface()
        om.MGlobal.setActiveSelectionList(self.selection)


def initializePlugin(obj):
    om.MFnPlugin(obj, 'ShellShifter', '1.4.131-beta', 'Any').registerCommand('shellShifterMergeUVSets', MergeCommand.creator)


def uninitializePlugin(obj):
    om.MFnPlugin(obj).deregisterCommand('shellShifterMergeUVSets')
