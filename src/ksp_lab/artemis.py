from __future__ import annotations

from dataclasses import asdict, dataclass

from .models import MissionSpec, RocketDesign, StageSpec
from .parts import estimate_design


@dataclass(slots=True)
class ArtemisVehiclePlan:
    key: str
    launch_order: int
    role: str
    target: str
    success_phase: str
    design: RocketDesign
    notes: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["design"] = self.design.to_dict()
        return data


@dataclass(slots=True)
class ArtemisArchitecturePlan:
    mission: MissionSpec
    vehicles: list[ArtemisVehiclePlan]
    operations: list[str]
    source_notes: list[str]
    limitations: list[str]

    def vehicle(self, key: str) -> ArtemisVehiclePlan:
        for vehicle in self.vehicles:
            if vehicle.key == key:
                return vehicle
        raise KeyError(f"Unknown Artemis vehicle key: {key}")

    def to_dict(self) -> dict:
        return {
            "mission": self.mission.to_dict(),
            "vehicles": [vehicle.to_dict() for vehicle in self.vehicles],
            "operations": list(self.operations),
            "source_notes": list(self.source_notes),
            "limitations": list(self.limitations),
        }


def build_artemis_architecture(mission: MissionSpec) -> ArtemisArchitecturePlan:
    """Build the stock-KSP interpretation of the Artemis split mission."""

    relay = RocketDesign(
        name="AI-Mun-Relay",
        mission_type="artemis_mun_relay",
        payload_mass_t=0.5,
        crewed=False,
        stages=[
            # decoupler_above places an inter-stage decoupler ABOVE the stage (toward the payload).
            # Launch (mainsail) and transfer (skipper) stages each need one so they can be
            # jettisoned when spent and so fuel does not cross-feed down into the heavy booster
            # (otherwise the Mainsail drains the whole stack at sea-level Isp -> suborbital). The
            # satellite bus (terrier) stays attached to the probe, so it has NO decoupler above it.
            StageSpec("sls_cargo_core", "liquidEngineMainsail.v2", "Rockomax32.BW", 3, True),
            StageSpec("relay_transfer_stage", "engineLargeSkipper", "Rockomax16.BW", 3, True),
            StageSpec("relay_satellite_bus", "liquidEngine3.v2", "fuelTank.long", 2, False),
        ],
        tags=["artemis", "relay", "mun-communications", "sls-cargo"],
        notes=(
            "Mun relay satellite launch. Stock-KSP target is a high stable Mun relay orbit, "
            "because a true Mun-stationary orbit is outside the Mun sphere of influence."
        ),
        source="artemis-construction-kit-sls-cargo",
    )
    relay.estimates = estimate_design(relay)

    hls = RocketDesign(
        name="AI-HLS-Starship",
        mission_type="artemis_hls_predeploy",
        payload_mass_t=2.0,
        # Predeployed UNCREWED (probe-controlled) so it is controllable at a headless launch and on
        # descent; crew arrives/transfers via the Orion phase (modeled rendezvous). A crewed-only
        # pod with no probe core has no control source at a headless launch.
        crewed=False,
        stages=[
            # decoupler_above: launch + transfer stages each get an inter-stage decoupler so they
            # jettison when spent; the lander stage (terrier) stays with the command/crew and is the
            # descent+ascent engine. (Same staging convention proven on the relay.)
            StageSpec("hls_launcher", "liquidEngineMainsail.v2", "Rockomax32.BW", 3, True),
            StageSpec("hls_transfer", "engineLargeSkipper", "Rockomax16.BW", 2, True),
            StageSpec("hls_lander_ascent", "liquidEngine3.v2", "fuelTank.long", 3, False),
        ],
        landing_legs=True,
        # Clamp-O-Tron + RCS so the Orion can dock and (literally) transfer crew in Mun orbit.
        docking_port=True,
        tags=["artemis", "hls", "starship-analogue", "hls-project"],
        notes=(
            "Starship HLS analogue. Live KSP runs prefer a verified Starship/Moonship public craft source, "
            "then an Artemis Construction Kit HLS fallback, so the craft uses a real KSP-authored "
            "serialization rather than the unstable minimal hand-written HLS Project craft."
        ),
        source="preferred-starship-hls-craft-source",
    )
    hls.estimates = estimate_design(hls)

    orion = RocketDesign(
        name="AI-Orion-SLS",
        mission_type="artemis_orion_sls_return",
        payload_mass_t=max(mission.payload_mass_t, 0.2),
        # CREWED (carries real kerbals for the crew transfer) AND probe-controlled: render() adds an
        # inline probe core to a crewed craft so a headless launch is still controllable through
        # capture, rendezvous, docking and the Kerbin return. Crew is seated post-launch via the
        # bridge /crew endpoint (a headless launch leaves the pod empty). HEAT SHIELD + parachute too.
        crewed=True,
        heatshield=True,
        # Clamp-O-Tron + RCS so it can dock with the parked HLS and transfer crew in Mun orbit (the
        # literal version of the originally-modeled rendezvous).
        docking_port=True,
        stages=[
            # Same proven decoupler staging as the relay/HLS: launch (SLS core) + transfer (ICPS)
            # jettison; the service-module stage (terrier) stays with the capsule for capture and
            # the trans-Kerbin return burn.
            # Lightened upper stages: a single Mainsail launch stage cannot lift the heavy 2x
            # Rockomax32 transfer + accessories (heat shield, reaction wheels) at TWR>1. Trimming
            # the transfer to Rockomax16 and the service module to 2 tanks keeps ~6 km/s dV (plenty
            # for Mun orbit + capture + Kerbin return) while restoring launch TWR.
            StageSpec("sls_core", "liquidEngineMainsail.v2", "Rockomax32.BW", 3, True),
            StageSpec("icps_transfer", "engineLargeSkipper", "Rockomax16.BW", 2, True),
            StageSpec("orion_service_module", "liquidEngine3.v2", "fuelTank.long", 2, False),
        ],
        tags=["artemis", "sls", "orion", "artemis-construction-kit"],
        notes=(
            "SLS/Orion analogue: Artemis Construction Kit SLS Block 1 / Orion craft, "
            "used as the real-looking crew launch and return vehicle."
        ),
        source="artemis-construction-kit",
    )
    orion.estimates = estimate_design(orion)

    return ArtemisArchitecturePlan(
        mission=mission,
        vehicles=[
            ArtemisVehiclePlan(
                key="relay",
                launch_order=1,
                role="Mun relay satellite",
                target="stable high Mun relay orbit for signal support",
                success_phase="artemis_mun_relay_deployed",
                design=relay,
                notes="Uses a separate SLS cargo-style launch before crew and HLS operations.",
            ),
            ArtemisVehiclePlan(
                key="hls",
                launch_order=2,
                role="predeployed Starship HLS analogue",
                target="stable low Mun orbit, then Mun surface sortie and return to Mun orbit",
                success_phase="artemis_hls_returned_to_mun_orbit",
                design=hls,
                notes="The HLS does not need Kerbin re-entry hardware.",
            ),
            ArtemisVehiclePlan(
                key="orion",
                launch_order=3,
                role="SLS-launched Orion crew return vehicle",
                target="Mun orbit rendezvous-equivalent, then Kerbin return and recovery",
                success_phase="recovered",
                design=orion,
                notes="The Orion analogue keeps heat shield and parachute recovery responsibility.",
            ),
        ],
        operations=[
            "launch relay satellite first and park it in high stable Mun orbit",
            "write HLS and Orion/SLS craft files separately",
            "launch HLS first and park it in low Mun orbit",
            "launch Orion/SLS second and capture in low Mun orbit",
            "switch active control to HLS for descent, landing, and ascent",
            "record crewed surface science before HLS ascent",
            "switch back to Orion for trans-Kerbin injection and recovery",
            "store phase telemetry and score each vehicle separately before scoring the architecture",
        ],
        source_notes=[
            "NASA SLS: https://www.nasa.gov/humans-in-space/space-launch-system/",
            "NASA Orion: https://www.nasa.gov/reference/orion-spacecraft/",
            "NASA HLS: https://www.nasa.gov/reference/human-landing-systems-2/",
            "NASA/SpaceX HLS mission sequence: https://www.nasa.gov/directorates/esdmd/artemis-campaign-development-division/human-landing-system-program/nasa-spacex-illustrate-key-moments-of-artemis-lunar-lander-mission/",
            "NASA Artemis II free-return trajectory reference: https://svs.gsfc.nasa.gov/5610/",
            "Public Moonship craft source used when present: Matt Lowne video description, https://www.youtube.com/watch?v=OJCCDIBmrBI",
            "KSP constraint: a true Mun-stationary orbit requires an altitude outside the Mun sphere of influence, so the stock goal uses a stable high relay orbit instead.",
        ],
        limitations=[
            "Current live control models crew transfer as a rendezvous-equivalent phase until docking automation is added.",
            "The public Moonship HLS source is adapted and tested through live KSP telemetry; it is not treated as a blindly accepted final design.",
        ],
    )


def artemis_phase_mission(parent: MissionSpec, mission_type: str, goal_suffix: str) -> MissionSpec:
    return MissionSpec(
        goal=f"{parent.goal} :: {goal_suffix}",
        mission_type=mission_type,
        target_body="Mun",
        target_orbit_m=parent.target_orbit_m,
        payload_mass_t=parent.payload_mass_t,
        crewed=parent.crewed,
        require_landing=parent.require_landing,
        require_return=parent.require_return,
        reusable=parent.reusable,
        reliability_trials=1,
        delta_v_budget_mps=parent.delta_v_budget_mps,
        phases=list(parent.phases),
    )
