"""Powered descent to touchdown: the astronaut's landing reflex. The AI sets every parameter.

Braking phase: every tick a suicide-burn predictor integrates a surface-retrograde burn at the
usable acceleration (1 - reserve) * a_max under gravity (minus the centrifugal relief v^2/r of the
horizontal speed) until the speed is down to the terminal speed, and reports the height that burn
would lose. Coast engine-off while the predicted stop is above `terminal_alt_m` over the *terrain
floor*; then brake surface-retrograde with the lowest throttle whose predicted stop still holds that
gate (bisection), so the burn tracks a stop at the gate with the reserve kept for errors. When even
a full-thrust retrograde burn cannot hold the gate (late or steep hand-off, low thrust), or when the
gate is reached with more horizontal speed than the terminal law can cancel, the brake flies
height-first: the vertical thrust component tracks a stoppable sink toward a hold height and the
rest of the thrust kills horizontal speed. The terrain floor is the highest ground under the path
ahead, sampled every game second along the great circle of the horizontal velocity out past the
predicted stopping point; peaks already seen are remembered until the lander has passed them.
Terminal phase (below terminal_alt_m over that floor, horizontal speed small): hold a sink rate
proportional to the height above the ground below (never faster than a terminal speed the engine can
stop, never slower than the touchdown speed), tilting up to `max_tilt_deg` against horizontal drift
without giving up the vertical thrust it needs. A few metres up, while the drift is still above
`drift_max`, it holds height above the ground (hover) until the drift is cancelled, then sets down
upright: a lander that touches down sliding sideways on a slope lands on one leg and can tip over.
Heights are measured from the vessel's lowest point in its current attitude (re-measured every
second, so it is right once the lander is upright and its legs are out), not its centre of mass.
Braking thrust is gated on pointing: until the vessel's forward axis first comes within
ALIGN_FULL_DEG of the braking direction, the brake fades to nothing by ALIGN_ZERO_DEG (a lander
handed over in its deorbit attitude would otherwise burn prograde); the terminal phase is never
gated. After contact the engine is off, the laws run `settle_s` more game seconds, and the attitude
is handed from the autopilot to SAS. All cadences and timers run on game time (metric `ut`).
"""

from __future__ import annotations

import math
import time

from astra.errors import AstraError
from astra.reflex import vec
from astra.reflex.context import Ctx
from astra.reflex.laws import AttitudeLaw, ThrottleLaw
from astra.telemetry import vessel_box

LOOKAHEAD_SAMPLES = 16
PEAK_MEMORY = 64  # remembered terrain peaks ahead (the floor forgets a peak once it is passed)
PREDICT_DT_S = 0.25
PREDICT_MAX_S = 3600.0  # safety cap; hopeless integrations end on the height budget instead
PREDICT_COARSE_AFTER_S = 180.0  # after this much burn time the predictor steps 1 s at a time
ALIGN_FULL_DEG = 10.0  # until the vessel first points this close to the braking command, the brake...
ALIGN_ZERO_DEG = 45.0  # ...fades from full at ALIGN_FULL_DEG to none at this pointing error
WRONG_WAY_DEG = (90.0, 135.0)  # once aligned, only thrust aimed against the command fades (full, none)


def _f(x) -> bool:
    return isinstance(x, (int, float)) and not math.isnan(x) and not math.isinf(x)


def stop_prediction(speed: float, vs: float, hs: float, g: float, accel: float, v_end: float,
                    radius_m: float = math.inf, max_drop: float = math.inf) -> tuple[float, float, float]:
    """Integrate a surface-retrograde burn at constant `accel` (m/s²) from the current velocity
    (vertical `vs`, up positive; horizontal `hs`) under gravity `g` less the centrifugal relief of the
    horizontal speed (hs²/radius), until the speed is down to `v_end`.

    Returns (height lost m, ground distance m, time s); the height is inf if the burn loses more than
    `max_drop` or cannot bring the speed down within PREDICT_MAX_S (thrust too weak against gravity)."""
    vx, vz = max(0.0, hs), vs
    if math.hypot(vx, vz) <= v_end:
        return 0.0, 0.0, 0.0
    if accel <= 0:
        return math.inf, 0.0, 0.0
    x = z = t = 0.0
    while t < PREDICT_MAX_S:
        v = math.hypot(vx, vz)
        if v <= v_end:
            return -z, x, t
        if -z > max_drop:
            return math.inf, x, t
        dt = PREDICT_DT_S if t < PREDICT_COARSE_AFTER_S else 1.0
        dt = min(dt, 0.5 * v / accel)  # keep the explicit step stable near the end speed
        k = accel / v
        vx = max(0.0, vx - k * vx * dt)
        vz += (-k * vz - g + vx * vx / radius_m) * dt
        x += vx * dt
        z += vz * dt
        t += dt
    return math.inf, x, t


