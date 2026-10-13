"""ShellShifter for Maya. Run: import shellshifter; shellshifter.show()."""
from collections import defaultdict, deque
from contextlib import contextmanager
from functools import partial
import math
import statistics
import uuid
import json
import hashlib

import maya.cmds as cmds
import maya.api.OpenMaya as om

WINDOW = 'uvStraightQtWindowWorkspaceControl'
PRODUCT_NAME = 'ShellShifter'
VERSION = '1.4.234-beta'
MARK_ATTR = 'uvStraightConstraints'
_preview = None
_window = None


@contextmanager
def _undo(label):
    if not cmds.undoInfo(query=True, state=True):
        raise ValueError('Enable Undo in Maya preferences before using ShellShifter.')
    # Unique identity prevents an empty failed chunk from undoing a prior action.
    chunk = label + ' ' + uuid.uuid4().hex[:8]
    cmds.undoInfo(openChunk=True, chunkName=chunk)
    try:
        yield
    except Exception:
        cmds.undoInfo(closeChunk=True)
        if cmds.undoInfo(query=True, undoName=True) == chunk:
            cmds.undo()
        raise
    else:
        cmds.undoInfo(closeChunk=True)


def select_uv_shell_components(components):
    """Select complete shell UV sets using Maya's shell selection mask."""
    if not components:
        cmds.select(clear=True)
        return
    owners = list(dict.fromkeys(item.split('.map[', 1)[0] for item in components))
    cmds.selectMode(component=True)
    cmds.selectType(allComponents=False)
    cmds.selectType(meshUVShell=True)
    cmds.hilite(owners, replace=True)
    cmds.select(components, replace=True)


def _selection():
    selected = cmds.ls(selection=True, long=True, flatten=True) or []
    if not selected:
        raise ValueError('Select UVs, polygon edges, faces or a mesh.')
    uv_list = cmds.ls(cmds.polyListComponentConversion(selected, toUV=True),
                      flatten=True, long=True) or []
    explicit_uvs = set(cmds.ls([item for item in selected if '.map[' in item],
                                flatten=True, long=True) or [])
    if not uv_list:
        raise ValueError('The selection has no UVs in the current UV set.')
    meshes = {}
    owners = {}
    for uv in uv_list:
        owner, index = uv.rsplit('.map[', 1)
        if owner not in owners:
            sel = om.MSelectionList()
            sel.add(owner)
            path = sel.getDagPath(0)
            if path.node().hasFn(om.MFn.kTransform):
                path.extendToShape()
            key = path.fullPathName()
            if key not in meshes:
                meshes[key] = Mesh(path)
            owners[owner] = meshes[key]
        uv_id = int(index[:-1])
        owners[owner].selected.add(uv_id)
        if uv in explicit_uvs:
            owners[owner].explicit_selected.add(uv_id)
    return selected, list(meshes.values())


class Mesh:
    def __init__(self, path):
        self.path = path
        self.name = path.fullPathName()
        self.fn = om.MFnMesh(path)
        self.uv_set = self.fn.currentUVSetName()
        u, v = self.fn.getUVs(self.uv_set)
        self.points = dict(enumerate(zip(u, v)))
        _, self.shell_ids = self.fn.getUvShellsIds(self.uv_set)
        self.selected = set()
        self.explicit_selected = set()
        self.faces = []
        self.face_vertices = []
        self.edge_uvs = defaultdict(set)
        self.edge_faces = defaultdict(int)
        iterator = om.MItMeshPolygon(path)
        while not iterator.isDone():
            edges = list(iterator.getEdges())
            for edge in edges:
                self.edge_faces[edge] += 1
            try:
                ids = tuple(iterator.getUVIndex(i, self.uv_set)
                            for i in range(iterator.polygonVertexCount()))
            except RuntimeError:
                iterator.next()
                continue
            self.faces.append(ids)
            vertices = list(iterator.getVertices())
            self.face_vertices.append(tuple(vertices))
            # Match edges by their actual mesh endpoints, independent of API ordering.
            for edge in edges:
                a, b = self.fn.getEdgeVertices(edge)
                pair = tuple(sorted((ids[vertices.index(a)], ids[vertices.index(b)])))
                self.edge_uvs[edge].add(pair)
            iterator.next()

    def component(self, index):
        return '{}.map[{}]'.format(self.name, index)

    def components(self, ids):
        return [self.component(i) for i in sorted(ids)]

    def shells(self):
        selected_shells = {self.shell_ids[i] for i in self.selected}
        result = defaultdict(set)
        for i, shell in enumerate(self.shell_ids):
            if shell in selected_shells:
                result[shell].add(i)
        return [result[k] for k in sorted(result)]

    def all_shells(self):
        result = defaultdict(set)
        for i, shell in enumerate(self.shell_ids):
            result[shell].add(i)
        return [result[k] for k in sorted(result)]

    def pins(self, ids):
        components = self.components(ids)
        # Maya query mode does not accept uvSetName as an input flag.
        # Mesh always captures the current set; commands run synchronously.
        weights = cmds.polyPinUV(components, query=True, value=True) or []
        return {i for i, weight in zip(sorted(ids), weights) if weight > 0.0}

    def write(self, positions):
        components = self.components(positions)
        weights = cmds.polyPinUV(components, query=True, value=True) or []
        pinned = [(component, weight) for component, weight in zip(components, weights) if weight > 0]
        # polyEditUV in Maya 2025 honors pins. Temporarily release only the
        # explicitly edited UVs, then restore their original pin weights.
        if pinned:
            cmds.polyPinUV([p[0] for p in pinned], operation=1, uvSetName=self.uv_set)
        try:
            for i, (u, v) in positions.items():
                cmds.polyEditUV(self.component(i), relative=False,
                                uValue=u, vValue=v, uvSetName=self.uv_set)
        finally:
            for component, weight in pinned:
                cmds.polyPinUV(component, operation=0, value=weight, uvSetName=self.uv_set)


def _axis(axis):
    if axis not in ('H', 'V'):
        raise ValueError("Axis must be 'H' or 'V'.")
    return 0 if axis == 'H' else 1


def _chains(pairs):
    graph = defaultdict(set)
    for a, b in pairs:
        if a == b:
            raise ValueError('Degenerate UV edge. Repair the UV topology first.')
        graph[a].add(b)
        graph[b].add(a)
    remaining = set(graph)
    chains = []
    while remaining:
        seed = min(remaining)
        connected, pending = set(), [seed]
        while pending:
            point = pending.pop()
            if point not in connected:
                connected.add(point)
                pending.extend(graph[point] - connected)
        remaining -= connected
        ends = sorted(i for i in connected if len(graph[i]) == 1)
        if len(ends) != 2 or any(len(graph[i]) > 2 for i in connected):
            raise ValueError('Select open, non-branching edge chains. Closed loops cannot form a line.')
        chain = [ends[0]]
        previous = None
        while chain[-1] != ends[1]:
            following = next(i for i in graph[chain[-1]] if i != previous)
            previous = chain[-1]
            chain.append(following)
        chains.append(chain)
    return chains


def _auto_paths(pairs):
    """Order selected UV edges, including closed shell borders."""
    graph = defaultdict(set)
    for a, b in pairs:
        if a == b:
            continue
        graph[a].add(b)
        graph[b].add(a)
    if any(len(adjoining) > 2 for adjoining in graph.values()):
        raise ValueError('Auto H/V needs non-branching UV contours.')
    unused = {tuple(sorted(pair)) for pair in pairs}
    paths = []
    while unused:
        seed = min(min(unused))
        component = set()
        pending = [seed]
        while pending:
            point = pending.pop()
            if point not in component:
                component.add(point)
                pending.extend(graph[point] - component)
        ends = sorted(i for i in component if len(graph[i]) == 1)
        closed = not ends
        current = min(component) if closed else ends[0]
        previous = None
        path = [current]
        while True:
            candidates = sorted(i for i in graph[current]
                                if tuple(sorted((current, i))) in unused)
            if not candidates:
                break
            following = next((i for i in candidates if i != previous), candidates[0])
            unused.remove(tuple(sorted((current, following))))
            path.append(following)
            previous, current = current, following
            if closed and current == path[0]:
                break
        if len(path) >= 2:
            paths.append((path, closed))
    return paths


def _auto_groups(mesh, pairs, max_deviation=15.0):
    groups = []
    for path, closed in _auto_paths(pairs):
        labels = []
        lengths = []
        for a, b in zip(path, path[1:]):
            dx = abs(mesh.points[b][0] - mesh.points[a][0])
            dy = abs(mesh.points[b][1] - mesh.points[a][1])
            length = math.hypot(dx, dy)
            lengths.append(length)
            if length < 1e-10:
                labels.append(None)
                continue
            angle = math.degrees(math.atan2(dy, dx))
            if angle <= max_deviation:
                labels.append('H')
            elif 90.0 - angle <= max_deviation:
                labels.append('V')
            else:
                labels.append(None)
        typical_length = statistics.median(lengths)
        # A tiny perpendicular jog between parallel runs is an irregularity,
        # not a new H/V guide.
        filtered = labels[:]
        for index, label in enumerate(labels):
            if label is None or lengths[index] >= .2 * typical_length:
                continue
            before = labels[index - 1] if (index or closed) else None
            after = labels[(index + 1) % len(labels)] if (index + 1 < len(labels) or closed) else None
            if before and before == after and before != label:
                filtered[index] = None
        labels = filtered
        span = max(max(mesh.points[i][0] for i in path) -
                   min(mesh.points[i][0] for i in path),
                   max(mesh.points[i][1] for i in path) -
                   min(mesh.points[i][1] for i in path))
        # The tiny flat top of a notch can itself be perfectly horizontal.
        # Skip it when steep unmarked sides isolate it from the main border.
        run_filtered = labels[:]
        for start in range(len(labels)):
            if labels[start] is None or (start and labels[start - 1] == labels[start]):
                continue
            end = start + 1
            while end < len(labels) and labels[end] == labels[start]:
                end += 1
            before = labels[start - 1] if start else (labels[-1] if closed else None)
            after = labels[end] if end < len(labels) else (labels[0] if closed else None)
            if before is None and after is None and sum(lengths[start:end]) < .05 * span:
                run_filtered[start:end] = [None] * (end - start)
        labels = run_filtered
        if closed:
            changes = [i for i in range(len(labels)) if labels[i] != labels[i - 1]]
            pivot = changes[0] if changes else 0
            vertices = path[:-1]
            path = vertices[pivot:] + vertices[:pivot] + [vertices[pivot]]
            labels = labels[pivot:] + labels[:pivot]
        start = 0
        for index in range(1, len(labels) + 1):
            if index == len(labels) or labels[index] != labels[start]:
                if labels[start] is not None:
                    groups.append({'axis': labels[start], 'ids': path[start:index + 1],
                                   'mode': 'edge'})
                start = index
    return groups


def _border_pairs(mesh, shells):
    ids = set().union(*shells) if shells else set()
    counts = defaultdict(int)
    for face in mesh.faces:
        if face and face[0] in ids:
            for a, b in zip(face, face[1:] + face[:1]):
                counts[tuple(sorted((a, b)))] += 1
    return {pair for pair, count in counts.items() if count == 1}


def _auto_plan(mesh, pairs, max_deviation):
    if not pairs:
        raise ValueError('No UV contour edges found for Auto H/V.')
    groups = _auto_groups(mesh, pairs, max_deviation)
    candidate_edges = {tuple(sorted(pair)) for pair in pairs}
    old = []
    for group in _marks(mesh):
        edges = {tuple(sorted(pair)) for pair in _marked_pairs(mesh, group)}
        if not edges & candidate_edges:
            old.append(group)
    old = [prior for prior in old if not any(
        len(set(prior['ids']) & set(group['ids'])) > 1 for group in groups)]
    return old + groups, {i for group in groups for i in group['ids']}


def _apply_auto_plan(mesh, plan):
    groups, ids = plan
    _migrate_legacy_marks(mesh)
    _release_native_pins(mesh, ids)
    _save_marks(mesh, groups)


