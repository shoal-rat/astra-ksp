import math

from ksp_lab.guidance import (
    burn_duration_s,
    capture_burn_estimate,
    circular_speed_mps,
    ejection_burn_delta_v_mps,
    finite_burn_lead_s,
    hohmann_transfer_delta_v_mps,
    hohmann_transfer_time_s,
    outward_transfer_phase_angle_rad,
    suicide_burn_distance_m,
    terminal_descent_target_vertical_mps,
    terminal_landing_throttle,
    vertical_landing_throttle,
    vis_viva_speed_mps,
)


def test_vis_viva_matches_circular_speed_for_circular_orbit():
    mu = 65_138_397_520.78069
    radius = 200_000.0 + 30_000.0

    assert math.isclose(vis_viva_speed_mps(mu, radius, radius), circular_speed_mps(mu, 200_000.0, 30_000.0))


def test_finite_burn_lead_includes_half_burn_and_settle_time():
    assert finite_burn_lead_s(100.0, settle_s=5.0, command_delay_s=1.0, min_lead_s=0.0) == 56.0


def test_burn_duration_uses_newtonian_acceleration_estimate():
    assert burn_duration_s(5_000.0, 50_000.0, 100.0) == 10.0


def test_kerbin_to_mun_hohmann_seed_is_in_expected_range():
    mu = 3_531_599_999_999.9995
    origin_radius = 600_000.0 + 80_000.0
    mun_radius = 12_000_000.0

    delta_v = hohmann_transfer_delta_v_mps(mu, origin_radius, mun_radius)
    transfer_time = hohmann_transfer_time_s(mu, origin_radius, mun_radius)
    phase_angle = math.degrees(outward_transfer_phase_angle_rad(mu, mun_radius, transfer_time))

    assert 820.0 < delta_v < 890.0
    assert 20_000.0 < transfer_time < 35_000.0
    assert 105.0 < phase_angle < 118.0


def test_inward_hohmann_delta_v_is_a_positive_magnitude():
    mu_sun = 1.1723328e18
    r_kerbin = 13_599_840_256.0
    r_duna = 20_726_155_264.0

    outward = hohmann_transfer_delta_v_mps(mu_sun, r_kerbin, r_duna)
    inward = hohmann_transfer_delta_v_mps(mu_sun, r_duna, r_kerbin)

    assert outward > 0.0
    assert inward > 0.0
    assert inward < outward


def test_kerbin_to_duna_interplanetary_transfer_matches_known_values():
    # The "Mars" (Duna) transfer: heliocentric Hohmann + Oberth ejection from a 100 km LKO.
    mu_sun = 1.1723328e18
    r_kerbin = 13_599_840_256.0
    r_duna = 20_726_155_264.0
    mu_kerbin = 3_531_600_000_000.0
    r_park = 600_000.0 + 100_000.0

    v_inf = hohmann_transfer_delta_v_mps(mu_sun, r_kerbin, r_duna)
    transfer_time = hohmann_transfer_time_s(mu_sun, r_kerbin, r_duna)
    phase_angle = math.degrees(outward_transfer_phase_angle_rad(mu_sun, r_duna, transfer_time))
    ejection_dv = ejection_burn_delta_v_mps(mu_kerbin, r_park, v_inf)

    # Canonical Kerbin->Duna window: ejection ~1060 m/s, phase ~44.4 deg, ~300 Kerbin-days, v_inf ~920.
    assert 1_000.0 < ejection_dv < 1_120.0
    assert 40.0 < phase_angle < 49.0
    assert 280.0 < transfer_time / 21_600.0 < 320.0
    assert 850.0 < v_inf < 980.0


def test_capture_estimate_is_positive_for_hyperbolic_mun_arrival():
    estimate = capture_burn_estimate(
        mu=65_138_397_520.78069,
        body_radius_m=200_000.0,
        periapsis_altitude_m=35_000.0,
        semi_major_axis_m=-550_000.0,
        mass_kg=5_000.0,
        thrust_n=60_000.0,
    )

    assert estimate.delta_v_mps > 0.0
    assert estimate.lead_time_s >= 90.0


def test_capture_estimate_targets_ellipse_speed_at_periapsis():
    mu = 65_138_397_520.78069
    body_radius = 200_000.0
    periapsis_altitude = 35_000.0
    target_apoapsis_altitude = 300_000.0
    semi_major_axis_arrival = -550_000.0

    estimate = capture_burn_estimate(
        mu=mu,
        body_radius_m=body_radius,
        periapsis_altitude_m=periapsis_altitude,
        semi_major_axis_m=semi_major_axis_arrival,
        mass_kg=5_000.0,
        thrust_n=60_000.0,
        target_capture_altitude_m=target_apoapsis_altitude,
    )

    r_periapsis = body_radius + periapsis_altitude
    r_apoapsis = body_radius + target_apoapsis_altitude
    target_sma = (r_periapsis + r_apoapsis) / 2.0
    expected = (
        vis_viva_speed_mps(mu, r_periapsis, semi_major_axis_arrival)
        - vis_viva_speed_mps(mu, r_periapsis, target_sma)
    )

    assert math.isclose(estimate.delta_v_mps, expected, rel_tol=1e-12)