def ground_track_points(pos: vec.Vec, horiz: vec.Vec, distance_m: float, n: int,
                        radius_m: float | None = None) -> list[tuple[float, float]]:
    """(lat, lon) in degrees of n+1 points from `pos` along the great circle toward the horizontal
    velocity `horiz`, out to `distance_m` over the ground (measured on a sphere of `radius_m`, the
    body radius; default |pos|). Body-fixed kRPC frame: x toward lat 0 / lon 0, y toward the north
    pole, z toward lat 0 / lon 90 E."""
    r = vec.norm(pos)
    ground_r = radius_m if radius_m and math.isfinite(radius_m) else r
    h = vec.norm(horiz)
    out = []
    for i in range(n + 1):
        theta = distance_m * i / n / ground_r if h > 1e-6 else 0.0
        q = vec.add(vec.scale(pos, math.cos(theta)), vec.scale(horiz, r * math.sin(theta) / max(h, 1e-9)))
        rq = vec.norm(q)
        out.append((math.degrees(math.asin(max(-1.0, min(1.0, q[1] / rq)))), math.degrees(math.atan2(q[2], q[0]))))
    return out


def _unit_of(lat: float, lon: float) -> vec.Vec:
    la, lo = math.radians(lat), math.radians(lon)
    return (math.cos(la) * math.cos(lo), math.sin(la), math.cos(la) * math.sin(lo))


