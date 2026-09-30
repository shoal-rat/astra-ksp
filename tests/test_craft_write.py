"""The .craft writer: structure KSP expects, only deliberate module state, faithful round trip."""

import json
import re
from pathlib import Path

import pytest

from astra.craft.catalog import Catalog, craft_part_name, parse_attn
from astra.craft.confignode import ConfigNode, parse_floats
from astra.craft.geometry import place, q_rot, v_add, v_len, v_sub
from astra.craft.spec import parse_spec
from astra.craft.staging import assign_stages
from astra.craft.writer import payload_envelope, render, spec_from_craft, write_craft
from astra.errors import AstraError

FIXTURES = Path(__file__).parent / "fixtures"
DELIBERATE_MODULES = {"ModuleEngines", "ModuleEnginesFX", "ModuleToggleCrossfeed", "ModuleProceduralFairing",
                      "KOSNameTag"}


@pytest.fixture(scope="module")
def cat():
    return Catalog.from_json(json.loads((FIXTURES / "parts_subset.json").read_text(encoding="utf-8")))


SPEC = {
    "name": "Writer Test-1",
    "description": "first line\nsecond line",
    "vessel_type": "Probe",
    "parts": [
        {"id": "probe", "part": "probeCoreOcto.v2"},
        {"id": "sep", "part": "Decoupler.1", "parent": "probe", "node": "bottom", "tag": "payload_sep"},
        {"id": "fairing", "part": "fairingSize1", "parent": "sep", "node": "bottom",
         "fairing": {"clearance_m": 0.15, "nose": "ogive"}},
        {"id": "tank2", "part": "fuelTank", "parent": "fairing", "node": "bottom", "resources": {"LiquidFuel": 0.5}},
        {"id": "eng2", "part": "liquidEngine3.v2", "parent": "tank2", "node": "bottom"},
        {"id": "dec", "part": "Decoupler.1", "parent": "eng2", "node": "bottom"},
        {"id": "tank1", "part": "fuelTank.long", "parent": "dec", "node": "bottom"},
        {"id": "eng1", "part": "liquidEngine2.v2", "parent": "tank1", "node": "bottom", "thrust_limit_pct": 85},
        {"id": "rdec", "part": "radialDecoupler2", "parent": "tank1", "surface": {"azimuth_deg": 90, "height_m": 0.3},
         "symmetry": 3, "crossfeed": True},
        {"id": "pod_tank", "part": "fuelTank", "parent": "rdec", "surface": {}},
        {"id": "pod_eng", "part": "liquidEngine3.v2", "parent": "pod_tank", "node": "bottom"},
        {"id": "fin", "part": "basicFin", "parent": "tank1", "surface": {"azimuth_deg": 30, "height_m": -1.6},
         "symmetry": 3},
    ],
}


@pytest.fixture(scope="module")
def built(cat):
    placement = place(parse_spec(SPEC), cat, uid_seed=11)
    assign_stages(placement)
    text = render(placement, seed=11)
    return placement, text, ConfigNode.parse(text)


def test_header_and_forbidden_blocks(built):
    _, text, root = built
    assert root.get("ship") == "Writer Test-1" and root.get("version") == "1.12.5"
    assert root.get("type") == "VAB" and root.get("vesselType") == "Probe"
    assert root.get("description") == "first line | second line"
    assert "ACTIONGROUPS" not in text and "STAGES" not in text
    assert [n.name for n in root.nodes()] == ["PART"] * 20  # 8 stack parts + 4 symmetric groups of 3
    assert len(parse_floats(root.get("size"))) == 3


def test_only_deliberate_module_state_is_written(built):
    placement, text, root = built
    names = [m.get("name") for pn in root.nodes("PART") for m in pn.nodes("MODULE")]
    assert set(names) <= DELIBERATE_MODULES
    assert sorted(names) == sorted(["KOSNameTag", "ModuleProceduralFairing", "ModuleEngines"]
                                   + ["ModuleToggleCrossfeed"] * 3)
    for field in ("EngineIgnited", "currentThrottle", "staged", "selectedVariant", "flameout"):
        assert field not in text  # nothing copied from other craft files
    by_part = {pn.get("part"): pn for pn in root.nodes("PART")}
    eng1 = next(pn for pid, pn in by_part.items() if pid.startswith("liquidEngine2.v2_"))
    assert eng1.node("MODULE").get("thrustPercentage") == "85"
    decs = [pn for pid, pn in by_part.items() if pid.startswith("radialDecoupler2_")]
    assert all(pn.node("MODULE").get("crossfeedStatus") == "True" for pn in decs)
    sep = next(pn for pn in by_part.values() if pn.node("MODULE") is not None
               and pn.node("MODULE").get("name") == "KOSNameTag")
    assert sep.node("MODULE").get("nameTag") == "payload_sep"


