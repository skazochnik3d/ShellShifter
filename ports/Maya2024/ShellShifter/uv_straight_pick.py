"""Click UV segments to add or remove custom H/V guides."""
import uv_straight_keymaps as keymaps
import math
import heapq
import weakref
from collections import defaultdict
from uv_straight_deps import np
from PySide2 import QtCore, QtGui, QtWidgets
from shiboken2 import isValid
from uv_straight_overlay import project_uv

_clients = globals().get('_clients', [])
_previous_session = globals().get('_session')
_saved_state = (_previous_session.snapshot() if _previous_session is not None else {})
if _previous_session is not None:
    _previous_session.detach_filter()
_session = None


def closest_segment(position, candidates, radius=8.):
    """Screen-space picking. Reject ties at corners/overlapping UV shells."""
    hits = {}
    x, y = position
    for key, a, b in candidates:
        dx, dy = b[0]-a[0], b[1]-a[1]
        length2 = dx*dx+dy*dy
        if length2 < 1e-12:
            continue
        t = max(0., min(1., ((x-a[0])*dx+(y-a[1])*dy)/length2))
        distance = math.hypot(x-a[0]-t*dx, y-a[1]-t*dy)
        if distance <= radius:
            hits[key] = min(distance, hits.get(key, float('inf')))
    ordered = sorted(hits, key=hits.get)
    if not ordered:
        return None
    if len(ordered) > 1 and abs(hits[ordered[1]]-hits[ordered[0]]) < .75:
        raise ValueError('Ambiguous corner/overlap: click the middle of the segment or select one mesh.')
    return ordered[0]


def selected_border_axes(meshes):
    """Infer axes from incident UV boundary edges, never from interior edges."""
    axes=set()
    for mesh in meshes:
        neighbors=getattr(mesh,'_pin_border_neighbors',None)
        if neighbors is None:
            import uv_straight as uv
            neighbors=defaultdict(set)
            for a,b in uv._border_pairs(mesh,mesh.all_shells()):
                neighbors[a].add(b);neighbors[b].add(a)
        for i in mesh.explicit_selected:
            a=mesh.points[i]
            for j in neighbors.get(i,()):
                b=mesh.points[j];dx,dy=abs(b[0]-a[0]),abs(b[1]-a[1])
                if max(dx,dy)>1e-12:
                    # A 45-degree edge has no preferred axis.
                    if abs(dx-dy)<=1e-6*max(dx,dy):axes.update(('H','V'))
                    else:axes.add('H' if dx>dy else 'V')
            if len(axes)==2:return frozenset(axes)
    return frozenset(axes or ('H','V'))


def enabled_pin_axes():
    """User-selected H/V switches, independent of inferred edge directions."""
    if _session is not None:return _session.axes
    return frozenset(('H','V'))


def auto_mark_axes(meshes):
    """Allowed axes for automatic contour search and automatic pin clicks."""
    axes=enabled_pin_axes()
    # An explicit Auto H/V action still works when click assignment is off.
    return axes if len(axes)==1 else selected_border_axes(meshes)


