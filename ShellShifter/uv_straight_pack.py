"""Independent shelf/free-rectangle UV packing with explicit gaps and density."""
from bisect import bisect_left, insort
from collections import defaultdict
import math
import numpy as np
import maya.cmds as cmds
import maya.api.OpenMaya as om
from uv_straight_pack_search import counted_trial
from uv_straight_pack_area import area_size, scale_limit

_last_packed=None  # Main-thread scene replay; validated against exact post-commit UVs.


@counted_trial(5)
def _shelves(boxes, order, scale, extent, gap, monitor=None):
    extent_x,extent_y=extent if isinstance(extent,tuple) else (extent,extent)
    rows, capacities, placed = [], [], {}
    bottom = 0.
    for index in order:
        if monitor: monitor.update('Placing shells in rows', len(placed), len(boxes))
        width, height = (value*scale+gap for value in boxes[index])
        if width > extent_x+1e-12 or height > extent_y+1e-12:
            return None
        position = bisect_left(capacities, (width-1e-12, -1))
        if position < len(capacities):
            _, row_index = capacities.pop(position)
            row = rows[row_index]
        else:
            if bottom+height > extent_y+1e-12:
                return None
            row_index = len(rows)
            row = [bottom, height, 0.]
            rows.append(row)
            bottom += height
        placed[index] = (row[2], row[0])
        row[2] += width
        insort(capacities, (max(0., extent_x-row[2]), row_index))
    return placed


def layout_boxes(boxes, gap, margin, monitor=None, target_size=(1.,1.)):
    """Plan without scene edits; pad each box so shell gaps are guaranteed."""
    target_size=area_size(target_size)
    if not boxes:
        return 1., {}
    if (not math.isfinite(gap) or gap < 0 or not math.isfinite(margin) or
            not 0 <= margin < min(target_size)*.5 or any(not all(math.isfinite(v) and v > 0 for v in box)
                                       for box in boxes)):
        raise ValueError('Invalid shell bounds or packing spacing.')
    extent = tuple(v-2.*margin+gap for v in target_size)
    order = sorted(range(len(boxes)), key=lambda i: (-boxes[i][1], -boxes[i][0], i))
    low = 0.
    placed = _shelves(boxes, order, low, extent, gap, monitor)
    if placed is None:
        raise ValueError('Cannot fit shells with the requested pixel spacing. Reduce spacing or increase texture size.')
    high = scale_limit(boxes,target_size,margin)
    candidate = _shelves(boxes, order, high, extent, gap, monitor)
    if candidate is not None:
        low, placed = high, candidate
        if monitor: monitor.consider(low, {i:(x+margin,y+margin) for i,(x,y) in placed.items()})
    else:
        # Twelve bounded trials avoid native layout mutations and gap retries.
        for _ in range(12):
            middle = (low+high)*.5
            candidate = _shelves(boxes, order, middle, extent, gap, monitor)
            if candidate is None:
                high = middle
            else:
                low, placed = middle, candidate
                if monitor: monitor.consider(low, {i:(x+margin,y+margin) for i,(x,y) in placed.items()})
    if low <= 0:
        raise ValueError('Cannot fit shells with the requested pixel spacing. Reduce spacing or increase texture size.')
    return low, {index: (x+margin, y+margin) for index, (x, y) in placed.items()}


def _shell_records(meshes, normalize_density=True, selected_only=False, allowed_owners=None,
                   progress=None, *, group_assignments=None, density_reference=1.):
    import numpy as np
    records = []
    group_assignments = group_assignments or {}
    for mesh_index, mesh in enumerate(meshes):
        if progress: progress('Measuring UV shells...', mesh_index, len(meshes))
        if allowed_owners is not None and not allowed_owners[id(mesh)]:continue
        if len(mesh.faces) != mesh.fn.numPolygons:
            raise ValueError('Packing requires UVs on every polygon: '+mesh.name)
        points = mesh.fn.getPoints(om.MSpace.kWorld)
        xyz = np.asarray([(point.x, point.y, point.z) for point in points], dtype=float)
        counts, indices = mesh.fn.getTriangles()
        triangles = np.asarray(indices, dtype=np.int32).reshape(-1, 3)
        cross = np.cross(xyz[triangles[:, 1]]-xyz[triangles[:, 0]],
                         xyz[triangles[:, 2]]-xyz[triangles[:, 0]])
        areas = .5*np.linalg.norm(cross, axis=1)
        face_ids = np.repeat(np.arange(len(counts)), np.asarray(counts, dtype=np.int32))
        world_areas = np.bincount(face_ids, weights=areas, minlength=mesh.fn.numPolygons)
        uv = np.asarray([mesh.points[i] for i in range(len(mesh.points))], dtype=float)
        uv_areas = np.zeros(len(mesh.faces))
        face_groups = {}
        for face_index, face in enumerate(mesh.faces):
            face_groups.setdefault(len(face), []).append((face_index, face))
        for group in face_groups.values():
            vertices = np.asarray([face for _, face in group], dtype=np.int32)
            coords = uv[vertices]
            following = np.roll(coords, -1, axis=1)
            area = .5*np.abs(np.sum(coords[:, :, 0]*following[:, :, 1] -
                                   following[:, :, 0]*coords[:, :, 1], axis=1))
            uv_areas[[index for index, _ in group]] = area
        owners = np.asarray([mesh.shell_ids[face[0]] for face in mesh.faces], dtype=np.int32)
        world_by_shell = np.bincount(owners, weights=world_areas)
        uv_by_shell = np.bincount(owners, weights=uv_areas)
        assigned_shells = set(owners)
        texel_groups = group_assignments.get(id(mesh), {}).get('texel', {})
        for shell in (mesh.shells() if selected_only else mesh.all_shells()):
            ids = sorted(shell)
            owner = mesh.shell_ids[ids[0]]
            if allowed_owners is not None and owner not in allowed_owners[id(mesh)]:
                continue
            if owner not in assigned_shells:
                continue  # Unassigned UV points have no polygon surface to pack.
            surface, mapped = world_by_shell[owner], uv_by_shell[owner]
            minimum, maximum = np.min(uv[ids], axis=0), np.max(uv[ids], axis=0)
            if mapped<=1e-24 and np.array_equal(minimum,maximum):
                continue  # A fully collapsed point has no packing footprint.
            if not math.isfinite(surface+mapped) or surface <= 1e-24 or mapped <= 1e-24:
                raise ValueError('Cannot pack a collapsed or zero-area shell on '+mesh.name)
            group = texel_groups.get(owner)
            normalize = math.sqrt(surface/mapped) if normalize_density or group is not None else 1.
            if group is not None:
                normalize *= group['density'] / density_reference
            bounds = tuple((maximum-minimum)*normalize)
            records.append((mesh, ids, tuple(minimum), normalize, bounds))
    return records


