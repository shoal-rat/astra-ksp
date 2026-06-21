---
name: trans-munar-injection
description: Plan and fly the trans-Munar injection (TMI) — seed the prograde burn and phase-angle window from Hohmann math, search ~3 orbital periods for an encounter, stage mid-burn, and re-align (don't flip) if apoapsis falls.
---

# Trans-Munar Injection (TMI)

From a Kerbin parking orbit, inject into a Mun encounter. Primitives:
`_find_mun_transfer_node(...)` (seed + grid search) and `_execute_node(..., "trans_mun_injection")`
in `flight_controller.py`; math in `guidance.py`. Requires periapsis ≥ 70 km first
(`mun_transfer_requires_parking_orbit` else).

## METHOD

1. **Seed from Hohmann** (`_estimate_mun_transfer_seed`):
   - `transfer_time = hohmann_transfer_time_s(μ, r_park, r_mun)`
   - `prograde_dv = hohmann_transfer_delta_v_mps(μ, r_park, r_mun)`, clamped to [740, 980] m/s
   - `target_phase = outward_transfer_phase_angle_rad(μ, r_mun, transfer_time)`
   - `launch_delay = (current_phase − target_phase) mod 2π / phase_rate`, bumped to ≥ 120 s.
2. **Grid-search a node** (`_search_mun_transfer_grid`): candidate UTs around the seed delay,
   prograde ±80 around the seed (step 10, in [740, 980]), radial {0, ±40}. Score by Mun encounter
   periapsis nearest 60 km (`_score_mun_transfer_node`); reject non-encounters.
3. **Widen to ~3 periods if no safe candidate** — coarse times `now + 120 + period·k/24` for
   k = 0..72 (≈ 3 orbital periods), prograde ±120 step 20. The Mun's phase angle is often not
   favorable within one period; this is the dominant cause of `mun_transfer_node_not_found`. Add the
   node only if `_is_safe_mun_transfer_candidate` (encounter periapsis 35–120 km).
4. **Execute** (`_execute_node`): align to orbital-prograde (autopilot, alignment-error wait), warp to
   `node.ut − lead_time`, ignite at full throttle. Steering: orbital-prograde for a near-pure-prograde
   node; the node-vector if lateral Δv ≥ 5 m/s.
5. **Stage mid-burn (P6).** On thrust loss, `_should_stage` → `activate_next_stage` → re-point →
   continue. Marker `trans_mun_injection_staged_to_continue_burn`. Only declare out-of-fuel when no
   fueled stage remains.
6. **Re-align, don't flip (P7).** A prograde TMI can only RAISE apoapsis. If apoapsis falls > 2 km
   below baseline after 2.5 s, the heavy stack ignited off-prograde: cut throttle, fully re-align to
   prograde, resume at throttle 0.15 (gimbal authority). Never flip the burn vector; never drive
   Kerbin periapsis below `min_kerbin_periapsis` (70 km). Abort after 3 failed re-aligns
   (`trans_mun_injection_misaligned_abort`).
7. **Stop on a captured-grade encounter:** when `next_orbit.body == "Mun"` and encounter periapsis is
   35 km–180 km, cut throttle. Apoapsis cap (14.5 Mm, or 22 Mm for a planned-safe transfer) ends a
   burn that's overshooting without an encounter.

## MATH

- `hohmann_dv = vis_viva(μ, r_park, a) − v_circ(r_park)`, `a = (r_park + r_mun)/2`.
- `transfer_time = π·sqrt(a³/μ)`.
- `phase_angle = (π − n_target·transfer_time) mod 2π`, `n_target = sqrt(μ/r_mun³)`.
- Finite-burn lead = `burn/2 + settle + command_delay` (`finite_burn_lead_s`).

## WORKED EXAMPLE

Kerbin park r ≈ 680 km, Mun orbit r ≈ 12,000 km (μ_Kerbin = 3.5316e12):
- `a = (680e3 + 12.0e6)/2 = 6.34e6 m`.
- `v_park = sqrt(3.5316e12/680e3) ≈ 2279 m/s`; `v_transfer = sqrt(3.5316e12·(2/680e3 − 1/6.34e6)) ≈
  3120 m/s` → `Δv ≈ 841 m/s` (lands in the [740, 980] seed band, ~860 m/s typical).
- `transfer_time = π·sqrt((6.34e6)³/3.5316e12) ≈ 16,900 s`.
- Grid search picks the UT whose Mun encounter periapsis is nearest 60 km; if none in 1 period, the
  3-period coarse sweep finds one. Ignite at `node.ut − lead`. If apoapsis dips after ignition →
  re-align prograde at 0.15 throttle, resume.

## SUCCESS / FAILURE MARKERS

- SUCCESS: `trans_mun_injection_complete` with a Mun encounter; then `mun_soi_entered` (see
  `soi-capture`).
- `mun_transfer_node_not_found` → phase angle unfavorable; **retry/relaunch** for fresh phasing (the
  search already swept ~3 periods). Dominant predeploy variance.
- `trans_mun_injection_realign` (expected, self-correcting) → off-axis start, re-aligning. Repeated →
  `..._misaligned_abort` = attitude authority too weak (add inline reaction wheels, P8).
- `trans_mun_injection_periapsis_guard_abort` → burn was lowering Kerbin periapsis into reentry;
  aborted safely.
- `..._out_of_fuel` → didn't stage into a fueled lower stage; check node-burn staging (P6).
