"""FastAPI server for the reliefkit web interface.

Two endpoints do the real work:

``POST /api/preview``
    Fetches a coarse DEM and returns the scaled heightfield as JSON so the
    browser can render a 3D preview. Deliberately small -- the point is to
    answer "does this region look good?" in a couple of seconds.

``POST /api/export``
    Builds the model at full resolution and streams back a binary STL.

``POST /api/tile-plan``
    Works out how a too-large model splits across a print bed. Touches no
    network at all -- the split follows from ground extent and scale alone --
    so the sidebar can recompute it on every keystroke.

``POST /api/export-tiles``
    Builds every tile and returns them as one ZIP.

Export is synchronous. A 1200-sample grid takes on the order of ten seconds,
which is tolerable for a single-user self-hosted tool and avoids dragging in a
job queue. If this ever grows past one user, that is the first thing to change.

Tiling makes that worse in proportion to the tile count -- each tile is its own
upstream fetch -- which is why ``MAX_WEB_TILES`` is well below the library's
own ceiling. Past that, the CLI is the right tool: it streams progress and no
proxy is waiting on it.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .. import __version__
from ..geo import BBox
from ..pipeline import build_model
from ..settings import ReliefSettings
from ..sources import SOURCES, SourceError
from ..stl import estimated_binary_size, write_binary_stl
from ..tiling import BedSpec, TileLayout, build_tiled_model, plan_layout, write_tiles_zip

STATIC = Path(__file__).parent / "static"

# Preview stays coarse on purpose: enough to judge the terrain, small enough
# to serialise as JSON and re-render on every settings tweak.
PREVIEW_GRID = 160
MAX_EXPORT_GRID = 2000

# One HTTP request has to survive one fetch per tile plus meshing all of them.
# Sixty-four is roughly where that stops being reasonable to hold a browser on.
MAX_WEB_TILES = 64

app = FastAPI(title="reliefkit", docs_url="/api/docs", redoc_url=None)


class ModelRequest(BaseModel):
    """Everything the sidebar can set."""

    west: float
    south: float
    east: float
    north: float

    source: str = "auto"
    square: bool = True
    grid: int = Field(default=800, ge=2, le=MAX_EXPORT_GRID)

    fmt: Literal["stl", "3mf"] = "stl"
    scale_mode: Literal["fit", "true"] = "fit"
    target_size_mm: float = Field(default=100.0, gt=0, le=2000)
    relief_height_mm: float = Field(default=12.0, gt=0, le=500)
    base_thickness_mm: float = Field(default=5.0, ge=0, le=200)
    scale_denominator: float | None = Field(default=None, gt=0)
    z_exaggeration: float = Field(default=1.0, gt=0, le=20)

    @field_validator("source")
    @classmethod
    def _known_source(cls, v: str) -> str:
        if v != "auto" and v not in SOURCES:
            raise ValueError(f"unknown source {v!r}")
        return v

    def to_bbox(self) -> BBox:
        try:
            return BBox(self.west, self.south, self.east, self.north)
        except ValueError as exc:
            raise HTTPException(422, f"Invalid region: {exc}") from exc

    def to_settings(self, grid: int) -> ReliefSettings:
        try:
            if self.scale_mode == "true":
                return ReliefSettings(
                    scale_mode="true",
                    scale_denominator=self.scale_denominator or 100_000,
                    z_exaggeration=self.z_exaggeration,
                    base_thickness_mm=self.base_thickness_mm,
                    max_grid=grid,
                )
            return ReliefSettings(
                scale_mode="fit",
                target_size_mm=self.target_size_mm,
                relief_height_mm=self.relief_height_mm,
                base_thickness_mm=self.base_thickness_mm,
                max_grid=grid,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


class TileRequest(ModelRequest):
    """A model request plus the machine it has to be cut up for.

    ``grid`` is inherited but means something different here: samples across a
    single *tile*, not across the whole region. A tile is what actually gets
    printed, so it is what the nozzle diameter has an opinion about.
    """

    bed_x_mm: float = Field(gt=0, le=2000)
    bed_y_mm: float = Field(gt=0, le=2000)
    bed_margin_mm: float = Field(default=0.0, ge=0, le=200)
    bed_rotate: bool = True

    # Both or neither; an explicit split overrides the one derived from the bed.
    tile_cols: int | None = Field(default=None, ge=1, le=MAX_WEB_TILES)
    tile_rows: int | None = Field(default=None, ge=1, le=MAX_WEB_TILES)

    def to_bed(self) -> BedSpec:
        try:
            return BedSpec(self.bed_x_mm, self.bed_y_mm, self.bed_margin_mm, allow_rotation=self.bed_rotate)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def forced_split(self) -> tuple[int, int] | None:
        if self.tile_cols and self.tile_rows:
            return self.tile_cols, self.tile_rows
        return None

    def to_layout(self) -> tuple[BBox, TileLayout]:
        bbox = self.to_bbox()
        if self.square:
            bbox = bbox.to_square()
        try:
            layout = plan_layout(bbox, self.to_settings(self.grid), self.to_bed(), tiles=self.forced_split())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return bbox, layout


def _build(req: ModelRequest, grid: int):
    """Shared fetch-and-mesh with upstream failures mapped to HTTP errors."""
    try:
        return build_model(req.to_bbox(), req.to_settings(grid), source=req.source, square=req.square)
    except SourceError as exc:
        # 502: we are fine, the upstream elevation service is not.
        raise HTTPException(502, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/health")
def health() -> dict:
    """Liveness probe for container orchestration.

    Deliberately touches nothing external -- it answers "is this process
    serving?", not "are the upstream elevation services up?". Conflating the
    two would have Docker restart a perfectly healthy container because USGS
    was having a bad afternoon.
    """
    return {"status": "ok", "version": __version__}


@app.get("/api/sources")
def list_sources() -> dict:
    return {
        "sources": [
            {"name": s.name, "licence": s.licence, "attribution": s.attribution}
            for s in SOURCES.values()
        ]
    }


@app.post("/api/preview")
def preview(req: ModelRequest) -> dict:
    """Coarse elevation grid in *metres*, plus the ground dimensions.

    Deliberately returns raw elevations rather than scaled millimetres. Scale
    settings then re-render in the browser with no round trip, which keeps the
    sliders instant and -- more importantly -- stops a dragged slider from
    firing a request per frame at a public USGS service. The client mirrors the
    scaling arithmetic in ``settings.py``; the server still recomputes it
    authoritatively when the STL is actually built.
    """
    bbox = req.to_bbox()
    if req.square:
        bbox = bbox.to_square()

    from ..sources import choose_source, get_source  # noqa: PLC0415

    try:
        src = choose_source(bbox) if req.source == "auto" else get_source(req.source)
        grid = src.fetch(bbox, PREVIEW_GRID).resample(PREVIEW_GRID)
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc

    rows, cols = grid.shape
    nodata_fraction = grid.nodata_filled / grid.elevations.size
    warnings = []
    if nodata_fraction > 0.01:
        warnings.append(f"{nodata_fraction:.1%} of this area had no elevation data and was flat-filled")
    if grid.elevation_range < 1.0:
        warnings.append(f"This area is nearly flat ({grid.elevation_range:.2f} m of relief)")

    return {
        "rows": rows,
        "cols": cols,
        # Decimetre precision: well under any printable resolution, ~30% smaller payload.
        "elevations_m": np.round(grid.elevations, 1).ravel().tolist(),
        "ground": {"width_m": round(bbox.width_m, 1), "height_m": round(bbox.height_m, 1)},
        "bbox": dict(zip(("west", "south", "east", "north"), bbox.as_tuple())),
        "elevation": {
            "min_m": round(grid.min_elevation, 1),
            "max_m": round(grid.max_elevation, 1),
            "range_m": round(grid.elevation_range, 1),
        },
        "source": src.name,
        "attribution": src.attribution,
        "warnings": warnings,
        "estimated": _estimate(req.grid, bbox.aspect),
    }


@app.post("/api/export")
def export(req: ModelRequest) -> Response:
    """Build at full resolution and return the STL as a download."""
    result = _build(req, req.grid)
    buffer = io.BytesIO()

    if req.fmt == "3mf":
        from ..threemf import write_3mf  # noqa: PLC0415

        write_3mf(result.solid, buffer, name=f"reliefkit {result.source_name}")
        media = "model/3mf"
    else:
        # write_binary_stl takes a path, so mirror its chunked write into memory.
        from ..stl import _write_binary  # noqa: PLC0415

        _write_binary(result.solid, buffer, f"reliefkit {result.source_name}")
        media = "model/stl"

    payload = buffer.getvalue()
    return Response(
        content=payload,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{_filename(result, req.fmt)}"',
            "X-Triangle-Count": str(result.solid.n_faces),
            "X-Attribution": result.attribution,
        },
    )


@app.post("/api/tile-plan")
def tile_plan(req: TileRequest) -> dict:
    """How the model splits across the bed, plus where the cuts land on the map.

    Deliberately offline. The split follows from the region's ground extent and
    the scale settings, neither of which needs elevation data, so this answers
    instantly and can be recomputed on every keystroke without touching a public
    elevation service.
    """
    bbox, layout = req.to_layout()

    # Interior cut lines only -- the region's own edges are not seams.
    lon_span, lat_span = bbox.east - bbox.west, bbox.north - bbox.south
    cut_lons = [bbox.west + lon_span * i / layout.cols for i in range(1, layout.cols)]
    cut_lats = [bbox.north - lat_span * i / layout.rows for i in range(1, layout.rows)]

    warnings = []
    if not layout.fits_bed:
        warnings.append(
            f"A {layout.tile_w_mm:.0f} × {layout.tile_h_mm:.0f} mm tile does not fit this bed."
        )
    if layout.n_tiles > MAX_WEB_TILES:
        warnings.append(
            f"{layout.n_tiles} tiles is more than this server will build in one request "
            f"(limit {MAX_WEB_TILES}). Use the reliefkit CLI for a job this size."
        )

    return {
        "cols": layout.cols,
        "rows": layout.rows,
        "n_tiles": layout.n_tiles,
        "tile_w_mm": round(layout.tile_w_mm, 2),
        "tile_h_mm": round(layout.tile_h_mm, 2),
        "total_w_mm": round(layout.total_w_mm, 2),
        "total_h_mm": round(layout.total_h_mm, 2),
        "rotated_on_bed": layout.rotated_on_bed,
        "fits_bed": layout.fits_bed,
        "exportable": layout.n_tiles <= MAX_WEB_TILES and layout.fits_bed,
        "max_tiles": MAX_WEB_TILES,
        "bbox": dict(zip(("west", "south", "east", "north"), bbox.as_tuple())),
        "cut_lons": cut_lons,
        "cut_lats": cut_lats,
        "estimated": layout.estimate(req.grid),
        "warnings": warnings,
    }


@app.post("/api/export-tiles")
def export_tiles(req: TileRequest) -> Response:
    """Build every tile and return them as one ZIP, with a manifest inside."""
    bbox, layout = req.to_layout()
    if layout.n_tiles > MAX_WEB_TILES:
        raise HTTPException(
            422,
            f"{layout.n_tiles} tiles exceeds this server's limit of {MAX_WEB_TILES}; "
            "use the reliefkit CLI for a job this size",
        )
    if not layout.fits_bed:
        raise HTTPException(
            422, f"a {layout.tile_w_mm:.0f} x {layout.tile_h_mm:.0f} mm tile does not fit the given bed"
        )

    try:
        model = build_tiled_model(
            bbox,
            req.to_bed(),
            settings=req.to_settings(req.grid),
            source=req.source,
            square=False,  # to_layout already squared it, if asked
            tiles=req.forced_split(),
        )
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(422, str(exc)) from exc

    buffer = io.BytesIO()
    write_tiles_zip(model, buffer, fmt=req.fmt)

    w, s, _, _ = bbox.as_tuple()
    stem = f"reliefkit_tiles_{s:.3f}_{w:.3f}".replace("-", "m").replace(".", "p")
    name = re.sub(r"[^A-Za-z0-9_]", "", stem) + ".zip"

    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Tile-Count": str(layout.n_tiles),
            "X-Triangle-Count": str(model.n_triangles),
            "X-Attribution": model.attribution,
        },
    )


def _estimate(grid: int, aspect: float) -> dict:
    """Triangle count and file size for the *export* grid, shown before committing."""
    if aspect >= 1.0:
        cols, rows = grid, max(2, round(grid / aspect))
    else:
        rows, cols = grid, max(2, round(grid * aspect))
    ring = 2 * rows + 2 * cols - 4
    faces = 2 * (rows - 1) * (cols - 1) + 3 * ring
    stl = estimated_binary_size(faces)
    # 3MF is indexed XML in a deflate stream. The ratio is stable across real
    # terrain meshes at about a fifth of the STL; measured, not derived.
    return {"triangles": faces, "bytes": stl, "bytes_3mf": round(stl * 0.21)}


def _filename(result, fmt: str = "stl") -> str:  # noqa: ANN001
    w, s, _, _ = result.grid.bbox.as_tuple()
    stem = f"reliefkit_{s:.3f}_{w:.3f}".replace("-", "m").replace(".", "p")
    return re.sub(r"[^A-Za-z0-9_]", "", stem) + f".{fmt}"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def run() -> None:
    """Console-script entry point for ``reliefkit-serve``."""
    import argparse

    import uvicorn

    p = argparse.ArgumentParser(prog="reliefkit-serve", description="Run the reliefkit web interface.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()

    print(f"reliefkit -> http://{args.host}:{args.port}")
    uvicorn.run("reliefkit.web.app:app", host=args.host, port=args.port, reload=args.reload)


__all__ = ["app", "run"]
