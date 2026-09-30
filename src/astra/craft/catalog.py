"""The part catalog: what every loaded part is, physically and geometrically.

Sources, best first:

1. the extended bridge ``POST /part-database {"detail": "full"}`` — prefab data from the running game
   (attach nodes, attach rules, bounds, every engine mode, decoupler/chute data);
2. otherwise the part configs — ModuleManager's ``ModuleManager.ConfigCache`` (every PART after
   patches, exactly what the game loaded at its last start) or, without it, every ``PART`` in
   ``GameData/**/*.cfg`` — overlaid with the basic live ``GET /part-database`` physics when the
   bridge answers (live dry mass, first-engine thrust/Isp, resource capacities, localized titles).

English titles always come from the raw cfg comments (``title = #autoLOC_x //#autoLOC_x = Title``)
because this install's dictionary is localized. The merged catalog is cached as
``CONFIG.cache_dir/parts.json`` keyed by a GameData fingerprint. Nothing is loaded at import time:
call :func:`get_catalog` (shared, lazy) or :meth:`Catalog.load`.

Units: masses in tonnes, thrust in kN, positions in metres in the part's local frame (Unity axes,
+Y up the stack), Isp in seconds, pressure in atmospheres.
"""

from __future__ import annotations

import bisect
import difflib
import hashlib
import json
import math
import re
import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from astra.config import CONFIG
from astra.craft.confignode import ConfigNode, parse_bool, parse_floats, read_text
from astra.errors import AstraError

G0 = 9.80665
CATALOG_VERSION = 4  # 4: bridge curve tangents and engine module names are kept over cfg guesses

ENGINE_MODULES = ("ModuleEngines", "ModuleEnginesFX")
DECOUPLER_MODULES = ("ModuleDecouple", "ModuleAnchoredDecoupler")
STAGED_MODULES = ENGINE_MODULES + DECOUPLER_MODULES + ("ModuleParachute", "ModuleProceduralFairing",
                                                      "LaunchClamp", "ModuleSimpleAdjustableFairing")

ROLES: dict[str, str] = {
    "command_pod": "crewed command pod or cockpit (ModuleCommand, crew > 0)",
    "probe_core": "uncrewed control core (ModuleCommand, no crew)",
    "liquid_engine": "liquid-fuel rocket engine (LF/Ox or LF-only)",
    "solid_booster": "solid rocket motor (burns its own SolidFuel, cannot throttle)",
    "jet_engine": "air-breathing engine (needs IntakeAir)",
    "ion_engine": "electric engine burning XenonGas",
    "monoprop_engine": "engine burning MonoPropellant",
    "decoupler": "stack decoupler or separator",
    "radial_decoupler": "radial decoupler for side-mounted boosters or pods",
    "heat_shield": "ablative heat shield",
    "docking_port": "docking port",
    "parachute": "parachute",
    "fairing": "procedural fairing base",
    "launch_clamp": "launch clamp",
    "landing_leg": "landing leg",
    "wheel": "wheel",
    "rcs_thruster": "RCS thruster block",
    "reaction_wheel": "reaction wheel",
    "control_surface": "active aerodynamic control surface",
    "aero_surface": "fin or wing (lifting surface)",
    "nose_cone": "aerodynamic nose cone",
    "fuel_tank": "liquid-fuel tank (LF/Ox or LF-only)",
    "monoprop_tank": "MonoPropellant tank",
    "xenon_tank": "XenonGas tank",
    "ore_tank": "Ore tank",
    "battery": "battery (ElectricCharge only)",
    "solar_panel": "solar panel",
    "generator": "RTG, fuel cell or converter",
    "antenna": "antenna",
    "science": "science instrument or lab",
    "crew_cabin": "crew cabin without command capability",
    "adapter": "structural adapter between diameters",
    "structural": "structural part",
    "cargo": "cargo bay, service bay or inventory container",
    "fuel_line": "fuel duct (feeds one part from another)",
    "intake": "air intake",
    "avionics": "SAS/avionics unit without command capability",
    "radiator": "radiator",
    "drill": "ore drill",
    "light": "light",
    "ladder": "ladder",
    "robotics": "hinge, piston, rotor or servo",
    "flag": "flag decal part",
    "other": "anything else",
}

PROPELLANT_FAMILIES = {
    "LFO": {"LiquidFuel", "Oxidizer"},
    "LF": {"LiquidFuel"},
    "Solid": {"SolidFuel"},
    "Mono": {"MonoPropellant"},
    "Xenon": {"XenonGas"},
    "Air": {"IntakeAir"},
}

_LOCK = threading.RLock()
_SHARED: Catalog | None = None


class CatalogError(AstraError):
    """A part name is unknown or the catalog cannot be built."""


# ---------------------------------------------------------------------------------------------
# Data model


@dataclass
class ResourceDef:
    name: str
    density_t: float  # tonnes per unit
    flow_mode: str
    unit_cost: float = 0.0


@dataclass
class AttachNode:
    id: str
    pos: list[float]  # metres, part-local
    dir: list[float]  # unit vector; stack nodes point away from the part (srf: geometry.srf_target_sign)
    size: int = 1
    known: bool = True  # False: defined by a model transform (NODE{}), position not in the cfg

    @property
    def diameter_m(self) -> float:
        return node_size_diameter(self.size)


