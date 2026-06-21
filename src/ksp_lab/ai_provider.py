from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .models import MissionSpec, RocketDesign, ScoreResult
from .research import KnowledgeBase


class ExternalDesignProvider:
    """Adapter for an external AI design process.

    The command receives a JSON request on stdin and must return a RocketDesign
    JSON object on stdout. This keeps the optimizer replaceable without letting
    the external process write files, call KSP, or mutate the trial database.
    """

    def __init__(self, command: str | list[str], knowledge_base: KnowledgeBase | None = None, timeout_s: int = 120):
        self.command = shlex.split(command) if isinstance(command, str) else command
        self.knowledge_base = knowledge_base
        self.timeout_s = timeout_s

    def propose(
        self,
        mission: MissionSpec,
        fallback_design: RocketDesign,
        last_score: ScoreResult | None,
        history: list[dict[str, Any]],
    ) -> RocketDesign:
        request = {
            "mission": mission.to_dict(),
            "fallback_design": fallback_design.to_dict(),
            "last_score": last_score.to_dict() if last_score else None,
            "history": history,
            "knowledge_base": self.knowledge_base.context_summary() if self.knowledge_base else "",
            "schema": {
                "name": "string",
                "mission_type": "string",
                "payload_mass_t": "number",
                "crewed": "boolean",
                "stages": [
                    {
                        "role": "string",
                        "engine": "stock part key",
                        "tank": "stock part key",
                        "tank_count": "integer",
                        "decoupler_above": "boolean",
                        "notes": "string",
                    }
                ],
            },
        }
        proc = subprocess.run(
            self.command,
            input=json.dumps(request),
            text=True,
            capture_output=True,
            timeout=self.timeout_s,
            cwd=str(Path.cwd()),
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"External design provider failed: {proc.stderr.strip()}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"External design provider returned invalid JSON: {proc.stdout[:500]}") from exc
        design = RocketDesign.from_dict(payload)
        design.source = "external"
        return design

