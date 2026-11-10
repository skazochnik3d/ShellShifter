"""Resize texture channels in high precision before exporting TIFF, TGA or PNG."""
from functools import lru_cache
import os
from pathlib import Path
import struct
import tempfile

import numpy as np
from PySide6 import QtGui

from uv_straight_texture_match import parse_name
from uv_straight_transfer_images import image_bit_depth


EXTENSIONS = {'.tif', '.tiff', '.tga', '.png'}
HIGH_PRECISION = {
    QtGui.QImage.Format_Grayscale16, QtGui.QImage.Format_RGBX64,
    QtGui.QImage.Format_RGBA64, QtGui.QImage.Format_RGBA64_Premultiplied,
}


def _read_tga(path):
    """Read the common uncompressed variants, including grayscale and alpha."""
    with path.open('rb') as stream:
        header = stream.read(18)
        if len(header) != 18:
            raise ValueError('Incomplete TGA header.')
        width, height, depth, descriptor = struct.unpack_from('<HHBB', header, 12)
        image_type = header[2]
        if header[1] or (image_type, depth) not in ((2, 24), (2, 32), (3, 8), (3, 16)):
            return None
        if not width or not height:
            raise ValueError('Invalid TGA dimensions.')
        stream.seek(header[0], 1)
        channels = depth // 8
        data = stream.read(width * height * channels)
    if len(data) != width * height * channels:
        raise ValueError('Incomplete TGA pixels.')
    pixels = np.frombuffer(data, dtype=np.uint8).reshape(height, width, channels)
    if not descriptor & 32:
        pixels = pixels[::-1]
    if descriptor & 16:
        pixels = pixels[:, ::-1]
    if image_type == 2:
        return pixels[..., [2, 1, 0, 3] if channels == 4 else [2, 1, 0]]
    return pixels[..., [0, 0, 0, 1] if channels == 2 else [0, 0, 0]]


