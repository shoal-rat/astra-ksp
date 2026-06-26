"""Tests for the METACOGNITION self-correction framework.

All state goes to a tmp dir so the real ``src/ksp_lab/data/calibrations.json`` is never touched. We cover:
the discrepancy ledger round-trip, the bounded calibration (needs N samples, agreement test, clamping,
whitelist), the calibrations.json round-trip + revert, and the needs_data / generate_missing_data hooks.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ksp_lab.astra.metacognition import (  # noqa: E402
    MIN_SAMPLES,
    TUNABLE_CONSTANTS,
    Metacognition,
)


def _meta(tmp_path) -> Metacognition:
    return Metacognition(
        memory_dir=tmp_path / "runs",
        calibrations_path=tmp_path / "data" / "calibrations.json",
    )


def test_discrepancy_ledger_roundtrip(tmp_path):
    m = _meta(tmp_path)
    m.record_discrepancy("ascent_drag_loss_mult", predicted=100.0, observed=120.0, source="flight-1")
    m.record_discrepancy("ascent_drag_loss_mult", predicted=100.0, observed=118.0, source="flight-2")
    m.record_discrepancy("capture_dv_margin_mult", predicted=50.0, observed=55.0, source="flight-3")

    assert m.ledger_path.exists()
    all_drag = m.discrepancies("ascent_drag_loss_mult")
    assert len(all_drag) == 2
    assert all_drag[0].source == "flight-1"
    assert abs(all_drag[0].ratio - 1.2) < 1e-9
    # ledger is real JSONL
    lines = m.ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["kind"] == "discrepancy"


def test_calibration_needs_min_samples(tmp_path):
    m = _meta(tmp_path)
    for i in range(MIN_SAMPLES - 1):
        m.record_discrepancy("ascent_drag_loss_mult", 100.0, 110.0, f"f{i}")
    assert m.propose_calibration("ascent_drag_loss_mult") is None  # too few

    m.record_discrepancy("ascent_drag_loss_mult", 100.0, 110.0, "f-last")
    calib = m.propose_calibration("ascent_drag_loss_mult")
    assert calib is not None
    assert calib.n_samples == MIN_SAMPLES


def test_bounded_calibration_within_band_is_applicable(tmp_path):
    m = _meta(tmp_path)
    # observed/predicted ~ 1.1 consistently -> a multiplier of ~1.1, inside [0.70, 1.40]
    for i in range(4):
        m.record_discrepancy("ascent_drag_loss_mult", 1000.0, 1100.0 + i, f"f{i}")
    calib = m.propose_calibration("ascent_drag_loss_mult")
    assert calib is not None
    assert calib.applicable is True
    assert 1.09 <= calib.value <= 1.11
    assert calib.kind == "multiplier"


def test_calibration_clamps_out_of_band_and_flags_for_review(tmp_path):
    m = _meta(tmp_path)
    # observed/predicted ~ 3.0 -> way outside the [0.70, 1.40] band -> clamped + NOT auto-applicable
    for i in range(4):
        m.record_discrepancy("ascent_drag_loss_mult", 100.0, 300.0, f"f{i}")
    calib = m.propose_calibration("ascent_drag_loss_mult")
    assert calib is not None
    assert calib.value == TUNABLE_CONSTANTS["ascent_drag_loss_mult"]["hi"]  # clamped to band edge
    assert calib.raw_value > calib.value
    assert calib.applicable is False  # clamp => human review
    assert "clamped" in calib.reason or "band" in calib.reason


def test_disagreeing_samples_are_not_applicable(tmp_path):
    m = _meta(tmp_path)
    # wildly inconsistent ratios -> high spread -> not auto-applicable even if mean is in band
    for obs in (500.0, 1350.0, 600.0, 1300.0):
        m.record_discrepancy("ascent_drag_loss_mult", 1000.0, obs, "f")
    calib = m.propose_calibration("ascent_drag_loss_mult")
    assert calib is not None
    assert calib.applicable is False
    assert "disagree" in calib.reason


def test_unmapped_quantity_is_proposal_only(tmp_path):
    m = _meta(tmp_path)
    for i in range(4):
        m.record_discrepancy("some_unwhitelisted_constant", 100.0, 105.0, f"f{i}")
    calib = m.propose_calibration("some_unwhitelisted_constant")
    assert calib is not None
    assert calib.applicable is False
    assert "whitelist" in calib.reason.lower() or "unmapped" in calib.constant


def test_apply_calibration_writes_data_file_and_is_reversible(tmp_path):
    m = _meta(tmp_path)
    for i in range(4):
        m.record_discrepancy("capture_dv_margin_mult", 100.0, 108.0, f"f{i}")
    calib = m.propose_calibration("capture_dv_margin_mult")
    assert calib.applicable is True

    assert m.apply_calibration(calib) is True
    # data file exists and round-trips
    assert m.calibrations_path.exists()
    data = json.loads(m.calibrations_path.read_text(encoding="utf-8"))
    assert "capture_dv_margin_mult" in data
    assert abs(data["capture_dv_margin_mult"]["value"] - calib.value) < 1e-9

    # the lab reads it as an override
    assert abs(m.get_override("capture_dv_margin_mult") - calib.value) < 1e-9

    # reversible
    assert m.revert_calibration("capture_dv_margin_mult") is True
    assert m.get_override("capture_dv_margin_mult") is None
    assert m.revert_calibration("capture_dv_margin_mult") is False  # already gone


def test_apply_refuses_non_applicable_proposal(tmp_path):
    m = _meta(tmp_path)
    for i in range(4):
        m.record_discrepancy("ascent_drag_loss_mult", 100.0, 300.0, f"f{i}")  # out of band -> clamped
    calib = m.propose_calibration("ascent_drag_loss_mult")
    assert calib.applicable is False
    assert m.apply_calibration(calib) is False
    assert not m.calibrations_path.exists()  # nothing written for a refused proposal


def test_needs_data_and_generate(tmp_path):
    m = _meta(tmp_path)
    store: dict = {}

    assert m.needs_data("drag_table", lambda: "drag_table" in store) is True
    out = m.generate_missing_data("drag_table", lambda: store.setdefault("drag_table", [1, 2, 3]))
    assert out == [1, 2, 3]
    assert m.needs_data("drag_table", lambda: "drag_table" in store) is False
    # generation was logged
    records = [json.loads(l) for l in m.ledger_path.read_text(encoding="utf-8").splitlines()]
    assert any(r.get("kind") == "data_generated" and r.get("name") == "drag_table" for r in records)


def test_needs_data_treats_failing_check_as_missing(tmp_path):
    m = _meta(tmp_path)

    def boom():
        raise RuntimeError("cannot check")

    assert m.needs_data("x", boom) is True


def test_record_experiment_persists_and_logs(tmp_path):
    m = _meta(tmp_path)
    exp = m.record_experiment(
        "ascent-test",
        predicted={"drag_loss": 100.0, "apoapsis_km": 100.0},
        observed={"drag_loss": 120.0, "apoapsis_km": 98.0},
        note="real flight telemetry",
    )
    assert exp.name == "ascent-test"
    assert len(m.experiment_log) == 1
    records = [json.loads(l) for l in m.ledger_path.read_text(encoding="utf-8").splitlines()]
    assert any(r.get("kind") == "experiment" for r in records)
