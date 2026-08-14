"""Turn shape bitmaps into lat/lon point clouds.

shape_to_latlon is the package entry point: sample the bitmap down to n
points, place them in a metres box centred on the target coordinate, then
project metres to degrees with the cos(lat) longitude correction. Row 0 of
a bitmap is the TOP of the image, so the row axis is flipped here; latitude
grows northward while row indices grow downward.
"""

from __future__ import annotations

import numpy as np

from meshcanvas.geometry.projection import (
    haversine_metres,
    metres_to_degrees,
    offset_latlon,
)
from meshcanvas.geometry.raster import (
    circle_bitmap,
    grid_bitmap,
    image_to_bitmap,
    latlon_path_to_bitmap,
    latlon_paths_to_bitmap,
    polygon_to_bitmap,
    star_bitmap,
    text_to_bitmap,
)
from meshcanvas.geometry.sample import sample_points

__all__ = [
    "shape_to_latlon",
    "sample_points",
    "metres_to_degrees",
    "offset_latlon",
    "haversine_metres",
    "text_to_bitmap",
    "image_to_bitmap",
    "latlon_path_to_bitmap",
    "latlon_paths_to_bitmap",
    "polygon_to_bitmap",
    "circle_bitmap",
    "grid_bitmap",
    "star_bitmap",
]


def shape_to_latlon(
    bitmap: np.ndarray,
    n: int,
    center_lat: float,
    center_lon: float,
    width_metres: float,
    seed: int = 0,
) -> list[tuple[float, float]]:
    """n (lat, lon) points drawing the bitmap centred on (center_lat, center_lon).

    The bitmap fills a box width_metres wide; its height in metres follows the
    bitmap's aspect ratio so shapes are not stretched. Points are placed at
    pixel centres, so the outermost points sit half a pixel inside the box
    edges rather than on them.
    """
    if width_metres <= 0:
        raise ValueError(f"width_metres must be positive, got {width_metres}")
    rows, cols = bitmap.shape
    height_metres = width_metres * rows / cols

    result: list[tuple[float, float]] = []
    for row, col in sample_points(bitmap, n, seed):
        metres_east = ((col + 0.5) / cols - 0.5) * width_metres
        # Row 0 is the top of the image and must land north of the last row.
        metres_north = (0.5 - (row + 0.5) / rows) * height_metres
        result.append(offset_latlon(center_lat, center_lon, metres_north, metres_east))
    return result
