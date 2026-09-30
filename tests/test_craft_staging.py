"""Automatic inverse staging from the part tree, and explicit overrides."""

import json
from pathlib import Path

import pytest

from astra.craft.catalog import Catalog
from astra.craft.geometry import place
from astra.craft.spec import parse_spec
from astra.craft.staging import assign_stages, build_sections, separated_by

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def cat():
    return Catalog.from_json(json.loads((FIXTURES / "parts_subset.json").read_text(encoding="utf-8")))


def two_stage_with_boosters(**overrides):
    parts = [
        {"id": "pod", "part": "mk1pod.v2"},
        {"id": "chute", "part": "parachuteSingle", "parent": "pod", "node": "top"},
        {"id": "shield", "part": "HeatShield1", "parent": "pod", "node": "bottom"},
        {"id": "dec_upper", "part": "Decoupler.1", "parent": "shield", "node": "bottom"},
        {"id": "tank2", "part": "fuelTank", "parent": "dec_upper", "node": "bottom"},
        {"id": "eng2", "part": "liquidEngine3.v2", "parent": "tank2", "node": "bottom"},
        {"id": "dec_lower", "part": "Decoupler.1", "parent": "eng2", "node": "bottom"},
        {"id": "tank1", "part": "fuelTank.long", "parent": "dec_lower", "node": "bottom"},
        {"id": "eng1", "part": "liquidEngine2.v2", "parent": "tank1", "node": "bottom"},
        {"id": "rdec", "part": "radialDecoupler2", "parent": "tank1", "surface": {"azimuth_deg": 0}, "symmetry": 2},
        {"id": "srb", "part": "solidBooster.v2", "parent": "rdec", "surface": {}},
        {"id": "fin", "part": "basicFin", "parent": "tank1", "surface": {"azimuth_deg": 45, "height_m": -1.5},
         "symmetry": 4},
    ]
    for p in parts:
        p.update(overrides.get(p["id"], {}))
    return {"name": "Staging Test", "parts": parts}


def staged(cat, data):
    placement = place(parse_spec(data), cat, uid_seed=1)
    table = assign_stages(placement)
    stage_of = {}
    for p in placement.parts:
        stage_of.setdefault(p.spec.id, set()).add((p.istg, p.dstg))
    return placement, table, stage_of


def test_serial_two_stage_with_radial_boosters(cat):
    placement, table, st = staged(cat, two_stage_with_boosters())
    # launch: core engine + both boosters; boosters drop; lower stage drops as the upper engine lights;
    # the upper stage drops; parachutes last. The heat shield is not staged: KSP ships heat-shield
    # decouplers with staging disabled (verified live: HeatShield1 sits at stage -1), and staging it
    # here would renumber every stage above it.
    assert [row["stage"] for row in table] == [4, 3, 2, 1, 0]
    assert [row.get("event") for row in table] == ["launch", "drop", "stage", "stage", "parachutes"]
    assert st["eng1"] == {(4, 2)} and st["srb"] == {(4, 3)}
    assert st["rdec"] == {(3, 3)}
    assert st["dec_lower"] == {(2, 2)} and st["eng2"] == {(2, 1)}  # decoupler shares the stage it exposes
    assert st["tank1"] == {(-1, 2)} and st["fin"] == {(-1, 2)}
    assert st["dec_upper"] == {(1, 1)} and st["tank2"] == {(-1, 1)}
    assert st["shield"] == {(-1, -1)}
    assert st["chute"] == {(0, -1)} and st["pod"] == {(-1, -1)}
    assert table[0]["activates"] == ["eng1 (liquidEngine2.v2)", "srb (solidBooster.v2) x2"]
    assert "srb (solidBooster.v2) x2" in table[1]["drops"]
    assert not any("warning" in row for row in table)


def test_sections_and_separated_sets(cat):
    placement, _, _ = staged(cat, two_stage_with_boosters())
    by = {p.key: p for p in placement.parts}
    assert {p.key for p in separated_by(by["dec_lower"], placement)} >= {"dec_lower", "tank1", "eng1", "rdec#1", "srb#2"}
    assert {p.key for p in separated_by(by["rdec#1"], placement)} == {"rdec#1", "srb#1"}
    shield_side = {p.key for p in separated_by(by["shield"], placement)}  # omni decoupler: all below it
    assert "shield" in shield_side and "eng2" in shield_side and "pod" not in shield_side
    root = build_sections(placement)
    assert [p.key for p in root.parts] == ["pod", "chute"]
    (shield_sec,) = root.children
    assert shield_sec.decoupler.key == "shield" and not shield_sec.radial


def test_explicit_stage_overrides_auto(cat):
    _, table, st = staged(cat, two_stage_with_boosters(srb={"stage": 7}))
    assert st["srb"] == {(7, 3)}
    assert table[0]["stage"] == 7 and table[0]["activates"] == ["srb (solidBooster.v2) x2"]


def test_fairing_and_payload_separation_get_their_own_stages(cat):
    data = {"name": "Sat", "parts": [
        {"id": "probe", "part": "probeCoreOcto.v2"},
        {"id": "sep", "part": "Decoupler.1", "parent": "probe", "node": "bottom"},
        {"id": "fairing", "part": "fairingSize1", "parent": "sep", "node": "bottom",
         "fairing": {"clearance_m": 0.1, "nose": "ogive"}},
        {"id": "tank", "part": "fuelTank", "parent": "fairing", "node": "bottom"},
        {"id": "eng", "part": "liquidEngine3.v2", "parent": "tank", "node": "bottom"},
    ]}
    _, table, st = staged(cat, data)
    assert [(row["stage"], row["event"]) for row in table] == [(2, "launch"), (1, "fairing"), (0, "stage")]
    assert st["fairing"] == {(1, 0)} and st["sep"] == {(0, 0)} and st["probe"] == {(-1, -1)}


def test_separators_sharing_a_stage_are_flagged(cat):
    # the automatic sequence is numbered without explicitly staged parts, so the shield lands on the
    # stage that separates the lower stage; the table flags the collision for the AI to fix
    _, table, st = staged(cat, two_stage_with_boosters(shield={"stage": 2}))
    assert st["shield"] == {(2, 2)} and st["dec_lower"] == {(2, 2)}
    shared = next(row for row in table if row["stage"] == 2)
    assert "dec_lower" in shared["warning"] and "shield" in shared["warning"]


def test_manual_mode_uses_only_explicit_stages(cat):
    data = two_stage_with_boosters()
    data["stages"] = "manual"
    for p in data["parts"]:
        if p["id"] in ("eng1", "srb"):
            p["stage"] = 3
        elif p["id"] in ("rdec", "dec_lower", "eng2"):
            p["stage"] = 2
        elif p["id"] in ("dec_upper", "shield"):
            p["stage"] = 1
        elif p["id"] == "chute":
            p["stage"] = 0
    _, table, st = staged(cat, data)
    assert st["rdec"] == {(2, 2)} and st["srb"] == {(3, 2)}
    assert [row["stage"] for row in table] == [3, 2, 1, 0]


def test_parachutes_sharing_a_stage_with_a_separator_are_flagged(cat):
    _, table, _ = staged(cat, two_stage_with_boosters(chute={"stage": 1}))
    row = next(r for r in table if r["stage"] == 1)
    # the automatic sequence (4 events) is numbered without the explicitly staged chute: stage 1 is
    # the lower-stage separation, which the chute now shares
    assert row["warning"] == "parachutes ['chute'] deploy in the same stage as separator ['dec_lower']"
