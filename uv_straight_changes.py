"""Invalidate cached UV previews without repeatedly walking the entire mesh."""
import maya.api.OpenMaya as om
import time


class ChangeTracker:
    def __init__(self, history_callback=None):
        self.dirty = True
        self.nodes = {}
        self.dirty_shapes = set()
        self.all_dirty = True
        self.scope_dirty = True
        self.previewing = False
        self.history_revision = 0
        self.history_time = 0.
        self.history_callback = history_callback
        self.events = [om.MEventMessage.addEventCallback('SelectionChanged', self.selection_changed)]
        self.events.extend(om.MEventMessage.addEventCallback(name, self.history_changed)
                           for name in ('Undo', 'Redo'))

    def history_changed(self, *args):
        # Callback only flags state; never query/edit Maya during Undo/Redo.
        self.history_revision += 1
        self.history_time = time.monotonic()
        self.dirty = True
        self.all_dirty = True
        self.scope_dirty = True
        if self.history_callback is not None:
            self.history_callback()

    def history_settling(self):
        # Let Maya finish DG replacement and queued Qt paint events before a
        # fresh UV overlay is attached to the editor.
        return time.monotonic() - self.history_time < .75

    def invalidate(self, *args):
        self.dirty = True
        self.all_dirty = True
        self.scope_dirty = True

    def selection_changed(self, *args):
        # Component/mode selection changes the visible UV filter, not the UV
        # coordinates. Keep any real mesh edits already queued by DG events.
        self.dirty = True
        self.scope_dirty = True

    def watch(self, meshes):
        wanted = {mesh.name: mesh for mesh in meshes}
        for name in list(self.nodes):
            handle, callbacks = self.nodes[name]
            if name not in wanted or not handle.isValid() or not handle.isAlive():
                self._remove(callbacks)
                del self.nodes[name]
        for name, mesh in wanted.items():
            if name in self.nodes:
                continue
            node = mesh.path.node()
            # Callbacks only flag selected meshes; no queries, DG evaluation,
            # color writes or Qt activity occur inside Maya's notifications.
            def dirty(node, plug, unused=None, shape=name):
                if self.previewing:
                    return
                attr = plug.partialName(useLongNames=True)
                if attr.startswith(('outMesh', 'inMesh', 'uvSet', 'currentUVSet')):
                    self.dirty_shapes.add(shape)
                    self.dirty = True
            def attribute(message, plug, other, unused=None, shape=name):
                if self.previewing:
                    return
                attr = plug.partialName(useLongNames=True)
                if (attr.startswith(('uvSet', 'currentUVSet', 'uvStraightConstraints')) and
                        message & (om.MNodeMessage.kAttributeSet | om.MNodeMessage.kAttributeAdded |
                                   om.MNodeMessage.kAttributeRemoved | om.MNodeMessage.kConnectionMade |
                                   om.MNodeMessage.kConnectionBroken)):
                    self.dirty_shapes.add(shape)
                    self.dirty = True
            callbacks = [om.MNodeMessage.addNodeDirtyPlugCallback(node, dirty),
                         om.MNodeMessage.addAttributeChangedCallback(node, attribute)]
            self.nodes[name] = (om.MObjectHandle(node), callbacks)

    def clean(self):
        self.dirty = False
        self.all_dirty = False
        self.scope_dirty = False
        self.dirty_shapes.clear()

    def attribute_changed(self, message, *args):
        if message & (om.MNodeMessage.kAttributeSet | om.MNodeMessage.kAttributeAdded |
                      om.MNodeMessage.kAttributeRemoved | om.MNodeMessage.kConnectionMade |
                      om.MNodeMessage.kConnectionBroken):
            self.dirty = True

    @staticmethod
    def _remove(callbacks):
        for callback in callbacks:
            try:
                om.MMessage.removeCallback(callback)
            except RuntimeError:
                pass

    def stop(self):
        self._remove(self.events)
        self.events = []
        for _, callbacks in self.nodes.values():
            self._remove(callbacks)
        self.nodes.clear()
        self.history_callback = None
