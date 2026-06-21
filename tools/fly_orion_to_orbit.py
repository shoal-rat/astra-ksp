"""Fly the crewed Orion to a Mun parking orbit and STOP (the rendezvous chaser).

    PYTHONPATH=src python tools/fly_orion_to_orbit.py configs/local-ksp.yaml

Parks at artemis_orion_waiting_in_mun_orbit so a separate docking step can rendezvous with the HLS.
"""
from __future__ import annotations

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
    mission = MissionPlanner().interpret("Artemis Mun SLS Orion crew rendezvous")
    suffix = uuid4().hex[:8]
    trial_dir = runner.run_dir / f"orion-chaser-{suffix}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    tel = trial_dir / "orion_to_orbit.telemetry.jsonl"

    orion = deepcopy(build_artemis_architecture(mission).vehicle("orion").design)
    orion.name = f"AI-Orion-Chaser-{suffix}"
    orion.estimates = estimate_design(orion)
    runner.writer.write(orion, runner._craft_dir(), template_path=None)
    log(f"crewed chaser {orion.name} (dV {orion.estimates['delta_v_mps']}, TWR {orion.estimates['launch_twr']})")

    bridge = BridgeClient(**runner.config["bridge"])
    log("loading + launching ...")
    runner._load_and_launch(bridge, orion.name)
    log("in FLIGHT; flying to Mun parking orbit ...")

    controller = KrpcFlightController(runner.config["krpc"])
    timeout_s = int(runner.config["runner"]["flight_timeout_s"])
    res = controller.fly(
        artemis_phase_mission(mission, "artemis_orion_mun_orbit_only", "orion chaser mun orbit"),
        orion, tel, timeout_s=timeout_s,
    )
    log("=== ORION CHASER PARKED ===")
    log(f"mission_phase : {res.mission_phase}")
    log(f"vessel        : {orion.name}")
    log(f"orbit         : ap {res.apoapsis_m:.0f} pe {res.periapsis_m:.0f}")
    ok = res.mission_phase == "artemis_orion_waiting_in_mun_orbit"
    log(f"RESULT        : {'SUCCESS' if ok else 'FAILED'}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
