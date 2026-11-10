"""Conservative local-AABB fold-and-push seed, inspired by TABI (2026).

Independent implementation for quarter turns and one uniform chart scale.
Interlocked row components move rigidly after compaction; exact validation is
mandatory before any proxy layout can enter the numerical search's best pool.
"""
import ctypes as ct
import math
import time
from uv_straight_deps import np

BANDS=16
CONFIGURATIONS=8
ROW_LIMIT=256
PROXY_BYTES=64*1024*1024

SOURCE=r'''
#define TABI_BANDS 16
#define TABI_CONFIGS 8
#define TABI_ROW 256
#define TABI_GROUP 128

// Local AABBs in a 4x4 chart partition capture both boundary directions and
// large voids. Their union contains the complete triangle surface.
__kernel void tabi_profiles(__global const double2 *triangles,
    __global const int2 *ranges, __global const double2 *boxes,
    __global double4 *proxies, int count) {
    int chart=get_global_id(0), band=get_global_id(1);
    if(chart>=count || band>=TABI_BANDS) return;
    double2 box=boxes[chart]; int2 span=ranges[chart];
    int bx=band&3,by=band>>2;
    double2 low=(double2)(box.x*bx/4,box.y*by/4),high=(double2)(box.x*(bx+1)/4,box.y*(by+1)/4);
    double left=INFINITY,right=-INFINITY,bottom=INFINITY,top=-INFINITY;
    for(int t=span.x;t<span.x+span.y;++t) {
        double2 p[3]={triangles[t*3],triangles[t*3+1],triangles[t*3+2]};
        if(fmin(p[0].x,fmin(p[1].x,p[2].x))>high.x || fmax(p[0].x,fmax(p[1].x,p[2].x))<low.x ||
           fmin(p[0].y,fmin(p[1].y,p[2].y))>high.y || fmax(p[0].y,fmax(p[1].y,p[2].y))<low.y) continue;
        double2 polygon[8],temp[8];int n=3;
        for(int j=0;j<3;++j) polygon[j]=p[j];
        for(int axis=0;axis<2;++axis) for(int side=0;side<2 && n;++side) {
            double bound=side?high[axis]:low[axis];int out=0;
            double2 previous=polygon[n-1];int before=side?previous[axis]<=bound:previous[axis]>=bound;
            for(int j=0;j<n;++j) {
                double2 point=polygon[j];int inside=side?point[axis]<=bound:point[axis]>=bound;
                if(inside!=before) {
                    double f=(bound-previous[axis])/(point[axis]-previous[axis]);
                    temp[out++]=previous+f*(point-previous);
                }
                if(inside) temp[out++]=point;
                previous=point;before=inside;
            }
            n=out;for(int j=0;j<n;++j) polygon[j]=temp[j];
        }
        for(int j=0;j<n;++j) {
            left=fmin(left,polygon[j].x);right=fmax(right,polygon[j].x);
            bottom=fmin(bottom,polygon[j].y);top=fmax(top,polygon[j].y);
        }
    }
    proxies[chart*TABI_BANDS+band]=left<=right ? (double4)(left,bottom,right,top) : (double4)(1.,1.,0.,0.);
}

// Quantize outward, covering the exact rasterizer's FP32 boundary tolerance.
// Rotate on the same ceil-sized integer canvases as the contour engine.
__kernel void tabi_scale(__global const double4 *proxies,
    __global const double2 *boxes, __global const double *scales,
    __global const int *turns, __global int4 *rects, __global int2 *dims,
    int count, int size, int layouts) {
    int chart=get_global_id(0), slot=get_global_id(1);
    if(chart>=count || slot>=layouts) return;
    int at=slot*count+chart,turn=turns[at]; double factor=scales[slot]*size;
    double2 box=(boxes[chart]*scales[slot])*size;
    int w=max(1,(int)ceil(box.x)),h=max(1,(int)ceil(box.y));
    dims[at]=(turn&1)?(int2)(h,w):(int2)(w,h);
    for(int band=0;band<TABI_BANDS;++band) {
        double4 p=proxies[chart*TABI_BANDS+band];int4 r=(int4)(1,1,0,0);
        if(p.x<=p.z && p.y<=p.w) {
            r=(int4)(max(0,(int)floor(p.x*factor-0.005)),max(0,(int)floor(p.y*factor-0.005)),
                     min(w,(int)floor(p.z*factor+0.005)+1),min(h,(int)floor(p.w*factor+0.005)+1));
            if(turn==1) r=(int4)(h-r.w,r.x,h-r.y,r.z);
            else if(turn==2) r=(int4)(w-r.z,h-r.w,w-r.x,h-r.y);
            else if(turn==3) r=(int4)(r.y,w-r.z,r.w,w-r.x);
        }
        rects[at*TABI_BANDS+band]=r;
    }
}

__kernel void tabi_heights(__global const uint *tile, __global int *heights,
    int stride, int size, int width, int height) {
    int x=get_global_id(0);if(x>=width) return;
    int top=0;
    for(int y=height-1;y>=0;--y) if(tile[y*stride+(x>>5)]&(1u<<(x&31))) {top=y+1;break;}
    heights[x]=top;
}

__kernel void tabi_start(__global const int *heights, __global int *front,
    int width, int layouts, int gap, int margin) {
    int x=get_global_id(0),slot=get_global_id(1);
    if(x>=width || slot>=layouts) return;
    int value=margin;
    for(int col=max(0,x-gap);col<min(width,x+gap+1);++col) {
        int h=heights[col];if(h) value=max(value,h+gap);
    }
    front[slot*width+x]=value;
}

inline int tabi_max(int value,__local int *scratch) {
    int lid=get_local_id(0);scratch[lid]=value;barrier(CLK_LOCAL_MEM_FENCE);
    for(int step=TABI_GROUP/2;step;step>>=1) {
        if(lid<step) scratch[lid]=max(scratch[lid],scratch[lid+step]);
        barrier(CLK_LOCAL_MEM_FENCE);
    }
    int result=scratch[0];barrier(CLK_LOCAL_MEM_FENCE);return result;
}

// Each workgroup tries one direction / compaction / knee combination.
// All earlier members of a row are tested, so non-neighbour interlocks are
// safe too. Only horizontally interlocked components must move rigidly.
__kernel void tabi_rows(__global const int4 *rects, __global const int2 *dims,
    __global const int *orders, __global const int4 *state, __global const int *front,
    __global int *row_x, __global int *row_y, __global int4 *choices, int count, int width,
    int height, int gap, int margin) {
    int task=get_group_id(0),slot=task/TABI_CONFIGS,cfg=task%TABI_CONFIGS;
    int lid=get_local_id(0);int4 s=state[slot];
    if(s.y!=1) {if(!lid) choices[task]=(int4)(0,0,0,0);return;}
    int lo=(cfg&4)?s.z:margin,hi=(cfg&4)?s.w:width-margin;
    int first=orders[slot*count+s.x],row_h=dims[slot*count+first].y;
    if(hi<=lo || row_h>height-2*margin) {if(!lid) choices[task]=(int4)(0,0,0,0);return;}
    __local int xs[TABI_ROW],ws[TABI_ROW],parents[TABI_ROW],bases[TABI_ROW],scratch[TABI_GROUP];
    int length=0,end=0;
    for(int j=0;j<TABI_ROW && s.x+j<count;++j) {
        int chart=orders[slot*count+s.x+j],at=slot*count+chart;
        int2 dim=dims[at];int local_y=row_h-dim.y,required=0;
        if(cfg&2) {
            for(int pair=lid;pair<j*TABI_BANDS*TABI_BANDS;pair+=TABI_GROUP) {
                int previous=pair/(TABI_BANDS*TABI_BANDS),rem=pair%(TABI_BANDS*TABI_BANDS);
                int id=orders[slot*count+s.x+previous],old=slot*count+id;
                int2 old_dim=dims[old];
                int4 a=rects[old*TABI_BANDS+rem/TABI_BANDS],b=rects[at*TABI_BANDS+rem%TABI_BANDS];
                if(a.x>=a.z || a.y>=a.w || b.x>=b.z || b.y>=b.w) continue;
                int ay=row_h-old_dim.y;
                if(ay+a.y-gap>=local_y+b.w || local_y+b.y>=ay+a.w+gap) continue;
                int right=(cfg&1)?old_dim.x-a.x:a.z,left=(cfg&1)?dim.x-b.z:b.x;
                required=max(required,xs[previous]+right+gap-left);
            }
        } else required=j?end+gap:0;
        int x=tabi_max(required,scratch);
        if(x+dim.x>hi-lo) break;
        if(!lid) {xs[j]=x;ws[j]=dim.x;}
        barrier(CLK_LOCAL_MEM_FENCE);
        end=max(end,x+dim.x);length=j+1;
    }
    if(!length) {if(!lid) choices[task]=(int4)(0,0,0,0);return;}
    // Disjoint X intervals can push independently. Union every overlapping
    // pair, including non-neighbours, preserving the relative Y of interlocks.
    if(!lid) {
        for(int j=0;j<length;++j) {
            parents[j]=j;
            for(int k=0;k<j;++k) {
                if(xs[j]>=xs[k]+ws[k]+gap || xs[k]>=xs[j]+ws[j]+gap) continue;
                int a=j,b=k;
                while(parents[a]!=a) a=parents[a];
                while(parents[b]!=b) b=parents[b];
                parents[max(a,b)]=min(a,b);
            }
        }
        for(int j=0;j<length;++j) {int root=j;while(parents[root]!=root) root=parents[root];parents[j]=root;}
    }
    for(int j=lid;j<length;j+=TABI_GROUP) bases[j]=-0x7fffffff;
    barrier(CLK_LOCAL_MEM_FENCE);
    int peak=margin;
    for(int x=margin+lid;x<width-margin;x+=TABI_GROUP) peak=max(peak,front[slot*width+x]);
    peak=tabi_max(peak,scratch);
    for(int j=lid;j<length;j+=TABI_GROUP) {
        int chart=orders[slot*count+s.x+j],at=slot*count+chart;int2 dim=dims[at];
        int x=(cfg&1)?hi-xs[j]-dim.x:lo+xs[j],local_y=row_h-dim.y,base=margin-local_y;
        for(int band=0;band<TABI_BANDS;++band) {
            int4 r=rects[at*TABI_BANDS+band];if(r.x>=r.z || r.y>=r.w) continue;
            for(int col=x+r.x;col<x+r.z;++col) base=max(base,front[slot*width+col]-local_y-r.y);
        }
        atomic_max(bases+parents[j],base);
    }
    barrier(CLK_LOCAL_MEM_FENCE);
    int top=margin;
    for(int j=lid;j<length;j+=TABI_GROUP) {
        int chart=orders[slot*count+s.x+j],h=dims[slot*count+chart].y;
        int y=bases[parents[j]]+row_h-h;
        row_x[task*TABI_ROW+j]=xs[j];row_y[task*TABI_ROW+j]=y;
        top=max(top,y+h);
    }
    top=tabi_max(top,scratch);
    if(!lid) choices[task]=top<=height-margin ?
        (int4)(length,0,max(peak,top+gap),row_h):(int4)(0,0,0,0);
}

inline int tabi_direction(__global const int4 *c,int a,int b) {
    if(!c[a].x) return b;if(!c[b].x) return a;
    return c[a].z<c[b].z || (c[a].z==c[b].z && c[a].x>=c[b].x)?a:b;
}
__kernel void tabi_choose(__global const int4 *rects,__global const int2 *dims,
    __global const int *orders,__global const int *turns,__global int4 *state,
    __global int *front,__global const int *row_x,__global const int *row_y,__global const int4 *choices,
    __global int4 *output,int count,int width,int gap,int margin) {
    int slot=get_group_id(0),lid=get_local_id(0);int4 s=state[slot];if(s.y!=1) return;
    __global const int4 *c=choices+slot*TABI_CONFIGS;
    __local int chosen;
    if(!lid) {
        // Compact horizontally only when it lets more charts enter this row.
        int l=c[2].x>c[0].x?2:0,r=c[3].x>c[1].x?3:1;
        int a=tabi_direction(c,l,r);
        l=c[6].x>c[4].x?6:4;r=c[7].x>c[5].x?7:5;
        int b=tabi_direction(c,l,r);
        chosen=(!c[a].x || (c[b].x && c[b].z+1<c[a].z))?b:a;
    }
    barrier(CLK_LOCAL_MEM_FENCE);
    int cfg=chosen;int4 row=c[cfg];
    if(!row.x) {if(!lid) state[slot].y=0;return;}
    int lo=(cfg&4)?s.z:margin,hi=(cfg&4)?s.w:width-margin;
    int task=slot*TABI_CONFIGS+cfg;
    for(int j=lid;j<row.x;j+=TABI_GROUP) {
        int chart=orders[slot*count+s.x+j],at=slot*count+chart;int2 dim=dims[at];
        int offset=row_x[task*TABI_ROW+j],x=(cfg&1)?hi-offset-dim.x:lo+offset;
        output[at]=(int4)(x,row_y[task*TABI_ROW+j],turns[at],1);
    }
    for(int entry=lid;entry<row.x*TABI_BANDS;entry+=TABI_GROUP) {
        int j=entry/TABI_BANDS,band=entry%TABI_BANDS;
        int chart=orders[slot*count+s.x+j],at=slot*count+chart;int2 dim=dims[at];
        int4 r=rects[at*TABI_BANDS+band];if(r.x>=r.z || r.y>=r.w) continue;
        int offset=row_x[task*TABI_ROW+j],x=(cfg&1)?hi-offset-dim.x:lo+offset;
        int top=row_y[task*TABI_ROW+j]+r.w+gap;
        for(int col=max(0,x+r.x-gap);col<min(width,x+r.z+gap);++col)
            atomic_max(front+slot*width+col,top);
    }
    barrier(CLK_GLOBAL_MEM_FENCE);
    if(!lid) {
        int next=s.x+row.x,peak=margin;
        for(int x=margin;x<width-margin;++x) peak=max(peak,front[slot*width+x]);
        int threshold=max(4,(peak-margin)/6),begin=margin,left=0,right=0;
        for(int x=margin;x<=width-margin;++x) {
            if(x<width-margin && front[slot*width+x]<peak-threshold) continue;
            if(x-begin>right-left) {left=begin;right=x;}begin=x+1;
        }
        state[slot]=(int4)(next,next==count?2:1,left,right);
    }
}

// Exact triangle geometry gate. A conservative supercover of the canonical
// raster is rotated cell-by-cell, matching the contour engine's ceil canvas.
// Any accepted plan therefore also fits its ordinary Euclidean gap masks.
__kernel void tabi_owner_init(__global const uint *tile,__global int *owners,
    int stride,int width,int height) {
    int x=get_global_id(0),y=get_global_id(1);if(x>=width || y>=height) return;
    owners[y*width+x]=(tile[y*stride+(x>>5)]&(1u<<(x&31)))?0:0x7fffffff;
}
__kernel void tabi_owner_raster(__global const double2 *triangles,
    __global const int *charts,__global const double2 *boxes,__global const int4 *placements,
    __global int *owners,__global int *invalid,int count,double scale,int size,
    int width,int height,int extent_x,int extent_y,int margin) {
    int triangle=get_global_id(0)/TABI_GROUP,lid=get_local_id(0);if(triangle>=count) return;
    int chart=charts[triangle],owner=chart+1;int4 location=placements[chart];
    __local double2 points[3];__local int first,last,bw,bh;
    if(!lid) {
        double2 box=(boxes[chart]*scale)*size;bw=max(1,(int)ceil(box.x));bh=max(1,(int)ceil(box.y));
        int w=(location.z&1)?bh:bw,h=(location.z&1)?bw:bh;
        if(location.x<margin || location.y<margin || location.x+w>extent_x-margin || location.y+h>extent_y-margin)
            atomic_or(invalid,1);
        double low=INFINITY,high=-INFINITY;
        for(int j=0;j<3;++j) {
            points[j]=(triangles[triangle*3+j]*scale)*size;
            low=fmin(low,points[j].y);high=fmax(high,points[j].y);
        }
        first=max(0,(int)floor(low-0.005));last=min(bh-1,(int)floor(high+0.005));
    }
    barrier(CLK_LOCAL_MEM_FENCE);
    for(int row=first+lid;row<=last;row+=TABI_GROUP) {
        double2 polygon[8],temp[8];int n=3;
        for(int j=0;j<3;++j) polygon[j]=points[j];
        for(int side=0;side<2 && n;++side) {
            double bound=row+side+(side?0.005:-0.005);int out=0;
            double2 previous=polygon[n-1];int before=side?previous.y<=bound:previous.y>=bound;
            for(int j=0;j<n;++j) {
                double2 point=polygon[j];int inside=side?point.y<=bound:point.y>=bound;
                if(inside!=before) {
                    double f=(bound-previous.y)/(point.y-previous.y);
                    temp[out++]=(double2)(previous.x+f*(point.x-previous.x),bound);
                }
                if(inside) temp[out++]=point;
                previous=point;before=inside;
            }
            n=out;for(int j=0;j<n;++j) polygon[j]=temp[j];
        }
        if(!n) continue;
        double left=polygon[0].x,right=left;
        for(int j=1;j<n;++j) {left=fmin(left,polygon[j].x);right=fmax(right,polygon[j].x);}
        int a=max(0,(int)floor(left-0.005)),b=min(bw-1,(int)floor(right+0.005));
        for(int col=a;col<=b;++col) {
            int x=col,y=row;
            if(location.z==1) {x=bh-1-row;y=col;}
            else if(location.z==2) {x=bw-1-col;y=bh-1-row;}
            else if(location.z==3) {x=row;y=bw-1-col;}
            x+=location.x;y+=location.y;
            if(x<0 || y<0 || x>=width || y>=height) {atomic_or(invalid,1);continue;}
            int old=atomic_min(owners+y*width+x,owner);
            if(old!=0x7fffffff && old!=owner) atomic_or(invalid,1);
        }
    }
}

// Separable min/max owner filters detect another chart within the square
// gap neighbourhood of each occupied cell. No cross-chart mask can be hidden
// by a union, including multiple source triangles of the same chart.
__kernel void tabi_owner_horizontal(__global const int *owners,__global uint *spans,
    int width,int height,int gap) {
    int x=get_global_id(0),y=get_global_id(1);if(x>=width || y>=height) return;
    int lo=65535,hi=0;
    for(int col=max(0,x-gap);col<min(width,x+gap+1);++col) {
        int owner=owners[y*width+col];if(owner==0x7fffffff) continue;
        lo=min(lo,owner);hi=max(hi,owner);
    }
    spans[y*width+x]=(uint)lo|((uint)hi<<16);
}
__kernel void tabi_owner_vertical(__global const int *owners,__global const uint *spans,
    __global int *invalid,int width,int height,int gap) {
    int x=get_global_id(0),y=get_global_id(1);if(x>=width || y>=height) return;
    int owner=owners[y*width+x];if(owner==0x7fffffff) return;
    for(int row=max(0,y-gap);row<min(height,y+gap+1);++row) {
        uint span=spans[row*width+x];int lo=span&65535,hi=span>>16;
        if(lo!=65535 && (lo!=owner || hi!=owner)) {atomic_or(invalid,1);return;}
    }
}
'''


