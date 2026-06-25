"""Stock-part catalog: the hand-validated core PLUS a comprehensive catalog materialized from the
ACTUAL KSP GameData .cfg files.

WHY TWO LAYERS. The sizer and the .craft renderer were built against a SMALL hand-list of engines/
tanks whose masses, heights and Isp were checked against the live game one-by-one (the "don't touch
validated masses" rule). That curated set (``CURATED_PARTS``) stays authoritative — it is the back-
compat surface every other module imports through ``part()`` / ``STOCK_PARTS`` / ``stage_masses``.

On TOP of it we now MATERIALIZE the whole stock parts tree. ``materialize_catalog()`` walks the real
GameData folders ONCE (offline, a build step — never at import time) and writes every rocket-relevant
PART{} node into a committed ``data/stock_parts.json``. At import we load that JSON so the lab knows
the entire stock catalog (every liquid engine, SRB, tank, decoupler, adapter, nose cone, fairing,
pod, RCS, reaction wheel, heat shield, chute, leg, science part) WITHOUT the game installed. Where a
materialized part shares a curated part's identity the CURATED value wins, so the validated numbers
are never overwritten — the JSON only ADDS the parts the hand-list never covered.

The design sizer then queries the full catalog ("every atmospheric engine in diameter class 2.5 m,
sorted by thrust") instead of the old five-engine pool.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from math import log
from pathlib import Path

from .models import RocketDesign, StageSpec

G0 = 9.80665

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

# bulkheadProfiles size class -> stack diameter (m). srf/mk2/mk3 are surface-attach / spaceplane
# profiles with no round stack diameter; they fall back to a nominal 1.25 m for drawing only.
BULKHEAD_DIAMETER_M = {
    "size0": 0.625,
    "size1": 1.25,
    "size2": 2.5,
    "size3": 3.75,
    "size4": 5.0,
}


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
# CURATED, hand-validated parts — the authoritative back-compat surface. Masses/heights/Isp here were
# each checked against the live game; the materialized catalog NEVER overwrites these (see
# _build_stock_parts). Adding a part to the JSON that shares one of these names keeps the curated value.
# --------------------------------------------------------------------------------------------------
CURATED_PARTS: dict[str, StockPart] = {
    "mk1pod.v2": StockPart("mk1pod.v2", "Mk1 Command Pod", 0.84, 0.84, 600, 1.05, crew_capacity=1, part_type="pod", category="Pods"),
    "probeCoreOcto.v2": StockPart("probeCoreOcto.v2", "Probodobodyne OKTO", 0.1, 0.1, 450, 0.374, part_type="probe", category="Pods"),
    "parachuteSingle": StockPart("parachuteSingle", "Mk16 Parachute", 0.1, 0.1, 422, 0.35, drag_area_m2=489.0, part_type="parachute", category="Utility"),
    "dockingPort2": StockPart("dockingPort2", "Clamp-O-Tron Docking Port", 0.05, 0.05, 280, 0.28, part_type="docking", category="Coupling"),
    # NOTE: mass 1.0 t + 1.25 m identify this as the Mk1 Crew Cabin (real height 1.875 m, node-to-node).
    # The part-name string "crewCabin" is KSP's internal id for the HITCHHIKER (2.5 t / 1.97 m) — a latent
    # name/identity mismatch (not a height issue); left as-is per the "don't touch validated masses" rule.
    "crewCabin": StockPart("crewCabin", "Mk1 Crew Cabin", 1.0, 1.0, 600, 1.875, crew_capacity=2, part_type="crew_cabin", category="Pods"),
    "RCSBlock": StockPart("RCSBlock", "RV-105 RCS Thruster Block", 0.04, 0.04, 620, 0.2, part_type="rcs", category="Control"),
    "rcsTankRadialLong": StockPart("rcsTankRadialLong", "FL-R25 RCS Fuel Tank", 0.1, 0.4, 330, 0.9, part_type="rcs_tank", category="FuelTank"),
    "HeatShield1": StockPart("HeatShield1", "Heat Shield 1.25m", 0.3, 0.3, 300, 0.2, part_type="heatshield", category="Thermal"),
    "Decoupler.1": StockPart("Decoupler.1", "TD-12 Decoupler", 0.05, 0.05, 400, 0.1, part_type="decoupler", category="Coupling"),
    # RADIAL decoupler: srfAttaches a strap-on booster pod to the side of the core and blows it off
    # when the booster is spent (the asparagus/radial-booster jettison). Stock TT-70 (radialDecoupler2):
    # 0.05 t, 700 funds, ~0.6 m mounting plate. Mass/cost from the stock 1.12 RadialDecoupler2.cfg;
    # diameter is the small mounting plate (the booster rides on it, the plate does not set the hull).
    "radialDecoupler2": StockPart("radialDecoupler2", "TT-70 Radial Decoupler", 0.05, 0.05, 700, 0.6, diameter_m=0.6, part_type="radial_decoupler", category="Coupling"),
    "ServiceBay.125.v2": StockPart("ServiceBay.125.v2", "Service Bay 1.25m", 0.1, 0.1, 500, 0.6, part_type="structural", category="Structural"),
    "fuelTankSmallFlat": StockPart(
        "fuelTankSmallFlat", "FL-T100 Fuel Tank", 0.0625, 0.5625, 150, 0.625, liquid_fuel=45, oxidizer=55, part_type="fuel_tank", category="FuelTank"
    ),
    "fuelTankSmall": StockPart(
        "fuelTankSmall", "FL-T200 Fuel Tank", 0.125, 1.125, 275, 1.11, liquid_fuel=90, oxidizer=110, part_type="fuel_tank", category="FuelTank"
    ),
    "fuelTank": StockPart(
        "fuelTank", "FL-T400 Fuel Tank", 0.25, 2.25, 500, 1.894, liquid_fuel=180, oxidizer=220, part_type="fuel_tank", category="FuelTank"
    ),
    "fuelTank.long": StockPart(
        "fuelTank.long", "FL-T800 Fuel Tank", 0.5, 4.5, 800, 3.762, liquid_fuel=360, oxidizer=440, part_type="fuel_tank", category="FuelTank"
    ),
    # 2.5 m Rockomax tank heights are node-to-node (node_stack_top.y - node_stack_bottom.y) from the stock
    # 1.12 cfgs: X200-16 = 0.92*2 = 1.84 m, X200-32 = 1.86*2 = 3.72 m. (Earlier values 3.75 / 7.5 were each
    # one size too large -- the X200-32 and Jumbo-64 heights -- which inflated the design-chart fineness.)
    "Rockomax16.BW": StockPart(
        "Rockomax16.BW", "Rockomax X200-16 Fuel Tank", 1.0, 9.0, 1550, 1.84, liquid_fuel=720, oxidizer=880, diameter_m=2.5, part_type="fuel_tank", category="FuelTank"
    ),
    "Rockomax32.BW": StockPart(
        "Rockomax32.BW", "Rockomax X200-32 Fuel Tank", 2.0, 18.0, 3000, 3.72, liquid_fuel=1440, oxidizer=1760, diameter_m=2.5, part_type="fuel_tank", category="FuelTank"
    ),
    "Size3SmallTank": StockPart(
        "Size3SmallTank", "Kerbodyne S3-3600 Tank", 2.25, 20.25, 3250, 1.927, liquid_fuel=1620, oxidizer=1980, diameter_m=3.75, part_type="fuel_tank", category="FuelTank"
    ),
    "Size3MediumTank": StockPart(
        "Size3MediumTank", "Kerbodyne S3-7200 Tank", 4.5, 40.5, 6500, 3.868, liquid_fuel=3240, oxidizer=3960, diameter_m=3.75, part_type="fuel_tank", category="FuelTank"
    ),
    "Size3LargeTank": StockPart(
        "Size3LargeTank", "Kerbodyne S3-14400 Tank", 9.0, 81.0, 13000, 7.48, liquid_fuel=6480, oxidizer=7920, diameter_m=3.75, part_type="fuel_tank", category="FuelTank"
    ),
    "liquidEngine": StockPart(
        "liquidEngine", "LV-T30 Reliant", 1.25, 1.25, 1100, 1.63, 205, 240, 265, 310, part_type="liquid_engine", category="Engine"
    ),
    "liquidEngine2": StockPart(
        "liquidEngine2", "LV-T45 Swivel", 1.5, 1.5, 1200, 1.63, 167.97, 215, 250, 320, part_type="liquid_engine", category="Engine"
    ),
    "liquidEngine3.v2": StockPart(
        "liquidEngine3.v2", "LV-909 Terrier", 0.5, 0.5, 390, 0.83, 14.78, 60, 85, 345, part_type="liquid_engine", category="Engine"
    ),
    "engineLargeSkipper": StockPart(
        "engineLargeSkipper", "RE-I5 Skipper", 3.0, 3.0, 5300, 2.375, 568.75, 650, 280, 320, diameter_m=2.5, part_type="liquid_engine", category="Engine"
    ),
    "liquidEngineMainsail.v2": StockPart(
        "liquidEngineMainsail.v2", "RE-M3 Mainsail", 6.0, 6.0, 13000, 2.97, 1379.03, 1500, 285, 310, diameter_m=2.5, part_type="liquid_engine", category="Engine"
    ),
    "Size3AdvancedEngine": StockPart(
        "Size3AdvancedEngine", 'Kerbodyne KR-2L+ "Rhino"', 9.0, 9.0, 25000, 4.025, 1205.88, 2000, 205, 340, diameter_m=3.75, part_type="liquid_engine", category="Engine"
    ),
    # Avionics / payload accessories (no propellant; mass/cost for the budget only).
    "longAntenna": StockPart("longAntenna", "Communotron 16 (direct antenna)", 0.005, 0.005, 300, 0.3, part_type="antenna", category="Communication"),
    "RelayAntenna5": StockPart("RelayAntenna5", "RA-2 Relay (relay antenna)", 0.015, 0.015, 600, 0.3, part_type="antenna", category="Communication"),
    # RA-100: the strongest stock RELAY antenna (100 Gm). Combined with a level-3 DSN (250 Gm) it
    # reaches Kerbin from anywhere in the system (~158 Gm) — fixes the no-signal-at-Duna problem; the
    # weak RA-2 (2 Gm -> ~22 Gm combined) drops out when Duna is past ~22 Gm. Relay antennas also let
    # the comsat constellation extend the network for other craft.
    "RelayAntenna100": StockPart("RelayAntenna100", "RA-100 Relay Antenna", 0.65, 0.65, 1000, 0.6, part_type="antenna", category="Communication"),
    "solarPanels5": StockPart("solarPanels5", "SP-W 3x2 Photovoltaic Panels", 0.0175, 0.0175, 380, 0.3, part_type="solar", category="Electrical"),
    "batteryBankMini": StockPart("batteryBankMini", "Z-200 Rechargeable Battery Bank", 0.01, 0.01, 360, 0.3, part_type="battery", category="Electrical"),
    "batteryBank": StockPart("batteryBank", "Z-1k Rechargeable Battery Bank", 0.05, 0.05, 880, 0.4, part_type="battery", category="Electrical"),
    # PB-NUK RTG: continuous ~0.75 EC/s, SUN-INDEPENDENT power. A comsat spends part of every orbit in
    # shadow; with solar alone the battery drains and the probe loses control (ControlState.none) — which
    # killed the keo circularisation burns. The RTG keeps it controllable through eclipse.
    "rtg": StockPart("rtg", "PB-NUK Radioisotope Thermoelectric Generator", 0.08, 0.08, 23300, 0.5, part_type="generator", category="Electrical"),
    "R8winglet": StockPart("R8winglet", "AV-R8 Winglet (active control surface)", 0.08, 0.08, 640, 0.5, fin_area_m2=2.0, part_type="fin", category="Aero"),
    "basicFin": StockPart("basicFin", "Basic Fin (passive aero stabiliser)", 0.01, 0.01, 25, 0.5, fin_area_m2=1.0, part_type="fin", category="Aero"),
    # NOTE: these numbers (0.05 t, 1.25 m, 0.3 m) model a SMALL inline reaction wheel and are left as-is.
    # KSP's part-name "asasmodule1-2" is actually the LARGE 2.5 m Advanced Reaction Wheel (0.2 t, 0.5 m) —
    # a latent name/identity mismatch. Correcting the diameter to 2.5 m would break the slender bus's
    # monotonic-taper gate and contradict the validated mass, so the fix belongs with the part-name audit.
    "asasmodule1-2": StockPart("asasmodule1-2", "Advanced Reaction Wheel Module (attitude authority)", 0.05, 0.05, 2100, 0.3, part_type="reaction_wheel", category="Control"),
    "landingLeg1": StockPart("landingLeg1", "LT-2 Landing Strut", 0.1, 0.1, 440, 0.5, part_type="landing_leg", category="Ground"),
    "noseCone": StockPart("noseCone", "Aerodynamic Nose Cone (streamlining)", 0.03, 0.03, 240, 0.7, part_type="nose_cone", category="Aero"),
    # Conical ADAPTER bridging a 2.5 m lower stage to a 1.25 m upper stage so there is no exposed flat
    # shoulder at the diameter step (the aerodynamic + structural fix the uniform-diameter rule wants).
    # diameter_m is the WIDE (lower) end; the cone tapers to 1.25 m on top.
    "adapterSize2-Size1": StockPart("adapterSize2-Size1", "Rockomax Brand Adapter (2.5 -> 1.25 m)",
                                    0.8, 0.8, 800, 2.5, diameter_m=2.5, part_type="adapter", category="Coupling"),
    "Size3To2Adapter_v2": StockPart(
        "Size3To2Adapter_v2", "Kerbodyne ADTP-2-3 (3.75 -> 2.5 m)",
        # Used as a structural aero adapter in generated launch stacks. The stock part can carry
        # fuel, but craft_writer strips those resources so hidden adapter propellant is not counted
        # outside the calculated stage budgets.
        1.875, 1.875, 1623, 2.25, diameter_m=3.75, part_type="adapter", category="Coupling"
    ),
    # PAYLOAD FAIRING bases. The base node-attaches below the payload; its ModuleProceduralFairing shell
    # (a list of XSECTIONS) wraps everything above it into an ogive nose and is jettisoned in space. This
    # is how a real satellite rides: enclosed + protected through max-Q, then the shroud splits away
    # before the dish/solar deploy. fairingSize1 = 1.25 m base (r 0.625), fairingSize2 = 2.5 m base (r 1.25).
    "fairingSize1": StockPart("fairingSize1", "AE-FF1 Airstream Fairing (1.25 m)", 0.075, 0.075, 200, 0.42, diameter_m=1.25, part_type="fairing", category="Aero"),
    "fairingSize2": StockPart("fairingSize2", "AE-FF2 Airstream Fairing (2.5 m)", 0.15, 0.15, 750, 0.42, diameter_m=2.5, part_type="fairing", category="Aero"),
}

# Back-compat alias retained for any importer that referenced the old name.
STOCK_PARTS_CURATED = CURATED_PARTS


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
    """node_stack_top.y - node_stack_bottom.y (×rescaleFactor) = the part's stacked height in metres."""
    top = _field(body, "node_stack_top")
    bot = _field(body, "node_stack_bottom")
    if not top or not bot:
        return 0.0
    try:
        ty = float(top.split(",")[1])
        by = float(bot.split(",")[1])
    except (ValueError, IndexError):
        return 0.0
    rescale = _float(body, "rescaleFactor", 1.0)
    return abs(ty - by) * (rescale if rescale > 0 else 1.0)


