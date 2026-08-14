"""Reduce a bitmap's active pixels to exactly n representative points.

Lloyd's k-means with k-means++ seeding over the active pixel coordinates,
driven by np.random.default_rng(seed) so the same bitmap and seed always
give the same points. Taking the first n active pixels instead would bias
every shape toward its top-left corner (argwhere is row-major), which is
why the spread-out centroids are used even though they cost iterations.

A centroid that loses every member mid-iteration is re-seeded onto the
active pixel farthest from all current centroids; averaging an empty
cluster is the classic way naive k-means emits NaN coordinates.
"""

from __future__ import annotations

import numpy as np

MAX_ITERATIONS = 100


def _kmeans_pp_init(
    pts: np.ndarray, k: int, rng: np.random.Generator
) -> np.ndarray:
    centroids = np.empty((k, 2), dtype=float)
    centroids[0] = pts[rng.integers(len(pts))]
    d2 = ((pts - centroids[0]) ** 2).sum(axis=1)
    for i in range(1, k):
        total = d2.sum()
        if total > 0:
            idx = rng.choice(len(pts), p=d2 / total)
        else:
            idx = rng.integers(len(pts))
        centroids[i] = pts[idx]
        d2 = np.minimum(d2, ((pts - centroids[i]) ** 2).sum(axis=1))
    return centroids


def sample_points(
    bitmap: np.ndarray, n: int, seed: int = 0
) -> list[tuple[float, float]]:
    """Exactly n (row, col) float points spread over the active pixels."""
    if n <= 0:
        raise ValueError(f"n must be positive, got {n}")
    coords = np.argwhere(bitmap).astype(float)
    active = len(coords)
    if active == 0:
        raise ValueError("bitmap has no active pixels")
    if active < n:
        raise ValueError(
            f"asked for {n} points but the bitmap has only {active} active pixels"
        )
    if active == n:
        return [(float(r), float(c)) for r, c in coords]

    rng = np.random.default_rng(seed)
    centroids = _kmeans_pp_init(coords, n, rng)
    for _ in range(MAX_ITERATIONS):
        d2 = ((coords[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)
        labels = d2.argmin(axis=1)
        new_centroids = np.empty_like(centroids)
        for k in range(n):
            members = coords[labels == k]
            if len(members) == 0:
                new_centroids[k] = coords[d2.min(axis=1).argmax()]
            else:
                new_centroids[k] = members.mean(axis=0)
        if np.allclose(new_centroids, centroids):
            break
        centroids = new_centroids
    return [(float(r), float(c)) for r, c in centroids]
