import json
from pathlib import Path

import pytest

from ksp_lab.astra.interpreter import Interpreter, LLMUnavailableError
from ksp_lab.astra.ledger import ExperienceLedger, LedgerEntry
from ksp_lab.astra.knowledge import KnowledgeBase
from ksp_lab.astra import primitives
from ksp_lab.astra.primitives import PrimitiveContext


def _names(plan):
    return [s["primitive"] for s in plan.steps]


def _stub_llm(monkeypatch, steps, *, target_body, rationale="stub plan"):
    """Make interpret() run the LLM path OFFLINE: set a dummy key + replace the network call with a stub
    that returns a well-formed architect JSON. The real API is NEVER touched."""
    response = json.dumps({"target_body": target_body, "steps": steps,
                           "mission_rationale": rationale})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(Interpreter, "_call_llm",
                        lambda self, system, command: response, raising=True)


# ----------------------------------------------------------------------------------------------------
# INTERPRETER HARD-REQUIRES THE LLM — no offline/heuristic fallback at all.
# ----------------------------------------------------------------------------------------------------
def test_interpret_decomposes_via_llm_cli_without_api_key(monkeypatch):
    # Decomposition is ALWAYS an LLM call over the LOCAL CLI (no ANTHROPIC_API_KEY). Mock the CLI's stdout
    # with a strict-JSON plan (codex echoes the prompt + frames the answer, so the JSON-extractor must take
    # the LAST object); the interpreter validates it against the catalog and finalize_plan adds the physics.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake_stdout = (
        'OpenAI Codex v0.139\nuser\n<the prompt, which itself contains an example {"x":1}>\ncodex\n'
        '{"target_body":"Mun","steps":['
        '{"primitive":"launch","args":{"crew":1}},'
        '{"primitive":"transfer","args":{"target_body":"Mun"}},'
        '{"primitive":"land","args":{}},'
        '{"primitive":"plant_flag","args":{}},'
        '{"primitive":"ascend","args":{}},'
        '{"primitive":"transfer","args":{"target_body":"Kerbin"}},'
        '{"primitive":"recover","args":{}}]}\ntokens used\n9001\n'
    )
    monkeypatch.setattr(Interpreter, "_call_llm", lambda self, system, command: fake_stdout, raising=True)
    plan = Interpreter().interpret("land a crew on the Mun, plant a flag, and return")
    assert plan.source == "llm"
    assert plan.target_body == "Mun"
    prims = [s["primitive"] for s in plan.steps]
    assert prims[0] == "launch" and "land" in prims and "ascend" in prims and "recover" in prims
    # the launch is mission-sized for the whole round trip (post-LKO Δv + legs threaded in by finalize_plan)
    launch_args = plan.steps[0]["args"]
    assert launch_args.get("mission_dv", 0) > 1000.0 and launch_args.get("needs_legs") is True


def test_interpret_raises_when_llm_call_fails(monkeypatch):
    # A failing local-CLI call must propagate as LLMUnavailableError — never a silent degrade (no fallback).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def boom(self, system, command):
        raise RuntimeError("local CLI not installed")

    monkeypatch.setattr(Interpreter, "_call_llm", boom, raising=True)
    with pytest.raises(LLMUnavailableError):
        Interpreter().interpret("land a probe on Duna")


def test_no_allow_llm_or_heuristic_attribute():
    # The offline knobs are gone: no allow_llm ctor option, no heuristic decomposer.
    import inspect

    assert "allow_llm" not in inspect.signature(Interpreter.__init__).parameters
    assert not hasattr(Interpreter, "_interpret_heuristic")


