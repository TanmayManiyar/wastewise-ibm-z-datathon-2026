"""Small geometry helpers (pure Python / numpy, no GIS libraries)."""
from __future__ import annotations

import math

import numpy as np

EARTH_M = 6_371_008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_M * math.asin(math.sqrt(a))


def haversine_matrix(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    lat, lon = np.radians(lat)[:, None], np.radians(lon)[:, None]
    dp, dl = lat - lat.T, lon - lon.T
    a = np.sin(dp / 2) ** 2 + np.cos(lat) * np.cos(lat.T) * np.sin(dl / 2) ** 2
    return 2 * EARTH_M * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def to_xy(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    """Local equirectangular projection in metres (fine at ward scale)."""
    k = math.radians(1) * EARTH_M
    return lon * k * math.cos(math.radians(lat0)), lat * k


def from_xy(x: float, y: float, lat0: float) -> tuple[float, float]:
    k = math.radians(1) * EARTH_M
    return y / k, x / (k * math.cos(math.radians(lat0)))


def project_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float
                       ) -> tuple[float, float, float, float]:
    """Closest point on segment AB to P. Returns (x, y, t in [0,1], distance)."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    x, y = ax + t * dx, ay + t * dy
    return x, y, t, math.hypot(px - x, py - y)


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360
