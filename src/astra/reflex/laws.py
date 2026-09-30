"""Control laws a reflex applies every tick. The AI chooses the law and every number in it.

Throttle laws return a throttle in [0, 1] (or None to leave it alone). Attitude laws steer with
the kRPC autopilot in frames that do not rotate with the vessel (body-fixed, body-inertial,
surface, or node frames), or hand attitude to SAS.
"""

from __future__ import annotations

import math
from typing import Any

from astra.errors import AstraError
from astra.reflex import vec
from astra.reflex.context import Ctx

_NAN = float("nan")


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not math.isnan(x) and not math.isinf(x)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# ============================================================================================
# Throttle


class ThrottleLaw:
    describe = "throttle"

    def start(self, ctx: Ctx, m: dict) -> None:  # noqa: D401 — hook
        pass

    def update(self, ctx: Ctx, m: dict) -> float | None:
        return None

    def done(self, ctx: Ctx, m: dict) -> str | None:
        """An implicit stop reason when the law has finished its job."""
        return None


class KeepThrottle(ThrottleLaw):
    describe = "keep"


class ConstantThrottle(ThrottleLaw):
    def __init__(self, value: float):
        self.value = _clamp(float(value), 0.0, 1.0)
        self.describe = f"constant {self.value:g}"

    def update(self, ctx, m):
        return self.value


class TwrThrottle(ThrottleLaw):
    """Throttle so thrust/weight in local gravity equals `twr` (clamped to [min, max])."""

    def __init__(self, twr: float, lo: float = 0.0, hi: float = 1.0):
        if twr <= 0:
            raise AstraError("twr throttle law needs twr > 0")
        self.twr, self.lo, self.hi = float(twr), lo, hi
        self.describe = f"hold TWR {self.twr:g}"

    def update(self, ctx, m):
        avail, mass, g = m.get("available_thrust"), m.get("mass"), m.get("local_g")
        if not (_finite(avail) and avail > 0 and _finite(mass) and _finite(g)):
            return self.hi
        return _clamp(self.twr * mass * g / avail, self.lo, self.hi)


class ApproachThrottle(ThrottleLaw):
    """Drive `metric` toward `target`, feathering the throttle as the estimated time-to-target at
    full throttle drops below `feather_s`. Stops (implicitly) when the metric reaches the target."""

    finishes = True

    def __init__(self, metric: str, target: float, feather_s: float, lo: float, hi: float):
        if feather_s <= 0:
            raise AstraError("approach law needs feather_s > 0")
        self.metric, self.target, self.feather_s, self.lo, self.hi = metric, float(target), feather_s, lo, hi
        self.describe = f"approach {metric} -> {target:g} (feather {feather_s:g}s)"
        self._prev: tuple[float, float, float] | None = None
        self._rate: float | None = None
        self._sign = 0.0
        self._reached = False

    def start(self, ctx, m):
        v = m.get(self.metric)
        if isinstance(v, float) and math.isinf(v):
            # e.g. apoapsis while still unbound during a capture burn: approach from that side
            self._sign = 1.0 if v < 0 else -1.0
            return
        if not _finite(v):
            raise AstraError(f"approach law: metric {self.metric!r} is not available right now ({v})",
                             "choose a metric that has a value now (see telemetry), or a trigger instead")
        self._sign = 1.0 if self.target > v else -1.0

    def update(self, ctx, m):
        v, ut, thr = m.get(self.metric), m.get("ut"), m.get("throttle", 0.0)
        if not _finite(v):
            self._prev = None  # no rate across an infinite/undefined stretch
            return self.hi
        if (self.target - v) * self._sign <= 0:
            self._reached = True
            return 0.0
        if self._prev is not None and _finite(ut):
            dt = ut - self._prev[0]
            if dt > 1e-3 and self._prev[2] > 0.02:
                r = (v - self._prev[1]) / dt / self._prev[2]  # metric change per second per unit throttle
                self._rate = r if self._rate is None else 0.7 * self._rate + 0.3 * r
        self._prev = (ut, v, thr if _finite(thr) else 0.0)
        if self._rate is None or self._rate * self._sign <= 0:
            return self.hi
        eta_full = (self.target - v) / self._rate
        return _clamp(eta_full / self.feather_s, self.lo, self.hi)

    def done(self, ctx, m):
        return f"{self.metric} reached {self.target:g}" if self._reached else None