def _split_free(free, occupied):
    """Subtract a padded rectangle, retaining maximal free rectangles."""
    ox, oy, ow, oh = occupied
    result = []
    for x, y, w, h in free:
        if ox >= x+w-1e-12 or ox+ow <= x+1e-12 or oy >= y+h-1e-12 or oy+oh <= y+1e-12:
            result.append((x, y, w, h))
            continue
        if ox > x+1e-12:
            result.append((x, y, ox-x, h))
        if ox+ow < x+w-1e-12:
            result.append((ox+ow, y, x+w-ox-ow, h))
        if oy > y+1e-12:
            result.append((x, y, w, oy-y))
        if oy+oh < y+h-1e-12:
            result.append((x, oy+oh, w, y+h-oy-oh))
    unique = list(dict.fromkeys(result))
    return [rect for i, rect in enumerate(unique) if not any(
        i != j and rect[0] >= other[0]-1e-12 and rect[1] >= other[1]-1e-12 and
        rect[0]+rect[2] <= other[0]+other[2]+1e-12 and
        rect[1]+rect[3] <= other[1]+other[3]+1e-12
        for j, other in enumerate(unique))]


@counted_trial(7)
def _rectangles(boxes, order, scale, extent, gap, rotate, obstacles, monitor=None):
    extent_x,extent_y=extent if isinstance(extent,tuple) else (extent,extent)
    cached=getattr(monitor,'rectangle_seed',None) if monitor is not None else None
    if cached is not None and cached[0] is obstacles and cached[1]==(extent_x,extent_y):
        free=cached[2]
        monitor.rectangle_cache_hits+=1
    else:
        free = [(0., 0., extent_x, extent_y)]
        for index,obstacle in enumerate(obstacles):
            if monitor is not None and index%8==0:monitor.check()
            free = _split_free(free, obstacle)
        if monitor is not None:
            monitor.check()
            monitor.rectangle_seed=(obstacles,(extent_x,extent_y),tuple(free))
            monitor.rectangle_cache_builds+=1
    placed, rotations = {}, {}
    for index in order:
        if monitor: monitor.update('Filling free rectangles', len(placed), len(boxes))
        width, height = boxes[index]
        best = None
        from uv_straight_contour_pack import rotation_options
        options=tuple(dict.fromkeys(bool(turn%2) for turn in rotation_options((width,height),rotate)))
        for turned in options:
            w, h = ((height, width) if turned else (width, height))
            w, h = w*scale+gap, h*scale+gap
            for x, y, fw, fh in free:
                if w <= fw+1e-12 and h <= fh+1e-12:
                    # Choose the tightest short-side fit, then the longest side.
                    candidate = (min(fw-w, fh-h), max(fw-w, fh-h), y, x, turned, w, h)
                    if best is None or candidate < best:
                        best = candidate
        if best is None:
            return None
        _, _, y, x, turned, w, h = best
        placed[index], rotations[index] = (x, y), turned
        free = _split_free(free, (x, y, w, h))
    return placed, rotations


def layout_rectangles(boxes, gap, margin, rotate=False, obstacles=(), monitor=None, target_size=(1.,1.)):
    """Independent free-rectangle placement with optional right-angle rotation."""
    target_size=area_size(target_size)
    if not boxes:
        return 1., {}, {}
    if (not math.isfinite(gap) or gap < 0 or not math.isfinite(margin) or
            not 0 <= margin < min(target_size)*.5 or any(not all(math.isfinite(v) and v > 0 for v in box)
                                       for box in boxes)):
        raise ValueError('Invalid shell bounds or packing spacing.')
    extent = tuple(v-2.*margin+gap for v in target_size)
    padded_obstacles = [(x-margin, y-margin, w+gap, h+gap) for x, y, w, h in obstacles]
    order = sorted(range(len(boxes)), key=lambda i: (-max(boxes[i]), -boxes[i][0]*boxes[i][1], i))
    low, placed, turns = 0., None, None
    # Keep the fast shelf result as a quality floor when there are no obstacles.
    if not obstacles:
        try:
            turns={i:bool(rotate and h>w) for i,(w,h) in enumerate(boxes)}
            if monitor: monitor.default_turns=turns
            sizes=[(h,w) if turns[i] else (w,h) for i,(w,h) in enumerate(boxes)]
            low, placed = layout_boxes(sizes, gap, margin, monitor, target_size)
            placed = {i: (x-margin, y-margin) for i, (x, y) in placed.items()}
        except ValueError:
            pass
    high = scale_limit(boxes,target_size,margin,rotate)
    candidate = _rectangles(boxes, order, high, extent, gap, rotate, padded_obstacles, monitor)
    if candidate is not None and high >= low:
        low, (placed, turns) = high, candidate
        if monitor: monitor.consider(low, {i:(x+margin,y+margin) for i,(x,y) in placed.items()}, turns)
    else:
        for _ in range(12):
            middle = (low+high)*.5
            candidate = _rectangles(boxes, order, middle, extent, gap, rotate, padded_obstacles, monitor)
            if candidate is None:
                high = middle
            else:
                low, (placed, turns) = middle, candidate
                if monitor: monitor.consider(low, {i:(x+margin,y+margin) for i,(x,y) in placed.items()}, turns)
    if low <= 0 or placed is None:
        raise ValueError('Cannot fit shells with the requested pixel spacing. Reduce spacing or increase texture size.')
    return low, {i: (x+margin, y+margin) for i, (x, y) in placed.items()}, turns


def _other_boxes(meshes, packed=None, face_masks=None):
    result = []
    for mesh in meshes:
        selected = (packed.get(id(mesh),set()) if packed is not None else
                    {mesh.shell_ids[i] for i in mesh.selected})
        assigned = {mesh.shell_ids[face[0]] for face in mesh.faces}
        for shell in mesh.all_shells():
            owner = mesh.shell_ids[next(iter(shell))]
            if owner in selected or owner not in assigned:
                continue
            if face_masks is not None:
                shell = shell & face_masks[id(mesh)][1]
                if not shell:continue
            points = [mesh.points[i] for i in shell]
            lo = [min(p[axis] for p in points) for axis in (0, 1)]
            hi = [max(p[axis] for p in points) for axis in (0, 1)]
            if not all(math.isfinite(value) for value in lo+hi):
                raise ValueError('Unselected shells have invalid UV coordinates.')
            result.append((lo[0], lo[1], hi[0]-lo[0], hi[1]-lo[1]))
    return result


def _uv_face_group_mode():
    """Native UV Editor uses face groups for Isolate Select / shader views."""
    editors=cmds.getPanel(scriptType='polyTexturePlacementPanel') or []
    focus=cmds.getPanel(withFocus=True)
    editors=[editor for editor in editors if cmds.textureWindow(editor,exists=True)]
    editors=sorted(editors,key=lambda editor:editor!=focus)
    for editor in editors:
        control=cmds.textureWindow(editor,query=True,control=True)
        if editor==focus or (control and not cmds.control(control,query=True,isObscured=True)):
            return bool(cmds.textureWindow(editor,query=True,useFaceGroup=True))
    return False


def _visible_faces(path, face_group, include_groups=True, group_snapshot=None):
    if not path.isVisible():return frozenset()
    fn=om.MFnMesh(path)
    faces=set(range(fn.numPolygons))-set(fn.getInvisibleFaces())
    if face_group:
        requested=fn.findPlug('displayFacesWithGroupId',False).asInt()
        groups=fn.findPlug('instObjGroups',False).elementByLogicalIndex(path.instanceNumber()).child(fn.attribute('objectGroups'))
        shown=set()
        for index in range(groups.numElements()):
            group=groups.elementByPhysicalIndex(index)
            if group.child(fn.attribute('objectGroupId')).asInt()!=requested:continue
            data=om.MFnComponentListData(group.child(fn.attribute('objectGrpCompList')).asMObject())
            for item in range(data.length()):
                component=data.get(item)
                if not component.hasFn(om.MFn.kMeshPolygonComponent):continue
                indices=om.MFnSingleIndexedComponent(component)
                shown.update(range(indices.getCompleteData()) if indices.isComplete else indices.getElements())
        faces &= shown
    if include_groups:
        from uv_straight_groups import hidden_faces
        faces-=hidden_faces(path,group_snapshot)
    return frozenset(faces)


