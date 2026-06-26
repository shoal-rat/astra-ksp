"""Stock-part catalog — ONE schema (``StockPart``), filled ENTIRELY from authoritative sources. No
hand-curated part values, every part on equal footing.

AUTHORITATIVE SOURCES (the "Transport Pod" schema — every field traces to one of these, none is a
hand-tuned literal):

  PHYSICS  (dry_mass_t, wet_mass_t, thrust ASL/vac, isp ASL/vac, LF/Ox/SolidFuel capacity)
      <- the LIVE-reconciled catalog in ``data/stock_parts.json``. That JSON is built by
         ``rebuild_from_live`` from the running game's ``/part-database`` (PartLoader's post-load truth:
         cfg + ModuleManager + variant resolution) and is verified 0-mismatch by
         ``tools/validate_parts_live.py``. Offline, the fallback is the bare cfg parse (same fields,
         from ``mass`` / ``maxThrust`` / ``atmosphereCurve`` / ``RESOURCE``).

  GEOMETRY (diameter_m, height_m, stack_size, category, part_type, crew_capacity)
      <- DERIVED from the part ``.cfg``: diameter from the WIDEST stack-node size class /
         ``bulkheadProfiles`` token (see ``_bulkhead_info`` — this is what makes an adapter's wide end
         correct without a hand-typed number), height from the node_stack top<->bottom span scaled by
         ``rescaleFactor``/``scale`` (see ``_node_height``), role/category by ``_classify``. Size-class
         FALLBACKS (a cfg with no sized stack node) use the documented nominal default and are LOGGED.

  AERO COEFFS (drag_area_m2, fin_area_m2)
      <- ``_TUNED_OVERRIDES``: the only fields the .cfg cannot express as the lab's calibrated reference
         areas (KSP stores ``fullyDeployedDrag`` / ``deflectionLiftCoeff``, not the Cd*A / fin reference
         area the lab's terminal-velocity and static-margin math want). These carry NO mass / thrust /
         dimensional override — only the two aero coefficients — and are documented as such.

NO DATA-OVERRIDING CURATION. There is no ``CURATED_PARTS`` layer whose hand-validated numbers win over
the catalog. The live ``/part-database`` reconciliation already PROVED the old curated data wrong (13
crewed-pod dry masses were off vs the running game), so the catalog is now materialized( cfg geometry +
live physics ) and nothing overrides it. The ONLY back-compat layer is ``ALIAS_RENAMES``: a thin
name->name map (e.g. ``RCSBlock`` -> the live ``RCSBlock.v2``) that resolves a legacy import name to its
authoritative live entry. A rename carries NO data — it returns the materialized part unchanged (only
re-keyed), so it cannot reintroduce a hand-tuned value.

The whole stock tree is materialized into ``data/stock_parts.json`` by ``materialize_catalog()`` /
``rebuild_from_live()`` (offline build steps, never at import), so the lab knows every rocket-relevant
PART without the game installed. The design sizer queries the full catalog ("every atmospheric engine in
diameter class 2.5 m, sorted by thrust") on equal footing.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from math import log
from pathlib import Path

from .models import RocketDesign, StageSpec

G0 = 9.80665

# Geometry derivation logs here (named ``_geom_log`` so it never shadows ``math.log``, used below in the
# rocket-equation Δv math). Size-class FALLBACKS (a part whose .cfg lacks a stack diameter, so the
# diameter is the nominal size-class default instead of a parsed value) are emitted at DEBUG so the build
# step / a curious caller can see every place the cfg under-specified geometry. Nothing is hand-tuned;
# the fallback is the documented size-class default, logged so it is never silent.
_geom_log = logging.getLogger(__name__)

# Resource densities (t per unit) from Squad/Resources/ResourcesGeneric.cfg — used to turn a tank's
# RESOURCE amounts into a propellant mass so wet = dry + propellant.
RESOURCE_DENSITY_T = {
    "LiquidFuel": 0.005,
    "Oxidizer": 0.005,
    "SolidFuel": 0.0075,
    "MonoPropellant": 0.004,
    "XenonGas": 0.0001,
    "Ore": 0.010,
}

# bulkheadProfiles size class -> stack diameter (m). This is the part's BODY profile — the authoritative
# hull diameter. ``size1p5`` (1.875 m) is the Making History intermediate class. srf/mk2/mk3 are surface-
# attach / spaceplane profiles with no round stack diameter; they fall back to a nominal 1.25 m for drawing.
BULKHEAD_DIAMETER_M = {
    "size0": 0.625,
    "size1": 1.25,
    "size1p5": 1.875,
    "size2": 2.5,
    "size3": 3.75,
    "size4": 5.0,
}

# A stock attach-node line is ``node_stack_X = px, py, pz, dx, dy, dz, SIZE`` where the trailing integer
# is the node's STACK SIZE CLASS (0 = 0.625 m, 1 = 1.25 m, 2 = 2.5 m, 3 = 3.75 m, 4 = 5 m). This is the
# AUTHORITATIVE per-end diameter KSP itself draws the attach node at — and it is how an ADAPTER encodes
# that its two ends differ (a 2.5 m -> 1.25 m adapter has a size-2 bottom node and a size-1 top node).
# The hull DIAMETER the renderer needs is the WIDEST end (the base the stack rests on), so we read every
# node's size suffix and take the max — which fixes the adapter diameter the single bulkheadProfiles
# token (it lists size1 first) read too narrow. Default size used when a part has only surface nodes.
NODE_SIZE_DIAMETER_M = {
    0: 0.625,
    1: 1.25,
    2: 2.5,
    3: 3.75,
    4: 5.0,
}
# Fallback hull diameter (m) for a part whose cfg exposes NO stack diameter at all (only srf/mk2/mk3
# profiles). Draw-only; such a part is never a standard round-stack part the sizer stacks.
FALLBACK_DIAMETER_M = 1.25


@dataclass(frozen=True, slots=True)
class StockPart:
    name: str
    title: str
    dry_mass_t: float
    wet_mass_t: float
    cost: float
    height_m: float = 1.0
    thrust_kn_asl: float = 0.0
    thrust_kn_vac: float = 0.0
    isp_asl_s: float = 0.0
    isp_vac_s: float = 0.0
    liquid_fuel: float = 0.0
    oxidizer: float = 0.0
    solid_fuel: float = 0.0
    # Fully-deployed parachute drag area Cd*A (m^2), 0 for non-chutes. Used by astro.terminal_velocity
    # to size chute counts for a target body's live atmospheric density. Calibrated from the live
    # datum "one Mk16 lands ~1.2 t at ~6.5 m/s at Kerbin sea level (rho=1.14)": Cd*A = 2 m g / (rho v^2).
    drag_area_m2: float = 0.0
    # Body diameter (m), used with height to estimate the lateral (side-on) area for the centre-of-
    # pressure / static-margin calculation in craft_writer. Stock stacks are 1.25 m or 2.5 m.
    diameter_m: float = 1.25
    # Side-on lift/drag reference area (m^2) for an aero surface (fin/winglet), 0 for body parts. This
    # is the area the centre-of-pressure calculation places at the part's position to stabilise the stack.
    fin_area_m2: float = 0.0
    # Catalog metadata (default-valued so the curated literals below need not pass it). ``category`` is
    # the KSP part category (Engine/FuelTank/Coupling/Pods/...) and ``part_type`` is the lab's coarse
    # role bucket the sizer queries on ("liquid_engine", "solid_booster", "fuel_tank", "decoupler", ...).
    category: str = ""
    part_type: str = ""
    crew_capacity: int = 0
    # The matched stock bulkhead size token (size0..size4), or "" for surface-attach/spaceplane parts.
    # A standard round-stack tank/engine has a real token; the sizer only stacks standard-token tanks so
    # the pool is not polluted by mk2/mk3 fuselages whose diameter is a draw-only 1.25 m fallback.
    stack_size: str = ""

    @property
    def propellant_mass_t(self) -> float:
        return max(0.0, self.wet_mass_t - self.dry_mass_t)


# --------------------------------------------------------------------------------------------------
# NAME FORM. KSP part names appear in TWO forms and the lab must agree with the GAME on which it stores.
# A .cfg / craft-persistence file writes ``name = Rockomax16_BW`` (underscores), but at load the game
# converts that to the runtime ``AvailablePart.name = Rockomax16.BW`` (dots) — and ``/part-database``
# reports the runtime (dotted) form. The craft_writer emits the catalog key verbatim as the ``part = ``
# id, so the catalog MUST key on the dotted live form for the same string to (a) match the live db and
# (b) name a part the game can actually load. ``live_part_name`` is that one canonical form; we apply it
# to every cfg-parsed name so the committed catalog is keyed exactly like the running game.
def live_part_name(cfg_name: str) -> str:
    """Canonicalize a cfg/persistence part name (underscore form) to the live ``AvailablePart.name``
    (dotted) form the game and ``/part-database`` use. Idempotent: a name already in live form is
    returned unchanged. KSP's own convention is a literal ``_`` -> ``.`` swap of the part-id string."""
    return cfg_name.replace("_", ".")


