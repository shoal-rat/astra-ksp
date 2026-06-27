"""Tests for the RIGOROUS ASTRA mission graph + plan validator.

These run fully OFFLINE (no kRPC, no ANTHROPIC_API_KEY): the graph builds from the closed-form helpers
in astro.py + bodies.py, and the validator reasons purely over the symbolic state it threads. They
assert the user's directive — a complete mission graph that verifies preconditions, postconditions,
resource budgets, body consistency, the presence of a return segment, and computed transfer windows,
and that a rejected plan is NOT silently trimmed but reported with specific errors.

Required cases (per the build spec):
  * a valid Mun relay plan PASSES;
  * a plan with `land` before `transfer` FAILS (precondition chaining);
  * a crewed "bring them home" plan missing a return `recover` FAILS (return segment);
  * a body-mismatch plan FAILS (body consistency);
  * a resource-over-budget plan FAILS (resource budget);
  * a transfer node carries a computed window + Δv (per-step math).
"""
from __future__ import annotations

import math

import pytest

from ksp_lab.astra.mission_graph import (
    MISSION_RESERVE_FRAC,
    Situation,
    build_mission_graph,
)
from ksp_lab.astra.plan_validator import ValidationReport, validate_plan


# --------------------------------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------------------------------- #
def _step(primitive, **args):
    return {"primitive": primitive, "args": args}


def _mun_relay_steps():
    return [
        _step("launch", target_alt_km=100.0),
        _step("transfer", target_body="Mun", capture_mode="circular"),
        _step("commission_relay"),
    ]


def _duna_round_trip_steps(crew=1):
    return [
        _step("launch", crew=crew, heatshield=True, chutes=True),
        _step("transfer", target_body="Duna", capture_mode="aerocapture"),
        _step("land"),
        _step("plant_flag"),
        _step("ascend", target_alt_km=30.0),
        _step("transfer", target_body="Kerbin", capture_mode="aerocapture"),
        _step("recover"),
    ]


# --------------------------------------------------------------------------------------------------- #
# REQUIRED: a valid Mun relay plan passes.
# --------------------------------------------------------------------------------------------------- #
def test_valid_mun_relay_passes():
    graph = build_mission_graph(_mun_relay_steps())
    report = validate_plan(graph, command="put a communications satellite in high Mun orbit")
    assert isinstance(report, ValidationReport)
    assert report.ok, f"valid Mun relay rejected: {report.errors}"
    assert report.errors == []
    # No return is implied for a bare relay (no crew, no 'return' phrasing).
    assert report.implies_return is False
    # The graph threads state: launch -> orbit Kerbin, transfer -> orbit Mun.
    assert graph.nodes[0].state_out.situation == Situation.ORBIT
    assert graph.nodes[1].state_out.body == "Mun"


# --------------------------------------------------------------------------------------------------- #
# REQUIRED: a transfer node carries a computed window + Δv (per-step math).
# --------------------------------------------------------------------------------------------------- #
def test_mun_transfer_node_has_computed_dv_and_window():
    graph = build_mission_graph(_mun_relay_steps())
    transfer = next(n for n in graph.nodes if n.primitive == "transfer")
    # The Mun transfer Δv is a CALCULATED Hohmann ejection+capture (~1.1-1.3 km/s), not zero/magic.
    assert 800.0 < transfer.dv_mps < 1600.0, transfer.dv_mps
    # A moon transfer carries a finite, positive time of flight (the phase-angle window).
    assert transfer.tof_s is not None and math.isfinite(transfer.tof_s) and transfer.tof_s > 0
    assert "Hohmann" in transfer.calc
    # The launch node's ascent Δv is the canonical ~3.3 km/s Kerbin ascent (calculated, not magic).
    launch = graph.nodes[0]
    assert 3000.0 < launch.dv_mps < 3700.0, launch.dv_mps


