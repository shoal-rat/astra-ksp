"""Offline tests for astra.physics: known KSP figures, inverse pairs, Lambert branch selection, calc safety."""

import math
import random

import pytest

from astra.physics import calc, lambert, orbits, rocket
from astra.physics.orbits import KeplerOrbit, vcross, vdot, vnorm, vsub

# Stock constants as read live from KSP 1.12.5 through kRPC.
MU_KERBIN, R_KERBIN = 3.5316e12, 600_000.0
MU_MUN, R_MUN, SOI_MUN, SMA_MUN = 6.5138398e10, 200_000.0, 2_429_559.1, 12_000_000.0
MU_SUN = 1.1723328e18
SMA_KERBIN, SMA_DUNA, SMA_EVE = 13_599_840_256.0, 20_726_155_264.0, 9_832_684_544.0
MU_DUNA, R_DUNA = 3.0136321e11, 320_000.0


def krpc_axes(kep: KeplerOrbit):
    """Ephemeris in kRPC's y-up, left-handed axes (x, y, z) -> (x, z, y), as the tool layer uses."""
    def state(ut):
        r, v = kep.state_at(ut)
        return (r[0], r[2], r[1]), (v[0], v[2], v[1])
    return state


# ---------------------------------------------------------------------------------------------
# vis-viva family


def test_circular_speed_low_kerbin_orbit():
    assert orbits.circular_speed(MU_KERBIN, R_KERBIN + 80_000) == pytest.approx(2278.93, abs=0.05)


def test_hohmann_lko_to_mun_and_capture():
    h = orbits.hohmann(MU_KERBIN, R_KERBIN + 80_000, SMA_MUN)
    assert h.dv1 == pytest.approx(856.36, abs=0.1)
    assert h.dv2 == pytest.approx(364.83, abs=0.1)  # = arrival v_inf relative to the Mun
    assert h.tof == pytest.approx(26_686.9, abs=1)
    assert math.degrees(orbits.phase_angle_for_transfer(MU_KERBIN, SMA_MUN, h.tof)) == pytest.approx(110.88, abs=0.02)
    cap = orbits.capture_dv(MU_MUN, R_MUN + 10_000, abs(h.dv2), R_MUN + 10_000)
    assert cap == pytest.approx(311.1, abs=0.2)


def test_hohmann_is_signed_both_ways():
    up = orbits.hohmann(MU_KERBIN, 700_000, 2_000_000)
    down = orbits.hohmann(MU_KERBIN, 2_000_000, 700_000)
    assert up.dv1 > 0 and up.dv2 > 0 and down.dv1 < 0 and down.dv2 < 0
    assert up.total == pytest.approx(down.total)


def test_interplanetary_reference_figures():
    v_dep, v_arr = orbits.hohmann_excess_speeds(MU_SUN, SMA_KERBIN, SMA_DUNA)
    assert v_dep == pytest.approx(918, abs=2) and v_arr == pytest.approx(826, abs=2)
    assert orbits.ejection_dv(MU_KERBIN, R_KERBIN + 80_500, v_dep) == pytest.approx(1072, abs=3)
    t_eve = orbits.hohmann(MU_SUN, SMA_KERBIN, SMA_EVE).tof
    assert math.degrees(orbits.phase_angle_for_transfer(MU_SUN, SMA_EVE, t_eve)) == pytest.approx(-54.1, abs=0.2)


def test_apsis_change_matches_hohmann_and_capture():
    r1, r2 = R_KERBIN + 80_000, 5_000_000.0
    h = orbits.hohmann(MU_KERBIN, r1, r2)
    assert orbits.apsis_change_dv(MU_KERBIN, r1, r1, r2) == pytest.approx(h.dv1)
    assert orbits.circularize_dv(MU_KERBIN, r2, h.a_transfer) == pytest.approx(h.dv2)
    # capture from a hyperbola is an apsis change with negative semi-major axis
    v_inf = 400.0
    a_hyp = -MU_MUN / v_inf ** 2
    r_pe = R_MUN + 20_000
    assert -orbits.apsis_change_dv(MU_MUN, r_pe, a_hyp, r_pe) == pytest.approx(
        orbits.capture_dv(MU_MUN, r_pe, v_inf, r_pe))


