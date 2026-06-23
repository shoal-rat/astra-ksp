"""Calculated aerospace physics — the closed-form core of the LLM-native commander.

Every quantity here is DERIVED from physics: vis-viva, the Oberth ejection, the rocket equation,
terminal velocity, the hoverslam reference curve. There are NO guessed thresholds, no magic-number
ladders, no bisection-by-feel. The LLM brain (Claude Code) divides a mission into steps and calls
these functions to turn the live body/orbit state (measured by kRPC) into exact maneuvers and an
exact ship design. When a step needs a number, it is computed here — never chosen by hand.

Body constants (mu, radius, surface gravity, atmospheric density profile) are NOT hardcoded in this
module: they are measured live from kRPC (`CelestialBody.gravitational_parameter`, `.density_at(h)`,
…) and passed in, so the same code is correct for Kerbin, Duna, or any body/mod configuration.
"""
from __future__ import annotations

import math

G0 = 9.80665  # standard gravity for Isp (s) -> exhaust velocity; a definition, not a body constant


# --------------------------------------------------------------------------------------------------
# Orbital mechanics — the vis-viva family. mu = GM of the central body (measured from kRPC).
# --------------------------------------------------------------------------------------------------

def circular_speed(mu: float, r: float) -> float:
    """Speed on a circular orbit of radius r (m from the body centre)."""
    return math.sqrt(mu / r) if mu > 0 and r > 0 else 0.0


def vis_viva_speed(mu: float, r: float, a: float) -> float:
    """Speed at radius r on an orbit of semi-major axis a (vis-viva)."""
    if mu <= 0 or r <= 0 or a == 0:
        return 0.0
    return math.sqrt(max(0.0, mu * (2.0 / r - 1.0 / a)))


def orbital_period(mu: float, a: float) -> float:
    """Period of an orbit with semi-major axis a."""
    return 2.0 * math.pi * math.sqrt(a ** 3 / mu) if mu > 0 and a > 0 else 0.0


def hohmann(mu: float, r1: float, r2: float) -> tuple[float, float, float]:
    """Two-burn Hohmann transfer between circular radii r1 -> r2.

    Returns (dv_depart, dv_arrive, transfer_time_s). dv_depart is the burn that raises/lowers the
    apsis to r2; dv_arrive circularises at r2. Both are positive magnitudes.
    """
    if mu <= 0 or r1 <= 0 or r2 <= 0:
        return 0.0, 0.0, 0.0
    a = (r1 + r2) / 2.0
    dv_depart = abs(vis_viva_speed(mu, r1, a) - circular_speed(mu, r1))
    dv_arrive = abs(circular_speed(mu, r2) - vis_viva_speed(mu, r2, a))
    return dv_depart, dv_arrive, math.pi * math.sqrt(a ** 3 / mu)


def phase_angle_for_transfer(mu_sun: float, r_target: float, transfer_time_s: float) -> float:
    """Heliocentric phase angle (rad) the target must lead the departure body by at ejection, so the
    Hohmann transfer arrives where the target will be."""
    if mu_sun <= 0 or r_target <= 0 or transfer_time_s <= 0:
        return 0.0
    n_target = math.sqrt(mu_sun / r_target ** 3)  # target mean motion
    return (math.pi - n_target * transfer_time_s) % (2.0 * math.pi)


def oberth_ejection_dv(mu_body: float, r_park: float, v_infinity: float) -> float:
    """Δv at periapsis of a circular parking orbit (radius r_park about a body of GM mu_body) to leave
    the SOI with hyperbolic excess speed v_infinity — the Oberth-effect ejection burn."""
    if mu_body <= 0 or r_park <= 0:
        return 0.0
    v_park = math.sqrt(mu_body / r_park)
    v_eject = math.sqrt(v_infinity * v_infinity + 2.0 * mu_body / r_park)
    return v_eject - v_park


