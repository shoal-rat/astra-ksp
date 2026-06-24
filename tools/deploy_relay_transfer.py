"""Deploy an RA-100 relay to a CIRCULAR orbit around ANOTHER BODY via a transfer — the Mun today,
Ike/Duna on the same skeleton later.

Composed almost entirely from proven code (per the design workflow):
  - launch_to_lko(..., target_alt_km=100): the hardened ascent to a ~100 km Kerbin PARKING orbit (booster
    force-separated, RTG+Z-1k bus so the coast is EC-survivable). Passing 100 km shrinks the insertion
    stage to a ~250 m/s trim, conserving the upper's ~3600 m/s for the transfer.
  - the Mun transfer + retro-capture from mj_to_mun (TMI grid-search node -> MechJeb executes -> warp to
    the Mun SOI -> warp to periapsis -> pure-retrograde capture). The refuel-before-capture CHEAT is NOT
    used here — the bus flies on its own propellant (it has ~2500 m/s of margin over the ~1100 m/s job).
  - commission(): jettison fairing, deploy RA-100 dish + solar, set vessel type = Relay.

All long warps use sc.warp_to (NOT MechJeb autowarp, which STALLS on far nodes).

    PYTHONPATH=src python tools/deploy_relay_transfer.py configs/local-ksp.yaml Mun 750 AI-Mun-Relay
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import krpc
import yaml

from ksp_lab.bridge_client import BridgeClient
from ksp_lab.runner import AutomationRunner
from ksp_lab.flight_controller import KrpcFlightController
from ksp_lab.telemetry import TelemetryRecorder

import deploy_relay
from mj_to_mun import _retro_capture, _wait_node_done


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _predicted_periapsis_at(v, body_name: str):
    """The predicted periapsis ALTITUDE (m) at the future encounter with body_name, walking the patched
    conics, or None if there is no such encounter yet. Used to catch a sub-surface (impact) closest
    approach BEFORE warping into the SOI."""
    try:
        o = v.orbit
        for _ in range(6):
            nxt = o.next_orbit
            if nxt is None:
                return None
            if nxt.body.name == body_name:
                return nxt.periapsis_altitude
            o = nxt
    except Exception:
        return None
    return None


def _circularize_at_apoapsis(conn, sc, v, max_s: float = 200.0) -> None:
    """Warp to apoapsis, then burn PROGRADE (autopilot in the body's non-rotating frame, tracking the
    velocity vector — a basic probe core can't hold SAS prograde, and MechJeb's node executor stalls on a
    far node) until the orbit is ~circular. The RTG keeps it controllable through any eclipse."""
    o = v.orbit
    ttap = o.time_to_apoapsis
    if ttap and 0 < ttap < 1e7:
        log(f"  warping {ttap:.0f}s to apoapsis to circularise ...")
        sc.warp_to(sc.ut + ttap - 20.0)
        time.sleep(2)
    ref = v.orbit.body.non_rotating_reference_frame
    ap = v.auto_pilot
    ap.reference_frame = ref
    ap.target_direction = v.velocity(ref)
    ap.engage()
    time.sleep(6)
    v.control.throttle = 1.0
    t0 = time.monotonic()
    best = 9.0
    last = ""
    while time.monotonic() - t0 < max_s:
        ap.target_direction = v.velocity(ref)        # keep tracking prograde
        ecc = v.orbit.eccentricity
        pe = v.orbit.periapsis_altitude
        apo = v.orbit.apoapsis_altitude
        m = f"circularise: pe {pe/1000:.0f}k ap {apo/1000:.0f}k ecc {ecc:.3f}"
        if m != last:
            log("  " + m); last = m
        if ecc < 0.02 or pe > apo * 0.97 or ecc > best + 0.004:   # circular, or ecc bottomed out
            break
        best = min(best, ecc)
        time.sleep(0.4)
    v.control.throttle = 0.0
    try:
        ap.disengage()
    except Exception:
        pass
    time.sleep(1)


def _raise_apoapsis(sc, v, target_ap_m: float, max_s: float = 320.0) -> None:
    """Burn PROGRADE to raise the apoapsis to ~target_ap_m, KEEPING the periapsis low. This lets the long
    interplanetary-window wait rails-warp at the high-altitude cap (100,000x near the SOI edge) instead of
    the ~50x cap at 100 km LKO, while the low periapsis still gives an efficient Oberth ejection later."""
    ref = v.orbit.body.non_rotating_reference_frame
    ap = v.auto_pilot
    ap.reference_frame = ref
    ap.target_direction = v.velocity(ref)
    ap.engage()
    time.sleep(5)
    soi_alt = v.orbit.body.sphere_of_influence - v.orbit.body.equatorial_radius
    t0 = time.monotonic()
    last = ""
    while time.monotonic() - t0 < max_s:
        ap.target_direction = v.velocity(ref)
        apo = v.orbit.apoapsis_altitude
        # Stop at the target, if already hyperbolic (apo<0 = escaped), or if nearing the SOI edge.
        if apo < 0 or apo >= target_ap_m or apo > soi_alt * 0.9:
            break
        # FEATHER the throttle as the apoapsis nears the target — it grows EXPONENTIALLY near escape, so a
        # full-throttle burn overshoots past the SOI and the craft escapes (the first attempt did). Slow the
        # final approach so the burn cuts off close to the target.
        frac = apo / target_ap_m
        v.control.throttle = 1.0 if frac < 0.5 else (0.2 if frac < 0.85 else 0.04)
        m = f"raising apoapsis {apo/1000:.0f}k km"
        if m != last:
            log("  " + m); last = m
        time.sleep(0.3)
    v.control.throttle = 0.0
    try:
        ap.disengage()
    except Exception:
        pass
    time.sleep(1)


def transfer_to_mun(conn, sc, bridge, v, name: str, target_alt_km: float) -> bool:
    """Kerbin parking orbit -> Mun: plan the TMI node (grid search, retry for a window), MechJeb executes
    it, warp to the Mun SOI then to periapsis, and CAPTURE with a pure-retrograde burn (no refuel)."""
    ctrl = KrpcFlightController(cfg["krpc"])
    rec = TelemetryRecorder(Path("runs") / f"transfer-{name}.jsonl")
    start = time.monotonic()
    sc.rails_warp_factor = 0
    try:
        v.control.remove_nodes()
    except Exception:
        pass
    node = None
    for attempt in range(4):
        node = ctrl._find_mun_transfer_node(conn, v, rec, start, transfer_profile="capture")
        if node is not None:
            break
        log(f"  no Mun transfer window (attempt {attempt + 1}); warping ahead a fraction of an orbit ...")
        sc.warp_to(sc.ut + max(300.0, v.orbit.period / 6.0))
    if node is None:
        log("  FAILED: no Mun transfer node found"); return False
    log(f"  TMI node: dv~{node.prograde:.0f} m/s at T+{node.ut - sc.ut:.0f}s")
    bridge.mj_execute_node()                          # the TMI node is near -> MechJeb autowarp is fine
    _wait_node_done(bridge, timeout_s=900.0, label="TMI")
    # FINE-TUNE the Mun closest approach BEFORE committing. The grid-search TMI can leave a periapsis that
    # is SUB-SURFACE (an impact — what destroyed the first AI-Mun-Relay-A at -40 km). MechJeb's course
    # correction places an optimal node to raise the closest approach; verify it, and ABORT rather than
    # warp into an impact.
    for attempt in range(3):
        pred = _predicted_periapsis_at(v, "Mun")
        if pred is not None and pred > 25_000.0:
            log(f"  Mun closest-approach periapsis {pred/1000:.0f} km — safe")
            break
        shown = f"{pred/1000:.0f} km" if pred is not None else "no encounter"
        log(f"  Mun closest approach {shown} -> course-correcting (attempt {attempt + 1}) ...")
        try:
            r = bridge.mj_plan(target="Mun", operation="correction")
            log(f"    correction node dv~{r.get('dv', 0):.0f} m/s")
            bridge.mj_execute_node()
            _wait_node_done(bridge, timeout_s=400.0, label="correction")
        except Exception as exc:
            log(f"    correction failed: {exc}")
            break
    pred = _predicted_periapsis_at(v, "Mun")
    if pred is None or pred < 5_000.0:
        log(f"  ABORT: Mun periapsis still unsafe ({round(pred/1000) if pred else pred} km) — not warping into an impact")
        return False
    # coast/warp to the Mun SOI
    if v.orbit.body.name != "Mun":
        try:
            soi_dt = v.orbit.time_to_soi_change
            if soi_dt and 0 < soi_dt < 1e7:
                log(f"  warping {soi_dt:.0f}s to the Mun SOI ...")
                sc.warp_to(sc.ut + soi_dt + 20.0)
        except Exception as exc:
            log(f"  SOI warp note: {exc}")
    time.sleep(3)
    if v.orbit.body.name != "Mun":
        log(f"  did not enter the Mun SOI (still {v.orbit.body.name})"); return False
    # warp to periapsis, then capture there (most efficient; burning at the SOI edge buries periapsis)
    ttp = v.orbit.time_to_periapsis
    if ttp and 0 < ttp < 1e7:
        log(f"  in Mun SOI; warping {ttp:.0f}s to periapsis ({v.orbit.periapsis_altitude/1000:.0f} km) ...")
        sc.warp_to(sc.ut + ttp - 25.0)
        time.sleep(2)
    # CAPTURE — pure retrograde burn, NO refuel. The bound-orbit CEILING must be ABOVE the ENCOUNTER
    # periapsis (which can be well above the requested target — e.g. 2196 km), so the burn STOPS at a
    # near-circular orbit THERE instead of burning past circular and driving the periapsis into the surface
    # (the A2 failure: it over-burned pe 2196k -> -24k). Capturing near the encounter periapsis is fine for
    # a relay — a higher Mun orbit just gives wider coverage.
    enc_pe = v.orbit.periapsis_altitude
    ap_target_m = max(150_000.0, enc_pe * 1.3)
    log(f"  capturing near the encounter periapsis ~{enc_pe/1000:.0f} km (bound ceiling {ap_target_m/1000:.0f} km)")
    _retro_capture(conn, sc, v, log, ap_target_m=ap_target_m, pe_floor_m=20_000.0)
    return v.orbit.body.name == "Mun" and v.orbit.periapsis_altitude > 8_000.0


def _lower_to_lko(conn, sc, bridge, v, target_apo_alt_m: float = 130_000.0) -> None:
    """Drop the high warp-orbit apoapsis back to a low ~circular orbit (keeping the ~100 km periapsis) with a
    PRECISE retrograde maneuver node at periapsis, executed by MechJeb. mj_plan's interplanetary transfer
    expects a low orbit; from a ~6,900 km apoapsis it returns a retrograde node that lowers the orbit instead
    of escaping. A hand-rolled retrograde burn OVERSHOT (drove the periapsis into the atmosphere), so compute
    the exact Δv from vis-viva and let MechJeb's node executor place it precisely. The upper carries ~4,400
    m/s; this ~800 m/s round-trip (raise for warp, lower for ejection) is affordable."""
    body = v.orbit.body
    mu = body.gravitational_parameter
    r_pe = v.orbit.periapsis                                  # radius at periapsis (m)
    a_now = v.orbit.semi_major_axis
    a_target = (r_pe + body.equatorial_radius + target_apo_alt_m) / 2.0
    v_now = (mu * (2.0 / r_pe - 1.0 / a_now)) ** 0.5
    v_target = (mu * (2.0 / r_pe - 1.0 / a_target)) ** 0.5
    dv = v_target - v_now                                     # negative = retrograde, lowers the apoapsis
    try:
        v.control.remove_nodes()
    except Exception:
        pass
    v.control.add_node(sc.ut + v.orbit.time_to_periapsis, prograde=dv)
    try:
        bridge.mj_execute_node()
        _wait_node_done(bridge, timeout_s=600.0, label="lower-to-LKO")
    finally:
        try:
            v.control.remove_nodes()
        except Exception:
            pass
    log(f"  lowered to ~{v.orbit.periapsis_altitude/1000:.0f}x{v.orbit.apoapsis_altitude/1000:.0f} km for a clean ejection")


def transfer_to_duna(conn, sc, bridge, v, name: str, target_alt_km: float) -> bool:
    """Kerbin parking orbit -> Duna: mj_plan interplanetary ejection at the NEXT window, warp to it, eject,
    course-check the closest approach, warp the ~75-day coast to the Duna SOI, then retro-capture ABOVE
    Duna's ~50 km atmosphere. The RTG holds control through the long coast. No refuel."""
    from ksp_lab.bodies import DUNA
    sc.rails_warp_factor = 0
    try:
        v.control.remove_nodes()
    except Exception:
        pass
    # 1) Plan the ejection just to read the WINDOW time, then drop the node (we re-plan after the warp).
    try:
        r = bridge.mj_plan(target="Duna", operation="interplanetary")
    except Exception as exc:
        log(f"  Duna ejection plan FAILED: {exc}"); return False
    node_ut = r.get("ut", sc.ut)
    wait = node_ut - sc.ut
    log(f"  Duna window in {wait:.0f}s (~{wait/(426*21600):.2f} Kerbin-yr); ejection ~{r.get('dv', 0):.0f} m/s")
    try:
        v.control.remove_nodes()
    except Exception:
        pass
    # 2) RAISE the apoapsis near the SOI edge so the long wait rails-warps at the high-altitude cap — the
    # 100 km LKO cap is only 50x, which would make a 1.77-yr wait ~90 real HOURS. Keep the periapsis low so
    # the later ejection is still an efficient Oberth burn (and the raise Δv is recovered there).
    if wait > 6 * 3600:
        from ksp_lab.bodies import MUN, KERBIN
        soi = v.orbit.body.sphere_of_influence
        mun_alt = MUN.orbit_radius_m - KERBIN.radius_m            # the Mun's orbital altitude (~11,400 km)
        # Stay below the Mun's SOI (~2,430 km radius reaches down to ~9,000 km), NOT just its orbit: even an
        # apoapsis of 9,900 km got flung out of Kerbin SOI by a Mun flyby. ~6,900 km clears the Mun's SOI by
        # ~2,000 km and is still high enough for a fast rails-warp cap (the earlier high orbits showed cap 7).
        target_ap = min(soi * 0.55, mun_alt - 4_500_000.0)
        log(f"  raising apoapsis to ~{target_ap/1000:.0f} km (below the Mun) so the {wait/(426*21600):.2f}-yr wait warps fast ...")
        _raise_apoapsis(sc, v, target_ap)
        log(f"  apoapsis {v.orbit.apoapsis_altitude/1000:.0f} km / periapsis {v.orbit.periapsis_altitude/1000:.0f} km")
    # 3) Warp to ~the window (fast now: most of the long orbit sits at the high-warp altitude band).
    sc.warp_to(node_ut - 1800.0)
    time.sleep(2)
    # 3b) Lower the high warp orbit back to ~LKO BEFORE re-planning. mj_plan's OperationInterplanetaryTransfer
    # expects a low circular orbit; from the high warp orbit (6,900 km apoapsis) it returned a RETROGRADE node
    # that LOWERED the orbit instead of escaping (validated: 6906 -> 416 km, no Duna encounter -> abort). The
    # warp strategy is what makes the long wait practical; we just don't eject from the raised orbit. The
    # upper carries ~4,400 m/s so the ~800 m/s round-trip (raise for warp, lower for ejection) is affordable.
    if v.orbit.apoapsis_altitude > 400_000.0:
        log(f"  lowering the {v.orbit.apoapsis_altitude/1000:.0f} km warp orbit back to LKO for a clean ejection ...")
        _lower_to_lko(conn, sc, bridge, v)
    log(f"  reached the window (UT {round(sc.ut)}); re-planning the ejection from the current orbit")
    # 4) Re-plan the ejection from the (now low) orbit and execute.
    try:
        r2 = bridge.mj_plan(target="Duna", operation="interplanetary")
        log(f"  ejection re-planned: dv~{r2.get('dv', 0):.0f} m/s at T+{r2.get('ut', sc.ut) - sc.ut:.0f}s")
    except Exception as exc:
        log(f"  ejection re-plan FAILED: {exc}"); return False
    bridge.mj_execute_node()
    _wait_node_done(bridge, timeout_s=1800.0, label="Duna ejection")
    # 4) Course-check the Duna closest approach (raise it above the atmosphere; abort rather than burn up).
    for attempt in range(3):
        pred = _predicted_periapsis_at(v, "Duna")
        if pred is not None and pred > 80_000.0:
            log(f"  Duna closest-approach periapsis {pred/1000:.0f} km — safe (above the atmosphere)")
            break
        shown = f"{pred/1000:.0f} km" if pred is not None else "no encounter"
        log(f"  Duna closest approach {shown} -> course-correcting (attempt {attempt + 1}) ...")
        try:
            bridge.mj_plan(target="Duna", operation="correction")
            bridge.mj_execute_node()
            _wait_node_done(bridge, timeout_s=600.0, label="correction")
        except Exception as exc:
            log(f"    correction failed: {exc}"); break
    pred = _predicted_periapsis_at(v, "Duna")
    if pred is None or pred < 60_000.0:
        log(f"  ABORT: Duna periapsis unsafe ({round(pred/1000) if pred else pred} km) — not committing to a burn-up")
        return False
    # 5) Warp the coast to the Duna SOI.
    if v.orbit.body.name != "Duna":
        try:
            soi_dt = v.orbit.time_to_soi_change
            if soi_dt and 0 < soi_dt < 1e9:
                log(f"  coasting {soi_dt:.0f}s (~{soi_dt/(6*3600):.0f} Kerbin-days) to the Duna SOI ...")
                sc.warp_to(sc.ut + soi_dt + 30.0)
        except Exception as exc:
            log(f"  SOI warp note: {exc}")
    time.sleep(3)
    if v.orbit.body.name != "Duna":
        log(f"  did not enter the Duna SOI (still {v.orbit.body.name})"); return False
    # 6) Warp to periapsis, then retro-capture ABOVE the atmosphere (pe floor 60 km > Duna's ~50 km top).
    ttp = v.orbit.time_to_periapsis
    if ttp and 0 < ttp < 1e7:
        log(f"  in Duna SOI; warping {ttp:.0f}s to periapsis ({v.orbit.periapsis_altitude/1000:.0f} km) ...")
        sc.warp_to(sc.ut + ttp - 30.0)
        time.sleep(2)
    enc_pe = v.orbit.periapsis_altitude
    ap_target_m = max(80_000.0, enc_pe * 1.3)
    log(f"  capturing near the encounter periapsis ~{enc_pe/1000:.0f} km (bound ceiling {ap_target_m/1000:.0f} km)")
    _retro_capture(conn, sc, v, log, ap_target_m=ap_target_m, pe_floor_m=60_000.0, max_s=400.0)
    return v.orbit.body.name == "Duna" and v.orbit.periapsis_altitude > 55_000.0


