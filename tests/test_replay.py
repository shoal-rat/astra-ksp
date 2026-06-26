"""OFFLINE REGRESSION SUITE for ASTRA's key flight state machines (no live KSP / kRPC / network).

KSP is hard to put in CI: a real flight needs the live game + kRPC + MechJeb and runs for
minutes-to-hours. ``ksp_lab.astra.replay`` re-implements the four key state machines — ascent, transfer,
docking, recovery — as PURE decision functions that consume a telemetry sequence (a list of per-tick
dicts) and emit a decision timeline. This module drives each machine with hand-authored TRACE JSON files
in ``tests/replay/`` (representative + failure cases) and asserts the timeline hits the expected
terminals / transitions. This is the net that catches state-machine logic regressions WITHOUT flying.

What the assertions guard:
  * nominal traces reach their success terminal (CIRCULARIZE / CAPTURE / ARRIVED / DOCKED / RECOVERED / LANDED);
  * failure traces hit ABORT with the RIGHT reason (broke up / falling back / crashed / docking timeout);
  * recovery chutes deploy ONLY when safe (<250 m/s AND <5 km), never above the gate;
  * transfer CAPTURE fires AT periapsis inside the target SOI, never before;
  * a one-frame glitch does not trip an abort (the streak requirement rides it out).
"""
import json
from pathlib import Path

import pytest

from ksp_lab.astra import replay
from ksp_lab.astra.replay import (
    assert_reaches,
    build_from_meta,
    is_abort,
    load_trace,
    make_ascent_sm,
    make_docking_sm,
    make_recovery_sm,
    make_transfer_sm,
    replay_trace,
)

TRACE_DIR = Path(__file__).resolve().parent / "replay"


def _trace_files():
    return sorted(TRACE_DIR.glob("*.json"))


# ==================================================================================================
# Generic, data-driven pass: every trace file is loaded, replayed through the SM named in its
# ``machine`` field (built with its ``params``), and asserted to hit the terminal/reason in ``expect``.
# This is the bulk of the regression net — adding a new trace file extends coverage with no code.
# ==================================================================================================
@pytest.mark.parametrize("path", _trace_files(), ids=lambda p: p.stem)
def test_trace_reaches_expected(path):
    meta, ticks = load_trace(path)
    assert ticks, f"{path.name}: empty trace"
    assert "machine" in meta and "expect" in meta, f"{path.name}: missing machine/expect"
    sm = build_from_meta(meta)
    decisions = replay_trace(sm, ticks)
    expect = meta["expect"]
    # A reason substring (e.g. "broke up", "docking timeout") asserts an ABORT carrying that reason;
    # an exact terminal token (CIRCULARIZE/CAPTURE/...) asserts that terminal was reached.
    terminals = {"CIRCULARIZE", "CAPTURE", "ARRIVED", "DOCKED", "LANDED", "RECOVERED"}
    transitions = {"climbing", "stage", "coast", "warp_to_periapsis", "corrected",
                   "rendezvous", "approach", "entry", "deploy_chutes"}
    if expect in terminals or expect in transitions:
        assert_reaches(decisions, expect)
    else:
        # Treat ``expect`` as an ABORT reason substring.
        aborts = [d for d in decisions if is_abort(d)]
        assert aborts, f"{path.name}: expected an ABORT carrying {expect!r}, timeline {decisions}"
        assert any(expect in d for d in aborts), \
            f"{path.name}: expected abort reason {expect!r}, got {aborts}"


def test_all_machines_have_a_trace():
    """Every state machine in the registry must have at least one trace exercising it — so a new
    machine cannot be added without an offline replay covering it."""
    covered = set()
    for path in _trace_files():
        covered.add(json.loads(path.read_text(encoding="utf-8"))["machine"])
    assert covered == set(replay.SM_BUILDERS), \
        f"machines without a trace: {set(replay.SM_BUILDERS) - covered}"


# ==================================================================================================
# ASCENT — climb / stage / circularize / ABORT(reason)
# ==================================================================================================
def test_ascent_nominal_reaches_circularize_and_stages():
    meta, ticks = load_trace(TRACE_DIR / "ascent_nominal.json")
    decisions = replay_trace(build_from_meta(meta), ticks)
    assert_reaches(decisions, "CIRCULARIZE")
    # The controlled 49->30 booster drop must read as 'stage', never as a break-up ABORT.
    assert "stage" in decisions
    assert not any(is_abort(d) for d in decisions)
    # CIRCULARIZE latches: once reached, the tail stays CIRCULARIZE.
    assert decisions[-1] == "CIRCULARIZE"


