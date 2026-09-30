"""Placement geometry against KSP's own saved positions.

The key test reads stock craft, converts them to specs (design_from_craft's converter), re-places
every part with our writer's geometry and compares each attachment with where KSP put it. Old stock
files were saved with older part revisions, so the comparison first uses the node offsets written
in the file (isolating our math), then the live catalog offsets (showing that only revised nodes
move parts).
"""

import copy
import json
import math
from pathlib import Path

import pytest

from astra.craft.catalog import Catalog, craft_part_name, parse_attn
from astra.craft.confignode import ConfigNode, parse_floats
from astra.craft.geometry import (q_angle_deg, q_rot, q_yaw, surface_rotation, v_len, v_sub, place)
from astra.craft.spec import parse_spec
from astra.craft.writer import spec_from_craft

FIXTURES = Path(__file__).parent / "fixtures"
CRAFTS = ["Kerbal 1-5", "AeroEquus", "Kerbal 1", "Jumping Flea", "Two-Stage Lander", "Ion-Powered Space Probe"]


@pytest.fixture(scope="module")
def cat():
    return Catalog.from_json(json.loads((FIXTURES / "parts_subset.json").read_text(encoding="utf-8")))


def file_node_offsets(root: ConfigNode) -> dict[tuple[str, str], list[float]]:
    """(part name, node id) -> node position as written in the craft's attN lines."""
    out = {}
    for pn in root.nodes("PART"):
        name = craft_part_name(pn.get("part"))
        for line in pn.get_all("attN"):
            off = parse_attn(line)[2]
            if off is not None:
                out[(name, line.split(",", 1)[0].strip())] = off
    return out


def with_file_offsets(cat: Catalog, offsets: dict) -> Catalog:
    parts = {}
    for name, p in cat.parts.items():
        q = copy.deepcopy(p)
        for n in q.nodes:
            for key in ((name, n.id), (name.removesuffix(".v2"), n.id)):
                if key in offsets:
                    n.pos = list(offsets[key])
        parts[name] = q
    return Catalog(parts, cat.resource_defs)


def compare(name: str, cat: Catalog):
    """Per-attachment position error (m) and rotation error (deg) against the stock file."""
    root = ConfigNode.load(FIXTURES / f"{name}.craft")
    spec_d, notes, source = spec_from_craft(root, cat)
    placement = place(parse_spec(spec_d), cat, uid_seed=3)
    stock = {pn.get("part").strip(): pn for pn in root.nodes("PART")}
    spos = {pid: parse_floats(pn.get("pos")) for pid, pn in stock.items()}
    srot = {pid: parse_floats(pn.get("rot")) for pid, pn in stock.items()}
    shift = v_sub(spos[source[placement.root.spec.id]], placement.root.pos)

    mapping, used = {}, set()
    for p in placement.parts:
        if "#" not in p.key and p.spec.id in source:
            mapping[id(p)] = source[p.spec.id]
        else:
            names = {p.info.name, p.info.name.removesuffix(".v2"), p.info.cfg_name}
            cands = [pid for pid in stock if craft_part_name(pid) in names and pid not in used]
            mapping[id(p)] = min(cands, key=lambda pid: v_len(v_sub(v_sub(spos[pid], shift), p.pos)))
        used.add(mapping[id(p)])

    flagged = {n.split(":", 1)[0] for n in notes if "by hand" in n or "offset tool" in n}
    rows = []
    for p in placement.parts:
        if p.parent is None or p.spec.id in flagged:
            continue
        ours = v_sub(p.pos, p.parent.pos)
        theirs = v_sub(spos[mapping[id(p)]], spos[mapping[id(p.parent)]])
        rows.append({"key": p.key, "attach": p.attach, "part": p.info.name,
                     "err_m": v_len(v_sub(ours, theirs)), "rot_deg": q_angle_deg(p.rot, srot[mapping[id(p)]]),
                     "parent_node": (p.parent.info.name, p.parent_node), "child_node": (p.info.name, p.child_node)})
    return rows, notes, root


@pytest.mark.parametrize("name", CRAFTS)
def test_attachments_match_ksp_with_the_files_node_offsets(cat, name):
    root = ConfigNode.load(FIXTURES / f"{name}.craft")
    rows, notes, _ = compare(name, with_file_offsets(cat, file_node_offsets(root)))
    stack = [r for r in rows if r["attach"] == "stack"]
    surface = [r for r in rows if r["attach"] == "surface"]
    assert stack and max(r["err_m"] for r in stack) < 0.01, max(stack, key=lambda r: r["err_m"])
    if surface:
        worst = max(surface, key=lambda r: r["err_m"])
        assert worst["err_m"] < 0.03, worst


