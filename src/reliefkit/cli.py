"""Command-line entry point: ``reliefkit``."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .geo import BBox
from .pipeline import generate_stl
from .resolution import TOOL_PRESETS, advise, recommended_grid, resolve_tool
from .settings import ReliefSettings
from .sources import SOURCES, SourceError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reliefkit",
        description="Turn public elevation data into printable terrain models.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  # Mount Rainier, 120 mm wide, 15 mm of relief\n"
            "  reliefkit --bbox -121.85 46.75 -121.65 46.90 -o rainier.stl --size 120 --relief 15\n\n"
            "  # true 1:100000 scale with 2x vertical exaggeration\n"
            "  reliefkit --bbox 6.8 45.8 7.0 45.95 -o mont-blanc.stl --true-scale 100000 --exaggeration 2\n"
        ),
    )
    p.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        required=True,
        metavar=("W", "S", "E", "N"),
        help="bounding box in WGS84 degrees: west south east north",
    )
    p.add_argument("-o", "--out", required=True, help="output STL path")
    p.add_argument(
        "--source",
        default="auto",
        choices=["auto", *SOURCES],
        help="elevation source (default: auto -- best resolution that covers the box)",
    )
    p.add_argument("--square", action="store_true", help="expand the box to 1:1 ground aspect")
    p.add_argument("--grid", type=int, default=1200, help="max grid samples on the long axis (default: 1200)")
    p.add_argument(
        "--format",
        choices=["auto", "stl", "3mf"],
        default="auto",
        help="output format; auto picks from the file extension (default: auto). "
        "3mf is ~5x smaller than stl for the same mesh",
    )
    p.add_argument(
        "--for-tool",
        metavar="MM|PRESET",
        help="size the mesh to a nozzle/bit width instead of --grid. Accepts a diameter "
        f"in mm or one of: {', '.join(TOOL_PRESETS)}",
    )

    scale = p.add_argument_group("scale")
    scale.add_argument("--size", type=float, default=100.0, help="longest horizontal dimension in mm (default: 100)")
    scale.add_argument("--relief", type=float, default=12.0, help="terrain height above the base in mm (default: 12)")
    scale.add_argument("--base", type=float, default=5.0, help="base thickness in mm (default: 5)")
    scale.add_argument(
        "--true-scale",
        type=float,
        metavar="D",
        help="use true cartographic scale 1:D instead of fitting to --size",
    )
    scale.add_argument(
        "--exaggeration", type=float, default=1.0, help="vertical exaggeration, true-scale mode only (default: 1)"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        bbox = BBox(*args.bbox)
    except ValueError as exc:
        print(f"error: invalid --bbox: {exc}", file=sys.stderr)
        return 2

    try:
        if args.true_scale:
            settings = ReliefSettings(
                scale_mode="true",
                scale_denominator=args.true_scale,
                z_exaggeration=args.exaggeration,
                base_thickness_mm=args.base,
                max_grid=args.grid,
            )
        else:
            settings = ReliefSettings(
                scale_mode="fit",
                target_size_mm=args.size,
                relief_height_mm=args.relief,
                base_thickness_mm=args.base,
                max_grid=args.grid,
            )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    tool_mm = None
    if args.for_tool:
        try:
            tool_mm = resolve_tool(args.for_tool)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        # Physical size is known up front in fit mode; in true mode it falls out
        # of the ground extent and the cartographic ratio.
        probe = bbox.to_square() if args.square else bbox
        longest_mm = (
            args.size
            if not args.true_scale
            else max(probe.width_m, probe.height_m) * 1000.0 / args.true_scale
        )
        grid = recommended_grid(longest_mm, tool_mm)
        settings = replace(settings, max_grid=grid)
        note = advise(longest_mm, grid, tool_mm)
        print(f"tool   : {tool_mm:g} mm -> {grid} samples ({note.mm_per_sample:.2f} mm/sample)")

    print(f"region : {bbox.to_square() if args.square else bbox}")
    print("fetching elevation ...")

    try:
        result = generate_stl(
            bbox, args.out, settings=settings, source=args.source, square=args.square, fmt=args.format
        )
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    print(result.summary())
    written = Path(args.out).stat().st_size
    print(f"written    : {args.out} ({written / 1e6:.1f} MB)")

    check = result.advice(tool_mm or 0.4)
    if check.is_wasteful:
        print(f"note       : {check.note()}")
        better = recommended_grid(max(result.size_mm[0], result.size_mm[1]), check.tool_mm)
        print(f"             --for-tool {check.tool_mm:g} would use --grid {better} instead")
    if args.format == "auto" and not str(args.out).lower().endswith(".3mf"):
        print("note       : the same mesh as .3mf is roughly 5x smaller")
    for warning in result.warnings:
        print(f"warning    : {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