def _load_cli_module():
    """Import tools/astra.py by path (the tools/ dir is not on sys.path, only src/ is)."""
    import importlib.util

    cli_path = Path(__file__).resolve().parent.parent / "tools" / "astra.py"
    spec = importlib.util.spec_from_file_location("astra_cli", cli_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_has_no_no_llm_flag():
    # The CLI must NOT expose --no-llm anymore (no way to force an offline heuristic): argparse rejects the
    # unknown option with SystemExit. The CLI source must also keep --dry-run (still plans via the LLM).
    cli = _load_cli_module()
    with pytest.raises(SystemExit):
        cli.main(["some mission", "--no-llm"])

    src = (Path(cli.__file__)).read_text(encoding="utf-8")
    assert "--no-llm" not in src
    assert "allow_llm" not in src
    assert "--dry-run" in src


# ----------------------------------------------------------------------------------------------------
# DECOMPOSER (LLM path, stubbed): a command breaks into a SENSIBLE, BODY-AGNOSTIC primitive sequence.
# ----------------------------------------------------------------------------------------------------
def test_decompose_mun_relay(monkeypatch):
    _stub_llm(monkeypatch, target_body="Mun", steps=[
        {"primitive": "launch", "args": {"target_alt_km": 100.0}},
        {"primitive": "transfer", "args": {"target_body": "Mun", "capture_mode": "circular"}},
        {"primitive": "commission_relay", "args": {}},
    ])
    plan = Interpreter().interpret("put a communications satellite in high Mun orbit")
    assert plan.source == "llm"
    assert plan.target_body == "Mun"
    names = _names(plan)
    assert names[0] == "launch"
    assert "transfer" in names
    assert "commission_relay" in names
    # a bare relay is NOT a crewed round trip
    assert "recover" not in names
    tr = next(s for s in plan.steps if s["primitive"] == "transfer")
    assert tr["args"]["target_body"] == "Mun"


def test_decompose_duna_probe_landing(monkeypatch):
    _stub_llm(monkeypatch, target_body="Duna", steps=[
        {"primitive": "launch", "args": {"target_alt_km": 100.0, "crew": 0}},
        {"primitive": "transfer", "args": {"target_body": "Duna", "capture_mode": "aerocapture"}},
        {"primitive": "land", "args": {}},
    ])
    plan = Interpreter().interpret("land a probe on Duna")
    assert plan.target_body == "Duna"
    names = _names(plan)
    assert names[0] == "launch"
    assert "transfer" in names and "land" in names
    tr = next(s for s in plan.steps if s["primitive"] == "transfer")
    assert tr["args"]["target_body"] == "Duna"
    assert tr["args"]["capture_mode"] == "aerocapture"
    assert plan.steps[0]["args"].get("crew", 0) == 0


def test_decompose_eve_gilly_flag_round_trip(monkeypatch):
    _stub_llm(monkeypatch, target_body="Gilly", steps=[
        {"primitive": "launch", "args": {"crew": 1, "heatshield": True, "chutes": True}},
        {"primitive": "transfer", "args": {"target_body": "Gilly", "capture_mode": "loose"}},
        {"primitive": "land", "args": {}},
        {"primitive": "plant_flag", "args": {}},
        {"primitive": "ascend", "args": {"target_alt_km": 30.0}},
        {"primitive": "transfer", "args": {"target_body": "Kerbin", "capture_mode": "aerocapture"}},
        {"primitive": "recover", "args": {}},
    ])
    plan = Interpreter().interpret("send a kerbal to Eve, plant a flag on Gilly, bring them home")
    assert plan.target_body == "Gilly"
    names = _names(plan)
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
    transfers = [s["args"]["target_body"] for s in plan.steps if s["primitive"] == "transfer"]
    assert transfers[0] == "Gilly"
    assert transfers[-1] == "Kerbin"


def test_target_body_defaults_when_llm_omits_it(monkeypatch):
    # If the LLM omits target_body, interpret() infers it from the command text (Minmus, not shadowed by
    # 'Mun'); a bodiless command defaults to the launch body Kerbin.
    _stub_llm(monkeypatch, target_body="", steps=[
        {"primitive": "launch", "args": {"target_alt_km": 100.0}},
        {"primitive": "transfer", "args": {"target_body": "Minmus", "capture_mode": "loose"}},
        {"primitive": "land", "args": {}},
    ])
    plan = Interpreter().interpret("land a probe on Minmus")
    assert plan.target_body == "Minmus"


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