def test_kerbal_1_5_with_live_catalog_offsets(cat):
    """With current part data only nodes revised since the file was saved move anything."""
    root = ConfigNode.load(FIXTURES / "Kerbal 1-5.craft")
    offsets = file_node_offsets(root)
    revised = set()
    for (name, node), off in offsets.items():
        info = cat.resolve(name) or cat.resolve(name + ".v2")
        n = info.node(node) if info else None
        if n is not None and v_len(v_sub(n.pos, off)) > 1e-4:
            revised.add((info.name, node))
    assert revised == {("liquidEngine3.v2", "bottom")}  # LV-909 bottom node: -0.81781 (1.6.0 file) -> -0.83 now
    rows, _, _ = compare("Kerbal 1-5", cat)
    for r in rows:
        touches_revision = r["parent_node"] in revised or r["child_node"] in revised
        limit = 0.013 if touches_revision else 0.01 if r["attach"] == "stack" else 0.03
        assert r["err_m"] < limit, r
    worst = max(rows, key=lambda r: r["err_m"])
    assert worst["parent_node"] == ("liquidEngine3.v2", "bottom") and worst["err_m"] == pytest.approx(0.0122, abs=1e-3)


def test_surface_orientation_matches_stock_convention(cat):
    root = ConfigNode.load(FIXTURES / "Kerbal 1-5.craft")
    rows, _, _ = compare("Kerbal 1-5", with_file_offsets(cat, file_node_offsets(root)))
    checked = [r for r in rows if r["part"] in ("basicFin", "radialDecoupler2", "solidBooster.v2", "RCSBlock.v2")]
    assert len(checked) == 12
    for r in checked:
        if r["part"] == "RCSBlock.v2":
            continue  # RCS blocks in this craft were tilted by hand; position still matches
        assert r["rot_deg"] < 1.0, r


@pytest.mark.parametrize("azimuth", [0, 45, 90, 180, 270])
def test_x_axis_node_yaw_is_180_minus_azimuth(cat, azimuth):
    spec = parse_spec({"name": "Yaw", "parts": [
        {"id": "tank", "part": "fuelTank"},
        {"id": "fin", "part": "basicFin", "parent": "tank", "surface": {"azimuth_deg": azimuth, "height_m": -0.5}}]})
    pl = place(spec, cat)
    fin = pl.by_key("fin")
    assert q_angle_deg(fin.rot, q_yaw(180 - azimuth)) < 1e-6
    tank = pl.by_key("tank")
    rel = v_sub(fin.pos, tank.pos)
    assert math.hypot(rel[0], rel[2]) == pytest.approx(0.625, abs=1e-6)  # FL-T400 skin
    assert math.degrees(math.atan2(rel[2], rel[0])) % 360 == pytest.approx(azimuth % 360, abs=1e-6)
    assert rel[1] == pytest.approx(-0.5)


def test_z_axis_node_points_away_from_parent_and_boosters_touch_the_decoupler(cat):
    spec = parse_spec({"name": "Booster", "parts": [
        {"id": "tank", "part": "fuelTank.long"},
        {"id": "dec", "part": "radialDecoupler2", "parent": "tank", "surface": {"azimuth_deg": 0}, "symmetry": 3},
        {"id": "srb", "part": "solidBooster.v2", "parent": "dec", "surface": {"height_m": 0.2}},
        {"id": "nose", "part": "noseCone", "parent": "srb", "node": "top"}]})
    pl = place(spec, cat)
    srbs = pl.copies("srb")
    assert len(srbs) == 3 and len(pl.copies("nose")) == 3
    for srb in srbs:
        dec = srb.parent
        outward = [dec.pos[0] - pl.root.pos[0], 0.0, dec.pos[2] - pl.root.pos[2]]
        n = [c / v_len(outward) for c in outward]
        node_dir = q_rot(srb.rot, srb.info.srf_node.dir)
        assert sum(a * b for a, b in zip(node_dir, n)) == pytest.approx(1.0, abs=1e-6)
        attach_point = v_sub(srb.pos, q_rot(srb.rot, [-c for c in srb.info.srf_node.pos]))
        from_dec = v_sub(attach_point, dec.pos)
        assert sum(a * b for a, b in zip(from_dec, n)) == pytest.approx(dec.info.outer_face_m, abs=1e-6)
        assert from_dec[1] == pytest.approx(0.2)
        assert srb.sym and len(srb.sym) == 2
    az = sorted(round(math.degrees(math.atan2(s.pos[2], s.pos[0])) % 360) for s in srbs)
    assert az == [0, 120, 240]


