"""Physical UV packing bounds; one UV unit keeps its original pixel scale."""
import math

AREA_CHOICES=(('square','Square',(1.,1.)),
              ('horizontal','Bottom Half',(1.,.5)))


def area_size(value):
    try:
        values=tuple(float(v) for v in value)
    except (TypeError,ValueError):
        raise ValueError('Use two UV area dimensions greater than zero and at most one.')
    if len(values)!=2 or not all(math.isfinite(v) and 0.<v<=1. for v in values):
        raise ValueError('Use two UV area dimensions greater than zero and at most one.')
    return values


def grid_extent(grid,monitor):
    return tuple(math.floor(v*grid+1e-10) for v in getattr(monitor,'target_size',(1.,1.)))


def scale_limit(boxes,target_size,margin,rotate=False,areas=None):
    width,height=(v-2.*margin for v in target_size)
    limit=min(max(min(width/w,height/h),min(width/h,height/w)) if rotate
              else min(width/w,height/h) for w,h in boxes)
    area=sum(areas) if areas is not None else sum(w*h for w,h in boxes)
    return min(limit,math.sqrt(max(0.,width*height)/max(area,1e-24)))