def test_duna_planet_transfer_node_carries_window_and_dv():
    # A Sun-to-Sun (planet) transfer: offline it carries the CALCULATED ejection+capture Δv, the
    # time of flight, and a wait bound (the synodic period). The window math is present even offline.
    graph = build_mission_graph(_duna_round_trip_steps(crew=0))
    duna = next(n for n in graph.nodes if n.primitive == "transfer" and n.target_body == "Duna")
    # Kerbin->Duna ejection ~1.1 km/s + capture. With AEROCAPTURE (the round-trip steps arrive behind a
    # heat shield) the capture is a cheap post-aerobrake circularization (~120 m/s), so the leg is ~1.1-1.3
    # km/s; a fully propulsive capture would be ~1.6-1.7 km/s. Either way it is a real CALCULATED Δv.
    assert 1000.0 < duna.dv_mps < 2600.0, duna.dv_mps
    assert duna.tof_s is not None and duna.tof_s > 100 * 21600  # > 100 Kerbin-days
    assert duna.wait_s is not None and duna.wait_s > 0          # the synodic wait bound
    assert "planet transfer" in duna.calc


# --------------------------------------------------------------------------------------------------- #
# REQUIRED: `land` before `transfer` fails (precondition chaining).
# --------------------------------------------------------------------------------------------------- #
def test_land_before_transfer_fails_precondition():
    steps = [
        _step("launch", target_alt_km=100.0),
        _step("land"),                                  # cannot land — never transferred to a body
        _step("transfer", target_body="Duna"),
    ]
    graph = build_mission_graph(steps)
    report = validate_plan(graph, command="land somewhere and go to Duna")
    assert not report.ok
    # The specific failure is named: the transfer after a landing is the precondition break (it needs
    # to be IN ORBIT, but the prior land left it on the surface). land itself is also flagged-adjacent.
    joined = " | ".join(report.errors)
    assert "precondition unmet" in joined
    assert any("transfer" in e and "orbit" in e for e in report.errors)


def test_plant_flag_before_land_fails_precondition():
    steps = [
        _step("launch", crew=1),
        _step("transfer", target_body="Mun"),
        _step("plant_flag"),                            # not landed yet
    ]
    report = validate_plan(build_mission_graph(steps), command="flag on the Mun")
    assert not report.ok
    assert any("plant_flag" in e and "LANDED" in e.upper() for e in report.errors)


def test_ascend_before_land_fails_precondition():
    steps = [
        _step("launch", crew=1),
        _step("transfer", target_body="Mun"),
        _step("ascend"),                                # cannot ascend — in orbit, not landed
    ]
    report = validate_plan(build_mission_graph(steps), command="go to the Mun")
    assert not report.ok
    assert any("ascend" in e and "landed" in e.lower() for e in report.errors)


# --------------------------------------------------------------------------------------------------- #
# REQUIRED: a crewed "bring them home" plan missing a return recover fails (return segment).
# --------------------------------------------------------------------------------------------------- #
def test_crewed_bring_home_missing_recover_fails():
    steps = [
        _step("launch", crew=1, heatshield=True, chutes=True),
        _step("transfer", target_body="Duna", capture_mode="aerocapture"),
        _step("land"),
        _step("plant_flag"),
        # NO ascend, NO transfer home, NO recover — the crew is stranded on Duna.
    ]
    graph = build_mission_graph(steps)
    report = validate_plan(graph, command="send a kerbal to Duna and bring them home")
    assert not report.ok
    assert report.implies_return is True
    assert any("return segment" in e.lower() and "strand" in e.lower() for e in report.errors)


def test_crewed_round_trip_with_recover_passes():
    # The complete crewed round trip (ascend -> transfer home -> recover) ends landed at Kerbin: valid.
    # Give it a generous vehicle Δv so the budget rule does not block (round trips are expensive).
    graph = build_mission_graph(_duna_round_trip_steps(crew=1), vehicle_dv=20000.0)
    report = validate_plan(graph, command="send a kerbal to Duna, plant a flag, bring them home")
    assert report.ok, f"valid round trip rejected: {report.errors}"
    assert report.implies_return is True
    assert graph.final_state.body == "Kerbin"
    assert graph.final_state.situation == Situation.LANDED


def test_implicit_return_for_crew_even_without_return_phrasing():
    # A crewed mission that LEAVES the home body implies a return even if the words 'home/return' are
    # absent: stranding kerbals is never the intent. Missing recover -> rejected.
    steps = [
        _step("launch", crew=2),
        _step("transfer", target_body="Duna"),
        _step("land"),
    ]
    report = validate_plan(build_mission_graph(steps), command="put two kerbals on Duna")
    assert report.implies_return is True
    assert not report.ok


def test_uncrewed_probe_one_way_is_fine():
    # An UNCREWED probe landing has no implied return (a probe is expendable): a one-way plan validates.
    steps = [
        _step("launch", crew=0),
        _step("transfer", target_body="Duna", capture_mode="aerocapture"),
        _step("land"),
    ]
    report = validate_plan(build_mission_graph(steps), command="land a probe on Duna")
    assert report.implies_return is False
    assert report.ok, report.errors