def test_ascent_breakup_aborts_with_reason():
    meta, ticks = load_trace(TRACE_DIR / "ascent_breakup.json")
    decisions = replay_trace(build_from_meta(meta), ticks)
    assert_reaches(decisions, "ABORT")
    assert any("broke up" in d for d in decisions if is_abort(d))


def test_ascent_fallback_aborts_with_reason():
    meta, ticks = load_trace(TRACE_DIR / "ascent_fallback.json")
    decisions = replay_trace(build_from_meta(meta), ticks)
    assert any(is_abort(d) and "falling back" in d for d in decisions)


def test_ascent_crash_aborts_with_reason():
    meta, ticks = load_trace(TRACE_DIR / "ascent_crash.json")
    decisions = replay_trace(build_from_meta(meta), ticks)
    assert any(is_abort(d) and "crashed" in d for d in decisions)


def test_ascent_single_bad_frame_does_not_abort():
    """One glitchy frame (a momentary part-count read blip) inside a healthy climb must NOT abort —
    the streak requirement rides it out. Built inline (not a trace file) to keep the glitch explicit."""
    sm = make_ascent_sm(post_staging_part_count=49, payload_part_count=3)
    ticks = [
        {"apoapsis": 50000, "vertical_speed": 180, "part_count": 49, "engine_lit": True, "situation": "Vessel.Situation.flying"},
        {"apoapsis": 49000, "vertical_speed": -5,  "part_count": 8,  "engine_lit": True, "situation": "Vessel.Situation.flying"},  # blip
        {"apoapsis": 70000, "vertical_speed": 150, "part_count": 49, "engine_lit": True, "situation": "Vessel.Situation.flying"},
        {"apoapsis": 90000, "vertical_speed": 100, "part_count": 49, "engine_lit": True, "situation": "Vessel.Situation.flying"},
    ]
    decisions = replay_trace(sm, ticks)
    assert not any(is_abort(d) for d in decisions), decisions


def test_ascent_short_history_does_not_abort():
    """Fewer ticks than the streak length is never enough evidence to abort."""
    sm = make_ascent_sm(post_staging_part_count=49, payload_part_count=3)
    decisions = replay_trace(sm, [
        {"apoapsis": 40000, "vertical_speed": -50, "part_count": 8, "engine_lit": True, "situation": "Vessel.Situation.flying"},
    ])
    assert decisions == ["climbing"]


# ==================================================================================================
# TRANSFER — coast / warp_to_periapsis / corrected / CAPTURE / ARRIVED
# ==================================================================================================
def test_transfer_capture_fires_at_periapsis_not_before():
    meta, ticks = load_trace(TRACE_DIR / "transfer_capture.json")
    decisions = replay_trace(build_from_meta(meta), ticks)
    assert_reaches(decisions, "CAPTURE")
    cap_i = decisions.index("CAPTURE")
    # Before CAPTURE: coast (still in the departure SOI), then warp_to_periapsis inside the Mun SOI.
    assert "coast" in decisions[:cap_i]
    assert "warp_to_periapsis" in decisions[:cap_i]
    # CAPTURE must NOT have fired while time_to_periapsis was still large: the tick just before the
    # capture is the one whose ttp dropped under the threshold; the earlier in-SOI ticks were warps.
    assert decisions[cap_i - 1] == "warp_to_periapsis"
    # After the capture burn (a bound orbit around the target) the machine latches to its terminal.
    assert decisions[-1] == "CAPTURE"


def test_transfer_does_not_capture_at_soi_edge():
    """A craft that just entered the SOI with periapsis far away (large time_to_periapsis) must warp,
    NOT capture — capturing at the SOI edge buries the periapsis. Guards the at-periapsis condition."""
    sm = make_transfer_sm(target_body="Mun")
    # In the Mun SOI but periapsis 15 minutes away.
    decision = replay_trace(sm, [{"body": "Mun", "periapsis": 40000, "apoapsis": 380000, "ut": 0, "time_to_periapsis": 900}])
    assert decision == ["warp_to_periapsis"]


def test_transfer_correction_on_unsafe_predicted_periapsis():
    meta, ticks = load_trace(TRACE_DIR / "transfer_correction.json")
    decisions = replay_trace(build_from_meta(meta), ticks)
    # The sub-surface predicted periapsis (in the departure SOI) triggers a course-correction.
    assert "corrected" in decisions
    assert_reaches(decisions, "CAPTURE")


