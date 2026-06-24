"""Precise, BODY-AGNOSTIC interplanetary transfer-window + ejection planner.

Replaces the imprecise phase-angle window estimate (which was ~3 days off MechJeb,
because it assumes circular/coplanar orbits and a tangential burn). Instead it solves
LAMBERT'S PROBLEM over the bodies' REAL Kepler-propagated positions (read from kRPC's
exact ``orbit.position_at(ut, ref)``), so it is correct for any departure->target pair
(Kerbin->Eve, Kerbin->Duna, Eve->Jool, ...) including eccentricity and inclination.

It fixes two bugs the agent had:
  * Bug 1 - phase-angle window ~3 days off  -> a Lambert search over real positions.
  * Bug 2 - heliocentric window UT != in-orbit ejection-node UT (1-5 h mismatch) ->
            the window returned here is the pure HELIOCENTRIC departure UT (body phase),
            independent of any one vessel's orbital phase, so warping the ground to it
            then letting the in-LKO planner pick the next ejection opportunity lands
            within ~1 parking-orbit period (minutes) of the optimum.

Math sources: Izzo 2015 "Revisiting Lambert's problem" (the solver, ported from
lamberthub/izzo2015); Vallado (porkchop bounds: ~1.5*synodic x 2*Hohmann); standard
patched-conic ejection geometry (eta = arccos(1/e)).  No numpy dependency.
"""
from __future__ import annotations

import math

# --------------------------------------------------------------------------- #
# 3-vector helpers (plain tuples, no numpy)                                    #
# --------------------------------------------------------------------------- #
def vadd(a, b):   return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def vsub(a, b):   return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def vscale(a, s): return (a[0] * s, a[1] * s, a[2] * s)
def vdot(a, b):   return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def vcross(a, b): return (a[1] * b[2] - a[2] * b[1],
                          a[2] * b[0] - a[0] * b[2],
                          a[0] * b[1] - a[1] * b[0])
def vnorm(a):     return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
def vunit(a):
    n = vnorm(a)
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-30 else (0.0, 0.0, 0.0)


def rotate_about_axis(v, axis, angle):
    """Rodrigues rotation of v about a unit ``axis`` by ``angle`` radians."""
    k = vunit(axis)
    c, s = math.cos(angle), math.sin(angle)
    return vadd(vadd(vscale(v, c), vscale(vcross(k, v), s)),
                vscale(k, vdot(k, v) * (1.0 - c)))


# --------------------------------------------------------------------------- #
# Lambert solver - Izzo 2015 (Householder), ported from lamberthub.izzo2015    #
# --------------------------------------------------------------------------- #
def _y(x, ll):
    return math.sqrt(1.0 - ll * ll * (1.0 - x * x))


def _tof_curve(x, ll, M):
    """Non-dimensional time-of-flight T(x) for the Izzo variable x."""
    y = _y(x, ll)
    if -1.0 <= x < 1.0:                                   # elliptic
        psi = math.acos(max(-1.0, min(1.0, x * y + ll * (1.0 - x * x))))
    elif x > 1.0:                                         # hyperbolic
        psi = math.asinh((y - x * ll) * math.sqrt(x * x - 1.0))
    else:                                                 # parabola x == 1
        psi = 0.0
    if abs(x - 1.0) < 1e-9:                               # parabolic limit guard
        return (2.0 / 3.0) * (1.0 - ll ** 3)
    return ((psi + M * math.pi) / math.sqrt(abs(1.0 - x * x)) - x + ll * y) / (1.0 - x * x)


