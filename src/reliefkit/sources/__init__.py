"""Elevation data sources.

Every source here is free to use and redistributable. Provenance matters for a
project that ships models, so each one documents its licence:

============== ========================= =====================================
source          coverage                  licence
============== ========================= =====================================
``usgs3dep``    United States, 1-30 m     Public domain (US Government work)
``copernicus``  Global 30 m (85N-85S)     Free, attribution to ESA/Copernicus
============== ========================= =====================================
"""

from __future__ import annotations

from .base import DEMSource, SourceError
from .copernicus import CopernicusGLO30
from .usgs3dep import USGS3DEP

SOURCES: dict[str, DEMSource] = {
    "usgs3dep": USGS3DEP(),
    "copernicus": CopernicusGLO30(),
}


def get_source(name: str) -> DEMSource:
    """Look up a source by name, or pick one automatically with ``"auto"``."""
    if name != "auto":
        try:
            return SOURCES[name]
        except KeyError:
            raise SourceError(f"unknown source {name!r}; choose from {', '.join(SOURCES)} or 'auto'") from None
    return SOURCES["usgs3dep"]


def choose_source(bbox) -> DEMSource:  # noqa: ANN001 - avoids a circular import
    """Highest-resolution source that covers ``bbox``."""
    for source in (SOURCES["usgs3dep"], SOURCES["copernicus"]):
        if source.covers(bbox):
            return source
    raise SourceError(f"no configured source covers {bbox}")


__all__ = ["DEMSource", "SourceError", "SOURCES", "get_source", "choose_source", "USGS3DEP", "CopernicusGLO30"]