def test_deorbit_dv():
    r_ap = R_KERBIN + 100_000
    dv = orbits.deorbit_dv(MU_KERBIN, r_ap, r_ap, R_KERBIN + 30_000)
    assert 0 < dv < 100
    with pytest.raises(ValueError):
        orbits.deorbit_dv(MU_KERBIN, r_ap, R_KERBIN + 30_000, R_KERBIN + 50_000)


def test_invalid_inputs_raise_instead_of_returning_zero():
    with pytest.raises(ValueError):
        orbits.circular_speed(MU_KERBIN, 0)
    with pytest.raises(ValueError):
        orbits.circular_speed(-1, 1e6)
    with pytest.raises(ValueError):
        orbits.vis_viva_speed(MU_KERBIN, 3e6, 1e6)  # beyond the apoapsis of that ellipse
    with pytest.raises(ValueError):
        orbits.orbital_period(MU_KERBIN, -1e6)
    with pytest.raises(ValueError):
        orbits.capture_dv(MU_MUN, 250_000, 300, 220_000)


def test_plane_change_and_flyby():
    assert orbits.plane_change_dv(2000, math.radians(60)) == pytest.approx(2000)
    turn = orbits.flyby_turn_angle(MU_MUN, R_MUN + 10_000, 500)
    e = 1 + (R_MUN + 10_000) * 500 ** 2 / MU_MUN
    assert turn == pytest.approx(2 * math.asin(1 / e))


def test_synchronous_radius_kerbin():
    assert orbits.synchronous_radius(MU_KERBIN, 21_549.425) - R_KERBIN == pytest.approx(2_863_334, abs=50)


# ---------------------------------------------------------------------------------------------
# anomalies, elements, propagation


@pytest.mark.parametrize("e", [0.0, 0.3, 0.95, 1.5, 3.0])
def test_anomaly_round_trip(e):
    for nu in (0.1, 1.0, 2.0, -1.5):
        if e >= 1 and abs(nu) >= math.acos(-1 / e):
            continue
        m = orbits.true_to_mean(nu, e)
        assert orbits.wrap_pi(orbits.mean_to_true(m, e) - nu) == pytest.approx(0, abs=1e-10)


def test_eccentric_anomaly_and_keplers_equation():
    e, nu = 0.4, 1.2
    big_e = orbits.true_to_eccentric(nu, e)
    assert math.cos(big_e) == pytest.approx((e + math.cos(nu)) / (1 + e * math.cos(nu)))
    m = orbits.eccentric_to_mean(big_e, e)
    assert orbits.mean_to_eccentric(m, e) == pytest.approx(big_e)
    assert orbits.eccentric_to_true(big_e, e) == pytest.approx(nu)
    h = orbits.true_to_eccentric(0.8, 1.8)  # hyperbolic anomaly
    assert math.cosh(h) == pytest.approx((1.8 + math.cos(0.8)) / (1 + 1.8 * math.cos(0.8)))
    assert orbits.mean_to_eccentric(orbits.eccentric_to_mean(h, 1.8), 1.8) == pytest.approx(h)


def test_time_between_anomalies_is_forward_on_ellipses():
    a, e = 1.5e6, 0.2
    t = orbits.time_between_anomalies(MU_KERBIN, a, e, 3.0, 0.5)
    assert 0 < t < orbits.orbital_period(MU_KERBIN, a)
    assert orbits.time_between_anomalies(MU_KERBIN, a, e, 0.0, math.pi) == pytest.approx(
        orbits.orbital_period(MU_KERBIN, a) / 2)


