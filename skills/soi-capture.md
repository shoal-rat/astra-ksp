---
name: soi-capture
description: Capture into a Mun orbit after SOI entry — hold retrograde on the engine gimbal (autopilot, re-pointed each tick), accept a relay-band high orbit vs a low capture for a lander, and correct a grazing periapsis before the burn.
---

# SOI Capture

After the Mun SOI is entered, remove energy to bind into orbit. Primitives:
`_coast_to_mun_soi(...)`, `_correct_mun_soi_periapsis(...)`, `_capture_mun_orbit(..., relay_capture)`
in `flight_controller.py`; `capture_burn_estimate(...)` in `guidance.py`.

## METHOD

1. **Coast to SOI** (`_coast_to_mun_soi`): warp to `time_to_soi_change − 60 s` repeatedly until
   `orbit.body == "Mun"` → `mun_soi_entered`. If the encounter was missed, attempt a correction node
   (≤ 2 tries) or a top-off burn before declaring `mun_encounter_missed`.
2. **Correct a grazing periapsis FIRST** (`_correct_mun_soi_periapsis`): finite-burn drift can leave a
   periapsis too high to capture. If `periapsis > gate`, search a node (prograde {0..−800}, radial
   {±100..±800}) that yields a BOUND orbit (apoapsis < 0.9·SOI) with periapsis ~100 km, and execute
   it. Mun-relative speed is low on a grazing pass, so a few hundred m/s of radial swings periapsis
   far. Gate: relay = `mun_relay_max_apoapsis_m` (2,150 km); lander = `max_mun_capture_periapsis_m`
   (1,000 km).
3. **Estimate the capture burn** (`capture_burn_estimate`): `Δv = v_arrival(periapsis) −
   v_circ(target)`; lead = finite-burn lead (min 90 s). Warp to `time_to_periapsis − capture_lead`.
4. **Hold retrograde on the GIMBAL, not SAS alone (P9).** Point orbital-prograde inverted
   (`_point_orbital_prograde(invert=True)`) under autopilot; re-point every ~1 s so the burn tracks
   the fast-moving retrograde marker. SAS-only reaction wheels cannot hold a heavy probe-controlled
   stack → > 1 km/s wasted, no capture. Ignite full throttle, ease throttle as the orbit binds
   (1.0 → 0.22 → 0.14 → 0.04 by apoapsis band).
5. **Accept the functional band (P10).**
   - **Relay** (`relay_capture=True`): stop as soon as the orbit is bound with apoapsis ≤ 2,150 km and
     periapsis ≥ 50 km → `mun_orbit_captured_relay_band`. Don't over-burn a valid high orbit down to a
     lander orbit. Then hand to relay shaping (see README / `_shape_mun_relay_orbit`).
   - **Lander/crew:** capture low — `targeted_capture` apoapsis 20–360 km, periapsis 12–260 km. If
     periapsis < 30 km, raise it (`_raise_mun_periapsis`).
6. **Guards:** no actual thrust after 8 s (`mun_capture_no_actual_thrust` — rails/warp left on, force
   warp 0 & verify thrust early); periapsis too low (`mun_capture_periapsis_too_low`); out of fuel;
   240 s timeout.

## MATH

- `v_circ(target) = sqrt(μ/(R + h_target))`.
- `v_arrival(periapsis) = vis_viva(μ, R + h_peri, a)` for the inbound semi-major axis.
- `capture_Δv = max(0, v_arrival − v_circ)`; `lead = burn/2 + settle + cmd_delay`, ≥ 90 s.

## WORKED EXAMPLE (relay)

Mun μ = 6.5138e10, R = 200 km. Inbound grazing periapsis 250 km → too high for a lander but fine for a
relay. `relay_capture=True`: ignite retrograde-on-gimbal, re-point each second; as soon as the orbit
becomes bound with apoapsis ≤ 2,150 km and periapsis ≥ 50 km, cut throttle →
`mun_orbit_captured_relay_band`. A lander on the same approach would instead use
`_correct_mun_soi_periapsis` (radial ~−400 m/s) to drop periapsis to ~100 km, then capture to apoapsis
20–360 km / periapsis 12–260 km. `v_circ` for a 100 km Mun orbit:
`sqrt(6.5138e10/(200000+100000)) = sqrt(6.5138e10/300000) ≈ 466 m/s`.

## SUCCESS / FAILURE MARKERS

- SUCCESS: `mun_orbit_captured_relay_band` (relay) | `mun_orbit_captured` / `..._low_periapsis` /
  `..._emergency` (lander/crew, then periapsis raise if needed).
- `mun_capture_no_actual_thrust` → warp/rails left on; force warp 0, verify thrust within 8 s.
- `mun_capture_periapsis_too_high` → gate exceeded; should have corrected periapsis first.
- `mun_capture_out_of_fuel` / `mun_capture_timeout` → under-Δv or weak attitude wasting fuel off-axis
  (P8/P9: inline reaction wheels + gimbal hold).
- `mun_encounter_missed` → upstream TMI didn't actually intersect the SOI; see `trans-munar-injection`.
