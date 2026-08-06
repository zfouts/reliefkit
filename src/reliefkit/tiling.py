"""Cutting one oversized model into print-bed-sized pieces.

A 250 km region rendered at 1000 mm across does not fit on a 200 mm bed. This
module splits it into a 5x5 grid of 200 mm tiles that butt together into the
finished object. It sits *above* the mesher: every tile is an ordinary
:func:`~reliefkit.mesh.build_solid` result, independently watertight and
independently printable.

Three rules make the pieces actually assemble.

**One elevation datum for the whole region.**
    Heights are measured from the region's minimum and scaled by the region's
    range. Scaling each tile against its own local min/max -- the obvious
    mistake -- would step every seam by the difference between two local minima.

**Uniform in degrees, linear degrees to millimetres.**
    Tiles divide the box evenly in longitude and latitude, and degrees map
    linearly to millimetres. That is the same plate-carree mapping the
    single-model path already applies to one box, and it is what makes every
    tile *exactly* the same size in millimetres. Dividing by ground metres
    instead would make northern rows narrower than southern ones, and the
    assembled sheet would not be a rectangle.

**A shared lattice, then reconciled seams.**
    Each tile is resampled to an identical sample count, so a tile's east edge
    column corresponds one-to-one with its neighbour's west edge column. Those
    two lines are then averaged and written back into both tiles, which makes
    the shared edge identical to the last bit.

    The averaging is not cosmetic. Sources hand back pixel-centred rasters, so
    a tile's edge column is sampled half a pixel inside the boundary it is
    supposed to sit on, and its neighbour's is half a pixel inside from the
    other direction. The two disagree by whatever the terrain does across one
    sample -- below what the tool can reproduce, but a visible ridge along
    every seam if left alone. Averaging removes it and moves each edge by at
    most half a sample of terrain.

Nothing here cuts joinery into the mesh. Tiles butt flat against each other and
are glued; the base is a plane on every one of them.
"""

from __future__ import annotations

import io
import json
import math
import zipfile
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .dem import DEMGrid
from .geo import BBox
from .mesh import Solid, build_solid, signed_volume_mm3
from .settings import ReliefSettings
from .sources import DEMSource, choose_source, get_source
from .stl import estimated_binary_size

# A sanity backstop, not a considered limit. Every tile is a separate upstream
# fetch and a separate mesh, so a four-figure split is a mistake, not a plan.
MAX_TILES = 400

# 3MF is an already-deflated ZIP; STL is raw floats that compress well.
_THREEMF_RATIO = 0.21

Progress = Callable[[int, int, str], None]
"""``(done, total, message)`` -- called as tiles are fetched and written."""


def _ceil_div(a: float, b: float) -> int:
    """Tiles needed to cover ``a`` in steps of ``b``.

    The epsilon matters: 1000/200 evaluates to 5.000000000000001 often enough
    that a naive ceil would quietly hand back a sixth tile that is 0.0000001 mm
    wide.
    """
    return max(1, math.ceil(a / b - 1e-9))


@dataclass(frozen=True)
class BedSpec:
    """The usable build area of a printer or machine, in millimetres.

    ``margin_mm`` is clearance kept on *each* side, so it comes off the usable
    span twice. Set it if your first layer misbehaves at the platter edge or
    your CNC needs room for clamps.
    """

    x_mm: float
    y_mm: float
    margin_mm: float = 0.0
    allow_rotation: bool = True

    def __post_init__(self) -> None:
        if self.x_mm <= 0 or self.y_mm <= 0:
            raise ValueError(f"bed size must be > 0, got {self.x_mm} x {self.y_mm}")
        if self.margin_mm < 0:
            raise ValueError("margin_mm must be >= 0")
        if min(self.usable) <= 0:
            raise ValueError(
                f"a {self.margin_mm:g} mm margin leaves nothing usable on a "
                f"{self.x_mm:g} x {self.y_mm:g} mm bed"
            )

    @classmethod
    def square(cls, mm: float, margin_mm: float = 0.0, allow_rotation: bool = True) -> BedSpec:
        return cls(mm, mm, margin_mm, allow_rotation)

    @property
    def usable(self) -> tuple[float, float]:
        return self.x_mm - 2 * self.margin_mm, self.y_mm - 2 * self.margin_mm

    def __str__(self) -> str:
        ux, uy = self.usable
        bed = f"{self.x_mm:g} x {self.y_mm:g} mm"
        return bed if not self.margin_mm else f"{bed} ({ux:g} x {uy:g} usable)"