class MaxQCap(ThrottleLaw):
    """Wraps another law and scales throttle down when dynamic pressure exceeds `max_q_pa`."""

    def __init__(self, inner: ThrottleLaw, max_q_pa: float):
        self.inner, self.max_q = inner, float(max_q_pa)
        self.finishes = getattr(inner, "finishes", False)
        self.describe = f"{inner.describe}, q<= {self.max_q:g} Pa"

    def start(self, ctx, m):
        self.inner.start(ctx, m)

    def update(self, ctx, m):
        t = self.inner.update(ctx, m)
        q = m.get("dynamic_pressure")
        if t is None or not _finite(q) or q <= self.max_q:
            return t
        return t * max(0.0, 1.0 - (q - self.max_q) / self.max_q * 4.0)

    def done(self, ctx, m):
        return self.inner.done(ctx, m)


def parse_throttle(spec: Any) -> ThrottleLaw:
    if spec is None or spec == "keep":
        return KeepThrottle()
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        return ConstantThrottle(spec)
    if isinstance(spec, dict):
        mode = spec.get("mode", "constant")
        lo, hi = float(spec.get("min", 0.0)), float(spec.get("max", 1.0))
        if mode == "constant":
            law: ThrottleLaw = ConstantThrottle(spec["value"])
        elif mode == "twr":
            law = TwrThrottle(spec["twr"], lo, hi)
        elif mode == "approach":
            law = ApproachThrottle(spec["metric"], spec["target"], float(spec.get("feather_s", 0) or 0), lo, hi)
        elif mode == "keep":
            law = KeepThrottle()
        else:
            raise AstraError(f"unknown throttle mode {mode!r}",
                             "use a number 0..1, 'keep', or {mode: constant|twr|approach, ...}")
        if spec.get("max_q_pa"):
            law = MaxQCap(law, float(spec["max_q_pa"]))
        return law
    raise AstraError(f"cannot read throttle spec {spec!r}",
                     "use a number 0..1, 'keep', or {mode: twr|approach|constant, ...}")


# ============================================================================================
# Attitude


class AttitudeLaw:
    describe = "attitude"
    uses_autopilot = False

    def start(self, ctx: Ctx, m: dict) -> None:
        pass

    def update(self, ctx: Ctx, m: dict) -> None:
        pass

    def finish(self, ctx: Ctx) -> str:
        return self.describe


class KeepAttitude(AttitudeLaw):
    describe = "keep current attitude control"


class FreeAttitude(AttitudeLaw):
    describe = "autopilot released"

    def start(self, ctx, m):
        ctx.vessel.auto_pilot.disengage()


class _Autopilot(AttitudeLaw):
    """Base for autopilot-steered laws: subclasses return (reference_frame, direction)."""

    uses_autopilot = True

    def __init__(self, roll: float | None = None, tuning: dict | None = None):
        self.roll = roll
        self.tuning = tuning or {}
        self._frame = None

    def target(self, ctx: Ctx, m: dict):  # -> (frame, vector) or None to leave as is
        raise NotImplementedError

    def start(self, ctx, m):
        ap = ctx.vessel.auto_pilot
        ctx.vessel.control.sas = False
        ap.target_roll = float(self.roll) if self.roll is not None else _NAN
        for key in ("stopping_time", "deceleration_time", "attenuation_angle"):
            if key in self.tuning:
                setattr(ap, key, tuple(self.tuning[key]))
        if "roll_threshold" in self.tuning:
            ap.roll_threshold = float(self.tuning["roll_threshold"])
        self._frame = None
        self.update(ctx, m)
        ap.engage()

    def update(self, ctx, m):
        t = self.target(ctx, m)
        if t is None:
            return
        frame, direction = t
        ap = ctx.vessel.auto_pilot
        if self._frame is None or not (frame == self._frame):
            ap.reference_frame = frame
            self._frame = frame
        ap.target_direction = direction

    def finish(self, ctx):
        return f"autopilot holding: {self.describe}"


