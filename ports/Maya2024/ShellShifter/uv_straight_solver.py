"""Vectorized metric warmup and per-mesh transactions for Unfold/Relax."""
from collections import defaultdict
from copy import copy
import os
import time
from uv_straight_deps import np
last_run={}


def warmup(positions, neighbors, locked, stiff_edges, anchors, anchor_weight,
           orientation, iterations, adaptive=False, diagnostics=None):
    """The original Jacobi update, with the same sequential inversion repair."""
    if not iterations:return positions
    ids=sorted(positions);index={uv:n for n,uv in enumerate(ids)};count=len(ids)
    links=[(index[i],index[j],length,100. if tuple(sorted((i,j))) in stiff_edges else 1.)
           for i in ids for j,length in neighbors[i].items()]
    if not links:return positions
    a=np.array([e[0] for e in links]);b=np.array([e[1] for e in links])
    rest=np.array([e[2] for e in links]);weights=np.array([e[3] for e in links])
    p=np.array([positions[i] for i in ids],dtype=float)
    target=np.array([anchors[i] for i in ids]);free=np.array([[d not in locked[i] for d in (0,1)] for i in ids])
    aw=np.array([anchor_weight*(3. if locked[i] else 1.) for i in ids])
    triangles=np.array([[index[i] for i in tri] for tri in orientation],dtype=int).reshape(-1,3)
    signs=np.array([v[0] for v in orientation.values()]);minimum=np.array([v[1] for v in orientation.values()])
    span=max(float(np.ptp(p,axis=0).max()),1e-12);stable=0;completed=0
    for iteration in range(iterations):
        check=adaptive and iteration>=19 and iteration%5==4
        previous=p.copy() if check else None
        delta=p[a]-p[b];distance=np.hypot(delta[:,0],delta[:,1]);valid=distance>=1e-10
        w=weights*valid;total=np.bincount(a,weights=w,minlength=count)
        wanted=p[b]+delta*(rest/np.maximum(distance,1e-30))[:,None]
        sums=np.column_stack([np.bincount(a,weights=w*wanted[:,d],minlength=count) for d in (0,1)])
        proposed=(sums+aw[:,None]*target)/np.maximum(total+aw,1e-30)[:,None]
        p+=np.where(free&(total>0)[:,None],.5*(proposed-p),0.)
        ab=p[triangles[:,1]]-p[triangles[:,0]];ac=p[triangles[:,2]]-p[triangles[:,0]]
        completed=iteration+1
        if np.all(signs*(ab[:,0]*ac[:,1]-ab[:,1]*ac[:,0])>=minimum):
            if check:
                stable=stable+1 if float(np.max(np.abs(p-previous)))<=span*1e-7 else 0
                if stable>=3:break
            continue
        stable=0
        # Repairs may affect the following triangle. Preserve the original
        # Gauss-Seidel order rather than correcting all bad faces in parallel.
        for tri,sign,floor in zip(triangles,signs,minimum):
            pa,pb,pc=p[tri];area=sign*((pb[0]-pa[0])*(pc[1]-pa[1])-(pb[1]-pa[1])*(pc[0]-pa[0]))
            if area>=floor:continue
            gradient=np.array([[pb[1]-pc[1],pc[0]-pb[0]],
                               [pc[1]-pa[1],pa[0]-pc[0]],
                               [pa[1]-pb[1],pb[0]-pa[0]]])
            gradient*=free[tri];denom=float(np.sum(gradient**2))
            if denom<1e-16:continue
            amount=min((floor-area)/denom,.25)
            for n,i in enumerate(tri):p[i]+=.35*amount*sign*gradient[n]
    if diagnostics is not None:diagnostics['warmup_iterations']=completed
    return {i:list(p[index[i]]) for i in ids}


