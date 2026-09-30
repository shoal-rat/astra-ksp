"""Craft spec parsing and validation: every problem is reported with part id, field and a fix."""

import json
from pathlib import Path

import pytest

from astra.craft.catalog import Catalog
from astra.craft.spec import SpecError, check_spec, parse_spec

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def cat():
    return Catalog.from_json(json.loads((FIXTURES / "parts_subset.json").read_text(encoding="utf-8")))


def base_spec(**extra):
    spec = {"name": "Test 1", "parts": [
        {"id": "pod", "part": "mk1pod.v2"},
        {"id": "tank", "part": "fuelTank", "parent": "pod", "node": "bottom"},
        {"id": "eng", "part": "liquidEngine3.v2", "parent": "tank", "node": "bottom"},
    ]}
    spec.update(extra)
    return spec


def issues_of(data):
    with pytest.raises(SpecError) as err:
        parse_spec(data)
    return err.value.issues


def test_valid_spec_parses_with_defaults():
    spec = parse_spec(json.dumps(base_spec(description="line one\nline two")))
    assert spec.root.id == "pod"
    assert spec.description == "line one | line two"
    assert spec.stages == "auto" and spec.vessel_type == "Ship"
    assert [c.id for c in spec.children("pod")] == ["tank"]
    assert spec.by_id("eng").node == "bottom" and spec.by_id("eng").symmetry == 1
    assert parse_spec(spec.to_dict()).to_dict() == spec.to_dict()


def test_structural_errors_name_part_field_and_fix():
    data = base_spec(name="../evil", extra_field=1)
    data["parts"] += [
        {"id": "tank", "part": "fuelTank", "parent": "pod", "node": "top"},  # duplicate id
        {"id": "orphan", "part": "fuelTank", "parent": "nobody", "node": "bottom"},
        {"id": "both", "part": "fuelTank", "parent": "pod", "node": "bottom", "surface": {"azimuth_deg": 0}},
        {"id": "none", "part": "fuelTank", "parent": "pod"},
        {"id": "symstack", "part": "fuelTank", "parent": "pod", "node": "top", "symmetry": 3},
        {"id": "typo", "part": "fuelTank", "parent": "pod", "parent_node": "top"},
        {"id": "limit", "part": "fuelTank", "parent": "pod", "node": "top", "thrust_limit_pct": 150},
        {"id": "fill", "part": "fuelTank", "parent": "pod", "node": "top", "resources": {"LiquidFuel": 2}},
        {"id": "fair", "part": "fairingSize1", "parent": "pod", "node": "top", "fairing": {"nose": "ogive"}},
        {"id": "badsrf", "part": "basicFin", "parent": "pod", "surface": {"azimuth": 10}},
    ]
    found = {(i.part, i.field) for i in issues_of(data)}
    expected = {(None, "name"), (None, "extra_field"), ("tank", "id"), ("orphan", "parent"), ("both", "node"),
                ("none", "node"), ("symstack", "symmetry"), ("typo", "parent_node"), ("limit", "thrust_limit_pct"),
                ("fill", "resources.LiquidFuel"), ("fair", "fairing.clearance_m"), ("badsrf", "surface.azimuth")}
    assert expected <= found
    typo = next(i for i in issues_of(data) if i.field == "parent_node")
    assert "'node'" in typo.fix
    assert "problem(s)" in str(SpecError(issues_of(data)))


def test_root_count_and_cycles():
    two_roots = {"name": "x", "parts": [{"id": "a", "part": "fuelTank"}, {"id": "b", "part": "fuelTank"}]}
    assert any(i.field == "parent" and "2 parts have no parent" in i.problem for i in issues_of(two_roots))
    cycle = {"name": "x", "parts": [{"id": "r", "part": "fuelTank"},
                                    {"id": "a", "part": "fuelTank", "parent": "b", "node": "top"},
                                    {"id": "b", "part": "fuelTank", "parent": "a", "node": "top"}]}
    assert any("loops" in i.problem for i in issues_of(cycle))
    assert issues_of("{not json")[0].field == "spec"
    assert issues_of({"name": "x", "parts": []})[0].field == "parts"