class HoldPitchHeading(_Autopilot):
    def __init__(self, pitch: float, heading: float, roll: float | None = None, tuning: dict | None = None):
        super().__init__(roll, tuning)
        self.pitch, self.heading = float(pitch), float(heading)
        self.describe = f"pitch {self.pitch:g}° heading {self.heading:g}°"

    def target(self, ctx, m):
        return ctx.srf, _pitch_heading_vector(self.pitch, self.heading)


def _pitch_heading_vector(pitch: float, heading: float) -> vec.Vec:
    """Direction in the vessel surface frame (x up, y north, z east)."""
    p, h = math.radians(pitch), math.radians(heading)
    return (math.sin(p), math.cos(p) * math.cos(h), math.cos(p) * math.sin(h))


class PitchProgram(_Autopilot):
    """Pitch interpolated from an AI-supplied table against altitude, speed, or apoapsis."""

    KEYS = {"altitude": "altitude", "surface_altitude": "surface_altitude", "speed": "surface_speed",
            "surface_speed": "surface_speed", "orbital_speed": "orbital_speed", "apoapsis": "apoapsis_altitude",
            "apoapsis_altitude": "apoapsis_altitude", "met": "met"}

    def __init__(self, points: list, heading: float, by: str = "altitude", roll: float | None = None,
                 tuning: dict | None = None):
        super().__init__(roll, tuning)
        if by not in self.KEYS:
            raise AstraError(f"pitch_program 'by' must be one of {sorted(self.KEYS)}")
        pts = sorted((float(x), float(p)) for x, p in points)
        if len(pts) < 2:
            raise AstraError("pitch_program needs at least two [x, pitch_deg] points")
        self.points, self.heading, self.by = pts, float(heading), by
        self.describe = f"pitch program by {by} {pts} heading {self.heading:g}°"

    def pitch_at(self, x: float) -> float:
        pts = self.points
        if x <= pts[0][0]:
            return pts[0][1]
        for (x0, p0), (x1, p1) in zip(pts, pts[1:]):
            if x <= x1:
                return p0 + (p1 - p0) * (x - x0) / (x1 - x0)
        return pts[-1][1]

    def target(self, ctx, m):
        x = m.get(self.KEYS[self.by])
        if not _finite(x):
            return None
        return ctx.srf, _pitch_heading_vector(self.pitch_at(x), self.heading)


