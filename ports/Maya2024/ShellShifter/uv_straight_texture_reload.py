"""Reload connected Maya File textures after their source images change."""
from collections import defaultdict
import glob
import os
from pathlib import Path
import re
import sys

from PySide2 import QtCore
import maya.api.OpenMaya as om
from maya.api.OpenMayaUI import M3dView
import maya.cmds as cmds
import maya.mel as mel

from uv_straight_material_textures import absolute_texture_path


def path_key(path):
    return os.path.normcase(os.path.abspath(path))


def file_matches(pattern):
    if '<udim>' not in pattern.lower():
        return (pattern,)
    expression = re.sub('<udim>', '1[0-9][0-9][0-9]', glob.escape(pattern), flags=re.I)
    return (path_key(path) for path in glob.iglob(expression))


class TextureReload(QtCore.QObject):
    changed = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.watcher = QtCore.QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self.file_changed)
        self.watcher.directoryChanged.connect(self.directory_changed)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.flush)
        self.nodes = {}
        self.bindings = {}
        self.observed = {}
        self.dirty_directories = set()
        self.dirty_files = set()
        self.rebuilding = False
        self.reloading = False
        self.node_callbacks = []
        self.callbacks = [
            om.MDGMessage.addNodeAddedCallback(self.invalidate, 'file'),
            om.MDGMessage.addNodeRemovedCallback(self.invalidate, 'file'),
        ]
        for event in (om.MSceneMessage.kAfterNew, om.MSceneMessage.kAfterOpen,
                      om.MSceneMessage.kAfterImport, om.MSceneMessage.kAfterLoadReference,
                      om.MSceneMessage.kAfterUnloadReference):
            self.callbacks.append(om.MSceneMessage.addCallback(event, self.invalidate))
        self.invalidate()

    def invalidate(self, *unused):
        self.rebuilding = True
        self.timer.start()

    def file_changed(self, path):
        path = path_key(path)
        self.dirty_files.add(path)
        self.directory_changed(os.path.dirname(path))

    def directory_changed(self, path):
        self.dirty_directories.add(path_key(path))
        self.timer.start()

    def attribute_changed(self, message, plug, *unused):
        if self.reloading:
            return
        if message & om.MNodeMessage.kAttributeSet:
            if plug.partialName(useLongNames=True) in ('fileTextureName', 'uvTilingMode'):
                self.invalidate()
        elif message & (om.MNodeMessage.kConnectionMade | om.MNodeMessage.kConnectionBroken):
            if not plug.attribute().hasFn(om.MFn.kMessageAttribute):
                self.invalidate()

    def collect_bindings(self):
        om.MMessage.removeCallbacks(self.node_callbacks)
        self.node_callbacks.clear()
        self.nodes.clear()
        sources = defaultdict(set)
        connected = set()
        iterator = om.MItDependencyNodes(om.MFn.kFileTexture)
        while not iterator.isDone():
            node = iterator.thisNode()
            iterator.next()
            fn = om.MFnDependencyNode(node)
            self.node_callbacks.append(om.MNodeMessage.addAttributeChangedCallback(node, self.attribute_changed))
            raw = fn.findPlug('fileTextureName', False).asString()
            if not raw:
                continue
            tiling = fn.findPlug('uvTilingMode', False).asInt()
            pattern = raw
            if tiling == 3 and '<udim>' not in raw.lower():
                pattern = fn.findPlug('computedFileTextureNamePattern', False).asString() or raw
            pattern = path_key(absolute_texture_path(pattern))
            identity = fn.uuid().asString()
            self.nodes[identity] = (om.MObjectHandle(node), raw, tiling)
            sources[pattern].add(identity)
            if any(plug.isSource and not plug.attribute().hasFn(om.MFn.kMessageAttribute)
                   for plug in fn.getConnections()):
                connected.add(pattern)
        # Unconnected File nodes can hold the same image in Maya's cache.
        # Reload them with their connected peers without watching orphan paths.
        bindings = defaultdict(dict)
        for pattern in connected:
            bindings[os.path.dirname(pattern)][pattern] = sources[pattern]
        self.bindings = bindings

    def flush(self):
        native = sys.modules.get('uv_straight_native')
        if native is not None and native.uv_edit_busy():
            self.timer.start()
            return
        folders = set(self.dirty_directories)
        if self.rebuilding:
            self.collect_bindings()
            folders.update(self.bindings)
            folders.update(self.observed)
            self.rebuilding = False
        changed, affected = set(), set()
        for folder in folders:
            previous = self.observed.pop(folder, {})
            current = {}
            for pattern, identities in self.bindings.get(folder, {}).items():
                for path in file_matches(pattern):
                    try:
                        stat = os.stat(path)
                    except OSError:
                        continue
                    if not stat.st_size:
                        continue
                    stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
                    current[path] = stamp
                    if (path in self.dirty_files or
                            (path in previous or folder in self.dirty_directories) and previous.get(path) != stamp):
                        changed.add(path)
                        affected.update(identities)
            if current:
                self.observed[folder] = current
        # Saving by rename drops the old file watch. The directory watch lets
        # us register the replacement and detect new tiles of a UDIM set.
        watched = set(self.watcher.files() + self.watcher.directories())
        desired = {folder for folder in self.bindings if os.path.isdir(folder)}
        desired.update(path for files in self.observed.values() for path in files)
        if watched - desired:
            self.watcher.removePaths(list(watched - desired))
        if desired - watched:
            self.watcher.addPaths(list(desired - watched))
        self.dirty_files.clear()
        self.dirty_directories.clear()
        reloaded = affected and self.reload_nodes(affected)
        if changed:
            self.changed.emit(changed)
        if reloaded and not cmds.about(batch=True):
            # Saving in another app must redraw Maya even while it lacks focus.
            try:
                M3dView.active3dView().refresh(all=True, force=True, offscreen=True)
            except RuntimeError:
                M3dView.scheduleRefreshAllViews()

    def reload_nodes(self, identities):
        was_modified = cmds.file(query=True, modified=True)
        sources = set()
        tiled = []
        cleared = []
        self.reloading = True
        try:
            for identity in identities:
                handle, raw, tiling = self.nodes[identity]
                if not handle.isValid() or not handle.isAlive():
                    continue
                fn = om.MFnDependencyNode(handle.object())
                plug = fn.findPlug('fileTextureName', False)
                if plug.isLocked or plug.isDestination or plug.asString() != raw:
                    continue
                try:
                    if fn.isFromReferencedFile:
                        plug.setString(raw)
                        sources.add(raw)
                        continue
                    space = fn.findPlug('colorSpace', False)
                    color_space = space.asString()
                    proxy = tiling and fn.findPlug('uvTileProxyQuality', False).asInt()
                    plug.setString('')
                    cleared.append((fn.name(), plug, raw, space, color_space, proxy))
                except RuntimeError:
                    continue
            # Evaluate all aliases with no image before restoring any of them.
            # Simply setting the same path leaves shared Maya caches stale.
            for node, *_ in cleared:
                try:
                    cmds.colorAtPoint(node, output='RGB', u=.5, v=.5)
                except RuntimeError:
                    pass
        finally:
            for node, plug, raw, space, color_space, proxy in cleared:
                try:
                    plug.setString(raw)
                    if not space.isLocked and not space.isDestination and space.asString() != color_space:
                        space.setString(color_space)
                    sources.add(raw)
                    if proxy:
                        tiled.append(node)
                except RuntimeError as error:
                    cmds.warning('ShellShifter texture reload: {}'.format(error))
            self.reloading = False
            if (sources or cleared) and not was_modified:
                cmds.file(modified=False)
        for raw in sources:
            try:
                cmds.callbacks(raw, executeCallbacks=True, hook='textureReload')
            except RuntimeError as error:
                cmds.warning('ShellShifter texture reload: {}'.format(error))
        if sources and not cmds.about(batch=True):
            for node in tiled:
                try:
                    mel.eval('generateUvTilePreview "{}"'.format(node))
                except RuntimeError as error:
                    cmds.warning('ShellShifter UV tile preview: {}'.format(error))
        return bool(sources)

    def stop(self):
        self.timer.stop()
        self.watcher.blockSignals(True)
        watched = self.watcher.files() + self.watcher.directories()
        if watched:
            self.watcher.removePaths(watched)
        om.MMessage.removeCallbacks(self.callbacks + self.node_callbacks)
        self.callbacks.clear()
        self.node_callbacks.clear()


_service = None


def start():
    global _service
    if _service is None:
        app = QtCore.QCoreApplication.instance()
        _service = TextureReload(app)
        app.aboutToQuit.connect(stop)
    return _service


def stop():
    global _service
    if _service is not None:
        QtCore.QCoreApplication.instance().aboutToQuit.disconnect(stop)
        _service.stop()
        _service.deleteLater()
        _service = None