@dataclass(frozen=True)
class TileLayout:
    """How a finished model is cut up, in millimetres and tile counts.

    Derived without touching the network -- it needs only the region's ground
    extent and the scale settings -- so it is cheap enough to recompute on every
    keystroke in a settings panel.
    """

    cols: int
    rows: int
    tile_w_mm: float
    tile_h_mm: float
    total_w_mm: float
    total_h_mm: float
    bed: BedSpec
    rotated_on_bed: bool = False

    def __post_init__(self) -> None:
        if self.cols < 1 or self.rows < 1:
            raise ValueError(f"tile counts must be >= 1, got {self.cols}x{self.rows}")
        if self.n_tiles > MAX_TILES:
            raise ValueError(
                f"that splits into {self.n_tiles} tiles (limit {MAX_TILES}); "
                "use a bigger bed, a smaller finished size, or a smaller region"
            )

    @property
    def n_tiles(self) -> int:
        return self.cols * self.rows

    @property
    def fits_bed(self) -> bool:
        """Whether a tile actually fits the bed in the chosen orientation."""
        bx, by = self.bed.usable
        if self.rotated_on_bed:
            bx, by = by, bx
        return self.tile_w_mm <= bx + 1e-9 and self.tile_h_mm <= by + 1e-9

    def sample_shape(self, max_grid: int) -> tuple[int, int]:
        """Samples ``(rows, cols)`` per tile, isotropic and capped at ``max_grid``.

        ``max_grid`` is per *tile*, which is what you want: a tile is what
        actually gets printed, so its sample count is what the nozzle diameter
        argues about.
        """
        aspect = self.tile_w_mm / self.tile_h_mm
        if aspect >= 1.0:
            return max(2, int(round(max_grid / aspect))), max_grid
        return max_grid, max(2, int(round(max_grid * aspect)))

    def estimate(self, max_grid: int) -> dict:
        """Triangle counts and file sizes for the split, before committing to it."""
        rows, cols = self.sample_shape(max_grid)
        ring = 2 * rows + 2 * cols - 4
        per_tile = 2 * (rows - 1) * (cols - 1) + 3 * ring
        stl = estimated_binary_size(per_tile)
        return {
            "sample_rows": rows,
            "sample_cols": cols,
            "triangles_per_tile": per_tile,
            "triangles_total": per_tile * self.n_tiles,
            "bytes_per_tile": stl,
            "bytes_total": stl * self.n_tiles,
            "bytes_per_tile_3mf": round(stl * _THREEMF_RATIO),
            "bytes_total_3mf": round(stl * _THREEMF_RATIO) * self.n_tiles,
            "mm_per_sample": self.tile_w_mm / cols,
        }

    def describe(self) -> str:
        turned = " (rotated on the bed)" if self.rotated_on_bed else ""
        return (
            f"{self.cols} x {self.rows} = {self.n_tiles} tiles of "
            f"{self.tile_w_mm:.1f} x {self.tile_h_mm:.1f} mm{turned}, "
            f"assembling to {self.total_w_mm:.1f} x {self.total_h_mm:.1f} mm"
        )


