from __future__ import annotations

import math
from dataclasses import dataclass


STANDARD_GRAVITY = 9.80665


@dataclass(frozen=True, slots=True)
class BurnEstimate:
    delta_v_mps: float
    burn_time_s: float
    lead_time_s: float


def circular_speed_mps(mu: float, radius_m: float, altitude_m: float) -> float:
    r = radius_m + altitude_m
    if mu <= 0.0 or r <= 0.0:
        return 0.0
    return math.sqrt(mu / r)


def vis_viva_speed_mps(mu: float, radius_m: float, semi_major_axis_m: float) -> float:
    if mu <= 0.0 or radius_m <= 0.0 or semi_major_axis_m == 0.0:
        return 0.0
    term = mu * (2.0 / radius_m - 1.0 / semi_major_axis_m)
    return math.sqrt(max(0.0, term))


def hohmann_transfer_delta_v_mps(mu: float, origin_radius_m: float, target_radius_m: float) -> float:
    if mu <= 0.0 or origin_radius_m <= 0.0 or target_radius_m <= 0.0:
        return 0.0
    semi_major_axis = (origin_radius_m + target_radius_m) / 2.0
    transfer_speed = vis_viva_speed_mps(mu, origin_radius_m, semi_major_axis)
    origin_speed = circular_speed_mps(mu, 0.0, origin_radius_m)
    return transfer_speed - origin_speed


def hohmann_transfer_time_s(mu: float, origin_radius_m: float, target_radius_m: float) -> float:
    if mu <= 0.0 or origin_radius_m <= 0.0 or target_radius_m <= 0.0:
        return 0.0
    semi_major_axis = (origin_radius_m + target_radius_m) / 2.0
    return math.pi * math.sqrt(semi_major_axis**3 / mu)


def outward_transfer_phase_angle_rad(mu: float, target_radius_m: float, transfer_time_s: float) -> float:
    if mu <= 0.0 or target_radius_m <= 0.0 or transfer_time_s <= 0.0:
        return 0.0
    target_mean_motion = math.sqrt(mu / target_radius_m**3)
    return (math.pi - target_mean_motion * transfer_time_s) % (2.0 * math.pi)


def burn_duration_s(mass_kg: float, thrust_n: float, delta_v_mps: float) -> float:
    if mass_kg <= 0.0 or thrust_n <= 0.0 or delta_v_mps <= 0.0:
        return 0.0
    return mass_kg * delta_v_mps / thrust_n


def finite_burn_lead_s(
    burn_time_s: float,
    *,
    settle_s: float = 8.0,
    command_delay_s: float = 1.0,
    min_lead_s: float = 20.0,
    max_lead_s: float = 360.0,
) -> float:
    return min(max_lead_s, max(min_lead_s, burn_time_s / 2.0 + settle_s + command_delay_s))


def capture_burn_estimate(
    *,
    mu: float,
    body_radius_m: float,
    periapsis_altitude_m: float,
    semi_major_axis_m: float,
    mass_kg: float,
    thrust_n: float,
    target_capture_altitude_m: float = 35_000.0,
) -> BurnEstimate:
    periapsis_altitude_m = max(1_000.0, periapsis_altitude_m)
    target_altitude_m = max(periapsis_altitude_m, target_capture_altitude_m)
    periapsis_radius_m = body_radius_m + periapsis_altitude_m
    arrival_speed = vis_viva_speed_mps(mu, periapsis_radius_m, semi_major_axis_m)
    target_speed = circular_speed_mps(mu, body_radius_m, target_altitude_m)
    delta_v = max(0.0, arrival_speed - target_speed)
    burn_time = burn_duration_s(mass_kg, thrust_n, delta_v)
    return BurnEstimate(delta_v, burn_time, finite_burn_lead_s(burn_time, min_lead_s=90.0))


def suicide_burn_distance_m(
    *,
    speed_mps: float,
    mass_kg: float,
    thrust_n: float,
    gravity_mps2: float,
    command_delay_s: float = 0.75,
    settle_s: float = 0.75,
    safety_margin_m: float = 25.0,
) -> float:
    if speed_mps <= 0.0:
        return safety_margin_m
    net_accel = max(0.1, thrust_n / max(0.1, mass_kg) - gravity_mps2)
    kinematic_distance = speed_mps * speed_mps / (2.0 * net_accel)
    delay_distance = speed_mps * max(0.0, command_delay_s + settle_s)
    return kinematic_distance + delay_distance + safety_margin_m


def hoverslam_reference_speed_mps(
    *,
    altitude_m: float,
    mass_kg: float,
    thrust_n: float,
    gravity_mps2: float,
    throttle_fraction: float = 0.92,
) -> float:
    """Falcon-9 'hoverslam' reference speed at a given height above the surface.

    This is the largest speed from which a burn at ``throttle_fraction`` of full thrust can still
    null all velocity exactly by touchdown: v_ref(h) = sqrt(2 * (a_max*frac - g) * h).

    The descent freefalls (engine off) while the actual speed is below this curve, then ignites and
    holds the speed ON the curve. Because v_ref shrinks to 0 as h -> 0, tracking it brings the craft
    to ~0 m/s right at the ground — a precise, time-optimal (minimum-fuel, maximum-freefall) landing.
    Reserving the top ``1 - throttle_fraction`` of thrust gives the controller headroom to catch up
    if it ignites slightly late.
    """
    net_accel = throttle_fraction * thrust_n / max(0.1, mass_kg) - gravity_mps2
    if net_accel <= 0.05:
        # Thrust barely beats gravity; treat as can't-stop so the controller burns at full throttle.
        return 0.0
    return (2.0 * net_accel * max(0.0, altitude_m)) ** 0.5


