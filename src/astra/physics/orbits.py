"""Two-body orbital mechanics: vis-viva family, apsis changes, transfers, anomalies, propagation.

Units are SI throughout (m, s, m/s, rad). Radii are measured from the body's centre, never
altitudes. Functions raise ``ValueError`` on physically meaningless input instead of returning 0.

Frames: kRPC reference frames are left-handed with +y toward the north pole, so the standard-formula
``r × v`` of a *prograde* orbit points along -y. Nothing here assumes an axis convention: every
direction-sensitive routine takes its reference axis from vectors in the caller's own frame
(e.g. the departure body's ``cross(r, v)``), which is handedness-safe. The element conversions
(:func:`state_from_elements`) use a right-handed z-up frame. KSP's orbital elements, as kRPC reports
them, reproduce ``orbit.position_at(ut, parent.non_rotating_reference_frame)`` exactly under the
axis map ``(x, y, z) -> (x, z, y)`` (checked live to ~1e-11 relative for every stock body).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

Vec3 = tuple[float, float, float]
TAU = 2.0 * math.pi

# ---------------------------------------------------------------------------------------------
# 3-vectors (plain tuples)


def vadd(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vsub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vscale(a: Vec3, s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def vdot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def vcross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def vnorm(a: Vec3) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def vunit(a: Vec3) -> Vec3:
    n = vnorm(a)
    if n == 0.0:
        raise ValueError("cannot normalize a zero vector")
    return (a[0] / n, a[1] / n, a[2] / n)


def vangle(a: Vec3, b: Vec3) -> float:
    """Unsigned angle between two vectors (rad, 0..pi)."""
    return math.atan2(vnorm(vcross(a, b)), vdot(a, b))


def signed_angle(a: Vec3, b: Vec3, axis: Vec3) -> float:
    """Angle from ``a`` to ``b`` in [0, 2pi), counted positive in the sense that rotating about
    ``axis`` by a small positive angle moves ``a`` toward ``cross(axis, a)``. With ``axis = r x v``
    this is "ahead along the direction of motion" in any frame handedness."""
    ang = math.atan2(vdot(vcross(a, b), vunit(axis)), vdot(a, b))
    return ang % TAU


def rotate_about(v: Vec3, axis: Vec3, angle: float) -> Vec3:
    """Rodrigues rotation of ``v`` about ``axis`` by ``angle`` (positive = toward ``cross(axis, v)``)."""
    k = vunit(axis)
    c, s = math.cos(angle), math.sin(angle)
    return vadd(vadd(vscale(v, c), vscale(vcross(k, v), s)), vscale(k, vdot(k, v) * (1.0 - c)))


def wrap_pi(angle: float) -> float:
    """Wrap an angle to (-pi, pi]."""
    a = math.fmod(angle + math.pi, TAU)
    if a <= 0.0:
        a += TAU
    return a - math.pi


# ---------------------------------------------------------------------------------------------
# input checks


def _pos(name: str, x: float) -> float:
    if not (isinstance(x, (int, float)) and math.isfinite(x) and x > 0):
        raise ValueError(f"{name} must be a positive finite number, got {x!r}")
    return float(x)


def _finite(name: str, x: float) -> float:
    if not (isinstance(x, (int, float)) and math.isfinite(x)):
        raise ValueError(f"{name} must be a finite number, got {x!r}")
    return float(x)


def _ecc(e: float) -> float:
    if not (math.isfinite(e) and e >= 0):
        raise ValueError(f"eccentricity must be >= 0, got {e!r}")
    return float(e)


# ---------------------------------------------------------------------------------------------
# vis-viva family


def circular_speed(mu: float, r: float) -> float:
    """Speed of a circular orbit of radius ``r``: sqrt(mu/r)."""
    return math.sqrt(_pos("mu", mu) / _pos("r", r))


def escape_speed(mu: float, r: float) -> float:
    """Parabolic escape speed at radius ``r``: sqrt(2 mu/r)."""
    return math.sqrt(2.0 * _pos("mu", mu) / _pos("r", r))


def vis_viva_speed(mu: float, r: float, a: float) -> float:
    """Speed at radius ``r`` on an orbit of semi-major axis ``a`` (a < 0 for a hyperbola).

    Raises if ``r`` is not reachable on that orbit (beyond the apoapsis of an ellipse)."""
    _pos("mu", mu)
    _pos("r", r)
    if not math.isfinite(a) or a == 0:
        raise ValueError(f"semi-major axis must be finite and non-zero, got {a!r}")
    v2 = mu * (2.0 / r - 1.0 / a)
    if v2 < 0:
        if v2 > -1e-9 * mu / r:  # rounding at the apoapsis itself
            return 0.0
        raise ValueError(f"radius {r:.1f} m lies beyond the apoapsis of an orbit with a={a:.1f} m")
    return math.sqrt(v2)


def semi_major_axis(r_pe: float, r_ap: float) -> float:
    """Semi-major axis of an ellipse with periapsis and apoapsis radii."""
    _pos("r_pe", r_pe)
    _pos("r_ap", r_ap)
    return 0.5 * (r_pe + r_ap)


def eccentricity(r_pe: float, r_ap: float) -> float:
    lo, hi = sorted((_pos("r_pe", r_pe), _pos("r_ap", r_ap)))
    return (hi - lo) / (hi + lo)


def orbital_period(mu: float, a: float) -> float:
    """Period of an elliptical orbit (a > 0)."""
    return TAU * math.sqrt(_pos("a", a) ** 3 / _pos("mu", mu))


def mean_motion(mu: float, a: float) -> float:
    """Mean motion n = sqrt(mu/|a|^3) (rad/s); valid for ellipses and hyperbolas."""
    if not math.isfinite(a) or a == 0:
        raise ValueError(f"semi-major axis must be finite and non-zero, got {a!r}")
    return math.sqrt(_pos("mu", mu) / abs(a) ** 3)


def specific_energy(mu: float, a: float) -> float:
    """Specific orbital energy -mu/(2a) (J/kg)."""
    if not math.isfinite(a) or a == 0:
        raise ValueError("semi-major axis must be finite and non-zero")
    return -_pos("mu", mu) / (2.0 * a)


def sma_from_speed(mu: float, r: float, v: float) -> float:
    """Semi-major axis of the orbit through radius ``r`` at speed ``v`` (inf for exactly parabolic)."""
    _pos("mu", mu)
    _pos("r", r)
    inv_a = 2.0 / r - v * v / mu
    return math.inf if inv_a == 0 else 1.0 / inv_a


def synchronous_radius(mu: float, rotation_period: float) -> float:
    """Orbit radius whose period equals ``rotation_period`` (may lie outside the SOI)."""
    return (_pos("mu", mu) * (_pos("rotation_period", abs(rotation_period)) / TAU) ** 2) ** (1.0 / 3.0)


# ---------------------------------------------------------------------------------------------
# impulsive maneuvers (signed: + prograde, - retrograde)


def apsis_change_dv(mu: float, r_burn: float, a_now: float, r_opposite_target: float) -> float:
    """Signed prograde Δv at an apsis (or any point with zero flight-path angle) of radius ``r_burn``
    that moves the opposite apsis to ``r_opposite_target``. ``a_now`` is the current semi-major axis
    (negative for a hyperbola, so this also covers capture from an arrival hyperbola)."""
    v_now = vis_viva_speed(mu, r_burn, a_now)
    v_new = vis_viva_speed(mu, r_burn, semi_major_axis(r_burn, r_opposite_target))
    return v_new - v_now


def circularize_dv(mu: float, r_burn: float, a_now: float) -> float:
    """Signed prograde Δv at an apsis of radius ``r_burn`` to make the orbit circular there."""
    return circular_speed(mu, r_burn) - vis_viva_speed(mu, r_burn, a_now)


def deorbit_dv(mu: float, r_ap: float, r_pe_now: float, r_pe_target: float) -> float:
    """Retrograde Δv magnitude at apoapsis ``r_ap`` to lower the periapsis to ``r_pe_target``."""
    if r_pe_target > r_pe_now:
        raise ValueError("deorbit target periapsis must be below the current periapsis")
    return -apsis_change_dv(mu, r_ap, semi_major_axis(r_ap, r_pe_now), r_pe_target)


@dataclass(frozen=True)
class Hohmann:
    dv1: float  # signed prograde Δv at departure
    dv2: float  # signed prograde Δv at arrival (circularize)
    tof: float  # transfer time, s
    a_transfer: float

    @property
    def total(self) -> float:
        return abs(self.dv1) + abs(self.dv2)


def hohmann(mu: float, r1: float, r2: float) -> Hohmann:
    """Coplanar Hohmann transfer between circular orbits of radii ``r1`` -> ``r2`` (either direction)."""
    a = semi_major_axis(r1, r2)
    dv1 = vis_viva_speed(mu, r1, a) - circular_speed(mu, r1)
    dv2 = circular_speed(mu, r2) - vis_viva_speed(mu, r2, a)
    return Hohmann(dv1, dv2, math.pi * math.sqrt(a ** 3 / _pos("mu", mu)), a)


def hohmann_excess_speeds(mu: float, r1: float, r2: float) -> tuple[float, float]:
    """(departure, arrival) hyperbolic excess speeds of a Hohmann transfer between the circular
    orbits of two bodies at radii ``r1`` and ``r2`` around a common primary of GM ``mu``."""
    h = hohmann(mu, r1, r2)
    return abs(h.dv1), abs(h.dv2)


def phase_angle_for_transfer(mu: float, r_target: float, tof: float) -> float:
    """Angle (rad, wrapped to (-pi, pi]) by which a target on a circular orbit of radius
    ``r_target`` must LEAD you at departure so that a transfer taking ``tof`` seconds and sweeping
    180 degrees meets it. Negative = the target must trail you (inward transfers)."""
    return wrap_pi(math.pi - mean_motion(mu, r_target) * _pos("tof", tof))


def synodic_period(t1: float, t2: float) -> float:
    """Time between repeats of the same relative geometry of two orbits with periods t1, t2."""
    d = abs(1.0 / _pos("t1", t1) - 1.0 / _pos("t2", t2))
    return math.inf if d == 0 else 1.0 / d


def ejection_dv(mu: float, r_burn: float, v_inf: float, v_now: float | None = None) -> float:
    """Prograde Δv at radius ``r_burn`` to leave the SOI with hyperbolic excess speed ``v_inf``.
    ``v_now`` is the speed before the burn (defaults to circular speed at ``r_burn``)."""
    v_hyp = math.sqrt(_finite("v_inf", v_inf) ** 2 + 2.0 * _pos("mu", mu) / _pos("r_burn", r_burn))
    return v_hyp - (circular_speed(mu, r_burn) if v_now is None else v_now)


def hyperbola_eccentricity(mu: float, r_pe: float, v_inf: float) -> float:
    """Eccentricity of the hyperbola with periapsis radius ``r_pe`` and excess speed ``v_inf``."""
    return 1.0 + _pos("r_pe", r_pe) * _finite("v_inf", v_inf) ** 2 / _pos("mu", mu)


def asymptote_true_anomaly(mu: float, r_pe: float, v_inf: float) -> float:
    """True anomaly of the outgoing asymptote, arccos(-1/e) (rad). The burn point (periapsis) lies
    this far BEHIND the desired v_inf direction, measured along the direction of motion."""
    e = hyperbola_eccentricity(mu, r_pe, v_inf)
    return math.acos(-1.0 / e)


def capture_dv(mu: float, r_pe: float, v_inf: float, r_ap_target: float) -> float:
    """Retrograde Δv magnitude at periapsis ``r_pe`` of an arrival hyperbola (excess ``v_inf``) to be
    captured into an ellipse with apoapsis ``r_ap_target`` (pass ``r_pe`` for a circular orbit)."""
    if r_ap_target < r_pe:
        raise ValueError("target apoapsis must be at or above the periapsis")
    v_arr = math.sqrt(_finite("v_inf", v_inf) ** 2 + 2.0 * _pos("mu", mu) / _pos("r_pe", r_pe))
    return v_arr - vis_viva_speed(mu, r_pe, semi_major_axis(r_pe, r_ap_target))


def plane_change_dv(speed: float, delta_rad: float) -> float:
    """Δv to rotate a velocity of magnitude ``speed`` by ``delta_rad``: 2 v sin(Δ/2)."""
    return 2.0 * abs(_finite("speed", speed)) * abs(math.sin(_finite("delta", delta_rad) / 2.0))


def flyby_turn_angle(mu: float, r_pe: float, v_inf: float) -> float:
    """Deflection of the v_inf vector by a flyby with periapsis ``r_pe``: 2 asin(1/e) (rad)."""
    return 2.0 * math.asin(1.0 / hyperbola_eccentricity(mu, r_pe, v_inf))


# ---------------------------------------------------------------------------------------------
# conic geometry and anomalies


def semi_latus_rectum(a: float, e: float) -> float:
    _ecc(e)
    if abs(e - 1.0) < 1e-12:
        raise ValueError("use the periapsis radius directly for parabolic orbits (p = 2 r_pe)")
    return a * (1.0 - e * e)


def radius_at_true_anomaly(a: float, e: float, nu: float) -> float:
    p = semi_latus_rectum(a, e)
    denom = 1.0 + e * math.cos(nu)
    if denom <= 0:
        raise ValueError("true anomaly lies outside the hyperbola's asymptotes")
    return p / denom


def true_anomaly_at_radius(a: float, e: float, r: float) -> float:
    """Outbound true anomaly (0..pi) at which the orbit reaches radius ``r``; the inbound crossing is
    the negative of this. Raises if the orbit never reaches ``r``."""
    _pos("r", r)
    if e < 1e-12:
        raise ValueError("a circular orbit has the same radius everywhere")
    c = (semi_latus_rectum(a, e) / r - 1.0) / e
    if c > 1.0 + 1e-12 or c < -1.0 - 1e-12:
        raise ValueError(f"the orbit never reaches radius {r:.1f} m")
    return math.acos(max(-1.0, min(1.0, c)))


def flight_path_angle(e: float, nu: float) -> float:
    """Angle of the velocity above the local horizontal (rad)."""
    return math.atan2(e * math.sin(nu), 1.0 + e * math.cos(nu))


def true_to_eccentric(nu: float, e: float) -> float:
    """Eccentric anomaly E (ellipse, in (-pi, pi]) or hyperbolic anomaly H (hyperbola) from true anomaly."""
    _ecc(e)
    if e < 1.0:
        return 2.0 * math.atan2(math.sqrt(1.0 - e) * math.sin(nu / 2.0), math.sqrt(1.0 + e) * math.cos(nu / 2.0))
    if e == 1.0:
        raise ValueError("parabolic orbits have no eccentric anomaly")
    nu = wrap_pi(nu)
    x = math.sqrt((e - 1.0) / (e + 1.0)) * math.tan(nu / 2.0)
    if abs(nu) >= math.pi or abs(x) >= 1.0:
        raise ValueError("true anomaly lies outside the hyperbola's asymptotes")
    return 2.0 * math.atanh(x)


def eccentric_to_true(big_e: float, e: float) -> float:
    """True anomaly from the eccentric (ellipse) or hyperbolic (hyperbola) anomaly."""
    _ecc(e)
    if e < 1.0:
        return 2.0 * math.atan2(math.sqrt(1.0 + e) * math.sin(big_e / 2.0), math.sqrt(1.0 - e) * math.cos(big_e / 2.0))
    if e == 1.0:
        raise ValueError("parabolic orbits have no eccentric anomaly")
    return 2.0 * math.atan(math.sqrt((e + 1.0) / (e - 1.0)) * math.tanh(big_e / 2.0))


def eccentric_to_mean(big_e: float, e: float) -> float:
    """Kepler's equation: M = E - e sin E (ellipse) or M = e sinh H - H (hyperbola)."""
    _ecc(e)
    if e < 1.0:
        return big_e - e * math.sin(big_e)
    return e * math.sinh(big_e) - big_e