def direct_system(labels,mask,degree,a,b,weights):
    """Reuse a small SPD inverse across local/global iterations; large maps use CG."""
    count=len(mask);free=np.flatnonzero(mask)
    if count>96 or not len(free):return None
    matrix=np.diag(degree)
    cross=labels[a]!=labels[b]
    np.add.at(matrix,(labels[a[cross]],labels[b[cross]]),-weights[cross])
    np.add.at(matrix,(labels[b[cross]],labels[a[cross]]),-weights[cross])
    try:
        inverse=np.linalg.inv(matrix[np.ix_(free,free)])
        if not np.isfinite(inverse).all():return None
        return free,inverse
    except np.linalg.LinAlgError:return None


def metric_lengths(mesh,shell):
    points=getattr(mesh,'solver_geometry',None)
    if points is None:points=mesh.fn.getPoints()
    lengths={}
    for edge,pairs in mesh.edge_uvs.items():
        a,b=mesh.fn.getEdgeVertices(edge);length=points[a].distanceTo(points[b])
        if length>1e-10:
            for pair in pairs:
                if set(pair)<=shell:lengths[tuple(sorted(pair))]=length
    for face,vertices in zip(mesh.faces,mesh.face_vertices):
        if len(face)!=4 or not set(face)<=shell:continue
        for a,b in ((0,2),(1,3)):
            length=points[vertices[a]].distanceTo(points[vertices[b]])
            if length>1e-10:lengths[tuple(sorted((face[a],face[b])))]=length
    return lengths


def needs_native_bridge(view,job,positions,strict=False):
    """Only skip Maya's extra relaxation for demonstrably near-isometric disks."""
    _,shell,groups,pins,_,_=job[:6]
    if set(pins)==shell:return False
    if groups or not view.faces or any(len(face) not in (3,4) for face in view.faces):return True
    basis=np.array(tuple(view.path.inclusiveMatrix()),dtype=float).reshape(4,4)[:3,:3]
    gram=basis@basis.T;scale=float(np.trace(gram)/3)
    # The custom metric is object-space. A nonuniform parent scale or shear
    # must not qualify as a proven isometry for Maya's native solver.
    if scale<=1e-30 or not np.allclose(gram,np.eye(3)*scale,rtol=1e-6,atol=scale*1e-8):return True
    edges=defaultdict(int);triangles=[];vertices=[]
    for face,points in zip(view.faces,view.face_vertices):
        for a,b in zip(face,face[1:]+face[:1]):edges[tuple(sorted((a,b)))]+=1
        for j in range(1,len(face)-1):
            triangles.append((face[0],face[j],face[j+1]));vertices.append((points[0],points[j],points[j+1]))
    degree=defaultdict(int)
    for (a,b),count in edges.items():
        if count==1:degree[a]+=1;degree[b]+=1
        elif count!=2:return True
    if len(shell)-len(edges)+len(view.faces)!=1 or not degree or any(n!=2 for n in degree.values()):return True
    if not simple_boundary([pair for pair,n in edges.items() if n==1],positions):return True
    # Use the selected shell's cached geometry, avoiding a second whole-mesh
    # triangulation/read when a tiny island belongs to a large production mesh.
    xyz=np.array([[(view.solver_geometry[i].x,view.solver_geometry[i].y,view.solver_geometry[i].z) for i in tri] for tri in vertices])
    ab=xyz[:,1]-xyz[:,0];ac=xyz[:,2]-xyz[:,0];length=np.linalg.norm(ab,axis=1)
    height=np.linalg.norm(np.cross(ab,ac),axis=1)/np.maximum(length,1e-30)
    if np.any(length<=1e-12) or np.any(height<=length*1e-10):return True
    points=np.array([[positions[i] for i in tri] for tri in triangles])
    u=points[:,1]-points[:,0];v=points[:,2]-points[:,0]
    area=u[:,0]*v[:,1]-u[:,1]*v[:,0]
    span=max(float(np.ptp(points.reshape(-1,2),axis=0).max()),1e-12)
    if not (np.all(area>span*span*1e-12) or np.all(area<-span*span*1e-12)):return True
    p=u/length[:,None];tx=np.sum(ab*ac,axis=1)/length
    q=(v-p*tx[:,None])/height[:,None]
    singular=np.linalg.svd(np.stack((p,q),axis=2),compute_uv=False)
    weight=length*height;weight/=weight.sum()
    ratio=singular[:,0]/np.maximum(singular[:,1],1e-30)
    energy=2*np.sqrt(np.dot(weight,np.sum(singular**2,axis=1))*np.dot(weight,np.sum(1/np.maximum(singular**2,1e-30),axis=1)))
    mean_limit,worst_limit,energy_limit=(1.00005,1.0005,4.0000001) if strict else (1.01,1.05,4.005)
    return not (np.isfinite(energy) and np.dot(weight,ratio)<=mean_limit and float(ratio.max())<=worst_limit and energy<=energy_limit)