def _read(path):
    depth = image_bit_depth(path, allow_tiff_alpha=True)
    if path.suffix.lower() == '.tga':
        pixels = _read_tga(path)
        if pixels is not None:
            return pixels, depth, QtGui.QColorSpace()
    image = QtGui.QImage(str(path))
    if image.isNull():
        raise ValueError('Could not decode texture.')
    if depth == 16 and image.format() not in HIGH_PRECISION:
        raise ValueError('The image decoder returned 8-bit pixels for a 16-bit file.')
    has_alpha = image.hasAlphaChannel()
    if path.suffix.lower() == '.tga':
        with path.open('rb') as stream:
            header = stream.read(18)
        has_alpha = len(header) == 18 and header[16] in (16, 32) and bool(header[17] & 15)
        if has_alpha and not image.hasAlphaChannel():
            raise ValueError('The image decoder could not read the TGA alpha channel.')
    color_space = image.colorSpace()
    image = image.convertToFormat(QtGui.QImage.Format_RGBA64 if depth == 16 else QtGui.QImage.Format_RGBA8888)
    dtype = np.uint16 if depth == 16 else np.uint8
    item_size = np.dtype(dtype).itemsize
    values = np.frombuffer(image.constBits(), dtype=dtype)
    pixels = values.reshape(image.height(), image.bytesPerLine() // item_size)[:, :image.width() * 4]
    return pixels.reshape(image.height(), image.width(), 4)[..., :4 if has_alpha else 3].copy(), depth, color_space


def _resample_axis(pixels, size, axis, sharpness, multiplier=1.):
    """Separable antialiased cubic filter with edge extension."""
    source = np.moveaxis(pixels, axis, 0)
    if len(source) == size:
        return pixels.astype(np.float32) * np.float32(multiplier)
    ratio = len(source) / size
    scale = max(1., ratio)
    centers = (np.arange(size) + .5) * ratio - .5
    first = np.floor(centers).astype(np.intp)
    radius = int(np.ceil(2. * scale))
    offsets = np.arange(-radius + 1, radius + 1)
    indices = first[None, :] + offsets[:, None]
    distance = np.abs((indices - centers) / scale)
    a = sharpness
    weights = np.where(distance < 1., ((a + 2.) * distance - (a + 3.)) * distance**2 + 1.,
                       np.where(distance < 2., ((a * distance - 5. * a) * distance + 8. * a)
                                * distance - 4. * a, 0.))
    weights /= weights.sum(axis=0)
    if size >= 16 and len(source) % size == 0:
        # Integer reductions reuse the same weights at every output position.
        # Strided views avoid gathering a full image for each filter tap.
        return _resample_integer(pixels, size, axis, indices[:, 0],
                                 weights[:, 0] * multiplier, radius)
    result = np.zeros((size, *source.shape[1:]), dtype=np.float32)
    indices = np.clip(indices, 0, len(source) - 1)
    for rows, weight in zip(indices, weights):
        if np.any(weight):
            result += source[rows] * (weight * multiplier).astype(np.float32)[:, None, None]
    return np.moveaxis(result, 0, axis)


def _resample_integer(pixels, size, axis, positions, weights, radius):
    step = pixels.shape[axis] // size
    padding = [(0, 0)] * pixels.ndim
    padding[axis] = (radius, radius)
    source = np.moveaxis(np.pad(pixels, padding, mode='edge'), axis, 0)
    shape = list(pixels.shape)
    shape[axis] = size
    result = np.zeros(shape, dtype=np.float32)
    destination = np.moveaxis(result, axis, 0)
    taps = [(slice(start, start + step * size, step), weight)
            for start, weight in zip(positions + radius, weights.astype(np.float32)) if weight]
    # Process about a MiB of output at a time so successive taps reuse cache.
    stripe = max(1, (1 << 18) // (size * source.shape[2]))
    buffer = np.empty_like(destination[:, :stripe])
    for start in range(0, source.shape[1], stripe):
        stop = start + stripe
        output = destination[:, start:stop]
        work = buffer[:, :output.shape[1]]
        for rows, weight in taps:
            np.multiply(source[rows, start:stop], weight, out=work)
            np.add(output, work, out=output)
    return result


def _resize(pixels, width, height, depth, normal):
    multiplier = 1. / ((1 << depth) - 1)
    reducing = width < pixels.shape[1] or height < pixels.shape[0]
    if (width, height) == (pixels.shape[1], pixels.shape[0]):
        return pixels.astype(np.float32) * np.float32(multiplier)
    if not normal or not reducing or pixels.shape[2] == 3:
        sharpness = -1.85 if normal and reducing else -.5
        rows = _resample_axis(pixels, width, 1, sharpness, multiplier)
        return _resample_axis(rows, height, 0, sharpness)
    # The normal-map filter is sharper than the alpha filter. Treat mask data
    # independently so transparent RGB values cannot alter opacity or vice versa.
    result = np.empty((height, width, 4), dtype=np.float32)
    for channels, sharpness in ((slice(0, 3), -1.85), (slice(3, 4), -.5)):
        rows = _resample_axis(pixels[..., channels], width, 1, sharpness, multiplier)
        result[..., channels] = _resample_axis(rows, height, 0, sharpness)
    return result


def _thresholds(shape, seed):
    """Use the same high-frequency quantization thresholds as the NordLog test."""
    noise = np.random.default_rng(seed).standard_normal(shape, dtype=np.float32)
    high = noise.copy()
    for axis in (0, 1):
        high -= (np.roll(noise, 1, axis) + np.roll(noise, -1, axis)) * .25
    order = np.argsort(high, axis=None)
    thresholds = np.empty(noise.size, np.float32)
    thresholds[order] = (np.arange(noise.size, dtype=np.float32) + .5) / noise.size
    return thresholds.reshape(shape)


def _quantize(values, depth, independent_rgb, thresholds):
    maximum = (1 << depth) - 1
    np.clip(values, 0., 1., out=values)
    values *= maximum
    if depth == 16:
        return np.rint(values, out=values).astype(np.uint16)
    result = np.empty(values.shape, dtype=np.uint8)
    # A common threshold keeps grayscale maps neutral. Normal vectors use
    # independent thresholds to match the tested NordLog conversion.
    threshold = thresholds(values.shape[:2], 1420)
    for channel in range(3):
        if independent_rgb and channel:
            threshold = thresholds(values.shape[:2], 1420 + channel)
        signal = values[..., channel]
        lower = np.floor(signal)
        result[..., channel] = lower + ((signal - lower) > threshold)
    if values.shape[2] == 4:
        result[..., 3] = np.rint(values[..., 3])
    return result


def _write(path, pixels, depth, color_space):
    height, width, channels = pixels.shape
    fmt = path.suffix[1:].lower()
    descriptor, temp = tempfile.mkstemp(prefix='.ss_convert_', suffix=path.suffix, dir=path.parent)
    os.close(descriptor)
    temporary = Path(temp)
    try:
        if fmt == 'tga':
            header = struct.pack('<BBBHHBHHHHBB', 0, 0, 2, 0, 0, 0, 0, 0,
                                 width, height, channels * 8, 8 if channels == 4 else 0)
            with temporary.open('wb') as stream:
                stream.write(header)
                stream.write(pixels[::-1, :, [2, 1, 0, 3] if channels == 4 else [2, 1, 0]].tobytes())
        else:
            if depth == 16 and channels == 3:
                rgbx = np.full((height, width, 4), 65535, dtype=np.uint16)
                rgbx[..., :3] = pixels
                pixels = rgbx
            image_format = {
                (8, 3): QtGui.QImage.Format_RGB888, (8, 4): QtGui.QImage.Format_RGBA8888,
                (16, 3): QtGui.QImage.Format_RGBX64, (16, 4): QtGui.QImage.Format_RGBA64,
            }[depth, channels]
            # A 4-byte aligned scanline is needed even for odd-width RGB output.
            if depth == 8 and channels == 3:
                row_bytes = width * 3
                stride = (row_bytes + 3) & ~3
                buffer = np.zeros((height, stride), dtype=np.uint8)
                buffer[:, :row_bytes] = pixels.reshape(height, row_bytes)
            else:
                buffer = np.ascontiguousarray(pixels)
            image = QtGui.QImage(buffer.data, width, height, buffer.strides[0], image_format)
            if color_space.isValid():
                image.setColorSpace(color_space)
            if not image.save(str(temporary), fmt.upper()):
                raise ValueError('Could not write {} output.'.format(fmt.upper()))
        if os.name == 'nt':
            temporary.rename(path)  # Windows rename rejects an existing destination.
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def convert(path, directory, size, fmt, depth, *, thresholds=_thresholds):
    """Convert one file, retaining aspect ratio and the source alpha channel."""
    source = Path(path)
    if source.suffix.lower() not in EXTENSIONS:
        raise ValueError('Choose TIFF, TGA or PNG files.')
    if fmt not in ('tif', 'tga', 'png') or depth not in (8, 16) or (fmt == 'tga' and depth != 8):
        raise ValueError('TGA supports 8 bits per channel. Use TIFF or PNG for 16-bit output.')
    target = Path(directory) / (source.stem + '.' + fmt)
    if target.exists():
        raise ValueError('Output already exists: ' + str(target))
    pixels, input_depth, color_space = _read(source)
    height, width = pixels.shape[:2]
    if size:
        factor = size / max(width, height)
        width, height = max(1, round(width * factor)), max(1, round(height * factor))
    if not 1 <= max(width, height) <= 32768:
        raise ValueError('The output resolution is out of range.')
    normal = parse_name(source)['key'] == 'normal'
    if (width, height) == (pixels.shape[1], pixels.shape[0]) and depth == input_depth:
        result = np.ascontiguousarray(pixels)
    else:
        values = _resize(pixels, width, height, input_depth, normal)
        result = _quantize(values, depth, normal, thresholds)
    _write(target, result, depth, color_space)
    return target


def convert_batch(paths, directory, size, fmt, depth):
    """Reuse up to three dither fields per batch, capped at 48 MiB."""
    cached = lru_cache(maxsize=3)(_thresholds)

    def thresholds(shape, seed):
        height, width = shape
        if height * width <= 2048 ** 2:
            return cached(shape, seed)
        return _thresholds(shape, seed)

    count, errors = 0, []
    for path in paths:
        try:
            convert(path, directory, size, fmt, depth, thresholds=thresholds)
            count += 1
        except Exception as error:
            errors.append('{}: {}'.format(Path(path).name, error))
    return count, errors
