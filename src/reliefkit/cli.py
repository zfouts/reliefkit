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
from .tiling import BedSpec, build_tiled_model, plan_layout, write_tiles, write_tiles_zip

# A whole model at 1200 is a sensible ceiling for one print. A *tile* at 1200 is
# not: 25 of them is 43 million triangles. 500 across a 200 mm tile lands on
# 0.4 mm per sample, which is exactly what a standard nozzle can reproduce.
DEFAULT_GRID = 1200
DEFAULT_TILE_GRID = 500


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
            "  reliefkit --bbox 6.8 45.8 7.0 45.95 -o mont-blanc.stl --true-scale 100000 --exaggeration 2\n\n"
            "  # a 1 m wall panel of the North Cascades, cut for a 200 mm printer\n"
            "  reliefkit --bbox -121.8 47.8 -120.4 48.9 -o cascades/ --size 1000 --relief 30 \\\n"
            "      --square --bed 200 --for-tool fdm-0.4\n"
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
    p.add_argument("-o", "--out", required=True, help="output file, or a directory/.zip when tiling")
    p.add_argument(
        "--source",
        default="auto",
        choices=["auto", *SOURCES],
        help="elevation source (default: auto -- best resolution that covers the box)",
    )
    p.add_argument("--square", action="store_true", help="expand the box to 1:1 ground aspect")
    p.add_argument(
        "--grid",
        type=int,
        default=None,
        help=f"max grid samples on the long axis (default: {DEFAULT_GRID}, "
        f"or {DEFAULT_TILE_GRID} per tile when --bed is given)",
    )
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
    p.add_argument("--dry-run", action="store_true", help="report what would be built, then stop")

    scale = p.add_argument_group("scale")
    scale.add_argument(
        "--size", type=float, default=100.0, help="longest horizontal dimension in mm, assembled (default: 100)"
    )
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

    tiling = p.add_argument_group(
        "tiling",
        "Split a model too big for one print into pieces that butt together. "
        "--size then describes the *assembled* object.",
    )
    tiling.add_argument(
        "--bed",
        nargs="+",
        type=float,
        metavar="MM",
        help="usable build area in mm; one value for a square bed, or X Y. Enables tiling",
    )
    tiling.add_argument(
        "--bed-margin", type=float, default=0.0, metavar="MM", help="clearance kept on each bed edge (default: 0)"
    )
    tiling.add_argument(
        "--tiles",
        nargs=2,
        type=int,
        metavar=("COLS", "ROWS"),
        help="force this split instead of deriving one from --bed",
    )
    tiling.add_argument(
        "--no-bed-rotate", action="store_true", help="do not consider rotating tiles 90 degrees on the bed"
    )
    tiling.add_argument(
        "--workers", type=int, default=4, metavar="N", help="parallel tile fetches (default: 4)"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        bbox = BBox(*args.bbox)
    except ValueError as exc:
        print(f"error: invalid --bbox: {exc}", file=sys.stderr)
        return 2

    tiled = args.bed is not None
    if args.tiles and not tiled:
        print("error: --tiles needs --bed as well, so tile size can be checked against the machine", file=sys.stderr)
        return 2

    grid = args.grid if args.grid is not None else (DEFAULT_TILE_GRID if tiled else DEFAULT_GRID)
    try:
        settings = _settings(args, grid)
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

    try:
        if tiled:
            return _run_tiled(args, bbox, settings, tool_mm)
        return _run_single(args, bbox, settings, tool_mm)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _settings(args: argparse.Namespace, grid: int) -> ReliefSettings:
    if args.true_scale:
        return ReliefSettings(
            scale_mode="true",
            scale_denominator=args.true_scale,
            z_exaggeration=args.exaggeration,
            base_thickness_mm=args.base,
            max_grid=grid,
        )
    return ReliefSettings(
        scale_mode="fit",
        target_size_mm=args.size,
        relief_height_mm=args.relief,
        base_thickness_mm=args.base,
        max_grid=grid,
    )


# ── one model ──────────────────────────────────────────────────────────


def _run_single(args: argparse.Namespace, bbox: BBox, settings: ReliefSettings, tool_mm: float | None) -> int:
    if tool_mm is not None:
        # Physical size is known up front in fit mode; in true mode it falls out
        # of the ground extent and the cartographic ratio.
        probe = bbox.to_square() if args.square else bbox
        longest_mm = (
            args.size if not args.true_scale else max(probe.width_m, probe.height_m) * 1000.0 / args.true_scale
        )
        grid = recommended_grid(longest_mm, tool_mm)
        settings = replace(settings, max_grid=grid)
        note = advise(longest_mm, grid, tool_mm)
        print(f"tool       : {tool_mm:g} mm -> {grid} samples ({note.mm_per_sample:.2f} mm/sample)")

    print(f"region     : {bbox.to_square() if args.square else bbox}")
    if args.dry_run:
        print("dry run    : nothing fetched or written")
        return 0

    print("fetching elevation ...")
    result = generate_stl(
        bbox, args.out, settings=settings, source=args.source, square=args.square, fmt=args.format
    )

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


# ── tiled ──────────────────────────────────────────────────────────────


def _run_tiled(args: argparse.Namespace, bbox: BBox, settings: ReliefSettings, tool_mm: float | None) -> int:
    if len(args.bed) == 1:
        bed_x = bed_y = args.bed[0]
    elif len(args.bed) == 2:
        bed_x, bed_y = args.bed
    else:
        print(f"error: --bed takes one or two values, got {len(args.bed)}", file=sys.stderr)
        return 2

    bed = BedSpec(bed_x, bed_y, args.bed_margin, allow_rotation=not args.no_bed_rotate)
    tiles = tuple(args.tiles) if args.tiles else None
    layout = plan_layout(bbox, settings, bed, square=args.square, tiles=tiles)

    if tool_mm is not None:
        # Per *tile*: a tile is what gets printed, so it is what the tool width
        # has an opinion about.
        grid = recommended_grid(max(layout.tile_w_mm, layout.tile_h_mm), tool_mm)
        settings = replace(settings, max_grid=grid)
        note = advise(max(layout.tile_w_mm, layout.tile_h_mm), grid, tool_mm)
        print(f"tool       : {tool_mm:g} mm -> {grid} samples/tile ({note.mm_per_sample:.2f} mm/sample)")

    fmt = _tile_format(args)
    out = Path(args.out)
    as_zip = out.suffix.lower() == ".zip"
    est = layout.estimate(settings.max_grid)
    bytes_total = est["bytes_total_3mf"] if fmt == "3mf" else est["bytes_total"]

    print(f"region     : {bbox.to_square() if args.square else bbox}")
    print(f"bed        : {bed}")
    print(f"tiles      : {layout.describe()}")
    print(f"per tile   : {est['sample_rows']}x{est['sample_cols']} samples, "
          f"{est['triangles_per_tile']:,} triangles, {est['mm_per_sample']:.2f} mm/sample")
    print(f"total      : {est['triangles_total']:,} triangles, ~{bytes_total / 1e6:.0f} MB of {fmt}")

    if not layout.fits_bed:
        print(
            f"error: a {layout.tile_w_mm:.1f} x {layout.tile_h_mm:.1f} mm tile does not fit {bed}",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        print("dry run    : nothing fetched or written")
        return 0

    model = build_tiled_model(
        bbox,
        bed,
        settings=settings,
        source=args.source,
        square=args.square,
        tiles=tiles,
        workers=args.workers,
        progress=_progress("fetching"),
    )

    if as_zip:
        write_tiles_zip(model, out, fmt=fmt, progress=_progress("writing"))
        written = [out]
    else:
        written = write_tiles(model, out, fmt=fmt, progress=_progress("writing"))

    print()
    print(model.summary())
    size = sum(p.stat().st_size for p in written)
    where = str(out) if as_zip else f"{out}{'' if str(out).endswith('/') else '/'}"
    print(f"written    : {len(written)} files to {where} ({size / 1e6:.1f} MB)")
    print("             manifest.json and ASSEMBLY.txt describe the layout")

    for warning in model.warnings:
        print(f"warning    : {warning}", file=sys.stderr)
    return 0


def _tile_format(args: argparse.Namespace) -> str:
    """Resolve the per-tile format.

    ``auto`` cannot read an extension off a directory, and tiling multiplies
    file size by the tile count, so it resolves to 3MF rather than STL.
    """
    if args.format != "auto":
        return args.format
    return "stl" if str(args.out).lower().endswith(".stl.zip") else "3mf"


def _progress(label: str):
    """Single-line progress on a terminal, one line per step when piped."""
    tty = sys.stdout.isatty()

    def report(done: int, total: int, message: str) -> None:
        line = f"{label:<11}: {done}/{total} ({message})"
        if tty:
            print(f"\r\033[K{line}", end="", flush=True)
            if done == total:
                print()
        else:
            print(line, flush=True)

    return report


if __name__ == "__main__":
    raise SystemExit(main())
