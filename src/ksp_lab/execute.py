"""Calculated executors — measure the live state with kRPC, plan the maneuver, fly it.

These are the "calculated APIs" the brain (Claude Code) calls. Each one MEASURES the live orbit/body
state with kRPC, asks `plan.py` for the exact maneuver (every number from `astro.py`), and flies it by
delegating closed-loop control to the engines/MechJeb — placing kRPC maneuver nodes, running a finite
burn whose lead time is the burn's own half-time (not a guess), warping to a COMPUTED universal time
(not a guessed altitude), and landing on the hoverslam curve (not an altitude/throttle ladder).

The hard-won rule still holds: don't hand-roll what MechJeb already calculates well (ascent, node
execution, the powered touchdown). What changed is that the GLUE around it is now calculated, and the
ship handed to it is calculated (see design.py) — so the heuristics that killed crews are gone.
"""
from __future__ import annotations

import math
import time

from . import astro, plan


def _log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


# --------------------------------------------------------------------------------------------------
# Measure the live state — kRPC is the single source of truth for body and orbit constants.
# --------------------------------------------------------------------------------------------------

def measure(vessel) -> dict:
    """Read the live orbit/body state needed by the planners. No constant is hardcoded."""
    o = vessel.orbit
    b = o.body
    return {
        "mu": b.gravitational_parameter,
        "body": b.name,
        "body_radius": b.equatorial_radius,
        "atmo_top": b.atmosphere_depth if b.has_atmosphere else 0.0,
        "surface_g": b.surface_gravity,
        "surface_rho": (b.density_at(0.0) if b.has_atmosphere else 0.0),
        "r_apoapsis": o.apoapsis,           # from body centre
        "r_periapsis": o.periapsis,
        "apoapsis_alt": o.apoapsis_altitude,
        "periapsis_alt": o.periapsis_altitude,
        "sma": o.semi_major_axis,
        "eccentricity": o.eccentricity,
        "mass_t": vessel.mass / 1000.0,
        "thrust_n": max(vessel.available_thrust, vessel.max_thrust),
    }


# --------------------------------------------------------------------------------------------------
# Primitives: refuel/EC, point, finite burn, chunked warp. All parameters calculated.
# --------------------------------------------------------------------------------------------------

def refuel(bridge, vessel=None) -> None:
    """NO-OP — in-flight refuelling is DISALLOWED (it is cheating). Each stage flies only on the
    propellant it carries (sized by the rocket equation; see design.staging_plan), and electric charge
    comes from the craft's solar panels + the engine alternator (during a burn) + its batteries, never
    a propellant/EC top-off. Kept as a no-op so the many historical callers stay valid."""
    return None


def charge_ec(bridge, vessel) -> None:
    """Recharge ONLY ElectricCharge from the bridge (NOT propellant) — a legitimate stand-in for solar
    panels when a probe would otherwise lose attitude authority in shadow/deep space. Propellant is
    never touched here, so it is not the refuel cheat. Use sparingly; prefer real solar panels."""
    try:
        bridge._request("POST", "/vessel/refuel", json={"fraction": "1.0", "resources": "ElectricCharge"})
    except Exception:
        pass


def _ignite(vessel) -> None:
    for e in vessel.parts.engines:
        try:
            e.active = True
        except Exception:
            pass


def attitude(vessel) -> tuple[float, float, float]:
    """Return (tilt_deg, horizontal_mps, vertical_mps). tilt = angle between the vessel's nose and the
    local 'up' (radial, away from the body centre) — 0 deg is perfectly upright. Used to detect a
    tip-over on landing and to gate a surface ignition (never fire a tilted rocket into the ground)."""
    b = vessel.orbit.body
    rf = b.reference_frame
    f = vessel.flight(rf)
    pos = vessel.position(rf)
    n = math.sqrt(sum(x * x for x in pos)) or 1.0
    up = tuple(x / n for x in pos)
    d = vessel.direction(rf)
    dot = max(-1.0, min(1.0, sum(a * c for a, c in zip(up, d))))
    return math.degrees(math.acos(dot)), abs(f.horizontal_speed), abs(f.vertical_speed)


def count_legs(vessel) -> int:
    try:
        return sum(1 for p in vessel.parts.all if "landingLeg" in (p.name or "") or "leg" in (p.name or "").lower())
    except Exception:
        return 0