def test_catalog_checks(cat):
    data = base_spec()
    data["parts"] += [
        {"id": "ghost", "part": "fuelTank.lonng", "parent": "tank", "surface": {"azimuth_deg": 0}},
        {"id": "nonode", "part": "fuelTank", "parent": "eng", "node": "side"},
        {"id": "engsrf", "part": "liquidEngine2.v2", "parent": "tank", "surface": {"azimuth_deg": 90}},
        {"id": "onengine", "part": "basicFin", "parent": "eng", "surface": {"azimuth_deg": 90}},
        {"id": "xfeed", "part": "fuelTankSmall", "parent": "pod", "node": "top", "crossfeed": True},
        {"id": "thrust", "part": "fuelTankSmall", "parent": "tank", "surface": {"azimuth_deg": 0}, "thrust_limit_pct": 50},
        {"id": "res", "part": "basicFin", "parent": "tank", "surface": {"azimuth_deg": 0}, "resources": {"Ore": 0.5}},
        {"id": "noaz", "part": "basicFin", "parent": "tank", "surface": {"height_m": 0}},
        {"id": "clash", "part": "fuelTank", "parent": "tank", "node": "bottom"},
        {"id": "fairing", "part": "fuelTank", "parent": "eng", "node": "bottom", "fairing": {"clearance_m": 0.1}},
    ]
    issues = check_spec(parse_spec(data), cat)
    by_part = {}
    for i in issues:
        by_part.setdefault(i.part, []).append(i)
    assert "fuelTank.long" in by_part["ghost"][0].fix  # spelling suggestion
    assert "top" in by_part["nonode"][0].fix and "bottom" in by_part["nonode"][0].fix
    assert "cannot be surface-attached" in by_part["engsrf"][0].problem
    assert "allowSrfAttach" in by_part["onengine"][0].problem
    assert by_part["xfeed"][0].field == "crossfeed"
    assert by_part["thrust"][0].field == "thrust_limit_pct"
    assert by_part["res"][0].field == "resources.Ore"
    assert by_part["noaz"][0].field == "surface.azimuth_deg"
    assert any("already used" in i.problem for i in issues if i.part in ("clash", "eng"))
    assert by_part["fairing"][0].field == "fairing"


def test_manual_staging_requires_stages_on_stageable_parts(cat):
    data = base_spec(stages="manual")
    issues = check_spec(parse_spec(data), cat)
    assert [(i.part, i.field) for i in issues] == [("eng", "stage")]
    data["parts"][2]["stage"] = 0
    assert check_spec(parse_spec(data), cat) == []


def test_surface_on_radial_parent_may_omit_azimuth(cat):
    data = base_spec()
    data["parts"] += [
        {"id": "dec", "part": "radialDecoupler2", "parent": "tank", "surface": {"azimuth_deg": 0}, "symmetry": 2},
        {"id": "srb", "part": "solidBooster.v2", "parent": "dec", "surface": {}},
    ]
    assert check_spec(parse_spec(data), cat) == []


# --- review additions: text that would corrupt a .craft, unusable names ----------------------------


def test_description_is_made_confignode_safe_and_tags_that_would_corrupt_the_file_are_rejected():
    spec = parse_spec(base_spec(description="payload {probe}\n// second line"))
    assert spec.description == "payload (probe) | / second line"
    for tag in ("a } b", "x // y", "{", "two\nlines"):
        data = base_spec()
        data["parts"][1]["tag"] = tag
        assert [(i.part, i.field) for i in issues_of(data)] == [("tank", "tag")]


@pytest.mark.parametrize("name", ["CON", "nul", "Com1", "LPT9.v2", "a..b", "../x", "-x", ""])
def test_unusable_craft_names_are_rejected(name):
    assert any(i.field == "name" for i in issues_of(base_spec(name=name)))


def test_fairing_xsections_must_be_finite_numbers():
    for xs in ([[True, 0.6], [2, 0.6]], [[0, float("nan")], [2, 0.6]], [[0, 0.6], [float("inf"), 0.6]]):
        data = base_spec()
        data["parts"][1]["fairing"] = {"xsections": xs}
        assert any(i.field == "fairing.xsections" for i in issues_of(data))
