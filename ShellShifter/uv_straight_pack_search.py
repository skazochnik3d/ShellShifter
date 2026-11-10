"""Pure-data packing search state. No scene or widget access."""
import time
from functools import wraps

# Use the post-stack group count consistently for coarse scale exploration
# and sampled GPU origins. Real UV-layout selections often fall below 512.
SAMPLED_SEARCH_GROUPS=200


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


def _diverse_seeds(pool, target_size, limit=4):
    """Keep the strongest seed and spatially different near-best layouts.

    Sample fixed group IDs, not insertion order. Work is bounded even for
    thousands of shells; this changes search seeds, never the accepted best.
    """
    ranked=sorted(pool,key=lambda p:(p['scale'],-p.get('pack_group_spread',0.)),reverse=True)
    if len(ranked)<=limit:return ranked
    floor=ranked[0]['scale']*.998
    eligible=[p for p in ranked if p['scale']>=floor]
    ids=sorted(ranked[0]['positions'])
    ids=ids[::max(1,(len(ids)+31)//32)]
    width,height=target_size
    def distance(a,b):
        total=0.
        for i in ids:
            ax,ay=a['positions'][i];bx,by=b['positions'][i]
            total+=min(1.,((ax-bx)/width)**2+((ay-by)/height)**2)
            total+=.25*(int(a['turns'].get(i,0))%4!=int(b['turns'].get(i,0))%4)
        return total
    selected=[ranked[0]]
    remaining=eligible[1:]
    while remaining and len(selected)<limit:
        at=max(range(len(remaining)),key=lambda j:(
            min(distance(remaining[j],p) for p in selected),
            remaining[j]['scale'],-remaining[j].get('pack_group_spread',0.)))
        selected.append(remaining.pop(at))
    # If few near-best seeds exist, retain the strongest remaining alternatives.
    for p in ranked:
        if len(selected)>=limit:break
        if not any(p is q for q in selected):selected.append(p)
    return selected


class SearchMonitor:
    def __init__(self, areas, static_area=0., callback=None, cancel=None, seconds=30., target_size=(1.,1.), iteration_limit=0):
        from uv_straight_pack_area import area_size
        self.target_size=area_size(target_size)
        self.target_area=self.target_size[0]*self.target_size[1]
        self.areas = areas
        self.static_area = static_area
        self.static_area_method='triangle_sum'
        self.callback, self.cancel = callback, cancel
        self.started = time.monotonic()
        # Zero is the public/UI unlimited value; only an explicit stop ends it.
        self.deadline = self.started+seconds if seconds else float('inf')
        self.seconds = seconds
        self.iteration = 0
        self.iteration_limit=int(iteration_limit)
        if self.iteration_limit<0 or self.iteration_limit!=iteration_limit:raise ValueError('Iteration limit must be a non-negative integer.')
        self.iteration_limit_reached=False
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
        self.external_poll=None
        self.external_finish=None
        self.cpu_workers=self.cpu_iterations=self.cpu_layouts=self.cpu_improvements=0
        self.pool=[]
        self.failed_orders=[]
        self.horizontal_axes=()
        self.local_attempts=0
        self.fixed_shells=0
        self.static_source=None
        self.static_rows={}
        self.static_cache_hits=self.static_cache_builds=0
        self.local_successes=0
        self.local_seconds=0.
        self.coarse_attempts=self.fine_attempts=0
        self.rectangle_seed=None
        self.rectangle_cache_builds=self.rectangle_cache_hits=0
        self.tail_size=0
        self.tail_attempts=self.tail_successes=0
        self.tail_seconds=0.
        self.pocket_lookahead=False
        self.pocket_attempts=self.pocket_successes=0
        self.tabi_seed=False
        self.tabi_attempts=self.tabi_proxy_layouts=self.tabi_validations=self.tabi_validated=0
        self.tabi_seconds=self.tabi_scale=0.
        self.tabi_fast_validations=self.tabi_fallback_validations=0

    def check(self):
        if self.external_poll is not None:self.external_poll()
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
                               iteration_limit=self.iteration_limit,
                               precision=self.precision,engine=self.engine,parallel=self.parallel,
                               cpu_workers=self.cpu_workers,cpu_iterations=self.cpu_iterations,
                               cpu_layouts=self.cpu_layouts,cpu_improvements=self.cpu_improvements,
                               attempts=self.attempts,full_layouts=self.full_layouts,
                               improvements=self.improvements,
                               active_attempts=self.started_attempts-self.attempts-self.aborted_attempts))

    def attempt(self,count=1):
        # Stop before the next batch, leaving every complete candidate from
        # the last batch available for consider(), including its best layout.
        if self.iteration_limit and self.iteration>=self.iteration_limit:
            self.iteration_limit_reached=True;raise SearchStopped()
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
        return dict(cpu_workers=self.cpu_workers,cpu_iterations=self.cpu_iterations,
                    cpu_layouts=self.cpu_layouts,cpu_improvements=self.cpu_improvements,
                    iterations=self.iteration,attempts=self.attempts,
                    iteration_limit=self.iteration_limit,iteration_limit_reached=self.iteration_limit_reached,
                    full_layouts=self.full_layouts,improvements=self.improvements,
                    aborted_attempts=self.aborted_attempts,
                    started_attempts=self.started_attempts,
                    static_cache_builds=self.static_cache_builds,
                    static_cache_hits=self.static_cache_hits,
                    local_successes=self.local_successes,
                    local_seconds=self.local_seconds,coarse_attempts=self.coarse_attempts,
                    fine_attempts=self.fine_attempts,
                    rectangle_cache_builds=self.rectangle_cache_builds,
                    rectangle_cache_hits=self.rectangle_cache_hits,
                    tail_attempts=self.tail_attempts,tail_successes=self.tail_successes,
                    tail_seconds=self.tail_seconds,
                    pocket_attempts=self.pocket_attempts,pocket_successes=self.pocket_successes,
                    tabi_attempts=self.tabi_attempts,tabi_proxy_layouts=self.tabi_proxy_layouts,
                    tabi_validations=self.tabi_validations,tabi_validated=self.tabi_validated,
                    tabi_seconds=self.tabi_seconds,tabi_scale=self.tabi_scale,
                    tabi_fast_validations=self.tabi_fast_validations,
                    tabi_fallback_validations=self.tabi_fallback_validations,
                    static_area_method=self.static_area_method)

    def consider(self, scale, positions, turns=None, order=None, objective=0, notify=True):
        if scale <= 0:return False
        coverage = sum(self.areas)*scale*scale+self.static_area
        candidate = dict(scale=scale, positions=dict(positions),
                         turns=dict(turns if turns is not None else self.default_turns or
                                    {i: False for i in positions}),
                         free_percent=max(0.,min(100.,100.*(1.-coverage/self.target_area))))
        if self.horizontal_axes:
            horizontal=sum(is_horizontal != bool(candidate['turns'][i]%2)
                           for i,is_horizontal in self.horizontal_axes)
            # Orientation is a statistic, not a reason to reject a denser fit.
            candidate['long_shells']=len(self.horizontal_axes)
            candidate['horizontal_long']=horizontal
        candidate['precision']=self.precision
        candidate['order']=tuple(order if order is not None else positions)
        candidate['objective']=objective
        if getattr(self,'pack_groups',None):
            from uv_straight_pack_groups import spread
            candidate['pack_group_spread']=spread(self.pack_boxes,candidate,self.pack_groups)
        # Keep several complete seeds, including distinct layouts at equal
        # scale. Local repair can escape a different layout's blocking pocket.
        if not any(p['scale']==scale and p['positions']==positions and p['turns']==candidate['turns']
                   for p in self.pool):
            self.pool.append(candidate)
            self.pool=_diverse_seeds(self.pool,self.target_size)
        if self.best is not None and (scale<self.best['scale'] or
                (scale==self.best['scale'] and candidate.get('pack_group_spread',0.)>=self.best.get('pack_group_spread',0.))):return True
        self.best=candidate
        self.improvements+=1
        if notify:self.update('Better layout found', force=True)
        return True
