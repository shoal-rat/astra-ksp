from pathlib import Path

import pytest

from ksp_lab.craft_writer import CraftValidationError, CraftWriter, resolve_craft_path
from ksp_lab.mission import MissionPlanner
from ksp_lab.optimizer import HistoryOptimizer


def test_craft_writer_outputs_parts(tmp_path: Path):
    mission = MissionPlanner().interpret("deliver payload to 80 km Kerbin orbit")
    design = HistoryOptimizer(mission).first_design()
    path = CraftWriter().write(design, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "ship = " in text
    assert "persistentId = 2147483001" in text
    assert "missionFlag = Squad/Flags/default" in text
    # Real KSP craft omit the top-level ACTIONGROUPS / Override* fields; emitting them made the
    # editor launch finalization NullReference, so render() must not produce them.
    assert "ACTIONGROUPS" not in text
    assert "OverrideDefault" not in text
    assert "PART" in text
    assert "attN =" in text
    assert "RESOURCE" in text


def test_craft_path_rejects_traversal(tmp_path: Path):
    with pytest.raises(CraftValidationError):
        resolve_craft_path(tmp_path, "../bad")


def test_extract_part_body_matches_exact_part_and_returns_modules():
    from ksp_lab.craft_writer import _extract_part_body

    source = "\n".join(
        [
            "ship = src",
            "version = 1.12.5",
            "type = VAB",
            "PART",
            "{",
            "\tpart = liquidEngine3.v2_42",
            "\tmodSize = 0,0,0",
            "\tEVENTS",
            "\t{",
            "\t}",
            "\tACTIONS",
            "\t{",
            "\t}",
            "\tMODULE",
            "\t{",
            "\t\tname = ModuleEnginesFX",
            "\t}",
            "}",
        ]
    )
    body = _extract_part_body(source, "liquidEngine3.v2")
    assert body is not None
    assert body.strip().startswith("EVENTS")
    assert "ModuleEnginesFX" in body

    # Exact-name guard: searching a prefix must not match a longer part id.
    long_only = source.replace("liquidEngine3.v2_42", "fuelTank.long_5")
    assert _extract_part_body(long_only, "fuelTank") is None
    assert _extract_part_body(long_only, "fuelTank.long") is not None


def test_render_splices_provided_part_bodies():
    mission = MissionPlanner().interpret("deliver payload to 80 km Kerbin orbit")
    design = HistoryOptimizer(mission).first_design()
    engine = design.stages[0].engine
    bodies = {engine: "\tEVENTS\n\t{\n\t}\n\tMODULE\n\t{\n\t\tname = ModuleTestMarker\n\t}"}
    text = CraftWriter().render(design, part_bodies=bodies)
    assert "ModuleTestMarker" in text


def _relay_comsat_design():
    """The calculated 2-stage RA-100 relay comsat (uncrewed, no legs) — the design that rides a fairing."""
    from ksp_lab.design import Phase, ShipRequirements, design_ship, default_reserve_frac

    req = ShipRequirements(
        name="AI-Relay-Probe", mission_type="relay_comsat", crew=0, payload_t=0.3,
        phases=[Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3, reserve_frac=default_reserve_frac(9.81)),
                Phase("insertion", 1300.0, twr_body_g=0.0, min_twr=0.0, reserve_frac=default_reserve_frac(0.0))],
        landing=None, needs_legs=False, needs_heatshield=False, needs_docking=False, max_engine_count=1)
    return design_ship(req)


def test_probe_comsat_rides_in_a_fairing_not_a_nose_cone():
    # An uncrewed, no-legs comsat must be enclosed in a payload fairing — and must NOT carry a separate
    # nose cone, because the fairing's ogive shell IS the streamlined nose.
    design = _relay_comsat_design()
    text = CraftWriter().render(design, part_bodies=None)
    assert "fairingSize1" in text
    assert "noseCone" not in text


def _eve_boosted_design():
    """The heavy Eve relay with 4 strap-on radial boosters (asparagus launch stage)."""
    from ksp_lab.design import Phase, ShipRequirements, design_ship, default_reserve_frac

    req = ShipRequirements(
        name="AI-Eve-Relay", mission_type="relay_comsat", crew=0, payload_t=0.3,
        phases=[Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3, reserve_frac=default_reserve_frac(9.81)),
                Phase("insertion", 3800.0, twr_body_g=9.81, min_twr=0.5, reserve_frac=default_reserve_frac(0.0))],
        landing=None, needs_legs=False, needs_heatshield=False, needs_docking=False,
        max_engine_count=1, radial_booster_count=4)
    return design_ship(req)