@dataclass
class FloatCurve:
    """KSP FloatCurve (a Unity AnimationCurve): Hermite segments, clamped at both ends.

    Keys are ``[time, value]`` or ``[time, value, inTangent, outTangent]``. Keys with tangents (the
    live prefab curve from the bridge, or a cfg ``key = t v in out``) are used exactly, as Unity's
    AnimationCurve does: a segment is a cubic Hermite spline, and a segment whose tangent is infinite
    (``None`` here: the bridge writes non-finite numbers as JSON null) is a step holding the left
    key's value. Keys without tangents get Unity's ``AddKey(time, value)`` auto tangents, computed
    once when each key is inserted (in file order): the slope towards the neighbours present at that
    moment; a lone first key stays flat.
    """

    keys: list[list[float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._t: list[float] = []
        self._k: list[list[float]] = []  # [t, v, in, out]
        for key in self.keys:
            t, v = float(key[0]), float(key[1])
            i = bisect.bisect_right(self._t, t)
            self._t.insert(i, t)
            if len(key) >= 4:
                self._k.insert(i, [t, v, _tangent(key[2]), _tangent(key[3])])
                continue
            self._k.insert(i, [t, v, 0.0, 0.0])
            n = len(self._k)
            if n < 2:
                continue
            if i == 0:
                m = _slope(self._k[0], self._k[1])
            elif i == n - 1:
                m = _slope(self._k[i - 1], self._k[i])
            else:
                m = 0.5 * _slope(self._k[i - 1], self._k[i]) + 0.5 * _slope(self._k[i], self._k[i + 1])
            self._k[i][2] = self._k[i][3] = m

    def __call__(self, x: float) -> float:
        k = self._k
        if not k:
            return 0.0
        if math.isnan(x):
            return math.nan  # an unknown pressure has no Isp; never index past the keys
        if x <= k[0][0]:
            return k[0][1]
        if x >= k[-1][0]:
            return k[-1][1]
        i = bisect.bisect_right(self._t, x) - 1
        (t0, v0, _, m0), (t1, v1, m1, _) = k[i], k[i + 1]
        if not (math.isfinite(m0) and math.isfinite(m1)):
            return v0  # Unity: an infinite tangent makes the segment a step
        dt = t1 - t0
        s = (x - t0) / dt
        s2, s3 = s * s, s * s * s
        return ((2 * s3 - 3 * s2 + 1) * v0 + (s3 - 2 * s2 + s) * m0 * dt
                + (-2 * s3 + 3 * s2) * v1 + (s3 - s2) * m1 * dt)


def _tangent(x: Any) -> float:
    """A key tangent; JSON null (the bridge's spelling of a non-finite number) means infinite."""
    if x is None:
        return math.inf
    try:
        t = float(x)
    except (TypeError, ValueError):
        return math.inf
    return math.inf if math.isnan(t) else t


def curve_keys(raw: Any) -> list[list[float]]:
    """Normalize curve keys from the bridge or a cache: each key a list ``[t, v]`` or
    ``[t, v, in, out]`` (numbers, numeric strings, or null tangents), or a ``"t v [in out]"`` string.
    Keys without a finite time and value are skipped."""
    out: list[list[float]] = []
    for key in raw if isinstance(raw, (list, tuple)) else []:
        if isinstance(key, str):
            vals: list[Any] = parse_floats(key)
        elif isinstance(key, (list, tuple)):
            vals = list(key)
        else:
            continue
        try:
            t, v = float(vals[0]), float(vals[1])
        except (IndexError, TypeError, ValueError):
            continue
        if not (math.isfinite(t) and math.isfinite(v)):
            continue
        out.append([t, v, _tangent(vals[2]), _tangent(vals[3])] if len(vals) >= 4 else [t, v])
    return out


def _slope(a: list[float], b: list[float]) -> float:
    dt = b[0] - a[0]
    return (b[1] - a[1]) / dt if dt else 0.0


@dataclass
class EngineMode:
    id: str
    module: str
    engine_type: str
    thrust_vac_kn: float
    min_thrust_kn: float
    isp_curve: list[list[float]]  # [[pressure_atm, isp_s, (in, out)]...]
    propellants: list[dict[str, Any]]  # [{name, ratio, flow_mode?, ignore_for_isp?}]
    throttle_locked: bool = False
    gimbal_deg: float = 0.0

    def isp(self, pressure_atm: float = 0.0) -> float:
        return FloatCurve(self.isp_curve)(pressure_atm) if self.isp_curve else 0.0

    @property
    def isp_vac(self) -> float:
        return self.isp(0.0)

    @property
    def isp_asl(self) -> float:
        return self.isp(1.0)

    def thrust_kn(self, pressure_atm: float = 0.0) -> float:
        """KSP keeps fuel flow constant, so thrust scales with Isp: F(p) = F_vac * Isp(p) / Isp(0)."""
        vac = self.isp_vac
        return self.thrust_vac_kn * self.isp(pressure_atm) / vac if vac > 0 else 0.0

    @property
    def mass_flow_tps(self) -> float:
        """Propellant mass flow at full throttle, tonnes per second."""
        return self.thrust_vac_kn / (self.isp_vac * G0) if self.isp_vac > 0 else 0.0

    @property
    def propellant_names(self) -> list[str]:
        return [p["name"] for p in self.propellants]

    @property
    def air_breathing(self) -> bool:
        return "IntakeAir" in self.propellant_names


@dataclass
class PartInfo:
    name: str  # live AvailablePart name (dotted), as written in .craft files
    cfg_name: str = ""
    title: str = ""
    title_en: str = ""
    category: str = ""
    role: str = "other"
    cost: float = 0.0
    mass_t: float = 0.0  # dry
    crew: int = 0
    bulkhead: list[str] = field(default_factory=list)
    tags: str = ""
    tech_required: str = ""
    nodes: list[AttachNode] = field(default_factory=list)
    srf_node: AttachNode | None = None
    attach_rules: dict[str, bool] = field(default_factory=dict)
    crossfeed: bool = True  # effective default (crossfeed toggle state when the part has one)
    crossfeed_toggle: bool = False
    stageable: bool = False
    staging_icon: str = ""
    resources: dict[str, dict[str, float]] = field(default_factory=dict)  # {name: {amount, max, density_t}}
    engines: list[EngineMode] = field(default_factory=list)
    decoupler: dict[str, Any] | None = None
    parachute: dict[str, Any] | None = None
    command: dict[str, Any] | None = None
    reaction_wheel: dict[str, float] | None = None
    modules: list[str] = field(default_factory=list)  # prefab module order
    wheel_type: str = ""  # ModuleWheelBase wheelType (LEG for landing legs)
    variants: list[dict[str, Any]] = field(default_factory=list)  # [{name, nodes: [AttachNode...]}]
    bounds: dict[str, list[float]] | None = None  # {size: [x,y,z], center: [x,y,z]}, part-local
    outer_face_m: float | None = None  # learned: distance to the outward face of a radially mounted part
    com_offset: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    max_temp: float = 0.0
    skin_max_temp: float = 0.0
    crash_tolerance: float = 0.0
    loaded: bool | None = None  # present in the running game's part list (None: unknown)
    source: str = "cfg"
    cfg_mass_t: float | None = None  # the cfg 'mass' when a live value replaced it

    # -- derived ------------------------------------------------------------------------------

    def node(self, node_id: str) -> AttachNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    @property
    def wet_mass_t(self) -> float:
        return self.mass_t + sum(r["amount"] * r["density_t"] for r in self.resources.values())

    @property
    def diameters_m(self) -> list[float]:
        """Distinct stack diameters from the bulkhead profiles (falls back to node sizes)."""
        ds = sorted({d for d in (bulkhead_diameter(b) for b in self.bulkhead) if d})
        if not ds:
            ds = sorted({n.diameter_m for n in self.nodes})
        return ds

    @property
    def radius_m(self) -> float:
        """Best estimate of the body radius about the part's +Y axis."""
        if self.srf_node is not None:
            x, _, z = self.srf_node.pos
            dx, dy, dz = self.srf_node.dir
            if abs(dy) < 0.3 and math.hypot(x, z) > 0.05:
                return math.hypot(x, z)
        if self.bounds:
            sx, _, sz = self.bounds["size"]
            if sx > 0 and sz > 0:
                return max(sx, sz) / 2
        ds = self.diameters_m
        return ds[-1] / 2 if ds else 0.625

    @property
    def surface_attachable(self) -> bool:
        return bool(self.attach_rules.get("srfAttach")) and self.srf_node is not None

    @property
    def propellant_family(self) -> str:
        """LFO / LF / Solid / Mono / Xenon / Air for engines and tanks; '' otherwise."""
        names = set(self.engines[0].propellant_names) if self.engines else set(self.resources)
        for fam in ("Air", "Solid", "Xenon", "LFO", "LF", "Mono"):
            need = PROPELLANT_FAMILIES[fam]
            if fam == "LF" and "Oxidizer" in names:
                continue
            if need <= names:
                return fam
        return ""

    def summary(self) -> dict[str, Any]:
        """Compact row for search results."""
        row: dict[str, Any] = {
            "name": self.name, "title": self.title_en or self.title, "role": self.role,
            "diameters_m": self.diameters_m, "mass_t": self.mass_t, "wet_mass_t": self.wet_mass_t,
            "cost": self.cost,
        }
        if self.title_en and self.title and self.title != self.title_en:
            row["title_local"] = self.title
        if self.crew:
            row["crew"] = self.crew
        caps = {k: v["max"] for k, v in self.resources.items() if v["max"] > 0}
        if caps:
            row["resources"] = caps
        if self.engines:
            e = self.engines[0]
            row.update(thrust_vac_kn=e.thrust_vac_kn, thrust_asl_kn=e.thrust_kn(1.0),
                       isp_vac_s=e.isp_vac, isp_asl_s=e.isp_asl,
                       propellants={p["name"]: p["ratio"] for p in e.propellants})
            if len(self.engines) > 1:
                row["modes"] = [m.id or m.engine_type for m in self.engines]
        row["nodes"] = [n.id for n in self.nodes]
        row["surface_attachable"] = self.surface_attachable
        if self.stageable:
            row["stageable"] = True
        if self.loaded is False:
            row["loaded"] = False
        return row

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PartInfo:
        d = dict(d)
        d["nodes"] = [AttachNode(**n) for n in d.get("nodes", [])]
        d["srf_node"] = AttachNode(**d["srf_node"]) if d.get("srf_node") else None
        d["engines"] = [EngineMode(**e) for e in d.get("engines", [])]
        d["variants"] = [{"name": v["name"], "nodes": [AttachNode(**n) for n in v.get("nodes", [])]}
                         for v in d.get("variants", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def node_size_diameter(size: int) -> float:
    return 0.625 if size <= 0 else 1.25 * size


def bulkhead_diameter(token: str) -> float | None:
    t = token.strip().lower()
    m = re.fullmatch(r"size(\d+)(?:p(\d+))?", t)
    if m:
        whole = int(m.group(1))
        frac = float("0." + m.group(2)) if m.group(2) else 0.0
        return 0.625 if whole == 0 and not frac else 1.25 * (whole + frac)
    return {"mk2": 1.5, "mk3": 3.75}.get(t)


def live_name(cfg_name: str) -> str:
    """cfg/persistence names use '_' where the loaded AvailablePart name uses '.'."""
    return cfg_name.replace("_", ".")


# ---------------------------------------------------------------------------------------------
# cfg parsing

_ATTACH_RULE_KEYS = ("stack", "srfAttach", "allowStack", "allowSrfAttach", "allowCollision",
                     "allowDock", "allowRotate", "allowRoot")
_ATTACH_RULE_DEFAULTS = (False, False, False, False, False, False, True, True)


def _f(text: str | None, default: float = 0.0) -> float:
    vals = parse_floats(text)
    return vals[0] if vals else default


def _node_from_floats(node_id: str, vals: list[float], k: float, default_size: int = 1) -> AttachNode | None:
    if len(vals) < 6:
        return None
    d = vals[3:6]
    norm = math.sqrt(sum(c * c for c in d)) or 1.0
    size = int(round(vals[6])) if len(vals) > 6 else default_size
    return AttachNode(node_id, [round(c * k, 6) for c in vals[:3]], [round(c / norm, 6) for c in d], size)


def _stack_nodes(holder: ConfigNode, k: float) -> list[AttachNode]:
    out = []
    for v in holder.values():
        if v.key.startswith("node_stack_"):
            n = _node_from_floats(v.key[len("node_stack_"):], parse_floats(v.value), k)
            if n:
                out.append(n)
    return out


def english_title(title: Any) -> str:
    """'#autoLOC_500820 //#autoLOC_500820 = TT-70 Radial Decoupler' style values -> English text."""
    value, comment = (title.value, title.comment) if hasattr(title, "value") else (str(title or ""), None)
    if not value.startswith("#"):
        return value
    if comment and "=" in comment:
        return comment.split("=", 1)[1].strip()
    return ""


def part_from_cfg(node: ConfigNode, resource_defs: dict[str, ResourceDef]) -> PartInfo | None:
    cfg_name = (node.get("name") or "").strip()
    if not cfg_name:
        return None
    scale = _f(node.get("scale"), 1.0) or 1.0
    k = scale * _f(node.get("rescaleFactor"), 1.25)
    title_v = node.get_value("title")
    p = PartInfo(
        name=live_name(cfg_name), cfg_name=cfg_name,
        title=title_v.value if title_v else "", title_en=english_title(title_v) if title_v else "",
        category=node.get("category", "") or "", cost=_f(node.get("cost")), mass_t=_f(node.get("mass")),
        crew=int(_f(node.get("CrewCapacity"))), tags=node.get("tags", "") or "",
        tech_required=node.get("TechRequired", "") or "",
        bulkhead=[b.strip() for b in (node.get("bulkheadProfiles") or "").split(",") if b.strip()],
        nodes=_stack_nodes(node, k), staging_icon=(node.get("stagingIcon") or "").strip(),
        max_temp=_f(node.get("maxTemp")), skin_max_temp=_f(node.get("skinMaxTemp")),
        crash_tolerance=_f(node.get("crashTolerance")), source="cfg",
    )
    if title_v and p.title.startswith("#") and p.title_en:
        p.title = p.title_en
    for block in node.nodes("NODE"):  # nodes placed on model transforms: learned later if possible
        nid = (block.get("name") or "").strip()
        if nid and p.node(nid) is None:
            down = "bottom" in nid.lower()
            p.nodes.append(AttachNode(nid, [0.0, 0.0, 0.0], [0.0, -1.0 if down else 1.0, 0.0],
                                     int(_f(block.get("size"), 1)), known=False))
    attach = parse_floats(node.get("node_attach"))
    if attach:
        p.srf_node = _node_from_floats("srfAttach", attach, k)
    rules = [int(x) for x in parse_floats(node.get("attachRules"))]
    p.attach_rules = {key: bool(rules[i]) if i < len(rules) else dflt
                      for i, (key, dflt) in enumerate(zip(_ATTACH_RULE_KEYS, _ATTACH_RULE_DEFAULTS))}
    com = parse_floats(node.get("CoMOffset"))
    if len(com) == 3:
        p.com_offset = com
    p.crossfeed = parse_bool(node.get("fuelCrossFeed"), True)

    for res in node.nodes("RESOURCE"):
        name = res.get("name")
        if not name:
            continue
        mx = _f(res.get("maxAmount"))
        amount = _f(res.get("amount"), mx)
        rd = resource_defs.get(name)
        p.resources[name] = {"amount": amount, "max": mx, "density_t": rd.density_t if rd else 0.0}

    gimbal = 0.0
    for mod in node.nodes("MODULE"):
        mname = (mod.get("name") or "").strip()
        if not mname:
            continue
        p.modules.append(mname)
        if mname in ENGINE_MODULES:
            p.engines.append(_engine_from_cfg(mod, mname))
        elif mname == "ModuleGimbal":
            gimbal = max(gimbal, _f(mod.get("gimbalRange")))
        elif mname in DECOUPLER_MODULES:
            explosive = (mod.get("explosiveNodeID") or ("srf" if mname == "ModuleAnchoredDecoupler" else "top")).strip()
            p.decoupler = {"ejection_force": _f(mod.get("ejectionForce")),
                           "is_omni": parse_bool(mod.get("isOmniDecoupler")),
                           "explosive_node": explosive,
                           "radial": mname == "ModuleAnchoredDecoupler" or explosive == "srf"}
        elif mname == "ModuleToggleCrossfeed":
            p.crossfeed_toggle = True
            if mod.has("crossfeedStatus"):
                p.crossfeed = parse_bool(mod.get("crossfeedStatus"))
        elif mname == "ModuleParachute":
            p.parachute = {key: _f(mod.get(key)) for key in (
                "stowedDrag", "semiDeployedDrag", "fullyDeployedDrag", "minAirPressureToOpen",
                "deployAltitude", "chuteMaxTemp", "autoCutSpeed") if mod.has(key)}
        elif mname == "ModuleCommand":
            p.command = {"minimum_crew": int(_f(mod.get("minimumCrew")))}
        elif mname == "ModuleReactionWheel":
            p.reaction_wheel = {"pitch_knm": _f(mod.get("PitchTorque")), "yaw_knm": _f(mod.get("YawTorque")),
                                "roll_knm": _f(mod.get("RollTorque"))}
        elif mname == "ModulePartVariants":
            for var in mod.nodes("VARIANT"):
                nodes_block = var.node("NODES")
                if nodes_block is not None:
                    p.variants.append({"name": var.get("name", ""), "nodes": _stack_nodes(nodes_block, k)})
        elif mname == "ModuleWheelBase":
            p.wheel_type = (mod.get("wheelType") or "").strip().upper()
    for e in p.engines:
        e.gimbal_deg = gimbal
    p.stageable = bool(p.staging_icon) or any(m in STAGED_MODULES for m in p.modules)
    p.role = classify(p)
    return p


def _engine_from_cfg(mod: ConfigNode, module_name: str) -> EngineMode:
    curve = mod.node("atmosphereCurve")
    keys = [parse_floats(v) for v in (curve.get_all("key") if curve else [])]
    props = []
    for pr in mod.nodes("PROPELLANT"):
        entry: dict[str, Any] = {"name": pr.get("name", ""), "ratio": _f(pr.get("ratio"), 1.0)}
        if pr.has("resourceFlowMode"):
            entry["flow_mode"] = pr.get("resourceFlowMode")
        if parse_bool(pr.get("ignoreForIsp")):
            entry["ignore_for_isp"] = True
        props.append(entry)
    return EngineMode(
        id=mod.get("engineID", "") or "", module=module_name,
        engine_type=mod.get("EngineType", "") or "",
        thrust_vac_kn=_f(mod.get("maxThrust")), min_thrust_kn=_f(mod.get("minThrust")),
        isp_curve=[k for k in keys if len(k) >= 2], propellants=props,
        throttle_locked=parse_bool(mod.get("throttleLocked")),
    )


def classify(p: PartInfo) -> str:
    mods = set(p.modules)
    cat = p.category.lower()
    if "ModuleCommand" in mods:
        return "command_pod" if p.crew > 0 else "probe_core"
    if p.engines:
        names = {n for e in p.engines for n in e.propellant_names}
        primary = set(p.engines[-1].propellant_names) if len(p.engines) > 1 else names
        if "SolidFuel" in names:
            return "solid_booster"
        if "IntakeAir" in primary:
            return "jet_engine"
        if "XenonGas" in names:
            return "ion_engine"
        if names and names <= {"MonoPropellant"}:
            return "monoprop_engine"
        return "liquid_engine"
    if "LaunchClamp" in mods:
        return "launch_clamp"
    if "ModuleAblator" in mods and ("ModuleDecouple" in mods or "heat" in p.name.lower() or cat == "thermal"):
        return "heat_shield"
    if p.decoupler:
        return "radial_decoupler" if p.decoupler["radial"] else "decoupler"
    if "ModuleDockingNode" in mods:
        return "docking_port"
    if "ModuleParachute" in mods:
        return "parachute"
    if "ModuleProceduralFairing" in mods or "ModuleSimpleAdjustableFairing" in mods:
        return "fairing"
    if p.wheel_type == "LEG":
        return "landing_leg"
    if "ModuleWheelBase" in mods:
        return "wheel"
    if mods & {"ModuleRCS", "ModuleRCSFX"}:
        return "rcs_thruster"
    if "ModuleReactionWheel" in mods:
        return "reaction_wheel"
    if mods & {"ModuleControlSurface", "ModuleAeroSurface"}:
        return "control_surface"
    if "ModuleDeployableSolarPanel" in mods:
        return "solar_panel"
    if "ModuleDataTransmitter" in mods and cat == "communication":
        return "antenna"
    if mods & {"ModuleGenerator", "ModuleResourceConverter"}:
        return "generator"
    if mods & {"ModuleScienceExperiment", "ModuleScienceLab", "ModuleScienceConverter", "ModuleOrbitalSurveyor",
                "ModuleScienceContainer", "ModuleResourceScanner"}:
        return "science"
    if "CModuleFuelLine" in mods:
        return "fuel_line"
    if "ModuleResourceIntake" in mods:
        return "intake"
    if "ModuleSAS" in mods:
        return "avionics"
    if "ModuleActiveRadiator" in mods:
        return "radiator"
    if "ModuleResourceHarvester" in mods:
        return "drill"
    if any(m.startswith("ModuleRobotic") for m in mods):
        return "robotics"
    if "RetractableLadder" in mods or "ladder" in p.name.lower():
        return "ladder"
    if "ModuleLight" in mods:
        return "light"
    if "FlagDecalBackground" in mods:
        return "flag"
    res = {k for k, v in p.resources.items() if v["max"] > 0}
    if res & {"LiquidFuel", "Oxidizer"}:
        return "fuel_tank"
    if "MonoPropellant" in res:
        return "monoprop_tank"
    if "XenonGas" in res:
        return "xenon_tank"
    if "Ore" in res:
        return "ore_tank"
    if res == {"ElectricCharge"}:
        return "battery"
    if p.crew > 0:
        return "crew_cabin"
    if "ModuleLiftingSurface" in mods:
        return "aero_surface"
    if cat == "aero" and ("nose" in p.name.lower() or "cone" in p.name.lower()):
        return "nose_cone"
    if cat == "cargo" or "ModuleCargoBay" in mods or "ModuleInventoryPart" in mods:
        return "cargo"
    if len({d for d in (bulkhead_diameter(b) for b in p.bulkhead) if d}) >= 2 and cat in ("structural", "coupling"):
        return "adapter"
    if cat in ("structural", "coupling"):
        return "structural"
    return "other"


def resource_defs_from(nodes: Iterable[ConfigNode]) -> dict[str, ResourceDef]:
    defs: dict[str, ResourceDef] = {}
    for n in nodes:
        name = n.get("name")
        if name:
            defs[name] = ResourceDef(name, _f(n.get("density")), (n.get("flowMode") or "NO_FLOW").strip(),
                                     _f(n.get("unitCost")))
    return defs


# ---------------------------------------------------------------------------------------------
# Live bridge data


def _vec(x: Any, default: list[float]) -> list[float]:
    try:
        vals = [float(c) for c in (x if isinstance(x, (list, tuple)) else parse_floats(str(x)))]
    except (TypeError, ValueError):
        return list(default)
    return vals[:3] if len(vals) >= 3 else list(default)


def _num(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _flag(x: Any, default: bool = False) -> bool:
    return x if isinstance(x, bool) else parse_bool(None if x is None else str(x), default)


def part_from_bridge_full(d: dict[str, Any], resource_defs: dict[str, ResourceDef]) -> PartInfo:
    """One entry of the extended ``/part-database`` (detail=full) -> PartInfo."""
    name = str(d.get("name", ""))
    nodes = [AttachNode(str(n.get("id", "")), _vec(n.get("pos"), [0, 0, 0]), _vec(n.get("dir"), [0, 1, 0]),
                        int(_num(n.get("size"), 1))) for n in d.get("nodes") or []]
    srf = d.get("srfNode")
    rules = d.get("attachRules") or {}
    p = PartInfo(
        name=name, cfg_name=name.replace(".", "_"), title=str(d.get("title", "")),
        category=str(d.get("category", "")), cost=_num(d.get("cost")), mass_t=_num(d.get("mass_t")),
        crew=int(_num(d.get("crewCapacity"))),
        bulkhead=[b.strip() for b in str(d.get("bulkhead", "")).split(",") if b.strip()],
        tags=str(d.get("tags", "")), tech_required=str(d.get("techRequired") or ""), nodes=nodes,
        srf_node=AttachNode("srfAttach", _vec(srf.get("pos"), [0, 0, 0]), _vec(srf.get("dir"), [1, 0, 0]),
                            int(_num(srf.get("size"), 1))) if isinstance(srf, dict) else None,
        attach_rules={k: _flag(rules.get(k), dflt) for k, dflt in zip(_ATTACH_RULE_KEYS, _ATTACH_RULE_DEFAULTS)},
        crossfeed=_flag(d.get("fuelCrossFeed"), True), stageable=_flag(d.get("stageable")),
        staging_icon=str(d.get("stagingIcon") or ""), max_temp=_num(d.get("maxTemp")),
        skin_max_temp=_num(d.get("skinMaxTemp")), crash_tolerance=_num(d.get("crashTolerance")),
        loaded=True, source="bridge-full",
    )
    b = d.get("bounds")
    if isinstance(b, dict) and b.get("size") is not None:
        p.bounds = {"size": _vec(b.get("size"), [0, 0, 0]), "center": _vec(b.get("center"), [0, 0, 0])}
    for rname, r in (d.get("resources") or {}).items():
        if isinstance(r, dict):
            mx = _num(r.get("max"))
            dens = _num(r.get("density"), resource_defs[rname].density_t if rname in resource_defs else 0.0)
            p.resources[rname] = {"amount": _num(r.get("amount"), mx), "max": mx, "density_t": dens}
        else:
            mx = _num(r)
            p.resources[rname] = {"amount": mx, "max": mx,
                                  "density_t": resource_defs[rname].density_t if rname in resource_defs else 0.0}
    p.modules = [m if isinstance(m, str) else str(m.get("name", "")) for m in d.get("modules") or []]
    # "type" is the EngineType; the module class is "module" (newer bridges), else the n-th engine
    # module in the prefab's module list
    engine_modules = [m for m in p.modules if m.startswith("ModuleEngines")]
    for i, e in enumerate(d.get("engines") or []):
        curve = curve_keys(e.get("atmosphereCurve"))
        if not curve:
            curve = [[0.0, _num(e.get("isp_vac"))], [1.0, _num(e.get("isp_asl"))]]
        module = str(e.get("module") or "").strip()
        if not module:
            module = engine_modules[i] if i < len(engine_modules) else "ModuleEngines"
        p.engines.append(EngineMode(
            id=str(e.get("id") or ""), module=module,
            engine_type=str(e.get("type") or ""),
            thrust_vac_kn=_num(e.get("maxThrust_kn")), min_thrust_kn=_num(e.get("minThrust_kn")),
            isp_curve=curve, propellants=[{"name": str(pr.get("name")), "ratio": _num(pr.get("ratio"), 1.0)}
                                          | ({"flow_mode": pr["flowMode"]} if pr.get("flowMode") else {})
                                          | ({"ignore_for_isp": True} if _flag(pr.get("ignoreForIsp")) else {})
                                          for pr in e.get("propellants") or []],
            throttle_locked=_flag(e.get("throttleLocked")), gimbal_deg=_num(e.get("gimbal_deg"))))
    b9 = d.get("b9Tank")
    if isinstance(b9, dict):  # B9PartSwitch tank mass is not in the prefab mass
        p.mass_t += _num(b9.get("tankMass_t")) + _num(b9.get("addedMass_t"))
    dec = d.get("decoupler")
    if isinstance(dec, dict):
        explosive = str(dec.get("explosiveNodeId") or "top")
        p.decoupler = {"ejection_force": _num(dec.get("ejectionForce")), "is_omni": _flag(dec.get("isOmni")),
                       "explosive_node": explosive, "radial": _flag(dec.get("radial")) or explosive == "srf"}
    if isinstance(d.get("parachute"), dict):
        p.parachute = dict(d["parachute"])
    if isinstance(d.get("command"), dict):
        p.command = {"minimum_crew": int(_num(d["command"].get("minimumCrew")))}
    rw = d.get("reactionWheel")
    if isinstance(rw, dict):
        p.reaction_wheel = {"pitch_knm": _num(rw.get("pitch")), "yaw_knm": _num(rw.get("yaw")),
                            "roll_knm": _num(rw.get("roll"))}
    if "ModuleToggleCrossfeed" in p.modules:
        p.crossfeed_toggle = True
    if not p.stageable:
        p.stageable = bool(p.staging_icon) or any(m in STAGED_MODULES for m in p.modules)
    p.role = classify(p)
    return p


def overlay_basic_live(p: PartInfo, live: dict[str, Any], resource_defs: dict[str, ResourceDef]) -> None:
    """Apply the basic ``GET /part-database`` physics (authoritative) onto a cfg-derived part."""
    p.loaded = True
    p.source = "cfg+live"
    if live.get("title"):
        p.title = str(live["title"])
    if live.get("category"):
        p.category = str(live["category"])
    if live.get("bulkhead"):
        p.bulkhead = [b.strip() for b in str(live["bulkhead"]).split(",") if b.strip()]
    p.crew = int(_num(live.get("crewCapacity"), p.crew))
    p.cfg_mass_t = p.mass_t
    p.mass_t = _num(live.get("dryMassT"), p.mass_t)
    for rname, mx in (live.get("resources") or {}).items():
        mx = _num(mx)
        old = p.resources.get(rname)
        frac = old["amount"] / old["max"] if old and old["max"] > 0 else 1.0
        dens = resource_defs[rname].density_t if rname in resource_defs else (old or {}).get("density_t", 0.0)
        p.resources[rname] = {"amount": mx * frac, "max": mx, "density_t": dens}
    if "maxThrustKn" in live and "ispVacS" in live:
        thrust, isp_vac, isp_asl = _num(live["maxThrustKn"]), _num(live["ispVacS"]), _num(live.get("ispAslS"))
        match = next((e for e in p.engines if abs(e.isp_vac - isp_vac) < 0.5), None)
        if match is not None:
            match.thrust_vac_kn = thrust
        elif not p.engines:
            p.engines.append(EngineMode("", "ModuleEngines", "", thrust, 0.0, [[0.0, isp_vac], [1.0, isp_asl]], []))
    p.role = classify(p)


# ---------------------------------------------------------------------------------------------
# The catalog


class Catalog:
    def __init__(self, parts: dict[str, PartInfo], resource_defs: dict[str, ResourceDef],
                 source: str = "cfg", fingerprint: str = "", built_at: float = 0.0):
        self.parts = parts
        self.resource_defs = resource_defs
        self.source = source
        self.fingerprint = fingerprint
        self.built_at = built_at or time.time()
        self._lower = {k.lower(): k for k in parts}

    def __len__(self) -> int:
        return len(self.parts)

    def __contains__(self, name: str) -> bool:
        return self.resolve(name) is not None

    # -- lookup -------------------------------------------------------------------------------

    def resolve(self, name: str) -> PartInfo | None:
        """Exact live name, the cfg (underscore) spelling, or a case-insensitive match."""
        if not name:
            return None
        for cand in (name, live_name(name)):
            if cand in self.parts:
                return self.parts[cand]
            key = self._lower.get(cand.lower())
            if key:
                return self.parts[key]
        return None

    def get(self, name: str) -> PartInfo:
        p = self.resolve(name)
        if p is None:
            close = self.suggest(name)
            raise CatalogError(f"unknown part {name!r}",
                               f"closest names: {close}" if close else "use parts_search to find part names")
        return p

    def suggest(self, name: str, n: int = 5) -> list[str]:
        """Loaded part names resembling ``name`` (spelling, then search matches)."""
        visible = [k for k, p in self.parts.items() if p.loaded is not False]
        lowered = {k.lower(): k for k in visible}
        close = [lowered[m] for m in difflib.get_close_matches(name.lower(), list(lowered), n=n, cutoff=0.6)]
        for p in self.search(name, limit=n):
            if p.name not in close:
                close.append(p.name)
        return close[:n]

    @property
    def kerbal_mass_t(self) -> float | None:
        """Mass one seated kerbal adds. KSP 1.11+ loads crewed parts lighter than their cfg mass by
        crew x kerbal mass; the median of that difference over loaded crewed parts measures it."""
        diffs = [(p.cfg_mass_t - p.mass_t) / p.crew for p in self.parts.values()
                 if p.crew > 0 and p.cfg_mass_t is not None and p.loaded and p.cfg_mass_t - p.mass_t > 1e-3]
        return round(statistics.median(diffs), 4) if diffs else None

    def density(self, resource: str) -> float:
        rd = self.resource_defs.get(resource)
        return rd.density_t if rd else 0.0

    def flow_mode(self, resource: str) -> str:
        rd = self.resource_defs.get(resource)
        return rd.flow_mode if rd else "STACK_PRIORITY_SEARCH"

    # -- search -------------------------------------------------------------------------------

    def search(self, query: str | None = None, role: str | None = None, diameter_m: float | None = None,
               propellant: str | None = None, min_thrust_kn: float | None = None,
               surface_attachable: bool | None = None, include_hidden: bool = False,
               limit: int = 20) -> list[PartInfo]:
        words = [w for w in re.split(r"\s+", (query or "").strip().lower()) if w]
        scored: list[tuple[float, str, PartInfo]] = []
        for p in self.parts.values():
            if not include_hidden and (p.category.lower() == "none" or p.loaded is False):
                continue
            if role and p.role != role:
                continue
            if diameter_m is not None and not any(abs(d - diameter_m) < 0.05 for d in p.diameters_m):
                continue
            if propellant and not _matches_family(p, propellant):
                continue
            if min_thrust_kn is not None and not any(e.thrust_vac_kn >= min_thrust_kn for e in p.engines):
                continue
            if surface_attachable is not None and p.surface_attachable != surface_attachable:
                continue
            score = _score(p, words) if words else 1.0
            if score > 0:
                scored.append((score, p.name.lower(), p))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [p for _, _, p in scored[:max(1, limit)]]

    # -- persistence --------------------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {"version": CATALOG_VERSION, "fingerprint": self.fingerprint, "source": self.source,
                "built_at": self.built_at,
                "resource_defs": {k: asdict(v) for k, v in self.resource_defs.items()},
                "parts": {k: v.to_dict() for k, v in self.parts.items()}}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Catalog:
        return cls({k: PartInfo.from_dict(v) for k, v in d["parts"].items()},
                   {k: ResourceDef(**v) for k, v in d.get("resource_defs", {}).items()},
                   d.get("source", "cfg"), d.get("fingerprint", ""), d.get("built_at", 0.0))

    @classmethod
    def build(cls, gamedata: Path | None = None, bridge: Any = None,
              craft_dirs: Iterable[Path] | None = None) -> Catalog:
        """Build from cfgs (+ live bridge data when ``bridge`` is given and answers)."""
        gamedata = Path(gamedata or CONFIG.gamedata_dir)
        cfg_parts, resource_defs, english = _read_configs(gamedata)
        source = "cfg"
        parts = cfg_parts
        if bridge is not None:
            full = _bridge_full(bridge)
            if full:
                parts = {}
                for d in full:
                    p = part_from_bridge_full(d, resource_defs)
                    base = cfg_parts.get(p.name)
                    if base is not None:
                        named = all(isinstance(e, dict) and str(e.get("module") or "").strip()
                                    for e in d.get("engines") or [])
                        _enrich_from_cfg(p, base, bridge_modules=named)
                    parts[p.name] = p
                source = "bridge-full"
            else:
                basic = _bridge_basic(bridge)
                if basic:
                    live = {str(d.get("name")): d for d in basic}
                    for name, p in parts.items():
                        if name in live:
                            overlay_basic_live(p, live[name], resource_defs)
                        else:
                            p.loaded = False
                    source = "cfg+live"
        for p in parts.values():
            if not p.title_en:
                p.title_en = english.get(p.name, "")
        cat = cls(parts, resource_defs, source, fingerprint(gamedata))
        learn_from_craft(cat, craft_dirs if craft_dirs is not None else _stock_craft_dirs(gamedata))
        return cat

    @classmethod
    def load(cls, *, refresh: bool = False, bridge: Any = None, gamedata: Path | None = None,
             cache_path: Path | None = None) -> Catalog:
        """Cached catalog; rebuilt when GameData changed, when forced, or when the cache holds only
        cfg data while the bridge now answers."""
        gamedata = Path(gamedata or CONFIG.gamedata_dir)
        cache_path = Path(cache_path or CONFIG.cache_dir / "parts.json")
        fp = fingerprint(gamedata)
        if not refresh and cache_path.exists():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            if data.get("version") == CATALOG_VERSION and data.get("fingerprint") == fp:
                stale = data.get("source") == "cfg" and bridge is not None and _bridge_up(bridge)
                if not stale:
                    return cls.from_json(data)
        cat = cls.build(gamedata, bridge)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cat.to_json(), ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return cat


def get_catalog(bridge: Any = None, refresh: bool = False) -> Catalog:
    """The shared catalog (built on first use). Pass the bridge to prefer live data."""
    global _SHARED
    with _LOCK:
        if _SHARED is None or refresh or (_SHARED.source == "cfg" and bridge is not None and _bridge_up(bridge)):
            _SHARED = Catalog.load(refresh=refresh, bridge=bridge)
        return _SHARED


def set_catalog(cat: Catalog | None) -> None:
    """Install a catalog as the shared one (tests, offline tools)."""
    global _SHARED
    with _LOCK:
        _SHARED = cat


def _matches_family(p: PartInfo, family: str) -> bool:
    fam = family.strip()
    for key in PROPELLANT_FAMILIES:
        if key.lower() == fam.lower():
            fam = key
            break
    if fam not in PROPELLANT_FAMILIES:
        return False
    if p.engines:
        return any(_family_of(set(e.propellant_names)) == fam for e in p.engines)
    return _family_of({k for k, v in p.resources.items() if v["max"] > 0}) == fam


def _family_of(names: set[str]) -> str:
    for fam in ("Air", "Solid", "Xenon", "LFO"):
        if PROPELLANT_FAMILIES[fam] <= names:
            return fam
    if "LiquidFuel" in names:
        return "LF"
    if "MonoPropellant" in names:
        return "Mono"
    return ""


def _score(p: PartInfo, words: list[str]) -> float:
    name, en, loc, tags = p.name.lower(), p.title_en.lower(), p.title.lower(), p.tags.lower()
    whole = " ".join(words)
    if name == whole or p.cfg_name.lower() == whole:
        return 100.0
    total = 0.0
    for w in words:
        if name.startswith(w):
            s = 60.0
        elif w in name:
            s = 45.0
        elif w in en:
            s = 40.0
        elif w in loc:
            s = 35.0
        elif w in p.role:
            s = 20.0
        elif w in tags:
            s = 10.0
        else:
            return 0.0
        total += s
    return total / len(words)


# ---------------------------------------------------------------------------------------------
# Sources


def fingerprint(gamedata: Path) -> str:
    cache = gamedata / "ModuleManager.ConfigCache"
    h = hashlib.sha1(f"v{CATALOG_VERSION}".encode())
    partdb = gamedata.parent / "PartDatabase.cfg"
    if partdb.exists():
        st = partdb.stat()
        h.update(f"pdb:{st.st_size}:{st.st_mtime_ns}".encode())
    for dll in sorted((gamedata / "KspAutomationBridge").rglob("*.dll")):  # a new bridge may export more
        st = dll.stat()
        h.update(f"bridge:{dll.name}:{st.st_size}:{st.st_mtime_ns}".encode())
    if cache.exists():
        st = cache.stat()
        h.update(f"mm:{st.st_size}:{st.st_mtime_ns}".encode())
        return h.hexdigest()[:16]
    for f in sorted(gamedata.rglob("*.cfg")):
        try:
            st = f.stat()
        except OSError:
            continue
        h.update(f"{f.relative_to(gamedata)}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:16]


def _read_configs(gamedata: Path) -> tuple[dict[str, PartInfo], dict[str, ResourceDef], dict[str, str]]:
    """Parts (from the MM cache or raw cfgs), resource definitions, and English titles."""
    raw_parts: list[ConfigNode] = []
    raw_res: list[ConfigNode] = []
    english: dict[str, str] = {}
    for f in sorted(gamedata.rglob("*.cfg")):
        try:
            text = read_text(f)
        except OSError:
            continue
        if "PART" not in text and "RESOURCE_DEFINITION" not in text:
            continue
        root = ConfigNode.parse(text)
        for n in root.nodes("PART"):
            raw_parts.append(n)
            t = n.get_value("title")
            if n.get("name") and t is not None and english_title(t):
                english[live_name(n.get("name").strip())] = english_title(t)
        raw_res.extend(root.nodes("RESOURCE_DEFINITION"))

    part_nodes, res_nodes = raw_parts, raw_res
    cache = gamedata / "ModuleManager.ConfigCache"
    if cache.exists():
        root = ConfigNode.load(cache)
        cached_parts = [n for u in root.nodes("UrlConfig") for n in u.nodes("PART")]
        cached_res = [n for u in root.nodes("UrlConfig") for n in u.nodes("RESOURCE_DEFINITION")]
        if cached_parts:
            part_nodes, res_nodes = cached_parts, cached_res or raw_res
    defs = resource_defs_from(res_nodes)
    bounds = drag_cube_bounds(gamedata.parent / "PartDatabase.cfg")
    parts: dict[str, PartInfo] = {}
    for n in part_nodes:
        p = part_from_cfg(n, defs)
        if p is not None:
            p.bounds = bounds.get(p.cfg_name)
            if p.name in english:
                p.title_en = english[p.name]
                if p.title.startswith("#"):
                    p.title = p.title_en
            parts[p.name] = p
    return parts, defs, english


def drag_cube_bounds(path: Path) -> dict[str, dict[str, list[float]]]:
    """Part bounds (part-local centre and size, metres) from KSP's PartDatabase.cfg drag cubes,
    keyed by cfg part name. KSP renders every prefab to build these, so they are the real mesh
    extents of the default variant ('Default' or 'Clean' cube, else the first)."""
    out: dict[str, dict[str, list[float]]] = {}
    if not path.exists():
        return out
    for part in ConfigNode.load(path).nodes("PART"):
        url = part.get("url") or ""
        cube_node = part.node("DRAG_CUBE")
        if not url or cube_node is None:
            continue
        cubes = {}
        for text in cube_node.get_all("cube"):
            name, _, rest = text.partition(",")
            vals = parse_floats(rest)
            if len(vals) == 24:
                cubes[name.strip()] = vals
        if not cubes:
            continue
        vals = cubes.get("Default") or cubes.get("Clean") or next(iter(cubes.values()))
        out[url.rsplit("/", 1)[-1]] = {"center": vals[18:21], "size": vals[21:24]}
    return out


def _enrich_from_cfg(p: PartInfo, base: PartInfo, *, bridge_modules: bool = False) -> None:
    """Fill what the extended bridge entry lacks from the cfg entry. ``bridge_modules``: the bridge
    named every engine's module class, so the cfg must not override it."""
    p.cfg_name = base.cfg_name
    p.cfg_mass_t = base.mass_t
    p.title_en = p.title_en or base.title_en
    p.tech_required = p.tech_required or base.tech_required
    p.variants = p.variants or base.variants
    p.com_offset = base.com_offset
    if p.bounds is None:
        p.bounds = base.bounds
    if not p.modules:
        p.modules = list(base.modules)
    if not p.tags:
        p.tags = base.tags
    if not p.crossfeed_toggle and base.crossfeed_toggle:
        p.crossfeed_toggle = True
    p.wheel_type = p.wheel_type or base.wheel_type
    for e in p.engines:  # older bridges omit tangents/flow modes; use the cfg's when the curves agree
        twin = next((b for b in base.engines if b.id == e.id or len(base.engines) == 1), None)
        live_tangents = any(len(k) >= 4 for k in e.isp_curve)  # the loaded prefab curve: exact
        if (twin and not live_tangents and abs(twin.isp_vac - e.isp_vac) < 0.5
                and any(len(k) >= 4 for k in twin.isp_curve)):
            e.isp_curve = twin.isp_curve
        if twin and not e.engine_type:
            e.engine_type = twin.engine_type
        if twin and not bridge_modules and len(p.engines) == len(base.engines):
            e.module = twin.module
        modes = {pr["name"]: pr.get("flow_mode") for pr in (twin.propellants if twin else [])}
        for pr in e.propellants:
            if not pr.get("flow_mode") and modes.get(pr["name"]):
                pr["flow_mode"] = modes[pr["name"]]
    p.role = classify(p)


def _bridge_up(bridge: Any) -> bool:
    try:
        return bool(bridge.up())
    except Exception:  # noqa: BLE001
        return False


def _bridge_full(bridge: Any) -> list[dict[str, Any]] | None:
    if not _bridge_up(bridge):
        return None
    try:
        data = bridge.post("/part-database", {"detail": "full"}, timeout=90)
    except Exception:  # noqa: BLE001 — old bridges only have GET; fall back
        return None
    parts = data.get("parts") or []
    if parts and isinstance(parts[0], dict) and "nodes" in parts[0]:
        return parts
    return None


def _bridge_basic(bridge: Any) -> list[dict[str, Any]] | None:
    if not _bridge_up(bridge):
        return None
    try:
        return bridge.get("/part-database", timeout=90).get("parts") or None
    except Exception:  # noqa: BLE001
        return None


def _stock_craft_dirs(gamedata: Path) -> list[Path]:
    root = gamedata.parent / "Ships"
    return [root / "VAB", root / "SPH"]


def learn_from_craft(cat: Catalog, craft_dirs: Iterable[Path]) -> None:
    """Fill geometry gaps from craft files KSP itself saved (the stock Ships folders):

    * attach nodes that a part defines on model transforms (``NODE {}``), whose positions are not in
      any cfg: KSP writes every used node's position (and, since 1.10, its direction) in ``attN``;
    * the outward face of radially mounted parts (radial decouplers, pylons, side tanks): where KSP
      put the parts surface-attached straight out from them.
    Used only where live bridge data is missing."""
    from astra.craft.geometry import outward_dir, q_inv, q_rot, v_add, v_dot, v_norm, v_sub

    faces: dict[str, list[float]] = {}
    nodes: dict[tuple[str, str], list[tuple[list[float], list[float] | None]]] = {}
    for d in craft_dirs:
        for f in sorted(Path(d).glob("*.craft")) if Path(d).is_dir() else []:
            try:
                root = ConfigNode.load(f)
            except OSError:
                continue
            blocks = {(pn.get("part") or "").strip(): pn for pn in root.nodes("PART")}
            for pid, pn in blocks.items():
                info = cat.resolve(craft_part_name(pid))
                if info is None:
                    continue
                for line in pn.get_all("attN"):
                    nid, _, pos, direction = parse_attn(line)
                    n = info.node(nid)
                    if n is not None and not n.known and pos is not None:
                        nodes.setdefault((info.name, nid), []).append((pos, direction))
                srf = (pn.get("srfN") or "").split(",")
                if len(srf) < 2 or srf[0] != "srfAttach" or srf[1] not in blocks:
                    continue
                parent = blocks[srf[1]]
                psrf = (parent.get("srfN") or "").split(",")
                if len(psrf) < 2 or psrf[0] != "srfAttach":
                    continue  # faces: only parents that are themselves radially mounted
                parent_info = cat.resolve(craft_part_name(srf[1]))
                if not parent_info or not info.srf_node or not parent_info.srf_node:
                    continue
                c_pos, c_rot = parse_floats(pn.get("pos")), parse_floats(pn.get("rot"))
                p_pos, p_rot = parse_floats(parent.get("pos")), parse_floats(parent.get("rot"))
                if len(c_pos) != 3 or len(c_rot) != 4 or len(p_pos) != 3 or len(p_rot) != 4:
                    continue
                point = v_add(c_pos, q_rot(c_rot, info.srf_node.pos))
                local = q_rot(q_inv(p_rot), v_sub(point, p_pos))
                outward = outward_dir(parent_info.srf_node)
                across = list(local)
                if abs(outward[1]) < 0.5:
                    across[1] = 0.0  # compare directions in the plane of the parent's surface
                if v_dot(v_norm(across), outward) < 0.94:
                    continue  # attached somewhere else on the part, not on its outer face
                faces.setdefault(parent_info.name, []).append(v_dot(local, outward))
    for name, vals in faces.items():
        p = cat.parts.get(name)
        if p is not None and p.outer_face_m is None:
            p.outer_face_m = round(statistics.median(vals), 4)
    for (name, nid), samples in nodes.items():
        n = cat.parts[name].node(nid)
        n.pos = [round(statistics.median(s[0][i] for s in samples), 6) for i in range(3)]
        dirs = [s[1] for s in samples if s[1] is not None]
        if dirs:
            mean = [sum(dv[i] for dv in dirs) for i in range(3)]
            norm = math.sqrt(sum(c * c for c in mean)) or 1.0
            n.dir = [round(c / norm, 6) for c in mean]
        n.known = True


_ATTN_RE = re.compile(r"^(?P<pid>.*?_\d+)(?:_(?P<pos>[^_|]+\|[^_|]+\|[^_|]+)(?:_(?P<dir>[^_|]+\|[^_|]+\|[^_|]+))?(?:_.*)?)?$")


def parse_attn(line: str) -> tuple[str, str, list[float] | None, list[float] | None]:
    """Split a craft ``attN`` value into (own node id, attached part id, own node position, direction).

    Handles KSP's short form ``bottom,Decoupler.1_4294606904_0|-0.405|0`` and the 1.10+ form that
    appends the direction and the original position/direction; free nodes name the part ``Null_0``."""
    node_id, _, target = line.partition(",")
    m = _ATTN_RE.match(target.strip())
    if not m:
        return node_id.strip(), "", None, None

    def vec(text: str | None) -> list[float] | None:
        if not text:
            return None
        try:
            return [float(c) for c in text.split("|")]
        except ValueError:
            return None

    return node_id.strip(), m.group("pid"), vec(m.group("pos")), vec(m.group("dir"))


def craft_part_name(part_id: str) -> str:
    """'fuelTank.long_4294453612' -> 'fuelTank.long' (strip the trailing _<uid>)."""
    base, sep, tail = part_id.strip().rpartition("_")
    return base if sep and tail.isdigit() else part_id.strip()
