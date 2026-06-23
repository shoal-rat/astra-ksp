"""Calculated design optimizer.

The old policy hand-picked stage specs (e.g. "4x Rockomax16") and then mutated them by string-matching
the failure reason ("twr" -> swap engine, "delta" -> add a tank). Nothing was validated against Δv or
TWR. This rewrite is calculated end to end:

  * first_design() translates the MissionSpec into a CALCULATED requirement — the ordered propulsive
    phases (with their physics Δv from `budget.mission_budget`), the crew/payload/heat-shield/docking
    needs, and (for an atmospheric landing) a parachute LandingSite sized from the body's live density —
    then calls `design.design_ship`, which sizes every tank by the rocket equation and every engine
    cluster by the TWR the phase demands. No part count is hand-picked.

  * next_design() does NOT swap parts by keyword. It adjusts the CALCULATED requirement — raises the
    Δv reserve on the phase implicated by the failure, or raises a launch/landing phase's min-TWR — and
    re-runs `design.design_ship`. The new part counts fall out of the new physics.

Public interface is unchanged: HistoryOptimizer(mission, seed=None) with .first_design() and
.next_design(last_score) returning a RocketDesign whose `estimates` carries the `delta_v_mps` /
`launch_twr` keys the runner and tests read.
"""
from __future__ import annotations

import hashlib
import random
from copy import deepcopy

from . import budget
from .design import LandingSite, Phase, ShipRequirements, design_ship
from .models import MissionSpec, RocketDesign, ScoreResult
from .parts import estimate_design


# How much a single failed trial raises a phase's Δv reserve or a launch/landing phase's TWR floor on
# the next requirement. These are step sizes for a calculated search over the requirement, not magic
# part choices: every concrete part count still comes from design_ship's rocket-equation sizing.
_DV_RESERVE_STEP = 0.08    # +8% Δv on the implicated phase per delta-v/apoapsis failure
_TWR_STEP = 0.25           # +0.25 to the launch/landing min-TWR per TWR failure


