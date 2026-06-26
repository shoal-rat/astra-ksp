"""ASTRA RIGOROUS PLAN VALIDATOR — verify a mission graph before anything flies.

The user's directive, exactly: "After the LLM generates output, we shouldn't simply trim parameters.
Instead a complete MISSION GRAPH should verify preconditions, postconditions, resource budgets,
target-celestial-body consistency, and the presence of a return segment." This module is the
verification half. It takes the MissionGraph built by mission_graph.build_mission_graph and returns a
ValidationReport with SPECIFIC errors (never a silent trim): if a plan is invalid, the agent REJECTS it
rather than flying a quietly-patched version.

The rules, each returning concrete error strings:

  1. PRECONDITION CHAINING — walk the graph; each node's precondition must be satisfied by the world
     state produced by the predecessor's postcondition. Catches: land before transfer, plant_flag
     without being landed/crewed, dock/rendezvous without a vehicle in orbit, recover when not at the
     home body, ascend when not landed, transfer when not in orbit.

  2. BODY CONSISTENCY — every on-body action (land/ascend/plant_flag/walk_to/set_orbit) acts on the
     body the chain has actually arrived at; every transfer targets a REAL body in bodies.py and the
     transfer chain is reachable from the launch body (each transfer departs the body the previous leg
     left you at).

  3. RESOURCE BUDGET — sum the per-node Δv. If vehicle_dv is known, require Σdv*(1+reserve) <= vehicle_dv
     and ERROR if over. If unknown, WARN with the required Δv so the launch can be sized.

  4. RETURN SEGMENT — if the command implies bringing the crew/vehicle back (return/home/round trip, or
     simply crew aboard), require a path that ENDS at the home body with a recover (or aerocapture+land);
     ERROR if the plan strands the crew off-world.

  5. WINDOW SANITY — every transfer must carry a computed window/tof; flag impossible (negative/NaN/
     in-the-past) ones surfaced by the graph builder.

Pure / offline: it reads only the graph + the command string + bodies.py. No kRPC.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..bodies import _REGISTRY
from .mission_graph import (
    MISSION_RESERVE_FRAC,
    MissionGraph,
    Situation,
    WorldState,
)

# Phrases that signal the crew/vehicle must come BACK to the home body.
_RETURN_PHRASES = (
    "return", "round trip", "round-trip", "roundtrip", "bring them home", "bring them back",
    "bring it back", "bring her back", "come home", "back home", "back to kerbin", "come back",
    "and back", "there and back", "home safely", "safe return", "recover",
)

_REAL_BODIES = {name for name in _REGISTRY if name != "sun"}


@dataclass(slots=True)
class ValidationReport:
    """The result of validating a plan: ok plus the specific errors and warnings."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    required_dv: float = 0.0
    implies_return: bool = False

    def render(self) -> str:
        head = "VALID" if self.ok else "REJECTED"
        lines = [f"PLAN VALIDATION: {head} (required Δv {self.required_dv:.0f} m/s, "
                 f"return_required={self.implies_return})"]
        for e in self.errors:
            lines.append(f"  ERROR:   {e}")
        for w in self.warnings:
            lines.append(f"  WARNING: {w}")
        if self.ok and not self.warnings:
            lines.append("  (all preconditions chained, bodies consistent, budget + window sane)")
        return "\n".join(lines)


def command_implies_return(command: str, graph: MissionGraph) -> bool:
    """True if the mission must end back at the home body. Triggered by an explicit return phrase OR by
    crew being aboard and the plan leaving the home body (a crewed mission that goes somewhere implies the
    crew should come home — stranding kerbals is never the intent)."""
    text = (command or "").lower()
    if any(p in text for p in _RETURN_PHRASES):
        return True
    # Crewed + leaves the home body => implicit return expectation.
    launch = graph.launch_body.lower()
    crewed = any(n.state_out.crew > 0 for n in graph.nodes)
    leaves_home = any(n.target_body and n.target_body.lower() != launch
                      for n in graph.nodes if n.primitive == "transfer")
    return crewed and leaves_home


