""".craft serialization for a placed, staged design.

Only what the design deliberately sets is written: geometry, links, staging, symmetry, resources
(every resource the part holds, at the spec's fill fraction or the prefab amount) and MODULE blocks
for explicit options — thrust limiter, crossfeed toggle, procedural-fairing shell, name tag. KSP
builds every other module from the part prefab, so no settings leak in from other craft files.
The file never has top-level ACTIONGROUPS or STAGES blocks (a malformed ACTIONGROUPS block broke
launches in the old project); the description is a single line.
"""

from __future__ import annotations

import math
import random
import re
from pathlib import Path
from typing import Any

from astra.craft.catalog import Catalog, craft_part_name, parse_attn
from astra.craft.confignode import ConfigNode, fmt_float, parse_bool, parse_floats
from astra.craft.geometry import (Placed, Placement, local_extent_y, outer_extent, outward_dir, q_angle_deg,
                                  q_inv, q_mul, q_rot, srf_target_sign, surface_radius, surface_rotation,
                                  v_add, v_norm, v_scale, v_sub)
from astra.craft.spec import FairingSpec, confignode_safe, default_child_node, valid_craft_name
from astra.errors import AstraError

KSP_VERSION = "1.12.5"


def _vec(v: list[float]) -> str:
    return ",".join(fmt_float(c) for c in v)


def _bar(v: list[float]) -> str:
    return "|".join(fmt_float(c) for c in v)


def build_craft(placement: Placement, *, seed: int | None = None) -> tuple[ConfigNode, list[str]]:
    """The .craft as a ConfigNode tree, plus notes (e.g. computed fairing profiles)."""
    rng = random.Random(seed)
    spec = placement.spec
    notes: list[str] = []
    ext = placement.extents()
    root = ConfigNode("")
    root.add("ship", spec.name)
    root.add("version", KSP_VERSION)
    root.add("description", confignode_safe(spec.description))  # one line, no braces or '//'
    root.add("type", "VAB")
    root.add("size", _vec(ext["size"]))
    root.add("steamPublishedFileId", 0)
    root.add("persistentId", rng.randint(1, 4_294_967_295))
    root.add("rot", "0,0,0,1")
    root.add("missionFlag", "Squad/Flags/default")
    root.add("vesselType", spec.vessel_type)
    for p in placement.parts:
        root.add_node(_part_node(p, placement, rng, notes))
    return root, notes


def render(placement: Placement, *, seed: int | None = None) -> str:
    node, _ = build_craft(placement, seed=seed)
    return node.to_text()