def test_suicide_burn_distance_accounts_for_command_delay():
    no_delay = suicide_burn_distance_m(
        speed_mps=50.0,
        mass_kg=5_000.0,
        thrust_n=60_000.0,
        gravity_mps2=1.63,
        command_delay_s=0.0,
        settle_s=0.0,
        safety_margin_m=0.0,
    )
    with_delay = suicide_burn_distance_m(
        speed_mps=50.0,
        mass_kg=5_000.0,
        thrust_n=60_000.0,
        gravity_mps2=1.63,
        command_delay_s=1.0,
        settle_s=1.0,
        safety_margin_m=0.0,
    )

    assert with_delay == no_delay + 100.0


def test_vertical_landing_throttle_increases_when_descending_fast():
    slow = vertical_landing_throttle(
        vertical_speed_mps=-2.0,
        target_vertical_mps=-4.0,
        mass_kg=5_000.0,
        thrust_n=60_000.0,
        gravity_mps2=1.63,
    )
    fast = vertical_landing_throttle(
        vertical_speed_mps=-20.0,
        target_vertical_mps=-4.0,
        mass_kg=5_000.0,
        thrust_n=60_000.0,
        gravity_mps2=1.63,
    )

    assert fast > slow


def test_terminal_landing_throttle_caps_hover_pulse_near_ground():
    throttle = terminal_landing_throttle(
        surface_altitude_m=31.0,
        vertical_speed_mps=-1.3,
        horizontal_speed_mps=1.3,
        surface_speed_mps=1.8,
        mass_kg=2_500.0,
        thrust_n=60_000.0,
        gravity_mps2=1.63,
    )

    assert 0.0 < throttle < 0.12


def test_terminal_landing_throttle_allows_stronger_braking_when_fast():
    throttle = terminal_landing_throttle(
        surface_altitude_m=26.0,
        vertical_speed_mps=-12.0,
        horizontal_speed_mps=10.0,
        surface_speed_mps=16.0,
        mass_kg=2_500.0,
        thrust_n=60_000.0,
        gravity_mps2=1.63,
    )

    assert 0.35 < throttle <= 0.75


def test_terminal_descent_keeps_descending_while_lateral_speed_remains_high():
    assert terminal_descent_target_vertical_mps(50.0, horizontal_speed_mps=7.0) == -1.2
    assert terminal_descent_target_vertical_mps(20.0, horizontal_speed_mps=5.0) == -0.8
    assert terminal_descent_target_vertical_mps(8.0, horizontal_speed_mps=1.2) == -0.5
    assert terminal_descent_target_vertical_mps(600.0, horizontal_speed_mps=7.0) == -3.0
    assert terminal_descent_target_vertical_mps(180.0, horizontal_speed_mps=7.0) == -3.5
    assert terminal_descent_target_vertical_mps(240.0, horizontal_speed_mps=7.0) == -3.5


def test_terminal_landing_throttle_keeps_braking_lateral_speed_near_ground():
    throttle = terminal_landing_throttle(
        surface_altitude_m=14.0,
        vertical_speed_mps=-0.2,
        horizontal_speed_mps=8.2,
        surface_speed_mps=8.2,
        mass_kg=2_500.0,
        thrust_n=60_000.0,
        gravity_mps2=1.63,
    )

    assert throttle >= 0.22


def test_terminal_landing_throttle_forces_lateral_kill_burn_before_touchdown():
    throttle = terminal_landing_throttle(
        surface_altitude_m=10.0,
        vertical_speed_mps=-0.5,
        horizontal_speed_mps=7.0,
        surface_speed_mps=7.1,
        mass_kg=2_500.0,
        thrust_n=60_000.0,
        gravity_mps2=1.63,
    )

    assert throttle >= 0.24


def test_terminal_landing_throttle_does_not_cut_with_lateral_speed_at_contact_height():
    throttle = terminal_landing_throttle(
        surface_altitude_m=5.0,
        vertical_speed_mps=-1.1,
        horizontal_speed_mps=2.4,
        surface_speed_mps=2.7,
        mass_kg=2_500.0,
        thrust_n=60_000.0,
        gravity_mps2=1.63,
    )

    assert throttle >= 0.20
