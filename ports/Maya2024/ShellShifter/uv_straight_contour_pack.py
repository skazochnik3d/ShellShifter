"""Independent conservative triangle raster/bit-row contour packing.

Pure numerical data only; no Maya calls. A completed placement is checked against
occupied conservative cells, with a Euclidean gap expansion. Integer bit rows
test all horizontal origins in parallel through forbidden-origin dilation.
"""
import math
import random
from uv_straight_deps import np
from uv_straight_pack_search import counted_trial,SAMPLED_SEARCH_GROUPS
from uv_straight_pack_area import grid_extent, scale_limit


def _clip_y(polygon, bound, above):
    result = []
    prev = polygon[-1]
    prev_inside = prev[1] >= bound if above else prev[1] <= bound
    for point in polygon:
        inside = point[1] >= bound if above else point[1] <= bound
        if inside != prev_inside:
            t = (bound-prev[1])/(point[1]-prev[1])
            result.append((prev[0]+t*(point[0]-prev[0]),bound))
        if inside:
            result.append(point)
        prev,prev_inside = point,inside
    return result


def raster_triangles(triangles, width, height, monitor=None):
    """Every cell intersecting a triangle's row slab is included."""
    mask = np.zeros((max(1,height),max(1,width)),dtype=np.bool_)
    for index, triangle in enumerate(triangles):
        if monitor is not None and index%64 == 0:
            monitor.check()
        first = max(0,math.floor(min(p[1] for p in triangle)-1e-10))
        last = min(height,math.floor(max(p[1] for p in triangle)+1e-10)+1)
        for row in range(first,last):
            clipped = _clip_y(list(triangle),row,True)
            if clipped: clipped = _clip_y(clipped,row+1,False)
            if not clipped: continue
            left = max(0,math.floor(min(p[0] for p in clipped)-1e-10))
            right = min(width,math.floor(max(p[0] for p in clipped)+1e-10)+1)
            if right > left:
                mask[row,left:right] = True
    return mask


def _rows(mask):
    return tuple(int.from_bytes(row.tobytes(),'little') for row in
                 np.packbits(mask,axis=1,bitorder='little'))


def _expand(rows, gap):
    # Minimum Euclidean distance between two occupied cell squares. Using
    # circle offsets avoids reserving a square's unnecessary diagonal corners.
    widths={dy:math.ceil(math.sqrt(max(0.,gap*gap-max(0,abs(dy)-1)**2))-1e-12)
            if gap else 0 for dy in range(-gap,gap+1)}
    horizontal={width:tuple(_dilate_right(bits << (2*width),2*width+1) << (gap-width)
                            for bits in rows) for width in set(widths.values())}
    expanded = []
    for y in range(-gap,len(rows)+gap):
        bits = 0
        for offset,width in widths.items():
            source=y-offset
            if 0 <= source < len(rows):bits |= horizontal[width][source]
        expanded.append(bits)
    return tuple(expanded)


def _runs(bits):
    result=[]
    while bits:
        start=(bits & -bits).bit_length()-1
        tail=bits >> start
        length=((tail ^ (tail+1)) >> 1).bit_length()
        result.append((start,length))
        bits &= ~(((1 << length)-1) << start)
    return result


from functools import lru_cache


@lru_cache(maxsize=4096)
def _dilate_right(bits, length):
    # Bit-parallel union of shifts 0..length-1, in logarithmic operations.
    span=1
    while span*2 <= length:
        bits |= bits >> span
        span *= 2
    if length > span: bits |= bits >> (length-span)
    return bits


def _location(occupied, padded, width, height, gap, margin, size, monitor, ceiling=None):
    extent_x,extent_y=grid_extent(size,monitor)
    max_x,max_y = extent_x-margin-width,extent_y-margin-height
    if ceiling is not None: max_y=min(max_y,ceiling)
    if max_x < margin or max_y < margin: return None
    rows=[(dy,_runs(bits)) for dy,bits in enumerate(padded) if bits]
    initial=((1 << (max_x-margin+1))-1) << margin
    # Compute all forbidden X origins at once with integer bit operations.
    # Every occupied row interval excludes a range of shell origins; a free
    # bit after intersecting all rows is an exact fit for this conservative mask.
    for y in range(margin,max_y+1):
        if y%16 == 0: monitor.check()
        available=initial
        for dy,runs in rows:
            yy=y+dy-gap
            if not 0 <= yy < size or not occupied[yy]: continue
            target=occupied[yy]
            for start,length in runs:
                shift=start-gap
                forbidden=(_dilate_right(target,length) >> shift if shift >= 0 else
                           _dilate_right(target << -shift,length))
                available &= ~forbidden
                if not available: break
            if not available: break
        if available: return (available & -available).bit_length()-1,y
    return None


