"""Independent shelf/free-rectangle UV packing with explicit gaps and density."""
from bisect import bisect_left, insort
import math
import maya.cmds as cmds
import maya.api.OpenMaya as om
from uv_straight_pack_search import counted_trial
from uv_straight_pack_area import area_size, scale_limit


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


def _shell_records(meshes, normalize_density=True, selected_only=False):
    import numpy as np
    records = []
    for mesh in meshes:
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
        for shell in (mesh.shells() if selected_only else mesh.all_shells()):
            ids = sorted(shell)
            owner = mesh.shell_ids[ids[0]]
            if owner not in assigned_shells:
                continue  # Unassigned UV points have no polygon surface to pack.
            surface, mapped = world_by_shell[owner], uv_by_shell[owner]
            minimum, maximum = np.min(uv[ids], axis=0), np.max(uv[ids], axis=0)
            if mapped<=1e-24 and np.array_equal(minimum,maximum):
                continue  # A fully collapsed point has no packing footprint.
            if not math.isfinite(surface+mapped) or surface <= 1e-24 or mapped <= 1e-24:
                raise ValueError('Cannot pack a collapsed or zero-area shell on '+mesh.name)
            normalize = math.sqrt(surface/mapped) if normalize_density else 1.
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
    free = [(0., 0., extent_x, extent_y)]
    for obstacle in obstacles:
        free = _split_free(free, obstacle)
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
            if monitor: monitor.default_turns = {i:False for i in range(len(boxes))}
            low, placed = layout_boxes(boxes, gap, margin, monitor, target_size)
            placed = {i: (x-margin, y-margin) for i, (x, y) in placed.items()}
            turns = {i: False for i in range(len(boxes))}
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


def _other_boxes(meshes, packed=None):
    result = []
    for mesh in meshes:
        selected = (packed.get(id(mesh),set()) if packed is not None else
                    {mesh.shell_ids[i] for i in mesh.selected})
        assigned = {mesh.shell_ids[face[0]] for face in mesh.faces}
        for shell in mesh.all_shells():
            owner = mesh.shell_ids[next(iter(shell))]
            if owner in selected or owner not in assigned:
                continue
            points = [mesh.points[i] for i in shell]
            lo = [min(p[axis] for p in points) for axis in (0, 1)]
            hi = [max(p[axis] for p in points) for axis in (0, 1)]
            if not all(math.isfinite(value) for value in lo+hi):
                raise ValueError('Unselected shells have invalid UV coordinates.')
            result.append((lo[0], lo[1], hi[0]-lo[0], hi[1]-lo[1]))
    return result


def _scene_obstacle_paths(excluded):
    """Other visible mesh shapes, once per shared UV data, on the Maya thread."""
    seen=set(excluded)
    result=[]
    for name in cmds.ls(type='mesh',long=True,noIntermediate=True,dag=True,allPaths=True) or []:
        selection=om.MSelectionList();selection.add(name)
        path=selection.getDagPath(0)
        identity=om.MObjectHandle(path.node()).hashCode()
        if identity in seen or not path.isVisible():continue
        fn=om.MFnMesh(path)
        if not fn.getUVSetNames() or not fn.numUVs():continue
        seen.add(identity)
        result.append(path)
    return sorted(result,key=lambda path:path.fullPathName())


def _triangles_by_shell(mesh):
    """Use Maya's actual polygon triangulation, preserving UV seams."""
    counts, vertices = mesh.fn.getTriangles()
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
    for polygon,count in zip(faces,counts):
        end=offset+count*3
        if polygon is None:
            offset=end
            continue
        face,corners=polygon
        mapping = dict(zip(corners, face))
        owner = mesh.shell_ids[face[0]]
        group = result.setdefault(owner, [])
        for _ in range(count):
            group.append(tuple(mesh.points[mapping[v]] for v in vertices[offset:offset+3]))
            offset += 3
    return result


def _triangle_area(triangle):
    a,b,c = triangle
    return abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))*.5


