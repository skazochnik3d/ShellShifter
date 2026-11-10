"""Dependency-free area of a UV union, sampled along horizontal scanlines.

X intervals remain continuous: only Y is sampled. Overlapping triangles,
different triangulations and partly offset stacks contribute once. This is an
area estimate, never an input to collision detection or scale acceptance.
"""
from bisect import bisect_right
import math
import time
import numpy as np


class AreaEstimateStopped(Exception):
    pass


def _small_union_area(points,grid,target_size,check):
    """Batch the same scanline intervals for compact stack contours."""
    if len(points)>128:return None
    if check is not None:check()
    width,height=target_size;count=math.ceil(height*grid)
    low=np.floor(np.clip(points[:,:,1].min(axis=1),0.,height)*grid).astype(np.int64)
    high=np.minimum(count,np.ceil(np.clip(points[:,:,1].max(axis=1),0.,height)*grid)).astype(np.int64)
    counts=np.maximum(0,high-low);total=int(counts.sum())
    if not total:return 0.
    ids=np.repeat(np.arange(len(points)),counts)
    starts=np.repeat(np.cumsum(counts)-counts,counts)
    rows=np.repeat(low,counts)+np.arange(total)-starts
    sample=np.minimum((rows+.5)/grid,(rows/grid+height)*.5)
    a=points[ids];b=np.roll(a,-1,axis=1)
    crosses=((a[:,:,1]<=sample[:,None]) & (sample[:,None]<b[:,:,1])) | \
            ((b[:,:,1]<=sample[:,None]) & (sample[:,None]<a[:,:,1]))
    dy=b[:,:,1]-a[:,:,1]
    x=a[:,:,0]+(sample[:,None]-a[:,:,1])*(b[:,:,0]-a[:,:,0])/np.where(dy!=0,dy,1.)
    left=np.maximum(0.,np.where(crosses,x,np.inf).min(axis=1))
    right=np.minimum(width,np.where(crosses,x,-np.inf).max(axis=1))
    valid=right>left;rows,left,right=rows[valid],left[valid],right[valid]
    if not len(rows):return 0.
    order=np.lexsort((left,rows));rows,left,right=rows[order],left[order],right[order]
    counts=np.bincount(rows,minlength=count);columns=int(counts.max())
    if count*columns>2_000_000:return None
    starts=np.cumsum(counts)-counts;column=np.arange(len(rows))-starts[rows]
    # Each row has at most one interval per triangle. A bounded 2D prefix max
    # avoids precision-losing coordinate offsets and Python per-interval loops.
    ends=np.zeros((count,columns),dtype=np.float64);ends[rows,column]=right
    np.maximum.accumulate(ends,axis=1,out=ends)
    previous=np.where(column>0,ends[rows,np.maximum(column-1,0)],0.)
    lengths=np.bincount(rows,weights=np.maximum(0.,right-np.maximum(left,previous)),minlength=count)
    if check is not None:check()
    heights=np.minimum(1./grid,np.maximum(0.,height-np.arange(count)/grid))
    return float(np.sum(lengths*heights))


