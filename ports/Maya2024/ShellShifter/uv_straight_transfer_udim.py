# Adapted from the local Normal_Map_UV_Transfer numerical core.
"""UDIM addressing and filename expansion."""

from __future__ import annotations

import glob
import math
import re
from pathlib import Path

from uv_straight_transfer_errors import ImageIoError, UdimLayoutError


UDIM_TOKEN = "<UDIM>"


def triangle_tiles(uvs, epsilon=1.0e-6):
    """Rasterize a crossing triangle independently inside every touched tile."""
    lo = [min(float(uv[a]) for uv in uvs) for a in (0, 1)]
    hi = [max(float(uv[a]) for uv in uvs) for a in (0, 1)]
    if lo[0] < -epsilon or lo[1] < -epsilon or hi[0] > 10 + epsilon:
        raise UdimLayoutError('Target UVs must be in nonnegative UDIM tiles with U < 10.')
    first = [max(0, math.floor(v + epsilon)) for v in lo]
    last = [max(first[a], math.ceil(hi[a] - epsilon) - 1) for a in (0, 1)]
    if (last[0]-first[0]+1)*(last[1]-first[1]+1) > 1000:
        raise UdimLayoutError('One UV triangle covers too many tiles.')
    return [(u,v,tile_to_udim(u,v)) for v in range(first[1],last[1]+1)
            for u in range(first[0],min(9,last[0])+1)]


def tile_to_udim(tile_u: int, tile_v: int) -> int:
    if tile_u < 0 or tile_v < 0:
        raise UdimLayoutError('Negative UDIM tile coordinates are not supported.')
    return 1001 + int(tile_u) + 10 * int(tile_v)


def udim_to_tile(udim: int) -> tuple[int, int]:
    offset = int(udim) - 1001
    if offset < 0:
        raise UdimLayoutError(f'Invalid UDIM number: {udim}')
    return offset % 10, offset // 10


def uv_tile(uv) -> tuple[int, int, int]:
    tile_u = math.floor(float(uv[0]))
    tile_v = math.floor(float(uv[1]))
    return tile_u, tile_v, tile_to_udim(tile_u, tile_v)


def triangle_tile(uvs, *, epsilon: float = 1.0e-6) -> tuple[int, int, int]:
    center_u = sum(float(uv[0]) for uv in uvs) / 3.0
    center_v = sum(float(uv[1]) for uv in uvs) / 3.0
    tile_u = math.floor(center_u)
    tile_v = math.floor(center_v)
    for uv in uvs:
        local_u = float(uv[0]) - tile_u
        local_v = float(uv[1]) - tile_v
        if local_u < -epsilon or local_u > 1.0 + epsilon:
            raise UdimLayoutError('A UV triangle crosses a tile boundary along U.')
        if local_v < -epsilon or local_v > 1.0 + epsilon:
            raise UdimLayoutError('A UV triangle crosses a tile boundary along V.')
    return tile_u, tile_v, tile_to_udim(tile_u, tile_v)


def discover_udim_paths(pattern: str) -> dict[int, str]:
    pattern=Path(pattern).as_posix()
    if UDIM_TOKEN not in pattern:
        path = Path(pattern)
        if not path.is_file():
            raise ImageIoError(f'Image file does not exist: {path}')
        return {1001: str(path)}
    glob_pattern = pattern.replace(UDIM_TOKEN, "[0-9][0-9][0-9][0-9]")
    escaped = re.escape(pattern).replace(re.escape(UDIM_TOKEN), r"(?P<udim>\d{4})")
    regex = re.compile(f"^{escaped}$", re.IGNORECASE)
    result: dict[int, str] = {}
    for candidate in glob.glob(glob_pattern):
        # Windows glob may return a backslash for the final separator even
        # when Maya supplied forward slashes in the texture path.
        match = regex.match(Path(candidate).as_posix())
        if not match:
            continue
        udim = int(match.group("udim"))
        if udim < 1001:
            continue
        result[udim] = candidate
    if not result:
        raise ImageIoError(f'No UDIM tiles match the pattern: {pattern}')
    return result


def output_path(pattern: str, udim: int, *, multiple_tiles: bool) -> str:
    if UDIM_TOKEN in pattern:
        return pattern.replace(UDIM_TOKEN, str(int(udim)))
    if multiple_tiles:
        raise UdimLayoutError(
            'Output paths for multiple target tiles must contain <UDIM>.'
        )
    return pattern