def test_part_blocks_links_nodes_and_symmetry(built, cat):
    placement, _, root = built
    blocks = {pn.get("part"): pn for pn in root.nodes("PART")}
    assert len(blocks) == 20  # unique ids
    for pid, pn in blocks.items():
        assert re.fullmatch(r"[A-Za-z0-9.\-]+_\d{10}", pid)
        info = cat.get(craft_part_name(pid))
        for child in pn.get_all("link"):
            assert child in blocks
        for att in pn.get_all("attN"):
            node_id = att.split(",", 1)[0]
            assert parse_attn(att)[1] in blocks
            assert parse_attn(att)[2] == pytest.approx(info.node(node_id).pos, abs=1e-6)
        if pn.get("srfN"):
            assert pn.get("attm") == "1" and pn.get("srfN").startswith("srfAttach,")
            assert pn.get("srfN").split(",")[1] in blocks
        for mate in pn.get_all("sym"):
            assert pid in blocks[mate].get_all("sym")
    groups = [pn.get_all("sym") for pn in blocks.values() if pn.get_all("sym")]
    assert sorted(len(g) for g in groups) == [2] * 12  # rdec, pod_tank, pod_eng, fin: 3 copies each


def test_stack_children_sit_exactly_where_ksp_resnaps_them(built):
    _, _, root = built
    blocks = {pn.get("part"): pn for pn in root.nodes("PART")}
    parent_of = {c: pid for pid, pn in blocks.items() for c in pn.get_all("link")}
    checked = 0
    for pid, pn in blocks.items():
        if pn.get("srfN") or pid not in parent_of:
            continue
        par = blocks[parent_of[pid]]
        own = next(parse_attn(a)[2] for a in pn.get_all("attN") if parse_attn(a)[1] == parent_of[pid])
        theirs = next(parse_attn(a)[2] for a in par.get_all("attN") if parse_attn(a)[1] == pid)
        expected = v_sub(v_add(parse_floats(par.get("pos")), q_rot(parse_floats(par.get("rot")), theirs)),
                         q_rot(parse_floats(pn.get("rot")), own))
        assert v_len(v_sub(parse_floats(pn.get("pos")), expected)) < 1e-5
        checked += 1
    assert checked == 10


def test_resources_and_stages(built, cat):
    placement, _, root = built
    tank2 = next(pn for pn in root.nodes("PART") if pn.get("part").startswith("fuelTank_")
                 and not pn.get("srfN"))
    res = {r.get("name"): (float(r.get("amount")), float(r.get("maxAmount"))) for r in tank2.nodes("RESOURCE")}
    assert res == {"LiquidFuel": (90.0, 180.0), "Oxidizer": (220.0, 220.0)}
    probe = next(pn for pn in root.nodes("PART") if pn.get("part").startswith("probeCoreOcto.v2_"))
    assert {r.get("name") for r in probe.nodes("RESOURCE")} == set(cat.get("probeCoreOcto.v2").resources)
    istg = {p.spec.id: int(pn.get("istg")) for p, pn in zip(placement.parts, root.nodes("PART"))}
    assert istg["eng1"] == istg["pod_eng"] == max(istg.values())


def test_fairing_shell_encloses_the_payload(built):
    placement, _, root = built
    base = placement.by_key("fairing")
    height, reach, keys = payload_envelope(placement, base)
    assert set(keys) == {"probe", "sep"}
    fair = next(m for pn in root.nodes("PART") for m in pn.nodes("MODULE") if m.get("name") == "ModuleProceduralFairing")
    xs = [(float(x.get("h")), float(x.get("r"))) for x in fair.nodes("XSECTION")]
    assert xs[0] == (0.0, 0.625)  # the 1.25 m base, like stock fairings (ComSat Lx: h=0 r=0.625)
    assert all(b[0] > a[0] for a, b in zip(xs, xs[1:]))
    body = [r for h, r in xs[1:] if h <= height + 0.15]
    assert body and min(body) >= reach + 0.15 - 1e-6  # clearance all round the payload
    assert xs[-1][0] > height + 0.15 and xs[-1][1] < xs[-2][1]


def test_write_refuses_overwrite_and_bad_names(built, cat, tmp_path):
    placement, _, _ = built
    target = tmp_path / "Ships" / "VAB" / "Writer Test-1.craft"
    path, notes = write_craft(placement, target, seed=1)
    assert path.read_bytes().count(b"\r\n") == 0 and path.read_text(encoding="utf-8").startswith("ship = ")
    assert any("XSECTION" in n or "fairing" in n for n in notes)
    with pytest.raises(AstraError):
        write_craft(placement, target)
    write_craft(placement, target, overwrite=True)
    placement.spec.name = "../escape"
    with pytest.raises(AstraError):
        write_craft(placement, tmp_path / "x.craft", overwrite=True)
    placement.spec.name = "Writer Test-1"


