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
    soi_m: float = 0.0                 # sphere-of-influence radius (m)  -- kRPC body.sphere_of_influence
    sidereal_period_s: float = 0.0     # rotational period (s)           -- kRPC body.rotational_period

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
    soi_m=84_159_286.5, sidereal_period_s=21_549.425,
)

MUN = Body(
    name="Mun", mu=6.5138398e10, radius_m=200_000.0, surface_g=1.6285,
    atmosphere_top_m=0.0, surface_rho=0.0, rotational_speed_mps=9.04,
    parent="Kerbin", orbit_radius_m=12_000_000.0,
    soi_m=2_429_559.1, sidereal_period_s=138_984.377,
)

DUNA = Body(
    name="Duna", mu=3.0136321e11, radius_m=320_000.0, surface_g=2.94,
    # surface_rho is DENSITY (kg/m^3), measured live from kRPC density_at(0)=0.13334 — NOT the 0.0677
    # atm surface pressure. This is the number the parachute sizing depends on; getting it wrong is
    # what made a single-chute lander look survivable on Duna when it was not.
    atmosphere_top_m=50_000.0, surface_rho=0.13334, rotational_speed_mps=29.36,
    parent="Sun", orbit_radius_m=20_726_155_264.0,
    soi_m=47_921_949.4, sidereal_period_s=65_517.859,
)

MINMUS = Body(
    name="Minmus", mu=1.7658000e9, radius_m=60_000.0, surface_g=0.491,
    atmosphere_top_m=0.0, surface_rho=0.0, rotational_speed_mps=9.32,
    parent="Kerbin", orbit_radius_m=47_000_000.0,
    soi_m=2_247_428.4, sidereal_period_s=40_400.0,
)

# Ike = Duna's moon (the "Mars moon" the comsat constellation must keep linked). Airless; tidally
# locked to Duna, so one hemisphere always faces away from any Duna-orbit relay -> it needs its OWN
# relay (the dedicated Ike-orbit sat). orbit_radius is its SMA about Duna (~3.2 Mm); the Duna ring is
# placed BELOW this (2.96 Mm SMA) so the ring sats are never captured by Ike.
IKE = Body(
    name="Ike", mu=1.8568369e10, radius_m=130_000.0, surface_g=1.10,
    atmosphere_top_m=0.0, surface_rho=0.0, rotational_speed_mps=18.6,
    parent="Duna", orbit_radius_m=3_200_000.0,
    soi_m=1_049_598.9, sidereal_period_s=65_517.862,
)


# Eve = the "Venus" target. Thick atmosphere (90 km, ~5 atm at sea level: surface_rho 6.17), deep
# gravity well (g 16.68) -> the hardest ascent in stock. Values read live from kRPC.
EVE = Body(
    name="Eve", mu=8.1717302e12, radius_m=700_000.0, surface_g=16.683,
    atmosphere_top_m=90_000.0, surface_rho=6.1722, rotational_speed_mps=54.6,
    parent="Sun", orbit_radius_m=9_832_684_544.0,
    soi_m=85_109_365.0, sidereal_period_s=80_500.0,
)

# Gilly = Eve's tiny moon (airless, eccentric, inclined). Needs its own relay for full Eve coverage.
GILLY = Body(
    name="Gilly", mu=8.2894498e6, radius_m=13_000.0, surface_g=0.049,
    atmosphere_top_m=0.0, surface_rho=0.0, rotational_speed_mps=2.9,
    parent="Eve", orbit_radius_m=31_500_000.0,
    soi_m=126_123.0, sidereal_period_s=28_255.0,
)


_REGISTRY: dict[str, Body] = {b.name.lower(): b for b in (SUN, KERBIN, MUN, DUNA, MINMUS, IKE, EVE, GILLY)}


def body(name: str) -> Body:
    """Look up a stock body by name (case-insensitive). Falls back to Kerbin for unknown names so the
    offline planner always has a launch body."""
    return _REGISTRY.get((name or "").strip().lower(), KERBIN)


def parent_of(b: Body) -> Body:
    """The body `b` orbits (its primary). Sun for planets, the planet for moons."""
    return _REGISTRY.get((b.parent or "").strip().lower(), SUN)


# --------------------------------------------------------------------------- #
# LIVE body parameters - the general-purpose, body-agnostic accessor.          #
# Read mu/radius/SOI/atmosphere/sidereal straight from a kRPC body so the agent #
# works for ANY body with NO hard-coded table or magic numbers.                #
# --------------------------------------------------------------------------- #
def params_from_krpc(krpc_body) -> Body:
    """Build a Body from a LIVE kRPC body handle. Same shape as the offline catalogue, so every
    downstream helper (astro.py, the functions below) is identical for any body the game exposes."""
    import math
    has = krpc_body.has_atmosphere
    rot = float(krpc_body.rotational_period or 0.0)
    R = float(krpc_body.equatorial_radius)
    orb = krpc_body.orbit
    return Body(
        name=krpc_body.name,
        mu=float(krpc_body.gravitational_parameter),
        radius_m=R,
        surface_g=float(krpc_body.surface_gravity),
        atmosphere_top_m=(float(krpc_body.atmosphere_depth) if has else 0.0),
        surface_rho=(float(krpc_body.density_at(0.0)) if has else 0.0),
        rotational_speed_mps=(2.0 * math.pi * R / rot if rot else 0.0),
        parent=(orb.body.name if orb else "Sun"),
        orbit_radius_m=(float(orb.semi_major_axis) if orb else 0.0),
        soi_m=float(krpc_body.sphere_of_influence),
        sidereal_period_s=rot,
    )


def synchronous_altitude_m(b: Body) -> float:
    """Stationary (synchronous) orbit ALTITUDE for ANY body: a = (mu * T_sidereal^2 / 4pi^2)^(1/3) - R.
    Returns -1.0 if the synchronous radius is OUTSIDE the SOI (e.g. the Mun) so the caller can fall back
    to a sub-synchronous ring. Works for Kerbin (keostationary), Duna, Eve, etc."""
    import math
    if b.sidereal_period_s <= 0 or b.mu <= 0:
        return -1.0
    a = (b.mu * b.sidereal_period_s ** 2 / (4.0 * math.pi ** 2)) ** (1.0 / 3.0)
    if b.soi_m and a >= b.soi_m:
        return -1.0
    return a - b.radius_m


def safe_periapsis_floor_m(b: Body, mode: str = "capture") -> float:
    """A safe minimum periapsis ALTITUDE for any body: clear of the atmosphere (or a clearance if
    airless). Used so capture/periapsis-lower burns never drop into the ground/atmosphere."""
    if b.atmosphere_top_m <= 0.0:
        return max(10_000.0, b.radius_m * 0.05)
    return b.atmosphere_top_m + (5_000.0 if mode == "capture" else -5_000.0)


def capture_apoapsis_ceiling_m(b: Body, strategy: str = "tight") -> float:
    """Bound-orbit apoapsis ceiling (above the body's surface) as an SOI fraction, for a loose vs tight
    capture. A relay only needs to be BOUND, so 'loose' (0.35*SOI) frees correction/capture Δv."""
    return b.soi_m * (0.35 if strategy == "loose" else 0.06)