def closest_uv_segment(position,meshes,bounds,width,height,radius=8.,axes=None):
    """Vectorized broad phase in UV, then exact pixel distances for nearby edges."""
    left,right,bottom,top=bounds
    if not all(math.isfinite(v) for v in bounds) or right<=left or top<=bottom or width<=0 or height<=0:
        raise ValueError('UV viewport bounds are invalid')
    scale=np.array((width/(right-left),-height/(top-bottom)))
    point=np.array((left+position[0]/scale[0],top+position[1]/scale[1]));extent=radius/np.abs(scale)
    hits=[]
    for index,mesh in enumerate(meshes):
        pairs=getattr(mesh,'_pin_uv_pairs',None)
        if pairs is None:pairs=np.asarray(sorted({tuple(sorted(p)) for values in mesh.edge_uvs.values() for p in values}),dtype=np.int32).reshape(-1,2)
        if not len(pairs) or not mesh.selected:continue
        if len(mesh.selected)!=len(mesh.points):
            owners=getattr(mesh,'_pin_shell_array',None)
            if owners is None:owners=np.asarray(mesh.shell_ids)
            selected=np.unique(owners[np.fromiter(mesh.selected,dtype=int)])
            pairs=pairs[np.isin(owners[pairs[:,0]],selected)]
        coordinates=getattr(mesh,'_pin_coordinates',None)
        if coordinates is None:coordinates=np.array([mesh.points[i] for i in range(len(mesh.points))])
        a=coordinates[pairs[:,0]];b=coordinates[pairs[:,1]]
        if axes is not None and len(axes)<2:
            horizontal=np.abs(b[:,0]-a[:,0])>=np.abs(b[:,1]-a[:,1])
            allowed=(horizontal if 'H' in axes else ~horizontal) if axes else np.zeros(len(pairs),dtype=bool)
            pairs=pairs[allowed];a=a[allowed];b=b[allowed]
        near=np.all(np.maximum(a,b)>=point-extent,axis=1)&np.all(np.minimum(a,b)<=point+extent,axis=1)
        pairs=pairs[near];a=(a[near]-point)*scale;b=(b[near]-point)*scale
        if not len(pairs):continue
        delta=b-a;length2=np.sum(delta*delta,axis=1)
        fraction=np.clip(-np.sum(a*delta,axis=1)/np.maximum(length2,1e-30),0.,1.)
        distances=np.linalg.norm(a+fraction[:,None]*delta,axis=1)
        for n in np.flatnonzero((length2>=1e-12)&(distances<=radius)):
            hits.append((float(distances[n]),index,tuple(map(int,pairs[n]))))
    hits.sort()
    if not hits:return None
    if len(hits)>1 and abs(hits[1][0]-hits[0][0])<.75:
        raise ValueError('Ambiguous corner/overlap: click the middle of the segment or select one mesh.')
    return hits[0][1:]


def segment_range(mesh, axis, first, last, axes=None):
    """Find a simple UV edge path; curved shell borders stay on the border."""
    first, last = tuple(sorted(first)), tuple(sorted(last))
    pairs = {tuple(sorted(pair)) for edge_pairs in mesh.edge_uvs.values()
             for pair in edge_pairs}
    if first not in pairs or last not in pairs:
        raise ValueError('The saved start or end edge is no longer available.')
    if first == last:
        return [first]
    import uv_straight as uv
    borders = uv._border_pairs(mesh, mesh.all_shells())
    if first in borders and last in borders:
        pairs = borders
    if axes is not None and len(axes)<2:
        pairs={p for p in pairs if ('H' if abs(mesh.points[p[1]][0]-mesh.points[p[0]][0])>=
                                   abs(mesh.points[p[1]][1]-mesh.points[p[0]][1]) else 'V') in axes}
        if first not in pairs or last not in pairs:
            raise ValueError('The saved path is outside the enabled H/V axes.')
    graph = defaultdict(dict)
    for a, b in pairs - {first, last}:
        weight = max(math.dist(mesh.points[a], mesh.points[b]), 1e-12)
        graph[a][b] = graph[b][a] = weight
    choices = []
    for start in first:
        for end in last:
            outer_start = first[1] if start == first[0] else first[0]
            outer_end = last[1] if end == last[0] else last[0]
            forbidden = {outer_start, outer_end}
            if start in forbidden or end in forbidden:
                continue
            queue = [(0., start, [start])]
            distances = {start: 0.}
            while queue:
                distance, current, path = heapq.heappop(queue)
                if distance > distances[current] + 1e-12:
                    continue
                if current == end:
                    vertices = [outer_start] + path + [outer_end]
                    if len(vertices) == len(set(vertices)):
                        result = [tuple(sorted(pair)) for pair in zip(vertices, vertices[1:])]
                        choices.append((distance, result))
                    break
                for following, weight in sorted(graph[current].items()):
                    if following in forbidden:
                        continue
                    candidate = distance + weight
                    if candidate < distances.get(following, float('inf')) - 1e-12:
                        distances[following] = candidate
                        heapq.heappush(queue, (candidate, following, path + [following]))
    if not choices:
        raise ValueError('No continuous UV path connects these edges on the same shell.')
    choices.sort(key=lambda choice: (choice[0], choice[1]))
    return choices[0][1]