def auto_mark(max_deviation=15.0, whole_shells=False):
    """Assign H/V from UV contour direction without moving any UV."""
    if not 0.0 <= max_deviation <= 45.0:
        raise ValueError('Auto H/V angle must be between 0 and 45 degrees.')
    selection, meshes = _selection()
    edge_mode = not whole_shells and all('.e[' in item for item in selection)
    plans = []
    for mesh in meshes:
        if edge_mode:
            pairs = set()
            for item in selection:
                owner, index = item.rsplit('.e[', 1)
                sl = om.MSelectionList()
                sl.add(owner)
                path = sl.getDagPath(0)
                if path.node().hasFn(om.MFn.kTransform):
                    path.extendToShape()
                if path.fullPathName() == mesh.name:
                    pairs.update(mesh.edge_uvs[int(index[:-1])])
        else:
            pairs = set()
            for shell in mesh.shells():
                contour = _border_pairs(mesh, [shell])
                if not contour:
                    continue
                try:
                    _auto_paths(contour)
                except ValueError:
                    cmds.warning('Auto H/V: skipped a branching UV contour on ' + mesh.name)
                    continue
                pairs.update(contour)
        if not pairs:
            continue
        plans.append((mesh, _auto_plan(mesh, pairs, max_deviation)))
    with _undo('ShellShifter Auto H/V'):
        for mesh, plan in plans:
            _apply_auto_plan(mesh, plan)
    count = sum(len(plan[0]) for _, plan in plans)
    if not count:
        cmds.warning('ShellShifter Auto H/V: no contour edges match the angle limit.')
    return count


def _mark_data(mesh):
    plug = mesh.name + '.' + MARK_ATTR
    raw = cmds.getAttr(plug) if cmds.objExists(plug) else None
    return json.loads(raw) if raw else {}


def _topology(mesh):
    payload = [len(mesh.points), mesh.faces]
    return hashlib.sha256(json.dumps(payload).encode('utf-8')).hexdigest()


def _marks(mesh):
    data = _mark_data(mesh).get(mesh.uv_set, {})
    if data and data.get('topology') != _topology(mesh):
        # Ignore stale indices without mutating the scene from overlay reads.
        # Undoing a topology edit makes the previous marks valid again.
        return []
    return data.get('groups', [])


def _save_marks(mesh, groups):
    data = _mark_data(mesh)
    data[mesh.uv_set] = {'topology': _topology(mesh), 'groups': groups, 'format': 2}
    if not cmds.attributeQuery(MARK_ATTR, node=mesh.name, exists=True):
        cmds.addAttr(mesh.name, longName=MARK_ATTR, dataType='string')
    cmds.setAttr(mesh.name + '.' + MARK_ATTR, json.dumps(data), type='string')


def _marked_pairs(mesh, group):
    """Return drawable UV-edge pairs for either edge or manual UV marks."""
    ids = list(group['ids'])
    if group.get('mode') == 'edge':
        return list(zip(ids, ids[1:]))
    selected = set(ids)
    return sorted({tuple(sorted(pair))
                   for pairs in mesh.edge_uvs.values() for pair in pairs
                   if set(pair) <= selected})


def _migrate_legacy_marks(mesh):
    """Release native weights created by older H/V marks, once per UV set."""
    entry = _mark_data(mesh).get(mesh.uv_set, {})
    if not entry or entry.get('format', 1) >= 2:
        return
    if entry.get('topology') != _topology(mesh):
        # UV ids are no longer reliable. Clear Marks can still remove metadata.
        return
    groups = _marks(mesh)
    ids = {i for group in groups for i in group['ids']}
    if ids:
        _release_native_pins(mesh, ids)
    _save_marks(mesh, groups)


def _release_native_pins(mesh, ids):
    pinned = mesh.pins(ids)
    if pinned:
        # Maya can evaluate an older UV-history state when polyPinUV removes a
        # weight. Reapply the coordinates visible at the moment of removal, so
        # unpinning changes constraints only.
        positions = {i: mesh.points[i] for i in pinned}
        cmds.polyPinUV(mesh.components(pinned), operation=1, uvSetName=mesh.uv_set)
        Mesh(mesh.path).write(positions)


def mark(axis='H'):
    """Store custom directional constraints without native pins or UV movement."""
    _axis(axis)
    selection, meshes = _selection()
    if any('.e[' not in item and '.map[' not in item for item in selection):
        raise ValueError('Mark H/V requires only polygon edges or UVs.')
    if any('.e[' in s for s in selection) and any('.map[' in s for s in selection):
        raise ValueError('Use either edge selection or UV selection, not both.')
    edge_mode = all('.e[' in s for s in selection)
    plans = []
    for mesh in meshes:
        groups = []
        if edge_mode:
            pairs = set()
            for item in selection:
                owner, index = item.rsplit('.e[', 1)
                sl = om.MSelectionList()
                sl.add(owner)
                path = sl.getDagPath(0)
                if path.node().hasFn(om.MFn.kTransform):
                    path.extendToShape()
                if path.fullPathName() == mesh.name:
                    pairs.update(mesh.edge_uvs[int(index[:-1])])
            groups = [{'axis': axis, 'ids': chain, 'mode': 'edge'} for chain in _chains(pairs)]
        else:
            for shell in mesh.shells():
                ids = shell & mesh.selected
                if len(ids) < 2:
                    raise ValueError('Select at least two UVs per shell.')
                groups.append({'axis': axis, 'ids': sorted(ids), 'mode': 'uv'})
        old = _marks(mesh)
        for group in groups:
            # A new guide replaces overlapping guides; a shared corner is valid.
            old = [g for g in old if len(set(g['ids']) & set(group['ids'])) <= 1]
            old.append(group)
        plans.append((mesh, old, {i for g in groups for i in g['ids']}))
    with _undo('ShellShifter Mark ' + axis):
        for mesh, groups, new_ids in plans:
            _migrate_legacy_marks(mesh)
            _release_native_pins(mesh, new_ids)
            _save_marks(mesh, groups)


def align_and_pin(axis='H'):
    """Compatibility entry point: v1.1 marks only; Relax applies alignment."""
    mark(axis)


def _constraint_positions(mesh, groups, native_pins=()):
    ids = {i for g in groups for i in g['ids']}
    proposals = [defaultdict(list), defaultdict(list)]
    fixed = [defaultdict(list), defaultdict(list)]
    parents = [{i: i for i in ids} for _ in (0, 1)]
    def root(dim, i):
        while parents[dim][i] != i:
            parents[dim][i] = parents[dim][parents[dim][i]]
            i = parents[dim][i]
        return i
    for group in groups:
        along = _axis(group['axis'])
        across = 1 - along
        chain = list(group['ids'])
        if mesh.points[chain[-1]][along] < mesh.points[chain[0]][along]:
            chain.reverse()
        value = sum(mesh.points[i][across] for i in chain) / len(chain)
        for i in chain:
            parents[across][root(across, i)] = root(across, chain[0])
            fixed[across][i].append(value)
        if group['mode'] == 'edge':
            distances = [0.0]
            for a, b in zip(chain, chain[1:]):
                distances.append(distances[-1] + math.dist(mesh.points[a], mesh.points[b]))
            if distances[-1] < 1e-10:
                raise ValueError('Marked UV chain has zero length.')
            center = sum(mesh.points[i][along] for i in chain) / len(chain)
            values = [center + d - sum(distances) / len(distances) for d in distances]
        else:
            values = [mesh.points[i][along] for i in chain]
        for i, value in zip(chain, values):
            proposals[along][i].append(value)
    for i in ids & set(native_pins):
        for dim in (0, 1):
            fixed[dim][i].append(mesh.points[i][dim])
    result = {i: [0.0, 0.0] for i in ids}
    for dim in (0, 1):
        members = defaultdict(list)
        for i in ids:
            members[root(dim, i)].append(i)
        for members_ids in members.values():
            values = [v for i in members_ids for v in fixed[dim][i]]
            if not values:
                values = [v for i in members_ids for v in proposals[dim][i]]
            value = sum(values) / len(values)
            for i in members_ids:
                result[i][dim] = value
    for group in groups:
        along = _axis(group['axis'])
        if max(result[i][along] for i in group['ids']) - min(result[i][along] for i in group['ids']) < 1e-10:
            raise ValueError('Constraints would collapse a chain. Change its direction or remove conflicting marks.')
    for i in ids & set(native_pins):
        if math.dist(result[i], mesh.points[i]) > 1e-6:
            raise ValueError('A native pin conflicts with an H/V guide. Remove that native pin.')
    return result


def clear_marks():
    """Clear custom H/V constraints for selected meshes."""
    _, meshes = _selection()
    with _undo('ShellShifter Clear Marks'):
        for mesh in meshes:
            _migrate_legacy_marks(mesh)
            _save_marks(mesh, [])


def _rectangle(mesh, ids, axis):
    """Develop a rectangular quad lattice; reject poles, holes and UV overlaps."""
    faces = [face for face in mesh.faces if set(face) & ids]
    if not faces or any(len(f) != 4 or len(set(f)) != 4 for f in faces):
        raise ValueError('Straighten Shell requires a rectangular quad grid (no triangles or n-gons).')
    edges = defaultdict(list)
    for n, face in enumerate(faces):
        for a, b in zip(face, face[1:] + face[:1]):
            edges[tuple(sorted((a, b)))].append(n)
    if any(len(users) > 2 for users in edges.values()):
        raise ValueError('Non-manifold UV grid.')
    coords = dict(zip(faces[0], ((0, 0), (1, 0), (1, 1), (0, 1))))
    queue, seen = deque([0]), set()
    while queue:
        n = queue.popleft()
        if n in seen:
            continue
        face = faces[n]
        shared = next((j for j in range(4) if face[j] in coords and face[(j + 1) % 4] in coords), None)
        if shared is None:
            raise ValueError('Disconnected UV grid.')
        face = face[shared:] + face[:shared]
        a, b = coords[face[0]], coords[face[1]]
        dx, dy = b[0] - a[0], b[1] - a[1]
        targets = (a, b, (b[0] - dy, b[1] + dx), (a[0] - dy, a[1] + dx))
        for uv, target in zip(face, targets):
            if uv in coords and coords[uv] != target:
                raise ValueError('Shell has poles or a closed cycle. Cut it into a rectangular quad strip first.')
            coords[uv] = target
        seen.add(n)
        for a, b in zip(face, face[1:] + face[:1]):
            queue.extend(i for i in edges[tuple(sorted((a, b)))] if i not in seen)
    if set(coords) != ids or len(set(coords.values())) != len(coords):
        raise ValueError('Shell is not a single, non-overlapping quad grid.')
    xmin, xmax = min(p[0] for p in coords.values()), max(p[0] for p in coords.values())
    ymin, ymax = min(p[1] for p in coords.values()), max(p[1] for p in coords.values())
    if len(faces) != (xmax - xmin) * (ymax - ymin):
        raise ValueError('Shell has holes or missing grid cells. A complete rectangle is required.')
    lengths = (defaultdict(list), defaultdict(list))
    for a, b in edges:
        pa, pb = coords[a], coords[b]
        dim = 0 if pa[0] != pb[0] else 1
        lengths[dim][min(pa[dim], pb[dim])].append(math.dist(mesh.points[a], mesh.points[b]))
    ticks = []
    for dim, lo, hi in ((0, xmin, xmax), (1, ymin, ymax)):
        values = {lo: 0.0}
        for step in range(lo, hi):
            samples = lengths[dim][step]
            width = sum(samples) / len(samples)
            if width < 1e-10:
                raise ValueError('Shell contains a zero-width UV row.')
            values[step + 1] = values[step] + width
        ticks.append(values)
    result = {i: (ticks[0][p[0]], ticks[1][p[1]]) for i, p in coords.items()}
    # Preserve mirrored orientation from the seed quad.
    f = faces[0]
    signed_area = sum(mesh.points[a][0] * mesh.points[b][1] - mesh.points[b][0] * mesh.points[a][1]
                      for a, b in zip(f, f[1:] + f[:1]))
    if signed_area < 0:
        result = {i: (-p[0], p[1]) for i, p in result.items()}
    width, height = ticks[0][xmax], ticks[1][ymax]
    if (axis == 'H' and height > width) or (axis == 'V' and width > height):
        result = {i: (-p[1], p[0]) for i, p in result.items()}
    old_center = [sum(mesh.points[i][k] for i in ids) / len(ids) for k in (0, 1)]
    new_center = [sum(result[i][k] for i in ids) / len(ids) for k in (0, 1)]
    result = {i: tuple(p[k] + old_center[k] - new_center[k] for k in (0, 1)) for i, p in result.items()}
    border = {i for edge, users in edges.items() if len(users) == 1 for i in edge}
    return result, border


