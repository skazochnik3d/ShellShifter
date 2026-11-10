# Adapted from the local Normal_Map_UV_Transfer numerical core.
"""Decode and encode tangent-space normal vectors without color transforms."""

from __future__ import annotations

import numpy as np

from uv_straight_transfer_errors import InputError


EPSILON = 1.0e-12


def normalize_vectors(vectors: np.ndarray, *, epsilon: float = EPSILON) -> np.ndarray:
    values = np.asarray(vectors, dtype=np.float64)
    lengths = np.linalg.norm(values, axis=-1, keepdims=True)
    if np.any(~np.isfinite(lengths)):
        raise InputError('Normal data contains NaN or infinite values.')
    normalized = values / np.maximum(lengths, epsilon)
    invalid = lengths[..., 0] <= epsilon
    if np.any(invalid):
        normalized[invalid] = np.array((0.0, 0.0, 1.0), dtype=np.float64)
    return normalized.astype(np.float32)


def decode_normals(
    pixels: np.ndarray, *, green_sign: int = 1, bc5: bool = False
) -> np.ndarray:
    values = np.asarray(pixels, dtype=np.float32)
    if values.shape[-1] < 2:
        raise InputError('A normal map must have at least two channels.')
    if green_sign not in (-1, 1):
        raise InputError('Green-channel sign must be +1 or -1.')
    x = values[..., 0] * 2.0 - 1.0
    y = (values[..., 1] * 2.0 - 1.0) * float(green_sign)
    if bc5 or values.shape[-1] < 3:
        z = np.sqrt(np.maximum(0.0, 1.0 - x * x - y * y))
    else:
        z = values[..., 2] * 2.0 - 1.0
    return normalize_vectors(np.stack((x, y, z), axis=-1))


def encode_normals(
    normals: np.ndarray, *, green_sign: int = 1, bc5: bool = False
) -> np.ndarray:
    if green_sign not in (-1, 1):
        raise InputError('Green-channel sign must be +1 or -1.')
    values = normalize_vectors(normals)
    r = values[..., 0] * 0.5 + 0.5
    g = values[..., 1] * float(green_sign) * 0.5 + 0.5
    if bc5:
        return np.clip(np.stack((r, g), axis=-1), 0.0, 1.0)
    b = values[..., 2] * 0.5 + 0.5
    return np.clip(np.stack((r, g, b), axis=-1), 0.0, 1.0)


def rotate_signed_xy(normals: np.ndarray, angle_degrees: int) -> np.ndarray:
    """Reference discrete transform for UV rotations measured CCW."""
    angle = int(angle_degrees) % 360
    values = np.asarray(normals, dtype=np.float32)
    result = values.copy()
    x = values[..., 0].copy()
    y = values[..., 1].copy()
    if angle == 0:
        return result
    if angle == 90:
        result[..., 0], result[..., 1] = -y, x
    elif angle == 180:
        result[..., 0], result[..., 1] = -x, -y
    elif angle == 270:
        result[..., 0], result[..., 1] = y, -x
    else:
        raise InputError('Discrete rotation must be 0°, 90°, 180° or 270°.')
    return result
