"""Validate the calculated physics core against known KSP/orbital-mechanics results.

These are the PROOF that the commander's numbers are CALCULATED, not guessed. Every assertion below
ties a function in `ksp_lab.astro` to a closed-form physical truth (the Oberth ejection, vis-viva,
terminal velocity, the rocket equation, the hoverslam reference curve). The terminal-velocity case in
particular is a regression test for the real crew-death root cause: a single Mk16 chute on Duna's thin
air falls at ~30 m/s (lethal), and only a multi-chute pack brings it under ~10 m/s.
"""
from __future__ import annotations

import math

import pytest

from ksp_lab import astro

# Live-measured KSP body constants (from kRPC), passed in — never hardcoded inside astro.py.
MU_SUN = 1.1723328e18          # Sun gravitational parameter (m^3/s^2)
MU_KERBIN = 3.5316e12          # Kerbin GM
R_KERBIN_ORBIT = 13_599_840_256  # Kerbin's heliocentric orbital radius (m)
R_DUNA_ORBIT = 20_726_155_264    # Duna's heliocentric orbital radius (m)
R_PARK = 700_000               # 100 km parking orbit: Kerbin radius 600 km + 100 km

# Duna surface, measured live: thin atmosphere, low gravity.
DUNA_G = 2.944
DUNA_RHO = 0.13334
MK16_CD_A = 489.0              # one Mk16 parachute's drag area Cd*A (m^2)


# --------------------------------------------------------------------------------------------------
# Oberth ejection — the headline interplanetary number, validated live at Kerbin->Duna = 1060 m/s.
# --------------------------------------------------------------------------------------------------

def test_kerbin_to_duna_ejection_is_oberth_calculated():
    """Kerbin->Duna departure from a 100 km parking orbit must land in the known ~1050-1075 m/s band.
    This is THE proof the ejection is physics (heliocentric Hohmann -> v_infinity -> Oberth burn), not
    a guessed magic number. Live game value is ~1060 m/s."""
    dep = astro.interplanetary_departure(
        mu_sun=MU_SUN, mu_body=MU_KERBIN,
        r_body_orbit=R_KERBIN_ORBIT, r_target_orbit=R_DUNA_ORBIT, r_park=R_PARK,
    )
    assert 1050.0 <= dep["ejection_dv"] <= 1075.0, dep
    # Sanity on the pieces: heliocentric excess must be positive and the transfer must take months.
    assert dep["v_infinity"] > 0.0
    assert dep["transfer_time_s"] > 0.0
    assert 0.0 <= dep["phase_angle_rad"] < 2.0 * math.pi


def test_oberth_beats_naive_escape_plus_vinf():
    """The Oberth burn from low orbit is cheaper than escaping then adding v_infinity in deep space.
    ejection_dv must be strictly less than (escape_dv + v_infinity) — the whole point of burning deep
    in the gravity well."""
    v_inf = 918.0
    v_park = math.sqrt(MU_KERBIN / R_PARK)
    v_escape = math.sqrt(2.0) * v_park
    naive = (v_escape - v_park) + v_inf
    oberth = astro.oberth_ejection_dv(MU_KERBIN, R_PARK, v_inf)
    assert oberth < naive
    assert oberth > 0.0


# --------------------------------------------------------------------------------------------------
# vis-viva consistency — circular_speed is vis-viva with a == r.
# --------------------------------------------------------------------------------------------------

def test_circular_speed_matches_vis_viva_at_a_equals_r():
    for r in (700_000, 1_000_000, 13_599_840_256):
        mu = MU_KERBIN if r < 1e9 else MU_SUN
        assert astro.vis_viva_speed(mu, r, r) == pytest.approx(astro.circular_speed(mu, r), rel=1e-12)


def test_vis_viva_higher_on_ellipse_at_periapsis_than_circular():
    """At periapsis of an ellipse (a > r), speed exceeds the circular speed at that radius."""
    r = R_PARK
    a = 1.5 * R_PARK
    assert astro.vis_viva_speed(MU_KERBIN, r, a) > astro.circular_speed(MU_KERBIN, r)


def test_hohmann_depart_raises_apoapsis_consistently():
    """Hohmann depart burn from r1 equals the vis-viva excess over circular at r1 on the transfer
    ellipse — a pure cross-check of the two functions."""
    r1, r2 = R_KERBIN_ORBIT, R_DUNA_ORBIT
    dv_depart, dv_arrive, t = astro.hohmann(MU_SUN, r1, r2)
    a = (r1 + r2) / 2.0
    expected = astro.vis_viva_speed(MU_SUN, r1, a) - astro.circular_speed(MU_SUN, r1)
    assert dv_depart == pytest.approx(expected, rel=1e-12)
    assert dv_depart > 0.0 and dv_arrive > 0.0 and t > 0.0


# --------------------------------------------------------------------------------------------------
# Terminal velocity — the crew-death regression. v scales as 1/sqrt(rho); one Mk16 on Duna kills.
# --------------------------------------------------------------------------------------------------

def test_terminal_velocity_scales_as_inverse_sqrt_rho():
    """Halving... quartering the density should raise terminal velocity by sqrt of the ratio."""
    v_dense = astro.terminal_velocity(5.0, 9.81, 4.0, 50.0)
    v_thin = astro.terminal_velocity(5.0, 9.81, 1.0, 50.0)
    # rho 4 -> 1 is a 4x drop, so v rises by sqrt(4) = 2.
    assert v_thin / v_dense == pytest.approx(2.0, rel=1e-12)


