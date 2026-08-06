# reliefkit

Printable 3D terrain models from public elevation data. Give it a bounding box,
get a watertight STL you can drop into a slicer or a CAM package.

```bash
reliefkit --bbox -121.85 46.75 -121.65 46.90 -o rainier.3mf \
  --size 200 --relief 25 --square --for-tool fdm-0.4
```

```
tool       : 0.4 mm -> 500 samples (0.40 mm/sample)
source     : usgs3dep (500x500 grid, elevation 693.9..4386.9 m)
model size : 200.0 x 200.0 x 30.0 mm
scale      : 1:83,376 horizontal, 0.56x vertical
mesh       : 503,990 triangles, 251,997 vertices
written    : rainier.3mf (5.1 MB)
```

## Install

```bash
pip install -e ".[dev,web]"
```

Requires Python 3.11+. `rasterio` pulls in GDAL, which ships as a binary wheel
on macOS, Linux and Windows.

## Docker

The fastest way to run it. No Python, no GDAL, no toolchain:

```bash
docker compose up -d            # then open http://127.0.0.1:8000
```

Or straight from the registry:

```bash
docker run --rm -p 127.0.0.1:8000:8000 ghcr.io/zfouts/reliefkit:latest
```

The image is built on `python:3.14-slim`. rasterio, numpy and pyproj all publish
manylinux wheels for CPython 3.14 on amd64 and arm64, and rasterio's wheels bundle
GDAL and PROJ — so nothing is compiled at build time and no system GDAL is
installed. Images are published for both architectures.

### Security posture

The container needs no privileges to serve HTTP and fetch elevation tiles, so it
is given none:

| | |
|---|---|
| User | `10001:10001`, system account, no home directory, `nologin` shell |
| Root filesystem | Read-only; only `/tmp` is writable, as `noexec,nosuid` tmpfs |
| Capabilities | All dropped (`cap_drop: ALL`) |
| Privilege escalation | Blocked (`no-new-privileges:true`) |
| Application code | Root-owned; the runtime user cannot modify its own code or dependencies |
| Port binding | Loopback only by default — there is no auth in front of this |

The build is multi-stage: pip, compilers, caches and the source tree stay in the
builder and never reach the shipped layer.

`docker compose up` applies all of the above. If you run `docker run` yourself,
the equivalent is:

```bash
docker run -d --name reliefkit \
  --user 10001:10001 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  -p 127.0.0.1:8000:8000 ghcr.io/zfouts/reliefkit:latest
```

Publishing on `0.0.0.0` exposes an unauthenticated app that will fetch arbitrary
regions on request. Put it behind a reverse proxy with auth first.

`GET /api/health` is the liveness probe. It deliberately does not check the
upstream elevation services — otherwise a USGS outage would have Docker restart a
container that is working perfectly.

## Web interface

Without Docker:

```bash
reliefkit-serve                 # http://127.0.0.1:8000
```

Hold <kbd>⌘</kbd>/<kbd>Ctrl</kbd> and drag on the map to select a region, tweak
the settings, and export. The 3D preview is the real mesh — same skirt, base and
triangulation the STL gets, built in the browser from the same algorithm.

Two things worth knowing about how it's wired:

**Scale changes never hit the network.** `/api/preview` returns raw elevations in
metres, and the browser does the millimetre scaling itself. Dragging the relief
slider re-renders instantly instead of firing a DEM request per frame at a public
USGS service. The server still recomputes the scaling authoritatively when it
builds the actual STL — the client copy only drives the preview.

**Export is synchronous.** A 1200-sample grid takes roughly ten seconds, which is
fine for one user and avoids dragging in a job queue. That's the first thing to
change if this ever grows past a single self-hosted instance.

The **Print bed** panel does the same tiling as `--bed`, and draws the cuts over
both the map and the 3D preview so you can see where the model comes apart before
committing. `/api/tile-plan` touches no elevation data at all — the split falls
out of ground extent and scale — so the layout updates as you type. Exporting
gives you a ZIP of every tile plus the manifest. Because each tile is its own
upstream fetch inside that one synchronous request, the server caps a browser job
at 64 tiles and tells you to use the CLI past that.

Basemaps come from OpenTopoMap and Esri World Imagery — no API key, but both have
usage policies aimed at low-volume use. Fine for a personal instance; point it at
your own tile server if you're doing anything heavier.

**Known limitation:** region selection needs modifier+drag, so it doesn't work on
touch devices. The layout is responsive and readable on a phone, but you'll want
a mouse to actually select anything.

## Data sources

Every source is free to use. Provenance is tracked per model and reported in
the run summary, because some of these require attribution when you publish.

| Source | Coverage | Resolution | Licence |
|---|---|---|---|
| `usgs3dep` | United States + territories | 1–30 m (best available) | Public domain (US Government work) |
| `copernicus` | Global, 85°N–85°S | 30 m | Free with attribution to ESA/Copernicus |

`--source auto` picks the highest-resolution source that covers your box.

## Scale modes

**Fit to size** (default) normalises the longest horizontal dimension to
`--size` and stretches the elevation range to exactly `--relief`. Use this when
the model has to fit a print bed or a stock blank.

