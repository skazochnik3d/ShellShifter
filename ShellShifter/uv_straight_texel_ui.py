"""Shared live texel-density display, updated outside Maya DG notifications."""
import math
import time
from PySide6 import QtCore, QtWidgets
import maya.api.OpenMaya as om
import maya.cmds as cmds
import uv_straight_texel as texel

_display = globals().get('_display')


class DensityMesh:
    """Only the UV and DAG data needed by the density measurement."""
    def __init__(self, path):
        self.path = path
        self.name = path.fullPathName()
        self.fn = om.MFnMesh(path)
        self.uv_set = self.fn.currentUVSetName()
        self.u, self.v = self.fn.getUVs(self.uv_set)
        self._points = self._shell_ids = None

    @property
    def points(self):
        if self._points is None:
            self._points = dict(enumerate(zip(self.u,self.v)))
        return self._points

    @property
    def shell_ids(self):
        if self._shell_ids is None:
            _, self._shell_ids = self.fn.getUvShellsIds(self.uv_set)
        return self._shell_ids

    def components(self, ids):
        return ['{}.map[{}]'.format(self.name, index) for index in ids]


class TexelDisplay(QtCore.QObject):
    changed = QtCore.Signal(object)

    def __init__(self):
        super().__init__(QtWidgets.QApplication.instance())
        self.groups = []
        self.scope = []
        self.callbacks = []
        self.active = self.refreshing = self.dirty = self.scene_invalid = False
        self.resolution = 2048
        self.reference_density = None
        self.signature = None
        self.settle_until = self.last_scan = 0.
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.poll)

    def invalidate(self, *unused):
        # Never read/evaluate/edit the DG in a Maya notification.
        self.dirty = True

    def history_changed(self, *unused):
        self.dirty = True
        self.settle_until = time.monotonic() + .8

    def scene_changed(self, *unused):
        self.scene_invalid = True

    def watch(self, groups):
        seen_meshes, seen_nodes = set(), set()
        for group in groups:
            for shell in group.shells:
                path = shell.mesh.path
                node = path.node()
                uuid = om.MFnDependencyNode(node).uuid().asString()
                if uuid in seen_meshes:
                    continue
                seen_meshes.add(uuid)
                self.scope.append((om.MObjectHandle(node), om.MDagPath(path)))
                def dirty_mesh(node, plug, unused=None):
                    name = plug.partialName(useLongNames=True)
                    if name.startswith(('outMesh', 'inMesh', 'uvSet', 'currentUVSet', 'pnts')):
                        self.dirty = True
                self.callbacks.append(om.MNodeMessage.addNodeDirtyPlugCallback(node, dirty_mesh))
                self.callbacks.append(om.MNodeMessage.addAttributeChangedCallback(node,
                    lambda message, plug, other, unused=None: self.invalidate()
                    if plug.partialName(useLongNames=True).startswith(('uvSet', 'currentUVSet', 'pnts', 'inMesh')) else None))
                dag = om.MDagPath(path)
                while dag.length() > 1:
                    dag.pop()
                    transform = dag.node()
                    key = om.MFnDependencyNode(transform).uuid().asString()
                    if key in seen_nodes:
                        continue
                    seen_nodes.add(key)
                    def dirty_transform(node, plug, unused=None):
                        if plug.partialName(useLongNames=True).startswith(
                                ('worldMatrix', 'matrix', 'translate', 'rotate', 'scale',
                                 'shear', 'parentMatrix', 'offsetParentMatrix')):
                            self.dirty = True
                    self.callbacks.append(om.MNodeMessage.addNodeDirtyPlugCallback(transform, dirty_transform))
        self.callbacks.extend(om.MEventMessage.addEventCallback(name, self.history_changed)
                              for name in ('Undo', 'Redo'))
        self.callbacks.extend(om.MSceneMessage.addCallback(name, self.scene_changed)
                              for name in (om.MSceneMessage.kBeforeNew, om.MSceneMessage.kBeforeOpen))

    def remove_callbacks(self):
        for callback in self.callbacks:
            try:om.MMessage.removeCallback(callback)
            except RuntimeError:pass
        self.callbacks = []

    def stop(self):
        self.timer.stop()
        self.remove_callbacks()
        self.active = False
        self.scope = []
        self.signature = None

    def clear(self):
        self.stop()
        self.scene_invalid = False
        self.groups = []
        self.reference_density = None
        texel.clear()
        self.changed.emit([])

    def start(self, groups, resolution):
        self.stop()
        shells = [shell for group in groups for shell in group.shells]
        self.reference_density = math.fsum(shell.density for shell in shells) / len(shells)
        groups = texel._group(shells,5,self.reference_density)
        self.groups = groups
        self.resolution = int(resolution)
        self.scene_invalid = False
        self.settle_until = 0.
        self.watch(groups)
        self.active = True
        _, self.signature = self.snapshot()
        self.refreshing = True
        try:texel.show(groups)
        finally:self.refreshing = False
        self.dirty = False
        self.last_scan = time.monotonic()
        self.changed.emit(groups)
        self.timer.start()

    def snapshot(self):
        meshes, signature = [], []
        for handle, path in self.scope:
            if not handle.isValid() or not handle.isAlive():
                continue
            try:
                if not path.isValid():
                    path = om.MDagPath.getAPathTo(handle.object())
                mesh = DensityMesh(path)
                counts, assigned = mesh.fn.getAssignedUVs(mesh.uv_set)
                points = mesh.fn.getPoints(om.MSpace.kObject)
                polygon_counts, polygon_vertices = mesh.fn.getVertices()
                matrix = tuple(path.inclusiveMatrix())
                signature.append((mesh.name, mesh.uv_set, tuple(zip(mesh.u,mesh.v)),
                                  tuple(counts), tuple(assigned),
                                  tuple(polygon_counts), tuple(polygon_vertices),
                                  tuple((p.x, p.y, p.z) for p in points), matrix))
                meshes.append(mesh)
            except (RuntimeError, ValueError):
                continue
        return meshes, (self.resolution, tuple(signature))

    def refresh(self, force=False):
        if not self.active or self.refreshing:
            return
        self.refreshing = True
        try:
            meshes, signature = self.snapshot()
            self.last_scan = time.monotonic()
            self.dirty = False
            if not force and signature == self.signature:
                return
            if not meshes:
                self.clear()
                return
            try:
                groups = texel._group(texel._measure(meshes, self.resolution), 5, self.reference_density)
            except ValueError:
                # All analyzed meshes may temporarily have no mapped UV faces.
                groups = []
            texel.show(groups)
            self.groups, self.signature = groups, signature
            self.changed.emit(groups)
        finally:
            self.refreshing = False

    def poll(self):
        if not self.active or self.refreshing:
            return
        if self.scene_invalid:
            self.clear()
            return
        now = time.monotonic()
        if now < self.settle_until or QtWidgets.QApplication.mouseButtons() != QtCore.Qt.NoButton:
            return
        # DG callbacks cover edits. A slow fallback also catches direct API edits
        # and renamed/deleted nodes; a signature excludes our own CPV writes.
        if self.dirty or now - self.last_scan >= .75:
            try:self.refresh()
            except (RuntimeError, ValueError):self.dirty = True

    def set_resolution(self, value):
        value = int(value)
        if value == self.resolution:
            return
        self.resolution = value
        if self.active:self.refresh(force=True)


