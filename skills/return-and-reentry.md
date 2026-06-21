---
name: return-and-reentry
description: Bring a craft home from Mun orbit — (optional Mun surface ascent), trans-Kerbin injection to a recoverable reentry band, coast to Kerbin SOI, reentry behind a heat shield, and parachute recovery.
---

# Return and Reentry

Final arc of `crew_return` (and the tail of `hls_land_return`'s Mun ascent). Primitives:
`_launch_from_mun(...)`, `_return_to_kerbin_from_mun_orbit(...)`, `_find_kerbin_return_node(...)`,
`_coast_to_kerbin_soi(...)`, `_recover_on_kerbin(...)` in `flight_controller.py`. Drivers:
`run_orion_return(name, ...)` and `_return_from_mun(...)`.

## METHOD

### A. Mun surface ascent (only if returning from the surface, `_launch_from_mun`)
1. **Settle first (P16 / methodology §4).** Legs deployed, hold local-up; wait until vertical < 0.25
   and surface speed < 0.55 for > 1.5 s → settled. Never throttle up while sliding
   (`mun_return_unstable_landing` else).
2. **Ascend.** Full throttle; pitch via `_mun_ascent_pitch` (90° low, easing to 0° as apoapsis nears
   the 20 km target); retract legs above 80 m; stage above 140 m. Target apoapsis 20 km.
3. **Coast then circularize** to periapsis ≥ 10 km, apoapsis < 90 km → `mun_return_orbit_established`.

### B. Trans-Kerbin injection (TEI, `_find_kerbin_return_node` + `_execute_node`)
4. **Search a return node.** Sweep ~1 period of UTs × prograde {±80..±560} step 10; score the Kerbin
   encounter periapsis nearest 30 km (`_score_kerbin_return_node`); accept only an encounter. Execute
   as `trans_kerbin_injection` (same staging/re-align rules as TMI).
5. **Never lower the return periapsis into the atmosphere mid-coast (P7).** Every correction must
   preserve a non-atmospheric (or deliberately-reentry) periapsis; abort if a correction would push it
   into reentry early.

### C. Coast to Kerbin SOI (`_coast_to_kerbin_soi`)
6. Warp to `time_to_soi_change − 60 s` until `orbit.body == "Kerbin"` → `kerbin_soi_entered`. Missed
   encounter → `kerbin_return_encounter_missed`.

### D. Reentry + recovery (`_recover_on_kerbin`)
7. **Reentry behind the heat shield.** Hold surface-retrograde (SAS surface retrograde) so the
   `HeatShield1` faces the airflow. The return craft MUST carry a heat shield (P14) — gated on the
   return requirement, not crew (the uncrewed Orion has `heatshield=True`). No shield → burns up.
8. **Parachute recovery.** Warp down through the upper atmosphere; below 8 km surface altitude deploy
   `control.parachutes = True` (or stage the chute). Settle to `landed`/`splashed` → `recovered`.

## MATH

- TEI Δv seed: leave Mun SOI prograde/retrograde to set a Kerbin periapsis ~30 km; search refines
  (the score targets 24–38 km, ideal 30 km).
- Reentry corridor: aim Kerbin periapsis ≈ 30 km (atmosphere top 70 km) — deep enough to capture, not
  so deep it over-heats. Chute deploy threshold: surface altitude < 8 km.

## WORKED EXAMPLE (Orion home)

From a parked Mun orbit, `_find_kerbin_return_node` picks a node whose `next_orbit.body == "Kerbin"`
with periapsis ≈ 30 km (best score). Execute `trans_kerbin_injection`; coast warps to Kerbin SOI →
`kerbin_soi_entered`. Hold surface-retrograde; the heat shield takes the −683 m/s entry; below 8 km
deploy the parachute; touchdown at ~−1.4 m/s → `recovered`. (These are the proven live numbers.)

## SUCCESS / FAILURE MARKERS

- SUCCESS: (`mun_return_orbit_established` →) `trans_kerbin_injection_complete` →
  `kerbin_soi_entered` → `recovered`.
- `mun_return_unstable_landing` → throttled up before settle (settle first, keep legs deployed).
- `mun_return_ascent_out_of_fuel` / `mun_return_orbit_timeout` → under-Δv ascent stage.
- `kerbin_return_node_not_found` / `kerbin_return_encounter_missed` → no return encounter; retry the
  node search (phasing variance) or top off.
- `kerbin_recovery_timeout` → chute didn't deploy / no heat shield (P14) — verify `heatshield=True` on
  the return craft and the < 8 km chute trigger.