def simple_boundary(pairs,positions):
    """Reject crossing/touching non-adjacent border edges before skipping native."""
    if len(pairs)>1024:return False  # Keep the quality gate's cost bounded.
    ids=np.array(pairs,dtype=int);points=np.array([[positions[a],positions[b]] for a,b in pairs])
    span=max(float(np.ptp(points.reshape(-1,2),axis=0).max()),1e-12)
    points=(points-points[0,0])/span;lo=points.min(axis=1);hi=points.max(axis=1)
    first,second=np.triu_indices(len(points),1)
    overlap=np.all(hi[second]>=lo[first]-1e-10,axis=1)&np.all(lo[second]<=hi[first]+1e-10,axis=1)
    independent=np.all(ids[second]!=ids[first,0,None],axis=1)&np.all(ids[second]!=ids[first,1,None],axis=1)
    first,second=first[overlap&independent],second[overlap&independent]
    a,b=points[first,0],points[first,1]
    c,d=points[second,0],points[second,1]
    ab,cd=b-a,d-c
    cross=lambda u,v:u[...,0]*v[...,1]-u[...,1]*v[...,0]
    x,y=cross(ab,c-a),cross(ab,d-a)
    u,v=cross(cd,a-c),cross(cd,b-c)
    return not np.any((x*y<=1e-20)&(u*v<=1e-20))


def preserve_orientation(positions,reference,faces,diagnostics=None):
    """Backtrack a free solve if a quad becomes concave on its other diagonal."""
    if any(len(face) not in (3,4) for face in faces):return positions
    ids=sorted(positions);index={i:n for n,i in enumerate(ids)};triangles=[]
    for face in faces:
        triangles.extend((face[0],face[j],face[j+1]) for j in range(1,len(face)-1))
        if len(face)==4:triangles.extend(((face[0],face[1],face[3]),(face[1],face[2],face[3])))
    if not triangles:return positions
    tri=np.array([[index[i] for i in t] for t in triangles]);base=np.array([reference[i] for i in ids])
    result=np.array([positions[i] for i in ids]);span=max(float(np.ptp(base,axis=0).max()),1e-12)
    def area(p):
        ab=p[tri[:,1]]-p[tri[:,0]];ac=p[tri[:,2]]-p[tri[:,0]]
        return ab[:,0]*ac[:,1]-ab[:,1]*ac[:,0]
    before=area(base);sign=1. if float(np.sum(before))>=0 else -1.
    floor=np.minimum(before*sign,span*span*1e-12)
    if np.all(area(result)*sign>=floor-span*span*1e-14):return positions
    for n in range(1,25):
        candidate=base+(result-base)*(.5**n)
        if np.all(area(candidate)*sign>=floor-span*span*1e-14):
            if diagnostics is not None:diagnostics['orientation_backtracks']=n
            return {i:tuple(candidate[index[i]]) for i in ids}
    if diagnostics is not None:diagnostics['orientation_backtracks']=25
    return {i:tuple(base[index[i]]) for i in ids}


