"""The ASTRA autonomous loop: DECOMPOSE -> execute primitives -> diagnose -> retry -> record.

The redesign turns ASTRA from a 3-bundle Mun selector into a GENERAL agent. The interpreter decomposes
the command into an ordered list of atomic, body-agnostic PRIMITIVES (primitives.py). This agent connects
to kRPC + the bridge ONCE, threads a live PrimitiveContext (conn / space_center / bridge / runner + the
current vessel / target state) through the steps, runs each step with bounded retry + the experience
ledger + knowledge diagnosis around per-step failures, and FAILS FAST: a failed primitive surfaces its
``mission_phase`` / ``RESULT:FAIL`` marker and aborts the mission rather than hanging silently.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import load_config
from .interpreter import Interpreter, MissionPlan
from .knowledge import Diagnosis, KnowledgeBase
from .ledger import ExperienceLedger, LedgerEntry
from .mission_graph import MissionGraph, build_mission_graph
from .plan_validator import ValidationReport, validate_plan
from .primitives import CATALOG, PrimitiveContext, PrimitiveResult, run_primitive


def post_bridge_status(phase: str, message: str, *, host: str = "127.0.0.1", port: int = 48500) -> None:
    """Push a live status line to the KspAutomationBridge so it shows in the in-game panel.
    Best-effort: silently does nothing if the bridge/endpoint isn't available."""
    import json as _json
    import urllib.request

    try:
        body = _json.dumps({"phase": phase, "message": message}).encode("utf-8")
        req = urllib.request.Request(
            f"http://{host}:{port}/status", data=body,
            headers={"content-type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=4).read()
    except Exception:
        pass


def _log(msg: str) -> None:
    print(f"[ASTRA {time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass(slots=True)
class StepResult:
    index: int
    primitive: str
    args: dict
    success: bool
    marker: str
    attempts: int
    detail: str = ""
    diagnosis: Diagnosis | None = None


@dataclass(slots=True)
class AstraResult:
    command: str
    plan: MissionPlan
    step_results: list[StepResult] = field(default_factory=list)
    success: bool = False
    graph: MissionGraph | None = None
    validation: ValidationReport | None = None

    def summary_text(self) -> str:
        lines = [
            f"ASTRA mission: {self.command!r}",
            f"  decomposed by: {self.plan.source} -> body={self.plan.target_body}",
            f"  plan: {self.plan.step_summary()}",
        ]
        if self.plan.rationale:
            lines.append(f"  rationale: {self.plan.rationale}")
        if self.graph is not None:
            lines.append("  " + self.graph.render().replace("\n", "\n  "))
        if self.validation is not None:
            lines.append("  " + self.validation.render().replace("\n", "\n  "))
        for sr in self.step_results:
            tick = "OK " if sr.success else "XX "
            argstr = ", ".join(f"{k}={v}" for k, v in sr.args.items()) if sr.args else ""
            lines.append(f"  [{tick}] {sr.index}. {sr.primitive}({argstr}): {sr.marker} (attempts {sr.attempts})")
            if sr.diagnosis and not sr.success:
                lines.append(f"        diagnosis: [{sr.diagnosis.principle}] {sr.diagnosis.fix}")
        lines.append(f"  RESULT: {'SUCCESS' if self.success else 'INCOMPLETE'}")
        return "\n".join(lines)


class AstraAgent:
    def __init__(
        self,
        config_path: str | Path,
        *,
        ledger_path: str | Path | None = None,
        interpreter: Interpreter | None = None,
        max_attempts: int = 2,
        dry_run: bool = False,
        vehicle_dv: float | None = None,
    ):
        self.config_path = Path(config_path).resolve()
        # lab root = .../ksp1-automation-lab (config lives in configs/)
        self.project_root = self.config_path.parent.parent
        self.config = load_config(self.config_path)
        ledger_path = ledger_path or (self.project_root / "runs" / "astra_experience.jsonl")
        self.ledger = ExperienceLedger(ledger_path)
        self.knowledge = KnowledgeBase(self.ledger, self.project_root)
        self.interpreter = interpreter or Interpreter()
        self.max_attempts = max(1, int(max_attempts))
        self.dry_run = dry_run
        self.vehicle_dv = vehicle_dv
        self.post_status = True
        self._graph: MissionGraph | None = None

    def _status(self, phase: str, message: str) -> None:
        _log(f"{phase}: {message}")
        if self.post_status:
            post_bridge_status(phase, message)

    # ---------- public ----------
    def run(self, command: str) -> AstraResult:
        # LIVE run: connect FIRST so the LLM mission-architect reasons over the LIVE universe state (vessel
        # orbits + resources/mass via the bridge), not just the static bodies+catalog — the user's "give the
        # LLM sufficient information". DRY-RUN: plan from the static context only (no connection needed).
        #
        # A LIVE run FAILS LOUDLY: there is NO quiet degrade. If we cannot open the kRPC/bridge connection,
        # or cannot build the live planning context, we RAISE rather than planning blind and pretending —
        # a real flight must reason over the real universe state. (Only a dry-run plans from the static
        # context, because by definition it is not flying.)
        ctx = None
        planning_ctx = None
        if not self.dry_run:
            ctx = self._connect_context()
            if ctx is None:
                raise RuntimeError(
                    "ASTRA live run requires a live KSP connection — failed to connect to kRPC/bridge "
                    "(see the 'connect' status above). No quiet static-context degrade for a real flight; "
                    "start KSP + the bridge, or use --dry-run to plan offline."
                )
            from . import planning_context as _pc
            try:
                planning_ctx = _pc.build_planning_context(ctx.conn, ctx.sc, command, bridge=ctx.bridge)
            except Exception as exc:
                self._status("connect", f"FAILED to build live planning context ({exc})")
                try:
                    if ctx.conn is not None:
                        ctx.conn.close()
                except Exception:
                    pass
                raise RuntimeError(
                    f"ASTRA live run could not build the live planning context ({exc}) — the mission "
                    "architect must reason over the live universe state, so this is a hard failure, not a "
                    "static-context degrade."
                ) from exc

        plan = self.interpreter.interpret(command, planning_ctx=planning_ctx)
        self._status("decompose", f"[{plan.source}] {plan.target_body}: {plan.step_summary()}")
        if plan.rationale:
            _log(f"rationale: {plan.rationale}")
        self.ledger.record(
            LedgerEntry(command, "decompose", 0, "info", "", "", plan.notes,
                        {"steps": plan.steps, "source": plan.source})
        )
        result = AstraResult(command=command, plan=plan)

        # ---- RIGOROUS PLAN VALIDATION (after interpret, BEFORE flying; also in dry-run). Build the
        # MISSION GRAPH (preconditions/postconditions + per-step orbital math) and validate it. A
        # REJECTED plan is NOT flown — we surface the SPECIFIC errors and fail the mission, rather than
        # silently trimming the LLM's parameters. The graph + report are attached to the result.
        report = self._validate_plan(command, plan, ctx)
        result.graph = self._graph
        result.validation = report
        if not report.ok:
            _log("plan REJECTED by the validator — NOT flying. Errors:")
            for err in report.errors:
                _log(f"  - {err}")
            self._status("reject", f"{len(report.errors)} validation error(s); mission NOT flown")
            self.ledger.record(
                LedgerEntry(command, "validate", 0, "failure", "plan_rejected", "",
                            "; ".join(report.errors)[:500],
                            {"errors": report.errors, "warnings": report.warnings})
            )
            result.success = False
            self._close_ctx(ctx)
            return result

        if self.dry_run:
            _log("dry-run: decomposition + validation only, no flight.")
            for i, step in enumerate(plan.steps, start=1):
                result.step_results.append(
                    StepResult(i, step["primitive"], step.get("args", {}), True, "dry_run", 0,
                               "decomposed + validated; not flown"))
            result.success = True
            return result

        # ctx is already connected above (live run).

        overall = True
        for i, step in enumerate(plan.steps, start=1):
            sr = self._run_step(command, ctx, i, step)
            result.step_results.append(sr)
            if not sr.success:
                overall = False
                _log(f"step {i} ({sr.primitive}) failed on '{sr.marker}' — FAIL FAST, aborting mission.")
                self._status("abort", f"step {i} {sr.primitive} -> {sr.marker}")
                break
        result.success = overall
        self._write_ledger_markdown()
        self._close_ctx(ctx)
        return result

    # ---------- internals ----------
    def _validate_plan(self, command: str, plan: MissionPlan, ctx: PrimitiveContext | None
                       ) -> ValidationReport:
        """Build the mission graph from the decomposed plan and validate it RIGOROUSLY. The graph is
        kept on ``self._graph`` so run() can attach it to the result. A live ``sc`` (if connected) lets
        planet transfers use the precise Lambert window; offline the closed-form estimate is used. The
        launch body is the body the active vessel sits on (Kerbin in stock); ``vehicle_dv`` (if the
        caller supplied it) gates the resource-budget check."""
        sc = getattr(ctx, "sc", None) if ctx is not None else None
        ut_now = 0.0
        if sc is not None:
            try:
                ut_now = float(sc.ut)
            except Exception:
                ut_now = 0.0
        launch_body = "Kerbin"
        if ctx is not None and getattr(ctx, "current_body", None):
            launch_body = str(ctx.current_body)
        graph = build_mission_graph(plan.steps, launch_body=launch_body,
                                    vehicle_dv=self.vehicle_dv, sc=sc, ut_now=ut_now)
        self._graph = graph
        _log("MISSION GRAPH + VALIDATION:")
        for line in graph.render().splitlines():
            _log(line)
        report = validate_plan(graph, command=command, vehicle_dv=self.vehicle_dv)
        for line in report.render().splitlines():
            _log(line)
        return report

    @staticmethod
    def _close_ctx(ctx: PrimitiveContext | None) -> None:
        """Close the live kRPC connection if one is open (no-op in dry-run, where ctx is None)."""
        try:
            if ctx is not None and ctx.conn is not None:
                ctx.conn.close()
        except Exception:
            pass
    def _connect_context(self) -> PrimitiveContext | None:
        """Open the kRPC + bridge connections ONCE and return a live PrimitiveContext. Fails loudly."""
        try:
            import krpc

            from ksp_lab.bridge_client import BridgeClient
            from ksp_lab.runner import AutomationRunner
        except Exception as exc:
            self._status("connect", f"FAILED to import live deps ({exc}); is '.[ksp]' installed?")
            return None
        kc = self.config["krpc"]
        try:
            conn = krpc.connect(
                name="astra-agent", address=kc.get("host", "127.0.0.1"),
                rpc_port=int(kc.get("rpc_port", 50000)), stream_port=int(kc.get("stream_port", 50001)),
            )
            bridge = BridgeClient(**self.config["bridge"])
            runner = AutomationRunner(str(self.config_path), offline=False)
        except Exception as exc:
            self._status("connect", f"FAILED to connect to KSP/bridge ({exc})")
            return None
        ctx = PrimitiveContext(conn=conn, sc=conn.space_center, bridge=bridge, runner=runner,
                               cfg=self.config, dry_run=False)
        ctx.refresh_vessel()
        self._status("connect", f"connected; active body {ctx.current_body}")
        return ctx

    def _run_step(self, command: str, ctx: PrimitiveContext, index: int, step: dict) -> StepResult:
        primitive = step["primitive"]
        args = step.get("args", {})
        if primitive not in CATALOG:
            d = Diagnosis("Unsupported primitive", f"No primitive named '{primitive}'.", "unknown")
            return StepResult(index, primitive, args, False, "unknown_primitive", 0, "", d)

        last_marker = ""
        last_detail = ""
        diagnosis: Diagnosis | None = None
        attempt = 0
        for attempt in range(1, self.max_attempts + 1):
            self._status(primitive, f"step {index} attempt {attempt}/{self.max_attempts} — {args}")
            try:
                pr: PrimitiveResult = run_primitive(ctx, primitive, args)
            except Exception as exc:  # a primitive must not crash the loop — surface + fail fast
                pr = PrimitiveResult(primitive, False, "primitive_exception", str(exc))
            last_marker = pr.marker or last_marker
            last_detail = pr.detail or last_detail
            self.ledger.record(
                LedgerEntry(
                    command, f"{index}:{primitive}", attempt,
                    "success" if pr.ok else "failure", pr.marker,
                    diagnosis.fix if diagnosis else "", pr.detail[-500:],
                    {"args": args, "data": pr.data},
                )
            )
            if pr.ok:
                self._status(primitive, f"step {index} OK: {pr.marker}")
                return StepResult(index, primitive, args, True, pr.marker, attempt, pr.detail, None)
            diagnosis = self.knowledge.diagnose(pr.marker, log_tail=pr.detail)
            self._status(primitive, f"step {index} FAILED on '{pr.marker}' — "
                                    f"diagnosis [{diagnosis.confidence}]: {diagnosis.principle}")
            _log(f"  fix: {diagnosis.fix}")
            # An unknown failure won't fix itself by retrying — stop and surface it.
            if diagnosis.confidence == "unknown":
                break
            if attempt < self.max_attempts:
                _log(f"  retrying {primitive} (known issue / variance)…")
        return StepResult(index, primitive, args, False, last_marker, attempt, last_detail, diagnosis)

    def _write_ledger_markdown(self) -> None:
        try:
            (self.project_root / "runs" / "ASTRA_LEDGER.md").write_text(
                self.ledger.render_markdown(), encoding="utf-8"
            )
        except OSError:
            pass
