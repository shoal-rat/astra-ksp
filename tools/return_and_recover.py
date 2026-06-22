"""Bring a crewed Orion home: deorbit, JETTISON the service section at the capsule decoupler (so the
reentry vehicle is just pod+heatshield+parachute — a short, stable capsule), reenter, recover.

⛔ DEPRECATED for the reentry/landing half — SUPERSEDED BY tools/mj_land_vessel.py (MechJeb Landing
Autopilot computes the deorbit + deceleration burn + parachute-deployment timing that this file
hand-rolls with guessed constants: the 32 km target periapsis, the 14 km chute-arm altitude, the
400 s reentry timeout). Use this tool ONLY for the value it still adds — the JETTISON-to-bare-capsule
step — then hand the freed capsule to `/mj-land`. Logic unchanged; do NOT extend the hand-flown
descent below.

Requires the craft built with the separable-capsule fix (craft_writer adds a Decoupler.1 directly
below HeatShield1 for crewed designs). The old long-command-bus Orions tumble + lose the crew.

    PYTHONPATH=src python tools/return_and_recover.py configs/local-ksp.yaml <VESSEL>
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
    name = sys.argv[2] if len(sys.argv) > 2 else "AI-Orion-Artemis"
    ctrl = KrpcFlightController(cfg["krpc"])
    bridge = BridgeClient(**cfg["bridge"])
    conn = ctrl._connect("return")
    sc = conn.space_center

    v = ctrl._select_vessel(conn, name)
    sc.active_vessel = v
    time.sleep(2)
    v = sc.active_vessel
    try:
        bridge.refuel_vessel(name, fraction=1.0, resources="LiquidFuel,Oxidizer,MonoPropellant")
    except Exception:
        pass
    log(f"{name}: ap {v.orbit.apoapsis_altitude/1000:.0f}k pe {v.orbit.periapsis_altitude/1000:.0f}k crew {v.crew_count}")

    body = v.orbit.body
    ref = body.non_rotating_reference_frame
    ap = v.auto_pilot
    ap.reference_frame = ref
    v.control.rcs = True

    def retro():
        vel = v.velocity(ref)
        return (-vel[0], -vel[1], -vel[2])

    # 1) Deorbit to a ~32 km periapsis (the reentry sweet spot — 38 skips, 22 is too hot).
    ap.target_direction = retro()
    ap.engage()
    time.sleep(10)
    v.control.throttle = 1.0
    t0 = time.monotonic()
    while time.monotonic() - t0 < 60:
        ap.target_direction = retro()
        if v.orbit.periapsis_altitude < 32000:
            break
        v.control.throttle = 1.0 if v.orbit.periapsis_altitude > 50000 else 0.3
        time.sleep(0.5)
    v.control.throttle = 0.0
    log(f"deorbited: pe {v.orbit.periapsis_altitude/1000:.1f}k")

    # 2) JETTISON the service section at the capsule decoupler (the Decoupler whose parent is the
    # heatshield), while still in vacuum, so only the clean capsule reenters.
    heat = next((p for p in v.parts.all if "HeatShield" in p.name), None)
    jettisoned = False
    if heat is not None:
        for child in heat.children:
            if "Decoupler" in child.name or "decoupler" in child.title.lower():
                for m in child.modules:
                    for ev in m.events:
                        if ("分离" in ev) or ("decouple" in ev.lower()):
                            try:
                                m.trigger_event(ev)
                                jettisoned = True
                                log(f"jettisoned service section via {child.title!r}")
                            except Exception:
                                pass
                            break
                if jettisoned:
                    break
    if not jettisoned:
        log("WARNING: could not find capsule decoupler; reentering whole stack (may tumble)")
    time.sleep(3)

    # 3) Switch to the crewed capsule and reenter it. Match by the ORIGINAL vessel name + crew + a
    # parachute (the capsule keeps the root pod's name and carries the chute; the jettisoned service
    # section is a separate, chuteless vessel). A loose "any crewed low-pe vessel" match grabbed an
    # unrelated clutter vessel last time.
    base = name.split()[0]
    cap = next((vv for vv in sc.vessels if base in vv.name and vv.crew_count > 0
                and len(vv.parts.parachutes) > 0), None)
    if cap is None:
        cap = next((vv for vv in sc.vessels if base in vv.name and vv.crew_count > 0), None)
    if cap is not None:
        sc.active_vessel = cap
        time.sleep(2)
        v = sc.active_vessel
    log(f"reentering capsule: {v.name} crew {v.crew_count} parts {len(v.parts.all)}")

    ap = v.auto_pilot
    ap.reference_frame = v.surface_velocity_reference_frame
    ap.target_direction = (0, -1, 0)
    ap.engage()
    time.sleep(3)
    # Step rails warp down to the atmosphere edge. Do NOT sc.warp_to(periapsis): with a sub-atmosphere
    # periapsis the craft enters the atmosphere at 70 km long before the periapsis TIME, KSP cancels
    # rails warp, and warp_to then BLOCKS forever waiting for a UT it won't reach — the reentry/chute
    # loop never runs and the crew die chuteless. (Verified root cause of the Gangwei loss.)
    tw = time.monotonic()
    while v.flight().mean_altitude > 72000 and time.monotonic() - tw < 220:
        alt = v.flight().mean_altitude
        sc.rails_warp_factor = 3 if alt > 150000 else (2 if alt > 95000 else 1)
        time.sleep(0.4)
    sc.rails_warp_factor = 0
    time.sleep(1)
    chutes = False
    t0 = time.monotonic()
    last = ""
    while time.monotonic() - t0 < 400:
        f = v.flight(body.reference_frame)
        alt, spd = f.surface_altitude, f.speed
        m = f"alt {alt/1000:.1f}k speed {spd:.0f} sit {str(v.situation)}"
        if m != last:
            log("  " + m)
            last = m
        # ARM the chute EARLY (alt < 14 km) and keep re-arming — a stock chute auto-deploys the moment
        # it is SAFE, so arming high beats waiting for a low/slow window a steep reentry never reaches
        # (that, plus reentry heat on a too-steep descent, is how Boke's chute failed to save him).
        if alt < 14000:
            for par in v.parts.parachutes:
                try:
                    par.deploy()
                except Exception:
                    pass
            if not chutes:
                log("  parachutes ARMED (auto-deploy when safe)")
                chutes = True
        if str(v.situation) in ("VesselSituation.landed", "VesselSituation.splashed"):
            break
        time.sleep(1)
    time.sleep(2)
    v = sc.active_vessel
    ok = str(v.situation) in ("VesselSituation.landed", "VesselSituation.splashed") and v.crew_count > 0
    log(f"=== {'CREW HOME SAFE' if ok else 'RETURN INCOMPLETE'} === {v.name} sit {str(v.situation)} crew {v.crew_count}")
    if v.crew_count > 0:
        log("crew: " + ", ".join(k.name for k in v.crew))
    conn.close()
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
