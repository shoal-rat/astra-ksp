"""Integration tests for the three capability wirings into ASTRA's primitives + planner.

Covers, WITHOUT touching kRPC or flying anything:
  1. WIRING 1 (three-view PNG design constraint): a `launch` whose design fails the geometry/PNG gate
     returns FAIL and NEVER calls the flight function (design_and_verify is mocked to fail; launch_to_lko
     is mocked to record whether it was called). The pass path is also checked.
  2. WIRING 2 (precise EVA): the new `walk_to` primitive is registered AND builds the right
     eva_control.walk_kerbal_to call (bridge + lat/lon + live body radius), via a mock.
  3. The planner catalog still advertises EVERY primitive, including the new `walk_to`.
  4. An offline dry-run of a flag mission still DECOMPOSES and the decomposed plan runs through the
     primitives in dry-run mode (no kRPC).
"""
from __future__ import annotations

import sys
import types

import pytest

from ksp_lab.astra import planning_context as pc
from ksp_lab.astra import primitives
from ksp_lab.astra.interpreter import Interpreter
from ksp_lab.astra.primitives import PrimitiveContext


# ----------------------------------------------------------------------------------------------------
# WIRING 1 — three-view PNG design constraint gates the launch.
# ----------------------------------------------------------------------------------------------------
def _install_fake_design_chart(monkeypatch, *, ok: bool, png="docs/design_chart_AI-Test.png"):
    """Install a fake `design_chart` module (imported by name from tools/ inside launch) whose
    design_and_verify returns (design, png_path, ok, report). Returns the module so the test can
    inspect whether it was called."""
    fake = types.ModuleType("design_chart")
    fake.calls = []

    def design_and_verify(req, *, out_dir, part_bodies=None, use_full_catalog=False):
        fake.calls.append({"name": getattr(req, "name", None), "out_dir": str(out_dir)})
        report = {
            "looks_like_a_rocket": ok,
            "png_rendered": ok,
            "png_path": png if ok else None,
            "svg_path": "docs/design_chart_AI-Test.svg",
            "failing_checks": [] if ok else ["slender body (4 <= L/D <= 19)"],
        }
        return object(), (png if ok else None), ok, report

    fake.design_and_verify = design_and_verify
    monkeypatch.setitem(sys.modules, "design_chart", fake)
    return fake


def _install_fake_deploy_relay(monkeypatch):
    """Install a fake `deploy_relay` whose launch_to_lko records that it was called (and returns True)."""
    fake = types.ModuleType("deploy_relay")
    fake.launched = []

    def launch_to_lko(sc, cfg, runner, bridge, name, target_alt_km, **kw):
        fake.launched.append(name)
        return True

    fake.launch_to_lko = launch_to_lko
    monkeypatch.setitem(sys.modules, "deploy_relay", fake)
    return fake


def _live_ctx():
    ctx = PrimitiveContext(dry_run=False)
    ctx.current_body = "Kerbin"
    ctx.refresh_vessel = lambda: ctx.vessel  # type: ignore  # don't touch kRPC
    return ctx


def test_launch_refuses_to_fly_when_design_gate_fails(monkeypatch):
    fake_dc = _install_fake_design_chart(monkeypatch, ok=False)
    fake_dr = _install_fake_deploy_relay(monkeypatch)
    ctx = _live_ctx()

    r = primitives.run_primitive(ctx, "launch", {"name": "AI-Test", "crew": 1, "target_alt_km": 100.0})

    assert not r.ok
    assert r.marker == "design_rejected"
    # the design WAS evaluated ...
    assert fake_dc.calls and fake_dc.calls[0]["name"] == "AI-Test"
    # ... but the flight function was NEVER called (refused to fly)
    assert fake_dr.launched == []
    assert r.data.get("failing_checks")


def test_launch_flies_when_design_gate_passes(monkeypatch):
    fake_dc = _install_fake_design_chart(monkeypatch, ok=True)
    fake_dr = _install_fake_deploy_relay(monkeypatch)
    ctx = _live_ctx()

    r = primitives.run_primitive(ctx, "launch", {"name": "AI-Test", "crew": 0, "target_alt_km": 100.0})

    assert r.ok
    assert r.marker == "launch_to_orbit"
    assert fake_dc.calls  # gate ran
    assert fake_dr.launched == ["AI-Test"]  # and only then did it fly
    assert r.data.get("design_png")  # the auditable PNG path is surfaced


