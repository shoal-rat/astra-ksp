"""Transfer a vessel from low Kerbin orbit to a low Mun orbit, delegating the burns to MechJeb.

kRPC plans the trans-Munar node (proven grid search) and the Mun circularization node; MechJeb's
node executor flies both burns precisely (auto-warps to each). The agent only sequences the phases.

    PYTHONPATH=src python tools/mj_to_mun.py configs/local-ksp.yaml <vessel-name>
"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

from ksp_lab.bridge_client import BridgeClient
from ksp_lab.config import load_config
from ksp_lab.flight_controller import KrpcFlightController
from ksp_lab.telemetry import TelemetryRecorder


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _wait_node_done(bridge, *, timeout_s: float, label: str) -> dict:
    t0 = time.monotonic()
    last = ""
    s = {}
    while time.monotonic() - t0 < timeout_s:
        try:
            s = bridge.mj_status()
        except Exception:
            time.sleep(3)
            continue
        msg = (f"{label}: nodeExec={s.get('nodeExecEnabled')} nodes={s.get('nodeCount')} "
               f"ap={round(s.get('apoapsis',0)/1000)}k pe={round(s.get('periapsis',0)/1000)}k body={s.get('body')}")
        if msg != last:
            log("  " + msg)
            last = msg
        if not s.get("nodeExecEnabled", False) and s.get("nodeCount", 1) == 0:
            return s
        time.sleep(4)
    return s


def _retro_capture(conn, sc, v, log, *, ap_target_m: float = 1_200_000.0, pe_floor_m: float = 30_000.0,
                   max_s: float = 220.0) -> None:
    """Burn retrograde (autopilot, non-rotating frame, tracking the velocity vector) until the orbit
    is bound within the Mun SOI with a safe periapsis. Robust where the SAS hold mode + MechJeb node
    executor both fail. Lowers apoapsis monotonically while preserving periapsis."""
    import time as _t
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
    _t.sleep(8)
    v.control.throttle = 1.0
    t0 = _t.monotonic()
    last = ""
    while _t.monotonic() - t0 < max_s:
        ap.target_direction = retro()
        o = v.orbit
        A, P = o.apoapsis_altitude, o.periapsis_altitude
        m = f"capture: ap {A/1000:.0f}k pe {P/1000:.0f}k ecc {o.eccentricity:.3f}"
        if m != last:
            log("  " + m)
            last = m
        if 0 < A < ap_target_m and P > pe_floor_m:
            log("  CAPTURED (bound within SOI, safe periapsis)")
            break
        if 0 < A and P < pe_floor_m * 0.8:
            log("  periapsis low; stopping")
            break
        v.control.throttle = 1.0 if (A < 0 or A > ap_target_m * 1.5) else 0.4
        _t.sleep(1.5)
    v.control.throttle = 0.0
    _t.sleep(1)
    try:
        ap.disengage()
    except Exception:
        pass


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    name = sys.argv[2] if len(sys.argv) > 2 else "AI-HLS-Artemis"
    cfg = load_config(Path(config_path).resolve())
    ctrl = KrpcFlightController(cfg["krpc"])
    bridge = BridgeClient(**cfg["bridge"])
    conn = ctrl._connect("mjmun")
    sc = conn.space_center
    rec = TelemetryRecorder(ctrl_run_dir(cfg) / f"mjmun-{name}.jsonl")
    start = time.monotonic()

    v = ctrl._select_vessel(conn, name)
    sc.active_vessel = v
    sc.rails_warp_factor = 0
    try:
        v.control.remove_nodes()
    except Exception:
        pass
    log(f"{name}: ap={v.orbit.apoapsis_altitude/1000:.0f}k pe={v.orbit.periapsis_altitude/1000:.0f}k body={v.orbit.body.name}")

    # 1) Plan the trans-Munar node (kRPC grid search), retrying a few times for a window.
    node = None
    for attempt in range(4):
        node = ctrl._find_mun_transfer_node(conn, v, rec, start, transfer_profile="capture")
        if node is not None:
            break
        log(f"  no transfer window (attempt {attempt+1}); waiting a bit and retrying ...")
        sc.warp_to(sc.ut + max(300.0, v.orbit.period / 6.0))
    if node is None:
        log("  FAILED: no Mun transfer node found.")
        return 2
    log(f"  TMI node: dv~{node.prograde:.0f} m/s at T+{node.ut - sc.ut:.0f}s")

    # 2) MechJeb executes the TMI burn (auto-warps to the node).
    bridge.mj_execute_node()
    _wait_node_done(bridge, timeout_s=900.0, label="TMI")

    # 3) Coast/warp to the Mun SOI change.
    try:
        if v.orbit.body.name != "Mun":
            soi_dt = v.orbit.time_to_soi_change
            if soi_dt and soi_dt > 0 and soi_dt < 1e7:
                log(f"  warping {soi_dt:.0f}s to Mun SOI ...")
                sc.warp_to(sc.ut + soi_dt + 20.0)
    except Exception as exc:
        log(f"  SOI warp note: {exc}")
    time.sleep(3)
    log(f"  now in body: {v.orbit.body.name}")
    if v.orbit.body.name != "Mun":
        log("  did not capture into Mun SOI.")
        return 2

    # Warp to PERIAPSIS before the capture burn. Burning retrograde at the closest/fastest point
    # preserves periapsis and is the most efficient; burning at the SOI edge (far from periapsis)
    # instead drives the periapsis below the surface.
    o = v.orbit
    ttp = o.time_to_periapsis
    if ttp and 0 < ttp < 1e7:
        log(f"  warping {ttp:.0f}s to Mun periapsis ({o.periapsis_altitude/1000:.0f} km alt) ...")
        sc.warp_to(sc.ut + ttp - 25)
        time.sleep(2)

    # 4) CAPTURE via a pure-retrograde burn (proven robust). The MechJeb node executor won't warp to
    # a distant capture node while it steers, and a basic probe core can't hold SAS retrograde, so we
    # point with the autopilot in the body's non-rotating frame, tracking the velocity vector, and
    # burn retrograde — that lowers apoapsis below the SOI while preserving periapsis. Refuel first so
    # the engine's connected tank is not starved (a multi-stage render craft can flame out with fuel
    # still aboard).
    try:
        bridge.refuel_vessel(name, fraction=1.0, resources="LiquidFuel,Oxidizer,MonoPropellant")
        log("  refuelled before capture")
    except Exception as exc:
        log(f"  refuel skipped: {exc}")
    _retro_capture(conn, sc, v, log)

    pe_km = v.orbit.periapsis_altitude / 1000
    ap_km = v.orbit.apoapsis_altitude / 1000
    ok = v.orbit.body.name == "Mun" and v.orbit.periapsis_altitude > 8000.0
    log(f"=== {'MUN ORBIT ACHIEVED' if ok else 'CAPTURE INCOMPLETE'} ===  ap={ap_km:.0f}k pe={pe_km:.0f}k body={v.orbit.body.name}")
    conn.close()
    return 0 if ok else 2


def ctrl_run_dir(cfg):
    p = Path(cfg.get("paths", {}).get("run_dir", "runs"))
    p.mkdir(parents=True, exist_ok=True)
    return p


if __name__ == "__main__":
    raise SystemExit(main())
