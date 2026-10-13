"""Merge usable UV shells into a named destination and preserve tagged sets."""
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import uuid
import re
import numpy as np
import maya.api.OpenMaya as om
import maya.cmds as cmds
from uv_straight_uv_sets import Geometry, uv_data, matches, ATTRS, DEFAULT_LIMIT
from uv_straight_move_overlaps import _mesh_fn, _selected_mesh_shapes


@dataclass(frozen=True)
class Shell:
    name: str
    shell: int
    faces: np.ndarray = field(compare=False, repr=False)
    mean: float = float('inf')
    worst: float = float('inf')
    boundary_angle: float = 45.
    reason: str = ''

    def good(self, limit):
        return not self.reason and self.mean <= limit + 1e-5

    @property
    def description(self):
        return '{} shell {}: {}'.format(self.name, self.shell,
            self.reason or 'mean {:.2f}%, max {:.2f}%, boundary {:.1f}°'.format(
                self.mean, self.worst, self.boundary_angle))


@dataclass(frozen=True)
class Result:
    shape: str
    uuid: str
    signature: str
    shells: tuple
    source: str
    reason: str
    delete_sets: tuple
    create_map1: bool
    good_shells: int
    recovered_faces: int
    kept_faces: int
    unmapped_faces: int
    output: tuple = field(compare=False, repr=False)
    metadata: dict = field(compare=False, repr=False)
    settings: tuple = ()
    destination: str = 'map1'
    protected: tuple = ('map1',)

    @property
    def actionable(self):
        return bool(self.delete_sets or self.create_map1 or self.recovered_faces)


def face_corners(geometry, data):
    """Partial sets are valid sources: map only faces with full assignments."""
    uv, counts, ids = data
    face_count = len(geometry.counts)
    result = np.full(len(geometry.vertices), -1, dtype=np.int32)
    mapped = np.zeros(face_count, dtype=bool)
    if len(counts) > face_count or np.any(counts < 0) or int(np.sum(counts)) != len(ids):
        return result, mapped
    padded = np.zeros(face_count, dtype=np.int32)
    padded[:len(counts)] = counts
    valid = (padded == geometry.counts) & (padded > 0)
    source_faces = np.repeat(np.arange(face_count), padded)
    target_faces = np.repeat(np.arange(face_count), geometry.counts)
    source_ids = ids[valid[source_faces]]
    positions = np.flatnonzero(valid[target_faces])
    result[positions] = source_ids
    invalid = (source_ids < 0) | (source_ids >= len(uv))
    if len(uv):
        invalid |= ~np.all(np.isfinite(uv[np.clip(source_ids, 0, len(uv)-1)]), axis=1)
    else:
        invalid[:] = True
    bad_faces = np.unique(target_faces[positions[invalid]])
    valid[bad_faces] = False
    result[~valid[target_faces]] = -1
    return result, valid


