"""Source behaviour.

The coverage/URL logic runs offline. Tests that actually call the elevation
services are marked ``network`` and skipped by default -- run them with
``pytest -m network`` when you want to confirm the upstreams still behave.
"""

from __future__ import annotations

import pytest

from reliefkit.geo import BBox
from reliefkit.sources import SourceError, choose_source, get_source
from reliefkit.sources.copernicus import _resolution_for, _tile_name, _tile_urls
from reliefkit.sources.usgs3dep import _request_shape

RAINIER = BBox(-121.85, 46.75, -121.65, 46.90)
MONT_BLANC = BBox(6.80, 45.80, 7.00, 45.95)


# --- offline ------------------------------------------------------------


def test_auto_prefers_3dep_inside_the_us():
    assert choose_source(RAINIER).name == "usgs3dep"


def test_auto_falls_back_to_copernicus_abroad():
    assert choose_source(MONT_BLANC).name == "copernicus"


def test_unknown_source_names_the_alternatives():
    with pytest.raises(SourceError, match="unknown source"):
        get_source("srtm")


@pytest.mark.parametrize(
    "lat, lon, expected",
    [
        (46, -122, "Copernicus_DSM_COG_10_N46_00_W122_00_DEM"),
        (45, 6, "Copernicus_DSM_COG_10_N45_00_E006_00_DEM"),
        (-34, 18, "Copernicus_DSM_COG_10_S34_00_E018_00_DEM"),
    ],
)
def test_copernicus_tile_naming(lat, lon, expected):
    assert _tile_name(lat, lon) == expected


def test_copernicus_covers_every_touched_degree_cell():
    # lon 6.8..8.2 touches cells 6, 7, 8; lat 45.8..46.3 touches cells 45, 46.
    urls = _tile_urls(BBox(6.8, 45.8, 8.2, 46.3))
    assert len(urls) == 3 * 2
    assert all(u.startswith("/vsicurl/https://") for u in urls)
    for cell in ("E006", "E007", "E008", "N45", "N46"):
        assert any(cell in u for u in urls)


def test_copernicus_single_cell_box_needs_one_tile():
    assert len(_tile_urls(BBox(6.2, 45.2, 6.8, 45.8))) == 1


def test_copernicus_never_requests_finer_than_native():
    # A tiny box must clamp to 1 arc-second rather than inventing detail.
    assert _resolution_for(BBox(6.80, 45.80, 6.81, 45.81), 5000) == pytest.approx(1 / 3600)


def test_copernicus_rejects_oversized_regions():
    from reliefkit.sources import CopernicusGLO30

    with pytest.raises(SourceError, match="limit"):
        CopernicusGLO30().fetch(BBox(0.0, 0.0, 10.0, 10.0), 500)


def test_3dep_request_shape_preserves_aspect_and_caps():
    cols, rows = _request_shape(RAINIER, 800)
    assert max(cols, rows) == 800
    assert cols / rows == pytest.approx(RAINIER.aspect, rel=0.01)
    assert max(_request_shape(RAINIER, 99_999)) == 4000


# --- network ------------------------------------------------------------


@pytest.mark.network
def test_3dep_returns_real_rainier_elevations():
    grid = get_source("usgs3dep").fetch(RAINIER, 200)
    assert max(grid.shape) == 200
    # Rainier's summit is 4392 m; the surrounding valleys sit well below 1000 m.
    assert 4300 < grid.max_elevation < 4400
    assert grid.min_elevation < 1000


@pytest.mark.network
def test_copernicus_returns_real_mont_blanc_elevations():
    grid = get_source("copernicus").fetch(MONT_BLANC, 200)
    # Mont Blanc's summit is 4806 m.
    assert 4700 < grid.max_elevation < 4900


@pytest.mark.network
def test_end_to_end_produces_a_watertight_solid(tmp_path):
    from reliefkit import ReliefSettings, generate_stl

    result = generate_stl(
        RAINIER,
        tmp_path / "rainier.stl",
        ReliefSettings(target_size_mm=80, relief_height_mm=10, max_grid=150),
        square=True,
    )
    assert result.solid.is_watertight()
    assert max(result.size_mm[:2]) == pytest.approx(80.0)
    assert (tmp_path / "rainier.stl").stat().st_size > 0
