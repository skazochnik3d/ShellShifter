"""Optional final distribution of small groups, at an unchanged packing scale.

Spacious pockets receive group quotas proportional to their usable area. Each
shape's preferred gap depends on its pocket's quota and its own dimensions.
Every accepted translation is checked against conservative full-contour masks.
Reference counts allow removing one group without erasing another group or a
static obstacle. Each move is committed atomically; cancellation keeps the last
complete layout. No Maya, OpenCL, SciPy or proxy geometry is required here.
"""
import math
import time
import numpy as np


class SpreadStopped(Exception):
    pass


def _raster(triangles, width, height, check):
    """Closed triangle/row-slab intersections, with vectorized clipping."""
    mask=np.zeros((height,width),dtype=bool)
    for first in range(0,len(triangles),128):
        check()
        group=triangles[first:first+128]
        low=np.maximum(0,np.floor(group[:,:,1].min(axis=1)-1e-9)).astype(int)
        high=np.minimum(height,np.floor(group[:,:,1].max(axis=1)+1e-9)+1).astype(int)
        counts=np.maximum(0,high-low);total=int(counts.sum())
        if not total:continue
        ids=np.repeat(np.arange(len(group)),counts)
        rows=np.repeat(low,counts)+np.arange(total)-np.repeat(np.cumsum(counts)-counts,counts)
        a=group[ids];b=np.roll(a,-1,axis=1)
        inside=(a[:,:,1]>=rows[:,None]-1e-9)&(a[:,:,1]<=rows[:,None]+1+1e-9)
        minimum=np.where(inside,a[:,:,0],np.inf).min(axis=1)
        maximum=np.where(inside,a[:,:,0],-np.inf).max(axis=1)
        dy=b[:,:,1]-a[:,:,1]
        for offset in (0,1):
            t=(rows[:,None]+offset-a[:,:,1])/np.where(dy!=0,dy,1.)
            valid=(dy!=0)&(t>=-1e-9)&(t<=1+1e-9)
            x=a[:,:,0]+t*(b[:,:,0]-a[:,:,0])
            minimum=np.minimum(minimum,np.where(valid,x,np.inf).min(axis=1))
            maximum=np.maximum(maximum,np.where(valid,x,-np.inf).max(axis=1))
        valid=np.isfinite(minimum)&np.isfinite(maximum)
        left=np.maximum(0,np.floor(minimum[valid]-1e-9)).astype(int)
        right=np.minimum(width,np.floor(maximum[valid]+1e-9)+1).astype(int)
        for y,lo,hi in zip(rows[valid],left,right):
            if hi>lo:mask[y,lo:hi]=True
    return mask


def _distance(occupied,check):
    """Eight-neighbour chamfer distance, used only to rank empty pockets."""
    height,width=occupied.shape
    distance=np.where(occupied,0.,1e6).astype(np.float32)
    x=np.arange(width,dtype=np.float32);diagonal=math.sqrt(2.)
    for sequence in (range(height),range(height-1,-1,-1)):
        step=1 if sequence.step==1 else -1
        for y in sequence:
            if y%64==0:check()
            row=distance[y]
            if 0<=y-step<height:
                previous=distance[y-step]
                np.minimum(row,previous+1,out=row)
                np.minimum(row[1:],previous[:-1]+diagonal,out=row[1:])
                np.minimum(row[:-1],previous[1:]+diagonal,out=row[:-1])
            row[:]=np.minimum.accumulate(row-x)+x
            row[:]=np.minimum.accumulate((row+x)[::-1])[::-1]-x
    return distance


def _fits(occupied,mask,x,y,gap,margin,padded=None):
    """Check a padded mask; the extra visual spacing never changes the border."""
    from uv_straight_contour_pack import _expand,_rows
    height,width=occupied.shape;mh,mw=mask.shape
    if x<margin or y<margin or x+mw>width-margin or y+mh>height-margin:return False
    if padded is None:padded=_expand(_rows(mask),gap)
    left=max(0,x-gap);right=min(width,x+mw+gap)
    shift=left-(x-gap);length=right-left
    limit=(1<<length)-1
    for dy,bits in enumerate(padded):
        yy=y+dy-gap
        if not 0<=yy<height:continue
        bits=(bits>>shift)&limit
        if not bits:continue
        row=occupied[yy,left:right]>0
        other=int.from_bytes(np.packbits(row,bitorder='little').tobytes(),'little')
        if bits&other:return False
    return True


