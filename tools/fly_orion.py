"""Fly the Artemis Orion crew-vehicle arc live: launch -> Mun orbit (rendezvous-equivalent with the
parked HLS) -> trans-Kerbin return -> reentry + recovery.

Phase 1: controller.fly(artemis_orion_mun_orbit_only) -> artemis_orion_waiting_in_mun_orbit.
Phase 2: controller.run_orion_return(name)            -> recovered.

Usage:
    PYTHONPATH=src python tools/fly_orion.py configs/local-ksp.yaml
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
    trial_dir = runner.run_dir / f"orion-{suffix}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    mun_tel = trial_dir / "orion_mun_orbit.telemetry.jsonl"
    ret_tel = trial_dir / "orion_return.telemetry.jsonl"

    orion = deepcopy(build_artemis_architecture(mission).vehicle("orion").design)
    orion.name = f"AI-Orion-{suffix}"
    orion.estimates = estimate_design(orion)

    craft_path = runner.writer.write(orion, runner._craft_dir(), template_path=None)
    log(f"wrote Orion craft (render): {craft_path.name} (dV {orion.estimates['delta_v_mps']}, TWR {orion.estimates['launch_twr']})")

    bridge = BridgeClient(**runner.config["bridge"])
    log("loading + launching via bridge ...")
    runner._load_and_launch(bridge, orion.name)
    log("in FLIGHT; flying Orion to Mun orbit ...")

    controller = KrpcFlightController(runner.config["krpc"])
    timeout_s = int(runner.config["runner"]["flight_timeout_s"])
    mun = controller.fly(
        artemis_phase_mission(mission, "artemis_orion_mun_orbit_only", "orion mun orbit"),
        orion,
        mun_tel,
        timeout_s=timeout_s,
    )
    log(f"Orion Mun-orbit phase: {mun.mission_phase} (ap {mun.apoapsis_m:.0f} pe {mun.periapsis_m:.0f} fuel {mun.fuel_fraction_left:.3f})")
    if mun.mission_phase != "artemis_orion_waiting_in_mun_orbit":
        log("RESULT: FAILED (did not reach Mun orbit)")
        return 2

    log("Orion parked in Mun orbit; running rendezvous-equivalent + Kerbin return ...")
    ret = controller.run_orion_return(orion.name, ret_tel, timeout_s=timeout_s)
    log("=== ORION RETURN COMPLETE ===")
    log(f"mission_phase : {ret.mission_phase}")
    log(f"recovered     : {ret.recovered}")
    success = ret.mission_phase == "recovered" or ret.recovered
    log(f"RESULT        : {'SUCCESS' if success else 'FAILED'}")
    (trial_dir / "summary.json").write_text(
        json.dumps({"mun": mun.to_dict(), "return": ret.to_dict()}, indent=2), encoding="utf-8"
    )
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
