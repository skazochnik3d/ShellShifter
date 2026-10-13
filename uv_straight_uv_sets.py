"""Evaluate extra UV sets, recover map1, and remove reviewed rejected layouts."""
from dataclasses import dataclass
import fnmatch
import hashlib
import json
import re
import numpy as np
import maya.api.OpenMaya as om
import maya.cmds as cmds
from uv_straight_move_overlaps import _selected_mesh_shapes, _mesh_fn

DEFAULT_LIMIT = 5.0
ATTRS = ('uvStraightConstraints', 'uvStraightMoveOverlapOriginals')


@dataclass(frozen=True)
class Quality:
    name: str
    worst: float = float('inf')
    mean: float = float('inf')
    shells: int = 0
    reason: str = ''
    boundary_angle: float = 45.
    ignored_triangles: int = 0

    def good(self, limit):
        return not self.reason and self.worst <= limit + 1e-5

    @property
    def description(self):
        text = self.reason or 'max distortion {:.2f}% · {} shells · boundary angle {:.1f}°'.format(
            self.worst, self.shells, self.boundary_angle)
        if self.ignored_triangles:
            text += ' · {} zero-area mesh triangles excluded from metric'.format(self.ignored_triangles)
        return text


@dataclass(frozen=True)
class Result:
    shape: str
    uuid: str
    signature: str
    qualities: tuple
    source: str
    reason: str
    delete_sets: tuple = ()
    create_map1: bool = False

    @property
    def actionable(self):
        return bool(self.source or self.delete_sets)


def matches(name, filters):
    patterns = [part.strip() for part in re.split('[,;]', filters) if part.strip()]
    return name != 'map1' and any(fnmatch.fnmatchcase(name, pattern) for pattern in (patterns or ['*']))


def uv_data(fn, name):
    u, v = fn.getUVs(name)
    counts, ids = fn.getAssignedUVs(name)
    return (np.column_stack((u, v)).astype(np.float64),
            np.asarray(counts, dtype=np.int32), np.asarray(ids, dtype=np.int32))


class Geometry:
    def __init__(self, fn):
        counts, vertices = fn.getVertices()
        triangles, triangle_vertices = fn.getTriangles()
        self.counts = np.asarray(counts, dtype=np.int32)
        self.vertices = np.asarray(vertices, dtype=np.int32)
        self.xyz = np.array([(p.x, p.y, p.z) for p in fn.getPoints(om.MSpace.kObject)])
        tri_vertices = np.asarray(triangle_vertices, dtype=np.int32).reshape(-1, 3)
        faces = np.repeat(np.arange(len(counts), dtype=np.int64), counts)
        tri_faces = np.repeat(np.arange(len(counts), dtype=np.int64), np.asarray(triangles)*3)
        stride = fn.numVertices
        codes = faces*stride + self.vertices
        ordered = np.argsort(codes)
        lookup = np.searchsorted(codes[ordered], tri_faces*stride + tri_vertices.ravel())
        self.corners = ordered[lookup].reshape(-1, 3)
        ab = self.xyz[tri_vertices[:, 1]] - self.xyz[tri_vertices[:, 0]]
        ac = self.xyz[tri_vertices[:, 2]] - self.xyz[tri_vertices[:, 0]]
        length = np.linalg.norm(ab, axis=1)
        area2 = np.linalg.norm(np.cross(ab, ac), axis=1)
        self.valid = (length > 1e-15) & (area2 > 1e-15)
        self.area = area2*.5
        self.inv_length = 1/np.maximum(length, 1e-30)
        self.x = np.einsum('ij,ij->i', ab, ac)*self.inv_length
        self.inv_y = length/np.maximum(area2, 1e-30)


def boundary_angle(uv, counts, ids):
    """Length-weighted deviation of shell boundaries from horizontal/vertical."""
    if not len(ids):
        return 45.
    ends = np.cumsum(counts)-1
    next_corner = np.arange(len(ids))+1
    nonempty = counts > 0
    starts = np.r_[0, np.cumsum(counts)[:-1]]
    next_corner[ends[nonempty]] = starts[nonempty]
    edges = np.sort(np.column_stack((ids, ids[next_corner])), axis=1)
    pairs, uses = np.unique(edges, axis=0, return_counts=True)
    boundary = pairs[uses == 1]
    delta = np.abs(uv[boundary[:, 1]] - uv[boundary[:, 0]])
    length = np.linalg.norm(delta, axis=1)
    valid = length > 0
    if not np.any(valid):
        return 45.
    angles = np.degrees(np.arcsin(np.clip(np.min(delta[valid], axis=1)/length[valid], 0, 1)))
    return float(np.average(angles, weights=length[valid]))