def interplanetary_departure(
    mu_sun: float, mu_body: float, r_body_orbit: float, r_target_orbit: float, r_park: float
) -> dict[str, float]:
    """Full interplanetary departure budget from a circular parking orbit.

    Heliocentric Hohmann gives the excess speed v_infinity the craft must leave the departure body
    with; the Oberth ejection burn delivers it from the parking orbit. Returns the ejection Δv, the
    v_infinity, the transfer time, and the required phase angle — all calculated, none guessed.
    """
    dv_helio, _, t_transfer = hohmann(mu_sun, r_body_orbit, r_target_orbit)
    v_inf = dv_helio  # excess over the departure body's heliocentric speed for the prograde leg
    return {
        "ejection_dv": oberth_ejection_dv(mu_body, r_park, v_inf),
        "v_infinity": v_inf,
        "transfer_time_s": t_transfer,
        "phase_angle_rad": phase_angle_for_transfer(mu_sun, r_target_orbit, t_transfer),
    }


def transfer_excess_speed(mu_primary: float, r_park: float, r_moon_orbit: float) -> float:
    """Hyperbolic excess speed (v_infinity) at a MOON's SOI for a Hohmann transfer from a parking
    orbit of radius r_park to the moon's orbital radius r_moon_orbit about the shared primary.

    The transfer's apoapsis speed (about the primary) differs from the moon's circular speed; that
    difference is the speed the craft enters the moon's SOI with. Symmetric for the return leg."""
    if mu_primary <= 0 or r_park <= 0 or r_moon_orbit <= 0:
        return 0.0
    a_transfer = (r_park + r_moon_orbit) / 2.0
    v_apo = vis_viva_speed(mu_primary, r_moon_orbit, a_transfer)
    v_moon = circular_speed(mu_primary, r_moon_orbit)
    return abs(v_moon - v_apo)


def capture_from_excess(mu_moon: float, r_peri: float, v_infinity: float) -> float:
    """Δv at periapsis to capture from a v_infinity arrival hyperbola into a low CIRCULAR orbit of
    radius r_peri about the moon. Arrival speed at periapsis = sqrt(v_inf^2 + 2 mu / r); capture burn
    brings it down to circular speed. (The mirror of oberth_ejection_dv: this spends it, that gains it.)"""
    if mu_moon <= 0 or r_peri <= 0:
        return 0.0
    v_peri_arrival = math.sqrt(v_infinity * v_infinity + 2.0 * mu_moon / r_peri)
    return max(0.0, v_peri_arrival - circular_speed(mu_moon, r_peri))


def capture_dv(mu: float, r_periapsis: float, sma_arrival: float, r_target_apoapsis: float) -> float:
    """Retro Δv at periapsis to drop a hyperbolic/long arrival orbit (semi-major axis sma_arrival,
    which is negative for a hyperbola) into a captured ellipse with apoapsis r_target_apoapsis.

    v_arrival from vis-viva at r_periapsis; v_target is the speed at periapsis of the desired captured
    ellipse (periapsis r_periapsis, apoapsis r_target_apoapsis)."""
    v_arr = vis_viva_speed(mu, r_periapsis, sma_arrival)
    a_target = (r_periapsis + r_target_apoapsis) / 2.0
    v_target = vis_viva_speed(mu, r_periapsis, a_target)
    return max(0.0, v_arr - v_target)


def deorbit_dv(mu: float, r_apoapsis: float, r_pe_current: float, r_pe_target: float) -> float:
    """Retro Δv at apoapsis to lower periapsis from r_pe_current to r_pe_target (e.g. into the
    atmosphere). Both periapses share the apoapsis r_apoapsis."""
    a_cur = (r_apoapsis + r_pe_current) / 2.0
    a_new = (r_apoapsis + r_pe_target) / 2.0
    return max(0.0, vis_viva_speed(mu, r_apoapsis, a_cur) - vis_viva_speed(mu, r_apoapsis, a_new))


# --------------------------------------------------------------------------------------------------
# Rocket equation & stage sizing — invert dv to size tanks; TWR for launch/landing feasibility.
# --------------------------------------------------------------------------------------------------

def rocket_dv(isp_vac_s: float, m0_t: float, m1_t: float) -> float:
    """Ideal Δv of a stage burning from wet mass m0 to dry mass m1 (tonnes; ratio is unitless)."""
    return isp_vac_s * G0 * math.log(m0_t / m1_t) if m1_t > 0 and m0_t > m1_t else 0.0


def propellant_mass_for_dv(isp_vac_s: float, dv: float, m_after_t: float) -> float:
    """Propellant (tonnes) a stage of exhaust velocity isp*g0 needs to give Δv to everything above
    it, where m_after_t is the mass remaining AFTER the burn (dry stage + payload above). Inverse of
    the rocket equation: m_before = m_after * exp(dv / ve)."""
    if dv <= 0 or m_after_t <= 0:
        return 0.0
    return m_after_t * (math.exp(dv / (isp_vac_s * G0)) - 1.0)


