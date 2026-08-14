"""Geometry package tests.

The load-bearing test is the metres-square-stays-square family: it measures
the rendered output with haversine, which is independent of the forward
projection, so a missing or misapplied cos(lat) correction fails it by the
full cos(lat) factor (30 percent at latitude 45).
"""

import io
import math

import numpy as np
import pytest
from PIL import Image

from meshcanvas.geometry import (
    circle_bitmap,
    grid_bitmap,
    haversine_metres,
    image_to_bitmap,
    latlon_path_to_bitmap,
    offset_latlon,
    polygon_to_bitmap,
    sample_points,
    shape_to_latlon,
    star_bitmap,
    text_to_bitmap,
)


def rendered_extent_metres(points):
    """(width, height) of a point cloud, measured with haversine."""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    mid_lat = (min(lats) + max(lats)) / 2.0
    mid_lon = (min(lons) + max(lons)) / 2.0
    width = haversine_metres(mid_lat, min(lons), mid_lat, max(lons))
    height = haversine_metres(min(lats), mid_lon, max(lats), mid_lon)
    return width, height


@pytest.mark.parametrize("center_lat", [0.0, 45.0, 60.0])
def test_square_metres_stays_square(center_lat):
    side = 20
    bitmap = np.ones((side, side), dtype=bool)
    width_metres = 1000.0
    # n == active pixel count returns every pixel, so the extent is exact:
    # pixel centres span (side - 1) / side of the box in both axes.
    points = shape_to_latlon(
        bitmap, side * side, center_lat, -99.0, width_metres, seed=1
    )
    width, height = rendered_extent_metres(points)
    expected = width_metres * (side - 1) / side
    assert width == pytest.approx(expected, rel=0.02)
    assert height == pytest.approx(expected, rel=0.02)
    assert width == pytest.approx(height, rel=0.02)


def test_square_aspect_survives_kmeans_at_lat_45():
    bitmap = np.ones((40, 40), dtype=bool)
    points = shape_to_latlon(bitmap, 100, 45.0, 7.0, 2000.0, seed=3)
    width, height = rendered_extent_metres(points)
    # Centroids pull in from the edges, but symmetrically: without the
    # cos(lat) correction the ratio would be near 0.707, not 1.
    assert width == pytest.approx(height, rel=0.1)


@pytest.mark.parametrize("n", [1, 4, 17, 60])
def test_output_count_is_exactly_n(n):
    bitmap = circle_bitmap(32)
    points = shape_to_latlon(bitmap, n, 19.4, -99.1, 500.0, seed=42)
    assert len(points) == n


def test_determinism_under_fixed_seed():
    bitmap = star_bitmap(points=5, size_px=64)
    a = shape_to_latlon(bitmap, 30, 19.4, -99.1, 500.0, seed=7)
    b = shape_to_latlon(bitmap, 30, 19.4, -99.1, 500.0, seed=7)
    assert a == b


def test_sampled_centroid_matches_shape_centroid():
    # Circle centred low-right in the canvas: taking the first n active
    # pixels (row-major) would put the sample mean far above the centre.
    bitmap = np.zeros((100, 100), dtype=bool)
    disc = circle_bitmap(40)
    bitmap[50:90, 40:80] = disc
    true_centroid = np.argwhere(bitmap).mean(axis=0)
    points = sample_points(bitmap, 25, seed=11)
    sampled_centroid = np.mean(points, axis=0)
    assert sampled_centroid[0] == pytest.approx(true_centroid[0], abs=2.0)
    assert sampled_centroid[1] == pytest.approx(true_centroid[1], abs=2.0)


def test_north_is_up():
    bitmap = np.zeros((10, 10), dtype=bool)
    bitmap[0, 4] = True
    bitmap[9, 4] = True
    top_point, bottom_point = shape_to_latlon(bitmap, 2, 45.0, 7.0, 100.0, seed=0)
    assert top_point[0] > bottom_point[0]


