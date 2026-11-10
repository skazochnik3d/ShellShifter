"""Resident double precision island solves. One workgroup owns one island."""
SOURCE = r'''
#pragma OPENCL EXTENSION cl_khr_fp64 : enable
#pragma OPENCL FP_CONTRACT OFF
#define CAP 256
#define WG 64
#define SYNC barrier(CLK_LOCAL_MEM_FENCE)

double2 sum2(double2 v, __local double2 *s) {
    int t=get_local_id(0); s[t]=v; SYNC;
    for(int d=WG/2;d;d/=2){if(t<d)s[t]+=s[t+d];SYNC;}
    return s[0];
}
double max1(double v, __local double2 *s) {
    int t=get_local_id(0);s[t]=(double2)(v,0);SYNC;
    for(int d=WG/2;d;d/=2){if(t<d)s[t].x=fmax(s[t].x,s[t+d].x);SYNC;}
    return s[0].x;
}
double area(double2 a,double2 b,double2 c){b-=a;c-=a;return b.x*c.y-b.y*c.x;}

__attribute__((reqd_work_group_size(WG,1,1)))
__kernel void solve_islands(
    __global const int4 *jobs, __global const int2 *tri_ranges,
    __global const double2 *initial, __global const double2 *anchors,
    __global const double *aw, __global const int *pinned,
    __global const int *offsets, __global const int *other,
    __global const double *rest, __global const double *weight,
    __global const int4 *triangles, __global const double2 *limits,
    __global const double *spans, __global double2 *output,
    __global int2 *stats, __global double2 *baseline) {
    int job=get_group_id(0),t=get_local_id(0);
    int4 meta=jobs[job];int base=meta.x,n=meta.y,iters=meta.z;
    int2 tr=tri_ranges[job];
    __local double2 p[CAP],proposal[CAP],r[CAP],direction[CAP],correction[CAP],product[CAP];
    __local double degree[CAP];__local double2 target[CAP];__local double2 scratch[WG];
    double span=spans[job];int stable=0,warm=0,passes=0;
    for(int i=t;i<n;i+=WG){
        p[i]=initial[base+i];target[i]=anchors[base+i];double d=aw[base+i];
        for(int k=offsets[base+i];k<offsets[base+i+1];++k)d+=weight[k];
        degree[i]=fmax(d,1e-15);
    }SYNC;
    for(int it=0;it<iters;++it){
        double movement=0;
        for(int i=t;i<n;i+=WG){
            double2 total=(double2)(0);double divisor=0;
            for(int k=offsets[base+i];k<offsets[base+i+1];++k){
                double2 delta=p[i]-p[other[k]];double d=length(delta);
                if(d>=1e-10){total+=p[other[k]]+delta*(rest[k]/d);divisor+=1;}
            }
            double2 q=p[i];
            if(!pinned[base+i] && divisor>0)
                q+=.5*((total+aw[base+i]*target[i])/(divisor+aw[base+i])-q);
            proposal[i]=q;movement=fmax(movement,fmax(fabs(q.x-p[i].x),fabs(q.y-p[i].y)));
        }SYNC;
        for(int i=t;i<n;i+=WG)p[i]=proposal[i];SYNC;
        double bad=0;
        for(int k=tr.x+t;k<tr.x+tr.y;k+=WG){int4 a=triangles[k];
            if(limits[k].x*area(p[a.x],p[a.y],p[a.z])<limits[k].y)bad=1;
        }
        bad=max1(bad,scratch);SYNC;
        if(bad>0 && t==0){
            // Match the CPU's ordered Gauss-Seidel inversion repair.
            for(int k=tr.x;k<tr.x+tr.y;++k){
                int4 a=triangles[k];double2 A=p[a.x],B=p[a.y],C=p[a.z];
                double signed_area=limits[k].x*area(A,B,C);
                if(signed_area>=limits[k].y)continue;
                double2 ga=(double2)(B.y-C.y,C.x-B.x),gb=(double2)(C.y-A.y,A.x-C.x),gc=(double2)(A.y-B.y,B.x-A.x);
                if(pinned[base+a.x])ga=(double2)(0);
                if(pinned[base+a.y])gb=(double2)(0);
                if(pinned[base+a.z])gc=(double2)(0);
                double denom=dot(ga,ga)+dot(gb,gb)+dot(gc,gc);
                if(denom<1e-16)continue;
                double amount=.35*fmin((limits[k].y-signed_area)/denom,.25)*limits[k].x;
                p[a.x]+=amount*ga;p[a.y]+=amount*gb;p[a.z]+=amount*gc;
            }
        }SYNC;
        warm=it+1;
        if(it>=19 && it%5==4){
            movement=max1(movement,scratch);SYNC;
            stable=bad==0 && movement<=span*1e-7?stable+1:0;
            if(stable>=3)break;
        }else if(bad>0)stable=0;
    }
    for(int cycle=0;cycle<=meta.w;++cycle){
    stable=0;double initial_energy=0;
    for(int pass=0;pass<40;++pass){
        double2 rz=(double2)(0);double before=0;
        for(int i=t;i<n;i+=WG){
            double2 rhs=aw[base+i]*(target[i]-p[i]);
            for(int k=offsets[base+i];k<offsets[base+i+1];++k){
                double2 delta=p[i]-p[other[k]];double d=length(delta);
                rhs+=weight[k]*(delta*(rest[k]/fmax(d,1e-15))-delta);
                double e=d-rest[k];before+=.5*weight[k]*e*e;
            }
            double2 diff=p[i]-target[i];before+=aw[base+i]*dot(diff,diff);
            if(pinned[base+i])rhs=(double2)(0);
            r[i]=rhs;direction[i]=rhs/degree[i];correction[i]=(double2)(0);
            rz+=rhs*direction[i];proposal[i]=p[i];
        }
        rz=sum2(rz,scratch);SYNC;
        before=sum2((double2)(before,0),scratch).x;SYNC;
        if(pass==0)initial_energy=before;
        double2 threshold=fmax(rz*1e-12,(double2)(1e-24));
        for(int cg=0;cg<min(300,n);++cg){
            int2 active=(int2)(rz.x>threshold.x,rz.y>threshold.y);
            if(!active.x && !active.y)break;
            double2 denom=(double2)(0);
            for(int i=t;i<n;i+=WG){
                double2 v=aw[base+i]*direction[i];
                for(int k=offsets[base+i];k<offsets[base+i+1];++k)
                    v+=weight[k]*(direction[i]-direction[other[k]]);
                if(pinned[base+i])v=(double2)(0);
                product[i]=v;denom+=direction[i]*v;
            }
            denom=sum2(denom,scratch);SYNC;
            double2 step=(double2)(active.x && denom.x>0?rz.x/denom.x:0,
                                   active.y && denom.y>0?rz.y/denom.y:0);
            double2 next=(double2)(0);
            for(int i=t;i<n;i+=WG){
                correction[i]+=step*direction[i];r[i]-=step*product[i];next+=r[i]*r[i]/degree[i];
            }
            next=sum2(next,scratch);SYNC;
            double2 beta=(double2)(active.x && denom.x>0?next.x/fmax(rz.x,1e-300):0,
                                   active.y && denom.y>0?next.y/fmax(rz.y,1e-300):0);
            for(int i=t;i<n;i+=WG)
                direction[i]=(double2)(active.x && denom.x>0?r[i].x/degree[i]+beta.x*direction[i].x:0,
                                      active.y && denom.y>0?r[i].y/degree[i]+beta.y*direction[i].y:0);
            SYNC;
            rz=(double2)(active.x && denom.x>0?next.x:0,active.y && denom.y>0?next.y:0);
        }
        for(int i=t;i<n;i+=WG)proposal[i]=p[i]+correction[i];SYNC;
        int accepted=0;double value=before,amount=0;
        for(int backtrack=0;backtrack<20;++backtrack){
            amount=ldexp(1.,-backtrack);double e=0,bad=0;
            for(int i=t;i<n;i+=WG){
                double2 q=p[i]+amount*(proposal[i]-p[i]);
                if(!isfinite(q.x)||!isfinite(q.y))bad=1;
                double2 d=q-target[i];e+=aw[base+i]*dot(d,d);
                for(int k=offsets[base+i];k<offsets[base+i+1];++k){
                    int j=other[k];double2 v=p[j]+amount*(proposal[j]-p[j]);
                    double residual=length(q-v)-rest[k];e+=.5*weight[k]*residual*residual;
                }
            }
            for(int k=tr.x+t;k<tr.x+tr.y;k+=WG){
                int4 a=triangles[k];double sign=limits[k].x;
                double floor=fmin(sign*area(p[a.x],p[a.y],p[a.z]),limits[k].y);
                double2 A=p[a.x]+amount*(proposal[a.x]-p[a.x]);
                double2 B=p[a.y]+amount*(proposal[a.y]-p[a.y]);
                double2 C=p[a.z]+amount*(proposal[a.z]-p[a.z]);
                if(sign*area(A,B,C)<floor-1e-14)bad=1;
            }
            value=sum2((double2)(e,0),scratch).x;SYNC;
            bad=max1(bad,scratch);SYNC;
            if(bad==0 && isfinite(value) && value<=before){accepted=1;break;}
        }
        ++passes;if(!accepted)break;
        double movement=0;
        for(int i=t;i<n;i+=WG){double2 d=amount*(proposal[i]-p[i]);p[i]+=d;
            movement=fmax(movement,fmax(fabs(d.x),fabs(d.y)));}
        movement=max1(movement,scratch);SYNC;
        if(pass>=3){
            stable=movement<=span*2e-7 && before-value<=fmax(initial_energy*1e-8,span*span*1e-14)?stable+1:0;
            if(stable>=3)break;
        }
    }
    for(int i=t;i<n;i+=WG){if(cycle==0)baseline[base+i]=p[i];target[i]=p[i];}SYNC;
    }
    for(int i=t;i<n;i+=WG)output[base+i]=p[i];
    if(t==0)stats[job]=(int2)(warm,passes);
}
'''
