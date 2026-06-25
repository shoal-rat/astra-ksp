"""Natural-language -> a DECOMPOSED mission plan, ALWAYS by the Claude LLM mission-architect.

ASTRA accepts one line of plain English and breaks it into an ORDERED list of atomic, body-agnostic
PRIMITIVES (see primitives.py). The interpreter forces a task DECOMPOSITION via Claude: the LLM is shown
the primitive CATALOG (names + descriptions + param schemas) plus the body constants + calculation
helpers (and, on a live run, the live universe state) and returns
``{"steps": [{"primitive": ..., "args": {...}}, ...], "rationale": ...}``.

There is NO offline/heuristic fallback. ``interpret()`` REQUIRES ``ANTHROPIC_API_KEY`` and a working
Claude call; if the key is unset or the call/parse fails, it RAISES rather than silently degrading to a
keyword guesser. The user's directive: "fully leverage the capabilities of Claude — no superfluous
offline fallback." A ``--dry-run`` still calls the LLM to PLAN; it just doesn't fly.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field

from ..mission import MissionPlanner
from ..models import MissionSpec
from . import primitives
from . import planning_context as _pc

_DEFAULT_MODEL = os.environ.get("ASTRA_MODEL", "claude-opus-4-8")
_API_URL = "https://api.anthropic.com/v1/messages"


class LLMUnavailableError(RuntimeError):
    """Raised when ASTRA cannot reach the Claude mission-architect (no key, or the call/parse failed).
    ASTRA has NO offline fallback by design — surfacing this loudly is the intended behaviour."""


@dataclass(slots=True)
class MissionPlan:
    command: str
    target_body: str
    steps: list[dict]                      # ordered [{"primitive": str, "args": {...}}, ...]
    mission: MissionSpec
    source: str = "llm"                    # always "llm" — the only planner ASTRA has
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
    def __init__(self, *, model: str | None = None):
        self.model = model or _DEFAULT_MODEL
        self.planner = MissionPlanner()

    # ----- public -----
    def interpret(self, command: str, planning_ctx: dict | None = None) -> MissionPlan:
        """Decompose+plan a command with the Claude mission-architect. ``planning_ctx`` (from
        planning_context.build_planning_context) lets the LLM reason over the LIVE universe state
        (vessel orbits, resources) — the agent passes it for a live run; for a dry-run it's None and the
        LLM plans from the static (bodies+catalog) context.

        HARD-REQUIRES the LLM: if ANTHROPIC_API_KEY is unset, raises immediately; if the Claude call or
        its parse fails, the error propagates (wrapped in LLMUnavailableError). There is NO offline
        fallback — failures are surfaced, never masked."""
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMUnavailableError(
                "ASTRA requires ANTHROPIC_API_KEY — no offline fallback. Set the key so the Claude "
                "mission-architect can decompose the command."
            )
        try:
            return self._interpret_llm(command, planning_ctx)
        except LLMUnavailableError:
            raise
        except Exception as exc:  # network / parse / empty-plan -> fail loudly, do NOT silently degrade
            raise LLMUnavailableError(
                f"ASTRA mission-architect failed for {command!r}: {exc} — no offline fallback."
            ) from exc

    # ----- body parsing (a small default-only helper for when the LLM omits target_body) -----
    @staticmethod
    def _default_target_body(text: str) -> str:
        """Best-effort destination body when the LLM did not return an explicit ``target_body`` (it almost
        always does). Scans the text for any catalogue body name (longest first, so 'Mun' isn't shadowed by
        'Minmus'); defaults to the launch body Kerbin if none is named."""
        from ..bodies import _REGISTRY

        t = text.lower()
        names = sorted((b.name for b in _REGISTRY.values() if b.name not in ("Sun",)),
                       key=len, reverse=True)
        for name in names:
            if name.lower() in t:
                return name
        aliases = {"moon": "Mun", "luna": "Mun", "mars": "Duna", "venus": "Eve"}
        for alias, real in aliases.items():
            if alias in t:
                return real
        return "Kerbin"

    # ----- LLM mission ARCHITECT -----
    def _build_system_prompt(self, planning_ctx: dict) -> str:
        """The MISSION-ARCHITECT system prompt: Claude is a reasoning flight planner, not a word-guesser.
        It is given the full planning context (catalog + body constants + calculation helpers + any live
        state) and told to DECOMPOSE the goal, REASON about each step's parameters (target/altitude/window/
        capture mode), and emit strict JSON with a per-step rationale + a mission rationale."""
        context_text = _pc.render_context_text(planning_ctx)
        return (
            "You are ASTRA's MISSION ARCHITECT — a careful spaceflight reasoning agent flying Kerbal "
            "Space Program 1. You are NOT a phrase-matcher: you are Claude, with real orbital-mechanics "
            "comprehension. Given ONE line of plain English, design the whole mission.\n\n"
            "DO THREE THINGS, in this order, thinking like a flight director:\n"
            "(a) DECOMPOSE the goal into an ORDERED list of atomic PRIMITIVE steps from the catalog. "
            "Flight order matters: launch -> (interplanetary transfer) -> on-body actions (land, "
            "plant_flag, ascend) -> (transfer home) -> recover. You have LEEWAY to sequence creatively "
            "for NOVEL multi-leg missions: a Moho 'loop near the Sun' (chain Kerbin->Eve gravity assist "
            "->Moho), a grand tour of several bodies, a constellation of relays, a refuel-depot pattern. "
            "Invent the right sequence — do not force a goal into a canned 3-step template.\n"
            "(b) REASON about each step's PARAMETERS and CALCULATE them from the body constants and the "
            "calculation helpers, not from guesswork. Specifically:\n"
            "   - TARGET / ALTITUDE: read the destination body from the text. Parse altitude words to "
            "real numbers using the BODIES table: 'synchronous'/'stationary'/'keostationary' -> that "
            "body's synchronous_alt_km; 'low orbit' -> low_orbit_alt_km; 'high orbit' -> a high but "
            "sub-SOI altitude. Put the number in capture_alt_km / target_alt_km.\n"
            "   - CAPTURE MODE: 'circular' for a relay/station that must hold a precise orbit; "
            "'aerocapture' to arrive cheaply at a body WITH an atmosphere when you will land or recover; "
            "'loose' for a cheap bound ellipse (flybys, fuel-limited captures).\n"
            "   - WINDOW / TIMING: for any Sun-to-Sun transfer (Kerbin<->Duna/Eve/Moho/etc.) note in the "
            "step reasoning that the agent must call transfer_planner.find_transfer_window(dep,tgt) to "
            "get ut_dep, then WAIT ON THE GROUND / in parking orbit and time-warp (e.g. 1000x) to that "
            "departure UT before the ejection burn. Give a rough sense of how long the wait is "
            "(a fraction of the synodic period). Moon transfers (Kerbin->Mun/Minmus) need only phasing, "
            "not a heliocentric window.\n"
            "   - LAUNCH PROFILE: set crew>0 for crewed goals; heatshield + chutes for re-entry/return; "
            "radial_boosters and max_core_engines for heavy interplanetary or crewed uppers.\n"
            "   - DOCKING / RETURN: add rendezvous+dock for assembly/refuel; a round trip needs a "
            "transfer back to Kerbin then recover.\n"
            "(c) For each step add an 'args.notes' string with the short CALCULATION you used (e.g. "
            "\"Duna sync alt = 2880 km from synchronous_altitude_m\", or \"wait ~0.4*synodic for the "
            "next Kerbin->Duna window, warp 1000x to ut_dep\").\n\n"
            "You may ONLY use primitive names and arg names that appear in the catalog. Pick the target "
            "body from any body in the BODIES table; the launch body is Kerbin.\n\n"
            "================ PLANNING CONTEXT ================\n" + context_text + "\n"
            "=================================================\n\n"
            "Respond with ONLY a strict JSON object, no prose around it:\n"
            "{\n"
            '  "target_body": "<primary destination>",\n'
            '  "steps": [ {"primitive": "<name>", "args": { ... , "notes": "<calc>"}, '
            '"reasoning": "<why this step, what you computed>"}, ... ],\n'
            '  "mission_rationale": "<one-paragraph plan overview>",\n'
            '  "open_questions": ["<assumptions or things you could not fully resolve>", ...]\n'
            "}"
        )

    def _interpret_llm(self, command: str, planning_ctx: dict | None = None) -> MissionPlan:
        # Offline-of-flight planning briefing (catalog + body constants + calc helpers). A live caller that
        # already holds a kRPC handle passes the live variant; otherwise we plan from the static one.
        if planning_ctx is None:
            planning_ctx = _pc.build_planning_context_static(command)
        system = self._build_system_prompt(planning_ctx)
        text = self._call_llm(system, command)
        data = json.loads(_extract_json(text))
        steps = _validate_steps(data.get("steps", []))
        if not steps:
            raise ValueError("LLM returned no valid primitive steps")
        mission = self.planner.interpret(command)
        target = str(data.get("target_body") or self._default_target_body(command.lower()))
        # Accept either the new "mission_rationale" or the legacy "rationale" key.
        rationale = str(data.get("mission_rationale") or data.get("rationale") or "")
        open_qs = data.get("open_questions") or []
        extra = {"open_questions": open_qs} if open_qs else {}
        return MissionPlan(
            command=command,
            target_body=target,
            steps=steps,
            mission=mission,
            source="llm",
            rationale=rationale,
            notes=f"Architected by {self.model}.",
            extra=extra,
        )

    def _call_llm(self, system: str, command: str) -> str:
        """POST the architect prompt to the Anthropic Messages API; return the concatenated text.
        Raised to 4000 max_tokens so the model has room to REASON per step. Network/parse errors
        propagate to interpret(), which wraps them in LLMUnavailableError and FAILS — no fallback."""
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": 4000,
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return "".join(
            blk.get("text", "") for blk in payload.get("content", []) if blk.get("type") == "text"
        ).strip()


def _validate_steps(raw: list) -> list[dict]:
    """Robustly turn a raw LLM 'steps' list into executable steps.

    - Keep only steps whose primitive EXISTS in the catalog (drop/repair unknown ones rather than
      crashing — an unknown primitive would explode the executor).
    - Coerce args to a dict; preserve the per-step ``reasoning`` so the report can show the LLM's
      thinking. The executor only reads step['primitive'] / step['args'], so extra keys are inert.
    - Lift the architect's free-text ``args.notes`` calculation OUT of the executable args (the live
      primitives take no ``notes`` kwarg and would raise) onto a step-level ``notes`` field, and DROP
      any other arg name that is not a real parameter of that primitive (repair hallucinated args).
    """
    steps: list[dict] = []
    for s in raw or []:
        if not isinstance(s, dict):
            continue
        name = s.get("primitive")
        if name not in primitives.CATALOG:
            continue  # repair: silently drop an unknown/hallucinated primitive
        raw_args = s.get("args") or {}
        if not isinstance(raw_args, dict):
            raw_args = {}
        notes = raw_args.get("notes")
        valid_params = set(primitives.CATALOG[name].params)
        args = {k: v for k, v in raw_args.items() if k in valid_params}
        step: dict = {"primitive": name, "args": args}
        if notes:
            step["notes"] = str(notes)
        reasoning = s.get("reasoning")
        if reasoning:
            step["reasoning"] = str(reasoning)
        steps.append(step)
    return steps


def _extract_json(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object in model output: {text[:120]!r}")
    return text[start : end + 1]
