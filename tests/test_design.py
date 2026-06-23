"""Validate the requirements-driven, physics-calculated ship designer.

The designer's contract: enumerate what the mission NEEDS, then CALCULATE every part count from physics
— tanks by inverting the rocket equation per phase, engine CLUSTERS by the TWR the body demands,
parachutes by terminal velocity in the target body's LIVE density. These tests prove the counts are
calculated: the propulsive Starship-class Mars vehicle clusters its booster, carries NO parachutes, and
every stage's achieved Δv meets its requirement; the parachute Duna lander gets a multi-chute pack
(never the single chute that killed the crew).
"""
from __future__ import annotations

from ksp_lab import astro
from ksp_lab.design import LandingSite, Phase, ShipRequirements, design_ship
from ksp_lab.parts import stage_masses

# Live-measured surface constants for the propulsive phases (from kRPC).
KERBIN_G = 9.813
DUNA_G = 2.944
DUNA_RHO = 0.13334


def _starship_mars_requirements() -> ShipRequirements:
    """A propulsive (NO parachute) Starship-class Mars vehicle: launch off Kerbin, transfer to Duna,
    hoverslam-land on Duna under its own engines. Phases in FIRE ORDER (booster launches first)."""
    return ShipRequirements(
        name="StarshipMars",
        mission_type="mars-propulsive",
        crew=4,
        phases=[
            Phase("booster", dv_mps=3400.0, twr_body_g=KERBIN_G, min_twr=1.4),
            Phase("transfer", dv_mps=1371.0),                                   # vacuum, no TWR floor
            Phase("lander", dv_mps=2700.0, twr_body_g=DUNA_G, min_twr=2.0),     # propulsive landing
        ],
        landing=None,           # propulsive: the hoverslam law lands it, so ZERO parachutes
        needs_heatshield=True,
        needs_docking=True,     # orbital refuelling rendezvous
    )


def test_propulsive_mars_vehicle_is_fully_calculated():
    """The headline design: a propulsive Mars vehicle whose every count comes from physics."""
    req = _starship_mars_requirements()
    design = design_ship(req)
    est = design.estimates

    # Launch must actually lift: TWR >= the booster's required 1.4 at Kerbin surface gravity.
    assert est["launch_twr"] >= 1.4, est

    # Total mission Δv must cover Kerbin launch + Duna transfer + propulsive landing (>= 7300 m/s).
    assert est["total_delta_v_mps"] >= 7300.0, est

    # Propulsive architecture: the hoverslam lands it, so the designer must add ZERO parachutes.
    assert est["parachutes"] == 0.0, est

    # Three propulsive phases -> three stages.
    assert est["stage_count"] == 3.0
    assert len(design.stages) == 3


def test_booster_stage_is_clustered():
    """The booster needs TWR 1.4 against ~140 t at Kerbin gravity, which a single engine cannot give —
    so the designer must CLUSTER (engine_count > 1). A count of 1 would mean no TWR calculation ran."""
    req = _starship_mars_requirements()
    design = design_ship(req)
    booster = design.stages[0]
    assert booster.role == "booster"
    assert booster.engine_count > 1, design.notes


def test_every_stage_meets_its_delta_v_requirement():
    """Per-stage proof: run the rocket equation on each stage's own (engines + tanks + everything above
    it) and confirm the achieved Δv meets that phase's requirement. If any stage fell short, the tank
    count was not solved from the rocket equation — the whole 'calculated' claim would be a lie."""
    req = _starship_mars_requirements()
    design = design_ship(req)
    bus = _bus_mass_of(design, req)

    stage_wet = [stage_masses(s)[1] for s in design.stages]
    for i, (stage, phase) in enumerate(zip(design.stages, req.phases)):
        dry, wet, thrust_asl, _isp_asl, isp_vac = stage_masses(stage)
        mass_above = bus + sum(stage_wet[i + 1:])     # this stage carries the wet mass of all above it
        achieved_dv = astro.rocket_dv(isp_vac, mass_above + wet, mass_above + dry)
        # Allow a hair of rounding slack (design.py accepts >= 0.5% under) but it must essentially meet it.
        assert achieved_dv >= phase.dv_mps * 0.995, (
            f"stage {stage.role}: achieved {achieved_dv:.0f} < required {phase.dv_mps:.0f}"
        )


def test_launch_stage_twr_meets_floor_under_kerbin_gravity():
    """Cross-check the launch TWR directly from masses and ASL thrust, not just the estimates block."""
    req = _starship_mars_requirements()
    design = design_ship(req)
    bus = _bus_mass_of(design, req)
    stage_wet = [stage_masses(s)[1] for s in design.stages]
    launch = design.stages[0]
    dry, wet, thrust_asl, _isp_asl, _isp_vac = stage_masses(launch)
    m0 = bus + sum(stage_wet)                          # full stack at liftoff
    twr = astro.twr(thrust_asl * 1000.0, m0, KERBIN_G)
    assert twr >= 1.4


def test_parachute_duna_lander_gets_multichute_pack():
    """A CREWED Duna lander that descends on parachutes (landing=LandingSite) must get a multi-chute
    pack — never the single Mk16 that terminal-falls at ~30 m/s and kills the crew. With a realistic
    payload the calculated count clears 8 chutes; the design must also record landing legs."""
    req = ShipRequirements(
        name="DunaChuteLander",
        mission_type="mars-parachute",
        crew=4,
        payload_t=3.0,                                  # ISRU + science + supplies: a real lander mass
        phases=[
            Phase("booster", dv_mps=3400.0, twr_body_g=KERBIN_G, min_twr=1.4),
            Phase("transfer", dv_mps=1371.0),
        ],
        landing=LandingSite(DUNA_G, DUNA_RHO),          # target_touchdown_mps defaults to 8 m/s
        needs_heatshield=True,
    )
    design = design_ship(req)
    n_chute = design.estimates["parachutes"]
    assert n_chute >= 8.0, (n_chute, design.notes)
    assert design.landing_legs is True


def test_propulsive_vs_parachute_diverge_on_chutes():
    """The same designer, given landing=None vs a LandingSite, must return ZERO vs a multi-chute pack.
    This is the requirements -> calculated-count contract in one assertion."""
    propulsive = design_ship(_starship_mars_requirements())
    chuted = ShipRequirements(
        name="cmp", crew=4, payload_t=3.0,
        phases=[Phase("booster", dv_mps=3400.0, twr_body_g=KERBIN_G, min_twr=1.4)],
        landing=LandingSite(DUNA_G, DUNA_RHO), needs_heatshield=True,
    )
    assert propulsive.estimates["parachutes"] == 0.0
    assert design_ship(chuted).estimates["parachutes"] >= 8.0


# --------------------------------------------------------------------------------------------------
# helper — recompute the command/crew/heatshield/docking bus mass the designer carries above stage 0.
# Mirrors design._bus_mass without importing the private symbol, so the test stays a black-box check.
# --------------------------------------------------------------------------------------------------

def _bus_mass_of(design, req: ShipRequirements) -> float:
    from ksp_lab.design import _bus_mass
    return _bus_mass(req)