# --------------------------------------------------------------------------------------------------
# ALIAS RENAMES — the ONLY back-compat layer, and it carries NO data.
#
# Some modules import a part by a LEGACY name that the running game renamed (the KSP 1.x part ids were
# superseded by ``.v2`` revisions). An alias is a pure name->name redirect: ``part("RCSBlock")`` resolves
# to the live ``RCSBlock.v2`` materialized entry and returns it UNCHANGED (re-keyed under the legacy name
# so the importer still finds it, but with the authoritative live ``.name`` so the .craft references a
# part the game can load). A rename therefore reintroduces ZERO hand-tuned value — it cannot, because it
# has no StockPart of its own; it points at the one materialized from cfg geometry + live physics.
#
# Each entry's RHS is a LIVE catalog key (the dotted ``AvailablePart.name`` the JSON is keyed on). These
# are the only three legacy ids the codebase still references that the live game spells differently:
#   * RCSBlock           -> RCSBlock.v2          (KSP renamed the RV-105 block to the .v2 revision)
#   * engineLargeSkipper -> engineLargeSkipper.v2 (RE-I5 Skipper .v2 revision)
#   * Size3To2Adapter_v2 -> Size3To2Adapter.v2   (cfg underscore form -> live dotted form)
# (Engines the SIZER picks already come straight from the catalog under their live names, so only the
# hand-referenced literals in design.py / craft_writer.py need these redirects.)
# --------------------------------------------------------------------------------------------------
ALIAS_RENAMES: dict[str, str] = {
    "RCSBlock": "RCSBlock.v2",
    "engineLargeSkipper": "engineLargeSkipper.v2",
    "Size3To2Adapter_v2": "Size3To2Adapter.v2",
}


# --------------------------------------------------------------------------------------------------
# .cfg PARSER — turn a stock PART{} node into a StockPart. Pure regex/brace scanning (no game needed),
# tolerant of the comment-decorated, autoLOC-tokenised, deeply-nested stock config format.
# --------------------------------------------------------------------------------------------------
DEFAULT_GAMEDATA_DIRS = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Kerbal Space Program\GameData\Squad\Parts",
    r"C:\Program Files (x86)\Steam\steamapps\common\Kerbal Space Program\GameData\SquadExpansion",
]

# KSP categories we keep for the rocket-design catalog (drop pure-spaceplane Aero wings, wheels, etc.
# only where they are not rocket-relevant; we keep Aero because nose cones/fairings/fins live there).
_KEEP_CATEGORIES = {
    "Engine", "FuelTank", "Coupling", "Pods", "Control", "Electrical", "Communication",
    "Thermal", "Structural", "Utility", "Aero", "Science", "Ground", "Propulsion", "Payload",
}


def _strip_comments(text: str) -> str:
    """Drop // line comments but KEEP the autoLOC readable title we recover separately."""
    out = []
    for line in text.splitlines():
        idx = line.find("//")
        out.append(line if idx < 0 else line[:idx])
    return "\n".join(out)


