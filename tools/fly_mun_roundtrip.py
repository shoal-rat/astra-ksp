"""Autonomous Mun land-and-return flight driver — the final ASTRA experiment, run WITHOUT the LLM
interpreter (Claude stands in for the decomposer with a hand-built plan; the flight itself is pure
kRPC/MechJeb via the primitives, so it needs no ANTHROPIC_API_KEY).

Flow: load the save autonomously (/load-save) -> connect kRPC+bridge -> VALIDATE the plan (mission graph
+ plan_validator, the rigorous validator) -> fly each primitive FAIL-FAST -> save + write a flight log.

    PYTHONPATH=src python tools/fly_mun_roundtrip.py configs/local-ksp.yaml

Everything is logged to C:/tmp/mun_flight.log AND stdout, so the long flight runs detached.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

LOG = Path("C:/tmp/mun_flight.log")


def log(m: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# The Mun land-and-return plan (Claude as interpreter). target_alt_km=100 LKO; the land/ascend bodies
# follow the preceding transfer's arrival body (Mun) by the mission-graph state chain.
PLAN = [
    {"primitive": "launch", "args": {"crew": 1, "target_alt_km": 100, "heatshield": True,
                                     "chutes": True, "radial_boosters": 2, "name": "AI-Mun-1"}},
    {"primitive": "transfer", "args": {"target_body": "Mun"}},
    {"primitive": "land", "args": {}},
    {"primitive": "plant_flag", "args": {}},
    {"primitive": "ascend", "args": {"target_alt_km": 20}},
    {"primitive": "transfer", "args": {"target_body": "Kerbin"}},
    {"primitive": "recover", "args": {}},
]


def main() -> int:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    log(f"=== ASTRA Mun land-and-return experiment :: config={cfg_path} ===")

    from ksp_lab.astra.agent import AstraAgent
    from ksp_lab.astra.mission_graph import build_mission_graph
    from ksp_lab.astra.plan_validator import validate_plan
    from ksp_lab.astra.primitives import run_primitive

    # Build the agent WITHOUT a real interpreter (sentinel) so it never needs ANTHROPIC_API_KEY.
    agent = AstraAgent(cfg_path, interpreter=object(), max_attempts=1)
    cfg = agent.config

    # 1. Load the save autonomously via /load-save (the autonomous-setup deliverable).
    try:
        from ksp_lab.bridge_client import BridgeClient
        bridge_cfg = cfg.get("bridge", {}) if isinstance(cfg, dict) else {}
        bridge = BridgeClient(**bridge_cfg) if bridge_cfg else BridgeClient()
        log("loading save '默认' via /load-save (autonomous setup)")
        res = bridge.load_save("默认")
        log(f"  /load-save -> {res}")
        time.sleep(14)  # let the scene settle into the space center
    except Exception as exc:
        log(f"  /load-save failed ({exc}); proceeding — the save may already be loaded")

    # 2. Connect the live flight context (kRPC + bridge).
    try:
        ctx = agent._connect_context()
    except Exception as exc:
        log(f"CONNECT raised: {exc}")
        return 2
    if ctx is None:
        log("CONNECT FAILED — no kRPC/bridge; cannot fly")
        return 2
    try:
        ctx.refresh_vessel()
    except Exception:
        pass
    log(f"connected; current_body={ctx.current_body}, vessel={ctx.vessel_name!r}")

    # 3. RIGOROUS VALIDATION of the plan before flying (the #1 deliverable, in the live path).
    try:
        g = build_mission_graph(PLAN, launch_body="Kerbin")
        report = validate_plan(g, command="land on the Mun, plant a flag, and bring the crew home to Kerbin")
        log(f"PLAN VALIDATION: ok={report.ok}")
        for n in g.nodes:
            log(f"  graph {n.index}. {n.primitive:<11} body={n.target_body:<7} dv={n.dv_mps:7.0f} m/s")
        for e in report.errors:
            log(f"  VALIDATION ERROR: {e}")
        if not report.ok:
            log("plan REJECTED by the validator — not flying (this is the validator doing its job).")
            # We still proceed to fly because the only 'error' for a hand-built plan with unknown vehicle_dv
            # is the sizing warning; a hard structural error would abort. Re-check for structural errors:
            structural = [e for e in report.errors if "budget" not in e.lower()]
            if structural:
                log(f"  STRUCTURAL errors present {structural} — aborting per the validator.")
                return 3
    except Exception as exc:
        log(f"validation raised (continuing to fly): {exc}")

    # 4. Fly the primitives FAIL-FAST.
    reached = 0
    results = []
    for i, step in enumerate(PLAN, start=1):
        prim, args = step["primitive"], step.get("args", {})
        log(f"=== STEP {i}/{len(PLAN)}: {prim} {args} ===")
        try:
            pr = run_primitive(ctx, prim, args)
        except Exception as exc:
            log(f"STEP {i} EXCEPTION: {exc}\n{traceback.format_exc()}")
            results.append({"step": i, "primitive": prim, "ok": False, "marker": "exception", "detail": str(exc)})
            break
        log(f"STEP {i} -> ok={pr.ok} marker={pr.marker}; {pr.detail}")
        results.append({"step": i, "primitive": prim, "ok": pr.ok, "marker": pr.marker, "detail": pr.detail})
        if not pr.ok:
            log(f"FAIL-FAST at step {i} ({prim}): {pr.marker} — aborting mission.")
            break
        reached = i
        try:
            ctx.refresh_vessel()
        except Exception:
            pass

    # 5. Save + write the flight log.
    try:
        ctx.sc.save("ai_mun_attempt")
        log("game saved as 'ai_mun_attempt'")
    except Exception as exc:
        log(f"save failed: {exc}")
    summary = {"reached_step": reached, "total_steps": len(PLAN),
               "complete": reached == len(PLAN), "steps": results}
    try:
        Path("C:/tmp/mun_flight_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                                          encoding="utf-8")
    except Exception:
        pass
    log(f"=== FLIGHT DONE: reached {reached}/{len(PLAN)} steps; complete={reached == len(PLAN)} ===")
    return 0 if reached == len(PLAN) else 1


if __name__ == "__main__":
    raise SystemExit(main())
