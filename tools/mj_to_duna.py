"""Transfer a vessel from low Kerbin orbit to Duna (KSP1's "Mars"), delegating the burns to MechJeb.

The interplanetary part is the only thing mj_to_mun can't do (its grid search is intra-SOI). Here we
CALCULATE the Hohmann transfer — the launch-window phase angle and the trans-Duna ejection Δv from
closed-form orbital mechanics (NOT guessed) — warp to the window, place a prograde ejection node, and
hand the burn to MechJeb's node executor (/mj-execute-node). After the Kerbol coast we capture at Duna
with the proven pure-retrograde burn (Duna also has an atmosphere, so a low capture periapsis
aerobrakes for free). A mid-course correction may be needed live — interplanetary aim is sensitive;
that's expected and is a small follow-up node.

    PYTHONPATH=src python tools/mj_to_duna.py configs/local-ksp.yaml <vessel-name>
"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

from ksp_lab.bridge_client import BridgeClient
from ksp_lab.config import load_config
from ksp_lab.flight_controller import KrpcFlightController
from ksp_lab.guidance import (
    ejection_burn_delta_v_mps,
    hohmann_transfer_delta_v_mps,
    hohmann_transfer_time_s,
    outward_transfer_phase_angle_rad,
)
from ksp_lab.telemetry import TelemetryRecorder


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _heliocentric_phase(sun_ref, departure_body, target_body) -> float:
    """Angle the target LEADS the departure body about the sun, in [0, 2π). Uses the ecliptic (x-z)
    projection of each body's position in the sun's non-rotating frame (KSP's orbital plane ≈ x-z)."""
    pa = departure_body.position(sun_ref)
    pb = target_body.position(sun_ref)
    ang_a = math.atan2(pa[2], pa[0])
    ang_b = math.atan2(pb[2], pb[0])
    return (ang_b - ang_a) % (2.0 * math.pi)


def main() -> int:
    cfg = load_config(Path(sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml").resolve())
    name = sys.argv[2] if len(sys.argv) > 2 else "AI-Duna-Comsat"
    ctrl = KrpcFlightController(cfg["krpc"])
    bridge = BridgeClient(**cfg["bridge"])
    conn = ctrl._connect("mjduna")
    sc = conn.space_center
    rec = TelemetryRecorder(_run_dir(cfg) / f"mjduna-{name}.jsonl")

    v = ctrl._select_vessel(conn, name)
    sc.active_vessel = v
    sc.rails_warp_factor = 0
    try:
        v.control.remove_nodes()
    except Exception:
        pass

    kerbin = v.orbit.body
    if kerbin.name != "Kerbin":
        log(f"FAILED: start in Kerbin orbit (currently {kerbin.name}).")
        return 2
    sun = kerbin.orbit.body                       # Kerbol
    duna = next((b for b in sc.bodies.values() if b.name == "Duna"), None)
    if duna is None:
        log("FAILED: no body named 'Duna' in this install.")
        return 2

    sun_ref = sun.non_rotating_reference_frame
    mu_sun = sun.gravitational_parameter
    mu_kerbin = kerbin.gravitational_parameter
    r1 = kerbin.orbit.semi_major_axis
    r2 = duna.orbit.semi_major_axis
    r_park = kerbin.equatorial_radius + v.orbit.periapsis_altitude  # ~ current parking radius

    # Reuse the existing guidance helpers (no re-derivation): heliocentric Hohmann Δv == v_inf, the
    # transfer time, the required phase angle; then the Oberth ejection burn from the parking orbit.
    v_inf = hohmann_transfer_delta_v_mps(mu_sun, r1, r2)
    transfer_time = hohmann_transfer_time_s(mu_sun, r1, r2)
    target_phase = outward_transfer_phase_angle_rad(mu_sun, r2, transfer_time)
    ejection_dv = ejection_burn_delta_v_mps(mu_kerbin, r_park, v_inf)
    log(f"{name}: Kerbin->Duna Hohmann — ejection dv {ejection_dv:.0f} m/s, v_inf {v_inf:.0f} m/s, "
        f"transfer {transfer_time/21600:.0f} Kerbin-days, window phase {math.degrees(target_phase):.1f} deg")
    rec.append({"phase": "duna_transfer_plan", "ejection_dv": ejection_dv, "v_inf": v_inf,
                "transfer_time_s": transfer_time, "target_phase_rad": target_phase})

    def refuel():
        # A render craft's engine starves when its CONNECTED tank empties while sibling tanks stay
        # full (available_thrust -> 0 at full throttle). /vessel/refuel restores flow. (Notebook §7.)
        try:
            bridge.refuel_vessel(name, fraction=1.0, resources="LiquidFuel,Oxidizer,MonoPropellant")
        except Exception:
            pass

    refuel()
    # 0) Raise apoapsis for fast warp. KSP caps rails warp at ~50x in LKO (an altitude limit), far too
    # slow to wait out an interplanetary window (months) — verified live. A prograde burn lifts
    # apoapsis above ~750 km where 100,000x warp unlocks; periapsis stays low for the Oberth ejection.
    if v.orbit.apoapsis_altitude < 750_000:
        kref = kerbin.non_rotating_reference_frame
        apc = v.auto_pilot
        apc.reference_frame = kref
        apc.target_direction = v.velocity(kref)
        apc.engage()
        time.sleep(5)
        v.control.throttle = 1.0
        tw0 = time.monotonic()
        while time.monotonic() - tw0 < 120 and v.orbit.apoapsis_altitude < 1_500_000:
            apc.target_direction = v.velocity(kref)
            time.sleep(0.5)
        v.control.throttle = 0.0
        try:
            apc.disengage()
        except Exception:
            pass
        log(f"  raised apoapsis to {v.orbit.apoapsis_altitude/1000:.0f} km for fast warp")

    # 1) Warp to the launch window: wait until Duna leads Kerbin by the required phase angle.
    # TODO(mj-plan): the precise ejection NODE (angle + timing from this eccentric orbit) is best
    # produced by MechJeb's interplanetary maneuver planner — add the POST /mj-plan bridge endpoint
    # (the audit's #1 gap) and use it here; the kRPC node search below is a calculated approximation.
    target = target_phase
    last = ""
    for _ in range(4000):
        phase = _heliocentric_phase(sun_ref, kerbin, duna)
        err = abs(((phase - target + math.pi) % (2.0 * math.pi)) - math.pi)
        msg = f"window: phase {math.degrees(phase):.1f} deg (target {math.degrees(target):.1f}), err {math.degrees(err):.1f}"
        if msg != last:
            log("  " + msg)
            last = msg
        if err < math.radians(1.2):
            break
        # Warp a fraction of the remaining wait; Kerbin closes the phase faster than Duna.
        w_rel = math.sqrt(mu_sun / r1 ** 3) - math.sqrt(mu_sun / r2 ** 3)
        if w_rel <= 0:
            break
        dt = (((phase - target) % (2.0 * math.pi)) / w_rel)
        sc.warp_to(sc.ut + max(600.0, min(dt * 0.5, 2_000_000.0)))
        time.sleep(0.5)
    log(f"  window reached at phase {math.degrees(_heliocentric_phase(sun_ref, kerbin, duna)):.1f} deg")

    # 2-3) Eject toward Duna with a DIRECT prograde burn at periapsis. Verified live: MechJeb's node
    # executor MISSES a distant ejection node (it overshoots the kRPC pre-warp and mis-times the
    # orient), so the robust pattern is a direct burn — the same one mj_to_mun uses for capture. Burn
    # prograde at periapsis until the HELIOCENTRIC transfer apoapsis reaches Duna's orbit, reading
    # next_orbit while still inside Kerbin's SOI but the CURRENT orbit once we cross into the Sun's SOI
    # (next_orbit is empty in the Sun's SOI; reading it there made an earlier run over-burn to 75 Gm).
    try:
        sc.target_body = duna
    except Exception:
        pass
    v.control.remove_nodes()
    ttp = v.orbit.time_to_periapsis
    if ttp and 30 < ttp < 1e6:
        sc.warp_to(sc.ut + ttp - 30)
        time.sleep(2)
    refuel()
    ap_ctrl = v.auto_pilot
    frame = v.orbit.body.non_rotating_reference_frame
    ap_ctrl.reference_frame = frame
    ap_ctrl.target_direction = v.velocity(frame)
    ap_ctrl.engage()
    time.sleep(6)
    v.control.throttle = 1.0
    t0 = time.monotonic()
    last = ""
    while time.monotonic() - t0 < 240:
        refuel()  # render craft starves mid-burn; refuel every loop to keep the engine fed
        frame = v.orbit.body.non_rotating_reference_frame
        ap_ctrl.reference_frame = frame
        ap_ctrl.target_direction = v.velocity(frame)
        o = v.orbit
        if o.body.name == "Sun":
            helio_ap = o.apoapsis
        else:
            try:
                helio_ap = o.next_orbit.apoapsis if o.next_orbit else 0.0
            except Exception:
                helio_ap = 0.0
        m = f"TDI: in {o.body.name}, heliocentric apoapsis {helio_ap/1e9:.1f}Gm (need {r2/1e9:.0f})"
        if m != last:
            log("  " + m)
            last = m
        if helio_ap >= r2:
            log("  transfer apoapsis reaches Duna's orbit — TDI complete")
            break
        time.sleep(0.5)
    v.control.throttle = 0.0
    try:
        ap_ctrl.disengage()
    except Exception:
        pass
    rec.append({"phase": "duna_tdi_done",
                "helio_ap": v.orbit.apoapsis if v.orbit.body.name == "Sun" else 0.0})

    # 4) Coast to Duna's SOI (warp through the long Kerbol cruise).
    log("  cruising to Duna SOI ...")
    for _ in range(200):
        if v.orbit.body.name == "Duna":
            break
        try:
            dt = v.orbit.time_to_soi_change
        except Exception:
            dt = 0.0
        if dt and 0 < dt < 1e9:
            sc.warp_to(sc.ut + dt + 30.0)
        else:
            sc.warp_to(sc.ut + 2_000_000.0)
        time.sleep(0.5)
    log(f"  now in body: {v.orbit.body.name}")
    if v.orbit.body.name != "Duna":
        log("  did NOT reach Duna SOI — a mid-course correction node is needed (interplanetary aim is "
            "sensitive). Recording and stopping for a live correction.")
        rec.append({"phase": "duna_no_encounter", "body": v.orbit.body.name})
        conn.close()
        return 2

    # 5) Capture at Duna. Warp to periapsis, then a pure-retrograde burn lowers apoapsis into the SOI;
    # a low periapsis aerobrakes in Duna's atmosphere (depth ~50 km) for free.
    ttp = v.orbit.time_to_periapsis
    if ttp and 0 < ttp < 1e7:
        sc.warp_to(sc.ut + ttp - 25)
        time.sleep(2)
    refuel()
    _retro_capture_duna(conn, sc, v, log, ap_target_m=900_000.0, pe_floor_m=20_000.0)

    ok = v.orbit.body.name == "Duna" and v.orbit.periapsis_altitude > 10_000.0
    log(f"=== {'DUNA ORBIT ACHIEVED' if ok else 'CAPTURE INCOMPLETE'} ===  "
        f"ap={v.orbit.apoapsis_altitude/1000:.0f}k pe={v.orbit.periapsis_altitude/1000:.0f}k body={v.orbit.body.name}")
    rec.append({"phase": "duna_capture_done", "ap": v.orbit.apoapsis_altitude,
                "pe": v.orbit.periapsis_altitude, "ok": ok})
    conn.close()
    return 0 if ok else 2


def _wait_node_done(bridge, *, timeout_s: float, label: str) -> dict:
    t0 = time.monotonic()
    last = ""
    s: dict = {}
    while time.monotonic() - t0 < timeout_s:
        try:
            s = bridge.mj_status()
        except Exception:
            time.sleep(3)
            continue
        msg = (f"{label}: nodeExec={s.get('nodeExecEnabled')} nodes={s.get('nodeCount')} "
               f"body={s.get('body')}")
        if msg != last:
            log("  " + msg)
            last = msg
        if not s.get("nodeExecEnabled", False) and s.get("nodeCount", 1) == 0:
            return s
        time.sleep(4)
    return s


def _retro_capture_duna(conn, sc, v, log_fn, *, ap_target_m: float, pe_floor_m: float,
                        max_s: float = 240.0) -> None:
    """Pure-retrograde capture burn (the proven robust pattern from mj_to_mun): point retrograde in
    the body's non-rotating frame, tracking the velocity vector each loop, and burn until apoapsis is
    bound within the SOI with a safe periapsis."""
    body = v.orbit.body
    ref = body.non_rotating_reference_frame
    ap = v.auto_pilot
    ap.reference_frame = ref
    v.control.rcs = True
    v.control.remove_nodes()

    def retro():
        vel = v.velocity(ref)
        return (-vel[0], -vel[1], -vel[2])

    ap.target_direction = retro()
    ap.engage()
    time.sleep(8)
    v.control.throttle = 1.0
    t0 = time.monotonic()
    last = ""
    while time.monotonic() - t0 < max_s:
        ap.target_direction = retro()
        o = v.orbit
        A, P = o.apoapsis_altitude, o.periapsis_altitude
        m = f"capture: ap {A/1000:.0f}k pe {P/1000:.0f}k ecc {o.eccentricity:.3f}"
        if m != last:
            log_fn("  " + m)
            last = m
        if 0 < A < ap_target_m and P > pe_floor_m:
            log_fn("  CAPTURED (bound within Duna SOI, safe periapsis)")
            break
        if 0 < A and P < pe_floor_m * 0.6:
            log_fn("  periapsis low; stopping (will aerobrake)")
            break
        v.control.throttle = 1.0 if (A < 0 or A > ap_target_m * 1.5) else 0.4
        time.sleep(1.5)
    v.control.throttle = 0.0
    try:
        ap.disengage()
    except Exception:
        pass


def _run_dir(cfg) -> Path:
    p = Path(cfg.get("paths", {}).get("run_dir", "runs"))
    p.mkdir(parents=True, exist_ok=True)
    return p


if __name__ == "__main__":
    raise SystemExit(main())