def _pockets(available,check):
    """Four-connected spacious cores; narrow channels separate nearby voids."""
    parents=[];runs=[];previous=[]
    def root(index):
        while parents[index]!=index:
            parents[index]=parents[parents[index]];index=parents[index]
        return index
    for y,row in enumerate(available):
        if y%32==0:check()
        changes=np.diff(np.r_[False,row,False].astype(np.int8))
        starts=np.flatnonzero(changes==1);ends=np.flatnonzero(changes==-1)
        current=[];offset=0
        for left,right in zip(starts,ends):
            index=len(parents);parents.append(index)
            runs.append((y,left,right,index));current.append((left,right,index))
            while offset<len(previous) and previous[offset][1]<=left:offset+=1
            j=offset
            while j<len(previous) and previous[j][0]<right:
                parents[root(previous[j][2])]=root(index);j+=1
        previous=current
    labels=np.zeros(available.shape,dtype=np.int32)
    for y,left,right,index in runs:labels[y,left:right]=root(index)+1
    areas=np.bincount(labels.ravel(),minlength=len(parents)+1);areas[0]=0
    kept=sorted(np.flatnonzero(areas>=4),key=lambda i:(-int(areas[i]),int(i)))[:512]
    mapping=np.full(len(areas),-1,dtype=np.int32)
    mapping[kept]=np.arange(len(kept))
    return mapping[labels],areas[kept]


def _quotas(areas,count):
    """Largest-remainder area allocation, conserving the available group count."""
    if not len(areas):return np.zeros(0,dtype=int)
    exact=np.asarray(areas,dtype=float)*(count/float(sum(areas)))
    result=np.floor(exact).astype(int)
    order=np.argsort(-(exact-result),kind='stable')
    result[order[:count-int(result.sum())]]+=1
    return result


def _candidates(data,result):
    """Select whole packing groups: compact pieces, thin strips and sparse bends.

    Bounds are in final texels as well as tile units. A narrow full-tile band
    must not become a donor simply because its area is small. Triangle area is
    only a conservative eligibility estimate, never collision geometry.
    """
    resolution=data.get('resolution',2048);scale=result['scale']
    side=min(data.get('target_size',(1.,1.)))
    compact=min(80./resolution,.06*side)
    span=min(1024./resolution,.25*side)
    thin=min(16./resolution,.012*side)
    kinds={}
    for index,(w,h) in enumerate(data['boxes']):
        lo=min(w,h)*scale;hi=max(w,h)*scale
        if lo<=0:continue
        if hi<=compact and hi<=4*lo:
            kinds[index]='compact';continue
        if hi>span:continue
        if lo<=thin and lo*hi<=compact**2:
            kinds[index]='strip';continue
        points=np.asarray(data['triangles'][index],dtype=float).reshape(-1,3,2)
        # Exact duplicate faces in stacks do not change the classification.
        order=np.lexsort((points[:,:,1],points[:,:,0]),axis=1)
        points=np.unique(np.take_along_axis(points,order[:,:,None],axis=1).reshape(-1,6),axis=0).reshape(-1,3,2)
        a=points[:,1]-points[:,0];b=points[:,2]-points[:,0]
        area=float(np.abs(a[:,0]*b[:,1]-a[:,1]*b[:,0]).sum())*.5*scale**2
        if area<=compact**2 and area<=lo*hi*.30 and area/hi<=2*thin:
            kinds[index]='bend'
    return kinds


