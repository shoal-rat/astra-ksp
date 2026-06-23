"""Mars program craft designs — KSP1's Mars analog is DUNA.

Built on the proven Artemis stage convention (decoupler_above on the launch + transfer stages so they
jettison when spent; the final bus stays with the payload). Sized for the interplanetary leg: LKO ->
trans-Duna injection (~1060 m/s) -> Duna capture (a low periapsis aerobrakes in Duna's atmosphere, so
the bus mostly needs the ejection refinement + circularization). See [[project_ksp_mars_duna]] and
tools/mj_to_duna.py (which CALCULATES the window + ejection and lets MechJeb fly the burns).
"""
from __future__ import annotations

from .models import RocketDesign
from .models import StageSpec
from .parts import estimate_design


def build_duna_comsat() -> RocketDesign:
    """A Duna relay comsat: a probe-bus payload on a launcher + interplanetary transfer stage + a
    satellite bus that captures at Duna. Three of these ~120 deg apart in a high Duna orbit give
    global surface coverage (the goal: "Mars internet"). NOTE: for true relay capability the rendered
    craft needs a RELAY antenna (RelayAntenna5/RA-2), not just the default direct antenna — a small
    craft_writer addition tracked in the program notes."""
    comsat = RocketDesign(
        name="AI-Duna-Comsat",
        mission_type="duna_comsat",
        payload_mass_t=0.5,
        crewed=False,
        stages=[
            # Launch to LKO; the transfer stage is bigger than the Mun relay's (trans-Duna ejection
            # ~1060 m/s vs the Mun's ~860); the satellite bus captures at Duna (aerobraked) and
            # circularizes into the relay orbit.
            StageSpec("duna_launch_core", "liquidEngineMainsail.v2", "Rockomax32.BW", 3, True),
            StageSpec("duna_transfer_stage", "engineLargeSkipper", "Rockomax16.BW", 3, True),
            StageSpec("duna_satellite_bus", "liquidEngine3.v2", "fuelTank.long", 2, False),
        ],
        tags=["duna", "mars", "relay", "comsat", "interplanetary"],
        notes=(
            "Duna (Mars) relay comsat. Launch to LKO, MechJeb-flown trans-Duna injection, aerobraked "
            "capture, then circularize into a high relay orbit. Three ~120 deg apart = global coverage."
        ),
        source="duna-program-comsat",
    )
    comsat.estimates = estimate_design(comsat)
    return comsat


def build_route_depot() -> RocketDesign:
    """A dedicated route refueling station: a large fuel reserve + capture engine placed in orbit.
    One in LKO (Earth/Kerbin departure refuel) and one in Duna orbit (Mars arrival refuel) bracket the
    route, Musk-depot style. Ships top up from it (the orbital-refuel model moves the propellant). It
    is heavier than a comsat, so a bigger launch core + transfer stage; the station module carries the
    big reserve and the engine it uses to capture at Duna."""
    depot = RocketDesign(
        name="AI-Route-Depot",
        mission_type="route_depot",
        payload_mass_t=1.0,
        crewed=False,
        stages=[
            StageSpec("depot_launch_core", "liquidEngineMainsail.v2", "Rockomax32.BW", 3, True),
            StageSpec("depot_transfer_stage", "engineLargeSkipper", "Rockomax16.BW", 2, True),
            StageSpec("depot_station_module", "liquidEngine3.v2", "fuelTank.long", 3, False),
        ],
        tags=["duna", "mars", "depot", "refuel-station", "interplanetary"],
        notes=(
            "Route refuel station: big fuel reserve + capture engine. Place one in LKO (departure) and "
            "one in Duna orbit (arrival). Orbital-refuel model handles propellant transfer to ships."
        ),
        source="duna-program-depot",
    )
    depot.estimates = estimate_design(depot)
    return depot