def test_state_elements_round_trip():
    rng = random.Random(7)
    for _ in range(300):
        a, e = rng.uniform(7e5, 5e7), rng.uniform(0, 0.9)
        angles = [rng.uniform(0, math.pi)] + [rng.uniform(0, math.tau) for _ in range(3)]
        r, v = orbits.state_from_elements(MU_KERBIN, a, e, *angles)
        el = orbits.classical_elements(MU_KERBIN, r, v)
        r2, v2 = orbits.state_from_elements(MU_KERBIN, el["a"], el["e"], el["inc"], el["lan"], el["argpe"], el["nu"])
        assert vnorm(vsub(r, r2)) < 1e-6 * vnorm(r) and vnorm(vsub(v, v2)) < 1e-6 * vnorm(v)


@pytest.mark.parametrize("e", [0.0, 0.4, 2.0])
def test_propagation_agrees_with_kepler_elements(e):
    a = 3e6 if e < 1 else -3e6
    kep = KeplerOrbit(MU_KERBIN, a, e, 0.3, 1.1, 0.7, 0.2, 1000.0)
    r0, v0 = kep.state_at(1000.0)
    for dt in (60.0, 3000.0, 20_000.0, -500.0):
        r1, v1 = orbits.propagate(MU_KERBIN, r0, v0, dt)
        r_ref, v_ref = kep.state_at(1000.0 + dt)
        assert vnorm(vsub(r1, r_ref)) < 1e-6 * vnorm(r_ref)
        assert vnorm(vsub(v1, v_ref)) < 1e-6 * vnorm(v_ref)


@pytest.mark.parametrize("e, a, m0, periods", [
    (0.9674, 2.74e10, 6.173, 1.268), (0.9944, 4.54e10, 0.655, 0.653), (0.9643, 1.05e10, 0.935, 0.667),
    (0.9891, 5.98e10, 1.216, 0.654), (0.3, 5e10, 0.3, 7.25)])
def test_propagation_converges_on_near_parabolic_ellipses(e, a, m0, periods):
    """Plain Newton on the universal Kepler equation did not converge on the first four (found by fuzzing)."""
    kep = KeplerOrbit(MU_SUN, a, e, 0.1, 0.2, 0.3, m0, 0.0)
    r0, v0 = kep.state_at(0.0)
    dt = periods * kep.period
    r1, v1 = orbits.propagate(MU_SUN, r0, v0, dt)
    r_ref, v_ref = kep.state_at(dt)
    assert vnorm(vsub(r1, r_ref)) < 1e-6 * vnorm(r_ref)
    assert vnorm(vsub(v1, v_ref)) < 1e-6 * vnorm(v_ref)


def test_propagation_fast_hyperbola_from_lambert():
    # a short heliocentric arc: ~400 km/s hyperbola, where a tight tolerance used to stall on noise
    r1, r2 = (1.97e10, 0.0, 0.0), (-3e10, 1e9, 4e10)
    v1, _ = lambert.lambert(MU_SUN, r1, r2, 131_835.0, normal=(0.0, -1.0, 0.0))
    r_end, _ = orbits.propagate(MU_SUN, r1, v1, 131_835.0)
    assert vnorm(vsub(r_end, r2)) < 1e-6 * vnorm(r2)


def test_speed_for_apsis_at_the_apsis_itself_is_circular():
    r = R_KERBIN + 90_000
    assert orbits.speed_for_apsis(MU_KERBIN, r, 0.0, r) == pytest.approx(orbits.circular_speed(MU_KERBIN, r))
    assert orbits.speed_for_apsis(MU_KERBIN, r, 1e-13, r) == pytest.approx(orbits.circular_speed(MU_KERBIN, r))
    with pytest.raises(ValueError):  # climbing through r: r cannot be an apsis
        orbits.speed_for_apsis(MU_KERBIN, r, math.radians(5), r)


def test_next_ut_at_true_anomaly():
    kep = KeplerOrbit(MU_KERBIN, 1e6, 0.1, mean_anomaly_at_epoch=1.0)
    t = kep.next_ut_at(math.pi, 0.0)
    assert orbits.wrap_pi(kep.true_anomaly_at(t) - math.pi) == pytest.approx(0, abs=1e-8)
    hyp = KeplerOrbit(MU_KERBIN, -1e6, 1.5, mean_anomaly_at_epoch=1.0)  # outbound, past periapsis
    with pytest.raises(ValueError):
        hyp.next_ut_at(0.0, 0.0)


