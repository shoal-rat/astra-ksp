---
name: ascent-to-orbit
description: Fly a gravity-turn ascent from the Kerbin pad to a parking orbit (apoapsis ≥ 80 km, then circularize periapsis ≥ 70 km) — measured-thrust autostaging, correct-frame liveness, no time-warp under power.
---

# Ascent to Orbit

First phase of every capability. Primitives: `KrpcFlightController.fly(...)` runs the ascent inline
(`flight_controller.py` ~line 347), then `_circularize_simple(...)`. You do not call kRPC directly —
the executor (`tools/fly_*.py`) does. This skill is the model of what it does and what to watch.

## METHOD

1. **Set up.** `sas=False`, `rcs=False`, `throttle=1.0`, autopilot engaged at pitch 90°, heading 90°.
   `_set_physics_warp(conn, 0)` — NEVER time-warp under power. Ignite via `_start_launch_sequence`
   (stages until a fueled active engine has thrust, then releases launch clamps).
2. **Gravity turn.** Below `turn_start_altitude = 1000 m` hold 90°. Between 1 km and
   `turn_end_altitude = 40 km` pitch linearly: `pitch = 90 − 90·(alt − 1000)/(40000 − 1000)`. Above
   40 km hold 0° (downrange). Heading 90° (due east).
3. **Read motion in the body frame (P4).** `flight = vessel.flight(vessel.orbit.body.reference_frame)`
   — NOT the default co-moving surface frame, which reads ~0 m/s the whole ascent and false-triggers
   stuck-on-pad.
4. **Autostage on MEASURED low thrust (`_should_stage`).** Stage when `available_thrust < 1 N` and a
   lower fueled engine exists, or active engines have no fuel, or dead engines on the active stage.
   Min autostage floor protects the last stage. Throttle back to 1.0 after each stage; re-acquire the
   vessel (`_reacquire_vessel`) — staging can change the active vessel.
5. **Apoapsis target reached →** when `apoapsis > target·1.10` cut throttle, phase →
   `coast_to_apoapsis`. KSP parking target ≥ 80 km.
6. **Coast + circularize (`_circularize_simple`).** Throttle 0; hold orbital-prograde
   `(0, 1, 0)`; warp/coast until `time_to_apoapsis ≤ 35 s`; then full throttle prograde until
   `periapsis ≥ 70 km` → marker `circularized`.

## MATH

- Parking circular speed: `v = sqrt(μ/r)`, `r = R + h`. Kerbin 80 km:
  `sqrt(3.5316e12 / (600000+80000)) = sqrt(3.5316e12/680000) ≈ 2279 m/s`.
- Gravity-turn pitch: linear in altitude over [1 km, 40 km] as above.
- Coast ignition lead: begin the circularization burn at `time_to_apoapsis ≤ 35 s`.

## WORKED EXAMPLE

At 12 km altitude during ascent: `pitch = 90 − 90·(12000−1000)/39000 = 90 − 90·0.282 = 64.6°`,
heading 90°. Booster runs dry → `available_thrust` drops < 1 N while a fueled transfer engine sits
below → autostage fires, throttle back to 1.0. Apoapsis climbs through `80 km·1.10 = 88 km` → cut
throttle, coast. At apoapsis−35 s, burn prograde to raise periapsis to 70 km → `circularized`.

## SUCCESS / FAILURE MARKERS

- SUCCESS: phase `coast_to_apoapsis` then `circularized` (periapsis ≥ 70 km, non-escaping).
- `ascent_stuck_on_pad` (apoapsis < 1 km, vert & surface speed < 2 m/s after 30 s) → **launch TWR < 1**
  (lighten upper stages to est TWR ≥ 1.4) OR stale-frame false read (clause: read the body frame).
- `coast_to_apoapsis_unthrottleable_thrust_guard` / `..._invalid_or_escape` → a stage has
  unthrottleable thrust pushing past escape; force throttle 0, hold retrograde, MEASURE thrust.
- `circularization_escape_abort` / `out_of_fuel_during_circularization` / `circularization_timeout` →
  under-Δv or staging stall; check `craft-design` Δv and node-burn staging.
- Tumble on launch → active control surfaces under a weak autopilot (use passive `basicFin`, P5).