def _check_preconditions(graph: MissionGraph) -> list[str]:
    """Rule 1: each node's precondition is satisfied by the incoming world state (which is the
    predecessor's postcondition). Returns specific 'step N (prim): <reason>' errors."""
    errors: list[str] = []
    for node in graph.nodes:
        for reason in node.precondition.unmet(node.state_in):
            errors.append(f"step {node.index} ({node.primitive}): precondition unmet — {reason}")
    return errors


def _check_body_consistency(graph: MissionGraph) -> list[str]:
    """Rule 2: transfers target real bodies and form a reachable chain; on-body actions act on the body
    the chain arrived at (the graph already threads state, so a mismatch shows up as the on-body node's
    state_in.body — we additionally validate transfer targets exist and the departure chain is sound)."""
    errors: list[str] = []
    for node in graph.nodes:
        if node.primitive == "transfer":
            tgt = (node.target_body or "").strip().lower()
            if not tgt:
                errors.append(f"step {node.index} (transfer): no target_body specified")
            elif tgt not in _REAL_BODIES:
                errors.append(
                    f"step {node.index} (transfer): target_body {node.target_body!r} is not a real "
                    f"body in bodies.py (known: {sorted(b.capitalize() for b in _REAL_BODIES)})")
            elif node.state_in.body.lower() == tgt:
                errors.append(
                    f"step {node.index} (transfer): already at {node.target_body} — a transfer to the "
                    f"body you are already at is a no-op/contradiction")
        # On-body actions: the body they act on is the chain's current body. The graph builds them
        # against state_in.body, so an inconsistency means the LLM put the action before the arrival —
        # which precondition-chaining already catches; here we add an explicit body-name cross-check for
        # any step whose args carry an explicit body that disagrees with the chain.
        arg_body = node.args.get("body") or node.args.get("target_body") if node.primitive in (
            "land", "ascend", "plant_flag", "walk_to") else None
        if arg_body and arg_body.strip().lower() != node.state_in.body.lower():
            errors.append(
                f"step {node.index} ({node.primitive}): acts on {arg_body!r} but the chain is at "
                f"{node.state_in.body} — body mismatch")
    return errors


def _check_budget(graph: MissionGraph) -> tuple[list[str], list[str], float]:
    """Rule 3: sum per-node Δv; compare to vehicle_dv with the mission reserve. Returns (errors,
    warnings, required_dv_with_reserve)."""
    errors: list[str] = []
    warnings: list[str] = []
    required = graph.required_dv_with_reserve()
    if graph.vehicle_dv is None:
        warnings.append(
            f"vehicle Δv unknown — size the launch for >= {required:.0f} m/s "
            f"(Σ {graph.total_dv:.0f} + {MISSION_RESERVE_FRAC*100:.0f}% reserve)")
    elif required > graph.vehicle_dv:
        errors.append(
            f"resource budget exceeded: mission needs {required:.0f} m/s "
            f"(Σ {graph.total_dv:.0f} + {MISSION_RESERVE_FRAC*100:.0f}% reserve) but the vehicle has only "
            f"{graph.vehicle_dv:.0f} m/s — short by {required - graph.vehicle_dv:.0f} m/s")
    return errors, warnings, required