def test_speed_for_apsis_general_point():
    r, fpa = R_KERBIN + 150_000, math.radians(4)
    for target in (R_KERBIN + 400_000, R_KERBIN + 30_000):
        v = orbits.speed_for_apsis(MU_KERBIN, r, fpa, target)
        rv = (r, 0.0, 0.0)
        vv = (v * math.sin(fpa), v * math.cos(fpa), 0.0)
        el = orbits.elements_from_state(MU_KERBIN, rv, vv)
        got = el["r_ap"] if target > r else el["r_pe"]
        assert got == pytest.approx(target, rel=1e-9)
    # at zero flight-path angle it is vis-viva on the ellipse through both radii
    assert orbits.speed_for_apsis(MU_KERBIN, 7e5, 0.0, 9e5) == pytest.approx(
        orbits.vis_viva_speed(MU_KERBIN, 7e5, 8e5))


def test_signed_angle_is_handedness_safe():
    r, v = (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)  # kRPC-like: prograde h = cross(r, v) along -y
    h = vcross(r, v)
    ahead = orbits.rotate_about(r, h, 0.3)
    assert vdot(vsub(ahead, r), v) > 0  # positive rotation about h moves along the motion
    assert orbits.signed_angle(r, ahead, h) == pytest.approx(0.3)


def test_find_zero_ignores_wraparounds():
    fn = lambda t: orbits.wrap_pi(3.0 - t)  # jumps from -pi to +pi at t = 3 + pi
    assert orbits.find_zero(fn, 0.0, 10.0, 50) == pytest.approx(3.0, abs=1e-3)
    assert orbits.find_zero(lambda t: t * t + 1, 0.0, 1.0, 10) is None


# ---------------------------------------------------------------------------------------------
# Lambert


def _lambert_round_trip(mu, r1, r2, tof, normal, **kw):
    v1, v2 = lambert.lambert(mu, r1, r2, tof, normal=normal, **kw)
    r_end, v_end = orbits.propagate(mu, r1, v1, tof)
    assert vnorm(vsub(r_end, r2)) < 1e-6 * vnorm(r2)
    assert vnorm(vsub(v_end, v2)) < 1e-6 * vnorm(v2)
    return v1, v2


@pytest.mark.parametrize("ahead_deg", [30, 100, 170, 190, 260, 340])
@pytest.mark.parametrize("y_offset", [-1e6, 0.0, 1e6])
def test_lambert_prograde_branch_in_y_up_frame(ahead_deg, y_offset):
    """kRPC frames: y is north, orbits lie in x-z, and prograde cross(r, v) points along -y. The old
    solver tested the z-component and returned ~19 km/s retrograde arcs for half of these cases."""
    v_k = math.sqrt(MU_SUN / SMA_KERBIN)
    r1, v_origin = (SMA_KERBIN, 0.0, 0.0), (0.0, 0.0, v_k)
    normal = vcross(r1, v_origin)
    assert normal[1] < 0
    r2 = orbits.rotate_about((SMA_DUNA, 0.0, 0.0), normal, math.radians(ahead_deg))
    r2 = (r2[0], y_offset, r2[2])
    tof = orbits.hohmann(MU_SUN, SMA_KERBIN, SMA_DUNA).tof * (0.6 + ahead_deg / 360)
    v1, _ = _lambert_round_trip(MU_SUN, r1, r2, tof, normal)
    v1_retro, _ = _lambert_round_trip(MU_SUN, r1, r2, tof, normal, prograde=False)
    assert vdot(vcross(r1, v1), normal) > 0 > vdot(vcross(r1, v1_retro), normal)
    assert vnorm(vsub(v1, v_origin)) < vnorm(vsub(v1_retro, v_origin))


