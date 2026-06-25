"""Tests for the ASTRA LLM MISSION ARCHITECT (offline — no network).

These exercise the reasoning-planner path without ever touching kRPC or the Anthropic API:
  * the STATIC planning context carries the bodies + the primitive catalog + the calc-helper names,
  * a well-formed (stubbed) LLM JSON response parses into ORDERED steps that keep their reasoning,
  * an unknown-primitive / hallucinated-arg response is REPAIRED (bad steps/args dropped),
  * a fully-garbage or empty-steps response makes interpret() RAISE (no offline fallback),
  * "synchronous orbit around Duna" (stubbed response) yields a Duna synchronous-altitude arg.
"""
from __future__ import annotations

import json

import pytest

from ksp_lab.astra import planning_context as pc
from ksp_lab.astra.interpreter import Interpreter, LLMUnavailableError
from ksp_lab.bodies import DUNA, synchronous_altitude_m


def _stub_llm(monkeypatch, response_text: str):
    """Make the architect path run offline: set a dummy key + replace the network call with a stub."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(Interpreter, "_call_llm",
                        lambda self, system, command: response_text, raising=True)


# --------------------------------------------------------------------------- #
# Planning context                                                            #
# --------------------------------------------------------------------------- #
def test_static_planning_context_has_bodies_catalog_and_helpers():
    ctx = pc.build_planning_context_static("land on Duna")
    # bodies catalog: real constants, Sun excluded, Duna present with its constants
    body_names = {b["name"] for b in ctx["bodies"]}
    assert {"Kerbin", "Mun", "Duna", "Eve", "Gilly"} <= body_names
    assert "Sun" not in body_names
    duna = next(b for b in ctx["bodies"] if b["name"] == "Duna")
    assert duna["parent"] == "Sun"
    assert duna["mu"] > 0 and duna["radius_km"] > 0
    assert duna["atmosphere"]["top_km"] == 50.0          # Duna has an atmosphere
    assert "synchronous_alt_km" in duna                  # derived for the plan

    # primitive catalog: the real actions are advertised
    prim_names = {p["primitive"] for p in ctx["primitive_catalog"]}
    assert {"launch", "transfer", "land", "recover", "commission_relay"} <= prim_names

    # calculation helpers: the window-finder + sync-altitude helper are named
    helper_blob = " ".join(h["helper"] for h in ctx["calculation_helpers"])
    assert "find_transfer_window" in helper_blob
    assert "synchronous_altitude_m" in helper_blob

    # no live universe offline
    assert ctx["live"] == {}


def test_render_context_text_is_compact_and_complete():
    ctx = pc.build_planning_context_static("grand tour")
    text = pc.render_context_text(ctx)
    assert "PRIMITIVE / ACTION CATALOG" in text
    assert "BODIES" in text
    assert "CALCULATION HELPERS" in text
    assert "find_transfer_window" in text
    assert "Duna" in text


def test_live_planning_context_adds_universe_and_vessels():
    class _Orbit:
        def __init__(self):
            self.body = type("B", (), {"name": "Kerbin", "equatorial_radius": 600_000.0})()
            self.periapsis = 600_000.0 + 80_000.0
            self.apoapsis = 600_000.0 + 100_000.0
            self.inclination = 0.05

    class _Vessel:
        name = "AI-Probe"
        orbit = _Orbit()

    class _SC:
        ut = 12345.0
        vessels = [_Vessel()]

    ctx = pc.build_planning_context(conn=None, sc=_SC(), command="rendezvous")
    live = ctx["live"]
    assert live["universe_time_s"] == 12345.0
    v = live["vessels"][0]
    assert v["name"] == "AI-Probe" and v["body"] == "Kerbin"
    assert v["periapsis_km"] == 80.0 and v["apoapsis_km"] == 100.0


def test_live_planning_context_survives_broken_sc():
    class _Boom:
        @property
        def ut(self):
            raise RuntimeError("no connection")

        @property
        def vessels(self):
            raise RuntimeError("no connection")

    ctx = pc.build_planning_context(conn=None, sc=_Boom(), command="x")
    # degrades to empty live state rather than raising
    assert ctx["live"] == {}
    assert {b["name"] for b in ctx["bodies"]}  # static parts still present


# --------------------------------------------------------------------------- #
# Architect parsing                                                           #
# --------------------------------------------------------------------------- #
def test_architect_parses_ordered_steps_with_reasoning(monkeypatch):
    response = json.dumps({
        "target_body": "Duna",
        "steps": [
            {"primitive": "launch",
             "args": {"target_alt_km": 100.0, "crew": 0, "heatshield": True,
                      "notes": "park at 100 km LKO before the Duna window"},
             "reasoning": "Reach low Kerbin orbit to wait for the transfer window."},
            {"primitive": "transfer",
             "args": {"target_body": "Duna", "capture_mode": "aerocapture",
                      "notes": "find_transfer_window(Kerbin,Duna); warp 1000x to ut_dep"},
             "reasoning": "Heliocentric Hohmann to Duna at the next launch window."},
            {"primitive": "land", "args": {},
             "reasoning": "Touch down on Duna using the atmosphere."},
        ],
        "mission_rationale": "Launch, wait for the Kerbin->Duna window, aerocapture, land.",
        "open_questions": ["exact landing site not specified"],
    })
    _stub_llm(monkeypatch, response)

    plan = Interpreter().interpret("land a probe on Duna")
    assert plan.source == "llm"
    assert plan.target_body == "Duna"
    names = [s["primitive"] for s in plan.steps]
    assert names == ["launch", "transfer", "land"]                       # ORDER preserved
    # per-step reasoning preserved
    assert all("reasoning" in s for s in plan.steps)
    # the free-text calculation note is lifted OUT of executable args onto the step
    launch_step = plan.steps[0]
    assert "notes" not in launch_step["args"]                           # not an executable kwarg
    assert "100 km" in launch_step["notes"]
    # mission rationale + open questions surfaced
    assert "window" in plan.rationale
    assert plan.extra["open_questions"]


def test_architect_repairs_unknown_primitive_and_bad_args(monkeypatch):
    response = json.dumps({
        "target_body": "Mun",
        "steps": [
            {"primitive": "launch", "args": {"target_alt_km": 100.0, "bogus_arg": 7}},
            {"primitive": "warp_drive", "args": {}},          # not in the catalog -> dropped
            {"primitive": "transfer", "args": {"target_body": "Mun", "capture_mode": "circular"}},
            {"primitive": "commission_relay", "args": {}},
        ],
        "mission_rationale": "Relay around the Mun.",
    })
    _stub_llm(monkeypatch, response)

    plan = Interpreter().interpret("put a relay in Mun orbit")
    names = [s["primitive"] for s in plan.steps]
    assert "warp_drive" not in names                          # hallucinated primitive repaired away
    assert names == ["launch", "transfer", "commission_relay"]
    # hallucinated arg dropped, real arg kept
    assert "bogus_arg" not in plan.steps[0]["args"]
    assert plan.steps[0]["args"]["target_alt_km"] == 100.0


def test_architect_garbage_raises_no_fallback(monkeypatch):
    # No JSON at all -> _extract_json raises -> interpret() must RAISE (no offline heuristic fallback).
    _stub_llm(monkeypatch, "I think we should fly to the Mun, but here is no JSON.")
    with pytest.raises(LLMUnavailableError):
        Interpreter().interpret("land a probe on Duna")


def test_architect_empty_steps_raises_no_fallback(monkeypatch):
    # Valid JSON but every step is an unknown primitive -> no usable steps -> RAISE (no fallback).
    response = json.dumps({"target_body": "Mun",
                           "steps": [{"primitive": "teleport", "args": {}}],
                           "mission_rationale": "bad plan"})
    _stub_llm(monkeypatch, response)
    with pytest.raises(LLMUnavailableError):
        Interpreter().interpret("orbit the Mun")


def test_architect_synchronous_duna_altitude(monkeypatch):
    """The headline use-case: 'synchronous orbit around Duna' must produce the Duna synchronous
    altitude as the capture altitude (computed from the body constants, not guessed)."""
    duna_sync_km = round(synchronous_altitude_m(DUNA) / 1000.0, 1)
    response = json.dumps({
        "target_body": "Duna",
        "steps": [
            {"primitive": "launch", "args": {"target_alt_km": 100.0}},
            {"primitive": "transfer",
             "args": {"target_body": "Duna", "capture_mode": "circular",
                      "capture_alt_km": duna_sync_km,
                      "notes": f"Duna synchronous alt = {duna_sync_km} km from synchronous_altitude_m"},
             "reasoning": "A stationary relay must sit at Duna's synchronous altitude."},
        ],
        "mission_rationale": "Synchronous relay at Duna.",
    })
    _stub_llm(monkeypatch, response)

    plan = Interpreter().interpret("put a relay in a synchronous orbit around Duna")
    assert plan.source == "llm" and plan.target_body == "Duna"
    transfer = next(s for s in plan.steps if s["primitive"] == "transfer")
    assert transfer["args"]["capture_mode"] == "circular"
    assert transfer["args"]["capture_alt_km"] == duna_sync_km
    assert duna_sync_km > 2000.0   # sanity: Duna sync is a high orbit (~2.9 Mm)