def _bulkhead_info(body: str) -> tuple[float, str]:
    """Return (stack diameter m, size token). The token is the matched size0..size4 string, or "" when
    the part has only surface/spaceplane (srf/mk2/mk3) profiles — in which case the diameter is a
    nominal 1.25 m fallback for DRAWING only and the part is NOT a standard round-stack part."""
    bp = _field(body, "bulkheadProfiles")
    if not bp:
        return 1.25, ""
    for tok in (t.strip() for t in bp.replace(",", " ").split()):
        if tok in BULKHEAD_DIAMETER_M:
            return BULKHEAD_DIAMETER_M[tok], tok
    return 1.25, ""


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
    if category == "FuelTank":
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
        diameter, stack_size = _bulkhead_info(body)
        part_type = _classify(category, body, engine, resources)
        parts.append(StockPart(
            name=name,
            title=_readable_title(raw_block) or name,
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


def materialize_catalog(gamedata_dirs: list[str] | None = None,
                        out_path: Path | None = None) -> dict[str, dict]:
    """Walk the real KSP GameData PART tree ONCE and write every rocket-relevant part to a JSON file.

    This is a BUILD step (run by hand / a test), never called at import. Returns the catalog dict it
    wrote. Parts that collide on ``name`` keep the FIRST parsed (stock wins over any duplicate cfg)."""
    dirs = gamedata_dirs or DEFAULT_GAMEDATA_DIRS
    out_path = out_path or CATALOG_JSON
    catalog: dict[str, StockPart] = {}
    skipped = 0
    for d in dirs:
        base = Path(d)
        if not base.exists():
            continue
        for cfg in base.rglob("*.cfg"):
            try:
                text = cfg.read_text(encoding="utf-8-sig", errors="ignore")
            except OSError:
                skipped += 1
                continue
            try:
                for sp in parse_part_cfg(text):
                    if sp.name not in catalog:
                        catalog[sp.name] = sp
            except Exception:
                skipped += 1
                continue
    parts = _apply_asl_thrust(list(catalog.values()))
    data = {p.name: _part_to_dict(p) for p in parts}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    return data


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


def _build_stock_parts() -> dict[str, StockPart]:
    """The full catalog the lab uses: the materialized JSON OVERLAID with the curated parts (curated
    always wins, so the hand-validated masses/heights are authoritative). The JSON only ADDS the parts
    the curated list never covered."""
    merged: dict[str, StockPart] = dict(load_catalog())
    merged.update(CURATED_PARTS)         # curated overrides any same-named materialized part
    return merged


# The public catalog. ``part()`` and ``stage_masses`` resolve through this, so every importer sees the
# curated parts PLUS the whole materialized stock tree.
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


if __name__ == "__main__":
    # Build step: python -m ksp_lab.parts  ->  materialize the catalog and print what was extracted.
    written = materialize_catalog()
    print(f"materialized {len(written)} stock parts -> {CATALOG_JSON}")
    for ptype, n in catalog_summary().items():
        print(f"  {ptype:18s} {n}")
