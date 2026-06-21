"""Fly the Artemis HLS surface sortie once, live: from a parked Mun orbit, descend + land + do
surface science + ascend back to Mun orbit (success phase: artemis_hls_returned_to_mun_orbit).

Usage:
    PYTHONPATH=src python tools/fly_hls_sortie.py configs/local-ksp.yaml [VESSEL_NAME]

If VESSEL_NAME is omitted, picks the AI-HLS-Starship vessel in the LOWEST Mun orbit (freshly parked).
"""
from __future__ import annotations

import json
import sys
import time
from uuid import uuid4

import krpc

from ksp_lab.config import load_config
from ksp_lab.flight_controller import KrpcFlightController
from pathlib import Path


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_parked_hls(krpc_cfg) -> str | None:
    conn = krpc.connect(
        name="hls-find",
        address=krpc_cfg.get("host", "127.0.0.1"),
        rpc_port=int(krpc_cfg.get("rpc_port", 50000)),
        stream_port=int(krpc_cfg.get("stream_port", 50001)),
    )
    best = None
    best_pe = 1e12
    for v in conn.space_center.vessels:
        try:
            if v.name.startswith("AI-HLS-Starship") and v.orbit.body.name == "Mun":
                pe = float(v.orbit.periapsis_altitude)
                if 0 < pe < best_pe:
                    best_pe = pe
                    best = v.name
        except Exception:
            continue
    conn.close()
    return best


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    cfg = load_config(Path(config_path).resolve())
    vessel_name = sys.argv[2] if len(sys.argv) > 2 else find_parked_hls(cfg["krpc"])
    if not vessel_name:
        log("no AI-HLS-Starship vessel found in Mun orbit")
        return 2
    run_dir = Path(cfg["paths"]["run_dir"])
    if not run_dir.is_absolute():
        run_dir = Path(config_path).resolve().parent.parent / run_dir
    trial_dir = run_dir / f"hls-sortie-{uuid4().hex[:8]}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    telemetry_path = trial_dir / "hls_surface_sortie.telemetry.jsonl"

    log(f"HLS surface sortie on vessel: {vessel_name}")
    controller = KrpcFlightController(cfg["krpc"])
    timeout_s = int(cfg["runner"]["flight_timeout_s"])
    summary = controller.run_hls_surface_sortie(vessel_name, telemetry_path, timeout_s=timeout_s)

    log("=== HLS SURFACE SORTIE COMPLETE ===")
    log(f"mission_phase : {summary.mission_phase}")
    log(f"landed        : {summary.landed}")
    log(f"fuel_left     : {summary.fuel_fraction_left:.3f}")
    success = summary.mission_phase == "artemis_hls_returned_to_mun_orbit"
    log(f"RESULT        : {'SUCCESS' if success else 'FAILED'}")
    (trial_dir / "summary.json").write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
