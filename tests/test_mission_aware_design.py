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
def test_general_planner_derives_mission_args_from_mun_plan():
    """The GENERAL planner's mission-aware sizing (planner._apply_mission_aware_launch — the body-agnostic
    successor to the deleted hardcoded fly_mun_roundtrip helper) reads the graph and returns mission_dv
    (post-LKO Δv + margin), needs_legs=True (airless Mun touchdown), and heatshield+chutes (Kerbin recover)."""
    from ksp_lab.astra import planner

    plan = [
        {"primitive": "launch", "args": {"crew": 1, "target_alt_km": 100, "name": "AI-Mun-1"}},
        {"primitive": "transfer", "args": {"target_body": "Mun"}},
        {"primitive": "land", "args": {}},
        {"primitive": "plant_flag", "args": {}},
        {"primitive": "ascend", "args": {"target_alt_km": 20}},
        {"primitive": "transfer", "args": {"target_body": "Kerbin"}},
        {"primitive": "recover", "args": {}},
    ]
    args = planner._apply_mission_aware_launch(plan, launch_body="Kerbin")
    assert args["needs_legs"] is True
    assert args["heatshield"] is True
    assert args["chutes"] is True
    # mission_dv ~= post-LKO sum (~3930) * 1.08 ~= 4244 m/s; allow a band for catalog/body constants.
    assert 3800.0 <= args["mission_dv"] <= 4600.0, args


def test_general_planner_no_post_lko_nodes_returns_empty():
    """A plain LKO-only plan (launch only) yields NO mission-aware args — the LKO launch is left as-is."""
    from ksp_lab.astra import planner

    plan = [{"primitive": "launch", "args": {"crew": 0, "target_alt_km": 100, "name": "Relay-1"}}]
    assert planner._apply_mission_aware_launch(plan, launch_body="Kerbin") == {}


# --------------------------------------------------------------------------------------------------- #
# 4. SPLIT-STAGE crewed PLANET round-trip: droppable transfer stage + short squat lander
# --------------------------------------------------------------------------------------------------- #
def test_split_stage_duna_roundtrip_lands_stable_with_independent_lander_budget():
    """The crewed Duna (planet) land-and-return SPLITS the upper into a droppable transfer stage + a SHORT
    wide lander. The plan inserts jettison_transfer_stage between capture and descent; the launch carries
    transfer_dv + lander_dv (NOT mission_dv); and the produced craft is feasible, lands UPRIGHT
    (landed_stable), has a >=2.5 m lander base, and does NOT bank the whole-mission reserve on the lander
    (so the lander's get-home budget is independent of the variable capture cost)."""
    import design_chart
    from ksp_lab.astra import planner
    from ksp_lab.design import (Phase, ShipRequirements, design_ship, default_reserve_frac,
                                mission_upper_phases, _mission_reserve_phase_name)
    from ksp_lab.bodies import DUNA

    steps, _ = planner.decompose("land a crew on Mars, plant a flag, and bring them home", "Duna")
    names = [s["primitive"] for s in steps]
    assert "jettison_transfer_stage" in names
    assert names.index("jettison_transfer_stage") == names.index("transfer") + 1
    assert names.index("jettison_transfer_stage") < names.index("land")
    la = steps[0]["args"]
    assert la.get("transfer_dv", 0) > 0 and la.get("lander_dv", 0) > 0
    assert "mission_dv" not in la
    assert abs(la["lander_body_g"] - DUNA.surface_g) < 0.1

    ph = [Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3, reserve_frac=default_reserve_frac(9.81)),
          Phase("insertion", 283.0, twr_body_g=0.0, min_twr=0.0, reserve_frac=default_reserve_frac(0.0))]
    ph += mission_upper_phases(transfer_dv=la["transfer_dv"], lander_dv=la["lander_dv"],
                               lander_body_g=la["lander_body_g"])
    req = ShipRequirements(name="AI-Duna-1", mission_type="crewed_launch", crew=1, payload_t=0.3, phases=ph,
                           landing=_KERBIN_LANDING, needs_legs=True, needs_heatshield=True,
                           needs_docking=False, max_engine_count=int(la.get("max_core_engines", 6)),
                           radial_booster_count=0)
    d = design_ship(req)
    assert d.feasible, d.infeasible_reasons
    assert d.landed_stable is True, "the short split lander must land upright (low CoG, wide legs)"
    lander_stage = next(s for s in d.stages if "land" in s.role.lower())
    assert lander_stage.diameter_m >= 2.5, "the lander base must be wide for a squat, low-CoG lander"
    assert _mission_reserve_phase_name(ph) != lander_stage.role, "the lander must NOT bank the mission reserve"
    assert design_chart.looks_like_a_rocket(d).get("looks_like_a_rocket") is True


def test_mun_roundtrip_stays_single_stage_unchanged():
    """A MOON (Mun) land-and-return keeps the proven SINGLE 'mission' stack: NO jettison step, mission_dv set
    (not transfer_dv/lander_dv) — the flight-proven Mun vehicle is left byte-for-byte unchanged by the split."""
    from ksp_lab.astra import planner

    steps, _ = planner.decompose("land a crew on the Mun, plant a flag, and return", "Mun")
    names = [s["primitive"] for s in steps]
    assert "jettison_transfer_stage" not in names
    la = steps[0]["args"]
    assert la.get("mission_dv", 0) > 1000.0
    assert "transfer_dv" not in la and "lander_dv" not in la


def test_jettison_transfer_stage_is_registered_and_validates():
    """jettison_transfer_stage must be a real catalog primitive (so _validate_steps keeps it) and a valid
    zero-Δv ORBIT->ORBIT mission-graph node (so the plan validator does not reject the split plan)."""
    from ksp_lab.astra.primitives import CATALOG
    from ksp_lab.astra.mission_graph import build_mission_graph
    assert "jettison_transfer_stage" in CATALOG
    plan = [{"primitive": "launch", "args": {"crew": 1, "target_alt_km": 80, "name": "AI-Duna-1"}},
            {"primitive": "transfer", "args": {"target_body": "Duna"}},
            {"primitive": "jettison_transfer_stage", "args": {"target_body": "Duna"}},
            {"primitive": "land", "args": {}}]
    g = build_mission_graph(plan, launch_body="Kerbin")
    jet = next(n for n in g.nodes if n.primitive == "jettison_transfer_stage")
    assert jet.dv_mps == 0.0
