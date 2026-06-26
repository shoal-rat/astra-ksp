"""Mission-aware launch DESIGN — the crewed Mun land-and-return sizing path.

The launch primitive USED to size for Kerbin-to-LKO only (booster 4200 + a small insertion burn, NO
landing legs), ~2600 m/s short of a Mun round-trip's post-LKO budget. The fix is ADDITIVE: ``launch`` and
``_launch_requirements`` take opt-in ``mission_dv`` + ``needs_legs``. When ``mission_dv > 0`` a VACUUM
``mission`` phase carrying the post-LKO budget is appended and legs + heatshield/chutes are requested, so
ONE vehicle is sized for the whole round-trip. When ``mission_dv == 0`` the requirement is IDENTICAL to
before, so relay/Eve launches are unchanged.

These tests prove exactly that contract and run fully OFFLINE (no kRPC, no browser, no ANTHROPIC_API_KEY):
the design is sized + gated via ``design_ship`` + ``looks_like_a_rocket`` (the geometry gate verdict, the
same one ``design_and_verify`` runs before the PNG rasterize). The harness deriver is tested against the
real Mun plan.
"""
from __future__ import annotations

import sys
from pathlib import Path

# tools/ holds design_chart + fly_mun_roundtrip; make them importable the way the primitives do.
_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from ksp_lab.astra.primitives import _launch_requirements  # noqa: E402
from ksp_lab.design import LandingSite, design_ship  # noqa: E402

# A Kerbin-return chute touchdown spec (stock Kerbin sea-level density), as the launch primitive builds it.
_KERBIN_LANDING = LandingSite(body_g=9.813, surface_rho=1.225, target_touchdown_mps=6.0)


# --------------------------------------------------------------------------------------------------- #
# 1. mission_dv == 0  ->  the requirement is IDENTICAL to the legacy LKO-only launch (unchanged behaviour)
# --------------------------------------------------------------------------------------------------- #
def test_mission_dv_zero_is_unchanged_lko_only_requirement():
    """The default (mission_dv=0) must produce the SAME two-phase booster+insertion req as before:
    no mission phase, no forced legs. This is the guarantee that relay/Eve launches are untouched."""
    req = _launch_requirements("Relay-1", target_alt_km=100.0, crew=0, heatshield=False,
                               landing=None, radial_boosters=0, max_core_engines=1)
    # Exactly two phases, in fire order, named booster + insertion.
    assert [p.name for p in req.phases] == ["booster", "insertion"], req.phases
    assert req.phases[0].dv_mps == 4200.0
    # No mission-aware additions when mission_dv is absent/zero.
    assert all(p.name != "mission" for p in req.phases)
    assert req.needs_legs is False
    assert req.needs_heatshield is False

    # And passing mission_dv=0 explicitly is byte-for-byte the same (the opt-in guard, not a side effect).
    req_explicit_zero = _launch_requirements("Relay-1", target_alt_km=100.0, crew=0, heatshield=False,
                                             landing=None, radial_boosters=0, max_core_engines=1,
                                             mission_dv=0.0, needs_legs=False)
    assert [p.name for p in req_explicit_zero.phases] == [p.name for p in req.phases]
    assert [round(p.dv_mps, 3) for p in req_explicit_zero.phases] == [round(p.dv_mps, 3) for p in req.phases]
    assert req_explicit_zero.needs_legs is False


def test_mission_dv_zero_crewed_eve_launch_still_lko_only():
    """A crewed LKO launch with a heatshield (the relay/Eve-style call) keeps its legs OFF and adds NO
    mission phase when mission_dv is 0 — the heatshield flag alone must not pull in a mission phase."""
    req = _launch_requirements("Eve-Probe", target_alt_km=100.0, crew=1, heatshield=True,
                               landing=_KERBIN_LANDING, radial_boosters=0, max_core_engines=1)
    assert [p.name for p in req.phases] == ["booster", "insertion"]
    assert req.needs_legs is False
    assert req.needs_heatshield is True  # heatshield is independent of mission-awareness


# --------------------------------------------------------------------------------------------------- #
# 2. mission_dv > 0  ->  a vacuum mission phase is present, legs+heatshield requested, Δv covers budget
# --------------------------------------------------------------------------------------------------- #
def test_mission_dv_appends_vacuum_mission_phase_and_legs():
    """With mission_dv>0 the req gains a VACUUM 'mission' phase carrying that Δv, and (with needs_legs)
    landing legs — so the design sizes the FULL round-trip craft, not an LKO-only stub."""
    mission_dv = 4126.0  # ~ the Mun plan's post-LKO budget (3930) + a 5% margin
    req = _launch_requirements("AI-Mun-1", target_alt_km=100.0, crew=1, heatshield=True,
                               landing=_KERBIN_LANDING, radial_boosters=2, max_core_engines=1,
                               mission_dv=mission_dv, needs_legs=True)
    names = [p.name for p in req.phases]
    assert names == ["booster", "insertion", "mission"], names
    mission = req.phases[-1]
    assert mission.dv_mps == mission_dv
    # VACUUM phase: no surface-gravity TWR floor (it fires only in space).
    assert mission.twr_body_g == 0.0
    assert mission.min_twr == 0.0
    # Legs + heatshield are now requested for the touchdown + re-entry.
    assert req.needs_legs is True
    assert req.needs_heatshield is True