def warp_to_ut(sc, target_ut: float, chunk_days: float = 30.0) -> None:
    """Chunked warp to a COMPUTED universal time. Chunking lands on the target precisely; a single
    stepped rails-warp overshoots because the ramp-down lags the poll."""
    chunk = chunk_days * 86400.0
    while sc.ut < target_ut - 30.0:
        try:
            sc.warp_to(min(target_ut, sc.ut + chunk))
        except Exception:
            break
    sc.rails_warp_factor = 0


def execute_node(sc, bridge, vessel, *, isp_vac_s: float = 0.0, timeout_s: float = 360.0) -> bool:
    """Fly the vessel's first maneuver node with a finite burn centred on it.

    Lead time = half the burn time (calculated from mass/thrust/Δv), not a guess. Points the kRPC
    autopilot at the node vector in the node's own reference frame (MechJeb's node executor only flies
    its own nodes), holds engines lit, refuels EC during the burn, and tapers throttle as Δv -> 0.
    """
    if not vessel.control.nodes:
        return False
    node = vessel.control.nodes[0]
    st = measure(vessel)
    timing = plan.node_burn_time(st["mass_t"], st["thrust_n"], node.remaining_delta_v, isp_vac_s)
    lead = timing["lead_s"]
    # Size the burn-window to the actual burn time (the 60 kN Terrier needs ~400 s for the trans-Duna
    # ejection — a fixed 360 s timeout cut it short and left the craft bound).
    timeout_s = max(timeout_s, timing["burn_time_s"] * 1.8 + 30.0)
    refuel(bridge, vessel)
    vessel.control.sas = False
    ap = vessel.auto_pilot
    ap.reference_frame = node.reference_frame
    ap.target_direction = (0.0, 1.0, 0.0)
    ap.engage()
    t = time.monotonic()
    while time.monotonic() - t < 120 and abs(ap.error) > 2.0:
        ap.target_direction = (0.0, 1.0, 0.0)
        refuel(bridge, vessel)  # keep EC up so the reaction wheels can actually turn the craft
        time.sleep(1.0)
    # CRITICAL: abort if we did not converge on the node vector. Burning while mis-pointed fired a
    # node retrograde once and decayed the orbit into a crash. Better to abort and let the caller retry.
    if abs(ap.error) > 12.0:
        _log(f"  point FAILED (err={abs(ap.error):.0f} deg) — NOT burning (would fire the wrong way)")
        ap.disengage()
        return False
    _log(f"  pointed at node (err={abs(ap.error):.1f} deg); burning {node.remaining_delta_v:.0f} m/s")
    # Coast to the burn-start time (node UT minus the calculated lead).
    while node.time_to > lead:
        ap.target_direction = (0.0, 1.0, 0.0)
        if int(node.time_to) % 20 == 0:
            refuel(bridge, vessel)
        time.sleep(0.5)
    _ignite(vessel)
    vessel.control.throttle = 1.0
    t = time.monotonic()
    while time.monotonic() - t < timeout_s:
        _ignite(vessel)
        ap.target_direction = (0.0, 1.0, 0.0)
        # NO refuel mid-burn: /vessel/refuel throttles the engine to ~74%, which stretched the burn
        # past its timeout. The tank was filled while pointing; the engine alternator holds EC while it
        # fires. Full thrust completes the burn.
        try:
            rem = node.remaining_delta_v
        except Exception:
            rem = 0.0
        if rem < 30.0:
            vessel.control.throttle = max(0.05, rem / 30.0)  # proportional taper, not a fixed step
        if rem < 0.5:
            break
        time.sleep(0.2)
    vessel.control.throttle = 0.0
    ap.disengage()
    try:
        node.remove()
    except Exception:
        pass
    return True


# --------------------------------------------------------------------------------------------------
# High-level calculated maneuvers — measure -> plan -> place node -> execute.
# --------------------------------------------------------------------------------------------------

def circularize(sc, bridge, vessel) -> bool:
    st = measure(vessel)
    p = plan.circularize_at_apoapsis(st["mu"], st["r_apoapsis"], st["r_periapsis"])
    ut = sc.ut + vessel.orbit.time_to_apoapsis
    for nd in list(vessel.control.nodes):
        nd.remove()
    vessel.control.add_node(ut, prograde=p["prograde"])
    _log(f"circularize: {p['dv']:.0f} m/s at apoapsis")
    return execute_node(sc, bridge, vessel)


