# Adapted from the local Normal_Map_UV_Transfer numerical core.
"""Nearest-neighbour dilation for normal and generic texture padding."""

from __future__ import annotations

from uv_straight_deps import np

from uv_straight_transfer_codec import normalize_vectors
from uv_straight_transfer_errors import CancelledError


def dilate_values(
    values: np.ndarray, coverage: np.ndarray, owner: np.ndarray, iterations: int, *, cancelled=None
) -> tuple[np.ndarray, np.ndarray]:
    result = np.asarray(values, dtype=np.float32).copy()
    valid = np.asarray(coverage, dtype=bool).copy()
    owners = np.asarray(owner, dtype=np.int32).copy()
    height, width = valid.shape
    for _ in range(max(0, int(iterations))):
        if cancelled and cancelled(): raise CancelledError('Transfer cancelled while generating padding.')
        fill_values = np.zeros_like(result)
        fill_owner = np.full_like(owners, -1)
        found = np.zeros_like(valid)
        neighbors = (
            (slice(1, height), slice(0, width), slice(0, height - 1), slice(0, width)),
            (slice(0, height - 1), slice(0, width), slice(1, height), slice(0, width)),
            (slice(0, height), slice(1, width), slice(0, height), slice(0, width - 1)),
            (slice(0, height), slice(0, width - 1), slice(0, height), slice(1, width)),
        )
        for dst_y, dst_x, src_y, src_x in neighbors:
            eligible = (~valid[dst_y, dst_x]) & (~found[dst_y, dst_x]) & valid[src_y, src_x]
            if not np.any(eligible):
                continue
            block_values = fill_values[dst_y, dst_x]
            block_owner = fill_owner[dst_y, dst_x]
            block_found = found[dst_y, dst_x]
            block_values[eligible] = result[src_y, src_x][eligible]
            block_owner[eligible] = owners[src_y, src_x][eligible]
            block_found[eligible] = True
        if not np.any(found):
            break
        result[found] = fill_values[found]
        owners[found] = fill_owner[found]
        valid[found] = True
    return result, owners


def dilate_vectors(
    vectors: np.ndarray, coverage: np.ndarray, owner: np.ndarray, iterations: int, *, cancelled=None
) -> tuple[np.ndarray, np.ndarray]:
    result, owners = dilate_values(vectors, coverage, owner, iterations, cancelled=cancelled)
    valid = owners >= 0
    if np.any(valid):
        result[valid] = normalize_vectors(result[valid])
    return result, owners
