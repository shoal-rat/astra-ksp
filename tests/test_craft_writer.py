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
