"""Shared live texel-density display, updated outside Maya DG notifications."""
import math
import time
from PySide2 import QtCore, QtWidgets
from uv_straight_qt5 import signals_blocked
import maya.api.OpenMaya as om
import maya.cmds as cmds
import uv_straight_texel as texel

_display = globals().get('_display')
CONTRAST_OPTION='uvStraightTexelContrast'


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
    contrastChanged = QtCore.Signal(float)

    def __init__(self):
        super().__init__(QtWidgets.QApplication.instance())
        self.groups = []
        self.scope = []
        self.callbacks = []
        self.active = self.refreshing = self.dirty = self.scene_invalid = False
        self.resolution = 2048
        self.reference_density = None
        saved=float(cmds.optionVar(query=CONTRAST_OPTION)) if cmds.optionVar(exists=CONTRAST_OPTION) else 1.
        self.contrast=round(max(.1,min(100.,saved)),2) if math.isfinite(saved) else 1.
        self.contrast_timer=QtCore.QTimer(self);self.contrast_timer.setSingleShot(True);self.contrast_timer.setInterval(40)
        self.contrast_timer.timeout.connect(self.recolor)
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
        self.contrast_timer.stop()
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
        groups = texel._group(shells,5,self.reference_density,self.contrast)
        self.groups = groups
        self.resolution = int(resolution)
        self.scene_invalid = False
        self.settle_until = 0.
        self.watch(groups)
        self.active = True
        _, self.signature = self.snapshot()
        self.refreshing = True
        try:texel.show(groups,self.contrast)
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
                groups = texel._group(texel._measure(meshes, self.resolution), 5, self.reference_density,self.contrast)
            except ValueError:
                # All analyzed meshes may temporarily have no mapped UV faces.
                groups = []
            self.contrast_timer.stop()
            texel.show(groups,self.contrast)
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

    def set_contrast(self,value):
        value=float(value)
        if not math.isfinite(value):return
        value=round(max(.1,min(100.,value)),2)
        if value==self.contrast:return
        self.contrast=value;cmds.optionVar(floatValue=(CONTRAST_OPTION,value))
        self.contrastChanged.emit(value)
        if self.active and not self.contrast_timer.isActive():self.contrast_timer.start()

    def flush_contrast(self):
        if self.contrast_timer.isActive():
            self.contrast_timer.stop();self.recolor()

    def recolor(self):
        """Use cached measurements; dragging must never walk mesh topology."""
        if not self.active or self.refreshing:return
        shells=[shell for group in self.groups for shell in group.shells]
        self.refreshing=True
        try:
            self.groups=texel._group(shells,5,self.reference_density,self.contrast) if shells else []
            texel.show(self.groups,self.contrast)
            self.changed.emit(self.groups)
        finally:self.refreshing=False


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
        self.expanded_ranges = set()
        if self.model.active:
            # A running analysis may come from the old free-step map-size UI.
            normalize=getattr(resolution,'nearest',int)
            self.model.set_resolution(normalize(self.model.resolution))
        self.connected = True
        self.model.changed.connect(self.present)
        self.model.contrastChanged.connect(self.sync_contrast)
        self.present(self.model.groups)
        resolution.valueChanged.connect(self.model.set_resolution)
        legend.valueChanged.connect(self.model.set_contrast)
        legend.sliderReleased.connect(self.model.flush_contrast)

    def sync_contrast(self,value):
        with signals_blocked(self.legend):self.legend.setValue(value)

    def present(self, groups):
        self.window.texel_groups = groups
        self.sync_contrast(self.model.contrast)
        self.legend.setReferenceDensity(self.model.reference_density)
        if self.model.active:
            with signals_blocked(self.resolution):
                self.resolution.setValue(self.model.resolution)
        while self.layout.count():
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        keys = {self.range_key(group) for group in groups}
        self.expanded_ranges.intersection_update(keys)
        for group in groups:
            key = self.range_key(group)
            children = texel._group(group.shells,4,group.color_reference,self.model.contrast)
            self.add_range(group,key,False,len(children)>1)
            if key in self.expanded_ranges and len(children)>1:
                for child in children:self.add_range(child,key,True,True)
        if hasattr(self.window,'adjust_popup_size'):
            QtCore.QTimer.singleShot(0,self.window.adjust_popup_size)

    @staticmethod
    def range_key(group):
        # Membership survives cached recoloring and resolution changes.
        return frozenset((shell.mesh.name,shell.mesh.uv_set,shell.shell_id)
                         for shell in group.shells)

    def toggle_range(self,key):
        if key in self.expanded_ranges:self.expanded_ranges.remove(key)
        else:self.expanded_ranges.add(key)
        self.present(self.model.groups)

    def add_range(self,group,key,child,expandable):
        import uv_straight as uv
        widget = QtWidgets.QWidget()
        widget.setObjectName('texelDensitySubrange' if child else 'texelDensityRange')
        row = QtWidgets.QHBoxLayout(widget);row.setContentsMargins(22 if child else 0,0,0,0)
        swatch = QtWidgets.QLabel();swatch.setFixedSize(12 if child else 18,12 if child else 18)
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
        button.setToolTip('Left-click to select UV shells in this density range.'+
                         ('\nRight-click: '+('collapse subranges.' if child or key in self.expanded_ranges else
                                    'show up to four subranges.') if expandable else
                          '\nAll shells in this range have the same density.'))
        if expandable:
            button.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            button.customContextMenuRequested.connect(lambda pos,key=key:self.toggle_range(key))
        button.clicked.connect(lambda checked=False,group=group: uv._run(lambda:texel.select_group(group)))
        row.addWidget(button,1);self.layout.addWidget(widget)

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
                self.model.contrastChanged.disconnect(self.sync_contrast)
                self.resolution.valueChanged.disconnect(self.model.set_resolution)
                self.legend.valueChanged.disconnect(self.model.set_contrast)
                self.legend.sliderReleased.disconnect(self.model.flush_contrast)
            except (RuntimeError,TypeError):
                pass
            self.connected = False
        # Closing a palette keeps the display live until Clear or scene change.