def safe_boundary_step(positions, reference, faces):
    """Limit motion that introduces a crossing on a small, simple UV shell."""
    if not faces or any(len(face) not in (3, 4) for face in faces):
        return positions
    counts = defaultdict(int)
    for face in faces:
        for a, b in zip(face, face[1:] + face[:1]):
            counts[tuple(sorted((a, b)))] += 1
    border = [edge for edge, count in counts.items() if count == 1]
    if not 4 <= len(border) <= 64 or not simple_boundary(border, reference):
        return positions
    # Existing folds may need to move through an invalid state to unfold.
    triangles = np.asarray([[reference[i] for i in (face[0], face[j], face[j + 1])]
                            for face in faces for j in range(1, len(face) - 1)])
    a, b = triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    signed = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
    if not (np.all(signed > 0) or np.all(signed < 0)):
        return positions

    accepted = reference
    for step in range(1, 5):
        factor = step / 4.
        sample = positions if step == 4 else {
            i: tuple(a + factor * (b - a) for a, b in zip(reference[i], point))
            for i, point in positions.items()
        }
        if (preserve_orientation(sample, reference, faces) is not sample or
                not simple_boundary(border, sample)):
            break
        accepted = sample
    return accepted

def optimize_surface(view, positions, pins):
    """Local/global similarity-rotation refinement on small free UV charts."""
    ids = sorted(positions)
    if not 4 <= len(ids) <= 96 or any(len(face) not in (3, 4) for face in view.faces):
        return positions
    index = {uv: i for i, uv in enumerate(ids)}
    points = np.asarray([positions[uv] for uv in ids], dtype=float)
    triangles, bases, weights = [], [], []
    for face, vertices in zip(view.faces, view.face_vertices):
        for j in range(1, len(face) - 1):
            corners = (0, j, j + 1)
            a, b, c = (view.solver_geometry[vertices[k]] for k in corners)
            ab, ac = b - a, c - a
            length = ab.length()
            height = (ab ^ ac).length() / length if length > 1e-12 else 0.
            if height <= max(1e-12, length * 1e-10):
                return positions
            triangles.append([index[face[k]] for k in corners])
            bases.append(((length, (ab * ac) / length), (0., height)))
            weights.append(length * height)
    if not triangles:
        return positions
    tri = np.asarray(triangles)
    rest_area = sum(weights)
    weights = np.asarray(weights) / rest_area

    def edges(coords):
        return (coords[tri[:, 1:]] - coords[tri[:, :1]]).transpose(0, 2, 1)

    original_area = np.linalg.det(edges(points))
    winding = 1. if original_area.sum() >= 0. else -1.
    area = original_area.sum() * winding
    if area <= 0. or np.any(original_area * winding <= area * 1e-10):
        return positions
    basis = np.asarray(bases) * np.sqrt(area / rest_area)
    basis[:, 1, 1] *= winding
    inverse = np.linalg.inv(basis)
    def evaluate(coords):
        jac = edges(coords) @ inverse
        determinant = np.linalg.det(jac)
        if not np.all(determinant > 1e-8):
            return float('inf'), jac
        norm = np.sum(jac * jac, axis=(1, 2))
        return float(weights @ (norm * (1. + determinant ** -2))), jac

    initial_energy, jac = evaluate(points)
    if not np.isfinite(initial_energy) or initial_energy <= 4.000001:
        return positions
    # The weighted gradient normal matrix has the cotangent Laplacian form.
    operator = np.zeros((len(tri), 2, len(ids)))
    rows = np.arange(len(tri))
    operator[rows, :, tri[:, 0]] = -inverse.sum(axis=1)
    operator[rows, :, tri[:, 1]] = inverse[:, 0, :]
    operator[rows, :, tri[:, 2]] = inverse[:, 1, :]
    operator = operator.reshape(-1, len(ids))
    weight = np.repeat(np.sqrt(weights), 2)
    operator *= weight[:, None]
    fixed = sorted(index[uv] for uv in pins) or [0]
    free = [i for i in range(len(ids)) if i not in fixed]
    if not free:
        return positions
    free_operator = operator[:, free]
    fixed_rhs = operator[:, fixed] @ points[fixed]
    try:
        projection = np.linalg.solve(free_operator.T @ free_operator, free_operator.T)
    except np.linalg.LinAlgError:
        return positions

    energy = initial_energy
    current = points.copy()
    for _ in range(8):
        t = (jac[:, 0, 0] + jac[:, 1, 1]) * .5
        r = (jac[:, 1, 0] - jac[:, 0, 1]) * .5
        length = np.hypot(t, r)
        blend = .5 + .5 / np.maximum(length, 1e-12)
        local = np.stack((t, r, -r, t), axis=1).reshape(-1, 2) * np.repeat(blend, 2)[:, None]
        target = current.copy()
        target[free] = projection @ (local * weight[:, None] - fixed_rhs)
        for fraction in (1., .5, .25, .125):
            candidate = current + fraction * (target - current)
            if not pins:
                candidate_area = np.linalg.det(edges(candidate)).sum() * winding
                if candidate_area <= 0.:
                    continue
                candidate -= candidate.mean(axis=0)
                candidate *= np.sqrt(area / candidate_area)
                candidate += points.mean(axis=0)
            value, next_jac = evaluate(candidate)
            if value < energy - 1e-8:
                current, energy, jac = candidate, value, next_jac
                break
        else:
            break
    if energy >= initial_energy:
        return positions
    result = {uv: tuple(current[i]) for i, uv in enumerate(ids)}
    # Keep the original candidate if geometric feasibility limits the polish.
    return result if safe_boundary_step(result, positions, view.faces) is result else positions

