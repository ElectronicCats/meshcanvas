"""Rasterize shape sources into monochrome bitmaps.

Every function returns a 2D numpy bool array with True = active pixel and
row 0 = TOP of the image, PIL's native orientation. Latitude grows northward
while the row index grows downward, so the caller converting rows to latitude
must flip the y axis (see shape_to_latlon); skipping the flip renders every
shape upside down.

Text uses PIL's embedded default font via ImageFont.load_default() so no
system TTF path has to exist; a sized default is requested first and the
fixed-size fallback is used on Pillow versions without the size argument.
"""

from __future__ import annotations

import io
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def _default_font(font_size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=font_size)
    except (TypeError, OSError):
        return ImageFont.load_default()


def text_to_bitmap(
    text: str, width_px: int = 200, font_size: int = 48
) -> np.ndarray:
    """Render text with the default font, cropped tight and scaled to width_px."""
    if not text.strip():
        raise ValueError("text is empty; nothing to rasterize")
    font = _default_font(font_size)

    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), text, font=font)
    w = max(1, right - left)
    h = max(1, bottom - top)

    img = Image.new("L", (w, h), 0)
    ImageDraw.Draw(img).text((-left, -top), text, fill=255, font=font)

    if w != width_px:
        img = img.resize(
            (width_px, max(1, round(h * width_px / w))), Image.Resampling.LANCZOS
        )
    return np.asarray(img) > 127


def image_to_bitmap(
    path_or_bytes: str | Path | bytes, threshold: int = 128, max_dim: int = 256
) -> np.ndarray:
    """Threshold an uploaded PNG/JPEG. Dark pixels (< threshold) are active."""
    if isinstance(path_or_bytes, bytes):
        img = Image.open(io.BytesIO(path_or_bytes))
    else:
        img = Image.open(path_or_bytes)
    img = img.convert("L")
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
    return np.asarray(img) < threshold


def polygon_to_bitmap(
    vertices: list[tuple[float, float]], resolution: int = 128
) -> np.ndarray:
    """Filled polygon from (x, y) vertices in normalized 0..1 image space.

    x grows rightward, y grows downward (image convention), so (0, 0) is the
    top-left corner of the bitmap.
    """
    if len(vertices) < 3:
        raise ValueError(f"polygon needs at least 3 vertices, got {len(vertices)}")
    img = Image.new("L", (resolution, resolution), 0)
    scaled = [(x * (resolution - 1), y * (resolution - 1)) for x, y in vertices]
    ImageDraw.Draw(img).polygon(scaled, fill=255)
    return np.asarray(img) > 0


def latlon_path_to_bitmap(
    vertices: list[tuple[float, float]],
    center_lat: float,
    resolution: int = 256,
    fill: bool = True,
    stroke_px: int = 3,
) -> np.ndarray:
    """Rasterize a path drawn on a map, given as (latitude, longitude) pairs.

    This is the entry point for anything the user draws, because a map client
    reports map coordinates and every other function here works in image space.
    Three conversions happen, and dropping any one of them silently ruins the
    shape:

    - Longitude is the x axis and latitude the y axis. Passing (lat, lon)
      straight through transposes the drawing.
    - Latitude grows north but the row index grows down, so y is flipped.
      Without this the shape renders upside down.
    - A degree of longitude is shorter than a degree of latitude by
      cos(latitude), so the bitmap's aspect follows the extent in metres rather
      than in degrees. Without it a square drawn at latitude 36 rasterizes 24
      percent too wide.

    fill=True closes and fills the path, which is what a polygon means.
    fill=False strokes it, which is what a freehand trace means: filling a
    squiggle collapses it into a blob and throws away the line.
    """
    return latlon_paths_to_bitmap(
        [vertices], center_lat, resolution, fill, stroke_px
    )