def straighten_shells(axis='H', pin_border=True):
    """Rectangularize whole selected quad shells, long side along H or V.

    Custom guides on reshaped shells are replaced; native fixed pins are respected.
    UV scale follows average original edge length for each lattice interval.
    """
    _axis(axis)
    _, meshes = _selection()
    plans = []
    for mesh in meshes:
        for shell in mesh.shells():
            if mesh.pins(shell):
                raise ValueError('Unpin the whole shell before Straighten Shell; it changes all UV coordinates.')
            positions, border = _rectangle(mesh, shell, axis)
            plans.append((mesh, positions, border))
    with _undo('ShellShifter Shells'):
        for mesh, positions, border in plans:
            _save_marks(mesh, [g for g in _marks(mesh)
                               if not set(g['ids']) & set(positions)])
            mesh.write(positions)
            if pin_border:
                fresh = Mesh(mesh.path)
                pairs = _border_pairs(fresh, [set(positions)])
                _apply_auto_plan(fresh, _auto_plan(fresh, pairs, 5.0))


def quick_straighten_quads(axis='H', max_deviation=15.0):
    """Straighten and mark selected shells; unfold the non-grid ribbons."""
    _axis(axis)
    if not 0.0 <= max_deviation <= 45.0:
        raise ValueError('Auto H/V angle must be between 0 and 45 degrees.')
    selection, meshes = _selection()
    plans = []
    skipped = 0
    for mesh in meshes:
        rectangles = []
        ribbon_analyzer = None
        for shell in mesh.shells():
            try:
                positions, _ = _rectangle(mesh, shell, axis)
            except ValueError:
                from uv_straight_ribbon import RibbonAnalyzer
                if ribbon_analyzer is None:
                    ribbon_analyzer = RibbonAnalyzer(mesh)
                try:
                    positions, guides = ribbon_analyzer.straighten(shell, axis)
                except ValueError:
                    skipped += 1
                    continue
            else:
                guides = None
            rectangles.append((shell, positions, guides))
        if rectangles:
            plans.append((mesh, rectangles))
    if not plans:
        raise ValueError('No selected quad grid or suitable UV ribbon found.')
    with _undo('ShellShifter Quick Quads'):
        try:
            for mesh, rectangles in plans:
                affected = set().union(*(shell for shell, _, _ in rectangles))
                kept = [group for group in _marks(mesh)
                        if not set(group['ids']) & affected]
                _save_marks(mesh, kept)
                cmds.polyPinUV(mesh.components(affected), operation=1,
                               uvSetName=mesh.uv_set)
                for _, positions, _ in rectangles:
                    mesh.write(positions)
                fresh = Mesh(mesh.path)
                pairs = _border_pairs(fresh, [shell for shell, _, guides in rectangles
                                             if guides is None])
                if pairs:
                    _apply_auto_plan(fresh, _auto_plan(fresh, pairs, max_deviation))
                ribbon_groups = [group for _, _, guides in rectangles if guides
                                 for group in guides]
                if ribbon_groups:
                    _apply_auto_plan(fresh, (_marks(fresh)+ribbon_groups,
                        {i for group in ribbon_groups for i in group['ids']}))
                    from uv_straight_ribbon import unfold_ribbons
                    unfold_ribbons(fresh, [(shell, guides) for shell, _, guides in rectangles
                                           if guides])
        finally:
            cmds.select(selection, replace=True)
    if skipped:
        cmds.warning('ShellShifter: skipped {} shell(s) without a reliable quad grid or ribbon contour.'.format(skipped))
    return sum(len(rectangles) for _, rectangles in plans)


def _selected_edge_ids(selection, mesh):
    result = set()
    for item in selection:
        if '.e[' not in item:
            continue
        owner, index = item.rsplit('.e[', 1)
        selected = om.MSelectionList()
        selected.add(owner)
        path = selected.getDagPath(0)
        if path.node().hasFn(om.MFn.kTransform):
            path.extendToShape()
        if path.fullPathName() == mesh.name:
            result.add(int(index[:-1]))
    return result


def _selected_meshes():
    selection = cmds.ls(selection=True, long=True, flatten=True) or []
    meshes = {}
    for item in selection:
        selected = om.MSelectionList()
        selected.add(item)
        path = selected.getDagPath(0)
        if path.node().hasFn(om.MFn.kTransform):
            path.extendToShape()
        if path.node().hasFn(om.MFn.kMesh):
            meshes[path.fullPathName()] = path
    if not meshes:
        raise ValueError('Select a polygon mesh or its edges.')
    return selection, [Mesh(path) for path in meshes.values()]


def _camera_project(mesh):
    """Create a fresh UV map as in the supplied camera-projection script."""
    faces = '{}.f[0:{}]'.format(mesh.name, mesh.fn.numPolygons - 1)
    if cmds.about(batch=True):
        cmds.polyPlanarProjection(faces, mapDirection='b',
                                  constructionHistory=False)
        return
    try:
        cmds.polyPlanarProjection(faces, mapDirection='c',
                                  constructionHistory=False)
    except RuntimeError:
        # Batch Maya has no active camera panel; best plane keeps tests usable.
        cmds.polyPlanarProjection(faces, mapDirection='b',
                                  constructionHistory=False)


def _unfold_new_shells(mesh):
    """Initial Maya unfold for the automatic unwrap commands."""
    for shell in mesh.all_shells():
        components = mesh.components(shell)
        cmds.unfold(components, iterations=100, pinSelected=False,
                    pinUvBorder=False, applyToShell=True, useScale=False)


def _shell_orientation(mesh, shell, reference):
    """Rigid orientation: longest straight border, or camera-reference fit.

    Join subdivided sides before measuring them. Short facets of round borders
    must not determine orientation. No UV lengths or angles are changed here.
    """
    ids = sorted(shell)
    points = mesh.points
    center = tuple(sum(points[i][axis] for i in ids) / len(ids) for axis in (0, 1))
    lo = [min(points[i][axis] for i in ids) for axis in (0, 1)]
    hi = [max(points[i][axis] for i in ids) for axis in (0, 1)]
    box_center = [(a + b) * .5 for a, b in zip(lo, hi)]
    diameter = 2 * max(math.hypot(points[i][0] - box_center[0],
                                  points[i][1] - box_center[1]) for i in ids)
    best = (0., 0., 0.)
    try:
        paths = _auto_paths(_border_pairs(mesh, [shell]))
    except ValueError:
        paths = []
    cosine = math.cos(math.radians(10.))
    for path, closed in paths:
        vectors = [(points[b][0] - points[a][0], points[b][1] - points[a][1])
                   for a, b in zip(path, path[1:])]
        # Compare to the starting direction, not only neighbouring edges:
        # a finely tessellated circle must never become one straight side.
        for start, (dx, dy) in enumerate(vectors):
            length = math.hypot(dx, dy)
            if length < 1e-12:
                continue
            x, y = dx, dy
            limit = len(vectors) if closed else len(vectors) - start
            for offset in range(1, limit):
                vx, vy = vectors[(start + offset) % len(vectors)]
                size = math.hypot(vx, vy)
                if size < 1e-12:
                    continue
                if (dx * vx + dy * vy) / (length * size) < cosine:
                    break
                x += vx
                y += vy
            span = math.hypot(x, y)
            if span > best[0] + max(1e-10, diameter * 1e-6):
                best = (span, x, y)
    if diameter > 1e-12 and best[0] >= .55 * diameter:
        # Long side horizontal; choose the smaller rotation, avoiding a flip.
        angle = -math.atan2(best[2], best[1])
        angle = (angle + math.pi / 2) % math.pi - math.pi / 2
        mode = 'longest_side'
    else:
        # Least-squares rotation to the original camera projection. Centering
        # removes translation and the fit cannot mirror or deform the shell.
        common = [i for i in ids if i in reference]
        angle = 0.
        if common:
            source_center = [sum(points[i][a] for i in common) / len(common) for a in (0, 1)]
            target_center = [sum(reference[i][a] for i in common) / len(common) for a in (0, 1)]
            dot = cross = 0.
            for i in common:
                x, y = [points[i][a] - source_center[a] for a in (0, 1)]
                u, v = [reference[i][a] - target_center[a] for a in (0, 1)]
                dot += x * u + y * v
                cross += x * v - y * u
            if math.hypot(dot, cross) > 1e-12:
                angle = math.atan2(cross, dot)
        mode = 'viewport'
    return angle, center, mode


def _orient_shell_positions(mesh, shell, reference):
    angle, center, mode = _shell_orientation(mesh, shell, reference)
    c, s = math.cos(angle), math.sin(angle)
    result = {}
    for i in shell:
        x, y = mesh.points[i][0] - center[0], mesh.points[i][1] - center[1]
        result[i] = (center[0] + c * x - s * y, center[1] + s * x + c * y)
    return result, mode


def _nearest_orthogonal_angle(vectors):
    """Fit an H/V frame, with a signed rotation of at most 45 degrees.

    Fourfold angles treat horizontal and vertical sides as the same frame.
    Length weights keep subdivided straight sides from gaining extra influence.
    """
    cosine = sine = weight = 0.
    for dx, dy in vectors:
        length = math.hypot(dx, dy)
        if not length or not math.isfinite(length):
            continue
        angle = 4. * math.atan2(dy, dx)
        cosine += length * math.cos(angle)
        sine += length * math.sin(angle)
        weight += length
    # UVs are stored as floats. Native unfolding leaves round caps with tiny
    # numerical asymmetry; this must not become a new orientation after Undo.
    if not weight or math.hypot(cosine, sine) <= weight * 1.e-5:
        return None  # Round/symmetric contours do not define a unique H/V frame.
    return -math.atan2(sine, cosine) * .25