def quality(fn, geometry, name, data):
    uv, counts, ids = data
    if not len(uv):
        return Quality(name, reason='Empty UV set')
    if not np.array_equal(counts, geometry.counts):
        return Quality(name, reason='Missing UVs on some faces')
    if len(ids) != len(geometry.vertices) or np.any(ids < 0) or np.any(ids >= len(uv)):
        return Quality(name, reason='Invalid UV assignments')
    if not np.all(np.isfinite(uv)) or not np.all(np.isfinite(geometry.xyz)):
        return Quality(name, reason='Non-finite coordinates')
    if not len(geometry.corners) or not np.any(geometry.valid):
        return Quality(name, reason='Degenerate mesh triangles')
    _, shell_ids = fn.getUvShellsIds(name)
    shell_ids = np.asarray(shell_ids, dtype=np.int32)
    # A zero-area 3D triangle has no stretch metric. It must not disqualify
    # the UV layout of all other faces on an otherwise usable mesh.
    valid = geometry.valid
    tri_ids = ids[geometry.corners[valid]]
    shells = shell_ids[tri_ids[:, 0]]
    b = uv[tri_ids[:, 1]] - uv[tri_ids[:, 0]]
    c = uv[tri_ids[:, 2]] - uv[tri_ids[:, 0]]
    signed = b[:, 0]*c[:, 1] - b[:, 1]*c[:, 0]
    magnitude = np.linalg.norm(b, axis=1)*np.linalg.norm(c, axis=1)
    if np.any(np.abs(signed) <= magnitude*1e-10) or np.any(magnitude == 0):
        return Quality(name, reason='Collapsed UV triangles')
    positive = np.bincount(shells, weights=(signed > 0).astype(float))
    negative = np.bincount(shells, weights=(signed < 0).astype(float))
    if np.any((positive > 0) & (negative > 0)):
        return Quality(name, reason='Folded UV shells')
    p = b*geometry.inv_length[valid, None]
    q = (c - p*geometry.x[valid, None])*geometry.inv_y[valid, None]
    trace = np.sum(p*p + q*q, axis=1)
    determinant = np.abs(p[:, 0]*q[:, 1] - q[:, 0]*p[:, 1])
    high = np.sqrt(np.maximum(0, .5*(trace + np.sqrt(np.maximum(0, trace*trace - 4*determinant**2)))))
    low = determinant/np.maximum(high, 1e-30)
    if np.any(low <= 0) or not np.all(np.isfinite(high)):
        return Quality(name, reason='Invalid UV metric')
    weights = geometry.area[valid]
    area = np.bincount(shells, weights=weights)
    logs = np.bincount(shells, weights=weights*.5*np.log(low*high))
    neutral = np.exp(logs/np.maximum(area, 1e-30))
    deviation = np.max(np.abs(np.log(np.column_stack((low, high))/neutral[shells, None])), axis=1)
    distortion = np.expm1(np.minimum(deviation, 700))*100
    return Quality(name, float(np.max(distortion)), float(np.average(distortion, weights=weights)),
                   int(np.count_nonzero(area)), boundary_angle=boundary_angle(uv, counts, ids),
                   ignored_triangles=int(np.count_nonzero(~valid)))


def signature(fn, geometry, data, metadata):
    digest = hashlib.sha256()
    digest.update(str(fn.uuid()).encode() if hasattr(fn, 'uuid') else b'')
    for array in (geometry.counts, geometry.vertices, geometry.xyz):
        digest.update(array.tobytes())
    for name, arrays in data.items():
        digest.update(name.encode('utf-8') + b'\0')
        for array in arrays:
            digest.update(str(array.shape).encode() + array.tobytes())
    digest.update(json.dumps(metadata, sort_keys=True).encode('utf-8'))
    return digest.hexdigest()