def _face_uvs(mesh, faces):
    counts,ids=mesh.fn.getAssignedUVs(mesh.uv_set)
    result=set();offset=0
    for face,count in enumerate(counts):
        if face in faces:result.update(ids[offset:offset+count])
        offset+=count
    return frozenset(result)


def _triangles_by_shell(mesh, material_faces=None, points=None, owners=None, check=None):
    """Use Maya's actual polygon triangulation, preserving UV seams."""
    counts, vertices = mesh.fn.getTriangles()
    points=mesh.points if points is None else points
    faces=list(zip(mesh.faces,mesh.face_vertices))
    if len(faces)!=mesh.fn.numPolygons:
        # Fixed objects may contain unmapped faces. Keep actual polygon indices
        # aligned with getTriangles rather than shifting later UV assignments.
        faces=[None]*mesh.fn.numPolygons
        iterator=om.MItMeshPolygon(mesh.path)
        while not iterator.isDone():
            try:
                ids=tuple(iterator.getUVIndex(i,mesh.uv_set)
                          for i in range(iterator.polygonVertexCount()))
                faces[iterator.index()]=(ids,tuple(iterator.getVertices()))
            except RuntimeError:pass
            iterator.next()
    offset, result = 0, {}
    for face_index,(polygon,count) in enumerate(zip(faces,counts)):
        if check is not None and face_index%128==0:check()
        end=offset+count*3
        if polygon is None or (material_faces is not None and face_index not in material_faces):
            offset=end
            continue
        face,corners=polygon
        mapping = dict(zip(corners, face))
        owner = mesh.shell_ids[face[0]]
        if owners is not None and owner not in owners:
            offset=end;continue
        group = result.setdefault(owner, [])
        for _ in range(count):
            group.append(tuple(points[mapping[v]] for v in vertices[offset:offset+3]))
            offset += 3
    return result


def _triangle_area(triangle):
    a,b,c = triangle
    return abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))*.5


