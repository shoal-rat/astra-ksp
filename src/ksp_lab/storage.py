from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import ScoreResult, TelemetrySummary, TrialRecord, utc_now


class TrialDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trials (
                    trial_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    mission_json TEXT NOT NULL,
                    design_json TEXT NOT NULL,
                    craft_path TEXT NOT NULL,
                    telemetry_path TEXT NOT NULL,
                    score_json TEXT,
                    telemetry_json TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trials_status ON trials(status)")

    def start_trial(self, record: TrialRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO trials
                (trial_id, status, mode, started_at, finished_at, mission_json, design_json, craft_path, telemetry_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.trial_id,
                    record.status,
                    record.mode,
                    record.started_at,
                    record.finished_at,
                    json.dumps(record.mission.to_dict(), sort_keys=True),
                    json.dumps(record.design.to_dict(), sort_keys=True),
                    record.craft_path,
                    record.telemetry_path,
                ),
            )

    def finish_trial(self, trial_id: str, status: str, score: ScoreResult, telemetry: TelemetrySummary) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trials
                SET status = ?, finished_at = ?, score_json = ?, telemetry_json = ?
                WHERE trial_id = ?
                """,
                (
                    status,
                    utc_now(),
                    json.dumps(score.to_dict(), sort_keys=True),
                    json.dumps(telemetry.to_dict(), sort_keys=True),
                    trial_id,
                ),
            )

    def mark_failed(self, trial_id: str, failure_reason: str) -> None:
        score = ScoreResult(0.0, False, failure_reason, {"mission": 0.0})
        telemetry = TelemetrySummary(mission_phase="runner_exception")
        self.finish_trial(trial_id, "failed", score, telemetry)

    def pending_or_running_trials(self) -> Iterable[sqlite3.Row]:
        with self._connect() as conn:
            yield from conn.execute(
                "SELECT * FROM trials WHERE status IN ('pending', 'running') ORDER BY started_at"
            )

    def last_score(self) -> ScoreResult | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT score_json FROM trials WHERE score_json IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return ScoreResult(**json.loads(row["score_json"]))

    def list_trials(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trial_id, status, mode, started_at, finished_at, score_json FROM trials ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            payload = dict(row)
            payload["score"] = json.loads(payload.pop("score_json")) if row["score_json"] else None
            result.append(payload)
        return result

