"""Parallel numerical preparation. Worker processes never import Maya or Qt."""
import math
import os
import pickle
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from uv_straight_deps import np


class NumericWorkerError(RuntimeError):
    pass


def group_geometry(item):
    sources,norms,target_ids,normalize_density=item
    def triangle_area(t):
        a,b,c=t
        return abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))*.5
    mapped=[sum(triangle_area(t) for t in source) for source in sources]
    requested=[(area,i) for i,area in enumerate(mapped) if normalize_density and target_ids[i] is not None]
    targets=defaultdict(list)
    for area,i in requested:targets[target_ids[i]].append((area,i))
    if len(targets)>1:
        values=[math.sqrt(sum(area*norms[i]**2 for area,i in group)/sum(area for area,i in group))
                for group in targets.values()]
        if not math.isclose(min(values),max(values),rel_tol=1e-4,abs_tol=1e-10):
            raise ValueError('A fixed Shell Group or UV stack has incompatible Texel Group targets. Match its relative densities first, or use R and separate the overlaps.')
    weights=requested or list(zip(mapped,range(len(sources))))
    normalize=math.sqrt(sum(area*norms[i]**2 for area,i in weights)/sum(area for area,i in weights))
    points=[point for source in sources for tri in source for point in tri]
    minimum=tuple(min(point[axis] for point in points) for axis in (0,1))
    bounds=tuple((max(point[axis] for point in points)-minimum[axis])*normalize for axis in (0,1))
    local=[];seen=set()
    for source in sources:
        for tri in source:
            key=tuple(sorted(tuple(round(v,9) for v in point) for point in tri))
            if key in seen:continue
            seen.add(key)
            local.append(tuple(((x-minimum[0])*normalize,(y-minimum[1])*normalize) for x,y in tri))
    if len(sources)>1:
        from uv_straight_pack_metrics import union_area
        extent=max(bounds);unit=np.asarray(local,dtype=np.float64)/extent
        area=union_area(unit,1024,tuple(value/extent for value in bounds))*extent**2
        area=max(max(mapped)*normalize**2,area)
    else:area=mapped[0]*normalize**2
    return minimum,normalize,bounds,local,area


def bounds_components(surfaces,progress=None):
    """Disjoint positive-area bounding-box components can be checked separately."""
    count=len(surfaces);low=np.zeros((count,2));high=low.copy();valid=[]
    for i,surface in enumerate(surfaces):
        if progress and i%64==0:progress('Reading UV stacks...',i,count)
        points=np.asarray(surface,dtype=np.float64).reshape(-1,3,2)
        if not np.isfinite(points).all():raise ValueError('Invalid UV overlap coordinates.')
        if len(points):
            a,b=points[:,1]-points[:,0],points[:,2]-points[:,0]
            points=points[np.abs(a[:,0]*b[:,1]-a[:,1]*b[:,0])>0.]
        if len(points):low[i]=points.min(axis=(0,1));high[i]=points.max(axis=(0,1));valid.append(i)
    parents=list(range(count))
    def root(i):
        while parents[i]!=i:parents[i]=parents[parents[i]];i=parents[i]
        return i
    active=[]
    for at,i in enumerate(sorted(valid,key=lambda i:(low[i,0],i))):
        if progress and at%32==0:progress('Partitioning UV stack checks...',at,count)
        ids=np.asarray(active,dtype=np.intp);ids=ids[high[ids,0]>low[i,0]];active=ids.tolist()
        candidates=ids[(high[ids,1]>low[i,1]) & (low[ids,1]<high[i,1])]
        for j in candidates:parents[root(i)]=root(j)
        active.append(i)
    result=defaultdict(list)
    for i in range(count):result[root(i)].append(i)
    return list(result.values())


def _write(path,value):
    staged=path.with_suffix('.new')
    with staged.open('wb') as stream:pickle.dump(value,stream,protocol=4)
    for attempt in range(4):
        try:os.replace(staged,path);return
        except PermissionError:
            if attempt==3:raise
            time.sleep(.002)


def _read(path):
    try:
        with path.open('rb') as stream:return pickle.load(stream)
    except (OSError,EOFError):return None


def _batches(items,weight,count):
    batches=[[] for _ in range(count)];loads=[0]*count
    for item in sorted(items,key=weight,reverse=True):
        at=min(range(count),key=lambda i:loads[i]);batches[at].append(item);loads[at]+=weight(item)
    return [batch for batch in batches if batch]


def _compute(operation,items):
    if operation=='overlaps':
        from uv_straight_pack_stacks import overlap_groups
        return [sorted(indices[i] for i in group) for indices,surfaces in items for group in overlap_groups(surfaces)]
    if operation=='groups':return [(i,group_geometry(item)) for i,item in items]
    raise ValueError('Unknown numerical preparation operation.')


