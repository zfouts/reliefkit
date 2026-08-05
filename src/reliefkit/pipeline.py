"""End-to-end: bounding box and settings in, printable solid out."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .dem import DEMGrid
from .geo import BBox
from .mesh import Solid, build_solid, signed_volume_mm3
from .settings import ReliefSettings
from .sources import DEMSource, choose_source, get_source
from .stl import estimated_binary_size, write_binary_stl
from .threemf import write_3mf


@dataclass(frozen=True)
class ModelResult:
    """A built model plus the numbers worth showing the user."""

    solid: Solid
    grid: DEMGrid
    settings: ReliefSettings
    source_name: str
    attribution: str
    size_mm: tuple[float, float, float]
    scale_denominator: float
    vertical_exaggeration: float

    @property
    def warnings(self) -> list[str]:
        out = []
        frac = self.grid.nodata_filled / self.grid.elevations.size
        if frac > 0.01:
            out.append(f"{frac:.1%} of the elevation grid was nodata and got flat-filled")
        if self.grid.elevation_range < 1.0:
            out.append(f"terrain is nearly flat ({self.grid.elevation_range:.2f} m of relief)")
        if self.solid.n_faces > 5_000_000:
            out.append(f"{self.solid.n_faces:,} triangles is heavy for most slicers; consider a smaller --grid")
        return out

    def advice(self, tool_mm: float = 0.4):
        """How this model's mesh density compares with a given tool width."""
        from .resolution import advise

        return advise(max(self.size_mm[0], self.size_mm[1]), self.settings.max_grid, tool_mm)

    def summary(self) -> str:
        x, y, z = self.size_mm
        lines = [
            f"source     : {self.source_name} ({self.grid.describe()})",
            f"model size : {x:.1f} x {y:.1f} x {z:.1f} mm",
            f"scale      : 1:{self.scale_denominator:,.0f} horizontal, {self.vertical_exaggeration:.2f}x vertical",
            f"mesh       : {self.solid.n_faces:,} triangles, {self.solid.n_vertices:,} vertices",
            f"attribution: {self.attribution}",
        ]
        return "\n".join(lines)


def build_model(
    bbox: BBox,
    settings: ReliefSettings | None = None,
    source: str | DEMSource = "auto",
    square: bool = False,
) -> ModelResult:
    """Fetch elevation for ``bbox`` and mesh it into a printable solid."""
    settings = settings or ReliefSettings()
    if square:
        bbox = bbox.to_square()

    if isinstance(source, str):
        src = choose_source(bbox) if source == "auto" else get_source(source)
    else:
        src = source

    grid = src.fetch(bbox, settings.max_grid).resample(settings.max_grid)
    return mesh_from_grid(grid, settings, src)


def mesh_from_grid(grid: DEMGrid, settings: ReliefSettings, src: DEMSource) -> ModelResult:
    """Scale an elevation grid into millimetres and build the solid.

    Split out from :func:`build_model` so callers that already hold a DEM (an
    uploaded GeoTIFF, a cached tile) can reuse the scaling and meshing rules.
    """
    width_m, height_m = grid.bbox.width_m, grid.bbox.height_m
    rows, cols = grid.shape

    xy_mm_per_m = settings.horizontal_mm_per_m(width_m, height_m)
    z_mm_per_m = settings.vertical_mm_per_m(grid.elevation_range, width_m, height_m)

    heights_mm = (grid.elevations - grid.min_elevation) * z_mm_per_m
    # Row 0 is north and build_solid expects the same convention, so no flip.
    heights_mm = np.ascontiguousarray(heights_mm, dtype=np.float64)

    model_w = width_m * xy_mm_per_m
    model_h = height_m * xy_mm_per_m
    dx = model_w / (cols - 1)
    dy = model_h / (rows - 1)

    solid = build_solid(heights_mm, dx, dy, settings.base_thickness_mm)
    if signed_volume_mm3(solid) <= 0:
        raise RuntimeError("mesh has inverted winding; this is a bug in reliefkit")

    z_total = settings.base_thickness_mm + float(heights_mm.max())
    exaggeration = (z_mm_per_m / xy_mm_per_m) if xy_mm_per_m else 1.0

    return ModelResult(
        solid=solid,
        grid=grid,
        settings=settings,
        source_name=src.name,
        attribution=src.attribution,
        size_mm=(model_w, model_h, z_total),
        scale_denominator=1000.0 / xy_mm_per_m,
        vertical_exaggeration=exaggeration,
    )


def write_model(result: ModelResult, out_path: str | Path, fmt: str = "auto") -> Path:
    """Write a built model as STL or 3MF.

    ``auto`` picks from the file extension. 3MF references an indexed vertex
    list inside a ZIP, so it lands roughly 3-4x smaller than the same mesh as
    binary STL, which repeats every vertex three times and cannot compress.
    """
    out_path = Path(out_path)
    if fmt == "auto":
        fmt = "3mf" if out_path.suffix.lower() == ".3mf" else "stl"
    if fmt == "3mf":
        return write_3mf(result.solid, out_path, name=f"reliefkit {result.source_name}")
    if fmt == "stl":
        return write_binary_stl(result.solid, out_path, header=f"reliefkit {result.source_name}")
    raise ValueError(f"unknown format {fmt!r}; expected 'stl', '3mf' or 'auto'")


def generate_stl(
    bbox: BBox,
    out_path: str | Path,
    settings: ReliefSettings | None = None,
    source: str | DEMSource = "auto",
    square: bool = False,
    fmt: str = "auto",
) -> ModelResult:
    """Build a model and write it to disk (STL by default, 3MF by extension)."""
    result = build_model(bbox, settings=settings, source=source, square=square)
    write_model(result, out_path, fmt)
    return result


__all__ = [
    "ModelResult",
    "build_model",
    "mesh_from_grid",
    "generate_stl",
    "write_model",
    "estimated_binary_size",
]