def prepare(meshes, resolution, spacing_px, margin_px=20, normalize_density=True,
            selected_only=False, rotate=False, method='fast', around_others=False,
            seconds=30., precision=1024, keep_stacks=False, gpu=False, target_size=(1.,1.)):
    """Scene reads on the caller thread; the data dictionary is worker-safe.

    With selected_only and around_others, reserve unselected shells plus the
    current UV sets of other visible mesh shapes, without moving those meshes.
    """
    target_size=area_size(target_size)
    short_resolution=resolution*min(target_size)
    if (not math.isfinite(resolution) or not 256 <= resolution <= 8192 or
            not all(math.isfinite(v) and v >= 0 for v in (spacing_px, margin_px)) or
            margin_px*2 >= short_resolution or spacing_px >= short_resolution*.5):
        raise ValueError('Use a texture size from 256 to 8192 and smaller non-negative spacing and margin.')
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
    records = _shell_records(meshes, normalize_density, selected_only)
    if not records:
        raise ValueError('Select UV shells with a valid polygon surface.')
    triangles_by_mesh = {id(mesh):_triangles_by_shell(mesh) for mesh in meshes}
    from uv_straight_pack_stacks import signatures, groups
    stack_tolerance=max(1e-5,min(.5/resolution,spacing_px/(4.*resolution)))
    outlines={id(mesh):signatures(mesh.faces,mesh.shell_ids,mesh.points) for mesh in meshes} if keep_stacks else {}
    if keep_stacks and selected_only:
        selected_owners={(id(mesh),mesh.shell_ids[ids[0]]) for mesh,ids,*_ in records}
        scope=[(mesh,shell) for mesh in meshes for shell in mesh.all_shells()
               if mesh.shell_ids[next(iter(shell))] in outlines[id(mesh)]]
        scoped_groups=groups([outlines[id(mesh)].get(mesh.shell_ids[next(iter(shell))]) for mesh,shell in scope],stack_tolerance)
        expanded=set(selected_owners)
        for indices in scoped_groups:
            owners={(id(scope[i][0]),scope[i][0].shell_ids[next(iter(scope[i][1]))]) for i in indices}
            if selected_owners & owners:expanded.update(owners)
        previous={id(mesh):mesh.selected for mesh in meshes}
        try:
            if expanded!=selected_owners:
                for mesh in meshes:
                    mesh.selected={i for shell in mesh.all_shells()
                                   if (id(mesh),mesh.shell_ids[next(iter(shell))]) in expanded for i in shell}
                records=_shell_records(meshes,normalize_density,True)
        finally:
            for mesh in meshes:mesh.selected=previous[id(mesh)]
    packed={id(mesh):set() for mesh in meshes}
    for mesh,ids,*_ in records:packed[id(mesh)].add(mesh.shell_ids[ids[0]])
    members=groups([outlines.get(id(mesh),{}).get(mesh.shell_ids[ids[0]])
                    for mesh,ids,*_ in records],stack_tolerance)
    triangles, areas, boxes, record_groups = [], [], [], [0]*len(records)
    adjusted=list(records)
    for group_index,indices in enumerate(members):
        sources=[triangles_by_mesh[id(records[i][0])][records[i][0].shell_ids[records[i][1][0]]]
                 for i in indices]
        mapped=[sum(_triangle_area(t) for t in source) for source in sources]
        normalize=math.sqrt(sum(area*records[i][3]**2 for area,i in zip(mapped,indices))/sum(mapped))
        points=[point for source in sources for tri in source for point in tri]
        minimum=tuple(min(point[axis] for point in points) for axis in (0,1))
        bounds=tuple((max(point[axis] for point in points)-minimum[axis])*normalize for axis in (0,1))
        local=[]; seen=set()
        for source in sources:
            for tri in source:
                key=tuple(sorted(tuple(round(v,9) for v in point) for point in tri))
                if key in seen: continue
                seen.add(key)
                local.append(tuple(((x-minimum[0])*normalize,(y-minimum[1])*normalize) for x,y in tri))
        triangles.append(local)
        areas.append(max(mapped)*normalize**2)
        boxes.append(bounds)
        for i in indices:
            mesh,ids,*_=records[i]
            adjusted[i]=(mesh,ids,minimum,normalize,bounds)
            record_groups[i]=group_index
    static = []
    obstacle_meshes=list(meshes)
    obstacle_paths=None
    if selected_only and around_others:
        from uv_straight import Mesh
        obstacle_paths=_scene_obstacle_paths(mesh_keys)
        obstacle_meshes.extend(Mesh(path) for path in obstacle_paths)
        for mesh in obstacle_meshes:
            selected = packed.get(id(mesh),set())
            sources=triangles_by_mesh.get(id(mesh))
            if sources is None:sources=_triangles_by_shell(mesh)
            for owner, group in sources.items():
                if owner in selected:continue
                if not all(math.isfinite(value) for tri in group for point in tri for value in point):
                    raise ValueError('Unselected shells have invalid UV coordinates: '+mesh.name)
                static.extend(group)
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
                stack_groups=sum(len(indices)>1 for indices in members),
                gap=spacing_px/float(resolution), margin=margin_px/float(resolution),
                rotate=rotate, method=method, seconds=seconds, precision=precision, gpu=gpu, target_size=target_size,
                obstacles=_other_boxes(obstacle_meshes,packed) if selected_only and around_others else [],
                static_triangles=static, static_area=static_area)
    return dict(records=adjusted, data=data, record_groups=record_groups,
                obstacle_roster=None if obstacle_paths is None else tuple(path.fullPathName() for path in obstacle_paths),
                obstacle_excluded=tuple(mesh_keys),
                snapshots=[(mesh.name,mesh.uv_set,dict(mesh.points),mesh.fn.numPolygons,
                            tuple(mesh.fn.getAssignedUVs(mesh.uv_set)[1]),
                            tuple(mesh.fn.getVertices()[1]),
                            tuple((p.x,p.y,p.z) for p in mesh.fn.getPoints(om.MSpace.kWorld))
                            if normalize_density and id(mesh) in packed else None) for mesh in obstacle_meshes])


