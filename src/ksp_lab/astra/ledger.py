"""The experience ledger — ASTRA's append-only long-term memory.

Every attempt (success or failure) is appended as one JSON line. The ledger is what makes the agent
get smarter over time and what lets a future run continue fast: the design contract and the
failure->fix rules learned across ~16 relay flights + the HLS/Orion arcs are seeded here, and live
attempts add to them. This mirrors the human "append-only rule ledger" that actually converged the
project (see GENERALIZED_AEROSPACE_METHODOLOGY.md).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class LedgerEntry:
    command: str
    capability: str
    attempt: int
    outcome: str  # "success" | "failure" | "info"
    marker: str = ""  # the mission_phase / failure marker the flight ended on
    fix_applied: str = ""  # the diagnosis/fix this attempt acted on (if any)
    notes: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Seeded failure -> fix rules, distilled from the project's hard-won lessons. The agent consults
# these (plus anything it learns live) to diagnose a flight that ended on a given marker.
SEED_RULES: list[dict[str, str]] = [
    {
        "match": "ascent_stuck_on_pad",
        "principle": "Launch TWR margin",
        "fix": "Actual launch TWR < 1 (estimate ignores accessory mass). Lighten upper stages or add "
        "launch thrust until est launch TWR >= ~1.4.",
    },
    {
        "match": "no command|no crew|probe core|object reference",
        "principle": "Control source",
        "fix": "Headless launch needs a probe-core control source; a bare crewed pod blocks launch. "
        "Render uncrewed (probe) or add a probe core.",
    },
    {
        "match": "finalizeanalytics|nullreference|won't launch|will not launch",
        "principle": "Craft-generation contract",
        "fix": "Generated craft must omit top-level ACTIONGROUPS/Override* headers and splice REAL "
        "part MODULE/RESOURCE serialization from stock VAB craft.",
    },
    {
        "match": "split|decoupled at launch|suborbital_cross_feed",
        "principle": "Decoupler staging",
        "fix": "Each inter-stage decoupler activates one inverse-stage LATER than its engine "
        "(render_index - 1); launch+transfer stages each need a decoupler, the bus stays attached.",
    },
    {
        "match": "stuck_on_pad|velocity.*0|zeroed",
        "principle": "Reference frame",
        "fix": "Read ascent/landing flight in vessel.orbit.body.reference_frame, not the default "
        "co-moving surface frame (which reads zero velocity). Key stuck-detector on apoapsis.",
    },
    {
        "match": "tmi.*wrong|apoapsis.*falling|injection.*direction",
        "principle": "Burn direction & re-align",
        "fix": "A prograde TMI can only raise apoapsis; falling apoapsis means mis-aligned start. "
        "Re-align to prograde and resume at low throttle — never flip; guard Kerbin periapsis.",
    },
    {
        "match": "out_of_fuel|staged_to_continue|node.*fuel",
        "principle": "Node-burn staging",
        "fix": "Stage into the next fueled stage BEFORE declaring out-of-fuel (transfer fuel sits in "
        "decoupled lower stages).",
    },
    {
        "match": "off-axis|wasted|capture.*fail|tumbl",
        "principle": "Attitude authority (master root cause)",
        "fix": "Heavy upper stacks need dedicated inline reaction-wheel torque (~3x asasmodule1-2) "
        "before any finite burn; hold energy-removal burns on the engine gimbal (autopilot), "
        "re-pointed each tick, not on reaction wheels alone.",
    },
    {
        "match": "deorbit_timeout|deorbit.*slow",
        "principle": "Big burns use full throttle",
        "fix": "A large deorbit/lowering burn must not use the precision-apsis throttle cap (0.18); "
        "pass max_throttle=1.0 and a longer max_burn_s.",
    },
    {
        "match": "wrong_direction|deorbit_wrong",
        "principle": "Always autopilot-align before igniting",
        "fix": "Point at a maneuver node with the autopilot + an alignment-error wait; never SAS + a "
        "fixed sleep that returns 'aligned' without checking.",
    },
    {
        "match": "landed_unstable|hard_contact|landing_out_of_fuel|crash",
        "principle": "Falcon-9 hoverslam landing",
        "fix": "Maximize freefall (engine off) until total speed reaches v_ref(h)=sqrt(2*(0.92*a_max-"
        "g)*h), then full-throttle brake on surface-retrograde to null all velocity at the ground; "
        "flip to local-up for the final slow settle. Needs a controllable lander with CLEAN staging "
        "(descent engine is the active stage) + landing legs + reaction wheels.",
    },
    {
        "match": "transfer_node_not_found|transfer.*not_found|no.*encounter",
        "principle": "Trans-Munar phase-angle variance",
        "fix": "The Mun's phase angle was unfavorable in the searched window. Search ~3 orbital "
        "periods ahead (not 1) for a transfer node, and/or simply retry — a relaunch gets a fresh "
        "phasing. This is the dominant predeploy variance.",
    },
    {
        "match": "relay.*periapsis|periapsis.*high|relay.*reject",
        "principle": "Target the functional band",
        "fix": "A relay WANTS a high orbit — accept a relay-band capture (apoapsis 250-2150 km, "
        "periapsis >= 50 km) instead of fighting finite-burn precision for a low periapsis.",
    },
    {
        "match": "reentry|burn up|no heat shield",
        "principle": "Return craft need a heat shield",
        "fix": "An uncrewed return craft still needs a heat shield + parachute for Kerbin reentry "
        "(render adds HeatShield1 when crewed OR heatshield flag set).",
    },
]


class ExperienceLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, entry: LedgerEntry) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def seed_rules(self) -> list[dict[str, str]]:
        return list(SEED_RULES)

    def learned_fixes(self) -> list[str]:
        """Distinct fixes that previously turned a failure into a later success, newest first."""
        fixes: list[str] = []
        for e in reversed(self.entries()):
            fx = (e.get("fix_applied") or "").strip()
            if fx and fx not in fixes:
                fixes.append(fx)
        return fixes

    def render_markdown(self) -> str:
        rows = self.entries()
        lines = ["# ASTRA Experience Ledger", "", f"Total recorded attempts: {len(rows)}", ""]
        lines.append("## Seeded failure -> fix rules")
        for r in SEED_RULES:
            lines.append(f"- **{r['principle']}** (`{r['match']}`): {r['fix']}")
        lines.append("")
        lines.append("## Recorded attempts")
        for e in rows:
            lines.append(
                f"- `{e.get('at','')}` **{e.get('capability','')}** attempt {e.get('attempt','?')}"
                f" — {e.get('outcome','')} on `{e.get('marker','')}`"
                + (f" — fix: {e.get('fix_applied')}" if e.get("fix_applied") else "")
            )
        return "\n".join(lines) + "\n"
