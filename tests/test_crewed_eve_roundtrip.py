"""Regression tests for the two crewed-Eve-round-trip bugs a live flight exposed.

BUG 1 — the 'crewed' pod launched EMPTY (kRPC crew_count == 0): the headless launch flies on a probe
core and the Mk1 pod has no kerbal. The fix boards one via the bridge's /spawn-crew endpoint, so these
tests pin (a) the bridge client posting the vessel-targeted spawn-crew request, and (b) the controller
exposing board_crew that verifies a kerbal is aboard.

BUG 2 — the capture ran the vehicle DRY: the old path captured into a LOW circular orbit (a costly
Hohmann-DOWN from the natural ~7,600 km encounter), Δv the budget never paid for. The fix captures into
a LOOSE ellipse (low periapsis, apoapsis ~0.30 SOI) for ~146 m/s. These tests pin the cheap budget and
that the realized capture apoapsis ceiling matches the budgeted 0.30*SOI (no Hohmann-down).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import crewed_eve_roundtrip as cer  # noqa: E402
from ksp_lab import bridge_client  # noqa: E402
from ksp_lab.bodies import EVE  # noqa: E402


# --------------------------------------------------------------------------------------------------
# BUG 2 — the loose elliptical capture is CHEAP and the design Δv budget closes.
# --------------------------------------------------------------------------------------------------
def test_eve_capture_is_a_cheap_loose_ellipse_not_a_hohmann_down():
    b = cer._vacuum_budget_mps()
    # The elliptical capture must stay the cheap ~146 m/s term, NOT the hundreds-of-m/s a Hohmann-down
    # to a low circular orbit would cost. Generous ceiling guards the intent without pinning the exact value.
    assert b["eve_capture_elliptical"] < 300.0
    # The return ejection leaves from the SAME low periapsis (Oberth-cheap), so it is small too.
    assert b["eve_eject_return"] < 300.0
    # The sized vacuum budget must cover every term with margin.
    assert b["budget"] >= b["ideal_sum"]


def test_capture_apoapsis_ceiling_matches_the_budgeted_0_30_soi():
    # The realized capture ceiling (altitude handed to _retro_capture) must equal the budget's assumed
    # apoapsis = EVE.radius + 0.30*SOI, expressed as an ALTITUDE (so the flown capture matches the sizing).
    ceiling_alt = cer._eve_capture_apoapsis_ceiling_m()
    assert ceiling_alt == 0.30 * EVE.soi_m
    # And it sits well above Eve's atmosphere (a genuine loose ellipse, not a low orbit).
    assert ceiling_alt > EVE.atmosphere_top_m * 50


def test_design_is_feasible_and_passes_the_geometry_gate():
    d, rep = cer.design_crew_vehicle("AI-Eve-Crew-Test", render=False)
    assert d.feasible
    assert d.crewed
    assert rep["looks_like_a_rocket"]


# --------------------------------------------------------------------------------------------------
# BUG (live): the WRITTEN craft had NO crewable pod. launch_to_lko re-derived a crew=0 PROBE design and
# wrote THAT (root probeCoreOcto, no mk1pod), so kRPC reported crew_capacity == 0 and /spawn-crew had no
# free seat. The crewed launch must pass crew/heatshield/landing through so the written craft is crewable.
# --------------------------------------------------------------------------------------------------
def test_launch_to_lko_accepts_crew_requirements():
    # The launcher must expose the crew/needs_heatshield/landing knobs the crewed mission threads through;
    # before the fix it hardcoded crew=0 and the signature had no way to ask for a crewable pod.
    import inspect

    import deploy_relay

    sig = inspect.signature(deploy_relay.launch_to_lko)
    for kw in ("crew", "needs_heatshield", "landing"):
        assert kw in sig.parameters, f"launch_to_lko is missing the {kw!r} parameter"
    assert sig.parameters["crew"].default == 0  # uncrewed by default (existing relay callers unchanged)


def test_crewed_launch_writes_a_craft_with_a_crewable_pod():
    # Reproduce the launch-path requirements for a crewed Eve launch (the same crew=1 / heatshield / Kerbin
    # chutes the crewed mission now threads into launch_to_lko) and assert the rendered craft is crewable.
    from ksp_lab.craft_writer import CraftWriter
    from ksp_lab.design import (Phase, ShipRequirements, default_reserve_frac, design_ship)

    insertion_dv = cer._vacuum_budget_mps()["budget"]
    req = ShipRequirements(
        name="AI-Eve-Crew", mission_type="crewed_launch", crew=1, payload_t=0.3,
        phases=[Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3, reserve_frac=default_reserve_frac(9.81)),
                Phase("insertion", insertion_dv, twr_body_g=9.81, min_twr=0.5, reserve_frac=default_reserve_frac(0.0))],
        landing=cer._kerbin_landing_site(), needs_legs=False, needs_heatshield=True, needs_docking=False,
        max_engine_count=1, radial_booster_count=4)
    d = design_ship(req)
    assert d.crewed and d.heatshield
    text = CraftWriter().render(d, part_bodies=None)
    assert "part = mk1pod" in text, "launch-path crewed craft has no crewable Mk1 pod"
    assert "HeatShield1" in text  # forward heat shield for the Eve-return aerocapture


# --------------------------------------------------------------------------------------------------
# BUG 1 — the bridge client seats a kerbal, and the controller boards + verifies one.
# --------------------------------------------------------------------------------------------------
class _RecordingBridge(bridge_client.BridgeClient):
    """Capture the (method, path, payload) of the bridge call without any HTTP."""

    def __init__(self):
        super().__init__(base_url="http://127.0.0.1:48500")
        self.calls = []

    def _request(self, method, path, **kwargs):  # type: ignore[override]
        self.calls.append((method, path, kwargs.get("json")))
        return {"ok": True, "message": "Spawned Jeb into Mk1 Command Pod", "crew": "Jebediah Kerman"}


def test_spawn_crew_posts_vessel_targeted_request():
    br = _RecordingBridge()
    br.spawn_crew(vessel="AI-Eve-Crew")
    assert br.calls == [("POST", "/spawn-crew", {"vessel": "AI-Eve-Crew"})]


def test_spawn_crew_without_vessel_posts_empty_body():
    br = _RecordingBridge()
    br.spawn_crew()
    assert br.calls == [("POST", "/spawn-crew", {})]


class _FakeVessel:
    def __init__(self, name, crew_after_spawn=1):
        self.name = name
        self.crew_count = 0
        self._crew_after_spawn = crew_after_spawn


class _FakeSpaceCenter:
    def __init__(self, vessel):
        self.active_vessel = vessel


class _SeatingBridge(_RecordingBridge):
    """Simulate /spawn-crew actually seating a kerbal (crew_count goes 0 -> 1)."""

    def __init__(self, vessel):
        super().__init__()
        self._vessel = vessel

    def _request(self, method, path, **kwargs):
        out = super()._request(method, path, **kwargs)
        if path == "/spawn-crew":
            self._vessel.crew_count = self._vessel._crew_after_spawn
        return out


def test_board_crew_seats_a_kerbal_and_verifies_crew_count():
    v = _FakeVessel("AI-Eve-Crew", crew_after_spawn=1)
    sc = _FakeSpaceCenter(v)
    br = _SeatingBridge(v)
    assert cer.board_crew(sc, br, v, retries=2, settle_s=0.0) is True
    assert v.crew_count == 1
    # It targeted the crew vehicle by name.
    assert br.calls[0] == ("POST", "/spawn-crew", {"vessel": "AI-Eve-Crew"})


def test_board_crew_returns_false_when_pod_stays_empty():
    v = _FakeVessel("AI-Eve-Crew", crew_after_spawn=0)  # bridge never actually seats anyone
    sc = _FakeSpaceCenter(v)
    br = _SeatingBridge(v)
    assert cer.board_crew(sc, br, v, retries=2, settle_s=0.0) is False
    assert v.crew_count == 0


def test_board_crew_short_circuits_when_already_crewed():
    v = _FakeVessel("AI-Eve-Crew")
    v.crew_count = 1
    sc = _FakeSpaceCenter(v)
    br = _SeatingBridge(v)
    assert cer.board_crew(sc, br, v, settle_s=0.0) is True
    assert br.calls == []  # no spawn-crew call needed
