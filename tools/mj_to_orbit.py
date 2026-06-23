"""Launch an Artemis vehicle and fly it to a parking orbit with MechJeb's ASCENT autopilot.

The agent only picks the craft + target orbit; MechJeb flies the gravity turn, staging, and
circularization. This is the "delegate to MechJeb" ascent that replaces the hand-rolled profile.

    PYTHONPATH=src python tools/mj_to_orbit.py configs/local-ksp.yaml <hls|orion> [altitude_km] [name]
"""
from __future__ import annotations

import sys
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from ksp_lab.artemis import build_artemis_architecture
from ksp_lab.bridge_client import BridgeClient
from ksp_lab.mission import MissionPlanner
from ksp_lab.parts import estimate_design
from ksp_lab.runner import AutomationRunner


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    vehicle = sys.argv[2] if len(sys.argv) > 2 else "hls"
    alt_km = float(sys.argv[3]) if len(sys.argv) > 3 else 90.0
    runner = AutomationRunner(config_path, offline=False)
    mission = MissionPlanner().interpret("Artemis Mun SLS Orion Starship HLS relay science return")

    suffix = uuid4().hex[:8]
    if vehicle == "duna_comsat":
        from ksp_lab.duna import build_duna_comsat
        design = build_duna_comsat()
    elif vehicle == "route_depot":
        from ksp_lab.duna import build_route_depot
        design = build_route_depot()
    else:
        design = deepcopy(build_artemis_architecture(mission).vehicle(vehicle).design)
    name = sys.argv[4] if len(sys.argv) > 4 else f"AI-{vehicle.upper()}-MJ-{suffix}"
    design.name = name
    design.estimates = estimate_design(design)
    runner.writer.write(design, runner._craft_dir(), template_path=None)

    bridge = BridgeClient(**runner.config["bridge"])
    log(f"launching {name} ({vehicle}) -> MechJeb ascent to {alt_km:.0f} km ...")
    runner._load_and_launch(bridge, name)
    time.sleep(4)  # let the pad scene settle + MechJebCore start

    # Hand the ascent to MechJeb.
    try:
        r = bridge.mj_ascent(altitude=alt_km * 1000.0, inclination=0.0)
        log(f"  mj-ascent: {r}")
    except Exception as exc:
        log(f"  mj-ascent rejected: {exc}")
        return 2

    # MechJeb's ascent AP does NOT auto-ignite the first stage from PRELAUNCH — kick it (throttle
    # up + activate the first stage); MechJeb then flies the gravity turn and autostages from there.
    try:
        import krpc
        kc = krpc.connect(name="ascent-kick", address=runner.config["krpc"]["host"],
                          rpc_port=runner.config["krpc"]["rpc_port"],
                          stream_port=runner.config["krpc"]["stream_port"])
        kv = kc.space_center.active_vessel
        if str(kv.situation) == "VesselSituation.pre_launch" and kv.thrust < 1.0:
            kv.control.throttle = 1.0
            kv.control.activate_next_stage()
            log("  kicked first stage to ignite the launch")
        kc.close()
    except Exception as exc:
        log(f"  launch-kick skipped: {exc}")

    # Poll until MechJeb finishes the ascent (autopilot disables itself) AND we have a stable orbit.
    t0 = time.monotonic()
    last = ""
    ok = False
    while time.monotonic() - t0 < 900.0:
        try:
            s = bridge.mj_status()
        except Exception as exc:
            log(f"  status error: {exc}")
            time.sleep(4)
            continue
        ap_km = round(s.get("apoapsis", 0) / 1000)
        pe_km = round(s.get("periapsis", 0) / 1000)
        msg = f"ascentEnabled={s.get('ascentEnabled')} ap={ap_km}k pe={pe_km}k sit={s.get('situation')} body={s.get('body')}"
        if msg != last:
            log("  " + msg)
            last = msg
        # Done = autopilot off and periapsis above the atmosphere (70 km).
        if not s.get("ascentEnabled", False) and s.get("periapsis", 0) > 70_000 and s.get("body") == "Kerbin":
            ok = True
            break
        time.sleep(5)

    log("=== MECHJEB ASCENT COMPLETE ===" if ok else "=== ASCENT did not confirm orbit ===")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