def prepare(meshes, resolution, spacing_px, margin_px=20, normalize_density=True,
            selected_only=False, rotate=False, method='fast', around_others=False,
            seconds=30., precision=1024, keep_stacks=False, gpu=False, target_size=(1.,1.), shape_proxies=False, spread_small=False, preserve_source=False,
            target_origin=(0.,0.), iteration_limit=0,base_density=500., progress=None, cpu_assist=False, reuse_preparation=True):
    """Scene reads on the caller thread; the data dictionary is worker-safe.

    With selected_only and around_others, reserve visible unselected shells on
    the selected meshes. Unrelated scene meshes never occupy packing space.
    """
    global _last_packed
    if not math.isfinite(base_density) or base_density<=0:raise ValueError('Base texel density must be positive.')
    target_size=area_size(target_size)
    if len(target_origin)!=2 or not all(math.isfinite(v) for v in target_origin):raise ValueError('Use a finite UV target origin.')
    target_origin=tuple(float(v) for v in target_origin)
    if not isinstance(iteration_limit,int) or iteration_limit<0:raise ValueError('Iteration limit must be a non-negative integer.')
    short_resolution=resolution*min(target_size)
    if (not math.isfinite(resolution) or not 2 <= resolution <= 8192 or
            not all(math.isfinite(v) and v >= 0 for v in (spacing_px, margin_px)) or
            margin_px*2 >= short_resolution or spacing_px >= short_resolution*.5):
        raise ValueError('Use a texture size from 2 to 8192 and smaller non-negative spacing and margin.')
    if method not in ('fast', 'compact', 'contours'):
        raise ValueError('Unknown packing method.')
    if gpu and method!='contours':
        raise ValueError('GPU packing requires the contour method.')
    if not math.isfinite(seconds) or seconds < 0 or precision not in (512,1024,2048,4096,8192):
        raise ValueError('Use a non-negative search time (0 is unlimited) and a supported contour precision.')
    if seconds == 0 and method != 'contours':
        raise ValueError('Unlimited search requires contour packing.')
    mesh_keys = [om.MObjectHandle(mesh.fn.object()).hashCode() for mesh in meshes]
    if len(set(mesh_keys)) != len(mesh_keys):
        raise ValueError('Select only one instance of each mesh; instances share the same UV coordinates.')
    from uv_straight_materials import packing_materials, packing_mask
    materials=packing_materials(meshes) if selected_only else None
    import uv_straight_groups as uv_groups
    group_state=uv_groups.fingerprint()
    group_snapshot=uv_groups.snapshot(group_state)
    # Large preparations contain topology, contours and several UV snapshots.
    keep_replay = reuse_preparation and sum(len(mesh.points) + len(mesh.faces) for mesh in meshes) <= 50000
    reuse_key=((resolution,spacing_px,margin_px,normalize_density,selected_only,rotate,method,
                around_others,precision,keep_stacks,gpu,target_size,shape_proxies,spread_small,
                preserve_source,target_origin,base_density,bool(cpu_assist)),materials,group_state,
               tuple((mesh.name,mesh.fn.uuid().asString(),mesh.uv_set,frozenset(mesh.selected)) for mesh in meshes)) if keep_replay else None
    if reuse_key is not None and _last_packed is not None and _last_packed['reuse_key']==reuse_key:
        try:validate(_last_packed)
        except ValueError:pass
        else:
            replay=dict(_last_packed);replay['data']=dict(replay['data'])
            replay['data'].update(seconds=seconds,iteration_limit=iteration_limit)
            replay['preparation_cache_hit']=True
            return replay
    _last_packed = None
    if progress: progress('Reading shell visibility...')
    face_masks=None
    allowed_owners=None
    face_group=_uv_face_group_mode() if selected_only else False
    visibility_faces=None
    material_faces=None
    if selected_only:
        visibility_faces={}
        for mesh_index, mesh in enumerate(meshes):
            if progress: progress('Reading shell visibility...', mesh_index, len(meshes))
            visibility_faces[mesh.name]=_visible_faces(mesh.path,face_group,group_snapshot=group_snapshot)
        face_masks={id(mesh):(visibility_faces[mesh.name],_face_uvs(mesh,visibility_faces[mesh.name])) for mesh in meshes}
        allowed_owners={}
        for mesh in meshes:
            faces=visibility_faces[mesh.name]
            visible={mesh.shell_ids[face[0]] for i,face in enumerate(mesh.faces) if i in faces}
            hidden={mesh.shell_ids[face[0]] for i,face in enumerate(mesh.faces) if i not in faces}
            if {mesh.shell_ids[i] for i in mesh.selected} & visible & hidden:
                raise ValueError('A selected UV shell crosses hidden faces. Show the whole shell before packing.')
            allowed_owners[id(mesh)]=visible-hidden
    if materials is not None:
        cache={}
        material_faces={}
        for mesh_index, mesh in enumerate(meshes):
            if progress: progress('Reading material assignments...', mesh_index, len(meshes))
            faces,ids=packing_mask(mesh.name,materials,cache)
            material_faces[mesh.name]=faces
            # A shell crossing a material boundary cannot be packed separately
            # without moving the other material or tearing its UV chart.
            owners={mesh.shell_ids[face[0]] for i,face in enumerate(mesh.faces) if i in faces}
            owners-={mesh.shell_ids[face[0]] for i,face in enumerate(mesh.faces) if i not in faces}
            if {mesh.shell_ids[i] for i in mesh.selected} & (allowed_owners[id(mesh)] - owners):
                raise ValueError('A selected UV shell spans materials. Separate its UVs at the material boundary or select all its materials.')
            allowed_owners[id(mesh)] &= owners
            shown_faces,shown_ids=face_masks[id(mesh)]
            face_masks[id(mesh)]=(shown_faces & faces,shown_ids & ids)
    assignments = uv_groups.resolve(meshes) if group_state else {}
    # With Density off, use actual px/unit targets at the packing resolution.
    # With Density on, ungrouped shells normalize to the base density.
    density_reference = base_density if normalize_density else resolution
    records = _shell_records(meshes, normalize_density, selected_only, allowed_owners, progress,
                             group_assignments=assignments, density_reference=density_reference)
    if not records:
        raise ValueError('Select visible UV shells with a valid polygon surface.')
    triangles_by_mesh = {}
    for mesh_index, mesh in enumerate(meshes):
        if progress: progress('Reading shell outlines...', mesh_index, len(meshes))
        triangles_by_mesh[id(mesh)]=_triangles_by_shell(mesh)
    from uv_straight_pack_prepare import PreparationCPU
    with PreparationCPU(progress,enabled=len(records)>=512) as numerical:
        overlap_labels={}
        if keep_stacks:
            scope=[(mesh,owner) for mesh in meshes for owner in triangles_by_mesh[id(mesh)]
                   if allowed_owners is None or owner in allowed_owners[id(mesh)]]
            scoped_groups=numerical.overlap_groups([triangles_by_mesh[id(mesh)][owner] for mesh,owner in scope])
            overlap_labels={(id(scope[i][0]),scope[i][1]):label
                            for label,indices in enumerate(scoped_groups) for i in indices}
        if keep_stacks and selected_only:
            selected_owners={(id(mesh),mesh.shell_ids[ids[0]]) for mesh,ids,*_ in records}
            selected_labels={overlap_labels[owner] for owner in selected_owners}
            expanded={owner for owner,label in overlap_labels.items() if label in selected_labels}
            previous={id(mesh):mesh.selected for mesh in meshes}
            try:
                if expanded!=selected_owners:
                    for mesh in meshes:
                        mesh.selected={i for shell in mesh.all_shells()
                                       if (id(mesh),mesh.shell_ids[next(iter(shell))]) in expanded for i in shell}
                    records = _shell_records(meshes, normalize_density, True, allowed_owners, progress,
                                             group_assignments=assignments, density_reference=density_reference)
            finally:
                for mesh in meshes:mesh.selected=previous[id(mesh)]
        packed={id(mesh):set() for mesh in meshes}
        for mesh,ids,*_ in records:packed[id(mesh)].add(mesh.shell_ids[ids[0]])
        parents=list(range(len(records)))
        def root(i):
            while parents[i]!=i:
                parents[i]=parents[parents[i]];i=parents[i]
            return i
        def merge(indices):
            for i in indices[1:]:parents[root(i)]=root(indices[0])
        stacks=defaultdict(list);rigid=defaultdict(list)
        texel_assignments=[];shell_assignments=[]
        for i,(mesh,ids,*_) in enumerate(records):
            owner=mesh.shell_ids[ids[0]];resolved=assignments.get(id(mesh),{})
            stacks[overlap_labels[(id(mesh),owner)] if keep_stacks else i].append(i)
            texel_assignments.append(resolved.get('texel',{}).get(owner))
            group=resolved.get('pack',{}).get(owner);shell_assignments.append(group)
            if group is not None and group.get('mode','relative')=='absolute':rigid[group['id']].append(i)
        for indices in list(stacks.values())+list(rigid.values()):merge(indices)
        grouped=defaultdict(list)
        for i in range(len(records)):grouped[root(i)].append(i)
        members=list(grouped.values())
        pack_memberships={}
        for group_index,indices in enumerate(members):
            for i in indices:
                group=shell_assignments[i]
                if group is not None and group.get('mode','relative')=='relative':
                    entry=pack_memberships.setdefault(group['id'],dict(id=group['id'],indices=[],strength=.85))
                    if group_index not in entry['indices']:entry['indices'].append(group_index)
        triangles, areas, boxes, record_groups = [], [], [], [0]*len(records)
        adjusted=list(records)
        group_inputs=[]
        for indices in members:
            sources=[triangles_by_mesh[id(records[i][0])][records[i][0].shell_ids[records[i][1][0]]]
                     for i in indices]
            group_inputs.append((sources,[records[i][3] for i in indices],
                                 [texel_assignments[i]['id'] if texel_assignments[i] is not None else None for i in indices],
                                 True))  # Texel targets are active in both Density modes.
        geometry=numerical.groups(group_inputs)
        for group_index,(indices,values) in enumerate(zip(members,geometry)):
            minimum,normalize,bounds,local,area=values
            triangles.append(local);areas.append(area);boxes.append(bounds)
            for i in indices:
                mesh,ids,*_=records[i]
                adjusted[i]=(mesh,ids,minimum,normalize,bounds)
                record_groups[i]=group_index
    static = []
    obstacle_meshes=list(meshes)
    if selected_only and around_others:
        for mesh in obstacle_meshes:
            selected = packed.get(id(mesh),set())
            sources=triangles_by_mesh.get(id(mesh)) if face_masks is None else None
            if sources is None:
                sources=_triangles_by_shell(mesh,None if face_masks is None else face_masks[id(mesh)][0])
            for owner, group in sources.items():
                if owner in selected:continue
                if not all(math.isfinite(value) for tri in group for point in tri for value in point):
                    raise ValueError('Unselected shells have invalid UV coordinates: '+mesh.name)
                # Numerical search always uses local tile coordinates. Fixed
                # scene UVs must use the same origin as the chosen target.
                static.extend(tuple((x-target_origin[0],y-target_origin[1]) for x,y in tri) for tri in group)
    static=[tri for tri in static if all(max(p[a] for p in tri)>=0. and min(p[a] for p in tri)<=target_size[a] for a in (0,1))]
    obstacles=[]
    if selected_only and around_others:
        for x,y,w,h in _other_boxes(obstacle_meshes,packed,face_masks):
            x-=target_origin[0];y-=target_origin[1]
            if x+w>=0 and y+h>=0 and x<=target_size[0] and y<=target_size[1]:obstacles.append((x,y,w,h))
    # Clip static triangles to the tile for the free-space estimate.
    from uv_straight_contour_pack import _clip_y
    static_area = 0.
    for triangle in static:
        polygon = list(triangle)
        for axis in (0,1):
            if axis == 0: polygon = [(y,x) for x,y in polygon]
            polygon = _clip_y(polygon,0.,True) if polygon else []
            polygon = _clip_y(polygon,target_size[axis],False) if polygon else []
            if axis == 0: polygon = [(y,x) for x,y in polygon]
        if polygon:
            static_area += abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in
                                   zip(polygon,polygon[1:]+polygon[:1])))*.5
    data = dict(boxes=boxes, triangles=triangles, areas=areas, shell_count=len(records),
                stack_groups=sum(len(indices)>1 for indices in stacks.values()),
                stacked_shells=sum(len(indices)-1 for indices in stacks.values()),
                fixed_shell_groups=sum(len(indices)>1 for indices in rigid.values()),
                gap=spacing_px/float(resolution), margin=margin_px/float(resolution),
                rotate=rotate, method=method, seconds=seconds, precision=precision, gpu=gpu, cpu_assist=bool(cpu_assist), target_size=target_size,
                obstacles=obstacles,target_origin=target_origin,iteration_limit=iteration_limit,
                static_triangles=static, static_area=static_area,
                shape_proxies=bool(shape_proxies), spread_small=bool(spread_small), resolution=resolution,
                # Density controls per-shell normalization, not global scale.
                # The old packer uniformly scaled the full set in either mode.
                preserve_scale=False,normalize_density=bool(normalize_density))
    data['pack_groups']=[g for g in pack_memberships.values() if len(g['indices'])>1]
    density_meshes = {id(mesh) for (mesh, *_), group in zip(records, texel_assignments)
                      if normalize_density or group is not None}
    snapshots=[]
    for mesh_index, mesh in enumerate(obstacle_meshes):
        if progress: progress('Saving UV state...', mesh_index, len(obstacle_meshes))
        snapshots.append((mesh.name,mesh.uv_set,dict(mesh.points),mesh.fn.numPolygons,
                          tuple(tuple(values) for values in mesh.fn.getAssignedUVs(mesh.uv_set)),
                          tuple(tuple(values) for values in mesh.fn.getVertices()),
                          tuple((p.x,p.y,p.z) for p in mesh.fn.getPoints(om.MSpace.kWorld))
                          if id(mesh) in density_meshes else None))
    return dict(records=adjusted, data=data, record_groups=record_groups,preserve_source=bool(preserve_source),target_origin=target_origin,
                reuse_key=reuse_key,preparation_cache_hit=False,
                preparation_workers=numerical.used_workers,preparation_cpu_error=numerical.error,
                materials=materials,group_state=group_state,
                material_faces=material_faces,
                face_group_mode=face_group,visibility_faces=visibility_faces,
                snapshots=snapshots)


