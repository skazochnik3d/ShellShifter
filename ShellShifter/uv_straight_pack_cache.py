"""One bounded, process-local packing session; bounded layout seeds and a per-run GPU backend."""
import atexit
from copy import deepcopy
import hashlib
import json
import threading
import numpy as np

if globals().get('_idle') is not None:_idle.close()
_lock=threading.Lock()
_idle=None
_GEOMETRY=('boxes','triangles','areas','static_triangles','obstacles')


def signature(data):
    digest=hashlib.sha256(b'ShellShifter pack session v1')
    for name in _GEOMETRY:
        value=data.get(name,[])
        if name=='triangles':
            digest.update(np.asarray([len(shell) for shell in value],dtype='<i8').tobytes())
            value=[triangle for shell in value for triangle in shell]
        array=np.asarray(value,dtype='<f8')
        digest.update(name.encode('ascii'))
        digest.update(str(array.shape).encode('ascii'));digest.update(array.tobytes())
    options={name:data.get(name,default) for name,default in (
        ('gap',0.),('margin',0.),('rotate',False),('precision',1024),('method','contours'),('static_area',0.),
        ('gpu',False),('cpu_assist',False),('target_size',(1.,1.)),('target_origin',(0.,0.)),
        ('preserve_scale',False),('shape_proxies',False),('resolution',2048),
        ('profiling',False),('sequence_steps',1),('tail_size',0),('search_upgrade',True),
        ('batch_preparation',True),('hierarchy',True),('interval_order',1),('tabi_seed',True),('pack_groups',[]))}
    digest.update(json.dumps(options,sort_keys=True,separators=(',',':')).encode('ascii'))
    return digest.digest()


class Session:
    def __init__(self,data,key):
        self.key=key
        self.geometry_target_size=tuple(data.get('target_size',(1.,1.)))
        self.backend=None
        self.best=None
        self.pool=[]
        self.failed_orders=[]
        self.rounds=0
        self.hit=False

    def restore(self,monitor):
        """Detach copies so an active search cannot mutate the saved state."""
        if self.best is None:return
        monitor.best=deepcopy(self.best)
        monitor.pool=deepcopy(self.pool or [self.best])
        monitor.failed_orders=deepcopy(self.failed_orders)

    def finish(self,best,rounds,pool=None,failed_orders=None):
        global _idle
        if self.key is None or best is None:
            self.close();return
        self.release_backend()
        from uv_straight_pack_search import _diverse_seeds
        self.best=deepcopy(best);self.rounds+=rounds
        candidates=list(pool or [])
        if not any(p['scale']==best['scale'] and p['positions']==best['positions'] and
                   p['turns']==best['turns'] for p in candidates):candidates.insert(0,best)
        self.pool=deepcopy(_diverse_seeds(candidates,self.geometry_target_size))
        self.failed_orders=deepcopy([f for f in (failed_orders or [])
            if best['scale']<=f['scale']<=best['scale']*1.025][:16])
        with _lock:
            previous=_idle;_idle=self
        if previous is not None and previous is not self:previous.close()

    def release_backend(self):
        backend, self.backend = self.backend, None
        if backend is not None:
            backend.close()

    def close(self):
        self.release_backend()
        self.best=None;self.pool=[];self.failed_orders=[]


def acquire(data):
    """Detach idle state: concurrent searches can never share a GPU queue."""
    global _idle
    shell_count = len(data.get('boxes', ()))
    key = (signature(data) if shell_count <= 2048 and data.get('reuse_cache', True)
           and data.get('method') == 'contours' else None)
    with _lock:
        previous, _idle = _idle, None
    if previous is not None and previous.key==key:
        previous.hit=True
        return previous
    if previous is not None:previous.close()
    return Session(data,key)


def close():
    global _idle
    with _lock:
        previous=_idle;_idle=None
    if previous is not None:previous.close()


def remember_committed(data,result):
    """Promote an independently certified, committed refinement for resume."""
    from uv_straight_pack_search import _diverse_seeds
    with _lock:
        if _idle is None:return
    key=signature(data)
    with _lock:
        session=_idle
        if session is None or session.key!=key:return
        if session.best is not None and result['scale']<session.best['scale']:return
        candidate=deepcopy(result)
        session.best=candidate
        session.pool=_diverse_seeds([candidate]+session.pool,session.geometry_target_size)
        session.failed_orders=[f for f in session.failed_orders
            if candidate['scale']<=f['scale']<=candidate['scale']*1.025]


atexit.register(close)
