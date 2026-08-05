"""Binary and ASCII STL writers.

Binary STL stores every triangle as three standalone vertices, so a large
terrain mesh expands hard: 50 bytes per triangle, and a 1200x1200 grid is
~2.9M triangles == ~140 MB. Writing is therefore chunked and streamed rather
than buffered whole.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO

import numpy as np

from .mesh import Solid, face_normals

# normal(3) + 3 vertices(9) + attribute byte count(1 uint16) = 50 bytes
_TRI = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
assert _TRI.itemsize == 50, "binary STL triangle record must be exactly 50 bytes"

_CHUNK = 200_000


def write_binary_stl(solid: Solid, path: str | Path, header: str = "") -> Path:
    """Write ``solid`` as a binary STL. Returns the path written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        _write_binary(solid, fh, header)
    return path


def _write_binary(solid: Solid, fh: BinaryIO, header: str) -> None:
    # An STL header must never start with "solid" or parsers guess ASCII.
    text = header.encode("ascii", errors="replace")[:79]
    if text[:5].lower() == b"solid":
        text = b"~" + text[:78]
    fh.write(text.ljust(80, b"\0"))
    fh.write(struct.pack("<I", solid.n_faces))

    normals = face_normals(solid)
    for start in range(0, solid.n_faces, _CHUNK):
        stop = min(start + _CHUNK, solid.n_faces)
        block = np.zeros(stop - start, dtype=_TRI)
        block["normal"] = normals[start:stop]
        block["v"] = solid.vertices[solid.faces[start:stop]]
        fh.write(block.tobytes())


def write_ascii_stl(solid: Solid, path: str | Path, name: str = "reliefkit") -> Path:
    """Write ``solid`` as ASCII STL. Much larger; useful for debugging."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    normals = face_normals(solid)
    tris = solid.vertices[solid.faces]
    with path.open("w", encoding="ascii") as fh:
        fh.write(f"solid {name}\n")
        for n, t in zip(normals, tris):
            fh.write(f"  facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}\n    outer loop\n")
            for v in t:
                fh.write(f"      vertex {v[0]:.6e} {v[1]:.6e} {v[2]:.6e}\n")
            fh.write("    endloop\n  endfacet\n")
        fh.write(f"endsolid {name}\n")
    return path


def estimated_binary_size(n_faces: int) -> int:
    """Bytes a binary STL with ``n_faces`` triangles will occupy."""
    return 84 + 50 * n_faces


def read_binary_stl(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Read a binary STL back as (normals, triangles). Used by the test-suite."""
    data = Path(path).read_bytes()
    (count,) = struct.unpack("<I", data[80:84])
    block = np.frombuffer(data, dtype=_TRI, count=count, offset=84)
    return np.array(block["normal"]), np.array(block["v"])
