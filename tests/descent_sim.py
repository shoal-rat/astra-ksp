"""Closed-loop landing simulator for the descent reflex (offline; no game).

A point-mass lander in the equatorial plane of a spherical, airless, non-rotating body with terrain
given as a function of longitude, flying the real DescentThrottle and DescentAttitude laws (and the
DescentGuidance they share) at the reflex engine's ~20 Hz and in the engine's order: the laws read
the same metrics the telemetry probe provides, the attitude law steers a stub autopilot, and the
thrust acts along the vessel's actual pointing. By default the pointing follows the autopilot target
at once; with `Lander.slew_dps` it turns toward it at a limited rate, starting `pointing_off_deg`
away from surface retrograde. Mass drops with propellant flow. At ground contact (the lowest point of
the vessel reaches the terrain) the contact speeds are recorded, the lander is clamped to the ground
with the situation 'landed', and the laws keep running until the throttle law finishes (the
post-contact settle) or the time limit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from astra.reflex import vec
from astra.reflex.descent import DescentAttitude, DescentGuidance, DescentThrottle

G0 = 9.80665


@dataclass
class Body:
    radius_m: float = 200_000.0
    surface_g: float = 1.63
    terrain: Callable[[float], float] = lambda lon_deg: 0.0  # m above the datum at a longitude

    @property
    def mu(self) -> float:
        return self.surface_g * self.radius_m ** 2

    @property
    def equatorial_radius(self) -> float:  # kRPC always provides it; the predictor's centrifugal relief uses it
        return self.radius_m

    def surface_height(self, lat: float, lon: float) -> float:
        return self.terrain(lon)


@dataclass
class Lander:
    mass_kg: float
    thrust_n: float
    isp_s: float = 345.0
    bottom_m: float = -3.0  # lowest point below the centre of mass when upright
    propellant_kg: float = 4000.0
    slew_dps: float | None = None  # attitude turn rate limit (deg/s); None: the pointing follows the autopilot


@dataclass
class Result:
    contact: bool
    vertical_mps: float
    horizontal_mps: float
    time_s: float
    propellant_used_kg: float
    min_clearance_m: float  # lowest clearance before contact
    phase_log: list[tuple[float, str]] = field(default_factory=list)
    touchdown_reported: dict | None = None
    done_s: float | None = None  # game time at which the throttle law finished (None: it never did)
    done_detail: str | None = None
    throttle_after_contact: float = 0.0  # largest throttle commanded after contact
    wrong_way_throttle_s: float = 0.0  # throttle x seconds while pointed more than 90 deg off the command
    autopilot_engaged: bool = False  # at the end of the run
    sas: bool = False
    legs_h_m: float | None = None  # height of the lowest point when the legs were deployed
    attitude_report: str = ""  # DescentAttitude.finish

    @property
    def soft(self) -> bool:
        return self.contact and abs(self.vertical_mps) <= 2.5 and self.horizontal_mps <= 1.0


class _AutoPilot:
    def __init__(self):
        self.engaged = False
        self.disengaged = 0
        self.reference_frame = None
        self.target_roll = None
        self.target_direction: vec.Vec | None = None

    def engage(self):
        self.engaged = True

    def disengage(self):
        self.engaged = False
        self.disengaged += 1


class _Control:
    def __init__(self):
        self.legs = False
        self.sas = False
        self.throttle = 0.0


class _Vessel:
    def __init__(self):
        self.control = _Control()
        self.auto_pilot = _AutoPilot()
        self.position = self.velocity = self.direction = None  # read through _Ctx.stream

        class _Parts:
            all: list = []
        self.parts = _Parts()


class _Ctx:
    """What the descent laws read from the reflex context."""

    def __init__(self, sim: "Sim"):
        self.sim = sim
        self.vessel = _Vessel()
        self.brf = self.srf = None
        self.body = sim.body

    def stream(self, key, fn, *args):
        # 2D (x, z) equatorial plane -> kRPC body frame (x toward lon 0, y north, z toward lon 90 E)
        if key == "pos_brf":
            return (self.sim.pos[0], 0.0, self.sim.pos[1])
        if key == "vel_brf":
            return (self.sim.vel[0], 0.0, self.sim.vel[1])
        if key == "dir_brf":
            return (math.cos(self.sim.pointing), 0.0, math.sin(self.sim.pointing))
        raise KeyError(key)


def _wrap(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi


class Sim:
    def __init__(self, body: Body, lander: Lander, *, altitude_m: float, lon_deg: float,
                 horizontal_mps: float, vertical_mps: float, retrograde: bool = True,
                 pointing_off_deg: float = 0.0):
        self.body, self.lander = body, lander
        r = body.radius_m + altitude_m
        a = math.radians(lon_deg)
        up = (math.cos(a), math.sin(a))
        # prograde = increasing longitude; retrograde orbits move toward decreasing longitude
        east = (-math.sin(a), math.cos(a))
        sgn = -1.0 if retrograde else 1.0
        self.pos = (r * up[0], r * up[1])
        self.vel = (vertical_mps * up[0] + sgn * horizontal_mps * east[0],
                    vertical_mps * up[1] + sgn * horizontal_mps * east[1])
        # the vessel's forward axis (angle in the x-z plane), pointing_off_deg from surface retrograde
        retro = math.atan2(-self.vel[1], -self.vel[0]) if math.hypot(*self.vel) > 1e-6 else a
        self.pointing = retro + math.radians(pointing_off_deg)
        self.mass = lander.mass_kg
        self.propellant = lander.propellant_kg
        self.situation = "flying"
        self.t = 0.0  # game time: the guidance's 1 s cadences and timers run on metrics['ut']

    # -- geometry ------------------------------------------------------------------------------
    def lon(self) -> float:
        return math.degrees(math.atan2(self.pos[1], self.pos[0]))

    def metrics(self) -> dict:
        r = math.hypot(*self.pos)
        up = (self.pos[0] / r, self.pos[1] / r)
        vs = self.vel[0] * up[0] + self.vel[1] * up[1]
        hvec = (self.vel[0] - vs * up[0], self.vel[1] - vs * up[1])
        hs = math.hypot(*hvec)
        alt = r - self.body.radius_m
        return {"altitude": alt, "surface_altitude": alt - self.body.terrain(self.lon()),
                "vertical_speed": vs, "horizontal_speed": hs, "surface_speed": math.hypot(*self.vel),
                "local_g": self.body.mu / r ** 2, "mass": self.mass,
                "available_thrust": self.lander.thrust_n if self.propellant > 0 else 0.0,
                "situation": self.situation, "periapsis_altitude": -1.0, "ut": self.t}

    # -- dynamics ------------------------------------------------------------------------------
    def _slew(self, target: vec.Vec | None, dt: float) -> None:
        if target is None or vec.norm(target) < 1e-9:
            return
        want = math.atan2(target[2], target[0])
        if self.lander.slew_dps is None:
            self.pointing = want
            return
        step = math.radians(self.lander.slew_dps) * dt
        self.pointing += max(-step, min(step, _wrap(want - self.pointing)))

    def _advance(self, thr: float, dt: float) -> None:
        thrust = max(0.0, min(1.0, thr)) * (self.lander.thrust_n if self.propellant > 0 else 0.0)
        r = math.hypot(*self.pos)
        ax = -self.body.mu * self.pos[0] / r ** 3 + thrust / self.mass * math.cos(self.pointing)
        az = -self.body.mu * self.pos[1] / r ** 3 + thrust / self.mass * math.sin(self.pointing)
        self.vel = (self.vel[0] + ax * dt, self.vel[1] + az * dt)
        self.pos = (self.pos[0] + self.vel[0] * dt, self.pos[1] + self.vel[1] * dt)
        burn = min(thrust / (self.lander.isp_s * G0) * dt, self.propellant)
        self.propellant -= burn
        self.mass -= burn

    def _clamp_to_ground(self) -> None:
        a = math.atan2(self.pos[1], self.pos[0])
        r = self.body.radius_m + self.body.terrain(self.lon()) - self.lander.bottom_m
        self.pos = (r * math.cos(a), r * math.sin(a))
        self.vel = (0.0, 0.0)

    # -- run -----------------------------------------------------------------------------------
    def run(self, guide: DescentGuidance, *, dt: float = 0.05, t_max: float = 900.0) -> Result:
        """Fly the descent laws the way engine.fly does: start both laws and set the first throttle
        before the game runs; then every tick read the metrics, update attitude, then throttle, and
        stop when the throttle law reports it is done."""
        ctx = _Ctx(self)
        ap, control = ctx.vessel.auto_pilot, ctx.vessel.control
        guide.bottom = self.lander.bottom_m
        guide._bottom_at = math.inf  # the simulator's geometry is fixed
        tlaw = DescentThrottle(guide)
        alaw = DescentAttitude(tlaw)
        m = self.metrics()
        tlaw.start(ctx, m)
        alaw.start(ctx, m)
        thr = tlaw.update(ctx, m)
        prop0 = self.propellant
        res = Result(False, m["vertical_speed"], m["horizontal_speed"], 0.0, 0.0, math.inf,
                     [(0.0, guide.phase)])
        t = 0.0
        while t < t_max:
            if not res.contact:  # the game runs one tick under the commanded throttle and attitude
                if ap.engaged:
                    self._slew(ap.target_direction, dt)
                self._advance(thr, dt)
            t += dt
            self.t = t
            m = self.metrics()
            clearance = m["surface_altitude"] + self.lander.bottom_m
            if not res.contact and clearance <= 0.0:
                # contact: the game reports 'landed' with the speeds of the arrival, then the legs hold it
                res.contact, res.vertical_mps, res.horizontal_mps, res.time_s = \
                    True, m["vertical_speed"], m["horizontal_speed"], t
                res.propellant_used_kg = prop0 - self.propellant
                self.situation = m["situation"] = "landed"
                self._clamp_to_ground()
            elif not res.contact:
                res.min_clearance_m = min(res.min_clearance_m, clearance)
            legs_before = control.legs
            alaw.update(ctx, m)
            thr = tlaw.update(ctx, m)
            if control.legs and not legs_before:
                res.legs_h_m = clearance
            if res.phase_log[-1][1] != guide.phase:
                res.phase_log.append((round(t, 2), guide.phase))
            if res.contact:
                res.throttle_after_contact = max(res.throttle_after_contact, thr)
            elif thr > 0.0 and vec.angle_deg(ctx.stream("dir_brf", None), tlaw.commanded(ctx)) > 90.0:
                res.wrong_way_throttle_s += thr * dt
            done = tlaw.done(ctx, m)
            if done:
                res.done_s, res.done_detail = t, done
                break
        if not res.contact:
            m = self.metrics()
            res.vertical_mps, res.horizontal_mps, res.time_s = m["vertical_speed"], m["horizontal_speed"], t
            res.propellant_used_kg = prop0 - self.propellant
        res.touchdown_reported = guide.touchdown
        res.attitude_report = alaw.finish(ctx)
        res.autopilot_engaged, res.sas = ap.engaged, control.sas
        return res


def guidance(**overrides) -> DescentGuidance:
    """The guidance with the parameters the Mun showcase flights used, overridable."""
    args = dict(touchdown_mps=1.5, reserve=0.2, terminal_alt_m=150.0, terminal_rate=0.15, max_tilt_deg=30.0,
                tilt_gain_deg_per_mps=10.0, sink_gain=0.8, legs_alt_m=None, drift_max_mps=0.5)
    args.update(overrides)
    reserve = args.pop("reserve")
    return DescentGuidance(args.pop("touchdown_mps"), reserve, args.pop("terminal_alt_m"), args.pop("terminal_rate"),
                           args.pop("max_tilt_deg"), args.pop("tilt_gain_deg_per_mps"), args.pop("sink_gain"),
                           args.pop("legs_alt_m"), drift_max_mps=args.pop("drift_max_mps"))
