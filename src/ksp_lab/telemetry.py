from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import TelemetrySummary, utc_now


class TelemetryRecorder:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, sample: dict[str, Any]) -> None:
        payload = {"timestamp": utc_now(), **sample}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")

    def summarize(self) -> TelemetrySummary:
        max_alt = 0.0
        apo = 0.0
        peri = -1.0
        elapsed = 0.0
        fuel = 0.0
        phase = "unknown"
        destroyed = False
        landed = False
        recovered = False
        phases_seen: set[str] = set()
        science_completed = False
        relay_deployed = False
        if not self.path.exists():
            return TelemetrySummary()
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                sample = json.loads(line)
                max_alt = max(max_alt, float(sample.get("altitude_m", 0.0)))
                apo = max(apo, float(sample.get("apoapsis_m", 0.0)))
                peri = max(peri, float(sample.get("periapsis_m", -1.0)))
                elapsed = max(elapsed, float(sample.get("elapsed_s", 0.0)))
                fuel = float(sample.get("fuel_fraction_left", fuel))
                phase = str(sample.get("phase", phase))
                phases_seen.add(phase)
                destroyed = destroyed or bool(sample.get("vessel_destroyed", False))
                landed = landed or bool(sample.get("landed", False))
                recovered = recovered or bool(sample.get("recovered", False))
                science_completed = science_completed or bool(sample.get("science_completed", False))
                relay_deployed = relay_deployed or bool(sample.get("relay_deployed", False))
        return TelemetrySummary(
            max_altitude_m=max_alt,
            apoapsis_m=apo,
            periapsis_m=peri,
            landed=landed,
            recovered=recovered,
            vessel_destroyed=destroyed,
            mission_phase=phase,
            elapsed_s=elapsed,
            fuel_fraction_left=fuel,
            extra={
                "phases_seen": sorted(phases_seen),
                "science_completed": science_completed,
                "relay_deployed": relay_deployed,
            },
        )
