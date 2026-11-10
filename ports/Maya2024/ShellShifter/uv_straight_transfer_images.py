# Adapted from the local Normal_Map_UV_Transfer numerical core.
"""Maya MImage-backed Raw image reader/writer."""

from __future__ import annotations

import ctypes
import os
import tempfile
import struct
from pathlib import Path

from uv_straight_deps import np

from uv_straight_transfer_errors import ImageIoError
from uv_straight_transfer_models import ImageSet, ImageTile
from uv_straight_transfer_udim import discover_udim_paths


def image_bit_depth(path, *, allow_tiff_alpha=False):
    """Inspect storage precision before a decoder can reduce it to bytes."""
    path=Path(path);extension=path.suffix.lower()
    if extension not in ('.png','.tga','.tif','.tiff','.jpg','.jpeg'):
        raise ValueError('Supported formats: 8/16-bit PNG/TIFF, TGA and JPEG: '+str(path))
    if extension=='.png':
        with path.open('rb') as stream:header=stream.read(26)
        if len(header)!=26 or header[:8]!=b'\x89PNG\r\n\x1a\n':raise ValueError('Invalid PNG: '+str(path))
        if header[24] not in (8,16):raise ValueError('Only 8/16-bit PNG is supported: '+str(path))
        return int(header[24])
    if extension not in ('.tif','.tiff'):return 8
    with path.open('rb') as stream:
        header=stream.read(8)
        if len(header)!=8 or header[:2] not in (b'II',b'MM'):raise ValueError('Invalid TIFF: '+str(path))
        endian='<' if header[:2]==b'II' else '>'
        if struct.unpack(endian+'H',header[2:4])[0]!=42:raise ValueError('Only standard 8/16-bit TIFF is supported: '+str(path))
        stream.seek(struct.unpack(endian+'I',header[4:8])[0]);raw=stream.read(2)
        if len(raw)!=2:raise ValueError('Invalid TIFF directory: '+str(path))
        count=struct.unpack(endian+'H',raw)[0]
        if count>1024:raise ValueError('Invalid TIFF directory: '+str(path))
        entries=[stream.read(12) for _ in range(count)];bits=(1,);sample_format=(1,)
        for entry in entries:
            if len(entry)!=12:raise ValueError('Incomplete TIFF directory: '+str(path))
            tag,kind,n=struct.unpack(endian+'HHI',entry[:8])
            if tag==338 and n and not allow_tiff_alpha:
                raise ValueError('TIFF has extra channels: save as 8/16-bit PNG to preserve RGB and alpha: '+str(path))
            if tag not in (258,339):continue
            if kind!=3 or not 1<=n<=16:raise ValueError('Unknown TIFF bit depth: '+str(path))
            if n*2<=4:raw=entry[8:8+n*2]
            else:stream.seek(struct.unpack(endian+'I',entry[8:])[0]);raw=stream.read(n*2)
            if len(raw)!=n*2:raise ValueError('Incomplete TIFF data: '+str(path))
            values=struct.unpack(endian+'H'*n,raw)
            if tag==258:bits=values
            else:sample_format=values
        if any(v!=1 for v in sample_format):raise ValueError('Float/signed TIFF is not supported yet; use 8/16-bit integer channels: '+str(path))
        if len(set(bits))!=1 or bits[0] not in (8,16):raise ValueError('Only TIFF with 8/16-bit integer channels is supported: '+str(path))
        return int(bits[0])


