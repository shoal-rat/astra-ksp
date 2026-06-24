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
    else:
        log(f"target {target_body} not yet wired (interplanetary Duna/Ike to follow on this skeleton)"); return 2

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
