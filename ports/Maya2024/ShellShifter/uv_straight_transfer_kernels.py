"""OpenCL 1.2 pixel-owned texture transfer; deterministic overlap handling."""

SOURCE=r'''
#pragma OPENCL EXTENSION cl_khr_fp64 : enable
#pragma OPENCL FP_CONTRACT OFF

float3 unit(double3 v) {
    double length=sqrt(dot(v,v));
    return length<=1e-12 ? (float3)(0,0,1) : convert_float3(v/length);
}

// Stored frames are row-major matrices with T/B/N in the columns.
void basis(__global const float *frames,int triangle,double3 w,
           double3 *t,double3 *b,double3 *n,int *bad) {
    double3 rt=0,rb=0,rn=0;double handed=0;
    for(int c=0;c<3;c++) {
        int p=triangle*27+c*9; double a=c==0?w.x:(c==1?w.y:w.z);
        double3 ct=convert_double3((float3)(frames[p],frames[p+3],frames[p+6]));
        double3 cb=convert_double3((float3)(frames[p+1],frames[p+4],frames[p+7]));
        double3 cn=convert_double3((float3)(frames[p+2],frames[p+5],frames[p+8]));
        rt+=a*ct;rb+=a*cb;rn+=a*cn;handed+=a*(dot(cross(cn,ct),cb)<0?-1.:1.);
    }
    double nl=sqrt(dot(rn,rn));
    if(!isfinite(nl)||nl<=1e-10) {*bad=1;*t=(double3)(1,0,0);*b=(double3)(0,1,0);*n=(double3)(0,0,1);return;}
    rn/=nl;rt-=rn*dot(rn,rt);double tl=sqrt(dot(rt,rt));
    if(!isfinite(tl)||tl<=1e-10) {*bad=1;*t=(double3)(1,0,0);*b=(double3)(0,1,0);*n=rn;return;}
    rt/=tl;double3 canonical=cross(rn,rt);canonical/=sqrt(dot(canonical,canonical));
    rb=canonical*(handed<0?-1.0:1.0);
    *t=convert_double3(convert_float3(rt));*b=convert_double3(convert_float3(rb));*n=convert_double3(convert_float3(rn));
}

float4 sample_map(__global const float4 *image,__global const int *meta,int tiles,
                  double2 uv,int repeat,int *channels,int *bad) {
    int tile=1001;
    if(repeat) uv-=floor(uv);
    else {
        double2 ij=floor(uv);
        if(ij.x<0||ij.x>=10||ij.y<0) {*bad|=2;return (float4)(0);}
        tile=1001+(int)ij.x+10*(int)ij.y;uv-=ij;
    }
    int k=0;while(k<tiles&&meta[k*5]!=tile)k++;
    if(k==tiles) {*bad|=1;return (float4)(0);}
    int width=meta[k*5+1],height=meta[k*5+2],base=meta[k*5+3];*channels=meta[k*5+4];
    double x=clamp(uv.x*width-.5,0.,(double)(width-1));
    double y=clamp((1.-uv.y)*height-.5,0.,(double)(height-1));
    int x0=(int)floor(x),y0=(int)floor(y),x1=min(x0+1,width-1),y1=min(y0+1,height-1);
    float fx=(float)(x-x0),fy=(float)(y-y0);
    float4 top=image[base+y0*width+x0]*(1.f-fx)+image[base+y0*width+x1]*fx;
    float4 bottom=image[base+y1*width+x0]*(1.f-fx)+image[base+y1*width+x1]*fx;
    return top*(1.f-fy)+bottom*fy;
}

__kernel void transfer_pixels(
    __global const double2 *target,__global const double2 *source,
    __global const float *source_frames,__global const float *target_frames,
    __global const int *offsets,__global const int *indices,int block,int bins_x,
    int width,int height,int tile_u,int tile_v,int row_start,int samples,
    __global const float4 *image,__global const int *meta,int tiles,int repeat,
    int normal,int green,int rg,double tolerance,int allow_conflicts,
    __global float4 *output,__global int *owners,__global uchar *coverage,
    __global uchar *conflicts,__global uint *status) {
    int x=get_global_id(0),y=get_global_id(1)+row_start;
    if(x>=width||y>=height)return;
    int pixel=y*width+x,bin=(y/block)*bins_x+x/block;
    int first=offsets[bin],last=offsets[bin+1],count=0,owner=-1,conflict=0,bad=0;
    float4 sum=0;
    for(int sy=0;sy<samples;sy++)for(int sx=0;sx<samples;sx++) {
        double2 p=(double2)((x+((double)sx+.5)/samples)/width,1.-(y+((double)sy+.5)/samples)/height);
        int sample_owner=-1;float4 value=0;
        for(int q=first;q<last;q++) {
            int tri=indices[q];double2 origin=(double2)(tile_u,tile_v);
            double2 a=target[tri*3]-origin,b=target[tri*3+1]-origin,c=target[tri*3+2]-origin;
            double denominator=(b.y-c.y)*(a.x-c.x)+(c.x-b.x)*(a.y-c.y);
            double wa=((b.y-c.y)*(p.x-c.x)+(c.x-b.x)*(p.y-c.y))/denominator;
            double wb=((c.y-a.y)*(p.x-c.x)+(a.x-c.x)*(p.y-c.y))/denominator;
            double wc=1.-wa-wb;
            if(wa< -1e-10||wb< -1e-10||wc< -1e-10||wa>1.+1e-10||wb>1.+1e-10||wc>1.+1e-10)continue;
            double3 weights=(double3)(wa,wb,wc);
            double2 uv=wa*source[tri*3]+wb*source[tri*3+1]+wc*source[tri*3+2];
            int channels=4;float4 sampled=sample_map(image,meta,tiles,uv,repeat,&channels,&bad);
            if(normal) {
                float3 raw=(float3)(sampled.x*2.f-1.f,(sampled.y*2.f-1.f)*(float)green,sampled.z*2.f-1.f);
                if(rg||channels<3)raw.z=sqrt(fmax(0.f,1.f-raw.x*raw.x-raw.y*raw.y));
                float3 n=unit(convert_double3(raw));double3 st,sb,sn,tt,tb,tn;int invalid=0;
                basis(source_frames,tri,weights,&st,&sb,&sn,&invalid);
                basis(target_frames,tri,weights,&tt,&tb,&tn,&invalid);
                if(invalid)bad|=4;
                double3 surface=st*(double)n.x+sb*(double)n.y+sn*(double)n.z;
                float3 mapped=unit((double3)(dot(tt,surface),dot(tb,surface),dot(tn,surface)));
                sampled=(float4)(unit(convert_double3(mapped)),1.f);
            }
            if(sample_owner<0) {sample_owner=tri;value=sampled;}
            else if(normal) {
                double angle=acos(clamp((double)dot(value.xyz,sampled.xyz),-1.,1.))*57.29577951308232;
                if(angle>tolerance)conflict=1;
                if(!allow_conflicts)value=(float4)(unit(convert_double3(value.xyz+sampled.xyz)),1.f);
            } else {
                float4 delta=fabs(value-sampled);
                if(fmax(fmax(delta.x,delta.y),fmax(delta.z,delta.w))>2.f/255.f)conflict=1;
                if(!allow_conflicts)value=(value+sampled)*.5f;
            }
        }
        if(sample_owner>=0) {if(owner<0)owner=sample_owner;sum+=value;count++;}
    }
    float4 value=count?sum/(float)count:(normal?(float4)(0,0,1,1):(float4)(0));
    if(normal&&count) value=(float4)(unit(convert_double3(unit(convert_double3(value.xyz)))),1.f);
    output[pixel]=value;owners[pixel]=owner;coverage[pixel]=count>0;
    conflicts[pixel]=(uchar)conflict;
    if(conflict) {atomic_inc(status);atomic_min(status+2,(uint)pixel);}
    if(bad)atomic_or(status+1,(uint)bad);
}

__kernel void pad_pixels(__global const float4 *values,__global const int *owners,
    __global float4 *output,__global int *out_owners,int width,int height,__global uint *found) {
    int index=get_global_id(0);if(index>=width*height)return;
    int owner=owners[index],chosen=index;
    if(owner<0) {
        int x=index%width,y=index/width;
        if(y>0&&owners[index-width]>=0)chosen=index-width;
        else if(y+1<height&&owners[index+width]>=0)chosen=index+width;
        else if(x>0&&owners[index-1]>=0)chosen=index-1;
        else if(x+1<width&&owners[index+1]>=0)chosen=index+1;
        owner=owners[chosen];if(owner>=0)atomic_or(found,1u);
    }
    output[index]=values[chosen];out_owners[index]=owner;
}
'''