def main() -> int:
    global cfg
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    target_body = sys.argv[2] if len(sys.argv) > 2 else "Mun"
    target_alt_km = float(sys.argv[3]) if len(sys.argv) > 3 else 750.0
    name = sys.argv[4] if len(sys.argv) > 4 else "AI-Mun-Relay"
    cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
    bridge = BridgeClient(**cfg["bridge"])
    runner = AutomationRunner(cfg_path, offline=False)
    kc = cfg["krpc"]
    c = krpc.connect(name="deploy-transfer", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center

    # 1) Launch to a 100 km Kerbin PARKING orbit (proven ascent + booster force-separation).
    if not deploy_relay.launch_to_lko(sc, cfg, runner, bridge, name, 100.0):
        log("launch to parking orbit FAILED"); return 2
    time.sleep(3)
    v = sc.active_vessel
    log(f"in parking orbit {round(v.orbit.periapsis_altitude/1000)}x{round(v.orbit.apoapsis_altitude/1000)} km; "
        f"transferring to {target_body} (target {target_alt_km:.0f} km)")

    # 2) Transfer + capture.
    if target_body == "Mun":
        if not transfer_to_mun(c, sc, bridge, v, name, target_alt_km):
            log("Mun transfer/capture FAILED"); return 2
    elif target_body == "Duna":
        if not transfer_to_duna(c, sc, bridge, v, name, target_alt_km):
            log("Duna transfer/capture FAILED"); return 2
    else:
        log(f"target {target_body} not yet wired (Ike to follow on this skeleton)"); return 2

    # 3) Circularise in the target SOI, then commission.
    log(f"  captured {round(v.orbit.periapsis_altitude/1000)}x{round(v.orbit.apoapsis_altitude/1000)} km "
        f"{v.orbit.body.name}; circularising")
    _circularize_at_apoapsis(c, sc, v)
    log(f"  circular {round(v.orbit.periapsis_altitude/1000)}x{round(v.orbit.apoapsis_altitude/1000)} km "
        f"ecc {v.orbit.eccentricity:.3f}")
    deploy_relay.commission(bridge, v)
    log(f"=== {target_body} RELAY {name} DEPLOYED: {round(v.orbit.periapsis_altitude/1000)}x"
        f"{round(v.orbit.apoapsis_altitude/1000)} km {v.orbit.body.name} ===")
    try:
        sc.save("persistent")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