def mean_to_eccentric(m: float, e: float) -> float:
    """Solve Kepler's equation for E (ellipse; M taken mod 2pi, E in [0, 2pi)) or H (hyperbola) by Newton."""
    _ecc(e)
    if e < 1.0:
        m = m % TAU
        big_e = m if e < 0.8 else math.pi
        for _ in range(60):
            step = (big_e - e * math.sin(big_e) - m) / (1.0 - e * math.cos(big_e))
            big_e -= step
            if abs(step) < 1e-14:
                break
        return big_e
    if e == 1.0:
        raise ValueError("parabolic orbits have no mean anomaly in this convention")
    h = math.asinh(m / e)
    for _ in range(100):
        step = (e * math.sinh(h) - h - m) / (e * math.cosh(h) - 1.0)
        h -= step
        if abs(step) < 1e-14 * max(1.0, abs(h)):
            break
    return h


def true_to_mean(nu: float, e: float) -> float:
    """Mean anomaly from true anomaly (elliptic: wrapped to [0, 2pi); hyperbolic: signed)."""
    m = eccentric_to_mean(true_to_eccentric(nu, e), e)
    return m % TAU if e < 1.0 else m


def mean_to_true(m: float, e: float) -> float:
    """True anomaly from mean anomaly."""
    return eccentric_to_true(mean_to_eccentric(m, e), e)