def _hull_area(triangles):
    points=sorted({tuple(point) for triangle in triangles for point in triangle})
    if len(points)<3:return 0.
    def chain(points):
        hull=[]
        for point in points:
            while len(hull)>=2:
                a,b=hull[-2:]
                if (b[0]-a[0])*(point[1]-a[1])>(b[1]-a[1])*(point[0]-a[0]):break
                hull.pop()
            hull.append(point)
        return hull
    hull=chain(points)[:-1]+chain(reversed(points))[:-1]
    return abs(sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(hull,hull[1:]+hull[:1])))*.5

def _order(boxes,ordering):
    mode=ordering%6
    rng=random.Random(ordering)
    amplitude=(.04,.12,.25,.45)[(ordering//6)%4] if ordering>=6 else 0.
    priority=[]
    for w,h in boxes:
        long,short=max(w,h),min(w,h)
        value=(long,w*h,short,short**.75*long**.25,w+h,short)[mode]
        value*=math.exp(rng.uniform(-amplitude,amplitude))
        # Height bands keep strips with similar thickness in the same rows.
        if mode==5:value=math.floor(math.log(max(value,1e-24))/math.log(1.35))
        priority.append(value)
    return sorted(range(len(boxes)),key=lambda i:(-priority[i],-max(boxes[i]),i))


def rotation_options(box,rotate):
    # Horizontal strips are preferred at equal fits, but vertical placements
    # remain available when they improve density or fill a narrow corridor.
    return (0,1,2,3) if rotate else (0,)


def solid_rectangle(box,triangles):
    """Prove the triangulated contour is its box before using a solid mask."""
    from collections import Counter
    w,h=box;edges=Counter();area=0.
    for tri in triangles:
        a,b,c=tri
        area+=abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))*.5
        for a,b in zip(tri,tri[1:]+tri[:1]):edges[tuple(sorted((tuple(a),tuple(b))))]+=1
    tolerance=max(w*h*1e-9,1e-24)
    if abs(area-w*h)>tolerance or not edges:return False
    sides=[0.,0.,0.,0.]
    for (a,b),count in edges.items():
        if count==2:continue
        if count!=1:return False
        if a[0]==b[0]==0.:sides[0]+=abs(a[1]-b[1])
        elif a[0]==b[0]==w:sides[1]+=abs(a[1]-b[1])
        elif a[1]==b[1]==0.:sides[2]+=abs(a[0]-b[0])
        elif a[1]==b[1]==h:sides[3]+=abs(a[0]-b[0])
        else:return False
    return all(abs(actual-expected)<=expected*1e-9 for actual,expected in zip(sides,(h,h,w,w)))


def _cell_origin(box,scale,grid,x,y,turn):
    w,h=box[0]*scale*grid,box[1]*scale*grid
    iw,ih=max(1,math.ceil(w)),max(1,math.ceil(h))
    dx=(ih-h) if turn==1 else (iw-w) if turn==2 else 0.
    dy=(ih-h) if turn==2 else (iw-w) if turn==3 else 0.
    return (x+dx)/grid,(y+dy)/grid


def _static_rows(static_triangles,grid,monitor,backend):
    """Search-owned immutable obstacles; shell commits always use a copy."""
    monitor.check()
    source=getattr(monitor,'static_source',None)
    if source is not static_triangles:
        monitor.static_source=static_triangles
        monitor.static_rows={}
    cache=monitor.static_rows
    key=(grid,backend is not None)
    if key in cache:
        monitor.static_cache_hits+=1
        return cache[key]
    raster=backend.raster if backend is not None else raster_triangles
    rows=(_rows(raster([[(x*grid,y*grid) for x,y in tri]
                             for tri in static_triangles],grid,grid,monitor))
          if static_triangles else (0,)*grid)
    monitor.check()
    cache[key]=rows
    monitor.static_cache_builds+=1
    return rows


