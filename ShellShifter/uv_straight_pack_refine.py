"""Bounded transactional recovery of a nearly complete contour candidate."""
import time


class GeometryBudget(Exception):
    pass


def certify_contours(groups, static=(), gap=0., margin=0., target_size=(1.,1.),
                     deadline=None, cancel=None):
    """Certify triangle unions, not bounding boxes or raster masks.

    Narrow-phase predicates use exact rational arithmetic on the supplied
    binary floats. Touching is allowed only at zero gap. Internal overlaps
    within a pack group are intentional and are not tested against themselves.
    An exhausted budget is inconclusive, never a successful certificate.
    Coordinates are local to the destination tile; static triangles may extend
    beyond it and are not required to satisfy its border margin.
    """
    import math
    import numpy as np
    from fractions import Fraction as F
    checked=0;cache={}
    def check():
        if (cancel is not None and cancel.is_set()) or (deadline is not None and time.monotonic()>=deadline):
            raise GeometryBudget()
    def orient(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def rational(tri):
        key=tuple(float(v) for p in tri for v in p)
        if key not in cache:cache[key]=tuple((F(key[i]),F(key[i+1])) for i in (0,2,4))
        return cache[key]
    def inside(p,t):
        area=orient(*t)
        if not area:return False
        return all(orient(t[i],t[(i+1)%3],p)*area>=0 for i in range(3))
    def on(a,b,p):return orient(a,b,p)==0 and all(min(a[k],b[k])<=p[k]<=max(a[k],b[k]) for k in (0,1))
    def intersects(a,b,c,d):
        x,y,z,w=orient(a,b,c),orient(a,b,d),orient(c,d,a),orient(c,d,b)
        return (x*y<0 and z*w<0) or (x==0 and on(a,b,c)) or (y==0 and on(a,b,d)) or (z==0 and on(c,d,a)) or (w==0 and on(c,d,b))
    def distance2(p,a,b):
        dx,dy=b[0]-a[0],b[1]-a[1];px,py=p[0]-a[0],p[1]-a[1]
        length=dx*dx+dy*dy;dot=px*dx+py*dy
        if not length or dot<=0:return px*px+py*py
        if dot>=length:return (p[0]-b[0])**2+(p[1]-b[1])**2
        return (dx*py-dy*px)**2/length
    def violates(a,b,gap2):
        a=rational(a);b=rational(b)
        aa,bb=orient(*a),orient(*b)
        if aa and bb:
            separated=False
            for t,other,area in ((a,b,aa),(b,a,bb)):
                if any(all(orient(t[i],t[(i+1)%3],p)*area<=0 for p in other) for i in range(3)):
                    separated=True;break
            if not separated:return True
        if not gap2:return False
        if any(inside(p,b) for p in a) or any(inside(p,a) for p in b):return True
        for i in range(3):
            for j in range(3):
                if intersects(a[i],a[(i+1)%3],b[j],b[(j+1)%3]):return True
        return any(distance2(p,t[i],t[(i+1)%3])<gap2
                   for vertices,t in ((a,b),(b,a)) for p in vertices for i in range(3))
    try:
        check()
        if not all(math.isfinite(float(x)) for x in (*target_size,gap,margin)) or gap<0 or margin<0 or min(target_size)<=2*margin:
            return dict(valid=False,reason='parameters',pairs=0)
        arrays=[]
        for g in list(groups)+[static]:
            check();a=np.asarray(g,dtype=float)
            if not a.size:a=np.empty((0,3,2))
            if a.ndim!=3 or a.shape[1:]!=(3,2) or not np.all(np.isfinite(a)):
                return dict(valid=False,reason='nonfinite_or_shape',pairs=checked)
            arrays.append(a)
        for a in arrays[:-1]:
            if not len(a):return dict(valid=False,reason='empty_group',pairs=checked)
            if np.any(a<margin) or np.any(a>np.asarray(target_size)-margin):
                return dict(valid=False,reason='margin',pairs=checked)
        lows=[a.min(axis=1) if len(a) else np.empty((0,2)) for a in arrays]
        highs=[a.max(axis=1) if len(a) else np.empty((0,2)) for a in arrays]
        boundlo=[a.min(axis=0) if len(a) else None for a in lows]
        boundhi=[a.max(axis=0) if len(a) else None for a in highs]
        magnitude=max([1.,gap]+[float(np.max(np.abs(a))) for a in arrays if len(a)])
        # Broad-phase expansion only adds pairs. Exact arithmetic decides all
        # potentially close pairs, including equality with the requested gap.
        broad=gap+64.*np.finfo(float).eps*magnitude
        gap2=F(float(gap))**2
        for i in range(len(groups)):
            for j in range(i+1,len(arrays)):
                check()
                if boundlo[j] is None or np.any(np.maximum(boundlo[i]-boundhi[j],boundlo[j]-boundhi[i])>broad):continue
                for k,t in enumerate(arrays[i]):
                    check()
                    near=np.flatnonzero(np.all(np.maximum(lows[i][k]-highs[j],lows[j]-highs[i][k])<=broad,axis=1))
                    for at in near:
                        check();checked+=1
                        if violates(t,arrays[j][at],gap2):return dict(valid=False,reason='overlap_or_gap',groups=(i,j),triangles=(k,int(at)),pairs=checked)
        return dict(valid=True,reason='certified',pairs=checked)
    except GeometryBudget:return dict(valid=False,reason='budget_or_cancel',pairs=checked)


def placed_contours(data,result):
    """Triangle geometry rounded as destination UV floats, for candidate checks."""
    import numpy as np
    origin=np.asarray(data.get('target_origin',(0.,0.)),dtype=float)
    groups=[];scale=result['scale']
    for i,triangles in enumerate(data['triangles']):
        p=np.asarray(triangles,dtype=float).reshape(-1,3,2)*scale
        w,h=np.asarray(data['boxes'][i])*scale;turn=int(result['turns'][i])%4
        if turn==1:p=np.stack((h-p[:,:,1],p[:,:,0]),axis=-1)
        elif turn==2:p=np.stack((w-p[:,:,0],h-p[:,:,1]),axis=-1)
        elif turn==3:p=np.stack((p[:,:,1],w-p[:,:,0]),axis=-1)
        p=(p+np.asarray(result['positions'][i])+origin).astype(np.float32).astype(float)-origin
        groups.append(p)
    return groups


def refine_border(data,result,seconds=.35,cancel=None,scale_cap=None):
    """Reclaim raster border slack by a certified uniform growth, no UV stretch.

    This bounded first contour refinement retains all rotations and relative
    positions. No result is replaced if certification is inconclusive.
    """
    import math
    import numpy as np
    from copy import deepcopy
    if (data.get('preserve_scale') or data.get('method')!='contours' or
            data.get('static_triangles') or data.get('obstacles') or
            data.get('pack_groups') or result.get('stopped') or result.get('cancelled') or
            len(data['boxes'])>64 or sum(map(len,data['triangles']))>2048):return result
    if cancel is not None and cancel.is_set():return result
    deadline=time.monotonic()+max(0.,seconds)
    margin=data['margin'];target=np.asarray(data.get('target_size',(1.,1.)))
    origin=data.get('target_origin',(0.,0.))
    guard=8.*float(np.spacing(np.float32(max(1.,*(abs(float(v))+max(target) for v in origin)))))
    extents=[tuple(reversed(box)) if int(result['turns'][i])%2 else box for i,box in enumerate(data['boxes'])]
    maximum=np.array([max(result['positions'][i][axis]+extents[i][axis]*result['scale']
                         for i in range(len(extents))) for axis in (0,1)])
    if np.any(maximum<=margin):return result
    anchor=margin+guard
    factor=min(1.02,float(np.min((target-margin-guard-anchor)/(maximum-margin))))
    if scale_cap is not None:factor=min(factor,scale_cap/result['scale'])
    if not math.isfinite(factor) or factor<=1.+1e-7:return result
    for _ in range(6):
        candidate=deepcopy(result);candidate['scale']=result['scale']*factor
        candidate['positions']={i:tuple(anchor+(np.asarray(p)-margin)*factor) for i,p in result['positions'].items()}
        report=certify_contours(placed_contours(data,candidate),gap=data['gap'],margin=margin,target_size=target,deadline=deadline,cancel=cancel)
        if report['valid']:
            candidate['_certified_contours']=True
            candidate['contour_refinement']=dict(scale_gain_percent=(factor-1.)*100.,pairs=report['pairs'])
            total=float(np.prod(target))
            occupied=result.get('occupied_area',sum(data.get('areas',[]))*result['scale']**2)*factor**2
            candidate['occupied_area']=occupied
            candidate['free_percent']=max(0.,min(100.,100.*(1.-occupied/total)))
            if data.get('areas') is not None:
                candidate['legacy_free_percent']=max(0.,100.*(1.-sum(data['areas'])*candidate['scale']**2/total))
            candidate['area_method']='scaled_union_estimate'
            return candidate
        if report['reason']=='budget_or_cancel':return result
        factor=1.+(factor-1.)*.5
    return result


def refine_contacts(data,result,cancel=None):
    """Keep border growth as a floor; compact a few edge groups below a cell.

    Every move is checked against triangle unions. Moves alone are discarded:
    only a certified, strictly larger scale can replace the border-only plan.
    """
    import numpy as np
    from copy import deepcopy
    base=refine_border(data,result) if cancel is None else refine_border(data,result,cancel=cancel)
    count=len(data['boxes'])
    if (count<2 or count>24 or sum(map(len,data['triangles']))>512 or
            data.get('preserve_scale') or data.get('method')!='contours' or
            data.get('static_triangles') or data.get('obstacles') or data.get('pack_groups') or
            result.get('stopped') or result.get('cancelled') or
            base['scale']>=result['scale']*1.02*(1.-1e-9) or
            (cancel is not None and cancel.is_set())):return base
    deadline=time.monotonic()+.15
    target=np.asarray(data.get('target_size',(1.,1.)),dtype=float)
    margin=data['margin'];origin=data.get('target_origin',(0.,0.))
    guard=8.*float(np.spacing(np.float32(max(1.,*(abs(float(v))+max(target) for v in origin)))))
    world=placed_contours(data,base)
    check=certify_contours(world,gap=data['gap'],margin=margin,target_size=target,deadline=deadline,cancel=cancel)
    if not check['valid']:return base
    working=deepcopy(base);moves=0;attempts=0
    high=[p.max(axis=(0,1)) for p in world]
    limiting=np.max(high,axis=0)/(target-margin)
    axes=sorted((0,1),key=lambda a:-limiting[a])
    # Work on the limiting edge first. Stable group IDs make repeated runs
    # deterministic; leave most groups unchanged on larger small selections.
    order=sorted(range(count),key=lambda i:(-max(high[i]/(target-margin)),i))[:8]
    precision=max(1,int(base.get('precision') or data.get('precision',1024)))
    step=1./precision
    exhausted=False
    for i in order:
        for axis in axes:
            available=float(world[i][:,:,axis].min())-margin-guard
            if available<=1e-12:continue
            for divisor in (1.,2.,4.):
                if time.monotonic()>=deadline or (cancel is not None and cancel.is_set()):
                    exhausted=True;break
                distance=min(step/divisor,available)
                candidate=deepcopy(working)
                point=list(candidate['positions'][i]);point[axis]-=distance
                candidate['positions'][i]=tuple(point)
                proposed=placed_contours(data,candidate)[i]
                trial=list(world);trial[i]=proposed;attempts+=1
                report=certify_contours(trial,gap=data['gap'],margin=margin,target_size=target,deadline=deadline,cancel=cancel)
                if report['valid']:
                    working=candidate;world=trial;moves+=1;break
                if report['reason']=='budget_or_cancel':exhausted=True;break
            if exhausted:break
        if exhausted:break
    if not moves or (cancel is not None and cancel.is_set()):return base
    grown=refine_border(data,working,seconds=.1,cancel=cancel,scale_cap=result['scale']*1.02)
    if grown['scale']<=base['scale']*(1.+1e-7):return base
    grown['contact_refinement']=dict(moves=moves,attempts=attempts,
        scale_gain_percent=(grown['scale']/base['scale']-1.)*100.)
    return grown


def fixed_contour_seed(data,monitor,seconds=.5,neighbor_repair=True):
    """Bounded continuous pocket placement at scale exactly one.

    Vertex contacts propose locations inside concavities and holes that a
    coarse raster can close. No completed layout is scaled or overwritten.
    """
    import numpy as np
    from collections import Counter
    from uv_straight_contour_pack import _order,rotation_options
    count=len(data['boxes'])
    if (not data.get('preserve_scale') or count<2 or count>24 or
            sum(map(len,data['triangles']))>512 or data.get('static_triangles') or
            data.get('obstacles') or data.get('pack_groups')):return None
    deadline=min(monitor.deadline,time.monotonic()+max(0.,seconds))
    target=np.asarray(data.get('target_size',(1.,1.)));margin=data['margin'];gap=data['gap']
    origin=data.get('target_origin',(0.,0.))
    guard=8.*float(np.spacing(np.float32(max(1.,*(abs(float(v))+max(target) for v in origin)))))
    anchor=margin+guard;clearance=gap+2.*guard
    def alive():
        monitor.check()
        return time.monotonic()<deadline
    def place(index,positions,turns,world,skip=0,collisions=None):
        ids=list(world);others=list(world.values())
        static=[tri for other in others for tri in other]
        owners=[i for i in ids for _ in world[i]]
        one=dict(data,boxes=[data['boxes'][index]],triangles=[data['triangles'][index]])
        for turn in rotation_options(data['boxes'][index],data['rotate']):
            size=np.asarray(data['boxes'][index][::-1] if turn%2 else data['boxes'][index])
            upper=target-margin-guard-size
            if np.any(upper<anchor):continue
            local=placed_contours(dict(one,target_origin=(0.,0.)),
                dict(scale=1.,positions={0:(0.,0.)},turns={0:turn}))[0]
            axes=[]
            for axis in (0,1):
                values={float(anchor),float(upper[axis])}
                own=np.unique(local[:,:,axis]);own=own[::max(1,(len(own)+7)//8)]
                for other in others:
                    vertices=np.unique(other[:,:,axis]);vertices=vertices[::max(1,(len(vertices)+15)//16)]
                    for v in vertices:
                        for u in own:
                            values.update((float(v+clearance-u),float(v-clearance-u)))
                values=sorted(v for v in values if anchor<=v<=upper[axis])
                axes.append(values[::max(1,(len(values)+31)//32)])
            candidates=sorted(((x,y) for y in axes[1] for x in axes[0]),key=lambda p:(p[1],p[0]))[:128]
            for point in candidates:
                if not alive():return False
                proposed=placed_contours(one,dict(scale=1.,positions={0:point},turns={0:turn}))[0]
                # Existing groups are already certified against one another.
                # Test only the proposed group, retaining exact contour checks.
                report=certify_contours([proposed],static=static,gap=gap,margin=margin,
                    target_size=target,deadline=deadline,cancel=monitor.cancel)
                if report['valid']:
                    if skip:skip-=1;continue
                    positions[index]=point;turns[index]=turn;world[index]=proposed
                    return True
                if report['reason']=='budget_or_cancel':return False
                if collisions is not None and report.get('triangles'):
                    at=report['triangles'][1]
                    if at<len(owners):collisions[owners[at]]+=1
        return False
    def repair(index,positions,turns,world,collisions):
        # Actual failed contact checks identify blockers. Recent neighbours
        # provide another bounded choice when the first collider hides others.
        ranked=list(dict.fromkeys([i for i,n in collisions.most_common(3)]+list(world)[-3:]))
        sets=[(i,) for i in ranked[:3]]
        sets.extend(tuple(ranked[:n]) for n in (2,3) if len(ranked)>=n)
        for removed in sets:
            for alternative in (0,1):
                if not alive():return None
                p={i:v for i,v in positions.items() if i not in removed}
                t={i:v for i,v in turns.items() if i not in removed}
                w={i:v for i,v in world.items() if i not in removed}
                if not place(index,p,t,w,skip=alternative):continue
                order=sorted(removed,key=lambda i:(-max(data['boxes'][i]),i))
                if all(place(i,p,t,w) for i in order):return p,t,w
        return None
    for ordering in (0,1):
        positions={};turns={};world={};repairs=0
        for index in _order(data['boxes'],ordering):
            if not alive():return None
            collisions=Counter()
            if place(index,positions,turns,world,collisions=collisions):continue
            recovered=repair(index,positions,turns,world,collisions) if neighbor_repair and repairs<2 and world else None
            if recovered is None:break
            positions,turns,world=recovered;repairs+=1
        if len(positions)==count:
            monitor.fixed_neighbor_repairs=repairs
            return positions,turns
    return None


def rescue_tail(boxes,history,blocked,slot,slots,scale,gap,margin,rotate,monitor,backend,following=()):
    size=min(int(monitor.tail_size),len(history))
    if not size or backend.shape_profiles is not None:return None
    budget=(monitor.seconds or 30.)*.1
    allowance=min(.5,max(0.,budget-monitor.tail_seconds))
    if allowance<.01:return None
    monitor.tail_attempts+=1
    started=time.monotonic();saved=history[-size:]
    previous=getattr(backend,'placement_modes',None)
    previous_excluded=backend.excluded_origins
    modes=list(getattr(backend,'current_modes',[0]*slots))
    backend.placement_modes=modes
    # No tile snapshot is needed: every committed raw mask is disjoint. The
    # successful prefix stays on the device at exactly the same trial scale.
    backend.edit_placed(saved,slot,scale,gap,margin,rotate,monitor,True)
    new=[]
    try:
        tail=[record[0] for record in saved]
        original=tuple([blocked]+tail)
        by_size=tuple([blocked]+sorted(tail,key=lambda j:(-max(boxes[j]),-boxes[j][0]*boxes[j][1])))
        orders=list(dict.fromkeys((original,by_size)))
        lookahead=bool(monitor.pocket_lookahead and following and
                       max(boxes[blocked])*scale*getattr(monitor,'shape_resolution',2048)<=64.)
        def place(index,excluded=None):
            indices=[None]*slots;indices[slot]=index
            backend.excluded_origins=excluded
            try:return backend.place_many(indices,scale,gap,margin,rotate,monitor)[slot]
            finally:backend.excluded_origins=previous_excluded
        for order in orders:
            exclusion=None
            for alternative in range(3 if lookahead else 1):
                if time.monotonic()-started>allowance:break
                if lookahead:monitor.pocket_attempts+=1
                location=place(blocked,exclusion)
                if location is None:break
                x,y,turn=location;new=[(blocked,x,y,turn)];probes=[]
                radius=max(1,int(max(boxes[blocked])*scale*backend.size))
                region=(x-radius,y-radius,x+radius,y+radius)
                if exclusion is not None:
                    old=exclusion[slot]
                    region=(min(old[0],region[0]),min(old[1],region[1]),max(old[2],region[2]),max(old[3],region[3]))
                exclusion=[(-1,-1,-1,-1)]*slots;exclusion[slot]=region
                # Temporarily reserve the upcoming groups before filling the
                # remaining pockets with small tail groups. Remove reservations
                # on success so normal search can place them in the same order.
                valid=True
                for index in (following if lookahead else ()):
                    location=place(index)
                    if location is None:valid=False;break
                    px,py,t=location;probes.append((index,px,py,t))
                    if time.monotonic()-started>allowance:valid=False;break
                if valid:
                    for index in order[1:]:
                        location=place(index)
                        if location is None:break
                        px,py,t=location;new.append((index,px,py,t))
                        if time.monotonic()-started>allowance:break
                if valid and len(new)==len(order):
                    backend.edit_placed(probes,slot,scale,gap,margin,rotate,monitor,True)
                    monitor.tail_successes+=1
                    if lookahead:monitor.pocket_successes+=1
                    return size,new
                backend.edit_placed(new+probes,slot,scale,gap,margin,rotate,monitor,True);new=[]
        backend.edit_placed(saved,slot,scale,gap,margin,rotate,monitor,False)
        return None
    finally:
        monitor.tail_seconds+=time.monotonic()-started
        backend.placement_modes=previous
        backend.excluded_origins=previous_excluded
