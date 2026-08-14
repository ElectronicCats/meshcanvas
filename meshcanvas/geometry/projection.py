"""Local equirectangular projection between metres and degrees.

One degree of latitude is treated as 111320 m everywhere; one degree of
longitude shrinks by cos(latitude). Skipping that divisor squashes a shape
horizontally by the same factor, 30 percent at latitude 45, so it is applied
on every east-west conversion here. haversine_metres exists so tests can
measure the rendered output independently of the forward projection.

The approximation is a local tangent plane: good to well under a percent for
the few-kilometre canvases this project draws, and it degrades as cos(lat)
approaches 0, which is why conversions refuse to run above |lat| = 85.
"""

from __future__ import annotations

import math

METRES_PER_DEGREE_LAT = 111320.0
MAX_ABS_LATITUDE = 85.0
EARTH_RADIUS_METRES = 6371000.0


def _check_latitude(center_lat: float) -> None:
    if abs(center_lat) > MAX_ABS_LATITUDE:
        raise ValueError(
            f"center latitude {center_lat} is beyond +/-{MAX_ABS_LATITUDE}; "
            "cos(lat) is too close to 0 there and longitude offsets blow up"
        )


def metres_to_degrees(
    metres_north: float, metres_east: float, center_lat: float
) -> tuple[float, float]:
    """(dlat, dlon) in degrees for a local offset in metres."""
    _check_latitude(center_lat)
    dlat = metres_north / METRES_PER_DEGREE_LAT
    dlon = metres_east / (METRES_PER_DEGREE_LAT * math.cos(math.radians(center_lat)))
    return dlat, dlon


def offset_latlon(
    center_lat: float,
    center_lon: float,
    metres_north: float,
    metres_east: float,
) -> tuple[float, float]:
    """(lat, lon) of the point metres_north / metres_east from the center."""
    dlat, dlon = metres_to_degrees(metres_north, metres_east, center_lat)
    return center_lat + dlat, center_lon + dlon


def haversine_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance on a sphere of radius EARTH_RADIUS_METRES."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_METRES * math.asin(math.sqrt(a))
