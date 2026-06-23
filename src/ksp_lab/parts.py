from __future__ import annotations

from dataclasses import dataclass
from math import log

from .models import RocketDesign, StageSpec

G0 = 9.80665


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

    @property
    def propellant_mass_t(self) -> float:
        return max(0.0, self.wet_mass_t - self.dry_mass_t)


STOCK_PARTS: dict[str, StockPart] = {
    "mk1pod.v2": StockPart("mk1pod.v2", "Mk1 Command Pod", 0.84, 0.84, 600, 1.2),
    "probeCoreOcto.v2": StockPart("probeCoreOcto.v2", "Probodobodyne OKTO", 0.1, 0.1, 450, 0.25),
    "parachuteSingle": StockPart("parachuteSingle", "Mk16 Parachute", 0.1, 0.1, 422, 0.35, drag_area_m2=489.0),
    "dockingPort2": StockPart("dockingPort2", "Clamp-O-Tron Docking Port", 0.05, 0.05, 280, 0.28),
    "crewCabin": StockPart("crewCabin", "Mk1 Crew Cabin", 1.0, 1.0, 600, 1.275),
    "RCSBlock": StockPart("RCSBlock", "RV-105 RCS Thruster Block", 0.04, 0.04, 620, 0.2),
    "rcsTankRadialLong": StockPart("rcsTankRadialLong", "FL-R25 RCS Fuel Tank", 0.1, 0.4, 330, 0.9),
    "HeatShield1": StockPart("HeatShield1", "Heat Shield 1.25m", 0.3, 0.3, 300, 0.2),
    "Decoupler.1": StockPart("Decoupler.1", "TD-12 Decoupler", 0.05, 0.05, 400, 0.25),
    "ServiceBay.125.v2": StockPart("ServiceBay.125.v2", "Service Bay 1.25m", 0.1, 0.1, 500, 0.5),
    "fuelTankSmallFlat": StockPart(
        "fuelTankSmallFlat", "FL-T100 Fuel Tank", 0.0625, 0.5625, 150, 0.55, liquid_fuel=45, oxidizer=55
    ),
    "fuelTankSmall": StockPart(
        "fuelTankSmall", "FL-T200 Fuel Tank", 0.125, 1.125, 275, 1.1, liquid_fuel=90, oxidizer=110
    ),
    "fuelTank": StockPart(
        "fuelTank", "FL-T400 Fuel Tank", 0.25, 2.25, 500, 1.85, liquid_fuel=180, oxidizer=220
    ),
    "fuelTank.long": StockPart(
        "fuelTank.long", "FL-T800 Fuel Tank", 0.5, 4.5, 800, 3.7, liquid_fuel=360, oxidizer=440
    ),
    "Rockomax16.BW": StockPart(
        "Rockomax16.BW", "Rockomax X200-16 Fuel Tank", 1.0, 9.0, 1550, 3.75, liquid_fuel=720, oxidizer=880
    , diameter_m=2.5),
    "Rockomax32.BW": StockPart(
        "Rockomax32.BW", "Rockomax X200-32 Fuel Tank", 2.0, 18.0, 3000, 7.5, liquid_fuel=1440, oxidizer=1760
    , diameter_m=2.5),
    "liquidEngine": StockPart(
        "liquidEngine", "LV-T30 Reliant", 1.25, 1.25, 1100, 1.5, 205, 240, 265, 310
    ),
    "liquidEngine2": StockPart(
        "liquidEngine2", "LV-T45 Swivel", 1.5, 1.5, 1200, 1.5, 167.97, 215, 250, 320
    ),
    "liquidEngine3.v2": StockPart(
        "liquidEngine3.v2", "LV-909 Terrier", 0.5, 0.5, 390, 0.85, 14.78, 60, 85, 345
    ),
    "engineLargeSkipper": StockPart(
        "engineLargeSkipper", "RE-I5 Skipper", 3.0, 3.0, 5300, 2.2, 568.75, 650, 280, 320
    , diameter_m=2.5),
    "liquidEngineMainsail.v2": StockPart(
        "liquidEngineMainsail.v2", "RE-M3 Mainsail", 6.0, 6.0, 13000, 2.8, 1379.03, 1500, 285, 310
    , diameter_m=2.5),
    # Avionics / payload accessories (no propellant; mass/cost for the budget only).
    "longAntenna": StockPart("longAntenna", "Communotron 16 (direct antenna)", 0.005, 0.005, 300, 0.3),
    "RelayAntenna5": StockPart("RelayAntenna5", "RA-2 Relay (relay antenna)", 0.015, 0.015, 600, 0.3),
    # RA-100: the strongest stock RELAY antenna (100 Gm). Combined with a level-3 DSN (250 Gm) it
    # reaches Kerbin from anywhere in the system (~158 Gm) — fixes the no-signal-at-Duna problem; the
    # weak RA-2 (2 Gm -> ~22 Gm combined) drops out when Duna is past ~22 Gm. Relay antennas also let
    # the comsat constellation extend the network for other craft.
    "RelayAntenna100": StockPart("RelayAntenna100", "RA-100 Relay Antenna", 0.65, 0.65, 1000, 0.6),
    "solarPanels5": StockPart("solarPanels5", "SP-W 3x2 Photovoltaic Panels", 0.0175, 0.0175, 380, 0.3),
    "batteryBankMini": StockPart("batteryBankMini", "Z-200 Rechargeable Battery Bank", 0.01, 0.01, 360, 0.3),
    "R8winglet": StockPart("R8winglet", "AV-R8 Winglet (active control surface)", 0.08, 0.08, 640, 0.5, fin_area_m2=2.0),
    "basicFin": StockPart("basicFin", "Basic Fin (passive aero stabiliser)", 0.01, 0.01, 25, 0.5, fin_area_m2=1.0),
    "asasmodule1-2": StockPart("asasmodule1-2", "Advanced Reaction Wheel Module (attitude authority)", 0.05, 0.05, 2100, 0.3),
    "landingLeg1": StockPart("landingLeg1", "LT-2 Landing Strut", 0.1, 0.1, 440, 0.5),
    "noseCone": StockPart("noseCone", "Aerodynamic Nose Cone (streamlining)", 0.03, 0.03, 240, 0.7),
    # Conical ADAPTER bridging a 2.5 m lower stage to a 1.25 m upper stage so there is no exposed flat
    # shoulder at the diameter step (the aerodynamic + structural fix the uniform-diameter rule wants).
    # diameter_m is the WIDE (lower) end; the cone tapers to 1.25 m on top.
    "adapterSize2-Size1": StockPart("adapterSize2-Size1", "Rockomax Brand Adapter (2.5 -> 1.25 m)",
                                    0.8, 0.8, 800, 1.6, diameter_m=2.5),
}


def part(name: str) -> StockPart:
    try:
        return STOCK_PARTS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown stock part {name!r}") from exc


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
