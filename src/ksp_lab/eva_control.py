"""Precise EVA / personnel control helpers over the KSP bridge.

The whole point of this layer is CALCULATED, error-free movement: we never guess a heading or a
duration. Surface moves are expressed as a great-circle (geodesic) bearing + distance on the body
sphere, computed here with the same haversine the C# bridge uses, and the actual pathing is done by
the stock engine (``KerbalEVA.SetWaypoint`` behind ``/eva-walk-to``). The bridge owns the physics;
this module owns the geometry the LLM planner reasons over.

Conventions:
  * lat/lon in DEGREES; bearing in DEGREES clockwise from north (0=N, 90=E, 180=S, 270=W).
  * distances in METRES; body radius in METRES (use ``Body.radius_m`` from ``ksp_lab.bodies``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Type-only import to avoid a hard runtime dependency cycle; bridge_client is a plain dataclass.
from .bridge_client import BridgeClient


@dataclass(frozen=True, slots=True)
class Geodesic:
    """Result of a great-circle calculation between two surface points."""

    distance_m: float
    bearing_deg: float  # initial bearing at the start point, 0..360 clockwise from north


def geodesic(
    lat1_deg: float,
    lon1_deg: float,
    lat2_deg: float,
    lon2_deg: float,
    radius_m: float,
) -> Geodesic:
    """Great-circle distance + initial bearing between two lat/lon points on a sphere.

    Haversine for distance, the standard initial-bearing formula for heading. This is the EXACT
    mirror of the C# ``Geodesic`` in the bridge, so the number the planner computes here equals the
    number the bridge reports back — no drift, no guessing.
    """
    phi1 = math.radians(lat1_deg)
    phi2 = math.radians(lat2_deg)
    d_phi = math.radians(lat2_deg - lat1_deg)
    d_lam = math.radians(lon2_deg - lon1_deg)
    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    distance_m = radius_m * c

    y = math.sin(d_lam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lam)
    bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return Geodesic(distance_m=distance_m, bearing_deg=bearing)


def destination_point(
    lat_deg: float,
    lon_deg: float,
    bearing_deg: float,
    distance_m: float,
    radius_m: float,
) -> tuple[float, float]:
    """Project a destination lat/lon from a start point along an initial bearing for a surface
    distance (the direct geodesic problem on a sphere). Returns ``(lat_deg, lon_deg)``.

    This is the same projection the bridge does for the ``{bearing,distance}`` form of /eva-walk-to,
    exposed here so a caller can preview exactly where a relative move lands before issuing it.
    """
    angular = distance_m / radius_m  # central angle subtended
    phi1 = math.radians(lat_deg)
    lam1 = math.radians(lon_deg)
    brng = math.radians(bearing_deg)
    phi2 = math.asin(
        math.sin(phi1) * math.cos(angular)
        + math.cos(phi1) * math.sin(angular) * math.cos(brng)
    )
    lam2 = lam1 + math.atan2(
        math.sin(brng) * math.sin(angular) * math.cos(phi1),
        math.cos(angular) - math.sin(phi1) * math.sin(phi2),
    )
    lat2 = math.degrees(phi2)
    lon2 = (math.degrees(lam2) + 540.0) % 360.0 - 180.0  # normalise to -180..180
    return lat2, lon2


def walk_kerbal_to(
    bridge: BridgeClient,
    lat: float,
    lon: float,
    body_radius_m: float | None = None,
    crew: str = "",
) -> dict:
    """Walk the active (or named) EVA kerbal to an absolute surface ``lat``/``lon``.

    The move itself is delegated to ``/eva-walk-to`` (which recomputes the world target from the
    body's own geodesy and drives ``KerbalEVA.SetWaypoint``). When ``body_radius_m`` is given we also
    attach a locally-computed ``plannedGeodesic`` (distance + bearing from the kerbal's CURRENT
    position, read via ``/eva-status``) so the caller has the calculated heading without a live guess.
    """
    planned: Geodesic | None = None
    if body_radius_m is not None:
        try:
            status = bridge.eva_status()
            if status.get("onEva"):
                planned = geodesic(
                    float(status["latitude"]),
                    float(status["longitude"]),
                    lat,
                    lon,
                    body_radius_m,
                )
        except Exception:
            # The plan annotation is best-effort; the move below is authoritative.
            planned = None

    result = bridge.eva_walk_to(lat=lat, lon=lon, crew=crew)
    if planned is not None:
        result = dict(result)
        result["plannedGeodesic"] = {
            "distanceM": planned.distance_m,
            "bearingDeg": planned.bearing_deg,
        }
    return result


def walk_kerbal_by(
    bridge: BridgeClient,
    bearing_deg: float,
    distance_m: float,
    crew: str = "",
) -> dict:
    """Walk the active (or named) EVA kerbal a relative ``distance_m`` along ``bearing_deg`` (deg
    clockwise from north). The bridge projects the destination on the body sphere — exact spherical
    trig, no guessing."""
    return bridge.eva_walk_to(bearing=bearing_deg, distance=distance_m, crew=crew)


def plant_flag_at(
    bridge: BridgeClient,
    lat: float | None = None,
    lon: float | None = None,
    body_radius_m: float | None = None,
    crew: str = "",
) -> dict:
    """Plant the stock flag with the active vessel's crew. If ``lat``/``lon`` are given, first walk the
    kerbal there (the kerbal must already be on EVA — call ``bridge.eva_go`` first), then plant.

    Returns the flag-plant result; when a walk happened, its result is attached under ``walkResult``.
    """
    walk_result: dict | None = None
    if lat is not None and lon is not None:
        walk_result = walk_kerbal_to(bridge, lat, lon, body_radius_m=body_radius_m, crew=crew)
    flag_result = bridge.eva_flag(crew=crew)
    if walk_result is not None:
        flag_result = dict(flag_result)
        flag_result["walkResult"] = walk_result
    return flag_result
