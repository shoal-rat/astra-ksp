from ksp_lab.mission import MissionPlanner
from ksp_lab.models import ScoreResult
from ksp_lab.optimizer import HistoryOptimizer


def test_optimizer_adds_margin_after_delta_v_failure():
    mission = MissionPlanner().interpret("deliver payload to 80 km Kerbin orbit")
    opt = HistoryOptimizer(mission, seed=1)
    first = opt.first_design()
    nxt = opt.next_design(ScoreResult(10, False, "under_delta_v", {}))
    # "Adds margin" = MORE capability. The diameter-laddered sizer may answer a Δv shortfall by upgrading
    # to a bigger tank type (fuelTank x3) rather than more small tanks (fuelTankSmall x5), so assert the
    # real intent — the next design carries more Δv — not the raw tank count (which can fall as tanks grow).
    assert nxt.estimates["delta_v_mps"] > first.estimates["delta_v_mps"], (
        first.estimates["delta_v_mps"], nxt.estimates["delta_v_mps"])

