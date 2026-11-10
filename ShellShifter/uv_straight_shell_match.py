"""Triangulation-independent chart checks; operates only on snapshot arrays."""
from collections import Counter, defaultdict
from itertools import product
import numpy as np


def cycle(face):
    face=tuple(face);pivot=face.index(min(face));forward=face[pivot:]+face[:pivot]
    return min(forward,(forward[0],)+tuple(reversed(forward[1:])))


def surface(faces,count):
    edges=Counter(tuple(sorted((a,b))) for face in faces for a,b in zip(face,face[1:]+face[:1]))
    boundary=frozenset(edge for edge,uses in edges.items() if uses==1)
    degrees=Counter(i for edge in boundary for i in edge)
    labels=tuple(degrees[i] for i in range(count))
    key=(count,count-len(edges)+len(faces),tuple(sorted(Counter(labels).items())))
    return key,labels,boundary


def same_surface(faces,other,mapping,points):
    """Same faces, or two unfolded tilings with the same complete boundary.

    Both tilings are checked in the template's UV domain. Consistent winding
    and positive fan triangles exclude folds; boundary edges retain holes.
    A different diagonal or a quad split into triangles is allowed.
    """
    mapped=tuple(tuple(int(mapping[i]) for i in face) for face in other)
    if Counter(map(cycle,faces))==Counter(map(cycle,mapped)):return True
    a=surface(faces,len(points));b=surface(mapped,len(points))
    if a[0]!=b[0] or a[2]!=b[2]:return False
    points=np.asarray(points,dtype=float);span=float(np.ptp(points,axis=0).max())
    if span<=1.e-15:return False
    points=(points-points.mean(axis=0))/span
    def area(loops):
        signs=[];total=0.;directed=Counter()
        for face in loops:
            p=points[list(face)];ab=p[1:-1]-p[0];ac=p[2:]-p[0]
            cross=ab[:,0]*ac[:,1]-ab[:,1]*ac[:,0]
            nonzero=cross[np.abs(cross)>1.e-13]
            if not len(nonzero):return None
            sign=1 if nonzero[0]>0 else -1
            if np.any(nonzero*sign<0):return None
            signs.append(sign);total+=float(np.abs(cross).sum())
            for u,v in zip(face,face[1:]+face[:1]):directed[(u,v)]+=1
        if min(signs)!=max(signs):return None
        # Interior edges must cancel; otherwise polygons overlap or disagree
        # about the surface orientation even when their undirected edges match.
        for u,v in directed:
            if (min(u,v),max(u,v)) not in a[2] and directed[(u,v)]!=directed[(v,u)]:return None
        return total
    x,y=area(faces),area(mapped)
    return x is not None and y is not None and abs(x-y)<=max(x,y)*1.e-8


def point_mapping(source,target,source_labels,target_labels,tolerance):
    """Unique destination-to-source bijection, with linear memory."""
    if source.shape!=target.shape:return None
    buckets=defaultdict(list);dims=source.shape[1]
    for i,p in enumerate(source):buckets[tuple(np.floor(p/tolerance).astype(np.int64))].append(i)
    offsets=tuple(product((-1,0,1),repeat=dims));used=set();mapping=[]
    for i,p in enumerate(target):
        cell=np.floor(p/tolerance).astype(np.int64);candidates=[]
        for delta in offsets:
            for j in buckets.get(tuple(cell+delta),()):
                if source_labels[j]==target_labels[i] and np.linalg.norm(source[j]-p)<=tolerance:candidates.append(j)
        if len(candidates)!=1 or candidates[0] in used:return None
        mapping.append(candidates[0]);used.add(candidates[0])
    return np.asarray(mapping,dtype=np.int32)


def rigid_match_steps(source,target,tolerance):
    """Fit full 3D point clouds, allowing reflection and uniform scaling.

    Existing face propagation handles bent isometric surfaces. This fallback
    handles matching shapes whose polygon diagonals differ.
    """
    sa,la,_=surface(source.faces,len(source.uv));sb,lb,_=surface(target.faces,len(target.uv))
    if sa!=sb:return None
    def normalized(x):
        x=x-x.mean(axis=0);return x/max(float(np.sqrt(np.mean(np.sum(x*x,axis=1)))),1.e-15)
    a,b=normalized(source.xyz),normalized(target.xyz)
    eps=tolerance*max(float(np.linalg.norm(a,axis=1).max()),1.)
    ra,rb=np.sum(a*a,axis=1),np.sum(b*b,axis=1)
    if not np.allclose(np.sort(ra),np.sort(rb),atol=eps*8,rtol=tolerance*4):return None
    _,va=np.linalg.eigh(a.T@a);_,vb=np.linalg.eigh(b.T@b)
    def frames():
        for signs in product((-1,1),repeat=3):yield a@va,(b@vb)*np.asarray(signs)
        # PCA axes are ambiguous for symmetric eigenvalues. A pair of radial
        # vectors fixes a frame without depending on polygon edges/diagonals.
        first=int(np.argmax(ra));axis=a[first]/np.sqrt(ra[first])
        perpendicular=a-np.outer(a@axis,axis);second=int(np.argmax(np.sum(perpendicular**2,axis=1)))
        if np.linalg.norm(perpendicular[second])<eps:return
        ay=perpendicular[second]/np.linalg.norm(perpendicular[second]);basis=np.column_stack((axis,ay,np.cross(axis,ay)))
        candidates=np.flatnonzero((np.abs(rb-ra[first])<eps*8)&(np.asarray(lb)==la[first]))
        attempts=0
        for i in candidates:
            bx=b[i]/np.linalg.norm(b[i]);dot=b@b[i]
            seconds=np.flatnonzero((np.abs(rb-ra[second])<eps*8)&
                                  (np.abs(dot-float(a[second]@a[first]))<eps*8)&(np.asarray(lb)==la[second]))
            for j in seconds:
                by=b[j]-bx*float(b[j]@bx);length=np.linalg.norm(by)
                if length<eps:continue
                by/=length;bz=np.cross(bx,by)
                for sign in (-1,1):yield a@basis,b@np.column_stack((bx,by,bz*sign))
                attempts+=1
                if attempts>=128:return
    best=None;merit=float('inf')
    for index,(pa,pb) in enumerate(frames()):
        if index==8 and best is not None:return best
        yield None
        mapping=point_mapping(pa,pb,la,lb,eps)
        if mapping is None or not same_surface(source.faces,target.faces,mapping,source.uv):continue
        score=float(np.sum((source.normalized_uv[mapping]-target.normalized_uv)**2))
        if score<merit:best,merit=mapping,score
    return best
