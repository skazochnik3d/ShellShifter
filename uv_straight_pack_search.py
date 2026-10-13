"""Pure-data packing search state. No scene or widget access."""
import time
from functools import wraps


def counted_trial(monitor_index):
    """Count a returned single-candidate pass; interruptions are excluded."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args,**kwargs):
            monitor=kwargs.get('monitor',args[monitor_index] if len(args)>monitor_index else None)
            if monitor is None:return function(*args,**kwargs)
            monitor.attempt()
            try:result=function(*args,**kwargs)
            except BaseException:
                monitor.abort_attempt();monitor.abort_round();raise
            scale=kwargs.get('scale',args[2] if len(args)>2 else 0.)
            monitor.finish_attempt(result is not None and scale>0.)
            monitor.finish_round()
            return result
        return wrapped
    return decorate


class SearchStopped(Exception):
    pass


class StageExpired(Exception):
    pass


class SearchMonitor:
    def __init__(self, areas, static_area=0., callback=None, cancel=None, seconds=30., target_size=(1.,1.)):
        from uv_straight_pack_area import area_size
        self.target_size=area_size(target_size)
        self.target_area=self.target_size[0]*self.target_size[1]
        self.areas = areas
        self.static_area = static_area
        self.callback, self.cancel = callback, cancel
        self.started = time.monotonic()
        # Zero is the public/UI unlimited value; only an explicit stop ends it.
        self.deadline = self.started+seconds if seconds else float('inf')
        self.seconds = seconds
        self.iteration = 0
        self.attempts = self.started_attempts = self.aborted_attempts = 0
        self.started_rounds = self.aborted_rounds = 0
        self.full_layouts = self.improvements = 0
        self.best = None
        self.default_turns = None
        self.last_update = 0.
        self.stopped = False
        self.stage_deadline = None
        self.precision = None
        self.engine = None
        self.parallel=1
        self.pool=[]
        self.failed_orders=[]
        self.horizontal_axes=()
        self.local_attempts=0
        self.fixed_shells=0

    def check(self):
        if (self.cancel is not None and self.cancel.is_set()) or time.monotonic() >= self.deadline:
            self.stopped = self.cancel is not None and self.cancel.is_set()
            raise SearchStopped()
        if self.stage_deadline is not None and time.monotonic() >= self.stage_deadline:
            raise StageExpired()

    def update(self, phase, done=0, total=0, force=False):
        self.check()
        now = time.monotonic()
        if self.callback is not None and (force or now-self.last_update >= .06):
            self.last_update = now
            self.callback(dict(phase=phase, done=done, total=total, iteration=self.iteration,
                               free_percent=None if self.best is None else self.best['free_percent'],
                               elapsed=now-self.started, seconds=self.seconds,
                               precision=self.precision,engine=self.engine,parallel=self.parallel,
                               attempts=self.attempts,full_layouts=self.full_layouts,
                               improvements=self.improvements,
                               active_attempts=self.started_attempts-self.attempts-self.aborted_attempts))

    def attempt(self,count=1):
        self.check()
        self.started_rounds+=1;self.started_attempts+=count
        try:self.update('Searching for a complete layout',force=True)
        except BaseException:
            self.abort_attempt(count);self.abort_round();raise

    def finish_attempt(self,complete=False,count=1):
        self.attempts+=count
        if complete:self.full_layouts+=count

    def abort_attempt(self,count=1):self.aborted_attempts+=count

    def finish_round(self):self.iteration+=1

    def abort_round(self):self.aborted_rounds+=1

    def statistics(self):
        return dict(iterations=self.iteration,attempts=self.attempts,
                    full_layouts=self.full_layouts,improvements=self.improvements,
                    aborted_attempts=self.aborted_attempts,
                    started_attempts=self.started_attempts)

    def consider(self, scale, positions, turns=None, order=None, objective=0):
        if scale <= 0:return
        coverage = sum(self.areas)*scale*scale+self.static_area
        candidate = dict(scale=scale, positions=dict(positions),
                         turns=dict(turns if turns is not None else self.default_turns or
                                    {i: False for i in positions}),
                         free_percent=max(0.,min(100.,100.*(1.-coverage/self.target_area))))
        if self.horizontal_axes:
            horizontal=sum(is_horizontal != bool(candidate['turns'][i]%2)
                           for i,is_horizontal in self.horizontal_axes)
            # A tiny density gain must not turn the requested horizontal rows
            # into a predominantly vertical layout. Pocket turns remain valid.
            if horizontal<len(self.horizontal_axes)*.75:return
            candidate['long_shells']=len(self.horizontal_axes)
            candidate['horizontal_long']=horizontal
        candidate['precision']=self.precision
        candidate['order']=tuple(order if order is not None else positions)
        candidate['objective']=objective
        # Keep several complete seeds, including distinct layouts at equal
        # scale. Local repair can escape a different layout's blocking pocket.
        if not any(p['scale']==scale and p['positions']==positions and p['turns']==candidate['turns']
                   for p in self.pool):
            self.pool.append(candidate)
            self.pool.sort(key=lambda p:p['scale'],reverse=True)
            del self.pool[4:]
        if self.best is not None and scale<=self.best['scale']:return
        self.best=candidate
        self.improvements+=1
        self.update('Better layout found', force=True)