def analyse_shells(fn, geometry, name, data, corners, mapped):
    uv, counts, ids = data
    if not np.any(mapped):
        return ()
    shell_count, uv_shells = fn.getUvShellsIds(name)
    uv_shells = np.asarray(uv_shells, dtype=np.int32)
    face_starts = np.r_[0, np.cumsum(geometry.counts)[:-1]]
    face_shells = np.full(len(mapped), -1, dtype=np.int32)
    face_shells[mapped] = uv_shells[corners[face_starts[mapped]]]
    # Geometry already maps triangles to face corners; recover their face IDs
    # by one vectorised lookup, without a polygon iterator.
    tri_faces = np.searchsorted(np.cumsum(geometry.counts), geometry.corners[:, 0], side='right')
    valid = mapped[tri_faces] & geometry.valid
    tri_ids = corners[geometry.corners[valid]]
    tri_shells = uv_shells[tri_ids[:, 0]]
    b = uv[tri_ids[:, 1]] - uv[tri_ids[:, 0]]
    c = uv[tri_ids[:, 2]] - uv[tri_ids[:, 0]]
    signed = b[:, 0]*c[:, 1] - b[:, 1]*c[:, 0]
    magnitude = np.linalg.norm(b, axis=1)*np.linalg.norm(c, axis=1)
    collapsed = (magnitude == 0) | (np.abs(signed) <= magnitude*1e-10)
    positive = np.bincount(tri_shells, weights=(signed > 0), minlength=shell_count)
    negative = np.bincount(tri_shells, weights=(signed < 0), minlength=shell_count)
    folded = (positive > 0) & (negative > 0)
    bad = np.bincount(tri_shells, weights=collapsed, minlength=shell_count) > 0
    p = b*geometry.inv_length[valid, None]
    q = (c - p*geometry.x[valid, None])*geometry.inv_y[valid, None]
    trace = np.sum(p*p + q*q, axis=1)
    determinant = np.abs(p[:, 0]*q[:, 1] - q[:, 0]*p[:, 1])
    high = np.sqrt(np.maximum(0, .5*(trace+np.sqrt(np.maximum(0, trace*trace-4*determinant**2)))))
    low = determinant/np.maximum(high, 1e-30)
    weights = geometry.area[valid]
    area = np.bincount(tri_shells, weights=weights, minlength=shell_count)
    logs = np.bincount(tri_shells, weights=weights*.5*np.log(np.maximum(low*high, 1e-300)), minlength=shell_count)
    neutral = np.exp(logs/np.maximum(area, 1e-30))
    deviation = np.max(np.abs(np.log(np.maximum(np.column_stack((low, high)), 1e-300)/
                                    np.maximum(neutral[tri_shells, None], 1e-300))), axis=1)
    distortion = np.expm1(np.minimum(deviation, 700))*100
    means = np.bincount(tri_shells, weights=weights*distortion, minlength=shell_count)/np.maximum(area, 1e-30)
    worst = np.zeros(shell_count)
    np.maximum.at(worst, tri_shells, distortion)
    # Compute boundary alignment once per set, then aggregate by UV shell.
    target_faces = np.repeat(np.arange(len(mapped)), geometry.counts)
    next_corner = np.arange(len(corners))+1
    ends = np.cumsum(geometry.counts)-1
    next_corner[ends] = face_starts
    edge_positions = mapped[target_faces]
    edges = np.sort(np.column_stack((corners[edge_positions], corners[next_corner[edge_positions]])), axis=1)
    pairs, uses = np.unique(edges, axis=0, return_counts=True)
    pairs = pairs[uses == 1]
    delta = np.abs(uv[pairs[:, 1]]-uv[pairs[:, 0]])
    length = np.linalg.norm(delta, axis=1)
    angle = np.degrees(np.arcsin(np.clip(np.min(delta, axis=1)/np.maximum(length, 1e-30), 0, 1)))
    edge_shells = uv_shells[pairs[:, 0]]
    border = np.bincount(edge_shells, weights=length, minlength=shell_count)
    angles = np.bincount(edge_shells, weights=length*angle, minlength=shell_count)/np.maximum(border, 1e-30)
    faces = np.flatnonzero(mapped)
    order = np.argsort(face_shells[faces], kind='stable')
    faces = faces[order]
    ids_ordered = face_shells[faces]
    split = np.flatnonzero(np.diff(ids_ordered))+1
    shells = []
    for group in np.split(faces, split):
        shell = int(face_shells[group[0]])
        reason = ('Folded UV shell' if folded[shell] else 'Collapsed UV triangles' if bad[shell]
                  else 'No measurable mesh area' if area[shell] <= 0 else '')
        shells.append(Shell(name, shell, group, float(means[shell]), float(worst[shell]),
                            float(angles[shell]), reason))
    return tuple(shells)


def snapshot(shape):
    fn = _mesh_fn(shape)
    geometry = Geometry(fn)
    data = {name: uv_data(_mesh_fn(shape), name) for name in fn.getUVSetNames()}
    metadata = {attr: cmds.getAttr(shape+'.'+attr) for attr in ATTRS if cmds.objExists(shape+'.'+attr)}
    digest = hashlib.sha256()
    for array in (geometry.counts, geometry.vertices, geometry.xyz):
        digest.update(array.tobytes())
    for name, arrays in data.items():
        digest.update(name.encode('utf-8')+b'\0')
        for array in arrays:
            digest.update(str(array.shape).encode()+array.tobytes())
    digest.update(json.dumps(metadata, sort_keys=True).encode())
    return fn, geometry, data, metadata, digest.hexdigest()