class DescentGuidance:
    """Shared state for the descent throttle and attitude laws."""

    def __init__(self, touchdown_mps: float, reserve: float, terminal_alt_m: float, terminal_rate: float,
                 max_tilt_deg: float, tilt_gain_deg_per_mps: float, sink_gain: float, legs_alt_m: float | None,
                 drift_max_mps: float):
        if touchdown_mps <= 0 or terminal_alt_m <= 0 or drift_max_mps <= 0:
            raise AstraError("touchdown_mps, terminal_alt_m and touchdown_drift_mps must be positive")
        if not 0 <= reserve < 0.9:
            raise AstraError("throttle_reserve must be in [0, 0.9)")
        if max_tilt_deg <= 0 or tilt_gain_deg_per_mps <= 0:
            raise AstraError("max_tilt_deg and tilt_gain_deg_per_mps must be positive",
                             "without tilt the terminal phase can never cancel drift and would hover until "
                             "the tank is empty")
        self.vt, self.reserve, self.h_term = touchdown_mps, reserve, terminal_alt_m
        self.rate, self.max_tilt, self.k_tilt = terminal_rate, max_tilt_deg, tilt_gain_deg_per_mps
        self.k_sink, self.legs_alt = sink_gain, legs_alt_m
        self.drift_max = drift_max_mps
        # hold height here until the drift is cancelled; at least the distance the sink loop needs to
        # arrest a touchdown-speed sink (vt / sink_gain) plus 2 m
        self.hover_h = max(3.0, 2.0 * touchdown_mps, touchdown_mps / sink_gain + 2.0)
        self.hold_h = max(self.hover_h + 2.0, 0.25 * terminal_alt_m)  # height-first brake levels off here
        self.flare_h = 1.5  # below this, stand up straight for contact
        self.hovered_s = 0.0
        self.brake_held_s = 0.0  # game seconds the brake was held back (weighted by how much) while turning
        self._hover_at: float | None = None
        self.bottom = 0.0
        self._bottom_at = float("-inf")
        self.floor: float | None = None  # highest terrain ahead, m above the datum
        self.floor_ahead_m = 0.0
        self._peaks: list[tuple[vec.Vec, float]] = []  # remembered peaks ahead: (unit vector, height)
        self.stop_ahead_m = 0.0  # predicted ground distance of the braking burn
        self.radius_m = math.inf  # body radius (centrifugal relief in the predictor)
        self._floor_at = float("-inf")
        self.phase = "coast"
        self.height_first = False  # braking height-first (the retrograde burn cannot hold the gate)
        self.legs_done = legs_alt_m is None
        self.touchdown: dict | None = None
        self.touchdown_at: float | None = None  # game time of contact; the lander settles after it
        self.settle_s = 4.0
        self.v_term_max = max(touchdown_mps, terminal_rate * terminal_alt_m)
        self.v_term = self.v_term_max
        self._terr: tuple[float, float] | None = None  # (terrain height below, game time)
        self.terr_rate = 0.0  # rate the ground below rises under the lander, m/s

    @staticmethod
    def _now(m: dict) -> float:
        ut = m.get("ut")
        return ut if _f(ut) else time.monotonic()

    def _measure_bottom(self, ctx: Ctx, now: float) -> None:
        """Lowest point below the CoM along local up, in the current attitude (surface frame x is up)."""
        if now - self._bottom_at < 1.0:
            return
        self._bottom_at = now
        try:
            box = vessel_box(ctx.vessel, ctx.srf)
        except Exception:  # noqa: BLE001 — keep the last measurement
            box = None
        if box is not None:
            self.bottom = min(0.0, box[0][0])

    def _measure_floor(self, ctx: Ctx, a_use: float, now: float) -> None:
        """Highest terrain under the path ahead, out past the predicted stopping point, together with
        every peak seen earlier that the lander has not passed yet."""
        if ctx is None or now - self._floor_at < 1.0:
            return
        self._floor_at = now
        try:
            pos = ctx.stream("pos_brf", ctx.vessel.position, ctx.brf)
            vel = ctx.stream("vel_brf", ctx.vessel.velocity, ctx.brf)
            up = vec.unit(pos)
            horiz = vec.sub(vel, vec.scale(up, vec.dot(vel, up)))
            hs = vec.norm(horiz)
            ahead = 200.0 + 1.3 * max(self.stop_ahead_m, hs * hs / (2.0 * max(a_use, 0.1)))
            points = ground_track_points(pos, horiz, ahead, LOOKAHEAD_SAMPLES, self.radius_m)
            heights = [ctx.body.surface_height(lat, lon) for lat, lon in points]
            i = max(range(len(heights)), key=heights.__getitem__)
            self._peaks.append((_unit_of(*points[i]), heights[i]))
            if hs > 1e-3:
                hhat = vec.scale(horiz, 1.0 / hs)
                r = vec.norm(pos)
                # forget a peak once passed, or once it lies far beyond any stop the lander can still make
                self._peaks = [(u, z) for u, z in self._peaks if -20.0 <= vec.dot(u, hhat) * r <= 2.0 * ahead]
            self._peaks = self._peaks[-PEAK_MEMORY:]
            self.floor = max(max(heights), max(z for _, z in self._peaks))
            self.floor_ahead_m = ahead
        except Exception:  # noqa: BLE001 — keep the last floor
            pass

    def _surface_g(self, m: dict) -> float:
        g = m["local_g"]
        if math.isfinite(self.radius_m) and _f(m.get("altitude")):
            g *= ((self.radius_m + m["altitude"]) / self.radius_m) ** 2
        return g

    def _cap_v_term(self, m: dict, a_max: float) -> None:
        """Terminal speed the sink profile can actually fly: following vs = -rate*h from v_term needs
        rate*v_term of net deceleration at the gate."""
        a_net = (1.0 - self.reserve) * a_max - self._surface_g(m)
        self.v_term = max(self.vt, min(self.v_term_max, a_net / self.rate)) if a_net > 0 else self.vt

    def _hs_handover(self, m: dict) -> float:
        """Largest horizontal speed the terminal tilt law can bring below drift_max within about one sink
        time constant (1/terminal_rate): constant deceleration g*tan(max_tilt) above the saturation drift
        max_tilt/tilt_gain, exponential decay at rate g*tilt_gain (per radian) below it."""
        g = self._surface_g(m)
        t_avail = 1.0 / self.rate
        d_sat = self.max_tilt / self.k_tilt
        lam = g * math.radians(self.k_tilt)  # 1/s, small-angle decay rate of the drift
        if d_sat <= self.drift_max:
            return max(self.drift_max, self.drift_max + t_avail * g * math.tan(math.radians(self.max_tilt)))
        t_lin = math.log(d_sat / self.drift_max) / lam
        if t_lin >= t_avail:
            return self.drift_max * math.exp(lam * t_avail)
        return d_sat + (t_avail - t_lin) * g * math.tan(math.radians(self.max_tilt))

    def start(self, ctx: Ctx, m: dict) -> None:
        self._measure_bottom(ctx, self._now(m))
        try:
            self.radius_m = float(ctx.body.equatorial_radius)
        except Exception:  # noqa: BLE001 — flat-ground prediction is the conservative fallback
            self.radius_m = math.inf
        a_max = m["available_thrust"] / m["mass"] if m.get("mass") else 0.0
        if a_max <= 0:
            raise AstraError("no thrust available for the descent", "stage or activate the landing engine first")
        g_s = self._surface_g(m)
        if (1 - self.reserve) * a_max <= g_s:
            raise AstraError(f"usable thrust {(1 - self.reserve) * a_max:.2f} m/s² cannot hold against surface "
                             f"gravity {g_s:.2f} m/s²", "lower the reserve, drop mass, or use a stronger engine")
        if m.get("situation") in ("orbiting", "escaping") and m.get("periapsis_altitude", 0) > 0:
            raise AstraError("the vessel is not on a descending trajectory (periapsis above the surface)",
                             "deorbit first (compute_maneuver kind=deorbit_to_periapsis), then call fly_descent")
        self._cap_v_term(m, a_max)

    def height(self, m: dict) -> float:
        """Height of the vessel's lowest point above the ground directly below."""
        return m["surface_altitude"] + self.bottom

    def gate_height(self, m: dict) -> float:
        """Height above the terrain floor ahead (or the ground below, if that is higher)."""
        h = self.height(m)
        if self.floor is not None and _f(m.get("altitude")):
            h = min(h, m["altitude"] - self.floor + self.bottom)
        return h

    def _brake_throttle(self, gate: float, speed: float, vs: float, hs: float, g: float, a_max: float,
                        radius: float) -> float | None:
        """Lowest throttle whose predicted retrograde stop still ends above the terminal gate; None if
        even full thrust cannot hold it."""
        budget = gate - self.h_term

        def holds(accel: float) -> bool:
            return stop_prediction(speed, vs, hs, g, accel, self.v_term, radius, budget + 1.0)[0] <= budget
        if not holds(a_max):
            return None
        lo, hi = 0.0, a_max
        for _ in range(10):
            mid = 0.5 * (lo + hi)
            if holds(mid):
                hi = mid
            else:
                lo = mid
        return max(0.0, min(1.0, hi / a_max))

    def _frame(self, ctx: Ctx) -> tuple[vec.Vec, vec.Vec]:
        up = vec.unit(ctx.stream("pos_brf", ctx.vessel.position, ctx.brf))
        vel = ctx.stream("vel_brf", ctx.vessel.velocity, ctx.brf)
        return up, vec.sub(vel, vec.scale(up, vec.dot(vel, up)))

    def _height_first(self, ctx: Ctx, m: dict, g: float, a_max: float, gate: float, h: float,
                      radius: float) -> tuple[float, vec.Vec]:
        """Full-authority brake when surface-retrograde cannot do the job: the vertical component tracks
        a sink the engine can stop at hold_h over the floor (and climbs back if below it over the ground
        below), the rest of the thrust kills horizontal speed."""
        vs, hs = m["vertical_speed"], m["horizontal_speed"]
        up, horiz = self._frame(ctx)
        g_eff = g - (hs * hs / radius if math.isfinite(radius) else 0.0)
        a_plan = 0.5 * max(a_max - self._surface_g(m), 0.1)  # plan the vertical stop on half the margin
        room_ahead, room_below = gate - self.hold_h, h - self.hold_h
        ff = 0.0
        if room_below < 0.0:
            vs_ref = min(self.v_term, 0.5 * -room_below)
        elif room_ahead <= 0.0:
            vs_ref = 0.0  # terrain ahead at or above the hold height: level off while braking
        else:
            s = math.sqrt(2.0 * a_plan * room_ahead)
            if gate <= self.h_term and s > self.v_term:
                vs_ref = -self.v_term
            else:
                vs_ref = -s
                ff = min(a_max, max(0.0, -a_plan * vs / max(s, 0.1)))  # the profile's own deceleration
        a_v = max(0.0, min(a_max, g_eff + ff + self.k_sink * (vs_ref - vs)))
        a_h = min(math.sqrt(max(0.0, a_max * a_max - a_v * a_v)), 2.0 * hs)
        self.stop_ahead_m = hs * hs / (2.0 * a_max)
        a = math.hypot(a_v, a_h)
        if a < 1e-6:
            return 0.0, up
        d = vec.scale(up, a_v)
        if hs > 1e-3:
            d = vec.add(d, vec.scale(horiz, -a_h / hs))
        return min(1.0, a / a_max), vec.unit(d)

    def step(self, ctx: Ctx, m: dict) -> tuple[float, vec.Vec | None]:
        """Return (throttle, desired direction in the body frame or None for surface-retrograde)."""
        now = self._now(m)
        self._measure_bottom(ctx, now)
        speed, vs, hs = m["surface_speed"], m["vertical_speed"], m["horizontal_speed"]
        g = m["local_g"]
        a_max = m["available_thrust"] / m["mass"] if m["mass"] else 0.0
        a_use = (1.0 - self.reserve) * a_max
        if a_max > 0:
            self._measure_floor(ctx, a_use, now)
        h = self.height(m)
        if not self.legs_done and h <= self.legs_alt:
            ctx.vessel.control.legs = True
            self.legs_done = True
        # Contact: the game's situation; the height test only catches a lander already at rest (a
        # commanded sink or a hover must not look like contact, or the engine is cut in the air).
        if self.phase == "down" or m.get("situation") in ("landed", "splashed") or \
                (h < 0.3 and abs(vs) < min(0.3, 0.5 * self.vt) and hs < 0.6 and self._hover_at is None):
            if self.touchdown is None:
                self.touchdown = {"vertical_mps": vs, "horizontal_mps": hs, "height_m": h}
                self.touchdown_at = now
            self.phase = "down"
            return 0.0, None
        if a_max <= 0:
            return 0.0, None
        gate = self.gate_height(m)
        radius = self.radius_m + m["altitude"] if _f(m.get("altitude")) else self.radius_m
        if self.phase == "terminal" and gate > 2.0 * self.h_term:
            self.phase = "coast"  # the ground fell away (or a far peak was forgotten): brake again from here
        if self.phase != "terminal":
            self._cap_v_term(m, a_max)
        if gate > self.h_term and self.phase != "terminal":
            # Above the terminal gate the only choices are coast or brake, whatever the vertical
            # speed (level or rising at apoapsis counts as a shallow path, not as the terminal phase).
            budget = gate - self.h_term
            drop, ground, _ = stop_prediction(speed, vs, hs, g, a_use, self.v_term, radius)
            self.stop_ahead_m = ground
            if self.phase == "coast" and drop < budget:
                return 0.0, None
            self.phase = "brake"
            if self.height_first and drop <= budget:
                self.height_first = False  # the reserve retrograde burn holds the gate again
            if not self.height_first:
                thr = self._brake_throttle(gate, speed, vs, hs, g, a_max, radius)
                if thr is not None:
                    # look ahead as far as the burn actually commanded reaches (a lower throttle stops later)
                    self.stop_ahead_m = max(ground, stop_prediction(speed, vs, hs, g, thr * a_max, self.v_term,
                                                                    radius, budget + 1.0)[1])
                    return thr, None
                self.height_first = True
            return self._height_first(ctx, m, g, a_max, gate, h, radius)
        if self.phase != "terminal" and (hs > self._hs_handover(m) or speed > 1.2 * self.v_term):
            # Below the gate but still too fast (a late or low hand-off, a floor that jumped, drift left
            # by the retrograde burn): the terminal law has only ~g*tan(max_tilt) of horizontal
            # authority, so keep braking height-first until the terminal law can finish the job.
            self.phase = "brake"
            self.height_first = True
            return self._height_first(ctx, m, g, a_max, gate, h, radius)
        # Terminal: sink rate proportional to the height above the ground below (capped at the
        # terminal speed), tilt against horizontal drift, heights and rates relative to the ground.
        self.phase = "terminal"
        up, horiz = self._frame(ctx)
        drift = vec.norm(horiz)
        if _f(m.get("altitude")) and _f(m.get("surface_altitude")):
            terr = m["altitude"] - m["surface_altitude"]
            if self._terr is not None and now - self._terr[1] > 1e-3:
                dt = now - self._terr[1]
                self.terr_rate += min(1.0, dt / 0.5) * ((terr - self._terr[0]) / dt - self.terr_rate)
            self._terr = (terr, now)
        vs_rel = vs - self.terr_rate  # rate of change of the height above the ground below
        tilt = min(self.max_tilt, self.k_tilt * drift)
        hover = h < self.hover_h and drift > self.drift_max
        if hover and self._hover_at is not None:
            self.hovered_s += min(1.0, now - self._hover_at)
        self._hover_at = now if hover else None
        if not hover and h < self.flare_h:
            tilt = min(tilt, 3.0)  # set down upright
        if hover:  # hold the hover height above the ground, climbing back if the ground rises
            vs_des, a_ff = max(-self.vt, min(self.vt, 0.5 * (self.hover_h - h))), 0.0
        else:
            vs_des = -min(self.v_term, max(self.vt, self.rate * max(h, 0.0)))
            # on the proportional segment the target itself decelerates at rate*|vs|: feed it forward
            a_ff = -self.rate * vs_rel if self.vt < self.rate * h < self.v_term else 0.0
        a_v = g + a_ff + self.k_sink * (vs_des - vs_rel)
        # vertical first: never tilt away thrust the sink command needs
        if a_v >= a_max:
            tilt = 0.0
        elif a_v > 0:
            tilt = min(tilt, math.degrees(math.acos(a_v / a_max)))
        direction = up
        if drift > 1e-3 and tilt > 0:
            r = math.radians(tilt)
            direction = vec.add(vec.scale(up, math.cos(r)), vec.scale(vec.unit(horiz), -math.sin(r)))
        thr = a_v / (a_max * math.cos(math.radians(tilt)))
        return max(0.0, min(1.0, thr)), direction