def _dT_set(x, ll, M):
    """Return (T, T', T'', T''') at x."""
    y = _y(x, ll)
    T = _tof_curve(x, ll, M)
    omx2 = 1.0 - x * x
    if abs(omx2) < 1e-12:
        omx2 = math.copysign(1e-12, omx2)
    dT = (3.0 * T * x - 2.0 + 2.0 * ll ** 3 * x / y) / omx2
    d2T = (3.0 * T + 5.0 * x * dT + 2.0 * (1.0 - ll * ll) * ll ** 3 / y ** 3) / omx2
    d3T = (7.0 * x * d2T + 8.0 * dT - 6.0 * (1.0 - ll * ll) * ll ** 5 * x / y ** 5) / omx2
    return T, dT, d2T, d3T


def _householder(x0, T0, ll, M, tol=1e-11, maxiter=40):
    x = x0
    for _ in range(maxiter):
        T, dT, d2T, d3T = _dT_set(x, ll, M)
        f = T - T0
        denom = dT * (dT * dT - f * d2T) + d3T * f * f / 6.0
        if abs(denom) < 1e-30:
            break
        xn = x - f * (dT * dT - f * d2T / 2.0) / denom
        if abs(xn - x) < tol * (1.0 + abs(x)):
            return xn
        x = xn
    return x


def _initial_guess(T, ll, M, low_path):
    if M == 0:
        T0 = math.acos(ll) + ll * math.sqrt(1.0 - ll * ll)     # x = 0
        T1 = (2.0 / 3.0) * (1.0 - ll ** 3)                     # x = 1 (parabola)
        if T >= T0:
            return (T0 / T) ** (2.0 / 3.0) - 1.0
        elif T < T1:
            return 2.5 * T1 / T * (T1 - T) / (1.0 - ll ** 5) + 1.0
        else:                                                  # T1 < T < T0
            return math.exp(math.log(2.0) * math.log(T / T0) / math.log(T1 / T0)) - 1.0
    a = ((M * math.pi + math.pi) / (8.0 * T)) ** (2.0 / 3.0)
    b = ((8.0 * T) / (M * math.pi)) ** (2.0 / 3.0)
    x0l = (a - 1.0) / (a + 1.0)
    x0r = (b - 1.0) / (b + 1.0)
    return max(x0l, x0r) if low_path else min(x0l, x0r)


def lambert_izzo(mu, r1, r2, tof, M=0, prograde=True, low_path=True):
    """Solve Lambert's problem. Returns (v1, v2) velocity 3-tuples at r1, r2.

    mu: central-body gravitational parameter; r1,r2: position 3-tuples; tof: seconds.
    Robust across elliptic/hyperbolic; single-rev (M=0) is the interplanetary case.
    """
    r1n, r2n = vnorm(r1), vnorm(r2)
    c = vsub(r2, r1)
    cn = vnorm(c)
    s = 0.5 * (r1n + r2n + cn)
    i_r1, i_r2 = vunit(r1), vunit(r2)
    i_h = vunit(vcross(i_r1, i_r2))
    ll = math.sqrt(max(0.0, 1.0 - min(1.0, cn / s)))
    if i_h[2] < 0.0:
        ll = -ll
        i_t1, i_t2 = vunit(vcross(i_r1, i_h)), vunit(vcross(i_r2, i_h))
    else:
        i_t1, i_t2 = vunit(vcross(i_h, i_r1)), vunit(vcross(i_h, i_r2))
    if not prograde:
        ll = -ll
        i_t1, i_t2 = vscale(i_t1, -1.0), vscale(i_t2, -1.0)
    T = math.sqrt(2.0 * mu / (s ** 3)) * tof

    x0 = _initial_guess(T, ll, M, low_path)
    x = _householder(x0, T, ll, M)
    y = _y(x, ll)

    gamma = math.sqrt(mu * s / 2.0)
    rho = (r1n - r2n) / cn
    sigma = math.sqrt(max(0.0, 1.0 - rho * rho))
    Vr1 = gamma * ((ll * y - x) - rho * (ll * y + x)) / r1n
    Vr2 = -gamma * ((ll * y - x) + rho * (ll * y + x)) / r2n
    Vt1 = gamma * sigma * (y + ll * x) / r1n
    Vt2 = gamma * sigma * (y + ll * x) / r2n
    v1 = vadd(vscale(i_r1, Vr1), vscale(i_t1, Vt1))
    v2 = vadd(vscale(i_r2, Vr2), vscale(i_t2, Vt2))
    return v1, v2


