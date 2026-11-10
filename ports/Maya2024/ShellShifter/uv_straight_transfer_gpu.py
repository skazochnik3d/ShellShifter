"""Thread-local OpenCL transfer with shared geometry and bounded sample memory."""
import ctypes as ct
import ctypes.util
import os
import time
from uv_straight_deps import np
from uv_straight_pack_gpu import OpenCLPlacement,GPUUnavailable,_check
from uv_straight_transfer_errors import CancelledError,DegenerateUvError,InputError,OverlapConflictError,TangentBasisError
from uv_straight_transfer_models import RasterTileResult,ColorRasterTileResult
from uv_straight_transfer_udim import triangle_tiles,udim_to_tile


class GPUTransfer(OpenCLPlacement):
    """Reuse driver/buffer helpers, compiling only the transfer kernels."""
    def __init__(self):
        self.context=self.queue=self.program=None
        self.buffers={};self.kernels=[];self.prepared={};self.pending=[]
        self.profile=None;self.profile_events=[];self.dispatches=0;self.geometry_key=None
        try:
            loader=ct.WinDLL if os.name=='nt' else ct.CDLL
            path=os.path.join(os.environ.get('SystemRoot','C:/Windows'),'System32','OpenCL.dll') if os.name=='nt' else ctypes.util.find_library('OpenCL')
            if not path:raise GPUUnavailable('OpenCL GPU driver is unavailable.')
            self.api=loader(path);self._bind();self.device=self._device();self.name=self._info(self.device,0x102B)
            error=ct.c_int();devices=(ct.c_void_p*1)(self.device)
            self.context=self.api.clCreateContext(None,1,devices,None,None,ct.byref(error));_check(error.value,'Creating transfer GPU context')
            self.queue=self.api.clCreateCommandQueue(self.context,self.device,0,ct.byref(error));_check(error.value,'Creating transfer GPU queue')
            from uv_straight_transfer_kernels import SOURCE
            source=(ct.c_char_p*1)(SOURCE.encode('ascii'))
            self.program=self.api.clCreateProgramWithSource(self.context,1,source,None,ct.byref(error));_check(error.value,'Creating transfer GPU program')
            code=self.api.clBuildProgram(self.program,1,devices,b'-cl-std=CL1.2',None,None)
            if code:
                log=ct.create_string_buffer(32768)
                self.api.clGetProgramBuildInfo(self.program,self.device,0x1183,len(log),log,None)
                raise GPUUnavailable('Transfer GPU compilation: '+log.value.decode('utf-8','replace'))
            self.named={}
            for name in ('transfer_pixels','pad_pixels'):
                kernel=self.api.clCreateKernel(self.program,name.encode('ascii'),ct.byref(error));_check(error.value,'Creating transfer GPU kernel')
                self.kernels.append(kernel);self.named[name]=kernel
            self.kernel_names={value:key for key,value in self.named.items()}
        except (OSError,AttributeError,GPUUnavailable) as error:
            self.close();raise GPUUnavailable(str(error)) from error

    @staticmethod
    def check(cancelled):
        if cancelled and cancelled():raise CancelledError('Texture transfer cancelled.')

    def bind(self,source,target,mapping,settings,cancelled=None):
        self.check(cancelled);started=time.perf_counter()
        if not 1<=settings.width<=8192 or not 1<=settings.height<=8192:raise InputError('Resolution must be between 1 and 8192.')
        if settings.supersampling not in (1,2,4):raise InputError('Supersampling: 1, 2 or 4.')
        corners=target.triangle_corners;source_corners=mapping.target_to_source[corners]
        target_ids=target.uv_set.corner_uv_ids[corners];source_ids=source.uv_set.corner_uv_ids[source_corners]
        if np.any(target_ids<0) or np.any(source_ids<0):raise InputError('A triangle has a corner without UVs.')
        target_uv=np.ascontiguousarray(target.uv_set.uvs[target_ids],dtype=np.float64)
        source_uv=np.ascontiguousarray(source.uv_set.uvs[source_ids],dtype=np.float64)
        a,b,c=target_uv[:,0],target_uv[:,1],target_uv[:,2]
        determinants=(b[:,1]-c[:,1])*(a[:,0]-c[:,0])+(c[:,0]-b[:,0])*(a[:,1]-c[:,1])
        if np.any(np.abs(determinants)<=1e-14):raise DegenerateUvError('The target UV triangle has zero area.')
        self._write('target_uv',target_uv);self._write('source_uv',source_uv)
        self._write('source_frames',np.ascontiguousarray(source.frames[source_corners],dtype=np.float32))
        self._write('target_frames',np.ascontiguousarray(target.frames[corners],dtype=np.float32))
        tiles={}
        for index,triangle in enumerate(target_uv):
            if index%1024==0:self.check(cancelled)
            for u,v,udim in triangle_tiles(triangle):tiles.setdefault(udim,[]).append(index)
        self.layouts={}
        for udim,triangles in sorted(tiles.items()):
            self.check(cancelled);u,v=udim_to_tile(udim);ids=np.asarray(triangles,dtype=np.int32)
            local=target_uv[ids]-(u,v);lo=local.min(axis=1);hi=local.max(axis=1)
            width,height=settings.width,settings.height
            x0=np.clip(np.floor(lo[:,0]*width).astype(np.int64)-1,0,width)
            x1=np.clip(np.ceil(hi[:,0]*width).astype(np.int64)+1,0,width)
            y0=np.clip(np.floor((1-hi[:,1])*height).astype(np.int64)-1,0,height)
            y1=np.clip(np.ceil((1-lo[:,1])*height).astype(np.int64)+1,0,height)
            block=16
            while True:
                bx=x0//block;by=y0//block
                nx=(x1+block-1)//block-bx;ny=(y1+block-1)//block-by
                counts=nx*ny;total=int(counts.sum())
                if total<=4_000_000:break
                block*=2
                if block>max(width,height)*2:raise GPUUnavailable('Too many overlapping GPU transfer candidates.')
            owners=np.repeat(np.arange(len(ids)),counts)
            linear=np.arange(total)-np.repeat(np.cumsum(counts)-counts,counts)
            bins_x=(width+block-1)//block;bins_y=(height+block-1)//block
            keys=(by[owners]+linear//nx[owners])*bins_x+bx[owners]+linear%nx[owners]
            order=np.argsort(keys,kind='stable')
            indices=np.ascontiguousarray(ids[owners[order]],dtype=np.int32)
            offsets=np.r_[0,np.cumsum(np.bincount(keys,minlength=bins_x*bins_y))].astype(np.int32)
            self._write('offsets_'+str(udim),offsets);self._write('indices_'+str(udim),indices)
            self.layouts[udim]=(block,bins_x)
        self.settings=settings
        self.bind_seconds=time.perf_counter()-started

    def rasterize(self,images,settings,normal,progress=None,cancelled=None,*,allow_conflicts=False):
        self.check(cancelled)
        if settings.supersampling not in (1,2,4):raise InputError('Supersampling: 1, 2 or 4.')
        values=[];metadata=[];offset=0
        for udim,tile in sorted(images.tiles.items()):
            pixels=np.asarray(tile.pixels,dtype=np.float32)
            if not np.all(np.isfinite(pixels)):raise InputError('The texture contains NaN or infinite values.')
            height,width,channels=pixels.shape
            if not 2<=channels<=4:raise InputError('Expected a texture with 2–4 channels.')
            if channels<4:
                rgba=np.ones((height,width,4),np.float32);rgba[...,:channels]=pixels;pixels=rgba
            values.append(pixels.reshape(-1,4));metadata.append((udim,width,height,offset,channels));offset+=width*height
        if images.repeat and 1001 not in images.tiles:images.require(1001)
        image=values[0] if len(values)==1 else np.concatenate(values)
        self._write('image',image);self._write('image_meta',np.asarray(metadata,dtype=np.int32))
        width,height=settings.width,settings.height;size=width*height
        out=self._buffer('output',size*16);owner=self._buffer('owner',size*4);coverage=self._buffer('coverage',size)
        self._buffer('conflicts',size)
        status=np.array([0,0,0xffffffff],np.uint32)
        ptr,integer,real=ct.c_void_p,ct.c_int,ct.c_double
        get=lambda name:ptr(self.buffers[name][0])
        results={};rows=max(1,min(64,262144//width));total=len(self.layouts)*height
        for tile_index,(udim,(block,bins_x)) in enumerate(self.layouts.items()):
            status[:]=(0,0,0xffffffff);self._write('status',status)
            out=self.buffers['output'][0];owner=self.buffers['owner'][0]
            tile_u,tile_v=udim_to_tile(udim)
            for first in range(0,height,rows):
                self.check(cancelled);count=min(rows,height-first)
                args=[get('target_uv'),get('source_uv'),get('source_frames'),get('target_frames'),
                      get('offsets_'+str(udim)),get('indices_'+str(udim)),integer(block),integer(bins_x),
                      integer(width),integer(height),integer(tile_u),integer(tile_v),integer(first),integer(settings.supersampling),
                      get('image'),get('image_meta'),integer(len(metadata)),integer(images.repeat),integer(normal),
                      integer(settings.input_green_sign),integer(settings.input_bc5),real(settings.overlap_tolerance_degrees),integer(allow_conflicts),
                      ptr(out),ptr(owner),ptr(coverage),get('conflicts'),get('status')]
                self._launch('transfer_pixels',args,(width,count));self._read(self.buffers['status'][0],status)
                self.check(cancelled)
                if status[1]&1:raise InputError('A source UDIM tile is missing for transfer.')
                if status[1]&2:raise InputError('Source UVs are outside supported UDIM tiles.')
                if status[1]&4:raise TangentBasisError('The tangent basis contains a degenerate axis.')
                if status[0] and not allow_conflicts:
                    point=dict(udim=udim,x=int(status[2])%width,y=int(status[2])//width)
                    raise OverlapConflictError('Target UV overlaps created conflicting pixels: {}. UDIM {}, pixel ({}, {}).'.format(
                        status[0],point['udim'],point['x'],point['y']),context=point)
                if progress:progress('GPU: '+self.name,tile_index*height+first+count,total)
            next_out=self._buffer('padded_output',size*16) if settings.padding else None
            next_owner=self._buffer('padded_owner',size*4) if settings.padding else None
            found=np.zeros(1,np.uint32)
            for step in range(settings.padding):
                self.check(cancelled);found[0]=0;flag=self._write('found',found)
                self._launch('pad_pixels',[ptr(out),ptr(owner),ptr(next_out),ptr(next_owner),integer(width),integer(height),ptr(flag)],(size,))
                self._read(flag,found);out,next_out=next_out,out;owner,next_owner=next_owner,owner
                if progress:progress('GPU: padding {}/{}'.format(step+1,settings.padding),step+1,settings.padding)
                if not found[0]:break
            pixels=self._read(out,np.empty((height,width,4),np.float32))
            owners=self._read(owner,np.empty((height,width),np.int32))
            covered=self._read(coverage,np.empty((height,width),np.uint8)).astype(bool)
            conflicts=self._read(self.buffers['conflicts'][0],np.empty((height,width),np.uint8)).astype(bool)
            if normal:results[udim]=RasterTileResult(udim,pixels[...,:3].copy(),covered,owners,conflicts)
            else:results[udim]=ColorRasterTileResult(udim,pixels,covered,owners,conflicts)
        self.check(cancelled)
        return results