def plan(data, callback=None, cancel=None):
    """Pure numerical search: never access Maya or widgets from this function."""
    import time
    from uv_straight_pack_search import SearchMonitor, SearchStopped
    monitor = SearchMonitor(data['areas'],data['static_area'],callback,cancel,data['seconds'],data.get('target_size',(1.,1.)))
    backend=None
    try:
        if data.get('gpu') and data['method'] == 'contours':
            monitor.update('Connecting to GPU…',force=True)
            from uv_straight_pack_gpu import OpenCLPlacement
            backend=OpenCLPlacement()
            # Driver/program setup must not consume a finite numerical search
            # budget and return an empty result before any shell was tried.
            if data['seconds']:monitor.deadline=time.monotonic()+data['seconds']
            monitor.engine='GPU · '+backend.name
            monitor.update('GPU ready',force=True)
        if backend is not None and data['rotate'] and len(data['boxes'])>=512:
            monitor.horizontal_axes=tuple((i,w>=h) for i,(w,h) in enumerate(data['boxes'])
                                         if max(w,h)>=4*min(w,h))
        result=_plan(data,monitor,cancel,backend)
        result['engine']=monitor.engine or 'CPU'
        if backend is not None:
            result.update(gpu_dispatches=backend.dispatches,gpu_candidates=backend.candidates,
                    gpu_cache_hits=backend.cache_hits,gpu_cache_bytes=backend.cache_bytes,
                    local_attempts=monitor.local_attempts,fixed_shells=monitor.fixed_shells,
                    solid_preparations=backend.solid_preparations)
        return result
    except SearchStopped:
        return dict(cancelled=True,shells=0,elapsed=time.monotonic()-monitor.started,
                    engine=monitor.engine,**monitor.statistics())
    finally:
        if backend is not None:backend.close()


def _plan(data, monitor, cancel, backend):
    import time
    from uv_straight_pack_search import SearchStopped
    boxes,gap,margin = data['boxes'],data['gap'],data['margin']
    rotate,method,obstacles = data['rotate'],data['method'],data['obstacles']
    target_size=monitor.target_size
    try:
        if method == 'compact' or obstacles:
            # Contours only need a cheap complete seed; avoid a long rectangle
            # search before the contour search on large unobstructed selections.
            layout_rectangles(boxes,gap,margin,rotate,obstacles,monitor,target_size)
        else:
            turns = {i:bool(rotate and h > w) for i,(w,h) in enumerate(boxes)}
            monitor.default_turns = turns
            sizes = [(h,w) if turns[i] else (w,h) for i,(w,h) in enumerate(boxes)]
            layout_boxes(sizes,gap,margin,monitor,target_size)
        if method == 'contours':
            from uv_straight_contour_pack import search
            search(boxes,data['triangles'],data['areas'],gap,margin,rotate,
                   data['static_triangles'],monitor,data['precision'],backend)
    except ValueError:
        if method != 'contours': raise
        # Rectangles may not fit while the actual contours do.
        try:
            from uv_straight_contour_pack import search
            search(boxes,data['triangles'],data['areas'],gap,margin,rotate,
                   data['static_triangles'],monitor,data['precision'],backend)
        except SearchStopped: pass
    except SearchStopped:
        pass
    if monitor.best is None:
        if cancel is not None and cancel.is_set():
            return dict(cancelled=True,shells=0,elapsed=time.monotonic()-monitor.started,**monitor.statistics())
        raise ValueError('No complete layout found. Increase search time or reduce spacing.')
    result = dict(monitor.best)
    result.pop('order',None)
    # Uniformly align an unobstructed completed layout to the requested border.
    # Coarse-cell rounding can reserve an extra pixel; a global translation
    # preserves every inter-shell distance and increases the opposite margins.
    if not obstacles and not data['static_triangles']:
        dx=margin-min(x for x,y in result['positions'].values())
        dy=margin-min(y for x,y in result['positions'].values())
        result['positions']={i:(x+min(0.,dx),y+min(0.,dy))
                             for i,(x,y) in result['positions'].items()}
    result.update(target_size=target_size,shells=data.get('shell_count',len(boxes)),groups=len(boxes),
                  stack_groups=data.get('stack_groups',0),
                  stacked_shells=data.get('shell_count',len(boxes))-len(boxes),iterations=monitor.iteration,
                  elapsed=time.monotonic()-monitor.started,
                  stopped=cancel is not None and cancel.is_set(),cancelled=False)
    result.update(monitor.statistics())
    return result