```bash
reliefkit --bbox -105.7 40.2 -105.5 40.4 -o rmnp.stl --size 150 --relief 20 --base 6
```

**True scale** applies a real cartographic ratio to all three axes, with
optional vertical exaggeration. Relief then comes from the terrain itself.

```bash
reliefkit --bbox 6.8 45.8 7.0 45.95 -o mont-blanc.stl --true-scale 100000 --exaggeration 2
```

Real terrain is far wider than it is tall, so honest 1:1 vertical usually looks
flat — 1.5×–3× is the normal range for a display piece.

## Tiling for a print bed

Give `--bed` your machine's usable build area and `--size` becomes the
*assembled* object. reliefkit works out how many pieces that takes and writes
one printable file per tile.

```bash
# 240 km of the North Cascades as a 1 m wall panel, cut for a 200 mm printer
reliefkit --bbox -121.8 47.8 -120.4 48.9 -o cascades/ \
  --size 1000 --relief 30 --square --bed 200 --for-tool fdm-0.4
```

```
tool       : 0.4 mm -> 500 samples/tile (0.40 mm/sample)
bed        : 200 x 200 mm
tiles      : 5 x 5 = 25 tiles of 200.0 x 200.0 mm, assembling to 1000.0 x 1000.0 mm
per tile   : 500x500 samples, 503,990 triangles, 0.40 mm/sample
total      : 12,599,750 triangles, ~132 MB of 3mf
```

Add `--dry-run` to see that plan without fetching anything. The output directory
gets `tile_r01c01.3mf` … `tile_r05c05.3mf` (row 01 is north, column 01 is west),
plus a `manifest.json` and an `ASSEMBLY.txt` layout map. Point `-o` at a `.zip`
to get one archive instead.

| Flag | |
|---|---|
| `--bed X [Y]` | usable build area; one value for a square bed |
| `--bed-margin MM` | clearance kept on **each** bed edge |
| `--tiles COLS ROWS` | force a split instead of deriving one |
| `--no-bed-rotate` | don't consider turning tiles 90° on the bed |
| `--workers N` | parallel tile fetches (default 4) |

`--grid` and `--for-tool` are read **per tile**, since a tile is what actually
gets printed. On a non-square bed, reliefkit checks both orientations and takes
whichever needs fewer prints — 1000 × 600 mm on a 200 × 250 mm bed is 5×3
upright but 4×3 turned.

### Why the seams line up

Three rules, all in `tiling.py`:

**One elevation datum for the whole region.** Heights are measured from the
region's minimum and scaled by the region's range, so `--relief 30` spreads
across the entire 240 km. Scaling each tile against its own local min/max — the
obvious mistake — would step every seam by the difference between two local
minima. Each tile ends up a *different* height; only the one holding the highest
ground reaches the full relief.

**Uniform in degrees, linear degrees to millimetres.** Tiles divide the box
evenly in longitude and latitude, which is the same plate-carrée mapping the
single-model path already applies, and it makes every tile exactly the same size
in millimetres. Dividing by ground metres instead would make northern rows
narrower than southern ones and the assembled sheet wouldn't be a rectangle.

**A shared lattice, then reconciled seams.** Every tile is resampled to an
identical sample count, so a tile's east edge column corresponds one-to-one with
its neighbour's west edge column; those two lines are averaged and written back
into both. This is not cosmetic. Elevation services return pixel-centred
rasters, so a tile's edge column sits half a pixel *inside* the boundary it is
supposed to land on, and its neighbour's sits half a pixel inside from the other
direction. The two disagree by whatever the terrain does across one sample —
under what the tool can reproduce, but a visible ridge along every seam if left
alone.

The result is that adjacent tiles share their edge vertices to the bit, so the
pieces butt flat and glue without sanding. There's no joinery cut into the mesh;
every tile is an ordinary watertight solid with a flat base.

## Library use

```python
from reliefkit import BBox, ReliefSettings, generate_stl

result = generate_stl(
    BBox(-121.85, 46.75, -121.65, 46.90),
    "rainier.stl",
    ReliefSettings(target_size_mm=120, relief_height_mm=15, base_thickness_mm=5),
    square=True,
)
print(result.summary())
for warning in result.warnings:
    print("warning:", warning)
```

`build_model()` returns the mesh without writing it; `mesh_from_grid()` skips
the network entirely if you already hold a DEM.

Tiling has the same shape. `plan_layout()` is offline — it needs only ground
extent and scale — so you can size a job before committing to it:

```python
from reliefkit import BBox, BedSpec, ReliefSettings, build_tiled_model, plan_layout, write_tiles

region = BBox(-121.8, 47.8, -120.4, 48.9).to_square()
settings = ReliefSettings(target_size_mm=1000, relief_height_mm=30, max_grid=500)
bed = BedSpec.square(200)

print(plan_layout(region, settings, bed).describe())   # no network
model = build_tiled_model(region, bed, settings)
write_tiles(model, "cascades/", fmt="3mf")
```

`model.iter_tiles()` yields one meshed tile at a time rather than building them
all up front — a 5×5 split at 500 samples is 50 MB of elevation but well over a
gigabyte of geometry, so the writers mesh and emit each tile before starting the
next.