def plan(data, callback=None, cancel=None, finish_cancel=None):
    """Pure numerical search: never access Maya or widgets from this function."""
    import time
    from copy import deepcopy
    from uv_straight_pack_cache import acquire
    from uv_straight_pack_search import SearchMonitor, SearchStopped
    started=time.monotonic()
    session=acquire(data)
    data=dict(data);data.update(session.geometry)
    monitor = SearchMonitor(data['areas'],data['static_area'],callback,cancel,data['seconds'],data.get('target_size',(1.,1.)),data.get('iteration_limit',0))
    monitor.search_upgrade=bool(data.get('search_upgrade',True))
    monitor.tabi_seed=bool(data.get('tabi_seed',True))
    monitor.pack_groups=data.get('pack_groups',[])
    monitor.pack_boxes=data['boxes']
    monitor.resumed=session.best is not None
    monitor.resume_rounds=session.rounds
    if monitor.resumed:
        session.restore(monitor)
    monitor.shape_resolution=data.get('resolution',2048)
    monitor.tail_size=int(data.get('tail_size',0))
    if not 0<=monitor.tail_size<=32:raise ValueError('Tail size must be between 0 and 32.')
    monitor.pocket_lookahead=bool(data.get('pocket_lookahead'))
    assistant=None;cpu_error=None
    backend=None;saved_best=deepcopy(monitor.best)
    saved_pool=deepcopy(monitor.pool);saved_failures=deepcopy(monitor.failed_orders)
    try:
        if data.get('static_triangles'):
            from uv_straight_pack_metrics import union_area
            monitor.update('Measuring occupied UV area',force=True)
            started=time.monotonic()
            def check_area_cancel():
                if cancel is not None and cancel.is_set():raise SearchStopped()
            monitor.static_area=union_area(data['static_triangles'],4096,monitor.target_size,check_area_cancel)
            monitor.static_area_method='scanline_union'
            if data['seconds']:monitor.deadline+=time.monotonic()-started
        if data.get('shape_proxies'):
            from uv_straight_pack_shapes import classify
            monitor.shape_profiles=classify(data['boxes'],data['triangles'],monitor)
        if data.get('gpu') and data['method'] == 'contours':
            monitor.update('Connecting to GPU…',force=True)
            from uv_straight_pack_gpu import OpenCLPlacement
            steps=int(data.get('sequence_steps',1))
            if steps not in (1,2,4,8):raise ValueError('Sequence steps must be 1, 2, 4 or 8.')
            backend=session.backend
            if backend is None:
                backend=OpenCLPlacement(profiling=bool(data.get('profiling')),experimental=bool(steps>1 or monitor.tail_size))
                session.backend=backend
            else:backend.begin_run()
            backend.sequence_steps=steps
            backend.batch_preparation=bool(data.get('batch_preparation',True))
            backend.hierarchy=bool(data.get('hierarchy',True))
            backend.interval_order=int(data.get('interval_order',1))
            if backend.interval_order not in (0,1):raise ValueError('Interval order must be 0 or 1.')
            backend.shape_profiles=getattr(monitor,'shape_profiles',None)
            backend.shape_resolution=monitor.shape_resolution
            # Driver/program setup must not consume a finite numerical search
            # budget and return an empty result before any shell was tried.
            if data['seconds']:monitor.deadline=time.monotonic()+data['seconds']
            monitor.engine='GPU · '+backend.name+(' · Resumed' if monitor.resumed else '')
            monitor.update('GPU ready',force=True)
        if data['rotate']:
            monitor.horizontal_axes=tuple((i,w>=h) for i,(w,h) in enumerate(data['boxes'])
                                         if max(w,h)>=4*min(w,h))
        if (backend is not None and data.get('cpu_assist') and not data.get('preserve_scale')
                and not monitor.iteration_limit and len(data['boxes'])>=12
                and (not data['seconds'] or data['seconds']>=5)):
            from uv_straight_pack_cpu import CPUAssist
            try:
                assistant=CPUAssist(data,monitor,workers=data.get('cpu_workers'))
                monitor.external_poll=assistant.poll
                monitor.external_finish=assistant.finish
                monitor.engine+=' + CPU'
            except OSError as error:cpu_error=str(error)
        setup_seconds=time.monotonic()-started
        search_started=time.monotonic()
        result=_plan(data,monitor,cancel,backend)
        search_seconds=time.monotonic()-search_started
        saved_best=deepcopy(monitor.best)
        saved_pool=deepcopy(monitor.pool);saved_failures=deepcopy(monitor.failed_orders)
        if data.get('spread_small') and not result.get('cancelled'):
            from uv_straight_pack_spread import redistribute
            def spread_progress(state):
                if callback:callback(dict(iteration=monitor.iteration,free_percent=result['free_percent'],
                                          engine=monitor.engine,**state))
            redistribute(data,result,finish_cancel if finish_cancel is not None else cancel,spread_progress)
            result['elapsed']=time.monotonic()-monitor.started
        if data.get('pack_groups') and not result.get('cancelled'):
            from uv_straight_pack_groups import cluster
            cluster(data,result,finish_cancel if finish_cancel is not None else cancel,callback)
            result['elapsed']=time.monotonic()-monitor.started
        _orient_square_layout(data,result)
        from uv_straight_pack_metrics import finish_area
        finish_area(data,result,cancel)
        result['static_union_area']=monitor.static_area
        result['engine']=monitor.engine or 'CPU'
        if cpu_error or (assistant is not None and assistant.error):
            result['cpu_error']=cpu_error or assistant.error
        result.update(resumed=monitor.resumed,session_cache_hit=session.hit,
                      setup_seconds=setup_seconds,search_seconds=search_seconds)
        if backend is not None:
            result.update(gpu_dispatches=backend.dispatches,gpu_candidates=backend.candidates,
                    gpu_cache_hits=backend.cache_hits,gpu_cache_bytes=backend.cache_bytes,
                    local_attempts=monitor.local_attempts,fixed_shells=monitor.fixed_shells,
                    fixed_readbacks=backend.fixed_readbacks,
                    preparation_batches=backend.preparation_batches,
                    preparation_readbacks=backend.preparation_readbacks,
                    hierarchy_rebuilds=backend.hierarchy_rebuilds,
                    sequence_batches=backend.sequence_batches,
                    solid_preparations=backend.solid_preparations,
                    rectangle_proxy_preparations=backend.rectangle_proxy_preparations,
                    circle_proxy_preparations=backend.circle_proxy_preparations,
                    shape_proxy_uses=backend.shape_proxy_uses,
                    shape_proxy_fallbacks=backend.shape_proxy_fallbacks)
            if backend.profile is not None:result['profiling']=backend.profiling_statistics()
        if getattr(monitor,'shape_profiles',None) is not None:
            result['shape_profiles']=dict(rectangles=sum(bool(p and 'rectangle_error' in p) for p in monitor.shape_profiles),
                                          circles=sum(bool(p and 'circle' in p) for p in monitor.shape_profiles),
                                          total=len(monitor.shape_profiles))
        return result
    except SearchStopped:
        return dict(cancelled=True,shells=0,elapsed=time.monotonic()-monitor.started,
                    engine=monitor.engine,**monitor.statistics())
    finally:
        monitor.external_poll=monitor.external_finish=None
        if assistant is not None:assistant.close()
        try:session.finish(saved_best,monitor.iteration,saved_pool,saved_failures)
        except BaseException:
            session.close();raise


