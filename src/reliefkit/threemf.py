"""3MF writer.

Binary STL stores three full vertex copies per triangle -- 50 bytes for what is
really 12 bytes of index -- and cannot be compressed in place. 3MF is a ZIP of
XML that references an indexed vertex list, so the same mesh lands roughly
3-4x smaller, and every current slicer prefers it.

Only the geometry subset of the spec is emitted: one object, one mesh, one build
item, millimetre units. That is all a terrain model needs.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterator

import numpy as np

from .mesh import Solid

_CHUNK = 40_000

_CONTENT_TYPES = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>"""

_RELS = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Target="/3D/3dmodel.model" Id="rel0" \
Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""

_MODEL_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def write_3mf(solid: Solid, target, name: str = "reliefkit terrain"):
    """Write ``solid`` as a 3MF package.

    ``target`` may be a path or any seekable binary file object, so the web API
    can build the package straight into memory.
    """
    if isinstance(target, (str, Path)):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
    path = target

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)

        # A bare ZipInfo defaults to ZIP_STORED and silently ignores the
        # ZipFile's compression setting -- which would ship the model XML
        # uncompressed and *larger* than the equivalent STL. Set it explicitly.
        info = zipfile.ZipInfo("3D/3dmodel.model")
        info.compress_type = zipfile.ZIP_DEFLATED
        info._compresslevel = 9
        with zf.open(info, "w") as fh:
            for block in _model_xml(solid, name):
                fh.write(block)
    return path


def _model_xml(solid: Solid, name: str) -> Iterator[bytes]:
    safe = name.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    yield (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{_MODEL_NS}">\n'
        f'<metadata name="Title">{safe}</metadata>\n'
        '<metadata name="Application">reliefkit</metadata>\n'
        '<resources><object id="1" type="model"><mesh><vertices>'
    ).encode()

    yield from _format_rows(solid.vertices, '<vertex x="%.3f" y="%.3f" z="%.3f"/>')
    yield b"</vertices><triangles>"
    yield from _format_rows(solid.faces, '<triangle v1="%d" v2="%d" v3="%d"/>')
    yield b'</triangles></mesh></object></resources><build><item objectid="1"/></build></model>'


def _format_rows(rows: np.ndarray, fmt: str) -> Iterator[bytes]:
    """Format an (N, 3) array in chunks.

    Chunked so a multi-million-triangle mesh never materialises as one giant
    Python string.
    """
    for start in range(0, rows.shape[0], _CHUNK):
        block = rows[start : start + _CHUNK]
        yield "".join([fmt % (a, b, c) for a, b, c in block]).encode()


def read_3mf(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """Read a 3MF back as (vertices, faces). Used by the test-suite."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("3D/3dmodel.model"))

    ns = {"m": _MODEL_NS}
    mesh = root.find(".//m:mesh", ns)
    verts = np.array(
        [[float(v.get("x")), float(v.get("y")), float(v.get("z"))] for v in mesh.find("m:vertices", ns)]
    )
    faces = np.array(
        [[int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))] for t in mesh.find("m:triangles", ns)],
        dtype=np.int64,
    )
    return verts, faces