class PreparationCPU:
    def __init__(self,progress=None,enabled=True,workers=None):
        self.progress=progress;self.enabled=enabled
        self.workers=max(1,min(4,(os.cpu_count() or 2)//2-2)) if workers is None else max(1,min(4,int(workers)))
        self.directory=None;self.processes=[];self.serial=0;self.error=None;self.seen={};self.used_workers=0

    def __enter__(self):return self

    def __exit__(self,*unused):self.close()

    def start(self):
        if self.directory is not None:return
        executable=Path(sys.executable)
        if executable.name.lower() in ('maya.exe','maya'):
            executable=executable.with_name('mayapy.exe' if os.name=='nt' else 'mayapy')
        if not executable.is_file():raise NumericWorkerError('Numerical interpreter is unavailable.')
        self.directory=tempfile.TemporaryDirectory(prefix='ShellShifter_Prepare_');self.root=Path(self.directory.name)
        environment=os.environ.copy();environment.update(OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',
            OPENBLAS_NUM_THREADS='1',MAYA_SKIP_USERSETUP_PY='1')
        for index in range(self.workers):
            self.processes.append(subprocess.Popen([str(executable),str(Path(__file__).resolve()),'--worker',
                str(self.root),str(index),str(os.getpid())],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,env=environment,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)))
        self.used_workers=len(self.processes)

    def map(self,operation,items,phase,weight):
        try:
            self.start();jobs=_batches(items,weight,self.workers*8);pending=list(enumerate(jobs));active={};result=[]
            total=len(items);done=0;last_progress=0.
            while pending or active:
                for index,process in enumerate(self.processes):
                    if process.poll() is not None:raise NumericWorkerError('A numerical worker exited unexpectedly.')
                    if index not in active and pending:
                        job,batch=pending.pop(0);self.serial+=1
                        active[index]=(self.serial,len(batch))
                        _write(self.root/('request%d.pkl'%index),dict(id=self.serial,operation=operation,items=batch))
                    if index not in active:continue
                    output=self.root/('result%d.pkl'%index)
                    try:stamp=output.stat().st_mtime_ns
                    except OSError:continue
                    if stamp==self.seen.get(index):continue
                    message=_read(output)
                    if message is not None:self.seen[index]=stamp
                    if message is None or message['id']!=active[index][0]:continue
                    if 'error' in message:raise NumericWorkerError(message['error'])
                    result.extend(message['result']);done+=active.pop(index)[1]
                if self.progress and time.monotonic()-last_progress>=.03:
                    last_progress=time.monotonic();self.progress(phase,done,total)
                if pending or active:time.sleep(.005)
            return result
        except (OSError,NumericWorkerError) as error:
            self.error=str(error);self.enabled=False;self.close()
            # Numeric work is read-only. Retrying locally cannot duplicate edits.
            result=[]
            for i,item in enumerate(items):
                if self.progress:self.progress(phase,i,len(items))
                result.extend(_compute(operation,[item]))
            return result

    def overlap_groups(self,surfaces):
        from uv_straight_pack_stacks import overlap_groups
        if not self.enabled or len(surfaces)<512:return overlap_groups(surfaces,self.progress)
        components=bounds_components(surfaces,self.progress)
        result=[group for group in components if len(group)==1]
        jobs=[(indices,[surfaces[i] for i in indices]) for indices in components if len(indices)>1]
        if jobs:
            weight=lambda item:len(item[0])*sum(len(s) for s in item[1])
            if len(jobs)>=32 and sum(weight(item) for item in jobs)>=20000:
                result.extend(self.map('overlaps',jobs,'Checking UV stacks...',weight))
            else:
                for i,item in enumerate(jobs):
                    if self.progress:self.progress('Checking UV stacks...',i,len(jobs))
                    result.extend(_compute('overlaps',[item]))
        return sorted(result,key=lambda group:group[0])

    def groups(self,items):
        expensive=(sum(len(item[0])>1 for item in items)>=128 or
                   sum(len(source) for item in items for source in item[0])>=200000)
        if not self.enabled or len(items)<256 or (self.directory is None and not expensive):
            result=[]
            for i,item in enumerate(items):
                if self.progress:self.progress('Preparing packing groups...',i,len(items))
                result.append(group_geometry(item))
            return result
        jobs=list(enumerate(items))
        result=self.map('groups',jobs,'Preparing packing groups...',
                        lambda entry:sum(len(s) for s in entry[1][0])*(32 if len(entry[1][0])>1 else 1))
        return [value for i,value in sorted(result)]

    def close(self):
        if self.directory is None:return
        try:(self.root/'stop').touch()
        except OSError:pass
        deadline=time.monotonic()+.3
        for process in self.processes:
            try:process.wait(timeout=max(0.,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:process.terminate()
        for process in self.processes:
            try:process.wait(timeout=1.)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        self.processes=[];self.directory.cleanup();self.directory=None


def _worker(root,index,parent):
    import ctypes
    handle=None
    if os.name=='nt':
        kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x100000,False,parent)
        if not handle:return
    last=None;last_stamp=None;request=root/('request%d.pkl'%index);output=root/('result%d.pkl'%index)
    try:
        while not (root/'stop').exists():
            if handle and kernel.WaitForSingleObject(handle,0)!=258:return
            if os.name!='nt':
                try:os.kill(parent,0)
                except ProcessLookupError:return
            try:stamp=request.stat().st_mtime_ns
            except OSError:time.sleep(.005);continue
            if stamp==last_stamp:time.sleep(.005);continue
            message=_read(request)
            if message is None or message['id']==last:time.sleep(.005);continue
            last=message['id'];last_stamp=stamp
            try:response=dict(id=last,result=_compute(message['operation'],message['items']))
            except Exception as error:response=dict(id=last,error=type(error).__name__+': '+str(error))
            _write(output,response)
    finally:
        if handle:kernel.CloseHandle(handle)


if __name__=='__main__' and len(sys.argv)==5 and sys.argv[1]=='--worker':
    _worker(Path(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4]))