def plan_layout(
    bbox: BBox,
    settings: ReliefSettings,
    bed: BedSpec,
    square: bool = False,
    tiles: tuple[int, int] | None = None,
) -> TileLayout:
    """Work out the tile grid for ``bbox`` at ``settings``, without fetching anything.

    ``tiles`` forces an explicit ``(cols, rows)`` split instead of deriving one
    from the bed -- useful when you would rather have a 4x4 of slightly
    undersized tiles than a 5x4 of maximal ones.
    """
    if square:
        bbox = bbox.to_square()

    xy_mm_per_m = settings.horizontal_mm_per_m(bbox.width_m, bbox.height_m)
    total_w = bbox.width_m * xy_mm_per_m
    total_h = bbox.height_m * xy_mm_per_m

    if tiles is not None:
        cols, rows = int(tiles[0]), int(tiles[1])
        if cols < 1 or rows < 1:
            raise ValueError(f"explicit tile counts must be >= 1, got {cols}x{rows}")
        rotated = _prefers_rotation(total_w / cols, total_h / rows, bed)
    else:
        cols, rows, rotated = _split(total_w, total_h, bed)

    return TileLayout(
        cols=cols,
        rows=rows,
        tile_w_mm=total_w / cols,
        tile_h_mm=total_h / rows,
        total_w_mm=total_w,
        total_h_mm=total_h,
        bed=bed,
        rotated_on_bed=rotated,
    )


def _split(total_w: float, total_h: float, bed: BedSpec) -> tuple[int, int, bool]:
    """Fewest tiles that cover the model, trying both bed orientations.

    Rotation is worth checking on a non-square bed: 1000 x 600 mm on a
    200 x 250 mm bed is 5x3 upright but 4x3 turned, which is three fewer prints
    for no other cost.
    """
    bx, by = bed.usable
    upright = (_ceil_div(total_w, bx), _ceil_div(total_h, by))
    if not bed.allow_rotation or bx == by:
        return upright[0], upright[1], False

    turned = (_ceil_div(total_w, by), _ceil_div(total_h, bx))
    if turned[0] * turned[1] < upright[0] * upright[1]:
        return turned[0], turned[1], True
    return upright[0], upright[1], False


def _prefers_rotation(tile_w: float, tile_h: float, bed: BedSpec) -> bool:
    """Whether a tile of this shape only fits, or fits better, turned 90 degrees."""
    bx, by = bed.usable
    if not bed.allow_rotation:
        return False
    upright = tile_w <= bx + 1e-9 and tile_h <= by + 1e-9
    turned = tile_w <= by + 1e-9 and tile_h <= bx + 1e-9
    return turned and not upright


def tile_bbox(region: BBox, layout: TileLayout, row: int, col: int) -> BBox:
    """The sub-box for one tile. Row 0 is the northernmost, column 0 the westernmost.

    That orientation matches both ``DEMGrid`` (row 0 is north) and how anyone
    would lay the printed pieces out on a table.
    """
    if not (0 <= row < layout.rows and 0 <= col < layout.cols):
        raise IndexError(f"tile r{row}c{col} is outside a {layout.cols}x{layout.rows} layout")

    lon_span = region.east - region.west
    lat_span = region.north - region.south
    # Interpolating from both ends rather than accumulating a step keeps the
    # outermost tiles landing exactly on the region's own edges.
    west = region.west + lon_span * col / layout.cols
    east = region.west + lon_span * (col + 1) / layout.cols
    north = region.north - lat_span * row / layout.rows
    south = region.north - lat_span * (row + 1) / layout.rows
    return BBox(west, south, east, north)


@dataclass(frozen=True)
class TilePiece:
    """One printable tile: a solid, plus where it came from and where it goes."""

    row: int  # 0 = north
    col: int  # 0 = west
    bbox: BBox
    solid: Solid
    size_mm: tuple[float, float, float]

    @property
    def name(self) -> str:
        """Stable, sortable, 1-based -- ``r01c01`` is the north-west corner."""
        return f"r{self.row + 1:02d}c{self.col + 1:02d}"


