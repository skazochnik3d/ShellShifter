"""Optional conservative primitive proxies; original UV geometry is never edited.

Classification is performed once. Pixel-error limits are evaluated at each trial's
actual scale, so a small input shell cannot silently become an inaccurate proxy.
"""
from collections import Counter, defaultdict
import math
from uv_straight_deps import np

MAX_ERROR_PX = 1.0
MAX_CIRCLE_DIAMETER_PX = 32.0
MIN_RECTANGLE_FILL = .97
MIN_CIRCLE_FILL = .80


def _convex_boundary(triangles):
    edges = Counter(tuple(sorted((tuple(a), tuple(b)))) for tri in triangles
                    for a, b in zip(tri, tri[1:]+tri[:1]) if a != b)
    if not edges or any(count > 2 for count in edges.values()):
        return None
    adjacent = defaultdict(list)
    for (a, b), count in edges.items():
        if count == 1:
            adjacent[a].append(b); adjacent[b].append(a)
    if not adjacent or any(len(v) != 2 for v in adjacent.values()):
        return None
    first = min(adjacent)
    polygon, previous, current = [], None, first
    for _ in range(len(adjacent)):
        polygon.append(current)
        neighbors = adjacent[current]
        following = neighbors[0] if neighbors[0] != previous else neighbors[1]
        previous, current = current, following
        if current == first:
            break
    if current != first or len(polygon) != len(adjacent):
        return None  # Holes or disconnected components require their real mask.
    points = np.asarray(polygon, dtype=float)
    following = np.roll(points, -1, axis=0)
    area = .5*float(np.sum(points[:,0]*following[:,1]-following[:,0]*points[:,1]))
    if area < 0:
        points = points[::-1]
        following = np.roll(points, -1, axis=0)
        area = -area
    delta = following-points
    next_delta = np.roll(delta, -1, axis=0)
    crosses = delta[:,0]*next_delta[:,1]-delta[:,1]*next_delta[:,0]
    extent = float(np.max(np.ptp(points, axis=0)))
    if extent <= 0 or np.any(crosses < -extent*extent*1e-12):
        return None
    triangle_area = math.fsum(abs((b[0]-a[0])*(c[1]-a[1])-
                                  (b[1]-a[1])*(c[0]-a[0]))*.5 for a,b,c in triangles)
    if area <= 0 or abs(triangle_area-area) > max(area*1e-8, 1e-24):
        return None
    return points, area


def _distance_to_edges(points, polygon):
    a = polygon
    delta = np.roll(polygon, -1, axis=0)-a
    squared = np.sum(delta*delta, axis=1)
    offset = np.asarray(points)[:,None,:]-a[None,:,:]
    t = np.clip(np.sum(offset*delta[None,:,:], axis=2)/np.maximum(squared,1e-300), 0., 1.)
    return np.sqrt(np.min(np.sum((offset-t[:,:,None]*delta)**2, axis=2), axis=1))


def _convex_components(triangles):
    """Keep slightly offset stacked copies separate, proving each filled shape.

    The packing group is a union of copies. A hole belongs to one connected
    triangulation and still fails _convex_boundary; it is never filled here.
    """
    parent=list(range(len(triangles)))
    def find(index):
        while parent[index]!=index:
            parent[index]=parent[parent[index]];index=parent[index]
        return index
    owners={}
    for index,tri in enumerate(triangles):
        for a,b in zip(tri,tri[1:]+tri[:1]):
            edge=tuple(sorted((tuple(a),tuple(b))))
            if edge in owners:
                parent[find(index)]=find(owners[edge])
            else:owners[edge]=index
    components=defaultdict(list)
    for index,tri in enumerate(triangles):
        components[find(index)].append(tri)
    if len(components)<2:
        return None
    boundaries=[_convex_boundary(component) for component in components.values()]
    return boundaries if all(boundary is not None for boundary in boundaries) else None


def profile(box, triangles):
    boundary = _convex_boundary(triangles)
    boundaries=[boundary] if boundary is not None else _convex_components(triangles)
    if not boundaries:
        return None
    points, area = max(boundaries,key=lambda item:item[1])
    w, h = box
    if min(w,h) <= 0:
        return None
    result = {}
    if area >= w*h*MIN_RECTANGLE_FILL:
        corners = np.asarray(((0.,0.),(w,0.),(w,h),(0.,h)))
        result['rectangle_error'] = min(float(np.max(_distance_to_edges(corners,polygon)))
                                        for polygon,_ in boundaries)
    if max(w,h) <= min(w,h)*1.25:
        center = np.asarray((w*.5,h*.5))
        radius = float(np.max(np.linalg.norm(np.asarray(triangles).reshape(-1,2)-center,axis=1)))
        if radius > 0 and area >= math.pi*radius*radius*MIN_CIRCLE_FILL:
            inner=[]
            for polygon,_ in boundaries:
                edges = np.roll(polygon,-1,axis=0)-polygon
                sides = edges[:,0]*(center[1]-polygon[:,1])-edges[:,1]*(center[0]-polygon[:,0])
                if np.all(sides >= -max(w,h)**2*1e-12):
                    inner.append(float(_distance_to_edges([center],polygon)[0]))
            if inner:
                result.update(circle=(float(center[0]),float(center[1]),radius),
                              circle_error=max(0.,radius-max(inner)))
    return result or None


def classify(boxes, triangles, monitor=None):
    profiles = []
    for index,(box,shell) in enumerate(zip(boxes,triangles)):
        if monitor is not None and index % 32 == 0:
            monitor.check()
        profiles.append(profile(box,shell))
    return profiles


def select(profile, scale, resolution):
    if not profile:
        return None
    factor = scale*resolution
    if profile.get('rectangle_error',float('inf'))*factor <= MAX_ERROR_PX:
        return ('rectangle',)
    circle = profile.get('circle')
    if (circle is not None and 2*circle[2]*factor <= MAX_CIRCLE_DIAMETER_PX and
            profile['circle_error']*factor <= MAX_ERROR_PX):
        return ('circle',)+circle
    return None


def raster(proxy, width, height, factor):
    """Conservative circle intersected with the shell bbox canvas, or full box."""
    if proxy[0] == 'rectangle':
        return np.ones((height,width),dtype=np.bool_)
    cx,cy,radius = (v*factor for v in proxy[1:])
    rows = np.arange(height,dtype=float)
    dy = np.maximum(0.,np.maximum(rows-cy,cy-rows-1.))
    dx = np.sqrt(np.maximum(0.,radius*radius-dy*dy))
    left = np.maximum(0,np.floor(cx-dx-1e-9).astype(np.int64))
    right = np.minimum(width-1,np.floor(cx+dx+1e-9).astype(np.int64))
    x = np.arange(width)
    return (dy[:,None] <= radius+1e-9) & (x >= left[:,None]) & (x <= right[:,None])
