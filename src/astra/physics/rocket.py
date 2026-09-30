"""Rocket and flight mechanics: the rocket equation, burn timing, TWR, braking, drag, ascent models.

Masses are in kg, thrust in N, Isp in s, speeds in m/s. Everything raises ``ValueError`` on
meaningless input. Functions marked MODEL are estimates built on stated simplifications; their
callers must pass the assumptions on to whoever reads the number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

G0 = 9.80665  # m/s^2; converts Isp (s) to exhaust velocity. A definition, not a body constant.


def _pos(name: str, x: float) -> float:
    if not (isinstance(x, (int, float)) and math.isfinite(x) and x > 0):
        raise ValueError(f"{name} must be a positive finite number, got {x!r}")
    return float(x)


def _nonneg(name: str, x: float) -> float:
    if not (isinstance(x, (int, float)) and math.isfinite(x) and x >= 0):
        raise ValueError(f"{name} must be a finite number >= 0, got {x!r}")
    return float(x)


# ---------------------------------------------------------------------------------------------
# rocket equation


def exhaust_velocity(isp_s: float) -> float:
    return _pos("isp_s", isp_s) * G0


def delta_v(isp_s: float, m0: float, m1: float) -> float:
    """Tsiolkovsky: Δv of burning from mass ``m0`` down to ``m1``."""
    _pos("m0", m0)
    _pos("m1", m1)
    if m1 > m0:
        raise ValueError(f"final mass {m1} exceeds initial mass {m0}")
    return exhaust_velocity(isp_s) * math.log(m0 / m1)


def final_mass(isp_s: float, dv: float, m0: float) -> float:
    """Mass left after spending ``dv`` from initial mass ``m0``."""
    return _pos("m0", m0) * math.exp(-_nonneg("dv", dv) / exhaust_velocity(isp_s))


def initial_mass(isp_s: float, dv: float, m1: float) -> float:
    """Initial mass needed to deliver ``dv`` and end at mass ``m1``."""
    return _pos("m1", m1) * math.exp(_nonneg("dv", dv) / exhaust_velocity(isp_s))


def propellant_for_dv(isp_s: float, dv: float, m1: float) -> float:
    """Propellant mass that delivers ``dv`` to a craft whose mass after the burn is ``m1``."""
    return initial_mass(isp_s, dv, m1) - m1


def mass_flow(thrust_n: float, isp_s: float) -> float:
    """Propellant mass flow (kg/s) at ``thrust_n``."""
    return _pos("thrust_n", thrust_n) / exhaust_velocity(isp_s)


def burn_time(dv: float, m0: float, thrust_n: float, isp_s: float | None = None) -> float:
    """Time to deliver ``dv`` at constant thrust. With ``isp_s`` the mass loss is integrated
    (exact); without it the constant-mass value m0·dv/F is returned (an overestimate)."""
    _nonneg("dv", dv)
    _pos("m0", m0)
    _pos("thrust_n", thrust_n)
    if isp_s is None:
        return m0 * dv / thrust_n
    return (m0 - final_mass(isp_s, dv, m0)) / mass_flow(thrust_n, isp_s)


def half_dv_time(dv: float, m0: float, thrust_n: float, isp_s: float | None = None) -> float:
    """Time into a burn at which half of ``dv`` has been delivered. Starting the burn this long
    before a node centres the impulse on it (later than burn_time/2, because the craft lightens)."""
    return burn_time(dv / 2.0, m0, thrust_n, isp_s)


def combined_isp(engines: list[tuple[float, float]]) -> float:
    """Effective Isp of engines burning together: Σthrust / Σ(thrust/isp). Pairs are (thrust, isp)."""
    if not engines:
        raise ValueError("no engines given")
    total = sum(_pos("thrust", f) for f, _ in engines)
    flow = sum(f / _pos("isp", i) for f, i in engines)
    return total / flow


# ---------------------------------------------------------------------------------------------
# gravity, TWR, hovering


def gravity_at(mu: float, r: float) -> float:
    """Gravitational acceleration mu/r^2 at radius ``r`` from the body's centre."""
    return _pos("mu", mu) / _pos("r", r) ** 2


def twr(thrust_n: float, mass_kg: float, g: float) -> float:
    """Thrust-to-weight ratio in local gravity ``g``."""
    return _nonneg("thrust_n", thrust_n) / (_pos("mass_kg", mass_kg) * _pos("g", g))


def hover_throttle(thrust_n: float, mass_kg: float, g: float) -> float:
    """Throttle fraction that exactly balances weight (> 1 means the craft cannot hover)."""
    return (_pos("mass_kg", mass_kg) * _pos("g", g)) / _pos("thrust_n", thrust_n)


# ---------------------------------------------------------------------------------------------
# braking / landing


