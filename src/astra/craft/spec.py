"""The craft spec: the part tree the AI writes, parsed and validated with actionable errors.

A spec is plain JSON (see docs/ARCHITECTURE.md, "Craft spec")::

    {"name": "Orbiter 1", "description": "one line", "stages": "auto",
     "parts": [{"id": "pod", "part": "mk1pod.v2"},
               {"id": "tank", "part": "fuelTank", "parent": "pod", "node": "bottom"},
               {"id": "fin", "part": "basicFin", "parent": "tank",
                "surface": {"azimuth_deg": 0, "height_m": -0.7}, "symmetry": 4}]}

Validation happens in two passes. :func:`parse_spec` checks structure (types, ids, one root, no
cycles, node/surface exclusivity) without any game data. :func:`check_spec` checks the spec against
the part catalog (part names, node ids, attach rules, modules the options need). Every issue names
the part id, the field, what is wrong, and how to fix it.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from astra.errors import AstraError

CRAFT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")
# Windows device names cannot be file names (CON.craft opens the console, not a file)
RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}
ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}$")
# ConfigNode syntax: braces are structural anywhere on a line and '//' starts a comment, so a value
# containing them would cut the .craft apart (KSP then fails in ShipConstruct.LoadShip).
CONFIGNODE_UNSAFE = ("{", "}", "//")


def valid_craft_name(name: str) -> bool:
    """A craft name that is safe as ``<name>.craft`` in a Ships folder."""
    stem = name.split(".", 1)[0].strip().lower()
    return bool(CRAFT_NAME_RE.match(name)) and ".." not in name and stem not in RESERVED_NAMES


def confignode_safe(text: str) -> str:
    """One line of free text that a ConfigNode value can hold: newlines become ' | ', braces
    become parentheses and '//' becomes '/'."""
    line = " | ".join(s.strip() for s in str(text).splitlines() if s.strip())
    line = line.replace("{", "(").replace("}", ")")
    while "//" in line:
        line = line.replace("//", "/")
    return line
NOSE_SHAPES = ("ogive", "cone", "blunt", "flat")
VESSEL_TYPES = ("Ship", "Probe", "Lander", "Relay", "Station", "Base", "Plane", "Rover", "Debris")

_PART_KEYS = {"id", "part", "parent", "node", "child_node", "surface", "symmetry", "stage",
              "thrust_limit_pct", "crossfeed", "resources", "fairing", "tag"}
_TOP_KEYS = {"name", "description", "parts", "stages", "vessel_type"}


@dataclass
class SurfaceSpec:
    azimuth_deg: float | None = None  # around the parent's +Y axis; None = outer face of a radial parent
    height_m: float = 0.0  # along the parent's +Y axis from its origin
    radius_m: float | None = None  # distance from the parent's axis; None = the parent's surface
    tilt_deg: float = 0.0  # elevation of the surface normal: 0 side wall, 90 top face, -90 bottom face


@dataclass
class FairingSpec:
    clearance_m: float | None = None
    nose: str = "ogive"
    nose_length_m: float | None = None
    xsections: list[tuple[float, float]] | None = None  # explicit [(h_m, r_m), ...] profile


@dataclass
class PartSpec:
    id: str
    part: str
    parent: str | None = None
    node: str | None = None
    child_node: str | None = None
    surface: SurfaceSpec | None = None
    symmetry: int = 1
    stage: int | None = None
    thrust_limit_pct: float | None = None
    crossfeed: bool | None = None
    resources: dict[str, float] = field(default_factory=dict)
    fairing: FairingSpec | None = None
    tag: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "part": self.part}
        if self.parent is not None:
            d["parent"] = self.parent
        if self.node is not None:
            d["node"] = self.node
        if self.child_node is not None:
            d["child_node"] = self.child_node
        if self.surface is not None:
            s: dict[str, Any] = {}
            if self.surface.azimuth_deg is not None:
                s["azimuth_deg"] = self.surface.azimuth_deg
            if self.surface.height_m:
                s["height_m"] = self.surface.height_m
            if self.surface.radius_m is not None:
                s["radius_m"] = self.surface.radius_m
            if self.surface.tilt_deg:
                s["tilt_deg"] = self.surface.tilt_deg
            d["surface"] = s
        if self.symmetry != 1:
            d["symmetry"] = self.symmetry
        for key in ("stage", "thrust_limit_pct", "crossfeed", "tag"):
            if getattr(self, key) is not None:
                d[key] = getattr(self, key)
        if self.resources:
            d["resources"] = dict(self.resources)
        if self.fairing is not None:
            f: dict[str, Any] = {}
            if self.fairing.xsections is not None:
                f["xsections"] = [list(x) for x in self.fairing.xsections]
            else:
                f["clearance_m"] = self.fairing.clearance_m
                f["nose"] = self.fairing.nose
                if self.fairing.nose_length_m is not None:
                    f["nose_length_m"] = self.fairing.nose_length_m
            d["fairing"] = f
        return d


@dataclass
class CraftSpec:
    name: str
    parts: list[PartSpec]
    description: str = ""
    stages: str = "auto"
    vessel_type: str = "Ship"

    def __post_init__(self) -> None:
        self._by_id = {p.id: p for p in self.parts}

    def by_id(self, pid: str) -> PartSpec:
        return self._by_id[pid]

    @property
    def root(self) -> PartSpec:
        return next(p for p in self.parts if p.parent is None)

    def children(self, pid: str) -> list[PartSpec]:
        return [p for p in self.parts if p.parent == pid]

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.description:
            d["description"] = self.description
        if self.vessel_type != "Ship":
            d["vessel_type"] = self.vessel_type
        d["stages"] = self.stages
        d["parts"] = [p.to_dict() for p in self.parts]
        return d


@dataclass
class SpecIssue:
    part: str | None
    field: str
    problem: str
    fix: str

    def __str__(self) -> str:
        where = f"part '{self.part}'" if self.part else "spec"
        return f"{where}, {self.field}: {self.problem} -> {self.fix}"

    def to_dict(self) -> dict[str, Any]:
        return {"part": self.part, "field": self.field, "problem": self.problem, "fix": self.fix}


class SpecError(AstraError):
    """The craft spec is invalid; ``issues`` lists every problem found."""

    def __init__(self, issues: list[SpecIssue]):
        self.issues = issues
        lines = "\n".join(f"  - {i}" for i in issues)
        super().__init__(f"craft spec has {len(issues)} problem(s):\n{lines}",
                         "fix every listed item and call design_check again; part_info shows a part's "
                         "nodes, attach rules and modules")


# ---------------------------------------------------------------------------------------------
# Structural parsing


def parse_spec(data: dict[str, Any] | str) -> CraftSpec:
    """Parse a spec dict (or JSON text). Raises :class:`SpecError` listing every structural issue."""
    issues: list[SpecIssue] = []
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise SpecError([SpecIssue(None, "spec", f"not valid JSON ({exc})", "pass a JSON object")]) from exc
    if not isinstance(data, dict):
        raise SpecError([SpecIssue(None, "spec", f"expected an object, got {type(data).__name__}",
                                   "pass {\"name\": ..., \"parts\": [...]}")])

    for key in sorted(set(data) - _TOP_KEYS):
        issues.append(SpecIssue(None, key, "unknown top-level field", f"remove it; allowed: {sorted(_TOP_KEYS)}"))
    name = str(data.get("name", "")).strip()
    if not valid_craft_name(name):
        issues.append(SpecIssue(None, "name", f"{name!r} is not a valid craft name",
                                "use 1-80 letters, digits, spaces, '_', '.', '-', starting with a letter or digit "
                                "(no '..', and not a Windows device name such as CON or NUL)"))
    description = confignode_safe(data.get("description", "") or "")
    stages = data.get("stages", "auto")
    if stages not in ("auto", "manual"):
        issues.append(SpecIssue(None, "stages", f"{stages!r} is not supported",
                                "use \"auto\" (explicit per-part 'stage' values override it) or \"manual\""))
    vtype = str(data.get("vessel_type", "Ship"))
    if vtype not in VESSEL_TYPES:
        issues.append(SpecIssue(None, "vessel_type", f"{vtype!r} is not a KSP vessel type", f"use one of {VESSEL_TYPES}"))

    raw_parts = data.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        issues.append(SpecIssue(None, "parts", "missing or empty", "give a list of part objects; the root has no parent"))
        raise SpecError(issues)

    parts: list[PartSpec] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_parts):
        label = f"#{i}"
        if not isinstance(raw, dict):
            issues.append(SpecIssue(label, "parts", "entry is not an object", "each part is {\"id\": ..., \"part\": ...}"))
            continue
        pid = str(raw.get("id", "")).strip()
        label = pid or label
        if not ID_RE.match(pid):
            issues.append(SpecIssue(label, "id", f"{pid!r} is not a valid id",
                                    "use 1-64 letters, digits, '_', '.', ':', '-'"))
        elif pid in seen:
            issues.append(SpecIssue(pid, "id", "duplicate id", "give every part a unique id"))
        seen.add(pid)
        for key in sorted(set(raw) - _PART_KEYS):
            hint = {"parent_node": "node", "attach_node": "child_node", "azimuth_deg": "surface.azimuth_deg",
                    "height_m": "surface.height_m", "count": "symmetry", "thrust_limit": "thrust_limit_pct"}.get(key)
            issues.append(SpecIssue(label, key, "unknown field",
                                    f"did you mean '{hint}'?" if hint else f"allowed: {sorted(_PART_KEYS)}"))
        part_name = raw.get("part")
        if not isinstance(part_name, str) or not part_name.strip():
            issues.append(SpecIssue(label, "part", "missing part name", "set 'part' to a catalog name (parts_search)"))
            part_name = ""
        ps = PartSpec(id=pid, part=part_name.strip())
        parent = raw.get("parent")
        if parent is not None:
            ps.parent = str(parent)
        ps.node = _opt_str(raw, "node", label, issues)
        ps.child_node = _opt_str(raw, "child_node", label, issues)
        ps.tag = _opt_str(raw, "tag", label, issues)
        if ps.tag is not None and ("\n" in ps.tag or "\r" in ps.tag or not ps.tag.strip()
                                   or any(bad in ps.tag for bad in CONFIGNODE_UNSAFE)):
            issues.append(SpecIssue(label, "tag", f"{ps.tag!r} is not a usable name tag",
                                    "use one line without '{', '}' or '//', e.g. 'booster_dec'"))
        if "surface" in raw:
            ps.surface = _parse_surface(raw["surface"], label, issues)
        sym = raw.get("symmetry", 1)
        if isinstance(sym, bool) or not isinstance(sym, (int, float)) or int(sym) != sym or not 1 <= sym <= 32:
            issues.append(SpecIssue(label, "symmetry", f"{sym!r} is not an integer 1..32", "use e.g. 2, 3, 4, 6, 8"))
        else:
            ps.symmetry = int(sym)
        if "stage" in raw and raw["stage"] is not None:
            st = raw["stage"]
            if isinstance(st, bool) or not isinstance(st, (int, float)) or int(st) != st or st < 0:
                issues.append(SpecIssue(label, "stage", f"{st!r} is not an inverse stage number >= 0",
                                        "0 fires last; the launch stage has the highest number"))
            else:
                ps.stage = int(st)
        if raw.get("thrust_limit_pct") is not None:
            ps.thrust_limit_pct = _number(raw["thrust_limit_pct"], label, "thrust_limit_pct", issues, 0.0, 100.0)
        if raw.get("crossfeed") is not None:
            if isinstance(raw["crossfeed"], bool):
                ps.crossfeed = raw["crossfeed"]
            else:
                issues.append(SpecIssue(label, "crossfeed", "must be true or false", "true lets fuel flow through the part"))
        if raw.get("resources") is not None:
            ps.resources = _parse_resources(raw["resources"], label, issues)
        if raw.get("fairing") is not None:
            ps.fairing = _parse_fairing(raw["fairing"], label, issues)
        parts.append(ps)

    ids = {p.id for p in parts}
    roots = [p.id for p in parts if p.parent is None]
    if len(roots) != 1:
        issues.append(SpecIssue(None, "parent", f"{len(roots)} parts have no parent ({roots})",
                                "exactly one part (the root, usually the command part) has no parent"))
    for p in parts:
        if p.parent is None:
            if p.node or p.surface or p.child_node:
                issues.append(SpecIssue(p.id, "node", "the root cannot attach to anything", "remove node/surface/child_node"))
            if p.symmetry != 1:
                issues.append(SpecIssue(p.id, "symmetry", "the root cannot have symmetry", "remove symmetry"))
            continue
        if p.parent == p.id:
            issues.append(SpecIssue(p.id, "parent", "a part cannot be its own parent", "name another part id"))
        elif p.parent not in ids:
            issues.append(SpecIssue(p.id, "parent", f"no part has id {p.parent!r}", f"use one of {sorted(ids - {p.id})}"))
        if p.node and p.surface is not None:
            issues.append(SpecIssue(p.id, "node", "both 'node' and 'surface' given",
                                    "use 'node' for stack attachment or 'surface' for radial attachment, not both"))
        elif not p.node and p.surface is None:
            issues.append(SpecIssue(p.id, "node", "no attachment given",
                                    "add \"node\": \"bottom\" (stack) or \"surface\": {\"azimuth_deg\": .., \"height_m\": ..}"))
        if p.symmetry > 1 and p.surface is None:
            issues.append(SpecIssue(p.id, "symmetry", "symmetry needs surface attachment",
                                    "stack nodes hold one part; use 'surface' with symmetry N"))
        if p.child_node and p.surface is not None:
            issues.append(SpecIssue(p.id, "child_node", "child_node applies to stack attachment only",
                                    "remove it; surface attachment uses the part's srfAttach node"))
    by_id = {p.id: p for p in parts}
    for p in parts:  # cycle check
        seen_chain: set[str] = set()
        cur: PartSpec | None = p
        while cur is not None and cur.parent is not None:
            if cur.id in seen_chain:
                issues.append(SpecIssue(p.id, "parent", "the parent chain loops back on itself",
                                        "make the tree acyclic: every chain must end at the root"))
                break
            seen_chain.add(cur.id)
            cur = by_id.get(cur.parent)
    if issues:
        raise SpecError(issues)
    return CraftSpec(name=name, parts=parts, description=description, stages=stages, vessel_type=vtype)


def _opt_str(raw: dict, key: str, label: str, issues: list[SpecIssue]) -> str | None:
    v = raw.get(key)
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        issues.append(SpecIssue(label, key, f"{v!r} is not a non-empty string", f"give {key} as text"))
        return None
    return v.strip()


def _number(v: Any, label: str, fld: str, issues: list[SpecIssue], lo: float | None = None,
            hi: float | None = None) -> float | None:
    if isinstance(v, bool):
        v = None
    try:
        x = float(v)
    except (TypeError, ValueError):
        issues.append(SpecIssue(label, fld, f"{v!r} is not a number", "give a number"))
        return None
    if not math.isfinite(x) or (lo is not None and x < lo) or (hi is not None and x > hi):
        rng = f"{lo if lo is not None else '-inf'}..{hi if hi is not None else 'inf'}"
        issues.append(SpecIssue(label, fld, f"{x} is outside {rng}", f"give a value in {rng}"))
        return None
    return x


def _parse_surface(v: Any, label: str, issues: list[SpecIssue]) -> SurfaceSpec | None:
    if not isinstance(v, dict):
        issues.append(SpecIssue(label, "surface", "must be an object",
                                "{\"azimuth_deg\": 0..360, \"height_m\": along the parent axis}"))
        return None
    for key in sorted(set(v) - {"azimuth_deg", "height_m", "radius_m", "tilt_deg"}):
        issues.append(SpecIssue(label, f"surface.{key}", "unknown field",
                                "allowed: azimuth_deg, height_m, radius_m, tilt_deg"))
    s = SurfaceSpec()
    if v.get("azimuth_deg") is not None:
        s.azimuth_deg = _number(v["azimuth_deg"], label, "surface.azimuth_deg", issues)
    if v.get("height_m") is not None:
        s.height_m = _number(v["height_m"], label, "surface.height_m", issues) or 0.0
    if v.get("radius_m") is not None:
        s.radius_m = _number(v["radius_m"], label, "surface.radius_m", issues, 0.0)
    if v.get("tilt_deg") is not None:
        s.tilt_deg = _number(v["tilt_deg"], label, "surface.tilt_deg", issues, -90.0, 90.0) or 0.0
    return s


def _parse_resources(v: Any, label: str, issues: list[SpecIssue]) -> dict[str, float]:
    if not isinstance(v, dict):
        issues.append(SpecIssue(label, "resources", "must be an object", "{\"LiquidFuel\": 0.5} = fill fraction 0..1"))
        return {}
    out = {}
    for name, frac in v.items():
        x = _number(frac, label, f"resources.{name}", issues, 0.0, 1.0)
        if x is not None:
            out[str(name)] = x
    return out


def _parse_fairing(v: Any, label: str, issues: list[SpecIssue]) -> FairingSpec | None:
    if not isinstance(v, dict):
        issues.append(SpecIssue(label, "fairing", "must be an object",
                                "{\"clearance_m\": 0.1, \"nose\": \"ogive\"} or {\"xsections\": [[h, r], ...]}"))
        return None
    for key in sorted(set(v) - {"clearance_m", "nose", "nose_length_m", "xsections"}):
        issues.append(SpecIssue(label, f"fairing.{key}", "unknown field",
                                "allowed: clearance_m, nose, nose_length_m, xsections"))
    f = FairingSpec()
    if v.get("xsections") is not None:
        xs = v["xsections"]
        ok = isinstance(xs, list) and len(xs) >= 2 and all(
            isinstance(x, (list, tuple)) and len(x) == 2
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) for c in x)
            for x in xs)
        if not ok:
            issues.append(SpecIssue(label, "fairing.xsections", "must be a list of at least two [height_m, radius_m] pairs",
                                    "e.g. [[0, 0.625], [2.0, 0.625], [3.0, 0.2]]"))
        else:
            f.xsections = [(float(h), float(r)) for h, r in xs]
            if any(r < 0 for _, r in f.xsections) or any(b[0] < a[0] for a, b in zip(f.xsections, f.xsections[1:])):
                issues.append(SpecIssue(label, "fairing.xsections", "heights must increase and radii be >= 0",
                                        "list sections bottom to top"))
        return f
    if v.get("clearance_m") is None:
        issues.append(SpecIssue(label, "fairing.clearance_m", "missing",
                                "give the gap in metres between the payload envelope and the shell"))
    else:
        f.clearance_m = _number(v["clearance_m"], label, "fairing.clearance_m", issues, 0.0, 10.0)
    nose = str(v.get("nose", "ogive"))
    if nose not in NOSE_SHAPES:
        issues.append(SpecIssue(label, "fairing.nose", f"{nose!r} is not a nose shape", f"use one of {NOSE_SHAPES}"))
    f.nose = nose
    if v.get("nose_length_m") is not None:
        f.nose_length_m = _number(v["nose_length_m"], label, "fairing.nose_length_m", issues, 0.0)
    return f


# ---------------------------------------------------------------------------------------------
# Catalog checks


def default_child_node(parent_node: Any, child_info: Any) -> str | None:
    """The child node that faces ``parent_node``: the one whose direction is most opposite to it,
    preferring the conventional top<->bottom pairing."""
    if not child_info.nodes:
        return None
    pid = parent_node.id
    conventional = {"top": "bottom", "bottom": "top"}.get(pid)
    if conventional and child_info.node(conventional) is not None:
        return conventional
    best = min(child_info.nodes, key=lambda n: sum(a * b for a, b in zip(n.dir, parent_node.dir)))
    return best.id


def check_spec(spec: CraftSpec, catalog: Any) -> list[SpecIssue]:
    """Validate a parsed spec against the part catalog. Returns issues (empty = buildable)."""
    issues: list[SpecIssue] = []
    infos = {}
    for p in spec.parts:
        info = catalog.resolve(p.part)
        if info is None:
            close = catalog.suggest(p.part, 4)
            issues.append(SpecIssue(p.id, "part", f"unknown part {p.part!r}",
                                    f"closest names: {close}" if close else "find names with parts_search"))
            continue
        if info.loaded is False:
            issues.append(SpecIssue(p.id, "part", f"{info.name} is not loaded in the running game",
                                    "pick another part (parts_search lists loaded parts)"))
        infos[p.id] = info
    used_nodes: dict[tuple[str, str], str] = {}
    for p in spec.parts:
        info = infos.get(p.id)
        if info is None:
            continue
        if p.parent is not None and p.parent in infos:
            parent = infos[p.parent]
            if p.surface is not None:
                if not info.surface_attachable:
                    issues.append(SpecIssue(p.id, "surface", f"{info.name} cannot be surface-attached "
                                            "(attachRules srfAttach=0 or no srfAttach node)",
                                            f"stack-attach it with 'node' (its nodes: {[n.id for n in info.nodes]})"))
                if not parent.attach_rules.get("allowSrfAttach"):
                    issues.append(SpecIssue(p.id, "surface", f"parent {p.parent} ({parent.name}) does not accept "
                                            "surface-attached parts (allowSrfAttach=0)",
                                            "attach to a tank or structural part instead"))
                if p.surface.azimuth_deg is None and not _is_radially_mounted(spec, p.parent):
                    issues.append(SpecIssue(p.id, "surface.azimuth_deg", "missing",
                                            "give the angle around the parent's axis (0 = +X, 90 = +Z); it may be "
                                            "omitted only when the parent is itself surface-attached (outer face)"))
            elif p.node:
                pn = parent.node(p.node)
                if pn is None:
                    issues.append(SpecIssue(p.id, "node", f"parent {p.parent} ({parent.name}) has no node {p.node!r}",
                                            f"use one of {[n.id for n in parent.nodes]}"))
                else:
                    cn_id = p.child_node or default_child_node(pn, info)
                    if cn_id is None or info.node(cn_id) is None:
                        issues.append(SpecIssue(p.id, "child_node", f"{info.name} has no node {cn_id!r}",
                                                f"use one of {[n.id for n in info.nodes]} or surface-attach it"))
                    else:
                        _claim(used_nodes, (p.id, cn_id), f"{p.id}->parent", p.id, "child_node", issues)
                        for owner, n in ((parent.name, pn), (info.name, info.node(cn_id))):
                            if not n.known:
                                issues.append(SpecIssue(p.id, "node", f"node {n.id!r} of {owner} sits on a model "
                                                        "transform and its position is unknown offline",
                                                        "start the game so the bridge part database supplies "
                                                        "it, or use another part"))
                    _claim(used_nodes, (p.parent, p.node), p.id, p.id, "node", issues)
                if not info.attach_rules.get("stack"):
                    issues.append(SpecIssue(p.id, "node", f"{info.name} cannot be stack-attached (attachRules stack=0)",
                                            "surface-attach it instead"))
                if not parent.attach_rules.get("allowStack"):
                    issues.append(SpecIssue(p.id, "node", f"parent {parent.name} does not accept stacked parts "
                                            "(allowStack=0)", "attach to another part"))
        if p.thrust_limit_pct is not None and not info.engines:
            issues.append(SpecIssue(p.id, "thrust_limit_pct", f"{info.name} is not an engine", "remove thrust_limit_pct"))
        if p.crossfeed is not None and not info.crossfeed_toggle:
            issues.append(SpecIssue(p.id, "crossfeed", f"{info.name} has no crossfeed toggle (ModuleToggleCrossfeed)",
                                    "remove it, or use a decoupler that has one (part_info shows crossfeed_toggle)"))
        for rname in p.resources:
            if rname not in info.resources:
                issues.append(SpecIssue(p.id, f"resources.{rname}", f"{info.name} holds no {rname}",
                                        f"it holds {sorted(info.resources) or 'nothing'}"))
        if p.fairing is not None and "ModuleProceduralFairing" not in info.modules:
            issues.append(SpecIssue(p.id, "fairing", f"{info.name} is not a procedural fairing base",
                                    "use a fairing part (parts_search role='fairing')"))
    if spec.stages == "manual":
        for p in spec.parts:
            info = infos.get(p.id)
            if info is not None and info.stageable and p.stage is None:
                issues.append(SpecIssue(p.id, "stage", f"{info.name} is stageable but has no stage in manual mode",
                                        "give it a stage number or use \"stages\": \"auto\""))
    return issues


def _is_radially_mounted(spec: CraftSpec, pid: str) -> bool:
    return spec.by_id(pid).surface is not None


def _claim(used: dict, key: tuple[str, str], who: str, pid: str, fld: str, issues: list[SpecIssue]) -> None:
    if key in used and used[key] != who:
        issues.append(SpecIssue(pid, fld, f"node {key[1]!r} of {key[0]} is already used by {used[key]}",
                                "each stack node holds one part; use another node or surface attachment"))
    else:
        used[key] = who