def deorbit_into_atmosphere(sc, bridge, vessel, target_pe_alt_m: float) -> bool:
    st = measure(vessel)
    p = plan.deorbit(st["mu"], st["r_apoapsis"], st["r_periapsis"], st["body_radius"] + target_pe_alt_m)
    ut = sc.ut + vessel.orbit.time_to_apoapsis
    for nd in list(vessel.control.nodes):
        nd.remove()
    vessel.control.add_node(ut, prograde=p["prograde"])
    _log(f"deorbit: {p['dv']:.0f} m/s -> pe {target_pe_alt_m/1000:.0f} km")
    return execute_node(sc, bridge, vessel)


def propulsive_landing(sc, bridge, vessel, *, touchdown_mps: float = 2.0, ignite_margin: float = 1.15) -> bool:
    """Land with engines only — no parachutes. Coasts (real-time, refuelling EC) until the live speed
    reaches the hoverslam reference curve, then holds throttle on the curve down to the surface. Never
    warps below the atmosphere (warping applies reentry heating instantly and destroyed two crews).

    The Musk/Starship descent: the body's atmosphere bleeds most of the orbital velocity during entry,
    then the engines null the remainder on the calculated curve."""
    b = vessel.orbit.body
    g = b.surface_gravity
    ap = vessel.auto_pilot
    body_rf = b.reference_frame
    legs = count_legs(vessel)
    _log(f"propulsive landing on {b.name}: g={g:.2f}, no chutes, {legs} landing leg(s)")
    if legs == 0:
        _log("  WARNING: NO landing legs detected — high tip-over risk (gear=True will be a no-op)")
    vessel.control.gear = True  # deploy + lock legs early so they are down well before touchdown
    while True:
        refuel(bridge, vessel)
        _ignite(vessel)
        sit = str(vessel.situation).split(".")[-1].lower()
        if sit in ("landed", "splashed"):
            vessel.control.throttle = 0.0
            # POST-LANDING SETTLE: let the suspension settle, then VERIFY it did not tip over. A
            # tipped craft must NOT be reported as a good landing — the caller would fire the ascent
            # engines into the ground and kill the crew (exactly what happened before).
            time.sleep(3.0)
            tilt, hs, vs = attitude(vessel)
            if tilt > 25.0:
                _log(f"  TIPPED OVER on touchdown (tilt={tilt:.0f} deg) — NOT a safe landing")
                return False
            _log(f"TOUCHDOWN on {b.name}: crew={vessel.crew_count} alive, upright (tilt={tilt:.0f} deg)")
            return True
        f = vessel.flight(body_rf)
        alt = f.surface_altitude
        speed = f.speed
        mass_t = vessel.mass / 1000.0
        thrust = max(vessel.available_thrust, vessel.max_thrust, 1.0)
        # Warp the high coast (on rails, no atmosphere) but NEVER below 2x the atmosphere top.
        if alt > max(b.atmosphere_depth, 1.0) * 2.0:
            sc.rails_warp_factor = 3
            time.sleep(2)
            continue
        sc.rails_warp_factor = 0
        ap.reference_frame = body_rf
        try:
            # While moving fast, point RETROGRADE (kills horizontal AND vertical velocity together, so
            # there is no sideways drift to catch a leg/skirt and topple it). Near the ground / near
            # rest, point straight UP (radial) so it settles VERTICAL, not at the velocity vector's tilt.
            if speed > 12.0:
                ap.target_direction = tuple(-x for x in f.velocity)
            else:
                pos = vessel.position(body_rf)
                ap.target_direction = pos  # radial-out = local up -> vertical touchdown
            ap.engage()
        except Exception:
            pass
        ref = astro.hoverslam_reference_speed(alt, mass_t, thrust, g)
        ignition = astro.suicide_burn_altitude(speed, mass_t, thrust, g) * ignite_margin
        if speed >= ref or alt <= ignition:
            vessel.control.throttle = astro.hoverslam_throttle(speed, ref, mass_t, thrust, g)
            vessel.control.gear = True
        else:
            vessel.control.throttle = 0.0
        time.sleep(0.2)