def _orient_square_layout(data,result):
    """Rotate an unobstructed square as a whole when strips run vertically."""
    width,height=data['target_size']
    if (not data['rotate'] or width!=height or data['obstacles'] or
            data['static_triangles'] or result.get('cancelled') or
            result.get('_certified_contours') or result.get('contact_seed')):
        return
    turns=result['turns']
    horizontal=vertical=0
    for i,(w,h) in enumerate(data['boxes']):
        if max(w,h)<2*min(w,h):continue
        if (w>h) != bool(turns[i]%2):horizontal+=1
        else:vertical+=1
    if vertical<=horizontal:return
    scale=result['scale']
    result['positions']={
        i:(width-y-data['boxes'][i][0 if turns[i]%2 else 1]*scale,x)
        for i,(x,y) in result['positions'].items()
    }
    result['turns']={i:(turn+1)%4 for i,turn in turns.items()}
    if 'horizontal_long' in result:
        result['horizontal_long']=result['long_shells']-result['horizontal_long']


def _certify_box_seed(data, positions, turns):
    """Independently check conservative bounds after Maya's float32 UV write.

    A separated bounding box implies separated triangle unions, including holes
    and intentional stacks inside a pack group. Only unobstructed seeds qualify.
    """
    if data['obstacles'] or data['static_triangles']:return False
    origin=np.asarray(data.get('target_origin',(0.,0.)),dtype=float)
    target=np.asarray(data.get('target_size',(1.,1.)),dtype=float)
    gap=float(data['gap']);margin=float(data['margin']);rects=[]
    if set(positions)!=set(range(len(data['boxes']))):return False
    for i,(w,h) in enumerate(data['boxes']):
        if int(turns.get(i,0))%2:w,h=h,w
        lo=np.asarray(positions[i],dtype=float)
        hi=lo+(w,h)
        # Conversion is monotone: these extrema bound every committed UV.
        lo=(lo+origin).astype(np.float32).astype(float)-origin
        hi=(hi+origin).astype(np.float32).astype(float)-origin
        if not np.all(np.isfinite((lo,hi))) or np.any(lo<margin) or np.any(hi>target-margin):return False
        for other_lo,other_hi in rects:
            separation=np.maximum(np.maximum(lo-other_hi,other_lo-hi),0.)
            if np.all(lo<other_hi) and np.all(other_lo<hi):return False
            if math.hypot(*separation)<gap:return False
        rects.append((lo,hi))
    return True


def _contact_box_seed(data,monitor):
    """Bounded continuous seed; retain contour search if this cannot fit."""
    boxes=data['boxes']
    if len(boxes)>256 or data['obstacles'] or data['static_triangles']:return None
    from uv_straight_contour_pack import _order
    target=tuple(data.get('target_size',(1.,1.)))
    origin=data.get('target_origin',(0.,0.))
    # Reserve float32 rounding room at the actual destination tile, not just 0–1.
    magnitude=max(1.,*(abs(float(v))+max(target) for v in origin))
    guard=8.*float(np.spacing(np.float32(magnitude)))
    gap=data['gap']+2.*guard;margin=data['margin']+guard
    extent=tuple(v-2.*margin+gap for v in target)
    if min(extent)<=0:return None
    obstacles=[]
    for ordering in range(4):
        monitor.check()
        result=_rectangles(boxes,_order(boxes,ordering),1.,extent,gap,data['rotate'],obstacles,monitor)
        if result is None:continue
        positions,turns=result
        positions={i:(x+margin,y+margin) for i,(x,y) in positions.items()}
        if _certify_box_seed(data,positions,turns):return positions,turns
    return None


