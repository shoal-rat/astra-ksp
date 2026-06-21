---
name: hoverslam-landing
description: Falcon-9 hoverslam powered descent to the airless Mun surface — deorbit, maximize freefall until total speed hits the reference curve, brake on surface-retrograde, then a reliable terminal flare below 70 m to a < 1.5 m/s horizontal touchdown.
---

# Hoverslam Landing

Airless powered descent (no parachute). Primitives: `_prepare_mun_landing_orbit`,
`_deorbit_mun_for_landing`, `_land_on_mun` in `flight_controller.py`;
`hoverslam_reference_speed_mps`, `hoverslam_throttle`, `suicide_burn_distance_m`,
`vertical_landing_throttle` in `guidance.py`.

## METHOD

1. **Lower the landing orbit** (`_prepare_mun_landing_orbit`): if apoapsis > 450 km, lower it to
   ~360 km at periapsis.
2. **Deorbit (LARGE burn, full throttle, P12)** (`_deorbit_mun_for_landing`): at apoapsis, burn
   retrograde to drop periapsis to a sub-surface target (~−5 km) so descent intersects terrain. Pass
   `max_throttle=1.0, max_burn_s=200` — NOT the 0.18 precision cap (it times out a low-thrust lander).
3. **Read in the body frame.** `flight = vessel.flight(vessel.orbit.body.reference_frame)`; deploy
   legs; point surface-retrograde (`surface_velocity_reference_frame`, `(0,−1,0)`).
4. **Maximize freefall (engine off).** Compute the reference curve every tick:
   `v_ref(h) = hoverslam_reference_speed_mps = sqrt(2·(0.92·a_max − g)·h)`, `a_max = thrust/mass`,
   `g = body.surface_gravity (Mun 1.63)`. While total `surface_speed < v_ref − 1.5` (deadband),
   throttle 0 and keep falling. Physics-warp only while comfortably above ignition (speed_margin >
   90 and altitude > 4 km) so a coarse warp step can't overshoot the burn.
5. **Ignite ON the curve (the hoverslam burn).** When speed reaches `v_ref`, set
   `throttle = hoverslam_throttle(...)`, which holds the speed on the shrinking curve (gravity-hold +
   0.6·overshoot). Because `v_ref → 0` as `h → 0`, tracking it nulls velocity right at the ground.
6. **Brake on surface-retrograde while there is real speed to kill** (kills horizontal too): if
   `surface_speed > 4` and `horizontal_speed > 2.5`, hold `surface_velocity_reference_frame (0,−1,0)`;
   otherwise flip to local-up (`_target_local_up`) for a level touchdown.
7. **Terminal flare < 70 m (reliable).** The 0.92-throttle curve lags below ~70 m, so hand to a flare
   that ramps the target descent rate down: target_v −9 → −5.5 → −3.0 → −1.8 → −1.0 m/s as altitude
   drops 45 → 25 → 12 → 5 m; `vertical_landing_throttle` holds it. Brake hard (≥ 0.95) if falling
   faster than target − 1.5; add ≥ 0.35 if horizontal > 3 m/s to null lateral drift; cut throttle when
   vert_speed > −0.5 and altitude < 3 m.
8. **Touchdown gate:** `situation == landed` AND `surface_speed ≤ 3.0` AND `horizontal ≤ 1.5` →
   `mun_landed`. Else `mun_landed_unstable`.

## THE CURVE + MATH

- `v_ref(h) = sqrt(2·(0.92·a_max − g)·h)`, `a_max = T/m`. Reserving 8% thrust gives late-ignition
  catch-up headroom. If `0.92·a_max − g ≤ 0.05` the craft can't stop from rest there → burn full.
- Suicide-burn distance (used only for the high-altitude warp gate):
  `d = v²/(2·(T/m − g)) + v·(cmd_delay+settle) + margin`.

## WORKED EXAMPLE (ignition altitude on the Mun)

Lander mass 6,000 kg, thrust 60,000 N → `a_max = 10.0 m/s²`. Mun `g = 1.63`.
`0.92·a_max − g = 0.92·10.0 − 1.63 = 7.57 m/s²`.
- At `h = 1,000 m`: `v_ref = sqrt(2·7.57·1000) = sqrt(15,140) ≈ 123 m/s` → freefall until ~123 m/s.
- At `h = 200 m`: `v_ref = sqrt(2·7.57·200) ≈ 55 m/s`.
- At `h = 70 m` (flare handoff): `v_ref = sqrt(2·7.57·70) ≈ 33 m/s` → below here the terminal flare
  takes over and brakes to ~−1 m/s for touchdown.
So: freefall down the curve from orbit, ignite when descent speed reaches `v_ref(h)`, hold the curve to
~70 m, then flare. Proven touchdown: −0.1 m/s vertical, 0.2 m/s horizontal.

## SUCCESS / FAILURE MARKERS

- SUCCESS: `mun_landed` (speed ≤ 3, horizontal ≤ 1.5). Then run `surface science` (see README) and
  `Mun ascent` (return-and-reentry).
- `mun_landed_unstable` → touched down too fast/tilted; usual cause is cutting throttle while still
  sliding or flipping to vertical too early (P16) — keep surface-retrograde until horizontal < ~3.5,
  enter the flare early (~120 m, earlier if horizontal is high).
- `mun_landing_out_of_fuel` → deorbited too aggressively / under-Δv lander.
- `mun_landing_deorbit_not_possible` → already sub-surface periapsis, or wrong burn direction.
- `mun_landing_hard_contact_risk` (alt < 5 m, |vert| > 12) → ignited too late; the freefall band ran
  past the curve (warp overshoot — tighten the warp gate).