def test_lambert_old_failure_case_is_fixed():
    v_k = math.sqrt(MU_SUN / SMA_KERBIN)
    r1, v_origin = (SMA_KERBIN, 0.0, 0.0), (0.0, 0.0, v_k)
    normal = vcross(r1, v_origin)
    r2 = orbits.rotate_about((SMA_DUNA, 0.0, 0.0), normal, math.radians(170))
    r2 = (r2[0], -1e6, r2[2])
    v1, _ = lambert.lambert(MU_SUN, r1, r2, 0.9 * orbits.hohmann(MU_SUN, SMA_KERBIN, SMA_DUNA).tof, normal=normal)
    assert vnorm(vsub(v1, v_origin)) == pytest.approx(948, abs=5)


def test_lambert_retrograde_request_and_frame_invariance():
    r1, r2 = (7e6, 0.0, 0.0), (0.0, 0.0, 9e6)
    v_ref = (0.0, 0.0, 1.0)  # the sense of motion we call prograde
    normal = vcross(r1, v_ref)
    v_pro, _ = _lambert_round_trip(MU_KERBIN, r1, r2, 4000.0, normal)
    v_ret, _ = _lambert_round_trip(MU_KERBIN, r1, r2, 4000.0, normal, prograde=False)
    assert vdot(vcross(r1, v_pro), normal) > 0 > vdot(vcross(r1, v_ret), normal)
    # Swapping two axes flips handedness. A normal rebuilt as cross(r, v) inside the swapped frame (as
    # evaluate_transfer does) gives the same physical transfer back; the swapped pseudo-vector would not.
    sw = lambda p: (p[0], p[2], p[1])
    v_sw, _ = lambert.lambert(MU_KERBIN, sw(r1), sw(r2), 4000.0, normal=vcross(sw(r1), sw(v_ref)))
    assert vnorm(vsub(sw(v_sw), v_pro)) < 1e-6
    v_wrong, _ = lambert.lambert(MU_KERBIN, sw(r1), sw(r2), 4000.0, normal=sw(normal))
    assert vnorm(vsub(sw(v_wrong), v_ret)) < 1e-6


def test_lambert_hyperbolic_multirev_and_degenerate():
    r1, r2 = (7e6, 0.0, 0.0), (0.0, 1e6, 8e6)
    _lambert_round_trip(MU_KERBIN, r1, r2, 600.0, (0.0, 0.0, 1.0))  # fast hyperbolic arc
    period = orbits.orbital_period(MU_KERBIN, 8e6)
    for low in (True, False):
        _lambert_round_trip(MU_KERBIN, r1, r2, 1.6 * period, (0.0, 0.0, 1.0), revs=1, low_path=low)
    with pytest.raises(ValueError):
        lambert.lambert(MU_KERBIN, r1, r2, 0.2 * period, normal=(0.0, 0.0, 1.0), revs=1)
    with pytest.raises(ValueError):
        lambert.lambert(MU_KERBIN, (7e6, 0.0, 0.0), (-8e6, 0.0, 0.0), 5000.0, normal=(0.0, 1.0, 0.0))


def test_lambert_refuses_an_unconverged_iterate():
    # one Householder step from a poor start is not a solution: it must raise, not return it
    ll, t = 0.3, 2.0
    with pytest.raises(ValueError, match="converge"):
        lambert._householder(0.9, t, ll, 0, maxiter=1)
    x = lambert._householder(0.0, t, ll, 0)
    assert lambert._tof(x, ll, 0) == pytest.approx(t, rel=1e-12)


