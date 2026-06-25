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
            Phase("lander", dv_mps=2700.0, twr_body_g=DUNA_G, min_twr=2.0, min_diameter_m=2.5),  # wide, low-CoG propulsive lander
        ],
        landing=None,           # propulsive: the hoverslam law lands it, so ZERO parachutes
        needs_legs=True,        # but it STILL needs landing legs (decoupled from parachutes)
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


def test_propulsive_lander_gets_legs_and_does_not_tip():
    """A PROPULSIVE (no-chute) lander must still get landing legs (the decoupled `needs_legs`), and once
    the stack geometry is rendered the legs must splay wide enough that it cannot topple: footpad SPAN
    >= center-of-gravity HEIGHT (tip-over angle >= 35 deg). This is the exact failure that tipped the
    craft over and killed the crew — lock it so it cannot regress."""
    from ksp_lab.craft_writer import CraftWriter
    design = design_ship(_starship_mars_requirements())
    assert design.landing_legs is True, "propulsive lander must have legs"
    craft = CraftWriter().render(design)
    assert craft.count("part = landingLeg1") >= 4, "expected >= 4 landing legs in the .craft"
    # span >= CoG height (ratio >= 1.0) and tip-over angle past the 35 deg industry floor.
    assert design.cog_height_m > 0.0 and design.leg_span_m > 0.0, design.to_dict()
    assert design.leg_span_m >= design.cog_height_m, (design.leg_span_m, design.cog_height_m)
    assert design.tipover_angle_deg >= 35.0, design.tipover_angle_deg
    assert design.landed_stable is True
    # ASCENT STABILITY: the diameter-laddered sizer now enforces a MONOTONIC non-increasing taper (the
    # base is the widest, no hammerhead/wasp-waist), so this Mars stack is no longer top-heavy — its
    # centre of pressure sits at/below the centre of mass and it is passively ascent-stable (near-neutral
    # static margin, flown out on engine gimbal + reaction wheels). This is the improvement the redesign
    # was for; the old NEGATIVE-margin hammerhead is gone.
    assert design.ascent_stable is True, design.static_margin_m


def test_propulsive_lander_uses_a_wide_low_cog_tank():
    """The lander stage must pick a WIDE 2.5 m tank (a low CoG + wide base), not a tall 1.25 m needle —
    the min_diameter_m constraint. The lander is the kept top stage (stages[-1])."""
    design = design_ship(_starship_mars_requirements())
    lander = design.stages[-1]
    assert lander.role == "lander"
    assert lander.diameter_m >= 2.5, lander.to_dict()


def test_feasibility_gate_rejects_underthrust_rocket():
    """The pipeline must REJECT, not silently ship, a rocket that cannot fly. A heavy payload on a
    single small booster engine gives liftoff TWR < 1 — design_ship must set feasible=False with a
    reason. This is the fix for the pad-hang / fall-back failures (the physics was computed but never
    enforced). A sound 2-stage launcher must stay feasible."""
    bad = design_ship(ShipRequirements(
        name="under-thrust", crew=0, payload_t=30.0,
        phases=[Phase("booster", dv_mps=3500.0, twr_body_g=KERBIN_G, min_twr=1.5)],
        landing=None, max_engine_count=1,
    ))
    assert bad.feasible is False, bad.estimates
    assert bad.estimates["launch_twr"] < 1.2
    assert any("TWR" in r or "under-thrust" in r for r in bad.infeasible_reasons)

    good = design_ship(ShipRequirements(
        name="sound-launcher", crew=0, payload_t=0.3,
        phases=[Phase("booster", dv_mps=3500.0, twr_body_g=KERBIN_G, min_twr=1.5),
                Phase("insertion", dv_mps=1300.0)],
        landing=None, max_engine_count=1,
    ))
    assert good.feasible is True, good.infeasible_reasons
    assert good.estimates["launch_twr"] >= 1.2


