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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StageSpec":
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

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["stages"] = [stage.to_dict() for stage in self.stages]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RocketDesign":
        payload = dict(data)
        payload["stages"] = [StageSpec.from_dict(s) for s in payload.get("stages", [])]
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

