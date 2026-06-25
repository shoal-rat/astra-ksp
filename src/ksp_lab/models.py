from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class MissionSpec:
    goal: str
    mission_type: str
    target_body: str = "Kerbin"
    target_orbit_m: int = 80000
    payload_mass_t: float = 0.0
    crewed: bool = False
    require_landing: bool = False
    require_return: bool = False
    reusable: bool = False
    reliability_trials: int = 1
    delta_v_budget_mps: int = 4500
    phases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class StageSpec:
    role: str
    engine: str
    tank: str
    tank_count: int
    decoupler_above: bool = True
    notes: str = ""
    engine_count: int = 1  # calculated cluster size (N engines) for the TWR the phase demands
    diameter_m: float = 1.25  # body diameter of this stage's tank (2.5 m for a wide/low-CoG lander)
    leg_count: int = 0        # calculated landing-leg count placed on this stage (0 = none)
    fin_count: int = 0        # calculated ascent-stabiliser fin count on this stage (0 = none)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StageSpec":
        return cls(**data)


@dataclass(slots=True)
class RadialBoosterSpec:
    """N symmetric strap-on booster pods clustered radially around the LAUNCH stage's core, each a tank
    stack + its own engine on a RADIAL decoupler (the asparagus / Soyuz / Falcon-Heavy pattern). They
    ignite WITH the core at T0, add liftoff thrust + a chunk of the ascent Δv, then jettison together
    once spent so the core flies on without their dead engine/tank mass. Every count is CALCULATED in
    design.py from the rocket equation + TWR — never guessed."""
    count: int                       # number of symmetric pods (default 4)
    engine: str                      # one sea-level engine per pod
    tank: str                        # the pod's tank type
    tank_count: int                  # whole tanks per pod
    engine_count: int = 1            # engines per pod (usually 1)
    decoupler: str = "radialDecoupler2"  # the radial decoupler each pod hangs on
    diameter_m: float = 1.25         # the pod tank diameter (for the geometry/envelope)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RadialBoosterSpec":
        return cls(**data)


@dataclass(slots=True)
class RocketDesign:
    name: str
    mission_type: str
    payload_mass_t: float
    crewed: bool
    stages: list[StageSpec]
    version: int = 1
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    estimates: dict[str, float] = field(default_factory=dict)
    source: str = "generated"
    landing_legs: bool = False
    heatshield: bool = False
    docking_port: bool = False
    # Calculated stability metrics (written by craft_writer after the stack geometry is known) — the
    # numbers that gate "this craft will not tip on landing / tumble on ascent". CoG height + leg span
    # give the tip-over angle; static margin is CoG-above-CoP in body diameters.
    cog_height_m: float = 0.0
    leg_span_m: float = 0.0
    tipover_angle_deg: float = 0.0
    static_margin_m: float = 0.0
    ascent_stable: bool = True
    landed_stable: bool = True
    # Aerodynamic / air-resistance metrics (shape -> numbers), computed by craft_writer from the
    # assembled stack: drag coefficient, frontal area, ballistic coefficient, the ascent drag-loss Δv,
    # and peak dynamic pressure (max-Q). The aerospace sign-off that the vehicle is streamlined enough.
    drag_cd: float = 0.0
    frontal_area_m2: float = 0.0
    ballistic_coeff_kgm2: float = 0.0
    ascent_drag_loss_mps: float = 0.0
    max_q_kpa: float = 0.0
    # Feasibility GATE — the design pipeline must REJECT, not silently ship, a rocket that cannot fly.
    # feasible=False means a hard constraint failed (liftoff TWR < 1.2, total Δv short of orbit, an
    # unstable ascent, etc.); callers MUST check this before writing the .craft and launching.
    feasible: bool = True
    infeasible_reasons: list[str] = field(default_factory=list)
    # RADIAL BOOSTERS: optional strap-on pods on the launch stage (None = single-core rocket). Set by
    # design.py when the launch stage is too heavy to lift on the core alone; rendered by craft_writer
    # as N symmetric tank+engine stacks on radial decouplers that fire at T0 and jettison when spent.
    radial_boosters: "RadialBoosterSpec | None" = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["stages"] = [stage.to_dict() for stage in self.stages]
        data["radial_boosters"] = self.radial_boosters.to_dict() if self.radial_boosters else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RocketDesign":
        payload = dict(data)
        payload["stages"] = [StageSpec.from_dict(s) for s in payload.get("stages", [])]
        rb = payload.get("radial_boosters")
        payload["radial_boosters"] = RadialBoosterSpec.from_dict(rb) if rb else None
        return cls(**payload)


@dataclass(slots=True)
class TelemetrySummary:
    max_altitude_m: float = 0.0
    apoapsis_m: float = 0.0
    periapsis_m: float = -1.0
    landed: bool = False
    recovered: bool = False
    vessel_destroyed: bool = False
    mission_phase: str = "unknown"
    elapsed_s: float = 0.0
    fuel_fraction_left: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScoreResult:
    score: float
    success: bool
    failure_reason: str = ""
    components: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TrialRecord:
    trial_id: str
    mission: MissionSpec
    design: RocketDesign
    craft_path: str
    telemetry_path: str
    mode: str
    status: str = "pending"
    score: ScoreResult | None = None
    telemetry: TelemetrySummary | None = None
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "mission": self.mission.to_dict(),
            "design": self.design.to_dict(),
            "craft_path": self.craft_path,
            "telemetry_path": self.telemetry_path,
            "mode": self.mode,
            "status": self.status,
            "score": self.score.to_dict() if self.score else None,
            "telemetry": self.telemetry.to_dict() if self.telemetry else None,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