def time_between_anomalies(mu: float, a: float, e: float, nu_from: float, nu_to: float) -> float:
    """Time to travel from true anomaly ``nu_from`` to ``nu_to``. Elliptic orbits: the next forward
    arrival (0..period). Hyperbolic: signed time (negative if ``nu_to`` was already passed)."""
    n = mean_motion(mu, a)
    dm = true_to_mean(nu_to, e) - true_to_mean(nu_from, e)
    if e < 1.0:
        dm %= TAU
    return dm / n


# ---------------------------------------------------------------------------------------------
# state vectors


def state_from_elements(mu: float, a: float, e: float, inc: float, lan: float, argpe: float,
                        nu: float) -> tuple[Vec3, Vec3]:
    """Position and velocity from classical elements, in a RIGHT-handed frame with z = reference-plane
    normal and x = reference direction. For kRPC axes use (x, y, z) -> (x, z, y)."""
    p = semi_latus_rectum(a, e)
    r = p / (1.0 + e * math.cos(nu))
    sq = math.sqrt(_pos("mu", mu) / p)
    r_pf = (r * math.cos(nu), r * math.sin(nu), 0.0)
    v_pf = (-sq * math.sin(nu), sq * (e + math.cos(nu)), 0.0)
    co, so = math.cos(lan), math.sin(lan)
    cw, sw = math.cos(argpe), math.sin(argpe)
    ci, si = math.cos(inc), math.sin(inc)
    rot = ((co * cw - so * sw * ci, -co * sw - so * cw * ci, so * si),
           (so * cw + co * sw * ci, -so * sw + co * cw * ci, -co * si),
           (sw * si, cw * si, ci))

    def apply(v: Vec3) -> Vec3:
        return (rot[0][0] * v[0] + rot[0][1] * v[1], rot[1][0] * v[0] + rot[1][1] * v[1],
                rot[2][0] * v[0] + rot[2][1] * v[1])

    return apply(r_pf), apply(v_pf)


