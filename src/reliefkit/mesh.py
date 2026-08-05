"""Turn a heightfield into a watertight, printable solid.

The mesh is built from three parts that share vertices exactly, so the result
has no T-junctions and no duplicate-but-not-quite-equal boundary points:

* **top**    -- the terrain surface, two triangles per grid cell
* **walls**  -- a vertical skirt dropped from every perimeter vertex to z=0
* **bottom** -- a triangle fan from a single centre vertex out to the *same*
                perimeter vertices the walls land on

Fanning the bottom from a centre point (rather than triangulating it as two big
corner-to-corner triangles) is what keeps it watertight: the skirt's lower edge
is subdivided once per perimeter sample, and the fan matches that subdivision
vertex for vertex.

Winding is counter-clockwise seen from outside, so face normals point out.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Solid:
    """An indexed triangle mesh."""

    vertices: np.ndarray  # (V, 3) float64, millimetres
    faces: np.ndarray  # (F, 3) int64, indices into vertices

    @property
    def n_vertices(self) -> int:
        return int(self.vertices.shape[0])

    @property
    def n_faces(self) -> int:
        return int(self.faces.shape[0])

    @property
    def bounds_mm(self) -> tuple[np.ndarray, np.ndarray]:
        return self.vertices.min(axis=0), self.vertices.max(axis=0)

    def is_watertight(self) -> bool:
        """True when every edge is shared by exactly two triangles.

        This is the property slicers care about. It is an O(F log F) check, so
        it is cheap enough to run in tests on real-sized meshes.
        """
        f = self.faces
        edges = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
        edges = np.sort(edges, axis=1)
        _, counts = np.unique(edges, axis=0, return_counts=True)
        return bool(np.all(counts == 2))


def perimeter_indices(rows: int, cols: int) -> np.ndarray:
    """Grid indices tracing the boundary once, CCW seen from +Z.

    Row 0 is the north edge, so screen-space y decreases as the row index grows;
    the traversal below therefore starts at the south-west corner and runs east.
    """
    if rows < 2 or cols < 2:
        raise ValueError(f"grid must be at least 2x2, got {rows}x{cols}")

    def idx(r: int, c: int) -> int:
        return r * cols + c

    ring: list[int] = []
    ring += [idx(rows - 1, c) for c in range(cols)]  # SW -> SE
    ring += [idx(r, cols - 1) for r in range(rows - 2, -1, -1)]  # SE -> NE
    ring += [idx(0, c) for c in range(cols - 2, -1, -1)]  # NE -> NW
    ring += [idx(r, 0) for r in range(1, rows - 1)]  # NW -> SW (corners already in)
    return np.asarray(ring, dtype=np.int64)


def build_solid(
    heights_mm: np.ndarray,
    dx_mm: float,
    dy_mm: float,
    base_thickness_mm: float,
) -> Solid:
    """Build a closed solid from a regular heightfield.

    ``heights_mm`` is terrain height above the top of the base, already scaled
    to millimetres. Row 0 is north. The returned solid sits in the +X/+Y/+Z
    octant with its base plane at z=0.
    """
    heights_mm = np.asarray(heights_mm, dtype=np.float64)
    if heights_mm.ndim != 2:
        raise ValueError(f"heights_mm must be 2-D, got shape {heights_mm.shape}")
    if not np.isfinite(heights_mm).all():
        raise ValueError("heights_mm contains NaN or inf; fill nodata before meshing")
    rows, cols = heights_mm.shape
    if rows < 2 or cols < 2:
        raise ValueError(f"grid must be at least 2x2, got {rows}x{cols}")
    if base_thickness_mm < 0:
        raise ValueError("base_thickness_mm must be >= 0")

    # --- vertices -------------------------------------------------------
    xs = np.arange(cols, dtype=np.float64) * dx_mm
    ys = (rows - 1 - np.arange(rows, dtype=np.float64)) * dy_mm  # north at max Y
    grid_x, grid_y = np.meshgrid(xs, ys)
    top_z = heights_mm + base_thickness_mm

    top = np.column_stack([grid_x.ravel(), grid_y.ravel(), top_z.ravel()])
    n_top = top.shape[0]

    ring = perimeter_indices(rows, cols)
    n_ring = ring.size

    skirt = np.column_stack([top[ring, 0], top[ring, 1], np.zeros(n_ring)])
    centre = np.array([[xs.mean(), ys.mean(), 0.0]])

    vertices = np.concatenate([top, skirt, centre], axis=0)
    skirt0 = n_top
    centre_idx = n_top + n_ring

    # --- top surface ----------------------------------------------------
    r = np.arange(rows - 1, dtype=np.int64)[:, None]
    c = np.arange(cols - 1, dtype=np.int64)[None, :]
    v00 = (r * cols + c).ravel()
    v01 = v00 + 1
    v10 = v00 + cols
    v11 = v10 + 1
    faces_top = np.concatenate(
        [np.column_stack([v00, v10, v11]), np.column_stack([v00, v11, v01])]
    )

    # --- skirt walls ----------------------------------------------------
    step = np.arange(n_ring, dtype=np.int64)
    nxt = (step + 1) % n_ring
    top_i, top_n = ring, ring[nxt]
    bot_i, bot_n = skirt0 + step, skirt0 + nxt
    faces_wall = np.concatenate(
        [np.column_stack([top_i, bot_i, bot_n]), np.column_stack([top_i, bot_n, top_n])]
    )

    # --- bottom fan (reversed vs. the ring, so normals face -Z) ---------
    faces_bottom = np.column_stack([np.full(n_ring, centre_idx, dtype=np.int64), bot_n, bot_i])

    faces = np.concatenate([faces_top, faces_wall, faces_bottom])
    return Solid(vertices=vertices, faces=faces)


def face_normals(solid: Solid) -> np.ndarray:
    """Unit normals per face; degenerate faces get a zero vector."""
    tri = solid.vertices[solid.faces]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    length = np.linalg.norm(n, axis=1, keepdims=True)
    return np.divide(n, length, out=np.zeros_like(n), where=length > 0)


def signed_volume_mm3(solid: Solid) -> float:
    """Signed volume via the divergence theorem.

    Positive means outward-facing winding, which is a useful sanity check that
    the three sub-meshes agree on orientation.
    """
    tri = solid.vertices[solid.faces]
    return float(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)
