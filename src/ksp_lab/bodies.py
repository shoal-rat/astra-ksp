"""Stock-KSP body constants for OFFLINE Δv budgeting and ship design.

The live execution layer (execute.py) measures GM / radius / surface gravity / atmospheric density
straight from kRPC, so it never needs this table. But the *planning* path — MissionPlanner.interpret()
and HistoryOptimizer.first_design() — runs with no KSP connected (CLI `plan`, the runner before a
craft is loaded, the test suite). To keep those calculated rather than hardcoded with flat magic Δv
numbers, we feed astro.py the published stock-KSP catalogue parameters of each body. These are the
SAME quantities kRPC would return live (`gravitational_parameter`, `equatorial_radius`,
`surface_gravity`, `atmosphere_depth`, `density_at(0)`); they are catalogue facts, not tuned guesses,
and `astro.py` turns them into every Δv via vis-viva / Hohmann / Oberth / the rocket equation.

Source: KSP stock planet data (KSP wiki "Kerbol System"), the same values kRPC exposes in-game.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Body:
    """One celestial body, with exactly the fields astro.py / design.py consume.

    All values are MEASURABLE: kRPC returns each one live. Offline we read them from this catalogue so
    the planners stay calculated instead of carrying flat magic Δv numbers.
    """

    name: str
    mu: float                 # gravitational parameter GM (m^3/s^2) -- kRPC body.gravitational_parameter
    radius_m: float           # equatorial radius (m)               -- kRPC body.equatorial_radius
    surface_g: float          # surface gravity (m/s^2)             -- kRPC body.surface_gravity
    atmosphere_top_m: float   # atmosphere depth (m), 0 if airless  -- kRPC body.atmosphere_depth
    surface_rho: float        # sea-level density (kg/m^3), 0 airless-- kRPC body.density_at(0)
    rotational_speed_mps: float = 0.0  # equatorial surface speed; a free prograde launch credit
    parent: str = "Sun"
    orbit_radius_m: float = 0.0        # circular orbit radius about its parent (for interplanetary)

    def low_orbit_radius_m(self) -> float:
        """Radius (from body centre) of a safe low circular orbit: just above the atmosphere, or a
        small clearance for an airless body. Mirrors plan.parking_orbit_altitude but in absolute r."""
        if self.atmosphere_top_m > 0:
            return self.radius_m + self.atmosphere_top_m * 1.15
        return self.radius_m + max(10_000.0, self.radius_m * 0.05)


# Stock-KSP catalogue (the values kRPC exposes live). Kerbin's GM/radius give the canonical
# v_circ ~ 2246 m/s at low orbit and surface escape ~ 3431 m/s, from which astro.py derives ascent,
# transfer, capture and landing Δv. Nothing below is a Δv: every Δv is computed from these.
SUN = Body(
    name="Sun", mu=1.1723328e18, radius_m=261_600_000.0, surface_g=0.0,
    atmosphere_top_m=0.0, surface_rho=0.0, parent="", orbit_radius_m=0.0,
)

KERBIN = Body(
    name="Kerbin", mu=3.5316000e12, radius_m=600_000.0, surface_g=9.81,
    atmosphere_top_m=70_000.0, surface_rho=1.225, rotational_speed_mps=174.94,
    parent="Sun", orbit_radius_m=13_599_840_256.0,
)

MUN = Body(
    name="Mun", mu=6.5138398e10, radius_m=200_000.0, surface_g=1.6285,
    atmosphere_top_m=0.0, surface_rho=0.0, rotational_speed_mps=9.04,
    parent="Kerbin", orbit_radius_m=12_000_000.0,
)

DUNA = Body(
    name="Duna", mu=3.0136321e11, radius_m=320_000.0, surface_g=2.94,
    atmosphere_top_m=50_000.0, surface_rho=0.07, rotational_speed_mps=29.36,
    parent="Sun", orbit_radius_m=20_726_155_264.0,
)

MINMUS = Body(
    name="Minmus", mu=1.7658000e9, radius_m=60_000.0, surface_g=0.491,
    atmosphere_top_m=0.0, surface_rho=0.0, rotational_speed_mps=9.32,
    parent="Kerbin", orbit_radius_m=47_000_000.0,
)


_REGISTRY: dict[str, Body] = {b.name.lower(): b for b in (SUN, KERBIN, MUN, DUNA, MINMUS)}


def body(name: str) -> Body:
    """Look up a stock body by name (case-insensitive). Falls back to Kerbin for unknown names so the
    offline planner always has a launch body."""
    return _REGISTRY.get((name or "").strip().lower(), KERBIN)


def parent_of(b: Body) -> Body:
    """The body `b` orbits (its primary). Sun for planets, the planet for moons."""
    return _REGISTRY.get((b.parent or "").strip().lower(), SUN)