def pointing_gate(err_deg: float, aligned: bool) -> float:
    """Fraction of the braking throttle allowed at a pointing error (deg) between the vessel's forward
    axis and the commanded direction. Before the vessel has first come within ALIGN_FULL_DEG of the
    command in this braking phase, the brake fades from full there to none at ALIGN_ZERO_DEG. After
    that only thrust aimed against the command (WRONG_WAY_DEG) is held back: a lander lagging a
    turning command still brakes usefully, and starving it then crashed aligned landers in the
    slew-limited simulator."""
    full, zero = WRONG_WAY_DEG if aligned else (ALIGN_FULL_DEG, ALIGN_ZERO_DEG)
    if err_deg <= full:
        return 1.0
    if err_deg >= zero:
        return 0.0
    return (zero - err_deg) / (zero - full)


class DescentThrottle(ThrottleLaw):
    finishes = True

    def __init__(self, guide: DescentGuidance):
        self.g = guide
        self.describe = (f"powered descent (touchdown {guide.vt:g} m/s, reserve {guide.reserve:.0%}, "
                         f"terminal below {guide.h_term:g} m)")
        self._dir: vec.Vec | None = None
        self._aligned = False  # pointed at the braking command since this braking phase began
        self._last_now: float | None = None
        self._last_gate = 1.0

    def start(self, ctx, m):
        self.g.start(ctx, m)

    def commanded(self, ctx: Ctx) -> vec.Vec:
        """The direction the attitude law steers toward: the guidance's, else surface retrograde (up
        when nearly at rest)."""
        if self._dir is not None:
            return self._dir
        vel = ctx.stream("vel_brf", ctx.vessel.velocity, ctx.brf)
        if vec.norm(vel) < 0.5:
            return vec.unit(ctx.stream("pos_brf", ctx.vessel.position, ctx.brf))
        return vec.scale(vec.unit(vel), -1.0)

    def update(self, ctx, m):
        thr, self._dir = self.g.step(ctx, m)
        now = self.g._now(m)
        if self._last_now is not None and self._last_gate < 1.0:  # the last tick's gate held until now
            self.g.brake_held_s += max(0.0, min(1.0, now - self._last_now)) * (1.0 - self._last_gate)
        gate = 1.0
        if self.g.phase != "brake":
            self._aligned = False  # the autopilot turns to retrograde while coasting; check again
        elif thr > 0:
            # Braking thrust goes where the vessel points, not where the guidance asks: a lander handed
            # over still holding its deorbit attitude would burn prograde. Hold the brake back until the
            # autopilot has turned it (the terminal phase is never gated: its thrust arrests the sink).
            try:
                err = vec.angle_deg(ctx.stream("dir_brf", ctx.vessel.direction, ctx.brf), self.commanded(ctx))
            except Exception:  # noqa: BLE001 — no pointing reading: do not hold the brake back
                err = 0.0
            self._aligned = self._aligned or err <= ALIGN_FULL_DEG
            gate = pointing_gate(err, self._aligned)
            thr *= gate
        self._last_now, self._last_gate = now, gate
        return thr

    def done(self, ctx, m):
        g = self.g
        if g.phase == "down" and (g.touchdown_at is None or g._now(m) - g.touchdown_at >= g.settle_s):
            return f"touchdown: {g.touchdown}"
        return None