@dataclass(frozen=True)
class KeplerOrbit:
    """Keplerian elements as KSP/kRPC report them (angles in rad, mean anomaly at ``epoch`` UT)."""

    mu: float
    a: float
    e: float
    inc: float = 0.0
    lan: float = 0.0
    argpe: float = 0.0
    mean_anomaly_at_epoch: float = 0.0
    epoch: float = 0.0

    def true_anomaly_at(self, ut: float) -> float:
        m = self.mean_anomaly_at_epoch + mean_motion(self.mu, self.a) * (ut - self.epoch)
        return mean_to_true(m, self.e)

    def state_at(self, ut: float) -> tuple[Vec3, Vec3]:
        """(r, v) at ``ut`` in the right-handed z-up element frame."""
        return state_from_elements(self.mu, self.a, self.e, self.inc, self.lan, self.argpe,
                                   self.true_anomaly_at(ut))

    def next_ut_at(self, nu: float, after_ut: float) -> float:
        """First UT at or after ``after_ut`` at true anomaly ``nu``. On a hyperbola the point is
        passed only once; raises if that already happened."""
        dt = time_between_anomalies(self.mu, self.a, self.e, self.true_anomaly_at(after_ut), nu)
        if dt < 0:
            raise ValueError("that point of the hyperbolic trajectory has already been passed")
        return after_ut + dt

    @property
    def period(self) -> float:
        return orbital_period(self.mu, self.a)


