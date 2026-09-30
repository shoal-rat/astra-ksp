"""The reflex engine: run the sim under AI-commanded control laws until a trigger fires.

One `fly()` call is one reflex. It unpauses the game, applies the throttle and attitude laws at
~20 Hz from stream-fed metrics, watches for events and interlocks, optionally stages on flameout
(only when the AI enabled it and the stage is safe), and stops on the first of: an AI trigger, an
implicit law completion, an interlock, a timeout, or an extra stop callback. It then pauses the
game for deliberation and returns what happened, including a compact trace.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable

from astra.errors import AstraError, NoVessel
from astra.ksp import ksp
from astra.reflex.context import Ctx
from astra.reflex.events import EventDetector, merge_interlocks
from astra.reflex.laws import AttitudeLaw, ThrottleLaw, parse_attitude, parse_throttle
from astra.reflex.triggers import parse_until
from astra.telemetry import probe

TICK_S = 0.05
TRACE_FIELDS = ("t", "altitude", "surface_altitude", "vertical_speed", "surface_speed", "orbital_speed",
                "apoapsis_altitude", "periapsis_altitude", "pitch", "throttle", "twr", "dynamic_pressure",
                "stage")
FINAL_FIELDS = ("ut", "situation", "body", "altitude", "surface_altitude", "vertical_speed", "horizontal_speed",
                "surface_speed", "orbital_speed", "apoapsis_altitude", "periapsis_altitude", "time_to_apoapsis",
                "time_to_periapsis", "eccentricity", "inclination", "pitch", "heading", "throttle", "twr",
                "max_twr", "mass", "stage", "stage_dv", "dynamic_pressure", "next_periapsis_altitude",
                "node_remaining_dv", "part_count")


def _f(x: Any) -> bool:
    return isinstance(x, (int, float)) and not math.isnan(x) and not math.isinf(x)


class AutoStage:
    """Stage on flameout, but only when staging is safe: it must light a new engine, or drop only
    propellant-empty parts while another engine keeps burning. Anything else goes back to the AI."""

    def __init__(self, spec: Any):
        if spec in (None, False):
            self.enabled, self.max_stages, self.min_interval = False, 0, 0.0
        elif spec is True:
            self.enabled, self.max_stages, self.min_interval = True, 99, 1.0
        elif isinstance(spec, dict):
            self.enabled = bool(spec.get("enabled", True))
            self.max_stages = int(spec.get("max_stages", 99))
            self.min_interval = float(spec.get("min_interval_s", 1.0))
        else:
            raise AstraError(f"auto_stage must be true/false or an object, got {spec!r}")
        self.fired = 0
        self.last_ut = -math.inf

    @property
    def active(self) -> bool:
        return self.enabled and self.fired < self.max_stages

    def check_safe(self, ctx: Ctx, stage: int) -> str | None:
        """Return None if staging is safe, else the reason not to."""
        v = ctx.vessel
        nxt = stage - 1
        if nxt < 0:
            return "no stages left"
        parts = v.parts
        igniting = [p for p in parts.in_stage(nxt) if p.engine is not None]
        dropped = list(parts.in_decouple_stage(nxt))
        dropped_prop = 0.0
        for p in dropped:
            for r in p.resources.all:
                if r.name in ("LiquidFuel", "Oxidizer", "SolidFuel", "MonoPropellant", "XenonGas"):
                    dropped_prop += r.amount * ctx.probe.density(r.name)
        if igniting:
            if dropped_prop > 50.0:
                return f"next stage would drop {dropped_prop:.0f} kg of propellant along with its parts"
            return None
        survivors = [e for e in parts.engines if e.active and e.has_fuel and e.part.decouple_stage < nxt]
        if dropped and dropped_prop <= 1.0 and survivors:
            return None
        if not dropped:
            return "next stage neither lights an engine nor separates anything (chutes/fairings/payload?)"
        return "next stage lights no engine and would leave no burning engine or drop propellant"

    def fire(self, ctx: Ctx, ut: float) -> dict:
        v = ctx.vessel
        before = len(v.parts.all)
        stage_before = v.control.current_stage
        v.control.activate_next_stage()
        self.fired += 1
        self.last_ut = ut
        # KSP splits the vessel over several frames; wait for the part count to settle.
        t0 = time.monotonic()
        after = before
        while time.monotonic() - t0 < 2.0:
            time.sleep(0.1)
            try:
                ctx.refresh()
                after = len(ctx.vessel.parts.all)
            except NoVessel:
                continue
            if after < before:
                break
        return {"type": "auto_staged", "detail": f"stage {stage_before} -> {stage_before - 1}, parts {before} -> {after}"}


def fly(*, until: list[dict] | None = None, throttle: Any = "keep", attitude: Any = "keep",
        auto_stage: Any = None, interlocks: dict | None = None, max_game_s: float | None = None,
        max_real_s: float = 600.0, extra_stop: Callable[[dict], str | None] | None = None,
        hands_off: bool = False, physics_warp: int = 0, exit_throttle: Any = "keep", label: str = "fly",
        trace_points: int = 16, throttle_law: ThrottleLaw | None = None,
        attitude_law: AttitudeLaw | None = None, pause_at_end: bool = True) -> dict:
    """Run one reflex. See module docstring. Returns a report dict."""
    k = ksp()
    k.require_flight()
    triggers = parse_until(until)
    tlaw = throttle_law or (parse_throttle("keep") if hands_off else parse_throttle(throttle))
    alaw = attitude_law or (parse_attitude("keep") if hands_off else parse_attitude(attitude))
    stager = AutoStage(None if hands_off else auto_stage)
    cfg = merge_interlocks(interlocks)
    if max_game_s is not None and max_game_s <= 0:
        raise AstraError("max_game_s must be positive")
    if not triggers and max_game_s is None and extra_stop is None and not getattr(tlaw, "finishes", False) \
            and not getattr(alaw, "finishes", False):
        raise AstraError("this reflex has no way to stop",
                         "give at least one trigger in `until`, or max_game_s")

    p = probe()
    p.invalidate()
    ctx = Ctx(k, p)
    det = EventDetector(p, cfg)
    sc = k.sc
    events_log: list[dict] = []
    samples: list[dict] = []
    last_m: dict[str, Any] = {}
    prev_m: dict[str, Any] = {}
    stopped_by: dict[str, Any] | None = None
    ap_error_stream = None
    wall0 = time.monotonic()
    heat_watch = bool(cfg.get("overheat"))
    chute_watch = False

    try:
        if sc.rails_warp_factor != 0:
            sc.rails_warp_factor = 0
        sc.physics_warp_factor = max(0, min(3, int(physics_warp)))
        m = p.read_extended(include_temp=False)
        ut0 = m["ut"]
        tlaw.start(ctx, m)
        alaw.start(ctx, m)
        if alaw.uses_autopilot:
            ap_error_stream = k.conn.add_stream(getattr, ctx.vessel.auto_pilot, "error")
        t = tlaw.update(ctx, m)
        if t is not None:
            ctx.vessel.control.throttle = t
        k.set_paused(False)
        wall0 = time.monotonic()

        while True:
            tick0 = time.monotonic()
            try:
                m = p.read_extended(include_temp=heat_watch and _hot(last_m))
            except NoVessel as exc:
                stopped_by = {"kind": "interlock", "detail": f"vessel_changed: {exc.message}"}
                break
            ctx.check_body(m["body"])
            ut = m["ut"]
            game_s = ut - ut0
            ap_err = None
            if ap_error_stream is not None:
                try:
                    ap_err = ap_error_stream()
                except Exception:  # noqa: BLE001
                    ap_err = None
            m["autopilot_error"] = ap_err

            events = det.update(m, ap_err, alaw.uses_autopilot)
            for e in events:
                e["t"] = round(game_s, 2)
            events_log.extend(events)

            # Auto-staging on flameout (only when the AI enabled it and it is safe).
            if stager.active and any(e["type"] in ("flameout", "all_engines_out") for e in events) \
                    and ut - stager.last_ut >= stager.min_interval:
                reason = stager.check_safe(ctx, int(m["stage"]))
                if reason is None:
                    ev = stager.fire(ctx, ut)
                    ev["t"] = round(game_s, 2)
                    events_log.append(ev)
                    det.note_staged(ut)
                    p.invalidate()
                    if ap_error_stream is not None:
                        ap_error_stream.remove()
                        ap_error_stream = k.conn.add_stream(getattr, ctx.vessel.auto_pilot, "error")
                    continue
                stopped_by = {"kind": "interlock", "detail": f"flameout, auto_stage declined: {reason}"}
                break

            # Control laws.
            if not hands_off:
                alaw.update(ctx, m)
                t = tlaw.update(ctx, m)
                if t is not None:
                    ctx.vessel.control.throttle = t
                    m["throttle"] = t

            prev_m, last_m = last_m, m
            samples.append({"t": game_s, **{f: m.get(f) for f in TRACE_FIELDS[1:]}})

            # Stop conditions, most specific first.
            hit = next((r for r in (tr.check(m, events) for tr in triggers) if r), None)
            if hit:
                stopped_by = {"kind": "trigger", "detail": hit}
                break
            done = tlaw.done(ctx, m) or _law_done(alaw, ctx, m)
            if done:
                stopped_by = {"kind": "law", "detail": done}
                break
            if extra_stop is not None:
                r = extra_stop(m)
                if r:
                    stopped_by = {"kind": "condition", "detail": r}
                    break
            il = det.interlock(m, events, stager.active)
            if il and il.startswith("impact"):
                chute_note = _chute_context(ctx, m, prev_m)
                if chute_note == "expected":
                    if not chute_watch:
                        chute_watch = True
                        events_log.append({"t": round(game_s, 2), "type": "impact_watch",
                                           "detail": "fast descent under semi-deployed parachutes above "
                                                     "their full-deploy altitude; continuing"})
                    il = None
                elif chute_note:
                    il = f"{il}; {chute_note}"
            if il:
                stopped_by = {"kind": "interlock", "detail": il}
                break
            if max_game_s is not None and game_s >= max_game_s:
                stopped_by = {"kind": "timeout", "detail": f"max_game_s {max_game_s:g} reached"}
                break
            if time.monotonic() - wall0 >= max_real_s:
                stopped_by = {"kind": "timeout",
                              "detail": f"max_real_s {max_real_s:g} reached (game time ran {game_s:.1f} s)"}
                break
            time.sleep(max(0.0, TICK_S - (time.monotonic() - tick0)))
    except AstraError:
        _safe_exit(k, ctx, exit_throttle)
        ctx.close()
        k.hold_for_deliberation()
        raise
    except Exception as exc:  # noqa: BLE001 — report whatever happened, with the game paused
        stopped_by = {"kind": "error", "detail": f"{type(exc).__name__}: {exc}"}

    real_s = time.monotonic() - wall0
    control_state = _safe_exit(k, ctx, exit_throttle)
    try:
        attitude_state = alaw.finish(ctx) if not hands_off else "hands off"
    except Exception:  # noqa: BLE001
        attitude_state = "unknown"
    if "sas" in control_state:
        try:  # finish() may hand the attitude to SAS (the descent's touchdown settle): report it as it is now
            control_state["sas"] = k.vessel().control.sas
        except Exception:  # noqa: BLE001
            pass
    if ap_error_stream is not None:
        try:
            ap_error_stream.remove()
        except Exception:  # noqa: BLE001
            pass
    ctx.close()
    try:
        final_m = p.read_extended(include_temp=False)
    except Exception:  # noqa: BLE001
        final_m = last_m
    return {
        "label": label,
        "stopped_by": stopped_by or {"kind": "unknown", "detail": ""},
        "game_s": round(samples[-1]["t"], 2) if samples else 0.0,
        "real_s": round(real_s, 1),
        "events": _compact_events(events_log),
        "stages_fired": stager.fired,
        "laws": {"throttle": tlaw.describe, "attitude": alaw.describe},
        "trace_fields": list(TRACE_FIELDS),
        "trace": _downsample(samples, events_log, trace_points),
        "final": {f: final_m.get(f) for f in FINAL_FIELDS if f in final_m},
        "control": {**control_state, "attitude": attitude_state},
        "paused": k.hold_for_deliberation() if pause_at_end else k.paused,
    }


def _law_done(alaw: AttitudeLaw, ctx: Ctx, m: dict) -> str | None:
    fn = getattr(alaw, "done", None)
    return fn(ctx, m) if fn else None


def _chute_context(ctx: Ctx, m: dict, prev: dict | None = None) -> str | None:
    """'expected' if parachutes explain the fast fall (semi-deployed above their full-deploy altitude,
    or deployed and already slowing the descent), else a note on chute state."""
    try:
        chutes = ctx.vessel.parts.parachutes
    except Exception:  # noqa: BLE001
        return None
    if not chutes:
        return None
    h = m.get("surface_altitude", 0.0)
    states = []
    for c in chutes:
        try:
            st = str(c.state).split(".")[-1]
            states.append(st)
            if st == "semi_deployed" and _f(h) and h > c.deploy_altitude:
                return "expected"
            vs, pvs = m.get("vertical_speed"), (prev or {}).get("vertical_speed")
            if st == "deployed" and _f(vs) and _f(pvs) and vs - pvs > 0.05:
                return "expected"  # just opened: the descent is already slowing
        except Exception:  # noqa: BLE001
            continue
    return f"parachutes: {', '.join(states)}"


def _hot(m: dict | None) -> bool:
    """Heating is plausible (in air and fast); only then are per-part temperature streams built."""
    if not m:
        return False
    rho, v = m.get("atmosphere_density"), m.get("surface_speed")
    return _f(rho) and rho > 1e-5 and _f(v) and v > 300.0


def _safe_exit(k, ctx: Ctx, exit_throttle: Any) -> dict:
    """Apply the exit throttle and let it take effect before the game pauses: kRPC applies control
    inputs on the next physics tick, so pausing immediately would freeze the old throttle."""
    out: dict[str, Any] = {}
    try:
        k.sc.physics_warp_factor = 0
    except Exception:  # noqa: BLE001
        pass
    try:
        v = k.vessel()
        target = None
        if exit_throttle == "cut":
            target = 0.0
        elif isinstance(exit_throttle, (int, float)) and not isinstance(exit_throttle, bool):
            target = max(0.0, min(1.0, float(exit_throttle)))
        if target is not None:
            v.control.throttle = target
            settle_controls(k, lambda: abs(v.control.throttle - target) < 1e-3)
        out["throttle"] = v.control.throttle
        out["sas"] = v.control.sas
    except Exception:  # noqa: BLE001 — vessel gone
        out["throttle"] = None
    return out


def settle_controls(k, done: Callable[[], bool], timeout_s: float = 1.0) -> bool:
    """Run the sim briefly (unpaused) until `done()` holds, so commanded inputs are applied."""
    was_paused = k.paused
    if was_paused:
        k.set_paused(False)
    t0 = time.monotonic()
    ok = False
    while time.monotonic() - t0 < timeout_s:
        try:
            if done():
                ok = True
                break
        except Exception:  # noqa: BLE001
            break
        time.sleep(0.05)
    if was_paused:
        k.set_paused(True)
    return ok


def _compact_events(events: list[dict], limit: int = 40) -> list[dict]:
    out = [{k: v for k, v in e.items() if k in ("t", "type", "detail")} for e in events]
    if len(out) > limit:
        out = out[:limit // 2] + [{"type": "…", "detail": f"{len(out) - limit} events omitted"}] + out[-limit // 2:]
    return out


def _downsample(samples: list[dict], events: list[dict], n: int) -> list[list]:
    if not samples:
        return []
    t_end = samples[-1]["t"]
    marks = sorted({round(t_end * i / max(1, n - 1), 3) for i in range(n)} | {e.get("t", 0.0) for e in events})
    rows, j = [], 0
    for mark in marks:
        while j < len(samples) - 1 and samples[j + 1]["t"] <= mark:
            j += 1
        s = samples[j]
        row = [round(s["t"], 2)] + [s.get(f) for f in TRACE_FIELDS[1:]]
        if not rows or rows[-1][0] != row[0]:
            rows.append(row)
    return rows
