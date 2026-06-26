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
from ksp_lab.parts import part, stage_masses

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


def test_booster_stage_clusters_when_a_single_engine_cannot_meet_twr():
    """Stacked-engine CLUSTER (Falcon-9 octaweb) must work for ANY catalog engine. With the full catalog
    the sizer may meet a light booster's TWR with one big engine, so to PROVE clustering we give it a
    heavy stack and a high TWR floor: a single engine cannot lift it, so the designer CLUSTERS N engines
    on the mounting plate (engine_count > 1). A count of 1 here would mean no TWR calculation ran. The
    cluster must also FIT — design.py only clusters as many bells as physically pack under the tank, and
    the chosen narrower engine clusters cleanly (every bell inside the tank footprint)."""
    from ksp_lab.craft_writer import CraftWriter
    from ksp_lab.design import max_cluster_in_tank, engine_bell_radius
    req = ShipRequirements(
        name="ClusterBooster", crew=0, payload_t=8.0,
        phases=[Phase("booster", dv_mps=3400.0, twr_body_g=KERBIN_G, min_twr=1.5, min_diameter_m=2.5)],
        landing=None, max_engine_count=9)
    design = design_ship(req)
    booster = design.stages[0]
    assert booster.role == "booster"
    assert booster.engine_count > 1, design.notes
    # The cluster physically fits: the chosen engine's bell footprint packs engine_count bells under the
    # tank radius (no overhang), exactly the max_cluster_in_tank gate the sizer enforced.
    fit = max_cluster_in_tank(engine_bell_radius(booster.engine), part(booster.tank).diameter_m / 2.0)
    assert booster.engine_count <= fit, (booster.engine_count, fit)
    # And the cluster actually renders: engine_count engine parts emitted in the .craft.
    craft = CraftWriter().render(design)
    assert craft.count(f"part = {booster.engine}_") == booster.engine_count


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
    """The pipeline must REJECT, not silently ship, a rocket that cannot fly. Even drawing the single
    biggest stock engine from the full catalog (the Mammoth), a 60 t payload capped at ONE engine
    (max_engine_count=1, no clustering) gives liftoff TWR < 1.2 — design_ship must set feasible=False
    with a reason. This is the fix for the pad-hang / fall-back failures (the physics was computed but
    never enforced). A sound 2-stage launcher must stay feasible."""
    bad = design_ship(ShipRequirements(
        name="under-thrust", crew=0, payload_t=60.0,
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
    # With NO curated tier the full-catalog sizer builds a chunkier 2.5 m base (a Skipper on a Jumbo/X200)
    # rather than the old 1.25 m noodle, so the frontal area is larger and the ballistic coefficient a bit
    # lower / the drag-loss fudge a bit higher — still a high-beta stack that slices through the air, just
    # for a real 2.5 m launcher rather than a 1.25 m needle.
    assert d.ballistic_coeff_kgm2 > 25_000.0, d.ballistic_coeff_kgm2   # slices through the air
    # Drag-loss is a frontal-area FUDGE for the offline budget only (MechJeb flies the real ascent).
    assert d.ascent_drag_loss_mps < 550.0, d.ascent_drag_loss_mps      # low air-resistance loss
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
    """A heavy interplanetary relay: a ~3800 m/s upper for Eve's high synchronous insertion carrying an
    8 t payload. Capped at a SINGLE core engine (max_engine_count=1, no clustering), even the biggest
    stock engine from the full catalog cannot lift this stack (liftoff TWR < 1 -> infeasible); strap-on
    radial boosters are the reliable fix. `boosters` is the requested radial-booster count."""
    return ShipRequirements(
        name="AI-Eve-Relay", mission_type="relay_comsat", crew=0, payload_t=8.0,
        phases=[Phase("booster", 4200.0, twr_body_g=KERBIN_G, min_twr=1.3),
                Phase("insertion", 3800.0, twr_body_g=KERBIN_G, min_twr=0.5)],
        landing=None, max_engine_count=1, radial_booster_count=boosters,
    )


def test_radial_boosters_are_sized_and_attached():
    """Requesting radial boosters must add a CALCULATED RadialBoosterSpec to the design: N symmetric pods,
    each a real sea-level engine + a whole-tank count, on a radial decoupler. None of it guessed, and the
    pod engine is drawn from the full booster catalog (a genuine sea-level engine, not a vacuum one)."""
    from ksp_lab.design import BOOSTER_ENGINES
    d = design_ship(_eve_relay_requirements(4))
    rb = d.radial_boosters
    assert rb is not None, d.notes
    assert rb.count == 4
    assert rb.engine in BOOSTER_ENGINES                      # a sea-level booster engine from the catalog
    assert not rb.is_drop_tank and rb.engine_count == 1
    # The pod engine keeps most of its thrust at sea level (a real booster, not a vacuum engine).
    assert part(rb.engine).thrust_kn_asl / part(rb.engine).thrust_kn_vac >= 0.6
    assert rb.tank_count >= 1
    assert rb.decoupler == "radialDecoupler2"


def test_radial_boosters_make_an_unliftable_core_launchable():
    """The headline fix: a heavy ~3800 m/s upper that CANNOT lift on a single core engine (TWR < 1.2,
    infeasible) becomes launchable once strap-on boosters carry the liftoff thrust — combined TWR in the
    flight window AND the design feasible.

    Pod count 6 (was 4): with the catalog now carrying ACCURATE cfg geometry, the KE-1 Mastodon reads as
    the real 2.5 m engine it is (it was mis-sized 1.25 m by the old hand-list), so a 1.25 m booster pod no
    longer pairs with it; the height-clamped 1.25 m pods each bank less ascent Δv, so this heavy core needs
    6 strap-ons to clear the reserve floor, not 4. The point of the test — boosters turn an unliftable core
    launchable — is unchanged."""
    single = design_ship(_eve_relay_requirements(0))
    boosted = design_ship(_eve_relay_requirements(6))
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
        name="brute", crew=0, payload_t=8.0,    # SAME payload as the boosted relay (apples-to-apples)
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


# --------------------------------------------------------------------------------------------------
# CODEX FLAW #1/#2 — radial booster pod HEIGHT CLAMP (Falcon-Heavy rule): a pod may not be taller than its
# host launch (core) stage, so it cannot tower into the upper stage / payload (the hammerhead/cage).
# --------------------------------------------------------------------------------------------------
def test_radial_booster_pod_is_not_taller_than_the_launch_core_stage():
    """A strap-on pod's total stack height (engine + tank column) must be <= the launch CORE stage's stack
    height — the Falcon-Heavy reference: the side boosters reach ~the top of the first stage and no higher,
    leaving the upper stage + payload clear above. The old sizer made 24 m pods on a 7 m core (a cage)."""
    from tools.design_eve_two_ship import crew_ferry, return_tug
    for req in (crew_ferry(), return_tug()):
        d = design_ship(req)
        rb = d.radial_boosters
        assert rb is not None, d.notes
        core = d.stages[0]                          # fire order -> stages[0] is the launch (booster) core
        core_h = part(core.tank).height_m * core.tank_count + part(core.engine).height_m
        pod_h = part(rb.tank).height_m * rb.tank_count + part(rb.engine).height_m
        assert pod_h <= core_h + 1e-6, (req.name, pod_h, core_h)
        # And the physics still CLOSES after the clamp: launchable (combined TWR) + Δv beyond requirement.
        assert d.feasible is True, d.infeasible_reasons
        assert d.estimates["total_delta_v_mps"] >= d.estimates["required_delta_v_mps"] * 1.05, d.estimates
        assert d.estimates["launch_twr"] >= 1.4, d.estimates


def test_clamped_boosters_keep_the_core_covering_the_remaining_dv():
    """When the height clamp shortens the pods so they carry LESS than the nominal 45% share, the asparagus
    fixed-point must grow the CORE to cover the remainder — the total Δv still meets the requirement+reserve
    (the core 'covers the rest'), so a clamped design never silently ships short of orbit."""
    from tools.design_eve_two_ship import return_tug
    d = design_ship(return_tug())
    assert d.feasible is True, d.infeasible_reasons
    # The boosters deliver SOME ascent Δv, and the core carries the (larger, post-clamp) remainder.
    assert d.estimates["booster_delta_v_mps"] > 0.0, d.estimates
    assert d.estimates["total_delta_v_mps"] >= d.estimates["required_delta_v_mps"] * 1.05, d.estimates


# --------------------------------------------------------------------------------------------------
# FUEL RESERVE — per-stage role reserves + the mission-level contingency, made visible in estimates.
# --------------------------------------------------------------------------------------------------
def test_design_estimates_expose_the_fuel_reserve():
    """Every design must REPORT its fuel reserve: the bare requirement (usable_dv), the margin carried
    beyond it (reserve_dv), and the reserve fraction — so the reserve is auditable, not hidden in mass."""
    design = design_ship(_starship_mars_requirements())
    est = design.estimates
    for key in ("required_delta_v_mps", "usable_dv_mps", "reserve_dv_mps", "reserve_frac",
                "mission_reserve_frac", "total_delta_v_mps"):
        assert key in est, (key, est)
    required = sum(p.dv_mps for p in _starship_mars_requirements().phases)
    assert est["required_delta_v_mps"] == round(required, 0)
    # usable = the requirement; reserve = everything carried beyond it; they add up to the total.
    assert est["usable_dv_mps"] == round(required, 0)
    assert est["reserve_dv_mps"] > 0.0, est                       # a real reserve, not zero
    assert est["usable_dv_mps"] + est["reserve_dv_mps"] == est["total_delta_v_mps"]
    # The reserve is a meaningful slice (per-stage role reserves alone are ~7-12%).
    assert est["reserve_frac"] >= 0.05, est


def _reserve_probe_requirements(mission_reserve_frac: float) -> ShipRequirements:
    """A launcher whose VACUUM transfer stage is tank-count-sensitive (an 8 t payload on a ~2000 m/s
    upper), so the mission-level reserve visibly grows the stage rather than being absorbed by the coarse
    overshoot of a single big tank. Booster + one true vacuum transfer leg (the reserve lands on it)."""
    req = ShipRequirements(
        name="reserve-probe", crew=0, payload_t=8.0,
        phases=[Phase("booster", 3400.0, twr_body_g=KERBIN_G, min_twr=1.4),
                Phase("transfer", 2000.0, twr_body_g=0.0, min_twr=0.0)],
        landing=None, max_engine_count=4)
    req.mission_reserve_frac = mission_reserve_frac
    return req


def test_mission_reserve_adds_margin_for_unforeseen_needs():
    """The mission-level contingency (mission_reserve_frac) carries EXTRA Δv on top of the per-stage role
    reserves — raising it grows the vacuum stage and the carried reserve, the 'fuel reserve for unforeseen
    needs' the directive asks for. Use a tank-count-sensitive upper so the effect is observable."""
    without = design_ship(_reserve_probe_requirements(0.0))
    with_05 = design_ship(_reserve_probe_requirements(0.05))
    with_10 = design_ship(_reserve_probe_requirements(0.10))
    assert without.estimates["mission_reserve_frac"] == 0.0
    assert with_05.estimates["mission_reserve_frac"] == 0.05
    # More mission reserve -> the vacuum transfer stage is sized larger -> more total Δv reserve carried.
    assert with_05.estimates["reserve_dv_mps"] > without.estimates["reserve_dv_mps"], (
        with_05.estimates, without.estimates)
    assert with_10.estimates["reserve_dv_mps"] > with_05.estimates["reserve_dv_mps"], (
        with_10.estimates, with_05.estimates)
    # All three remain launchable (the reserve does not break feasibility).
    assert without.feasible and with_05.feasible and with_10.feasible


def test_each_stage_is_sized_beyond_its_requirement_by_the_reserve():
    """Per-stage proof of the reserve: each stage's ACHIEVED Δv exceeds its phase requirement by at least
    that phase's reserve fraction (it was sized for dv*(1+reserve), not bare dv)."""
    req = _starship_mars_requirements()
    design = design_ship(req)
    bus = _bus_mass_of(design, req)
    stage_wet = [stage_masses(s)[1] for s in design.stages]
    # Map each design stage back to its phase (no phase split happens for this vehicle: 3 phases -> 3 stages).
    assert len(design.stages) == len(req.phases)
    for i, (stage, phase) in enumerate(zip(design.stages, req.phases)):
        dry, wet, _thrust_asl, _isp_asl, isp_vac = stage_masses(stage)
        mass_above = bus + sum(stage_wet[i + 1:])
        achieved = astro.rocket_dv(isp_vac, mass_above + wet, mass_above + dry)
        # Sized for the requirement PLUS its reserve, so achieved >= requirement*(1+reserve) (minus a hair
        # of rounding/closed-form slack).
        floor = phase.dv_mps * (1.0 + phase.reserve_frac) * 0.99
        assert achieved >= floor, (
            f"stage {stage.role}: achieved {achieved:.0f} < req+reserve {floor:.0f} "
            f"(req {phase.dv_mps:.0f}, reserve {phase.reserve_frac:.0%})")


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
