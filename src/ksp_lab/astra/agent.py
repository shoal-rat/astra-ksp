"""The ASTRA autonomous loop: interpret -> (design+fly via proven drivers) -> diagnose -> retry ->
record, until the mission's capabilities all succeed.

ASTRA orchestrates the project's proven single-phase drivers (tools/fly_*.py) rather than
re-deriving the flight calls — they are the validated path. Around them it adds the things that make
it an *agent*: a natural-language front door, a persistent experience ledger, automatic diagnosis of
a failed flight against known failure->fix rules, and bounded retry to absorb run-to-run variance.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import load_config
from .interpreter import Interpreter, MissionPlan
from .knowledge import Diagnosis, KnowledgeBase
from .ledger import ExperienceLedger, LedgerEntry

# Each capability is an ordered list of (step name, driver script). All steps must pass for the
# capability to succeed. Drivers are invoked as: python <script> <config_path> [extra args].
CAPABILITY_DRIVERS: dict[str, list[tuple[str, str]]] = {
    "relay": [("relay", "tools/fly_relay_once.py")],
    "hls_land_return": [
        ("hls_predeploy", "tools/fly_hls_predeploy.py"),
        ("hls_sortie", "tools/fly_hls_sortie.py"),  # vessel name injected at runtime
    ],
    "crew_return": [("orion_return", "tools/fly_orion.py")],
}

_PHASE_RE = re.compile(r"mission_phase\s*[:=]\s*([A-Za-z0-9_]+)")
_RESULT_RE = re.compile(r"RESULT\s*[:=]\s*([A-Za-z]+)")


def _log(msg: str) -> None:
    print(f"[ASTRA {time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass(slots=True)
class CapabilityResult:
    capability: str
    success: bool
    marker: str
    attempts: int
    diagnosis: Diagnosis | None = None


@dataclass(slots=True)
class AstraResult:
    command: str
    plan: MissionPlan
    capability_results: list[CapabilityResult] = field(default_factory=list)
    success: bool = False

    def summary_text(self) -> str:
        lines = [
            f"ASTRA mission: {self.command!r}",
            f"  interpreted by: {self.plan.source} -> body={self.plan.target_body} "
            f"capabilities={self.plan.capabilities}",
        ]
        if self.plan.rationale:
            lines.append(f"  rationale: {self.plan.rationale}")
        for cr in self.capability_results:
            tick = "OK " if cr.success else "XX "
            lines.append(f"  [{tick}] {cr.capability}: {cr.marker} (attempts {cr.attempts})")
            if cr.diagnosis and not cr.success:
                lines.append(f"        diagnosis: [{cr.diagnosis.principle}] {cr.diagnosis.fix}")
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
        try:
            self._driver_timeout = int(self.config["runner"]["flight_timeout_s"]) + 300
        except Exception:
            self._driver_timeout = 2100

    # ---------- public ----------
    def run(self, command: str) -> AstraResult:
        plan = self.interpreter.interpret(command)
        _log(f"interpreted ({plan.source}): body={plan.target_body} caps={plan.capabilities}")
        if plan.rationale:
            _log(f"rationale: {plan.rationale}")
        self.ledger.record(
            LedgerEntry(command, "interpret", 0, "info", "", "", plan.notes,
                        {"capabilities": plan.capabilities, "source": plan.source})
        )
        result = AstraResult(command=command, plan=plan)
        if self.dry_run:
            _log("dry-run: interpretation only, no flight.")
            result.success = True
            return result

        overall = True
        for cap in plan.capabilities:
            cr = self._run_capability(command, cap)
            result.capability_results.append(cr)
            if not cr.success:
                overall = False
                _log(f"capability {cap} did not complete; later phases depend on it — stopping.")
                break
        result.success = overall
        self._write_ledger_markdown()
        return result

    # ---------- internals ----------
    def _run_capability(self, command: str, cap: str) -> CapabilityResult:
        steps = CAPABILITY_DRIVERS.get(cap)
        if not steps:
            d = Diagnosis("Unsupported capability", f"No proven driver for '{cap}'.", "unknown")
            return CapabilityResult(cap, False, "unsupported", 0, d)

        last_marker = ""
        diagnosis: Diagnosis | None = None
        attempt = 0
        for attempt in range(1, self.max_attempts + 1):
            _log(f"capability {cap}: attempt {attempt}/{self.max_attempts}")
            step_ok = True
            for step_name, script in steps:
                extra: list[str] = []
                if script.endswith("fly_hls_sortie.py"):
                    name = self._fresh_hls_vessel_name()
                    if name:
                        extra = [name]
                        _log(f"  targeting freshly parked HLS: {name}")
                marker, success, tail = self._run_driver(script, extra)
                last_marker = marker or last_marker
                self.ledger.record(
                    LedgerEntry(
                        command, f"{cap}:{step_name}", attempt,
                        "success" if success else "failure", marker,
                        diagnosis.fix if diagnosis else "", tail[-500:],
                    )
                )
                if not success:
                    step_ok = False
                    diagnosis = self.knowledge.diagnose(marker, log_tail=tail)
                    _log(f"  FAILED on '{marker}'. diagnosis [{diagnosis.confidence}]: "
                         f"{diagnosis.principle} — {diagnosis.fix}")
                    break
                _log(f"  step {step_name} OK ({marker})")
            if step_ok:
                return CapabilityResult(cap, True, last_marker, attempt, None)
            # An unknown failure won't fix itself by retrying — stop and surface it.
            if diagnosis and diagnosis.confidence == "unknown":
                break
            if attempt < self.max_attempts:
                _log(f"  retrying {cap} (known issue / variance)…")
        return CapabilityResult(cap, False, last_marker, attempt, diagnosis)

    def _run_driver(self, script: str, extra_args: list[str]) -> tuple[str, bool, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.project_root / "src")
        cmd = [sys.executable, script, str(self.config_path), *extra_args]
        try:
            proc = subprocess.run(
                cmd, cwd=str(self.project_root), env=env,
                capture_output=True, text=True, timeout=self._driver_timeout,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            success = proc.returncode == 0
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") + (exc.stderr or "") + "\n[ASTRA] driver timed out"
            success = False
        marker = ""
        for m in _PHASE_RE.finditer(out):
            marker = m.group(1)
        return marker, success, out

    def _fresh_hls_vessel_name(self) -> str | None:
        """The just-parked HLS is the active/loaded vessel after predeploy (most parts)."""
        try:
            import krpc

            k = self.config["krpc"]
            conn = krpc.connect(
                name="astra-find", address=k.get("host", "127.0.0.1"),
                rpc_port=int(k.get("rpc_port", 50000)), stream_port=int(k.get("stream_port", 50001)),
            )
            try:
                best, best_parts = None, -1
                for v in conn.space_center.vessels:
                    try:
                        if v.name.startswith("AI-HLS-Starship") and v.orbit.body.name == "Mun":
                            n = len(v.parts.all)
                            if n > best_parts:
                                best_parts, best = n, v.name
                    except Exception:
                        continue
                return best
            finally:
                conn.close()
        except Exception:
            return None

    def _write_ledger_markdown(self) -> None:
        try:
            (self.project_root / "runs" / "ASTRA_LEDGER.md").write_text(
                self.ledger.render_markdown(), encoding="utf-8"
            )
        except OSError:
            pass
