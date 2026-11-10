"""Co-solve selected coincident UV islands, including reversed face winding.

Matching uses absolute UV positions and surface coverage across triangulations. Results
share displacements, preserving small initial offsets and every fixed UV.
No Maya reads or writes occur here; the caller supplies indexed shell views.
"""
from collections import Counter,defaultdict
from dataclasses import dataclass
from itertools import product
from functools import cached_property
import math
from uv_straight_deps import np
from uv_straight_shell_match import surface, same_surface, point_mapping


def _cycle(face):
    pivot=face.index(min(face))
    forward=face[pivot:]+face[:pivot]
    reverse=(forward[0],)+tuple(reversed(forward[1:]))
    return min(forward,reverse)


@dataclass
class Record:
    job: tuple
    mesh: object
    ids: tuple
    points: np.ndarray
    faces: tuple
    labels: tuple
    key: tuple
    bounds: np.ndarray
    tolerance: float

    @property
    def identity(self):return (self.mesh.name,min(self.job[1]))

    @cached_property
    def ordered(self):
        order=np.lexsort((self.points[:,1],self.points[:,0]))
        rank=np.empty(len(order),dtype=np.int64);rank[order]=np.arange(len(order))
        topology=Counter(_cycle(tuple(int(rank[i]) for i in face)) for face in self.faces)
        return order,self.points[order],tuple(self.labels[i] for i in order),topology


def _record(job,view):
    ids=tuple(sorted(job[1]));local={uv:i for i,uv in enumerate(ids)}
    points=np.asarray([job[5][uv] for uv in ids],dtype=float)
    if not len(ids) or not np.isfinite(points).all():return None
    faces=tuple(tuple(local[i] for i in face) for face in view.faces)
    if not faces or any(len(set(face))!=len(face) for face in faces):return None
    incident=defaultdict(list);edges=Counter();neighbors=defaultdict(set)
    for face in faces:
        for i in face:incident[i].append(len(face))
        for a,b in zip(face,face[1:]+face[:1]):
            neighbors[a].add(b);neighbors[b].add(a);edges[tuple(sorted((a,b)))]+=1
    if any(count>2 for count in edges.values()):return None
    boundary=Counter(i for pair,count in edges.items() if count==1 for i in pair)
    labels=tuple((len(neighbors[i]),boundary[i],tuple(sorted(incident[i]))) for i in range(len(ids)))
    key=surface(faces,len(ids))[0]
    lo,hi=points.min(axis=0),points.max(axis=0);span=float((hi-lo).max())
    if span<=1.e-12:return None
    roundoff=float(np.abs(points).max())*np.finfo(np.float32).eps*2
    tolerance=max(span*1.e-5,min(roundoff,span*1.e-3),1.e-8)
    return Record(job,view,ids,points,faces,labels,key,np.concatenate((lo,hi)),tolerance)


def _mapping(source,target):
    """Prove a unique point bijection and the same chart surface."""
    tolerance=max(source.tolerance,target.tolerance)
    if np.max(np.abs(source.bounds-target.bounds))>tolerance:return None
    # Exact stacks are common: sort once and reuse the representative's face
    # signature instead of running a spatial neighbor search for every copy.
    a,pa,la,fa=source.ordered;b,pb,lb,fb=target.ordered
    if np.array_equal(pa,pb):
        if len(pa)>1 and np.any(np.all(pa[1:]==pa[:-1],axis=1)):return None
        if la==lb and fa==fb:
            return {source.ids[int(i)]:target.ids[int(j)] for i,j in zip(a,b)}
        target_to_source=np.empty(len(b),dtype=np.int32);target_to_source[b]=a
    else:
        target_to_source=point_mapping(source.points,target.points,
                                      surface(source.faces,len(source.ids))[1],
                                      surface(target.faces,len(target.ids))[1],tolerance)
        if target_to_source is None:return None
    if not same_surface(source.faces,target.faces,target_to_source,source.points):return None
    return {source.ids[s]:target.ids[t] for t,s in enumerate(target_to_source)}


