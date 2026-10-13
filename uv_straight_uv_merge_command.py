"""Reloadable bulk-operation worker; completed undo entries retain their worker."""
import json
import maya.api.OpenMaya as om
import maya.cmds as cmds


def mesh_fn(shape):
    selection = om.MSelectionList()
    selection.add(shape)
    return om.MFnMesh(selection.getDagPath(0))


def write_uvs(shape, arrays, destination='map1'):
    uv, counts, ids = arrays
    mesh_fn(shape).clearUVs(destination)
    fn = mesh_fn(shape)
    if len(uv):
        fn.setUVs(uv[:, 0].tolist(), uv[:, 1].tolist(), destination)
    mesh_fn(shape).assignUVs(counts.tolist(), ids.tolist(), destination)
    mesh_fn(shape).updateSurface()


class MergeOperation:
    def __init__(self):
        self.modifiers = []
        self.before = []
        self.results = []
        self.selection = None
        self.stage = 'initialising'
        self.shape = ''
        self.touched = set()

    def prepare(self, results):
        self.results = results
        self.selection = om.MGlobal.getActiveSelectionList()
        from uv_straight_uv_sets import uv_data
        for result in self.results:
            self.shape = result.shape
            self.stage = 'saving original UV sets'
            try:
                names = list(mesh_fn(result.shape).getUVSetNames())
                self.before.append((result.shape, {name: uv_data(mesh_fn(result.shape), name) for name in names},
                                    mesh_fn(result.shape).currentUVSetName()))
            except RuntimeError as error:
                raise RuntimeError('Cannot save original UVs on {} before Apply: {}'.format(result.shape, error)) from error

    def execute(self, modifier):
        # Keep the modifier before executing it so failures can undo partial work.
        self.modifiers.append(modifier)
        modifier.doIt()

    def redoIt(self):
        self.modifiers = []
        self.touched = set()
        try:
            for result in self.results:
                self.shape = result.shape
                self.touched.add(result.shape)
                self.apply(result)
        except Exception as error:
            failed_shape, failed_stage = self.shape, self.stage
            rollback_error = ''
            try:
                self.undoIt()
            except Exception as rollback:
                rollback_error = ' Rollback also reported: '+str(rollback)
            import uv_straight_uv_merge as merge
            merge._last_error_context = dict(shape=failed_shape, stage=failed_stage,
                                             original_error=str(error), rollback_error=rollback_error)
            raise RuntimeError('UV cleanup failed on {} while {}: {}.{}'.format(
                failed_shape, failed_stage, error, rollback_error)) from error
        finally:
            om.MGlobal.setActiveSelectionList(self.selection)

    def apply(self, result):
        shape = result.shape
        destination = result.destination
        protected = result.protected
        self.stage = 'reading mesh and UV links'
        fn = mesh_fn(shape)
        dep = om.MFnDependencyNode(fn.object())
        incoming = dep.findPlug('inMesh', False).connectedTo(True, False)
        # Capture UV chooser destinations before changing the set names.
        uv_sets = dep.findPlug('uvSet', False)
        name_attr = dep.attribute('uvSetName')
        destinations = []
        for index in uv_sets.getExistingArrayAttributeIndices():
            name_plug = uv_sets.elementByLogicalIndex(index).child(name_attr)
            destinations.extend((name_plug, dest, name_plug.asString()) for dest in name_plug.connectedTo(False, True))
        first = list(fn.getUVSetNames())[0] if fn.numUVSets else None
        # Preserve an existing protected primary set. Otherwise reuse the
        # primary slot for the destination, since Maya cannot delete that slot.
        names = list(fn.getUVSetNames())
        if first != destination and first not in protected:
            self.stage = 'making '+destination+' the primary set'
            if destination in names:
                temporary = '__shellShifterOldDestination'
                while temporary in names:
                    temporary += '_'
                mod = om.MDGModifier()
                fn.renameUVSet(destination, temporary, mod)
                self.execute(mod)
            mod = om.MDGModifier()
            if first:
                mesh_fn(shape).renameUVSet(first, destination, mod)
            else:
                mesh_fn(shape).createUVSet(destination, mod)
            self.execute(mod)
        elif destination not in names:
            self.stage = 'creating '+destination
            mod = om.MDGModifier()
            mesh_fn(shape).createUVSet(destination, mod)
            self.execute(mod)
        # Renaming UV sets can add or replace history nodes. Re-read the live
        # shape and history plugs after those commands instead of retaining them.
        dep = om.MFnDependencyNode(mesh_fn(shape).object())
        incoming = dep.findPlug('inMesh', False).connectedTo(True, False)
        if incoming:
            self.stage = 'writing merged UVs through construction history'
            # Native polyTransfer retains upstream geometry/history. Its UV
            # source is storable mesh data, so saving needs no ShellShifter node.
            fn = mesh_fn(shape)
            data = om.MFnMeshData().create()
            snapshot = om.MFnMesh(om.MFnMesh().copy(fn.object(), data))
            snapshot.clearUVs(destination)
            uv, counts, ids = result.output
            if len(uv):
                snapshot.setUVs(uv[:, 0].tolist(), uv[:, 1].tolist(), destination)
            snapshot.assignUVs(counts.tolist(), ids.tolist(), destination)
            mod = om.MDGModifier()
            node = mod.createNode('polyTransfer')
            mod.renameNode(node, 'shellShifterUVTransfer#')
            # Materialise the node before requesting its plugs. In an active
            # GUI evaluation cycle, a queued creation may not yet own live plugs.
            self.execute(mod)
            node_fn = om.MFnDependencyNode(node)
            mod = om.MDGModifier()
            mod.newPlugValue(node_fn.findPlug('otherPoly', False), data)
            mod.disconnect(incoming[0], dep.findPlug('inMesh', False))
            mod.connect(incoming[0], node_fn.findPlug('inputPolymesh', False))
            mod.connect(node_fn.findPlug('output', False), dep.findPlug('inMesh', False))
            self.execute(mod)
        else:
            self.stage = 'writing merged UV coordinates and face assignments'
            write_uvs(shape, result.output, destination)
        # Preserve links to protected sets; redirect removed sets to destination.
        dep = om.MFnDependencyNode(mesh_fn(shape).object())
        self.stage = 'updating UV links and pin metadata'
        uv_sets = dep.findPlug('uvSet', False)
        name_attr = dep.attribute('uvSetName')
        plugs = {uv_sets.elementByLogicalIndex(index).child(name_attr).asString():
                 uv_sets.elementByLogicalIndex(index).child(name_attr)
                 for index in uv_sets.getExistingArrayAttributeIndices()}
        mod = om.MDGModifier()
        for original, linked, name in destinations:
            target = plugs[name if name in protected else destination]
            if original != target:
                mod.disconnect(original, linked)
                mod.connect(target, linked)
        for attr, value in result.metadata.items():
            mod.newPlugValueString(dep.findPlug(attr, False), json.dumps(value))
        self.execute(mod)
        # Validate complete merged arrays before deleting any extra set.
        self.stage = 'verifying merged UVs before deletion'
        self.verify(result)
        for name in list(mesh_fn(shape).getUVSetNames()):
            if name not in protected:
                self.stage = 'deleting extra UV set '+name
                mod = om.MDGModifier()
                mesh_fn(shape).deleteUVSet(name, mod)
                self.execute(mod)
        mod = om.MDGModifier()
        self.stage = 'setting '+destination+' current'
        mesh_fn(shape).setCurrentUVSetName(destination, mod)
        self.execute(mod)
        self.stage = 'verifying the final UV layout'
        self.verify(result)
        names = list(mesh_fn(shape).getUVSetNames())
        if destination not in names or any(name not in protected for name in names):
            raise RuntimeError('Extra UV sets remain on '+shape)

    def verify(self, result):
        from uv_straight_uv_merge import verify_output
        verify_output(result.shape, result.output, result.destination)
        from uv_straight_uv_sets import uv_data
        import numpy as np
        original = next(arrays for shape, arrays, current in self.before if shape == result.shape)
        for name in result.protected:
            if name != result.destination and name in original:
                current = uv_data(mesh_fn(result.shape), name)
                if not all(np.array_equal(a, b) for a, b in zip(original[name], current)):
                    raise RuntimeError('Protected UV set changed: '+name)

    def undoIt(self):
        for mod in reversed(self.modifiers):
            mod.undoIt()
        for shape, arrays, current in self.before:
            if shape not in self.touched:
                continue
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
                    mesh_fn(shape).assignUVs(counts.tolist(), ids.tolist(), name)
                mesh_fn(shape).setCurrentUVSetName(current)
                mesh_fn(shape).updateSurface()
        om.MGlobal.setActiveSelectionList(self.selection)