def elements_from_state(mu: float, r: Vec3, v: Vec3) -> dict[str, float]:
    """Frame-independent elements of the orbit through (r, v): a, e, r_pe, r_ap (inf if unbound),
    true anomaly (sign from the radial velocity), energy, specific angular momentum."""
    _pos("mu", mu)
    rn, vn = vnorm(r), vnorm(v)
    h = vnorm(vcross(r, v))
    energy = vn * vn / 2.0 - mu / rn
    a = math.inf if energy == 0 else -mu / (2.0 * energy)
    e_vec = vsub(vscale(r, vn * vn / mu - 1.0 / rn), vscale(v, vdot(r, v) / mu))
    e = vnorm(e_vec)
    p = h * h / mu
    r_pe = p / (1.0 + e)
    r_ap = p / (1.0 - e) if e < 1.0 else math.inf
    if e > 1e-12:
        nu = math.acos(max(-1.0, min(1.0, vdot(e_vec, r) / (e * rn))))
        if vdot(r, v) < 0:
            nu = -nu
    else:
        nu = 0.0
    return {"a": a, "e": e, "r_pe": r_pe, "r_ap": r_ap, "nu": nu, "energy": energy, "h": h}


def classical_elements(mu: float, r: Vec3, v: Vec3) -> dict[str, float]:
    """Classical elements (a, e, inc, lan, argpe, nu; rad) of (r, v) given in the RIGHT-handed z-up
    frame used by :func:`state_from_elements` (its inverse). Undefined angles (equatorial or circular
    orbits) are set to 0 and the remaining angle absorbs them, as KSP does."""
    base = elements_from_state(mu, r, v)
    h = vcross(r, v)
    hn = vnorm(h)
    inc = math.acos(max(-1.0, min(1.0, h[2] / hn)))
    node = (-h[1], h[0], 0.0)
    nn = vnorm(node)
    rn, vn = vnorm(r), vnorm(v)
    e_vec = vsub(vscale(r, vn * vn / mu - 1.0 / rn), vscale(v, vdot(r, v) / mu))
    e = base["e"]
    lan = math.atan2(node[1], node[0]) % TAU if nn > 1e-12 * hn else 0.0
    n_hat = (math.cos(lan), math.sin(lan), 0.0)
    # in-plane reference directions: ascending node and 90 degrees ahead of it
    q_hat = vcross(vscale(h, 1.0 / hn), n_hat)
    if e > 1e-12:
        argpe = math.atan2(vdot(e_vec, q_hat), vdot(e_vec, n_hat)) % TAU
        nu = math.atan2(vdot(vcross(e_vec, r), h) / hn, vdot(e_vec, r)) % TAU
    else:
        argpe = 0.0
        nu = math.atan2(vdot(r, q_hat), vdot(r, n_hat)) % TAU
    return {"a": base["a"], "e": e, "inc": inc, "lan": lan, "argpe": argpe, "nu": nu}


def _stumpff(z: float) -> tuple[float, float]:
    if z > 1e-8:
        s = math.sqrt(z)
        return (1.0 - math.cos(s)) / z, (s - math.sin(s)) / (s ** 3)
    if z < -1e-8:
        s = math.sqrt(-z)
        return (math.cosh(s) - 1.0) / (-z), (math.sinh(s) - s) / (s ** 3)
    return 0.5 - z / 24.0 + z * z / 720.0, 1.0 / 6.0 - z / 120.0 + z * z / 5040.0