class DescentAttitude(AttitudeLaw):
    uses_autopilot = True
    describe = "surface retrograde, then up with drift correction"

    def __init__(self, throttle_law: DescentThrottle):
        self.t = throttle_law
        self._frame = None
        self._settling = False
        self._last_now: float | None = None

    def start(self, ctx, m):
        ctx.vessel.control.sas = False
        ap = ctx.vessel.auto_pilot
        ap.reference_frame = ctx.brf
        ap.target_roll = float("nan")
        self.update(ctx, m)
        ap.engage()

    def update(self, ctx, m):
        self._last_now = self.t.g._now(m)
        ap = ctx.vessel.auto_pilot
        if self.t.g.phase == "down":
            # Settling on the legs: stop steering toward the last (drift-tilted) direction and let SAS
            # damp the rotation, so the lander does not rock over its downhill legs.
            if not self._settling:
                self._settling = True
                ap.disengage()
                ctx.vessel.control.sas = True
            return
        ap.target_direction = self.t.commanded(ctx)

    def finish(self, ctx):
        g = self.t.g
        if g.phase == "down":
            # On the ground a held direction is a torque on the legs: the reaction wheels lean the
            # lander toward the last (drift-tilted) target, and when the pilot leaves on EVA or boards
            # again the legs spring back and can throw the lander over. The autopilot is off; SAS
            # (stability assist) holds the settled attitude, which loads nothing.
            try:
                ctx.vessel.auto_pilot.disengage()
                ctx.vessel.control.sas = True
            except Exception:  # noqa: BLE001
                pass
            settled = 0.0
            if g.touchdown_at is not None and self._last_now is not None:
                settled = max(0.0, self._last_now - g.touchdown_at)
            if settled >= g.settle_s:
                return f"autopilot off at touchdown; SAS holding the settled attitude after {settled:.1f} s"
            return (f"autopilot off, SAS on; the reflex stopped {settled:.1f} s after contact, before the "
                    f"{g.settle_s:g} s settle ended")
        return "autopilot holding the last descent direction"