def _split_top_blocks(body: str, key: str) -> list[str]:
    """Return the brace-matched bodies of every top-level ``key { ... }`` block inside ``body``."""
    blocks: list[str] = []
    i = 0
    pat = re.compile(r"\b" + re.escape(key) + r"\b\s*\{", re.IGNORECASE)
    while True:
        m = pat.search(body, i)
        if not m:
            break
        depth = 1
        j = m.end()
        while j < len(body) and depth:
            c = body[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        blocks.append(body[m.end():j - 1])
        i = j
    return blocks


def _field(body: str, key: str) -> str | None:
    m = re.search(r"(?m)^\s*" + re.escape(key) + r"\s*=\s*(.+?)\s*$", body)
    return m.group(1).strip() if m else None


def _float(body: str, key: str, default: float = 0.0) -> float:
    v = _field(body, key)
    if v is None:
        return default
    try:
        return float(v.split()[0])
    except (ValueError, IndexError):
        return default


def _readable_title(raw_block: str) -> str:
    """The stock title is ``title = #autoLOC_500532 //#autoLOC_500532 = FL-T400 Fuel Tank``. Recover the
    human text after the last ``=`` in the trailing comment; fall back to the token itself."""
    m = re.search(r"(?m)^\s*title\s*=\s*(.+)$", raw_block)
    if not m:
        return ""
    line = m.group(1)
    if "//" in line:
        comment = line.split("//", 1)[1]
        if "=" in comment:
            return comment.split("=", 1)[1].strip()
        return comment.strip().lstrip("#").strip()
    return line.strip()


def _node_height(body: str) -> float:
    """(node_stack_top.y - node_stack_bottom.y) × the part's node-position scaling = stacked height (m).

    KSP scales attach-node positions by ``rescaleFactor`` (default 1.0 here). CRITICALLY, the legacy
    engine models (Reliant/Swivel/Terrier/...) author their nodes in an UNSCALED model space and set
    ``scale = 0.1`` — KSP multiplies the node offsets by that ``scale`` too. Ignoring it read the Reliant
    as a 14 m monster (raw nodes ±7) instead of ~1.42 m; every legacy engine height was therefore wrong,
    which corrupted stage fineness and the rendered stack length. Multiply by ``scale`` whenever it is set
    AND differs from 1 (tanks set scale=1/omit it and were already correct), then by ``rescaleFactor``."""
    top = _field(body, "node_stack_top")
    bot = _field(body, "node_stack_bottom")
    if not top and not bot:
        return 0.0
    try:
        # A few base-mounted engines (Mammoth, Twin-Boar) define ONLY node_stack_top — their bottom IS
        # the part origin (y=0), where the bell/base sits. Treat a missing bottom node as y=0 so their
        # height is the top-node offset, not the 1 m fallback (which read the Mammoth as a 1 m stub).
        ty = float(top.split(",")[1]) if top else 0.0
        by = float(bot.split(",")[1]) if bot else 0.0
    except (ValueError, IndexError):
        return 0.0
    rescale = _float(body, "rescaleFactor", 1.0)
    scale = _float(body, "scale", 1.0)
    mult = (rescale if rescale > 0 else 1.0) * (scale if scale > 0 else 1.0)
    return abs(ty - by) * mult


def _node_stack_diameters(body: str) -> list[float]:
    """Every STACK attach node's diameter (m), read from the trailing size-class integer of each
    ``node_stack_*`` line (``... , dy, dz, SIZE``). This is the per-end diameter KSP draws the node at,
    and it is how an adapter records its two different ends. Returns [] when no stack node carries a size
    suffix (older parts omit it)."""
    out: list[float] = []
    for m in re.finditer(r"(?m)^\s*node_stack_\w+\s*=\s*(.+?)\s*$", body):
        fields = [f.strip() for f in m.group(1).split(",") if f.strip()]
        if len(fields) >= 7:
            try:
                size_cls = int(float(fields[6]))
            except (ValueError, IndexError):
                continue
            if size_cls in NODE_SIZE_DIAMETER_M:
                out.append(NODE_SIZE_DIAMETER_M[size_cls])
    return out


def _bulkhead_info(body: str, part_name: str = "") -> tuple[float, str]:
    """Return (hull diameter m, size token) derived ENTIRELY from the .cfg geometry, no hand-tuned value.

    PRIORITY 1 — ``bulkheadProfiles``. This is the part's BODY profile and the authoritative hull
    diameter. We take the WIDEST recognized size token, which gives the right answer for BOTH a normal
    part (a single token = its body, e.g. a tank's ``size2``) AND an ADAPTER (KSP lists BOTH ends, e.g.
    ``size1, size2`` -> the 2.5 m wide base the stack rests on). The returned token is that widest one.

    PRIORITY 2 — node stack size class. If ``bulkheadProfiles`` is absent or names only non-standard
    profiles (srf/mk2/mk3/...), fall to the WIDEST ``node_stack_*`` size-class suffix (the diameter KSP
    draws that attach node at). This recovers a diameter for older parts that omit bulkheadProfiles.

    FALLBACK — a part with neither a recognized bulkhead token nor a sized stack node has no round stack
    diameter (a pure surface/spaceplane part): it uses the nominal FALLBACK_DIAMETER_M for DRAWING only
    (token "" => the sizer never stacks it), and the fallback is LOGGED so it is never silent."""
    _dia_to_token = {d: tok for tok, d in BULKHEAD_DIAMETER_M.items()}

    bp = _field(body, "bulkheadProfiles")
    bp_tokens = [t.strip() for t in bp.replace(",", " ").split()] if bp else []
    bp_dias = [(BULKHEAD_DIAMETER_M[t], t) for t in bp_tokens if t in BULKHEAD_DIAMETER_M]
    if bp_dias:                                            # PRIORITY 1: the body profile (widest token)
        return max(bp_dias, key=lambda c: c[0])

    node_dias = _node_stack_diameters(body)
    if node_dias:                                          # PRIORITY 2: widest sized stack node
        dia = max(node_dias)
        _geom_log.debug("geometry: %s has no standard bulkheadProfiles token; using widest node size "
                  "%.3f m", part_name or "<part>", dia)
        return dia, _dia_to_token.get(dia, "")

    # No sized stack geometry at all (srf/mk2/mk3 only): draw-only fallback, logged.
    _geom_log.debug("geometry fallback: %s has no sized stack node/bulkhead; "
              "using nominal %.3f m draw diameter (cfg under-specified)",
              part_name or "<part>", FALLBACK_DIAMETER_M)
    return FALLBACK_DIAMETER_M, ""


def _resources(body: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for res in _split_top_blocks(body, "RESOURCE"):
        name = _field(res, "name")
        amount = _float(res, "maxAmount", _float(res, "amount", 0.0))
        if name:
            out[name] = out.get(name, 0.0) + amount
    return out


def _parse_one_engine_module(mod: str) -> dict:
    """Pull maxThrust (kN), the Isp atmosphereCurve (key 0 = ASL Isp, key 1 = vacuum Isp), the engine
    type and the propellant names from ONE ModuleEngines* block."""
    thrust = _float(mod, "maxThrust", 0.0)
    isp_asl = isp_vac = 0.0
    for curve in _split_top_blocks(mod, "atmosphereCurve"):
        keys = re.findall(r"(?m)^\s*key\s*=\s*([\d.eE+-]+)\s+([\d.eE+-]+)", curve)
        kv = {}
        for press, isp in keys:
            try:
                kv[float(press)] = float(isp)
            except ValueError:
                continue
        if kv:
            isp_vac = kv.get(0.0, 0.0)
            isp_asl = kv.get(1.0, isp_vac)
            break
    propellants = {(_field(p, "name") or "").strip() for p in _split_top_blocks(mod, "PROPELLANT")}
    propellants.discard("")
    etype = (_field(mod, "EngineType") or "").strip()
    return {"thrust_kn": thrust, "isp_asl": isp_asl, "isp_vac": isp_vac,
            "engine_type": etype, "propellants": propellants}


def _engine_module(body: str) -> dict | None:
    """The part's PROPULSIVE engine module. For a multimode engine (e.g. the RAPIER: a Turbine air-
    breathing mode + a LiquidFuel closed-cycle rocket mode) prefer the ROCKET mode so the catalog
    records the part as a usable rocket engine, not its jet mode. Returns None for non-engine parts.

    ``air_breathing`` flags a pure jet (burns IntakeAir, no Oxidizer) so the sizer can keep it OUT of
    the rocket engine pool — a jet has a meaningless 10000 s 'Isp' for vacuum rocketry."""
    mods = [_parse_one_engine_module(m) for m in _split_top_blocks(body, "MODULE")
            if (_field(m, "name") or "").startswith("ModuleEngines")]
    mods = [m for m in mods if m["thrust_kn"] > 0]
    if not mods:
        return None
    rocket_modes = [m for m in mods if "IntakeAir" not in m["propellants"]]
    chosen = rocket_modes[0] if rocket_modes else mods[0]
    chosen["air_breathing"] = not rocket_modes  # True only if EVERY mode breathes air (a pure jet)
    return chosen


def _crew_capacity(body: str) -> int:
    return int(_float(body, "CrewCapacity", 0.0))


def _has_module(body: str, module_name: str) -> bool:
    for mod in _split_top_blocks(body, "MODULE"):
        if (_field(mod, "name") or "") == module_name:
            return True
    return False


def _classify(category: str, body: str, engine: dict | None, resources: dict) -> str:
    """Coarse role bucket the design sizer queries on."""
    if engine is not None:
        etype = engine.get("engine_type", "")
        if engine.get("air_breathing"):
            return "jet_engine"          # pure air-breather: never a rocket-stage engine
        if etype == "SolidBooster" or ("SolidFuel" in resources and engine["thrust_kn"] > 0):
            return "solid_booster"
        # An ion / electric or monoprop thruster is a real engine but not a chemical rocket-stage
        # engine; tag it separately so the booster/vacuum pools stay chemical.
        if etype == "Electric" or "ElectricCharge" in engine.get("propellants", set()):
            return "ion_engine"
        if engine.get("propellants") == {"MonoPropellant"}:
            return "monoprop_engine"
        return "liquid_engine"
    # A non-engine part that CARRIES propellant is a tank — REGARDLESS of whether the cfg files it under
    # category=FuelTank or category=Propulsion. Stock files the big Kerbodyne Size3 (S3-3600/7200/14400)
    # and Size4 cylinders under Propulsion, not FuelTank, so keying the tank branch on category alone
    # silently dropped every 3.75 m+ stack tank into "misc" — the sizer then found ZERO tanks at 3.75 m.
    # Key on the propellant content instead (the physics), so every propellant cylinder is a tank.
    _tank_resources = {"LiquidFuel", "Oxidizer", "MonoPropellant", "XenonGas"}
    if category in ("FuelTank", "Propulsion") and any(r in resources for r in _tank_resources):
        nm = (_field(body, "name") or "").lower()
        if "XenonGas" in resources:
            return "xenon_tank"
        if "MonoPropellant" in resources and "LiquidFuel" not in resources:
            return "rcs_tank"
        # A fuel-carrying ADAPTER / slant / spaceplane fuselage is cataloged but is NOT a straight
        # cylindrical stack tank — keep it out of the primary fuel_tank pool the sizer stacks, so a
        # generated stage is always a clean uniform-diameter cylinder. The stock part names carry these
        # markers (adapterMk3-Size2, adapterSize2-Size1Slant, mk3Fuselage*, ...).
        if any(k in nm for k in ("adapter", "slant", "mk2", "mk3", "fuselage")):
            return "adapter_tank"
        # The Size4 EngineAdapter tank is also a transition piece, not a straight cylinder.
        if "engineadapter" in nm:
            return "adapter_tank"
        return "fuel_tank"
    if category == "Pods":
        if _crew_capacity(body) > 0:
            return "pod"
        if _has_module(body, "ModuleCommand"):
            return "probe"
        return "pod"
    if category == "Coupling":
        nm = (_field(body, "name") or "").lower()
        if "radial" in nm or _has_module(body, "ModuleAnchoredDecoupler"):
            return "radial_decoupler"
        if "dock" in nm or _has_module(body, "ModuleDockingNode"):
            return "docking"
        return "decoupler"
    if category == "Thermal":
        return "heatshield" if _has_module(body, "ModuleAblator") or "heatshield" in (_field(body, "name") or "").lower() else "thermal"
    if category in ("Ground", "Wheel"):
        return "landing_leg" if _has_module(body, "ModuleWheelDeployment") or "leg" in (_field(body, "name") or "").lower() else "ground"
    if category == "Aero":
        nm = (_field(body, "name") or "").lower()
        if "fairing" in nm or _has_module(body, "ModuleProceduralFairing"):
            return "fairing"
        if "nose" in nm or "cone" in nm:
            return "nose_cone"
        if _has_module(body, "ModuleControlSurface") or _has_module(body, "ModuleLiftingSurface"):
            return "fin"
        return "aero"
    if category == "Control":
        if _has_module(body, "ModuleReactionWheel"):
            return "reaction_wheel"
        if _has_module(body, "ModuleRCS") or "rcs" in (_field(body, "name") or "").lower():
            return "rcs"
        return "control"
    if category == "Communication":
        return "antenna"
    if category == "Electrical":
        nm = (_field(body, "name") or "").lower()
        if "rtg" in nm or "nuk" in nm or _has_module(body, "ModuleGenerator"):
            return "generator"
        if _has_module(body, "ModuleDeployableSolarPanel") or "solar" in nm:
            return "solar"
        return "battery"
    if category == "Science":
        return "science"
    if category == "Utility":
        if _has_module(body, "ModuleParachute"):
            return "parachute"
        return "utility"
    if category == "Structural":
        return "structural"
    return "misc"


def parse_part_cfg(text: str) -> list[StockPart]:
    """Parse every PART{} node in a .cfg file into StockParts (skips non-rocket categories)."""
    clean = text
    parts: list[StockPart] = []
    for raw_block in _split_top_blocks(text, "PART"):
        body = _strip_comments(raw_block)
        name = _field(body, "name")
        if not name:
            continue
        category = (_field(body, "category") or "").strip()
        if category not in _KEEP_CATEGORIES and category != "none":
            continue
        engine = _engine_module(body)
        resources = _resources(body)
        # If category is "none" keep ONLY if it carries an engine (some engines hide themselves).
        if category == "none" and engine is None:
            continue
        dry = _float(body, "mass", 0.0)
        prop = sum(amount * RESOURCE_DENSITY_T.get(res, 0.0) for res, amount in resources.items())
        wet = dry + prop
        height = _node_height(body) or 1.0
        diameter, stack_size = _bulkhead_info(body, name)
        part_type = _classify(category, body, engine, resources)
        # Store the runtime (dotted) AvailablePart.name so the catalog key matches the live game and the
        # id the craft_writer emits. _classify() above already ran on the raw cfg ``name`` substring, so
        # its marker checks ("adapter"/"mk3"/...) are unaffected by the canonicalization.
        parts.append(StockPart(
            name=live_part_name(name),
            title=_readable_title(raw_block) or live_part_name(name),
            dry_mass_t=round(dry, 5),
            wet_mass_t=round(wet, 5),
            cost=_float(body, "cost", 0.0),
            height_m=round(height, 4),
            thrust_kn_asl=round(engine["thrust_kn"], 3) if engine else 0.0,
            thrust_kn_vac=round(engine["thrust_kn"], 3) if engine else 0.0,
            isp_asl_s=round(engine["isp_asl"], 3) if engine else 0.0,
            isp_vac_s=round(engine["isp_vac"], 3) if engine else 0.0,
            liquid_fuel=resources.get("LiquidFuel", 0.0),
            oxidizer=resources.get("Oxidizer", 0.0),
            solid_fuel=resources.get("SolidFuel", 0.0),
            diameter_m=diameter,
            category=category,
            part_type=part_type,
            crew_capacity=_crew_capacity(body),
            stack_size=stack_size,
        ))
    _ = clean
    return parts


# --------------------------------------------------------------------------------------------------
# Materialize / load the static catalog. The JSON is the COMMITTED artefact so the lab runs offline.
# --------------------------------------------------------------------------------------------------
DATA_DIR = Path(__file__).resolve().parent / "data"
CATALOG_JSON = DATA_DIR / "stock_parts.json"

# Engine ASL thrust in KSP = maxThrust * (Isp_asl / Isp_vac) at sea level. The cfg's maxThrust is the
# VACUUM thrust; thrust at the surface is throttled by the Isp ratio. We back that out so the catalog's
# thrust_kn_asl is the real sea-level thrust the sizer uses for liftoff TWR (matching the curated values).
def _apply_asl_thrust(parts: list[StockPart]) -> list[StockPart]:
    out: list[StockPart] = []
    for p in parts:
        if p.part_type in ("liquid_engine", "solid_booster") and p.isp_vac_s > 0 and p.isp_asl_s > 0:
            asl = p.thrust_kn_vac * (p.isp_asl_s / p.isp_vac_s)
            out.append(replace(p, thrust_kn_asl=round(asl, 3)))
        else:
            out.append(p)
    return out


def parse_gamedata_tree(gamedata_dirs: list[str] | None = None) -> dict[str, StockPart]:
    """Walk the real KSP GameData PART tree and return ``{live_name: StockPart}`` from the cfgs (offline).

    Names are already canonicalized to the live (dotted) form by ``parse_part_cfg``. Parts that collide
    on name keep the FIRST parsed (stock wins over a duplicate cfg). ASL thrust is back-filled."""
    dirs = gamedata_dirs or DEFAULT_GAMEDATA_DIRS
    catalog: dict[str, StockPart] = {}
    for d in dirs:
        base = Path(d)
        if not base.exists():
            continue
        for cfg in base.rglob("*.cfg"):
            try:
                text = cfg.read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                continue
            try:
                for sp in parse_part_cfg(text):
                    if sp.name not in catalog:
                        catalog[sp.name] = sp
            except Exception:
                continue
    return {p.name: p for p in _apply_asl_thrust(list(catalog.values()))}


def materialize_catalog(gamedata_dirs: list[str] | None = None,
                        out_path: Path | None = None) -> dict[str, dict]:
    """OFFLINE build: parse the GameData PART tree and write every rocket-relevant part to the JSON.

    This is the FALLBACK source — accurate, but it materializes whatever cfgs are on disk regardless of
    whether THIS install actually loads them (e.g. DLC folders present but the DLC disabled). Prefer
    ``rebuild_from_live`` when the game is running so the catalog is exactly the loaded part set."""
    out_path = out_path or CATALOG_JSON
    data = {name: _part_to_dict(p) for name, p in parse_gamedata_tree(gamedata_dirs).items()}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    return data


def reconcile_to_live(cfg_catalog: dict[str, StockPart],
                      live_parts: list[dict]) -> tuple[dict[str, StockPart], list[str]]:
    """Make the LIVE ``/part-database`` dump authoritative over the cfg parse, keyed by the live name.

    Returns ``(catalog, dropped)`` where ``catalog`` is ``{live_name: StockPart}`` containing ONLY parts
    the running game actually loaded (so the design can never pick an unbuildable part), and ``dropped``
    lists the cfg-parsed names this install does NOT load (e.g. DLC parts when the DLC is off).

    For every cfg part that IS live we keep the cfg-derived GEOMETRY/cost/role (``height_m``,
    ``diameter_m``, ``stack_size``, ``cost``, ``part_type``, drag/fin areas — ``/part-database`` does not
    expose them) but overwrite the PHYSICS with the game's post-load truth: ``dry_mass_t``, engine
    ``thrust``/``isp``, and the LF/Ox/SolidFuel capacities (so ``wet_mass_t`` is recomputed from live).
    cfg names are matched to live via ``live_part_name`` so the underscore/dot form never causes a miss."""
    by_live = {p.get("name"): p for p in live_parts if p.get("name")}
    catalog: dict[str, StockPart] = {}
    dropped: list[str] = []
    for name, sp in cfg_catalog.items():
        live = by_live.get(name) or by_live.get(live_part_name(name))
        if live is None:
            dropped.append(name)
            continue
        live_name = live.get("name", name)
        res = live.get("resources", {}) or {}
        lf = float(res.get("LiquidFuel", 0.0))
        ox = float(res.get("Oxidizer", 0.0))
        sf = float(res.get("SolidFuel", 0.0))
        dry = float(live.get("dryMassT", sp.dry_mass_t))
        # Recompute wet from the live dry mass + the live resource amounts at the same densities the
        # cfg used (so wet_mass_t stays consistent with the live dry+propellant the game reports).
        prop = sum(float(amt) * RESOURCE_DENSITY_T.get(rname, 0.0) for rname, amt in res.items())
        merged = replace(
            sp,
            name=live_name,
            dry_mass_t=round(dry, 5),
            wet_mass_t=round(dry + prop, 5),
            liquid_fuel=lf, oxidizer=ox, solid_fuel=sf,
        )
        # Engine physics: take live thrust/Isp when the live part exposes a ModuleEngines. The live
        # maxThrust is the part's PRIMARY-mode vacuum thrust; back out ASL thrust by the Isp ratio.
        #
        # MULTIMODE EXCEPTION (the RAPIER). A multimode engine's /part-database entry reports its primary
        # mode, which for the RAPIER is the air-breathing JET (105 kN, an "Isp" of 3200 s that is really a
        # velocity curve, NOT a vacuum Isp). The cfg parser already selected the engine's ROCKET mode
        # (180 kN, 305/275 s) — the rocket-relevant numbers the sizer needs. So when the live "Isp" is a
        # jet-curve artifact (absurdly high for any chemical rocket — Nerv tops out at 800 s) we KEEP the
        # cfg rocket-mode physics rather than overwrite it with the jet mode. validate_parts_live skips the
        # engine-stat comparison for these, so a multimode part is reconciled on mass and is never a
        # spurious MISMATCH.
        if "maxThrustKn" in live and float(live.get("ispVacS", 0.0)) <= 900.0:
            thr_vac = float(live["maxThrustKn"])
            isp_vac = float(live.get("ispVacS", sp.isp_vac_s))
            isp_asl = float(live.get("ispAslS", sp.isp_asl_s))
            thr_asl = thr_vac * (isp_asl / isp_vac) if isp_vac > 0 else thr_vac
            merged = replace(merged, thrust_kn_vac=round(thr_vac, 3), thrust_kn_asl=round(thr_asl, 3),
                             isp_vac_s=round(isp_vac, 3), isp_asl_s=round(isp_asl, 3))
        catalog[live_name] = merged
    return catalog, dropped


def rebuild_from_live(live_parts: list[dict], gamedata_dirs: list[str] | None = None,
                      out_path: Path | None = None) -> tuple[dict[str, dict], list[str]]:
    """LIVE-AUTHORITATIVE build: rebuild ``stock_parts.json`` to be exactly the running game's parts.

    Parses the GameData tree for geometry, reconciles physics to the live ``/part-database`` dump, drops
    every part the game did NOT load, writes the JSON, and returns ``(data, dropped)``."""
    out_path = out_path or CATALOG_JSON
    catalog, dropped = reconcile_to_live(parse_gamedata_tree(gamedata_dirs), live_parts)
    data = {name: _part_to_dict(p) for name, p in catalog.items()}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    return data, dropped


def _part_to_dict(p: StockPart) -> dict:
    return {
        "name": p.name, "title": p.title, "dry_mass_t": p.dry_mass_t, "wet_mass_t": p.wet_mass_t,
        "cost": p.cost, "height_m": p.height_m, "thrust_kn_asl": p.thrust_kn_asl,
        "thrust_kn_vac": p.thrust_kn_vac, "isp_asl_s": p.isp_asl_s, "isp_vac_s": p.isp_vac_s,
        "liquid_fuel": p.liquid_fuel, "oxidizer": p.oxidizer, "solid_fuel": p.solid_fuel,
        "drag_area_m2": p.drag_area_m2, "diameter_m": p.diameter_m, "fin_area_m2": p.fin_area_m2,
        "category": p.category, "part_type": p.part_type, "crew_capacity": p.crew_capacity,
        "stack_size": p.stack_size,
    }


def _part_from_dict(d: dict) -> StockPart:
    return StockPart(
        name=d["name"], title=d.get("title", d["name"]), dry_mass_t=d.get("dry_mass_t", 0.0),
        wet_mass_t=d.get("wet_mass_t", 0.0), cost=d.get("cost", 0.0), height_m=d.get("height_m", 1.0),
        thrust_kn_asl=d.get("thrust_kn_asl", 0.0), thrust_kn_vac=d.get("thrust_kn_vac", 0.0),
        isp_asl_s=d.get("isp_asl_s", 0.0), isp_vac_s=d.get("isp_vac_s", 0.0),
        liquid_fuel=d.get("liquid_fuel", 0.0), oxidizer=d.get("oxidizer", 0.0),
        solid_fuel=d.get("solid_fuel", 0.0), drag_area_m2=d.get("drag_area_m2", 0.0),
        diameter_m=d.get("diameter_m", 1.25), fin_area_m2=d.get("fin_area_m2", 0.0),
        category=d.get("category", ""), part_type=d.get("part_type", ""),
        crew_capacity=d.get("crew_capacity", 0), stack_size=d.get("stack_size", ""),
    )


def load_catalog() -> dict[str, StockPart]:
    """Load the materialized stock catalog from the committed JSON (offline). Returns {} if absent."""
    if not CATALOG_JSON.exists():
        return {}
    try:
        data = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {name: _part_from_dict(d) for name, d in data.items()}


# AERO COEFFICIENTS the .cfg cannot express as the lab's calibrated reference areas. This is the ONLY
# non-cfg field layer, and it touches NO mass / thrust / Isp / capacity / dimension — only two derived
# aero coefficients KSP stores in an incompatible form:
#   * drag_area_m2 — the fully-deployed parachute Cd*A (m^2) the lab's astro.terminal_velocity uses to
#     size chute counts. The cfg stores ``fullyDeployedDrag`` (a unitless drag cube coefficient), not a
#     Cd*A, so the lab's value is calibrated from the live datum "one Mk16 lands ~1.2 t at ~6.5 m/s at
#     Kerbin sea level" — a physics-derived reference area, not a hand-picked geometry number.
#   * fin_area_m2 — the lateral reference area the static-margin (CoP) calc places at a fin's position.
#     The cfg stores ``deflectionLiftCoeff`` (a lift slope), not the reference area the lab's planar
#     CoP model needs; basicFin=1.0 / R8winglet=2.0 are the lab's calibrated relative areas.
# Both are coefficients for the lab's OWN aero models; they are not part geometry and do not override any
# materialized field. (Geometry — diameter/height — is 100% cfg-derived; nothing is patched here.)
_TUNED_OVERRIDES: dict[str, dict] = {
    "parachuteSingle": {"drag_area_m2": 489.0},
    "R8winglet": {"fin_area_m2": 2.0},
    "basicFin": {"fin_area_m2": 1.0},
}


def _build_stock_parts() -> dict[str, StockPart]:
    """The full catalog the lab uses: the MATERIALIZED (live-reconciled) catalog, with ZERO value
    override. Every part stands on equal footing with its authoritative numbers — PHYSICS from the live
    game (or the offline cfg parse), GEOMETRY derived from the .cfg. The only additions are name-only
    alias redirects and the two cfg-inexpressible aero coefficients above.

    Materialization order:
      1. Start from the materialized catalog (``load_catalog()``) — every part keyed on its live
         ``AvailablePart.name`` with the running game's mass/thrust/Isp/capacity and cfg-derived geometry
         (see ``rebuild_from_live``). NOTHING is overridden here; the materialized values ARE the catalog.
      2. Add ALIAS RENAMES (``ALIAS_RENAMES``): for each legacy ``alias -> live`` pair where the live part
         exists, register the SAME live StockPart UNCHANGED under the legacy key (so an importer that looks
         up ``RCSBlock`` finds the live RV-105 block, and its ``.name`` is the LIVE loadable id so the
         .craft references a part the game can load). No data is changed — only an extra dict key points at
         the existing live entry. (The legacy id ``RCSBlock`` is NOT loadable in KSP 1.12 — the real part
         is ``RCSBlock.v2`` — so resolving the alias to the live ``.name`` also FIXES the unloadable-id bug
         the old curated entry shipped.)
      3. PATCH the two cfg-inexpressible aero coefficients (``_TUNED_OVERRIDES``) — drag/fin reference
         areas for the lab's own terminal-velocity / static-margin models. These touch no materialized
         physics or geometry.

    OFFLINE FALLBACK: when the live-reconciled JSON is unavailable, build the catalog from a pure cfg parse
    of the GameData tree (same schema, physics from the bare cfgs) so the lab still runs without the JSON
    or the game. If even that is empty (no JSON, no GameData), return an empty catalog rather than any
    hand-curated stand-in — there is no curated data to fall back to.

    The design sizer queries these pools directly (parts.engines / parts.tanks), so it picks from every
    real stock part with authoritative numbers; there is no hand-tuned tier in front of it."""
    materialized: dict[str, StockPart] = dict(load_catalog())
    if not materialized:
        # OFFLINE FALLBACK: no committed JSON — parse the cfgs on disk (pure cfg geometry + cfg physics).
        materialized = parse_gamedata_tree()
    merged: dict[str, StockPart] = dict(materialized)
    # ALIAS RENAMES — pure name redirects, no data. Register the live part UNCHANGED under the legacy key
    # (its ``.name`` stays the live loadable id, so a craft built from the alias references a part the game
    # can actually load — and an importer that keyed on the legacy name still resolves to the live entry).
    for alias, live_key in ALIAS_RENAMES.items():
        live_part = merged.get(live_key)
        if live_part is not None:
            merged[alias] = live_part
    # PATCH the cfg-inexpressible aero coefficients onto the materialized part (no geometry/physics change).
    for name, fields in _TUNED_OVERRIDES.items():
        if name in merged:
            merged[name] = replace(merged[name], **fields)
    return merged


# The public catalog. ``part()`` and ``stage_masses`` resolve through this, so every importer sees the
# whole materialized stock tree plus the thin alias redirects.
STOCK_PARTS: dict[str, StockPart] = _build_stock_parts()


def part(name: str) -> StockPart:
    try:
        return STOCK_PARTS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown stock part {name!r}") from exc


# --------------------------------------------------------------------------------------------------
# CATALOG QUERIES — the design sizer asks "all engines in this diameter class, sorted by thrust", etc.
# --------------------------------------------------------------------------------------------------
def parts_of_type(part_type: str) -> list[StockPart]:
    """Every catalog part of a coarse role bucket (liquid_engine, fuel_tank, solid_booster, ...)."""
    return [p for p in STOCK_PARTS.values() if p.part_type == part_type]


def engines(diameter_m: float | None = None, atmospheric: bool | None = None,
            include_solid: bool = False, standard_stack_only: bool = True) -> list[StockPart]:
    """All liquid (and optionally solid) engines, optionally filtered to a diameter class and ranked.

    ``atmospheric=True`` sorts by sea-level thrust (booster role), ``atmospheric=False`` sorts by
    vacuum Isp then vacuum thrust (upper-stage role), ``None`` sorts by vacuum thrust.
    ``standard_stack_only`` drops radial/surface-mount engines (Thud, Twitch, Spider — no size token)
    so the inline-stage pool stays clean; the curated anchors in design.py still guarantee coverage."""
    types = {"liquid_engine"} | ({"solid_booster"} if include_solid else set())
    pool = [p for p in STOCK_PARTS.values() if p.part_type in types and p.thrust_kn_vac > 0]
    if standard_stack_only:
        pool = [p for p in pool if p.stack_size]
    if diameter_m is not None:
        pool = [p for p in pool if p.diameter_m <= diameter_m + 1e-6]
    if atmospheric is True:
        pool.sort(key=lambda p: p.thrust_kn_asl)
    elif atmospheric is False:
        pool.sort(key=lambda p: (p.isp_vac_s, p.thrust_kn_vac))
    else:
        pool.sort(key=lambda p: p.thrust_kn_vac)
    return pool


def tanks(diameter_m: float | None = None, propellant: str = "lfo",
          standard_stack_only: bool = True) -> list[StockPart]:
    """All fuel tanks (LiquidFuel+Oxidizer by default), optionally in a diameter class, largest first.

    ``standard_stack_only`` keeps only ROUND-STACK tanks whose bulkhead matched a size0..size4 token, so
    the design pool is not polluted by mk2/mk3 spaceplane fuselages and slant adapter tanks whose
    diameter is only a draw-time 1.25 m fallback. The curated tanks pass (they carry a stack_size)."""
    pool = []
    for p in STOCK_PARTS.values():
        if p.part_type != "fuel_tank":
            continue
        if propellant == "lfo" and not (p.liquid_fuel > 0 and p.oxidizer > 0):
            continue
        if standard_stack_only and not p.stack_size:
            continue
        pool.append(p)
    if diameter_m is not None:
        pool = [p for p in pool if abs(p.diameter_m - diameter_m) < 1e-6]
    pool.sort(key=lambda p: -p.propellant_mass_t)
    return pool


def catalog_summary() -> dict[str, int]:
    """Count parts by coarse role bucket — used by the build/verify step to report what was extracted."""
    out: dict[str, int] = {}
    for p in STOCK_PARTS.values():
        out[p.part_type or "misc"] = out.get(p.part_type or "misc", 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------------------------------
# Mass / estimate helpers (unchanged API).
# --------------------------------------------------------------------------------------------------
def payload_bus_mass(payload_mass_t: float, crewed: bool) -> float:
    command = part("mk1pod.v2" if crewed else "probeCoreOcto.v2").wet_mass_t
    recovery = part("parachuteSingle").wet_mass_t + (part("HeatShield1").wet_mass_t if crewed else 0.0)
    return command + recovery + max(0.0, payload_mass_t)


def stage_masses(stage: StageSpec) -> tuple[float, float, float, float, float]:
    engine = part(stage.engine)
    tank = part(stage.tank)
    n_eng = max(1, stage.engine_count)
    dry = engine.dry_mass_t * n_eng + tank.dry_mass_t * stage.tank_count
    wet = engine.wet_mass_t * n_eng + tank.wet_mass_t * stage.tank_count
    thrust_asl = engine.thrust_kn_asl * n_eng
    isp_asl = engine.isp_asl_s
    isp_vac = engine.isp_vac_s
    if stage.decoupler_above:
        dry += part("Decoupler.1").dry_mass_t
        wet += part("Decoupler.1").wet_mass_t
    return dry, wet, thrust_asl, isp_asl, isp_vac


def estimate_design(design: RocketDesign) -> dict[str, float]:
    payload = payload_bus_mass(design.payload_mass_t, design.crewed)
    stage_wet = [stage_masses(stage)[1] for stage in design.stages]
    total_wet = payload + sum(stage_wet)
    total_cost = sum(part(stage.engine).cost + part(stage.tank).cost * stage.tank_count for stage in design.stages)
    total_cost += part("mk1pod.v2" if design.crewed else "probeCoreOcto.v2").cost
    total_cost += part("parachuteSingle").cost
    total_delta_v = 0.0
    first_twr = 0.0
    for index, stage in enumerate(design.stages):
        dry, wet, thrust_asl, isp_asl, isp_vac = stage_masses(stage)
        mass_above = payload + sum(stage_wet[index + 1 :])
        m0 = mass_above + wet
        m1 = mass_above + dry
        if m1 > 0 and m0 > m1:
            total_delta_v += isp_vac * G0 * log(m0 / m1)
        if index == 0:
            first_twr = thrust_asl / (m0 * G0)
    part_count = sum(2 + stage.tank_count for stage in design.stages) + 2
    if design.crewed:
        part_count += 1
    return {
        "wet_mass_t": round(total_wet, 3),
        "delta_v_mps": round(total_delta_v, 1),
        "launch_twr": round(first_twr, 3),
        "cost": round(total_cost, 1),
        "part_count": float(part_count),
    }


def _main(argv: list[str]) -> int:
    """Build the catalog. ``--from-live`` makes the RUNNING game authoritative (drops parts this install
    does not load, reconciles physics to /part-database); the default offline path parses the cfgs only."""
    import argparse

    ap = argparse.ArgumentParser(description="Materialize the stock-parts catalog.")
    ap.add_argument("--from-live", action="store_true",
                    help="rebuild from the live bridge /part-database (authoritative; drops unloaded parts)")
    ap.add_argument("--bridge", default="http://127.0.0.1:48500", help="KSP bridge base URL")
    args = ap.parse_args(argv)

    if args.from_live:
        from .bridge_client import BridgeClient, BridgeError
        bridge = BridgeClient(base_url=args.bridge, timeout_s=60)
        try:
            db = bridge.part_database()
        except BridgeError as exc:
            print(f"--from-live failed (bridge unreachable / endpoint missing): {exc}")
            print("Start KSP with the bridge up, or run without --from-live to parse cfgs offline.")
            return 1
        live_parts = db.get("parts", []) or []
        written, dropped = rebuild_from_live(live_parts)
        print(f"rebuilt {len(written)} stock parts from LIVE /part-database "
              f"({len(live_parts)} loaded) -> {CATALOG_JSON}")
        if dropped:
            print(f"  dropped {len(dropped)} cfg part(s) NOT loaded by this install:")
            for n in sorted(dropped):
                print(f"    - {n}")
        else:
            print("  dropped 0 cfg parts (every cfg part is loaded by this install)")
    else:
        written = materialize_catalog()
        print(f"materialized {len(written)} stock parts (OFFLINE cfg parse) -> {CATALOG_JSON}")

    # Reload so the summary reflects what was just written.
    global STOCK_PARTS
    STOCK_PARTS = _build_stock_parts()
    for ptype, n in catalog_summary().items():
        print(f"  {ptype:18s} {n}")
    return 0


if __name__ == "__main__":
    import sys as _sys

    raise SystemExit(_main(_sys.argv[1:]))