# --------------------------------------------------------------------------- #
# Body state from kRPC (exact position, finite-difference velocity)           #
# --------------------------------------------------------------------------- #
def body_state(body, ut, ref, eps=5.0):
    """Heliocentric (in ``ref``) position and velocity 3-tuples of ``body`` at ``ut``.
    Position is exact (kRPC analytic Kepler); velocity is a centred finite difference.
    """
    o = body.orbit
    r = o.position_at(ut, ref)
    p_m = o.position_at(ut - eps, ref)
    p_p = o.position_at(ut + eps, ref)
    v = ((p_p[0] - p_m[0]) / (2 * eps),
         (p_p[1] - p_m[1]) / (2 * eps),
         (p_p[2] - p_m[2]) / (2 * eps))
    return r, v


# --------------------------------------------------------------------------- #
# Seeds                                                                        #
# --------------------------------------------------------------------------- #
def hohmann_time(r1, r2, mu):
    a_t = 0.5 * (r1 + r2)
    return math.pi * math.sqrt(a_t ** 3 / mu)


def synodic_period(T1, T2):
    d = abs(1.0 / T1 - 1.0 / T2)
    return (1.0 / d) if d > 0 else float("inf")


# --------------------------------------------------------------------------- #
# Window search - precise departure UT via Lambert over real positions        #
# --------------------------------------------------------------------------- #
def _departure_cost(dep_body, tgt_body, ut_dep, tof, ref, mu_sun):
    """Departure hyperbolic-excess speed |v_inf| for a transfer leaving at ut_dep
    and arriving tof later. Lower = cheaper departure. Returns (cost, vinf_vec)."""
    r1, v1b = body_state(dep_body, ut_dep, ref)
    r2, _ = body_state(tgt_body, ut_dep + tof, ref)
    try:
        v1, _v2 = lambert_izzo(mu_sun, r1, r2, tof, M=0, prograde=True)
    except (ValueError, ZeroDivisionError):
        return None, None
    vinf = vsub(v1, v1b)
    return vnorm(vinf), vinf


def _golden_min(f, a, b, iters=40):
    gr = (math.sqrt(5.0) - 1.0) / 2.0
    c = b - gr * (b - a)
    d = a + gr * (b - a)
    fc, fd = f(c), f(d)
    for _ in range(iters):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - gr * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + gr * (b - a)
            fd = f(d)
    return 0.5 * (a + b)


