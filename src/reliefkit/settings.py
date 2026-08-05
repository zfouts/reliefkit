"""User-facing model settings and the derived scale factors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ScaleMode = Literal["fit", "true"]


@dataclass(frozen=True)
class ReliefSettings:
    """How a patch of terrain becomes a physical object.

    Two scale modes:

    ``fit``
        Normalise the longest horizontal dimension to ``target_size_mm`` and
        stretch the elevation range to exactly ``relief_height_mm``. This is
        what you want when the model has to fit a print bed or a stock blank.

    ``true``
        Apply a real cartographic scale (``1 : scale_denominator``) to all three
        axes, optionally exaggerating Z. Vertical relief then falls out of the
        terrain itself rather than being dialled in.
    """

    scale_mode: ScaleMode = "fit"

    # fit mode
    target_size_mm: float = 100.0
    relief_height_mm: float = 12.0

    # true mode
    scale_denominator: float | None = None
    z_exaggeration: float = 1.0

    base_thickness_mm: float = 5.0
    max_grid: int = 1200

    def __post_init__(self) -> None:
        if self.scale_mode not in ("fit", "true"):
            raise ValueError(f"scale_mode must be 'fit' or 'true', got {self.scale_mode!r}")
        if self.base_thickness_mm < 0:
            raise ValueError("base_thickness_mm must be >= 0")
        if self.max_grid < 2:
            raise ValueError("max_grid must be >= 2")
        if self.scale_mode == "fit":
            if self.target_size_mm <= 0:
                raise ValueError("target_size_mm must be > 0 in fit mode")
            if self.relief_height_mm <= 0:
                raise ValueError("relief_height_mm must be > 0 in fit mode")
        else:
            if not self.scale_denominator or self.scale_denominator <= 0:
                raise ValueError("scale_denominator must be > 0 in true mode")
            if self.z_exaggeration <= 0:
                raise ValueError("z_exaggeration must be > 0")

    def horizontal_mm_per_m(self, width_m: float, height_m: float) -> float:
        """Model millimetres per real-world ground metre, on the XY plane."""
        if self.scale_mode == "fit":
            return self.target_size_mm / max(width_m, height_m)
        # 1 : D means one real metre becomes 1/D metres == 1000/D millimetres.
        return 1000.0 / float(self.scale_denominator)

    def vertical_mm_per_m(self, elev_range_m: float, width_m: float, height_m: float) -> float:
        """Model millimetres per real-world elevation metre.

        In ``fit`` mode a flat tile would divide by zero, so a zero elevation
        range collapses to a flat top rather than raising.
        """
        if self.scale_mode == "fit":
            if elev_range_m <= 0:
                return 0.0
            return self.relief_height_mm / elev_range_m
        return self.horizontal_mm_per_m(width_m, height_m) * self.z_exaggeration

    @property
    def vertical_exaggeration_hint(self) -> str:
        if self.scale_mode == "true":
            return f"{self.z_exaggeration:g}x"
        return "auto (fit mode)"