class HistoryOptimizer:
    """Calculated optimizer: builds designs from physics requirements, refines the requirement on
    failure. Keeps the deterministic-with-seed surface the runner and tests rely on."""

    def __init__(self, mission: MissionSpec, seed: int | None = None):
        digest = hashlib.sha256(mission.goal.encode("utf-8")).hexdigest()
        self.random = random.Random(seed if seed is not None else int(digest[:8], 16))
        self.mission = mission
        self._budget = budget.mission_budget(mission)
        # Per-phase requirement adjustments the search accumulates across trials (calculated knobs,
        # not part edits): a Δv reserve multiplier and a min-TWR floor override per phase name.
        self._dv_reserve: dict[str, float] = {}
        self._twr_floor: dict[str, float] = {}
        self._last_design: RocketDesign | None = None

    # ----- public API (unchanged) -----------------------------------------------------------------

    def first_design(self) -> RocketDesign:
        design = self._build()
        self._last_design = design
        return design

    def next_design(self, last_score: ScoreResult | None) -> RocketDesign:
        if self._last_design is None:
            return self.first_design()
        prev_dv = self._last_design.estimates.get("delta_v_mps", 0.0)
        reason = (last_score.failure_reason if last_score else "").lower()
        self._adjust_requirement(reason, last_score)
        design = self._build()
        # On a Δv shortfall the rebuilt design must actually carry MORE Δv. Because tank counts are
        # integers, a small reserve bump can round to the same design; keep escalating the launch
        # reserve (a calculated requirement change, still no hand-edited parts) until design_ship
        # yields a strictly larger total Δv. Bounded so it always terminates.
        wants_more_dv = ("delta" in reason or "apoapsis" in reason
                         or (last_score is not None and not last_score.success and "twr" not in reason))
        if wants_more_dv:
            launch = self._first_phase_name()
            for _ in range(40):
                if design.estimates.get("delta_v_mps", 0.0) > prev_dv:
                    break
                self._dv_reserve[launch] = self._dv_reserve.get(launch, 0.0) + _DV_RESERVE_STEP
                design = self._build()
        design.version = self._last_design.version + 1
        design.notes = (
            f"Iteration {design.version}; requirement adjusted after previous result: "
            f"{reason or 'no prior result'}.\n" + design.notes
        )
        self._last_design = design
        return design

    # ----- requirement construction (calculated) --------------------------------------------------

    def _requirements(self) -> ShipRequirements:
        """Translate the MissionSpec + the accumulated calculated adjustments into a ShipRequirements."""
        m = self.mission
        crew = self._crew_count()
        phases = [self._design_phase(pb) for pb in self._budget.phases]
        # A heat shield is needed whenever crew come home through Kerbin's atmosphere; docking when the
        # profile rendezvous in orbit (a crewed return architecture). Both are mission facts, not tuning.
        needs_heatshield = m.require_return and crew > 0
        needs_docking = m.require_return and m.require_landing and crew > 0
        landing = self._landing_site()
        return ShipRequirements(
            name=self._name(m.mission_type.split("_")[0]),
            mission_type=m.mission_type,
            crew=crew,
            payload_t=m.payload_mass_t,
            phases=phases,
            landing=landing,
            needs_heatshield=needs_heatshield,
            needs_docking=needs_docking,
        )

    def _design_phase(self, pb: budget.PhaseBudget) -> Phase:
        """One budget phase -> a design.Phase, applying the accumulated Δv reserve and TWR floor."""
        dv = pb.dv_mps * (1.0 + self._dv_reserve.get(pb.name, 0.0))
        min_twr = max(pb.min_twr, self._twr_floor.get(pb.name, 0.0))
        return Phase(name=pb.name, dv_mps=dv, twr_body_g=pb.twr_body_g, min_twr=min_twr)

    def _landing_site(self) -> LandingSite | None:
        """A parachute LandingSite only when the mission lands on a body that HAS atmosphere — then the
        chute count is sized from that body's live surface density. An airless target (Mun) lands
        propulsively (hoverslam), so there is no parachute site and design_ship adds none."""
        if not self.mission.require_landing:
            return None
        from . import bodies
        target = bodies.body(self.mission.target_body)
        if target.atmosphere_top_m <= 0 or target.surface_rho <= 0:
            return None
        return LandingSite(body_g=target.surface_g, surface_rho=target.surface_rho)

    def _crew_count(self) -> int:
        if not self.mission.crewed:
            return 0
        # One pilot is the floor for a crewed profile; payload mass does not imply extra seats here.
        return 1

    def _build(self) -> RocketDesign:
        design = design_ship(self._requirements())
        # estimate_design populates the keys the runner/tests read (delta_v_mps, launch_twr, cost,
        # part_count); design_ship's own `estimates` use different keys, so normalise to estimate_design.
        design.estimates = estimate_design(design)
        return design

    # ----- failure-driven requirement refinement (calculated, not string->part) -------------------

    def _adjust_requirement(self, reason: str, last_score: ScoreResult | None) -> None:
        """Raise the calculated requirement based on WHY the last trial failed, then design_ship is
        re-run. We bump the implicated phase's Δv reserve or a launch/landing phase's TWR floor — the
        new part counts are recomputed by physics, never edited by hand."""
        launch = self._first_phase_name()
        if "twr" in reason or "pad" in reason or "atmosphere_failure" in reason:
            # Could not hold TWR off the pad (or on a powered landing): raise the TWR floor on the
            # TWR-constrained phases so design_ship clusters more engines.
            for name in self._twr_constrained_phase_names():
                self._twr_floor[name] = max(self._twr_floor.get(name, 0.0),
                                            self._phase_min_twr(name)) + _TWR_STEP
            return
        if "delta" in reason or "apoapsis" in reason or (last_score is not None and not last_score.success):
            # Short on Δv (or an unexplained failure): add reserve to the launch phase, which cascades
            # the most mass and is the usual shortfall. design_ship re-sizes tanks by the rocket eqn.
            self._dv_reserve[launch] = self._dv_reserve.get(launch, 0.0) + _DV_RESERVE_STEP
            return
        if last_score is not None and last_score.success:
            margin = last_score.components.get("fuel_margin")  # noqa: F841 (reserved for future trimming)
            # Success with comfortable margin: nudge the launch reserve DOWN a touch to trim mass, but
            # never below zero (the bare physics budget). This is still a requirement change, not a part edit.
            self._dv_reserve[launch] = max(0.0, self._dv_reserve.get(launch, 0.0) - _DV_RESERVE_STEP / 2.0)
            return
        # No prior result / unknown reason: small calculated jitter on the launch reserve so repeated
        # calls explore nearby requirements deterministically (seeded).
        self._dv_reserve[launch] = max(0.0, self._dv_reserve.get(launch, 0.0)
                                       + self.random.choice([_DV_RESERVE_STEP, 0.0]))

    # ----- helpers --------------------------------------------------------------------------------

    def _first_phase_name(self) -> str:
        return self._budget.phases[0].name if self._budget.phases else "ascent_to_orbit"

    def _twr_constrained_phase_names(self) -> list[str]:
        return [pb.name for pb in self._budget.phases if pb.min_twr > 0 and pb.twr_body_g > 0]

    def _phase_min_twr(self, name: str) -> float:
        for pb in self._budget.phases:
            if pb.name == name:
                return pb.min_twr
        return 0.0

    def _name(self, prefix: str) -> str:
        suffix = self.random.randint(1000, 9999)
        return f"AI-{prefix}-{suffix}"