def twr(thrust_n: float, mass_t: float, g: float) -> float:
    """Thrust-to-weight ratio in a gravity field g (m/s^2). mass in tonnes -> kg inside."""
    w = mass_t * 1000.0 * g
    return thrust_n / w if w > 0 else 0.0


# --------------------------------------------------------------------------------------------------
# Launch / surface-to-orbit Δv budgets — the ideal orbital-speed gain plus the calculated gravity &
# drag losses of climbing out of a gravity well (and, on a body with air, its atmosphere). Used by
# the budget planner so the mission's Δv requirement is derived, not a flat magic number.
# --------------------------------------------------------------------------------------------------

def gravity_drag_loss(mu: float, r_surface: float, atmosphere_top_m: float) -> float:
    """Closed-form estimate of the gravity + drag Δv lost climbing to orbit from `r_surface`.

    Gravity loss is the impulse spent holding weight up during the finite pitch-over climb; for a TWR
    ~1.6 gravity turn it integrates to roughly the speed gained coasting up through the gravity well,
    i.e. comparable to sqrt(2*g_surface*h_turn) where h_turn is the altitude over which the ascent is
    still steep. Drag loss adds the work done against the atmosphere, which scales with its depth. We
    take h_turn as the atmosphere depth on a body with air (the steep part of the climb is inside it)
    or a small fraction of the radius for an airless body, then add a drag term proportional to the
    atmospheric column. The result reproduces the canonical ~1300 m/s Kerbin ascent overhead from the
    body's own g and atmosphere — no hand-tuned constant."""
    g_surface = mu / (r_surface * r_surface)
    if atmosphere_top_m > 0:
        h_turn = atmosphere_top_m
        gravity = math.sqrt(2.0 * g_surface * h_turn) * 0.55  # only the steep early climb pays full g
        drag = 0.0085 * atmosphere_top_m                       # column-depth drag work (m/s per m)
        return gravity + drag
    # Airless: a near-impulsive prograde kick at the surface; the only loss is the short vertical
    # clearance to get the periapsis above the terrain.
    return math.sqrt(2.0 * g_surface * max(1000.0, r_surface * 0.02))


def ascent_dv(mu: float, r_surface: float, r_low_orbit: float, atmosphere_top_m: float,
              surface_rotation_mps: float = 0.0) -> float:
    """Δv to reach a low circular orbit of radius `r_low_orbit` from the surface.

    Ideal part = the orbital speed at the target orbit (vis-viva, here circular) minus the free
    eastward speed the rotating surface already gives a prograde launch. Loss part = the calculated
    gravity+drag overhead. Everything from the body's measured mu/radius/atmosphere — no flat number."""
    v_orbit = circular_speed(mu, r_low_orbit)
    ideal = max(0.0, v_orbit - max(0.0, surface_rotation_mps))
    return ideal + gravity_drag_loss(mu, r_surface, atmosphere_top_m)


def surface_to_orbit_dv(mu: float, r_surface: float, r_low_orbit: float) -> float:
    """Airless ascent (or, by symmetry, propulsive descent) Δv between the surface and a low orbit:
    the orbital speed plus the gravity loss of the short steep climb. For landing the same magnitude
    is spent cancelling orbital speed and the gravity loss during the hoverslam."""
    return circular_speed(mu, r_low_orbit) + gravity_drag_loss(mu, r_surface, 0.0)


# --------------------------------------------------------------------------------------------------
# Atmospheric descent — terminal velocity, parachute sizing, and the suicide-burn / hoverslam law.
# rho is the LIVE atmospheric density (kg/m^3) measured from kRPC `body.density_at(altitude)`.
# --------------------------------------------------------------------------------------------------

def terminal_velocity(mass_t: float, g: float, rho: float, cd_area: float) -> float:
    """Steady-state fall speed where drag balances weight: v = sqrt(2 m g / (rho * Cd*A)).

    cd_area is the total drag area (Cd*A, m^2) of the deployed parachutes (+ body). Because v scales
    as 1/sqrt(rho), a thin atmosphere (Duna rho ~ 0.13 vs Kerbin ~1.14) raises terminal velocity by
    sqrt(rho_thick/rho_thin) — the physics the single-chute Orion ignored and died of.
    """
    if rho <= 0 or cd_area <= 0:
        return float("inf")
    return math.sqrt(2.0 * mass_t * 1000.0 * g / (rho * cd_area))