@counted_trial(8)
def trial(boxes, triangles, scale, grid, gap, margin, rotate, static_triangles,
          monitor, ordering=0, backend=None, order=None):
    order=list(_order(boxes,ordering) if order is None else order)
    if len(order)!=len(boxes) or set(order)!=set(range(len(boxes))):
        raise ValueError('Packing order must contain every group exactly once.')
    gap_cells,margin_cells = math.ceil(gap*grid),math.ceil(margin*grid)
    extent_x,extent_y=grid_extent(grid,monitor)
    occupied = _static_rows(static_triangles,grid,monitor,backend)
    if backend is None or not hasattr(backend,'place_group'):occupied=list(occupied)
    raster=backend.raster if backend is not None else raster_triangles
    if backend is not None: backend.reset(occupied,grid)
    resident=backend is not None and hasattr(backend,'place_group')
    if resident:backend.bind_geometry(boxes,triangles)
    if getattr(monitor,'pack_groups',None):
        from uv_straight_pack_groups import preferred_order
        order=preferred_order(order,monitor.pack_groups)
    positions,turns = {},{}
    for placed,index in enumerate(order):
        monitor.update('Packing shell contours',placed,len(order))
        w,h = boxes[index][0]*scale*grid,boxes[index][1]*scale*grid
        iw,ih = max(1,math.ceil(w)),max(1,math.ceil(h))
        if not ((iw<=extent_x-2*margin_cells and ih<=extent_y-2*margin_cells) or
                (rotate and ih<=extent_x-2*margin_cells and iw<=extent_y-2*margin_cells)):
            return None
        if resident:
            result=backend.place_group(index,scale,gap_cells,margin_cells,rotate,monitor)
            if result is None:
                _remember_failure(monitor,dict(scale=scale,order=tuple(order),
                                  index=index,placed=placed,objective=0))
                return None
            x,y,turn=result
            dx=(ih-h) if turn==1 else (iw-w) if turn==2 else 0.
            dy=(ih-h) if turn==2 else (iw-w) if turn==3 else 0.
            positions[index],turns[index]=((x+dx)/grid,(y+dy)/grid),turn
            continue
        profiles=getattr(monitor,'shape_profiles',None)
        proxy=None
        if profiles is not None:
            from uv_straight_pack_shapes import select
            proxy=select(profiles[index],scale,monitor.shape_resolution)
        best = None
        # Prefer horizontal strips at equal bottom-left positions.
        options = rotation_options(boxes[index],rotate)
        for use_proxy in ((True,False) if proxy is not None else (False,)):
            if use_proxy:
                from uv_straight_pack_shapes import raster as primitive_raster
                base=primitive_raster(proxy,iw,ih,scale*grid)
            else:
                base = raster([[(x*scale*grid,y*scale*grid) for x,y in tri]
                                         for tri in triangles[index]],iw,ih,monitor)
            seen = set()
            for turn in options:
                mask = np.rot90(base,-turn) if turn else base
                height,width = mask.shape
                if width > extent_x-2*margin_cells or height > extent_y-2*margin_cells: continue
                raw = _rows(mask)
                identity = (width,height,raw)
                if identity in seen: continue
                seen.add(identity)
                locate=backend.location if backend is not None else _location
                location = locate(occupied,_expand(raw,gap_cells),width,height,gap_cells,
                                     margin_cells,grid,monitor,best[0] if best is not None else None)
                if location is not None:
                    x,y = location
                    candidate = (y,x,height,width,turn,raw)
                    if best is None or candidate[:5] < best[:5]: best = candidate
            if best is not None:break
        if best is None:
            _remember_failure(monitor,dict(scale=scale,order=tuple(order),
                              index=index,placed=placed,objective=0))
            return None
        y,x,height,width,turn,raw = best
        if backend is not None: backend.commit(raw,width,x,y)
        for dy,bits in enumerate(raw): occupied[y+dy] |= bits << x
        # Rotation of ceil-sized raster canvases introduces a subcell offset.
        # Include it in the UV origin so the real geometry stays inside its mask.
        dx = (ih-h) if turn == 1 else (iw-w) if turn == 2 else 0.
        dy = (ih-h) if turn == 2 else (iw-w) if turn == 3 else 0.
        positions[index],turns[index] = ((x+dx)/grid,(y+dy)/grid),turn
    return positions,turns


def _remember_failure(monitor,failure):
    """Retain diverse nearly complete candidates instead of recent noise."""
    failures=monitor.failed_orders
    if monitor.best:
        low=monitor.best['scale']
        failures[:]=[f for f in failures if low<=f['scale']<=low*1.025]
    same=next((f for f in failures if f['order']==failure['order'] and
               f['objective']==failure['objective']),None)
    if same is not None:
        if (same['placed'],-same['scale'])>=(failure['placed'],-failure['scale']):return
        failures.remove(same)
    failures.append(failure)
    failures.sort(key=lambda f:(-f['placed'],f['scale']))
    del failures[16:]


