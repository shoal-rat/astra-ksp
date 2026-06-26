"""ASTRA's knowledge base: the generalized methodology + the experience ledger, plus a diagnoser
that maps a flight's ending marker to a known fix."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .ledger import ExperienceLedger
from .rule_base import RuleBase


@dataclass(slots=True)
class Diagnosis:
    principle: str
    fix: str
    confidence: str  # "known" (seed/learned rule matched) | "unknown"


# Where the generalized methodology lives, searched relative to a few likely roots.
_METHODOLOGY_CANDIDATES = [
    "GENERALIZED_AEROSPACE_METHODOLOGY.md",
    "../GENERALIZED_AEROSPACE_METHODOLOGY.md",
    "../../GENERALIZED_AEROSPACE_METHODOLOGY.md",
    "../../../GENERALIZED_AEROSPACE_METHODOLOGY.md",
    "docs/GENERALIZED_AEROSPACE_METHODOLOGY.md",
]


class KnowledgeBase:
    def __init__(self, ledger: ExperienceLedger, project_root: str | Path | None = None):
        self.ledger = ledger
        self.project_root = Path(project_root) if project_root else Path.cwd()
        # The structured, queryable failure rule base is consulted FIRST by diagnose(). If the packaged
        # JSON is somehow unavailable, fall back silently to the ledger's hardcoded seed rules.
        try:
            self.rule_base: RuleBase | None = RuleBase.load()
        except (OSError, ValueError, KeyError):
            self.rule_base = None

    def methodology_text(self) -> str:
        for rel in _METHODOLOGY_CANDIDATES:
            p = (self.project_root / rel)
            if p.exists():
                try:
                    return p.read_text(encoding="utf-8")
                except OSError:
                    continue
        return ""

    def diagnose(self, marker: str, *, log_tail: str = "") -> Diagnosis:
        """Match a flight's ending marker (and optional log tail) to a known fix.

        Consults the STRUCTURED rule base (rule_base.py / failure_rules.json) FIRST — it is richer
        (symptom/cause/fix, primitive-scoped, confidence-ranked) — and falls back to the ledger's
        hardcoded seed rules, then to an explicit 'unknown'. The Diagnosis shape is unchanged so
        callers are unaffected: a structured hit reports principle=<cause>, fix=<fix>, confidence='known'."""
        if self.rule_base is not None:
            hit = self.rule_base.diagnose(marker, log_tail)
            if hit is not None:
                return Diagnosis(hit.cause, hit.fix, "known")

        haystack = f"{marker} {log_tail}".lower()
        for rule in self.ledger.seed_rules():
            pattern = rule["match"]
            try:
                if re.search(pattern, haystack):
                    return Diagnosis(rule["principle"], rule["fix"], "known")
            except re.error:
                if any(tok in haystack for tok in pattern.split("|")):
                    return Diagnosis(rule["principle"], rule["fix"], "known")
        return Diagnosis(
            "Unknown failure",
            "No seeded rule matched. Record telemetry, inspect the last phase, and add a new "
            "failure->fix rule to the ledger so the next run handles it.",
            "unknown",
        )

    def context_text(self, *, max_chars: int = 6000) -> str:
        """Prompt-ready knowledge context for the LLM interpreter/planner."""
        parts = ["## Generalized aerospace methodology (excerpt)"]
        meth = self.methodology_text()
        if meth:
            parts.append(meth[:max_chars])
        parts.append("\n## Seeded failure->fix rules")
        for r in self.ledger.seed_rules():
            parts.append(f"- {r['principle']}: {r['fix']}")
        learned = self.ledger.learned_fixes()
        if learned:
            parts.append("\n## Fixes learned in earlier runs")
            parts.extend(f"- {fx}" for fx in learned[:20])
        return "\n".join(parts)