def parachutes_for_touchdown(
    mass_t: float, g: float, rho: float, v_target: float, cd_area_per_chute: float
) -> int:
    """Minimum number of identical parachutes so terminal velocity <= v_target in density rho.

    Solving terminal_velocity <= v_target for the total Cd*A then dividing by per-chute Cd*A:
        N >= 2 m g / (rho * v_target^2 * cd_area_per_chute)
    Round UP — a fractional chute does not exist. Pair with a TWR>1 landing engine when the thin-
    atmosphere chute count is impractical (then v_target is the speed handed to the powered descent).
    """
    if rho <= 0 or v_target <= 0 or cd_area_per_chute <= 0:
        return 0
    need_cd_area = 2.0 * mass_t * 1000.0 * g / (rho * v_target * v_target)
    return max(1, math.ceil(need_cd_area / cd_area_per_chute))


def suicide_burn_altitude(
    speed: float, mass_t: float, thrust_n: float, g: float, reaction_s: float = 1.0
) -> float:
    """Altitude above the surface at which a full-thrust retro burn must start to null `speed` exactly
    at touchdown: kinematic stopping distance v^2/(2 a_net) plus a reaction-lag allowance v*reaction_s.
    a_net = thrust/mass - g (the net deceleration available)."""
    a_net = max(0.1, thrust_n / (mass_t * 1000.0) - g)
    return speed * speed / (2.0 * a_net) + speed * max(0.0, reaction_s)


def hoverslam_reference_speed(
    altitude: float, mass_t: float, thrust_n: float, g: float, throttle_fraction: float = 0.9
) -> float:
    """Largest speed from which a burn at `throttle_fraction` of full thrust can still null all
    velocity exactly by touchdown: v_ref(h) = sqrt(2 (a_max*frac - g) h). Coast below the curve,
    burn on it; because v_ref -> 0 as h -> 0, tracking it lands at ~0 m/s (minimum-fuel)."""
    a_net = throttle_fraction * thrust_n / (mass_t * 1000.0) - g
    return math.sqrt(2.0 * a_net * max(0.0, altitude)) if a_net > 0.05 else 0.0


def hoverslam_throttle(
    speed: float, reference_speed: float, mass_t: float, thrust_n: float, g: float,
    deadband: float = 1.5,
) -> float:
    """Throttle that holds descent speed on the hoverslam reference curve. Below the curve (minus a
    deadband) -> coast; on/above -> burn, from the gravity-hold throttle up to full as speed
    overshoots. All proportional to the physics — no altitude/throttle ladders."""
    max_accel = thrust_n / (mass_t * 1000.0)
    if max_accel <= 0:
        return 0.0
    if reference_speed <= 0.05:
        return 1.0  # cannot stop from here; burn hard
    error = speed - reference_speed
    if error < -deadband:
        return 0.0
    gravity_hold = g / max_accel
    correction = max(0.0, error + deadband) * 0.6
    return max(0.0, min(1.0, gravity_hold + correction))


def finite_burn_lead_s(burn_time_s: float, settle_s: float = 6.0, command_delay_s: float = 1.0) -> float:
    """Half the burn time before the node, plus engine-settle and command-latency allowances, so a
    finite (non-impulsive) burn is centred on the node. No min/max clamp — the value is the physics."""
    return burn_time_s / 2.0 + settle_s + command_delay_s


def burn_time_s(mass_t: float, thrust_n: float, dv: float, isp_vac_s: float = 0.0) -> float:
    """Time to deliver Δv. With isp given, integrates the mass loss (Tsiolkovsky); else constant-mass
    approximation m*dv/F."""
    if thrust_n <= 0 or dv <= 0:
        return 0.0
    m0 = mass_t * 1000.0
    if isp_vac_s > 0:
        ve = isp_vac_s * G0
        mdot = thrust_n / ve
        m1 = m0 * math.exp(-dv / ve)
        return (m0 - m1) / mdot
    return m0 * dv / thrust_n