def _check_return(graph: MissionGraph, implies_return: bool) -> list[str]:
    """Rule 4: if a return is implied, the plan must END back at the home body with a recover (or an
    aerocapture transfer home followed by a land). Returns the error if it strands the crew/vehicle."""
    if not implies_return:
        return []
    launch = graph.launch_body.lower()
    final: WorldState = graph.final_state
    has_recover = any(n.primitive == "recover" for n in graph.nodes)
    # An aerocapture/transfer back to the home body + a land is an acceptable alternative to recover.
    transfers_home = any(n.primitive == "transfer" and (n.target_body or "").lower() == launch
                         for n in graph.nodes)
    lands_home_idx = next((n.index for n in graph.nodes
                           if n.primitive in ("land", "recover")
                           and n.state_in.body.lower() == launch), None)
    ends_home = final.body.lower() == launch and final.situation in (
        Situation.LANDED, Situation.ORBIT)
    if has_recover and ends_home:
        return []
    if transfers_home and lands_home_idx is not None and ends_home:
        return []
    # Otherwise the return segment is missing/incomplete.
    if not (has_recover or transfers_home):
        return [f"return segment MISSING: the command implies bringing the crew/vehicle home to "
                f"{graph.launch_body}, but the plan has no transfer back to {graph.launch_body} and no "
                f"recover — it would strand the crew at {final.body}"]
    if not has_recover:
        return [f"return segment INCOMPLETE: there is a transfer back toward {graph.launch_body} but no "
                f"recover (or land at {graph.launch_body}) to bring the crew down — the plan ends in orbit "
                f"at {final.body}"]
    return [f"return segment INCOMPLETE: a recover exists but the plan does not end landed at "
            f"{graph.launch_body} (ends at {final.body}/{final.situation.value})"]


def _check_windows(graph: MissionGraph) -> tuple[list[str], list[str]]:
    """Rule 5: each transfer must carry a computed window/tof; surface impossible ones. Node-level build
    errors (bad window, undefined tof, negative synodic) are promoted here. A closed-form planet transfer
    with no absolute UT (offline) is a WARNING, not an error — the math (phase angle, tof) is still there."""
    errors: list[str] = []
    warnings: list[str] = []
    for node in graph.nodes:
        # Promote any node-level math errors the builder recorded.
        for e in node.errors:
            if "used closed-form estimate" in e or "live window unavailable" in e:
                warnings.append(f"step {node.index} ({node.primitive}): {e}")
            else:
                errors.append(f"step {node.index} ({node.primitive}): {e}")
        if node.primitive != "transfer":
            continue
        if node.tof_s is None or not math.isfinite(node.tof_s) or node.tof_s <= 0:
            errors.append(f"step {node.index} (transfer): time of flight is undefined/non-positive "
                          f"({node.tof_s}) — impossible transfer")
        if node.window_ut is not None and (not math.isfinite(node.window_ut)):
            errors.append(f"step {node.index} (transfer): computed window_ut is not finite")
        if node.wait_s is not None and math.isfinite(node.wait_s) and node.wait_s < 0:
            errors.append(f"step {node.index} (transfer): transfer window is in the PAST "
                          f"(wait {node.wait_s:.0f}s < 0) — impossible")
    return errors, warnings


def validate_plan(graph: MissionGraph, command: str = "", vehicle_dv: float | None = None
                  ) -> ValidationReport:
    """RIGOROUSLY validate a mission graph. Returns ValidationReport(ok, errors, warnings).

    ``vehicle_dv`` overrides the value baked into the graph (so a caller that learns the launched
    vehicle's real Δv can re-validate). All five rules run and accumulate; ok is True only if there are
    NO errors (warnings do not block — they inform, e.g. an unknown vehicle Δv sizes the launch)."""
    if vehicle_dv is not None:
        graph.vehicle_dv = vehicle_dv

    errors: list[str] = []
    warnings: list[str] = []

    implies_return = command_implies_return(command, graph)

    errors += _check_preconditions(graph)
    errors += _check_body_consistency(graph)
    b_err, b_warn, required = _check_budget(graph)
    errors += b_err
    warnings += b_warn
    errors += _check_return(graph, implies_return)
    w_err, w_warn = _check_windows(graph)
    errors += w_err
    warnings += w_warn

    return ValidationReport(ok=not errors, errors=errors, warnings=warnings,
                            required_dv=required, implies_return=implies_return)
