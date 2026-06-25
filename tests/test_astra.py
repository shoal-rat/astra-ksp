from pathlib import Path

from ksp_lab.astra.interpreter import Interpreter
from ksp_lab.astra.ledger import ExperienceLedger, LedgerEntry
from ksp_lab.astra.knowledge import KnowledgeBase
from ksp_lab.astra import primitives
from ksp_lab.astra.primitives import PrimitiveContext


def _names(plan):
    return [s["primitive"] for s in plan.steps]


# ----------------------------------------------------------------------------------------------------
# DECOMPOSER: every command must break into a SENSIBLE, BODY-AGNOSTIC primitive sequence (no body="Mun"
# default; the body is inferred from the text). These mirror the --dry-run path.
# ----------------------------------------------------------------------------------------------------
def test_decompose_mun_relay():
    plan = Interpreter(allow_llm=False).interpret("put a communications satellite in high Mun orbit")
    assert plan.source == "heuristic"
    assert plan.target_body == "Mun"
    names = _names(plan)
    assert names[0] == "launch"
    assert "transfer" in names
    assert "commission_relay" in names
    # a bare relay is NOT a crewed round trip
    assert "recover" not in names
    # the transfer targets Mun, not a hardcoded body
    tr = next(s for s in plan.steps if s["primitive"] == "transfer")
    assert tr["args"]["target_body"] == "Mun"


def test_decompose_duna_probe_landing():
    plan = Interpreter(allow_llm=False).interpret("land a probe on Duna")
    assert plan.target_body == "Duna"
    names = _names(plan)
    assert names[0] == "launch"
    assert "transfer" in names and "land" in names
    tr = next(s for s in plan.steps if s["primitive"] == "transfer")
    assert tr["args"]["target_body"] == "Duna"
    # Duna has an atmosphere -> the heuristic should aerocapture, not propulsively capture.
    assert tr["args"]["capture_mode"] == "aerocapture"
    # a probe is not crewed
    assert plan.steps[0]["args"].get("crew", 0) == 0


def test_decompose_eve_gilly_flag_round_trip():
    plan = Interpreter(allow_llm=False).interpret(
        "send a kerbal to Eve, plant a flag on Gilly, bring them home"
    )
    # The actual landing destination named is Gilly.
    assert plan.target_body == "Gilly"
    names = _names(plan)
    # full crewed round-trip sequence, body-agnostic
    assert names[0] == "launch"
    assert plan.steps[0]["args"].get("crew", 0) >= 1
    assert "transfer" in names
    assert "land" in names
    assert "plant_flag" in names
    assert "ascend" in names
    assert "recover" in names
    # plant_flag comes after land; recover is last
    assert names.index("plant_flag") > names.index("land")
    assert names[-1] == "recover"
    # the outbound transfer goes to Gilly, the homebound transfer to Kerbin
    transfers = [s["args"]["target_body"] for s in plan.steps if s["primitive"] == "transfer"]
    assert transfers[0] == "Gilly"
    assert transfers[-1] == "Kerbin"


def test_heuristic_is_body_agnostic_no_mun_default():
    # A command with NO body named must NOT silently default to Mun.
    plan = Interpreter(allow_llm=False).interpret("launch a satellite to orbit")
    assert plan.target_body != "Mun"
    assert plan.target_body == "Kerbin"   # launch-body orbit, no transfer
    assert "transfer" not in _names(plan)
    # And a Minmus command is detected as Minmus, not shadowed by 'Mun'.
    plan2 = Interpreter(allow_llm=False).interpret("land a probe on Minmus")
    assert plan2.target_body == "Minmus"


# ----------------------------------------------------------------------------------------------------
# PRIMITIVE CATALOG
# ----------------------------------------------------------------------------------------------------
def test_catalog_has_required_primitives():
    required = {
        "launch", "transfer", "set_orbit", "land", "ascend", "plant_flag",
        "rendezvous", "dock", "transfer_crew", "recover", "commission_relay", "select_vessel",
    }
    assert required <= set(primitives.CATALOG)
    # every spec advertises which proven function it wraps (for the report/docs)
    for spec in primitives.CATALOG.values():
        assert spec.wraps
        assert spec.description


