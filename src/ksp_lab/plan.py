"""Calculated mission planners — live orbital state in, an exact maneuver out.

This is the layer the brain (Claude Code) calls to turn "what do I want to do next" into a precise
maneuver-node vector and timing. Every function is closed-form over `astro.py`; nothing is guessed.
Body constants (GM, radius, atmosphere top, heliocentric orbit radius) are MEASURED from kRPC and
passed in by the caller, so the same planner is correct for any body.

A "plan" is a dict with at least a `dv` (m/s) and, for node-based maneuvers, the node components
(`prograde`, `normal`, `radial`) and the `ut` (universal time) to place it at — ready to hand to kRPC
`add_node` and then MechJeb's node executor, or the calculated finite-burn executor in `execute.py`.
"""
from __future__ import annotations

import math

from . import astro


# --------------------------------------------------------------------------------------------------
# Ascent / parking-orbit targets — what altitude and inclination to ask MechJeb's ascent AP for.
# --------------------------------------------------------------------------------------------------

def parking_orbit_altitude(atmosphere_top_m: float, body_radius_m: float) -> float:
    """Lowest safe circular parking altitude: just above the atmosphere (15% margin so drag is
    negligible), or a low orbit clearance for an airless body."""
    if atmosphere_top_m > 0:
        return atmosphere_top_m * 1.15
    return max(10_000.0, body_radius_m * 0.05)


# --------------------------------------------------------------------------------------------------
# In-orbit maneuvers about the current body.
# --------------------------------------------------------------------------------------------------

def circularize_at_apoapsis(mu: float, r_apoapsis: float, r_periapsis: float) -> dict:
    """Prograde Δv at apoapsis to raise periapsis up to apoapsis (circularise)."""
    a = (r_apoapsis + r_periapsis) / 2.0
    v_now = astro.vis_viva_speed(mu, r_apoapsis, a)
    v_circ = astro.circular_speed(mu, r_apoapsis)
    return {"maneuver": "circularize", "dv": v_circ - v_now, "prograde": v_circ - v_now, "normal": 0.0, "radial": 0.0}


def deorbit(mu: float, r_apoapsis: float, r_pe_current: float, r_pe_target: float) -> dict:
    """Retrograde Δv at apoapsis to drop periapsis to r_pe_target (typically into the atmosphere)."""
    dv = astro.deorbit_dv(mu, r_apoapsis, r_pe_current, r_pe_target)
    return {"maneuver": "deorbit", "dv": dv, "prograde": -dv, "normal": 0.0, "radial": 0.0}


def capture(mu: float, r_periapsis: float, sma_arrival: float, r_target_apoapsis: float) -> dict:
    """Retrograde Δv at periapsis to capture a hyperbolic arrival into an ellipse."""
    dv = astro.capture_dv(mu, r_periapsis, sma_arrival, r_target_apoapsis)
    return {"maneuver": "capture", "dv": dv, "prograde": -dv, "normal": 0.0, "radial": 0.0}


# --------------------------------------------------------------------------------------------------
# Interplanetary departure — the Oberth ejection from a parking orbit, fully calculated.
# --------------------------------------------------------------------------------------------------

def interplanetary_transfer(
    mu_sun: float, mu_body: float, r_body_orbit: float, r_target_orbit: float, r_park: float
) -> dict:
    """Departure budget from a circular parking orbit to a Hohmann transfer toward another planet.

    Returns the prograde ejection Δv, the hyperbolic excess speed, the transfer time and the
    heliocentric phase angle the target must lead by at ejection. The actual node UT (when the craft
    is at the correct ejection point) is found by the executor from the live phase angle; this gives
    the magnitude and the window."""
    d = astro.interplanetary_departure(mu_sun, mu_body, r_body_orbit, r_target_orbit, r_park)
    return {
        "maneuver": "ejection",
        "dv": d["ejection_dv"],
        "prograde": d["ejection_dv"],
        "normal": 0.0,
        "radial": 0.0,
        "v_infinity": d["v_infinity"],
        "transfer_time_s": d["transfer_time_s"],
        "phase_angle_deg": math.degrees(d["phase_angle_rad"]),
    }


# --------------------------------------------------------------------------------------------------
# Propulsive landing — the Musk/Starship way. No parachutes: the hoverslam law nulls velocity at the
# surface using only the engines. Returns the ignition altitude and the reference-curve parameters.
# --------------------------------------------------------------------------------------------------

def landing_burn(
    *, speed_mps: float, altitude_m: float, mass_t: float, thrust_n: float, g: float, throttle_fraction: float = 0.9
) -> dict:
    """Propulsive (no-chute) landing plan at the current descent state.

    `ignition_altitude` is where a full-thrust burn must start to null `speed_mps` by touchdown;
    `reference_speed` is the hoverslam curve value at the current altitude (coast below it, burn on
    it); `should_burn` is whether the craft is at/above the curve now. The executor calls this every
    tick with the live speed/altitude and applies `hoverslam_throttle`."""
    ignition = astro.suicide_burn_altitude(speed_mps, mass_t, thrust_n, g)
    ref = astro.hoverslam_reference_speed(altitude_m, mass_t, thrust_n, g, throttle_fraction)
    return {
        "maneuver": "propulsive_landing",
        "ignition_altitude": ignition,
        "reference_speed": ref,
        "should_burn": speed_mps >= ref or altitude_m <= ignition,
        "throttle": astro.hoverslam_throttle(speed_mps, ref, mass_t, thrust_n, g),
    }


def node_burn_time(mass_t: float, thrust_n: float, dv: float, isp_vac_s: float = 0.0) -> dict:
    """Finite-burn timing for a node: total burn time and the lead (start the burn half a burn-time
    before the node so it is centred on it)."""
    bt = astro.burn_time_s(mass_t, thrust_n, dv, isp_vac_s)
    return {"burn_time_s": bt, "lead_s": astro.finite_burn_lead_s(bt)}