def trial_many(boxes,triangles,scale,grid,gap,margin,rotate,static_triangles,
               monitor,orderings,backend,orders=None):
    """One cycle contains four independently evaluated placement candidates."""
    monitor.parallel=len(orderings)
    orders=orders if orders is not None else [_order(boxes,ordering) for ordering in orderings]
    if getattr(monitor,'pack_groups',None):
        from uv_straight_pack_groups import preferred_order
        orders=[preferred_order(order,monitor.pack_groups) for order in orders]
    layouts=[({}, {}) for _ in orders]
    histories=[[] for _ in orders]
    rescued=[False]*len(orders)
    monitor.attempt(len(orders))
    try:
        occupied=_static_rows(static_triangles,grid,monitor,backend)
        backend.reset_many(occupied,grid,len(orderings));backend.bind_geometry(boxes,triangles)
        gap_cells,margin_cells=math.ceil(gap*grid),math.ceil(margin*grid)
        pending=[]
        sequence=max(1,min(8,int(getattr(backend,'sequence_steps',1))))
        for placed in range(len(boxes)):
            monitor.update('Packing layouts in parallel',placed,len(boxes))
            indices=[order[placed] if layout is not None else None for order,layout in zip(orders,layouts)]
            if not pending:
                if sequence>1 and hasattr(backend,'place_sequence'):
                    limit=min(len(boxes),placed+sequence)
                    if monitor.tail_size:
                        boundary=max(0,len(boxes)-64)
                        limit=min(limit,boundary) if placed<boundary else placed+1
                    steps=[[order[at] if layout is not None else None for order,layout in zip(orders,layouts)]
                           for at in range(placed,limit)]
                    pending=backend.place_sequence(steps,scale,gap_cells,margin_cells,rotate,monitor)
                else:pending=[backend.place_many(indices,scale,gap_cells,margin_cells,rotate,monitor)]
            locations=pending.pop(0)
            for slot,(index,location) in enumerate(zip(indices,locations)):
                if layouts[slot] is None:continue
                if location is None:
                    if (monitor.tail_size and not rescued[slot] and not pending and
                            placed>=max(len(boxes)-64,int(len(boxes)*.9)) and hasattr(backend,'edit_placed')):
                        from uv_straight_pack_refine import rescue_tail
                        rescued[slot]=True
                        recovered=rescue_tail(boxes,histories[slot],index,slot,len(orders),scale,
                            gap_cells,margin_cells,rotate,monitor,backend,orders[slot][placed+1:placed+5])
                        if recovered is not None:
                            count,new=recovered
                            old=histories[slot][-count:]
                            histories[slot][-count:]=new
                            orders[slot][placed-count:placed+1]=[j for j,x,y,t in new]
                            for j,x,y,t in old:
                                del layouts[slot][0][j];del layouts[slot][1][j]
                            for j,x,y,t in new:
                                layouts[slot][0][j]=_cell_origin(boxes[j],scale,grid,x,y,t)
                                layouts[slot][1][j]=t
                            continue
                    monitor.finish_attempt(False)
                    modes=getattr(backend,'current_modes',[0]*len(orders))
                    _remember_failure(monitor,dict(scale=scale,order=tuple(orders[slot]),
                        index=index,placed=placed,objective=modes[slot]))
                    layouts[slot]=None;continue
                x,y,turn=location
                if monitor.tail_size:histories[slot].append((index,x,y,turn))
                layouts[slot][0][index]=_cell_origin(boxes[index],scale,grid,x,y,turn)
                layouts[slot][1][index]=turn
            if not any(layout is not None for layout in layouts):break
        monitor.finish_attempt(True,sum(layout is not None for layout in layouts))
        monitor.finish_round()
        return layouts
    except BaseException:
        monitor.abort_attempt(sum(layout is not None for layout in layouts));monitor.abort_round();raise
    finally:monitor.parallel=1