@dataclass(frozen=True)
class TiledModel:
    """A fetched, seam-reconciled region ready to be emitted tile by tile.

    Holds elevation grids, not meshes. A 5x5 split at 500 samples per tile is
    50 MB of elevation but would be well over a gigabyte of vertices and faces
    if every solid were built up front, so :meth:`iter_tiles` builds them lazily
    and callers write each one out before the next is meshed.
    """

    region: BBox
    layout: TileLayout
    grids: tuple[tuple[DEMGrid, ...], ...]  # [row][col], seams already reconciled
    settings: ReliefSettings
    source_name: str
    attribution: str
    min_elevation_m: float
    max_elevation_m: float
    nodata_filled: int

    @property
    def elevation_range_m(self) -> float:
        return self.max_elevation_m - self.min_elevation_m

    @property
    def xy_mm_per_m(self) -> float:
        return self.settings.horizontal_mm_per_m(self.region.width_m, self.region.height_m)

    @property
    def z_mm_per_m(self) -> float:
        """Vertical scale, derived from the *region's* elevation range.

        This is the single datum every tile shares. Deriving it per tile is what
        would put a step in each seam.
        """
        return self.settings.vertical_mm_per_m(
            self.elevation_range_m, self.region.width_m, self.region.height_m
        )

    @property
    def size_mm(self) -> tuple[float, float, float]:
        """Assembled dimensions, including the tallest point anywhere in the region."""
        z = self.settings.base_thickness_mm + self.elevation_range_m * self.z_mm_per_m
        return self.layout.total_w_mm, self.layout.total_h_mm, z

    @property
    def scale_denominator(self) -> float:
        return 1000.0 / self.xy_mm_per_m

    @property
    def vertical_exaggeration(self) -> float:
        xy = self.xy_mm_per_m
        return (self.z_mm_per_m / xy) if xy else 1.0

    @property
    def n_triangles(self) -> int:
        return self.layout.estimate(self.settings.max_grid)["triangles_total"]

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        total_samples = sum(g.elevations.size for row in self.grids for g in row)
        frac = self.nodata_filled / total_samples if total_samples else 0.0
        if frac > 0.01:
            out.append(f"{frac:.1%} of the elevation data was nodata and got flat-filled")
        if self.elevation_range_m < 1.0:
            out.append(f"the region is nearly flat ({self.elevation_range_m:.2f} m of relief)")
        if not self.layout.fits_bed:
            w, h = self.layout.tile_w_mm, self.layout.tile_h_mm
            out.append(f"tiles are {w:.1f} x {h:.1f} mm, which does not fit {self.layout.bed}")
        if self.n_triangles > 20_000_000:
            out.append(f"{self.n_triangles:,} triangles across all tiles is a lot to slice")
        return out

    def iter_tiles(self, progress: Progress | None = None) -> Iterator[TilePiece]:
        """Mesh each tile in turn, north-west first, reading order.

        Yields one solid at a time so a caller that writes as it goes never
        holds more than a single tile's geometry.
        """
        z = self.z_mm_per_m
        base = self.settings.base_thickness_mm
        total = self.layout.n_tiles
        done = 0

        for row in range(self.layout.rows):
            for col in range(self.layout.cols):
                grid = self.grids[row][col]
                rows_n, cols_n = grid.shape

                heights = np.ascontiguousarray(
                    (grid.elevations - self.min_elevation_m) * z, dtype=np.float64
                )
                dx = self.layout.tile_w_mm / (cols_n - 1)
                dy = self.layout.tile_h_mm / (rows_n - 1)

                solid = build_solid(heights, dx, dy, base)
                if signed_volume_mm3(solid) <= 0:
                    raise RuntimeError("tile mesh has inverted winding; this is a bug in reliefkit")

                piece = TilePiece(
                    row=row,
                    col=col,
                    bbox=grid.bbox,
                    solid=solid,
                    size_mm=(
                        self.layout.tile_w_mm,
                        self.layout.tile_h_mm,
                        base + float(heights.max()),
                    ),
                )
                done += 1
                if progress:
                    progress(done, total, f"meshing tile {piece.name}")
                yield piece

    def summary(self) -> str:
        x, y, zt = self.size_mm
        est = self.layout.estimate(self.settings.max_grid)
        return "\n".join(
            [
                f"source     : {self.source_name} ({self.layout.n_tiles} tiles, "
                f"{est['sample_rows']}x{est['sample_cols']} samples each, "
                f"elevation {self.min_elevation_m:.1f}..{self.max_elevation_m:.1f} m)",
                f"assembled  : {x:.1f} x {y:.1f} x {zt:.1f} mm",
                f"tiles      : {self.layout.describe()}",
                f"scale      : 1:{self.scale_denominator:,.0f} horizontal, "
                f"{self.vertical_exaggeration:.2f}x vertical",
                f"mesh       : {est['triangles_total']:,} triangles total "
                f"({est['triangles_per_tile']:,} per tile)",
                f"attribution: {self.attribution}",
            ]
        )

    def manifest(self, entries: Sequence[dict] | None = None) -> dict:
        from . import __version__  # noqa: PLC0415 - avoids a circular import at module load

        x, y, zt = self.size_mm
        return {
            "generator": f"reliefkit {__version__}",
            "region": dict(zip(("west", "south", "east", "north"), self.region.as_tuple())),
            "layout": {
                "cols": self.layout.cols,
                "rows": self.layout.rows,
                "tile_w_mm": round(self.layout.tile_w_mm, 4),
                "tile_h_mm": round(self.layout.tile_h_mm, 4),
                "rotated_on_bed": self.layout.rotated_on_bed,
                "bed_x_mm": self.layout.bed.x_mm,
                "bed_y_mm": self.layout.bed.y_mm,
                "bed_margin_mm": self.layout.bed.margin_mm,
            },
            "assembled_mm": {"x": round(x, 3), "y": round(y, 3), "z": round(zt, 3)},
            "scale": {
                "denominator": round(self.scale_denominator, 1),
                "vertical_exaggeration": round(self.vertical_exaggeration, 4),
                "base_thickness_mm": self.settings.base_thickness_mm,
            },
            "elevation_m": {
                "min": round(self.min_elevation_m, 2),
                "max": round(self.max_elevation_m, 2),
                "range": round(self.elevation_range_m, 2),
            },
            "source": self.source_name,
            "attribution": self.attribution,
            "tiles": list(entries or []),
        }

    def assembly_sheet(self) -> str:
        """A human-readable layout map to print alongside the tiles."""
        x, y, zt = self.size_mm
        lines = [
            "reliefkit tile assembly",
            "=" * 23,
            "",
            f"Finished model : {x:.1f} x {y:.1f} x {zt:.1f} mm",
            f"Cut into       : {self.layout.describe()}",
            f"Print bed      : {self.layout.bed}",
            f"Scale          : 1:{self.scale_denominator:,.0f} horizontal, "
            f"{self.vertical_exaggeration:.2f}x vertical",
            f"Base thickness : {self.settings.base_thickness_mm:g} mm on every tile",
            "",
            "Row 01 is the NORTH edge, column 01 the WEST edge. Adjacent tiles share",
            "their edge heights exactly, so they butt flat -- glue the seams, no",
            "sanding needed. Every tile is watertight on its own.",
            "",
        ]
        if self.layout.rotated_on_bed:
            lines += ["Rotate each tile 90 degrees on the bed to fit.", ""]

        width = 10
        lines.append(" " * 5 + "".join(f"c{c + 1:02d}".center(width) for c in range(self.layout.cols)))
        for r in range(self.layout.rows):
            cells = "".join(f"[r{r + 1:02d}c{c + 1:02d}]".center(width) for c in range(self.layout.cols))
            lines.append(f"r{r + 1:02d}  {cells}")
        lines += ["", self.attribution, ""]
        return "\n".join(lines)


