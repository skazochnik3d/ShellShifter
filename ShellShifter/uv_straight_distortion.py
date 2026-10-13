"""Bulk Maya topology and vector UV distortion, without per-edge guide work."""
import math
import numpy as np
import maya.api.OpenMaya as om


def shell_neutral_scale(densities, weights):
    """Current shell scale, robust to an isolated face edit and independent of history.

    The area-weighted median anchors unchanged faces while a local region is
    edited. A whole-shell texel change follows the current scale immediately,
    even when the same operation also removes shear or directional stretch.
    """
    densities = np.asarray(densities, dtype=np.float64)
    weights = np.asarray(weights, dtype=np.float64)
    valid = ((densities > 0.) & np.isfinite(densities) &
             (weights > 0.) & np.isfinite(weights))
    if not np.any(valid):
        return 1.
    logs = np.log(densities[valid])
    weights = weights[valid]
    order = np.argsort(logs, kind='stable')
    logs, weights = logs[order], weights[order]
    cumulative = np.cumsum(weights)
    half = cumulative[-1]*.5
    index = min(int(np.searchsorted(cumulative, half)), len(logs)-1)
    value = logs[index]
    # An exact half has an interval of valid medians. Its log midpoint keeps
    # the choice symmetric for two equal-size faces with opposite changes.
    if index+1 < len(logs) and abs(cumulative[index]-half) <= cumulative[-1]*1e-12:
        value = (value+logs[index+1])*.5
    return float(np.exp(value))


class EdgeUVLookup:
    def __init__(self, mesh):
        self.mesh = mesh
        self.cache = {}

    def get(self, edge, default=()):
        if edge not in self.cache:
            iterator = om.MItMeshEdge(self.mesh.path)
            iterator.setIndex(edge)
            a, b = self.mesh.fn.getEdgeVertices(edge)
            pairs = set()
            for face in iterator.getConnectedFaces():
                data = self.mesh.face_lookup.get(face)
                if data is not None:
                    uvs, vertices = data
                    pairs.add(tuple(sorted((uvs[vertices.index(a)], uvs[vertices.index(b)]))))
            self.cache[edge] = pairs
        return self.cache.get(edge, default)


class DistortionMesh:
    """Only the connectivity needed for distortion; guides use the full Mesh."""
    def __init__(self, path):
        self.path = path
        self.name = path.fullPathName()
        self.fn = om.MFnMesh(path)
        self.uv_set = self.fn.currentUVSetName()
        u, v = self.fn.getUVs(self.uv_set)
        self.points = dict(enumerate(zip(u, v)))
        _, self.shell_ids = self.fn.getUvShellsIds(self.uv_set)
        self.selected = set()
        self.explicit_selected = set()
        self.faces = []
        self.face_vertices = []
        self.face_lookup = {}
        self.distortion_faces = []
        poly_counts, vertices = self.fn.getVertices()
        uv_counts, uv_ids = self.fn.getAssignedUVs(self.uv_set)
        tri_counts, tri_vertices = self.fn.getTriangles()
        vertices, uv_ids, tri_vertices = tuple(vertices), tuple(uv_ids), tuple(tri_vertices)
        vertex_offset = uv_offset = triangle_offset = 0
        for face, (count, uv_count, triangles) in enumerate(zip(poly_counts, uv_counts, tri_counts)):
            face_vertices = vertices[vertex_offset:vertex_offset+count]
            face_uvs = uv_ids[uv_offset:uv_offset+uv_count]
            if uv_count == count and count:
                local = {vertex: i for i, vertex in enumerate(face_vertices)}
                indices = tuple(tuple(local[tri_vertices[triangle_offset+j+k]] for k in range(3))
                                for j in range(0, triangles*3, 3))
                self.faces.append(face_uvs)
                self.face_vertices.append(face_vertices)
                self.face_lookup[face] = face_uvs, face_vertices
                self.distortion_faces.append((face, self.shell_ids[face_uvs[0]],
                                              face_uvs, face_vertices, indices))
            vertex_offset += count
            uv_offset += uv_count
            triangle_offset += triangles*3
        self.edge_uvs = EdgeUVLookup(self)