def test_primitives_run_in_dry_run_without_krpc():
    # In dry-run the primitives must not touch kRPC (offline-safe).
    ctx = PrimitiveContext(dry_run=True)
    r = primitives.run_primitive(ctx, "launch", {"name": "AI-Test", "crew": 1})
    assert r.ok and r.primitive == "launch"
    r2 = primitives.run_primitive(ctx, "transfer", {"target_body": "Eve", "capture_mode": "loose"})
    assert r2.ok


# ----------------------------------------------------------------------------------------------------
# select_vessel: tolerance of KSP localized name suffixes (the stranded-vessel bug).
# ----------------------------------------------------------------------------------------------------
def test_select_vessel_tolerates_localized_suffix():
    from ksp_lab.vessel_match import vessel_names_match

    # The Chinese "飞船" (spacecraft) suffix KSP appended must still match the launched craft name.
    assert vessel_names_match("AI-Eve-Crew 飞船", "AI-Eve-Crew")
    assert vessel_names_match("AI-Mun-Relay Probe", "AI-Mun-Relay")
    assert vessel_names_match("AI-Duna-Craft", "ai-duna-craft")          # case-insensitive
    assert vessel_names_match("AI-Relay-1 探测器", "AI-Relay-1")
    # but it must NOT cross-match different craft (a real suffix must be set off by a separator)
    assert not vessel_names_match("AI-Relay-2", "AI-Relay-1")
    assert not vessel_names_match("AI-Relay-12", "AI-Relay-1")
    assert not vessel_names_match("Totally-Other", "AI-Relay-1")
    assert not vessel_names_match("AI-Relay-1", "")                       # empty wanted matches nothing


class _FakeVessel:
    def __init__(self, name):
        self.name = name


class _FakeSC:
    def __init__(self, names):
        self.vessels = [_FakeVessel(n) for n in names]
        self.active_vessel = None


class _FakeCtx(PrimitiveContext):
    pass


def test_select_vessel_primitive_finds_suffixed_vessel(monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    ctx = PrimitiveContext()
    ctx.sc = _FakeSC(["Some-Debris", "AI-Eve-Crew 飞船"])

    # patch refresh_vessel to avoid touching orbit on the fake
    ctx.refresh_vessel = lambda: ctx.vessel  # type: ignore
    r = primitives.run_primitive(ctx, "select_vessel", {"name": "AI-Eve-Crew"})
    assert r.ok
    assert ctx.sc.active_vessel is not None
    assert "AI-Eve-Crew" in ctx.sc.active_vessel.name


def test_select_vessel_primitive_reports_stranded():
    ctx = PrimitiveContext()
    ctx.sc = _FakeSC(["Unrelated-1", "Unrelated-2"])
    ctx.refresh_vessel = lambda: ctx.vessel  # type: ignore
    r = primitives.run_primitive(ctx, "select_vessel", {"name": "AI-Eve-Crew"})
    assert not r.ok
    assert r.marker == "vessel_not_found"


# ----------------------------------------------------------------------------------------------------
# Ledger + knowledge (unchanged behaviour, kept green).
# ----------------------------------------------------------------------------------------------------
def test_ledger_roundtrip(tmp_path: Path):
    led = ExperienceLedger(tmp_path / "exp.jsonl")
    led.record(LedgerEntry("cmd", "1:launch", 1, "failure", "ascent_stuck_on_pad", "", "tail"))
    led.record(LedgerEntry("cmd", "1:launch", 2, "success", "launch_to_orbit", "TWR fix"))
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
    d3 = kb.diagnose("some_marker_never_seen_before")
    assert d3.confidence == "unknown"


def test_hoverslam_guidance_curve_monotone():
    from ksp_lab.guidance import hoverslam_reference_speed_mps, hoverslam_throttle

    args = dict(mass_kg=10000.0, thrust_n=60000.0, gravity_mps2=1.63)
    v_high = hoverslam_reference_speed_mps(altitude_m=5000.0, **args)
    v_low = hoverslam_reference_speed_mps(altitude_m=50.0, **args)
    assert v_high > v_low > 0.0
    assert hoverslam_throttle(speed_mps=v_low - 10.0, reference_speed_mps=v_low, **args) == 0.0
    assert hoverslam_throttle(speed_mps=v_low + 10.0, reference_speed_mps=v_low, **args) > 0.5