def find_transfer_window(sc, dep_name, tgt_name, ut_now=None, n_coarse=160,
                         tof_refine=True):
    """Precise next departure window from ``dep_name`` to ``tgt_name`` (any bodies
    sharing a parent, i.e. both orbit the Sun). Returns a dict:
        ut_dep      - optimal heliocentric departure UT
        tof         - time of flight (s)
        vinf_dep    - departure v_infinity vector (planet-relative, in ``ref``)
        vinf_mag    - |v_inf| (m/s)
        c3          - characteristic energy (m^2/s^2)
        synodic     - synodic period (s)
    Uses a coarse 1-D scan over departure UT (Hohmann TOF) + golden refine, then an
    optional light TOF refine. Real Kepler positions => correct date (fixes Bug 1)."""
    if ut_now is None:
        ut_now = sc.ut
    dep = sc.bodies[dep_name]
    tgt = sc.bodies[tgt_name]
    sun = dep.orbit.body                                   # shared parent (the Sun)
    ref = sun.non_rotating_reference_frame
    mu_sun = sun.gravitational_parameter
    R1 = dep.orbit.semi_major_axis
    R2 = tgt.orbit.semi_major_axis
    t_H = hohmann_time(R1, R2, mu_sun)
    T_syn = synodic_period(dep.orbit.period, tgt.orbit.period)

    def cost_at(ut_dep, tof):
        c, _ = _departure_cost(dep, tgt, ut_dep, tof, ref, mu_sun)
        return c if c is not None else 1e30

    # coarse scan over one synodic period for the global-min departure date
    best_ut, best_c = ut_now, 1e31
    for k in range(n_coarse + 1):
        ut = ut_now + T_syn * (k / n_coarse)
        c = cost_at(ut, t_H)
        if c < best_c:
            best_c, best_ut = c, ut
    # golden refine the departure UT (TOF fixed at Hohmann)
    span = T_syn / n_coarse
    ut_dep = _golden_min(lambda u: cost_at(u, t_H), best_ut - span, best_ut + span)

    tof = t_H
    if tof_refine:                                         # light 2-D: refine TOF too
        for _ in range(3):
            tof = _golden_min(lambda tf: cost_at(ut_dep, tf), 0.6 * t_H, 1.6 * t_H)
            ut_dep = _golden_min(lambda u: cost_at(u, tof), ut_dep - span, ut_dep + span)

    cmag, vinf = _departure_cost(dep, tgt, ut_dep, tof, ref, mu_sun)
    return {
        "ut_dep": ut_dep,
        "tof": tof,
        "vinf_dep": vinf,
        "vinf_mag": cmag,
        "c3": (cmag * cmag) if cmag else None,
        "synodic": T_syn,
        "hohmann_tof": t_H,
    }


# --------------------------------------------------------------------------- #
# Ejection geometry (patched conic) - eta = arccos(1/e)                        #
# --------------------------------------------------------------------------- #
def ejection_dv(vinf_mag, r_park, mu_dep):
    """Prograde dv to inject from a circular parking orbit of radius r_park onto a
    hyperbola with the given excess speed."""
    v_circ = math.sqrt(mu_dep / r_park)
    v_peri = math.sqrt(vinf_mag * vinf_mag + 2.0 * mu_dep / r_park)
    return v_peri - v_circ


def ejection_periapsis_direction(vinf_vec, r_park, mu_dep, h_hat):
    """Unit vector to the ejection periapsis (where the burn happens) so that the
    outgoing hyperbolic asymptote points along ``vinf_vec``. ``h_hat`` is the parking
    orbit's angular-momentum (normal) unit vector. The asymptote true anomaly is
    nu_inf = arccos(-1/e), so the periapsis is the asymptote rotated backward by that
    angle. Using arccos(+1/e) points at the complement and misses the SOI by a wide
    angle."""
    vinf_mag = vnorm(vinf_vec)
    e_hyp = 1.0 + r_park * vinf_mag * vinf_mag / mu_dep
    nu_inf = math.acos(max(-1.0, min(1.0, -1.0 / e_hyp)))
    return vunit(rotate_about_axis(vunit(vinf_vec), h_hat, -nu_inf)), nu_inf