def build_tiled_model(
    bbox: BBox,
    bed: BedSpec,
    settings: ReliefSettings | None = None,
    source: str | DEMSource = "auto",
    square: bool = False,
    tiles: tuple[int, int] | None = None,
    workers: int = 4,
    progress: Progress | None = None,
) -> TiledModel:
    """Fetch every tile of ``bbox`` and reconcile the seams between them.

    ``settings.max_grid`` is read as samples per *tile*, not across the whole
    region -- a tile is what gets printed, so it is what the tool width has an
    opinion about.

    Fetches run on a small thread pool because they are entirely I/O bound; the
    default of four is deliberately modest, since these are free public services
    and a 25-tile job hitting them all at once is rude.
    """
    settings = settings or ReliefSettings()
    if square:
        bbox = bbox.to_square()

    layout = plan_layout(bbox, settings, bed, square=False, tiles=tiles)

    # Chosen once for the region, not per tile: a mixed-source assembly would
    # step at whichever seam the sources changed over.
    if isinstance(source, str):
        src = choose_source(bbox) if source == "auto" else get_source(source)
    else:
        src = source

    sample_rows, sample_cols = layout.sample_shape(settings.max_grid)
    coords = [(r, c) for r in range(layout.rows) for c in range(layout.cols)]
    total = len(coords)
    fetched = 0

    def fetch_one(rc: tuple[int, int]) -> DEMGrid:
        r, c = rc
        box = tile_bbox(bbox, layout, r, c)
        return src.fetch(box, max(sample_rows, sample_cols)).resample_to(sample_rows, sample_cols)

    if progress:
        progress(0, total, f"fetching {total} tiles from {src.name}")

    with ThreadPoolExecutor(max_workers=max(1, min(workers, total))) as pool:
        results = []
        for rc, grid in zip(coords, pool.map(fetch_one, coords)):
            results.append((rc, grid))
            fetched += 1
            if progress:
                progress(fetched, total, f"fetched tile r{rc[0] + 1:02d}c{rc[1] + 1:02d}")

    lookup = dict(results)
    elevations = [[lookup[(r, c)].elevations.copy() for c in range(layout.cols)] for r in range(layout.rows)]
    reconcile_seams(elevations)

    grids = tuple(
        tuple(
            DEMGrid(
                elevations[r][c],
                tile_bbox(bbox, layout, r, c),
                source=src.name,
                nodata_filled=lookup[(r, c)].nodata_filled,
            )
            for c in range(layout.cols)
        )
        for r in range(layout.rows)
    )

    flat = [g.elevations for row in grids for g in row]
    return TiledModel(
        region=bbox,
        layout=layout,
        grids=grids,
        settings=settings,
        source_name=src.name,
        attribution=src.attribution,
        min_elevation_m=float(min(a.min() for a in flat)),
        max_elevation_m=float(max(a.max() for a in flat)),
        nodata_filled=sum(g.nodata_filled for row in grids for g in row),
    )