def test_radial_boosters_render_as_jettisonable_strap_on_pods():
    # The craft must carry the 4 pods (engine + tanks) each on a radial decoupler, surface-attached to the
    # core so they cluster around the base, not inline.
    design = _eve_boosted_design()
    rb = design.radial_boosters
    text = CraftWriter().render(design, part_bodies=None)
    assert text.count("part = radialDecoupler2") == 4               # one decoupler per pod
    assert text.count(f"part = {rb.engine}") >= 4                   # at least the 4 pod engines
    assert text.count(f"part = {rb.tank}") >= 4 * rb.tank_count     # all pod tanks rendered
    assert "srfN = srfAttach" in text                               # pods are radially (surface) attached


def test_radial_boosters_ignite_with_core_and_decouple_first():
    # Staging: the booster engines + core ignite together at the TOP inverse-stage (T0); the radial
    # decouplers fire exactly ONE stage below that (boosters drop FIRST), above the core/upper separation.
    from ksp_lab.craft_writer import CraftWriter as CW
    design = _eve_boosted_design()
    rb = design.radial_boosters
    nodes = CW()._build_nodes(design, part_bodies=None)
    by_name_stage = [(n.part_name, n.stage_index) for n in nodes]
    launch_istg = len(design.stages) + 1
    # The radial decouplers all fire one stage below the T0 ignition.
    dec_stages = {s for nm, s in by_name_stage if nm == "radialDecoupler2"}
    assert dec_stages == {launch_istg - 1}, dec_stages
    # The pod engines ignite AT the T0 launch stage (same as the core), so they light together.
    pod_engine_stages = {s for nm, s in by_name_stage if nm == rb.engine}
    assert launch_istg in pod_engine_stages, pod_engine_stages
    # Boosters (launch_istg) decouple (launch_istg-1) BEFORE the core/upper inter-stage decoupler fires.
    assert launch_istg - 1 > 0


def _crewed_design(name: str = "AI-Crew-Test"):
    """A crewed Eve-style vehicle (crew>=1, forward heat shield, Kerbin chutes) — the design the crewed
    launcher must WRITE so a kerbal can board. Built directly from ShipRequirements (no kRPC)."""
    from ksp_lab.design import (LandingSite, Phase, ShipRequirements, default_reserve_frac, design_ship)

    req = ShipRequirements(
        name=name, mission_type="crewed_launch", crew=1, payload_t=0.3,
        phases=[Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3, reserve_frac=default_reserve_frac(9.81)),
                Phase("insertion", 3600.0, twr_body_g=9.81, min_twr=0.5, reserve_frac=default_reserve_frac(0.0))],
        # Kerbin re-entry chutes (sized from Kerbin's gravity + sea-level density).
        landing=LandingSite(body_g=9.81, surface_rho=1.225, target_touchdown_mps=6.0),
        needs_legs=False, needs_heatshield=True, needs_docking=False,
        max_engine_count=1, radial_booster_count=4)
    return design_ship(req)


# Crewable stock command/cabin parts and their KSP crew capacities. The Mk1 pod is the crewable COMMAND
# part the headless launch's probe core does NOT provide a seat for (the probe core has capacity 0). A
# crewed craft MUST render at least one of these with a free seat or no kerbal can board (the live bug:
# kRPC crew_capacity == 0 / /spawn-crew "No crewable part with a free seat found").
_CREW_CAPACITY = {"mk1pod.v2": 1, "crewCabin": 2}


def test_crewed_design_renders_a_crewable_mk1_pod():
    # REGRESSION (live bug): a crewed design must render a crewable Mk1 command pod as the command part so a
    # kerbal can board — not a headless probe core only. The probeCoreOcto may remain as an inline control
    # source for the headless ascent, but it provides NO crew seat, so the rendered craft must also carry a
    # crewable pod. Asserts on BOTH render paths (minimal/offline and module-spliced).
    design = _crewed_design()
    assert design.crewed is True
    for part_bodies in (None, {"mk1pod.v2": "\tEVENTS\n\t{\n\t}\n\tMODULE\n\t{\n\t\tname = ModuleCommand\n\t}"}):
        text = CraftWriter().render(design, part_bodies=part_bodies)
        assert "part = mk1pod" in text, "crewed craft is missing the crewable Mk1 command pod"
        assert text.count("part = mk1pod") >= 1
        # Total crew capacity from the rendered command/cabin parts must seat at least the 1 crew.
        import re

        total_capacity = sum(
            cap * len(re.findall(rf"part = {re.escape(part_name)}_\d", text))
            for part_name, cap in _CREW_CAPACITY.items()
        )
        # The design carries a `crewed` flag (not a crew count); it was built from crew=1, so seat >= 1.
        assert total_capacity >= 1, f"rendered crew capacity {total_capacity} cannot seat a kerbal"


