"""Tiny 3-vector helpers for control laws (kRPC returns tuples)."""

from __future__ import annotations

import math

Vec = tuple[float, float, float]


def add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def scale(a: Vec, k: float) -> Vec:
    return (a[0] * k, a[1] * k, a[2] * k)


def dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a: Vec) -> float:
    return math.sqrt(dot(a, a))


def unit(a: Vec) -> Vec:
    n = norm(a)
    if n < 1e-12:
        raise ValueError("zero-length vector")
    return (a[0] / n, a[1] / n, a[2] / n)


def angle_deg(a: Vec, b: Vec) -> float:
    c = dot(unit(a), unit(b))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def tilt_toward(direction: Vec, up: Vec, min_elev_deg: float | None, max_elev_deg: float | None) -> Vec:
    """Clamp a direction's elevation above the plane normal to `up` into [min, max] degrees."""
    d, u = unit(direction), unit(up)
    elev = math.degrees(math.asin(max(-1.0, min(1.0, dot(d, u)))))
    lo = -90.0 if min_elev_deg is None else min_elev_deg
    hi = 90.0 if max_elev_deg is None else max_elev_deg
    if lo <= elev <= hi:
        return d
    target = max(lo, min(hi, elev))
    horiz = sub(d, scale(u, dot(d, u)))
    if norm(horiz) < 1e-9:  # straight up/down: no azimuth information; keep vertical
        return u if target > 0 else scale(u, -1.0)
    h = unit(horiz)
    r = math.radians(target)
    return add(scale(h, math.cos(r)), scale(u, math.sin(r)))
