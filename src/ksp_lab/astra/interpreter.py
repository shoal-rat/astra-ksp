"""Natural-language -> mission plan.

ASTRA accepts one line of plain English. The interpreter turns it into an ordered list of
capabilities (proven flight pipelines) plus a MissionSpec. It uses the Claude API when
ANTHROPIC_API_KEY is set (no SDK dependency — a plain HTTPS call), and always has a deterministic
heuristic fallback so the agent runs with zero configuration.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..mission import MissionPlanner
from ..models import MissionSpec

# The capabilities ASTRA can actually fly today (each maps to a proven driver in agent.py).
KNOWN_CAPABILITIES = {
    "relay": "Launch a comsat to a high stable Mun orbit.",
    "hls_land_return": "Fly a lander to Mun orbit, land on the surface (Falcon-9 hoverslam), do "
    "science, and ascend back to Mun orbit.",
    "crew_return": "Launch a crew vehicle to Mun orbit (rendezvous with the lander), then return "
    "to Kerbin and recover.",
}

_DEFAULT_MODEL = os.environ.get("ASTRA_MODEL", "claude-opus-4-8")
_API_URL = "https://api.anthropic.com/v1/messages"


@dataclass(slots=True)
class MissionPlan:
    command: str
    target_body: str
    capabilities: list[str]
    mission: MissionSpec
    source: str = "heuristic"  # "llm" | "heuristic"
    notes: str = ""
    rationale: str = ""
    extra: dict = field(default_factory=dict)


class Interpreter:
    def __init__(self, *, model: str | None = None, allow_llm: bool = True):
        self.model = model or _DEFAULT_MODEL
        self.allow_llm = allow_llm
        self.planner = MissionPlanner()

    # ----- public -----
    def interpret(self, command: str) -> MissionPlan:
        plan = None
        if self.allow_llm and os.environ.get("ANTHROPIC_API_KEY"):
            try:
                plan = self._interpret_llm(command)
            except Exception:  # network/parse/key issues -> graceful fallback
                plan = None
        if plan is None:
            plan = self._interpret_heuristic(command)
        return plan

    # ----- heuristic -----
    def _interpret_heuristic(self, command: str) -> MissionPlan:
        text = command.lower()
        body = "Mun" if ("mun" in text or "moon" in text or "luna" in text) else "Mun"
        caps: list[str] = []
        full = any(k in text for k in ("artemis", "everything", "full mission", "whole"))
        wants_relay = full or any(k in text for k in ("relay", "comsat", "satellite", "signal", "comm"))
        wants_land = full or any(
            k in text for k in ("land", "lander", "hls", "surface", "touchdown", "descent")
        )
        wants_crew = full or any(
            k in text
            for k in ("crew", "astronaut", "orion", "return", "bring", "home", "recover", "round trip")
        )
        if wants_relay:
            caps.append("relay")
        if wants_land:
            caps.append("hls_land_return")
        if wants_crew:
            caps.append("crew_return")
        if not caps:
            # Bare "go to the Mun" -> at least put a relay up (the safest proven capability).
            caps = ["relay"]
        mission = self.planner.interpret(command)
        return MissionPlan(
            command=command,
            target_body=body,
            capabilities=caps,
            mission=mission,
            source="heuristic",
            notes="Heuristic interpretation (no ANTHROPIC_API_KEY, or LLM unavailable).",
        )

    # ----- LLM -----
    def _interpret_llm(self, command: str) -> MissionPlan:
        system = (
            "You are the mission interpreter for ASTRA, an agent that flies Kerbal Space Program 1 "
            "missions. Map the user's one-line goal to an ordered list of capabilities from this "
            "EXACT set and nothing else: " + json.dumps(KNOWN_CAPABILITIES) + ". Respond with ONLY "
            "a JSON object: {\"target_body\": str, \"capabilities\": [str, ...], \"rationale\": str}. "
            "Order capabilities in flight order (relay first, then hls_land_return, then "
            "crew_return). Only include capabilities the goal actually needs."
        )
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": 600,
                "system": system,
                "messages": [{"role": "user", "content": command}],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            _API_URL,
            data=body,
            headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=40) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        text = "".join(
            blk.get("text", "") for blk in payload.get("content", []) if blk.get("type") == "text"
        ).strip()
        data = json.loads(_extract_json(text))
        caps = [c for c in data.get("capabilities", []) if c in KNOWN_CAPABILITIES]
        if not caps:
            raise ValueError("LLM returned no known capabilities")
        mission = self.planner.interpret(command)
        return MissionPlan(
            command=command,
            target_body=str(data.get("target_body") or "Mun"),
            capabilities=caps,
            mission=mission,
            source="llm",
            rationale=str(data.get("rationale") or ""),
            notes=f"Interpreted by {self.model}.",
        )


def _extract_json(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object in model output: {text[:120]!r}")
    return text[start : end + 1]