def propagate(mu: float, r0: Vec3, v0: Vec3, dt: float) -> tuple[Vec3, Vec3]:
    """Two-body propagation of (r0, v0) by ``dt`` seconds (universal variables; any conic, any frame).

    Solved with the Laguerre-Conway iteration, which converges from a crude start on every conic
    (plain Newton diverges on near-parabolic ellipses over long arcs)."""
    _pos("mu", mu)
    if dt == 0:
        return r0, v0
    sqmu = math.sqrt(mu)
    r0n = vnorm(r0)
    sigma0 = vdot(r0, v0) / sqmu
    alpha = 2.0 / r0n - vdot(v0, v0) / mu  # 1/a
    if alpha > 1e-12:  # ellipse: whole periods change nothing, so propagate the remainder only
        dt = math.fmod(dt, TAU / (sqmu * alpha ** 1.5))
        if dt == 0:
            return r0, v0
    # initial guess (Vallado)
    if alpha > 1e-12:
        chi = sqmu * dt * alpha
    elif alpha < -1e-12:
        a = 1.0 / alpha
        sign = 1.0 if dt > 0 else -1.0
        num = -2.0 * mu * alpha * dt
        den = vdot(r0, v0) + sign * math.sqrt(-mu * a) * (1.0 - r0n * alpha)
        chi = sign * math.sqrt(-a) * math.log(max(num / den, 1e-300)) if den != 0 else sqmu * dt / r0n
    else:
        chi = sqmu * dt / r0n
    order = 5.0
    for _ in range(200):
        z = alpha * chi * chi
        c, s = _stumpff(z)
        f = sigma0 * chi * chi * c + (1.0 - alpha * r0n) * chi ** 3 * s + r0n * chi - sqmu * dt
        df = sigma0 * chi * (1.0 - z * s) + (1.0 - alpha * r0n) * chi * chi * c + r0n  # = r
        ddf = sigma0 * (1.0 - z * c) + (1.0 - alpha * r0n) * chi * (1.0 - z * s)
        disc = abs((order - 1.0) ** 2 * df * df - order * (order - 1.0) * f * ddf)
        step = order * f / (df + math.copysign(math.sqrt(disc), df))
        chi -= step
        if abs(step) < 1e-10 * max(1.0, abs(chi)):
            break
    else:
        raise ValueError("Kepler propagation did not converge")
    z = alpha * chi * chi
    c, s = _stumpff(z)
    f = 1.0 - chi * chi / r0n * c
    g = dt - chi ** 3 / sqmu * s
    r = vadd(vscale(r0, f), vscale(v0, g))
    rn = vnorm(r)
    fdot = sqmu / (rn * r0n) * (z * s - 1.0) * chi
    gdot = 1.0 - chi * chi / rn * c
    return r, vadd(vscale(r0, fdot), vscale(v0, gdot))


# ---------------------------------------------------------------------------------------------
# single-burn geometry


def speed_for_apsis(mu: float, r: float, fpa: float, r_apsis: float) -> float:
    """Speed needed at radius ``r`` with flight-path angle ``fpa`` (rad) so that the orbit has an apsis
    at ``r_apsis`` (the apoapsis if above ``r``, the periapsis if below). A burn along the velocity
    vector keeps ``fpa``, so ``speed_for_apsis - speed`` is the prograde dv that puts the opposite
    side of the orbit at ``r_apsis`` from any point, not only from an apsis."""
    _pos("mu", mu)
    _pos("r", r)
    _pos("r_apsis", r_apsis)
    k = (r * math.cos(fpa) / r_apsis) ** 2 - 1.0
    num = 2.0 * mu * (1.0 / r_apsis - 1.0 / r)
    if k == 0 and num == 0:  # the apsis is here and the motion horizontal: the circular orbit (0/0)
        return circular_speed(mu, r)
    if k == 0 or num / k <= 0:
        raise ValueError(f"no orbit through r={r:.1f} m at flight-path angle {math.degrees(fpa):.3f} deg "
                         f"has an apsis at {r_apsis:.1f} m")
    return math.sqrt(num / k)


def find_zero(fn: Callable[[float], float], t0: float, t1: float, samples: int, tol: float = 1e-3,
              prefer: float | None = None) -> float | None:
    """A root of ``fn`` on [t0, t1]: the first one, or the one nearest ``prefer``. ``fn`` may be an
    angle wrapped to (-pi, pi]; jumps larger than pi (wrap-arounds) are not treated as crossings."""
    if t1 <= t0 or samples < 2:
        raise ValueError("need t1 > t0 and samples >= 2")
    step = (t1 - t0) / samples
    roots: list[float] = []
    ta, fa = t0, fn(t0)
    for i in range(1, samples + 1):
        tb = t0 + i * step
        fb = fn(tb)
        if fa == 0:
            roots.append(ta)
        elif fa * fb < 0 and abs(fb - fa) < math.pi:
            lo, hi, flo = ta, tb, fa
            while hi - lo > tol:
                mid = 0.5 * (lo + hi)
                fm = fn(mid)
                if (fm < 0) == (flo < 0):
                    lo, flo = mid, fm
                else:
                    hi = mid
            roots.append(0.5 * (lo + hi))
        if roots and prefer is None:
            return roots[0]
        ta, fa = tb, fb
    if not roots:
        return None
    return min(roots, key=lambda t: abs(t - prefer)) if prefer is not None else roots[0]


# ---------------------------------------------------------------------------------------------
# burn planners over ephemerides: ``StateFn(ut) -> (r, v)``. All inputs must share one inertial
# frame (any axes, any handedness); directions come from the craft's own r x v.

StateFn = Callable[[float], tuple[Vec3, Vec3]]


def fpa_of(r: Vec3, v: Vec3) -> float:
    """Flight-path angle (rad above the local horizontal) of the state (r, v)."""
    return math.asin(max(-1.0, min(1.0, vdot(r, v) / (vnorm(r) * vnorm(v)))))


