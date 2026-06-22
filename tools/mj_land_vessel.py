"""Reenter and land a vessel by handing the ENTIRE descent to MechJeb's Landing Autopilot.

MechJeb CALCULATES everything the hand-rolled reentry kept getting wrong: the deorbit burn, the
attitude hold (so the craft does NOT tumble), the deceleration / landing burn timing (the "recoil"),
and — critically — the PARACHUTE DEPLOYMENT TIMING. The bridge's /mj-land already sets
DeployChutes=true, DeployGears=true, TouchdownSpeed=0.5. The agent's only job here is to pick the
vessel and let MechJeb fly it; NO altitude/speed/burn timing is computed in Python. This replaces the
hand-rolled return_and_recover/recover_crew whose guessed chute altitude and loop timeouts repeatedly
killed crew (Gangwei, Boke, Defan) — exactly the "use the API, don't reinvent the wheel" principle.

    PYTHONPATH=src python tools/mj_land_vessel.py configs/local-ksp.yaml <VESSEL-NAME-SUBSTRING>
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from ksp_lab.bridge_client import BridgeClient
from ksp_lab.config import load_config
from ksp_lab.flight_controller import KrpcFlightController


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> int:
    cfg = load_config(Path(sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml").resolve())
    name = sys.argv[2] if len(sys.argv) > 2 else ""
    ctrl = KrpcFlightController(cfg["krpc"])
    bridge = BridgeClient(**cfg["bridge"])
    conn = ctrl._connect("mjland")
    sc = conn.space_center

    v = next((x for x in sc.vessels if name and name in x.name), None)
    if v is None:
        v = ctrl._select_vessel(conn, name)
    sc.active_vessel = v
    time.sleep(3)
    # Re-select by name after the switch — KSP can auto-focus a different nearby vessel, and acting on
    # the wrong one is how a recovery grabbed the wrong craft before.
    v = next((x for x in sc.vessels if name and name in x.name), sc.active_vessel)
    if sc.active_vessel.name != v.name:
        sc.active_vessel = v
        time.sleep(2)
    body = v.orbit.body
    log(f"{v.name!r} crew {v.crew_count} ap {v.orbit.apoapsis_altitude/1000:.0f}k "
        f"pe {v.orbit.periapsis_altitude/1000:.0f}k chutes {len(v.parts.parachutes)} "
        f"engines {len(v.parts.engines)}")

    # Hand the whole reentry+landing to MechJeb's Landing Autopilot (deorbit -> decel burn -> chutes).
    try:
        r = bridge.mj_land(touchdown_speed=0.5)
    except Exception as exc:
        log(f"mj_land call failed: {exc}")
        conn.close()
        return 2
    log(f"MechJeb Landing Autopilot engaged: {r}")

    # Monitor ONLY. MechJeb owns all the timing; the generous limit guarantees the chute phase is
    # never cut off the way the old 400 s reentry loop cut it off 1.7 km above chute-arm and killed
    # Defan. We do not deploy chutes or compute burns here.
    t0 = time.monotonic()
    last = ""
    landing_seen = False
    while time.monotonic() - t0 < 2400:
        try:
            f = v.flight(body.reference_frame)
            alt, spd, sit = f.mean_altitude, f.speed, str(v.situation).split(".")[-1]
        except Exception:
            time.sleep(2)
            continue
        try:
            landing_seen = landing_seen or bool(bridge.mj_status().get("landingEnabled", False))
        except Exception:
            pass
        m = f"alt {alt/1000:.1f}k spd {spd:.0f} {sit} mjLanding={landing_seen}"
        if m != last:
            log("  " + m)
            last = m
        if sit in ("landed", "splashed"):
            break
        time.sleep(3)

    time.sleep(3)
    v = sc.active_vessel
    f = v.flight(body.reference_frame)
    sit = str(v.situation).split(".")[-1]
    ok = sit in ("landed", "splashed") and f.speed < 10
    log(f"=== {'LANDED SOFT' if ok else 'INCOMPLETE'} === {v.name!r} {sit} "
        f"final_speed {f.speed:.1f} m/s crew {v.crew_count}")
    if v.crew_count > 0:
        log("crew: " + ", ".join(k.name for k in v.crew))
    conn.close()
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
