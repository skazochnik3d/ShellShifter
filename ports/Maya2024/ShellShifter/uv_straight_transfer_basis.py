# Adapted from the local Normal_Map_UV_Transfer numerical core.
"""Tangent frame construction and source-to-target normal transformation."""

from __future__ import annotations

from uv_straight_deps import np

from uv_straight_transfer_errors import TangentBasisError
from uv_straight_transfer_codec import normalize_vectors


def _normalize(values: np.ndarray, epsilon: float = 1.0e-10) -> np.ndarray:
    lengths = np.linalg.norm(values, axis=-1, keepdims=True)
    if np.any(~np.isfinite(lengths)) or np.any(lengths <= epsilon):
        raise TangentBasisError('The tangent basis contains a degenerate axis.')
    return values / lengths


def frames_from_axes(
    tangents: np.ndarray, bitangents: np.ndarray, normals: np.ndarray, *, handedness=None
) -> np.ndarray:
    """Return orthonormal frames with T, B, N stored as matrix columns."""
    t_raw = np.asarray(tangents, dtype=np.float64)
    b_raw = np.asarray(bitangents, dtype=np.float64)
    n = _normalize(np.asarray(normals, dtype=np.float64))
    t = _normalize(t_raw - n * np.sum(n * t_raw, axis=-1, keepdims=True))
    canonical_b = _normalize(np.cross(n, t))
    handed = np.sign(np.sum(canonical_b * b_raw, axis=-1, keepdims=True) if handedness is None
                     else np.asarray(handedness,dtype=np.float64)[...,None])
    handed[handed == 0.0] = 1.0
    b = canonical_b * handed
    return np.stack((t, b, n), axis=-1).astype(np.float32)


def interpolate_frames(corner_frames: np.ndarray, weights: np.ndarray) -> np.ndarray:
    frames = np.asarray(corner_frames, dtype=np.float64)
    bary = np.asarray(weights, dtype=np.float64)
    raw = np.einsum("ni,ijk->njk", bary, frames)
    # Interpolate tangent handedness as a scalar. Blended B can point across
    # cross(N,T) even when all three corner frames have the same handedness;
    # deriving its sign after blending introduces a false reflection inside
    # curved triangles and inconsistent normals between matching UV stacks.
    signs=np.sign(np.sum(np.cross(frames[...,2],frames[...,0])*frames[...,1],axis=-1))
    handed=bary@signs
    return frames_from_axes(raw[..., 0], raw[..., 1], raw[..., 2],handedness=handed)


def transform_normals(
    source_normals: np.ndarray, source_frames: np.ndarray, target_frames: np.ndarray
) -> np.ndarray:
    source = np.asarray(source_normals, dtype=np.float64)
    src_frames = np.asarray(source_frames, dtype=np.float64)
    dst_frames = np.asarray(target_frames, dtype=np.float64)
    surface = np.einsum("...ij,...j->...i", src_frames, source)
    target = np.einsum("...ji,...j->...i", dst_frames, surface)
    return normalize_vectors(target)


def angular_error_degrees(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    first = normalize_vectors(a).astype(np.float64)
    second = normalize_vectors(b).astype(np.float64)
    dots = np.clip(np.sum(first * second, axis=-1), -1.0, 1.0)
    return np.degrees(np.arccos(dots)).astype(np.float32)