def test_mission_aware_design_sizes_full_mun_roundtrip_and_passes_gate():
    """The headline: design the mission-aware Mun craft and assert it covers the round-trip budget,
    carries legs + a heatshield, and the three-view geometry gate PASSES."""
    import design_chart  # tools/design_chart.py — the same gate design_and_verify runs

    post_lko_dv = 3930.0                      # Σ of the Mun plan's non-launch nodes (from the graph)
    mission_dv = round(post_lko_dv * 1.05, 1)  # + the harness's 5% margin
    req = _launch_requirements("AI-Mun-1", target_alt_km=100.0, crew=1, heatshield=True,
                               landing=_KERBIN_LANDING, radial_boosters=2, max_core_engines=1,
                               mission_dv=mission_dv, needs_legs=True)

    design = design_ship(req)
    est = design.estimates

    # The full Mun round-trip is ~7241 m/s; sized with reserves the vehicle must carry >= ~7800 m/s.
    assert est["total_delta_v_mps"] >= 7800.0, est
    # And it must cover its own (requirement + 5% floor): the design's feasibility gate enforces this.
    assert design.feasible, design.infeasible_reasons
    assert est["total_delta_v_mps"] >= est["required_delta_v_mps"] * 1.05, est

    # Landing legs + heatshield are on the actual built craft (not just the requirement).
    assert design.landing_legs is True, "mission-aware Mun craft must carry landing legs"
    assert design.heatshield is True, "mission-aware Mun craft must carry a heatshield for Kerbin re-entry"
    # Kerbin-return chutes are sized too (the landing spec was passed).
    assert est["parachutes"] >= 1.0, est

    # The three-view geometry GATE must PASS for this taller, mission-sized rocket.
    report = design_chart.looks_like_a_rocket(design)
    failing = [k for k, ok in report.get("checks", {}).items() if not ok]
    assert report.get("looks_like_a_rocket") is True, f"geometry gate failed: {failing}"


def test_mission_aware_total_dv_exceeds_lko_only_by_the_mission_budget():
    """Sanity: the mission-aware craft carries materially MORE Δv than the LKO-only one — proof the
    mission phase actually grew the vehicle, not just relabelled it."""
    lko = _launch_requirements("LKO", target_alt_km=100.0, crew=1, heatshield=True,
                               landing=_KERBIN_LANDING, radial_boosters=2, max_core_engines=1)
    mun = _launch_requirements("Mun", target_alt_km=100.0, crew=1, heatshield=True,
                               landing=_KERBIN_LANDING, radial_boosters=2, max_core_engines=1,
                               mission_dv=4126.0, needs_legs=True)
    dv_lko = design_ship(lko).estimates["total_delta_v_mps"]
    dv_mun = design_ship(mun).estimates["total_delta_v_mps"]
    assert dv_mun >= dv_lko + 3500.0, (dv_lko, dv_mun)


# --------------------------------------------------------------------------------------------------- #
# 3. the harness deriver turns the Mun plan into the right mission-aware launch args
# --------------------------------------------------------------------------------------------------- #
def test_harness_derives_mission_args_from_mun_plan():
    """fly_mun_roundtrip._mission_aware_launch_args must read the graph and return mission_dv (post-LKO
    Δv + margin), needs_legs=True (airless Mun touchdown), and heatshield+chutes (Kerbin recover)."""
    import fly_mun_roundtrip as fmr

    plan = [
        {"primitive": "launch", "args": {"crew": 1, "target_alt_km": 100, "name": "AI-Mun-1"}},
        {"primitive": "transfer", "args": {"target_body": "Mun"}},
        {"primitive": "land", "args": {}},
        {"primitive": "plant_flag", "args": {}},
        {"primitive": "ascend", "args": {"target_alt_km": 20}},
        {"primitive": "transfer", "args": {"target_body": "Kerbin"}},
        {"primitive": "recover", "args": {}},
    ]
    args = fmr._mission_aware_launch_args(plan, launch_body="Kerbin")
    assert args["needs_legs"] is True
    assert args["heatshield"] is True
    assert args["chutes"] is True
    # mission_dv ~= post-LKO sum (~3930) * 1.05 ~= 4126 m/s; allow a band for catalog/body constants.
    assert 3800.0 <= args["mission_dv"] <= 4600.0, args


def test_harness_no_post_lko_nodes_returns_empty():
    """A plain LKO-only plan (launch only) yields NO mission-aware args — the LKO launch is left as-is."""
    import fly_mun_roundtrip as fmr

    plan = [{"primitive": "launch", "args": {"crew": 0, "target_alt_km": 100, "name": "Relay-1"}}]
    assert fmr._mission_aware_launch_args(plan, launch_body="Kerbin") == {}
