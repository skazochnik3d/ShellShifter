"""Recognize and straighten open UV ribbons without changing mesh topology."""
from collections import defaultdict
from copy import copy
import heapq
import math
from types import SimpleNamespace

import numpy as np
import maya.api.OpenMaya as om
from uv_straight_distortion import DistortionMesh, VectorGeometry


def _cross2d(a, b):
    """Signed 2D area, supported by NumPy 1.x and 2.x."""
    return a[..., 0]*b[..., 1]-a[..., 1]*b[..., 0]


def _arc(loop, start, end):
    n = len(loop)
    return [loop[(start+k) % n] for k in range((end-start) % n+1)]


def _length(chain, points):
    return math.fsum(math.dist(points[a], points[b]) for a, b in zip(chain, chain[1:]))


def _parallel_fraction(first, second, points):
    """Paired rail tangents must mostly follow the same route through bends."""
    directions, ticks = [], []
    for chain in (first, second):
        deltas = np.diff(np.asarray([points[i] for i in chain]), axis=0)
        lengths = np.linalg.norm(deltas, axis=1)
        directions.append(deltas/lengths[:, None])
        ticks.append(np.r_[0., np.cumsum(lengths)]/lengths.sum())
    scores = []
    for t in np.linspace(.025, .975, 32):
        a, b = [min(np.searchsorted(cumulative, t)-1, len(vectors)-1)
                for cumulative, vectors in zip(ticks, directions)]
        scores.append(np.dot(directions[0][a], directions[1][b]))
    return float(np.mean(np.asarray(scores) > .25))


def _distances(graph, sources):
    distances = {i: 0. for i in sources}
    pending = [(0., i) for i in sources]
    heapq.heapify(pending)
    while pending:
        distance, a = heapq.heappop(pending)
        if distance != distances[a]:
            continue
        for b, length in graph[a].items():
            value = distance+length
            if value < distances.get(b, float('inf')):
                distances[b] = value
                heapq.heappush(pending, (value, b))
    return distances