def test_transfer_window_search_kerbin_duna():
    kerbin = KeplerOrbit(MU_SUN, SMA_KERBIN, 0.0, mean_anomaly_at_epoch=3.14)
    duna = KeplerOrbit(MU_SUN, SMA_DUNA, 0.051, 0.00105, 2.3649, 0.0, 3.14)
    t_h = orbits.hohmann(MU_SUN, SMA_KERBIN, SMA_DUNA).tof
    syn = orbits.synodic_period(kerbin.period, duna.period)
    ws = lambert.search_transfer_window(MU_SUN, krpc_axes(kerbin), krpc_axes(duna), t_start=0.0, t_end=syn,
                                        tof_min=0.5 * t_h, tof_max=1.5 * t_h, cost=lambda d, a: d,
                                        n_departure=60, n_tof=24)
    best = ws.best
    assert 600 < best.v_inf_departure_mag < 1000
    assert 0.5 * t_h <= best.tof <= 1.5 * t_h
    r1, _ = krpc_axes(kerbin)(best.departure_ut)
    r2, _ = krpc_axes(duna)(best.arrival_ut)
    v1 = vsub(best.v_inf_departure, vsub((0, 0, 0), best.origin_velocity))  # v1 = v_inf + v_origin
    r_end, _ = orbits.propagate(MU_SUN, r1, v1, best.tof)
    assert vnorm(vsub(r_end, r2)) < 1e-6 * SMA_DUNA
    assert all(best.cost <= c for row in ws.grid_cost for c in row if c is not None)


# ---------------------------------------------------------------------------------------------
# burn planners over ephemerides


def test_plan_transfer_to_satellite_hits_the_moon():
    craft = KeplerOrbit(MU_KERBIN, R_KERBIN + 80_000, 0.0, mean_anomaly_at_epoch=0.3)
    moon = KeplerOrbit(MU_KERBIN, SMA_MUN, 0.0, mean_anomaly_at_epoch=1.7)
    p = orbits.plan_transfer_to_satellite(MU_KERBIN, krpc_axes(craft), krpc_axes(moon), 1000.0)
    assert p["ut"] >= 1000.0
    assert p["prograde_dv"] == pytest.approx(856.36, abs=0.1)
    assert math.degrees(p["lead_angle"]) == pytest.approx(110.88, abs=0.05)
    r, v = krpc_axes(craft)(p["ut"])
    r_end, _ = orbits.propagate(MU_KERBIN, r, orbits.vadd(v, orbits.vscale(orbits.vunit(v), p["prograde_dv"])), p["tof"])
    assert vnorm(vsub(r_end, krpc_axes(moon)(p["ut"] + p["tof"])[0])) < 100


def test_plan_escape_from_moon_reaches_target_periapsis():
    moon = KeplerOrbit(MU_KERBIN, SMA_MUN, 0.0)
    craft = KeplerOrbit(MU_MUN, R_MUN + 20_000, 0.0, 0.2, 0.5)
    p = orbits.plan_escape_from_satellite(MU_KERBIN, MU_MUN, krpc_axes(craft), krpc_axes(moon),
                                          R_KERBIN + 35_000, 100.0, SOI_MUN)
    assert 250 < p["prograde_dv"] < 320
    # independent check: burn, fly the moon-centric conic to the SOI edge, convert to Kerbin-centric
    r, v = krpc_axes(craft)(p["ut"])
    v2 = orbits.vadd(v, orbits.vscale(orbits.vunit(v), p["prograde_dv"]))
    dt, r_e, v_e = orbits.soi_exit(MU_MUN, r, v2, SOI_MUN)
    rs, vs = krpc_axes(moon)(p["ut"] + dt)
    el = orbits.elements_from_state(MU_KERBIN, orbits.vadd(rs, r_e), orbits.vadd(vs, v_e))
    assert el["r_pe"] - R_KERBIN == pytest.approx(35_000, abs=50)


def test_plan_ejection_points_the_asymptote():
    craft = KeplerOrbit(MU_KERBIN, R_KERBIN + 100_000, 0.0)
    want = orbits.vscale(orbits.vunit((0.3, 0.0, -1.0)), 900.0)  # in the craft's plane (x-z)
    p = orbits.plan_ejection(MU_KERBIN, krpc_axes(craft), want, 50_000.0, 84_159_286.0)
    r, v = krpc_axes(craft)(p["ut"])
    v2 = orbits.vadd(v, orbits.vscale(orbits.vunit(v), p["prograde_dv"]))
    _, _, v_far = orbits.soi_exit(MU_KERBIN, r, v2, 1e11)  # far out the velocity ~ the asymptote
    assert math.degrees(orbits.vangle(v_far, want)) < 0.5
    assert p["prograde_dv"] == pytest.approx(orbits.ejection_dv(MU_KERBIN, R_KERBIN + 100_000, 900.0), abs=0.01)