class FlightDirection(_Autopilot):
    """Point along a navball direction, recomputed every tick.

    surface prograde/retrograde use the body-fixed frame (velocity relative to the ground);
    orbital directions use the body's non-rotating frame. Optional pitch clamps keep a
    prograde-following ascent from falling below (or rising above) chosen elevations.
    """

    ORBITAL = {"prograde", "retrograde", "normal", "antinormal", "radial_out", "radial_in"}

    def __init__(self, kind: str, frame: str = "orbital", min_pitch: float | None = None,
                 max_pitch: float | None = None, roll: float | None = None, tuning: dict | None = None,
                 fallback_pitch: float | None = None, heading: float | None = None):
        super().__init__(roll, tuning)
        if kind not in self.ORBITAL:
            raise AstraError(f"unknown direction {kind!r}")
        if frame not in ("orbital", "surface"):
            raise AstraError("direction frame must be 'orbital' or 'surface'")
        if frame == "surface" and kind not in ("prograde", "retrograde"):
            raise AstraError("surface frame only supports prograde/retrograde")
        self.kind, self.frame = kind, frame
        self.min_pitch, self.max_pitch = min_pitch, max_pitch
        self.fallback_pitch, self.heading = fallback_pitch, heading
        clamp = ""
        if min_pitch is not None or max_pitch is not None:
            clamp = f" pitch in [{min_pitch}, {max_pitch}]"
        self.describe = f"{frame} {kind}{clamp}"

    def target(self, ctx, m):
        v = ctx.vessel
        if self.frame == "surface":
            ref = ctx.brf
            vel = ctx.stream("vel_brf", v.velocity, ref)
            pos = ctx.stream("pos_brf", v.position, ref)
            up = vec.unit(pos)
            if vec.norm(vel) < 1.0:  # no meaningful velocity yet (on the pad): hold fallback pitch
                if self.fallback_pitch is None:
                    return None
                return ctx.srf, _pitch_heading_vector(self.fallback_pitch, self.heading or 90.0)
            d = vec.unit(vel) if self.kind == "prograde" else vec.scale(vec.unit(vel), -1.0)
            d = vec.tilt_toward(d, up, self.min_pitch, self.max_pitch)
            return ref, d
        ref = ctx.nrf
        fl = _flight(ctx, ref)
        if self.kind in ("prograde", "retrograde", "normal", "antinormal"):
            attr = {"prograde": "prograde", "retrograde": "retrograde", "normal": "normal",
                    "antinormal": "anti_normal"}[self.kind]
            d = ctx.stream("fl_" + attr, getattr, fl, attr)
        else:
            radial = ctx.stream("fl_radial", getattr, fl, "radial")
            pos = ctx.stream("pos_nrf", v.position, ref)
            out = radial if vec.dot(radial, pos) > 0 else vec.scale(radial, -1.0)
            d = out if self.kind == "radial_out" else vec.scale(out, -1.0)
        if self.min_pitch is not None or self.max_pitch is not None:
            pos = ctx.stream("pos_nrf", v.position, ref)
            d = vec.tilt_toward(d, vec.unit(pos), self.min_pitch, self.max_pitch)
        return ref, d


def _flight(ctx: Ctx, ref):
    key = "_flight_nrf"
    fl = getattr(ctx, key, None)
    if fl is None or getattr(ctx, "_flight_owner", None) != ctx._owner:
        fl = ctx.vessel.flight(ref)
        setattr(ctx, key, fl)
        setattr(ctx, "_flight_owner", ctx._owner)
    return fl


class LocalUp(_Autopilot):
    describe = "local up"

    def target(self, ctx, m):
        return ctx.brf, vec.unit(ctx.stream("pos_brf", ctx.vessel.position, ctx.brf))


class TargetDirection(_Autopilot):
    def __init__(self, anti: bool = False, roll: float | None = None, tuning: dict | None = None):
        super().__init__(roll, tuning)
        self.anti = anti
        self.describe = "anti-target" if anti else "toward target"

    def target(self, ctx, m):
        sc = ctx.sc
        tgt = sc.target_docking_port or sc.target_vessel or sc.target_body
        if tgt is None:
            raise AstraError("no target set", "use target_set first")
        d = vec.sub(tgt.position(ctx.nrf), ctx.vessel.position(ctx.nrf))
        d = vec.unit(d)
        return ctx.nrf, vec.scale(d, -1.0) if self.anti else d


class NodeDirection(_Autopilot):
    """Point along the remaining burn vector of maneuver node `index`."""

    def __init__(self, index: int = 0, freeze_below_mps: float = 0.0, roll: float | None = None,
                 tuning: dict | None = None):
        super().__init__(roll, tuning)
        self.index, self.freeze_below = index, freeze_below_mps
        self.describe = f"maneuver node {index} burn vector"
        self._last: vec.Vec | None = None

    def target(self, ctx, m):
        nodes = ctx.vessel.control.nodes
        if len(nodes) <= self.index:
            return None if self._last is None else (ctx.nrf, self._last)
        node = nodes[self.index]
        try:
            rem = node.remaining_burn_vector(ctx.nrf)
        except Exception:  # noqa: BLE001 — null burn vector right after an SOI change
            rem = node.burn_vector(ctx.nrf)
        mag = vec.norm(rem)
        if self._last is not None and mag < self.freeze_below:
            return ctx.nrf, self._last  # near the end the vector swings; hold the last good one
        if mag < 1e-6:
            return None
        self._last = vec.unit(rem)
        return ctx.nrf, self._last


