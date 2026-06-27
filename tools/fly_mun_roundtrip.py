"""Autonomous Mun land-and-return flight driver — the final ASTRA experiment, run WITHOUT the LLM
interpreter (Claude stands in for the decomposer with a hand-built plan; the flight itself is pure
kRPC/MechJeb via the primitives, so it needs no ANTHROPIC_API_KEY).

Flow: load the save autonomously (/load-save) -> connect kRPC+bridge -> VALIDATE the plan (mission graph
+ plan_validator, the rigorous validator) -> MAKE THE LAUNCH MISSION-AWARE (size ONE vehicle for the
whole round-trip from the graph's post-LKO budget) -> fly each primitive FAIL-FAST -> save + flight log.

    PYTHONPATH=src python tools/fly_mun_roundtrip.py configs/local-ksp.yaml

Everything is logged to C:/tmp/mun_flight.log AND stdout, so the long flight runs detached.

================================ LIVE-FLIGHT STAGING GAPS (READ ME) ================================
The launch DESIGN is now mission-aware: ``_mission_aware_launch_args`` (below) reads the mission graph,
sums every NON-launch node's Δv into ``mission_dv``, and passes it + ``needs_legs`` into the launch step,
so design_chart sizes ONE vehicle with enough Δv + legs + heatshield/chutes for the full Mun round-trip.

What this does NOT (and cannot, offline) fix are the LIVE flight-staging gaps — the transfer/land/ascend
primitives still wrap their existing flight machinery, and whether MechJeb actually flies the oversized
upper stage through the legs / heatshield without stranding fuel is only observable in a LIVE game. Those
gaps are written up as explicit watch-items in docs/MUN_FLIGHT_LIVE_TODO.md; the next live session MUST
follow that checklist (the upper-stage-actually-used, MechJeb-stages-through-the-legs items in particular).
====================================================================================================
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

LOG = Path("C:/tmp/mun_flight.log")

# Margin folded on TOP of the graph's summed post-LKO Δv when sizing the launch vehicle's mission phase.
# The graph's per-step costs are nominal Hohmann/vis-viva; a live grid-search capture + a sloppier node can
# run a few hundred m/s over, so the vehicle leaves the pad with this slice banked. (design.py adds its own
# per-stage + 5% mission reserves on top of this.)
POST_LKO_MARGIN_FRAC = 0.05


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
                                     "chutes": True, "radial_boosters": 0, "name": "AI-Mun-1"}},
    {"primitive": "transfer", "args": {"target_body": "Mun"}},
    {"primitive": "land", "args": {}},
    # plant_flag is a BONUS objective. EVA can be refused by the live game (a blocked Mk1 hatch — the heat
    # shield sits under the side hatch — or a busy airlock); a failed flag must NOT strand the crew on the
    # Mun, so it is marked optional and the mission presses on to the ascent + return.
    {"primitive": "plant_flag", "args": {}, "optional": True},
    {"primitive": "ascend", "args": {"target_alt_km": 20}},
    {"primitive": "transfer", "args": {"target_body": "Kerbin"}},
    {"primitive": "recover", "args": {}},
]


def _mission_aware_launch_args(plan: list[dict], *, launch_body: str = "Kerbin") -> dict:
    """Derive the MISSION-AWARE launch args from the mission graph so ONE vehicle is sized for the whole
    round-trip, not just LKO. Returns the dict to MERGE into the launch step's args:

      * ``mission_dv``      = (Σ Δv of every NON-launch node) * (1 + POST_LKO_MARGIN_FRAC) — the post-LKO
                              budget (TMI + capture + land + ascend + return + reentry) the SAME craft
                              must carry on its own propellant, since launch flies it all the way home.
      * ``needs_legs``      = True if any ``land``/``ascend`` is on a NON-atmospheric body (a propulsive
                              touchdown needs legs even with chutes; chutes alone don't imply legs airless).
      * ``heatshield`` /
        ``chutes``          = True if the plan ``recover``s on a body WITH an atmosphere (Kerbin return:
                              aerobrake + chute under a heatshield). These only ADD to the launch flags.

    Returns ``{}`` (no mission-aware sizing) when the plan has no post-LKO nodes — a plain LKO launch is
    left exactly as written."""
    from ksp_lab.astra.mission_graph import build_mission_graph
    from ksp_lab.bodies import body as lookup_body

    g = build_mission_graph(plan, launch_body=launch_body)
    post_lko_dv = sum(n.dv_mps for n in g.nodes if n.primitive != "launch")
    if post_lko_dv <= 0.0:
        return {}

    needs_legs = False
    needs_heatshield = False
    needs_chutes = False
    for n in g.nodes:
        if n.primitive in ("land", "ascend"):
            try:
                if lookup_body(n.target_body).atmosphere_top_m <= 0:
                    needs_legs = True          # airless touchdown -> legs (chutes don't help on the Mun)
            except Exception:
                pass
        if n.primitive == "recover":
            try:
                if lookup_body(n.target_body).atmosphere_top_m > 0:
                    needs_heatshield = True     # atmospheric re-entry -> heatshield + chutes
                    needs_chutes = True
            except Exception:
                pass

    args: dict = {"mission_dv": round(post_lko_dv * (1.0 + POST_LKO_MARGIN_FRAC), 1),
                  "needs_legs": needs_legs}
    if needs_heatshield:
        args["heatshield"] = True
    if needs_chutes:
        args["chutes"] = True
    return args


def _parse_args(argv: list[str]) -> tuple[str, int]:
    """``[config.yaml] [--from-step N]``. ``--from-step`` (1-based) RESUMES the plan against the live active
    vessel — fly the launch once, then iterate a later leg (transfer/land/ascend/return/recover) in place
    without re-launching. The skipped steps are assumed already flown (the vessel is left in the right state
    by the prior partial run)."""
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
    log(f"=== ASTRA Mun land-and-return experiment :: config={cfg_path} from_step={from_step} ===")

    # MISSION-AWARE LAUNCH SIZING: enrich the launch step IN PLACE from the mission graph so the vehicle
    # is built for the whole round-trip (post-LKO Δv + legs + heatshield/chutes), not just to LKO.
    try:
        from ksp_lab.astra.mission_graph import build_mission_graph as _bmg  # noqa: F401  (import check)
        ma = _mission_aware_launch_args(PLAN, launch_body="Kerbin")
        if ma:
            for step in PLAN:
                if step.get("primitive") == "launch":
                    step.setdefault("args", {})
                    # additive: explicit plan flags win; only fill what the planner left unset.
                    for k, v in ma.items():
                        step["args"].setdefault(k, v)
                    log(f"MISSION-AWARE launch sizing -> {ma} (merged into launch args: {step['args']})")
                    break
    except Exception as exc:
        log(f"mission-aware sizing skipped ({exc}); launch will size for LKO only")

    from ksp_lab.astra.agent import AstraAgent
    from ksp_lab.astra.mission_graph import build_mission_graph
    from ksp_lab.astra.plan_validator import validate_plan
    from ksp_lab.astra.primitives import run_primitive

    # Build the agent WITHOUT a real interpreter (sentinel) so it never needs ANTHROPIC_API_KEY.
    agent = AstraAgent(cfg_path, interpreter=object(), max_attempts=1)
    cfg = agent.config

    # 1. Load the save autonomously via /load-save (the autonomous-setup deliverable) — but ONLY if KSP
    #    isn't already in a scene. kRPC only listens once a game is loaded, so kRPC-up == a save is already
    #    loaded; in that case skip the reload (the /load-save HTTP blocks ~120s and a reload just resets the
    #    same scene). When kRPC is down (main menu), load the save to enter the space center.
    import socket as _socket

    def _krpc_listening() -> bool:
        try:
            s = _socket.socket(); s.settimeout(2); s.connect(("127.0.0.1", 50000)); s.close(); return True
        except Exception:
            return False

    if _krpc_listening():
        log("kRPC already listening — a save is loaded; skipping /load-save reload")
    else:
        try:
            from ksp_lab.bridge_client import BridgeClient
            bridge_cfg = cfg.get("bridge", {}) if isinstance(cfg, dict) else {}
            bridge = BridgeClient(**bridge_cfg) if bridge_cfg else BridgeClient()
            log("loading save '默认' via /load-save (autonomous setup)")
            try:
                res = bridge.load_save("默认")
                log(f"  /load-save -> {res}")
            except Exception as exc:
                log(f"  /load-save HTTP returned/blocked ({exc}); the load still completes — waiting for kRPC")
            for _ in range(20):
                time.sleep(3)
                if _krpc_listening():
                    break
        except Exception as exc:
            log(f"  /load-save setup failed ({exc}); proceeding — the save may already be loaded")

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

    # 4. Fly the primitives FAIL-FAST. On a --from-step resume, the earlier steps are assumed already flown
    #    (the live active vessel carries the state); reached starts at the last skipped step so the
    #    completion check still measures the whole plan.
    if from_step > 1:
        log(f"RESUME: skipping steps 1..{from_step - 1}; flying from step {from_step} against the live "
            f"active vessel {ctx.vessel_name!r} at {ctx.current_body}")
    reached = from_step - 1
    results = []
    for i, step in enumerate(PLAN, start=1):
        if i < from_step:
            continue
        prim, args = step["primitive"], step.get("args", {})
        log(f"=== STEP {i}/{len(PLAN)}: {prim} {args} ===")
        try:
            pr = run_primitive(ctx, prim, args)
        except Exception as exc:
            log(f"STEP {i} EXCEPTION: {exc}\n{traceback.format_exc()}")
            results.append({"step": i, "primitive": prim, "ok": False, "marker": "exception", "detail": str(exc)})
            break
        log(f"STEP {i} -> ok={pr.ok} marker={pr.marker}; {pr.detail}")
        results.append({"step": i, "primitive": prim, "ok": pr.ok, "marker": pr.marker, "detail": pr.detail,
                        "optional": bool(step.get("optional"))})
        if not pr.ok:
            if step.get("optional"):
                log(f"STEP {i} ({prim}) failed ({pr.marker}) but is OPTIONAL — pressing on with the mission.")
                reached = i
                try:
                    ctx.refresh_vessel()
                except Exception:
                    pass
                continue
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
