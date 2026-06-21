from __future__ import annotations

import hashlib
import random
from copy import deepcopy

from .artemis import build_artemis_architecture
from .models import MissionSpec, RocketDesign, ScoreResult, StageSpec
from .parts import estimate_design


class HistoryOptimizer:
    """Small deterministic optimizer with an external-AI seam.

    External systems can replace this class or feed candidate designs into the
    same RocketDesign model. The built-in policy is deliberately simple: it
    varies tank counts and engine choices based on scored failures.
    """

    def __init__(self, mission: MissionSpec, seed: int | None = None):
        digest = hashlib.sha256(mission.goal.encode("utf-8")).hexdigest()
        self.random = random.Random(seed if seed is not None else int(digest[:8], 16))
        self.mission = mission
        self._last_design: RocketDesign | None = None

    def first_design(self) -> RocketDesign:
        if self.mission.mission_type == "artemis_hls_orion_return":
            design = build_artemis_architecture(self.mission).vehicle("orion").design
            design.name = self._name("orion-sls")
        elif self.mission.mission_type == "mun_landing_return":
            design = RocketDesign(
                name=self._name("mun"),
                mission_type=self.mission.mission_type,
                payload_mass_t=self.mission.payload_mass_t,
                crewed=True,
                stages=[
                    StageSpec("launcher", "engineLargeSkipper", "Rockomax16.BW", 4, False),
                    StageSpec("transfer", "liquidEngine2", "fuelTank.long", 3, True),
                    StageSpec("lander_return", "liquidEngine3.v2", "fuelTank", 2, True),
                ],
                tags=["stock", "mun", "generated"],
                notes="Three-stage stack with a high-margin launcher, transfer stage, and Terrier lander-return stage.",
            )
        elif self.mission.mission_type == "kerbin_orbit":
            payload_factor = max(0, int(self.mission.payload_mass_t * 2))
            design = RocketDesign(
                name=self._name("orbit"),
                mission_type=self.mission.mission_type,
                payload_mass_t=self.mission.payload_mass_t,
                crewed=self.mission.crewed,
                stages=[
                    StageSpec("launcher", "liquidEngine2", "fuelTank.long", 3 + payload_factor, False),
                    StageSpec("orbital", "liquidEngine3.v2", "fuelTank", 1 + payload_factor // 2, True),
                ],
                tags=["stock", "kerbin-orbit", "generated"],
                notes="Two-stage stock launch vehicle with a Terrier circularization stage.",
            )
        else:
            design = RocketDesign(
                name=self._name("generic"),
                mission_type=self.mission.mission_type,
                payload_mass_t=self.mission.payload_mass_t,
                crewed=self.mission.crewed,
                stages=[
                    StageSpec("launcher", "liquidEngine2", "fuelTank.long", 4, False),
                    StageSpec("upper", "liquidEngine3.v2", "fuelTank", 2, True),
                ],
                tags=["stock", "generated"],
            )
        design.estimates = estimate_design(design)
        self._last_design = design
        return design

    def next_design(self, last_score: ScoreResult | None) -> RocketDesign:
        if self._last_design is None:
            return self.first_design()
        design = deepcopy(self._last_design)
        design.version += 1
        design.name = self._name(design.mission_type.split("_")[0])
        reason = (last_score.failure_reason if last_score else "").lower()

        if "twr" in reason:
            self._upgrade_first_stage_engine(design)
        elif "delta" in reason or "apoapsis" in reason or not last_score or not last_score.success:
            self._add_propellant(design)
        elif last_score.success and design.estimates.get("delta_v_mps", 0) > self.mission.delta_v_budget_mps * 1.25:
            self._trim_margin(design)
        else:
            self._mutate_small(design)

        design.estimates = estimate_design(design)
        design.notes = f"Iteration {design.version}; adjusted after previous result: {reason or 'no prior result'}."
        self._last_design = design
        return design

    def _name(self, prefix: str) -> str:
        suffix = self.random.randint(1000, 9999)
        return f"AI-{prefix}-{suffix}"

    @staticmethod
    def _upgrade_first_stage_engine(design: RocketDesign) -> None:
        if not design.stages:
            return
        engine = design.stages[0].engine
        if engine == "liquidEngine2":
            design.stages[0].engine = "engineLargeSkipper"
            design.stages[0].tank = "Rockomax16.BW"
            design.stages[0].tank_count = max(2, design.stages[0].tank_count // 2)
        elif engine == "engineLargeSkipper":
            design.stages[0].engine = "liquidEngineMainsail.v2"
            design.stages[0].tank = "Rockomax32.BW"
            design.stages[0].tank_count = max(1, design.stages[0].tank_count // 2)
        else:
            design.stages[0].tank_count = max(1, design.stages[0].tank_count - 1)

    @staticmethod
    def _add_propellant(design: RocketDesign) -> None:
        if not design.stages:
            return
        design.stages[0].tank_count += 1
        if len(design.stages) > 1:
            design.stages[-1].tank_count += 1

    @staticmethod
    def _trim_margin(design: RocketDesign) -> None:
        for stage in reversed(design.stages):
            if stage.tank_count > 1:
                stage.tank_count -= 1
                return

    def _mutate_small(self, design: RocketDesign) -> None:
        stage = self.random.choice(design.stages)
        stage.tank_count = max(1, stage.tank_count + self.random.choice([-1, 1]))