def shell_views(mesh,jobs):
    """Index topology once, not by scanning the whole mesh for every island."""
    wanted={mesh.shell_ids[min(job[1])] for job in jobs}
    faces=defaultdict(list);edges=defaultdict(lambda:defaultdict(set))
    for face,vertices in zip(mesh.faces,mesh.face_vertices):
        owner=mesh.shell_ids[face[0]]
        if owner in wanted:faces[owner].append((face,vertices))
    for edge,pairs in mesh.edge_uvs.items():
        for pair in pairs:
            owner=mesh.shell_ids[pair[0]]
            if owner in wanted:edges[owner][edge].add(pair)
    for job in jobs:
        shell=job[1];owner=mesh.shell_ids[min(shell)];view=copy(mesh)
        view.points={i:mesh.points[i] for i in shell}
        view.faces=[pair[0] for pair in faces[owner]]
        view.face_vertices=[pair[1] for pair in faces[owner]]
        view.edge_uvs=edges[owner]
        yield job,view


def recover_folded_shells(mesh, jobs):
    """Rebuild free UV disks whose starting coordinates are folded or collapsed."""
    free = {mesh.shell_ids[min(job[1])]: job for job in jobs if not job[2] and not job[3]}
    if not free:
        return {}
    limits = {owner: max(float(np.ptp(list(job[5].values()), axis=0).max()), 1e-12)**2 * 1e-12
              for owner, job in free.items()}
    winding = {}
    folded = set()
    for face in mesh.faces:
        owner = mesh.shell_ids[face[0]]
        if owner not in free or owner in folded:
            continue
        u, v = mesh.points[face[0]]
        for b, c in zip(face[1:-1], face[2:]):
            x, y = mesh.points[b]
            s, t = mesh.points[c]
            area = (x-u)*(t-v) - (y-v)*(s-u)
            sign = 1 if area > 0 else -1
            if abs(area) <= limits[owner] or winding.setdefault(owner, sign) != sign:
                folded.add(owner)
                break
    if not folded:
        return {}

    from uv_straight_unwrap import Surface
    surface = Surface(mesh, shell_ids=folded)
    recovered = {}
    for owner in sorted(folded & surface.disks):
        shell, original = free[owner][1], free[owner][5]
        if not surface.quality(shell, original)[0]:
            continue
        seed = surface.seed(shell)
        if seed is None or surface.quality(shell, seed)[0]:
            continue
        ids = sorted(shell)
        old = np.asarray([original[i] for i in ids])
        uv = np.asarray([seed[i] for i in ids])
        triangles = surface.triangles[surface.by_shell[owner]]
        local = {i: n for n, i in enumerate(ids)}
        tri = np.array([[local[int(i)] for i in corners] for corners in triangles])
        def areas(points):
            a = points[tri[:, 1]] - points[tri[:, 0]]
            b = points[tri[:, 2]] - points[tri[:, 0]]
            return a[:, 0]*b[:, 1] - a[:, 1]*b[:, 0]
        before, seeded = areas(old), areas(uv)
        signs = before[np.abs(before) > limits[owner]]
        mirrored = np.count_nonzero(signs < 0) > np.count_nonzero(signs > 0)
        if mirrored != (seeded.sum() < 0):
            uv[:, 1] *= -1
        center = old.mean(axis=0)
        uv -= uv.mean(axis=0)
        left, _, right = np.linalg.svd(uv.T @ (old-center))
        left[:, -1] *= np.linalg.det(left @ right)
        old_area = np.abs(before).sum()
        scale = np.sqrt(old_area / np.abs(seeded).sum()) if old_area > limits[owner] else 0.
        if scale <= 0.:
            # A completely flattened line has a useful span but no UV area.
            span = float(np.ptp(old, axis=0).max())
            scale = span if span > 1e-12 else 1.
        aligned = dict(zip(ids, map(tuple, (uv @ (left @ right))*scale + center)))

        edges = defaultdict(int)
        for a, b, c in triangles:
            for edge in ((a,b), (b,c), (c,a)):
                edges[tuple(sorted(edge))] += 1
        border = [edge for edge, count in edges.items() if count == 1]
        if not simple_boundary(border, aligned):
            continue
        refined = surface.refine(shell, aligned)
        candidates = [aligned]
        if refined is not None:
            points = np.asarray([refined[i] for i in ids])
            target_area = np.abs(seeded).sum() * scale**2
            factor = np.sqrt(target_area / np.abs(areas(points)).sum())
            candidates.insert(0, dict(zip(ids, map(tuple, (points-points.mean(axis=0))*factor + center))))
        # Certify the coordinates that Maya will actually store.
        for candidate in candidates:
            stored = {i: tuple(map(float, np.asarray(p, dtype=np.float32))) for i, p in candidate.items()}
            if not surface.quality(shell, stored)[0] and simple_boundary(border, stored):
                recovered[ids[0]] = stored
                break
    return recovered


