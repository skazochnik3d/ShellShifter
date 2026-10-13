"""Identify existing coincident UV outlines; no Maya or Qt access."""
from collections import Counter, defaultdict
from itertools import product
import math
import numpy as np


def _outline_edges(edges, points):
    adjacent=defaultdict(list)
    for a,b in edges:adjacent[a].append(b);adjacent[b].append(a)
    if any(len(neighbors)!=2 for neighbors in adjacent.values()):
        return [tuple(sorted((points[a],points[b]))) for a,b in edges]
    remaining=set(edges);result=[]
    while remaining:
        a,b=min(remaining);remaining.remove((a,b))
        loop=[a];previous,current=a,b
        while current!=a:
            loop.append(current)
            following=next(i for i in adjacent[current] if i!=previous)
            remaining.discard(tuple(sorted((current,following))))
            previous,current=current,following
        polygon=[points[i] for i in loop]
        # Different mesh tessellations may split the same straight UV outline.
        changed=True
        while changed and len(polygon)>3:
            changed=False
            for i,b in enumerate(polygon):
                a,c=polygon[i-1],polygon[(i+1)%len(polygon)]
                dx,dy=c[0]-a[0],c[1]-a[1];length=math.hypot(dx,dy)
                cross=abs((b[0]-a[0])*dy-(b[1]-a[1])*dx)
                between=(b[0]-a[0])*(b[0]-c[0])+(b[1]-a[1])*(b[1]-c[1])
                if length>0 and cross<=length*1e-7 and between<=1e-14:
                    polygon.pop(i);changed=True;break
        result.extend(tuple(sorted((a,b))) for a,b in zip(polygon,polygon[1:]+polygon[:1]))
    return result


def signatures(faces, shell_ids, points):
    edges=Counter(tuple(sorted((a,b))) for face in faces
                  for a,b in zip(face,face[1:]+face[:1]))
    outlines=defaultdict(list)
    for (a,b),count in edges.items():
        if count != 1: continue
        # Absolute coordinates prevent stacking identical shapes at different
        # atlas positions. Imported copies can differ by a few float ULPs.
        outlines[shell_ids[a]].append((a,b))
    return {owner:dict(raw=tuple(sorted(tuple(sorted((points[a],points[b]))) for a,b in outline)),
                       outline=tuple(sorted(_outline_edges(outline,points))))
            for owner,outline in outlines.items()}


def _segments_cover(source, target, tolerance):
    """Prove every source edge lies in the union of target-edge corridors.

    Project nearby target segments onto each source line. Their intervals are
    expanded only by the remaining Euclidean distance budget, then unioned.
    This handles different subdivisions without sampling long edges or
    merging different holes or crossing-edge topologies.
    """
    a,b=target[:,0,:],target[:,1,:]
    for start,end in source:
        delta=end-start;length=float(np.linalg.norm(delta))
        if length<=1e-15:
            v=b-a;offset=start-a
            t=np.clip((offset*v).sum(axis=1)/np.maximum((v*v).sum(axis=1),1e-30),0.,1.)
            if np.min(np.linalg.norm(offset-t[:,None]*v,axis=1))>tolerance:return False
            continue
        direction=delta/length
        offset_a,offset_b=a-start,b-start
        perpendicular=np.maximum(np.abs(offset_a[:,0]*direction[1]-offset_a[:,1]*direction[0]),
                                 np.abs(offset_b[:,0]*direction[1]-offset_b[:,1]*direction[0]))
        valid=perpendicular<=tolerance
        if not valid.any():return False
        first,last=offset_a[valid]@direction,offset_b[valid]@direction
        extra=np.sqrt(np.maximum(0.,tolerance*tolerance-perpendicular[valid]**2))
        left,right=np.minimum(first,last)-extra,np.maximum(first,last)+extra
        covered=0.
        for i in np.argsort(left):
            if left[i]>covered+1e-14:break
            covered=max(covered,float(right[i]))
            if covered>=length-1e-14:break
        if covered<length-1e-14:return False
    return True


def _same_outline(a,b,tolerance):
    return bool(len(a) and len(b) and _segments_cover(a,b,tolerance) and _segments_cover(b,a,tolerance))


def groups(keys, tolerance=1e-5):
    buckets=defaultdict(list); result=[]; shapes=[]
    cell_size=max(.001,tolerance*4)
    for index,key in enumerate(keys):
        if not key:
            result.append([index]);shapes.append(None);continue
        variants=([np.asarray(key[name],dtype=float) for name in ('raw','outline')]
                  if isinstance(key,dict) else [np.asarray(key,dtype=float)])
        edges=variants[0]
        points=edges.reshape(-1,2)
        box=np.concatenate((points.min(axis=0),points.max(axis=0)))
        cell=tuple(math.floor(v/cell_size) for v in box)
        found=None
        candidates=set()
        for length in {len(edge) for edge in variants} | {-1}:
            for delta in product((-1,0,1),repeat=4):
                bucket=(length,)+tuple(a+b for a,b in zip(cell,delta))
                candidates.update(buckets.get(bucket,()))
        for candidate in sorted(candidates):
            others,other_box=shapes[candidate]
            if np.max(np.abs(box-other_box))>tolerance:continue
            if tolerance>1e-5:
                overlap=np.minimum(box[2:],other_box[2:])-np.maximum(box[:2],other_box[:2])
                extent=np.maximum(box[2:]-box[:2],other_box[2:]-other_box[:2])
                if np.any(overlap<extent*.9):continue
            for edges,other in ((a,b) for a in variants for b in others if len(a)==len(b)):
                direct=np.max(np.abs(edges-other),axis=(1,2))
                reverse=np.max(np.abs(edges-other[:,::-1,:]),axis=(1,2))
                if np.all(np.minimum(direct,reverse)<=tolerance):
                    found=candidate;break
                # Sorting can change at nearly vertical/horizontal edges.
                # Compare unordered segments with bounded temporary arrays.
                compatible=True;covered=np.zeros(len(other),dtype=bool)
                for start in range(0,len(edges),32):
                    part=edges[start:start+32,None,:,:]
                    d=np.max(np.abs(part-other[None,:,:,:]),axis=(2,3))
                    r=np.max(np.abs(part-other[None,:,::-1,:]),axis=(2,3))
                    matches=np.minimum(d,r)<=tolerance
                    if not np.all(matches.any(axis=1)):compatible=False;break
                    covered|=matches.any(axis=0)
                if compatible and covered.all():found=candidate;break
            if found is None and _same_outline(variants[-1],others[-1],tolerance):found=candidate
            if found is not None:break
        if found is None:
            found=len(result);result.append([]);shapes.append((variants,box))
            for length in {len(edge) for edge in variants} | {-1}:buckets[(length,)+cell].append(found)
        result[found].append(index)
    return result