def redistribute(data,result,cancel=None,callback=None,budget=10.):
    """Distribute compact groups, then fill remaining voids with thin donors."""
    if result.get('cancelled') or not result.get('positions'):return result
    started=time.monotonic();moved=[];eligible=[];improvements=[];pocket_stats=[]
    density_before=density_after=None;transfers=0;applied_gaps={}
    def check():
        if (cancel is not None and cancel.is_set()) or time.monotonic()-started>=budget:
            raise SpreadStopped()
    # About 512 coarse cells per tile axis propose candidates; fine masks reserve
    # one cell per texel up to 4K, and never less than a 2K contour grid.
    resolution=data.get('resolution',2048)
    grid=max(2048,min(4096,int(resolution)));block=grid//512
    target=data.get('target_size',(1.,1.))
    width=math.ceil(target[0]*grid);height=math.ceil(target[1]*grid)
    scale=result['scale'];kinds=_candidates(data,result)
    eligible=list(kinds)
    eligible.sort(key=lambda i:(kinds[i]!='compact',-max(data['boxes'][i]),i))
    result['positions']=dict(result['positions'])
    try:
        check()
        if not eligible:return result
        if callback:callback(dict(phase='Distributing small shells',postprocess=True))
        from uv_straight_pack_metrics import placed_triangles
        points=placed_triangles(data,result)
        # Large/static shapes stay fixed. uint32 also handles heavily stacked
        # layouts when Keep UV Stacks is disabled.
        occupied=np.zeros((height,width),dtype=np.uint32)
        masks={};cursor=0;selected=set(eligible)
        for index,triangles in enumerate(data['triangles']):
            end=cursor+len(triangles);group=points[cursor:end]*grid;cursor=end
            if index not in selected:continue
            check()
            lower=np.floor(group.min(axis=(0,1))-1e-9).astype(int)
            upper=np.floor(group.max(axis=(0,1))+1e-9).astype(int)+1
            x,y=np.maximum(0,lower);right,top=np.minimum((width,height),upper)
            mask=_raster(group-(x,y),right-x,top-y,check)
            masks[index]=(int(x),int(y),mask)
        # Rebuild fixed occupancy without movable groups. A union mask cannot
        # be subtracted safely: adjacent triangles may share conservative cells.
        fixed=[];donors=[];cursor=0
        for index,triangles in enumerate(data['triangles']):
            end=cursor+len(triangles)
            if index not in selected:fixed.append(points[cursor:end])
            elif kinds[index]!='compact':donors.append(points[cursor:end])
            cursor=end
        fixed.append(points[cursor:])
        occupied[:]=_raster(np.concatenate(fixed)*grid,width,height,check)
        margin=math.ceil(data['margin']*grid)
        minimum_gap=math.ceil(data['gap']*grid)
        ch=math.ceil(height/block);cw=math.ceil(width/block)
        border=max(1,math.ceil(data['margin']*grid/block))
        def coarse_mask():
            pooled=np.ones((ch*block,cw*block),dtype=bool)
            pooled[:height,:width]=occupied>0
            coarse=pooled.reshape(ch,block,cw,block).any(axis=(1,3))
            coarse[:border]=True;coarse[-border:]=True
            coarse[:,:border]=True;coarse[:,-border:]=True
            return coarse
        # Compact groups must not fragment a roomy recipient pocket. Define
        # these pockets before extracting thin donors. Otherwise
        # removing a row of strips can merge genuine voids through long channels
        # and allocate the new pieces straight back into those channels.
        if donors:occupied+=_raster(np.concatenate(donors)*grid,width,height,check)
        fixed_field=_distance(coarse_mask(),check)
        if donors:occupied[:]=_raster(np.concatenate(fixed)*grid,width,height,check)
        sides=[min(data['boxes'][i])*scale*grid for i in eligible if kinds[i]=='compact']
        if not sides:sides=[min(data['boxes'][i])*scale*grid for i in eligible]
        core=max(1.5,(minimum_gap+float(np.median(sides))*.25)/block)
        labels,areas=_pockets(fixed_field>=core,check)
        quotas=_quotas(areas,len(eligible))
        samples=[np.nonzero(labels==i) for i in range(len(areas))]
        counts=np.zeros(len(areas),dtype=int);membership={}
        compact_counts=np.zeros(len(areas),dtype=int)
        for index,(x,y,mask) in masks.items():
            cy=min(ch-1,int((y+mask.shape[0]*.5)//block))
            cx=min(cw-1,int((x+mask.shape[1]*.5)//block))
            pocket=int(labels[cy,cx]);membership[index]=pocket
            if pocket>=0:counts[pocket]+=1
            if pocket>=0 and kinds[index]=='compact':compact_counts[pocket]+=1
            occupied[y:y+mask.shape[0],x:x+mask.shape[1]]+=mask
        initial_counts=counts.copy()
        def density_error():
            outside=len(eligible)-int(counts.sum())
            return float((np.abs(counts-quotas).sum()+outside)/max(1,2*len(eligible)))
        density_before=density_error()
        compact=[i for i in eligible if kinds[i]=='compact']
        compact_quotas=_quotas(areas,len(compact))
        active_quotas=compact_quotas;active_counts=compact_counts
        field=None;field_moves=0;paddings={};footprints={}
        def current_field():
            nonlocal field,field_moves
            if field is None or field_moves>=8:
                field=_distance(coarse_mask(),check);field_moves=0
            return field
        def old_clearance(x,y,mask,shaped=False):
            mh,mw=mask.shape
            lx=max(0,x//block-16);ly=max(0,y//block-16)
            hx=min(cw,math.ceil((x+mw)/block)+16);hy=min(ch,math.ceil((y+mh)/block)+16)
            local=np.ones(((hy-ly)*block,(hx-lx)*block),dtype=bool)
            crop=occupied[ly*block:min(height,hy*block),lx*block:min(width,hx*block)]>0
            local[:crop.shape[0],:crop.shape[1]]=crop
            local=local.reshape(hy-ly,block,hx-lx,block).any(axis=(1,3))
            local[0]=True;local[-1]=True;local[:,0]=True;local[:,-1]=True
            distance=_distance(local,check)
            def measure(distance,ox,oy):
                crop=distance[y//block-oy:math.ceil((y+mh)/block)-oy,
                              x//block-ox:math.ceil((x+mw)/block)-ox]
                if shaped:
                    footprint=np.zeros((crop.shape[0]*block,crop.shape[1]*block),dtype=bool)
                    footprint[y%block:y%block+mh,x%block:x%block+mw]=mask
                    coverage=footprint.reshape(crop.shape[0],block,crop.shape[1],block).any(axis=(1,3))
                    return float(crop[coverage].min())
                return float(crop.min())
            value=measure(distance,lx,ly)
            if value>=12.:
                # A very spacious old position can see the artificial window
                # edge. Measure the whole tile before considering a move, so an
                # already centred group does not drift toward a worse position.
                whole=_distance(coarse_mask(),check)
                value=measure(whole,0,0)
            return value
        def locate(index,pocket,required=-1.):
            """Adapt the preferred gap to this pocket's quota and this shape."""
            x,y,mask=masks[index];mh,mw=mask.shape
            ys,xs=samples[pocket];distance=current_field()
            if kinds[index]!='compact' and index not in footprints:
                # Sample the actual occupied footprint, not the empty corners
                # of an L-shaped bounding box. Fine contour checks below are
                # authoritative; these samples only rank candidate centres.
                ph=math.ceil(mh/block);pw=math.ceil(mw/block)
                pooled=np.zeros((ph*block,pw*block),dtype=bool)
                pooled[:mh,:mw]=mask
                fy,fx=np.nonzero(pooled.reshape(ph,block,pw,block).any(axis=(1,3)))
                chosen=np.unique(np.r_[np.linspace(0,len(fx)-1,min(64,len(fx)),dtype=int),
                                        fx.argmin(),fx.argmax(),fy.argmin(),fy.argmax()])
                footprints[index]=(np.rint(fy[chosen]+.5-mh/(2*block)).astype(int),
                                   np.rint(fx[chosen]+.5-mw/(2*block)).astype(int))
            scores=distance[ys,xs].copy()
            if kinds[index]!='compact':
                scores.fill(1e6)
                for dy,dx in zip(*footprints[index]):
                    yy=ys+dy;xx=xs+dx;valid=(yy>=0)&(yy<ch)&(xx>=0)&(xx<cw)
                    values=np.zeros(len(xs),dtype=np.float32)
                    values[valid]=distance[yy[valid],xx[valid]]
                    np.minimum(scores,values,out=scores)
            candidates=[]
            radius=max(1.,min(mw,mh)/block*.4)
            for _ in range(16 if kinds[index]=='compact' else 24):
                check()
                best=int(scores.argmax());cy,cx=int(ys[best]),int(xs[best])
                if scores[best]<=max(required,minimum_gap/block*.5):break
                score=float(scores[best])
                scores[(np.abs(ys-cy)<=radius)&(np.abs(xs-cx)<=radius)]=-1
                xx=round((cx+.5)*block-mw*.5);yy=round((cy+.5)*block-mh*.5)
                if xx<margin or yy<margin or xx+mw>width-margin or yy+mh>height-margin:continue
                if kinds[index]=='compact':
                    score=float(distance[yy//block:math.ceil((yy+mh)/block),
                                         xx//block:math.ceil((xx+mw)/block)].min())
                if score>required:candidates.append((score,xx,yy))
            pitch=math.sqrt(float(areas[pocket])/max(1,int(active_quotas[pocket]),int(active_counts[pocket])))
            preferred=max(minimum_gap,int(.45*max(0.,pitch*block-(mw+mh)*.5)))
            from uv_straight_contour_pack import _rows,_expand
            for gap in dict.fromkeys((preferred,minimum_gap)):
                key=index,gap
                if key not in paddings:paddings[key]=_expand(_rows(mask),gap)
                for score,xx,yy in sorted(candidates,reverse=True):
                    check()
                    if _fits(occupied,mask,xx,yy,gap,margin,paddings[key]):
                        return score,xx,yy,gap
            return None
        def move(index,pocket,relax=False):
            nonlocal transfers,field_moves
            x,y,mask=masks[index];mh,mw=mask.shape;source=membership[index]
            current_field()
            occupied[y:y+mh,x:x+mw]-=mask
            try:
                old=old_clearance(x,y,mask,kinds[index]!='compact') if relax else 0.
                best=locate(index,pocket,old+.5 if relax else -1.)
                if best is not None:
                    score,xx,yy,gap=best
                    original=result['positions'][index]
                    result['positions'][index]=(original[0]+(xx-x)/grid,original[1]+(yy-y)/grid)
                    if index not in moved:moved.append(index)
                    if relax:improvements.append((score-old)*block/grid*resolution)
                    if source!=pocket:
                        if source>=0:counts[source]-=1
                        counts[pocket]+=1;transfers+=1
                        if kinds[index]=='compact':
                            if source>=0:compact_counts[source]-=1
                            compact_counts[pocket]+=1
                    membership[index]=pocket
                    applied_gaps[index]=gap/grid*resolution
                    x,y=xx,yy
                    masks[index]=(x,y,mask);field_moves+=1
                    # Conservative proposal update; full contour distances are
                    # refreshed every eight moves to remove stale source cells.
                    dx=np.maximum(np.maximum(x//block-np.arange(cw),np.arange(cw)-math.ceil((x+mw)/block)),0)
                    dy=np.maximum(np.maximum(y//block-np.arange(ch),np.arange(ch)-math.ceil((y+mh)/block)),0)
                    np.minimum(field,np.hypot(dy[:,None],dx[None,:]),out=field)
            finally:
                occupied[y:y+mh,x:x+mw]+=mask
            if callback and best is not None:
                callback(dict(phase='Distributing small shells',postprocess=True,done=len(moved)))
            return best is not None
        # First fill per-pocket deficits using groups outside spacious cores
        # or groups in overfull pockets. All other groups remain valid obstacles.
        def allocate(indices,limit):
            for sweep in range(2):
                accepted=0
                for index in indices:
                    check()
                    if time.monotonic()-started>=budget*limit:return
                    source=membership[index]
                    if source>=0 and active_counts[source]<=active_quotas[source]:continue
                    deficient=np.flatnonzero(active_counts<active_quotas)
                    if kinds[index]=='compact':
                        order=sorted(deficient,key=lambda p:(active_counts[p]/active_quotas[p],
                                                             -int(active_quotas[p]-active_counts[p]),int(p)))
                    else:
                        # Long donors cannot fit the smallest empty cores.
                        # Rank remaining area before relative group deficit.
                        order=sorted(deficient,key=lambda p:(-float(areas[p])*(1-active_counts[p]/active_quotas[p]),int(p)))
                    for pocket in order[:8 if kinds[index]=='compact' else 16]:
                        if move(index,int(pocket)):
                            accepted+=1;break
                if not accepted:break
        # Compact groups retain their original area allocation. Thin donors are
        # considered afterwards, so they cannot displace the compact fillers.
        allocate(compact,.65)
        for index in compact:
            check()
            pocket=membership[index]
            if pocket>=0:move(index,pocket,True)
        active_quotas=quotas;active_counts=counts
        donors=[i for i in eligible if kinds[i]!='compact']
        allocate(donors,.85)
        for index in donors:
            check()
            pocket=membership[index]
            if pocket>=0:move(index,pocket,True)
        density_after=density_error()
        pocket_stats=[dict(area_px2=float(area)*(block/grid*resolution)**2,
                           target_groups=int(quotas[i]),before_groups=int(initial_counts[i]),
                           after_groups=int(counts[i])) for i,area in enumerate(areas)]
    except SpreadStopped:
        if 'counts' in locals() and 'initial_counts' in locals():
            density_after=density_error()
            pocket_stats=[dict(area_px2=float(area)*(block/grid*resolution)**2,
                               target_groups=int(quotas[i]),before_groups=int(initial_counts[i]),
                               after_groups=int(counts[i])) for i,area in enumerate(areas)]
    finally:
        result.update(spread_small_eligible=len(eligible),spread_small_moved=len(moved),
                      spread_small_groups=moved,spread_small_seconds=time.monotonic()-started,
                      spread_small_clearance_gain_px=improvements,
                      spread_small_pockets=pocket_stats,spread_small_transfers=transfers,
                      spread_small_density_error_before=density_before,
                      spread_small_density_error_after=density_after,
                      spread_small_applied_gaps_px=applied_gaps,
                      spread_small_kinds={kind:sum(v==kind for v in kinds.values()) for kind in ('compact','strip','bend')},
                      spread_small_moved_kinds={kind:sum(kinds[i]==kind for i in moved) for kind in ('compact','strip','bend')})
    return result