def write_batch(plans):
    """One native Undo command, including history-backed meshes and UV pins."""
    import maya.api.OpenMaya as om
    from uv_straight_pack_commit import commit
    entries=[]
    for mesh,positions in plans:
        before=[list(values) for values in mesh.fn.getUVs(mesh.uv_set)]
        ids=[i for i,p in positions.items() if (before[0][i],before[1][i])!=tuple(p)]
        if not ids:continue
        after=[values[:] for values in before]
        for i in ids:after[0][i],after[1][i]=positions[i]
        entries.append(dict(handle=om.MObjectHandle(mesh.fn.object()),uv_set=mesh.uv_set,
                            before=before,after=after,ids=ids,shells=1))
    if entries:commit(entries)


def solve_tasks(tasks,iterations,anchor_weight,relax_bridge,snapshots=None,axis=None):
    import uv_straight as uv
    global last_run
    last_run=dict(stages=[],native_called=0,native_skipped=0,native_batches=0,gpu_shells=0,gpu_seconds=0.)
    if axis is not None:last_run['axis']=axis
    tasks=list(tasks)
    initial_meshes={}
    for job in tasks:
        name=job[0].fullPathName()
        if name not in initial_meshes:
            initial_meshes[name]=snapshots[name] if snapshots and name in snapshots else uv.Mesh(job[0])
    from uv_straight_solver_stacks import prepare
    stacks=prepare(tasks,initial_meshes,shell_views,uv._constraint_positions)
    last_run.update(input_shells=len(tasks),solved_shells=len(stacks.jobs),
                    stack_groups=stacks.groups,stacked_shells=len(stacks.followers),
                    stack_constraint_conflicts=stacks.conflicts)
    tasks=stacks.jobs
    geometry={};metrics={};bridge_errors={};bridge_views={}
    def configure(view,job,reference):
        view.adaptive_solver=True;view.solve_stats={}
        view.relax_axis=axis
        name=view.path.fullPathName();key=(name,min(job[1]))
        if name not in geometry:geometry[name]=view.fn.getPoints()
        view.solver_geometry=geometry[name]
        if key in metrics:view.metric_lengths=metrics[key]
        view.bridge_check=relax_bridge and reference is None
        return key
    def remember(view,job,key,result,final):
        if len(job)>6:
            # Exact output mask in addition to the solver's fixed variables.
            result=dict(result)
            result.update({i:job[4][i] for i in job[6]})
        if axis is not None:
            frozen=1-uv._axis(axis)
            result={i:tuple(job[5][i][d] if d==frozen else p[d] for d in (0,1)) for i,p in result.items()}
        if final and axis is None and not job[2]:
            optimized = optimize_surface(view, result, job[3])
            if optimized is not result:
                view.solve_stats['surface_optimized'] = True
                result = optimized
        if final and not job[2] and result != job[5]:
            guarded = safe_boundary_step(result, job[5], view.faces)
            if guarded is not result:
                view.solve_stats['boundary_step_limited'] = True
                result = guarded
        metrics[key]=view.metric_lengths
        if view.bridge_check:
            bridge_errors[key]=view.solve_stats.get('edge_rms',float('inf'));bridge_views[key]=view
        last_run['stages'].append(dict(view.solve_stats,uvs=len(job[1])))
        return result
    def calculate(view,job,count,reference=None):
        key=configure(view,job,reference)
        result=uv._relaxed_positions(view,job[1],job[2],job[3],count,anchor_weight,reference)
        view.solve_stats['backend']='CPU'
        return remember(view,job,key,result,reference is not None)
    def calculate_batch(mesh_jobs,count,final=False):
        views=[(mesh,job,view) for mesh,jobs in mesh_jobs for job,view in shell_views(mesh,jobs)]
        positions=defaultdict(dict)
        mode=os.environ.get('SHELLSHIFTER_SOLVER_BACKEND','auto').lower()
        eligible=[(job,view) for _,job,view in views if axis is None and not job[2] and 3<=len(job[1])<=256]
        use_gpu=mode!='cpu' and (mode=='gpu' or (len(eligible)>=8 and sum(len(job[1]) for job,_ in eligible)>=256))
        completed=set()
        if use_gpu and eligible:
            # Build a complete numerical snapshot before touching device memory.
            from uv_straight_pack_gpu import GPUUnavailable
            prepared=[]
            for job,view in eligible:
                reference=job[5] if final else None;key=configure(view,job,reference)
                state=uv._relaxed_positions(view,job[1],job[2],job[3],count,anchor_weight,reference,prepare_only=True)
                if relax_bridge and not final:state['polish']=2
                prepared.append((job,view,key,state))
            try:
                from uv_straight_solver_gpu import backend
                device=backend();start=time.perf_counter()
                results=device.solve([entry[3] for entry in prepared])
                last_run['gpu_seconds']+=time.perf_counter()-start;last_run['gpu_device']=device.name
            except (GPUUnavailable,OSError,MemoryError) as error:
                last_run['gpu_fallback']=str(error)
            else:
                for (job,view,key,state),(result,stats) in zip(prepared,results):
                    baseline=stats.pop('_baseline',None)
                    view.solve_stats.update(stats)
                    result=uv._finish_relaxed_positions(view,job[1],job[2],job[3],result,state['scale'],state['reference'],True,view.solve_stats)
                    if baseline is not None:
                        try:accept=view.solve_stats.get('edge_rms',float('inf'))<=1e-5 and not needs_native_bridge(view,job,result,strict=True)
                        except (ValueError,RuntimeError,np.linalg.LinAlgError):accept=False
                        view.solve_stats['gpu_polish_accepted']=accept
                        if not accept:
                            result=uv._finish_relaxed_positions(view,job[1],job[2],job[3],baseline,state['scale'],state['reference'],True,view.solve_stats)
                    positions[view.name].update(remember(view,job,key,result,final));completed.add(key);last_run['gpu_shells']+=1
        for mesh,job,view in views:
            if (view.name,min(job[1])) not in completed:
                positions[mesh.name].update(calculate(view,job,count,job[5] if final else None))
        for mesh, _ in mesh_jobs:
            positions[mesh.name].update(recovered.get(mesh.name, {}))
        return [(mesh,positions[mesh.name]) for mesh,_ in mesh_jobs]
    grouped=defaultdict(list)
    for job in tasks:grouped[job[0].fullPathName()].append(job)
    recovered = defaultdict(dict)
    if axis is None:
        for name, jobs in grouped.items():
            repairs = recover_folded_shells(initial_meshes[name], jobs)
            if repairs:
                grouped[name] = [job for job in jobs if min(job[1]) not in repairs]
                for result in repairs.values():
                    recovered[name].update(result)
                    last_run['stages'].append(dict(backend='CPU', uvs=len(result), recovered_fold=True))
    if relax_bridge:
        first_iterations=min(100,max(20,int(iterations)//3))
        plans=calculate_batch([(initial_meshes[name],jobs) for name,jobs in grouped.items()],first_iterations)
        write_batch(stacks.expand(plans))
        bridge_iterations=max(5,min(20,int(iterations)//20))
        for mesh,positions in plans:
            native_shell=set()
            fixed_unselected={}
            for job in grouped[mesh.name]:
                needed=True
                try:
                    if set(job[3])==job[1]:needed=False
                    elif not job[2] and bridge_errors.get((mesh.name,min(job[1])),float('inf'))<=.015:
                        needed=needs_native_bridge(bridge_views[(mesh.name,min(job[1]))],job,positions)
                except (ValueError,RuntimeError,np.linalg.LinAlgError):needed=True
                if not needed:
                    last_run['native_skipped']+=1;continue
                last_run['native_called']+=1
                native_shell.update(job[1])
                if len(job)>6:fixed_unselected.update({i:job[4][i] for i in job[6]})
            if native_shell:
                last_run['native_batches']+=1
                try:
                    from uv_straight_unfold_bridge import relax as isolated_relax
                    details=(isolated_relax(mesh,native_shell,bridge_iterations,fixed=fixed_unselected)
                             if fixed_unselected else isolated_relax(mesh,native_shell,bridge_iterations))
                    last_run.setdefault('isolated_native',[]).append(details)
                except (ValueError,RuntimeError) as error:
                    last_run.setdefault('native_fallback',[]).append(str(error))
                    # A direct native fallback has no transient-anchor channel.
                    # Keep the constrained custom result for partial selections.
                    if not fixed_unselected:uv._maya_relax_bridge(mesh,native_shell,{},bridge_iterations)
    mesh_jobs=[]
    for name,jobs in grouped.items():
        if relax_bridge:
            # Unfold changes coordinates only: retain topology and shell IDs,
            # but always read fresh evaluated UV arrays after the native stage.
            mesh=copy(initial_meshes[name]);u,v=mesh.fn.getUVs(mesh.uv_set)
            mesh.points=dict(enumerate(zip(u,v)))
        else:mesh=initial_meshes[name]
        # Some Maya versions let untangleUV move native pins. Restore their
        # exact source coordinates in the numerical state before the last solve.
        for job in jobs:mesh.points.update(job[4])
        mesh_jobs.append((mesh,jobs))
    plans=calculate_batch(mesh_jobs,iterations,final=True)
    write_batch(stacks.expand(plans))
