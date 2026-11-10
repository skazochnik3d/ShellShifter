"""Independent CPU contour searches. Children never import Maya or touch a scene."""
import os
import pickle
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _write(path, value):
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


class CPUAssist:
    """Poll only from the numerical search thread; no shared mutable monitor."""
    def __init__(self,data,monitor,workers=None):
        self.monitor=monitor
        self.directory=tempfile.TemporaryDirectory(prefix='ShellShifter_CPU_')
        self.root=Path(self.directory.name)
        self.processes=[];self.last_poll=0.;self.last_seed=None;self.busy=False
        self.seen={};self.counts={};self.accepted=0;self.error=None
        count=max(1,min(4,((os.cpu_count() or 2)-4)//4)) if workers is None else max(1,min(4,int(workers)))
        executable=Path(sys.executable)
        if executable.name.lower() in ('maya.exe','maya'):
            executable=executable.with_name('mayapy.exe' if os.name=='nt' else 'mayapy')
        if not executable.is_file():raise OSError('CPU search interpreter is unavailable.')
        payload={key:data[key] for key in ('boxes','triangles','areas','static_triangles','gap','margin','rotate','precision')}
        payload.update(target_size=monitor.target_size,static_area=monitor.static_area,
                       deadline=monitor.deadline,parent=os.getpid(),pack_groups=data.get('pack_groups',[]))
        _write(self.root/'input.pkl',payload)
        environment=os.environ.copy()
        # Each process is one CPU lane. Avoid nested BLAS thread pools.
        environment.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',
                           MAYA_SKIP_USERSETUP_PY='1')
        try:
            for index in range(count):
                process=subprocess.Popen([str(executable),str(Path(__file__).resolve()),'--worker',str(self.root),str(index)],
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    env=environment,creationflags=(getattr(subprocess,'CREATE_NO_WINDOW',0) |
                                   getattr(subprocess,'BELOW_NORMAL_PRIORITY_CLASS',0)))
                self.processes.append(process)
        except BaseException:
            self.close();raise
        monitor.cpu_workers=len(self.processes)
        self.poll(force=True)

    def poll(self,force=False):
        if self.busy:return
        now=time.monotonic()
        if not force and now-self.last_poll<.25:return
        self.last_poll=now;self.busy=True
        m=self.monitor
        try:
            if m.best is not self.last_seed and m.best is not None:
                _write(self.root/'seed.pkl',m.best);self.last_seed=m.best
            for index,process in enumerate(self.processes):
                path=self.root/('result%d.pkl'%index)
                try:stamp=path.stat().st_mtime_ns
                except OSError:continue
                if stamp==self.seen.get(index):continue
                state=_read(path)
                if state is None:continue
                self.seen[index]=stamp;self.counts[index]=state
                if state.get('error'):self.error=state['error']
                candidate=state.get('best')
                if candidate is None:continue
                if m.best is not None and candidate['scale']<m.best['scale']:continue
                if set(candidate['positions'])!=set(range(len(m.areas))):continue
                previous=m.best;precision=m.precision
                m.precision=candidate['precision']
                try:m.consider(candidate['scale'],candidate['positions'],candidate['turns'],candidate['order'],notify=False)
                finally:m.precision=precision
                if m.best is not previous:self.accepted+=1
            m.cpu_iterations=sum(s.get('iterations',0) for s in self.counts.values())
            m.cpu_layouts=sum(s.get('layouts',0) for s in self.counts.values())
            m.cpu_improvements=self.accepted
        except OSError as error:
            # A CPU helper failure must never discard the GPU's complete best.
            self.error=str(error)
        finally:self.busy=False

    def _stop_workers(self):
        try:(self.root/'stop').touch()
        except OSError:pass
        deadline=time.monotonic()+.5
        for process in self.processes:
            try:process.wait(timeout=max(0.,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:process.terminate()
        for process in self.processes:
            try:process.wait(timeout=1.)
            except subprocess.TimeoutExpired:process.kill();process.wait()

    def finish(self):
        self._stop_workers()
        try:self.poll(force=True)
        finally:self.directory.cleanup()

    def close(self):
        self._stop_workers()
        self.directory.cleanup()


def _worker(root,index):
    import ctypes
    from copy import deepcopy
    from uv_straight_pack_search import SearchMonitor,SearchStopped
    import uv_straight_contour_pack as contour
    data=_read(root/'input.pkl')
    if data is None:return
    m=SearchMonitor(data['areas'],data['static_area'],seconds=0,target_size=data['target_size'])
    m.deadline=data['deadline'];m.pack_groups=data['pack_groups'];m.pack_boxes=data['boxes']
    m.search_upgrade=True;m.resumed=False;m.resume_rounds=0
    # Distinct deterministic orders across CPU lanes and the GPU search.
    original_order=contour._order
    contour._order=lambda boxes,ordering:original_order(boxes,ordering+100003*(index+1))
    last=[0.];last_seed=[None];busy=[False]
    handle=None
    if os.name=='nt':
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=kernel.OpenProcess(0x100000,False,data['parent'])
        if not handle:return
    original_check=m.check
    def check():
        original_check()
        if busy[0] or time.monotonic()-last[0]<.5:return
        last[0]=time.monotonic();busy[0]=True
        try:
            if (root/'stop').exists():raise SearchStopped()
            if handle and kernel.WaitForSingleObject(handle,0)!=258:raise SearchStopped()
            if os.name!='nt':
                try:os.kill(data['parent'],0)
                except ProcessLookupError:raise SearchStopped()
            seed_path=root/'seed.pkl'
            try:stamp=seed_path.stat().st_mtime_ns
            except OSError:stamp=None
            if stamp is not None and stamp!=last_seed[0]:
                seed=_read(seed_path);last_seed[0]=stamp
                if seed and (m.best is None or seed['scale']>m.best['scale']):
                    seed=deepcopy(seed);seed['_external_cpu_seed']=True
                    m.best=seed;m.pool=[seed]
            publish()
        finally:busy[0]=False
    def publish():
        own=m.best if m.best is not None and not m.best.get('_external_cpu_seed') else None
        _write(root/('result%d.pkl'%index),dict(iterations=m.iteration,layouts=m.full_layouts,best=own))
    m.check=check
    try:
        contour.search(data['boxes'],data['triangles'],data['areas'],data['gap'],data['margin'],data['rotate'],
                       data['static_triangles'],m,min(data['precision'],(512,1024,2048,4096)[index%4]))
    finally:
        try:publish()
        except OSError:pass
        if handle:kernel.CloseHandle(handle)


if __name__=='__main__' and len(sys.argv)==4 and sys.argv[1]=='--worker':
    root=Path(sys.argv[2]);index=int(sys.argv[3])
    try:_worker(root,index)
    except Exception as error:
        try:_write(root/('result%d.pkl'%index),dict(error=type(error).__name__+': '+str(error)))
        except OSError:pass
        raise