def test_n_larger_than_active_pixels_raises():
    bitmap = np.zeros((10, 10), dtype=bool)
    bitmap[2:4, 2:4] = True
    with pytest.raises(ValueError, match="4 active"):
        sample_points(bitmap, 5, seed=0)


def test_n_zero_raises():
    with pytest.raises(ValueError, match="positive"):
        sample_points(np.ones((5, 5), dtype=bool), 0, seed=0)


def test_empty_bitmap_raises():
    with pytest.raises(ValueError, match="no active"):
        sample_points(np.zeros((5, 5), dtype=bool), 3, seed=0)


@pytest.mark.parametrize("lat", [85.5, -86.0, 90.0])
def test_polar_latitude_raises(lat):
    with pytest.raises(ValueError, match="85"):
        offset_latlon(lat, 0.0, 10.0, 10.0)
    with pytest.raises(ValueError, match="85"):
        shape_to_latlon(np.ones((4, 4), dtype=bool), 4, lat, 0.0, 100.0, seed=0)


def test_text_bitmap_is_nonempty():
    bitmap = text_to_bitmap("SOS", width_px=120, font_size=32)
    assert bitmap.dtype == np.bool_
    assert bitmap.ndim == 2
    assert bitmap.any()


def test_empty_text_raises():
    with pytest.raises(ValueError, match="empty"):
        text_to_bitmap("   ")


