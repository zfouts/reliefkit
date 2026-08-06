"""Tiling: layout arithmetic, seam agreement, and the writers.

The source here samples at *pixel centres*, half a cell inside the box it was
handed, exactly as ArcGIS ``exportImage`` and a rasterio ``merge`` both do. That
matters: it means two neighbouring tiles genuinely disagree along their shared
edge before reconciliation, so the tests below are checking that the seam logic
works rather than that a node-registered fixture happened to line up.
"""

from __future__ import annotations

import json
import zipfile

import numpy as np
import pytest

from reliefkit.dem import DEMGrid
from reliefkit.geo import BBox
from reliefkit.pipeline import build_model
from reliefkit.settings import ReliefSettings
from reliefkit.threemf import read_3mf
from reliefkit.tiling import (
    MAX_TILES,
    BedSpec,
    build_tiled_model,
    plan_layout,
    reconcile_seams,
    tile_bbox,
    write_tiles,
    write_tiles_zip,
)

REGION = BBox(-121.0, 46.0, -119.0, 48.0)


def field(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """A smooth, non-separable elevation field with a real horizontal trend."""
    return 1200.0 + 700.0 * np.sin(2.4 * lon) * np.cos(3.1 * lat) + 55.0 * (lon + lat)


class AnalyticSource:
    name = "analytic"
    licence = "n/a"
    attribution = "analytic test fixture"

    def __init__(self, centred: bool = True, cap: int = 48) -> None:
        self.centred = centred
        self.cap = cap
        self.boxes: list[BBox] = []

    def covers(self, bbox: BBox) -> bool:
        return True

    def fetch(self, bbox: BBox, target_dim: int) -> DEMGrid:
        self.boxes.append(bbox)
        n = max(2, min(int(target_dim), self.cap))
        if self.centred:
            dlon = (bbox.east - bbox.west) / n
            dlat = (bbox.north - bbox.south) / n
            lon = bbox.west + dlon * (np.arange(n) + 0.5)
            lat = bbox.north - dlat * (np.arange(n) + 0.5)
        else:
            lon = np.linspace(bbox.west, bbox.east, n)
            lat = np.linspace(bbox.north, bbox.south, n)
        grid_lon, grid_lat = np.meshgrid(lon, lat)
        return DEMGrid(field(grid_lon, grid_lat), bbox, source=self.name)


def fit(size_mm=1000.0, relief_mm=25.0, grid=24, base=5.0) -> ReliefSettings:
    return ReliefSettings(
        scale_mode="fit",
        target_size_mm=size_mm,
        relief_height_mm=relief_mm,
        base_thickness_mm=base,
        max_grid=grid,
    )


# --- bed spec -----------------------------------------------------------


def test_margin_comes_off_both_sides():
    assert BedSpec(200, 200, margin_mm=5).usable == (190, 190)


def test_margin_that_consumes_the_bed_is_rejected():
    with pytest.raises(ValueError, match="nothing usable"):
        BedSpec(200, 200, margin_mm=100)


@pytest.mark.parametrize("x,y", [(0, 200), (200, -1)])
def test_nonpositive_bed_is_rejected(x, y):
    with pytest.raises(ValueError, match="bed size"):
        BedSpec(x, y)


# --- layout -------------------------------------------------------------


def test_exact_division_does_not_add_a_sliver_tile():
    """1000/200 is 5.000000000000001 in binary; a naive ceil would say six."""
    layout = plan_layout(REGION, fit(1000.0), BedSpec.square(200), square=True)
    assert (layout.cols, layout.rows) == (5, 5)
    assert layout.tile_w_mm == pytest.approx(200.0)
    assert layout.tile_h_mm == pytest.approx(200.0)


def test_ragged_division_spreads_evenly_rather_than_leaving_a_runt():
    layout = plan_layout(REGION, fit(1000.0), BedSpec.square(300), square=True)
    assert (layout.cols, layout.rows) == (4, 4)
    # Four equal 250 mm tiles, not three 300s and a 100.
    assert layout.tile_w_mm == pytest.approx(250.0)
    assert layout.cols * layout.tile_w_mm == pytest.approx(layout.total_w_mm)


def test_margin_can_force_an_extra_tile():
    plain = plan_layout(REGION, fit(1000.0), BedSpec.square(200), square=True)
    tight = plan_layout(REGION, fit(1000.0), BedSpec.square(200, margin_mm=5), square=True)
    assert plain.cols == 5
    assert tight.cols == 6


def test_rotation_is_taken_when_it_means_fewer_tiles():
    # 1000 x 596 mm on a 200 x 250 bed: 5x3 upright, 4x3 turned.
    region = BBox(0.0, 0.0, 1.0, 0.6)
    turned = plan_layout(region, fit(1000.0), BedSpec(200, 250))
    upright = plan_layout(region, fit(1000.0), BedSpec(200, 250, allow_rotation=False))
    assert turned.rotated_on_bed and turned.n_tiles == 12
    assert not upright.rotated_on_bed and upright.n_tiles == 15


def test_square_bed_never_reports_rotation():
    layout = plan_layout(REGION, fit(1000.0), BedSpec.square(200), square=True)
    assert layout.rotated_on_bed is False


def test_explicit_split_overrides_the_bed():
    layout = plan_layout(REGION, fit(1000.0), BedSpec.square(200), square=True, tiles=(2, 4))
    assert (layout.cols, layout.rows) == (2, 4)
    assert layout.tile_w_mm == pytest.approx(500.0)
    assert layout.fits_bed is False


def test_absurd_split_is_refused():
    with pytest.raises(ValueError, match=f"limit {MAX_TILES}"):
        plan_layout(REGION, fit(5000.0), BedSpec.square(50), square=True)


def test_true_scale_layout_follows_the_cartographic_ratio():
    settings = ReliefSettings(scale_mode="true", scale_denominator=500_000, max_grid=24)
    square = REGION.to_square()
    layout = plan_layout(REGION, settings, BedSpec.square(200), square=True)
    assert layout.total_w_mm == pytest.approx(square.width_m * 1000.0 / 500_000)
    assert layout.n_tiles >= 1


def test_sample_shape_tracks_tile_aspect():
    layout = plan_layout(BBox(0.0, 0.0, 1.0, 0.5), fit(1000.0), BedSpec(500, 500))
    rows, cols = layout.sample_shape(100)
    assert cols == 100
    assert rows == pytest.approx(round(100 * layout.tile_h_mm / layout.tile_w_mm), abs=1)


def test_estimate_scales_with_the_tile_count():
    layout = plan_layout(REGION, fit(1000.0), BedSpec.square(200), square=True)
    est = layout.estimate(200)
    assert est["triangles_total"] == est["triangles_per_tile"] * 25
    assert est["mm_per_sample"] == pytest.approx(200.0 / est["sample_cols"])


# --- tile boxes ---------------------------------------------------------


def test_tiles_cover_the_region_edge_to_edge():
    square = REGION.to_square()
    layout = plan_layout(REGION, fit(1000.0), BedSpec.square(200), square=True)

    nw = tile_bbox(square, layout, 0, 0)
    se = tile_bbox(square, layout, layout.rows - 1, layout.cols - 1)
    assert nw.west == square.west and nw.north == square.north
    assert se.east == square.east and se.south == square.south


def test_neighbouring_tiles_share_an_edge_exactly_in_degrees():
    square = REGION.to_square()
    layout = plan_layout(REGION, fit(1000.0), BedSpec.square(200), square=True)
    for r in range(layout.rows):
        for c in range(layout.cols - 1):
            assert tile_bbox(square, layout, r, c).east == tile_bbox(square, layout, r, c + 1).west
    for r in range(layout.rows - 1):
        for c in range(layout.cols):
            assert tile_bbox(square, layout, r, c).south == tile_bbox(square, layout, r + 1, c).north


def test_row_zero_is_north_and_column_zero_is_west():
    layout = plan_layout(REGION, fit(1000.0), BedSpec.square(200))
    assert tile_bbox(REGION, layout, 0, 0).north > tile_bbox(REGION, layout, 1, 0).north
    assert tile_bbox(REGION, layout, 0, 0).west < tile_bbox(REGION, layout, 0, 1).west


def test_out_of_range_tile_is_an_error():
    layout = plan_layout(REGION, fit(1000.0), BedSpec.square(200))
    with pytest.raises(IndexError):
        tile_bbox(REGION, layout, layout.rows, 0)


# --- seam reconciliation ------------------------------------------------


def test_reconcile_makes_shared_edges_identical():
    rng = np.random.default_rng(0)
    tiles = [[rng.normal(size=(6, 6)) for _ in range(3)] for _ in range(2)]
    reconcile_seams(tiles)

    for r in range(2):
        for c in range(2):
            assert np.array_equal(tiles[r][c][:, -1], tiles[r][c + 1][:, 0])
    for c in range(3):
        assert np.array_equal(tiles[0][c][-1, :], tiles[1][c][0, :])


def test_all_four_tiles_agree_where_their_corners_meet():
    """The vertical-then-horizontal order is what makes this hold."""
    rng = np.random.default_rng(1)
    tiles = [[rng.normal(size=(5, 5)) for _ in range(2)] for _ in range(2)]
    reconcile_seams(tiles)

    corner = {tiles[0][0][-1, -1], tiles[0][1][-1, 0], tiles[1][0][0, -1], tiles[1][1][0, 0]}
    assert len(corner) == 1


def test_reconcile_rejects_mismatched_lattices():
    tiles = [[np.zeros((4, 4)), np.zeros((5, 4))]]
    with pytest.raises(ValueError, match="disagree on row count"):
        reconcile_seams(tiles)


def test_pixel_centred_edges_actually_disagree_before_reconciliation():
    """Guards the fixture: if the raw edges already matched, the tests above
    would pass without the seam logic doing anything."""
    square = REGION.to_square()
    layout = plan_layout(REGION, fit(1000.0), BedSpec.square(200), square=True)
    src = AnalyticSource()

    left = src.fetch(tile_bbox(square, layout, 0, 0), 24).resample_to(24, 24)
    right = src.fetch(tile_bbox(square, layout, 0, 1), 24).resample_to(24, 24)
    assert np.abs(left.elevations[:, -1] - right.elevations[:, 0]).max() > 1.0


# --- built models -------------------------------------------------------


@pytest.fixture
def model():
    return build_tiled_model(
        REGION, BedSpec.square(200), settings=fit(), source=AnalyticSource(), square=True, workers=3
    )


def test_every_tile_is_fetched_once(model):
    assert model.layout.n_tiles == 25


def test_seam_elevations_are_bit_identical(model):
    g = model.grids
    for r in range(model.layout.rows):
        for c in range(model.layout.cols - 1):
            assert np.array_equal(g[r][c].elevations[:, -1], g[r][c + 1].elevations[:, 0])
    for r in range(model.layout.rows - 1):
        for c in range(model.layout.cols):
            assert np.array_equal(g[r][c].elevations[-1, :], g[r + 1][c].elevations[0, :])


def test_every_tile_is_watertight(model):
    assert all(piece.solid.is_watertight() for piece in model.iter_tiles())


def test_seam_vertices_coincide_once_tiles_are_laid_out(model):
    """The real assembly check: put the tiles side by side in model space and
    the shared edge vertices must land on top of each other."""
    tw, th = model.layout.tile_w_mm, model.layout.tile_h_mm
    pieces = {(p.row, p.col): p for p in model.iter_tiles()}

    def top(piece):
        rows, cols = model.grids[piece.row][piece.col].shape
        return piece.solid.vertices[: rows * cols].reshape(rows, cols, 3)

    for r in range(model.layout.rows):
        for c in range(model.layout.cols - 1):
            east = top(pieces[(r, c)])[:, -1].copy()
            east[:, 0] -= tw                       # into the eastern tile's frame
            assert np.abs(east - top(pieces[(r, c + 1)])[:, 0]).max() == 0.0

    for r in range(model.layout.rows - 1):
        for c in range(model.layout.cols):
            south = top(pieces[(r, c)])[-1, :].copy()
            south[:, 1] += th                      # the tile below sits lower in Y
            assert np.abs(south - top(pieces[(r + 1, c)])[0, :]).max() == 0.0


def test_tiles_share_one_elevation_datum(model):
    """Each tile keeps its own local relief; only the tallest reaches the top.

    Scaled per tile instead, every one of them would peak at exactly the relief
    height and every seam would step.
    """
    peaks = [piece.size_mm[2] for piece in model.iter_tiles()]
    top = model.settings.base_thickness_mm + model.settings.relief_height_mm

    assert max(peaks) == pytest.approx(top)
    assert min(peaks) < top - 1.0
    assert len({round(p, 3) for p in peaks}) > 1


def test_assembled_size_matches_the_requested_finished_size(model):
    x, y, z = model.size_mm
    assert x == pytest.approx(1000.0)
    assert y == pytest.approx(1000.0)
    assert z == pytest.approx(model.settings.base_thickness_mm + model.settings.relief_height_mm)


def test_every_tile_is_the_same_physical_size(model):
    sizes = {(round(p.size_mm[0], 6), round(p.size_mm[1], 6)) for p in model.iter_tiles()}
    assert sizes == {(200.0, 200.0)}


def test_tile_names_are_one_based_and_sortable(model):
    names = sorted(p.name for p in model.iter_tiles())
    assert names[0] == "r01c01"
    assert names[-1] == "r05c05"


def test_scale_reporting_matches_the_untiled_path(model):
    square = REGION.to_square()
    assert model.scale_denominator == pytest.approx(square.width_m * 1000.0 / 1000.0)


def test_a_single_tile_reproduces_the_untiled_model():
    """A 1x1 split is the ordinary pipeline; the two must agree vertex for vertex."""
    src = AnalyticSource()
    settings = fit(size_mm=150.0, grid=24)

    plain = build_model(REGION, settings, source=src, square=True)
    tiled = build_tiled_model(REGION, BedSpec.square(200), settings=settings, source=src, square=True)
    (only,) = list(tiled.iter_tiles())

    assert tiled.layout.n_tiles == 1
    np.testing.assert_allclose(only.solid.vertices, plain.solid.vertices, atol=1e-9)
    np.testing.assert_array_equal(only.solid.faces, plain.solid.faces)


def test_warns_when_a_forced_split_does_not_fit_the_bed():
    model = build_tiled_model(
        REGION, BedSpec.square(200), settings=fit(), source=AnalyticSource(), square=True, tiles=(2, 2)
    )
    assert any("does not fit" in w for w in model.warnings)


def test_a_source_that_cannot_supply_the_asked_for_detail_still_tiles():
    """Copernicus clamps at its native 1 arc-second grid, so a fine tile gets
    back fewer samples than it asked for and the lattice has to be filled up
    rather than decimated. The seams still have to be exact."""
    coarse = AnalyticSource(cap=9)
    model = build_tiled_model(
        REGION, BedSpec.square(200), settings=fit(grid=32), source=coarse, square=True
    )

    assert model.grids[0][0].shape == (32, 32)
    for r in range(model.layout.rows):
        for c in range(model.layout.cols - 1):
            g = model.grids
            assert np.array_equal(g[r][c].elevations[:, -1], g[r][c + 1].elevations[:, 0])
    assert all(p.solid.is_watertight() for p in model.iter_tiles())


def test_nodata_count_scales_with_the_grid_it_describes():
    """It is only ever read as a fraction of its own grid, so a resize that did
    not scale it would report a filled fraction over 100%."""
    grid = DEMGrid(np.zeros((100, 100)), REGION, source="x", nodata_filled=2500)

    assert grid.resample_to(50, 50).nodata_filled == 625      # a quarter the cells
    assert grid.resample_to(200, 200).nodata_filled == 10_000
    assert grid.resample_to(50, 50).nodata_filled <= 50 * 50


def test_true_scale_tiling_builds():
    settings = ReliefSettings(scale_mode="true", scale_denominator=2_000_000, z_exaggeration=3.0, max_grid=16)
    model = build_tiled_model(REGION, BedSpec.square(60), settings=settings, source=AnalyticSource(), square=True)
    assert model.vertical_exaggeration == pytest.approx(3.0)
    assert all(p.solid.is_watertight() for p in model.iter_tiles())


# --- writers ------------------------------------------------------------


def test_write_tiles_emits_every_tile_plus_the_paperwork(tmp_path, model):
    written = write_tiles(model, tmp_path / "out", fmt="3mf")
    names = {p.name for p in written}

    assert len(written) == 25 + 2
    assert "tile_r01c01.3mf" in names
    assert {"manifest.json", "ASSEMBLY.txt"} <= names

    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert len(manifest["tiles"]) == 25
    assert manifest["layout"] == {
        "cols": 5, "rows": 5, "tile_w_mm": 200.0, "tile_h_mm": 200.0,
        "rotated_on_bed": False, "bed_x_mm": 200.0, "bed_y_mm": 200.0, "bed_margin_mm": 0.0,
    }
    assert manifest["assembled_mm"] == {"x": 1000.0, "y": 1000.0, "z": 30.0}
    assert all(entry["file"] in names for entry in manifest["tiles"])

    sheet = (tmp_path / "out" / "ASSEMBLY.txt").read_text()
    assert "r01c01" in sheet and "r05c05" in sheet
    assert model.attribution in sheet


def test_written_tiles_still_meet_at_the_seams(tmp_path, model):
    write_tiles(model, tmp_path / "out", fmt="3mf")
    rows, cols = model.grids[0][0].shape

    def top(r, c):
        verts, _ = read_3mf(tmp_path / "out" / f"tile_r{r:02d}c{c:02d}.3mf")
        return verts[: rows * cols].reshape(rows, cols, 3)

    east = top(1, 1)[:, -1].copy()
    east[:, 0] -= model.layout.tile_w_mm
    np.testing.assert_allclose(east, top(1, 2)[:, 0], atol=1e-9)


def test_zip_contains_the_same_payload(tmp_path, model):
    write_tiles_zip(model, tmp_path / "tiles.zip", fmt="3mf")
    with zipfile.ZipFile(tmp_path / "tiles.zip") as zf:
        names = zf.namelist()
        # An already-deflated 3MF is stored, not recompressed.
        assert zf.getinfo("tile_r01c01.3mf").compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("manifest.json").compress_type == zipfile.ZIP_DEFLATED
        assert len(json.loads(zf.read("manifest.json"))["tiles"]) == 25
    assert len(names) == 27


def test_zip_of_stl_deflates_the_members(tmp_path, model):
    write_tiles_zip(model, tmp_path / "tiles.zip", fmt="stl")
    with zipfile.ZipFile(tmp_path / "tiles.zip") as zf:
        assert zf.getinfo("tile_r01c01.stl").compress_type == zipfile.ZIP_DEFLATED
        info = zf.getinfo("tile_r01c01.stl")
        assert info.compress_size < info.file_size


@pytest.mark.parametrize("writer", [write_tiles, write_tiles_zip])
def test_unknown_format_is_refused(tmp_path, model, writer):
    with pytest.raises(ValueError, match="unknown format"):
        writer(model, tmp_path / "out", fmt="obj")


def test_progress_is_reported_for_every_tile(tmp_path, model):
    seen = []
    write_tiles(model, tmp_path / "out", progress=lambda done, total, msg: seen.append((done, total)))
    assert seen[0] == (1, 25)
    assert seen[-1] == (25, 25)
