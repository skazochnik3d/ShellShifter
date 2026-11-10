# Adapted from the local Normal_Map_UV_Transfer numerical core.
"""CPU reference rasterizer for UV-to-UV tangent normal transfer."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np

from uv_straight_transfer_udim import triangle_tiles, tile_to_udim
from uv_straight_transfer_models import (
    CancelCallback,
    ColorRasterTileResult,
    CornerMap,
    ImageSet,
    MeshSnapshot,
    ProgressCallback,
    RasterTileResult,
    TransferSettings,
)
from uv_straight_transfer_errors import (
    CancelledError,
    DegenerateUvError,
    InputError,
    OverlapConflictError,
)
from uv_straight_transfer_codec import decode_normals, normalize_vectors
from uv_straight_transfer_padding import dilate_values, dilate_vectors
from uv_straight_transfer_basis import interpolate_frames, transform_normals


def _barycentric(points: np.ndarray, triangle: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a, b, c = np.asarray(triangle, dtype=np.float64)
    p = np.asarray(points, dtype=np.float64)
    denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
    if abs(float(denominator)) <= 1.0e-14:
        raise DegenerateUvError('The target UV triangle has zero area.')
    w0 = ((b[1] - c[1]) * (p[:, 0] - c[0]) + (c[0] - b[0]) * (p[:, 1] - c[1])) / denominator
    w1 = ((c[1] - a[1]) * (p[:, 0] - c[0]) + (a[0] - c[0]) * (p[:, 1] - c[1])) / denominator
    w2 = 1.0 - w0 - w1
    weights = np.stack((w0, w1, w2), axis=-1)
    inside = np.all(weights >= -1.0e-10, axis=-1) & np.all(weights <= 1.0 + 1.0e-10, axis=-1)
    return weights, inside


def _sample_bilinear(tile_pixels: np.ndarray, source_uv: np.ndarray, tile_u: int, tile_v: int) -> np.ndarray:
    pixels = np.asarray(tile_pixels, dtype=np.float32)
    height, width = pixels.shape[:2]
    local_u = source_uv[:, 0] - float(tile_u)
    local_v = source_uv[:, 1] - float(tile_v)
    x = local_u * width - 0.5
    y = (1.0 - local_v) * height - 0.5
    x = np.clip(x, 0.0, width - 1.0)
    y = np.clip(y, 0.0, height - 1.0)
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    x1 = np.minimum(x0 + 1, width - 1)
    y1 = np.minimum(y0 + 1, height - 1)
    fx = (x - x0).astype(np.float32)[:, None]
    fy = (y - y0).astype(np.float32)[:, None]
    top = pixels[y0, x0] * (1.0 - fx) + pixels[y0, x1] * fx
    bottom = pixels[y1, x0] * (1.0 - fx) + pixels[y1, x1] * fx
    return top * (1.0 - fy) + bottom * fy


def _sample_images(images, uv):
    if images.repeat:
        return _sample_bilinear(images.require(1001).pixels, np.mod(uv, 1.), 0, 0)
    coordinates = np.floor(uv).astype(np.int64)
    if np.any(coordinates < 0) or np.any(coordinates[:, 0] >= 10):
        raise InputError('Source UVs are outside supported UDIM tiles.')
    values = None
    for tile_u, tile_v in np.unique(coordinates, axis=0):
        selected = np.all(coordinates == (tile_u, tile_v), axis=1)
        tile = images.require(tile_to_udim(int(tile_u), int(tile_v)))
        sampled = _sample_bilinear(tile.pixels, uv[selected], tile_u, tile_v)
        if values is None: values = np.empty((len(uv), sampled.shape[1]), dtype=np.float32)
        values[selected] = sampled
    return values


def _jobs(target):
    return [(i, corners, tile) for i, corners in enumerate(target.triangle_corners)
            for tile in triangle_tiles(_triangle_uvs(target, corners))]


def _pixel_bounds(local_uv: np.ndarray, width: int, height: int) -> tuple[int, int, int, int]:
    min_u, min_v = np.min(local_uv, axis=0)
    max_u, max_v = np.max(local_uv, axis=0)
    x0 = max(0, int(math.floor(min_u * width)) - 1)
    x1 = min(width, int(math.ceil(max_u * width)) + 1)
    y0 = max(0, int(math.floor((1.0 - max_v) * height)) - 1)
    y1 = min(height, int(math.ceil((1.0 - min_v) * height)) + 1)
    return x0, x1, y0, y1


def _triangle_uvs(mesh: MeshSnapshot, corners: np.ndarray) -> np.ndarray:
    uv_ids = mesh.uv_set.corner_uv_ids[np.asarray(corners, dtype=np.int32)]
    if np.any(uv_ids < 0):
        raise InputError('A triangle has a face corner without UV coordinates.')
    return mesh.uv_set.uvs[uv_ids]


def _rasterize_transfer_pass(
    source: MeshSnapshot,
    target: MeshSnapshot,
    corner_map: CornerMap,
    source_images: ImageSet,
    settings: TransferSettings,
    *,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
    _sample_offset=(0., 0.),
    allow_conflicts=False,
) -> dict[int, RasterTileResult]:
    if settings.width <= 0 or settings.height <= 0:
        raise InputError('Output map width and height must be positive.')
    supersampling = int(settings.supersampling)
    if supersampling not in (1, 2, 4):
        raise InputError('Supersampling must be 1, 2 or 4.')
    jobs = _jobs(target)
    results: dict[int, RasterTileResult] = {}
    for _, _, udim in sorted({job[2] for job in jobs}):
        shape = (settings.height, settings.width)
        vectors = np.zeros(shape + (3,), dtype=np.float32)
        vectors[..., 2] = 1.0
        results[udim] = RasterTileResult(
            udim=udim,
            vectors=vectors,
            coverage=np.zeros(shape, dtype=bool),
            owner=np.full(shape, -1, dtype=np.int32),
            conflicts=np.zeros(shape, dtype=bool),
        )
    sample_offsets = (np.arange(supersampling, dtype=np.float64) + 0.5) / supersampling - 0.5
    total = len(jobs)
    for job_index, (triangle_index, target_corners, target_tile_info) in enumerate(jobs):
        if cancelled and cancelled():
            raise CancelledError('Normal-map transfer cancelled.')
        target_corners = np.asarray(target_corners, dtype=np.int32)
        source_corners = corner_map.target_to_source[target_corners]
        target_uv = _triangle_uvs(target, target_corners)
        source_uv = _triangle_uvs(source, source_corners)
        target_tile_u, target_tile_v, target_udim = target_tile_info
        target_local_uv = target_uv - np.array((target_tile_u, target_tile_v), dtype=np.float64)
        x0, x1, y0, y1 = _pixel_bounds(target_local_uv, settings.width, settings.height)
        if x0 >= x1 or y0 >= y1:
            continue
        source_corner_frames = source.frames[source_corners]
        target_corner_frames = target.frames[target_corners]
        result = results[target_udim]
        # Limit temporary arrays independently of the final texture resolution.
        # A single UV triangle can cover a complete 8K tile; allocating its whole
        # bounding box as float64 would otherwise require several gigabytes.
        block_size = 256
        for block_y0 in range(y0, y1, block_size):
            block_y1 = min(block_y0 + block_size, y1)
            for block_x0 in range(x0, x1, block_size):
                if cancelled and cancelled():
                    raise CancelledError('Normal-map transfer cancelled.')
                block_x1 = min(block_x0 + block_size, x1)
                grid_y, grid_x = np.mgrid[block_y0:block_y1, block_x0:block_x1]
                flat_x = grid_x.ravel()
                flat_y = grid_y.ravel()
                vector_sum = np.zeros((flat_x.size, 3), dtype=np.float64)
                sample_count = np.zeros(flat_x.size, dtype=np.int32)
                for offset_y in sample_offsets + _sample_offset[1]:
                    for offset_x in sample_offsets + _sample_offset[0]:
                        sample_uv = np.stack(
                            (
                                (flat_x + 0.5 + offset_x) / settings.width,
                                1.0
                                - (flat_y + 0.5 + offset_y) / settings.height,
                            ),
                            axis=-1,
                        )
                        weights, inside = _barycentric(sample_uv, target_local_uv)
                        if not np.any(inside):
                            continue
                        selected_weights = weights[inside]
                        mapped_source_uv = np.einsum(
                            "ni,ij->nj", selected_weights, source_uv
                        )
                        sampled_pixels = _sample_images(source_images, mapped_source_uv)
                        sampled_normals = decode_normals(
                            sampled_pixels,
                            green_sign=settings.input_green_sign,
                            bc5=settings.input_bc5,
                        )
                        source_frames = interpolate_frames(
                            source_corner_frames, selected_weights
                        )
                        target_frames = interpolate_frames(
                            target_corner_frames, selected_weights
                        )
                        transformed = transform_normals(
                            sampled_normals, source_frames, target_frames
                        )
                        vector_sum[inside] += transformed
                        sample_count[inside] += 1
                active = sample_count > 0
                if not np.any(active):
                    continue
                new_vectors = normalize_vectors(
                    vector_sum[active] / sample_count[active, None]
                )
                rows = flat_y[active]
                columns = flat_x[active]
                already = result.coverage[rows, columns]
                if np.any(already):
                    old_vectors = result.vectors[rows[already], columns[already]]
                    overlap_vectors = new_vectors[already]
                    dots = np.clip(
                        np.sum(old_vectors * overlap_vectors, axis=-1), -1.0, 1.0
                    )
                    angles = np.degrees(np.arccos(dots))
                    conflict = angles > float(settings.overlap_tolerance_degrees)
                    if np.any(conflict):
                        conflict_rows = rows[already][conflict]
                        conflict_columns = columns[already][conflict]
                        result.conflicts[conflict_rows, conflict_columns] = True
                    if not allow_conflicts:
                        result.vectors[rows[already], columns[already]] = normalize_vectors(old_vectors + overlap_vectors)
                fresh = ~already
                result.vectors[rows[fresh], columns[fresh]] = new_vectors[fresh]
                result.coverage[rows, columns] = True
                result.owner[rows[fresh], columns[fresh]] = triangle_index
        if progress:
            progress("Rasterizing", job_index + 1, total)
    total_conflicts = sum(int(np.count_nonzero(item.conflicts)) for item in results.values())
    if total_conflicts and not allow_conflicts:
        raise OverlapConflictError(
            f'Target UV overlaps created conflicting pixels: {total_conflicts}.',
            context={"conflict_pixels": total_conflicts},
        )
    for result in results.values():
        result.vectors, result.owner = dilate_vectors(
            result.vectors, result.coverage, result.owner, settings.padding, cancelled=cancelled
        )
    return results


def _rasterize_color_pass(
    source: MeshSnapshot,
    target: MeshSnapshot,
    corner_map: CornerMap,
    source_images: ImageSet,
    settings: TransferSettings,
    *,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
    _sample_offset=(0., 0.),
    allow_conflicts=False,
) -> dict[int, ColorRasterTileResult]:
    """Remap RGBA pixels geometrically, without tangent-space channel changes."""
    if settings.width <= 0 or settings.height <= 0:
        raise InputError('Output texture width and height must be positive.')
    supersampling = int(settings.supersampling)
    if supersampling not in (1, 2, 4):
        raise InputError('Supersampling must be 1, 2 or 4.')

    jobs = _jobs(target)
    results: dict[int, ColorRasterTileResult] = {}
    for _, _, udim in sorted({job[2] for job in jobs}):
        shape = (settings.height, settings.width)
        results[udim] = ColorRasterTileResult(
            udim=udim,
            pixels=np.zeros(shape + (4,), dtype=np.float32),
            coverage=np.zeros(shape, dtype=bool),
            owner=np.full(shape, -1, dtype=np.int32),
            conflicts=np.zeros(shape, dtype=bool),
        )

    sample_offsets = (np.arange(supersampling, dtype=np.float64) + 0.5) / supersampling - 0.5
    total = len(jobs)
    for job_index, (triangle_index, target_corners, target_tile_info) in enumerate(jobs):
        if cancelled and cancelled():
            raise CancelledError('Material texture transfer cancelled.')
        target_corners = np.asarray(target_corners, dtype=np.int32)
        source_corners = corner_map.target_to_source[target_corners]
        target_uv = _triangle_uvs(target, target_corners)
        source_uv = _triangle_uvs(source, source_corners)
        target_tile_u, target_tile_v, target_udim = target_tile_info
        target_local_uv = target_uv - np.array((target_tile_u, target_tile_v), dtype=np.float64)
        x0, x1, y0, y1 = _pixel_bounds(target_local_uv, settings.width, settings.height)
        result = results[target_udim]
        for block_y0 in range(y0, y1, 256):
            block_y1 = min(block_y0 + 256, y1)
            for block_x0 in range(x0, x1, 256):
                if cancelled and cancelled():
                    raise CancelledError('Material texture transfer cancelled.')
                block_x1 = min(block_x0 + 256, x1)
                grid_y, grid_x = np.mgrid[block_y0:block_y1, block_x0:block_x1]
                flat_x, flat_y = grid_x.ravel(), grid_y.ravel()
                pixel_sum = np.zeros((flat_x.size, 4), dtype=np.float64)
                sample_count = np.zeros(flat_x.size, dtype=np.int32)
                for offset_y in sample_offsets + _sample_offset[1]:
                    for offset_x in sample_offsets + _sample_offset[0]:
                        sample_uv = np.stack(
                            (
                                (flat_x + 0.5 + offset_x) / settings.width,
                                1.0 - (flat_y + 0.5 + offset_y) / settings.height,
                            ),
                            axis=-1,
                        )
                        weights, inside = _barycentric(sample_uv, target_local_uv)
                        if not np.any(inside):
                            continue
                        mapped_source_uv = np.einsum("ni,ij->nj", weights[inside], source_uv)
                        sampled = _sample_images(source_images, mapped_source_uv)
                        if sampled.shape[1] == 3:
                            sampled = np.concatenate(
                                (sampled, np.ones((len(sampled), 1), dtype=np.float32)), axis=1
                            )
                        pixel_sum[inside] += sampled[:, :4]
                        sample_count[inside] += 1
                active = sample_count > 0
                if not np.any(active):
                    continue
                new_pixels = (pixel_sum[active] / sample_count[active, None]).astype(np.float32)
                rows, columns = flat_y[active], flat_x[active]
                already = result.coverage[rows, columns]
                if np.any(already):
                    old_pixels = result.pixels[rows[already], columns[already]]
                    overlap_pixels = new_pixels[already]
                    conflict = np.max(np.abs(old_pixels - overlap_pixels), axis=-1) > (2.0 / 255.0)
                    result.conflicts[rows[already][conflict], columns[already][conflict]] = True
                    if not allow_conflicts:result.pixels[rows[already], columns[already]] = (
                        old_pixels + overlap_pixels
                    ) * 0.5
                fresh = ~already
                result.pixels[rows[fresh], columns[fresh]] = new_pixels[fresh]
                result.coverage[rows, columns] = True
                result.owner[rows[fresh], columns[fresh]] = triangle_index
        if progress:
            progress("Rasterizing material texture", job_index + 1, total)

    total_conflicts = sum(int(np.count_nonzero(item.conflicts)) for item in results.values())
    if total_conflicts and not allow_conflicts:
        raise OverlapConflictError(
            f'Target UV overlaps created conflicting texture pixels: {total_conflicts}.',
            context={"conflict_pixels": total_conflicts},
        )
    for result in results.values():
        result.pixels, result.owner = dilate_values(
            result.pixels, result.coverage, result.owner, settings.padding, cancelled=cancelled
        )
    return results


def _supersampled(function, normal, source, target, corner_map, images, settings, progress, cancelled,allow_conflicts):
    samples = int(settings.supersampling)
    if samples not in (1, 2, 4): raise InputError('Supersampling: 1, 2 or 4.')
    if samples == 1:
        return function(source,target,corner_map,images,settings,progress=progress,cancelled=cancelled,allow_conflicts=allow_conflicts)
    # A pass owns one exact subpixel position. Checking overlaps between means
    # of different triangle fragments would falsely reject valid sharp textures.
    offsets = (np.arange(samples)+.5)/samples-.5
    combined = None; counts = {}; pass_index = 0
    for y in offsets:
        for x in offsets:
            def update(phase, done, total):
                if progress: progress(phase,pass_index*total+done,samples*samples*total)
            tiles = function(source,target,corner_map,images,replace(settings,supersampling=1,padding=0),
                             progress=update,cancelled=cancelled,_sample_offset=(x,y),allow_conflicts=allow_conflicts)
            if combined is None:
                combined = tiles
                for tile,item in combined.items():
                    counts[tile] = item.coverage.astype(np.uint8)
                    values = item.vectors if normal else item.pixels
                    values[~item.coverage] = 0
            else:
                for tile,item in tiles.items():
                    previous=combined[tile];active=item.coverage
                    values=previous.vectors if normal else previous.pixels
                    values[active]+=(item.vectors if normal else item.pixels)[active]
                    previous.owner[active & ~previous.coverage]=item.owner[active & ~previous.coverage]
                    previous.coverage |= active;counts[tile]+=active
                    previous.conflicts |= item.conflicts
            pass_index += 1
    for tile,item in combined.items():
        values=item.vectors if normal else item.pixels;valid=item.coverage
        values[valid]/=counts[tile][valid,None]
        if normal:
            values[valid]=normalize_vectors(values[valid]);values[~valid]=(0,0,1)
            item.vectors,item.owner=dilate_vectors(values,valid,item.owner,settings.padding,cancelled=cancelled)
        else:
            item.pixels,item.owner=dilate_values(values,valid,item.owner,settings.padding,cancelled=cancelled)
    return combined


def rasterize_transfer(source,target,corner_map,source_images,settings,*,progress=None,cancelled=None,allow_conflicts=False):
    return _supersampled(_rasterize_transfer_pass,True,source,target,corner_map,source_images,settings,progress,cancelled,allow_conflicts)


def rasterize_color_transfer(source,target,corner_map,source_images,settings,*,progress=None,cancelled=None,allow_conflicts=False):
    return _supersampled(_rasterize_color_pass,False,source,target,corner_map,source_images,settings,progress,cancelled,allow_conflicts)