def _fixed_layout(data,monitor,backend):
    """Search translations/quarter turns at exactly the source UV scale."""
    boxes=data['boxes'];gap=data['gap'];margin=data['margin'];rotate=data['rotate']
    width,height=(v-2*margin for v in monitor.target_size)
    if any(not ((w<=width+1e-12 and h<=height+1e-12) or
                (rotate and h<=width+1e-12 and w<=height+1e-12)) for w,h in boxes):
        raise ValueError('Density is off: a shell does not fit in the selected UV Area at the original scale. UVs were not changed.')
    from uv_straight_contour_pack import _order,trial,trial_many
    method=data['method'];obstacles=data['obstacles'];grid=data['precision']
    extent=tuple(v-2*margin+gap for v in monitor.target_size)
    padded=[(x-margin,y-margin,w+gap,h+gap) for x,y,w,h in obstacles]
    if method=='contours':
        seed=_contact_box_seed(data,monitor)
        if seed is not None:
            monitor.consider(1.,*seed)
            monitor.best['contact_seed']=True
            return
    ordering=0;exact_attempted=False
    while True:
        monitor.check()
        if method=='contours':
            monitor.precision=grid
            if backend is not None and hasattr(backend,'place_many') and len(boxes)>=12:
                orderings=tuple(range(ordering,ordering+4))
                results=trial_many(boxes,data['triangles'],1.,grid,gap,margin,rotate,
                                   data['static_triangles'],monitor,orderings,backend)
                ordering+=4
            else:
                results=[trial(boxes,data['triangles'],1.,grid,gap,margin,rotate,
                               data['static_triangles'],monitor,ordering,backend)]
                ordering+=1
            for result in results:
                if result is not None:monitor.consider(1.,*result)
            if monitor.best is None and not exact_attempted:
                exact_attempted=True
                from uv_straight_pack_refine import fixed_contour_seed
                seed=fixed_contour_seed(data,monitor,seconds=min(.5,(monitor.seconds or 5.)*.1))
                if seed is not None:
                    monitor.consider(1.,*seed)
                    monitor.best['_certified_contours']=True
                    monitor.best['fixed_contour_contacts']=True
                    monitor.best['fixed_neighbor_repairs']=getattr(monitor,'fixed_neighbor_repairs',0)
        elif method=='compact' or obstacles:
            result=_rectangles(boxes,_order(boxes,ordering),1.,extent,gap,rotate,padded,monitor)
            ordering+=1
            if result is not None:
                positions,turns=result
                monitor.consider(1.,{i:(x+margin,y+margin) for i,(x,y) in positions.items()},turns)
        else:
            turns={i:bool(rotate and h>w) for i,(w,h) in enumerate(boxes)}
            sizes=[(h,w) if turns[i] else (w,h) for i,(w,h) in enumerate(boxes)]
            result=_shelves(sizes,_order(sizes,ordering),1.,extent,gap,monitor)
            ordering+=1
            if result is not None:monitor.consider(1.,{i:(x+margin,y+margin) for i,(x,y) in result.items()},turns)
        # Occupied area cannot increase at a locked scale. A complete plan
        # meets the objective; do not spend the remaining time growing it.
        if monitor.best is not None:return


def _plan(data, monitor, cancel, backend):
    import time
    from uv_straight_pack_search import SearchStopped,StageExpired
    boxes,gap,margin = data['boxes'],data['gap'],data['margin']
    rotate,method,obstacles = data['rotate'],data['method'],data['obstacles']
    target_size=monitor.target_size
    try:
        if monitor.best is not None:
            pass  # Resume a complete validated seed instead of rebuilding a floor.
        elif data.get('preserve_scale'):
            _fixed_layout(data,monitor,backend)
        elif method=='contours' and obstacles:
            # Bounding boxes can overfill a tile where the true contours fit.
            # Keep this optional seed short, including obstacle subtraction.
            previous_deadline=monitor.stage_deadline
            monitor.stage_deadline=time.monotonic()+min(2.,(monitor.seconds or 20.)*.1)
            try:layout_rectangles(boxes,gap,margin,rotate,obstacles,monitor,target_size)
            except (StageExpired,ValueError):pass
            finally:monitor.stage_deadline=previous_deadline
        elif method == 'compact' or obstacles:
            # Contours only need a cheap complete seed; avoid a long rectangle
            # search before the contour search on large unobstructed selections.
            layout_rectangles(boxes,gap,margin,rotate,obstacles,monitor,target_size)
        else:
            turns = {i:bool(rotate and h > w) for i,(w,h) in enumerate(boxes)}
            monitor.default_turns = turns
            sizes = [(h,w) if turns[i] else (w,h) for i,(w,h) in enumerate(boxes)]
            layout_boxes(sizes,gap,margin,monitor,target_size)
        if method == 'contours' and not data.get('preserve_scale'):
            if monitor.iteration_limit and monitor.iteration>=monitor.iteration_limit:
                monitor.iteration_limit_reached=True;raise SearchStopped()
            # A raster cell can close an otherwise usable pocket at the source
            # scale. Keep the certified continuous fit as a floor and still
            # let the normal search grow all shells together.
            if (not data.get('normalize_density') and 2<=len(boxes)<=24 and
                    (monitor.best is None or monitor.best['scale']<1.)):
                from uv_straight_pack_area import scale_limit
                from uv_straight_pack_refine import fixed_contour_seed
                if scale_limit(boxes,target_size,margin,rotate,data['areas'])>=1.:
                    seed=fixed_contour_seed(dict(data,preserve_scale=True),monitor,
                                            seconds=min(.5,(monitor.seconds or 5.)*.1))
                    if seed is not None:
                        try:
                            monitor.consider(1.,*seed)
                        finally:
                            monitor.best['_certified_contours']=True
                            monitor.best['fixed_contour_contacts']=True
            from uv_straight_contour_pack import search
            search(boxes,data['triangles'],data['areas'],gap,margin,rotate,
                   data['static_triangles'],monitor,data['precision'],backend)
    except ValueError:
        if method != 'contours' or data.get('preserve_scale'): raise
        # Rectangles may not fit while the actual contours do.
        try:
            from uv_straight_contour_pack import search
            search(boxes,data['triangles'],data['areas'],gap,margin,rotate,
                   data['static_triangles'],monitor,data['precision'],backend)
        except SearchStopped: pass
    except SearchStopped:
        pass
    if monitor.external_finish is not None:
        monitor.external_finish()
        monitor.external_poll=monitor.external_finish=None
    if monitor.best is None:
        if cancel is not None and cancel.is_set():
            return dict(cancelled=True,shells=0,elapsed=time.monotonic()-monitor.started,**monitor.statistics())
        if data.get('preserve_scale'):
            raise ValueError('Density is off: no complete layout fits at the original scale. UVs were not changed. Increase Time/Iters, reduce Gap/Margin, or enable Density to allow scaling.')
        raise ValueError('No complete layout found. Increase search time or iteration limit, or reduce spacing.')
    result = dict(monitor.best)
    result.pop('order',None)
    # Uniformly align an unobstructed completed layout to the requested border.
    # Coarse-cell rounding can reserve an extra pixel; a global translation
    # preserves every inter-shell distance and increases the opposite margins.
    if not obstacles and not data['static_triangles'] and not result.get('contact_seed') and not result.get('_certified_contours'):
        dx=margin-min(x for x,y in result['positions'].values())
        dy=margin-min(y for x,y in result['positions'].values())
        result['positions']={i:(x+min(0.,dx),y+min(0.,dy))
                             for i,(x,y) in result['positions'].items()}
    result.update(target_size=target_size,target_origin=data.get('target_origin',(0.,0.)),shells=data.get('shell_count',len(boxes)),groups=len(boxes),
                  stack_groups=data.get('stack_groups',0),fixed_shell_groups=data.get('fixed_shell_groups',0),
                  stacked_shells=data.get('stacked_shells',data.get('shell_count',len(boxes))-len(boxes)),iterations=monitor.iteration,
                  elapsed=time.monotonic()-monitor.started,
                  stopped=cancel is not None and cancel.is_set(),cancelled=False)
    result.update(monitor.statistics())
    return result


