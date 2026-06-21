"""Fly the Artemis HLS predeploy phase once, live: launch the Starship/HLS analogue and park it in
a low Mun orbit (success phase: artemis_hls_parked_in_mun_orbit).

Usage:
    PYTHONPATH=src python tools/fly_hls_predeploy.py configs/local-ksp.yaml
"""
from __future__ import annotations

import json
import sys
import time
from copy import deepcopy
from uuid import uuid4

from ksp_lab.artemis import artemis_phase_mission, build_artemis_architecture
from ksp_lab.bridge_client import BridgeClient
from ksp_lab.flight_controller import KrpcFlightController
from ksp_lab.mission import MissionPlanner
from ksp_lab.parts import estimate_design
from ksp_lab.runner import AutomationRunner


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    runner = AutomationRunner(config_path, offline=False)
    mission = MissionPlanner().interpret(
        "Artemis Mun SLS Orion Starship HLS relay science return"
    )
    suffix = uuid4().hex[:8]
    trial_id = f"hls-predeploy-{suffix}"
    trial_dir = runner.run_dir / trial_id
    trial_dir.mkdir(parents=True, exist_ok=True)
    telemetry_path = trial_dir / "hls_predeploy.telemetry.jsonl"

    hls = deepcopy(build_artemis_architecture(mission).vehicle("hls").design)
    hls.name = f"AI-HLS-Starship-{suffix}"
    hls.estimates = estimate_design(hls)

    craft_dir = runner._craft_dir()
    # Use render() to build a CONTROLLABLE crewed lander (clean staging, reaction wheels, landing
    # legs, nose cone) instead of the 901 t MUNSHIP, whose custom Starship staging the descent
    # controller cannot fly (no active descent engine -> crash). See AEROSPACE_AGENT_LOG.md.
    craft_path = runner.writer.write(hls, craft_dir, template_path=None)
    log(f"trial {trial_id}")
    log(f"wrote HLS craft (render lander): {craft_path.name}")

    bridge = BridgeClient(**runner.config["bridge"])
    log("loading + launching via bridge ...")
    runner._load_and_launch(bridge, hls.name)
    log("in FLIGHT; starting live HLS predeploy profile ...")

    controller = KrpcFlightController(runner.config["krpc"])
    phase_mission = artemis_phase_mission(mission, "artemis_hls_predeploy", "hls predeploy")
    timeout_s = int(runner.config["runner"]["flight_timeout_s"])
    summary = controller.fly(phase_mission, hls, telemetry_path, timeout_s=timeout_s)

    log("=== HLS PREDEPLOY COMPLETE ===")
    log(f"mission_phase : {summary.mission_phase}")
    log(f"apoapsis_m    : {summary.apoapsis_m:.0f}")
    log(f"periapsis_m   : {summary.periapsis_m:.0f}")
    log(f"fuel_left     : {summary.fuel_fraction_left:.3f}")
    success = summary.mission_phase == "artemis_hls_parked_in_mun_orbit"
    log(f"RESULT        : {'SUCCESS' if success else 'FAILED'}")
    (trial_dir / "summary.json").write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
