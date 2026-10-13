"""Independent conservative triangle raster/bit-row contour packing.

Pure numerical data only; no Maya calls. A completed placement is checked against
occupied conservative cells, with a Euclidean gap expansion. Integer bit rows
test all horizontal origins in parallel through forbidden-origin dilation.
"""
import math
import random
import numpy as np
from uv_straight_pack_search import counted_trial
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
    # Horizontal orientations win equal-position ties; vertical alternatives
    # remain available when they fill a pocket the horizontal shape cannot.
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
    if not static_triangles:return [0]*grid
    raster=backend.raster if backend is not None else raster_triangles
    return list(_rows(raster([[(x*grid,y*grid) for x,y in tri]
                             for tri in static_triangles],grid,grid,monitor)))


@counted_trial(8)
def trial(boxes, triangles, scale, grid, gap, margin, rotate, static_triangles,
          monitor, ordering=0, backend=None):
    gap_cells,margin_cells = math.ceil(gap*grid),math.ceil(margin*grid)
    extent_x,extent_y=grid_extent(grid,monitor)
    occupied = _static_rows(static_triangles,grid,monitor,backend)
    raster=backend.raster if backend is not None else raster_triangles
    if backend is not None: backend.reset(occupied,grid)
    resident=backend is not None and hasattr(backend,'place_group')
    if resident:backend.bind_geometry(boxes,triangles)
    order=_order(boxes,ordering)
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
            if result is None:return None
            x,y,turn=result
            dx=(ih-h) if turn==1 else (iw-w) if turn==2 else 0.
            dy=(ih-h) if turn==2 else (iw-w) if turn==3 else 0.
            positions[index],turns[index]=((x+dx)/grid,(y+dy)/grid),turn
            continue
        base = raster([[(x*scale*grid,y*scale*grid) for x,y in tri]
                                 for tri in triangles[index]],iw,ih,monitor)
        best = None
        seen = set()
        # Prefer horizontal strips at equal bottom-left positions.
        options = rotation_options(boxes[index],rotate)
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
        if best is None: return None
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
    layouts=[({}, {}) for _ in orders]
    monitor.attempt(len(orders))
    try:
        occupied=_static_rows(static_triangles,grid,monitor,backend)
        backend.reset_many(occupied,grid,len(orderings));backend.bind_geometry(boxes,triangles)
        gap_cells,margin_cells=math.ceil(gap*grid),math.ceil(margin*grid)
        for placed in range(len(boxes)):
            monitor.update('Packing layouts in parallel',placed,len(boxes))
            indices=[order[placed] if layout is not None else None for order,layout in zip(orders,layouts)]
            locations=backend.place_many(indices,scale,gap_cells,margin_cells,rotate,monitor)
            for slot,(index,location) in enumerate(zip(indices,locations)):
                if layouts[slot] is None:continue
                if location is None:
                    monitor.finish_attempt(False)
                    modes=getattr(backend,'current_modes',[0]*len(orders))
                    _remember_failure(monitor,dict(scale=scale,order=tuple(orders[slot]),
                        index=index,placed=placed,objective=modes[slot]))
                    layouts[slot]=None;continue
                x,y,turn=location
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
    # Rotate among spatial clusters at the top, right and a selected shell.
    rng=random.Random(ordering)
    anchor=list(seed['positions'])[rng.randrange(len(boxes))]
    ax,ay=seed['positions'][anchor]
    mode=ordering%3
    priority=sorted(range(len(boxes)),key=lambda i:
        -(seed['positions'][i][1] if mode==0 else seed['positions'][i][0]) if mode<2 else
        (seed['positions'][i][0]-ax)**2+(seed['positions'][i][1]-ay)**2)
    moving=set(priority[:max(1,len(boxes)//8)])
    positions,turns={},{}
    for done,index in enumerate(_order(boxes,ordering)):
        monitor.update('Refining the best layout',done,len(boxes))
        if index in moving:continue
        turn=int(seed['turns'][index])
        px,py=seed['positions'][index]
        px,py=margin+(px-margin)*ratio,margin+(py-margin)*ratio
        dx,dy=_cell_origin(boxes[index],scale,grid,0,0,turn)
        x,y=math.ceil((px-dx)*grid-1e-8),math.ceil((py-dy)*grid-1e-8)
        if backend.fixed_group(index,scale,gap_cells,margin_cells,turn,x,y,rotate,monitor):
            positions[index]=_cell_origin(boxes[index],scale,grid,x,y,turn);turns[index]=turn
            monitor.fixed_shells+=1
        else:moving.add(index)
    for index in _order(boxes,ordering):
        if index not in moving:continue
        monitor.update('Repairing a local region',len(positions),len(boxes))
        location=backend.place_group(index,scale,gap_cells,margin_cells,rotate,monitor)
        if location is None:return None
        x,y,turn=location
        positions[index]=_cell_origin(boxes[index],scale,grid,x,y,turn);turns[index]=turn
    return positions,turns


def search(boxes, triangles, areas, gap, margin, rotate, static_triangles, monitor,
           grid=1024, backend=None):
    import time
    from uv_straight_pack_search import StageExpired, SearchStopped
    upper = scale_limit(boxes,monitor.target_size,margin,rotate,areas)
    if backend is not None and hasattr(backend,'shelves_many') and len(boxes)>=512 and not static_triangles:
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
                    if result is not None:complete=True;monitor.consider(candidate,*result,order=order)
                if complete:low=max(low,candidate)
                else:high=candidate;growing=False
                if high-low<=max(1e-7,high*.0005):break
        except StageExpired:pass
        finally:
            monitor.stage_deadline=previous_deadline
            monitor.parallel=1
    # Large selections first obtain a usable shape layout on a coarser grid.
    # Coarse padding rounds upward, so all saved plans still meet pixel gaps.
    stages=[grid]
    if len(boxes)>200 and grid>1024 and monitor.best is None:
        stages=[1024,grid]
    elif backend is not None and hasattr(backend,'place_many') and len(boxes)>=512 and grid<8192:
        stages=[grid,min(8192,grid*2)]
    unlimited=not monitor.seconds
    remaining=30. if unlimited else max(0.,monitor.deadline-time.monotonic())
    start=time.monotonic()
    shares=[.5,1.] if len(stages)==2 else [1.]
    parallel=backend is not None and hasattr(backend,'place_many') and len(boxes)>=12
    def attempt(candidate,precision,ordering,orders=None,modes=None):
        if parallel:
            backend.placement_modes=(modes if modes is not None else
                [monitor.best.get('objective',0)]*2+[0,1] if orders is not None and monitor.best else None)
            orderings=tuple(range(ordering,ordering+4))
            orders=orders if orders is not None else [_order(boxes,o) for o in orderings]
            results=trial_many(boxes,triangles,candidate,precision,gap,margin,rotate,
                       static_triangles,monitor,orderings,backend,orders)
        else:
            orders=[_order(boxes,ordering)]
            results=[trial(boxes,triangles,candidate,precision,gap,margin,rotate,
                       static_triangles,monitor,ordering,backend)]
        complete=False
        for slot,(order,result) in enumerate(zip(orders,results)):
            if result is not None:
                mode=getattr(backend,'current_modes',[0]*len(orders))[slot] if parallel else 0
                complete=True;monitor.consider(candidate,*result,order=order,objective=mode)
        return complete
    try:
        for precision,share in zip(stages,shares):
            monitor.check()
            monitor.stage_deadline=start+remaining*share if share<1. else None
            monitor.precision=precision
            low=monitor.best['scale'] if monitor.best is not None else 0.
            high=upper
            growing=True
            growth=(1.002 if len(boxes)>200 else
                    1.01 if precision != stages[0] else 1.04)
            candidate=min(high,low*growth) if low else high*.35
            try:
                for step in range(18):
                    monitor.check()
                    if candidate<=0:break
                    complete=attempt(candidate,precision,step*4 if parallel else step)
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
        variation=18
        final_grid=min(8192,grid*2) if parallel and len(boxes)>=512 else grid
        monitor.precision=final_grid
        while True:
            monitor.check()
            large=parallel and len(boxes)>=512
            low=monitor.best['scale'] if monitor.best is not None else 0.
            if not low:
                candidate=upper*.2
            else:
                growth=((.0005,.0005,.001,.002,.005) if large else
                        (.002,.005,.01,.02,.04))[variation%5]
                candidate=min(upper,low*(1.+growth))
            precision=final_grid
            monitor.precision=precision
            if large and monitor.best is not None:
                # Mix learned orders with fresh ones; half-cell contours reclaim
                # raster slack around thousands of small shells without reducing
                # the requested physical spacing or border margins.
                orders=_evolved_orders(boxes,monitor.best,variation)
                promoted=_promote_failed(monitor,variation)
                modes=None
                if promoted:
                    orders[1]=promoted[0]
                    modes=[monitor.best.get('objective',0),promoted[1],0,1]
                attempt(candidate,precision,variation,orders,modes)
            elif (backend is not None and hasattr(backend,'fixed_group') and monitor.pool and
                    variation%3==0):
                seeds=[p for p in monitor.pool if p.get('precision')]
                if seeds:
                    seed=seeds[(variation//3)%len(seeds)]
                    result=repair(boxes,triangles,candidate,grid,gap,margin,rotate,
                                  static_triangles,monitor,variation,backend,seed)
                    if result is not None:monitor.consider(candidate,*result)
                else:attempt(candidate,grid,variation)
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