def validate(prepared):
    """Reject stale results if another script changed the scene during search."""
    if prepared.get('obstacle_roster') is not None:
        roster=tuple(path.fullPathName() for path in _scene_obstacle_paths(prepared['obstacle_excluded']))
        if roster!=prepared['obstacle_roster']:
            raise ValueError('Other meshes changed during packing. Run packing again.')
    for name, uv_set, points, polygons, assignments, vertices, world in prepared['snapshots']:
        try:
            selection = om.MSelectionList(); selection.add(name)
            fn = om.MFnMesh(selection.getDagPath(0))
            u,v = fn.getUVs(uv_set)
            unchanged = (fn.currentUVSetName() == uv_set and fn.numPolygons == polygons and
                         tuple(fn.getAssignedUVs(uv_set)[1]) == assignments and
                         tuple(fn.getVertices()[1]) == vertices and
                         (world is None or tuple((p.x,p.y,p.z) for p in fn.getPoints(om.MSpace.kWorld)) == world) and
                         dict(enumerate(zip(u,v))) == points)
        except (RuntimeError,ValueError): unchanged = False
        if not unchanged:
            raise ValueError('The mesh or UVs changed during packing. Run packing again.')


def apply(prepared, result, callback=None):
    """Apply one complete plan on the Maya thread; outer caller owns undo."""
    if result.get('cancelled'): return result
    validate(prepared)
    records,boxes = prepared['records'],prepared['data']['boxes']
    scale,positions,turns = result['scale'],result['positions'],result['turns']
    import numpy as np
    from uv_straight_pack_commit import commit
    if callback:callback(0,len(records))
    by_mesh={}
    groups=prepared.get('record_groups',range(len(records)))
    for index,(mesh,ids,minimum,normalize,_) in enumerate(records):
        if id(mesh) not in by_mesh:
            before=np.asarray(list(mesh.points.values()),dtype=float)
            by_mesh[id(mesh)]=dict(handle=om.MObjectHandle(mesh.fn.object()),uv_set=mesh.uv_set,
                                  before=before,after=before.copy(),ids=[],shells=0)
        entry=by_mesh[id(mesh)];group=groups[index]
        x,y=positions[group];turn=int(turns[group])%4
        w,h=boxes[group][0]*scale,boxes[group][1]*scale
        points=(entry['before'][ids]-minimum)*(normalize*scale)
        if turn==1:points=np.column_stack((h-points[:,1],points[:,0]))
        elif turn==2:points=np.column_stack((w-points[:,0],h-points[:,1]))
        elif turn==3:points=np.column_stack((points[:,1],w-points[:,0]))
        entry['after'][ids]=points+(x,y)
        entry['ids'].extend(ids);entry['shells']+=1
    entries=list(by_mesh.values())
    for entry in entries:
        for key in ('before','after'):entry[key]=entry[key].T.tolist()
    commit(entries,callback)
    return result


def pack(meshes, resolution, spacing_px, margin_px=20, normalize_density=True,
         selected_only=False, rotate=False, method='fast', around_others=False,
         seconds=3600., precision=1024, callback=None, cancel=None, keep_stacks=False, gpu=False, target_size=(1.,1.)):
    prepared = prepare(meshes,resolution,spacing_px,margin_px,normalize_density,
                       selected_only,rotate,method,around_others,seconds,precision,keep_stacks,gpu,target_size)
    return apply(prepared,plan(prepared['data'],callback,cancel))
