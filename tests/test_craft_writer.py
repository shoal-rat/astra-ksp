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