def test_round_trip_back_to_an_equivalent_spec(built, cat):
    placement, text, root = built
    spec, notes, source = spec_from_craft(root, cat)
    assert notes == []
    parts = {p["part"] + ":" + str(p.get("parent")) for p in spec["parts"]}
    assert len(spec["parts"]) == len(SPEC["parts"])
    by_orig = {p["id"]: p for p in SPEC["parts"]}
    back = {source[p["id"]]: p for p in spec["parts"]}
    for p, pn in zip(placement.parts, root.nodes("PART")):
        if "#" in p.key and not p.key.endswith("#1"):
            continue
        entry = back[pn.get("part")]
        orig = by_orig[p.spec.id]
        assert entry["part"] == orig["part"]
        if "node" in orig:
            assert entry["node"] == orig["node"]
        if "surface" in orig:
            assert entry.get("symmetry", 1) == orig.get("symmetry", 1)
            s = entry["surface"]
            if "azimuth_deg" in orig["surface"]:
                assert s["azimuth_deg"] == pytest.approx(orig["surface"]["azimuth_deg"], abs=1e-3)
                assert s.get("height_m", 0) == pytest.approx(orig["surface"]["height_m"], abs=1e-3)
            assert "radius_m" not in s
        for key in ("thrust_limit_pct", "crossfeed", "tag"):
            assert entry.get(key) == orig.get(key)
        if "resources" in orig:
            assert entry["resources"] == orig["resources"]
        if "fairing" in orig:
            assert len(entry["fairing"]["xsections"]) >= 4
    assert parts  # the rebuilt spec validates and places again
    again = place(parse_spec(spec), cat, uid_seed=11)
    assert len(again.parts) == len(placement.parts)


# --- review additions ------------------------------------------------------------------------------


def test_free_text_cannot_break_the_craft_file(cat):
    spec = dict(SPEC, description="shell {inner} // not a comment")
    placement = place(parse_spec(spec), cat, uid_seed=5)
    assign_stages(placement)
    root = ConfigNode.parse(render(placement, seed=5))
    assert root.get("description") == "shell (inner) / not a comment"
    assert [n.name for n in root.nodes()] == ["PART"] * 20
    placement.spec.description = "raw {text} set after parsing"  # the writer cleans it again
    placement.parts[1].spec.tag = "sep } 2"
    text = render(placement, seed=5)
    root = ConfigNode.parse(text)
    assert root.get("description") == "raw (text) set after parsing" and "{text}" not in text
    assert [n.name for n in root.nodes()] == ["PART"] * 20
    placement.parts[1].spec.tag = "payload_sep"


def test_non_finite_geometry_is_refused_not_written(cat, tmp_path):
    placement = place(parse_spec(SPEC), cat, uid_seed=5)
    assign_stages(placement)
    placement.parts[3].pos[0] = float("nan")
    with pytest.raises(AstraError):
        write_craft(placement, tmp_path / "nan.craft")
    assert not (tmp_path / "nan.craft").exists()


def test_fairing_envelope_ignores_tall_boosters_on_the_stage_below(cat):
    from astra.craft.geometry import local_extent_y
    from astra.craft.writer import fairing_profile
    spec = {"name": "Envelope", "parts": [
        {"id": "probe", "part": "probeCoreOcto.v2"},
        {"id": "fairing", "part": "fairingSize1", "parent": "probe", "node": "bottom", "fairing": {"clearance_m": 0.1}},
        {"id": "tank", "part": "fuelTankSmallFlat", "parent": "fairing", "node": "bottom"},
        {"id": "eng", "part": "liquidEngine3.v2", "parent": "tank", "node": "bottom"},
        {"id": "rdec", "part": "radialDecoupler2", "parent": "tank", "surface": {"azimuth_deg": 0, "height_m": 0.1},
         "symmetry": 2},
        {"id": "srb", "part": "solidBooster.v2", "parent": "rdec", "surface": {}},
    ]}
    placement = place(parse_spec(spec), cat, uid_seed=3)
    assign_stages(placement)
    base = placement.by_key("fairing")
    top_y = base.node_world("top")[1]
    tops = [max(q.world([0.0, y, 0.0])[1] for y in local_extent_y(q.info)) for q in placement.copies("srb")]
    assert min(tops) > top_y + 0.5  # precondition: the boosters rise well above the fairing base
    height, reach, keys = payload_envelope(placement, base)
    assert keys == ["probe"] and reach < 1.0
    assert max(r for _, r in fairing_profile(placement, base, placement.spec.by_id("fairing").fairing)) < 1.2