class PickerSession(QtCore.QObject):
    """One input filter and one state, independent of the lifetime of palettes."""
    def __init__(self, state=None):
        super().__init__()
        state = state or {}
        import maya.cmds as cmds
        # Start with pin assignment off and retain manual choices on UI reload.
        axes=state.get('axes',()) if state.get('axes_revision',0)>=3 else ()
        self.axes=frozenset(axes)
        self.active=bool(self.axes)
        self.auto_axes=len(self.axes)==2
        cmds.optionVar(stringValue=('uvStraightPinAxes',''.join(axis for axis in ('H','V') if axis in self.axes)))
        cmds.optionVar(intValue=('uvStraightAutoPinAxes',int(self.auto_axes)))
        self.inferred_axes=frozenset(state.get('inferred_axes',('H','V')))
        self.selection_key = state.get('selection_key')
        self.swallow_release = False
        self.pending_press = None
        self.replaying_press = False
        self.anchor = state.get('anchor') if state.get('axes_revision',0)>=3 else None
        self.message = self.instructions() if self.active else 'Segment picker off.'
        app = QtWidgets.QApplication.instance()
        app.installEventFilter(self)
        app.aboutToQuit.connect(self.detach_filter)
        # Defer overlay startup until the palette is built. Never open an editor
        # or read buffers from a mouse/selection callback during UV edits.
        self.native_timer=QtCore.QTimer(self)
        self.native_timer.setSingleShot(True)
        self.native_timer.timeout.connect(self.sync_native)
        import maya.api.OpenMaya as om
        self.selection_callback=om.MEventMessage.addEventCallback('SelectionChanged',self.queue_selection)
        self.native_timer.start(16)

    def snapshot(self):
        return dict(active=self.active, axes=tuple(self.axes), auto_axes=self.auto_axes, axes_revision=3,inferred_axes=tuple(self.inferred_axes),selection_key=self.selection_key, anchor=self.anchor,
                    message=self.message)

    def sync(self):
        live = []
        for reference in _clients:
            client = reference()
            if (client is None or not isValid(client) or client.stopped or
                    not isValid(client.feedback)):
                continue
            live.append(reference)
            buttons = [(client.button, self.active)] if client.button is not None and isValid(client.button) else []
            auto_button=getattr(client,'auto_button',None)
            if auto_button is not None and isValid(auto_button):buttons.append((auto_button,self.auto_axes))
            buttons.extend((button, axis in self.axes)
                           for axis, button in client.axis_buttons.items())
            for button, checked in buttons:
                blocked = button.blockSignals(True)
                button.setChecked(checked)
                button.blockSignals(blocked)
            client.feedback.setText(self.message)
        _clients[:] = live

    def setText(self, message):
        self.message = message
        self.sync()

    @property
    def feedback(self):
        return self

    def axis_override(self):
        return self.axis

    @property
    def axis(self):
        return next(iter(self.axes)) if len(self.axes)==1 else None

    def set_axes(self, axes):
        import maya.cmds as cmds
        axes=frozenset(axes)
        if not axes.issubset(('H','V')):raise ValueError('Choose H and/or V.')
        changed=axes!=self.axes
        if changed:
            self.anchor=None
            self.pending_press=None
            self.swallow_release=False
        self.axes=axes
        self.active=bool(axes)
        self.auto_axes=len(axes)==2
        cmds.optionVar(stringValue=('uvStraightPinAxes',''.join(axis for axis in ('H','V') if axis in axes)))
        cmds.optionVar(intValue=('uvStraightAutoPinAxes',int(self.auto_axes)))
        if changed:
            self.selection_key=None
            self.native_timer.start(16)
        self.setText(self.instructions() if self.active else 'Segment picker off.')

    def toggle_axis(self, axis, checked):
        self.set_axes((self.axes | {axis}) if checked else (self.axes - {axis}))

    def set_auto_axes(self, enabled):
        # Compatibility with saved shortcuts for the former Auto H/V button.
        self.set_axes(('H','V') if enabled else ())

    def queue_selection(self, *unused):
        # Inference runs on demand at the next click/overlay refresh. Keeping H/V
        # enabled must not add topology reads to Maya's selection callbacks.
        self.selection_key=None

    def refresh_selection(self):
        from uv_straight_pin_cache import _action_cache
        import sys
        manager=getattr(sys.modules.get('uv_straight_native'),'_manager',None)
        try:self.update_selection(getattr(manager,'pin_mesh_cache',_action_cache))
        except (RuntimeError,ValueError) as error:self.setText(str(error))

    def update_selection(self, cache, meshes=None, key=None):
        if not self.auto_axes:return
        if key is None:
            import maya.cmds as cmds
            key=tuple(cmds.ls(selection=True,long=True) or [])
        if key==self.selection_key:return
        self.selection_key=key
        from uv_straight_pin_cache import selected_meshes
        if meshes is None:
            # Object/face/edge selections have no explicit UV point to infer from.
            meshes=selected_meshes(cache) if any('.map[' in s for s in key) else []
        inferred=selected_border_axes(meshes)
        if inferred!=self.inferred_axes:self.anchor=None
        self.inferred_axes=inferred

    def set_axis(self, axis):
        if axis not in (None, 'H', 'V'):
            raise ValueError('Choose Automatic, H or V for segment picking.')
        self.set_axes((axis,) if axis else ('H','V'))

    def instructions(self):
        return ('{} = add/remove {} segment. Shift + click = path from saved start. '
                'Drag to move shells; click away from edges to select. Esc = exit.').format(
                    keymaps.text('pick'), 'automatic H/V' if self.auto_axes else 'forced '+self.axis if self.axis else 'no')

    def set_active(self, enabled):
        # Saved shortcuts and Esc now operate on the visible controls too.
        self.set_axes((self.axes or ('H','V')) if enabled else ())

    def sync_native(self):
        import maya.cmds as cmds
        if cmds.about(batch=True):return
        import uv_straight_native as native
        if native.uv_edit_busy():
            self.native_timer.start(50)
            return
        try:
            if self.active:
                if native._manager is None:native.enable(open_uv_editor=False)
                if not native._manager.pins_enabled:native.set_pins(True)
            manager=native._manager
            if manager is not None and manager.picker_active!=self.active:
                manager.set_picker_active(self.active)
        except (RuntimeError,ValueError) as error:
            self.setText(str(error))

    def pick(self, layer, local_position, manager, range_requested=False, commit=True):
        if not self.axes:return False
        import uv_straight as uv
        try:
            from uv_straight_pin_cache import PinMeshCache,selected_meshes
            cache=getattr(manager,'pin_mesh_cache',None)
            if cache is None:cache=manager.pin_mesh_cache=PinMeshCache()
            meshes=selected_meshes(cache)
            if not meshes:raise ValueError('No selected UVs')
            self.update_selection(cache,meshes)
        except ValueError:
            self.feedback.setText(
                'Select a UV shell first, or click outside an edge to keep using Maya selection.')
            return False
        bounds = layer.bounds_provider()
        manual_axis=self.axis
        # A single switch chooses the constraint to WRITE, not which edge
        # directions can be clicked. Only automatic mode filters by geometry.
        axes=None if manual_axis else auto_mark_axes(meshes)
        hit=closest_uv_segment((local_position.x(),local_position.y()),meshes,bounds,layer.viewport.width(),layer.viewport.height(),axes=axes)
        if hit is None:
            self.feedback.setText('No UV edge here. Click closer to a visible UV segment.')
            return False
        if not commit:
            return True
        index,pair=hit
        mesh = meshes[index]
        a, b = (mesh.points[uv_id] for uv_id in pair)
        natural_axis = 'H' if abs(b[0]-a[0]) >= abs(b[1]-a[1]) else 'V'
        axis=natural_axis;marked_axes=set()
        for group in uv._marks(mesh):
            if pair in {tuple(sorted(edge)) for edge in uv._marked_pairs(mesh,group)}:
                axis=group['axis'];marked_axes.add(axis)
        is_marked=(manual_axis in marked_axes) if manual_axis else bool(marked_axes)
        if range_requested and self.anchor is not None:
            (anchor_path, anchor_uuid, anchor_uv_set, anchor_topology, range_axis,
             pin_axis, first, add) = self.anchor
            if (mesh.name != anchor_path or mesh.fn.uuid().asString() != anchor_uuid or
                    mesh.uv_set != anchor_uv_set or
                    uv._topology(mesh) != anchor_topology):
                raise ValueError('Shift range needs two edges on the same mesh and UV set.')
            pairs = segment_range(mesh, range_axis, first, pair, axes=axes)
            changed = (uv.pin_uv_segments(mesh, pairs, pin_axis,replace_axis=bool(manual_axis)) if add else
                       uv.unpin_uv_segments(mesh, pairs, pin_axis))
            action = 'Added' if add else 'Removed'
            self.feedback.setText('{} {} {} segments from the saved start.'.format(
                action, len(pairs), pin_axis))
        else:
            add = not is_marked
            pin_axis = manual_axis or (natural_axis if add else axis)
            changed = (uv.pin_uv_segment(mesh, pair, pin_axis,replace_axis=bool(manual_axis)) if add else
                       uv.unpin_uv_segment(mesh, pair, pin_axis))
            self.anchor = (mesh.name, mesh.fn.uuid().asString(), mesh.uv_set, uv._topology(mesh),
                           natural_axis, pin_axis, pair, add)
            action = 'Added' if add else 'Removed'
            self.feedback.setText('{} {} segment; start saved. Shift + click sets the end.'.format(
                action, pin_axis))
        manager.changes.dirty=True
        manager.changes.dirty_shapes.add(mesh.name)
        manager.refresh()
        # A valid edge/path still owns this gesture when it was already pinned.
        return True

    def _forward_press(self, event):
        """Replay the deferred press and this drag event through native Qt input."""
        pending = self.pending_press
        self.pending_press = None
        self.swallow_release = False
        if pending is None:
            return False
        target = pending['target']()
        if target is None or not isValid(target):
            return False
        # Qt mouse events share their pointing device's latest global position.
        # Snapshot coordinates before creating another event on that device.
        position = QtCore.QPointF(target.mapFromGlobal(event.globalPos()))
        global_position = QtCore.QPointF(event.screenPos())
        kind, button, buttons, modifiers = event.type(), event.button(), event.buttons(), event.modifiers()
        self.replaying_press = True
        try:
            press = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress,
                pending['position'], pending['global_pos'], pending['button'],
                pending['buttons'], pending['modifiers'])
            QtWidgets.QApplication.sendEvent(target, press)
            follow = QtGui.QMouseEvent(kind, position, global_position, button, buttons, modifiers)
            QtWidgets.QApplication.sendEvent(target, follow)
        finally:
            self.replaying_press = False
        return True

    def eventFilter(self, watched, event):
        if self.replaying_press:
            return False
        kind = event.type()
        if self.pending_press is None and kind not in (QtCore.QEvent.MouseButtonPress,QtCore.QEvent.KeyPress):
            return False
        app=QtWidgets.QApplication.instance()
        if app.activeModalWidget() is not None or app.activePopupWidget() is not None:
            self.pending_press=None
            self.swallow_release=False
            return False
        pending = self.pending_press
        if pending is not None:
            if kind in (QtCore.QEvent.ApplicationDeactivate, QtCore.QEvent.WindowDeactivate):
                self.pending_press = None
                self.swallow_release = False
                return False
            if kind == QtCore.QEvent.MouseMove:
                if not event.buttons() & pending['button']:
                    self.pending_press = None
                    self.swallow_release = False
                    return False
                distance = (event.screenPos()-pending['global_pos']).manhattanLength()
                if distance >= QtWidgets.QApplication.startDragDistance():
                    return self._forward_press(event)
                event.accept()
                return True
            if kind == QtCore.QEvent.MouseButtonRelease and event.button() == pending['button']:
                distance = (event.screenPos()-pending['global_pos']).manhattanLength()
                if distance >= QtWidgets.QApplication.startDragDistance():
                    return self._forward_press(event)
                self.pending_press = None
                self.swallow_release = False
                try:
                    self.pick(pending['layer'], pending['local'], pending['manager'],
                              pending['range'])
                except (RuntimeError, ValueError) as error:
                    self.feedback.setText(str(error))
                event.accept()
                return True
        if not self.active:
            return False
        escape=kind == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_Escape
        if kind == QtCore.QEvent.KeyPress and not escape:return False
        base_modifiers = keymaps.modifiers('pick')
        range_modifiers = base_modifiers | QtCore.Qt.ShiftModifier
        # Only a normal press/release pair edits a pin. Processing a double-click
        # as another edit would immediately reverse the previous click.
        if not escape and (kind != QtCore.QEvent.MouseButtonPress
                or event.button() != keymaps.mouse_button('pick')
                or event.modifiers() not in (base_modifiers, range_modifiers)):
            return False
        import uv_straight_native as native
        manager = native._manager
        if manager is None or not isinstance(watched, QtWidgets.QWidget):
            return False
        for layer in list(manager.editors.values()):
            if not isValid(layer) or not isValid(layer.viewport):
                continue
            port = layer.viewport
            if not port.isVisible() or not (watched == port or port.isAncestorOf(watched)):
                continue
            if escape:
                self.set_active(False)
                event.accept()
                return True
            local = port.mapFromGlobal(event.globalPos())
            if not port.rect().contains(local):
                continue
            try:
                # Update projection at click time; panning with an empty overlay
                # must not leave the picker using its original view rectangle.
                if hasattr(manager, 'prepare_picker_view'):
                    manager.prepare_picker_view(layer)
                handled = self.pick(
                    layer, local, manager,
                    event.modifiers() == range_modifiers and
                    range_modifiers != base_modifiers, commit=False)
            except (RuntimeError, ValueError) as error:
                self.feedback.setText(str(error))
                handled = False
            if not handled:
                # Let Maya receive empty-space clicks and clicks on shells that
                # are not part of the current selection.
                return False
            self.swallow_release = True
            # A short click edits the segment on release. A drag forwards this
            # original press and every following move/release to Maya, so a UV
            # manipulator can start even when it overlaps a pinned edge.
            self.pending_press = dict(
                target=weakref.ref(watched), layer=layer, local=local, manager=manager,
                range=event.modifiers() == range_modifiers and range_modifiers != base_modifiers,
                position=QtCore.QPointF(event.localPos()),
                global_pos=QtCore.QPointF(event.screenPos()),
                button=event.button(), buttons=event.buttons(), modifiers=event.modifiers())
            event.accept()
            return True
        return False

    def detach_filter(self):
        self.pending_press = None
        self.native_timer.stop()
        if self.selection_callback is not None:
            import maya.api.OpenMaya as om
            om.MMessage.removeCallback(self.selection_callback)
            self.selection_callback=None
        self.swallow_release = False
        app = QtWidgets.QApplication.instance()
        app.removeEventFilter(self)
        try:
            app.aboutToQuit.disconnect(self.detach_filter)
        except (RuntimeError, TypeError):
            pass