def hoverslam_throttle(
    *,
    speed_mps: float,
    reference_speed_mps: float,
    mass_kg: float,
    thrust_n: float,
    gravity_mps2: float,
    deadband_mps: float = 1.5,
) -> float:
    """Throttle that holds the descent speed on the hoverslam reference curve.

    Below the curve (minus a deadband) -> coast (throttle 0, keep falling). On/above the curve ->
    burn, scaling from the gravity-hold throttle up to full as the speed overshoots the reference.
    """
    max_accel = thrust_n / max(0.1, mass_kg)
    if max_accel <= 0.0:
        return 0.0
    if reference_speed_mps <= 0.05:
        return 1.0  # cannot stop from here; burn hard
    error = speed_mps - reference_speed_mps
    if error < -deadband_mps:
        return 0.0
    gravity_hold = gravity_mps2 / max_accel
    # Each 1 m/s of overshoot adds a chunk of throttle on top of the gravity-hold point.
    correction = max(0.0, error + deadband_mps) * 0.6
    return max(0.0, min(1.0, gravity_hold + correction))


def vertical_landing_throttle(
    *,
    vertical_speed_mps: float,
    target_vertical_mps: float,
    mass_kg: float,
    thrust_n: float,
    gravity_mps2: float,
    response_time_s: float = 2.0,
) -> float:
    max_accel = thrust_n / max(0.1, mass_kg)
    if max_accel <= 0.0:
        return 0.0
    correction_accel = max(0.0, target_vertical_mps - vertical_speed_mps) / max(0.25, response_time_s)
    return max(0.0, min(1.0, (gravity_mps2 + correction_accel) / max_accel))


def terminal_descent_target_vertical_mps(surface_altitude_m: float, horizontal_speed_mps: float = 0.0) -> float:
    if surface_altitude_m > 60.0:
        target = -3.0
    elif surface_altitude_m > 30.0:
        target = -2.0
    elif surface_altitude_m > 12.0:
        target = -1.2
    else:
        target = -0.7

    if horizontal_speed_mps > 8.0 and surface_altitude_m < 500.0:
        target = min(target, -5.5)
    if horizontal_speed_mps > 5.0 and surface_altitude_m < 300.0:
        target = min(target, -3.5)
    if horizontal_speed_mps > 3.0 and surface_altitude_m < 160.0:
        target = max(target, -2.0)
    if horizontal_speed_mps > 2.0 and surface_altitude_m < 70.0:
        target = max(target, -1.2)
    if horizontal_speed_mps > 1.5 and surface_altitude_m < 25.0:
        target = max(target, -0.8)
    if horizontal_speed_mps > 1.0 and surface_altitude_m < 10.0:
        target = max(target, -0.5)
    return target


def terminal_landing_throttle(
    *,
    surface_altitude_m: float,
    vertical_speed_mps: float,
    horizontal_speed_mps: float,
    surface_speed_mps: float,
    mass_kg: float,
    thrust_n: float,
    gravity_mps2: float,
) -> float:
    target_vertical = terminal_descent_target_vertical_mps(surface_altitude_m, horizontal_speed_mps)
    throttle_cap = 0.24 if horizontal_speed_mps < 6.0 else 0.45
    if vertical_speed_mps < -8.0:
        throttle_cap = max(throttle_cap, 0.75)
    elif vertical_speed_mps < -5.0:
        throttle_cap = max(throttle_cap, 0.50)

    if surface_altitude_m > 6.0 and vertical_speed_mps > target_vertical + 0.8:
        if horizontal_speed_mps > 3.0 and surface_altitude_m < 650.0:
            lateral_floor = 0.14
            if surface_altitude_m < 500.0 and horizontal_speed_mps > 5.0:
                lateral_floor = 0.18
            if surface_altitude_m < 250.0 and horizontal_speed_mps > 3.0:
                lateral_floor = 0.22
            if surface_altitude_m < 120.0 and horizontal_speed_mps > 2.0:
                lateral_floor = 0.25
            if surface_altitude_m < 35.0 and horizontal_speed_mps > 1.5:
                lateral_floor = 0.28
            return min(throttle_cap, lateral_floor)
        return 0.0

    throttle = vertical_landing_throttle(
        vertical_speed_mps=vertical_speed_mps,
        target_vertical_mps=target_vertical,
        mass_kg=mass_kg,
        thrust_n=thrust_n,
        gravity_mps2=gravity_mps2,
        response_time_s=1.0,
    )
    if surface_altitude_m < 650.0 and horizontal_speed_mps > 6.0:
        throttle = max(throttle, 0.14)
    if surface_altitude_m < 500.0 and horizontal_speed_mps > 5.0:
        throttle = max(throttle, 0.18)
    if surface_altitude_m < 250.0 and horizontal_speed_mps > 3.0:
        throttle = max(throttle, 0.22)
    if surface_altitude_m < 80.0 and horizontal_speed_mps > 2.0:
        throttle = max(throttle, 0.25)
    if surface_altitude_m < 35.0 and horizontal_speed_mps > 1.5:
        throttle = max(throttle, 0.28)
    if surface_altitude_m < 15.0 and horizontal_speed_mps > 1.0:
        throttle = max(throttle, 0.30)
    if (
        surface_altitude_m < 8.0
        and vertical_speed_mps > -1.5
        and surface_speed_mps < 7.5
        and horizontal_speed_mps < 1.0
    ):
        return 0.0
    return min(throttle_cap, max(0.0, throttle))
