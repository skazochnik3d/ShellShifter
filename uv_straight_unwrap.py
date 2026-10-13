"""Unconstrained geometry seeds and quality checks for Unwrap Mesh.

Only UV coordinates change here. The caller owns seams, pin removal, Undo,
orientation and packing. Temporary gauge coordinates never become Maya pins.
"""
import heapq
import math
from collections import defaultdict

import numpy as np
import maya.api.OpenMaya as om
import maya.cmds as cmds


class Surface:
    def __init__(self, mesh):
        self.mesh = mesh
        world = np.array([(p.x, p.y, p.z) for p in mesh.fn.getPoints(om.MSpace.kWorld)])
        triangles, vertices = [], []
        iterator = om.MItMeshPolygon(mesh.path)
        while not iterator.isDone():
            face_vertices = list(iterator.getVertices())
            uv_ids = [iterator.getUVIndex(i, mesh.uv_set) for i in range(len(face_vertices))]
            mapping = dict(zip(face_vertices, uv_ids))
            _, indices = iterator.getTriangles(om.MSpace.kWorld)
            for j in range(0, len(indices), 3):
                corners = list(indices[j:j+3])
                triangles.append([mapping[v] for v in corners])
                vertices.append(corners)
            iterator.next()
        self.triangles = np.array(triangles, dtype=int).reshape(-1, 3)
        xyz = world[np.array(vertices, dtype=int).reshape(-1, 3)]
        ab, ac = xyz[:, 1]-xyz[:, 0], xyz[:, 2]-xyz[:, 0]
        length = np.linalg.norm(ab, axis=1)
        height = np.linalg.norm(np.cross(ab, ac), axis=1)/np.maximum(length, 1e-30)
        self.x = np.sum(ab*ac, axis=1)/np.maximum(length, 1e-30)
        self.length, self.height = length, height
        self.area = length*height*.5
        self.valid = (length > 1e-12) & (height > length*1e-10)
        self.xyz = xyz
        self.by_shell = defaultdict(list)
        edges = defaultdict(lambda: defaultdict(int))
        for n, triangle in enumerate(self.triangles):
            owner = mesh.shell_ids[int(triangle[0])]
            self.by_shell[owner].append(n)
            for a, b in zip(triangle, np.roll(triangle, -1)):
                edges[owner][tuple(sorted((int(a), int(b))))] += 1
        self.disks = set()
        self.metric_edges = {}
        for owner, rows in self.by_shell.items():
            degree = defaultdict(int)
            for (a, b), count in edges[owner].items():
                if count == 1:
                    degree[a] += 1; degree[b] += 1
            euler = len({int(i) for i in self.triangles[rows].ravel()})-len(edges[owner])+len(rows)
            # A partial slit can have disk Euler characteristic but a pinched
            # boundary. Unfold3D may crash there; require a manifold contour.
            if (euler == 1 and degree and all(v == 2 for v in degree.values())
                    and max(edges[owner].values()) <= 2):
                self.disks.add(owner)
            lengths = {}
            for triangle, points in zip(self.triangles[rows], self.xyz[rows]):
                for k in range(3):
                    a, b = int(triangle[k]), int(triangle[(k+1) % 3])
                    length = float(np.linalg.norm(points[k]-points[(k+1) % 3]))
                    if a != b and length > 1e-12:
                        lengths[tuple(sorted((a, b)))] = length
            self.metric_edges[owner] = lengths

    def length_error(self, shell, positions):
        """Relative edge/diagonal strain, ignoring uniform shell scale."""
        lengths = self.metric_edges[self.mesh.shell_ids[min(shell)]]
        if not lengths:
            return 0., 1.
        ratios = np.array([math.dist(positions[a], positions[b])/length
                           for (a, b), length in lengths.items()])
        total = float(ratios.sum())
        if total <= 1e-30 or not np.isfinite(ratios).all():
            return float('inf'), 1.
        scale = float(np.dot(ratios, ratios)/total)
        return float(np.sqrt(np.mean((ratios/scale-1.)**2))), scale

    def stretch_energy(self, shell, positions):
        """Symmetric stretch/compression cost, with the best uniform UV scale."""
        rows = np.array(self.by_shell[self.mesh.shell_ids[min(shell)]], dtype=int)
        rows = rows[self.valid[rows]]
        if not len(rows):
            return 4.
        points = np.array([positions[int(i)] for i in self.triangles[rows].ravel()]).reshape(-1, 3, 2)
        p = (points[:, 1]-points[:, 0])/self.length[rows, None]
        q = (points[:, 2]-points[:, 0]-p*self.x[rows, None])/self.height[rows, None]
        singular = np.linalg.svd(np.stack((p, q), axis=2), compute_uv=False)
        weights = self.area[rows]/self.area[rows].sum()
        forward = float(np.dot(weights, np.sum(singular**2, axis=1)))
        inverse = float(np.dot(weights, np.sum(1./np.maximum(singular**2, 1e-30), axis=1)))
        return 2.*math.sqrt(forward*inverse)

    def refine(self, shell, positions):
        """Free global length fitting; no borders are pinned or rectangularized."""
        before = self.quality(shell, positions)
        error, scale = self.length_error(shell, positions)
        if before[0] or error <= 2e-4 or not math.isfinite(error):
            return None
        from uv_straight import _global_length_relax
        neighbors = defaultdict(dict)
        for (a, b), length in self.metric_edges[self.mesh.shell_ids[min(shell)]].items():
            neighbors[a][b] = neighbors[b][a] = length*scale
        orientation = {}
        for n in self.by_shell[self.mesh.shell_ids[min(shell)]]:
            if not self.valid[n]:
                continue
            tri = tuple(int(i) for i in self.triangles[n])
            a, b, c = (positions[i] for i in tri)
            area = (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
            orientation[tri] = (1. if area > 0. else -1., abs(area)*.01)
        initial = {i: positions[i] for i in shell}
        initial_energy = self.stretch_energy(shell, initial)
        refined = _global_length_relax(initial, neighbors, defaultdict(set), set(),
                                      initial, 0., orientation,
                                      passes=40 if len(shell) <= 4096 else 16)
        # Conformal maps can have correct angles yet an uneven local scale.
        # Accept better metric fit with only a small angle tradeoff; protect
        # narrow faces and never introduce new collapsed/inverted triangles.
        # A full length fit can overcorrect a curved surface. Backtracking
        # keeps a useful partial correction instead of rejecting the whole pass.
        for step in range(8):
            fraction = .5**step
            candidate = {i: tuple(initial[i][d]+fraction*(refined[i][d]-initial[i][d])
                                  for d in (0, 1)) for i in shell}
            after = self.quality(shell, candidate)
            improved, _ = self.length_error(shell, candidate)
            if (after[0] == 0 and improved < error*.99
                    and after[1] <= max(1.03, before[1]*1.03)
                    and after[2] <= max(1.1, before[2]*1.05)
                    and self.stretch_energy(shell, candidate) < initial_energy-1e-6):
                return candidate
        return None

    def quality(self, shell, positions):
        """Scale-independent mean/worst stretch, with explicit inversion checks."""
        owner = self.mesh.shell_ids[min(shell)]
        rows = np.array(self.by_shell[owner], dtype=int)
        rows = rows[self.valid[rows]]
        if not len(rows):
            return (0, 1., 1.)
        points = np.array([positions[int(i)] for i in self.triangles[rows].ravel()]).reshape(-1, 3, 2)
        if not np.isfinite(points).all():
            return (len(rows), float('inf'), float('inf'))
        ab, ac = points[:, 1]-points[:, 0], points[:, 2]-points[:, 0]
        p = ab/self.length[rows, None]
        q = (ac-p*self.x[rows, None])/self.height[rows, None]
        jacobian = np.stack((p, q), axis=2)
        singular = np.linalg.svd(jacobian, compute_uv=False)
        ratios = singular[:, 0]/np.maximum(singular[:, 1], 1e-30)
        det = ab[:, 0]*ac[:, 1]-ab[:, 1]*ac[:, 0]
        span = max(float(np.ptp(points[:, :, 0])), float(np.ptp(points[:, :, 1])), 1e-30)
        collapsed = int(np.count_nonzero(abs(det) < span*span*1e-12))
        flipped = min(int(np.count_nonzero(det > 0.)), int(np.count_nonzero(det < 0.)))
        return (collapsed+flipped, float(np.average(ratios, weights=self.area[rows])), float(ratios.max()))

    def seed(self, shell):
        """Least-squares conformal map, using Maya's actual face triangulation."""
        ids = sorted(shell)
        owner = self.mesh.shell_ids[ids[0]]
        rows = np.array(self.by_shell[owner], dtype=int)
        rows = rows[self.valid[rows]]
        if not len(rows):
            return None
        index = {i: j for j, i in enumerate(ids)}
        tri = np.array([[index[int(i)] for i in t] for t in self.triangles[rows]])
        length, height, tx = self.length[rows], self.height[rows], self.x[rows]
        weight = np.sqrt(length*height)
        gx = np.stack((-height, height, np.zeros(len(rows))), axis=1)/weight[:, None]
        gy = np.stack((tx-length, -tx, length), axis=1)/weight[:, None]
        labels = np.stack((2*tri, 2*tri+1), axis=2).reshape(-1, 6)
        coefficients = np.empty((len(rows), 2, 6))
        coefficients[:, 0, 0::2] = gx; coefficients[:, 0, 1::2] = -gy
        coefficients[:, 1, 0::2] = gy; coefficients[:, 1, 1::2] = gx

        # Two geodesic sweeps find well-separated boundary anchors even when
        # the mesh folds in 3D (for example, the two sides of a cylinder seam).
        graph = defaultdict(dict)
        counts = defaultdict(int)
        for triangle, xyz in zip(tri, self.xyz[rows]):
            for k in range(3):
                a, b = int(triangle[k]), int(triangle[(k+1) % 3])
                if a == b:
                    continue
                distance = float(np.linalg.norm(xyz[k]-xyz[(k+1) % 3]))
                graph[a][b] = graph[b][a] = distance
                counts[tuple(sorted((a, b)))] += 1
        boundary = {i for pair, count in counts.items() if count == 1 for i in pair}
        candidates = boundary or set(graph)
        if len(candidates) < 2:
            return None
        def farthest(start):
            distances, pending = {start: 0.}, [(0., start)]
            while pending:
                distance, a = heapq.heappop(pending)
                if distance != distances[a]:
                    continue
                for b, length in graph[a].items():
                    value = distance+length
                    if value < distances.get(b, float('inf')):
                        distances[b] = value
                        heapq.heappush(pending, (value, b))
            return max((distances.get(i, -1.), i) for i in candidates)
        _, a = farthest(min(candidates))
        distance, b = farthest(a)
        if distance <= 1e-12:
            return None
        fixed = np.array([2*a, 2*a+1, 2*b, 2*b+1])
        count = 2*len(ids)
        free = np.setdiff1d(np.arange(count), fixed)
        values = np.zeros(count)
        # A unit gauge keeps the numerical solve independent of scene units.
        values[fixed] = [0., 0., 1., 0.]
        rhs = -np.einsum('tjk,tk->tj', coefficients, values[labels]).ravel()
        if len(ids) <= 128:
            matrix = np.zeros((2*len(rows), count))
            for k in range(6):
                matrix[np.arange(2*len(rows)), np.repeat(labels[:, k], 2)] += coefficients[:, :, k].ravel()
            values[free] = np.linalg.lstsq(matrix[:, free], rhs, rcond=None)[0]
        else:
            # Sparse LSQR needs O(UVs+triangles) memory and only NumPy. Avoid
            # allocating a quadratic dense matrix on large production shells.
            diagonal = np.bincount(labels.ravel(), weights=np.sum(coefficients**2, axis=1).ravel(), minlength=count)
            scale = 1./np.sqrt(np.maximum(diagonal[free], 1e-30))
            def multiply(x):
                full = np.zeros(count); full[free] = x*scale
                return np.einsum('tjk,tk->tj', coefficients, full[labels]).ravel()
            def transpose(x):
                weights = np.einsum('tj,tjk->tk', x.reshape(-1, 2), coefficients)
                return np.bincount(labels.ravel(), weights=weights.ravel(), minlength=count)[free]*scale
            values[free] = _lsqr(multiply, transpose, rhs, len(free))*scale
        if not np.isfinite(values).all():
            return None
        points = values.reshape(-1, 2)
        span = float(np.ptp(points, axis=0).max())
        if span <= 1e-12:
            return None
        points = (points-np.mean(points, axis=0))/span
        return {i: tuple(points[j]) for j, i in enumerate(ids)}


def _lsqr(multiply, transpose, rhs, count):
    """Golub-Kahan least squares with right Jacobi scaling and a bounded solve."""
    x = np.zeros(count)
    beta = float(np.linalg.norm(rhs))
    if beta == 0.:
        return x
    u = rhs/beta; v = transpose(u)
    alpha = float(np.linalg.norm(v))
    if alpha == 0.:
        return x
    v /= alpha; w = v.copy(); rho_bar, phi_bar = alpha, beta
    norm = alpha*alpha
    for _ in range(min(2000, max(200, count))):
        u = multiply(v)-alpha*u; beta = float(np.linalg.norm(u))
        if beta:
            u /= beta
        v = transpose(u)-beta*v; alpha = float(np.linalg.norm(v))
        if alpha:
            v /= alpha
        norm += beta*beta+alpha*alpha
        rho = math.hypot(rho_bar, beta)
        if rho == 0.:
            break
        c, s = rho_bar/rho, beta/rho
        theta, rho_bar = s*alpha, -c*alpha
        phi, phi_bar = c*phi_bar, s*phi_bar
        x += (phi/rho)*w; w = v-(theta/rho)*w
        # Normal-equation residual tests stationarity also when a curved
        # surface cannot have zero conformal residual.
        if alpha*abs(s*phi) <= 1e-9*math.sqrt(norm)*max(abs(phi_bar), 1e-30):
            break
    return x


def _better(candidate, reference):
    if candidate[0] != reference[0]:
        return candidate[0] < reference[0]
    # Retain the earlier map if native unfolding sacrifices a narrow face to
    # improve the area-weighted mean. Tolerate numerical noise near 1:1.
    return candidate[1] <= reference[1]+1e-5 and candidate[2] <= reference[2]*1.05+1e-5


def unfold(mesh):
    from uv_straight import Mesh
    from uv_straight_pack_commit import commit

    surface = Surface(mesh)
    shells = mesh.all_shells()
    positions, seeded = dict(mesh.points), set()
    for n, shell in enumerate(shells):
        seed = surface.seed(shell)
        if seed and _better(surface.quality(shell, seed), surface.quality(shell, positions)):
            positions.update(seed); seeded.add(n)
    _write(mesh, positions, commit)
    # Keep the native solver away from closed/periodic shells. A shell with
    # holes no longer disables native unfolding for the other, valid disks.
    disks = [shell for shell in shells if mesh.shell_ids[min(shell)] in surface.disks]
    periodic = [shell for shell in shells if mesh.shell_ids[min(shell)] not in surface.disks]
    for shell in periodic:
        # Maya's legacy global solver safely supports periodic UV topology.
        # This is also the fallback for partial slits; it never adds seams.
        cmds.unfold(mesh.components(shell), iterations=100, pinSelected=False,
                    pinUvBorder=False, applyToShell=True, useScale=False,
                    globalBlend=1., globalMethodBlend=.5)
    if periodic:
        legacy = Mesh(mesh.path)
        for shell in periodic:
            if _better(surface.quality(shell, legacy.points), surface.quality(shell, positions)):
                positions.update({i: legacy.points[i] for i in shell})
        _write(legacy, positions, commit)
    # Unfold3D preprocesses the whole mesh even for a UV subset. Folded
    # periodic faces elsewhere on this mesh can crash it while solving caps.
    unsafe_periodic = any(surface.quality(shell, positions)[0] for shell in periodic)
    if disks and not unsafe_periodic:
        components = ['{}.map[{}]'.format(mesh.name, i) for shell in disks for i in sorted(shell)]
        cmds.select(components, replace=True)
        cmds.u3dUnfold(ite=5, p=0, bi=1, tf=1, ms=1024, rs=0)
    after = Mesh(mesh.path)
    rejected = 0
    for shell in disks:
        if not _better(surface.quality(shell, after.points), surface.quality(shell, positions)):
            for i in shell:
                after.points[i] = positions[i]
            rejected += 1
    refined = 0
    for shell in shells:
        fitted = surface.refine(shell, after.points)
        if fitted:
            after.points.update(fitted); refined += 1
    _write(Mesh(mesh.path), after.points, commit)
    return dict(seeded=len(seeded), restored=rejected, legacy=len(periodic), refined=refined)


def _write(mesh, positions, commit):
    ids = [i for i in positions if tuple(mesh.points[i]) != tuple(positions[i])]
    if not ids:
        return
    before = [list(values) for values in mesh.fn.getUVs(mesh.uv_set)]
    after = [list(values) for values in before]
    for i in ids:
        after[0][i], after[1][i] = positions[i]
    commit([dict(handle=om.MObjectHandle(mesh.fn.object()), uv_set=mesh.uv_set,
                 before=before, after=after, ids=ids, shells=len(mesh.all_shells()))])
