"""Material assignments and mapped UV selection, without changing UV sets."""
from collections import defaultdict
from dataclasses import dataclass
from itertools import accumulate
import maya.api.OpenMaya as om
import maya.cmds as cmds


@dataclass(frozen=True)
class Binding:
    path: str
    uuid: str
    face_count: int
    uv_set: str


@dataclass(frozen=True)
class Material:
    name: str
    uuid: str
    color: tuple
    bindings: tuple

    @property
    def objects(self):
        return len({binding.path.rsplit('|', 1)[0] for binding in self.bindings})

    @property
    def uv_sets(self):
        return tuple(sorted({binding.uv_set for binding in self.bindings}))


def mesh_paths():
    return sorted(set(cmds.ls(dag=True, type='mesh', long=True, allPaths=True,
                              noIntermediate=True) or []))


def mesh(path):
    selection = om.MSelectionList()
    selection.add(path)
    dag = selection.getDagPath(0)
    return dag, om.MFnMesh(dag)


def source_material(group):
    """Follow ShellShifter's checker backup to the actual assigned material."""
    seen = set()
    while True:
        fn = om.MFnDependencyNode(group)
        identity = fn.uuid().asString()
        if identity in seen:
            return None
        seen.add(identity)
        if fn.hasAttribute('uvStraightOriginalMaterial'):
            originals = fn.findPlug('uvStraightOriginalMaterial', False).connectedTo(True, False)
            if originals:
                group = originals[0].node()
                continue
        if not fn.hasAttribute('surfaceShader'):
            return None
        sources = fn.findPlug('surfaceShader', False).connectedTo(True, False)
        return om.MFnDependencyNode(sources[0].node()) if sources else None


def material_color(fn):
    for name in ('baseColor', 'color', 'diffuse_color', 'diffuseColor', 'outColor'):
        if not fn.hasAttribute(name):
            continue
        try:
            plug = fn.findPlug(name, False)
            if plug.isCompound and plug.numChildren() >= 3:
                return tuple(max(0., min(1., plug.child(i).asDouble())) for i in range(3))
        except (RuntimeError, TypeError):
            pass
    return (.55, .55, .55)


def mapped_assignments(path, cache):
    dag, fn = mesh(path)
    if om.MFnDagNode(dag).isIntermediateObject:
        return None
    uv_set = fn.currentUVSetName()
    if not uv_set or fn.numUVs(uv_set) == 0:
        return None
    counts, uv_ids = fn.getAssignedUVs(uv_set)
    if not uv_ids:
        return None
    shaders, indices = fn.getConnectedShaders(dag.instanceNumber())
    materials = {}
    for index, group in enumerate(shaders):
        key = om.MFnDependencyNode(group).uuid().asString()
        if key not in cache:
            source = source_material(group)
            cache[key] = ((source.name(), source.uuid().asString(), material_color(source))
                          if source is not None else None)
        materials[index] = cache[key]
    faces = defaultdict(list)
    for face, index in enumerate(indices):
        material = materials.get(index)
        if material is not None and face < len(counts) and counts[face]:
            faces[material].append(face)
    return fn.uuid().asString(), uv_set, counts, uv_ids, faces


def scan(paths=None):
    """Yield small progress steps; return sorted records without selecting anything."""
    paths = mesh_paths() if paths is None else list(paths)
    groups = defaultdict(list)
    cache = {}
    for done, path in enumerate(paths, 1):
        try:
            data = mapped_assignments(path, cache)
            if data is not None:
                uuid, uv_set, _, _, assignments = data
                for material, faces in assignments.items():
                    groups[material].append(Binding(path, uuid, len(faces), uv_set))
        except RuntimeError:
            pass  # A mesh can disappear while a cooperative scan is running.
        yield done, len(paths)
    return tuple(Material(name, uuid, color, tuple(bindings))
                 for (name, uuid, color), bindings in
                 sorted(groups.items(), key=lambda entry: (entry[0][0].casefold(), entry[0][1])))


def read():
    steps = scan()
    while True:
        try:
            next(steps)
        except StopIteration as finished:
            return finished.value


def component_ranges(path, ids):
    ids = sorted(set(ids))
    if not ids:
        return []
    result = []
    first = previous = ids[0]
    for value in ids[1:] + [None]:
        if value is not None and value == previous + 1:
            previous = value
            continue
        suffix = str(first) if first == previous else '{}:{}'.format(first, previous)
        result.append('{}.map[{}]'.format(path, suffix))
        first = previous = value
    return result


def selection(records):
    """Recheck live assignments/current UV sets; stale records cannot select wrong faces."""
    wanted = {record.uuid for record in records}
    identities = {binding.uuid for record in records for binding in record.bindings}
    paths = set()
    for identity in identities:
        paths.update(cmds.ls(identity, long=True, allPaths=True) or [])
    components, transforms, count = [], [], 0
    cache = {}
    for path in sorted(paths):
        try:
            data = mapped_assignments(path, cache)
            if data is None:
                continue
            _, _, counts, uv_ids, assignments = data
            faces = {face for material, members in assignments.items()
                     if material[1] in wanted for face in members}
            if not faces:
                continue
            offsets = [0] + list(accumulate(counts))
            ids = {int(uv_ids[index]) for face in faces
                   for index in range(offsets[face], offsets[face + 1]) if uv_ids[index] >= 0}
            if ids:
                components.extend(component_ranges(path, ids))
                transforms.append(path.rsplit('|', 1)[0])
                count += len(ids)
        except RuntimeError:
            continue
    return components, sorted(set(transforms)), count


def select(records):
    records = tuple(records)
    components, transforms, count = selection(records)
    if records and not components:
        raise ValueError('No matching UVs remain. Refresh the material list.')
    import uv_straight as uv
    with uv._undo('ShellShifter Select Material UVs'):
        if components:
            cmds.selectMode(component=True)
            cmds.selectType(allComponents=False)
            cmds.selectType(polymeshUV=True)
            cmds.hilite(transforms, replace=True)
            cmds.select(components, replace=True)
        else:
            cmds.select(clear=True)
    return count, len(transforms)
