"""DEPRECATED — the hardcoded Mun PLAN has been removed.

This script used to carry a hand-built 7-step Mun mission (``PLAN = [...]``) — a "preset Moon script",
exactly the rigid per-mission logic the owner asked to ELIMINATE. Missions are now decomposed
AUTONOMOUSLY by the agent's general, body-agnostic planner (``ksp_lab.astra.planner.decompose`` +
``mission_graph``); the mission-aware launch sizing this file used to compute lives in
``planner._apply_mission_aware_launch``. Drive ANY mission — Mun, Duna/Mars, Eve relay — through the agent:

    PYTHONPATH=src python tools/astra.py "land a crew on the Mun, plant a flag, and return"
    PYTHONPATH=src python tools/astra.py "land a crew on Mars, plant a flag, and bring them home"
    PYTHONPATH=src python tools/astra.py "<goal>" --from-step N    # resume a leg against the live vessel

This thin shim simply forwards the Mun round-trip command to the agent so existing call sites keep working.
"""
from __future__ import annotations

import sys

from ksp_lab.astra import AstraAgent
from ksp_lab.astra.interpreter import Interpreter


def _parse_args(argv: list[str]) -> tuple[str, int]:
    cfg_path = "configs/local-ksp.yaml"
    from_step = 1
    rest = argv[1:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--from-step" and i + 1 < len(rest):
            from_step = int(rest[i + 1]); i += 2; continue
        if a.startswith("--from-step="):
            from_step = int(a.split("=", 1)[1]); i += 1; continue
        if not a.startswith("--"):
            cfg_path = a
        i += 1
    return cfg_path, max(1, from_step)


def main() -> int:
    cfg_path, from_step = _parse_args(sys.argv)
    print("[DEPRECATED] tools/fly_mun_roundtrip.py now delegates to the AUTONOMOUS agent — the hardcoded "
          "Mun PLAN is gone. Equivalent: tools/astra.py \"land a crew on the Mun, plant a flag, and return\".",
          flush=True)
    agent = AstraAgent(cfg_path, interpreter=Interpreter(), max_attempts=1)
    result = agent.run("land a crew on the Mun, plant a flag, and return", from_step=from_step)
    print("\n" + result.summary_text())
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
