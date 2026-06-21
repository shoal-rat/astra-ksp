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

    # 4) Plan the Mun circularization node at periapsis (kRPC), MechJeb executes it.
    o = v.orbit
    mun = sc.bodies["Mun"]
    r_pe = o.periapsis  # radius from Mun centre
    if r_pe < mun.equatorial_radius + 8000.0:
        # periapsis too low -> would impact; raise the burn target to a safe 20 km orbit radius.
        r_pe = mun.equatorial_radius + 20000.0
    mu = mun.gravitational_parameter
    a = o.semi_major_axis
    v_pe = math.sqrt(max(0.0, mu * (2.0 / o.periapsis - 1.0 / a)))
    v_circ = math.sqrt(mu / o.periapsis)
    dv = v_circ - v_pe  # negative => retrograde, circularizes a hyperbolic/elliptical capture
    ut_pe = sc.ut + o.time_to_periapsis
    v.control.remove_nodes()
    v.control.add_node(ut_pe, prograde=dv, radial=0.0)
    log(f"  Mun capture: dv~{dv:.0f} m/s at periapsis ({o.periapsis_altitude/1000:.0f} km alt) in {o.time_to_periapsis:.0f}s")
    bridge.mj_execute_node()
    s = _wait_node_done(bridge, timeout_s=900.0, label="capture")

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
