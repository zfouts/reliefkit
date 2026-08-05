"""Source protocol shared by every elevation provider."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..dem import DEMGrid
from ..geo import BBox


class SourceError(RuntimeError):
    """A source could not supply elevation data for the requested area."""


@runtime_checkable
class DEMSource(Protocol):
    """Fetches elevation grids for a bounding box."""

    name: str
    licence: str
    attribution: str

    def covers(self, bbox: BBox) -> bool:
        """Whether this source has data for the whole box."""
        ...

    def fetch(self, bbox: BBox, target_dim: int) -> DEMGrid:
        """Return a grid whose longest side is roughly ``target_dim`` samples."""
        ...