# --------------------------------------------------------------------------------------------------- #
# REQUIRED: a body-mismatch plan fails (body consistency).
# --------------------------------------------------------------------------------------------------- #
def test_transfer_to_unknown_body_fails():
    steps = [
        _step("launch"),
        _step("transfer", target_body="Tatooine"),      # not a real body in bodies.py
    ]
    report = validate_plan(build_mission_graph(steps), command="go to Tatooine")
    assert not report.ok
    assert any("not a real body" in e for e in report.errors)


def test_on_body_action_body_mismatch_fails():
    # The chain arrives at Duna, but the land step explicitly names Mun: a body-consistency violation.
    steps = [
        _step("launch"),
        _step("transfer", target_body="Duna"),
        _step("land", target_body="Mun"),               # disagrees with the chain (at Duna)
    ]
    report = validate_plan(build_mission_graph(steps), command="land on Duna")
    assert not report.ok
    assert any("body mismatch" in e for e in report.errors)


def test_transfer_to_current_body_fails():
    # Transferring to the body you are already at is a contradiction.
    steps = [
        _step("launch"),
        _step("transfer", target_body="Kerbin"),        # already at Kerbin
    ]
    report = validate_plan(build_mission_graph(steps), command="transfer to Kerbin")
    assert not report.ok
    assert any("already at" in e for e in report.errors)


# --------------------------------------------------------------------------------------------------- #
# REQUIRED: a resource-over-budget plan fails (resource budget).
# --------------------------------------------------------------------------------------------------- #
def test_over_budget_fails():
    steps = _duna_round_trip_steps(crew=1)
    graph = build_mission_graph(steps, vehicle_dv=3000.0)   # far too little for a Duna round trip
    report = validate_plan(graph, command="send a kerbal to Duna and bring them home")
    assert not report.ok
    assert any("resource budget exceeded" in e for e in report.errors)
    # The error states exactly how short the vehicle is.
    assert any("short by" in e for e in report.errors)


def test_unknown_vehicle_dv_warns_not_errors():
    # With no vehicle Δv supplied, the budget is a WARNING that sizes the launch — not a blocking error.
    graph = build_mission_graph(_mun_relay_steps())        # vehicle_dv=None
    report = validate_plan(graph, command="put a comsat in high Mun orbit")
    assert report.ok
    assert any("size the launch" in w for w in report.warnings)
    # The required Δv carries the mission reserve.
    expected = graph.total_dv * (1.0 + MISSION_RESERVE_FRAC)
    assert report.required_dv == pytest.approx(expected)


def test_budget_passes_when_vehicle_dv_sufficient():
    graph = build_mission_graph(_mun_relay_steps(), vehicle_dv=8000.0)
    report = validate_plan(graph, command="put a comsat in high Mun orbit")
    assert report.ok, report.errors
    assert report.warnings == [] or all("size the launch" not in w for w in report.warnings)


# --------------------------------------------------------------------------------------------------- #
# WINDOW SANITY + graph structure.
# --------------------------------------------------------------------------------------------------- #
def test_unknown_primitive_is_reported_not_crashed():
    steps = [_step("launch"), _step("teleport", target_body="Jool")]
    graph = build_mission_graph(steps)                     # must not raise
    report = validate_plan(graph, command="teleport to Jool")
    assert not report.ok
    assert any("unknown primitive" in e for e in report.errors)


def test_graph_renders_and_edges_are_linear():
    graph = build_mission_graph(_mun_relay_steps())
    text = graph.render()
    assert "MISSION GRAPH" in text
    assert "TOTAL" in text
    # 3 nodes -> 2 directed edges (a linear state chain).
    assert graph.edges() == [(1, 2), (2, 3)]


def test_total_dv_is_sum_of_node_dv():
    graph = build_mission_graph(_duna_round_trip_steps(crew=1))
    assert graph.total_dv == pytest.approx(sum(n.dv_mps for n in graph.nodes))


def test_empty_plan_validates_trivially_but_has_no_steps():
    graph = build_mission_graph([])
    report = validate_plan(graph, command="")
    assert graph.nodes == []
    assert report.ok  # nothing to violate (the agent separately requires non-empty steps from the LLM)