class MImageBackend:
    """Read images as float RGBA and write formats verified through MImage."""

    _FORMAT_ALIASES = {
        ".tiff": "tif",
        ".tif": "tif",
        ".tga": "tga",
        ".png": "png",
    }

    @staticmethod
    def _api():
        try:
            from maya.api import OpenMaya as om
        except ImportError as exc:
            raise ImageIoError('Image processing must run in Maya or mayapy.') from exc
        return om

    def read_tile(self, path: str, udim: int = 1001) -> ImageTile:
        om = self._api()
        image = om.MImage()
        try:
            # The verified backend supports 8-bit PNG/TIFF/TGA. Some Maya image
            # readers return kFloat data in 0..1, while Maya-written PNG can be
            # returned in 0..255. Reading bytes and scaling explicitly is stable.
            image.readFromFile(str(path), om.MImage.kByte)
            width, height = image.getSize()
            pointer = image.pixels()
            bytes_per_channel = ctypes.sizeof(ctypes.c_ubyte)
            bytes_per_pixel = int(image.depth())
            if bytes_per_pixel <= 0 or bytes_per_pixel % bytes_per_channel:
                raise ImageIoError(
                    f'Maya returned an invalid image depth: {bytes_per_pixel} bytes per pixel.'
                )
            channel_count = bytes_per_pixel // bytes_per_channel
            if channel_count not in (2, 3, 4):
                raise ImageIoError(
                    f'A normal map must have 2, 3 or 4 channels; got: {channel_count}.'
                )
            byte_count = int(width) * int(height) * bytes_per_pixel
            payload = ctypes.string_at(pointer, byte_count)
            source_pixels = (
                np.frombuffer(payload, dtype=np.uint8)
                .copy()
                .reshape(height, width, channel_count)
                .astype(np.float32)
                / 255.0
            )
            # MImage exposes scanlines bottom-to-top, while NumPy image code and
            # the rasterizer use conventional top-to-bottom row order.
            source_pixels = np.flip(source_pixels, axis=0).copy()
            # MImage exposes RGBA pixels with RGB premultiplied by alpha. The
            # transfer pipeline works with straight-alpha texture values, so
            # undo the premultiplication before resampling. This is essential
            # for opacity-bearing albedo/mask textures; otherwise every
            # read/write cycle darkens RGB once more.
            if channel_count == 4:
                alpha = source_pixels[..., 3:4]
                nonzero = alpha[..., 0] > (0.5 / 255.0)
                source_pixels[..., :3][nonzero] /= alpha[nonzero]
                source_pixels[..., :3] = np.clip(source_pixels[..., :3], 0.0, 1.0)
            # Normalize every supported source format to RGBA. Many production
            # PNG/TGA normal maps may be RGB; normalize them to one RGBA contract.
            pixels = np.ones((height, width, 4), dtype=np.float32)
            pixels[..., :channel_count] = source_pixels
        except Exception as exc:
            if isinstance(exc, ImageIoError):
                raise
            raise ImageIoError(
                f'Could not read image: {path}\nReason: {exc}'
            ) from exc
        finally:
            try:
                image.release()
            except Exception:
                pass
        if not np.all(np.isfinite(pixels)):
            raise ImageIoError(f'The image contains NaN or infinite values: {path}')
        return ImageTile(udim=udim, pixels=pixels, path=str(path))

    def read(self, pattern: str) -> ImageSet:
        paths = discover_udim_paths(pattern)
        return ImageSet({udim: self.read_tile(path, udim) for udim, path in paths.items()})

    def write(self, path: str, pixels: np.ndarray) -> str:
        om = self._api()
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        extension = destination.suffix.lower()
        output_format = self._FORMAT_ALIASES.get(extension)
        if not output_format:
            raise ImageIoError(
                f'Unsupported output file extension: {extension}. Use PNG, TIFF or TGA. Writing float EXR requires an additional module.'
            )
        values = np.asarray(pixels, dtype=np.float32)
        if values.ndim != 3 or values.shape[2] not in (2, 3, 4):
            raise ImageIoError('The output array must have shape H×W×2, H×W×3 or H×W×4.')
        rgba = np.ones((values.shape[0], values.shape[1], 4), dtype=np.float32)
        rgba[..., : values.shape[2]] = values
        image = om.MImage()
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{destination.stem}.nmut-", suffix=extension, dir=str(destination.parent)
        )
        os.close(fd)
        try:
            # Convert our top-to-bottom NumPy rows to MImage's bottom-to-top
            # memory convention before handing off the buffer.
            maya_rows = np.flip(rgba, axis=0)
            contiguous = np.ascontiguousarray(
                np.rint(np.clip(maya_rows, 0.0, 1.0) * 255.0), dtype=np.uint8
            )
            image.setPixels(contiguous.ctypes.data, contiguous.shape[1], contiguous.shape[0])
            image.writeToFile(temp_path, output_format)
            if not Path(temp_path).is_file() or Path(temp_path).stat().st_size <= 0:
                raise ImageIoError(f'Maya did not create a valid image: {temp_path}')
            os.replace(temp_path, destination)
        except Exception as exc:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass
            if isinstance(exc, ImageIoError):
                raise
            raise ImageIoError(f'Could not write image: {destination}') from exc
        finally:
            try:
                image.release()
            except Exception:
                pass
        return str(destination)
