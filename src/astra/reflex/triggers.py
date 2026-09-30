"""Stop conditions for reflexes, written by the AI.

A trigger list is any-of. Each entry is either a metric comparison
``{"metric": "apoapsis_altitude", "op": ">=", "value": 80000}`` or an event
``{"event": "flameout"}``. Metric names are those in `astra.telemetry.METRICS`.
"""

from __future__ import annotations

import math
import operator
from dataclasses import dataclass
from typing import Any, Callable

from astra.errors import AstraError
from astra.telemetry import METRICS

OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt,
    "==": operator.eq, "!=": operator.ne,
}

EVENTS = {
    "flameout": "an active engine ran dry (debounced)",
    "all_engines_out": "every active engine is dry or no engine is producing thrust while throttled up",
    "staged": "a stage was activated (by auto_stage or an autopilot)",
    "part_lost": "parts disappeared without a staging event (breakup, crash)",
    "soi_change": "the vessel entered another body's sphere of influence",
    "situation_change": "the vessel's situation changed (e.g. flying -> sub_orbital)",
    "landed": "situation became landed or splashed",
    "apoapsis_passed": "the vessel passed apoapsis (time_to_apoapsis jumped up)",
    "periapsis_passed": "the vessel passed periapsis",
    "node_passed": "the next maneuver node's time was reached",
}

# Metrics that need slower (RPC-backed) reads; the engine refreshes them only when used.
EXTENDED = {"next_periapsis_altitude", "node_time_to", "node_remaining_dv", "target_distance", "target_speed",
            "part_count", "electric_charge", "stage_propellant", "stage_dv", "max_temp_fraction"}


@dataclass
class Trigger:
    spec: dict
    metric: str | None = None
    op: str | None = None
    value: Any = None
    event: str | None = None

    def describe(self) -> str:
        if self.event:
            return f"event {self.event}"
        return f"{self.metric} {self.op} {self.value}"

    def check(self, m: dict, events: list[dict]) -> str | None:
        if self.event:
            for e in events:
                if e["type"] == self.event or (self.event == "landed" and e["type"] == "landed"):
                    return f"event {self.event}: {e.get('detail', '')}".strip()
            return None
        v = m.get(self.metric)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        try:
            hit = OPS[self.op](v, self.value)
        except TypeError:
            return None
        return f"{self.metric} {self.op} {self.value} (now {v:.6g})" if hit and isinstance(v, (int, float)) \
            else (f"{self.metric} {self.op} {self.value} (now {v})" if hit else None)


def parse_until(specs: list[dict] | None) -> list[Trigger]:
    out: list[Trigger] = []
    for spec in specs or []:
        if not isinstance(spec, dict):
            raise AstraError(f"trigger must be an object, got {spec!r}",
                             'e.g. {"metric": "apoapsis_altitude", "op": ">=", "value": 75000} or {"event": "flameout"}')
        if "event" in spec:
            ev = spec["event"]
            if ev not in EVENTS:
                raise AstraError(f"unknown event {ev!r}", f"events: {sorted(EVENTS)}")
            out.append(Trigger(spec, event=ev))
            continue
        metric, op, value = spec.get("metric"), spec.get("op"), spec.get("value")
        if metric not in METRICS:
            raise AstraError(f"unknown metric {metric!r}", f"metrics: {sorted(METRICS)}")
        if op not in OPS:
            raise AstraError(f"unknown op {op!r} in trigger on {metric}", f"ops: {sorted(OPS)}")
        if value is None:
            raise AstraError(f"trigger on {metric} needs a value")
        out.append(Trigger(spec, metric=metric, op=op, value=value))
    return out


def needs_extended(triggers: list[Trigger]) -> bool:
    return any(t.metric in EXTENDED for t in triggers if t.metric)
