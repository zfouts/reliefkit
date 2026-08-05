"""Matching mesh density to what a machine can actually cut or print.

Sampling terrain finer than the tool that will reproduce it adds file size and
nothing else. A 0.4 mm nozzle cannot place a feature narrower than 0.4 mm, so on
a 200 mm model roughly 500 samples across is the ceiling; 800 samples is 1.6x
finer than anything the printer can render, and costs 2.5x the bytes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Typical tool diameters in millimetres.
TOOL_PRESETS: dict[str, float] = {
    "fdm-0.4": 0.4,
    "fdm-0.6": 0.6,
    "fdm-0.8": 0.8,
    "resin": 0.1,
    "cnc-1mm": 1.0,
    "cnc-3mm": 3.0,
    "cnc-6mm": 6.0,
}

MAX_GRID = 2000


@dataclass(frozen=True)
class ResolutionAdvice:
    """What a given grid buys you on a model of a given physical size."""

    grid: int
    mm_per_sample: float
    tool_mm: float
    triangles: int
    stl_bytes: int

    @property
    def oversampled_by(self) -> float:
        """How many times finer than the tool this grid samples. <=1 is sensible."""
        return self.tool_mm / self.mm_per_sample if self.mm_per_sample else float("inf")

    @property
    def is_wasteful(self) -> bool:
        # 15% slack so a borderline choice is not nagged about.
        return self.oversampled_by > 1.15

    def note(self) -> str:
        if self.is_wasteful:
            return (
                f"{self.mm_per_sample:.2f} mm/sample is {self.oversampled_by:.1f}x finer than a "
                f"{self.tool_mm:g} mm tool can reproduce"
            )
        return f"{self.mm_per_sample:.2f} mm/sample, matched to a {self.tool_mm:g} mm tool"


def recommended_grid(longest_mm: float, tool_mm: float) -> int:
    """Samples across the long axis so one sample lands per tool width."""
    if longest_mm <= 0 or tool_mm <= 0:
        raise ValueError("longest_mm and tool_mm must both be > 0")
    return max(2, min(MAX_GRID, math.ceil(longest_mm / tool_mm)))


def triangle_count(grid: int, aspect: float = 1.0) -> int:
    """Triangles a solid built at this grid will have, including skirt and base."""
    if aspect >= 1.0:
        cols, rows = grid, max(2, round(grid / aspect))
    else:
        rows, cols = grid, max(2, round(grid * aspect))
    ring = 2 * rows + 2 * cols - 4
    return 2 * (rows - 1) * (cols - 1) + 3 * ring


def advise(longest_mm: float, grid: int, tool_mm: float, aspect: float = 1.0) -> ResolutionAdvice:
    """Describe what ``grid`` delivers for a model ``longest_mm`` across."""
    tris = triangle_count(grid, aspect)
    return ResolutionAdvice(
        grid=grid,
        mm_per_sample=longest_mm / grid if grid else float("inf"),
        tool_mm=tool_mm,
        triangles=tris,
        stl_bytes=84 + 50 * tris,
    )


def resolve_tool(name_or_mm: str | float) -> float:
    """Accept either a preset name (``"fdm-0.4"``) or a diameter in millimetres."""
    if isinstance(name_or_mm, (int, float)):
        return float(name_or_mm)
    key = str(name_or_mm).strip().lower()
    if key in TOOL_PRESETS:
        return TOOL_PRESETS[key]
    try:
        return float(key)
    except ValueError:
        raise ValueError(
            f"unknown tool {name_or_mm!r}; use a diameter in mm or one of: {', '.join(TOOL_PRESETS)}"
        ) from None
