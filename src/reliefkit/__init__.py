"""reliefkit -- printable terrain models from public elevation data."""

from .dem import DEMGrid
from .geo import BBox
from .mesh import Solid, build_solid
from .pipeline import ModelResult, build_model, generate_stl, mesh_from_grid, write_model
from .resolution import TOOL_PRESETS, advise, recommended_grid, resolve_tool
from .threemf import write_3mf
from .settings import ReliefSettings
from .sources import SOURCES, DEMSource, SourceError
from .stl import write_ascii_stl, write_binary_stl

__version__ = "0.1.0"

__all__ = [
    "BBox",
    "DEMGrid",
    "DEMSource",
    "ModelResult",
    "ReliefSettings",
    "SOURCES",
    "Solid",
    "SourceError",
    "build_model",
    "build_solid",
    "generate_stl",
    "mesh_from_grid",
    "write_model",
    "write_3mf",
    "recommended_grid",
    "advise",
    "resolve_tool",
    "TOOL_PRESETS",
    "write_ascii_stl",
    "write_binary_stl",
    "__version__",
]