def test_image_bitmap_roundtrip():
    img = Image.new("L", (16, 16), 255)
    for y in range(4, 12):
        for x in range(4, 12):
            img.putpixel((x, y), 0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    bitmap = image_to_bitmap(buf.getvalue())
    assert bitmap.shape == (16, 16)
    assert bitmap[8, 8]
    assert not bitmap[0, 0]
    assert bitmap.sum() == 64


def test_image_bitmap_resizes_preserving_aspect():
    img = Image.new("L", (400, 200), 0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    bitmap = image_to_bitmap(buf.getvalue(), max_dim=100)
    assert bitmap.shape == (50, 100)


def test_polygon_grid_star_smoke():
    triangle = polygon_to_bitmap([(0.5, 0.1), (0.9, 0.9), (0.1, 0.9)], resolution=64)
    assert triangle.any()
    grid = grid_bitmap(3, 4, dot_px=2, pitch_px=10)
    assert grid.sum() == 3 * 4 * 2 * 2
    star = star_bitmap(points=5, size_px=64)
    assert star.any()
    # One spike points up: the top rows are narrower than the middle.
    assert star[1].sum() < star[32].sum()


class TestLatLonPathRasterization:
    """Paths drawn on the map arrive as (latitude, longitude), not as
    normalized image coordinates.

    Passing map coordinates straight into polygon_to_bitmap scales them far off
    the bitmap and yields zero active pixels, which surfaced as
    "shape has 0 active pixels" for every polygon and freehand draw.
    """

    LAT = 36.13472
    # 0.001 degrees square, drawn near Las Vegas.
    SQUARE = [
        (36.1347, -115.1617),
        (36.1357, -115.1617),
        (36.1357, -115.1607),
        (36.1347, -115.1607),
    ]

    def test_map_coordinates_produce_a_non_empty_bitmap(self):
        bitmap = latlon_path_to_bitmap(self.SQUARE, self.LAT)
        assert bitmap.any()

    def test_the_old_normalized_rasterizer_would_have_produced_nothing(self):
        # Guards the exact regression: this is what the API used to call.
        assert not polygon_to_bitmap(self.SQUARE).any()

    def test_aspect_follows_extent_in_metres_not_degrees(self):
        # A degree of longitude is shorter than a degree of latitude by
        # cos(lat), so this equal-degree square is NOT square on the ground.
        bitmap = latlon_path_to_bitmap(self.SQUARE, self.LAT)
        height_m = haversine_metres(36.1347, -115.1617, 36.1357, -115.1617)
        width_m = haversine_metres(36.1347, -115.1617, 36.1347, -115.1607)
        pixel_ratio = bitmap.shape[1] / bitmap.shape[0]
        assert pixel_ratio == pytest.approx(width_m / height_m, rel=0.02)

    def test_a_ground_square_rasterizes_square(self):
        # Widen the longitude span by 1/cos(lat) so the shape is square in
        # metres. The bitmap must then be square in pixels.
        import math

        span = 0.001 / math.cos(math.radians(self.LAT))
        square = [
            (36.1347, -115.1617),
            (36.1357, -115.1617),
            (36.1357, -115.1617 + span),
            (36.1347, -115.1617 + span),
        ]
        bitmap = latlon_path_to_bitmap(square, self.LAT)
        assert bitmap.shape[0] == pytest.approx(bitmap.shape[1], rel=0.02)

    def test_north_is_row_zero(self):
        # Apex to the north must land in the top half of the bitmap. Without the
        # y flip the shape renders upside down.
        triangle = [
            (36.1347, -115.1617),
            (36.1357, -115.1612),
            (36.1347, -115.1607),
        ]
        bitmap = latlon_path_to_bitmap(triangle, self.LAT)
        rows = np.nonzero(bitmap.any(axis=1))[0]
        top_width = bitmap[rows[0]].sum()
        bottom_width = bitmap[rows[-1]].sum()
        assert top_width < bottom_width

    def test_longitude_is_the_x_axis(self):
        # A wide, short strip must produce a wide, short bitmap. Swapping the
        # axes transposes it.
        strip = [
            (36.1347, -115.1617),
            (36.1348, -115.1517),
            (36.1347, -115.1517),
        ]
        bitmap = latlon_path_to_bitmap(strip, self.LAT)
        assert bitmap.shape[1] > bitmap.shape[0]

    def test_fill_covers_far_more_than_stroke(self):
        filled = latlon_path_to_bitmap(self.SQUARE, self.LAT, fill=True)
        stroked = latlon_path_to_bitmap(self.SQUARE, self.LAT, fill=False)
        assert filled.sum() > stroked.sum() * 5

    def test_an_open_squiggle_survives_as_a_line(self):
        # Filling a squiggle collapses it to a blob. Stroking keeps the drawing.
        squiggle = [(36.1347 + i * 0.0002, -115.1617 + (i % 2) * 0.0003)
                    for i in range(8)]
        stroked = latlon_path_to_bitmap(squiggle, self.LAT, fill=False)
        assert stroked.any()
        assert stroked.sum() < latlon_path_to_bitmap(
            squiggle, self.LAT, fill=True
        ).sum()

    def test_two_points_are_enough_to_stroke(self):
        assert latlon_path_to_bitmap(
            [(36.1347, -115.1617), (36.1357, -115.1607)], self.LAT, fill=False
        ).any()

    def test_a_fill_needs_three_points(self):
        with pytest.raises(ValueError, match="3 vertices"):
            latlon_path_to_bitmap(
                [(36.1347, -115.1617), (36.1357, -115.1607)], self.LAT, fill=True
            )

    def test_a_single_point_is_rejected(self):
        with pytest.raises(ValueError, match="at least 2"):
            latlon_path_to_bitmap([(36.1347, -115.1617)], self.LAT)

    def test_identical_points_are_rejected(self):
        with pytest.raises(ValueError, match="no extent"):
            latlon_path_to_bitmap([(36.1347, -115.1617)] * 4, self.LAT)

    def test_a_perfectly_straight_line_still_rasterizes(self):
        # Zero span on one axis would divide by zero if unguarded.
        horizontal = [(36.1347, -115.1617), (36.1347, -115.1607),
                      (36.1347, -115.1600)]
        assert latlon_path_to_bitmap(horizontal, self.LAT, fill=False).any()
        vertical = [(36.1347, -115.1617), (36.1357, -115.1617),
                    (36.1360, -115.1617)]
        assert latlon_path_to_bitmap(vertical, self.LAT, fill=False).any()