def reconcile_seams(elevations: list[list[np.ndarray]]) -> None:
    """Force neighbouring tiles to agree exactly along their shared edges, in place.

    Vertical seams first, then horizontal. The order is what makes the points
    where four tiles meet work: the vertical pass leaves the left pair agreeing
    and the right pair agreeing, and the horizontal pass then runs across the
    full row *including* those columns, so all four end up on the same value.
    """
    rows, cols = len(elevations), len(elevations[0])

    for r in range(rows):
        for c in range(cols - 1):
            left, right = elevations[r][c], elevations[r][c + 1]
            if left.shape[0] != right.shape[0]:
                raise ValueError(f"tiles r{r}c{c} and r{r}c{c + 1} disagree on row count")
            shared = 0.5 * (left[:, -1] + right[:, 0])
            left[:, -1] = shared
            right[:, 0] = shared

    for r in range(rows - 1):
        for c in range(cols):
            north, south = elevations[r][c], elevations[r + 1][c]
            if north.shape[1] != south.shape[1]:
                raise ValueError(f"tiles r{r}c{c} and r{r + 1}c{c} disagree on column count")
            shared = 0.5 * (north[-1, :] + south[0, :])
            north[-1, :] = shared
            south[0, :] = shared


def _write_solid(solid: Solid, target, fmt: str, name: str) -> None:
    """Serialise one solid to a path or an open binary stream."""
    if fmt == "3mf":
        from .threemf import write_3mf  # noqa: PLC0415

        write_3mf(solid, target, name=name)
    elif fmt == "stl":
        from .stl import _write_binary, write_binary_stl  # noqa: PLC0415

        if isinstance(target, (str, Path)):
            write_binary_stl(solid, target, header=name)
        else:
            _write_binary(solid, target, name)
    else:
        raise ValueError(f"unknown format {fmt!r}; expected 'stl' or '3mf'")