def _combined_job(source,target,mapping,validate):
    inverse={v:k for k,v in mapping.items()}
    groups=list(source.job[2]);keys=set()
    def group_key(group):
        ids=group['ids']
        topology=(tuple(sorted(tuple(sorted(pair)) for pair in zip(ids,ids[1:])))
                  if group['mode']=='edge' else tuple(sorted(ids)))
        return group['axis'],group['mode'],topology
    keys.update(group_key(group) for group in groups)
    for group in target.job[2]:
        copied=dict(group,ids=[inverse[i] for i in group['ids']]);key=group_key(copied)
        if key not in keys:groups.append(copied);keys.add(key)
    pins=set(source.job[3])|{inverse[i] for i in target.job[3]}
    try:validate(source.mesh,groups,pins)
    except ValueError:return None
    fixed={i:source.job[5][i] for i in pins}
    job=(source.job[0],source.job[1],groups,pins,fixed,source.job[5])
    # Pins inherited from another copy need explicit scratch-native anchors too.
    return job+(set(pins),) if pins else job


class StackPlan:
    def __init__(self,jobs,meshes,followers,groups,conflicts):
        self.jobs,self.meshes,self.followers=jobs,meshes,followers
        self.groups,self.conflicts=groups,conflicts

    def expand(self,plans):
        if not self.followers:return plans
        values={mesh.name:dict(positions) for mesh,positions in plans}
        references={mesh.name:mesh for mesh,positions in plans}
        for source,target,mapping in self.followers:
            result=values[source.mesh.name]
            copied=values.setdefault(target.mesh.name,{})
            references.setdefault(target.mesh.name,self.meshes[target.mesh.name])
            for a,b in mapping.items():
                initial=source.job[5][a];original=target.job[5][b];point=result[a]
                if b in target.job[3] or tuple(point)==tuple(initial):copied[b]=original
                elif tuple(initial)==tuple(original):copied[b]=point
                else:copied[b]=tuple(original[d]+point[d]-initial[d] for d in (0,1))
        return [(references[name],positions) for name,positions in values.items()]


def prepare(tasks,meshes,shell_views,validate):
    """Only shells present in tasks may join a stack; no unselected copies."""
    by_mesh=defaultdict(list)
    for job in tasks:by_mesh[job[0].fullPathName()].append(job)
    records={}
    for name,jobs in by_mesh.items():
        for job,view in shell_views(meshes[name],jobs):
            records[(name,min(job[1]))]=_record(job,view)
    max_tolerance=max((record.tolerance for record in records.values() if record is not None),default=1.e-8)
    cell_size=max(1.e-3,max_tolerance*4)
    buckets=defaultdict(list);roots=[];followers=[];grouped=set();conflicts=0
    for job in tasks:
        identity=(job[0].fullPathName(),min(job[1]));record=records[identity]
        if record is None:roots.append((identity,job));continue
        center=(record.bounds[:2]+record.bounds[2:])*.5
        cell=tuple(math.floor(v/cell_size) for v in center)
        candidates=[]
        for du,dv in product((-1,0,1),repeat=2):
            candidates.extend(buckets.get((record.key,cell[0]+du,cell[1]+dv),()))
        found=False
        for source in candidates:
            mapping=_mapping(source,record)
            if mapping is None:continue
            combined=_combined_job(source,record,mapping,validate)
            if combined is None:conflicts+=1;continue
            source.job=combined
            followers.append((source,record,mapping));grouped.add(source.identity)
            found=True;break
        if not found:
            roots.append((identity,job));buckets[(record.key,*cell)].append(record)
    jobs=[records[key].job if records.get(key) is not None else job for key,job in roots]
    return StackPlan(jobs,meshes,followers,len(grouped),conflicts)
