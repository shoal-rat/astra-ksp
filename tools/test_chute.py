"""EXPENDABLE-CRAFT CHUTE TEST (run before any crewed rescue).

Reenter an UNCREWED vessel and ARM its parachute EARLY, then watch the parachute state and the
speed all the way down. Boke's crew died because the recovery armed the chute only at alt<4.5km &
speed<330 — a window a steep reentry reaches too low to open in time. A stock chute, once armed,
auto-deploys the moment it is SAFE (speed/pressure), so arming early at altitude is the reliable fix.
This script verifies that empirically without risking any crew.

    PYTHONPATH=src python tools/test_chute.py configs/local-ksp.yaml AI-HLS-Starship-1e483096
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from ksp_lab.config import load_config
from ksp_lab.flight_controller import KrpcFlightController


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> int:
    cfg = load_config(Path(sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml").resolve())
    name = sys.argv[2] if len(sys.argv) > 2 else "AI-HLS-Starship-1e483096"
    ctrl = KrpcFlightController(cfg["krpc"])
    conn = ctrl._connect("testchute")
    sc = conn.space_center
    v = next((x for x in sc.vessels if x.name == name), None)
    if v is None:
        log(f"{name!r} not found")
        return 2
    if v.crew_count > 0:
        log("REFUSING: vessel has crew — the chute test must be EXPENDABLE/uncrewed")
        return 2
    sc.active_vessel = v
    time.sleep(3)
    v = sc.active_vessel
    body = v.orbit.body
    ref = body.non_rotating_reference_frame
    log(f"{v.name!r} ap {v.orbit.apoapsis_altitude/1000:.0f}k pe {v.orbit.periapsis_altitude/1000:.0f}k "
        f"chutes {len(v.parts.parachutes)} engines {len(v.parts.engines)} thrust {v.available_thrust:.0f}")
    if not v.parts.parachutes:
        log("no parachute on this craft")
        return 2

    ap = v.auto_pilot
    ap.reference_frame = ref
    v.control.rcs = True

    def retro():
        vel = v.velocity(ref)
        return (-vel[0], -vel[1], -vel[2])

    # Deorbit to a ~30 km periapsis (shallow — gives the chute altitude/time to open).
    ap.target_direction = retro()
    ap.engage()
    time.sleep(8)
    has_engine = v.available_thrust > 1
    t0 = time.monotonic()
    while time.monotonic() - t0 < 150:
        ap.target_direction = retro()
        if v.orbit.periapsis_altitude < 30000:
            break
        if has_engine:
            v.control.throttle = 1.0 if v.orbit.periapsis_altitude > 50000 else 0.3
        else:
            v.control.forward = 1.0
        time.sleep(0.5)
    v.control.throttle = 0.0
    v.control.forward = 0.0
    log(f"deorbit pe {v.orbit.periapsis_altitude/1000:.1f}k")

    ap.reference_frame = v.surface_velocity_reference_frame
    ap.target_direction = (0, -1, 0)
    ap.engage()
    time.sleep(3)
    # Coast down to the atmosphere edge by STEPPING rails warp — NOT sc.warp_to(periapsis), which
    # hangs forever: the craft enters the atmosphere at 70 km long before the periapsis TIME, KSP
    # cancels rails warp, but warp_to keeps waiting for a UT the vessel won't reach by rails. That
    # hang is what stopped the reentry/chute logic and killed Gangwei.
    t0 = time.monotonic()
    while v.flight().mean_altitude > 72000 and time.monotonic() - t0 < 220:
        alt = v.flight().mean_altitude
        sc.rails_warp_factor = 3 if alt > 150000 else (2 if alt > 95000 else 1)
        time.sleep(0.4)
    sc.rails_warp_factor = 0
    time.sleep(1)

    # Reentry: ARM the chute early (alt < 15 km). Re-arm every loop until it is no longer STOWED, so a
    # single missed call can't doom it. Then KSP auto-deploys it when speed/pressure are safe.
    armed = False
    t0 = time.monotonic()
    last = ""
    while time.monotonic() - t0 < 400:
        f = v.flight(body.reference_frame)
        alt, spd = f.surface_altitude, f.speed
        states = []
        for p in v.parts.parachutes:  # these are Parachute objects already, not Parts
            try:
                states.append(str(p.state).split(".")[-1])
            except Exception:
                states.append("?")
        m = f"alt {alt/1000:.1f}k spd {spd:.0f} chute {states} {str(v.situation).split('.')[-1]}"
        if m != last:
            log("  " + m)
            last = m
        if alt < 15000 and any(s.lower() in ("stowed", "?") for s in states):
            for p in v.parts.parachutes:
                try:
                    p.deploy()
                except Exception:
                    pass
            if not armed:
                log("  chute ARM commanded (will auto-deploy when safe)")
                armed = True
        if str(v.situation) in ("VesselSituation.landed", "VesselSituation.splashed"):
            break
        time.sleep(1)
    time.sleep(2)
    v = sc.active_vessel
    f = v.flight(body.reference_frame)
    safe = f.speed < 15
    log(f"=== CHUTE TEST {'PASS (soft landing)' if safe else 'FAIL (fast impact)'} === "
        f"{v.name!r} {str(v.situation).split('.')[-1]} final_speed {f.speed:.1f} m/s parts {len(v.parts.all)}")
    conn.close()
    return 0 if safe else 2


if __name__ == "__main__":
    raise SystemExit(main())