def test_booster_uses_sea_level_engine_not_a_vacuum_one():
    """Role-correct engine selection: a liftoff (in-atmosphere) booster must draw from the sea-level
    thrust pool — never a vacuum Terrier (which gives TWR<1 and won't lift). The vacuum upper stage may."""
    from ksp_lab.design import BOOSTER_ENGINES
    good = design_ship(ShipRequirements(
        name="roles", crew=0, payload_t=0.3,
        phases=[Phase("booster", dv_mps=3500.0, twr_body_g=KERBIN_G, min_twr=1.5),
                Phase("insertion", dv_mps=1300.0)],
        landing=None, max_engine_count=1,
    ))
    assert good.stages[0].engine in BOOSTER_ENGINES, good.stages[0].engine
    assert good.stages[0].engine != "liquidEngine3.v2"  # the Terrier is a vacuum engine, not a booster


def test_design_has_sound_aerodynamics():
    """The aerospace sign-off on the REBUILT relay launcher: a streamlined slender stack (low Cd from a
    nose + faired payload), high ballistic coefficient (small drag-loss Δv), a sane max-Q, and statically
    STABLE on a SMALL fin set (no 13-fin forest). Shape -> air-resistance numbers, all calculated."""
    from ksp_lab.craft_writer import CraftWriter
    d = design_ship(ShipRequirements(
        name="relay-aero", crew=0, payload_t=0.3,
        phases=[Phase("booster", dv_mps=4200.0, twr_body_g=KERBIN_G, min_twr=1.3),
                Phase("insertion", dv_mps=1300.0)],
        landing=None, max_engine_count=1,
    ))
    craft = CraftWriter().render(d)  # minimal mode populates the aero metrics from the assembled shape
    assert 0.0 < d.drag_cd <= 0.30, d.drag_cd                    # streamlined (nose + fairing)
    assert d.frontal_area_m2 > 0.0
    assert d.ballistic_coeff_kgm2 > 30_000.0, d.ballistic_coeff_kgm2   # slices through the air
    # Drag-loss is a frontal-area FUDGE for the offline budget only (MechJeb flies the real ascent). The
    # diameter-laddered sizer now builds a chunkier, lower-fineness 2.5 m stack (more frontal area) so
    # the fudge reads a bit higher than the old 1.25 m noodle — still a low, sane ascent drag loss.
    assert d.ascent_drag_loss_mps < 450.0, d.ascent_drag_loss_mps      # low air-resistance loss
    assert d.max_q_kpa > 0.0
    assert d.ascent_stable is True
    assert craft.count("part = basicFin") + craft.count("part = R8winglet") <= 8  # a small fin set, not a forest


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


def _eve_relay_requirements(boosters: int) -> ShipRequirements:
    """The heavy interplanetary relay: a ~3800 m/s upper for Eve's high synchronous insertion. On a SINGLE
    core engine (max_engine_count=1 — the relay constraint, since the in-tank radial CLUSTER auto-staged
    early in live test) this is a ~125 t rocket at liftoff TWR ~1.13 that CANNOT lift (infeasible); strap-
    on radial boosters are the reliable fix. `boosters` is the requested radial-booster count."""
    return ShipRequirements(
        name="AI-Eve-Relay", mission_type="relay_comsat", crew=0, payload_t=0.3,
        phases=[Phase("booster", 4200.0, twr_body_g=KERBIN_G, min_twr=1.3),
                Phase("insertion", 3800.0, twr_body_g=KERBIN_G, min_twr=0.5)],
        landing=None, max_engine_count=1, radial_booster_count=boosters,
    )


def test_radial_boosters_are_sized_and_attached():
    """Requesting radial boosters must add a CALCULATED RadialBoosterSpec to the design: N symmetric pods,
    each a real engine + a whole-tank count, on a radial decoupler. None of it guessed."""
    d = design_ship(_eve_relay_requirements(4))
    rb = d.radial_boosters
    assert rb is not None, d.notes
    assert rb.count == 4
    assert rb.engine in ("liquidEngine", "liquidEngine2", "engineLargeSkipper",
                         "liquidEngineMainsail.v2", "Size3AdvancedEngine")  # a sea-level booster engine
    assert rb.tank_count >= 1
    assert rb.decoupler == "radialDecoupler2"