def test_single_mk16_on_duna_is_lethal_but_ten_chutes_are_safe():
    """THE crew-death root cause, encoded as a regression test. A 10 t craft hanging on ONE Mk16
    (Cd*A=489) in Duna's thin air (rho=0.13334, g=2.944) terminal-falls at ~30 m/s — a lethal impact.
    Ten Mk16s (10x the drag area) bring it under 10 m/s — survivable. The designer must therefore
    calculate a multi-chute pack, never a single chute."""
    mass_t = 10.0
    v_one = astro.terminal_velocity(mass_t, DUNA_G, DUNA_RHO, MK16_CD_A)
    v_ten = astro.terminal_velocity(mass_t, DUNA_G, DUNA_RHO, MK16_CD_A * 10.0)
    assert v_one == pytest.approx(30.0, abs=1.0)   # ~30 m/s: lethal
    assert v_one > 25.0                            # unambiguously deadly on one chute
    assert v_ten < 10.0                            # ~9.5 m/s: survivable on ten
    # Ten chutes is 10x the area, so exactly sqrt(10) slower than one — pure 1/sqrt(area) physics.
    assert v_one / v_ten == pytest.approx(math.sqrt(10.0), rel=1e-9)


def test_terminal_velocity_infinite_without_drag():
    """No atmosphere or no chute -> no terminal velocity (free fall). The function must signal that
    with +inf so a propulsive lander is forced instead of a (nonexistent) parachute solution."""
    assert astro.terminal_velocity(10.0, DUNA_G, 0.0, MK16_CD_A) == float("inf")
    assert astro.terminal_velocity(10.0, DUNA_G, DUNA_RHO, 0.0) == float("inf")


def test_parachutes_for_touchdown_inverts_terminal_velocity():
    """The chute count must be exactly enough that terminal velocity drops to the target. Sizing N for
    a 10 t Duna lander at 8 m/s, then feeding N*Cd*A back into terminal_velocity, must clear 8 m/s."""
    target = 8.0
    n = astro.parachutes_for_touchdown(10.0, DUNA_G, DUNA_RHO, target, MK16_CD_A)
    assert n >= 1
    v = astro.terminal_velocity(10.0, DUNA_G, DUNA_RHO, n * MK16_CD_A)
    assert v <= target                              # rounded UP, so it must satisfy the target
    # One fewer chute must FAIL the target (proves it rounded up to the minimum, not over-provisioned).
    if n > 1:
        v_short = astro.terminal_velocity(10.0, DUNA_G, DUNA_RHO, (n - 1) * MK16_CD_A)
        assert v_short > target


# --------------------------------------------------------------------------------------------------
# Rocket equation — propellant_mass_for_dv and rocket_dv are exact inverses.
# --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("isp,dv,m_after", [(345.0, 2000.0, 10.0), (320.0, 3400.0, 25.0), (310.0, 950.0, 5.0)])
def test_rocket_dv_and_propellant_mass_are_inverses(isp, dv, m_after):
    """Size the propellant for a target Δv, then run the rocket equation forward over (m_after+prop) ->
    m_after; it must return the same Δv. This closes the loop the tank-sizing in design.py relies on."""
    prop = astro.propellant_mass_for_dv(isp, dv, m_after)
    assert prop > 0.0
    dv_back = astro.rocket_dv(isp, m_after + prop, m_after)
    assert dv_back == pytest.approx(dv, rel=1e-9)


def test_rocket_dv_zero_when_no_propellant_burned():
    assert astro.rocket_dv(320.0, 10.0, 10.0) == 0.0      # m0 == m1
    assert astro.propellant_mass_for_dv(320.0, 0.0, 10.0) == 0.0


def test_twr_is_thrust_over_weight():
    """A 10 t craft with 200 kN under Duna gravity has TWR = 200000 / (10000 * 2.944)."""
    t = astro.twr(200_000.0, 10.0, DUNA_G)
    assert t == pytest.approx(200_000.0 / (10_000.0 * DUNA_G), rel=1e-12)


# --------------------------------------------------------------------------------------------------
# Hoverslam reference curve — zero when you cannot stop, sqrt(2 a h) when you can.
# --------------------------------------------------------------------------------------------------

def test_hoverslam_reference_speed_zero_when_thrust_not_above_weight():
    """If usable thrust at the working throttle does not exceed weight, no descent can be arrested:
    the reference speed must be 0 (the controller then burns full and accepts it cannot hover)."""
    mass_t = 10.0
    weight_n = mass_t * 1000.0 * DUNA_G
    # Thrust exactly at weight, and below weight: both yield zero reference speed.
    assert astro.hoverslam_reference_speed(500.0, mass_t, weight_n, DUNA_G) == 0.0
    assert astro.hoverslam_reference_speed(500.0, mass_t, 0.5 * weight_n, DUNA_G) == 0.0


def test_hoverslam_reference_speed_follows_sqrt_2ah_when_thrust_exceeds_weight():
    """With real margin, v_ref(h) = sqrt(2 (throttle*a_max - g) h): it must be positive, grow with
    altitude, and match the closed form. Tracking this curve lands at ~0 m/s (minimum fuel)."""
    mass_t = 10.0
    thrust_n = 3.0 * mass_t * 1000.0 * DUNA_G     # TWR 3 -> ample margin
    throttle = 0.9
    a_net = throttle * thrust_n / (mass_t * 1000.0) - DUNA_G
    h = 500.0
    expected = math.sqrt(2.0 * a_net * h)
    assert astro.hoverslam_reference_speed(h, mass_t, thrust_n, DUNA_G, throttle) == pytest.approx(expected, rel=1e-12)
    # Monotone in altitude, and zero at the ground.
    assert astro.hoverslam_reference_speed(1000.0, mass_t, thrust_n, DUNA_G, throttle) > \
        astro.hoverslam_reference_speed(100.0, mass_t, thrust_n, DUNA_G, throttle)
    assert astro.hoverslam_reference_speed(0.0, mass_t, thrust_n, DUNA_G, throttle) == 0.0
