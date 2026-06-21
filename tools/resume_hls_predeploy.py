from __future__ import annotations

import argparse
import json
from pathlib import Path

from ksp_lab.artemis import artemis_phase_mission, build_artemis_architecture
from ksp_lab.config import load_config
from ksp_lab.flight_controller import KrpcFlightController
from ksp_lab.mission import MissionPlanner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/local-ksp.yaml")
    parser.add_argument("--telemetry", required=True)
    parser.add_argument("--timeout-s", type=int, default=2400)
    args = parser.parse_args()

    cfg = load_config(args.config)
    mission = MissionPlanner().interpret("Artemis_SLS_Orion_Starship_HLS_Mun_landing_return")
    phase = artemis_phase_mission(mission, "artemis_hls_predeploy", "hls predeploy resume")
    design = build_artemis_architecture(mission).vehicle("hls").design
    telemetry_path = Path(args.telemetry)
    telemetry_path.parent.mkdir(parents=True, exist_ok=True)

    summary = KrpcFlightController(cfg["krpc"]).fly(
        phase,
        design,
        telemetry_path,
        timeout_s=args.timeout_s,
    )
    print(json.dumps(summary.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
