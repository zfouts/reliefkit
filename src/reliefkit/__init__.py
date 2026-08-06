"""reliefkit -- printable terrain models from public elevation data."""

from importlib.metadata import PackageNotFoundError, version as _distribution_version

from .dem import DEMGrid
from .geo import BBox
from .mesh import Solid, build_solid
from .pipeline import ModelResult, build_model, generate_stl, mesh_from_grid, write_model
from .resolution import TOOL_PRESETS, advise, recommended_grid, resolve_tool
from .threemf import write_3mf
from .settings import ReliefSettings
from .sources import SOURCES, DEMSource, SourceError
from .stl import write_ascii_stl, write_binary_stl
from .tiling import (
    BedSpec,
    TileLayout,
    TiledModel,
    TilePiece,
    build_tiled_model,
    plan_layout,
    write_tiles,
    write_tiles_zip,
)

try:
    __version__ = _distribution_version("reliefkit")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"
"""Version of the installed distribution.

Read from package metadata rather than hardcoded here, so ``pyproject.toml`` is
the only place a version number lives. A second copy in this file is exactly
how ``/api/health`` came to report 0.1.0 from an image tagged 1.0.1 -- and a
health endpoint that misreports its own version cannot be used to verify what
is actually deployed. CI now refuses to publish a ``v*`` tag that disagrees
with the version below.
"""

__all__ = [
    "BBox",
    "BedSpec",
    "DEMGrid",
    "DEMSource",
    "ModelResult",
    "ReliefSettings",
    "SOURCES",
    "Solid",
    "SourceError",
    "TileLayout",
    "TiledModel",
    "TilePiece",
    "build_model",
    "build_solid",
    "build_tiled_model",
    "generate_stl",
    "mesh_from_grid",
    "plan_layout",
    "write_model",
    "write_3mf",
    "write_tiles",
    "write_tiles_zip",
    "recommended_grid",
    "advise",
    "resolve_tool",
    "TOOL_PRESETS",
    "write_ascii_stl",
    "write_binary_stl",
    "__version__",
]