def display():
    global _display
    if _display is None:_display = TexelDisplay()
    return _display


def stop_display():
    global _display
    if _display is not None:
        _display.clear()
        _display.deleteLater()
        _display = None


class TexelDensityController(QtCore.QObject):
    """Bind each palette to one shared analysis scope and live color map."""
    def __init__(self, window, resolution, layout, legend):
        super().__init__(window)
        self.window, self.resolution, self.layout = window, resolution, layout
        self.legend = legend
        self.model = display()
        self.connected = True
        self.model.changed.connect(self.present)
        self.present(self.model.groups)
        resolution.valueChanged.connect(self.model.set_resolution)

    def present(self, groups):
        self.window.texel_groups = groups
        self.legend.setToolTip(
            'Green: {:.4f} px/unit at the start of this analysis. Lower densities are blue/cyan; '
            'higher densities are yellow/red. Analyse again to reset the reference.'.format(self.model.reference_density)
            if self.model.reference_density is not None else 'Blue: low density. Green: reference density. Red: high density.')
        if self.model.active:
            with QtCore.QSignalBlocker(self.resolution):
                self.resolution.setValue(self.model.resolution)
        while self.layout.count():
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        import uv_straight as uv
        for group in groups:
            widget = QtWidgets.QWidget()
            row = QtWidgets.QHBoxLayout(widget);row.setContentsMargins(0,0,0,0)
            swatch = QtWidgets.QLabel();swatch.setFixedSize(18,18)
            color = '#{:02x}{:02x}{:02x}'.format(*(int(c*255) for c in group.color))
            swatch.setStyleSheet('background: {}; border-radius: 5px;'.format(color))
            row.addWidget(swatch)
            uniform = math.isclose(group.minimum, group.maximum,
                                   rel_tol=texel.DENSITY_REL_TOLERANCE,
                                   abs_tol=texel.DENSITY_ABS_TOLERANCE)
            text = ('{:.1f} px/unit · {} shell{}'.format(
                (group.minimum+group.maximum)*.5,len(group.shells),'' if len(group.shells)==1 else 's')
                if uniform else '{:.1f} – {:.1f} px/unit · {} shells'.format(
                    group.minimum,group.maximum,len(group.shells)))
            button = QtWidgets.QPushButton(text)
            button.setToolTip('Select UV shells in this texel-density range')
            button.clicked.connect(lambda checked=False,group=group: uv._run(lambda:texel.select_group(group)))
            row.addWidget(button,1);self.layout.addWidget(widget)
        if hasattr(self.window,'adjust_popup_size'):
            QtCore.QTimer.singleShot(0,self.window.adjust_popup_size)

    def analyse(self):
        groups = texel.analyse(self.resolution.value())
        self.model.start(groups,self.resolution.value())

    def refresh(self):
        self.model.set_resolution(self.resolution.value())
        self.model.refresh(force=True)

    def clear(self):
        self.model.clear()

    def stop(self):
        if self.connected:
            try:
                self.model.changed.disconnect(self.present)
                self.resolution.valueChanged.disconnect(self.model.set_resolution)
            except (RuntimeError,TypeError):
                pass
            self.connected = False
        # Closing a palette keeps the display live until Clear or scene change.
