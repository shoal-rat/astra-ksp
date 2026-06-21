"""Fly a crewed Orion to a low Kerbin parking orbit and stop (a fast, reliably-coplanar place to
test rendezvous + docking + crew transfer between two ships).

    PYTHONPATH=src python tools/fly_to_lko.py configs/local-ksp.yaml <VESSEL_NAME>
"""
from __future__ import annotations

import sys
import time
from copy import deepcopy

from ksp_lab.artemis import build_artemis_architecture
from ksp_lab.bridge_client import BridgeClient
from ksp_lab.flight_controller import KrpcFlightController
from ksp_lab.mission import MissionPlanner
from ksp_lab.parts import estimate_design
from ksp_lab.runner import AutomationRunner


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    name = sys.argv[2] if len(sys.argv) > 2 else "AI-Crew-LKO"
    runner = AutomationRunner(config_path, offline=False)
    mission = MissionPlanner().interpret("crewed craft to 95 km Kerbin orbit")
    trial = runner.run_dir / f"lko-{name}"
    trial.mkdir(parents=True, exist_ok=True)
    tel = trial / "lko.telemetry.jsonl"

    o = deepcopy(build_artemis_architecture(MissionPlanner().interpret("crew")).vehicle("orion").design)
    o.name = name
    o.estimates = estimate_design(o)
    runner.writer.write(o, runner._craft_dir(), template_path=None)
    bridge = BridgeClient(**runner.config["bridge"])
    log(f"launching crewed {name} to ~95 km Kerbin orbit ...")
    runner._load_and_launch(bridge, o.name)
    controller = KrpcFlightController(runner.config["krpc"])
    res = controller.fly(mission, o, tel, timeout_s=int(runner.config["runner"]["flight_timeout_s"]))
    log("=== LKO PARK COMPLETE ===")
    log(f"mission_phase : {res.mission_phase}")
    log(f"vessel        : {name}")
    log(f"orbit         : ap {res.apoapsis_m:.0f} pe {res.periapsis_m:.0f}")
    ok = res.mission_phase == "circularized" and res.periapsis_m > 70_000
    log(f"RESULT        : {'SUCCESS' if ok else res.mission_phase}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
