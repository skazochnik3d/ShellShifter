"""Soft pack-group preferences and a bounded contour-checked proximity pass."""
import math
import time
import numpy as np


def preferred_order(order,groups):
    if not groups:return order
    rank={index:i for i,index in enumerate(order)};priority=dict(rank)
    for group in groups:
        members=[i for i in group['indices'] if i in rank]
        if len(members)<2:continue
        strength=group['strength'];anchor=sum(rank[i] for i in members)/len(members)
        for i in members:priority[i]=rank[i]*(1-strength)+anchor*strength
    return sorted(order,key=lambda i:(priority[i],rank[i]))


def spread(boxes,result,groups):
    """Weighted within-group variance of shell centers, in final UV units."""
    total=0.
    for group in groups:
        ids=group['indices']
        centers=[]
        for i in ids:
            w,h=boxes[i]
            if int(result['turns'][i])%2:w,h=h,w
            centers.append(np.asarray(result['positions'][i])+np.asarray((w,h))*result['scale']*.5)
        values=np.asarray(centers)
        total+=float(np.sum((values-values.mean(axis=0))**2))*group['strength']/max(1,len(ids))
    return total


def cluster(data,result,cancel=None,callback=None,budget=None):
    """Translate or exchange shells; never change scale, rotation or spacing.

    Original fractional raster origins are retained. A proposed exchange is
    published only after BOTH masks fit the remaining reference-count grid.
    Cancellation restores a half-tested exchange before returning a full plan.
    """
    groups=[g for g in data.get('pack_groups',[]) if g['strength']>0 and len(g['indices'])>1]
    if not groups or result.get('cancelled'):return result
    from uv_straight_pack_spread import _raster,_fits,SpreadStopped
    from uv_straight_pack_metrics import placed_triangles
    from uv_straight_contour_pack import _rows,_expand
    started=time.monotonic();strength=max(g['strength'] for g in groups)
    budget=1.+3.*strength if budget is None else budget
    grid=max(1024,min(4096,int(data.get('resolution',2048))))
    target=data.get('target_size',(1.,1.));width=math.ceil(target[0]*grid);height=math.ceil(target[1]*grid)
    moved=set();checks=0
    before=spread(data['boxes'],result,groups)
    def check():
        if time.monotonic()-started>budget or (cancel is not None and cancel.is_set()):raise SpreadStopped()
    result['positions']=dict(result['positions'])
    try:
        check()
        if callback:callback(dict(phase='Keeping Shell Groups together',postprocess=True))
        points=placed_triangles(data,result);cursor=0;masks={};pads={};mask_bytes=0
        occupied=np.zeros((height,width),dtype=np.uint32)
        margin=math.ceil(data['margin']*grid);gap=math.ceil(data['gap']*grid)
        # All shells can participate in exchanges, including ungrouped shells.
        # Sum cropped footprints is bounded by tile occupancy, rather than N full canvases.
        for i,triangles in enumerate(data['triangles']):
            check();end=cursor+len(triangles);p=points[cursor:end]*grid;cursor=end
            lower=np.maximum(0,np.floor(p.min(axis=(0,1))-1e-9).astype(int))
            upper=np.minimum((width,height),np.floor(p.max(axis=(0,1))+1e-9).astype(int)+1)
            x,y=map(int,lower);right,top=map(int,upper)
            if right<=x or top<=y:continue
            mask_bytes+=(right-x)*(top-y)
            if mask_bytes>64*1024*1024:raise SpreadStopped()
            mask=_raster(p-(x,y),right-x,top-y,check)
            masks[i]=(x,y,mask);occupied[y:top,x:right]+=mask
        occupied+=_raster(points[cursor:]*grid,width,height,check)
        def occupancy(index,sign):
            x,y,mask=masks[index];h,w=mask.shape
            if sign>0:occupied[y:y+h,x:x+w]+=mask
            else:occupied[y:y+h,x:x+w]-=mask
        def fits(index,x,y):
            nonlocal checks
            check();checks+=1
            if index not in pads:pads[index]=_expand(_rows(masks[index][2]),gap)
            return _fits(occupied,masks[index][2],x,y,gap,margin,pads[index])
        def proposal(index,x,y):
            ox,oy,_=masks[index];old=result['positions'][index]
            return old[0]+(x-ox)/grid,old[1]+(y-oy)/grid
        def centers(ids):
            return np.asarray([(masks[i][0]+masks[i][2].shape[1]*.5,
                                masks[i][1]+masks[i][2].shape[0]*.5) for i in ids])
        owner={i:g for g in groups for i in g['indices']}
        for sweep in range(1+int(strength*3)):
            accepted=0
            for group in sorted(groups,key=lambda g:-g['strength']):
                ids=[i for i in group['indices'] if i in masks]
                if len(ids)<2:continue
                center=centers(ids).mean(axis=0)
                order=sorted(ids,key=lambda i:-float(np.linalg.norm(centers([i])[0]-center)))
                for i in order:
                    check();x,y,mask=masks[i];h,w=mask.shape
                    old_score=spread(data['boxes'],result,groups)
                    targets=[]
                    fraction=group['strength']
                    cx,cy=center;dx=cx-w*.5-x;dy=cy-h*.5-y
                    for f in (fraction,.75*fraction,.5*fraction,.25*fraction):
                        xx=round(x+dx*f);yy=round(y+dy*f)
                        for offx,offy in ((0,0),(w+gap,0),(-w-gap,0),(0,h+gap),(0,-h-gap)):
                            targets.append((xx+offx,yy+offy))
                    occupancy(i,-1);chosen=None
                    try:
                        for xx,yy in dict.fromkeys(targets):
                            if not fits(i,xx,yy):continue
                            original=result['positions'][i];result['positions'][i]=proposal(i,xx,yy)
                            score=spread(data['boxes'],result,groups)
                            result['positions'][i]=original
                            if score<old_score-1e-12:chosen=(xx,yy);break
                        if chosen is not None:
                            result['positions'][i]=proposal(i,*chosen);masks[i]=(*chosen,mask)
                            moved.add(i);accepted+=1
                    finally:occupancy(i,1)
                    if chosen is not None:continue
                    # A dense atlas often has no empty destination. Try bounded swaps,
                    # accepting only a decrease of the sum over ALL affected groups.
                    candidates=[j for j in masks if j!=i and owner.get(j) is not group]
                    candidates.sort(key=lambda j:float(np.linalg.norm(centers([j])[0]-center)))
                    for j in candidates[:max(2,round(12*fraction))]:
                        check();jx,jy,jmask=masks[j];jh,jw=jmask.shape
                        xx=round(jx+(jw-w)*.5);yy=round(jy+(jh-h)*.5)
                        tx=round(x+(w-jw)*.5);ty=round(y+(h-jh)*.5)
                        occupancy(i,-1);occupancy(j,-1);staged=False;keep=False
                        original_i=result['positions'][i];original_j=result['positions'][j]
                        try:
                            if not fits(i,xx,yy):continue
                            masks[i]=(xx,yy,mask);occupancy(i,1);staged=True
                            if not fits(j,tx,ty):continue
                            result['positions'][i]=(original_i[0]+(xx-x)/grid,original_i[1]+(yy-y)/grid)
                            result['positions'][j]=(original_j[0]+(tx-jx)/grid,original_j[1]+(ty-jy)/grid)
                            if spread(data['boxes'],result,groups)<old_score-1e-12:
                                masks[j]=(tx,ty,jmask);keep=True;moved.update((i,j));accepted+=1
                        finally:
                            if not keep:
                                if staged:occupancy(i,-1)
                                masks[i]=(x,y,mask);masks[j]=(jx,jy,jmask)
                                result['positions'][i]=original_i;result['positions'][j]=original_j
                                occupancy(i,1)
                            occupancy(j,1)
                        if keep:break
            if not accepted:break
    except SpreadStopped:pass
    result.update(pack_groups_moved=len(moved),pack_groups_checks=checks,
                  pack_groups_seconds=time.monotonic()-started,
                  pack_groups_spread_before=before,pack_groups_spread_after=spread(data['boxes'],result,groups))
    return result