def merge_metadata(raw, remaps, face_sources, output, destination='map1', protected=('map1',)):
    result = {}
    for attr, text in raw.items():
        old = json.loads(text or '{}')
        if attr == 'uvStraightConstraints':
            groups = []
            for name, mapping in remaps.items():
                for group in old.get(name, {}).get('groups', []):
                    if all(int(index) in mapping for index in group['ids']):
                        groups.append(dict(group, ids=[mapping[int(index)] for index in group['ids']]))
            uv, counts, ids = output
            starts = np.r_[0, np.cumsum(counts)[:-1]]
            faces = [tuple(ids[start:start+count].tolist()) for start, count in zip(starts, counts) if count]
            topology = hashlib.sha256(json.dumps([len(uv), faces]).encode()).hexdigest()
            result[attr] = {destination: {'topology': topology, 'groups': groups, 'format': 2}} if groups else {}
        else:
            originals = []
            for name, entries in old.items():
                for faces in entries:
                    if faces and all(0 <= int(face) < len(face_sources) and face_sources[int(face)] == name for face in faces):
                        originals.append(faces)
            result[attr] = {destination: originals} if originals else {}
        result[attr].update({name: value for name, value in old.items()
                             if name in protected and name != destination})
    return result


def keep_names(names):
    if isinstance(names, str):
        raise ValueError('Provide UV set names as separate tags.')
    names = tuple(dict.fromkeys(names))
    if not names:
        raise ValueError('Add at least one UV set to keep. The first name is the cleanup destination.')
    if any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) for name in names):
        raise ValueError('UV set names must start with a letter or underscore and contain only letters, numbers or underscores.')
    return names


def analyse_mesh(shape, filters='', limit=DEFAULT_LIMIT, keep=None):
    protected = keep_names(('map1',) if keep is None else keep)
    destination = protected[0]
    identity = (cmds.ls(shape, uuid=True) or [''])[0]
    if cmds.referenceQuery(shape, isNodeReferenced=True) or any(cmds.lockNode(shape, query=True, lock=True)):
        return Result(shape, identity, '', (), '', 'Mesh is referenced or locked', (), False, 0, 0, 0, 0, (), {})
    fn, geometry, data, raw_metadata, digest = snapshot(shape)
    if destination in data and all(name in protected for name in data):
        return None
    if any(fn.isUVSetPerInstance(name) for name in data):
        return Result(shape, identity, '', (), '', 'Per-instance UV sets are not supported', (), False, 0, 0, 0, 0, (), {})
    corners = {}
    mapped = {}
    shells = []
    for name, arrays in data.items():
        corners[name], mapped[name] = face_corners(geometry, arrays)
        # Every set is inspected. Previously saved source-name filters must not
        # hide a good shell and then delete its only layout during cleanup.
        shells.extend(analyse_shells(fn, geometry, name, arrays, corners[name], mapped[name]))
    eligible = sorted((shell for shell in shells if shell.good(limit)),
                      key=lambda shell: (round(shell.mean, 3), bool(filters.strip()) and not matches(shell.name, filters),
                                         round(shell.boundary_angle, 3),
                                         round(shell.worst, 3), shell.name != destination, shell.name, shell.shell))
    owner = np.full(len(geometry.counts), '', dtype=object)
    chosen = 0
    for shell in eligible:
        faces = shell.faces[owner[shell.faces] == '']
        if len(faces):
            owner[faces] = shell.name
            chosen += 1
    recovered = int(np.count_nonzero((owner != '') & (owner != destination)))
    # Retain existing destination UVs on faces with no eligible source.
    # Without that set, such faces are explicitly reported as unmapped.
    kept = 0
    if destination in data:
        retain = (owner == '') & mapped[destination]
        kept = int(np.count_nonzero(retain))
        owner[retain] = destination
    counts = np.where(owner != '', geometry.counts, 0).astype(np.int32)
    target_faces = np.repeat(np.arange(len(owner)), geometry.counts)
    packed_ids = np.full(len(geometry.vertices), -1, dtype=np.int32)
    uv_chunks = []
    remaps = {}
    offset = 0
    for name, arrays in data.items():
        mask = owner[target_faces] == name
        original_ids = corners[name][mask]
        if not len(original_ids):
            continue
        unique, inverse = np.unique(original_ids, return_inverse=True)
        packed_ids[mask] = inverse + offset
        uv_chunks.append(arrays[0][unique])
        remaps[name] = dict(zip(unique.tolist(), (np.arange(len(unique))+offset).tolist()))
        offset += len(unique)
    uv = np.concatenate(uv_chunks) if uv_chunks else np.empty((0, 2), dtype=np.float64)
    ids = packed_ids[owner[target_faces] != '']
    output = (uv, counts, ids)
    unmapped = int(np.count_nonzero(owner == ''))
    delete_sets = tuple(name for name in data if name not in protected)
    source = ', '.join(name for name in data if name != destination and name in remaps)
    reason = 'Merge {} low-distortion shells into {}; remove {} extra sets.'.format(chosen, destination, len(delete_sets))
    if kept:
        reason += ' Keep existing {} UVs on {} other faces.'.format(destination, kept)
    if unmapped:
        reason += ' {} faces have no qualifying layout and will remain unmapped.'.format(unmapped)
    settings = (filters, limit) if keep is None else (filters, limit, protected)
    return Result(shape, identity, digest, tuple(shells), source, reason, delete_sets, destination not in data,
                  chosen, recovered, kept, unmapped, output,
                  merge_metadata(raw_metadata, remaps, owner, output, destination, protected), settings,
                  destination, protected)