def analyse_mesh(shape, filters='', limit=DEFAULT_LIMIT):
    fn = _mesh_fn(shape)
    names = list(fn.getUVSetNames())
    missing_map1 = 'map1' not in names
    empty_map1 = False
    incomplete_map1 = False
    if not missing_map1:
        map1_uv, map1_counts, _ = uv_data(fn, 'map1')
        empty_map1 = not len(map1_uv) or not np.any(map1_counts)
        mesh_counts, _ = fn.getVertices()
        incomplete_map1 = not np.array_equal(map1_counts, np.asarray(mesh_counts, dtype=np.int32))
    recover = missing_map1 or empty_map1 or incomplete_map1
    selected = [name for name in names if name != 'map1' and (recover or matches(name, filters))]
    if not selected:
        if missing_map1:
            return Result(shape, (cmds.ls(shape, uuid=True) or [''])[0], '', (), '',
                          'map1 is missing and this mesh has no UV sets to copy.')
        return None
    identity = (cmds.ls(shape, uuid=True) or [''])[0]
    if cmds.referenceQuery(shape, isNodeReferenced=True) or any(cmds.lockNode(shape, query=True, lock=True)):
        return Result(shape, identity, '', (), '', 'Mesh is referenced or locked')
    if any(fn.isUVSetPerInstance(name) for name in names):
        return Result(shape, identity, '', (), '', 'Per-instance UV sets are not supported')
    geometry = Geometry(fn)
    data = {name: uv_data(fn, name) for name in names}
    metadata = {attr: cmds.getAttr(shape+'.'+attr) for attr in ATTRS if cmds.objExists(shape+'.'+attr)}
    qualities = tuple(quality(fn, geometry, name, data[name]) for name in ['map1'] + selected if name in data)
    candidates = sorted((entry for entry in qualities if entry.name != 'map1' and entry.good(limit)),
                        key=lambda entry: (round(entry.worst, 3), round(entry.mean, 3),
                                           round(entry.boundary_angle, 3), entry.name))
    map1_quality = next((entry for entry in qualities if entry.name == 'map1'), None)
    damaged_map1 = map1_quality is not None and not map1_quality.good(limit)
    # Recover a missing/empty default set even when a finished layout has some
    # local stretch. Structural failures still exclude a candidate; the result
    # explicitly reports that this fallback exceeds the user's limit.
    fallback = (recover or damaged_map1) and not candidates
    if fallback:
        candidates = sorted((entry for entry in qualities if entry.name != 'map1' and not entry.reason),
                            key=lambda entry: (round(entry.mean, 3), round(entry.worst, 3),
                                               round(entry.boundary_angle, 3), entry.name))
        if not recover:
            # A complete but distorted map1 can be improved by an above-limit
            # source. Do not replace it with an equally bad or worse layout.
            candidates = [entry for entry in candidates if map1_quality.reason
                          or (round(entry.mean, 3), round(entry.worst, 3))
                          < (round(map1_quality.mean, 3), round(map1_quality.worst, 3))]
    source, reason = '', 'No extra set passes the {:.2f}% limit'.format(limit)
    if candidates:
        best = candidates[0]
        source = best.name
        reason = ('Rename {} to map1' if missing_map1 else 'Copy {} to map1').format(source)
        if not missing_map1:
            reason += '; delete '+source
    rejected = tuple(entry.name for entry in qualities
                     if entry.name != source and matches(entry.name, filters) and not entry.good(limit))
    delete_sets = ((source,) if source else ()) + (rejected if source or not recover else ())
    if rejected and delete_sets:
        reason = (('Rename {} to map1. ' if missing_map1 else 'Copy {} to map1. ').format(source)
                  if source else 'Keep map1 UVs unchanged. ')
        reason += 'Delete: '+', '.join(rejected if missing_map1 else delete_sets)
    elif not source and recover:
        reason = 'No usable extra layout and map1 is missing, empty, or incomplete. Nothing will be deleted.'
    if source and fallback:
        reason += ' Best usable recovery layout exceeds {:.2f}% limit (max {:.2f}%, mean {:.2f}%).'.format(
            limit, candidates[0].worst, candidates[0].mean)
    return Result(shape, identity, signature(fn, geometry, data, metadata), qualities, source, reason,
                  delete_sets, missing_map1 and bool(source))