# ---------------------------------------------------------------------------------------------
# rocket and flight mechanics


def test_rocket_equation_inverses():
    dv = rocket.delta_v(320, 12_000, 7_000)
    assert rocket.final_mass(320, dv, 12_000) == pytest.approx(7_000)
    assert rocket.initial_mass(320, dv, 7_000) == pytest.approx(12_000)
    assert rocket.propellant_for_dv(320, dv, 7_000) == pytest.approx(5_000)
    assert rocket.delta_v(320, 5_000, 5_000) == 0
    with pytest.raises(ValueError):
        rocket.delta_v(320, 5_000, 6_000)


def test_burn_time_with_mass_flow():
    assert rocket.burn_time(1000, 10_000, 200_000, 345) == pytest.approx(43.3, abs=0.1)
    assert rocket.burn_time(1000, 10_000, 200_000) == pytest.approx(50.0)  # constant mass
    half = rocket.half_dv_time(1000, 10_000, 200_000, 345)
    assert 43.3 / 2 < half < 25.0
    assert rocket.combined_isp([(100, 300), (100, 300)]) == pytest.approx(300)
    assert rocket.combined_isp([(200, 350), (100, 250)]) == pytest.approx(300 / (200 / 350 + 100 / 250))


def test_twr_and_hover():
    g = rocket.gravity_at(MU_MUN, R_MUN)
    assert g == pytest.approx(1.628, abs=0.001)
    assert rocket.twr(16_280, 1000, g) == pytest.approx(10, rel=1e-3)
    assert rocket.hover_throttle(16_280, 1000, g) == pytest.approx(0.1, rel=1e-3)


def test_vertical_stop_and_simulated_retro_burn_agree():
    vs = rocket.vertical_stop(100, 5.0, 1.63)
    assert vs.distance_m == pytest.approx(100 ** 2 / (2 * 3.37))
    sim = rocket.retro_burn(0.0, -100.0, 1000, 5000, 1.63)
    assert sim.stopped
    assert sim.altitude_lost_m == pytest.approx(vs.distance_m, rel=2e-3)
    assert sim.time_s == pytest.approx(vs.time_s, rel=2e-3)
    with pytest.raises(ValueError):
        rocket.vertical_stop(10, 1.0, 1.63)


def test_retro_burn_with_horizontal_speed_and_mass_loss():
    heavy = rocket.retro_burn(500.0, -30.0, 5000, 30_000, 1.63)
    light = rocket.retro_burn(500.0, -30.0, 5000, 30_000, 1.63, isp_s=320)
    assert heavy.stopped and light.stopped
    assert heavy.downrange_m > 0 and heavy.altitude_lost_m > 0
    assert light.altitude_lost_m < heavy.altitude_lost_m  # lighter craft brakes harder
    assert light.final_mass_kg < 5000
    assert heavy.dv_used_mps > 500  # gravity losses on top of cancelling the speed


def test_retro_burn_rejects_non_finite_speed_fast():
    import time
    t0 = time.perf_counter()
    for bad in (math.nan, math.inf):
        with pytest.raises(ValueError, match="finite"):
            rocket.retro_burn(10.0, bad, 1000, 5000, 1.63)
    assert time.perf_counter() - t0 < 0.5  # a NaN used to spin 2 million RK4 steps


def test_launch_azimuth_at_a_pole_raises_value_error():
    with pytest.raises(ValueError, match="poles"):
        rocket.launch_azimuth(math.radians(90), math.radians(90))


def test_drag_helpers_are_inverse():
    vt = rocket.terminal_velocity(1200, 9.81, 1.2, 50.0)
    assert rocket.drag_area_from_terminal_velocity(1200, 9.81, 1.2, vt) == pytest.approx(50.0)
    assert rocket.dynamic_pressure(1.2, 100) == pytest.approx(6000)