def validate(prepared):
    """Reject stale results if another script changed the scene during search."""
    group_snapshot=None
    if 'group_state' in prepared:
        from uv_straight_groups import fingerprint,snapshot
        if fingerprint()!=prepared['group_state']:
            raise ValueError('UV group settings changed during packing. Run packing again.')
        group_snapshot=snapshot(prepared['group_state'])
    if prepared.get('visibility_faces') is not None:
        if _uv_face_group_mode()!=prepared['face_group_mode']:
            raise ValueError('UV Editor visibility changed during packing. Run packing again.')
        for name,faces in prepared['visibility_faces'].items():
            try:
                selection=om.MSelectionList();selection.add(name)
                unchanged=_visible_faces(selection.getDagPath(0),prepared['face_group_mode'],group_snapshot=group_snapshot)==faces
            except (RuntimeError,ValueError):unchanged=False
            if not unchanged:
                raise ValueError('Mesh visibility changed during packing. Run packing again.')
    if prepared.get('material_faces') is not None:
        from uv_straight_materials import packing_mask
        cache={}
        for name,faces in prepared['material_faces'].items():
            try:unchanged=packing_mask(name,prepared['materials'],cache)[0]==faces
            except (RuntimeError,ValueError):unchanged=False
            if not unchanged:
                raise ValueError('Material assignments changed during packing. Run packing again.')
    for name, uv_set, points, polygons, assignments, vertices, world in prepared['snapshots']:
        try:
            selection = om.MSelectionList(); selection.add(name)
            fn = om.MFnMesh(selection.getDagPath(0))
            u,v = fn.getUVs(uv_set)
            unchanged = (fn.currentUVSetName() == uv_set and fn.numPolygons == polygons and
                         tuple(tuple(values) for values in fn.getAssignedUVs(uv_set)) == assignments and
                         tuple(tuple(values) for values in fn.getVertices()) == vertices and
                         (world is None or tuple((p.x,p.y,p.z) for p in fn.getPoints(om.MSpace.kWorld)) == world) and
                         dict(enumerate(zip(u,v))) == points)
        except (RuntimeError,ValueError): unchanged = False
        if not unchanged:
            raise ValueError('The mesh or UVs changed during packing. Run packing again.')


def apply(prepared, result, callback=None, _refine=True, source_callback=None):
    """Apply one complete plan on the Maya thread; outer caller owns undo."""
    global _last_packed
    if result.get('cancelled'): return result
    validate(prepared)
    original=result
    if _refine:
        from uv_straight_pack_refine import refine_contacts
        result=refine_contacts(prepared['data'],result)
    records,boxes = prepared['records'],prepared['data']['boxes']
    scale,positions,turns = result['scale'],result['positions'],result['turns']
    import numpy as np
    from uv_straight_pack_commit import commit
    if callback:callback(0,len(records))
    by_mesh={}
    current={snapshot[0]:snapshot[2] for snapshot in prepared['snapshots']}
    groups=prepared.get('record_groups',range(len(records)))
    for index,(mesh,ids,minimum,normalize,_) in enumerate(records):
        if id(mesh) not in by_mesh:
            before=np.asarray(list(current[mesh.name].values()),dtype=float)
            by_mesh[id(mesh)]=dict(handle=om.MObjectHandle(mesh.fn.object()),uv_set=mesh.uv_set,name=mesh.name,
                                  before=before,after=before.copy(),ids=[],shells=0,bulk_history=True)
        entry=by_mesh[id(mesh)];group=groups[index]
        x,y=positions[group];turn=int(turns[group])%4
        w,h=boxes[group][0]*scale,boxes[group][1]*scale
        source=prepared.get('source_points',{}).get(mesh.name,mesh.points)
        points=(np.asarray([source[i] for i in ids])-minimum)*(normalize*scale)
        if turn==1:points=np.column_stack((h-points[:,1],points[:,0]))
        elif turn==2:points=np.column_stack((w-points[:,0],h-points[:,1]))
        elif turn==3:points=np.column_stack((points[:,1],w-points[:,0]))
        origin=prepared.get('target_origin',(0.,0.))
        entry['after'][ids]=points+(x+origin[0],y+origin[1])
        entry['ids'].extend(ids);entry['shells']+=1
    entries=list(by_mesh.values())
    if result.get('_certified_contours'):
        import time
        from uv_straight_pack_refine import certify_contours,GeometryBudget
        deadline=time.monotonic()+.5
        def check_geometry():
            if time.monotonic()>=deadline:raise GeometryBudget()
        origin=np.asarray(prepared.get('target_origin',(0.,0.)))
        actual=[[] for _ in boxes];sources={}
        owners_by_mesh={}
        for mesh,ids,*_ in records:owners_by_mesh.setdefault(id(mesh),set()).add(mesh.shell_ids[ids[0]])
        try:
            for mesh,*_ in records:
                check_geometry()
                if id(mesh) not in sources:
                    points=by_mesh[id(mesh)]['after'].astype(np.float32).astype(float)-origin
                    sources[id(mesh)]=_triangles_by_shell(mesh,points=points,owners=owners_by_mesh[id(mesh)],check=check_geometry)
            seen=set()
            for index,(mesh,ids,*_) in enumerate(records):
                owner=mesh.shell_ids[ids[0]];key=(id(mesh),owner)
                if key not in seen:actual[groups[index]].extend(sources[id(mesh)].get(owner,[]));seen.add(key)
            report=certify_contours(actual,prepared['data']['static_triangles'],
                prepared['data']['gap'],prepared['data']['margin'],prepared['data']['target_size'],deadline=deadline)
        except GeometryBudget:report=dict(valid=False,reason='budget_or_cancel')
        if not report['valid']:
            from uv_straight_pack_cache import close
            close()
            if original is result:
                raise ValueError('The resumed contour layout could not be certified. UVs were not changed; run Pack again.')
            return apply(prepared,original,callback,_refine=False,source_callback=source_callback)
    if prepared.get('preserve_source'):
        from uv_straight_transfer_snapshot import preserve
        if source_callback:source_callback(0,0)
        preserve(prepared,source_callback)
        if callback:callback(0,len(records))
    for entry in entries:
        for key in ('before', 'after'):
            entry[key] = tuple(om.MFloatArray(values.tolist()) for values in entry[key].T)
        entry['ids'] = om.MIntArray(entry['ids'])
    commit(entries,callback)
    if result is not original:
        from uv_straight_pack_cache import remember_committed
        remember_committed(prepared['data'],result)
    result['preparation_cache_hit']=prepared.get('preparation_cache_hit',False)
    if prepared.get('reuse_key') is not None:
        replay=dict(prepared)
        after={entry['name']:dict(enumerate(zip(*om.MFnMesh(entry['handle'].object()).getUVs(entry['uv_set'])))) for entry in entries}
        replay['snapshots']=[(name,uv_set,after.get(name,points),*rest)
                             for name,uv_set,points,*rest in prepared['snapshots']]
        replay['source_points']=prepared.get('source_points') or {mesh.name:dict(mesh.points) for mesh,*_ in records}
        _last_packed=replay
    return result


def pack(meshes, resolution, spacing_px, margin_px=20, normalize_density=True,
         selected_only=False, rotate=False, method='fast', around_others=False,
         seconds=3600., precision=1024, callback=None, cancel=None, keep_stacks=False, gpu=False, target_size=(1.,1.), shape_proxies=False, spread_small=False, preserve_source=False,
         target_origin=(0.,0.),iteration_limit=0,base_density=500.,cpu_assist=False,reuse_preparation=True):
    prepared = prepare(meshes,resolution,spacing_px,margin_px,normalize_density,
                       selected_only,rotate,method,around_others,seconds,precision,keep_stacks,gpu,target_size,shape_proxies,spread_small,preserve_source,target_origin,iteration_limit,base_density,cpu_assist=cpu_assist,reuse_preparation=reuse_preparation)
    result=plan(prepared['data'],callback,cancel)
    if preserve_source:
        from uv_straight import _undo
        with _undo('ShellShifter Pack UVs'):return apply(prepared,result)
    return apply(prepared,result)
