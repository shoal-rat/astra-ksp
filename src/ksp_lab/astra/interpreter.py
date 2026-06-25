"""Natural-language -> a DECOMPOSED mission plan.

ASTRA accepts one line of plain English and breaks it into an ORDERED list of atomic, body-agnostic
PRIMITIVES (see primitives.py). This is the redesign's core: instead of mapping a command to one of three
coarse, MUN-hardcoded bundles, the interpreter forces a task DECOMPOSITION.

  * When ANTHROPIC_API_KEY is set, the LLM is shown the primitive CATALOG (names + descriptions + param
    schemas) and returns ``{"steps": [{"primitive": ..., "args": {...}}, ...], "rationale": ...}``.
  * Otherwise a DETERMINISTIC, BODY-AGNOSTIC heuristic decomposer parses the target body from ANY body
    name in bodies.py and the crew/flag/relay/land/return keywords, and emits a sensible primitive
    sequence. No ``body="Mun"`` default — the body is inferred from the text.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field

from ..bodies import KERBIN, _REGISTRY
from ..mission import MissionPlanner
from ..models import MissionSpec
from . import primitives

_DEFAULT_MODEL = os.environ.get("ASTRA_MODEL", "claude-opus-4-8")
_API_URL = "https://api.anthropic.com/v1/messages"


@dataclass(slots=True)
class MissionPlan:
    command: str
    target_body: str
    steps: list[dict]                      # ordered [{"primitive": str, "args": {...}}, ...]
    mission: MissionSpec
    source: str = "heuristic"              # "llm" | "heuristic"
    notes: str = ""
    rationale: str = ""
    extra: dict = field(default_factory=dict)

    def step_summary(self) -> str:
        return " -> ".join(
            f"{s['primitive']}({_fmt_args(s.get('args', {}))})" for s in self.steps
        )


def _fmt_args(args: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in args.items() if v not in (None, "", False, 0))


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

    # ----- body parsing (body-agnostic, shared) -----
    @staticmethod
    def _parse_target_body(text: str) -> str | None:
        """Find a destination body by scanning the text for ANY catalogue body name (longest first, so
        'Mun' isn't shadowed by 'Minmus' etc.). Returns None if no body is named (caller decides default)."""
        t = text.lower()
        # Real catalogue body names take precedence (longest first so 'Mun' isn't shadowed by 'Minmus').
        # 'home'/'back'/'return' are RETURN cues, not destinations, so they are NOT aliases here.
        names = sorted((b.name for b in _REGISTRY.values() if b.name not in ("Sun",)),
                       key=len, reverse=True)
        for name in names:
            if name.lower() in t:
                return name
        # Common nicknames, only if no catalogue name appeared.
        aliases = {"moon": "Mun", "luna": "Mun", "mars": "Duna", "venus": "Eve"}
        for alias, real in aliases.items():
            if alias in t:
                return real
        return None

    # ----- heuristic decomposer (body-agnostic) -----
    def _interpret_heuristic(self, command: str) -> MissionPlan:
        text = command.lower()
        target = self._parse_target_body(text)            # None if unspecified
        from ..bodies import body as _lookup, parent_of

        wants_relay = any(k in text for k in ("relay", "comsat", "satellite", "signal", "comm", "network"))
        wants_flag = any(k in text for k in ("flag", "plant"))
        wants_land = wants_flag or any(
            k in text for k in ("land", "lander", "surface", "touchdown", "descent", "rover", "probe on"))
        wants_crew = any(
            k in text for k in ("crew", "astronaut", "kerbal", "man", "manned", "people", "person", "pilot"))
        wants_return = any(
            k in text for k in ("return", "bring", "home", "recover", "round trip", "round-trip", "back"))
        wants_dock = any(k in text for k in ("dock", "rendezvous", "berth"))

        steps: list[dict] = []
        crew = 1 if (wants_crew or wants_flag) else 0
        # An interplanetary or crewed-return mission needs heat shield + chutes + a heavy upper.
        interplanetary = bool(target) and parent_of(_lookup(target)).name == "Sun" and target != "Kerbin"
        heavy = interplanetary or wants_return or wants_crew
        name = f"AI-{(target or 'Kerbin')}-{'Crew' if crew else ('Relay' if wants_relay else 'Craft')}"

        # 1) Launch to a parking orbit of the LAUNCH body (Kerbin in stock).
        steps.append({"primitive": "launch", "args": _prune({
            "target_alt_km": 100.0,
            "crew": crew,
            "heatshield": heavy,
            "chutes": wants_return or wants_land or crew,
            "radial_boosters": 4 if heavy else 0,
            "name": name,
        })})

        # 2) Transfer to the target body (if any named, and it isn't the launch body).
        if target and target != "Kerbin":
            tb = _lookup(target)
            mode = "circular" if wants_relay else "loose"
            # If we will land on a body with atmosphere, aerocapture is the cheap arrival.
            if wants_land and tb.atmosphere_top_m > 0 and not wants_relay:
                mode = "aerocapture"
            steps.append({"primitive": "transfer", "args": _prune({
                "target_body": target, "capture_mode": mode,
            })})

        # 3) On-body actions in flight order: land -> (flag) -> ascend(if returning) -> recover.
        landing_body = target or "Kerbin"
        if wants_relay and not wants_land:
            steps.append({"primitive": "commission_relay", "args": {}})
        if wants_land:
            steps.append({"primitive": "land", "args": {}})
            if wants_flag:
                steps.append({"primitive": "plant_flag", "args": {}})
            if wants_return:
                steps.append({"primitive": "ascend", "args": {"target_alt_km": 30.0}})
        if wants_dock:
            # A rendezvous+dock needs a named target; left generic for the heuristic (LLM fills the name).
            steps.append({"primitive": "rendezvous", "args": {"target_name": ""}})
            steps.append({"primitive": "dock", "args": {"target_name": ""}})
        if wants_return:
            # Transfer home from an interplanetary/moon body before recovering.
            if target and target != "Kerbin":
                steps.append({"primitive": "transfer", "args": {"target_body": "Kerbin",
                                                                "capture_mode": "aerocapture"}})
            steps.append({"primitive": "recover", "args": {}})

        if not steps:
            steps = [{"primitive": "launch", "args": {"target_alt_km": 100.0, "name": name}}]

        mission = self.planner.interpret(command)
        return MissionPlan(
            command=command,
            target_body=target or "Kerbin",
            steps=steps,
            mission=mission,
            source="heuristic",
            notes="Heuristic decomposition (no ANTHROPIC_API_KEY, or LLM unavailable). Body-agnostic.",
            rationale=f"Inferred target={target or 'Kerbin (launch body)'}, crew={crew}, "
                      f"relay={wants_relay}, land={wants_land}, flag={wants_flag}, return={wants_return}.",
        )

    # ----- LLM decomposer -----
    def _interpret_llm(self, command: str) -> MissionPlan:
        catalog = primitives.catalog_for_prompt()
        bodies = [b.name for b in _REGISTRY.values()]
        system = (
            "You are the mission DECOMPOSER for ASTRA, a general agent that flies Kerbal Space Program 1. "
            "Break the user's one-line goal into an ORDERED list of atomic PRIMITIVE calls from the catalog "
            "below. Be BODY-AGNOSTIC: read the destination from the text (any of these bodies: "
            + ", ".join(bodies) + "; the launch body is Kerbin). Flight order matters: launch, then "
            "transfer to the target, then on-body actions (land, plant_flag, ascend), then transfer home + "
            "recover for a round trip. Use crew>0 and heatshield/chutes/radial_boosters for crewed or "
            "interplanetary missions.\n\nPRIMITIVE CATALOG (JSON):\n" + json.dumps(catalog, ensure_ascii=False)
            + "\n\nRespond with ONLY a JSON object: "
            "{\"target_body\": str, \"steps\": [{\"primitive\": str, \"args\": {..}}, ...], "
            "\"rationale\": str}. Use only primitive names and arg names from the catalog."
        )
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": 1200,
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        text = "".join(
            blk.get("text", "") for blk in payload.get("content", []) if blk.get("type") == "text"
        ).strip()
        data = json.loads(_extract_json(text))
        steps = _validate_steps(data.get("steps", []))
        if not steps:
            raise ValueError("LLM returned no valid primitive steps")
        mission = self.planner.interpret(command)
        target = str(data.get("target_body") or self._parse_target_body(command.lower()) or "Kerbin")
        return MissionPlan(
            command=command,
            target_body=target,
            steps=steps,
            mission=mission,
            source="llm",
            rationale=str(data.get("rationale") or ""),
            notes=f"Decomposed by {self.model}.",
        )


def _prune(args: dict) -> dict:
    """Drop falsy/default args so the dry-run summary stays readable (keep explicit numeric alts)."""
    out = {}
    for k, v in args.items():
        if k in ("target_alt_km",) or v not in (None, "", False, 0):
            out[k] = v
    return out


def _validate_steps(raw: list) -> list[dict]:
    """Keep only steps whose primitive exists in the catalog; coerce args to a dict."""
    steps: list[dict] = []
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        name = s.get("primitive")
        if name not in primitives.CATALOG:
            continue
        args = s.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        steps.append({"primitive": name, "args": args})
    return steps


def _extract_json(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object in model output: {text[:120]!r}")
    return text[start : end + 1]