def latlon_paths_to_bitmap(
    paths: list[list[tuple[float, float]]],
    center_lat: float,
    resolution: int = 256,
    fill: bool = True,
    stroke_px: int = 3,
) -> np.ndarray:
    """Rasterize several map paths into one bitmap.

    A freehand drawing is usually more than one stroke: lift the pointer and the
    next stroke is a separate path. All paths share a single bounding box, which
    is what keeps their positions relative to each other. Normalizing each path
    on its own would stack every stroke on top of the others.
    """
    paths = [p for p in paths if p]
    if not paths:
        raise ValueError("no path was supplied")
    for path in paths:
        if len(path) < 2:
            raise ValueError(f"a path needs at least 2 points, got {len(path)}")
        if fill and len(path) < 3:
            raise ValueError(
                f"a filled polygon needs at least 3 vertices, got {len(path)}"
            )

    points = [point for path in paths for point in path]
    lats = [lat for lat, _ in points]
    lons = [lon for _, lon in points]
    span_lat = max(lats) - min(lats)
    span_lon = max(lons) - min(lons)

    if span_lat <= 0 and span_lon <= 0:
        raise ValueError("every point is identical, so the path has no extent")

    # Extent in metres decides the bitmap's aspect ratio.
    metres_north = span_lat * 111_320.0
    metres_east = span_lon * 111_320.0 * math.cos(math.radians(center_lat))

    if metres_east >= metres_north:
        width = resolution
        height = max(2, round(resolution * (metres_north / metres_east))) if metres_east else 2
    else:
        height = resolution
        width = max(2, round(resolution * (metres_east / metres_north))) if metres_north else 2

    def to_pixel(point: tuple[float, float]) -> tuple[float, float]:
        lat, lon = point
        x = ((lon - min(lons)) / span_lon * (width - 1)) if span_lon else (width - 1) / 2
        # max(lats) maps to row 0 so north is at the top.
        y = ((max(lats) - lat) / span_lat * (height - 1)) if span_lat else (height - 1) / 2
        return (x, y)

    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)

    for path in paths:
        scaled = [to_pixel(point) for point in path]
        if fill:
            draw.polygon(scaled, fill=255)
            # A thin or self-intersecting outline can fill to almost nothing;
            # the stroke keeps the drawing recoverable instead of a blank.
            draw.line(scaled + [scaled[0]], fill=255, width=stroke_px, joint="curve")
        else:
            draw.line(scaled, fill=255, width=stroke_px, joint="curve")

    return np.asarray(image) > 0


def circle_bitmap(diameter_px: int = 64) -> np.ndarray:
    """Filled circle spanning the whole bitmap."""
    img = Image.new("L", (diameter_px, diameter_px), 0)
    ImageDraw.Draw(img).ellipse((0, 0, diameter_px - 1, diameter_px - 1), fill=255)
    return np.asarray(img) > 0


def grid_bitmap(
    rows: int, cols: int, dot_px: int = 3, pitch_px: int = 12
) -> np.ndarray:
    """rows x cols lattice of square dots, dot_px wide, pitch_px apart."""
    if rows < 1 or cols < 1:
        raise ValueError(f"grid needs at least 1x1 dots, got {rows}x{cols}")
    height = (rows - 1) * pitch_px + dot_px
    width = (cols - 1) * pitch_px + dot_px
    bitmap = np.zeros((height, width), dtype=bool)
    for r in range(rows):
        for c in range(cols):
            y = r * pitch_px
            x = c * pitch_px
            bitmap[y : y + dot_px, x : x + dot_px] = True
    return bitmap


def star_bitmap(
    points: int = 5, size_px: int = 128, inner_ratio: float = 0.45
) -> np.ndarray:
    """Filled star with one spike pointing to the top of the bitmap."""
    if points < 2:
        raise ValueError(f"star needs at least 2 points, got {points}")
    outer = 0.5
    inner = 0.5 * inner_ratio
    vertices: list[tuple[float, float]] = []
    for i in range(points * 2):
        radius = outer if i % 2 == 0 else inner
        # -pi/2 puts the first outer vertex at the top; y grows downward.
        angle = -math.pi / 2.0 + i * math.pi / points
        vertices.append(
            (0.5 + radius * math.cos(angle), 0.5 + radius * math.sin(angle))
        )
    return polygon_to_bitmap(vertices, resolution=size_px)
