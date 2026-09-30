"""Event detection and safety interlocks for reflexes.

Events are facts observed during a reflex (staging, flameout, SOI change...). Interlocks are
events serious enough to hand control back to the AI immediately. Interlocks never take action
on their own; they stop the reflex, pause the game, and report.
"""

from __future__ import annotations

import math
from typing import Any

from astra.errors import AstraError

DEFAULT_INTERLOCKS: dict[str, Any] = {
    # Stop when an engine runs dry (unless auto_stage is enabled and has stages left).
    "flameout": True,
    # Stop when parts disappear without a staging event (breakup, collision).
    "part_lost": True,
    # Stop on entering another sphere of influence.
    "soi_change": True,
    # Stop when any part reaches this fraction of its max temperature (None disables).
    "overheat": 0.95,
    # Stop when the ground is `seconds` away at more than `speed_mps` descent rate (None disables).
    "impact": {"seconds": 10.0, "speed_mps": 12.0},
    # Stop when the autopilot error stays above error_deg for `seconds` at dynamic pressure >= min_q_pa.
    "loss_of_control": {"error_deg": 25.0, "seconds": 4.0, "min_q_pa": 3000.0},
    # Stop when ElectricCharge falls below this fraction (probe cores lose control without power).
    "low_power": 0.02,
    # An engine must read dry this many game seconds before it counts (tank-crossfeed transients).
    "dry_debounce_s": 0.4,
}


