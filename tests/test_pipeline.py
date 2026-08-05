"""Scaling rules, STL round-trip and the offline half of the pipeline."""

from __future__ import annotations

import numpy as np
import pytest

from reliefkit.dem import DEMGrid, fill_nodata
from reliefkit.geo import BBox
from reliefkit.mesh import build_solid
from reliefkit.pipeline import mesh_from_grid
from reliefkit.settings import ReliefSettings
from reliefkit.stl import estimated_binary_size, read_binary_stl, write_binary_stl


class FakeSource:
    name = "fake"
    licence = "n/a"
    attribution = "test fixture"

    def covers(self, bbox):
        return True

    def fetch(self, bbox, target_dim):
        raise NotImplementedError


def make_grid(rows=32, cols=32, relief=500.0):
    ys = np.linspace(0, 1, rows)[:, None]
    xs = np.linspace(0, 1, cols)[None, :]
    elev = 1000.0 + relief * (np.sin(3 * xs) * np.cos(3 * ys))
    return DEMGrid(elev, BBox(-121.85, 46.75, -121.65, 46.90), source="fake")


# --- geo ----------------------------------------------------------------


def test_bbox_ground_dimensions_are_plausible():
    bbox = BBox(-121.85, 46.75, -121.65, 46.90)
    assert 14_000 < bbox.width_m < 16_500
    assert 16_000 < bbox.height_m < 17_000


def test_to_square_equalises_ground_axes():
    square = BBox(-121.85, 46.75, -121.65, 46.90).to_square()
    assert square.aspect == pytest.approx(1.0, rel=1e-6)


def test_to_square_only_grows():
    original = BBox(-121.85, 46.75, -121.65, 46.90)
    square = original.to_square()
    assert square.west <= original.west and square.east >= original.east
    assert square.south <= original.south and square.north >= original.north


@pytest.mark.parametrize("args", [(10, 0, 5, 1), (0, 10, 1, 5), (-181, 0, 1, 1)])
def test_bbox_rejects_bad_bounds(args):
    with pytest.raises(ValueError):
        BBox(*args)


# --- scaling ------------------------------------------------------------


def test_fit_mode_hits_requested_size_and_relief():
    grid = make_grid()
    settings = ReliefSettings(scale_mode="fit", target_size_mm=100.0, relief_height_mm=12.0, base_thickness_mm=5.0)
    result = mesh_from_grid(grid, settings, FakeSource())

    x, y, z = result.size_mm
    assert max(x, y) == pytest.approx(100.0)
    assert z == pytest.approx(17.0)  # 5 mm base + 12 mm relief


def test_fit_mode_preserves_ground_aspect():
    grid = make_grid()
    result = mesh_from_grid(grid, ReliefSettings(), FakeSource())
    x, y, _ = result.size_mm
    assert x / y == pytest.approx(grid.bbox.aspect, rel=1e-6)


def test_true_scale_applies_cartographic_ratio():
    grid = make_grid()
    settings = ReliefSettings(scale_mode="true", scale_denominator=100_000, z_exaggeration=2.0, base_thickness_mm=4.0)
    result = mesh_from_grid(grid, settings, FakeSource())

    x, _, z = result.size_mm
    assert x == pytest.approx(grid.bbox.width_m * 1000.0 / 100_000)
    assert result.scale_denominator == pytest.approx(100_000)
    assert result.vertical_exaggeration == pytest.approx(2.0)
    assert z == pytest.approx(4.0 + grid.elevation_range * 2000.0 / 100_000)


def test_flat_terrain_does_not_divide_by_zero():
    grid = DEMGrid(np.full((10, 10), 42.0), BBox(-1, -1, 1, 1), source="flat")
    result = mesh_from_grid(grid, ReliefSettings(base_thickness_mm=3.0), FakeSource())
    assert result.size_mm[2] == pytest.approx(3.0)
    assert result.solid.is_watertight()
    assert "nearly flat" in " ".join(result.warnings)


# --- dem ----------------------------------------------------------------


def test_fill_nodata_replaces_sentinels_and_nan():
    values = np.array([[1.0, 2.0], [-9999.0, np.nan]])
    filled, n = fill_nodata(values, -9999.0)
    assert n == 2
    assert np.isfinite(filled).all()
    assert filled[1, 0] == pytest.approx(1.5)


def test_fill_nodata_rejects_fully_empty_tile():
    with pytest.raises(ValueError, match="entirely nodata"):
        fill_nodata(np.full((4, 4), np.nan), None)


def test_resample_caps_long_axis_and_keeps_range():
    grid = make_grid(rows=400, cols=200)
    small = grid.resample(100)
    assert max(small.shape) == 100
    assert small.shape == (100, 50)
    assert small.min_elevation >= grid.min_elevation - 1e-9
    assert small.max_elevation <= grid.max_elevation + 1e-9


def test_resample_is_a_noop_when_already_small():
    grid = make_grid(rows=20, cols=20)
    assert grid.resample(100) is grid


# --- stl ----------------------------------------------------------------


def test_binary_stl_round_trip(tmp_path):
    solid = build_solid(np.random.default_rng(3).uniform(0, 4, (12, 15)), 1.0, 1.0, 2.0)
    path = write_binary_stl(solid, tmp_path / "m.stl", header="reliefkit test")

    assert path.stat().st_size == estimated_binary_size(solid.n_faces)
    normals, tris = read_binary_stl(path)
    assert len(tris) == solid.n_faces
    assert np.allclose(tris, solid.vertices[solid.faces], atol=1e-3)
    assert np.allclose(np.linalg.norm(normals, axis=1), 1.0, atol=1e-5)


def test_header_never_starts_with_solid(tmp_path):
    """A binary STL whose header begins with 'solid' is misparsed as ASCII."""
    solid = build_solid(np.zeros((3, 3)), 1.0, 1.0, 1.0)
    path = write_binary_stl(solid, tmp_path / "m.stl", header="solid but actually binary")
    assert path.read_bytes()[:5].lower() != b"solid"