def test_crewed_reentry_capsule_rides_in_a_fairing_keeping_pod_heatshield_chutes():
    # REGRESSION (live bug): the crewed Eve vehicle flew with an EXPOSED Mk1 pod + forward heat shield at
    # the nose. With no payload fairing the ascent stack reads as unfaired (Cd +0.12 -> ~0.35 vs the
    # relay's ~0.23), so the blunt capsule made the gravity turn draggy/unstable and the vehicle burned
    # all its Δv suborbital. A crewed REENTRY CAPSULE (heat shield + chutes, no docking port) must now ride
    # INSIDE a payload fairing like the relay — AND still retain the crewable pod + heat shield + chutes so
    # that, once the fairing is jettisoned in orbit, the Kerbin-return reentry + chute recovery still work.
    design = _crewed_design()
    assert design.crewed is True and design.heatshield is True and design.docking_port is False

    # Module-spliced render path (mirrors the live harvested-bodies launch where the fairing penalty bit).
    # Provide minimal bodies for the fairing + pod so both can_emit and the fairing branch fire.
    part_bodies = {
        "fairingSize1": (
            "\tEVENTS\n\t{\n\t}\n\tMODULE\n\t{\n\t\tname = ModuleProceduralFairing\n"
            "\t\tXSECTION\n\t\t{\n\t\t\th = 0\n\t\t\tr = 0.625\n\t\t}\n\t}"
        ),
        "mk1pod.v2": "\tEVENTS\n\t{\n\t}\n\tMODULE\n\t{\n\t\tname = ModuleCommand\n\t}",
        "HeatShield1": "\tEVENTS\n\t{\n\t}\n\tMODULE\n\t{\n\t\tname = ModuleAblator\n\t}",
        "parachuteSingle": "\tEVENTS\n\t{\n\t}\n\tMODULE\n\t{\n\t\tname = ModuleParachute\n\t}",
    }
    for pb in (None, part_bodies):
        nodes = CraftWriter()._build_nodes(design, part_bodies=pb)
        names = [n.part_name for n in nodes]
        # 1) A payload fairing now shrouds the capsule for ascent.
        assert "fairingSize1" in names, "crewed reentry capsule is missing its ascent payload fairing"
        # The fairing's ogive shell IS the nose, so there is NO separate nose cone on a faired capsule
        # (a body part above the fairing would poke out of the shroud).
        assert "noseCone" not in names, "a faired capsule must not also carry a nose cone"
        # 2) Pod + heat shield + chutes are STILL present so reentry + recovery survive the in-orbit
        #    fairing jettison (which removes only the shell, leaving the enclosed parts intact).
        assert "mk1pod.v2" in names, "crewed capsule lost its crewable Mk1 pod"
        assert "HeatShield1" in names, "crewed capsule lost its reentry heat shield"
        assert names.count("parachuteSingle") >= 1, "crewed capsule lost its recovery chutes"
        # 3) The fairing makes the ascent read as aerodynamically clean — Cd near the relay's ~0.23,
        #    not the ~0.35 unfaired blunt-capsule value, and the ascent margin gate stays True.
        assert design.drag_cd <= 0.26, f"faired crewed Cd should be near the relay ~0.23, got {design.drag_cd}"
        assert design.ascent_stable is True


def test_fairing_xsection_shell_is_overridden_to_wrap_this_payload():
    # The harvested fairing module keeps its real KSP serialization, but its XSECTION ogive (sized for the
    # donor craft) is replaced by the computed shell that wraps THIS payload — so a tall bus is fully
    # enclosed, not left poking out of a too-short donor shroud.
    design = _relay_comsat_design()
    donor = (
        "\tEVENTS\n\t{\n\t}\n\tMODULE\n\t{\n\t\tname = ModuleProceduralFairing\n"
        "\t\tXSECTION\n\t\t{\n\t\t\th = 0\n\t\t\tr = 0.625\n\t\t}\n"
        "\t\tXSECTION\n\t\t{\n\t\t\th = 1.1\n\t\t\tr = 0.5\n\t\t}\n"
        "\t\tXSECTION\n\t\t{\n\t\t\th = 1.8\n\t\t\tr = 0.2\n\t\t}\n\t}"
    )
    text = CraftWriter().render(design, part_bodies={"fairingSize1": donor})
    assert "ModuleProceduralFairing" in text          # real module preserved
    assert text.count("XSECTION") == 4                # donor's 3 sections replaced by the computed 4