def _orientation_point_hull(points):
    """Order selected corners without choosing a diagonal between them."""
    points = sorted(set(points))
    if len(points) <= 2:
        return points
    def cross(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    lower, upper = [], []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0.:
            lower.pop()
        lower.append(point)
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0.:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _orient_shell_by_selected(mesh, shell, selected, border_pairs=None):
    """Rigid nearest-axis orientation from selected points and contour sides."""
    ids = sorted(set(shell) & set(selected))
    points = mesh.points
    vectors = []
    if len(ids) == 2:
        a, b = (points[i] for i in ids)
        vectors = [(b[0]-a[0], b[1]-a[1])]
    elif len(ids) > 2:
        # Prefer selected real sides. Interior triangulation diagonals must
        # never determine the orientation of four selected shell corners.
        pairs = _border_pairs(mesh, [shell]) if border_pairs is None else border_pairs
        selected_ids = set(ids)
        for a, b in sorted(pairs):
            if a in selected_ids and b in selected_ids:
                vectors.append((points[b][0]-points[a][0], points[b][1]-points[a][1]))
        if not vectors:
            # Selected corners may have unselected subdividing UVs between
            # them, or may be interior UVs. Use their outline, not a farthest pair.
            hull = _orientation_point_hull([points[i] for i in ids])
            vectors = [(b[0]-a[0], b[1]-a[1]) for a, b in zip(hull, hull[1:]+hull[:1])]
    angle = _nearest_orthogonal_angle(vectors)
    if angle is None:
        # Symmetric contours need the longest-side fallback before snapping
        # to the closest H or V axis.
        angle, center, mode = _shell_orientation(mesh, shell, points)
        if mode != 'longest_side':
            return {}, 'unchanged'
        angle = (angle + math.pi*.25) % (math.pi*.5) - math.pi*.25
    else:
        center = tuple(math.fsum(points[i][axis] for i in shell)/len(shell) for axis in (0, 1))
    if abs(angle) <= 1.e-10:
        return {}, 'unchanged'
    c, s = math.cos(angle), math.sin(angle)
    result = {}
    for i in shell:
        x, y = points[i][0]-center[0], points[i][1]-center[1]
        result[i] = (center[0]+c*x-s*y, center[1]+s*x+c*y)
    return result, 'selected_components'


def stack_selected_shells():
    """Stack complete selected copies onto their least distorted UV layout."""
    from uv_straight_stack import stack_selected_shells as stack
    return stack()


def orient_selected_shells():
    """Rotate selected shells to their closest H/V frame without deformation."""
    _, meshes = _selection()
    plans = []
    for mesh in meshes:
        positions = {}
        for shell in mesh.shells():
            rotated, mode = _orient_shell_by_selected(mesh, shell, mesh.selected)
            if mode in ('longest_side', 'selected_components'):
                positions.update(rotated)
        groups = []
        for original in _marks(mesh):
            group = dict(original)
            if set(group['ids']) <= set(positions):
                points = [positions[i] for i in group['ids']]
                span = [max(p[d] for p in points)-min(p[d] for p in points) for d in (0, 1)]
                group['axis'] = 'H' if span[0] >= span[1] else 'V'
            groups.append(group)
        if positions:
            plans.append((mesh, positions, groups))
    if not plans:
        return 0
    with _undo('ShellShifter Orient Shells'):
        for mesh, positions, groups in plans:
            mesh.write(positions)
            if groups:
                _save_marks(mesh, groups)
    import uv_straight_native as native
    if native._manager is not None:
        native._manager.changes.invalidate()
        native._manager.refresh()
    return len(plans)


def unwrap_cylinder(max_deviation=15.0):
    """Explicit seams + hard edges; rectangularize suitable quad shells."""
    return unwrap_from_edges(max_deviation, straighten_quads=True, use_u3d=False)


def _ensure_unfold3d():
    if not cmds.pluginInfo('Unfold3D', query=True, loaded=True):
        cmds.loadPlugin('Unfold3D', quiet=True)


def _seed_collapsed_shells(mesh):
    faces_by_shell, areas = defaultdict(list), defaultdict(float)
    for index, face in enumerate(mesh.faces):
        owner = mesh.shell_ids[face[0]]
        faces_by_shell[owner].append(index)
        areas[owner] += abs(sum(mesh.points[a][0]*mesh.points[b][1]-mesh.points[b][0]*mesh.points[a][1]
                               for a,b in zip(face,face[1:]+face[:1]))) * .5
    plans = [faces for owner, faces in faces_by_shell.items() if areas[owner] < 1e-12]
    for faces in plans:
        normal = mesh.fn.getPolygonNormal(faces[0], om.MSpace.kObject)
        direction = 'xyz'[max(range(3), key=lambda axis: abs(normal[axis]))]
        cmds.polyPlanarProjection(['{}.f[{}]'.format(mesh.name,i) for i in faces],
                                  mapDirection=direction, constructionHistory=False)
    return len(plans)


def _packing_gap(meshes, limit):
    """Measure nearby border segments using a UV spatial grid."""
    grid = defaultdict(list)
    segments = []
    best = limit
    def point_distance(p, a, b):
        dx, dy = b[0]-a[0], b[1]-a[1]
        length = dx*dx+dy*dy
        t = max(0., min(1., ((p[0]-a[0])*dx+(p[1]-a[1])*dy)/length)) if length else 0.
        return math.hypot(p[0]-a[0]-t*dx, p[1]-a[1]-t*dy)
    def cross(a,b,c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def cells(box, margin=0.):
        x0,x1,y0,y1=box
        for x in range(math.floor((x0-margin)/limit),math.floor((x1+margin)/limit)+1):
            for y in range(math.floor((y0-margin)/limit),math.floor((y1+margin)/limit)+1):
                yield x,y
    for mi, mesh in enumerate(meshes):
        for si, shell in enumerate(mesh.all_shells()):
            owner=(mi,si)
            for ia,ib in _border_pairs(mesh,[shell]):
                a,b=mesh.points[ia],mesh.points[ib]
                box=(min(a[0],b[0]),max(a[0],b[0]),min(a[1],b[1]),max(a[1],b[1]))
                candidates={i for cell in cells(box,limit) for i in grid[cell]}
                for i in candidates:
                    other,c,d,bb=segments[i]
                    if other==owner:
                        continue
                    dx=max(0,box[0]-bb[1],bb[0]-box[1]); dy=max(0,box[2]-bb[3],bb[2]-box[3])
                    if math.hypot(dx,dy)>=best:
                        continue
                    distance=min(point_distance(a,c,d),point_distance(b,c,d),point_distance(c,a,b),point_distance(d,a,b))
                    if cross(a,b,c)*cross(a,b,d)<0 and cross(c,d,a)*cross(c,d,b)<0:
                        distance=0.
                    best=min(best,distance)
                index=len(segments); segments.append((owner,a,b,box))
                for cell in cells(box):
                    grid[cell].append(index)
    return best


def _pack_unwrapped(meshes, resolution, spacing_px):
    # Automatic layout is a bounded final step, without the interactive
    # packing dialog. The separate Pack UVs action keeps its search window.
    from uv_straight_pack import pack
    return pack(meshes, resolution, spacing_px, method='fast', seconds=30.)


def pack_selected_uvs(resolution=2048, spacing_px=16, margin_px=16,
                      normalize_density=True, rotate=True, method='compact',
                      around_others=True, seconds=30., precision=1024,
                      interactive=False, keep_stacks=False, gpu=False, target_size=(1.,1.)):
    """Pack existing complete selected shells without projection or unfolding."""
    from uv_straight_pack import pack, prepare, apply
    selection, meshes = _selection()
    if interactive:
        from uv_straight_pack_progress import run
        def commit(prepared, result, callback):
            with _undo('ShellShifter Pack UVs'):
                try:
                    return apply(prepared,result,callback)
                finally:
                    cmds.select(selection,replace=True)
        return run(lambda: prepare(meshes,resolution,spacing_px,margin_px,
                                  normalize_density,True,rotate,method,around_others,
                                  seconds,precision,keep_stacks,gpu,target_size),commit)
    with _undo('ShellShifter Pack UVs'):
        try:
            return pack(meshes, resolution, spacing_px, margin_px,
                        normalize_density, selected_only=True, rotate=rotate,
                        method=method, around_others=around_others,
                        seconds=seconds, precision=precision, keep_stacks=keep_stacks,gpu=gpu,target_size=target_size)
        finally:
            cmds.select(selection, replace=True)

def _scale_selected_shells_v(factor, label):
    """Scale complete selected shells around V=0, with one bulk Undo."""
    from uv_straight_pack_commit import commit
    _, meshes = _selection()
    entries = {}
    for mesh in meshes:
        handle = om.MObjectHandle(mesh.fn.object())
        key = handle.hashCode()
        if key not in entries:
            u, v = mesh.fn.getUVs(mesh.uv_set)
            entries[key] = dict(handle=handle, uv_set=mesh.uv_set,
                                before=[list(u), list(v)], after=[list(u), list(v)],
                                ids=set(), shell_ids=set())
        entry = entries[key]
        for shell in mesh.shells():
            entry['ids'].update(shell)
            entry['shell_ids'].add(mesh.shell_ids[min(shell)])
    plans = []
    for entry in entries.values():
        for index in entry['ids']:
            v = entry['before'][1][index]
            if not math.isfinite(v) or not math.isfinite(v * factor):
                raise ValueError('The selected shells contain invalid UV coordinates.')
            entry['after'][1][index] = v * factor
        if entry['before'][1] == entry['after'][1]:
            continue
        entry['shells'] = len(entry.pop('shell_ids'))
        entry['ids'] = sorted(entry['ids'])
        plans.append(entry)
    if plans:
        with _undo('ShellShifter ' + label):
            commit(plans)
    return dict(shells=sum(entry['shells'] for entry in plans),
                uvs=sum(len(entry['ids']) for entry in plans), v_scale=factor)


def rectangle_to_square():
    """Stretch bottom-half UVs to a full tile: U unchanged, V multiplied by 2."""
    return _scale_selected_shells_v(2.0, 'To Square')


def square_to_rectangle():
    """Compress full-tile UVs to the bottom half: U unchanged, V multiplied by 0.5."""
    return _scale_selected_shells_v(0.5, 'To Rectangle')


def _disk_shell(mesh, shell):
    faces = [face for face in mesh.faces if set(face) <= shell]
    edges = {tuple(sorted((a,b))) for face in faces for a,b in zip(face,face[1:]+face[:1])}
    return len(shell)-len(edges)+len(faces) == 1


def unwrap_from_edges(max_deviation=15.0, straighten_quads=False,
                      resolution=2048, spacing_px=40, use_u3d=True):
    """Cut exactly selected 3D edges plus hard edges, then unwrap the mesh."""
    if not 0.0 <= max_deviation <= 45.0:
        raise ValueError('Auto H/V angle must be between 0 and 45 degrees.')
    if int(resolution) != resolution or not 256 <= resolution <= 8192:
        raise ValueError('Texture resolution must be an integer between 256 and 8192.')
    resolution = int(resolution)
    if not math.isfinite(spacing_px) or not 0 <= spacing_px < resolution / 4:
        raise ValueError('Shell spacing must be between 0 and one quarter of the texture size.')
    selection, meshes = _selected_meshes()
    plans = []
    for mesh in meshes:
        chosen = _selected_edge_ids(selection, mesh)
        if any(count > 2 for count in mesh.edge_faces.values()):
            raise ValueError('Non-manifold edges must be repaired before automatic unwrap.')
        hard = {edge for edge in range(mesh.fn.numEdges)
                if mesh.edge_faces[edge] == 2 and not mesh.fn.isEdgeSmooth(edge)}
        cuts = {edge for edge in chosen | hard if mesh.edge_faces[edge] == 2}
        if not cuts and not any(count == 1 for count in mesh.edge_faces.values()):
            raise ValueError('Closed mesh has no hard-edge seams. Select edges to cut.')
        plans.append((mesh, cuts))
    if use_u3d:
        _ensure_unfold3d()
    oriented = 0
    rectangular = 0
    rectangular_shells = defaultdict(list)
    label = 'ShellShifter Unwrap Cylinder' if straighten_quads else 'ShellShifter Unwrap From Edges'
    with _undo(label):
        try:
            for mesh, cuts in plans:
                _save_marks(mesh, [])
                if mesh.points:
                    cmds.polyPinUV(mesh.components(mesh.points), operation=1,
                                   uvSetName=mesh.uv_set)
                _camera_project(mesh)
                if cuts:
                    cmds.polyMapCut(['{}.e[{}]'.format(mesh.name, i)
                                     for i in sorted(cuts)],
                                    constructionHistory=False)
                fresh = Mesh(mesh.path)
                if not fresh.points:
                    raise ValueError('Camera projection did not create UVs on {}.'.format(mesh.name))
                _save_marks(fresh, [])
                camera_reference = dict(fresh.points)
                if use_u3d:
                    from uv_straight_unwrap import unfold as unfold_unpinned
                    unfold_unpinned(fresh)
                else:
                    _unfold_new_shells(fresh)
                    fresh = Mesh(mesh.path)
                    cmds.unfold(fresh.components(fresh.points), iterations=100,
                                pinSelected=False, pinUvBorder=False,
                                applyToShell=True, useScale=False)
                fresh = Mesh(mesh.path)
                positions = {}
                all_shells = fresh.all_shells()
                orientation_borders = defaultdict(set)
                if use_u3d:
                    for a, b in _border_pairs(fresh, all_shells):
                        orientation_borders[fresh.shell_ids[a]].add((a, b))
                for shell in all_shells:
                    if straighten_quads:
                        try:
                            positions, _ = _rectangle(fresh, shell, 'H')
                        except ValueError:
                            pass  # Caps and irregular shells keep their shape.
                        else:
                            fresh.write(positions)
                            rectangular_shells[mesh.path.fullPathName()].append(set(shell))
                            rectangular += 1
                            continue
                    if use_u3d:
                        # Use the same nearest-axis, rigid rotation as Orient
                        # Shells; all shell boundary points determine its frame.
                        rotated, _ = _orient_shell_by_selected(
                            fresh, shell, shell, orientation_borders[fresh.shell_ids[min(shell)]])
                        positions.update(rotated)
                    else:
                        angle, center, _ = _shell_orientation(fresh, shell, camera_reference)
                        if abs(angle) > 1e-10:
                            cmds.polyMoveUV(fresh.components(shell),
                                           rotationAngle='{}rad'.format(angle),
                                           pivotU=center[0], pivotV=center[1],
                                           constructionHistory=False)
                    oriented += 1
                if positions:
                    from uv_straight_unwrap import _write
                    from uv_straight_pack_commit import commit
                    _write(fresh, positions, commit)
                if not use_u3d:
                    cmds.polyMultiLayoutUV(mesh.name, layout=2, percentageSpace=.02,
                                           rotateForBestFit=0, prescale=0, scale=1,
                                           uvSetName=mesh.uv_set)
            if use_u3d:
                _pack_unwrapped([Mesh(mesh.path) for mesh, _ in plans], resolution, spacing_px)
            if straighten_quads:
                for mesh, _ in plans:
                    fresh = Mesh(mesh.path)
                    pairs = _border_pairs(fresh, fresh.all_shells())
                    _apply_auto_plan(fresh, _auto_plan(fresh, pairs, max_deviation))
                # Relax the rectangular cylinder sides after their H/V guides
                # exist.  This redistributes the grid to reduce distortion while
                # the guides keep every row and column straight.  Caps and other
                # irregular shells deliberately keep the preceding Maya unfold.
                tasks = []
                for mesh, _ in plans:
                    fresh = Mesh(mesh.path)
                    groups = _marks(fresh)
                    for shell in rectangular_shells[mesh.path.fullPathName()]:
                        relevant = [g for g in groups if set(g['ids']) & shell]
                        pins = fresh.pins(shell)
                        tasks.append((fresh.path, set(shell), relevant, pins,
                                      {i: fresh.points[i] for i in pins},
                                      {i: fresh.points[i] for i in shell}))
                _solve_tasks(tasks, 350, 0.005, True)
        finally:
            cmds.select(selection, replace=True)
    return {'meshes': len(plans), 'oriented_shells': oriented, 'rectangular_shells': rectangular}


def _trim_mark(mesh, group, selected, selected_edges=None):
    """Split a guide at removed segments without bridging across the gap."""
    pairs = _marked_pairs(mesh, group)
    if selected_edges is None:
        remaining = [(a, b) for a, b in pairs if a not in selected and b not in selected]
    else:
        remaining = [(a, b) for a, b in pairs
                     if tuple(sorted((a, b))) not in selected_edges]
    if remaining == pairs:
        if pairs or selected_edges is not None or not set(group['ids']) & selected:
            return [group]
        # Manual alignment can also contain disconnected UV points.
        ids = [i for i in group['ids'] if i not in selected]
        return [dict(group, ids=ids)] if len(ids) >= 2 else []
    if group.get('mode') == 'edge':
        return [dict(group, ids=path) for path, _ in _auto_paths(remaining)]
    graph = defaultdict(set)
    for a, b in remaining:
        graph[a].add(b)
        graph[b].add(a)
    unseen = set(graph)
    result = []
    while unseen:
        component, pending = set(), [min(unseen)]
        while pending:
            point = pending.pop()
            if point not in component:
                component.add(point)
                pending.extend(graph[point] - component)
        unseen -= component
        result.append(dict(group, ids=[i for i in group['ids'] if i in component]))
    return result


def unpin_uv_segments(mesh, pairs, axis):
    """Remove drawn UV-side segments as one undoable operation."""
    _axis(axis)
    selected_edges = {tuple(sorted(pair)) for pair in pairs}
    if not selected_edges:
        return False
    groups = _marks(mesh)
    kept = []
    for group in groups:
        if group['axis'] == axis:
            kept.extend(_trim_mark(mesh, group, set(), selected_edges))
        else:
            kept.append(group)
    if kept == groups:
        return False
    released = {i for group in groups for i in group['ids']}
    released -= {i for group in kept for i in group['ids']}
    with _undo('ShellShifter Remove Pin Segment'):
        _migrate_legacy_marks(mesh)
        _save_marks(mesh, kept)
        if released:
            _release_native_pins(mesh, released)
    return True


def unpin_uv_segment(mesh, pair, axis):
    """Remove exactly one drawn UV-side segment and only its chosen axis."""
    return unpin_uv_segments(mesh, [pair], axis)


def pin_uv_segments(mesh, pairs, axis=None):
    """Add clicked UV edges to an H/V guide as one undoable operation."""
    pairs = {tuple(sorted(pair)) for pair in pairs}
    if not pairs:
        return False
    available = {tuple(sorted(edge_pair))
                 for edge_pairs in mesh.edge_uvs.values() for edge_pair in edge_pairs}
    if not pairs <= available:
        raise ValueError('The clicked UV segment is no longer available.')
    if axis is None:
        if len(pairs) != 1:
            raise ValueError('Choose H or V when adding several UV segments.')
        a, b = (mesh.points[index] for index in next(iter(pairs)))
        axis = 'H' if abs(b[0]-a[0]) >= abs(b[1]-a[1]) else 'V'
    _axis(axis)
    groups = _marks(mesh)
    marked = {tuple(sorted(edge)) for group in groups for edge in _marked_pairs(mesh, group)}
    pairs -= marked
    if not pairs:
        return False
    added = set(pairs)
    new_groups = [{'axis': axis, 'ids': chain, 'mode': 'edge'}
                  for chain in _chains(pairs)]
    kept = []
    for group in groups:
        if group['axis'] == axis and group.get('mode') == 'edge':
            combined = set(pairs) | {tuple(sorted(edge))
                                     for edge in _marked_pairs(mesh, group)}
            try:
                chains = _chains(combined)
            except ValueError:
                # Existing independent guides can meet at a junction. Keep
                # them separate instead of refusing the new clicked segment.
                kept.append(group)
            else:
                pairs = combined
                new_groups = [{'axis': axis, 'ids': chain, 'mode': 'edge'}
                              for chain in chains]
        else:
            kept.append(group)
    kept.extend(new_groups)
    with _undo('ShellShifter Add Pin Segments'):
        _migrate_legacy_marks(mesh)
        _release_native_pins(mesh, {index for pair in added for index in pair})
        _save_marks(mesh, kept)
    return True


def pin_uv_segment(mesh, pair, axis=None):
    """Add one clicked UV edge to an H/V guide without moving its UVs."""
    return pin_uv_segments(mesh, [pair], axis)


def unpin(whole_shell=False):
    """Remove complete guides touched by the selected UVs/components."""
    _, meshes = _selection()
    plans = []
    for mesh in meshes:
        ids = set().union(*mesh.shells()) if whole_shell else set(mesh.selected)
        groups = _marks(mesh)
        removed = [group for group in groups if set(group['ids']) & ids]
        kept = [group for group in groups if not set(group['ids']) & ids]
        ids.update(i for group in removed for i in group['ids'])
        # Keep shared corners pinned if a remaining constraint still owns them.
        ids -= {i for g in kept for i in g['ids']}
        plans.append((mesh, ids, kept))
    with _undo('ShellShifter Unpin'):
        for mesh, ids, kept in plans:
            _migrate_legacy_marks(mesh)
            _save_marks(mesh, kept)
            if ids:
                _release_native_pins(mesh, ids)


def _guided_start(mesh, shell, groups, aligned, native_pins):
    """Carry interior UVs with the guide's local frame before length fitting.

    Snapping only the guide can turn a bent strip inside out before the solver
    even starts. Transport both tangential and normal offsets with each edge.
    """
    segments = []
    for group in groups:
        if group['mode'] != 'edge':
            continue
        for a, b in zip(group['ids'], group['ids'][1:]):
            p, q = mesh.points[a], mesh.points[b]
            u, v = aligned[a], aligned[b]
            dx, dy = q[0] - p[0], q[1] - p[1]
            length2 = dx * dx + dy * dy
            if length2 > 1e-16:
                segments.append((p, u, dx, dy, length2, v[0]-u[0], v[1]-u[1]))
    result = dict(mesh.points)
    if segments:
        for i in shell - set(aligned) - set(native_pins):
            x, y = mesh.points[i]
            candidates = []
            for p, u, dx, dy, length2, tx, ty in segments:
                px, py = x-p[0], y-p[1]
                along = (px*dx + py*dy) / length2
                across = (dx*py - dy*px) / length2
                t = min(1., max(0., along))
                distance2 = (px-t*dx)**2 + (py-t*dy)**2
                candidates.append((distance2,
                                   (u[0]+along*tx-across*ty, u[1]+along*ty+across*tx)))
            # Blend the two nearest local frames to avoid seams at guide vertices.
            nearest = sorted(candidates, key=lambda value: value[0])[:2]
            weights = [1. / max(d, 1e-16)**2 for d, _ in nearest]
            result[i] = tuple(sum(w*p[k] for w, (_, p) in zip(weights, nearest))/sum(weights)
                              for k in (0, 1))
    result.update(aligned)
    for i in native_pins:
        result[i] = mesh.points[i]
    return result


def _global_length_relax(positions, neighbors, locked, stiff_edges, anchors,
                         anchor_weight, orientation, passes=40, groups=(), native_pins=()):
    """Local/global length fitting with sparse matrix-free conjugate gradients.

    Global solves propagate corrections along long strips in one solve instead
    of only one neighbour per iteration. Guides share a transverse variable;
    native pins fix their variables. Backtracking rejects energy increases
    and new triangle inversions; this is not a global overlap test.
    """
    import numpy as np
    ids = sorted(positions)
    index = {uv: n for n, uv in enumerate(ids)}
    edges = [(a, b, length) for a in ids for b, length in neighbors[a].items()
             if a < b]
    if not edges:
        return positions
    a = np.array([index[e[0]] for e in edges], dtype=int)
    b = np.array([index[e[1]] for e in edges], dtype=int)
    lengths = np.array([e[2] for e in edges])
    # Fit relative strain: a short edge at an elbow must matter as much as a
    # long span. Absolute-length errors otherwise sacrifice narrow features.
    weights = (np.median(lengths) / np.maximum(lengths, np.median(lengths)*1e-4))**2
    p = np.array([positions[i] for i in ids], dtype=float)
    target = np.array([anchors[i] for i in ids], dtype=float)
    aw = np.array([anchor_weight * (3. if locked[i] else 1.) for i in ids])
    # A guide constrains equality, not an absolute coordinate. Each transverse
    # chain shares one variable; native pins alone fix variables in place.
    systems = []
    for dim in (0, 1):
        parents = list(range(len(ids)))
        def root(i):
            while parents[i] != i:
                parents[i] = parents[parents[i]]
                i = parents[i]
            return i
        for group in groups:
            if dim == 1 - _axis(group['axis']):
                members = [index[i] for i in group['ids']]
                for i in members[1:]:
                    parents[root(i)] = root(members[0])
        roots = [root(i) for i in range(len(ids))]
        unique = {r: n for n, r in enumerate(sorted(set(roots)))}
        labels = np.array([unique[r] for r in roots])
        count = len(unique)
        mask = np.ones(count, dtype=bool)
        for i in native_pins:
            mask[labels[index[i]]] = False
        # Keep compatibility for callers with no equality groups.
        if not groups:
            for n, i in enumerate(ids):
                if dim in locked[i]:
                    mask[labels[n]] = False
        cross = labels[a] != labels[b]
        degree = (np.bincount(labels[a[cross]], weights=weights[cross], minlength=count) +
                  np.bincount(labels[b[cross]], weights=weights[cross], minlength=count) +
                  np.bincount(labels, weights=aw, minlength=count))
        systems.append((labels, count, mask, degree))

    def laplacian(x):
        diff = weights * (x[a] - x[b])
        return (np.bincount(a, weights=diff, minlength=len(ids)) -
                np.bincount(b, weights=diff, minlength=len(ids)) + aw*x)

    def energy(x):
        residual = np.linalg.norm(x[a] - x[b], axis=1) - lengths
        return np.dot(weights, residual*residual) + np.sum(aw[:, None]*(x-target)**2)

    tris = np.array([[index[i] for i in tri] for tri in orientation], dtype=int).reshape(-1, 3)
    signs = np.array([value[0] for value in orientation.values()])
    minimum = np.array([value[1] for value in orientation.values()])
    def areas(x):
        ab = x[tris[:, 1]] - x[tris[:, 0]]
        ac = x[tris[:, 2]] - x[tris[:, 0]]
        return signs * (ab[:, 0]*ac[:, 1] - ab[:, 1]*ac[:, 0])

    for _ in range(passes):
        delta = p[a] - p[b]
        distance = np.linalg.norm(delta, axis=1)
        desired = delta * (lengths / np.maximum(distance, 1e-15))[:, None]
        proposal = p.copy()
        for dim in (0, 1):
            labels, count, mask, degree = systems[dim]
            def reduce(x):
                return np.bincount(labels, weights=x, minlength=count)
            if not mask.any():
                continue
            rhs = (np.bincount(a, weights=weights*desired[:, dim], minlength=len(ids)) -
                   np.bincount(b, weights=weights*desired[:, dim], minlength=len(ids)) +
                   aw*target[:, dim])
            # Solve for a correction with zero entries at fixed coordinates.
            residual = reduce(rhs - laplacian(p[:, dim])) * mask
            z = residual / np.maximum(degree, 1e-15)
            direction = z.copy()
            rz = np.dot(residual, z)
            threshold = max(rz * 1e-12, 1e-24)
            correction = np.zeros(count)
            for __ in range(min(300, count)):
                if rz <= threshold:
                    break
                product = reduce(laplacian(direction[labels])) * mask
                denom = np.dot(direction, product)
                if denom <= 0.:
                    break
                step = rz / denom
                correction += step * direction
                residual -= step * product
                z = residual / np.maximum(degree, 1e-15)
                next_rz = np.dot(residual, z)
                direction = z + (next_rz / rz) * direction
                rz = next_rz
            proposal[:, dim] += (correction * mask)[labels]
        before = energy(p)
        floor = np.minimum(areas(p), minimum)
        accepted = False
        for backtrack in range(20):
            candidate = p + (proposal-p) * (0.5**backtrack)
            if (np.isfinite(candidate).all() and energy(candidate) <= before and
                    np.all(areas(candidate) >= floor - 1e-14)):
                p = candidate
                accepted = True
                break
        if not accepted:
            break
    return {i: tuple(p[index[i]]) for i in ids}


def _shell_winding(mesh, shell, reference):
    """Keep a mirrored shell mirrored, but do not preserve local foldovers."""
    votes = []
    for face in mesh.faces:
        if not set(face) <= shell:
            continue
        a = reference[face[0]]
        for j in range(1,len(face)-1):
            b,c = reference[face[j]], reference[face[j+1]]
            area = (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
            if abs(area)>1e-12:
                votes.append(1 if area>0 else -1)
    return -1. if sum(votes)<0 else 1.


def _conformal_guide_start(mesh, shell, groups, aligned, native_pins, reference=None):
    """Small-shell conformal seed with exact shared H/V coordinate variables.

    Bound dense least-squares memory; large shells retain the iterative seed.
    Temporary gauge anchors only choose the seed's scale/translation. They are
    not stored as pins and are free again in the length fitting stage.
    """
    if len(shell) > 128 or not groups:
        return None
    import numpy as np
    reference = reference or mesh.points
    winding = _shell_winding(mesh, shell, reference)
    ids = sorted(shell)
    idx = {i: n for n, i in enumerate(ids)}
    parents = list(range(2*len(ids)))
    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    for g in groups:
        dim = 1-_axis(g['axis'])
        members = [2*idx[i]+dim for i in g['ids']]
        for j in members[1:]:
            parents[root(j)] = root(members[0])
    roots = [root(i) for i in range(len(parents))]
    unique = {r: j for j, r in enumerate(sorted(set(roots)))}
    labels = np.array([unique[r] for r in roots])
    count = len(unique)
    rows = []
    for face, vertices in zip(mesh.faces, mesh.face_vertices):
        if not set(face) <= shell:
            continue
        for j in range(1, len(face)-1):
            uv_ids = [face[k] for k in (0,j,j+1)]
            a,b,c = [mesh.fn.getPoint(vertices[k]) for k in (0,j,j+1)]
            ab, ac = b-a, c-a
            length = ab.length()
            if length < 1e-12:
                continue
            x = (ab*ac)/length
            y = math.sqrt(max(0., ac.length()**2-x*x))
            y *= winding
            if abs(y) < 1e-12:
                continue
            gx = np.array([-y,y,0.]) / math.sqrt(length*abs(y))
            gy = np.array([x-length,-x,length]) / math.sqrt(length*abs(y))
            first, second = np.zeros(count), np.zeros(count)
            for k,i in enumerate(uv_ids):
                u,v = labels[2*idx[i]:2*idx[i]+2]
                first[u] += gx[k]; first[v] -= gy[k]
                second[u] += gy[k]; second[v] += gx[k]
            rows.extend([first,second])
    if not rows:
        return None
    fixed = {}
    for i in native_pins:
        for dim in (0,1):
            fixed[labels[2*idx[i]+dim]] = mesh.points[i][dim]
    if not native_pins:
        # The longest marked span is a stable gauge for narrow L-shaped strips.
        candidates = [(math.dist(aligned[a], aligned[b]),a,b)
                      for g in groups for a in g['ids'] for b in g['ids'] if a < b]
        if not candidates:
            return None
        _,a,b = max(candidates)
        for i in (a,b):
            for dim in (0,1):
                fixed[labels[2*idx[i]+dim]] = aligned[i][dim]
    matrix = np.array(rows)
    fixed_ids = sorted(fixed)
    free = [i for i in range(count) if i not in fixed]
    x = np.zeros(count)
    x[fixed_ids] = [fixed[i] for i in fixed_ids]
    if free:
        # Tiny positional regularization removes null modes with partial pins.
        initial = np.zeros(count)
        for i in ids:
            for dim in (0,1):
                initial[labels[2*idx[i]+dim]] += mesh.points[i][dim]
        initial /= np.bincount(labels, minlength=count)
        lhs = np.vstack([matrix[:,free], np.eye(len(free))*1e-6])
        rhs = np.concatenate([-matrix[:,fixed_ids]@x[fixed_ids], initial[free]*1e-6])
        x[free] = np.linalg.lstsq(lhs,rhs,rcond=None)[0]
    if not np.isfinite(x).all():
        return None
    # A conformal least-squares seed can contain small inversions near strong
    # boundary constraints. Repair in the same shared-coordinate space instead
    # of discarding it and returning to a folded input.
    triangles = [(f[0],f[j],f[j+1]) for f in mesh.faces if set(f)<=shell
                 for j in range(1,len(f)-1)]
    mapped = [np.array([labels[2*idx[i]+d] for i in tri for d in (0,1)])
              for tri in triangles]
    span = max(np.ptp(x[labels[0::2]]),np.ptp(x[labels[1::2]]),1e-8)
    floor = span*span*1e-10
    inverse_mass = 1. / np.bincount(labels,minlength=count)
    inverse_mass[fixed_ids] = 0.
    for _ in range(500):
        repairs = 0
        for indices in mapped:
            a,b,c = x[indices].reshape(3,2)
            signed = winding*((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))
            if signed >= floor:
                continue
            gradients = winding*np.array([b[1]-c[1],c[0]-b[0],c[1]-a[1],a[0]-c[0],a[1]-b[1],b[0]-a[0]])
            grad = np.bincount(indices,weights=gradients,minlength=count)
            denom = np.dot(grad*inverse_mass,grad)
            if denom>1e-24:
                x += (1.2*floor-signed)/denom * grad*inverse_mass
            repairs += 1
        if not repairs:
            break
    result = {i: tuple(x[labels[2*idx[i]:2*idx[i]+2]]) for i in ids}
    def area(points, triangle):
        a,b,c = [points[i] for i in triangle]
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    for face in mesh.faces:
        if not set(face) <= shell:
            continue
        for j in range(1,len(face)-1):
            tri = (face[0],face[j],face[j+1])
            if area(result,tri)*winding <= 0.:
                return None
    return result


def _relaxed_positions(mesh, shell, groups, native_pins, iterations,
                       anchor_weight=0.05, orientation_reference=None):
    """Fit 3D edge and quad-diagonal lengths under custom H/V constraints."""
    aligned = _constraint_positions(mesh, groups, native_pins)
    locked = defaultdict(set)
    for group in groups:
        transverse = 1 - _axis(group['axis'])
        for i in group['ids']:
            locked[i].add(transverse)
    # Native pins remain full 2D anchors; H/V marks alone are directional.
    for i in native_pins:
        locked[i].update((0, 1))

    # 3D edge lengths remain stable between presses. Using the current UV
    # lengths here would make every new Relax preserve the previous result.
    world_lengths = {}
    for edge, pairs in mesh.edge_uvs.items():
        a, b = mesh.fn.getEdgeVertices(edge)
        length = mesh.fn.getPoint(a).distanceTo(mesh.fn.getPoint(b))
        if length > 1e-10:
            for pair in pairs:
                if set(pair) <= shell:
                    world_lengths[tuple(sorted(pair))] = length
    # Edge lengths alone cannot remove shear in a quad grid. Both diagonals
    # constrain each quad's angles while allowing its border guides to slide.
    for face, vertices in zip(mesh.faces, mesh.face_vertices):
        if len(face) != 4 or not set(face) <= shell:
            continue
        for a, b in ((0, 2), (1, 3)):
            length = mesh.fn.getPoint(vertices[a]).distanceTo(
                mesh.fn.getPoint(vertices[b]))
            if length > 1e-10:
                world_lengths[tuple(sorted((face[a], face[b])))] = length
    ratios = [math.dist(mesh.points[a], mesh.points[b]) / length
              for (a, b), length in world_lengths.items()
              if math.dist(mesh.points[a], mesh.points[b]) > 1e-10]
    scale = statistics.median(ratios) if ratios else 1.0
    neighbors = defaultdict(dict)
    stiff_edges = {tuple(sorted((a, b))) for group in groups if group['mode'] == 'edge'
                   for a, b in zip(group['ids'], group['ids'][1:])}
    for (a, b), world_length in world_lengths.items():
        length = world_length * scale
        if length > 1e-10:
            neighbors[a][b] = length
            neighbors[b][a] = length

    start = _guided_start(mesh, shell, groups, aligned, native_pins)
    seed = _conformal_guide_start(mesh, shell, groups, aligned, native_pins,
                                 orientation_reference)
    if seed is not None:
        start.update(seed)
    positions = {i: list(start[i]) for i in shell}
    anchors = {i: tuple(positions[i]) for i in shell}
    triangles = []
    for face in mesh.faces:
        if len(face) >= 3 and set(face) <= shell:
            triangles.extend((face[0], face[j], face[j + 1])
                             for j in range(1, len(face) - 1))
    orientation = {}
    reference = orientation_reference or mesh.points
    winding = _shell_winding(mesh, shell, reference)
    for triangle in triangles:
        a, b, c = [reference[i] for i in triangle]
        area = (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])
        if abs(area) > 1e-12:
            orientation[triangle] = (winding, abs(area) * .01)
    for _ in range(0 if seed is not None else max(1, int(iterations))):
        updated = {}
        for i in shell:
            old = positions[i]
            if not neighbors[i] or len(locked[i]) == 2:
                continue
            totals = [0.0, 0.0]
            weight_sum = 0.0
            for j, length in neighbors[i].items():
                neighbor = positions[j]
                dx, dy = old[0] - neighbor[0], old[1] - neighbor[1]
                distance = math.hypot(dx, dy)
                if distance < 1e-10:
                    continue
                weight = 100.0 if tuple(sorted((i, j))) in stiff_edges else 1.0
                totals[0] += weight * (neighbor[0] + length * dx / distance)
                totals[1] += weight * (neighbor[1] + length * dy / distance)
                weight_sum += weight
            next_position = old[:]
            for dim in (0, 1):
                if dim in locked[i] or not weight_sum:
                    continue
                # A weak anchor stops global drift without freezing the UV.
                weight = anchor_weight * (3.0 if locked[i] else 1.0)
                target = (totals[dim] + weight * anchors[i][dim]) / (weight_sum + weight)
                next_position[dim] = old[dim] + 0.5 * (target - old[dim])
            updated[i] = next_position
        positions.update(updated)
        # Edge lengths have mirrored solutions. A small signed-area barrier
        # rejects local foldovers while retaining sliding degrees of freedom.
        for triangle, (sign, minimum) in orientation.items():
            a, b, c = triangle
            pa, pb, pc = positions[a], positions[b], positions[c]
            area = sign * ((pb[0]-pa[0])*(pc[1]-pa[1]) -
                           (pb[1]-pa[1])*(pc[0]-pa[0]))
            if area >= minimum:
                continue
            gradients = {a: [pb[1]-pc[1], pc[0]-pb[0]],
                         b: [pc[1]-pa[1], pa[0]-pc[0]],
                         c: [pa[1]-pb[1], pb[0]-pa[0]]}
            denominator = sum(gradients[i][dim] ** 2
                              for i in triangle for dim in (0, 1)
                              if dim not in locked[i])
            if denominator < 1e-16:
                continue
            amount = min((minimum-area) / denominator, .25)
            for i in triangle:
                for dim in (0, 1):
                    if dim not in locked[i]:
                        positions[i][dim] += .35 * amount * sign * gradients[i][dim]
    result = _global_length_relax(positions, neighbors, locked, stiff_edges,
                                  anchors, anchor_weight, orientation,
                                  groups=groups, native_pins=native_pins)
    return _surface_relax(mesh, shell, groups, native_pins, result, scale)



def _surface_relax(mesh, shell, groups, native_pins, positions, scale, strain_weight=0.):
    """Refine triangle stretch/shear using constrained L-BFGS.

    Shared transverse variables keep H/V exact. Native pins remain fixed.
    Positive Jacobians reject foldovers; the soft relative-strain penalty
    prevents tiny bevel edges from being sacrificed to large surface areas.
    No external optimizer or Unfold3D plugin is required.
    """
    if not groups:
        return positions
    import numpy as np
    ids=sorted(shell); index={i:k for k,i in enumerate(ids)}
    parents=list(range(2*len(ids)))
    def root(i):
        while parents[i]!=i:
            parents[i]=parents[parents[i]]; i=parents[i]
        return i
    for group in groups:
        dim=1-_axis(group['axis']); members=[2*index[i]+dim for i in group['ids']]
        for i in members[1:]: parents[root(i)]=root(members[0])
    roots=[root(i) for i in range(len(parents))]; unique={r:j for j,r in enumerate(sorted(set(roots)))}
    labels=np.array([unique[r] for r in roots]); count=len(unique)
    initial=np.array([positions[i] for i in ids]).ravel()
    x=np.bincount(labels,weights=initial,minlength=count)/np.bincount(labels,minlength=count)
    fixed={labels[2*index[i]+dim] for i in native_pins for dim in (0,1)}
    free=np.array([i for i in range(count) if i not in fixed],dtype=int)
    if not len(free): return positions
    triangles=[]; inverses=[]; weights=[]
    winding=_shell_winding(mesh,shell,positions)
    for face,vertices in zip(mesh.faces,mesh.face_vertices):
        if not set(face)<=shell: continue
        for j in range(1,len(face)-1):
            corners=(0,j,j+1)
            a,b,c=[mesh.fn.getPoint(vertices[k]) for k in corners]
            ab,ac=b-a,c-a; length=ab.length()
            if length<1e-12: continue
            tx=(ab*ac)/length; ty=math.sqrt(max(0.,ac.length()**2-tx*tx))
            if ty<length*1e-10: continue
            inverses.append(np.linalg.inv(np.array([[length,tx],[0.,ty*winding]])*scale))
            weights.append(length*ty)
            triangles.append([index[face[k]] for k in corners])
    if not triangles: return positions
    tri=np.array(triangles); inv=np.array(inverses); weight=np.array(weights);weight/=weight.sum()
    strain_edges=[]
    for edge,pairs in mesh.edge_uvs.items():
        a,b=mesh.fn.getEdgeVertices(edge)
        length=mesh.fn.getPoint(a).distanceTo(mesh.fn.getPoint(b))*scale
        if length>1e-12:
            strain_edges.extend((index[i],index[j],length) for i,j in pairs if {i,j}<=shell)
    ea=np.array([e[0] for e in strain_edges]);eb=np.array([e[1] for e in strain_edges])
    rest=np.array([e[2] for e in strain_edges])
    def evaluate(values):
        points=values[labels].reshape(-1,2)
        edges=np.stack((points[tri[:,1]]-points[tri[:,0]],points[tri[:,2]]-points[tri[:,0]]),axis=2)
        jac=edges@inv
        det=jac[:,0,0]*jac[:,1,1]-jac[:,0,1]*jac[:,1,0]
        if np.any(det<=1e-10): return float('inf'),None
        inverse=np.empty_like(jac)
        inverse[:,0,0]=jac[:,1,1];inverse[:,1,1]=jac[:,0,0]
        inverse[:,0,1]=-jac[:,0,1];inverse[:,1,0]=-jac[:,1,0];inverse/=det[:,None,None]
        energy=float(np.sum(weight*np.sum(jac*jac+inverse*inverse,axis=(1,2))))
        transpose=inverse.transpose(0,2,1)
        derivative=2*(jac-transpose@inverse@transpose)*weight[:,None,None]
        derivative=derivative@inv.transpose(0,2,1)
        gradient=np.zeros_like(points)
        np.add.at(gradient,tri[:,1],derivative[:,:,0]);np.add.at(gradient,tri[:,2],derivative[:,:,1])
        np.add.at(gradient,tri[:,0],-derivative.sum(axis=2))
        delta=points[ea]-points[eb];distance=np.linalg.norm(delta,axis=1)
        residual=distance/rest-1.
        # Allow small edge strain, then penalize compression/expansion. This
        # balances surface quality against preservation of narrow features.
        edge_weight=strain_weight/len(rest)
        residual=np.sign(residual)*np.maximum(np.abs(residual)-.05,0.)
        energy+=edge_weight*np.dot(residual,residual)
        force=(2*edge_weight*residual/rest/np.maximum(distance,1e-15))[:,None]*delta
        np.add.at(gradient,ea,force);np.add.at(gradient,eb,-force)
        return energy,np.bincount(labels,weights=gradient.ravel(),minlength=count)[free]
    energy,gradient=evaluate(x)
    if gradient is None: return positions
    history=[]
    for iteration in range(300):
        if np.linalg.norm(gradient)<1e-7: break
        direction=gradient.copy(); alphas=[]
        for step,change,rho in reversed(history):
            alpha=rho*np.dot(step,direction);alphas.append(alpha);direction-=alpha*change
        if history:
            step,change,_=history[-1];direction*=np.dot(step,change)/np.dot(change,change)
        else:
            direction*=1/max(np.linalg.norm(direction),1.)
        for (step,change,rho),alpha in zip(history,reversed(alphas)):
            direction+=step*(alpha-rho*np.dot(change,direction))
        direction=-direction; slope=np.dot(gradient,direction)
        if slope>=0: direction=-gradient; slope=-np.dot(gradient,gradient);history=[]
        accepted=False
        for backtrack in range(35):
            candidate=x.copy();candidate[free]+=direction*(.5**backtrack)
            value,next_gradient=evaluate(candidate)
            if value<=energy+1e-4*(.5**backtrack)*slope:
                accepted=True;break
        if not accepted: break
        step=candidate[free]-x[free];change=next_gradient-gradient;product=np.dot(step,change)
        if product>1e-14:
            history.append((step,change,1/product));history=history[-10:]
        x,energy,gradient=candidate,value,next_gradient
    points=x[labels].reshape(-1,2)
    result = {i:tuple(points[index[i]]) for i in ids}
    if strain_weight == 0.:
        def strain(p):
            coords=np.array([p[i] for i in ids])
            ratios=np.linalg.norm(coords[ea]-coords[eb],axis=1)/rest
            median=float(np.median(ratios))
            return float(np.sqrt(np.mean((ratios/max(median,1e-15)-1.)**2)))
        # Only engage edge protection when surface optimization over-stretches
        # narrow features. A permanent penalty can preserve their initial shear.
        border = _border_pairs(mesh, [shell])
        internal_guide = any(tuple(sorted(pair)) not in border
                             for group in groups for pair in _marked_pairs(mesh,group))
        # Interior guides explicitly request redistribution across adjacent rows.
        # Allow more metric change there than for boundary-only straightening.
        limit = .25 if internal_guide else .095
        if strain(result) > max(limit, strain(positions)*1.05):
            return _surface_relax(mesh,shell,groups,native_pins,positions,scale,50.)
    return result


def _maya_relax_bridge(mesh, shell, pin_positions, iterations=10):
    """Use Maya's legacy Relax to escape a poor local custom-solver state.

    H/V marks are metadata, so the final custom pass can restore them exactly.
    Native UV pins are full anchors and are restored even if untangleUV moves
    them on a particular Maya configuration.
    """
    try:
        cmds.untangleUV(mesh.components(shell), relax='harmonic', pinBorder=True,
                        pinSelected=False, pinUnselected=False,
                        maxRelaxIterations=max(1, int(iterations)))
    except RuntimeError as error:
        cmds.warning('ShellShifter: Maya Relax bridge skipped: {}'.format(error))
    finally:
        if pin_positions:
            Mesh(mesh.path).write(pin_positions)


def _solve_tasks(tasks, iterations, anchor_weight, relax_bridge=False):
    """Solve prepared shells without opening an undo chunk or changing selection."""
    if relax_bridge:
        # A shorter first pass gives legacy Relax a correctly oriented
        # starting shape. The final full pass restores precise guides.
        first_iterations = min(100, max(20, int(iterations) // 3))
        for path, shell, groups, pins, _, _ in tasks:
            mesh = Mesh(path)
            mesh.write(_relaxed_positions(mesh, shell, groups, pins,
                                          first_iterations, anchor_weight))
        bridge_iterations = max(5, min(20, int(iterations) // 20))
        for path, shell, _, _, pin_positions, _ in tasks:
            _maya_relax_bridge(Mesh(path), shell, pin_positions,
                               bridge_iterations)

    for path, shell, groups, pins, pin_positions, reference in tasks:
        mesh = Mesh(path)
        # Restore anchors before reading targets for the final solve.
        if pin_positions:
            mesh.write(pin_positions)
            mesh = Mesh(path)
        mesh.write(_relaxed_positions(mesh, shell, groups, pins,
                                      iterations, anchor_weight, reference))


def _solve_selected_shells(iterations, anchor_weight, label, relax_bridge=False):
    selection, meshes = _selection()
    with _undo(label):
        try:
            tasks = []
            for mesh in meshes:
                _migrate_legacy_marks(mesh)
                groups = _marks(mesh)
                for shell in mesh.shells():
                    relevant = [g for g in groups if set(g['ids']) & shell]
                    pins = mesh.pins(shell)
                    tasks.append((mesh.path, set(shell), relevant, pins,
                                  {i: mesh.points[i] for i in pins},
                                  {i: mesh.points[i] for i in shell}))
            _solve_tasks(tasks, iterations, anchor_weight, relax_bridge)
        finally:
            cmds.select(selection, replace=True)


def relax(iterations=100):
    """Reduce distortion with the custom orientation-safe solver."""
    _solve_selected_shells(iterations, 0.05, 'ShellShifter Relax')


def unfold(iterations=350):
    """Custom solve, legacy Maya Relax bridge, then precise custom solve."""
    _solve_selected_shells(iterations, 0.005, 'ShellShifter Unfold', True)


def _done_fill(shape, enabled):
    import uv_straight_colors as display_colors
    plug = shape + '.uvStraightDoneFillBackup'
    exists = cmds.objExists(plug)
    sets = cmds.polyColorSet(shape, query=True, allColorSets=True) or []
    attrs = ('displayColors', 'materialBlend', 'displayColorChannel',
             'displayColorAsGreyScale', 'displayRedColorChannel',
             'displayGreenColorChannel', 'displayBlueColorChannel')
    if enabled:
        if not exists:
            state = {a: cmds.getAttr(shape + '.' + a) for a in attrs}
            state['current'] = (cmds.polyColorSet(shape, query=True, currentColorSet=True) or [None])[0]
            state['set'] = 'uvStraightDone_' + uuid.uuid4().hex[:12]
            cmds.addAttr(shape, longName='uvStraightDoneFillBackup', dataType='string', hidden=True)
            cmds.setAttr(plug, json.dumps(state), type='string')
        else:
            state = json.loads(cmds.getAttr(plug))
        if state['set'] not in sets:
            cmds.polyColorSet(shape, create=True, colorSet=state['set'], representation='RGBA')
        cmds.polyColorSet(shape, currentColorSet=True, colorSet=state['set'])
        checker = cmds.attributeQuery('uvStraightCheckerState', node=shape, exists=True)
        color = display_colors.done_surface(checker)
        cmds.polyColorPerVertex(shape + '.vtx[*]', rgb=color[:3], alpha=color[3])
        cmds.setAttr(shape + '.displayColors', True)
        cmds.setAttr(shape + '.materialBlend', 6 if checker else 0)
        cmds.setAttr(shape + '.displayColorChannel', 'Diffuse', type='string')
        cmds.setAttr(shape + '.displayColorAsGreyScale', False)
        for channel in ('Red', 'Green', 'Blue'):
            cmds.setAttr(shape + '.display' + channel + 'ColorChannel', True)
    elif exists:
        state = json.loads(cmds.getAttr(plug))
        if state['set'] in sets:
            cmds.polyColorSet(shape, delete=True, colorSet=state['set'])
        if state['current'] in sets and state['current'] != state['set']:
            cmds.polyColorSet(shape, currentColorSet=True, colorSet=state['current'])
        for a in attrs:
            value = state[a]
            if isinstance(value, str):
                cmds.setAttr(shape + '.' + a, value, type='string')
            else:
                cmds.setAttr(shape + '.' + a, value)
        cmds.deleteAttr(plug)


def restore_done_colors():
    """Repair persisted completion markers once, without idle polling."""
    repaired = 0
    import uv_straight_colors as display_colors
    shapes = cmds.ls('*.uvStraightDoneFillBackup', objectsOnly=True, long=True) or []
    with _undo('ShellShifter Restore Done Colors'):
        for shape in shapes:
            if cmds.nodeType(shape) != 'mesh':
                continue
            if cmds.attributeQuery('uvStraightViewportPreviewState', node=shape, exists=True):
                continue  # Keep the active preview visible.
            try:
                checker = cmds.attributeQuery('uvStraightCheckerState', node=shape, exists=True)
                expected = display_colors.done_surface(checker)
                state = json.loads(cmds.getAttr(shape + '.uvStraightDoneFillBackup'))
                sets = cmds.polyColorSet(shape, query=True, allColorSets=True) or []
                current = cmds.polyColorSet(shape, query=True, currentColorSet=True) or []
                ready = (state['set'] in sets and current == [state['set']] and
                         cmds.getAttr(shape + '.displayColors') and
                         cmds.getAttr(shape + '.materialBlend') == (6 if checker else 0) and
                         cmds.getAttr(shape + '.displayColorChannel') == 'Diffuse' and
                         not cmds.getAttr(shape + '.displayColorAsGreyScale') and
                         all(cmds.getAttr(shape + '.display' + c + 'ColorChannel')
                             for c in ('Red', 'Green', 'Blue')))
                if ready:
                    selection = om.MSelectionList()
                    selection.add(shape)
                    fn = om.MFnMesh(selection.getDagPath(0))
                    colors = fn.getVertexColors(state['set'])
                    ready = len(colors) == fn.numVertices and all(
                        all(abs(value-target) < 1e-5 for value, target in
                            zip((c.r, c.g, c.b, c.a), expected)) for c in colors)
                if not ready:
                    _done_fill(shape, True)
                    repaired += 1
            except (RuntimeError, ValueError, KeyError, TypeError) as error:
                cmds.warning('ShellShifter: cannot restore {}: {}'.format(shape, error))
    return repaired


def mark_done(enabled=True):
    """Persistent manual completion mark; restore original display on removal."""
    selected = cmds.ls(selection=True, long=True, objectsOnly=True) or []
    shapes = set()
    for node in selected:
        if cmds.nodeType(node) == 'mesh':
            shapes.add(node)
        else:
            shapes.update(cmds.listRelatives(node, allDescendents=True, type='mesh',
                                            fullPath=True) or [])
    shapes = {s for s in shapes if not cmds.getAttr(s + '.intermediateObject')}
    if not shapes:
        raise ValueError('Select meshes or their components to mark as finished.')
    transforms = {cmds.listRelatives(s, parent=True, fullPath=True)[0] for s in shapes}
    # Store on each affected node, so rename/save/reopen cannot break restoration.
    plans = [(node, {'useOutlinerColor': True, 'outlinerColor': (.18, .85, .3)})
             for node in sorted(transforms)]
    plans += [(node, {'useObjectColor': 2, 'wireColorRGB': (.18, .85, .3)})
              for node in sorted(shapes)]
    attr = 'uvStraightDoneDisplayBackup'
    import uv_straight_native as native
    if any(cmds.attributeQuery('uvStraightViewportPreviewState', node=shape, exists=True)
           for shape in shapes):
        # Back up the underlying display, never a temporary distortion set.
        native.restore_viewport_colors(only=shapes)
    with _undo('ShellShifter Mark Done' if enabled else 'ShellShifter Clear Done'):
        for shape in sorted(shapes):
            import uv_straight_checker as checker
            checker_state = checker._state(shape)
            if checker_state is not None:
                cmds.setAttr(shape+'.displayColors',checker_state['displayColors'])
            _done_fill(shape, enabled)
            if checker_state is not None:
                checker_state['displayColors']=cmds.getAttr(shape+'.displayColors')
                cmds.setAttr(shape+'.'+checker.STATE,json.dumps(checker_state),type='string')
                cmds.setAttr(shape+'.displayColors',bool(enabled))
        for node, values in plans:
            plug = node + '.' + attr
            exists = cmds.attributeQuery(attr, node=node, exists=True)
            if enabled:
                if not exists:
                    original = {key: cmds.getAttr(node + '.' + key) for key in values}
                    cmds.addAttr(node, longName=attr, dataType='string', hidden=True)
                    cmds.setAttr(plug, json.dumps(original), type='string')
                target = values
            else:
                if not exists:
                    continue
                target = json.loads(cmds.getAttr(plug))
            for key, value in target.items():
                if isinstance(value, (list, tuple)):
                    if len(value) == 1 and isinstance(value[0], (list, tuple)):
                        value = value[0]
                    cmds.setAttr(node + '.' + key, *value, type='double3')
                else:
                    cmds.setAttr(node + '.' + key, value)
            if not enabled:
                cmds.deleteAttr(plug)
    native.refresh_viewport_colors()
    return len(shapes)


def _run(function, *unused):
    try:
        function()
    except (ValueError, RuntimeError) as error:
        cmds.warning('ShellShifter: {}'.format(error))
        cmds.confirmDialog(title='ShellShifter', message=str(error), button=['OK'])
    finally:
        if _preview is not None:
            _preview.refresh()


def reload_tool():
    """Reload the complete display backend, including Maya's isolated loader."""
    import importlib
    import sys
    progress = sys.modules.get('uv_straight_pack_progress')
    if progress is not None and progress.is_running():
        raise ValueError('Stop the packing search before reloading ShellShifter.')
    import uv_straight_native as native
    from uv_straight_preview import QtWidgets
    import uv_straight_menu
    uv_straight_menu.uninstall()
    enabled = native._manager is not None
    texel_ui = sys.modules.get('uv_straight_texel_ui')
    if texel_ui is not None:
        texel_ui.stop_display()
    native.disable()
    for widget in QtWidgets.QApplication.allWidgets():
        if widget.objectName() not in ('uvStraightQtWindow', 'uvStraightQtPopup'):
            continue
        custom_keymaps = getattr(widget, 'custom_keymaps', None)
        if custom_keymaps is not None and hasattr(custom_keymaps, 'close_functions'):
            custom_keymaps.close_functions()
        pie_editor = getattr(widget, 'pie_preview', None)
        if pie_editor is not None:
            if hasattr(pie_editor,'close_functions'):
                pie_editor.close_functions()
            pie_editor.canvas.drag.cancel()
        section_drag = getattr(widget, 'section_drag', None)
        if section_drag is not None:
            section_drag.cancel()
        controller = getattr(widget, 'materials', None)
        if controller is not None:
            controller.stop()
        controller = getattr(widget, 'updates', None)
        if controller is not None:
            controller.stop()
        controller = getattr(widget, 'error_checks', None)
        if controller is not None:
            controller.stop()
        controller = getattr(widget, 'stack_shells', None)
        if controller is not None:
            controller.stop()
        controller = getattr(widget, 'uv_sets', None)
        if controller is not None:
            controller.stop()
        if widget.objectName() == 'uvStraightQtPopup':
            widget.close()
    import uv_straight_preview
    uv_straight_preview._popup = None
    # Keep the registered VP2 provider and shared draw-state module alive.
    # Unloading it while a model editor owns draw data is unsafe.
    for name in ('uv_straight_actions', 'uv_straight_icons', 'uv_straight_custom_keymaps', 'uv_straight_pie',
                 'uv_straight_section_drag',
                 'uv_straight_pie_drag', 'uv_straight_pie_catalog', 'uv_straight_pie_preview',
                 'uv_straight_pie_popup',
                 'uv_straight_uv_set_tags', 'uv_straight_changes',
                 'uv_straight_distortion', 'uv_straight_pack_area', 'uv_straight_pack_stacks', 'uv_straight_pack_search', 'uv_straight_contour_pack',
                 'uv_straight_pack_kernels', 'uv_straight_pack_gpu', 'uv_straight_pack_commit', 'uv_straight_pack', 'uv_straight_pack_progress', 'uv_straight_pack_ui', 'uv_straight_colors', 'uv_straight_checker',
                 'uv_straight_raster', 'uv_straight_overlay',
                 'uv_straight_live_preview', 'uv_straight_texel', 'uv_straight_texel_ui', 'uv_straight_uv_seams', 'uv_straight_error_checks',
                 'uv_straight_materials', 'uv_straight_material_groups', 'uv_straight_material_ui',
                 'uv_straight_overlap_quality', 'uv_straight_check_ui',
                 'uv_straight_stack', 'uv_straight_stack_ui', 'uv_straight_unwrap', 'uv_straight_ribbon', 'uv_straight_qt_native',
                 'uv_straight_native'):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    if enabled:
        native.enable()
    show()
    return native.status()


def show():
    """Open the tool window using the Qt runtime bundled with Maya."""
    global _preview, _window
    restore_done_colors()
    # A crash or forced exit can leave the ordinary mesh preview color set in
    # the scene. When distortion is off, restore the original display before
    # constructing the UI; when it is on, the display manager will refresh it.
    import uv_straight_native as native
    native.unload_legacy_viewport_plugin()
    if not (bool(cmds.optionVar(query='uvStraightDistortion'))
            if cmds.optionVar(exists='uvStraightDistortion') else False):
        native.restore_viewport_colors()
    import uv_straight_preview
    import importlib
    import uv_straight_checker
    importlib.reload(uv_straight_checker)
    uv_straight_checker.refresh_palette()
    importlib.reload(uv_straight_preview)
    _window = uv_straight_preview.show_window()
    _preview = None
    import install_shellshifter
    install_shellshifter.rename_existing_buttons()
    import uv_straight_menu
    uv_straight_menu.uninstall()
    importlib.reload(uv_straight_menu)
    uv_straight_menu.install()


if __name__ == '__main__':
    show()
