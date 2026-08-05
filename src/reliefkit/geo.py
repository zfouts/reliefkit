"""Geographic bounding boxes and real-world sizing."""

from __future__ import annotations

from dataclasses import dataclass

from pyproj import Geod

_GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True)
class BBox:
    """A WGS84 lon/lat bounding box.

    Coordinates are in decimal degrees. ``west``/``east`` are longitudes,
    ``south``/``north`` are latitudes.
    """

    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        if not -180.0 <= self.west < self.east <= 180.0:
            raise ValueError(f"longitudes must satisfy -180 <= west < east <= 180, got {self.west}, {self.east}")
        if not -90.0 <= self.south < self.north <= 90.0:
            raise ValueError(f"latitudes must satisfy -90 <= south < north <= 90, got {self.south}, {self.north}")

    @property
    def center(self) -> tuple[float, float]:
        return (self.west + self.east) / 2.0, (self.south + self.north) / 2.0

    @property
    def width_m(self) -> float:
        """Ground width in metres, measured along the mid-latitude parallel."""
        _, mid_lat = self.center
        _, _, dist = _GEOD.inv(self.west, mid_lat, self.east, mid_lat)
        return dist

    @property
    def height_m(self) -> float:
        """Ground height in metres, measured along the mid-longitude meridian."""
        mid_lon, _ = self.center
        _, _, dist = _GEOD.inv(mid_lon, self.south, mid_lon, self.north)
        return dist

    @property
    def aspect(self) -> float:
        """Ground width divided by ground height."""
        return self.width_m / self.height_m

    def to_square(self, tol: float = 1e-9, max_iter: int = 20) -> BBox:
        """Expand the shorter ground axis so the box is 1:1 in real-world metres.

        Longitude degrees shrink with latitude, so a square-on-the-ground box is
        not a square in degrees. Expanding (rather than cropping) guarantees the
        user's whole selection stays inside the result.

        Degrees do not convert to metres by a constant: a geodesic between two
        points at equal latitude bulges poleward, so it is not the parallel arc,
        and metres-per-degree varies across the span being solved for. A single
        linear extrapolation is therefore off by a few parts per million. This
        instead iterates the half-span against the same measurement the rest of
        the class uses, which converges in two or three passes and is exact by
        construction rather than by approximation.
        """
        mid_lon, mid_lat = self.center
        target = max(self.width_m, self.height_m)
        widen = self.width_m < self.height_m
        if abs(self.width_m - self.height_m) < tol:
            return self

        # Seed from a local linear estimate, then refine.
        if widen:
            _, _, m_per_deg = _GEOD.inv(mid_lon, mid_lat, mid_lon + 0.01, mid_lat)
        else:
            _, _, m_per_deg = _GEOD.inv(mid_lon, mid_lat, mid_lon, mid_lat + 0.01)
        half = (target / (m_per_deg * 100.0)) / 2.0

        box = self
        for _ in range(max_iter):
            if widen:
                box = BBox(mid_lon - half, self.south, mid_lon + half, self.north)
                current = box.width_m
            else:
                box = BBox(self.west, mid_lat - half, self.east, mid_lat + half)
                current = box.height_m
            if abs(current - target) <= tol * target:
                break
            half *= target / current
        return box

    def as_tuple(self) -> tuple[float, float, float, float]:
        return self.west, self.south, self.east, self.north

    def __str__(self) -> str:
        return (
            f"BBox({self.west:.5f}, {self.south:.5f}, {self.east:.5f}, {self.north:.5f}) "
            f"[{self.width_m / 1000:.2f} x {self.height_m / 1000:.2f} km]"
        )
