"""Independent OpenCL contour placement, using only the installed GPU driver.

The host supplies conservative occupied cells and expanded shell intervals.
GPU work items test candidate XY origins in parallel and atomically keep the
lowest valid origin. No Maya, Qt, third-party engine or CPU OpenCL device.
"""
import ctypes as ct
import ctypes.util
import os
import math
from collections import OrderedDict
import numpy as np


class GPUUnavailable(RuntimeError):
    pass


SOURCE = r'''
#pragma OPENCL EXTENSION cl_khr_fp64 : enable
#pragma OPENCL FP_CONTRACT OFF

// Each item clips one triangle against one row slab, then fills its inclusive
// X interval. Double precision matches the host's UV arithmetic at boundaries.
__kernel void raster(__global const double2 *triangles, int count,
                     int width, int height, int stride, __global uint *mask) {
    int t = get_global_id(0), row = get_global_id(1);
    if (t >= count || row >= height) return;
    double2 polygon[8], temp[8];
    int n = 3;
    for (int i = 0; i < 3; ++i) polygon[i] = triangles[t * 3 + i];
    double low = fmin(polygon[0].y, fmin(polygon[1].y, polygon[2].y));
    double high = fmax(polygon[0].y, fmax(polygon[1].y, polygon[2].y));
    if (row < floor(low - 1e-10) || row >= floor(high + 1e-10) + 1) return;
    for (int side = 0; side < 2 && n; ++side) {
        double bound = row + side;
        int out = 0;
        double2 previous = polygon[n - 1];
        int before = side ? previous.y <= bound : previous.y >= bound;
        for (int i = 0; i < n; ++i) {
            double2 point = polygon[i];
            int inside = side ? point.y <= bound : point.y >= bound;
            if (inside != before) {
                double fraction = (bound - previous.y) / (point.y - previous.y);
                temp[out++] = (double2)(previous.x + fraction * (point.x - previous.x), bound);
            }
            if (inside) temp[out++] = point;
            previous = point; before = inside;
        }
        n = out;
        for (int i = 0; i < n; ++i) polygon[i] = temp[i];
    }
    if (!n) return;
    double left = polygon[0].x, right = left;
    for (int i = 1; i < n; ++i) {
        left = fmin(left, polygon[i].x); right = fmax(right, polygon[i].x);
    }
    int a = max(0, (int)floor(left - 1e-10));
    int b = min(width - 1, (int)floor(right + 1e-10));
    if (a > b) return;
    int first = a >> 5, last = b >> 5;
    for (int word = first; word <= last; ++word) {
        uint bits = 0xffffffffu;
        if (word == first) bits &= 0xffffffffu << (a & 31);
        if (word == last) bits &= 0xffffffffu >> (31 - (b & 31));
        atomic_or(mask + row * stride + word, bits);
    }
}

__kernel void locate(__global const uint *tile, __global const int4 *runs,
                     int count, int stride, int size, int minx, int maxx,
                     int firsty, int lasty, __global uint *best) {
    int x = minx + get_global_id(0), y = firsty + get_global_id(1);
    if (x > maxx || y > lasty) return;
    uint key = (uint)(y * size + x);
    if (key >= atomic_add(best, 0u)) return;
    for (int i = 0; i < count; ++i) {
        int4 r = runs[i];
        int yy = y + r.x;
        if (yy < 0 || yy >= size) continue;
        int a = max(0, x + r.y), b = min(size - 1, x + r.z);
        if (a > b) continue;
        int left = a >> 5, right = b >> 5;
        uint lo = 0xffffffffu << (a & 31);
        uint hi = 0xffffffffu >> (31 - (b & 31));
        int base = yy * stride;
        if (left == right) {
            if (tile[base + left] & lo & hi) return;
        } else {
            if ((tile[base + left] & lo) || (tile[base + right] & hi)) return;
            for (int w = left + 1; w < right; ++w)
                if (tile[base + w]) return;
        }
    }
    atomic_min(best, key);
}

__kernel void commit(__global uint *tile, __global const uint *shape,
                     int stride, int words, int height, int x, int y) {
    int word = get_global_id(0), row = get_global_id(1);
    if (word >= words || row >= height) return;
    uint value = shape[row * words + word];
    int target = (y + row) * stride + (x >> 5) + word, shift = x & 31;
    atomic_or(tile + target, value << shift);
    if (shift && (x >> 5) + word + 1 < stride)
        atomic_or(tile + target + 1, value >> (32 - shift));
}
'''


def _check(code, operation):
    if code != 0:
        raise GPUUnavailable('{} failed (OpenCL {}).'.format(operation, code))