def merge_interlocks(overrides: dict | None) -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_INTERLOCKS.items()}
    for k, v in (overrides or {}).items():
        if k not in cfg:
            raise AstraError(f"unknown interlock {k!r}", f"interlocks: {sorted(DEFAULT_INTERLOCKS)}")
        if isinstance(cfg[k], dict) and isinstance(v, dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def _f(x: Any) -> bool:
    return isinstance(x, (int, float)) and not math.isnan(x) and not math.isinf(x)


class EventDetector:
    def __init__(self, probe, cfg: dict):
        self.probe = probe
        self.cfg = cfg
        self.prev: dict[str, Any] | None = None
        self.last_stage_ut = -math.inf
        self._dry_since: dict[int, float] = {}
        self._flamed: set[int] = set()
        self._engine_key = None
        self._all_out_since: float | None = None
        self._all_out_fired = False
        self._loc_since: float | None = None
        self.engines: list[dict] = []

    def note_staged(self, ut: float) -> None:
        self.last_stage_ut = ut
        self._dry_since.clear()
        self._flamed.clear()
        self._all_out_since = None
        self._all_out_fired = False

    def update(self, m: dict, ap_error: float | None, law_uses_ap: bool) -> list[dict]:
        ev: list[dict] = []
        p = self.prev
        ut = m.get("ut", math.nan)
        if p is not None:
            if m.get("stage") != p.get("stage"):
                ev.append({"type": "staged", "detail": f"stage {p.get('stage')} -> {m.get('stage')}"})
                self.note_staged(ut)
            if m.get("body") != p.get("body"):
                ev.append({"type": "soi_change", "detail": f"{p.get('body')} -> {m.get('body')}"})
            if m.get("situation") != p.get("situation"):
                ev.append({"type": "situation_change", "detail": f"{p.get('situation')} -> {m.get('situation')}"})
                if m.get("situation") in ("landed", "splashed"):
                    ev.append({"type": "landed", "detail": m.get("situation")})
            pc, pp = m.get("part_count"), p.get("part_count")
            if _f(pc) and _f(pp) and pc < pp:
                if ut - self.last_stage_ut <= 3.0:
                    ev.append({"type": "separated", "detail": f"{int(pp - pc)} parts dropped after staging"})
                else:
                    ev.append({"type": "part_lost", "detail": f"{int(pp - pc)} parts lost ({int(pp)} -> {int(pc)})"})
            airborne = m.get("situation") not in ("pre_launch", "landed", "splashed")
            for key, name in (("time_to_apoapsis", "apoapsis_passed"), ("time_to_periapsis", "periapsis_passed")):
                a, b = p.get(key), m.get(key)
                if airborne and _f(a) and _f(b) and b > a + 60 and a < 30:
                    ev.append({"type": name, "detail": ""})
            a, b = p.get("node_time_to"), m.get("node_time_to")
            if _f(a) and _f(b) and a > 0 >= b:
                ev.append({"type": "node_passed", "detail": ""})
        ev.extend(self._engine_events(m, ut))
        self._loc_check(m, ut, ap_error, law_uses_ap)
        self.prev = m
        return ev

    def _engine_events(self, m: dict, ut: float) -> list[dict]:
        out: list[dict] = []
        try:
            engines = self.probe.engines()
        except Exception:  # noqa: BLE001 — vessel mid-separation; try next tick
            return out
        self.engines = engines
        key = self.probe._engine_key
        if key != self._engine_key:
            self._engine_key = key
            self._dry_since.clear()
            self._flamed.clear()
        debounce = float(self.cfg.get("dry_debounce_s") or 0.0)
        dry_now = []
        for i, e in enumerate(engines):
            if not e["has_fuel"]:
                t0 = self._dry_since.setdefault(i, ut)
                if ut - t0 >= debounce and i not in self._flamed:
                    self._flamed.add(i)
                    dry_now.append(e["part"])
            else:
                self._dry_since.pop(i, None)
        if dry_now:
            out.append({"type": "flameout", "detail": f"dry: {', '.join(dry_now)}", "engines": dry_now})
        thr = m.get("throttle", 0.0)
        all_dry = bool(engines) and all(not e["has_fuel"] for e in engines)
        no_thrust = (not engines or m.get("available_thrust", 0.0) <= 0.0) and _f(thr) and thr > 0.05
        if all_dry or no_thrust:
            if self._all_out_since is None:
                self._all_out_since = ut
            if ut - self._all_out_since >= debounce and not self._all_out_fired:
                self._all_out_fired = True
                out.append({"type": "all_engines_out", "detail": "no active engine can produce thrust"})
        else:
            self._all_out_since = None
            self._all_out_fired = False
        return out

    def _loc_check(self, m: dict, ut: float, ap_error: float | None, law_uses_ap: bool) -> None:
        cfg = self.cfg.get("loss_of_control")
        q = m.get("dynamic_pressure", 0.0)
        if not cfg or not law_uses_ap or ap_error is None or not _f(ap_error) or not _f(q) \
                or q < cfg["min_q_pa"] or ap_error < cfg["error_deg"]:
            self._loc_since = None
            return
        if self._loc_since is None:
            self._loc_since = ut
        self.loc_seconds = ut - self._loc_since

    def interlock(self, m: dict, events: list[dict], auto_stage_active: bool) -> str | None:
        cfg = self.cfg
        for e in events:
            if e["type"] == "part_lost" and cfg.get("part_lost"):
                return f"part_lost: {e['detail']}"
            if e["type"] == "soi_change" and cfg.get("soi_change"):
                return f"soi_change: {e['detail']}"
            if e["type"] in ("flameout", "all_engines_out") and cfg.get("flameout") and not auto_stage_active:
                return f"{e['type']}: {e['detail']}"
        heat = cfg.get("overheat")
        tf = m.get("max_temp_fraction")
        if heat and _f(tf) and tf >= heat:
            return f"overheat: a part is at {tf:.0%} of its max temperature"
        imp = cfg.get("impact")
        vs, h = m.get("vertical_speed"), m.get("surface_altitude")
        if imp and _f(vs) and _f(h) and vs < -imp["speed_mps"] and h / -vs < imp["seconds"] \
                and m.get("situation") not in ("landed", "splashed", "pre_launch"):
            return f"impact: ground in {h / -vs:.1f} s at {-vs:.0f} m/s descent"
        loc = cfg.get("loss_of_control")
        if loc and self._loc_since is not None and m.get("ut", 0) - self._loc_since >= loc["seconds"]:
            return f"loss_of_control: autopilot error above {loc['error_deg']}° for {loc['seconds']} s at q >= {loc['min_q_pa']} Pa"
        low = cfg.get("low_power")
        ec = m.get("electric_charge")
        if low and _f(ec) and ec < low:
            return f"low_power: ElectricCharge at {ec:.1%}"
        return None
