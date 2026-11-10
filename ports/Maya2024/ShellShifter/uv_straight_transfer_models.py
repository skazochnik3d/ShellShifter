# Adapted from the local Normal_Map_UV_Transfer numerical core.
"""Data contracts shared by Maya, core, image, and UI layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from uv_straight_deps import np


ProgressCallback = Callable[[str, int, int], None]
CancelCallback = Callable[[], bool]


@dataclass(frozen=True)
class UvSetSnapshot:
    name: str
    uvs: np.ndarray
    face_uv_counts: np.ndarray
    corner_uv_ids: np.ndarray
    uv_shell_ids: np.ndarray
    shell_count: int

    @property
    def corner_shell_ids(self) -> np.ndarray:
        result = np.full(self.corner_uv_ids.shape, -1, dtype=np.int32)
        valid = self.corner_uv_ids >= 0
        result[valid] = self.uv_shell_ids[self.corner_uv_ids[valid]]
        return result


@dataclass(frozen=True)
class MeshSnapshot:
    name: str
    uuid: str
    points: np.ndarray
    face_counts: np.ndarray
    face_vertices: np.ndarray
    triangle_faces: np.ndarray
    triangle_corners: np.ndarray
    normals: np.ndarray
    frames: np.ndarray
    uv_set: UvSetSnapshot
    topology_hash: str
    geometry_hash: str
    triangulation_hash: str
    normal_hash: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def vertex_count(self) -> int:
        return int(self.points.shape[0])

    @property
    def face_count(self) -> int:
        return int(self.face_counts.size)

    @property
    def corner_count(self) -> int:
        return int(self.face_vertices.size)

    @property
    def triangle_count(self) -> int:
        return int(self.triangle_corners.shape[0])

    @property
    def face_offsets(self) -> np.ndarray:
        return np.concatenate(
            (np.array([0], dtype=np.int64), np.cumsum(self.face_counts, dtype=np.int64))
        )


@dataclass(frozen=True)
class CornerMap:
    target_to_source: np.ndarray
    method: str
    confidence: str
    face_map: np.ndarray
    vertex_map: np.ndarray
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShellRelation:
    source_shells: tuple[int, ...]
    target_shells: tuple[int, ...]
    relation: str
    corner_count: int


@dataclass(frozen=True)
class ImageTile:
    udim: int
    pixels: np.ndarray
    path: str = ""
    bit_depth: int = 8

    @property
    def width(self) -> int:
        return int(self.pixels.shape[1])

    @property
    def height(self) -> int:
        return int(self.pixels.shape[0])


@dataclass(frozen=True)
class ImageSet:
    tiles: Mapping[int, ImageTile]
    repeat: bool = False

    def require(self, udim: int) -> ImageTile:
        from uv_straight_transfer_errors import ImageIoError

        try:
            return self.tiles[udim]
        except KeyError as exc:
            raise ImageIoError(
                f'Missing tile {udim} of the source image.', context={"udim": udim}
            ) from exc


@dataclass(frozen=True)
class MaterialTexture:
    node: str
    path: str
    color_space: str = ""
    uv_tiling_mode: int = 0
    is_normal: bool = False


@dataclass(frozen=True)
class TransferSettings:
    width: int
    height: int
    input_green_sign: int = 1
    output_green_sign: int = 1
    input_bc5: bool = False
    output_bc5: bool = False
    padding: int = 8
    supersampling: int = 1
    overlap_tolerance_degrees: float = 1.0
    background: tuple[float, float, float] = (0.5, 0.5, 1.0)
    output_bit_depth: int = 0  # Per-texture automatic depth; explicit 8/16 also supported.


@dataclass(frozen=True)
class TransferRequest:
    source_mesh: str
    target_mesh: str
    source_uv_set: str
    target_uv_set: str
    source_map: str
    output_map: str
    settings: TransferSettings
    snapshot_path: str = ""
    match_mode: str = "auto"
    transfer_material_textures: bool = False


@dataclass
class RasterTileResult:
    udim: int
    vectors: np.ndarray
    coverage: np.ndarray
    owner: np.ndarray
    conflicts: np.ndarray


@dataclass
class ColorRasterTileResult:
    udim: int
    pixels: np.ndarray
    coverage: np.ndarray
    owner: np.ndarray
    conflicts: np.ndarray


@dataclass(frozen=True)
class TransferResult:
    output_paths: tuple[str, ...]
    report_path: str
    report: Mapping[str, object]


def default_report_path(output_path: str) -> str:
    path = Path(output_path)
    if "<UDIM>" in path.name:
        name = path.name.replace("<UDIM>", "report")
        return str(path.with_name(name).with_suffix(".json"))
    return str(path.with_suffix(path.suffix + ".nmut.json"))
