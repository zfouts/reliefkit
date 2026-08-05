"""USGS 3DEP -- the US National Map elevation service.

3DEP publishes a seamless best-available mosaic (1 m where lidar exists, else
1/9, 1/3 or 1 arc-second) through an ArcGIS ImageServer. ``exportImage`` will
resample any bounding box to any raster size in one request, which saves us
tile discovery and mosaicking entirely.

Data produced by the US Geological Survey is a US Government work and is in the
public domain. No API key is required.
"""

from __future__ import annotations

import io

import numpy as np
import rasterio
import requests

from ..dem import DEMGrid, fill_nodata
from ..geo import BBox
from .base import SourceError

_ENDPOINT = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"

# Conservative bound; the service rejects very large single requests.
_MAX_PIXELS = 4000
_NODATA = -9999.0

# 3DEP covers the US, its territories and a Mexico/Canada border buffer.
_COVERAGE = (-179.5, 14.0, -63.0, 72.0)


class USGS3DEP:
    name = "usgs3dep"
    licence = "Public domain (US Government work)"
    attribution = "Elevation data courtesy of the U.S. Geological Survey (3DEP)"

    def __init__(self, timeout: float = 120.0) -> None:
        self.timeout = timeout

    def covers(self, bbox: BBox) -> bool:
        w, s, e, n = _COVERAGE
        return bbox.west >= w and bbox.south >= s and bbox.east <= e and bbox.north <= n

    def fetch(self, bbox: BBox, target_dim: int) -> DEMGrid:
        cols, rows = _request_shape(bbox, target_dim)
        params = {
            "bbox": ",".join(f"{v:.10f}" for v in bbox.as_tuple()),
            "bboxSR": "4326",
            "imageSR": "4326",
            "size": f"{cols},{rows}",
            "format": "tiff",
            "pixelType": "F32",
            "noData": str(_NODATA),
            "noDataInterpretation": "esriNoDataMatchAny",
            "interpolation": "RSP_BilinearInterpolation",
            "f": "image",
        }
        try:
            resp = requests.get(_ENDPOINT, params=params, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SourceError(f"3DEP request failed: {exc}") from exc

        # On error the service answers 200 with a JSON body rather than a TIFF.
        if resp.content[:4] not in (b"II*\x00", b"MM\x00*"):
            detail = resp.text[:300].replace("\n", " ")
            raise SourceError(f"3DEP returned no raster (is the area covered?): {detail}")

        with rasterio.open(io.BytesIO(resp.content)) as ds:
            band = ds.read(1).astype(np.float64)
            nodata = ds.nodata if ds.nodata is not None else _NODATA

        values, n_filled = fill_nodata(band, nodata)
        return DEMGrid(values, bbox, source=self.name, nodata_filled=n_filled)


def _request_shape(bbox: BBox, target_dim: int) -> tuple[int, int]:
    """Pixel dimensions preserving ground aspect, capped at the service limit."""
    target = max(2, min(int(target_dim), _MAX_PIXELS))
    aspect = bbox.aspect
    if aspect >= 1.0:
        cols, rows = target, max(2, int(round(target / aspect)))
    else:
        rows, cols = target, max(2, int(round(target * aspect)))
    return cols, rows