def _simple_border(loop, positions):
    """Reject crossings introduced by transporting a stepped ribbon border."""
    edges = []
    for a, b in zip(loop, loop[1:]+loop[:1]):
        p, q = positions[a], positions[b]
        edges.append((min(p[0], q[0]), max(p[0], q[0]), min(p[1], q[1]),
                      max(p[1], q[1]), a, b, p, q))
    def cross(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    active = []
    for edge in sorted(edges):
        x0, x1, y0, y1, a, b, p, q = edge
        active = [prior for prior in active if prior[1] >= x0]
        for prior in active:
            if (prior[3] < y0 or y1 < prior[2] or
                    {a, b} & {prior[4], prior[5]}):
                continue
            c, d = prior[6:]
            if cross(p, q, c)*cross(p, q, d) <= 0. and cross(c, d, p)*cross(c, d, q) <= 0.:
                return False
        active.append(edge)
    return True


def _harmonic(graph, boundary, shell):
    """Positive edge weights keep interior points inside the straight border."""
    free = sorted(shell-set(boundary))
    if not free:
        return dict(boundary)
    indices = {i: n for n, i in enumerate(free)}
    diagonal = np.zeros(len(free))
    rhs = np.zeros((len(free), 2))
    aa, bb, weights = [], [], []
    for a in free:
        row = indices[a]
        for b, length in graph[a].items():
            weight = 1./max(length, 1e-20)
            diagonal[row] += weight
            if b in indices:
                aa.append(row); bb.append(indices[b]); weights.append(weight)
            else:
                rhs[row] += weight*np.asarray(boundary[b])
    if np.any(diagonal <= 0.):
        raise ValueError('Disconnected ribbon interior.')
    aa, bb, weights = np.asarray(aa, int), np.asarray(bb, int), np.asarray(weights)
    if len(free) <= 128:
        matrix = np.diag(diagonal)
        np.add.at(matrix, (aa, bb), -weights)
        values = np.linalg.solve(matrix, rhs)
    else:
        def multiply(x):
            return diagonal*x-np.bincount(aa, weights=weights*x[bb], minlength=len(free))
        values = np.zeros_like(rhs)
        for dim in (0, 1):
            x = np.full(len(free), np.mean([p[dim] for p in boundary.values()]))
            residual = rhs[:, dim]-multiply(x)
            tolerance = max(np.linalg.norm(rhs[:, dim]), 1e-20)*1e-9
            z = residual/diagonal
            direction = z.copy()
            rz = np.dot(residual, z)
            for _ in range(min(4*len(free), 2000)):
                if np.linalg.norm(residual) <= tolerance:
                    break
                product = multiply(direction)
                denominator = np.dot(direction, product)
                if denominator <= 0.:
                    break
                alpha = rz/denominator
                x += alpha*direction
                residual -= alpha*product
                z = residual/diagonal
                next_rz = np.dot(residual, z)
                direction = z+(next_rz/max(rz, 1e-30))*direction
                rz = next_rz
            if np.linalg.norm(residual) > tolerance*10.:
                raise ValueError('Ribbon interior did not converge.')
            values[:, dim] = x
    result = dict(boundary)
    result.update({i: tuple(values[n]) for n, i in enumerate(free)})
    return result


def unfold_ribbons(mesh, shells):
    """Run the existing constrained Unfold only on newly marked ribbons.

    Refine against Maya's physical triangles too: fan triangulation of a
    stepped polygon can give the regular solver an unsuitable metric. Keep
    only orientation-safe results that improve the current ribbon distortion,
    then restore its area and centre. The caller owns the single Undo chunk.
    """
    import uv_straight as uv
    data = DistortionMesh(mesh.path)
    world = mesh.fn.getPoints(om.MSpace.kWorld)
    seeds = [{i: mesh.points[i] for i in shell} for shell, _ in shells]
    tasks = [(mesh.path, set(shell), groups, set(), {}, seed)
             for (shell, groups), seed in zip(shells, seeds)]
    uv._solve_tasks(tasks, 350, .005, relax_bridge=True)
    solved = uv.Mesh(mesh.path)
    for (shell, groups), seed in zip(shells, seeds):
        faces = [row for row in data.distortion_faces if set(row[2]) <= shell]
        geometry = VectorGeometry(faces, world)
        triangles = geometry.tri_uvs
        border = uv._auto_paths(uv._border_pairs(mesh, [shell]))[0][0][:-1]
        def measures(positions):
            points = np.asarray([positions[int(i)] for tri in triangles for i in tri]).reshape(-1, 3, 2)
            area2 = _cross2d(points[:, 1]-points[:, 0], points[:, 2]-points[:, 0])
            p = (points[:, 1]-points[:, 0])*geometry.inv_length[:, None]
            q = ((points[:, 2]-points[:, 0])-p*geometry.x[:, None])*geometry.inv_y[:, None]
            valid = geometry.valid
            scales = np.linalg.svd(np.stack((p[valid], q[valid]), axis=2), compute_uv=False)
            weights = geometry.area[valid]/geometry.area[valid].sum()
            density = math.sqrt(float(np.sum(weights*np.prod(scales, axis=1))))
            deviations = np.log(np.maximum(scales, 1e-20)/max(density, 1e-20))
            distortion = float(np.sqrt(np.sum(weights[:, None]*deviations**2)*.5))
            anisotropy = float(np.max(scales[:, 0]/np.maximum(scales[:, 1], 1e-20)))
            return float(np.sum(np.abs(area2)))*.5, area2, distortion, anisotropy
        area, signs, best_error, maximum = measures(seed)
        best = seed
        # The surface pass uses the actual triangulation and world-space
        # lengths without altering the scene's polygon faces or seams.
        proxy = copy(mesh)
        proxy.faces, proxy.face_vertices = [], []
        for _, _, face, vertices, local_triangles in faces:
            for tri in local_triangles:
                proxy.faces.append(tuple(face[j] for j in tri))
                proxy.face_vertices.append(tuple(vertices[j] for j in tri))
        proxy.fn = SimpleNamespace(getPoint=lambda i: world[i],
                                   getEdgeVertices=mesh.fn.getEdgeVertices)
        scale = math.sqrt(area/float(geometry.area.sum()))
        refined = uv._surface_relax(proxy, set(shell), groups, set(), seed, scale)
        candidates = ({i: solved.points[i] for i in shell}, refined)
        for candidate in candidates:
            if not np.isfinite(np.asarray(list(candidate.values()))).all():
                continue
            target_area, target_signs, error, anisotropy = measures(candidate)
            if (np.any(target_signs*signs <= 0.) or
                    np.any(np.abs(target_signs) <= area*2e-10) or
                    not _simple_border(border, candidate) or
                    error > best_error+1e-10 or anisotropy > maximum*1.001):
                continue
            if any(max(candidate[i][1-uv._axis(g['axis'])] for i in g['ids'])-
                   min(candidate[i][1-uv._axis(g['axis'])] for i in g['ids']) > 1e-6
                   for g in groups):
                continue
            best, best_error = candidate, error
        target_area = measures(best)[0]
        factor = math.sqrt(area/target_area)
        original_center = [math.fsum(seed[i][k] for i in shell)/len(shell) for k in (0, 1)]
        centre = [math.fsum(best[i][k] for i in shell)/len(shell) for k in (0, 1)]
        mesh.write({i: tuple((best[i][k]-centre[k])*factor+original_center[k]
                            for k in (0, 1)) for i in shell})


class RibbonAnalyzer:
    """Build polygon/triangle connectivity once for the selected mesh."""
    def __init__(self, mesh):
        self.mesh = mesh
        self.by_shell = defaultdict(list)
        data = DistortionMesh(mesh.path)
        self.world = mesh.fn.getPoints(om.MSpace.kWorld)
        for row in data.distortion_faces:
            self.by_shell[row[1]].append(row)

    def straighten(self, shell, axis='H'):
        import uv_straight as uv
        rows = self.by_shell[self.mesh.shell_ids[min(shell)]]
        faces = [row[2] for row in rows]
        if not faces or any(len(set(face)) != len(face) for face in faces):
            raise ValueError('Invalid ribbon faces.')
        edges = defaultdict(list)
        vertices = {}
        triangles = []
        for n, (_, _, face, xyz, local_triangles) in enumerate(rows):
            vertices.update(zip(face, xyz))
            triangles.extend(tuple(face[j] for j in tri) for tri in local_triangles)
            for a, b in zip(face, face[1:]+face[:1]):
                edges[tuple(sorted((a, b)))].append(n)
        if (set(vertices) != set(shell) or max(map(len, edges.values())) > 2 or
                len(shell)-len(edges)+len(faces) != 1):
            raise ValueError('Ribbon must be a manifold disk without holes.')
        contours = uv._auto_paths({edge for edge, users in edges.items() if len(users) == 1})
        if len(contours) != 1 or not contours[0][1]:
            raise ValueError('Ribbon needs one unbranched closed border.')
        loop = contours[0][0][:-1]
        point = self.mesh.points
        graph = defaultdict(dict)
        metric = defaultdict(dict)
        for a, b in edges:
            length = math.dist(point[a], point[b])
            world_length = (self.world[vertices[a]]-self.world[vertices[b]]).length()
            if length <= 1e-12 or world_length <= 1e-12:
                raise ValueError('Ribbon has collapsed edges.')
            graph[a][b] = graph[b][a] = length
            metric[a][b] = metric[b][a] = world_length
        source = np.asarray([point[i] for tri in triangles for i in tri]).reshape(-1, 3, 2)
        source_areas = _cross2d(source[:, 1]-source[:, 0], source[:, 2]-source[:, 0])
        floor = float(np.sum(np.abs(source_areas)))*1e-10
        winding = 1. if float(source_areas.sum()) > 0. else -1.
        if not len(triangles) or np.any(source_areas*winding <= floor):
            raise ValueError('Ribbon has folded or degenerate UV triangles.')
        area = float(np.sum(np.abs(source_areas)))*.5
        perimeter = _length(loop+[loop[0]], point)
        index = {i: n for n, i in enumerate(loop)}
        dual = defaultdict(set)
        for users in edges.values():
            if len(users) == 2:
                a, b = users
                dual[a].add(b); dual[b].add(a)
        ends = [n for n in range(len(rows)) if len(dual[n]) == 1]
        strip = len(ends) == 2 and all(len(dual[n]) <= 2 for n in range(len(rows)))
        if strip:
            caps = []
            for end in ends:
                candidates = [edge for edge, users in edges.items() if users == [end]]
                caps.append([(index[a], index[b]) if (index[b]-index[a]) % len(loop) == 1
                             else (index[b], index[a]) for a, b in candidates])
            pairs = [(a, b) for a in caps[0] for b in caps[1]]
        else:
            corners = []
            for n, i in enumerate(loop):
                a, b, c = np.asarray(point[loop[n-1]]), np.asarray(point[i]), np.asarray(point[loop[(n+1) % len(loop)]])
                u, v = b-a, c-b
                cosine = np.dot(u, v)/(np.linalg.norm(u)*np.linalg.norm(v))
                if cosine < math.cos(math.radians(25.)):
                    corners.append(n)
            caps = [(a, b) for a, b in zip(corners, corners[1:]+corners[:1])
                    if _length(_arc(loop, a, b), point) <= perimeter*.22]
            pairs = [(a, b) for n, a in enumerate(caps) for b in caps[n+1:]]
        candidates = []
        for first, second in pairs:
            a, b = first; c, d = second
            size = len(loop)
            offsets = [(j-a) % size for j in (b, c, d)]
            if not 0 < offsets[0] < offsets[1] < offsets[2] < size:
                continue
            cap1, cap2 = _arc(loop, a, b), _arc(loop, c, d)
            rail1, rail2 = _arc(loop, b, c), list(reversed(_arc(loop, d, a)))
            lengths = [_length(rail, point) for rail in (rail1, rail2)]
            widths = [_length(cap, point) for cap in (cap1, cap2)]
            if (min(lengths) <= 0. or min(widths) <= 0. or
                    min(lengths)/max(lengths) < .3 or max(widths)/min(widths) > 8.):
                continue
            score = min(lengths)/(sum(widths)*.5)*min(lengths)/max(lengths)
            candidates.append((score, rail1, rail2, cap1, cap2, lengths, widths))
        single_sides = []
        for _, rail1, rail2, cap1, cap2, lengths, widths in sorted(candidates, reverse=True)[:24]:
            if _parallel_fraction(rail1, rail2, point) < .65:
                continue
            distances = _distances(graph, rail1)
            samples = [distances[i] for i in rail2[1:-1]] or [distances[i] for i in rail2]
            width = float(np.median(samples))
            length = sum(lengths)*.5
            if (width <= 1e-12 or length/width < (1.5 if strip else 2.5) or
                    not .35 <= (sum(widths)*.5)/width <= 3.):
                continue
            single_sides.extend((rail_length, rail) for rail_length, rail in zip(lengths, (rail1, rail2))
                                if rail_length/width >= (1.5 if strip else 2.5))
            # Preserve UV area (and average texel density) while removing bends.
            width = area/length
            if length/width < 1.5:
                continue
            boundary = {}
            for rail, transverse in ((rail1, -.5*width), (rail2, .5*width)):
                total = _length(rail, point)
                distance = 0.
                boundary[rail[0]] = (0., transverse)
                for a, b in zip(rail, rail[1:]):
                    distance += math.dist(point[a], point[b])
                    boundary[b] = (length*distance/total, transverse)
            for cap, longitudinal in ((cap1, 0.), (cap2, length)):
                start, end = boundary[cap[0]], boundary[cap[-1]]
                total, distance = _length(cap, point), 0.
                for a, b in zip(cap, cap[1:]):
                    distance += math.dist(point[a], point[b])
                    boundary[b] = (longitudinal, start[1]+(end[1]-start[1])*distance/total)
            try:
                positions = _harmonic(metric, boundary, set(shell))
            except (ValueError, np.linalg.LinAlgError):
                continue
            target = np.asarray([positions[i] for tri in triangles for i in tri]).reshape(-1, 3, 2)
            areas = _cross2d(target[:, 1]-target[:, 0], target[:, 2]-target[:, 0])
            direction = 1. if areas.sum() > 0. else -1.
            if not np.isfinite(target).all() or np.any(areas*direction <= floor):
                continue
            if direction != winding:
                positions = {i: (p[0], -p[1]) for i, p in positions.items()}
            if axis == 'V':
                positions = {i: (-p[1], p[0]) for i, p in positions.items()}
            old_center = [math.fsum(point[i][k] for i in shell)/len(shell) for k in (0, 1)]
            new_center = [math.fsum(positions[i][k] for i in shell)/len(shell) for k in (0, 1)]
            positions = {i: tuple(p[k]+old_center[k]-new_center[k] for k in (0, 1))
                         for i, p in positions.items()}
            guides = [dict(axis=axis, ids=rail, mode='edge') for rail in (rail1, rail2)]
            return positions, guides
        # A stepped width can put three corners of a physical triangle on one
        # border. Flattening both borders would collapse that triangle. In that
        # case straighten one reliable long side and transport the rest with
        # its local frames, preserving the transverse profile.
        tried = set()
        for _, rail in sorted(single_sides, reverse=True):
            key = tuple(rail)
            if key in tried:
                continue
            tried.add(key)
            members = set(rail)
            if any(set(triangle) <= members for triangle in triangles):
                continue
            guides = [dict(axis=axis, ids=rail, mode='edge')]
            aligned = uv._constraint_positions(self.mesh, guides)
            transported = uv._guided_start(self.mesh, set(shell), guides, aligned, set())
            positions = {i: transported[i] for i in shell}
            target = np.asarray([positions[i] for tri in triangles for i in tri]).reshape(-1, 3, 2)
            areas = _cross2d(target[:, 1]-target[:, 0], target[:, 2]-target[:, 0])
            if (not np.isfinite(target).all() or np.any(areas*winding <= floor) or
                    not _simple_border(loop, positions)):
                continue
            scale = math.sqrt(area/(float(np.sum(np.abs(areas)))*.5))
            old_center = [math.fsum(point[i][k] for i in shell)/len(shell) for k in (0, 1)]
            new_center = [math.fsum(positions[i][k] for i in shell)/len(shell) for k in (0, 1)]
            positions = {i: tuple((p[k]-new_center[k])*scale+old_center[k] for k in (0, 1))
                         for i, p in positions.items()}
            return positions, guides
        raise ValueError('No reliable longitudinal sides found for this UV shell.')
