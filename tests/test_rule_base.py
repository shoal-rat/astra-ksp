"""Tests for the structured, queryable failure rule base (ksp_lab.astra.rule_base) and its wiring
into KnowledgeBase.diagnose. No kRPC; pure data + matching logic."""
from __future__ import annotations

from pathlib import Path

import pytest

from ksp_lab.astra.knowledge import KnowledgeBase
from ksp_lab.astra.ledger import ExperienceLedger
from ksp_lab.astra.rule_base import Rule, RuleBase

_REQUIRED = ("id", "marker", "symptom", "cause", "fix", "applicable_primitives", "confidence", "tags")


# --------------------------------------------------------------------------------------------------
# Schema: the JSON loads, every rule has all required fields, confidence in [0, 1], ids unique.
# --------------------------------------------------------------------------------------------------
def test_rule_base_loads_from_packaged_json():
    rb = RuleBase.load()
    assert len(rb) >= 12  # ~12-20+ high-quality seeded rules


def test_every_rule_has_all_required_fields_and_valid_confidence():
    rb = RuleBase.load()
    seen_ids: set[str] = set()
    for r in rb.rules:
        assert isinstance(r, Rule)
        for fld in _REQUIRED:
            assert getattr(r, fld) is not None, f"{r.id} missing {fld}"
        assert r.id and r.marker and r.symptom and r.cause and r.fix
        assert 0.0 <= r.confidence <= 1.0, f"{r.id} confidence out of range: {r.confidence}"
        assert isinstance(r.applicable_primitives, list) and r.applicable_primitives
        assert isinstance(r.tags, list) and r.tags
        assert r.id not in seen_ids, f"duplicate rule id {r.id}"
        seen_ids.add(r.id)


def test_markers_split_on_pipe():
    r = Rule.from_dict(
        {
            "id": "x",
            "marker": "alpha|beta gamma | delta",
            "symptom": "s",
            "cause": "c",
            "fix": "f",
            "applicable_primitives": ["launch"],
            "confidence": 0.5,
            "tags": ["t"],
        }
    )
    assert r.markers() == ["alpha", "beta gamma", "delta"]


# --------------------------------------------------------------------------------------------------
# query() by marker: returns the right rule, ranked by confidence.
# --------------------------------------------------------------------------------------------------
def test_query_by_marker_returns_right_rule_ranked():
    rb = RuleBase.load()
    hits = rb.query(marker="ascent_stuck_on_pad")
    assert hits, "expected a stuck-on-pad rule"
    assert hits[0].id == "ascent-stuck-on-pad-twr"
    assert "twr" in hits[0].fix.lower()
    # results are confidence-ranked among equally-relevant matches (non-increasing confidence is fine).


def test_query_by_marker_matches_substring_inside_a_log_line():
    rb = RuleBase.load()
    # a real log tail with the signature embedded must still match
    hits = rb.query(marker="[12:30:01] launch to parking orbit FAILED; aborting")
    ids = {h.id for h in hits}
    # the empty-pod / crewable-pod or crew rules carry this kind of launch-failure context; at minimum
    # SOMETHING matches a launch failure marker
    assert hits, f"no rule matched embedded marker; ids={ids}"


def test_query_by_marker_regex_alternative_matches():
    rb = RuleBase.load()
    # 'velocity.*0' is a regex alternative on the stale-reference-frame rule
    hits = rb.query(marker="reported velocity 0 the whole ascent")
    assert any(h.id == "stale-reference-frame-zero-velocity" for h in hits)


# --------------------------------------------------------------------------------------------------
# query() by primitive: filters to rules that apply to that primitive.
# --------------------------------------------------------------------------------------------------
def test_query_by_primitive_filters():
    rb = RuleBase.load()
    land_rules = rb.query(primitive="land")
    assert land_rules
    for r in land_rules:
        assert "land" in [p.lower() for p in r.applicable_primitives]
    # a primitive nothing references returns nothing
    assert rb.query(primitive="not_a_real_primitive") == []


def test_query_by_primitive_and_marker_together():
    rb = RuleBase.load()
    # the loose-capture / dry-out rule applies to 'transfer'
    hits = rb.query(marker="ran the vehicle dry", primitive="transfer")
    assert any(h.id == "eve-capture-hohmann-down-dry" for h in hits)
    # filtering by a primitive the rule lacks removes it
    assert not any(h.id == "eve-capture-hohmann-down-dry" for h in rb.query(
        marker="ran the vehicle dry", primitive="plant_flag"))