class VectorGeometry:
    """Keep the reference metric; only UV coordinates enter repeated calculations."""
    def __init__(self, faces, positions):
        self.face_ids = np.array([face[0] for face in faces], dtype=np.int32)
        self.face_shells = np.array([face[1] for face in faces], dtype=np.int32)
        self.face_offsets = {int(face): i for i, face in enumerate(self.face_ids)}
        self.triangle_counts = np.array([len(face[-1]) for face in faces], dtype=np.int32)
        self.starts = np.r_[0, np.cumsum(self.triangle_counts)]
        tri_uvs, tri_vertices = [], []
        for _, _, uv_ids, vertices, triangles in faces:
            tri_uvs.extend(tuple(uv_ids[i] for i in triangle) for triangle in triangles)
            tri_vertices.extend(tuple(vertices[i] for i in triangle) for triangle in triangles)
        self.tri_uvs = np.array(tri_uvs, dtype=np.int32).reshape(-1, 3)
        xyz = np.array([(p.x, p.y, p.z) for p in positions], dtype=np.float64)
        verts = np.array(tri_vertices, dtype=np.int32).reshape(-1, 3)
        ab, ac = xyz[verts[:, 1]]-xyz[verts[:, 0]], xyz[verts[:, 2]]-xyz[verts[:, 0]]
        length = np.linalg.norm(ab, axis=1)
        area2 = np.linalg.norm(np.cross(ab, ac), axis=1)
        self.valid = (length > 1e-15) & (area2 > 1e-15)
        self.inv_length = 1./np.maximum(length, 1e-30)
        self.x = np.einsum('ij,ij->i', ab, ac)*self.inv_length
        self.inv_y = length/np.maximum(area2, 1e-30)
        self.area = np.where(self.valid, area2*.5, 0.)

    def calculate(self, color_range, faces, coordinates, reference, mesh_key, shape):
        u, v, _ = coordinates
        face_indices = np.array([self.face_offsets[face[0]] for face in faces], dtype=np.int32)
        if len(face_indices) == 0:
            return [], [], []
        # A partial update groups faces by UV shell. Even when every face was
        # edited, that order can differ from the mesh's triangle order. Reuse
        # the flat triangle array only for the original face sequence.
        triangle_counts = self.triangle_counts[face_indices]
        if (len(face_indices) == len(self.face_ids) and
                np.array_equal(face_indices, np.arange(len(self.face_ids)))):
            triangle_indices = np.arange(len(self.tri_uvs))
        else:
            # Vectorized ragged indexing keeps whole-model edits inexpensive
            # and supports faces with different numbers of triangles.
            local_starts = np.r_[0, np.cumsum(triangle_counts[:-1])]
            triangle_indices = (np.arange(int(triangle_counts.sum())) +
                                np.repeat(self.starts[face_indices]-local_starts,
                                          triangle_counts))
        triangles = self.tri_uvs[triangle_indices]
        uv = np.column_stack((np.asarray(u, dtype=np.float64), np.asarray(v, dtype=np.float64)))
        delta_b = uv[triangles[:, 1]]-uv[triangles[:, 0]]
        delta_c = uv[triangles[:, 2]]-uv[triangles[:, 0]]
        p = delta_b*self.inv_length[triangle_indices, None]
        q = (delta_c-p*self.x[triangle_indices, None])*self.inv_y[triangle_indices, None]
        trace = np.sum(p*p+q*q, axis=1)
        det = np.abs(p[:, 0]*q[:, 1]-q[:, 0]*p[:, 1])
        high = np.sqrt(np.maximum(0., .5*(trace+np.sqrt(np.maximum(0., trace*trace-4*det*det)))))
        low = np.maximum(det/np.maximum(high, 1e-30), 1e-15)
        high = np.maximum(high, 1e-15)
        weights = self.area[triangle_indices]
        valid = self.valid[triangle_indices]
        tri_faces = np.repeat(np.arange(len(face_indices)), triangle_counts)
        world_area = np.bincount(tri_faces, weights=weights, minlength=len(faces))
        uv_area = np.bincount(tri_faces, weights=weights*low*high, minlength=len(faces))
        mapped = world_area > 1e-15
        density = np.sqrt(np.maximum(uv_area, 1e-30)/np.maximum(world_area, 1e-30))
        shells = self.face_shells[face_indices]
        tri_shells = shells[tri_faces]
        scales = np.column_stack((low, high))
        neutral = np.ones(int(np.max(shells))+1)
        # Partial updates contain complete changed shells. Their normalization
        # uses only current geometry/UV data, so repair and Undo cannot leave a
        # stale texel scale while other cached shells remain untouched.
        order = np.argsort(shells, kind='stable')
        ordered_shells = shells[order]
        splits = np.r_[0, np.flatnonzero(np.diff(ordered_shells))+1, len(order)]
        for begin, end in zip(splits[:-1], splits[1:]):
            if begin == end:
                continue
            shell = int(ordered_shells[begin])
            indices = order[begin:end]
            neutral[shell] = shell_neutral_scale(density[indices], world_area[indices])
            reference[(mesh_key, shell)] = {'neutral': neutral[shell]}
        deviations = np.log(scales/neutral[tri_shells, None])
        minimum = np.full(len(faces), np.inf)
        maximum = np.full(len(faces), -np.inf)
        np.minimum.at(minimum, tri_faces[valid], np.min(deviations[valid], axis=1))
        np.maximum.at(maximum, tri_faces[valid], np.max(deviations[valid], axis=1))
        signed = np.where(np.round(np.abs(minimum), 10) >= np.round(np.abs(maximum), 10), minimum, maximum)
        dead_zone = math.log(1.02)
        strength = np.minimum(1., np.maximum(0., np.abs(signed)-dead_zone)/
                              (math.log(2.)-dead_zone)*max(.01, float(color_range)))
        targets = np.where(signed[:, None] > 0, np.array([.08, .32, 1.]), np.array([1., .08, .03]))
        # Maya's CPV buffer stores float32. Keep that same precision in the
        # cache so harmless double rounding cannot trigger native rewrites.
        rgb = (.48*(1-strength[:, None])+targets*strength[:, None]).astype(np.float32)
        uv_result, face_colors = [], []
        for i in np.flatnonzero(mapped):
            face, _, ids, _, _ = faces[i]
            color = tuple(float(value) for value in rgb[i])
            uv_result.append((color+(.65*float(strength[i]),), [(u[j], v[j]) for j in ids]))
            face_colors.append((shape, face, color+(1.,)))
        return uv_result, [], face_colors