def session():
    global _session
    if _session is None:
        _session = PickerSession(_saved_state)
    return _session


class PinSegmentPicker(QtCore.QObject):
    """Palette adapter; closing it does not shut down the shared click tool."""
    def __init__(self, window, button, feedback, axis_buttons=None, auto_button=None):
        super().__init__(window)
        self.button, self.feedback = button, feedback
        self.axis_buttons = axis_buttons or {}
        self.auto_button=auto_button
        self.stopped = False
        _clients.append(weakref.ref(self))
        if button is not None:button.toggled.connect(self.set_active)
        if auto_button is not None:auto_button.toggled.connect(self.set_auto_axes)
        for axis, axis_button in self.axis_buttons.items():
            axis_button.toggled.connect(
                lambda checked=False, axis=axis: self.set_axis(axis, checked))
        session().sync()

    def set_auto_axes(self, enabled):
        if not self.stopped:session().set_auto_axes(enabled)

    @property
    def active(self):
        return not self.stopped and session().active

    @property
    def anchor(self):
        return session().anchor

    @property
    def swallow_release(self):
        return session().swallow_release

    @swallow_release.setter
    def swallow_release(self, value):
        session().swallow_release = value

    def axis_override(self):
        return session().axis

    def set_axis(self, axis, checked):
        if not self.stopped:
            session().toggle_axis(axis, checked)

    def set_active(self, enabled):
        if not self.stopped:
            session().set_active(enabled)

    def pick(self, *args, **kwargs):
        return session().pick(*args, **kwargs)

    def eventFilter(self, watched, event):
        return False if self.stopped else session().eventFilter(watched, event)

    def stop(self):
        self.stopped = True
        session().sync()


# Reinstall exactly one filter when this module is refreshed while a popup is
# created. The active flag, mode and saved range start survive the refresh.
if _previous_session is not None:
    session().sync()