# ==================================================================================================
# DOCKING — rendezvous / approach / DOCKED / ABORT(timeout)
# ==================================================================================================
def test_dock_nominal_rendezvous_then_approach_then_docked():
    meta, ticks = load_trace(TRACE_DIR / "dock_nominal.json")
    decisions = replay_trace(build_from_meta(meta), ticks)
    assert_reaches(decisions, "DOCKED")
    dock_i = decisions.index("DOCKED")
    # The far phase is rendezvous (main engine), the near phase is approach (fine RCS), in that order.
    assert "rendezvous" in decisions[:dock_i]
    assert "approach" in decisions[:dock_i]
    assert decisions.index("rendezvous") < decisions.index("approach")
    assert decisions[-1] == "DOCKED"


def test_dock_timeout_aborts():
    meta, ticks = load_trace(TRACE_DIR / "dock_timeout.json")
    decisions = replay_trace(build_from_meta(meta), ticks)
    assert any(is_abort(d) and "docking timeout" in d for d in decisions)


# ==================================================================================================
# RECOVERY — entry / deploy_chutes (when safe) / LANDED / RECOVERED
# ==================================================================================================
def test_recovery_nominal_deploys_chutes_only_when_safe():
    meta, ticks = load_trace(TRACE_DIR / "recovery_nominal.json")
    decisions = replay_trace(build_from_meta(meta), ticks)
    assert_reaches(decisions, "RECOVERED")
    assert "LANDED" in decisions  # touchdown precedes recovery
    # Chutes must arm exactly when the trace first goes <250 m/s AND <5 km — at the 4200 m / 210 m/s
    # tick (index 3), never at the fast/high entry ticks before it.
    deploy_i = decisions.index("deploy_chutes")
    armed_tick = ticks[deploy_i]
    assert armed_tick["surface_speed"] < 250.0 and armed_tick["altitude"] < 5000.0
    # Every tick BEFORE the chute arm must have violated the safe gate (too fast OR too high).
    for t in ticks[:deploy_i]:
        assert not (t["surface_speed"] < 250.0 and t["altitude"] < 5000.0), \
            f"chutes should have armed earlier at {t}"


def test_recovery_chutes_do_not_deploy_slow_but_high():
    """The AND gate: a craft can be slow (<250 m/s) while still HIGH (>5 km) during a gentle descent —
    chutes must NOT arm there. The splashdown trace has exactly that two-tick window."""
    meta, ticks = load_trace(TRACE_DIR / "recovery_splashdown.json")
    decisions = replay_trace(build_from_meta(meta), ticks)
    # Ticks 1 and 2 are slow (180, 150 m/s) but high (12 km, 7 km) -> must read 'entry', not deploy.
    assert ticks[1]["surface_speed"] < 250.0 and ticks[1]["altitude"] > 5000.0
    assert decisions[1] == "entry"
    assert ticks[2]["surface_speed"] < 250.0 and ticks[2]["altitude"] > 5000.0
    assert decisions[2] == "entry"
    assert_reaches(decisions, "LANDED")


def test_recovery_does_not_deploy_at_hypersonic_low():
    """Inverse guard: fast (>250 m/s) even when low (<5 km) must NOT arm chutes (they would shred)."""
    sm = make_recovery_sm()
    decisions = replay_trace(sm, [
        {"body": "Kerbin", "altitude": 3000, "surface_speed": 600, "chutes_deployed": False, "situation": "Vessel.Situation.flying"},
    ])
    assert decisions == ["entry"]


# ==================================================================================================
# Replay-engine mechanics
# ==================================================================================================
def test_replay_trace_latches_terminal():
    """Once a hard terminal is emitted, every later tick re-emits it (the outcome latches)."""
    sm = make_docking_sm(approach_range_m=2500.0, timeout_ticks=10)
    ticks = [
        {"distance_m": 5, "rel_speed_mps": 0.2, "ports_state": "docked"},
        {"distance_m": 5, "rel_speed_mps": 0.2, "ports_state": "ready"},   # would be 'approach' if not latched
        {"distance_m": 5, "rel_speed_mps": 0.2, "ports_state": "ready"},
    ]
    decisions = replay_trace(sm, ticks)
    assert decisions == ["DOCKED", "DOCKED", "DOCKED"]


def test_replay_trace_empty_is_empty():
    assert replay_trace(make_ascent_sm(post_staging_part_count=49), []) == []


def test_assert_reaches_raises_when_missing():
    with pytest.raises(AssertionError):
        assert_reaches(["coast", "coast"], "CAPTURE")
