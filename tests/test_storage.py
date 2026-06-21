from pathlib import Path

from ksp_lab.mission import MissionPlanner
from ksp_lab.models import ScoreResult, TelemetrySummary, TrialRecord
from ksp_lab.optimizer import HistoryOptimizer
from ksp_lab.storage import TrialDatabase


def test_trial_database_stores_finished_trial(tmp_path: Path):
    db = TrialDatabase(tmp_path / "trials.sqlite3")
    mission = MissionPlanner().interpret("deliver payload to 80 km Kerbin orbit")
    design = HistoryOptimizer(mission).first_design()
    record = TrialRecord(
        trial_id="trial-test",
        mission=mission,
        design=design,
        craft_path="craft.craft",
        telemetry_path="telemetry.jsonl",
        mode="offline",
        status="running",
    )
    db.start_trial(record)
    db.finish_trial("trial-test", "succeeded", ScoreResult(90, True), TelemetrySummary(max_altitude_m=90000))
    rows = db.list_trials()
    assert rows[0]["trial_id"] == "trial-test"
    assert rows[0]["score"]["success"] is True

