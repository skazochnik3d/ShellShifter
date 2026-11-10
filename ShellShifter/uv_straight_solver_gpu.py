"""Batched GPU Relax; Maya access/transactions stay in uv_straight_solver.

No per-iteration host synchronization. FP64, ordered inversion repairs, exact
pins, and the same local/global objective as the CPU solver. Guided islands
and islands over 256 UVs retain the CPU path (bounded local memory/watchdog).
"""
import atexit
import ctypes as ct
import ctypes.util
import os
import threading
import time
import numpy as np
from uv_straight_pack_gpu import OpenCLPlacement, GPUUnavailable, _check

_state=threading.local()


class GPURelax(OpenCLPlacement):
    def __init__(self):
        self.context=self.queue=self.program=None
        self.buffers={};self.kernels=[];self.prepared={};self.pending=[]
        self.profile=None;self.profile_events=[];self.dispatches=0
        try:
            loader=ct.WinDLL if os.name=='nt' else ct.CDLL
            path=os.path.join(os.environ.get('SystemRoot','C:/Windows'),'System32','OpenCL.dll') if os.name=='nt' else ctypes.util.find_library('OpenCL')
            if not path:raise GPUUnavailable('OpenCL driver is unavailable.')
            self.api=loader(path);self._bind();self.device=self._device();self.name=self._info(self.device,0x102B)
            error=ct.c_int();devices=(ct.c_void_p*1)(self.device)
            self.context=self.api.clCreateContext(None,1,devices,None,None,ct.byref(error));_check(error.value,'Creating Relax GPU context')
            self.queue=self.api.clCreateCommandQueue(self.context,self.device,0,ct.byref(error));_check(error.value,'Creating Relax GPU queue')
            from uv_straight_solver_kernels import SOURCE
            source=(ct.c_char_p*1)(SOURCE.encode('ascii'))
            self.program=self.api.clCreateProgramWithSource(self.context,1,source,None,ct.byref(error));_check(error.value,'Creating Relax GPU program')
            code=self.api.clBuildProgram(self.program,1,devices,b'-cl-std=CL1.2',None,None)
            if code:
                log=ct.create_string_buffer(32768)
                self.api.clGetProgramBuildInfo(self.program,self.device,0x1183,len(log),log,None)
                raise GPUUnavailable('Relax GPU compilation: '+log.value.decode('utf8','replace'))
            kernel=self.api.clCreateKernel(self.program,b'solve_islands',ct.byref(error));_check(error.value,'Creating Relax GPU kernel')
            self.kernels.append(kernel);self.named={'solve_islands':kernel};self.kernel_names={kernel:'solve_islands'}
        except (OSError,AttributeError,GPUUnavailable) as error:
            self.close();raise GPUUnavailable(str(error)) from error

    def solve(self,states):
        started=time.perf_counter();results=[]
        # Bounded dispatches avoid a long kernel on Windows' display GPU.
        for begin in range(0,len(states),64):
            batch=states[begin:begin+64]
            arrays=dict(jobs=[],tri_ranges=[],initial=[],anchors=[],aw=[],pinned=[],offsets=[0],
                        other=[],rest=[],weight=[],triangles=[],limits=[],spans=[])
            id_lists=[]
            for state in batch:
                ids=sorted(state['positions']);index={uv:i for i,uv in enumerate(ids)};id_lists.append(ids)
                base=len(arrays['initial']);n=len(ids)
                if not 1<=n<=256:raise GPUUnavailable('Island exceeds GPU local memory bound.')
                if state['stiff_edges'] or any(len(v)==1 for v in state['locked'].values()):
                    raise GPUUnavailable('Directional guides require the constrained CPU solver.')
                arrays['jobs'].append((base,n,state['iterations'],state.get('polish',0)))
                arrays['tri_ranges'].append((len(arrays['triangles']),len(state['orientation'])))
                for tri,limit in state['orientation'].items():
                    arrays['triangles'].append((*[index[i] for i in tri],0));arrays['limits'].append(limit)
                p=np.array([state['positions'][i] for i in ids]);arrays['initial'].extend(p)
                arrays['anchors'].extend(state['anchors'][i] for i in ids)
                arrays['spans'].append(max(float(np.ptp(p,axis=0).max()),1e-12))
                lengths=[length for a in ids for b,length in state['neighbors'][a].items() if a<b]
                median=float(np.median(lengths)) if lengths else 1.
                for i in ids:
                    fixed=bool(state['locked'][i]);arrays['pinned'].append(int(fixed))
                    arrays['aw'].append(state['anchor_weight']*(3. if fixed else 1.))
                    for j,length in state['neighbors'][i].items():
                        arrays['other'].append(index[j]);arrays['rest'].append(length)
                        arrays['weight'].append((median/max(length,median*1e-4))**2)
                    arrays['offsets'].append(len(arrays['other']))
            names=('jobs','tri_ranges','initial','anchors','aw','pinned','offsets','other','rest','weight','triangles','limits','spans')
            integers={'jobs':4,'tri_ranges':2,'pinned':1,'offsets':1,'other':1,'triangles':4}
            for name in names:
                value=np.asarray(arrays[name],dtype=np.int32 if name in integers else np.float64)
                self._write(name,value,blocking=False)
            output=np.empty((len(arrays['initial']),2),np.float64);stats=np.empty((len(batch),2),np.int32)
            self._buffer('output',output.nbytes);self._buffer('stats',stats.nbytes);self._buffer('baseline',output.nbytes)
            args=[ct.c_void_p(self.buffers[name][0]) for name in names+('output','stats','baseline')]
            self._arguments(self.kernels[0],args);self._enqueue(self.kernels[0],(64*len(batch),),local=(64,))
            self._read(self.buffers['output'][0],output);self._read(self.buffers['stats'][0],stats)
            baseline=np.empty_like(output) if any(s.get('polish') for s in batch) else None
            if baseline is not None:self._read(self.buffers['baseline'][0],baseline)
            if not np.isfinite(output).all():raise GPUUnavailable('Non-finite Relax GPU result.')
            if baseline is not None and not np.isfinite(baseline).all():raise GPUUnavailable('Non-finite Relax GPU baseline.')
            for row,(state,ids) in enumerate(zip(batch,id_lists)):
                base=arrays['jobs'][row][0];points=output[base:base+len(ids)]
                # Exact anchors also guard against a broken driver/kernel.
                fixed=[i for i,uv in enumerate(ids) if state['locked'][uv]]
                if fixed and not np.array_equal(points[fixed],np.array([state['positions'][ids[i]] for i in fixed])):
                    raise GPUUnavailable('Relax GPU pin validation failed.')
                details=dict(backend='GPU',warmup_iterations=int(stats[row,0]),global_passes=int(stats[row,1]))
                if state.get('polish'):details['_baseline']={uv:tuple(baseline[base+i]) for i,uv in enumerate(ids)}
                results.append(({uv:tuple(points[i]) for i,uv in enumerate(ids)},details))
        self.seconds=time.perf_counter()-started
        return results


def backend():
    failure=getattr(_state,'failure',None)
    if failure is not None:raise GPUUnavailable(failure)
    result=getattr(_state,'backend',None)
    if result is None:
        try:result=_state.backend=GPURelax()
        except GPUUnavailable as error:
            _state.failure=str(error);raise
    return result


def close():
    result=getattr(_state,'backend',None)
    if result is not None:result.close();_state.backend=None
    _state.failure=None


atexit.register(close)