def write_craft(placement: Placement, path: Path, *, overwrite: bool = False,
                seed: int | None = None) -> tuple[Path, list[str]]:
    if not valid_craft_name(placement.spec.name):
        raise AstraError(f"invalid craft name {placement.spec.name!r}",
                         "use letters, digits, space, _ . - (no '..', not a Windows device name such as CON)")
    path = Path(path)
    if path.exists() and not overwrite:
        raise AstraError(f"{path} already exists", "pass overwrite=true to replace it, or choose another name")
    node, notes = build_craft(placement, seed=seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(node.to_text())
    return path, notes


def _part_node(p: Placed, placement: Placement, rng: random.Random, notes: list[str]) -> ConfigNode:
    if not all(math.isfinite(c) for c in list(p.pos) + list(p.rot)):
        raise AstraError(f"{p.key} ({p.info.name}) has no finite position/rotation, so it cannot be written",
                         "check its surface/node numbers with design_check detail='full' and part_info")
    n = ConfigNode("PART")
    n.add("part", p.craft_id)
    n.add("partName", "Part")
    n.add("persistentId", rng.randint(1, 4_294_967_295))
    n.add("pos", _vec(p.pos))
    n.add("attPos", "0,0,0")
    n.add("attPos0", _vec(p.rel_pos))
    n.add("rot", _vec(p.rot))
    n.add("attRot", "0,0,0,1")
    n.add("attRot0", _vec(p.rel_rot))
    n.add("mir", "1,1,1")
    n.add("symMethod", "Radial")
    n.add("autostrutMode", "Off")
    n.add("rigidAttachment", False)
    n.add("istg", p.istg if p.info.stageable else p.dstg)
    n.add("resPri", 0)
    n.add("dstg", max(p.dstg, 0))
    n.add("sidx", -1)
    n.add("sqor", -1)
    n.add("sepI", -1)
    n.add("attm", 1 if p.attach == "surface" else 0)
    n.add("modCost", 0)
    n.add("modMass", 0)
    n.add("modSize", "0,0,0")
    for c in p.children:
        n.add("link", c.craft_id)
    for s in p.sym:
        n.add("sym", s.craft_id)
    if p.attach == "surface":
        n.add("srfN", f"srfAttach,{p.parent.craft_id}")
    elif p.attach == "stack":
        n.add("attN", f"{p.child_node},{p.parent.craft_id}_{_bar(p.info.node(p.child_node).pos)}")
    for c in p.children:
        if c.attach == "stack":
            n.add("attN", f"{c.parent_node},{c.craft_id}_{_bar(p.info.node(c.parent_node).pos)}")
    n.add_node("EVENTS")
    n.add_node("ACTIONS")
    n.add_node("PARTDATA")
    for m in _modules(p, placement, notes):
        n.add_node(m)
    for name, (amount, mx) in p.resource_amounts().items():
        r = n.add_node("RESOURCE")
        r.add("name", name)
        r.add("amount", fmt_float(amount, 6))
        r.add("maxAmount", fmt_float(mx, 6))
        r.add("flowState", True)
        r.add("isTweakable", True)
        r.add("hideFlow", False)
        r.add("isVisible", True)
        r.add("flowMode", "Both")
    return n


def _modules(p: Placed, placement: Placement, notes: list[str]) -> list[ConfigNode]:
    """Only the modules whose state the spec sets, in the prefab's module order."""
    out: list[tuple[int, ConfigNode]] = []
    order = {name: i for i, name in reversed(list(enumerate(p.info.modules)))}
    s = p.spec
    if s.thrust_limit_pct is not None:
        for e in p.info.engines:
            m = ConfigNode("MODULE")
            m.add("name", e.module)
            m.add("isEnabled", True)
            if e.id:
                m.add("engineID", e.id)
            m.add("thrustPercentage", fmt_float(s.thrust_limit_pct, 3))
            out.append((order.get(e.module, 999), m))
    if s.crossfeed is not None:
        m = ConfigNode("MODULE")
        m.add("name", "ModuleToggleCrossfeed")
        m.add("isEnabled", True)
        m.add("crossfeedStatus", s.crossfeed)
        m.add("stagingEnabled", True)
        out.append((order.get("ModuleToggleCrossfeed", 999), m))
    if s.fairing is not None:
        m = ConfigNode("MODULE")
        m.add("name", "ModuleProceduralFairing")
        m.add("isEnabled", True)
        profile = fairing_profile(placement, p, s.fairing)
        for h, r in profile:
            x = m.add_node("XSECTION")
            x.add("h", fmt_float(h, 4))
            x.add("r", fmt_float(r, 4))
        notes.append(f"{p.key}: fairing XSECTIONs (h_m, r_m) = {[(round(h, 3), round(r, 3)) for h, r in profile]}")
        out.append((order.get("ModuleProceduralFairing", 999), m))
    if s.tag is not None:
        m = ConfigNode("MODULE")
        m.add("name", "KOSNameTag")
        m.add("isEnabled", True)
        m.add("nameTag", confignode_safe(s.tag))
        m.add("stagingEnabled", True)
        out.append((order.get("KOSNameTag", 999), m))
    return [m for _, m in sorted(out, key=lambda t: t[0])]


# ---------------------------------------------------------------------------------------------
# Procedural fairing shell


def payload_envelope(placement: Placement, base: Placed) -> tuple[float, float, list[str]]:
    """(height above the base's top node, max radius from the base axis, part keys) of the payload:
    the parts joined to the base through its top node that rise above that node. When the base hangs
    from its parent by its top node that is the rest of the craft minus the base's own subtree (so
    boosters on the stage below, however tall, are not enclosed); otherwise it is whatever is
    stacked on the base's top node."""
    top_node = base.info.node("top")
    y0 = base.world(top_node.pos)[1] if top_node else base.pos[1]
    if base.parent is not None and base.attach == "stack" and base.child_node == "top":
        below = {id(q) for q in placement.subtree(base)}
        candidates = [q for q in placement.parts if id(q) not in below]
    else:
        candidates = [q for c in base.children if c.attach == "stack" and c.parent_node == "top"
                      for q in placement.subtree(c)]
    height, radius, keys = 0.0, 0.0, []
    for q in candidates:
        if q is base:
            continue
        bottom, top = local_extent_y(q.info)
        ys = [q.world([0.0, y, 0.0])[1] for y in (bottom, top)]
        if max(ys) <= y0 + 1e-3:
            continue
        keys.append(q.key)
        height = max(height, max(ys) - y0)
        reach = math.hypot(q.pos[0] - base.pos[0], q.pos[2] - base.pos[2]) + q.info.radius_m
        radius = max(radius, reach)
    return height, radius, keys


def fairing_profile(placement: Placement, base: Placed, f: FairingSpec) -> list[tuple[float, float]]:
    if f.xsections is not None:
        return list(f.xsections)
    height, reach, keys = payload_envelope(placement, base)
    r0 = base.info.diameters_m[-1] / 2 if base.info.diameters_m else base.info.radius_m  # nominal base size
    c = f.clearance_m or 0.0
    body_r = max(r0, reach + c)
    body_h = height + c
    sections = [(0.0, r0)]
    if body_r > r0 + 0.01:
        sections.append((body_r - r0, body_r))  # 45-degree flare out to the payload envelope
    if body_h > sections[-1][0] + 0.01:
        sections.append((body_h, body_r))
    base_h = sections[-1][0]
    length = f.nose_length_m if f.nose_length_m else 1.5 * body_r
    tip = 0.1 * body_r
    if f.nose == "cone":
        nose = [(1.0, tip)]
    elif f.nose == "flat":
        nose = [(min(1.0, 0.05 / max(length, 1e-6)), tip)]
    elif f.nose == "blunt":  # elliptical
        nose = [(x, max(tip, body_r * math.sqrt(max(0.0, 1 - x * x)))) for x in (0.5, 0.8, 0.95)] + [(1.0, tip)]
    else:  # tangent ogive
        rho = (body_r ** 2 + length ** 2) / (2 * body_r)
        nose = [(x, max(tip, math.sqrt(max(0.0, rho ** 2 - (length - x * length) ** 2)) + body_r - rho))
                for x in (0.35, 0.65, 0.85)] + [(1.0, tip)]
    sections += [(base_h + x * length, r) for x, r in nose]
    return [(round(h, 4), round(r, 4)) for h, r in sections]


def describe_placement(placement: Placement) -> list[dict[str, Any]]:
    """Per-part geometry for reports: key, part, attachment, position, stage numbers."""
    rows = []
    for p in placement.parts:
        row: dict[str, Any] = {"key": p.key, "part": p.info.name, "pos_m": [round(c, 3) for c in p.pos]}
        if p.attach == "stack":
            row["attach"] = f"{p.child_node} -> {p.parent.key}.{p.parent_node}"
        elif p.attach == "surface":
            row["attach"] = f"surface of {p.parent.key} @ {p.azimuth_deg:.0f} deg"
        if p.istg >= 0:
            row["stage"] = p.istg
        if p.dstg >= 0:
            row["decouple_stage"] = p.dstg
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------------------------
# Reading a craft back into a spec


def spec_from_craft(root: ConfigNode, catalog: Catalog) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """Convert a saved .craft into a spec dict.

    Returns ``(spec, notes, source)``; ``source`` maps each spec id to the craft part id it came from.
    Symmetry groups collapse to one spec part with ``symmetry: N`` (their replicated subtrees collapse
    with them); stageable parts keep their saved stage; thrust limiters, crossfeed toggles, fill
    levels, fairing shells and name tags carry over. Fuel lines cannot be expressed and are dropped
    with a note.
    """
    notes: list[str] = []
    blocks = [(str(pn.get("part") or "").strip(), pn) for pn in root.nodes("PART")]
    by_id = dict(blocks)
    order = [pid for pid, _ in blocks]
    parent_of: dict[str, str] = {}
    children: dict[str, list[str]] = {pid: [] for pid in order}
    for pid, pn in blocks:
        for child in pn.get_all("link"):
            child = child.strip()
            if child in by_id:
                parent_of[child] = pid
                children[pid].append(child)
    roots = [pid for pid in order if pid not in parent_of]
    if len(roots) != 1:
        raise AstraError(f"craft has {len(roots)} root parts", "the craft file is damaged")

    info = {pid: _resolve_legacy(catalog, craft_part_name(pid), notes) for pid in order}
    pos = {pid: parse_floats(pn.get("pos")) for pid, pn in blocks}
    rot = {pid: parse_floats(pn.get("rot")) or [0.0, 0.0, 0.0, 1.0] for pid, pn in blocks}
    for pid in order:
        if info[pid] is None:
            notes.append(f"{pid}: part {craft_part_name(pid)!r} is not in the catalog (legacy or mod part)")
    notes[:] = list(dict.fromkeys(notes))

    # symmetry: keep the first member of each group; drop the others with their subtrees
    dropped: set[str] = set()
    sym_count: dict[str, int] = {}
    for pid in order:
        if pid in dropped:
            continue
        mates = [m.strip() for m in by_id[pid].get_all("sym") if m.strip() in by_id]
        same_parent = [m for m in mates if parent_of.get(m) == parent_of.get(pid)]
        if same_parent:
            sym_count[pid] = len(same_parent) + 1
        for m in same_parent:
            todo = [m]
            while todo:
                q = todo.pop()
                dropped.add(q)
                todo.extend(children.get(q, []))

    ids: dict[str, str] = {}
    used: set[str] = set()
    for pid in order:
        if pid in dropped:
            continue
        base = re.sub(r"[^A-Za-z0-9_.:-]", "_", craft_part_name(pid))[:56] or "part"
        name, k = base, 2
        while name in used:
            name, k = f"{base}_{k}", k + 1
        used.add(name)
        ids[pid] = name

    parts: list[dict[str, Any]] = []
    for pid in order:
        if pid in dropped or pid not in ids:
            continue
        pn, pi = by_id[pid], info[pid]
        entry: dict[str, Any] = {"id": ids[pid], "part": pi.name if pi else craft_part_name(pid)}
        par = parent_of.get(pid)
        if par is not None:
            if par not in ids:  # its parent was a dropped fuel line or similar
                _drop_subtree(pid, children, ids)
                continue
            entry["parent"] = ids[par]
            srf = (pn.get("srfN") or "").split(",")
            if len(srf) >= 2 and srf[1].strip() == par:
                if pi is not None and ({"CModuleFuelLine", "CModuleLinkedMesh"} & set(pi.modules)):
                    notes.append(f"{ids[pid]}: fuel line dropped (specs cannot express fuel lines; "
                                 "enable crossfeed on the decoupler instead)")
                    _drop_subtree(pid, children, ids)
                    continue
                entry["surface"] = _surface_params(pid, par, pi, info[par], pos, rot, by_id, notes, ids)
                if sym_count.get(pid, 1) > 1:
                    entry["symmetry"] = sym_count[pid]
            else:
                own = next((a.split(",", 1)[0].strip() for a in pn.get_all("attN") if _att_target(a) == par), None)
                theirs = next((a.split(",", 1)[0].strip() for a in by_id[par].get_all("attN")
                               if _att_target(a) == pid), None)
                if theirs is None:
                    notes.append(f"{ids[pid]}: the file has no node link to its parent; attached to 'bottom'")
                entry["node"] = theirs or "bottom"
                pnode = info[par].node(entry["node"]) if info[par] is not None else None
                if own and (pnode is None or pi is None or default_child_node(pnode, pi) != own):
                    entry["child_node"] = own
                moved = _stack_displacement(pid, par, by_id, pos, rot)
                if moved > 0.01:
                    notes.append(f"{ids[pid]}: sits {moved:.2f} m away from its attach node in the source craft "
                                 "(moved with the offset tool); the spec puts it back on the node")
        _carry_options(entry, pn, pi)
        parts.append(entry)

    ship_v = root.get_value("ship")
    ship = ship_v.value if ship_v else "Imported"
    if ship.startswith("#") and ship_v.comment and "=" in ship_v.comment:
        ship = ship_v.comment.split("=", 1)[1]
    name = re.sub(r"[^A-Za-z0-9 _.-]", "", ship).strip().lstrip("_.-") or "Imported"
    spec = {"name": name[:80], "description": _plain_description(root), "stages": "auto", "parts": parts}
    return spec, notes, {v: k for k, v in ids.items()}


def _resolve_legacy(catalog: Catalog, name: str, notes: list[str]) -> Any:
    """Catalog entry for a craft part name; retired names map to their loaded '.v2' successor."""
    info = catalog.resolve(name)
    if info is not None and info.loaded is not False:
        return info
    successor = catalog.resolve(name + ".v2") or catalog.resolve(name + "_v2")
    if successor is not None and successor.loaded is not False:
        notes.append(f"{name} is not loaded in this game; using its successor {successor.name}")
        return successor
    return info


def _stack_displacement(pid: str, par: str, by_id: dict, pos: dict, rot: dict) -> float:
    """How far a stack child sits from where its and its parent's attN offsets put it (metres)."""
    own = next((_att_offset(a) for a in by_id[pid].get_all("attN") if _att_target(a) == par), None)
    theirs = next((_att_offset(a) for a in by_id[par].get_all("attN") if _att_target(a) == pid), None)
    if own is None or theirs is None or len(pos[pid]) != 3 or len(pos[par]) != 3:
        return 0.0
    expected = v_sub(v_add(pos[par], q_rot(rot[par], theirs)), q_rot(rot[pid], own))
    return math.sqrt(sum(c * c for c in v_sub(pos[pid], expected)))


def _drop_subtree(pid: str, children: dict[str, list[str]], ids: dict[str, str]) -> None:
    todo = [pid]
    while todo:
        q = todo.pop()
        ids.pop(q, None)
        todo.extend(children.get(q, []))


def _att_target(att_line: str) -> str:
    return parse_attn(att_line)[1]


def _att_offset(att_line: str) -> list[float] | None:
    return parse_attn(att_line)[2]


def _plain_description(root: ConfigNode) -> str:
    v = root.get_value("description")
    if v is None:
        return ""
    text = v.value
    if text.startswith("#") and v.comment and "=" in v.comment:
        text = v.comment.split("=", 1)[1]
    # stock files spell newlines '\n'; KSP's own saves write them as '¨'
    return " ".join(text.replace("\\n", " ").replace("¨", " ").split())[:300]


def _surface_params(pid: str, par: str, pi: Any, parent_info: Any, pos: dict, rot: dict, by_id: dict,
                    notes: list[str], ids: dict[str, str]) -> dict[str, Any]:
    """Measure a surface attachment in the parent's frame: azimuth, height, radius, and the tilt of
    the surface normal. Notes parts whose saved orientation is not the canonical one."""
    if pi is None or pi.srf_node is None or parent_info is None or len(pos[pid]) != 3 or len(pos[par]) != 3:
        notes.append(f"{ids[pid]}: surface placement could not be measured; set azimuth/height by hand")
        return {"azimuth_deg": 0.0}
    point = v_add(pos[pid], q_rot(rot[pid], pi.srf_node.pos))
    local = q_rot(q_inv(rot[par]), v_sub(point, pos[par]))
    rel_rot = q_mul(q_inv(rot[par]), rot[pid])
    normal = v_norm(v_scale(q_rot(rel_rot, pi.srf_node.dir), srf_target_sign(pi.srf_node)))
    tilt = math.degrees(math.asin(max(-1.0, min(1.0, normal[1]))))
    horiz = math.hypot(local[0], local[2])
    height = local[1]
    out: dict[str, Any] = {}
    psrf = (by_id[par].get("srfN") or "").split(",")
    radial_parent = len(psrf) >= 2 and psrf[0] == "srfAttach" and parent_info.srf_node is not None
    outward = outward_dir(parent_info.srf_node) if radial_parent else None
    outer, o = False, [1.0, 0.0, 0.0]
    if outward is not None and abs(outward[1]) < 0.5 and abs(tilt) < 20.0 and horiz > 1e-3:
        o = v_norm([outward[0], 0.0, outward[2]])
        outer = (local[0] * o[0] + local[2] * o[2]) / horiz > math.cos(math.radians(10.0))
    if outer:
        radius = local[0] * o[0] + local[2] * o[2]
        est, _ = outer_extent(parent_info, o)
        canon_normal = o
    else:
        if horiz > 0.01 or abs(tilt) < 20.0:
            az = math.degrees(math.atan2(local[2], local[0])) % 360.0
        elif math.hypot(normal[0], normal[2]) > 1e-3:  # on a top/bottom face axis: the normal fixes roll
            az = math.degrees(math.atan2(normal[2], normal[0])) % 360.0
        else:
            az = 0.0
        out["azimuth_deg"] = round(az, 3)
        radius = horiz
        if abs(tilt) >= 20.0:
            out["tilt_deg"] = round(tilt, 2)
            est = 0.0 if abs(tilt) >= 45.0 else surface_radius(parent_info, height)[0]
        else:
            est, _ = surface_radius(parent_info, height)
        a, t = math.radians(az), math.radians(out.get("tilt_deg", 0.0))
        canon_normal = [math.cos(t) * math.cos(a), math.sin(t), math.cos(t) * math.sin(a)]
    if abs(height) > 1e-4:
        out["height_m"] = round(height, 4)
    if abs(radius - est) > 0.02:
        out["radius_m"] = round(radius, 4)
    if q_angle_deg(surface_rotation(pi.srf_node, canon_normal), rel_rot) > 10.0:
        notes.append(f"{ids[pid]}: rotated by hand in the source craft; the spec attaches it facing its "
                     "parent the standard way, so it may sit differently")
    return out


def _carry_options(entry: dict[str, Any], pn: ConfigNode, pi: Any) -> None:
    if pi is None:
        return
    istg = (pn.get("istg") or "").strip()
    if pi.stageable and istg.lstrip("-").isdigit() and int(istg) >= 0:
        entry["stage"] = int(istg)
    for m in pn.nodes("MODULE"):
        mname = m.get("name")
        if mname in ("ModuleEngines", "ModuleEnginesFX") and m.has("thrustPercentage") and "thrust_limit_pct" not in entry:
            pct = float(m.get("thrustPercentage") or 100)
            if abs(pct - 100.0) > 1e-3:
                entry["thrust_limit_pct"] = round(pct, 3)
        elif mname == "ModuleToggleCrossfeed" and m.has("crossfeedStatus") and pi.crossfeed_toggle:
            status = parse_bool(m.get("crossfeedStatus"))
            if status != pi.crossfeed:
                entry["crossfeed"] = status
        elif mname == "ModuleProceduralFairing":
            xs = [(float(x.get("h") or 0), float(x.get("r") or 0)) for x in m.nodes("XSECTION")]
            if len(xs) >= 2:
                entry["fairing"] = {"xsections": [[round(h, 4), round(r, 4)] for h, r in xs]}
        elif mname == "KOSNameTag" and (m.get("nameTag") or "").strip():
            entry["tag"] = m.get("nameTag").strip()
    fills = {}
    for r in pn.nodes("RESOURCE"):
        name = r.get("name")
        try:
            amount, mx = float(r.get("amount") or 0), float(r.get("maxAmount") or 0)
        except ValueError:
            continue
        if name in pi.resources and mx > 0 and amount < mx - 1e-6:
            fills[name] = round(amount / mx, 4)
    if fills:
        entry["resources"] = fills

