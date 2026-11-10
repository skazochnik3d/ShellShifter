# Adapted from the local Normal_Map_UV_Transfer numerical core.
"""Typed errors used across the transfer pipeline."""

from __future__ import annotations


class NmutError(RuntimeError):
    """Base class for user-facing NMUT failures."""

    code = "NMUT_ERROR"

    def __init__(self, message: str, *, context: dict | None = None) -> None:
        super().__init__(message)
        self.context = context or {}


class InputError(NmutError):
    code = "INPUT_ERROR"


class TopologyMismatchError(NmutError):
    code = "TOPOLOGY_MISMATCH"


class TriangulationMismatchError(NmutError):
    code = "TRIANGULATION_MISMATCH"


class AmbiguousMatchError(NmutError):
    code = "AMBIGUOUS_MATCH"


class DegenerateUvError(NmutError):
    code = "DEGENERATE_UV"


class UdimLayoutError(NmutError):
    code = "UDIM_LAYOUT"


class OverlapConflictError(NmutError):
    code = "OVERLAP_CONFLICT"


class TangentBasisError(NmutError):
    code = "TANGENT_BASIS"


class ImageIoError(NmutError):
    code = "IMAGE_IO"


class SnapshotError(NmutError):
    code = "SNAPSHOT_ERROR"


class CancelledError(NmutError):
    code = "CANCELLED"

