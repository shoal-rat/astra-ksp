"""Time warp with safety caps: never past atmospheric entry or a terrain floor, never under thrust."""

from __future__ import annotations

import math
import time
from typing import Any

from astra.errors import AstraError
from astra.ksp import ksp
from astra.reflex.engine import settle_controls


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not math.isnan(x) and not math.isinf(x)


def descent_crossing_ut(orbit, body_radius: float, altitude_m: float, now: float) -> float | None:
    """UT when the orbit next descends through `altitude_m`, or None if it never does."""
    r = body_radius + altitude_m
    if orbit.periapsis >= r:
        return None  # never descends that low
    try:
        ta = orbit.true_anomaly_at_radius(r)
    except Exception:  # noqa: BLE001
        return None
    if not _finite(ta):
        return None
    ut = orbit.ut_at_true_anomaly(-abs(ta))  # descending crossing is before periapsis
    if ut < now:
        period = orbit.period
        ut = ut + period if _finite(period) else None
    return ut


def warp(*, to: str, ut: float | None, seconds: float | None, offset_s: float, floor_alt_m: float | None,
         max_rails_rate: float) -> dict:
    k = ksp()
    k.require_flight()
    sc = k.sc
    v = k.vessel()
    orbit = v.orbit
    body = orbit.body
    now = sc.ut
    if to == "ut":
        if ut is None:
            raise AstraError("to='ut' needs ut")
        target = ut
    elif to == "in":
        if seconds is None:
            raise AstraError("to='in' needs seconds")
        target = now + seconds
    elif to == "apoapsis":
        target = now + orbit.time_to_apoapsis
    elif to == "periapsis":
        target = now + orbit.time_to_periapsis
    elif to == "soi":
        t = orbit.time_to_soi_change
        if not _finite(t):
            raise AstraError("no SOI change on the current trajectory", "check orbit_info patches")
        target = now + t
    elif to == "node":
        nodes = v.control.nodes
        if not nodes:
            raise AstraError("no maneuver node planned")
        target = nodes[0].ut
    else:
        raise AstraError(f"unknown warp target {to!r}", "to: ut | in | apoapsis | periapsis | soi | node")
    target -= offset_s
    if target <= now + 1.0:
        raise AstraError(f"target time is {target - now:.1f} s away; nothing to warp",
                         "for short waits use fly_until with max_game_s")

    flight = v.flight(body.reference_frame)
    grounded = str(v.situation).split(".")[-1] in ("landed", "splashed", "pre_launch")
    if body.has_atmosphere and flight.mean_altitude < body.atmosphere_depth and not grounded:
        raise AstraError("inside the atmosphere: rails warp is unavailable in flight and physics warp heats the craft",
                         "use fly_until (optionally with physics_warp) to wait inside the atmosphere; "
                         "on the ground rails warp works")
    if v.control.throttle > 0 or v.thrust > 1.0:
        v.control.throttle = 0.0
        settle_controls(k, lambda: v.thrust <= 1.0, timeout_s=2.0)
    if v.thrust > 1.0:
        raise AstraError(f"engines still produce {v.thrust / 1000:.1f} kN with the throttle cut",
                         "solid boosters cannot be stopped; wait for burnout or decouple them")

    cap_alt = body.atmosphere_depth if body.has_atmosphere else floor_alt_m
    clamp_note = None
    if cap_alt is not None and not grounded:  # a landed vessel has no descent to clamp to
        cross = descent_crossing_ut(orbit, body.equatorial_radius, cap_alt, now)
        if cross is not None and cross < target:
            what = "atmosphere entry" if body.has_atmosphere else f"descending through {cap_alt:.0f} m"
            clamp_note = f"clamped to {what} at UT {cross:.1f} (requested {target:.1f})"
            target = cross - 30.0
    if target <= now + 1.0:
        raise AstraError(f"the safe warp window is too short ({clamp_note})",
                         "you are about to reach the atmosphere/terrain floor; fly it with fly_until/fly_descent")

    v.control.throttle = 0.0
    k.set_paused(False)
    t0 = time.monotonic()
    body_before = body.name
    sc.warp_to(target, max_rails_rate, 1.0)
    sc.rails_warp_factor = 0
    time.sleep(0.5)  # patched conics settle after warp
    v = k.vessel()
    o = v.orbit
    res = {
        "requested": to, "target_ut": target, "reached_ut": sc.ut, "real_s": round(time.monotonic() - t0, 1),
        "clamped": clamp_note, "body_before": body_before, "body_after": o.body.name,
        "orbit_after": {"apoapsis_m": o.apoapsis_altitude, "periapsis_m": o.periapsis_altitude,
                        "time_to_apoapsis_s": o.time_to_apoapsis, "time_to_periapsis_s": o.time_to_periapsis,
                        "time_to_soi_change_s": o.time_to_soi_change},
        "situation": str(v.situation).split(".")[-1],
    }
    res["paused"] = k.hold_for_deliberation()
    return res
