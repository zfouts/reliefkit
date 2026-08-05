"""Geometric correctness of the solid builder.

The volume tests matter more than they look: the top surface, the skirt and the
bottom fan are built independently, so an exact analytic volume is a strong
signal that all three agree on orientation *and* share vertices properly.
"""

from __future__ import annotations

import numpy as np
import pytest

from reliefkit.mesh import build_solid, face_normals, perimeter_indices, signed_volume_mm3


def test_perimeter_traces_boundary_once():
    rows, cols = 4, 5
    ring = perimeter_indices(rows, cols)
    assert ring.size == 2 * rows + 2 * cols - 4
    assert len(set(ring.tolist())) == ring.size, "perimeter must not repeat a vertex"

    on_edge = {
        r * cols + c
        for r in range(rows)
        for c in range(cols)
        if r in (0, rows - 1) or c in (0, cols - 1)
    }
    assert set(ring.tolist()) == on_edge


@pytest.mark.parametrize("shape", [(2, 2), (3, 7), (17, 5), (40, 40)])
def test_solid_is_watertight(shape):
    rng = np.random.default_rng(0)
    heights = rng.uniform(0, 10, size=shape)
    solid = build_solid(heights, dx_mm=1.0, dy_mm=1.0, base_thickness_mm=3.0)
    assert solid.is_watertight()


@pytest.mark.parametrize("shape", [(2, 2), (5, 9), (30, 30)])
def test_winding_is_outward(shape):
    rng = np.random.default_rng(1)
    heights = rng.uniform(0, 5, size=shape)
    solid = build_solid(heights, dx_mm=2.0, dy_mm=2.0, base_thickness_mm=1.0)
    assert signed_volume_mm3(solid) > 0


def test_flat_plate_has_exact_volume():
    rows, cols = 11, 21
    dx = dy = 2.0
    terrain, base = 4.0, 3.0
    solid = build_solid(np.full((rows, cols), terrain), dx, dy, base)

    width = (cols - 1) * dx
    height = (rows - 1) * dy
    assert signed_volume_mm3(solid) == pytest.approx(width * height * (terrain + base))


def test_linear_ramp_has_exact_volume():
    """A planar top is integrated exactly by the triangulation, so we can check
    the volume in closed form even though the surface is not flat."""
    rows, cols = 9, 13
    dx = dy = 1.5
    slope, base = 0.8, 2.0
    xs = np.arange(cols) * dx
    heights = np.tile(slope * xs, (rows, 1))

    solid = build_solid(heights, dx, dy, base)
    width = (cols - 1) * dx
    height = (rows - 1) * dy
    expected = width * height * base + slope * width**2 / 2 * height
    assert signed_volume_mm3(solid) == pytest.approx(expected)


def test_face_and_vertex_counts():
    rows, cols = 6, 8
    solid = build_solid(np.zeros((rows, cols)), 1.0, 1.0, 1.0)
    ring = 2 * rows + 2 * cols - 4
    assert solid.n_faces == 2 * (rows - 1) * (cols - 1) + 2 * ring + ring
    assert solid.n_vertices == rows * cols + ring + 1


def test_base_plane_sits_at_zero_and_north_is_up():
    rows, cols = 5, 5
    heights = np.zeros((rows, cols))
    heights[0, :] = 10.0  # north edge raised
    solid = build_solid(heights, 1.0, 1.0, base_thickness_mm=2.0)

    lo, hi = solid.bounds_mm
    assert lo[2] == pytest.approx(0.0)
    assert hi[2] == pytest.approx(12.0)

    # The tall row must land at maximum Y.
    tall = solid.vertices[solid.vertices[:, 2] > 11.0]
    assert np.allclose(tall[:, 1], hi[1])


def test_top_normals_point_up():
    rng = np.random.default_rng(2)
    rows, cols = 12, 12
    solid = build_solid(rng.uniform(0, 2, (rows, cols)), 1.0, 1.0, 1.0)
    n_top = 2 * (rows - 1) * (cols - 1)
    assert np.all(face_normals(solid)[:n_top][:, 2] > 0)


def test_zero_base_still_closes():
    solid = build_solid(np.ones((6, 6)), 1.0, 1.0, base_thickness_mm=0.0)
    assert solid.is_watertight()
    assert signed_volume_mm3(solid) > 0


@pytest.mark.parametrize(
    "heights, message",
    [
        (np.zeros((1, 5)), "at least 2x2"),
        (np.full((3, 3), np.nan), "NaN"),
        (np.zeros(5), "2-D"),
    ],
)
def test_invalid_input_rejected(heights, message):
    with pytest.raises(ValueError, match=message):
        build_solid(heights, 1.0, 1.0, 1.0)