def _evolved_orders(boxes,seed,variation):
    """Keep the successful placement sequence and perturb nearby items.

    Small mutations preserve useful rows while changing blocking neighbours.
    A fresh priority order in every batch still explores different layouts.
    """
    base=list(seed.get('order',seed['positions']))
    if len(base)!=len(boxes) or set(base)!=set(range(len(boxes))):
        base=_order(boxes,2)
    orders=[base]
    rng=random.Random(variation)
    if variation%10:
        # Explore close neighbours of the successful large-shell sequence.
        # Replaying an unchanged order at a larger scale usually repeats the
        # same failed fit; occasional replay still exploits finer precision.
        order=list(base);a=rng.randrange(max(1,len(order)//2))
        b=max(0,min(len(order)-1,a+rng.randint(-max(2,len(order)//100),max(2,len(order)//100))))
        order.insert(b,order.pop(a));orders[0]=order
    for fraction in (.002,):
        order=list(base)
        for _ in range(max(1,int(len(order)*fraction))):
            a=rng.randrange(len(order))
            span=max(2,len(order)//50)
            b=max(0,min(len(order)-1,a+rng.randint(-span,span)))
            order[a],order[b]=order[b],order[a]
        orders.append(order)
    orders.extend((_order(boxes,variation),_order(boxes,variation+1)))
    return orders


def _promote_failed(monitor,variation):
    """Move a late blocking shell ahead of its previous neighbours."""
    best=monitor.best
    failures=[f for f in monitor.failed_orders if f['placed']>=len(f['order'])//2 and
              best['scale']<=f['scale']<=best['scale']*1.025]
    if not failures:return None
    # A nearly complete failed candidate provides more useful feedback than
    # one that overflowed halfway through. Keep a small diverse frontier.
    failures.sort(key=lambda f:(-f['placed'],f['scale']))
    failure=failures[variation%min(3,len(failures))]
    order=list(failure['order']);at=order.index(failure['index'])
    distance=max(1,int(at*(.002,.01,.03,.1)[variation%4]))
    order.insert(max(0,at-distance),order.pop(at))
    return order,failure['objective']


def _repair_region(boxes,seed,ordering):
    """Vary compact spatial neighbourhoods, including edges of empty pockets."""
    rng=random.Random(ordering)
    centers={i:(x+boxes[i][int(seed['turns'][i])%2]*seed['scale']*.5,
                y+boxes[i][1-int(seed['turns'][i])%2]*seed['scale']*.5)
             for i,(x,y) in seed['positions'].items()}
    mode=ordering%4
    if mode==3:
        # A small sampling grid only chooses a neighbourhood. All placements
        # still use exact conservative contour masks and the requested gaps.
        def clearance(point):
            return min(max(abs(point[0]-cx)-boxes[i][int(seed['turns'][i])%2]*seed['scale']*.5,
                           abs(point[1]-cy)-boxes[i][1-int(seed['turns'][i])%2]*seed['scale']*.5)
                       for i,(cx,cy) in centers.items())
        extent=(max(x+boxes[i][int(seed['turns'][i])%2]*seed['scale'] for i,(x,y) in seed['positions'].items()),
                max(y+boxes[i][1-int(seed['turns'][i])%2]*seed['scale'] for i,(x,y) in seed['positions'].items()))
        anchor=max(((x*extent[0]/8,y*extent[1]/8) for x in range(1,8) for y in range(1,8)),key=clearance)
    else:anchor=centers[rng.randrange(len(boxes))]
    ax,ay=anchor
    priority=sorted(centers,key=lambda i:
                    -centers[i][1] if mode==0 else -centers[i][0] if mode==1 else
                    (centers[i][0]-ax)**2+(centers[i][1]-ay)**2)
    fraction=(32,16,8)[(ordering//4)%3]
    return set(priority[:max(2,len(boxes)//fraction)])


@counted_trial(8)
def repair(boxes,triangles,scale,grid,gap,margin,rotate,static_triangles,
           monitor,ordering,backend,seed):
    """Increase scale, preserve valid fixed placements, repair the freed region.

    Every fixed origin is rechecked against conservative GPU padding. Rounding
    and existing close neighbours can therefore only add to the movable set,
    never silently introduce a gap violation.
    """
    monitor.parallel=1
    monitor.local_attempts+=1
    backend.reset(_static_rows(static_triangles,grid,monitor,backend),grid)
    backend.bind_geometry(boxes,triangles)
    gap_cells,margin_cells=math.ceil(gap*grid),math.ceil(margin*grid)
    ratio=scale/seed['scale']
    moving=_repair_region(boxes,seed,ordering)
    positions,turns={},{}
    order=list(seed.get('order',seed['positions']))
    if len(order)!=len(boxes) or set(order)!=set(range(len(boxes))):order=_order(boxes,ordering)
    fixed=[]
    for done,index in enumerate(order):
        monitor.update('Refining the best layout',done,len(boxes))
        if index in moving:continue
        turn=int(seed['turns'][index])
        px,py=seed['positions'][index]
        px,py=margin+(px-margin)*ratio,margin+(py-margin)*ratio
        dx,dy=_cell_origin(boxes[index],scale,grid,0,0,turn)
        x,y=math.ceil((px-dx)*grid-1e-8),math.ceil((py-dy)*grid-1e-8)
        fixed.append((index,turn,x,y))
    if hasattr(backend,'fixed_groups'):
        accepted=backend.fixed_groups(fixed,scale,gap_cells,margin_cells,rotate,monitor)
    else:
        accepted=[backend.fixed_group(index,scale,gap_cells,margin_cells,turn,x,y,rotate,monitor)
                  for index,turn,x,y in fixed]
    for (index,turn,x,y),fits in zip(fixed,accepted):
        if fits:
            positions[index]=_cell_origin(boxes[index],scale,grid,x,y,turn);turns[index]=turn
            monitor.fixed_shells+=1
        else:moving.add(index)
    # Freeing a region can reject a large fixed chart as well as small nearby
    # charts. Reinsert difficult large shapes before small pocket fillers.
    for index in _order(boxes,ordering):
        if index not in moving:continue
        monitor.update('Repairing a local region',len(positions),len(boxes))
        if hasattr(backend,'place_many'):
            previous_modes=getattr(backend,'placement_modes',None)
            backend.placement_modes=[seed.get('objective',0)]
            try:location=backend.place_many([index],scale,gap_cells,margin_cells,rotate,monitor)[0]
            finally:backend.placement_modes=previous_modes
        else:location=backend.place_group(index,scale,gap_cells,margin_cells,rotate,monitor)
        if location is None:return None
        x,y,turn=location
        positions[index]=_cell_origin(boxes[index],scale,grid,x,y,turn);turns[index]=turn
    return positions,turns


def search(boxes, triangles, areas, gap, margin, rotate, static_triangles, monitor,
           grid=1024, backend=None):
    import time
    from uv_straight_pack_search import StageExpired, SearchStopped
    upper = scale_limit(boxes,monitor.target_size,margin,rotate,areas)
    packed_area=sum(areas)
    resumed=getattr(monitor,'resumed',False)
    upgraded=getattr(monitor,'search_upgrade',False)
    if not resumed and backend is not None and hasattr(backend,'shelves_many') and len(boxes)>=SAMPLED_SEARCH_GROUPS and not static_triangles:
        # Explore complete large layouts without thousands of host/GPU roundtrips.
        # Contour placement subsequently improves on the safe box-based floor.
        low=monitor.best['scale'] if monitor.best else 0.
        high=upper;growing=True
        previous_deadline=monitor.stage_deadline
        budget=min(3.,max(.1,(monitor.deadline-time.monotonic())*.12))
        monitor.stage_deadline=time.monotonic()+budget
        try:
            for step in range(6):
                monitor.check();monitor.precision=None
                candidate=min(high,low*1.03) if growing and low else (low+high)*.5
                orders=[_order(boxes,ordering+step*32) for ordering in range(32)]
                monitor.parallel=len(orders)
                results=backend.shelves_many(boxes,candidate,grid,gap,margin,rotate,orders,monitor)
                complete=False
                for order,result in zip(orders,results):
                    if result is not None:
                        complete=monitor.consider(candidate,*result,order=order) or complete
                if complete:low=max(low,candidate)
                else:high=candidate;growing=False
                if high-low<=max(1e-7,high*.0005):break
        except StageExpired:pass
        finally:
            monitor.stage_deadline=previous_deadline
            monitor.parallel=1
    contour_floor=monitor.best['scale'] if monitor.best is not None else 0.
    compact_seed=False
    if not resumed and getattr(monitor,'tabi_seed',False) and backend is not None and hasattr(backend,'tabi_many') and len(boxes)>=12:
        from uv_straight_pack_tabi import seed as compact_start
        try:compact_seed=compact_start(boxes,triangles,areas,gap,margin,rotate,static_triangles,monitor,grid,backend)
        except StageExpired:compact_seed=monitor.tabi_validated>0
    # Large selections first obtain a usable shape layout on a coarser grid.
    # Coarse padding rounds upward, so all saved plans still meet pixel gaps.
    stages=[grid]
    large_gpu=backend is not None and hasattr(backend,'place_many') and len(boxes)>=SAMPLED_SEARCH_GROUPS
    if large_gpu:
        # A rectangle seed is only a safe floor, not a dense contour layout.
        # Obtain useful scale cheaply before paying for 4K/8K contour masks.
        stages=list(dict.fromkeys((min(grid,1024),min(grid,2048),grid)))
    elif len(boxes)>200 and grid>1024 and monitor.best is None:
        stages=[1024,grid]
    if resumed:stages=[]
    unlimited=not monitor.seconds
    remaining=30. if unlimited else max(0.,monitor.deadline-time.monotonic())
    if large_gpu:
        # Search Limit controls total effort, not how slowly startup explores
        # scales. In Unlimited the last precision stage used to have no bound,
        # delaying order refinement for tens of seconds on large selections.
        remaining=min(20.,remaining)
    start=time.monotonic()
    shares=([.3,.65,1.] if len(stages)==3 else [.45,1.] if len(stages)==2 else [1.])
    parallel=backend is not None and hasattr(backend,'place_many') and len(boxes)>=12
    def attempt(candidate,precision,ordering,orders=None,modes=None):
        if precision<grid:monitor.coarse_attempts+=1
        else:monitor.fine_attempts+=1
        if parallel:
            backend.placement_modes=(modes if modes is not None else
                [monitor.best.get('objective',0)]*2+[0,1] if orders is not None and monitor.best else None)
            orderings=tuple(range(ordering,ordering+4))
            orders=orders if orders is not None else [_order(boxes,o) for o in orderings]
            results=trial_many(boxes,triangles,candidate,precision,gap,margin,rotate,
                       static_triangles,monitor,orderings,backend,orders)
        else:
            orders=[list(orders[0]) if orders is not None else _order(boxes,ordering)]
            results=[trial(boxes,triangles,candidate,precision,gap,margin,rotate,
                       static_triangles,monitor,ordering,backend,order=orders[0])]
        complete=False
        for slot,(order,result) in enumerate(zip(orders,results)):
            if result is not None:
                mode=getattr(backend,'current_modes',[0]*len(orders))[slot] if parallel else 0
                # Preserve the effective order after material-group priorities
                # or tail rescue, rather than the originally requested sequence.
                complete=monitor.consider(candidate,*result,order=tuple(result[0]),objective=mode) or complete
        return complete
    try:
        for requested_precision,share in zip(stages,shares):
            monitor.check()
            end=start+remaining*share
            monitor.stage_deadline=end if share<1. or (large_gpu and end<monitor.deadline) else None
            low=monitor.best['scale'] if monitor.best is not None else 0.
            # A fine-grid proxy seed is an additional complete layout. Keep
            # the established first coarse scale probes: rounding differs
            # there, and changing these probes can redirect the whole search.
            if compact_seed and large_gpu and len(stages)>1 and requested_precision==stages[0]:low=contour_floor
            coverage=(packed_area*low*low+monitor.static_area)/monitor.target_area
            precision=requested_precision
            monitor.precision=precision
            high=upper
            growing=True
            growth=((1.002 if precision>=4096 else 1.06 if coverage<.6 else 1.03 if coverage<.75 else 1.002) if large_gpu else
                    1.002 if len(boxes)>200 else 1.01 if precision != stages[0] else 1.04)
            candidate=min(high,low*growth) if low else high*.35
            try:
                # Coarse grids establish scale, not the final ordering. Do not
                # spend their whole time allowance bisecting one local basin;
                # a consistent probe count also avoids machine speed choosing
                # a different starting scale for the next precision stage.
                steps=6 if large_gpu and precision<grid else 18
                for step in range(steps):
                    monitor.check()
                    if candidate<=0:break
                    stage_end=monitor.stage_deadline
                    # Finish the current large-layout probe before switching
                    # grids. Cutting it off near the last shell throws away
                    # seconds of useful work; the global limit/user stop still
                    # interrupts inside every placement as before.
                    if large_gpu:monitor.stage_deadline=None
                    orders=(_evolved_orders(boxes,monitor.best,step+precision)
                            if upgraded and parallel and monitor.best is not None and precision>stages[0] else None)
                    try:complete=attempt(candidate,precision,step*4 if parallel else step,orders)
                    finally:monitor.stage_deadline=stage_end
                    if not complete:
                        high=candidate;growing=False
                    else:
                        low=max(low,candidate)
                    if high-low<=max(1e-7,high*.0005):break
                    candidate=min(high,low*1.10) if growing else (low+high)*.5
            except StageExpired:
                monitor.stage_deadline=None
                continue
            monitor.stage_deadline=None
        # The initial scale search is only a seed. Continue trying distinct
        # shell orderings until the deadline or the user's stop, including
        # after a bisection has converged. Never replace a complete better plan
        # with an unfinished or smaller one.
        variation=18+getattr(monitor,'resume_rounds',0)*4
        probe_scale=probe_seed=0.
        probe_rounds=0
        probe_area_step=.012
        hull_order=None
        final_grid=grid
        monitor.precision=final_grid
        while True:
            monitor.check()
            large=parallel and len(boxes)>=SAMPLED_SEARCH_GROUPS
            low=monitor.best['scale'] if monitor.best is not None else 0.
            if not low:
                candidate=upper*.2
            else:
                if large and packed_area>0:
                    # A failed order does not prove this scale cannot fit.
                    # Reuse its exact masks for several different orders before
                    # changing scale and rebuilding hundreds of GPU contours.
                    if low>probe_seed or probe_rounds<=0:
                        # Probe in atlas-area units. Push farther after a gain,
                        # then reduce the step after several unsuccessful orders.
                        if low>probe_seed:
                            probe_area_step=min(.015,max(.004,probe_area_step*1.25))
                        else:
                            probe_area_step*=.55
                            if probe_area_step<.00125:probe_area_step=.012
                        added_area=monitor.target_area*probe_area_step
                        probe_scale=min(upper,math.sqrt(low*low+added_area/packed_area))
                        probe_seed=low;probe_rounds=4
                    candidate=probe_scale;probe_rounds-=1
                else:
                    candidate=min(upper,low*(1.+(.002,.005,.01,.02,.04)[variation%5]))
            precision=final_grid
            if large and monitor.best is not None:
                coverage=(packed_area*monitor.best['scale']**2+monitor.static_area)/monitor.target_area
                if coverage<.64 or (coverage<.73 and variation%2):precision=min(grid,2048)
                elif coverage>=.82 and variation%5==0:precision=min(8192,grid*2)
            monitor.precision=precision
            local=(upgraded and backend is not None and hasattr(backend,'fixed_group') and
                   monitor.best is not None and monitor.best.get('precision') and variation%4==0 and
                   monitor.local_seconds<(monitor.seconds or 30.)*.18)
            if local:
                seeds=[seed for seed in monitor.pool if seed.get('precision') and
                       seed['scale']>=monitor.best['scale']*.998]
                seed=seeds[(variation//4)%len(seeds)] if seeds else monitor.best
                candidate=min(upper,monitor.best['scale']*(1.+(.0005,.001,.002)[(variation//4)%3]))
                previous_deadline=monitor.stage_deadline
                local_started=time.monotonic()
                allowance=min(3.,max(.8,len(boxes)/600.,(monitor.seconds or 30.)*.06))
                monitor.stage_deadline=min(monitor.deadline,local_started+allowance)
                try:
                    result=repair(boxes,triangles,candidate,precision,gap,margin,rotate,
                                  static_triangles,monitor,variation,backend,seed)
                    if result is not None:
                        monitor.local_successes+=1
                        monitor.consider(candidate,*result,objective=seed.get('objective',0))
                except StageExpired:pass
                finally:
                    monitor.local_seconds+=time.monotonic()-local_started
                    monitor.stage_deadline=previous_deadline
            elif large and monitor.best is not None:
                # Mix learned orders with fresh ones; half-cell contours reclaim
                # raster slack around thousands of small shells without reducing
                # the requested physical spacing or border margins.
                orders=_evolved_orders(boxes,monitor.best,variation)
                if variation%2==0:
                    if hull_order is None:
                        hull_areas=[_hull_area(shape) for shape in triangles]
                        hull_order=sorted(range(len(boxes)),key=lambda i:(-hull_areas[i],i))
                    orders[-1]=hull_order
                promoted=_promote_failed(monitor,variation)
                modes=None
                if promoted:
                    orders[1]=promoted[0]
                    modes=[monitor.best.get('objective',0),promoted[1],0,1]
                attempt(candidate,precision,variation,orders,modes)
            elif (not upgraded and backend is not None and hasattr(backend,'fixed_group') and monitor.pool and
                    variation%3==0):
                seeds=[p for p in monitor.pool if p.get('precision')]
                if seeds:
                    seed=seeds[(variation//3)%len(seeds)]
                    result=repair(boxes,triangles,candidate,grid,gap,margin,rotate,
                                  static_triangles,monitor,variation,backend,seed)
                    if result is not None:
                        monitor.local_successes+=1
                        monitor.consider(candidate,*result,objective=seed.get('objective',0))
                else:attempt(candidate,grid,variation)
            elif upgraded and monitor.best is not None and variation%2==0:
                # Smaller GPU batches and CPU searches also benefit from the
                # successful order. Alternate with fresh priorities for diversity.
                seeds=monitor.pool or [monitor.best]
                seed=seeds[(variation//2)%len(seeds)]
                orders=_evolved_orders(boxes,seed,variation)
                promoted=_promote_failed(monitor,variation)
                if promoted:orders[0]=promoted[0]
                attempt(candidate,grid,variation,orders)
            else:attempt(candidate,grid,variation)
            variation+=1
    except StageExpired:
        # A stage timeout must advance resolution, not end the whole search.
        pass
    except SearchStopped:
        pass
    finally:
        monitor.stage_deadline=None
        monitor.parallel=1
    return monitor.best