def hypotheses(boxes,scales,rotate):
    """Four quarter-turn policies, sorted by their actual oriented heights."""
    boxes=np.asarray(boxes,dtype=float)
    result=[]
    for scale in scales:
        for policy in range(4 if rotate else 1):
            tall=policy<2
            odd=(boxes[:,0]>boxes[:,1]) if tall else (boxes[:,0]<boxes[:,1])
            turns=np.where(odd,1,0).astype(np.int32) if rotate else np.zeros(len(boxes),dtype=np.int32)
            if policy%2:turns=(turns+2)%4
            dims=np.where((turns%2)[:,None],boxes[:,::-1],boxes)
            order=np.lexsort((np.arange(len(boxes)),-dims[:,0],-dims[:,1])).astype(np.int32)
            result.append((float(scale),turns,order))
    return result


def proxy_layouts(backend,boxes,triangles,scales,grid,gap,margin,rotate,static_triangles,monitor):
    """One GPU workgroup per row configuration; no per-chart host waits."""
    from uv_straight_pack_area import grid_extent
    from uv_straight_contour_pack import _static_rows,_cell_origin
    b=backend;p,i=ct.c_void_p,ct.c_int
    b.load_tabi()
    if not len(scales) or not len(boxes):return []
    if len(scales)*(4 if rotate else 1)>32:raise ValueError('At most 32 TABI hypotheses per batch.')
    if any(not math.isfinite(s) or s<=0 for s in scales):raise ValueError('Invalid TABI scale.')
    if len(scales)*(4 if rotate else 1)*len(boxes)*BANDS*16>PROXY_BYTES:return []
    b.bind_geometry(boxes,triangles)
    count=len(boxes);width,height=grid_extent(grid,monitor)
    g,m=math.ceil(gap*grid),math.ceil(margin*grid)
    if min(width,height)<=2*m:return []
    source=b._write('tabi_boxes',np.asarray(boxes,dtype=np.float64),False)
    profiles=b._buffer('tabi_profiles',count*BANDS*32)
    if getattr(b,'tabi_source',None) is not triangles:
        ranges=b._write('tabi_ranges',np.asarray(b.triangle_ranges,dtype=np.int32),False)
        b._launch('tabi_profiles',[p(b.geometry),p(ranges),p(source),p(profiles),i(count)],(count,BANDS))
        b.tabi_source=triangles
    specs=hypotheses(boxes,scales,rotate);slots=len(specs)
    factors=b._write('tabi_scales',np.asarray([s[0] for s in specs],dtype=np.float64),False)
    turns=b._write('tabi_turns',np.asarray([s[1] for s in specs],dtype=np.int32),False)
    orders=b._write('tabi_orders',np.asarray([s[2] for s in specs],dtype=np.int32),False)
    rects=b._buffer('tabi_rects',slots*count*BANDS*16)
    dims=b._buffer('tabi_dims',slots*count*8)
    b._launch('tabi_scale',[p(profiles),p(source),p(factors),p(turns),p(rects),p(dims),i(count),i(grid),i(slots)],(count,slots))
    heights=b._buffer('tabi_heights',width*4)
    if static_triangles:
        occupied=_static_rows(static_triangles,grid,monitor,b);b.reset(occupied,grid)
        b._launch('tabi_heights',[p(b.tile),p(heights),i(b.stride),i(grid),i(width),i(height)],(width,))
    else:heights=b._write('tabi_heights',np.zeros(width,dtype=np.int32),False)
    front=b._buffer('tabi_front',slots*width*4)
    b._launch('tabi_start',[p(heights),p(front),i(width),i(slots),i(g),i(m)],(width,slots))
    values=np.zeros((slots,4),dtype=np.int32);values[:,1]=1
    state=b._write('tabi_state',values,False)
    row_x=b._buffer('tabi_row_x',slots*CONFIGURATIONS*ROW_LIMIT*4)
    row_y=b._buffer('tabi_row_y',slots*CONFIGURATIONS*ROW_LIMIT*4)
    choices=b._buffer('tabi_choices',slots*CONFIGURATIONS*16)
    output=b._buffer('tabi_output',slots*count*16)
    row_args=[p(rects),p(dims),p(orders),p(state),p(front),p(row_x),p(row_y),p(choices),i(count),i(width),i(height),i(g),i(m)]
    choose_args=[p(rects),p(dims),p(orders),p(turns),p(state),p(front),p(row_x),p(row_y),p(choices),p(output),i(count),i(width),i(g),i(m)]
    for first in range(0,count,4):
        monitor.update('Building a compact starting layout',int(values[:,0].max()),count)
        for _ in range(min(4,count-first)):
            b._arguments(b.named['tabi_rows'],row_args)
            b._enqueue(b.named['tabi_rows'],(slots*CONFIGURATIONS*128,),None,(128,))
            b._arguments(b.named['tabi_choose'],choose_args)
            b._enqueue(b.named['tabi_choose'],(slots*128,),None,(128,))
        b._read(state,values);monitor.check()
        if not np.any(values[:,1]==1):break
    cells=b._read(output,np.empty((slots,count,4),dtype=np.int32))
    results=[]
    for slot,(scale,turn,order) in enumerate(specs):
        if values[slot,1]!=2:continue
        raw=cells[slot].copy()
        positions={j:_cell_origin(boxes[j],scale,grid,int(x),int(y),int(t)) for j,(x,y,t,valid) in enumerate(raw)}
        results.append(dict(scale=scale,positions=positions,turns={j:int(t) for j,t in enumerate(turn)},
                            order=tuple(map(int,order)),cells=raw))
    return results


