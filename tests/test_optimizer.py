from ksp_lab.mission import MissionPlanner
from ksp_lab.models import ScoreResult
from ksp_lab.optimizer import HistoryOptimizer


def test_optimizer_adds_margin_after_delta_v_failure():
    mission = MissionPlanner().interpret("deliver payload to 80 km Kerbin orbit")
    opt = HistoryOptimizer(mission, seed=1)
    first = opt.first_design()
    nxt = opt.next_design(ScoreResult(10, False, "under_delta_v", {}))
    assert nxt.stages[0].tank_count >= first.stages[0].tank_count
    assert nxt.estimates["delta_v_mps"] != first.estimates["delta_v_mps"]