def plan_transfer_to_satellite(mu: float, craft: StateFn, satellite: StateFn, t_start: float,
                               samples: int = 720) -> dict:
    """First prograde burn at or after ``t_start`` that sends a craft from its orbit to the radius of a
    satellite of the same primary, timed so the satellite is there on arrival (Hohmann phasing).

    Assumes one prograde burn and arrival 180 degrees from the burn point. Returns ``ut, prograde_dv,
    tof, lead_angle, required_lead`` (angles in rad, measured in the craft's plane)."""
    r0, v0 = craft(t_start)
    h = vcross(r0, v0)
    n_c = vnorm(h) / vnorm(r0) ** 2
    rs0, vs0 = satellite(t_start)
    n_s = vnorm(vcross(rs0, vs0)) / vnorm(rs0) ** 2
    if abs(n_c - n_s) < 1e-15:
        raise ValueError("craft and satellite have the same angular rate: the phase never changes")

    def solve(t: float) -> tuple[float, float, float]:
        rc, _ = craft(t)
        rs, _ = satellite(t)
        r1 = vnorm(rc)
        tof = math.pi * math.sqrt(((r1 + vnorm(rs)) / 2.0) ** 3 / mu)
        tof = math.pi * math.sqrt(((r1 + vnorm(satellite(t + tof)[0])) / 2.0) ** 3 / mu)
        swept = signed_angle(rs, satellite(t + tof)[0], h)
        return wrap_pi(signed_angle(rc, rs, h)), wrap_pi(math.pi - swept), tof

    def mismatch(t: float) -> float:
        lead, required, _ = solve(t)
        return wrap_pi(lead - required)

    synodic = TAU / abs(n_c - n_s)
    t_b = find_zero(mismatch, t_start, t_start + 1.05 * synodic, samples)
    if t_b is None:
        raise ValueError("no transfer opportunity found within one synodic period")
    lead, required, tof = solve(t_b)
    rc, vc = craft(t_b)
    r_arr = vnorm(satellite(t_b + tof)[0])
    dv = speed_for_apsis(mu, vnorm(rc), fpa_of(rc, vc), r_arr) - vnorm(vc)
    lead_now, _, _ = solve(t_start)
    return {"ut": t_b, "prograde_dv": dv, "tof": tof, "lead_angle": lead, "required_lead": required,
            "lead_angle_at_start": lead_now, "synodic_period": synodic, "arrival_radius": r_arr}


def escape_time(mu: float, r_pe: float, v_inf: float, r_exit: float) -> float:
    """Time from periapsis ``r_pe`` of an escape hyperbola (excess ``v_inf``) out to radius ``r_exit``."""
    a = -mu / _pos("v_inf", v_inf) ** 2
    e = hyperbola_eccentricity(mu, r_pe, v_inf)
    nu = true_anomaly_at_radius(a, e, _pos("r_exit", r_exit))
    return time_between_anomalies(mu, a, e, 0.0, nu)


def plan_ejection(mu: float, craft: StateFn, v_inf_vec: Vec3, t_exit: float, soi: float,
                  samples: int = 360) -> dict:
    """Prograde burn that puts the craft on an escape hyperbola whose asymptote points along
    ``v_inf_vec`` (the excess velocity wanted when leaving the SOI around ``t_exit``).

    The burn point is the in-plane asymptote direction rotated back by arccos(-1/e) in the craft's
    orbital plane. The out-of-plane part of ``v_inf_vec`` is NOT achieved by this in-plane burn; it is
    reported as ``out_of_plane`` (rad) for a correction or node search. Picks the burn nearest to
    ``t_exit`` minus the escape time."""
    v_inf = vnorm(v_inf_vec)
    r0, v0 = craft(t_exit)
    h = vcross(r0, v0)
    h_hat = vunit(h)
    in_plane = vsub(v_inf_vec, vscale(h_hat, vdot(v_inf_vec, h_hat)))
    out_of_plane = math.asin(max(-1.0, min(1.0, vdot(v_inf_vec, h_hat) / v_inf)))
    a0 = sma_from_speed(mu, vnorm(r0), vnorm(v0))
    if not 0 < a0 < math.inf:
        raise ValueError("the craft must be on a bound orbit to plan an ejection")
    period = orbital_period(mu, a0)

    def burn_dir(t: float) -> tuple[Vec3, float, float]:
        rb = vnorm(craft(t)[0])
        nu = asymptote_true_anomaly(mu, rb, v_inf)
        return rotate_about(vunit(in_plane), h, -nu), rb, nu

    center = t_exit - escape_time(mu, vnorm(r0), v_inf, soi)
    t_b = find_zero(lambda t: wrap_pi(signed_angle(craft(t)[0], burn_dir(t)[0], h)),
                    center - period, center + period, samples, prefer=center)
    if t_b is None:
        raise ValueError("could not time the ejection burn")
    rc, vc = craft(t_b)
    _, rb, nu = burn_dir(t_b)
    return {"ut": t_b, "prograde_dv": math.sqrt(v_inf ** 2 + 2.0 * mu / rb) - vnorm(vc), "burn_radius": rb,
            "asymptote_true_anomaly": nu, "eccentricity": hyperbola_eccentricity(mu, rb, v_inf),
            "escape_time": escape_time(mu, rb, v_inf, soi), "out_of_plane": out_of_plane}