@dataclass(frozen=True)
class VerticalStop:
    distance_m: float
    time_s: float
    net_decel_mps2: float


def vertical_stop(speed_down: float, thrust_accel: float, g: float) -> VerticalStop:
    """Constant-mass, 1-D braking of a vertical descent at ``speed_down`` (m/s, positive down) with
    thrust acceleration ``thrust_accel`` against gravity ``g``: distance v²/2(a-g), time v/(a-g).
    Conservative: real deceleration grows as propellant burns. Raises if a <= g (cannot stop)."""
    _nonneg("speed_down", speed_down)
    a_net = _pos("thrust_accel", thrust_accel) - _pos("g", g)
    if a_net <= 0:
        raise ValueError(f"thrust acceleration {thrust_accel:.3f} does not exceed gravity {g:.3f}: "
                         "the descent cannot be stopped")
    return VerticalStop(speed_down ** 2 / (2.0 * a_net), speed_down / a_net, a_net)


@dataclass(frozen=True)
class RetroBurn:
    time_s: float
    altitude_lost_m: float
    downrange_m: float
    dv_used_mps: float
    final_mass_kg: float
    stopped: bool  # False if thrust could not overcome gravity at the end


def retro_burn(horizontal_speed: float, vertical_speed: float, mass_kg: float, thrust_n: float, g: float,
               isp_s: float | None = None, steps: int = 4000) -> RetroBurn:
    """MODEL: simulate a braking burn held exactly retrograde to the surface velocity, from now until
    the craft is at rest, over flat ground in uniform gravity with no drag.

    ``vertical_speed`` is positive UP (descending craft pass a negative value). With ``isp_s`` the mass
    loss is included; without it the mass stays constant (conservative). Returns the altitude and
    ground distance consumed; start the burn at least ``altitude_lost_m`` above the terrain.
    ``steps`` sets the integration resolution (RK4 steps over the expected burn).
    """
    _nonneg("horizontal_speed", abs(horizontal_speed))
    if not math.isfinite(vertical_speed):  # a NaN would otherwise spin the integrator to its cap
        raise ValueError(f"vertical_speed must be a finite number, got {vertical_speed!r}")
    _pos("mass_kg", mass_kg)
    _pos("thrust_n", thrust_n)
    _pos("g", g)
    ve = exhaust_velocity(isp_s) if isp_s is not None else math.inf
    vx, vz = abs(horizontal_speed), float(vertical_speed)
    speed0 = math.hypot(vx, vz)
    if speed0 == 0:
        return RetroBurn(0.0, 0.0, 0.0, 0.0, mass_kg, thrust_n / mass_kg > g)
    a0 = thrust_n / mass_kg
    if a0 <= g and vz <= 0:
        raise ValueError(f"thrust acceleration {a0:.3f} m/s^2 does not exceed gravity {g:.3f}: cannot stop")
    t_est = speed0 / max(a0 - g, 0.05 * a0)
    dt = min(0.05, max(1e-4, t_est / max(100, steps)))

    def deriv(state: tuple[float, float, float, float, float]):
        x_, z_, vx_, vz_, m_ = state
        sp = math.hypot(vx_, vz_)
        a = thrust_n / m_
        ux, uz = (vx_ / sp, vz_ / sp) if sp > 1e-9 else (0.0, -1.0)
        return (vx_, vz_, -a * ux, -a * uz - g, -thrust_n / ve if ve != math.inf else 0.0)

    state = (0.0, 0.0, vx, vz, float(mass_kg))
    t, dv_used = 0.0, 0.0
    best = (speed0, t, state, dv_used)
    for _ in range(2_000_000):
        k1 = deriv(state)
        k2 = deriv(tuple(s + dt / 2 * k for s, k in zip(state, k1)))
        k3 = deriv(tuple(s + dt / 2 * k for s, k in zip(state, k2)))
        k4 = deriv(tuple(s + dt * k for s, k in zip(state, k3)))
        new = tuple(s + dt / 6 * (a + 2 * b + 2 * c + d) for s, a, b, c, d in zip(state, k1, k2, k3, k4))
        dv_used += dt * thrust_n / (0.5 * (state[4] + new[4]))
        t += dt
        state = new
        sp = math.hypot(state[2], state[3])
        if sp < best[0]:
            best = (sp, t, state, dv_used)
        elif sp > best[0] + 1e-9:
            break  # passed the minimum speed: the craft is at rest (or can no longer decelerate)
        if sp < 1e-3:
            break
    sp, t, (x, z, _, _, m), dv_used = best
    stopped = sp < max(0.05, 1e-3 * speed0) and thrust_n / m > g
    return RetroBurn(t, -z, x, dv_used, m, stopped)


# ---------------------------------------------------------------------------------------------
# atmosphere