def test_min_confidence_filters_out_low_rules():
    rb = RuleBase.load()
    hi = rb.query(min_confidence=0.9)
    assert hi  # several rules at >= 0.9
    assert all(r.confidence >= 0.9 for r in hi)


def test_query_no_filters_returns_all_sorted_by_confidence():
    rb = RuleBase.load()
    allr = rb.query()
    assert len(allr) == len(rb)
    confs = [r.confidence for r in allr]
    assert confs == sorted(confs, reverse=True)


# --------------------------------------------------------------------------------------------------
# query() by symptom text: fuzzy / keyword match.
# --------------------------------------------------------------------------------------------------
def test_query_by_symptom_text_keyword_match():
    rb = RuleBase.load()
    hits = rb.query(symptom_text="the spacecraft broke up and most of the parts disappeared")
    assert any(h.id == "ascent-part-count-collapse" for h in hits)


# --------------------------------------------------------------------------------------------------
# diagnose(): best match for a marker; sensible fix for the named documented bugs.
# --------------------------------------------------------------------------------------------------
def test_diagnose_parking_orbit_failed_returns_sensible_fix():
    rb = RuleBase.load()
    r = rb.diagnose("launch to parking orbit FAILED", "ascent never reached the parking target")
    assert r is not None
    assert r.fix  # a non-empty remedy


def test_diagnose_decoupler_strand_bug():
    rb = RuleBase.load()
    r = rb.diagnose("payload decoupler mis-fired", "stranded payload before parking")
    assert r is not None
    assert r.id == "decoupler-id-on-proxies-strand"
    # the fix names the real remedy: use kRPC equality, never python id()/is
    assert "==" in r.fix or "id()" in r.fix
    assert "decoupler" in " ".join(r.tags).lower()


def test_diagnose_silent_launch_poll_failfast():
    rb = RuleBase.load()
    r = rb.diagnose("ascent poll timeout", "polled 20 min while the vehicle sat alive after break-up")
    assert r is not None
    assert "fail" in r.fix.lower() or "abort" in r.fix.lower()


def test_diagnose_did_not_escape_eve_soi():
    rb = RuleBase.load()
    r = rb.diagnose("did not escape Eve SOI on the return (still Eve)")
    assert r is not None
    assert r.id == "did-not-escape-soi-return"


def test_diagnose_unknown_marker_returns_none():
    rb = RuleBase.load()
    assert rb.diagnose("zxqwv_totally_unseen_signature_42") is None


# --------------------------------------------------------------------------------------------------
# KnowledgeBase.diagnose still returns a Diagnosis and now consults the rule base FIRST.
# --------------------------------------------------------------------------------------------------
def test_knowledge_diagnose_returns_diagnosis_from_rule_base(tmp_path: Path):
    kb = KnowledgeBase(ExperienceLedger(tmp_path / "exp.jsonl"), tmp_path)
    assert kb.rule_base is not None and len(kb.rule_base) >= 12
    d = kb.diagnose("payload decoupler mis-fired", log_tail="stranded payload")
    assert d.confidence == "known"
    assert "==" in d.fix or "id()" in d.fix  # the structured rule's fix flows through


def test_knowledge_diagnose_keeps_existing_known_and_unknown_contract(tmp_path: Path):
    # The existing test_astra contract must still hold: stuck-on-pad -> a TWR fix (known); a never-seen
    # marker -> unknown.
    kb = KnowledgeBase(ExperienceLedger(tmp_path / "exp.jsonl"), tmp_path)
    d = kb.diagnose("ascent_stuck_on_pad")
    assert d.confidence == "known"
    assert "TWR" in d.fix
    d2 = kb.diagnose("some_marker_never_seen_before")
    assert d2.confidence == "unknown"


def test_knowledge_diagnose_falls_back_to_ledger_when_rule_base_absent(tmp_path: Path):
    # If the structured rule base is unavailable, the hardcoded ledger seed rules still diagnose.
    kb = KnowledgeBase(ExperienceLedger(tmp_path / "exp.jsonl"), tmp_path)
    kb.rule_base = None
    d = kb.diagnose("ascent_stuck_on_pad")
    assert d.confidence == "known"
    assert "TWR" in d.fix


def test_rule_base_load_missing_file_raises():
    with pytest.raises((OSError, ValueError)):
        RuleBase.load(Path("does_not_exist_failure_rules.json"))