def soi_exit(mu: float, r: Vec3, v: Vec3, soi: float) -> tuple[float, Vec3, Vec3] | None:
    """(seconds from now, position, velocity) when the conic through (r, v) first reaches radius
    ``soi``, or None if it never does (a bound orbit whose apoapsis lies inside)."""
    if vnorm(r) >= soi:
        return 0.0, r, v
    el = elements_from_state(mu, r, v)
    if el["e"] < 1.0 and el["r_ap"] < soi:
        return None
    nu_exit = true_anomaly_at_radius(el["a"], el["e"], soi)
    dt = time_between_anomalies(mu, el["a"], el["e"], el["nu"], nu_exit)
    r_e, v_e = propagate(mu, r, v, dt)
    return dt, r_e, v_e


def golden_min(fn: Callable[[float], float], a: float, b: float, tol: float) -> tuple[float, float]:
    """(x, fn(x)) minimizing a unimodal ``fn`` on [a, b] to within ``tol``."""
    g = (math.sqrt(5.0) - 1.0) / 2.0
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = fn(c), fn(d)
    while b - a > tol:
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = fn(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = fn(d)
    x = 0.5 * (a + b)
    return x, fn(x)


def plan_escape_from_satellite(mu_parent: float, mu_sat: float, craft_rel: StateFn, satellite: StateFn,
                               r_pe_target: float, t_start: float, soi: float, samples: int = 120) -> dict:
    """Cheapest in-plane prograde burn, within one orbit after ``t_start``, that takes a craft orbiting a
    satellite (moon) out of its SOI onto a parent-centric orbit with periapsis radius ``r_pe_target``.

    Solved exactly in the patched-conic model rather than with the asymptote approximation (which is
    poor when the SOI is only ~10 periapsis radii, as at the Mun): for a burn of ``dv`` at time ``t``
    the craft's conic is propagated to the SOI boundary and turned into a parent-centric orbit. For
    each ``dv`` the burn time that minimizes the parent periapsis is found (the exit most nearly
    opposite to the satellite's motion); ``dv`` is then bisected until that minimum equals the target.
    ``craft_rel`` is the craft's state relative to the satellite, ``satellite`` the satellite's state
    relative to the parent, both in the same inertial axes."""
    _pos("r_pe_target", r_pe_target)
    r0, v0 = craft_rel(t_start)
    a0 = sma_from_speed(mu_sat, vnorm(r0), vnorm(v0))
    if not 0 < a0 < math.inf:
        raise ValueError("the craft must be on a bound orbit around the satellite")
    period = orbital_period(mu_sat, a0)
    h_hat = vunit(vcross(r0, v0))

    def exit_state(t: float, dv: float):
        rc, vc = craft_rel(t)
        ex = soi_exit(mu_sat, rc, vadd(vc, vscale(vunit(vc), dv)), soi)
        if ex is None:
            return None
        dt, r_e, v_e = ex
        rs, vs = satellite(t + dt)
        return dt, vadd(rs, r_e), vadd(vs, v_e), vs

    def parent_pe(t: float, dv: float) -> float:
        ex = exit_state(t, dv)
        return math.inf if ex is None else elements_from_state(mu_parent, ex[1], ex[2])["r_pe"]

    step = period / samples

    def best_time(dv: float) -> tuple[float, float]:
        grid = [(parent_pe(t_start + i * step, dv), t_start + i * step) for i in range(samples + 1)]
        pe, t = min(grid)
        if not math.isfinite(pe):
            return t, pe
        lo, hi = max(t_start, t - step), min(t_start + period, t + step)
        return golden_min(lambda x: parent_pe(x, dv), lo, hi, 1e-3)

    rb, vb = vnorm(r0), vnorm(v0)
    v_sat = vnorm(satellite(t_start)[1])
    dv_lo = math.sqrt(2.0 * mu_sat * (1.0 / rb - 1.0 / soi)) - vb + 1e-3  # just reaches the SOI edge
    dv_hi = math.sqrt(v_sat ** 2 + 2.0 * mu_sat / rb) - vb  # v_inf ~ satellite speed: falls straight in
    pe_lo, pe_hi = best_time(dv_lo)[1], best_time(dv_hi)[1]
    if not (pe_hi <= r_pe_target <= pe_lo or (not math.isfinite(pe_lo) and pe_hi <= r_pe_target)):
        raise ValueError("no in-plane prograde escape reaches that parent periapsis from this orbit")
    lo, hi = dv_lo, dv_hi
    while hi - lo > 1e-3:
        mid = 0.5 * (lo + hi)
        if best_time(mid)[1] > r_pe_target:
            lo = mid
        else:
            hi = mid
    dv = 0.5 * (lo + hi)
    t_b, pe = best_time(dv)
    dt, r_par, v_par, v_sat_exit = exit_state(t_b, dv)
    rc, vc = craft_rel(t_b)
    v_after = vnorm(vc) + dv
    return {"ut": t_b, "prograde_dv": dv, "parent_periapsis": pe, "escape_time": dt,
            "v_inf": math.sqrt(max(0.0, v_after ** 2 - 2.0 * mu_sat / vnorm(rc))), "burn_radius": vnorm(rc),
            "out_of_plane": math.asin(max(-1.0, min(1.0, vdot(vunit(v_sat_exit), h_hat))))}
