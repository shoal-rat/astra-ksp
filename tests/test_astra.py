from pathlib import Path

from ksp_lab.astra.interpreter import Interpreter, KNOWN_CAPABILITIES
from ksp_lab.astra.ledger import ExperienceLedger, LedgerEntry
from ksp_lab.astra.knowledge import KnowledgeBase


def test_heuristic_interpreter_full_mission():
    plan = Interpreter(allow_llm=False).interpret(
        "do the full Artemis mission: a Mun relay, land an HLS, and bring the crew home"
    )
    assert plan.source == "heuristic"
    assert plan.capabilities == ["relay", "hls_land_return", "crew_return"]
    assert all(c in KNOWN_CAPABILITIES for c in plan.capabilities)
    assert plan.target_body == "Mun"


def test_heuristic_interpreter_relay_only():
    plan = Interpreter(allow_llm=False).interpret("put a communications satellite in high Mun orbit")
    assert "relay" in plan.capabilities
    assert "crew_return" not in plan.capabilities


def test_heuristic_interpreter_lander():
    plan = Interpreter(allow_llm=False).interpret("land a probe on the Mun surface")
    assert "hls_land_return" in plan.capabilities


def test_ledger_roundtrip(tmp_path: Path):
    led = ExperienceLedger(tmp_path / "exp.jsonl")
    led.record(LedgerEntry("cmd", "relay", 1, "failure", "ascent_stuck_on_pad", "", "tail"))
    led.record(LedgerEntry("cmd", "relay", 2, "success", "artemis_mun_relay_deployed", "TWR fix"))
    rows = led.entries()
    assert len(rows) == 2
    assert rows[1]["outcome"] == "success"
    assert "TWR fix" in led.learned_fixes()
    assert "Experience Ledger" in led.render_markdown()


def test_knowledge_diagnoses_known_markers(tmp_path: Path):
    kb = KnowledgeBase(ExperienceLedger(tmp_path / "exp.jsonl"), tmp_path)
    d = kb.diagnose("ascent_stuck_on_pad")
    assert d.confidence == "known"
    assert "TWR" in d.fix
    d2 = kb.diagnose("mun_landing_deorbit_timeout")
    assert d2.confidence == "known"
    assert "throttle" in d2.fix.lower()
    d3 = kb.diagnose("some_marker_never_seen_before")
    assert d3.confidence == "unknown"


def test_hoverslam_guidance_curve_monotone():
    from ksp_lab.guidance import hoverslam_reference_speed_mps, hoverslam_throttle

    args = dict(mass_kg=10000.0, thrust_n=60000.0, gravity_mps2=1.63)
    v_high = hoverslam_reference_speed_mps(altitude_m=5000.0, **args)
    v_low = hoverslam_reference_speed_mps(altitude_m=50.0, **args)
    assert v_high > v_low > 0.0
    # Below the curve -> coast; on/above -> burn.
    assert hoverslam_throttle(speed_mps=v_low - 10.0, reference_speed_mps=v_low, **args) == 0.0
    assert hoverslam_throttle(speed_mps=v_low + 10.0, reference_speed_mps=v_low, **args) > 0.5
