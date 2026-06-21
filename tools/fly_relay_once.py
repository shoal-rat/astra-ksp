"""Fly the Artemis Mun relay phase once, live, and report the outcome.

Single-phase driver: builds the relay design, writes the probe-core relay craft via the
fixed runner path, loads + launches through the bridge, then flies the live
`artemis_mun_relay` profile (ascent -> Kerbin parking orbit -> TMI -> Mun capture ->
relay-orbit shaping). Telemetry is streamed to runs/<trial>/relay_predeploy.telemetry.jsonl.

Usage:
    PYTHONPATH=src python tools/fly_relay_once.py
"""
from __future__ import annotations

import json
import sys
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from ksp_lab.artemis import artemis_phase_mission, build_artemis_architecture
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
    trial_id = f"relay-only-{suffix}"
    trial_dir = runner.run_dir / trial_id
    trial_dir.mkdir(parents=True, exist_ok=True)
    telemetry_path = trial_dir / "relay_predeploy.telemetry.jsonl"

    relay = deepcopy(build_artemis_architecture(mission).vehicle("relay").design)
    relay.name = f"AI-Mun-Relay-{suffix}"
    relay.estimates = estimate_design(relay)

    craft_dir = runner._craft_dir()
    craft_path = runner._write_artemis_relay_craft(relay, craft_dir)
    log(f"trial {trial_id}")
    log(f"wrote relay craft: {craft_path.name} (est dV {relay.estimates.get('delta_v_mps')} m/s, "
        f"TWR {relay.estimates.get('launch_twr')})")

    from ksp_lab.bridge_client import BridgeClient

    bridge = BridgeClient(**runner.config["bridge"])
    log("loading + launching via bridge ...")
    runner._load_and_launch(bridge, relay.name)
    log("in FLIGHT; starting live relay profile ...")

    controller = KrpcFlightController(runner.config["krpc"])
    phase_mission = artemis_phase_mission(mission, "artemis_mun_relay", "mun relay")
    timeout_s = int(runner.config["runner"]["flight_timeout_s"])
    summary = controller.fly(phase_mission, relay, telemetry_path, timeout_s=timeout_s)

    log("=== RELAY PHASE COMPLETE ===")
    log(f"mission_phase   : {summary.mission_phase}")
    log(f"apoapsis_m      : {summary.apoapsis_m:.0f}")
    log(f"periapsis_m     : {summary.periapsis_m:.0f}")
    log(f"fuel_left       : {summary.fuel_fraction_left:.3f}")
    log(f"relay_deployed  : {summary.extra.get('relay_deployed')}")
    success = summary.mission_phase == "artemis_mun_relay_deployed" or bool(
        summary.extra.get("relay_deployed")
    )
    log(f"RESULT          : {'SUCCESS' if success else 'FAILED'}")
    (trial_dir / "summary.json").write_text(
        json.dumps(summary.to_dict(), indent=2), encoding="utf-8"
    )
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
