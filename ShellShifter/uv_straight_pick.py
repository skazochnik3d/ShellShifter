"""Click UV segments to add or remove custom H/V guides."""
import uv_straight_keymaps as keymaps
import math
import heapq
import weakref
from collections import defaultdict
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid
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


def segment_range(mesh, axis, first, last):
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
        self.active = state.get('active', False)
        self.axis = state.get('axis')
        self.swallow_release = False
        self.pending_press = None
        self.replaying_press = False
        self.anchor = state.get('anchor')
        self.message = state.get('message', 'Segment picker off.')
        app = QtWidgets.QApplication.instance()
        app.installEventFilter(self)
        app.aboutToQuit.connect(self.detach_filter)

    def snapshot(self):
        return dict(active=self.active, axis=self.axis, anchor=self.anchor,
                    message=self.message)

    def sync(self):
        live = []
        for reference in _clients:
            client = reference()
            if (client is None or not isValid(client) or client.stopped or
                    not isValid(client.button) or not isValid(client.feedback)):
                continue
            live.append(reference)
            buttons = [(client.button, self.active)]
            buttons.extend((button, axis == self.axis)
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

    def set_axis(self, axis):
        if axis not in (None, 'H', 'V'):
            raise ValueError('Choose Automatic, H or V for segment picking.')
        if axis != self.axis:
            self.anchor = None
        self.axis = axis
        self.setText(self.instructions() if self.active else 'Segment picker off.')

    def instructions(self):
        return ('{} = add/remove {} segment. Shift + click = path from saved start. '
                'Drag to move shells; click away from edges to select. Esc = exit.').format(
                    keymaps.text('pick'), self.axis or 'H/V')

    def set_active(self, enabled):
        enabled = bool(enabled)
        if enabled != self.active:
            self.anchor = None
        self.active = enabled
        self.swallow_release = False
        self.pending_press = None
        if enabled:
            try:
                import uv_straight_native as native
                if native._manager is None:
                    native.enable()
                native._manager.set_picker_active(True)
                self.setText(self.instructions())
            except (RuntimeError, ValueError) as error:
                self.active = False
                self.setText(str(error))
        else:
            try:
                import uv_straight_native as native
                if native._manager is not None:
                    native._manager.set_picker_active(False)
            except (RuntimeError, ValueError):
                pass
            self.setText('Segment picker off.')

    def pick(self, layer, local_position, manager, range_requested=False, commit=True):
        import uv_straight as uv
        try:
            _, meshes = uv._selection()
        except ValueError:
            self.feedback.setText(
                'Select a UV shell first, or click outside an edge to keep using Maya selection.')
            return False
        bounds = layer.bounds_provider()
        candidates = []
        for index, mesh in enumerate(meshes):
            marked = {}
            for group in uv._marks(mesh):
                for a, b in uv._marked_pairs(mesh, group):
                    marked[tuple(sorted((a, b)))] = group['axis']
            shells = mesh.shells()
            shell_ids = set().union(*shells) if shells else set()
            pairs = {tuple(sorted(pair)) for edge_pairs in mesh.edge_uvs.values()
                     for pair in edge_pairs if set(pair) <= shell_ids}
            for pair in pairs:
                a, b = pair
                pa = project_uv(mesh.points[a], bounds, layer.viewport.width(), layer.viewport.height())
                pb = project_uv(mesh.points[b], bounds, layer.viewport.width(), layer.viewport.height())
                axis = marked.get(pair)
                if axis is None:
                    axis = ('H' if abs(mesh.points[b][0]-mesh.points[a][0]) >=
                            abs(mesh.points[b][1]-mesh.points[a][1]) else 'V')
                key = (index, axis, pair, pair in marked)
                candidates.append((key, (pa.x(), pa.y()), (pb.x(), pb.y())))
        hit = closest_segment((local_position.x(), local_position.y()), candidates)
        if hit is None:
            self.feedback.setText('No UV edge here. Click closer to a visible UV segment.')
            return False
        if not commit:
            return True
        index, axis, pair, is_marked = hit
        mesh = meshes[index]
        a, b = (mesh.points[uv_id] for uv_id in pair)
        natural_axis = 'H' if abs(b[0]-a[0]) >= abs(b[1]-a[1]) else 'V'
        if range_requested and self.anchor is not None:
            (anchor_path, anchor_uuid, anchor_uv_set, anchor_topology, range_axis,
             pin_axis, first, add) = self.anchor
            if (mesh.name != anchor_path or mesh.fn.uuid().asString() != anchor_uuid or
                    mesh.uv_set != anchor_uv_set or
                    uv._topology(mesh) != anchor_topology):
                raise ValueError('Shift range needs two edges on the same mesh and UV set.')
            pairs = segment_range(mesh, range_axis, first, pair)
            changed = (uv.pin_uv_segments(mesh, pairs, pin_axis) if add else
                       uv.unpin_uv_segments(mesh, pairs, pin_axis))
            action = 'Added' if add else 'Removed'
            self.feedback.setText('{} {} {} segments from the saved start.'.format(
                action, len(pairs), pin_axis))
        else:
            add = not is_marked
            pin_axis = (self.axis_override() or natural_axis) if add else axis
            changed = (uv.pin_uv_segment(mesh, pair, pin_axis) if add else
                       uv.unpin_uv_segment(mesh, pair, pin_axis))
            self.anchor = (mesh.name, mesh.fn.uuid().asString(), mesh.uv_set, uv._topology(mesh),
                           natural_axis, pin_axis, pair, add)
            action = 'Added' if add else 'Removed'
            self.feedback.setText('{} {} segment; start saved. Shift + click sets the end.'.format(
                action, pin_axis))
        manager.changes.invalidate()
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
        position = QtCore.QPointF(target.mapFromGlobal(event.globalPosition().toPoint()))
        global_position = QtCore.QPointF(event.globalPosition())
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
                distance = (event.globalPosition()-pending['global_pos']).manhattanLength()
                if distance >= QtWidgets.QApplication.startDragDistance():
                    return self._forward_press(event)
                event.accept()
                return True
            if kind == QtCore.QEvent.MouseButtonRelease and event.button() == pending['button']:
                distance = (event.globalPosition()-pending['global_pos']).manhattanLength()
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
        if kind == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_Escape:
            self.set_active(False)
            event.accept()
            return True
        base_modifiers = keymaps.modifiers('pick')
        range_modifiers = base_modifiers | QtCore.Qt.ShiftModifier
        # Only a normal press/release pair edits a pin. Processing a double-click
        # as another edit would immediately reverse the previous click.
        if (kind != QtCore.QEvent.MouseButtonPress
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
            local = port.mapFromGlobal(event.globalPosition().toPoint())
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
                position=QtCore.QPointF(event.position()),
                global_pos=QtCore.QPointF(event.globalPosition()),
                button=event.button(), buttons=event.buttons(), modifiers=event.modifiers())
            event.accept()
            return True
        return False

    def detach_filter(self):
        self.pending_press = None
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
    def __init__(self, window, button, feedback, axis_buttons=None):
        super().__init__(window)
        self.button, self.feedback = button, feedback
        self.axis_buttons = axis_buttons or {}
        self.stopped = False
        _clients.append(weakref.ref(self))
        button.toggled.connect(self.set_active)
        for axis, axis_button in self.axis_buttons.items():
            axis_button.toggled.connect(
                lambda checked=False, axis=axis: self.set_axis(axis, checked))
        session().sync()

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
            session().set_axis(axis if checked else None)

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
