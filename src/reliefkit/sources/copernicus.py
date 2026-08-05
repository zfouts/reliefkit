"""Copernicus DEM GLO-30 -- global 30 m coverage from ESA.

Hosted as cloud-optimised GeoTIFFs on AWS Open Data with anonymous access, one
tile per 1x1 degree cell. Because they are COGs we can let GDAL range-request
just the window we need through ``/vsicurl/`` instead of pulling whole tiles.

Licence: free to use and redistribute with attribution to
"(c) DLR e.V. 2010-2014 / (c) Airbus Defence and Space GmbH ... provided under
COPERNICUS by the European Union and ESA".
"""

from __future__ import annotations

import math

import numpy as np
import rasterio
from rasterio.merge import merge

from ..dem import DEMGrid, fill_nodata
from ..geo import BBox
from .base import SourceError

_BUCKET = "https://copernicus-dem-30m.s3.amazonaws.com"
_ARCSEC = 1.0 / 3600.0
_MAX_TILES = 36


class CopernicusGLO30:
    name = "copernicus"
    licence = "Free with attribution (ESA / Copernicus)"
    attribution = "Contains modified Copernicus DEM GLO-30 data, (c) ESA / DLR / Airbus DS"

    def covers(self, bbox: BBox) -> bool:
        return bbox.south >= -85.0 and bbox.north <= 85.0

    def fetch(self, bbox: BBox, target_dim: int) -> DEMGrid:
        urls = _tile_urls(bbox)
        if len(urls) > _MAX_TILES:
            raise SourceError(
                f"region spans {len(urls)} Copernicus tiles (limit {_MAX_TILES}); select a smaller area"
            )

        res = _resolution_for(bbox, target_dim)
        datasets = []
        try:
            for url in urls:
                try:
                    datasets.append(rasterio.open(url))
                except rasterio.RasterioIOError:
                    continue  # absent tile == open water, filled in below
            if not datasets:
                raise SourceError(
                    "no Copernicus tiles found for this area (fully oceanic regions have no land tiles)"
                )
            mosaic, _ = merge(datasets, bounds=bbox.as_tuple(), res=res, nodata=np.nan)
        finally:
            for ds in datasets:
                ds.close()

        band = np.asarray(mosaic[0], dtype=np.float64)
        if min(band.shape) < 2:
            raise SourceError(f"region too small for 30 m data (got {band.shape[0]}x{band.shape[1]} samples)")

        values, n_filled = fill_nodata(band, None)
        return DEMGrid(values, bbox, source=self.name, nodata_filled=n_filled)


def _tile_name(lat: int, lon: int) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def _tile_urls(bbox: BBox) -> list[str]:
    """One URL per 1-degree cell the box touches (SW corner naming)."""
    lat0, lat1 = math.floor(bbox.south), math.ceil(bbox.north)
    lon0, lon1 = math.floor(bbox.west), math.ceil(bbox.east)
    urls = []
    for lat in range(lat0, lat1):
        for lon in range(lon0, lon1):
            name = _tile_name(lat, lon)
            urls.append(f"/vsicurl/{_BUCKET}/{name}/{name}.tif")
    return urls


def _resolution_for(bbox: BBox, target_dim: int) -> float:
    """Degrees per pixel, never finer than the native 1 arc-second grid."""
    span = max(bbox.east - bbox.west, bbox.north - bbox.south)
    return max(span / max(2, int(target_dim)), _ARCSEC)