def test_stack_math_uses_real_asymmetric_nodes(cat):
    spec = parse_spec({"name": "Stack", "parts": [
        {"id": "pod", "part": "mk1pod.v2"},
        {"id": "tank", "part": "fuelTank", "parent": "pod", "node": "bottom"},
        {"id": "swivel", "part": "liquidEngine2.v2", "parent": "tank", "node": "bottom"}]})
    pl = place(spec, cat)
    pod, tank, swivel = (pl.by_key(k) for k in ("pod", "tank", "swivel"))
    assert pod.pos[1] - tank.pos[1] == pytest.approx(0.4050379 + 0.981725, abs=1e-6)
    assert tank.pos[1] - swivel.pos[1] == pytest.approx(0.9125 - 0.0, abs=1e-6)  # Swivel top node at 0
    assert swivel.child_node == "top" and tank.child_node == "top"
    ext = pl.extents()
    assert ext["bottom_y"] == pytest.approx(1.0)


def test_flipped_stack_part_faces_its_parent(cat):
    spec = parse_spec({"name": "Flip", "parts": [
        {"id": "tank", "part": "fuelTank"},
        {"id": "eng", "part": "liquidEngine3.v2", "parent": "tank", "node": "top", "child_node": "top"}]})
    pl = place(spec, cat)
    eng, tank = pl.by_key("eng"), pl.by_key("tank")
    top_dir = q_rot(eng.rot, eng.info.node("top").dir)
    assert top_dir[1] == pytest.approx(-1.0)  # upside down, facing the tank's top node
    assert v_len(v_sub(eng.world(eng.info.node("top").pos), tank.world(tank.info.node("top").pos))) < 1e-9


def test_top_face_attachment_with_tilt(cat):
    spec = parse_spec({"name": "Tilt", "parts": [
        {"id": "pod", "part": "mk1pod.v2"},
        {"id": "port", "part": "dockingPort3", "parent": "pod",
         "surface": {"azimuth_deg": 0, "height_m": 0.64, "tilt_deg": 90}}]})
    pl = place(spec, cat)
    port = pl.by_key("port")
    assert q_angle_deg(port.rot, [0, 0, 0, 1]) < 1e-6  # standing upright on the pod's top
    assert port.pos[0] == pytest.approx(0.0) and port.pos[2] == pytest.approx(0.0)


def test_surface_rotation_general_node_directions(cat):
    normal = [0.0, 0.0, 1.0]  # parent surface facing +Z
    for d, sign in (([1, 0, 0], -1), ([0, -1, 0], -1), ([0, 0, 1], 1), ([0, 0, -1], 1)):
        node = type("N", (), {"dir": d})()
        q = surface_rotation(node, normal)
        world = q_rot(q, d)
        assert [round(c, 9) for c in world] == [round(sign * c, 9) for c in normal]


KSP = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Kerbal Space Program")


@pytest.mark.skipif(not (KSP / "Ships" / "VAB").exists(), reason="KSP install not present")
def test_every_stock_vab_craft_converts_and_places():
    from astra.craft.spec import SpecError

    real = Catalog.build(KSP / "GameData", bridge=None)
    placed, rejected = [], {}
    for f in sorted((KSP / "Ships" / "VAB").glob("*.craft")):
        spec_d, notes, _ = spec_from_craft(ConfigNode.load(f), real)
        try:
            placement = place(parse_spec(spec_d), real)
        except SpecError as exc:  # retired parts without a successor, mod parts with model-defined nodes
            rejected[f.stem] = [i.field for i in exc.issues]
            continue
        assert len(placement.parts) >= len(spec_d["parts"])
        placed.append(f.stem)
    assert {"Kerbal 1-5", "Kerbal 1", "AeroEquus", "GDLV3", "Ion-Powered Space Probe", "PT Series Munsplorer",
            "Two-Stage Lander", "Super-Heavy Lander", "Space Station Core", "Science Jr",
            "SLS Block 1", "Orion Spacecraft", "Kerbal Landing System"} <= set(placed)  # mod parts: learned nodes
    assert len(placed) >= 25, rejected
    # what is left is honest: retired parts without a loaded successor, or a surface attach the rules forbid
    assert all(set(fields) <= {"part", "surface"} for fields in rejected.values()), rejected