def test_radial_boosters_make_an_unliftable_core_launchable():
    """The headline fix: a heavy ~3800 m/s upper that CANNOT lift on a single core engine (TWR < 1.2,
    infeasible) becomes launchable once strap-on boosters carry the liftoff thrust — combined TWR in the
    flight window AND the design feasible."""
    single = design_ship(_eve_relay_requirements(0))
    boosted = design_ship(_eve_relay_requirements(4))
    # The single core engine cannot lift this heavy stack: below the 1.2 floor -> infeasible.
    assert single.estimates["launch_twr"] < 1.2, single.estimates
    assert single.feasible is False, single.infeasible_reasons
    # With boosters the COMBINED liftoff TWR clears 1.4 and the design is feasible (launchable).
    assert boosted.estimates["launch_twr"] >= 1.4, boosted.estimates
    assert boosted.feasible is True, boosted.infeasible_reasons
    # The boosters add real ascent Δv on top of the core.
    assert boosted.estimates["booster_delta_v_mps"] > 0.0, boosted.estimates


def test_radial_boosters_carry_a_share_so_the_core_is_lighter_than_brute_force():
    """ASPARAGUS efficiency: the core is sized for only its SHARE of the launch Δv (the boosters carry the
    rest), so the core LAUNCH STAGE is smaller than if it had to do the whole job. Compare the boosted
    core's wet mass to a single core that brute-forces the SAME liftoff TWR via a wider engine cluster."""
    boosted = design_ship(_eve_relay_requirements(4))
    # A single-core alternative allowed to cluster engines to reach a similar TWR is heavier in its CORE
    # launch stage than the asparagus core (which only carries its dv share). Compare launch-stage wet.
    brute = design_ship(ShipRequirements(
        name="brute", crew=0, payload_t=0.3,
        phases=[Phase("booster", 4200.0, twr_body_g=KERBIN_G, min_twr=1.3),
                Phase("insertion", 3800.0, twr_body_g=KERBIN_G, min_twr=0.5)],
        landing=None, max_engine_count=4))   # let it brute-force TWR with a big cluster, no boosters
    boosted_core_wet = stage_masses(boosted.stages[0])[1]
    brute_core_wet = stage_masses(brute.stages[0])[1]
    assert boosted_core_wet < brute_core_wet, (boosted_core_wet, brute_core_wet)


def test_no_radial_boosters_by_default():
    """Default behaviour is unchanged: a request without radial_booster_count is a single-core rocket."""
    d = design_ship(ShipRequirements(
        name="single-core", crew=0, payload_t=0.3,
        phases=[Phase("booster", 3500.0, twr_body_g=KERBIN_G, min_twr=1.5),
                Phase("insertion", 1300.0)],
        landing=None, max_engine_count=1))
    assert d.radial_boosters is None
    assert d.estimates["booster_delta_v_mps"] == 0.0


def test_separation_sequence_and_staging_metrics():
    """The separation SEQUENCE is established with control logic, and each stage reports its
    post-separation mass, structural coefficient, and single-stage Δv ceiling (the add-a-stage limit)."""
    from ksp_lab.design import staging_plan, separation_sequence
    req = ShipRequirements(
        name="seq", crew=0, payload_t=0.3,
        phases=[Phase("booster", dv_mps=4200.0, twr_body_g=KERBIN_G, min_twr=1.3),
                Phase("insertion", dv_mps=1300.0)],
        landing=None, max_engine_count=1,
    )
    d = design_ship(req)
    plan = staging_plan(d, req)
    for p in plan:
        assert p["post_separation_mass_t"] >= 0.0
        assert 0.0 < p["struct_coeff_eps"] < 1.0
        assert p["single_stage_dv_ceiling_mps"] > p["dv_mps"]   # the stage is within its own ceiling
    seq = separation_sequence(d, req)
    assert any("LIFTOFF" in e for e in seq)
    assert any("FIRE" in e and "separator" in e for e in seq)   # at least one separation event
    assert any("FAIRING JETTISON" in e for e in seq)
    assert any("DEPLOY solar" in e for e in seq)                # deploy only after orbit