class VectorDirection(_Autopilot):
    FRAMES = ("surface", "orbital", "body", "inertial")

    def __init__(self, vector: list, frame: str, roll: float | None = None, tuning: dict | None = None):
        super().__init__(roll, tuning)
        if frame not in self.FRAMES:
            raise AstraError(f"vector frame must be one of {self.FRAMES}",
                             "surface: x up, y north, z east; orbital: x anti-radial (per kRPC docs; use radial_out/radial_in modes when the sign matters), y prograde, z normal "
                             "(kRPC vessel.orbital_reference_frame); body: rotating body frame; inertial: "
                             "body non-rotating frame")
        self.v = vec.unit(tuple(float(x) for x in vector))  # type: ignore[arg-type]
        self.frame = frame
        self.describe = f"vector {self.v} in {frame} frame"

    def target(self, ctx, m):
        v = ctx.vessel
        ref = {"surface": ctx.srf, "orbital": v.orbital_reference_frame,
               "body": ctx.brf, "inertial": ctx.nrf}[self.frame]
        return ref, self.v


class SasMode(AttitudeLaw):
    def __init__(self, mode: str):
        self.mode = mode
        self.describe = f"SAS {mode}"

    def start(self, ctx, m):
        v = ctx.vessel
        v.auto_pilot.disengage()
        v.control.sas = True
        enum = ctx.sc.SASMode
        try:
            v.control.sas_mode = getattr(enum, self.mode)
        except Exception as exc:  # noqa: BLE001
            raise AstraError(f"cannot set SAS mode {self.mode!r}: {exc}",
                             "basic probe cores only support stability_assist; use an autopilot mode "
                             "(prograde/retrograde/...) instead") from exc

    def finish(self, ctx):
        return f"SAS holding {self.mode}"


def parse_attitude(spec: Any) -> AttitudeLaw:
    if spec is None or spec == "keep":
        return KeepAttitude()
    if spec in ("free", "off"):
        return FreeAttitude()
    if isinstance(spec, str):
        spec = {"mode": spec}
    if not isinstance(spec, dict):
        raise AstraError(f"cannot read attitude spec {spec!r}")
    mode = spec.get("mode")
    roll, tuning = spec.get("roll"), spec.get("tuning")
    if mode in ("keep", None):
        return KeepAttitude()
    if mode in ("free", "off"):
        return FreeAttitude()
    if mode == "hold":
        return HoldPitchHeading(spec["pitch"], spec["heading"], roll, tuning)
    if mode == "pitch_program":
        return PitchProgram(spec["points"], spec["heading"], spec.get("by", "altitude"), roll, tuning)
    if mode in FlightDirection.ORBITAL:
        return FlightDirection(mode, spec.get("frame", "orbital"), spec.get("min_pitch"), spec.get("max_pitch"),
                               roll, tuning, spec.get("fallback_pitch"), spec.get("heading"))
    if mode == "up":
        return LocalUp(roll, tuning)
    if mode in ("target", "anti_target"):
        return TargetDirection(mode == "anti_target", roll, tuning)
    if mode == "node":
        return NodeDirection(int(spec.get("index", 0)), float(spec.get("freeze_below_mps", 0.0)), roll, tuning)
    if mode == "vector":
        return VectorDirection(spec["vector"], spec.get("frame", "inertial"), roll, tuning)
    if mode == "sas":
        return SasMode(spec["sas_mode"])
    raise AstraError(f"unknown attitude mode {mode!r}",
                     "modes: hold, pitch_program, prograde, retrograde, normal, antinormal, radial_out, "
                     "radial_in, up, target, anti_target, node, vector, sas, free, keep")