def union_area(triangles,grid=4096,target_size=(1.,1.),check=None):
    points=np.asarray(triangles,dtype=np.float64).reshape(-1,3,2)
    if not len(points):return 0.
    if not np.isfinite(points).all():raise ValueError('Invalid UV area coordinates.')
    width,height=target_size
    # Exact duplicate faces are common in stacked UVs. Canonicalize vertex
    # order without quantizing or merging slightly different coordinates.
    order=np.lexsort((points[:,:,1],points[:,:,0]),axis=1)
    points=np.take_along_axis(points,order[:,:,None],axis=1)
    points=np.unique(points.reshape(-1,6),axis=0).reshape(-1,3,2)
    small=_small_union_area(points,grid,target_size,check)
    if small is not None:return small
    count=math.ceil(height*grid);intervals=[[] for _ in range(count)]
    lengths=[0.]*count
    for first in range(0,len(points),128):
        if check is not None:check()
        group=points[first:first+128]
        low=np.floor(np.clip(group[:,:,1].min(axis=1),0.,height)*grid).astype(np.int64)
        high=np.minimum(count,np.ceil(np.clip(group[:,:,1].max(axis=1),0.,height)*grid)).astype(np.int64)
        counts=np.maximum(0,high-low);total=int(counts.sum())
        if not total:continue
        ids=np.repeat(np.arange(len(group)),counts)
        starts=np.repeat(np.cumsum(counts)-counts,counts)
        rows=np.repeat(low,counts)+np.arange(total)-starts
        sample=np.minimum((rows+.5)/grid,(rows/grid+height)*.5)
        a=group[ids];b=np.roll(a,-1,axis=1)
        crosses=((a[:,:,1]<=sample[:,None]) & (sample[:,None]<b[:,:,1])) | \
                ((b[:,:,1]<=sample[:,None]) & (sample[:,None]<a[:,:,1]))
        dy=b[:,:,1]-a[:,:,1]
        x=a[:,:,0]+(sample[:,None]-a[:,:,1])*(b[:,:,0]-a[:,:,0])/np.where(dy!=0,dy,1.)
        left=np.maximum(0.,np.where(crosses,x,np.inf).min(axis=1))
        right=np.minimum(width,np.where(crosses,x,-np.inf).max(axis=1))
        valid=right>left
        for done,(row,lo,hi) in enumerate(zip(rows[valid],left[valid],right[valid])):
            if check is not None and done%4096==0:check()
            lo,hi=float(lo),float(hi);runs=intervals[row]
            index=bisect_right(runs,(lo,float('inf')))
            if index and runs[index-1][1]>=lo:index-=1
            end=index;removed=0.
            while end<len(runs) and runs[end][0]<=hi:
                lo=min(lo,runs[end][0]);hi=max(hi,runs[end][1])
                removed+=runs[end][1]-runs[end][0];end+=1
            runs[index:end]=[(lo,hi)]
            lengths[row]+=hi-lo-removed
    return float(sum(length*min(1./grid,max(0.,height-row/grid)) for row,length in enumerate(lengths)))


def placed_triangles(data,result):
    groups=[];scale=result['scale']
    for index,(box,triangles) in enumerate(zip(data['boxes'],data['triangles'])):
        points=np.asarray(triangles,dtype=np.float64).reshape(-1,3,2)*scale
        turn=int(result['turns'][index])%4;w,h=(v*scale for v in box)
        if turn==1:points=np.stack((h-points[:,:,1],points[:,:,0]),axis=-1)
        elif turn==2:points=np.stack((w-points[:,:,0],h-points[:,:,1]),axis=-1)
        elif turn==3:points=np.stack((points[:,:,1],w-points[:,:,0]),axis=-1)
        groups.append(points+result['positions'][index])
    static=np.asarray(data['static_triangles'],dtype=np.float64).reshape(-1,3,2)
    groups.append(static)
    return np.concatenate(groups,axis=0)


def finish_area(data,result,cancel=None,budget=2.5):
    """A stopped estimate must never discard a completed packing plan."""
    if result.get('cancelled') or not result.get('positions'):return
    started=time.monotonic();target=data.get('target_size',(1.,1.));total=target[0]*target[1]
    result['legacy_free_percent']=max(0.,100.*(1.-(sum(data['areas'])*result['scale']**2+data['static_area'])/total))
    result['area_method']='group_sum_static_union'
    def check():
        if (cancel is not None and cancel.is_set()) or time.monotonic()-started>=budget:
            raise AreaEstimateStopped()
    try:
        check()
        area=union_area(placed_triangles(data,result),4096,target,check)
        result['occupied_area']=area
        result['free_percent']=max(0.,min(100.,100.*(1.-area/total)))
        result['area_method']='scanline_union'
        result['area_grid']=4096
    except AreaEstimateStopped:pass
    finally:result['area_seconds']=time.monotonic()-started