def plan_ejection_node(sc, vessel, vinf_vec, ut_min):
    """Compute a PRECISE ejection maneuver node so the outgoing asymptote matches
    ``vinf_vec`` (planet-relative, in the planet non-rotating frame). Returns a dict
    {ut, prograde, normal, radial, dv, ut_min_gap}. Places the burn at the in-plane
    periapsis direction (eta before the asymptote) at the next alignment >= ut_min
    (the two-clock fix). Any out-of-plane v_inf component is carried as a normal burn.
    """
    planet = vessel.orbit.body
    ref = planet.non_rotating_reference_frame
    mu = planet.gravitational_parameter
    o = vessel.orbit
    r_park = o.semi_major_axis                              # ~circular parking radius
    # parking-orbit normal from current state
    r_ship = o.position_at(sc.ut, ref)
    eps = 3.0
    p_m = o.position_at(sc.ut - eps, ref)
    p_p = o.position_at(sc.ut + eps, ref)
    v_ship = ((p_p[0] - p_m[0]) / (2 * eps), (p_p[1] - p_m[1]) / (2 * eps), (p_p[2] - p_m[2]) / (2 * eps))
    h_hat = vunit(vcross(r_ship, v_ship))
    vinf_mag = vnorm(vinf_vec)
    # split v_inf into in-plane / out-of-plane (relative to the parking plane)
    vinf_oop = vdot(vinf_vec, h_hat)                        # signed out-of-plane component
    vinf_ip = vsub(vinf_vec, vscale(h_hat, vinf_oop))
    vinf_ip_hat = vunit(vinf_ip)
    e_hyp = 1.0 + r_park * vinf_mag * vinf_mag / mu
    # The outgoing asymptote (v_inf direction) sits at TRUE ANOMALY nu_inf = arccos(-1/e) AHEAD of the
    # periapsis (in the direction of motion). So the burn periapsis is v_inf rotated BACKWARD by nu_inf.
    # (Using arccos(+1/e) here was a 72-deg aim error -> 600x-SOI miss.)
    nu_inf = math.acos(max(-1.0, min(1.0, -1.0 / e_hyp)))
    r_peri_hat = vunit(rotate_about_axis(vinf_ip_hat, h_hat, -nu_inf))
    # UT of the next time the ship sits at r_peri_hat (uniform-rotation model, exact for circular)
    T_park = 2.0 * math.pi * math.sqrt(r_park ** 3 / mu)
    r_ship_hat = vunit(r_ship)
    ang = math.atan2(vdot(vcross(r_ship_hat, r_peri_hat), h_hat), vdot(r_ship_hat, r_peri_hat))
    if ang < 0:
        ang += 2.0 * math.pi
    ut = sc.ut + ang / (2.0 * math.pi) * T_park
    while ut < ut_min:
        ut += T_park
    dv = ejection_dv(vinf_mag, r_park, mu)
    return {"ut": ut, "prograde": dv, "normal": vinf_oop, "radial": 0.0,
            "dv": math.sqrt(dv * dv + vinf_oop * vinf_oop), "nu_inf_deg": math.degrees(nu_inf)}


def plan_transfer(sc, vessel, dep_name, tgt_name, ut_now=None):
    """End-to-end: find the precise window then the ejection node for ``vessel`` (in a
    parking orbit of ``dep_name``) toward ``tgt_name``. Returns {window, ejection}.
    Iterates the two-clock fix once (re-solve v_inf at the node UT)."""
    if ut_now is None:
        ut_now = sc.ut
    w = find_transfer_window(sc, dep_name, tgt_name, ut_now)
    node = plan_ejection_node(sc, vessel, w["vinf_dep"], w["ut_dep"])
    # one refinement pass: the planet moves between ut_dep and the node UT
    w2 = find_transfer_window(sc, dep_name, tgt_name, node["ut"] - 0.01 * w["synodic"])
    node = plan_ejection_node(sc, vessel, w2["vinf_dep"], w2["ut_dep"])
    return {"window": w2, "ejection": node}


if __name__ == "__main__":          # self-test against the live game (cross-check)
    import sys, yaml
    cfg = yaml.safe_load(open("configs/local-ksp.yaml", encoding="utf-8"))
    kc = cfg["krpc"]
    import krpc
    c = krpc.connect(name="tp-test", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center
    yr = 426 * 21600
    for tgt in ("Duna", "Eve"):
        w = find_transfer_window(sc, "Kerbin", tgt)
        print(f"Kerbin->{tgt}: dep in {(w['ut_dep']-sc.ut)/yr:.3f} yr  tof {w['tof']/yr:.3f} yr  "
              f"|vinf| {w['vinf_mag']:.0f} m/s  synodic {w['synodic']/yr:.2f} yr")
    c.close()