def test_launch_requirements_mirror_crew_and_boosters():
    # The req fed to the gate must reflect the launch flags (crewed pod, radial boosters).
    req = primitives._launch_requirements(
        "AI-Heavy", target_alt_km=100.0, crew=2, heatshield=True, landing=None,
        radial_boosters=4, max_core_engines=2,
    )
    assert req.crew == 2
    assert req.mission_type == "crewed_launch"
    assert req.needs_heatshield is True
    assert req.radial_booster_count == 4
    assert req.max_engine_count == 2
    assert [p.name for p in req.phases] == ["booster", "insertion"]


# ----------------------------------------------------------------------------------------------------
# WIRING 2 — walk_to primitive registered + builds the right precise-EVA call.
# ----------------------------------------------------------------------------------------------------
def test_walk_to_registered_in_catalog():
    spec = primitives.CATALOG.get("walk_to")
    assert spec is not None
    assert "eva_control.walk_kerbal_to" in spec.wraps
    assert set(spec.params) == {"lat", "lon"}


def test_walk_to_builds_precise_eva_call(monkeypatch):
    captured = {}

    def fake_walk_kerbal_to(bridge, lat, lon, body_radius_m=None, crew=""):
        captured.update(bridge=bridge, lat=lat, lon=lon, body_radius_m=body_radius_m)
        return {"resolvedLat": lat, "resolvedLon": lon,
                "plannedGeodesic": {"distanceM": 1234.0, "bearingDeg": 90.0}}

    from ksp_lab import eva_control
    monkeypatch.setattr(eva_control, "walk_kerbal_to", fake_walk_kerbal_to)

    ctx = _live_ctx()
    ctx.current_body = "Gilly"  # body radius 13_000 m
    ctx.bridge = object()

    r = primitives.run_primitive(ctx, "walk_to", {"lat": 1.5, "lon": -2.5})
    assert r.ok
    assert r.marker == "walked_to"
    assert captured["bridge"] is ctx.bridge
    assert captured["lat"] == 1.5 and captured["lon"] == -2.5
    # body radius is read LIVE from the current body (Gilly = 13 km), not guessed
    assert captured["body_radius_m"] == pytest.approx(13_000.0)


def test_walk_to_dry_run_offline():
    ctx = PrimitiveContext(dry_run=True)
    r = primitives.run_primitive(ctx, "walk_to", {"lat": 0.0, "lon": 0.0})
    assert r.ok and r.marker == "walk_planned"


# ----------------------------------------------------------------------------------------------------
# Catalog completeness — the planner still sees every primitive incl. walk_to.
# ----------------------------------------------------------------------------------------------------
def test_planner_catalog_lists_every_primitive_including_walk_to():
    catalog = primitives.catalog_for_prompt()
    names = {entry["primitive"] for entry in catalog}
    assert names == set(primitives.CATALOG)
    assert "walk_to" in names
    # the static planning context the LLM is shown also carries it
    ctx = pc.build_planning_context_static("walk somewhere")
    ctx_names = {e["primitive"] for e in ctx["primitive_catalog"]}
    assert "walk_to" in ctx_names


# ----------------------------------------------------------------------------------------------------
# Offline dry-run of a flag mission still decomposes AND runs through the primitives.
# ----------------------------------------------------------------------------------------------------
def test_offline_flag_mission_decomposes_and_dry_runs():
    plan = Interpreter(allow_llm=False).interpret(
        "send a kerbal to Eve, plant a flag on Gilly, bring them home"
    )
    names = [s["primitive"] for s in plan.steps]
    assert names[0] == "launch"
    assert "plant_flag" in names
    assert names[-1] == "recover"

    # Execute the decomposed plan in dry-run (offline, no kRPC): every step must succeed.
    ctx = PrimitiveContext(dry_run=True)
    for step in plan.steps:
        r = primitives.run_primitive(ctx, step["primitive"], step.get("args", {}))
        assert r.ok, f"dry-run step {step['primitive']} failed: {r.marker}"
