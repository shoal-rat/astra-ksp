"""Bring an already-in-orbit crewed CAPSULE home: deorbit (RCS), reenter, parachute, land.

⚠️ DANGER — THIS TOOL HAS KILLED CREW. On 2026-06-22 an earlier version killed 5 live kerbals:
  * It reentered FULL STACKS whole (engine+tanks+cabin) → they tumbled and the chute could not slow
    them → 238 m/s impact. It now REFUSES any vessel that has an engine (guard below). A stack must
    be jettisoned to its bare capsule (return_and_recover.py) or grabber-rescued instead.
  * A clean capsule (Boke) still crashed because its parachute did not actually deploy/function on
    command (splashed 152 m/s). A `chutes>0` count does NOT guarantee a working chute.
  * Merely making an uncontrollable/chuteless craft ACTIVE near periapsis decays its orbit into a
    reentry and dooms it. Do NOT switch focus to such craft.
DO NOT use this on a crewed vessel unless you have INDEPENDENTLY verified: no engine, a small part
count, and a parachute that you have confirmed deploys (test on an expendable craft first). Live
crew is irreversible — a stranded-but-alive crew is far better than a dead one.

    PYTHONPATH=src python tools/recover_crew.py configs/local-ksp.yaml "Gangwei Kerman"
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
    crew_name = sys.argv[2] if len(sys.argv) > 2 else "Gangwei Kerman"
    ctrl = KrpcFlightController(cfg["krpc"])
    conn = ctrl._connect("recover")
    sc = conn.space_center

    v = next((vv for vv in sc.vessels if vv.crew_count > 0
              and any(k.name == crew_name for k in vv.crew)), None)
    if v is None:
        log(f"no in-flight vessel carries {crew_name!r}")
        return 2
    sc.active_vessel = v
    time.sleep(3)
    # Re-select by crew identity, NOT sc.active_vessel: KSP can auto-switch focus to a different
    # nearby vessel (e.g. one reentering close by), and grabbing active_vessel then controls the
    # WRONG craft. Re-find the one that actually carries our kerbal.
    v = next((vv for vv in sc.vessels if vv.crew_count > 0
              and any(k.name == crew_name for k in vv.crew)), v)
    if sc.active_vessel.name != v.name:
        sc.active_vessel = v
        time.sleep(2)
    # SAFETY: a full stack (has an engine) tumbles on reentry and the chute cannot save it — that is
    # exactly how Gangwei Kerman was lost. Only self-recover a CLEAN capsule (no engine); a stack
    # must be jettisoned to its capsule first (return_and_recover.py) or grabber-rescued.
    if len(v.parts.engines) > 0:
        log(f"REFUSING: {v.name!r} is a full stack ({len(v.parts.engines)} engine) — would tumble "
            f"and crash on a whole reentry. Needs jettison/grabber, not self-recovery.")
        return 2
    body = v.orbit.body
    ref = body.non_rotating_reference_frame
    chutes = len(v.parts.parachutes)
    has_engine = v.available_thrust > 1.0
    mp = v.resources.amount("MonoPropellant")
    log(f"{v.name!r} crew {v.crew_count} ap {v.orbit.apoapsis_altitude/1000:.0f}k "
        f"pe {v.orbit.periapsis_altitude/1000:.0f}k chutes {chutes} "
        f"thrust {v.available_thrust:.0f} MP {mp:.0f} control {str(v.control.state).split('.')[-1]}")
    if chutes == 0:
        log("ABORT: no parachute — this vessel needs a grabber-rescue, not a self-recovery")
        return 2

    ap = v.auto_pilot
    ap.reference_frame = ref
    v.control.rcs = True

    def retro():
        vel = v.velocity(ref)
        return (-vel[0], -vel[1], -vel[2])

    # 1) Deorbit to a ~24 km periapsis (sure reentry). Burn at apoapsis for high/eccentric orbits
    # (far more efficient than burning low). Use the engine if present, else RCS fore translation
    # (nose held on retrograde, so +forward pushes retrograde).
    if v.orbit.apoapsis_altitude > 150000 and 0 < v.orbit.time_to_apoapsis < v.orbit.period * 0.95:
        log(f"warping {v.orbit.time_to_apoapsis-30:.0f}s to apoapsis for an efficient deorbit ...")
        sc.warp_to(sc.ut + v.orbit.time_to_apoapsis - 30)
        time.sleep(2)
    ap.target_direction = retro()
    ap.engage()
    time.sleep(8)
    t0 = time.monotonic()
    last = ""
    while time.monotonic() - t0 < 240:
        ap.target_direction = retro()
        pe = v.orbit.periapsis_altitude
        if pe < 24000:
            break
        if has_engine:
            v.control.throttle = 1.0 if pe > 45000 else 0.35
        else:
            v.control.forward = 1.0  # RCS retrograde translation
        m = f"deorbit: pe {pe/1000:.1f}k"
        if m != last:
            log("  " + m)
            last = m
        if not has_engine and v.resources.amount("MonoPropellant") < 1:
            log("  out of monoprop")
            break
        time.sleep(0.5)
    v.control.throttle = 0.0
    v.control.forward = 0.0
    log(f"deorbit done: pe {v.orbit.periapsis_altitude/1000:.1f}k")

    # 2) Orient retrograde for reentry and warp down to the atmosphere (rails-warp auto-drops at ~70 km).
    ap.reference_frame = v.surface_velocity_reference_frame
    ap.target_direction = (0, -1, 0)
    ap.engage()
    if v.flight().mean_altitude > 70000 and 0 < v.orbit.time_to_periapsis < 1e6:
        sc.warp_to(sc.ut + v.orbit.time_to_periapsis - 15)
        time.sleep(2)

    # 3) Ride the reentry; pop chutes when safe; confirm landing.
    deployed = False
    t0 = time.monotonic()
    last = ""
    while time.monotonic() - t0 < 400:
        f = v.flight(body.reference_frame)
        alt, spd = f.surface_altitude, f.speed
        m = f"alt {alt/1000:.1f}k speed {spd:.0f} {str(v.situation).split('.')[-1]}"
        if m != last:
            log("  " + m)
            last = m
        if not deployed and alt < 4500 and spd < 330:
            for par in v.parts.parachutes:
                try:
                    par.deploy()
                except Exception:
                    pass
            deployed = True
            log("  parachutes deployed")
        if str(v.situation) in ("VesselSituation.landed", "VesselSituation.splashed"):
            break
        time.sleep(1)
    time.sleep(2)
    v = sc.active_vessel
    ok = str(v.situation) in ("VesselSituation.landed", "VesselSituation.splashed") and v.crew_count > 0
    log(f"=== {'CREW HOME SAFE' if ok else 'RECOVERY INCOMPLETE'} === {v.name!r} "
        f"{str(v.situation).split('.')[-1]} crew {v.crew_count}")
    if v.crew_count > 0:
        log("crew: " + ", ".join(k.name for k in v.crew))
    conn.close()
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
