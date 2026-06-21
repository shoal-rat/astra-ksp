"""Automated docking + crew transfer between two docking-port-equipped craft already in orbit.

    PYTHONPATH=src python tools/fly_dock.py configs/local-ksp.yaml <CHASER_NAME> <TARGET_NAME>

The chaser (e.g. the Orion) rendezvous-closes on, then docks with, the target (e.g. the parked HLS);
docking merges the vessels (the crew transfer), then the chaser undocks. Both craft must have been
built with docking_port=True (Clamp-O-Tron + RCS). Success phase: dock_and_transfer_complete.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from ksp_lab.config import load_config
from ksp_lab.flight_controller import KrpcFlightController


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    if len(sys.argv) < 4:
        log("usage: fly_dock.py <config> <CHASER_NAME> <TARGET_NAME>")
        return 2
    chaser, target = sys.argv[2], sys.argv[3]
    cfg = load_config(Path(config_path).resolve())
    run_dir = Path(cfg["paths"]["run_dir"])
    if not run_dir.is_absolute():
        run_dir = Path(config_path).resolve().parent.parent / run_dir
    trial_dir = run_dir / f"dock-{uuid4().hex[:8]}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    telemetry_path = trial_dir / "dock.telemetry.jsonl"

    log(f"docking chaser={chaser} -> target={target}")
    controller = KrpcFlightController(cfg["krpc"])
    timeout_s = int(cfg["runner"]["flight_timeout_s"])
    summary = controller.run_dock_and_transfer(chaser, target, telemetry_path, timeout_s=timeout_s)
    log("=== DOCK + TRANSFER COMPLETE ===")
    log(f"mission_phase : {summary.mission_phase}")
    success = summary.mission_phase == "dock_and_transfer_complete"
    log(f"RESULT        : {'SUCCESS' if success else 'FAILED'}")
    (trial_dir / "summary.json").write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