## How the mesh is built

The solid is three pieces that share vertices exactly:

- **top** — the terrain surface, two triangles per grid cell
- **walls** — a vertical skirt from every perimeter vertex down to z=0
- **bottom** — a triangle fan from one centre vertex out to those same
  perimeter vertices

Fanning the bottom is the part that matters. The obvious alternative — closing
the base with two corner-to-corner triangles — leaves the skirt's subdivided
lower edge meeting an unsubdivided base, which is a wall of T-junctions and a
mesh that slicers reject. The fan matches the skirt subdivision vertex for
vertex, so every edge belongs to exactly two triangles.

`Solid.is_watertight()` checks that property directly, and the test suite
asserts exact closed-form volumes for flat and ramped inputs — which only come
out right if the three pieces agree on both orientation and shared vertices.

## File size

Two independent things get confused here: **physical size** (`--size`) is just a
scale factor and has no effect on file size at all. File size is triangle count,
and triangle count is `--grid`.

The thing that actually matters is how the mesh spacing compares to the tool that
will reproduce it. A 0.4 mm nozzle cannot place a feature narrower than 0.4 mm, so
on a 200 mm model anything past ~500 samples is detail you pay for in bytes and
never see in plastic:

| `--grid` | mm/sample @200 mm | triangles | STL | 3MF |
|---|---|---|---|---|
| 250 | 0.80 | 127k | 6.3 MB | 1.3 MB |
| 400 | 0.50 | 323k | 16.2 MB | 3.3 MB |
| **500** | **0.40** — matched to a 0.4 mm nozzle | 504k | 25.2 MB | **5.1 MB** |
| 800 | 0.25 — 1.6× oversampled | 1.29M | 64.3 MB | 13.5 MB |
| 1200 | 0.17 — 2.4× oversampled | 2.89M | 144.5 MB | 30.3 MB |

Let `--for-tool` size the mesh instead of guessing:

```bash
reliefkit --bbox -121.85 46.75 -121.65 46.90 -o rainier.3mf \
  --size 200 --relief 25 --square --for-tool fdm-0.4
```

```
tool       : 0.4 mm -> 500 samples (0.40 mm/sample)
mesh       : 503,990 triangles, 251,997 vertices
written    : rainier.3mf (5.1 MB)
```

Presets: `fdm-0.4`, `fdm-0.6`, `fdm-0.8`, `resin`, `cnc-1mm`, `cnc-3mm`, `cnc-6mm`
— or pass a diameter in mm. The web UI has the same control, and warns inline when
the mesh is finer than the selected tool can reproduce.

When tiling, every number above is **per tile** and multiplies by the tile count:
that same 500-sample row is 5.1 MB one time on a single print, and 25 × 5.1 MB
across a 5×5 wall panel. `--grid` therefore defaults to 500 with `--bed`
(0.40 mm/sample on a 200 mm tile) rather than the 1200 a one-piece model gets.

## Output formats

| | STL | 3MF |
|---|---|---|
| Vertices | repeated 3× per triangle | indexed |
| Compression | none possible | deflate |
| Same mesh | 25.2 MB | **5.1 MB** |

3MF is roughly **5× smaller** for identical geometry and is what current slicers
prefer. Pick it with `--format 3mf` or just name the output `.3mf`.

Together, matching the tool *and* using 3MF takes the 200 mm Rainier model from
64 MB to 5.1 MB — a 12× reduction with nothing visible lost.

## Testing

```bash
pytest                     # offline: geometry, scaling, STL round-trip, API
pytest -m network          # also hits the live elevation services
```

The offline suite stubs the elevation sources, so it needs no network and is
deterministic. Geometry is checked by asserting exact closed-form volumes and by
verifying every mesh edge is used by exactly two triangles.

Tiling is checked against a *pixel-centred* fixture — one that samples half a
cell inside the box it was handed, exactly as the real services do — so
neighbouring tiles genuinely disagree along a shared edge before reconciliation.
The tests then lay the built tiles out in model space and assert their seam
vertices coincide to zero, both in memory and after a round trip through 3MF.

## Development

Claude Code was used on this codebase. There was always a human in the loop
every step of the process — directing the work, reviewing the output, and
deciding what shipped.

## Licence

MIT — see [LICENSE](LICENSE).

This is an independent implementation written from public documentation and
public data. It contains no third-party copyleft code — every dependency is
permissively licensed:

| | |
|---|---|
| numpy, rasterio, pyproj, requests | BSD / MIT / Apache-2.0 |
| fastapi, pydantic | MIT |
| uvicorn, starlette | BSD-3-Clause |
| three.js | MIT |
| MapLibre GL JS | BSD-3-Clause |

Note that basemap *tiles* are not code and carry their own terms: OpenTopoMap is
CC-BY-SA, Esri World Imagery is free for non-commercial use with attribution.
Both are credited in the map's attribution control.

Elevation data carries its own terms. USGS 3DEP output is public domain.
Copernicus DEM requires attribution to ESA/Copernicus — `ModelResult.attribution`
carries the correct string for whichever source produced a given model.