def write_tiles(
    model: TiledModel,
    out_dir: str | Path,
    fmt: str = "3mf",
    prefix: str = "tile",
    progress: Progress | None = None,
) -> list[Path]:
    """Write every tile into ``out_dir``, alongside a manifest and assembly sheet.

    Tiles are meshed and written one at a time, so peak memory is one tile's
    geometry regardless of how many there are.
    """
    if fmt not in ("stl", "3mf"):
        raise ValueError(f"unknown format {fmt!r}; expected 'stl' or '3mf'")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    entries: list[dict] = []
    total = model.layout.n_tiles

    for piece in model.iter_tiles():
        filename = f"{prefix}_{piece.name}.{fmt}"
        path = out_dir / filename
        _write_solid(piece.solid, path, fmt, f"reliefkit {piece.name}")
        paths.append(path)
        entries.append(_entry(piece, filename))
        if progress:
            progress(len(paths), total, f"wrote {filename}")

    manifest = out_dir / "manifest.json"
    manifest.write_text(json.dumps(model.manifest(entries), indent=2) + "\n", encoding="utf-8")
    sheet = out_dir / "ASSEMBLY.txt"
    sheet.write_text(model.assembly_sheet(), encoding="utf-8")
    return [*paths, manifest, sheet]


def write_tiles_zip(
    model: TiledModel,
    target,
    fmt: str = "3mf",
    prefix: str = "tile",
    progress: Progress | None = None,
):
    """Write every tile into a ZIP at a path or an open binary stream.

    3MF is already a deflated ZIP, so its members are stored rather than
    recompressed -- deflating a deflate stream costs CPU and saves nothing.
    STL is raw little-endian floats and compresses well, so it is deflated.
    """
    if fmt not in ("stl", "3mf"):
        raise ValueError(f"unknown format {fmt!r}; expected 'stl' or '3mf'")

    if isinstance(target, (str, Path)):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)

    member_compression = zipfile.ZIP_STORED if fmt == "3mf" else zipfile.ZIP_DEFLATED
    entries: list[dict] = []
    total = model.layout.n_tiles

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for piece in model.iter_tiles():
            filename = f"{prefix}_{piece.name}.{fmt}"
            # Both writers want a seekable stream, so each tile is staged in
            # memory -- one tile at a time, then released.
            buffer = io.BytesIO()
            _write_solid(piece.solid, buffer, fmt, f"reliefkit {piece.name}")
            zf.writestr(filename, buffer.getvalue(), compress_type=member_compression)
            entries.append(_entry(piece, filename))
            if progress:
                progress(len(entries), total, f"packed {filename}")

        zf.writestr("manifest.json", json.dumps(model.manifest(entries), indent=2) + "\n")
        zf.writestr("ASSEMBLY.txt", model.assembly_sheet())

    return target


def _entry(piece: TilePiece, filename: str) -> dict:
    w, s, e, n = piece.bbox.as_tuple()
    return {
        "row": piece.row + 1,
        "col": piece.col + 1,
        "file": filename,
        "bbox": {"west": w, "south": s, "east": e, "north": n},
        "size_mm": [round(v, 3) for v in piece.size_mm],
        "triangles": piece.solid.n_faces,
    }


__all__ = [
    "BedSpec",
    "MAX_TILES",
    "TileLayout",
    "TilePiece",
    "TiledModel",
    "build_tiled_model",
    "plan_layout",
    "reconcile_seams",
    "tile_bbox",
    "write_tiles",
    "write_tiles_zip",
]