def analyse_steps(filters='', limit=DEFAULT_LIMIT, keep=None):
    if keep is not None:
        keep = keep_names(keep)
    if not np.isfinite(limit) or limit < 0:
        raise ValueError('The distortion limit must be a finite, non-negative percentage.')
    shapes = list(dict.fromkeys(_selected_mesh_shapes()))
    results = []
    seen = set()
    for index, shape in enumerate(shapes):
        identity = (cmds.ls(shape, uuid=True) or [''])[0]
        if identity not in seen:
            seen.add(identity)
            result = analyse_mesh(shape, filters, limit, keep)
            if result is not None:
                results.append(result)
        yield index+1, len(shapes)
    return tuple(results)


if '_pending' not in globals():
    _pending = {}


def verify_output(shape, output, destination='map1'):
    current = uv_data(_mesh_fn(shape), destination)
    if not all(np.array_equal(a, b) for a, b in zip(current, output)):
        raise RuntimeError('Merged UV verification failed for '+shape)


def promote(results, filters='', limit=DEFAULT_LIMIT, keep=None):
    global _last_error_context
    _last_error_context = {}
    pending = [result for result in results if result.actionable]
    if not pending:
        raise ValueError('Check at least one cleanup result first.')
    if not cmds.undoInfo(query=True, state=True):
        raise ValueError('Enable Undo in Maya preferences before using ShellShifter.')
    # Validate snapshots without re-running shell metrics or shell ranking.
    settings = (filters, limit) if keep is None else (filters, limit, keep_names(keep))
    for result in pending:
        if result.settings != settings or not cmds.objExists(result.shape) or (cmds.ls(result.shape, uuid=True) or [''])[0] != result.uuid:
            raise ValueError('The checked mesh or settings changed. Analyse again.')
        try:
            current_signature = snapshot(result.shape)[-1]
        except RuntimeError as error:
            _last_error_context = dict(shape=result.shape, stage='Apply snapshot validation', original_error=str(error))
            raise RuntimeError('Cannot read UV sets on {} during Apply validation: {}'.format(result.shape, error)) from error
        if current_signature != result.signature:
            raise ValueError('UVs, geometry, or sets changed. Analyse again.')
    plugin = 'uv_straight_uv_merge_command_plugin'
    if not cmds.pluginInfo(plugin, query=True, loaded=True):
        cmds.loadPlugin(str(Path(__file__).with_name(plugin+'.py')), quiet=True)
        cmds.pluginInfo(plugin, edit=True, writeRequires=False)
    key = uuid.uuid4().hex
    _pending[key] = pending
    try:
        cmds.shellShifterCollectUVShells(key)
    finally:
        _pending.pop(key, None)
    import uv_straight_native as native
    tracker = getattr(native._manager, 'changes', None)
    if tracker is not None:
        tracker.invalidate()
    return len(pending)