def seed(boxes,triangles,areas,gap,margin,rotate,static_triangles,monitor,grid,backend):
    """Bounded startup stage; only a full exact fit may replace the safe floor."""
    from uv_straight_pack_area import scale_limit
    started=time.monotonic();previous=monitor.stage_deadline
    remaining=monitor.deadline-started
    budget=min(2.,max(.05,remaining*.10))
    monitor.stage_deadline=min(previous or float('inf'),started+budget)
    accepted=False
    try:
        maximum=min(8,PROXY_BYTES//max(1,len(boxes)*BANDS*16*(4 if rotate else 1)))
        if not maximum:return False
        low=monitor.best['scale'] if monitor.best else 0.
        high=scale_limit(boxes,monitor.target_size,margin,rotate,areas)
        for refinement in range(2):
            monitor.check()
            bottom=low*1.001 if low else high*.20
            if bottom>=high:break
            scales=np.linspace(bottom,high,maximum).tolist()
            slots=len(scales)*(4 if rotate else 1)
            monitor.parallel=slots;monitor.precision=grid
            monitor.attempt(slots);finished=False;verified=None
            try:
                monitor.tabi_attempts+=slots
                layouts=backend.tabi_many(boxes,triangles,scales,grid,gap,margin,rotate,static_triangles,monitor)
                monitor.tabi_proxy_layouts+=len(layouts)
                layouts.sort(key=lambda row:row['scale'],reverse=True)
                for layout in layouts:
                    if layout['scale']<=low:break
                    monitor.update('Checking the starting layout against exact contours',force=True)
                    monitor.tabi_validations+=1
                    fits=validate(backend,boxes,triangles,layout,grid,gap,margin,rotate,static_triangles,monitor)
                    if fits:verified=layout;break
                complete=int(verified is not None)
                monitor.finish_attempt(bool(complete),complete)
                monitor.finish_attempt(False,slots-complete);monitor.finish_round();finished=True
                if verified is not None:
                    from uv_straight_contour_pack import _cell_origin
                    # Publish the coordinates that were checked, never a
                    # separate cached transformation accompanying the cells.
                    points={j:_cell_origin(boxes[j],verified['scale'],grid,int(x),int(y),int(t))
                            for j,(x,y,t,valid) in enumerate(verified['cells'])}
                    turns={j:int(t) for j,(x,y,t,valid) in enumerate(verified['cells'])}
                    monitor.tabi_validated+=1
                    low=verified['scale'];accepted=True
                    monitor.tabi_scale=max(monitor.tabi_scale,low)
                    monitor.consider(verified['scale'],points,turns,order=verified['order'])
                upper=[s for s in scales if s>low]
                if not layouts or not upper:break
                high=min(upper)
            finally:
                if not finished:monitor.abort_attempt(slots);monitor.abort_round()
            if monitor.stage_deadline-time.monotonic()<.2:break
        return accepted
    finally:
        try:release_temporary(backend)
        finally:
            monitor.tabi_seconds+=time.monotonic()-started
            monitor.stage_deadline=previous;monitor.parallel=1


def release_temporary(backend):
    """Startup scratch must not occupy memory during the exact layout search."""
    if not any(isinstance(key,str) and key.startswith('tabi_') for key in backend.buffers):return
    backend.finish_run();backend.pending.clear()
    for key in list(backend.buffers):
        if isinstance(key,str) and key.startswith('tabi_'):
            handle,_=backend.buffers.pop(key);backend.api.clReleaseMemObject(handle)
    backend.tabi_source=backend.tabi_owner_source=None


def validate(backend,boxes,triangles,layout,grid,gap,margin,rotate,static_triangles,monitor):
    """Exact geometry with a conservative gap; bounded to 128 MiB scratch.

    Larger atlases / owner ids use the existing compact contour validator.
    The ordinary validator also resolves any conservative supercover rejection.
    """
    from uv_straight_pack_area import grid_extent
    from uv_straight_contour_pack import _static_rows
    b=backend;p,i,d=ct.c_void_p,ct.c_int,ct.c_double
    b.load_tabi()
    cells=np.asarray(layout['cells'])
    if (cells.shape!=(len(boxes),4) or not np.all(np.isfinite(cells)) or not np.all(cells==np.floor(cells)) or
        not np.all((cells[:,2]>=0)&(cells[:,2]<4)) or not np.all(cells[:,3]==1) or
        (not rotate and np.any(cells[:,2])) or not math.isfinite(layout['scale']) or layout['scale']<=0 or
        any(len(shell)==0 for shell in triangles)):
        return False
    width,height=grid_extent(grid,monitor);g,m=math.ceil(gap*grid),math.ceil(margin*grid)
    occupied=_static_rows(static_triangles,grid,monitor,b);b.reset(occupied,grid)
    b.bind_geometry(boxes,triangles)
    fast=grid<=4096 and len(boxes)<65535 and g<=64
    if fast:
        monitor.tabi_fast_validations+=1
        if getattr(b,'tabi_owner_source',None) is not triangles:
            values=np.repeat(np.arange(len(triangles),dtype=np.int32),[len(shell) for shell in triangles])
            charts=b._write('tabi_triangle_charts',values,False);b.tabi_owner_source=triangles
        else:charts=b.buffers['tabi_triangle_charts'][0]
        source=b._write('tabi_boxes',np.asarray(boxes,dtype=np.float64),False)
        placements=b._write('tabi_placements',np.asarray(layout['cells'],dtype=np.int32),False)
        # Keep the entire backing tile: padding near a half-tile boundary can
        # still collide with the visible portion of a fixed shell beyond it.
        owners=b._buffer('tabi_owners',grid*grid*4);spans=b._buffer('tabi_owner_spans',grid*grid*4)
        invalid=b._write('tabi_invalid',np.zeros(1,dtype=np.int32),False)
        b._launch('tabi_owner_init',[p(b.tile),p(owners),i(b.stride),i(grid),i(grid)],(grid,grid))
        total=sum(len(shell) for shell in triangles)
        # Bound raster batches so stop/deadline checks do not wait for a scene.
        kernel=b.named['tabi_owner_raster']
        b._arguments(kernel,[p(b.geometry),p(charts),p(source),p(placements),p(owners),p(invalid),i(total),d(layout['scale']),i(grid),i(grid),i(grid),i(width),i(height),i(m)])
        for first in range(0,total,4096):
            monitor.check()
            b._enqueue(kernel,(min(4096,total-first)*128,),(first*128,),(128,))
        b._launch('tabi_owner_horizontal',[p(owners),p(spans),i(grid),i(grid),i(g)],(grid,grid))
        b._launch('tabi_owner_vertical',[p(owners),p(spans),p(invalid),i(grid),i(grid),i(g)],(grid,grid))
        accepted=not b._read(invalid,np.empty(1,dtype=np.int32))[0]
        monitor.check()
        if accepted:return True
    monitor.tabi_fallback_validations+=1
    placements=[(j,int(t),int(x),int(y)) for j,(x,y,t,valid) in enumerate(layout['cells'])]
    saved=b.shape_profiles;b.shape_profiles=None
    try:return all(b.fixed_groups(placements,layout['scale'],g,m,rotate,monitor))
    finally:b.shape_profiles=saved