class OpenCLPlacement:
    """Create/use/release on the numerical worker thread; no global handles."""
    def __init__(self):
        self.buffers = {}
        self.context = self.queue = self.program = None
        self.kernels = []
        self.dispatches = 0
        self.candidates = 0
        self.pending=[]
        self.prepared=OrderedDict()
        self.cache_bytes=0
        self.cache_hits=self.cache_misses=0
        self.solid_preparations=0
        self.geometry_source=None
        self.protected=set()
        try:
            loader = ct.WinDLL if os.name == 'nt' else ct.CDLL
            path = (os.path.join(os.environ.get('SystemRoot', 'C:/Windows'),
                                 'System32', 'OpenCL.dll') if os.name == 'nt' else
                    ctypes.util.find_library('OpenCL'))
            if not path: raise GPUUnavailable('The OpenCL GPU driver is unavailable.')
            self.api = loader(path)
            self._bind()
            self.device = self._device()
            self.name = self._info(self.device, 0x102B)  # CL_DEVICE_NAME
            error = ct.c_int()
            devices = (ct.c_void_p * 1)(self.device)
            self.context = self.api.clCreateContext(None, 1, devices, None, None, ct.byref(error))
            _check(error.value, 'Creating GPU context')
            self.queue = self.api.clCreateCommandQueue(self.context, self.device, 0, ct.byref(error))
            _check(error.value, 'Creating GPU queue')
            from uv_straight_pack_kernels import SOURCE as resident_source
            source = (ct.c_char_p * 1)((SOURCE+resident_source).encode('ascii'))
            self.program = self.api.clCreateProgramWithSource(self.context, 1, source, None, ct.byref(error))
            _check(error.value, 'Creating GPU program')
            code = self.api.clBuildProgram(self.program, 1, devices,b'-cl-std=CL1.2', None, None)
            if code:
                log = ct.create_string_buffer(32768)
                self.api.clGetProgramBuildInfo(self.program, self.device, 0x1183,
                                                len(log), log, None)
                raise GPUUnavailable('GPU kernel compilation: '+log.value.decode('utf8', 'replace'))
            names=(b'locate', b'commit', b'raster',b'raster_shell',b'rotate_shapes',
                   b'pad_shapes',b'count_runs',b'prefix_runs',b'emit_runs',b'locate_batch',b'commit_batch',
                   b'validate_fixed',b'locate_multi',b'commit_multi',b'assemble_batch',b'solid_shapes',b'raster_shell_float',b'shelf_layouts',b'locate_multi_direct',b'commit_multi_direct')
            self.named={}
            for name in names:
                kernel = self.api.clCreateKernel(self.program, name, ct.byref(error))
                _check(error.value, 'Creating GPU kernel')
                self.kernels.append(kernel)
                self.named[name.decode('ascii')]=kernel
        except (OSError, AttributeError, GPUUnavailable) as error:
            self.close()
            raise GPUUnavailable(str(error)) from error

    def _bind(self):
        p, u, i, sz, bits = ct.c_void_p, ct.c_uint, ct.c_int, ct.c_size_t, ct.c_uint64
        signatures = {
            'clGetPlatformIDs': (i, [u, p, p]),
            'clGetDeviceIDs': (i, [p, bits, u, p, p]),
            'clGetDeviceInfo': (i, [p, u, sz, p, p]),
            'clCreateContext': (p, [p, u, p, p, p, p]),
            'clCreateCommandQueue': (p, [p, p, bits, p]),
            'clCreateProgramWithSource': (p, [p, u, p, p, p]),
            'clBuildProgram': (i, [p, u, p, ct.c_char_p, p, p]),
            'clGetProgramBuildInfo': (i, [p, p, u, sz, p, p]),
            'clCreateKernel': (p, [p, ct.c_char_p, p]),
            'clCreateBuffer': (p, [p, bits, sz, p, p]),
            'clSetKernelArg': (i, [p, u, sz, p]),
            'clEnqueueWriteBuffer': (i, [p, p, u, sz, sz, p, u, p, p]),
            'clEnqueueReadBuffer': (i, [p, p, u, sz, sz, p, u, p, p]),
            'clEnqueueCopyBuffer': (i, [p,p,p,sz,sz,sz,u,p,p]),
            'clEnqueueNDRangeKernel': (i, [p, p, u, p, p, p, u, p, p]),
            'clFinish': (i, [p]),
        }
        for name in ('clReleaseMemObject', 'clReleaseKernel', 'clReleaseProgram',
                     'clReleaseCommandQueue', 'clReleaseContext'):
            signatures[name] = (i, [p])
        for name, (restype, argtypes) in signatures.items():
            function = getattr(self.api, name)
            function.restype, function.argtypes = restype, argtypes

    def _info(self, device, key):
        value = ct.create_string_buffer(1024)
        _check(self.api.clGetDeviceInfo(device, key, len(value), value, None), 'Reading GPU name')
        return value.value.decode('utf8', 'replace')

    def _device(self):
        count = ct.c_uint()
        _check(self.api.clGetPlatformIDs(0, None, ct.byref(count)), 'Finding OpenCL platforms')
        platforms = (ct.c_void_p * count.value)()
        _check(self.api.clGetPlatformIDs(count.value, platforms, None), 'Reading OpenCL platforms')
        for platform in platforms:
            count = ct.c_uint()
            code = self.api.clGetDeviceIDs(platform, 4, 0, None, ct.byref(count))  # GPU only
            if code == -1 or not count.value: continue
            _check(code, 'Finding GPUs')
            devices = (ct.c_void_p * count.value)()
            _check(self.api.clGetDeviceIDs(platform, 4, count.value, devices, None), 'Reading GPUs')
            return devices[0]
        raise GPUUnavailable('No OpenCL GPU device was found. Update the graphics driver.')

    def _buffer(self, key, nbytes):
        size = max(4, int(nbytes))
        if key not in self.buffers or self.buffers[key][1] < size:
            if key in self.buffers:
                self.api.clReleaseMemObject(self.buffers.pop(key)[0])
            error = ct.c_int()
            handle = self.api.clCreateBuffer(self.context, 1, size, None, ct.byref(error))
            _check(error.value, 'Allocating GPU buffer')
            self.buffers[key] = (handle, size)
        return self.buffers[key][0]

    def _write(self, key, values, blocking=True):
        values = np.ascontiguousarray(values)
        handle = self._buffer(key, values.nbytes)
        if values.nbytes:
            _check(self.api.clEnqueueWriteBuffer(self.queue, handle, int(blocking), 0, values.nbytes,
                                                values.ctypes.data, 0, None, None), 'Uploading GPU data')
            if blocking:self.pending.clear()
            else:self.pending.append(values)  # Driver reads these after this call returns.
        return handle

    def _read(self, handle, values):
        _check(self.api.clEnqueueReadBuffer(self.queue,handle,1,0,values.nbytes,
                    values.ctypes.data,0,None,None),'Reading GPU result')
        self.pending.clear()
        return values

    def _arguments(self,kernel,args):
        for index,value in enumerate(args):
            _check(self.api.clSetKernelArg(kernel,index,ct.sizeof(value),ct.byref(value)),
                   'Setting GPU argument')

    def _enqueue(self,kernel,shape,offset=None,local=None):
        sizes=(ct.c_size_t*len(shape))(*shape)
        origin=(ct.c_size_t*len(shape))(*offset) if offset is not None else None
        group=(ct.c_size_t*len(shape))(*local) if local is not None else None
        _check(self.api.clEnqueueNDRangeKernel(self.queue,kernel,len(shape),origin,sizes,group,
                   0,None,None),'Running GPU placement')
        self.dispatches+=1

    def _launch(self,name,args,shape):
        kernel=self.named[name]
        self._arguments(kernel,args)
        self._enqueue(kernel,shape)

    def bind_geometry(self,boxes,triangles):
        """Upload original geometry once per search, retain it across scales."""
        if self.geometry_source is triangles:return
        self.clear_cache()
        self.geometry_source=triangles
        self.boxes=boxes
        from uv_straight_contour_pack import solid_rectangle
        self.solids=[solid_rectangle(box,shell) for box,shell in zip(boxes,triangles)]
        self.geometry_ids=[];identities={}
        for index,(box,shell) in enumerate(zip(boxes,triangles)):
            signature=(tuple(box),tuple(sorted(tuple(sorted(tuple(point) for point in tri)) for tri in shell)))
            self.geometry_ids.append(identities.setdefault(signature,index))
        self.triangle_ranges=[]
        flat=[]
        for shell in triangles:
            self.triangle_ranges.append((len(flat),len(shell)))
            flat.extend(shell)
        self.raster_fp32=len(boxes)>=512
        self.geometry=self._write('geometry',np.asarray(flat,dtype=np.float64).reshape(-1,3,2))

    def clear_cache(self):
        for entry in self.prepared.values():
            for key in entry['keys']:
                handle,_=self.buffers.pop(key)
                self.api.clReleaseMemObject(handle)
        self.prepared.clear();self.cache_bytes=0

    def prepare_group(self,index,scale,gap,margin,rotate,monitor):
        from uv_straight_pack_area import grid_extent
        extent_x,extent_y=grid_extent(self.size,monitor)
        w,h=(v*scale*self.size for v in self.boxes[index])
        iw,ih=max(1,math.ceil(w)),max(1,math.ceil(h))
        if not ((iw<=extent_x-2*margin and ih<=extent_y-2*margin) or
                (rotate and ih<=extent_x-2*margin and iw<=extent_y-2*margin)):return None
        solid=self.solids[index]
        # Rectangles depend only on their conservative integer canvas; exact
        # duplicate triangle contours share a geometry identity. No tolerance
        # merges different shapes or weakens their spacing masks.
        key=(('solid',iw,ih) if solid else ('contour',self.geometry_ids[index],scale),
             self.size,extent_x,extent_y,gap,margin,bool(rotate))
        if key in self.prepared:
            self.cache_hits+=1;self.prepared.move_to_end(key)
            return self.prepared[key]
        monitor.check();self.cache_misses+=1
        turns=(0,1,2,3) if rotate else (0,)
        desc=[];raw_words=pad_words=row_count=0
        dimensions=[(ih,iw,t) if t%2 else (iw,ih,t) for t in turns]
        ranks={t:rank for rank,(_,_,t) in enumerate(sorted(dimensions,key=lambda d:(d[1],d[0],d[2])))}
        from uv_straight_contour_pack import rotation_options
        allowed=rotation_options(self.boxes[index],rotate)
        solid=self.solids[index]
        for width,height,turn in dimensions:
            stride=(width+31)//32
            pw,ph=width+2*gap,height+2*gap
            ps=(pw+31)//32
            desc.append((width,height,stride,raw_words,pw,ph,ps,pad_words,row_count,
                         row_count if solid else 0,ph if solid else 0,
                         turn,ranks[turn],extent_x-margin-width if turn in allowed else -1,
                         extent_y-margin-height if turn in allowed else -1,0))
            raw_words+=stride*height;pad_words+=ps*ph;row_count+=ph
        desc=np.asarray(desc,dtype=np.int32)
        keys=[('shape',key,name) for name in ('raw','desc','runs')]
        raw=self._write(keys[0],np.zeros(raw_words,dtype=np.uint32),False)
        descriptors=self._write(keys[1],desc,False)
        padded=self._buffer('padded',pad_words*4)
        counts=self._buffer('row_counts',row_count*4)
        prefix=self._buffer('row_prefix',row_count*4)
        total=self._buffer('total_runs',4)
        widths=np.asarray([math.ceil(math.sqrt(max(0.,gap*gap-max(0,abs(dy)-1)**2))-1e-12)
                           if gap else 0 for dy in range(-gap,gap+1)],dtype=np.int32)
        radii=self._write('gap_widths',widths,False)
        p,i,d=ct.c_void_p,ct.c_int,ct.c_double
        if solid:
            runs_count=row_count
            runs=self._buffer(keys[2],runs_count*16)
            self._launch('solid_shapes',[p(raw),p(runs),p(descriptors),p(radii),i(len(turns)),i(gap)],
                         (int(desc[:,2].max()),int(desc[:,5].max()),len(turns)))
            self.solid_preparations+=1
            return self.cache_entry(key,keys,raw,descriptors,runs,desc,w,h,iw,ih,raw_words,runs_count)
        offset,count=self.triangle_ranges[index]
        # All preparation is queued; only the run count returns to the host.
        for first in range(0,max(1,count),512):
            length=min(512,count-first)
            name='raster_shell_float' if self.raster_fp32 else 'raster_shell'
            self._launch(name,[p(self.geometry),i(offset+first),i(length),d(scale),d(self.size),
                i(iw),i(ih),i(desc[0,2]),p(raw)],(max(1,length),ih))
            if count>512:
                _check(self.api.clFinish(self.queue),'Rasterizing GPU contours')
                self.pending.clear();monitor.check()
        maximum_raw=(int(desc[:,2].max()),int(desc[:,1].max()),len(turns))
        self._launch('rotate_shapes',[p(raw),p(descriptors),i(len(turns))],maximum_raw)
        pad_height=int(desc[:,5].max())
        chunk=64 if gap>=32 else pad_height
        for first in range(0,pad_height,chunk):
            self._launch('pad_shapes',[p(raw),p(padded),p(descriptors),p(radii),i(len(turns)),i(gap),i(first)],
                      (int(desc[:,6].max()),min(chunk,pad_height-first),len(turns)))
            if gap>=32:
                _check(self.api.clFinish(self.queue),'Preparing GPU spacing')
                self.pending.clear();monitor.check()
        self._launch('count_runs',[p(padded),p(descriptors),i(len(turns)),p(counts)],
                     (int(desc[:,5].max()),len(turns)))
        self._launch('prefix_runs',[p(descriptors),i(len(turns)),p(counts),p(prefix),p(total)],(1,))
        # Compact exact storage keeps masks resident across whole large
        # layouts instead of evicting them for a worst-case run allocation.
        runs_count=int(self._read(total,np.zeros(1,dtype=np.int32))[0])
        monitor.check()
        runs=self._buffer(keys[2],runs_count*16)
        self._launch('emit_runs',[p(padded),p(descriptors),i(len(turns)),i(gap),p(prefix),p(runs)],
                     (int(desc[:,5].max()),len(turns)))
        return self.cache_entry(key,keys,raw,descriptors,runs,desc,w,h,iw,ih,raw_words,runs_count)

    def cache_entry(self,key,keys,raw,descriptors,runs,desc,w,h,iw,ih,raw_words,runs_count):
        entry=dict(raw=raw,desc=descriptors,runs=runs,host=desc,w=w,h=h,iw=iw,ih=ih,
                   keys=keys,cache_key=key,raw_bytes=raw_words*4,run_bytes=runs_count*16,
                   bytes=raw_words*4+desc.nbytes+max(4,runs_count*16))
        self.prepared[key]=entry;self.cache_bytes+=entry['bytes']
        # A bounded cache avoids consuming the memory Maya needs for its scene.
        while self.cache_bytes>256*1024*1024 and len(self.prepared)>1:
            victim=next((k for k in self.prepared if k not in self.protected and k!=key),None)
            if victim is None:break
            old=self.prepared.pop(victim);self.cache_bytes-=old['bytes']
            for buffer_key in old['keys']:
                handle,_=self.buffers.pop(buffer_key)
                self.api.clReleaseMemObject(handle)
        return entry

    def place_group(self,index,scale,gap,margin,rotate,monitor):
        entry=self.prepare_group(index,scale,gap,margin,rotate,monitor)
        if entry is None:return None
        desc=entry['host'];count=len(desc)
        result=np.array([0xffffffff],dtype=np.uint32)
        best=self._write('batch_best',result,False)
        p,i=ct.c_void_p,ct.c_int
        kernel=self.named['locate_batch']
        maxx,maxy=int(desc[:,13].max()),int(desc[:,14].max())
        step=self.origin_step()
        bundle=4 if len(self.boxes)>=512 else 1
        for sampling in ((step,1) if step>1 else (1,)):
            self._arguments(kernel,[p(self.tile),p(entry['runs']),p(entry['desc']),i(count),
                 i(self.stride),i(self.size),i(margin),i(margin),i(sampling),p(best)])
            nx=((maxx-margin)//sampling+32)//32*32
            rows=(maxy-margin)//sampling+1
            for first in range(0,rows,128*bundle):
                monitor.check()
                for chunk in range(first,min(rows,first+128*bundle),128):
                    monitor.check()
                    ny=((min(128,rows-chunk)+3)//4)*4
                    self._enqueue(kernel,(nx,ny,count),(0,chunk,0),(32,4,1))
                    self.candidates+=nx*ny*count
                self._read(best,result);monitor.check()
                if result[0]!=0xffffffff:break
            if result[0]!=0xffffffff:break
        if result[0]==0xffffffff:return None
        if step>1:
            y=int(result[0])//4//self.size
            self._arguments(kernel,[p(self.tile),p(entry['runs']),p(entry['desc']),i(count),
                i(self.stride),i(self.size),i(margin),i(max(margin,y-step+1)),i(1),p(best)])
            nx=((maxx-margin+32)//32)*32
            ny=((step+3)//4)*4
            self._enqueue(kernel,(nx,ny,count),None,(32,4,1))
            self.candidates+=nx*ny*count
            self._read(best,result);monitor.check()
        position,rank=divmod(int(result[0]),4)
        y,x=divmod(position,self.size)
        variant=int(np.flatnonzero(desc[:,12]==rank)[0]);turn=int(desc[variant,11])
        self._launch('commit_batch',[p(self.tile),p(entry['raw']),p(entry['desc']),i(variant),
               i(self.stride),i(x),i(y)],(int(desc[variant,2]),int(desc[variant,1])))
        return x,y,turn

    def origin_step(self):
        # Large selections sample positions first, then refine the winning
        # band with exact cells. A failed sample always retries every origin.
        return max(1,self.size//512) if len(self.boxes)>=512 else 1

    def shelves_many(self,boxes,scale,grid,gap,margin,rotate,orders,monitor):
        """Whole rectangle layouts on-device, in cancellable 128-shell chunks."""
        from uv_straight_contour_pack import rotation_options
        extent_x,extent_y=(v-2.*margin+gap for v in monitor.target_size)
        dims=np.asarray(boxes,dtype=np.float64)*scale+gap
        flags=np.asarray([sum(1<<t for t in set(turn%2 for turn in rotation_options(box,rotate)))
                          for box in boxes],dtype=np.int32)
        sizes=self._write('shelf_sizes',dims,False)
        allowed=self._write('shelf_flags',flags,False)
        order_buffer=self._write('shelf_orders',np.asarray(orders,dtype=np.int32),False)
        rows=self._write('shelf_rows',np.zeros((len(orders),len(boxes),3),dtype=np.float64),False)
        active_values=np.zeros((len(orders),2),dtype=np.int32);active_values[:,0]=1
        active=self._write('shelf_active',active_values,False)
        output=self._buffer('shelf_output',len(orders)*len(boxes)*3*8)
        p,i,d=ct.c_void_p,ct.c_int,ct.c_double
        kernel=self.named['shelf_layouts']
        monitor.attempt(len(orders))
        unfinished=np.ones(len(orders),dtype=bool)
        try:
            for first in range(0,len(boxes),128):
                monitor.update('Building compact GPU layouts',first,len(boxes))
                self._arguments(kernel,[p(sizes),p(allowed),p(order_buffer),p(rows),p(active),p(output),
                        i(len(boxes)),d(extent_x),d(extent_y),i(first),i(min(128,len(boxes)-first))])
                self._enqueue(kernel,(256*len(orders),),None,(256,))
                self._read(active,active_values)
                failed=unfinished & (active_values[:,0]==0)
                monitor.finish_attempt(False,int(failed.sum()));unfinished[failed]=False
                monitor.check()
                if not unfinished.any():break
            result=self._read(output,np.empty((len(orders),len(boxes),3),dtype=np.float64))
            layouts=[]
            for fits,values in zip(active_values[:,0],result):
                if not fits:layouts.append(None);continue
                positions={index:(float(x)+margin,float(y)+margin) for index,(x,y,t) in enumerate(values)}
                turns={index:int(t) for index,(x,y,t) in enumerate(values)}
                layouts.append((positions,turns))
            monitor.finish_attempt(True,int(unfinished.sum()));monitor.finish_round()
            return layouts
        except BaseException:
            monitor.abort_attempt(int(unfinished.sum()));monitor.abort_round();raise

    def fixed_group(self,index,scale,gap,margin,turn,x,y,rotate,monitor):
        entry=self.prepare_group(index,scale,gap,margin,rotate,monitor)
        if entry is None:return False
        variant=int(turn) if rotate else 0
        p,i=ct.c_void_p,ct.c_int
        result=self._buffer('fixed_result',4)
        self._launch('validate_fixed',[p(self.tile),p(entry['runs']),p(entry['desc']),i(variant),
              i(self.stride),i(self.size),i(margin),i(x),i(y),p(result)],(1,))
        fits=bool(self._read(result,np.zeros(1,dtype=np.uint32))[0])
        monitor.check()
        if fits:
            desc=entry['host'][variant]
            self._launch('commit_batch',[p(self.tile),p(entry['raw']),p(entry['desc']),i(variant),
                  i(self.stride),i(x),i(y)],(int(desc[2]),int(desc[1])))
        return fits

    def reset_many(self,occupied,size,count):
        self.size,self.stride=size,(size+31)//32
        data=b''.join(int(row).to_bytes(self.stride*4,'little') for row in occupied)*count
        self.tile=self._write('tile',np.frombuffer(data,dtype='<u4'))

    def place_many(self,indices,scale,gap,margin,rotate,monitor):
        entries=[]
        try:
            for index in indices:
                entry=self.prepare_group(index,scale,gap,margin,rotate,monitor) if index is not None else None
                entries.append(entry)
                if entry is not None:self.protected.add(entry['cache_key'])
            if not any(entries):return [None]*len(indices)
            fallback=next(entry for entry in entries if entry is not None)
            bound=[entry or fallback for entry in entries]
            active=self._write('active_slots',np.asarray([entry is not None for entry in entries],dtype=np.int32),False)
            result=np.full(len(entries),0xffffffff,dtype=np.uint32)
            best=self._write('multi_best',result,False)
            p,i=ct.c_void_p,ct.c_int
            direct=len(bound)<=4
            if direct:
                four=bound+[fallback]*(4-len(bound))
                shape_args=[p(e['runs']) for e in four]+[p(e['desc']) for e in four]
                commit_args=[p(e['raw']) for e in four]+[p(e['desc']) for e in four]
            else:
                raw=self._buffer('multi_raw',sum(e['raw_bytes'] for e in bound))
                runs=self._buffer('multi_runs',sum(e['run_bytes'] for e in bound))
                descriptors=self._buffer('multi_desc',len(bound)*64*4)
                raw_offset=run_offset=0
                for slot,entry in enumerate(bound):
                    for source,target,offset,nbytes in ((entry['raw'],raw,raw_offset,entry['raw_bytes']),
                                                       (entry['runs'],runs,run_offset,entry['run_bytes'])):
                        if nbytes:
                            _check(self.api.clEnqueueCopyBuffer(self.queue,source,target,0,offset,nbytes,
                                        0,None,None),'Preparing GPU batch')
                    self._launch('assemble_batch',[p(entry['desc']),p(descriptors),i(4 if rotate else 1),
                         i(slot),i(raw_offset//4),i(run_offset//16)],(64,))
                    raw_offset+=entry['raw_bytes'];run_offset+=entry['run_bytes']
                shape_args=[p(runs),p(descriptors)];commit_args=[p(raw),p(descriptors)]
            kernel=self.named['locate_multi_direct' if direct else 'locate_multi']
            requested=getattr(self,'placement_modes',None)
            self.current_modes=list(requested if requested is not None else
                                    [(slot%2 if len(self.boxes)>=512 else 0) for slot in range(len(entries))])
            modes=self._write('placement_modes',np.asarray(self.current_modes,dtype=np.int32),False)
            flags=[]
            for index in indices:
                box=self.boxes[index] if index is not None else (1.,1.)
                long=rotate and len(self.boxes)>=512 and max(box)>=4*min(box)
                flags.append((1 if box[0]>=box[1] else 2) if long else 3)
            horizontal=self._write('horizontal_rotations',np.asarray(flags,dtype=np.int32),False)
            common=[p(self.tile)]+shape_args+[p(active),i(len(entries)),i(4 if rotate else 1),
                    i(self.stride),i(self.size),i(margin),p(modes),p(horizontal)]
            maxx=max(int(e['host'][:,13].max()) for e in entries if e is not None)
            maxy=max(int(e['host'][:,14].max()) for e in entries if e is not None)
            origins=self._write('origin_rows',np.full(len(entries),margin,dtype=np.int32),False)
            step=self.origin_step()
            bundle=4 if len(self.boxes)>=512 else 1
            for sampling in ((step,1) if step>1 else (1,)):
                self._write('active_slots',np.asarray([entry is not None and value==0xffffffff
                      for entry,value in zip(entries,result)],dtype=np.int32),False)
                self._arguments(kernel,common+[p(origins),i(sampling),i(0),p(best)])
                nx=((maxx-margin)//sampling+32)//32*32
                rows=(maxy-margin)//sampling+1
                for first in range(0,rows,128*bundle):
                    monitor.check()
                    for chunk in range(first,min(rows,first+128*bundle),128):
                        monitor.check()
                        ny=((min(128,rows-chunk)+3)//4)*4
                        self._enqueue(kernel,(nx,ny,4*len(entries)),(0,chunk,0),(32,4,1))
                        self.candidates+=nx*ny*4*len(entries)
                    self._read(best,result);monitor.check()
                    if all(entry is None or value!=0xffffffff for entry,value in zip(entries,result)):break
                    self._write('active_slots',np.asarray([entry is not None and value==0xffffffff
                          for entry,value in zip(entries,result)],dtype=np.int32),False)
                if all(entry is None or value!=0xffffffff for entry,value in zip(entries,result)):break
            if step>1:
                self._write('active_slots',np.asarray([entry is not None and value!=0xffffffff
                      for entry,value in zip(entries,result)],dtype=np.int32),False)
                ys=np.asarray([max(margin,int(value)//4//self.size-step+1) if value!=0xffffffff else margin
                               for value in result],dtype=np.int32)
                self._write('origin_rows',ys,False)
                self._arguments(kernel,common+[p(origins),i(1),i(1),p(best)])
                nx=((maxx-margin+32)//32)*32;ny=((step+3)//4)*4
                self._enqueue(kernel,(nx,ny,4*len(entries)),None,(32,4,1))
                self.candidates+=nx*ny*4*len(entries)
                self._read(best,result);monitor.check()
            self._launch('commit_multi_direct' if direct else 'commit_multi',[p(self.tile)]+commit_args+[p(best),p(modes),
                    i(len(entries)),i(4 if rotate else 1),i(self.stride),i(self.size)],
                 (max(int(e['host'][:,2].max()) for e in bound),
                  max(int(e['host'][:,1].max()) for e in bound),len(entries)))
            locations=[]
            for slot,(entry,value) in enumerate(zip(entries,result)):
                if entry is None or value==0xffffffff:locations.append(None);continue
                position,rank=divmod(int(value),4);y,x=divmod(position,self.size)
                variant=int(np.flatnonzero(entry['host'][:,12]==rank)[0])
                y-=self.current_modes[slot]*int(entry['host'][variant,1])
                locations.append((x,y,int(entry['host'][variant,11])))
            return locations
        finally:self.protected.clear()

    def _dispatch(self, kernel, args, width, height):
        for index, value in enumerate(args):
            _check(self.api.clSetKernelArg(kernel, index, ct.sizeof(value), ct.byref(value)), 'Setting GPU argument')
        shape = (ct.c_size_t * 2)(width, height)
        _check(self.api.clEnqueueNDRangeKernel(self.queue, kernel, 2, None, shape, None,
                                              0, None, None), 'Running GPU placement')
        self.dispatches += 1

    def reset(self, occupied, size):
        self.size, self.stride = size, (size + 31) // 32
        bytewidth = self.stride * 4
        data = b''.join(int(row).to_bytes(bytewidth, 'little') for row in occupied)
        self.tile = self._write('tile', np.frombuffer(data, dtype='<u4'))

    def raster(self, triangles, width, height, monitor):
        monitor.check()
        stride=(width+31)//32
        points=np.asarray(triangles,dtype=np.float64)
        cells=np.zeros((height,stride),dtype=np.uint32)
        destination=self._write('raster',cells)
        if len(triangles):
            p,i=ct.c_void_p,ct.c_int
            # Split tall rasters for responsive cancellation even on a large
            # single shell; no dispatch grows with the whole scene.
            for start in range(0,len(triangles),256):
                chunk=np.ascontiguousarray(points[start:start+256])
                source=self._write('triangles',chunk)
                self._dispatch(self.kernels[2],[p(source),i(len(chunk)),i(width),i(height),
                    i(stride),p(destination)],len(chunk),height)
                _check(self.api.clFinish(self.queue),'Rasterizing GPU contours')
                monitor.check()
        _check(self.api.clEnqueueReadBuffer(self.queue,destination,1,0,cells.nbytes,
                                           cells.ctypes.data,0,None,None),'Reading GPU contours')
        # Reconstruct only for the existing quarter-turn/dedup preparation.
        mask=np.unpackbits(cells.view(np.uint8),axis=1,bitorder='little')[:,:width].astype(bool)
        monitor.check()
        return mask

    def location(self, occupied, padded, width, height, gap, margin, size, monitor, ceiling=None):
        from uv_straight_contour_pack import _runs
        from uv_straight_pack_area import grid_extent
        extent_x,extent_y=grid_extent(size,monitor)
        maxx, maxy = extent_x-margin-width, extent_y-margin-height
        if ceiling is not None: maxy = min(maxy, ceiling)
        if maxx < margin or maxy < margin: return None
        # Restrict the first dispatch when early rows still offer space. Bounding
        # each launch also makes cancellation observable between short GPU jobs.
        runs = [(dy-gap, start-gap, start-gap+length-1, 0)
                for dy, row in enumerate(padded) for start, length in _runs(row)]
        intervals = self._write('runs', np.asarray(runs, dtype=np.int32).reshape(-1, 4))
        result = np.array([0xffffffff], dtype=np.uint32)
        destination = self._write('best', result)
        ptr, integer = ct.c_void_p, ct.c_int
        for firsty in range(margin, maxy+1, 32):
            monitor.check()
            lasty = min(firsty+31, maxy)
            self._dispatch(self.kernels[0], [ptr(self.tile), ptr(intervals), integer(len(runs)),
                integer(self.stride), integer(size), integer(margin), integer(maxx),
                integer(firsty), integer(lasty), ptr(destination)], maxx-margin+1, lasty-firsty+1)
            self.candidates += (maxx-margin+1)*(lasty-firsty+1)
            _check(self.api.clEnqueueReadBuffer(self.queue, destination, 1, 0, result.nbytes,
                                               result.ctypes.data, 0, None, None), 'Reading GPU placement')
            monitor.check()
            if result[0] != 0xffffffff:
                return int(result[0] % size), int(result[0] // size)
        return None

    def commit(self, raw, width, x, y):
        words = (width+31)//32
        data = b''.join(int(row).to_bytes(words*4, 'little') for row in raw)
        source = self._write('shape', np.frombuffer(data, dtype='<u4'))
        p, i = ct.c_void_p, ct.c_int
        self._dispatch(self.kernels[1], [p(self.tile), p(source), i(self.stride),
            i(words), i(len(raw)), i(x), i(y)], words, len(raw))

    def close(self):
        if not hasattr(self, 'api'): return
        if self.queue: self.api.clFinish(self.queue)
        self.pending.clear()
        self.prepared.clear()
        for handle, _ in self.buffers.values(): self.api.clReleaseMemObject(handle)
        self.buffers.clear()
        for kernel in self.kernels: self.api.clReleaseKernel(kernel)
        self.kernels.clear()
        for name, release in (('program', 'clReleaseProgram'), ('queue', 'clReleaseCommandQueue'),
                              ('context', 'clReleaseContext')):
            handle = getattr(self, name, None)
            if handle: getattr(self.api, release)(handle)
            setattr(self, name, None)
