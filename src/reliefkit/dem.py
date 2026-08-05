"""Elevation grids: the hand-off format between a data source and the mesher."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geo import BBox


@dataclass(frozen=True)
class DEMGrid:
    """A north-up regular grid of elevations in metres.

    ``elevations[0]`` is the northernmost row and ``elevations[:, 0]`` the
    westernmost column, matching how GeoTIFFs are normally laid out.
    """

    elevations: np.ndarray  # (rows, cols) float64, metres, no NaN
    bbox: BBox
    source: str = "unknown"
    nodata_filled: int = 0

    def __post_init__(self) -> None:
        if self.elevations.ndim != 2:
            raise ValueError(f"elevations must be 2-D, got shape {self.elevations.shape}")
        if min(self.elevations.shape) < 2:
            raise ValueError(f"grid must be at least 2x2, got {self.elevations.shape}")

    @property
    def shape(self) -> tuple[int, int]:
        return self.elevations.shape  # type: ignore[return-value]

    @property
    def min_elevation(self) -> float:
        return float(self.elevations.min())

    @property
    def max_elevation(self) -> float:
        return float(self.elevations.max())

    @property
    def elevation_range(self) -> float:
        return self.max_elevation - self.min_elevation

    def resample(self, max_dim: int) -> DEMGrid:
        """Decimate so neither dimension exceeds ``max_dim``.

        Uses index striding via linear interpolation on each axis, which is
        adequate here because the mesh is a low-pass artefact anyway -- a 3D
        printer resolves far less detail than a 1 m DEM carries.
        """
        rows, cols = self.shape
        if max(rows, cols) <= max_dim:
            return self
        scale = max_dim / max(rows, cols)
        new_rows = max(2, int(round(rows * scale)))
        new_cols = max(2, int(round(cols * scale)))

        row_idx = np.linspace(0, rows - 1, new_rows)
        col_idx = np.linspace(0, cols - 1, new_cols)
        # Separable linear interpolation: rows first, then columns.
        tmp = _interp_axis(self.elevations, row_idx, axis=0)
        out = _interp_axis(tmp, col_idx, axis=1)
        return DEMGrid(out, self.bbox, self.source, self.nodata_filled)

    def describe(self) -> str:
        rows, cols = self.shape
        return (
            f"{rows}x{cols} grid from {self.source}, "
            f"elevation {self.min_elevation:.1f}..{self.max_elevation:.1f} m "
            f"(range {self.elevation_range:.1f} m)"
            + (f", {self.nodata_filled} nodata cells filled" if self.nodata_filled else "")
        )


def _interp_axis(arr: np.ndarray, idx: np.ndarray, axis: int) -> np.ndarray:
    """Linear interpolation of ``arr`` at fractional positions ``idx``."""
    arr = np.moveaxis(arr, axis, 0)
    lo = np.floor(idx).astype(int)
    hi = np.minimum(lo + 1, arr.shape[0] - 1)
    w = (idx - lo).reshape((-1,) + (1,) * (arr.ndim - 1))
    out = arr[lo] * (1.0 - w) + arr[hi] * w
    return np.moveaxis(out, 0, axis)


def fill_nodata(values: np.ndarray, nodata: float | None) -> tuple[np.ndarray, int]:
    """Replace nodata/NaN with the mean of the valid cells.

    Returns the filled array and the number of cells replaced. A flat fill is
    deliberately crude -- it is a guard against unprintable NaN geometry, not a
    substitute for choosing a source with real coverage. Callers should warn the
    user when the count is a meaningful fraction of the grid.
    """
    values = np.asarray(values, dtype=np.float64)
    bad = ~np.isfinite(values)
    if nodata is not None:
        bad |= np.isclose(values, nodata)
        # Sources commonly signal voids with large sentinels as well.
        bad |= values <= -9998.0
    n_bad = int(bad.sum())
    if n_bad == 0:
        return values, 0
    if n_bad == values.size:
        raise ValueError("elevation tile is entirely nodata; try a different source or region")
    filled = values.copy()
    filled[bad] = float(values[~bad].mean())
    return filled, n_bad