def dynamic_pressure(rho: float, speed: float) -> float:
    """q = ½ρv² (Pa)."""
    return 0.5 * _nonneg("rho", rho) * speed * speed


def terminal_velocity(mass_kg: float, g: float, rho: float, cd_area_m2: float) -> float:
    """Speed where drag balances weight: sqrt(2mg / (ρ·CdA))."""
    return math.sqrt(2.0 * _pos("mass_kg", mass_kg) * _pos("g", g) / (_pos("rho", rho) * _pos("cd_area", cd_area_m2)))


def drag_area_from_terminal_velocity(mass_kg: float, g: float, rho: float, v_terminal: float) -> float:
    """Effective Cd·A (m²) implied by an observed steady descent speed — calibrate chutes live."""
    return 2.0 * _pos("mass_kg", mass_kg) * _pos("g", g) / (_pos("rho", rho) * _pos("v_terminal", v_terminal) ** 2)


def altitude_at_pressure_fraction(profile: list[tuple[float, float]], fraction: float) -> float:
    """Altitude where static pressure falls to ``fraction`` of the first sample's, interpolating
    log-pressure linearly in a sampled (altitude_m, pressure_pa) profile."""
    if not 0 < fraction < 1:
        raise ValueError("fraction must be in (0, 1)")
    pts = sorted((h, p) for h, p in profile if p > 0)
    if len(pts) < 2:
        raise ValueError("need at least two positive pressure samples")
    target = math.log(pts[0][1] * fraction)
    for (h0, p0), (h1, p1) in zip(pts, pts[1:]):
        l0, l1 = math.log(p0), math.log(p1)
        if l1 <= target <= l0 and l0 != l1:
            return h0 + (h1 - h0) * (l0 - target) / (l0 - l1)
    raise ValueError("the profile does not reach that pressure fraction")


# ---------------------------------------------------------------------------------------------
# ascent MODELS (estimates; always report their assumptions)


def level_burn_dv(v_start: float, v_circ: float, twr_: float, steps: int = 4000) -> float:
    """MODEL: Δv spent accelerating horizontally from ``v_start`` to circular speed ``v_circ`` while
    holding altitude, with constant thrust acceleration ``twr_``·g (mass loss ignored, so this is
    conservative). The thrust is tilted up just enough to cancel gravity minus centrifugal relief,
    g(1 - v²/v_circ²). Needs twr > 1. Loss = result - (v_circ - v_start)."""
    _pos("v_circ", v_circ)
    n = _pos("twr", twr_)
    if n <= 1.0:
        raise ValueError("a level burn needs TWR > 1")
    u0 = max(0.0, min(1.0, v_start / v_circ))
    if u0 >= 1.0:
        return 0.0
    steps += steps % 2
    h = (1.0 - u0) / steps

    def f(u: float) -> float:
        w = 1.0 - u * u
        return n / math.sqrt(n * n - w * w)

    acc = f(u0) + f(1.0)
    for k in range(1, steps):
        acc += (4 if k % 2 else 2) * f(u0 + k * h)
    return v_circ * acc * h / 3.0


def vertical_climb_gravity_loss(g: float, twr_: float, height_m: float) -> float:
    """MODEL: gravity loss g·t of climbing straight up ``height_m`` from rest at constant thrust
    acceleration ``twr_``·g (constant mass): t = sqrt(2h / ((n-1)g))."""
    _pos("g", g)
    n = _pos("twr", twr_)
    if n <= 1.0:
        raise ValueError("a vertical climb needs TWR > 1")
    return g * math.sqrt(2.0 * _nonneg("height_m", height_m) / ((n - 1.0) * g))


def launch_azimuth(latitude_rad: float, inclination_rad: float) -> float:
    """Inertial launch azimuth (rad from north toward east) into ``inclination`` from ``latitude``:
    sin(az) = cos(i)/cos(lat). Raises if the inclination is below the latitude (not reachable)."""
    if not abs(latitude_rad) < math.pi / 2:
        raise ValueError("launch latitude must lie strictly between the poles (azimuth is undefined at a pole)")
    c = math.cos(inclination_rad) / math.cos(latitude_rad)
    if abs(c) > 1.0 + 1e-12:
        raise ValueError("inclination is lower than the launch latitude: not reachable by direct ascent")
    return math.asin(max(-1.0, min(1.0, c)))


def rotation_assisted_dv(v_orbit: float, v_surface_east: float, azimuth_rad: float) -> float:
    """Horizontal Δv from rest on a rotating surface (eastward speed ``v_surface_east``) to an
    inertial velocity ``v_orbit`` flown along ``azimuth_rad``."""
    east = v_orbit * math.sin(azimuth_rad) - v_surface_east
    north = v_orbit * math.cos(azimuth_rad)
    return math.hypot(east, north)