def analyse_steps(filters='', limit=DEFAULT_LIMIT):
    if not np.isfinite(limit) or limit < 0:
        raise ValueError('The distortion limit must be a finite, non-negative percentage.')
    shapes = []
    seen = set()
    for shape in _selected_mesh_shapes():
        identity = (cmds.ls(shape, uuid=True) or [shape])[0]
        if identity not in seen:
            seen.add(identity)
            shapes.append(shape)
    results = []
    for index, shape in enumerate(shapes):
        try:
            result = analyse_mesh(shape, filters, limit)
        except (RuntimeError, ValueError, IndexError) as error:
            result = Result(shape, (cmds.ls(shape, uuid=True) or [''])[0], '', (), '', str(error))
        if result is not None:
            results.append(result)
        yield index + 1, len(shapes)
    return tuple(results)


def promote(results, filters='', limit=DEFAULT_LIMIT):
    """Rename/copy a reviewed source or keep map1, then remove rejected sets."""
    pending = [result for result in results if result.actionable]
    if not pending:
        raise ValueError('Select a row with a UV set cleanup recommendation first.')
    checked = []
    for result in pending:
        if not cmds.objExists(result.shape) or (cmds.ls(result.shape, uuid=True) or [''])[0] != result.uuid:
            raise ValueError('A checked mesh was removed or replaced. Analyse UV sets again.')
        current = analyse_mesh(result.shape, filters, limit)
        if (current is None or current.signature != result.signature or current.source != result.source
                or current.delete_sets != result.delete_sets or current.create_map1 != result.create_map1):
            raise ValueError('UVs, geometry, or UV sets changed. Analyse UV sets again.')
        checked.append((result, uv_data(_mesh_fn(result.shape), result.source or 'map1')))
    from uv_straight import _undo
    with _undo('ShellShifter Promote UV Sets'):
        for result, source_data in checked:
            shape, source = result.shape, result.source
            indices = cmds.polyUVSet(shape, query=True, allUVSetsIndices=True) or []
            plugs = {cmds.getAttr('{}.uvSet[{}].uvSetName'.format(shape, index)):
                     '{}.uvSet[{}].uvSetName'.format(shape, index) for index in indices}
            textures = {texture for name in result.delete_sets
                        for texture in (cmds.uvLink(query=True, uvSet=plugs[name]) or [])}
            if result.create_map1:
                cmds.polyUVSet(shape, rename=True, uvSet=source, newUVSet='map1')
            elif source:
                cmds.polyUVSet(shape, copy=True, uvSet=source, newUVSet='map1')
            if not all(np.array_equal(a, b) for a, b in zip(source_data, uv_data(_mesh_fn(shape), 'map1'))):
                raise RuntimeError('UV copy verification failed for '+shape)
            cmds.polyUVSet(shape, currentUVSet=True, uvSet='map1')
            names = list(_mesh_fn(shape).getUVSetNames())
            if names[0] != 'map1':
                cmds.polyUVSet(shape, reorder=True, uvSet='map1', newUVSet=names[0])
            indices = cmds.polyUVSet(shape, query=True, allUVSetsIndices=True) or []
            plugs = {cmds.getAttr('{}.uvSet[{}].uvSetName'.format(shape, index)):
                     '{}.uvSet[{}].uvSetName'.format(shape, index) for index in indices}
            for texture in textures:
                cmds.uvLink(make=True, texture=texture, uvSet=plugs['map1'])
            for attr in ATTRS:
                plug = shape+'.'+attr
                if cmds.objExists(plug):
                    metadata = json.loads(cmds.getAttr(plug) or '{}')
                    if source:
                        metadata.pop('map1', None)
                        if source in metadata:
                            metadata['map1'] = metadata.pop(source)
                    for name in result.delete_sets:
                        metadata.pop(name, None)
                    cmds.setAttr(plug, json.dumps(metadata), type='string')
            for name in result.delete_sets:
                if result.create_map1 and name == source:
                    continue
                cmds.polyUVSet(shape, delete=True, uvSet=name)
            if any(name in _mesh_fn(shape).getUVSetNames() for name in result.delete_sets):
                raise RuntimeError('UV set deletion failed for '+shape)
            if not all(np.array_equal(a, b) for a, b in zip(source_data, uv_data(_mesh_fn(shape), 'map1'))):
                raise RuntimeError('map1 changed unexpectedly during cleanup for '+shape)
    import uv_straight_native as native
    tracker = getattr(native._manager, 'changes', None)
    if tracker is not None:
        tracker.invalidate()
    return len(checked)
