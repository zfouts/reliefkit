"""3MF output and the resolution advisor."""

from __future__ import annotations

import zipfile

import numpy as np
import pytest

from reliefkit.mesh import build_solid
from reliefkit.resolution import (
    MAX_GRID,
    TOOL_PRESETS,
    advise,
    recommended_grid,
    resolve_tool,
    triangle_count,
)
from reliefkit.stl import write_binary_stl
from reliefkit.threemf import read_3mf, write_3mf


@pytest.fixture
def solid():
    rng = np.random.default_rng(7)
    return build_solid(rng.uniform(0, 8, (90, 90)), 1.0, 1.0, 3.0)


# --- 3mf ----------------------------------------------------------------


def test_3mf_is_a_valid_opc_package(solid, tmp_path):
    path = write_3mf(solid, tmp_path / "m.3mf")
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        assert set(zf.namelist()) == {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"}


def test_3mf_round_trips_the_exact_mesh(solid, tmp_path):
    path = write_3mf(solid, tmp_path / "m.3mf")
    verts, faces = read_3mf(path)
    assert len(verts) == solid.n_vertices
    assert len(faces) == solid.n_faces
    assert np.array_equal(faces, solid.faces)
    assert np.allclose(verts, solid.vertices, atol=1e-3)


def test_3mf_preserves_watertightness(solid, tmp_path):
    _, faces = read_3mf(write_3mf(solid, tmp_path / "m.3mf"))
    edges = np.sort(
        np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1
    )
    _, counts = np.unique(edges, axis=0, return_counts=True)
    assert np.all(counts == 2)


def test_3mf_is_substantially_smaller_than_stl(solid, tmp_path):
    """Regression guard: a bare ZipInfo defaults to ZIP_STORED, which once
    shipped the model XML uncompressed and *larger* than the STL."""
    stl = write_binary_stl(solid, tmp_path / "m.stl")
    mf = write_3mf(solid, tmp_path / "m.3mf")
    assert mf.stat().st_size < stl.stat().st_size / 3


def test_3mf_model_part_is_actually_deflated(solid, tmp_path):
    path = write_3mf(solid, tmp_path / "m.3mf")
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo("3D/3dmodel.model")
    assert info.compress_type == zipfile.ZIP_DEFLATED
    assert info.compress_size < info.file_size / 3


def test_3mf_accepts_a_file_object(solid):
    import io

    buf = io.BytesIO()
    write_3mf(solid, buf)
    assert zipfile.ZipFile(buf).testzip() is None


def test_3mf_escapes_markup_in_the_title(solid, tmp_path):
    path = write_3mf(solid, tmp_path / "m.3mf", name='bad & <name>')
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("3D/3dmodel.model").decode()
    assert "bad &amp; &lt;name>" in xml


# --- resolution ---------------------------------------------------------


@pytest.mark.parametrize(
    "longest, tool, expected",
    [
        (200, 0.4, 500),   # the case that started this: 200 mm on a 0.4 mm nozzle
        (100, 0.4, 250),
        (200, 0.8, 250),
        (200, 3.0, 67),    # a ball mill needs far less mesh than people assume
    ],
)
def test_recommended_grid(longest, tool, expected):
    assert recommended_grid(longest, tool) == expected


def test_recommended_grid_is_capped():
    assert recommended_grid(2000, 0.01) == MAX_GRID


@pytest.mark.parametrize("bad", [(0, 0.4), (200, 0), (-5, 1)])
def test_recommended_grid_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        recommended_grid(*bad)


def test_advice_flags_oversampling():
    over = advise(200, 800, 0.4)
    assert over.is_wasteful
    assert over.oversampled_by == pytest.approx(1.6)
    assert "finer than" in over.note()


def test_advice_accepts_a_matched_grid():
    ok = advise(200, 500, 0.4)
    assert not ok.is_wasteful
    assert ok.mm_per_sample == pytest.approx(0.4)
    assert "matched" in ok.note()


def test_advice_reports_the_real_stl_size():
    a = advise(200, 500, 0.4)
    assert a.triangles == triangle_count(500)
    assert a.stl_bytes == 84 + 50 * a.triangles


def test_triangle_count_matches_the_mesher():
    grid = 40
    solid = build_solid(np.zeros((grid, grid)), 1.0, 1.0, 1.0)
    assert triangle_count(grid) == solid.n_faces


@pytest.mark.parametrize("value, expected", [("fdm-0.4", 0.4), ("cnc-3mm", 3.0), ("0.25", 0.25), (1.5, 1.5)])
def test_resolve_tool(value, expected):
    assert resolve_tool(value) == expected


def test_resolve_tool_rejects_unknown_names():
    with pytest.raises(ValueError, match="unknown tool"):
        resolve_tool("laser-beam")


def test_every_preset_resolves():
    assert all(resolve_tool(name) > 0 for name in TOOL_PRESETS)
