from __future__ import annotations

import re

from . import budget
from .models import MissionSpec


ORBIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*km", re.IGNORECASE)
PAYLOAD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:t|ton|tons|tonne|tonnes|kg|kilogram)", re.IGNORECASE)


class MissionPlanner:
    """Heuristic mission interpreter.

    This is intentionally deterministic. It converts natural language into a
    concrete contract that an external AI provider or local optimizer can use.

    The Δv budget on each MissionSpec is no longer a flat hand-picked number: it is CALCULATED by
    `budget.total_budget_mps` from `astro.py` + the stock-body catalogue (ascent + transfer + capture
    + descent + surface-ascent + return, only the phases the mission needs). The same `budget` module
    feeds the optimizer's per-phase design requirements, so the mission's Δv requirement and the
    design's Δv target are one physics.
    """

    def interpret(self, goal: str) -> MissionSpec:
        text = goal.lower()
        orbit_match = ORBIT_RE.search(text)
        orbit_m = int(float(orbit_match.group(1)) * 1000) if orbit_match else 80000
        payload_t = self._payload_mass_t(text)
        crewed = any(word in text for word in ["crew", "crewed", "kerbal", "manned"])
        reusable = "reusable" in text or "recoverable" in text

        artemis_keywords = ["artemis", "sls", "orion", "starship", "hls", "human landing system"]
        if ("mun" in text or "moon" in text) and any(keyword in text for keyword in artemis_keywords):
            phases = [
                "research Artemis/SLS/Orion/HLS architecture",
                "launch a high Mun relay satellite for signal coverage",
                "predeploy Starship HLS analogue to Mun orbit",
                "launch SLS/Orion analogue with crew",
                "capture Orion analogue in Mun orbit",
                "model Orion-HLS crew transfer in Mun orbit",
                "fly HLS analogue to Mun surface",
                "perform crewed Mun surface science",
                "return HLS analogue to Mun orbit",
                "model crew transfer back to Orion",
                "return Orion analogue to Kerbin",
                "recover crew and score the architecture",
                "iterate vehicle and guidance changes",
            ]
            spec = MissionSpec(
                goal=goal,
                mission_type="artemis_hls_orion_return",
                target_body="Mun",
                target_orbit_m=80_000,
                payload_mass_t=max(payload_t, 0.2),
                crewed=True,
                require_landing=True,
                require_return=True,
                reusable=reusable,
                reliability_trials=2,
                phases=phases,
            )
            spec.delta_v_budget_mps = budget.total_budget_mps(spec)
            return spec

        if "mun" in text or "moon" in text:
            phases = [
                "design launcher and crewed transfer stack",
                "launch to low Kerbin orbit",
                "trans-Mun injection",
                "Mun capture",
                "descent and landing",
                "ascent and Kerbin return",
                "re-entry and recovery",
                "score and iterate",
            ]
            spec = MissionSpec(
                goal=goal,
                mission_type="mun_landing_return",
                target_body="Mun",
                target_orbit_m=orbit_m,
                payload_mass_t=max(payload_t, 0.2 if crewed else payload_t),
                crewed=crewed or "landing" in text,
                require_landing=True,
                require_return=True,
                reusable=reusable,
                reliability_trials=2,
                phases=phases,
            )
            spec.delta_v_budget_mps = budget.total_budget_mps(spec)
            return spec

        if "orbit" in text:
            phases = [
                "design launch vehicle",
                "write craft file",
                "load and launch",
                "gravity turn",
                "circularize",
                "evaluate orbit and payload delivery",
                "score and iterate",
            ]
            spec = MissionSpec(
                goal=goal,
                mission_type="kerbin_orbit",
                target_body="Kerbin",
                target_orbit_m=orbit_m,
                payload_mass_t=payload_t,
                crewed=crewed,
                reusable=reusable,
                reliability_trials=2 if reusable else 1,
                phases=phases,
            )
            spec.delta_v_budget_mps = budget.total_budget_mps(spec)
            return spec

        phases = [
            "design prototype",
            "write craft file",
            "load and launch",
            "execute controller",
            "record telemetry",
            "score and iterate",
        ]
        spec = MissionSpec(
            goal=goal,
            mission_type="generic",
            payload_mass_t=payload_t,
            crewed=crewed,
            reusable=reusable,
            phases=phases,
        )
        # Generic missions default to a Kerbin low-orbit ascent budget (the safest proven profile),
        # calculated rather than the old flat 5000.
        spec.delta_v_budget_mps = budget.total_budget_mps(spec)
        return spec

    @staticmethod
    def _payload_mass_t(text: str) -> float:
        match = PAYLOAD_RE.search(text)
        if not match:
            return 0.0
        value = float(match.group(1))
        unit_text = match.group(0).lower()
        if "kg" in unit_text or "kilogram" in unit_text:
            return value / 1000.0
        return value