def test_ascent_models():
    v_c = 2400.0
    losses = [rocket.level_burn_dv(0, v_c, n) - v_c for n in (1.2, 1.5, 2, 4, 50)]
    assert all(a > b > 0 for a, b in zip(losses, losses[1:]))
    assert losses[-1] < 1.0
    with pytest.raises(ValueError):
        rocket.level_burn_dv(0, v_c, 1.0)
    assert rocket.vertical_climb_gravity_loss(9.81, 2.0, 10_000) == pytest.approx(9.81 * math.sqrt(2 * 10_000 / 9.81))
    profile = [(h, 1e5 * math.exp(-h / 5000)) for h in range(0, 70_001, 5000)]
    assert rocket.altitude_at_pressure_fraction(profile, math.exp(-2)) == pytest.approx(10_000, rel=1e-6)
    assert math.degrees(rocket.launch_azimuth(0.0, 0.0)) == pytest.approx(90)
    with pytest.raises(ValueError):
        rocket.launch_azimuth(math.radians(30), math.radians(10))


# ---------------------------------------------------------------------------------------------
# calc


@pytest.mark.parametrize("expr, value", [
    ("1 + 2 * 3", 7), ("2 ** 10", 1024), ("2 ^ 3", 8), ("-3 ** 2", -9), ("7 % 4 + 7 // 2", 6),
    ("sqrt(16) + hypot(3, 4)", 9), ("degrees(atan2(1, 1))", 45), ("log(8, 2)", 3), ("max(1, 5, 3)", 5),
    ("g0 * 300", 2941.995), ("pi == pi", True), ("1 < 2 < 3", True), ("3 if 2 > 1 else 4", 3),
    ("not 0 and 5", 5), ("floor(2.7) + ceil(2.1) + abs(-1)", 6),
])
def test_calc_values(expr, value):
    assert calc.evaluate(expr)["value"] == pytest.approx(value)


def test_calc_worksheet_and_variables():
    out = calc.evaluate("ve = isp * g0\ndv = ve * log(m0 / m1); dv", {"isp": 320, "m0": 12.5, "m1": 7.5})
    assert out["value"] == pytest.approx(320 * 9.80665 * math.log(12.5 / 7.5))
    assert set(out["assigned"]) == {"ve", "dv"}


@pytest.mark.parametrize("expr", [
    "__import__('os')", "(1).__class__", "math.pi", "lambda: 1", "[x for x in (1, 2)]", "'abc'",
    "open('x')", "(x := 3)", "sqrt(x=4)", "[1, 2][0]", "import os", "eval('1')", "globals()",
    "sqrt", "foo + 1", "1 +", "",
])
def test_calc_rejects_unsafe_or_invalid(expr):
    with pytest.raises(calc.CalcError):
        calc.evaluate(expr)


@pytest.mark.parametrize("expr", ["not " * 900 + "1", "-" * 1990 + "1", "(" * 300 + "1" + ")" * 300])
def test_calc_deep_nesting_is_a_clean_error(expr):
    with pytest.raises(calc.CalcError):  # a RecursionError used to escape
        calc.evaluate(expr)


@pytest.mark.parametrize("expr", ["10 ** 10 ** 10", "1 / 0", "sqrt(-1)", "(-8) ** (1/3)", "acos(2)"])
def test_calc_math_errors_are_clean(expr):
    with pytest.raises(calc.CalcError):
        calc.evaluate(expr)


def test_calc_syntax_warnings_stay_off_stderr():
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert calc.evaluate("1if 1 else 2")["value"] == 1  # Python warns "invalid decimal literal"
    assert not [w for w in caught if issubclass(w.category, SyntaxWarning)]


def test_calc_rejects_bad_variable_names():
    for bad in ({"sqrt": 1.0}, {"__x": 1.0}, {"a b": 1.0}):
        with pytest.raises(calc.CalcError):
            calc.evaluate("1", bad)
    with pytest.raises(calc.CalcError):
        calc.evaluate("pi = 3")
