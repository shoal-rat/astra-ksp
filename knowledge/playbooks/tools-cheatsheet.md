# Tools cheat-sheet: combining instruments, slide rule, and stick

Keywords: tools, cheatsheet, cheat sheet, how to, workflow, fly_until, trigger, triggers, until, throttle law, attitude law, reflex, interlocks, auto_stage, fly_burn, fly_warp, fly_descent, checkpoint, journal, examples, compute_calc, units, mechjeb, autopilot, mj_status, mj_abort, hyperbolic

This page shows how the tools fit together. Everything in angle brackets is a number **you compute
for this vehicle and this situation**; the text next to it says how. Exact parameter lists are in
each tool's description (`astra tools -v`).

## 1. The loop, in tool calls

| Step | Tools |
|---|---|
| Observe | `telemetry` (brief; full when something is off), `orbit_info`, `vessel_stages`, `target_info`, `body_info`, `camera_look` |
| Assess | compare with your last prediction; the last reflex report's `stopped_by`, `events`, trace |
| Compute | `compute_*`, `compute_calc` for anything else |
| Decide | `journal_note(kind="decision", ...)`: action, expected result, abort condition |
| Act | one `control_*` / `node_*` command, or one `fly_*` reflex with explicit triggers |
| Verify | read the report; `orbit_info`/`telemetry`; `journal_note(kind="result", ...)` |

## 2. Which instrument answers which question

| Question | Tool |
|---|---|
| Where am I, how fast, what is my orbit, attitude, thrust, TWR? | `telemetry` |
| What does each stage have left (Δv, TWR, burn time), and what lights next? | `vessel_stages` |
| Which part is which (index, staging, decoupler, chute, leg state)? | `vessel_parts` |
| Where does my trajectory go (patches, encounters, SOI changes)? | `orbit_info` |
| Body constants: μ, radius, rotation, atmosphere profile, SOI, terrain height | `body_info` |
| Where is the target, how fast relative to me, closest approach, phase, AN/DN? | `target_info` |
| What does it look like? | `camera_look` |
| What is the game doing (scene, pause, active vessel, active mission)? | `game_status` |

## 3. `fly_until`: the general reflex

A reflex applies a throttle law and an attitude law, runs the simulation, and stops at the first
trigger that fires (or an interlock, or `max_game_s`). Then the game pauses and you get a report.

**Triggers** (any-of): `{"metric": <name>, "op": ">=", "value": <x>}` over the telemetry metrics
(apoapsis_altitude, periapsis_altitude, time_to_apoapsis, surface_altitude, vertical_speed,
surface_speed, dynamic_pressure, eccentricity, next_periapsis_altitude, target_distance, ...) or
`{"event": <name>}` for flameout, all_engines_out, staged, part_lost, soi_change,
situation_change, landed, apoapsis_passed, periapsis_passed, node_passed.

**Throttle laws:** a number; `"keep"`; `{"mode": "twr", "twr": <x>}` (thrust-to-weight held in local
gravity); `{"mode": "approach", "metric": <m>, "target": <x>, "feather_s": <s>, "min": <lo>,
"max": <hi>}` (drives a metric to a target, easing off when the time to reach it at full throttle
falls below feather_s, and ends the reflex when reached); any of them plus `"max_q_pa": <Pa>`.

**Attitude laws:** `{"mode": "hold", "pitch": <deg>, "heading": <deg>}`; `{"mode": "pitch_program",
"by": "altitude", "points": [[<x>, <pitch>], ...], "heading": <deg>}`; `{"mode": "prograde" |
"retrograde", "frame": "surface" | "orbital", "min_pitch": <deg>, "heading": <deg>}`; `"normal"`,
`"antinormal"`, `"radial_out"`, `"radial_in"`, `"up"`, `"target"`, `"anti_target"`, `"node"`,
`{"mode": "vector", "vector": [x, y, z], "frame": ...}`, `{"mode": "sas", "sas_mode": ...}`, `"free"`,
`"keep"`. Basic probe cores only have stability-assist SAS; the autopilot modes work on anything
with torque.

**Choosing the bounds.** `max_game_s`: longer than you expect the segment to take (from your own
estimate: Δv / acceleration, distance / speed, time to apsis) but short enough that a wrong
assumption is caught early. Short segments where things change fast (low in the atmosphere, near the
ground), long ones on a coast. `auto_stage`: off unless you already decided the next stage fires on
burnout. Interlocks only stop the reflex; leave them on unless you know why one would trip falsely.

### Examples

Liftoff to the pitch-over point:

    fly_until(
      until=[{"metric": "surface_speed", "op": ">=", "value": <speed where you start the turn>},
             {"event": "flameout"}, {"event": "part_lost"}],
      throttle={"mode": "twr", "twr": <liftoff TWR you chose, at most max_twr from telemetry>},
      attitude={"mode": "hold", "pitch": 90, "heading": <launch azimuth from the inclination formula>},
      max_game_s=<a few times the time to reach that speed at (TWR − 1)·g>)

Gravity turn to the target apoapsis:

    fly_until(
      until=[{"metric": "apoapsis_altitude", "op": ">=", "value": <target apoapsis>},
             {"metric": "time_to_apoapsis", "op": "<=", "value": <floor below which you must pitch up>},
             {"event": "flameout"}, {"event": "part_lost"}],
      throttle={"mode": "approach", "metric": "apoapsis_altitude", "target": <target apoapsis>,
                "feather_s": <seconds of margin at the current acceleration>, "min": <floor>, "max": 1,
                "max_q_pa": <only if the vehicle loses attitude at its natural max-Q>},
      attitude={"mode": "prograde", "frame": "surface", "heading": <azimuth>, "min_pitch": <floor>},
      max_game_s=<estimate from the remaining Δv / acceleration>)

Capture burn from a hyperbolic arrival, protecting the periapsis. While the orbit is unbound the
apoapsis has no value (null in reports, +infinity inside triggers and laws), so an `apoapsis <= x`
trigger cannot fire early and an `approach` on apoapsis burns at full throttle until the orbit is
bound, then feathers. Driving the eccentricity works too and is smooth through e = 1:

    fly_until(
      until=[{"metric": "eccentricity", "op": "<=", "value": <(r_a − r_p)/(r_a + r_p) of the orbit you want>},
             {"metric": "periapsis_altitude", "op": "<=", "value": <terrain or atmosphere floor>},
             {"event": "flameout"}],
      throttle={"mode": "approach", "metric": "eccentricity", "target": <same e_target>,
                "feather_s": <from de/dv = 2·r_p·v/μ and your tolerance>},
      attitude={"mode": "retrograde", "frame": "orbital"},
      max_game_s=<1.5 × computed burn time>)

Parachute descent:

    fly_until(
      until=[{"event": "landed"}, {"event": "part_lost"}],
      throttle=0, attitude="free", physics_warp=<0 during the hot part; more only under open chutes>,
      max_game_s=<altitude / expected descent rate, with margin>)

The impact interlock knows about parachutes: a fast fall under chutes that are semi-deployed above
their full-deploy altitude is expected (it records an `impact_watch` event and keeps flying). If it
still trips, read the chute states it reports before deciding.

## 4. The special reflexes

- **`fly_burn`** executes a maneuver node: warps to the start minus `align_margin_s`, turns to the burn
  vector, ignites at the planned start once within `align_deg` (or does not burn at all if still
  unaligned `max_late_s` after the start: a late burn along the node's fixed vector bends the orbit,
  so re-plan instead), feathers, cuts at `tolerance_mps`, and reports the applied Δv (integrated over
  game time). Derive: `align_deg` from the off-axis error you can accept (a pointing error δ puts
  Δv·sin δ in the wrong direction: 1° on a 1,000 m/s burn is ~17 m/s); `max_throttle` so a small
  correction lasts long enough to control (burn time ≈ m·Δv/(F·max_throttle) should be several
  seconds); `feather_s` from how precisely the burn must end; `stop_when` to stop on the outcome
  (e.g. `next_periapsis_altitude`) instead of the node's Δv. Check the control point first: the
  autopilot aims the control part, not the engines.
- **`fly_warp`** (`to` = ut, in, apoapsis, periapsis, soi, node; `offset_s` = the lead you need to
  turn and settle before the event) turns the throttle off and in flight never warps into an
  atmosphere (a landed, splashed or pre-launch vessel can rails-warp on the ground, e.g. to wait for
  a launch window); on airless bodies pass `floor_alt_m` (a height above terrain you computed). Low
  orbits warp slowly in real time; plan long waits accordingly. Warp keeps the vessel's attitude.
- **`fly_descent`** flies a powered landing from a descending trajectory: it holds braking thrust
  until the vessel points near the braking direction, so turn retrograde before a low hand-off, and
  after contact it leaves the autopilot off and SAS on. Its parameters are derived in the
  landing-airless playbook.
- **MechJeb** (`mj_ascent`, `mj_execute_node`, `mj_land`, `mj_rendezvous`, `mj_dock`) with `watch` and
  `max_game_s` runs as a reflex too. Every setting is yours; `mj_status` shows what it is doing and
  `mj_abort` releases its modules (including the staging controller, which otherwise may stage during
  later burns).

- **EVA tools** run game time too: `crew_eva` until the kerbal stands, `crew_walk` (with `wait`)
  until the walk ends, which can be minutes, `crew_hop` until the kerbal stands again,
  `crew_plant_flag` until KSP names the flag, `crew_board` and `crew_transfer` until the seat changes.
  Their results report the game time that ran and `paused`; the EVA playbook has the details.

## 5. Instant commands

`control_set` (throttle, SAS, RCS, gear, legs, lights, brakes), `control_stage` (next stage; reports
what fired and lit), `control_attitude` (persistent autopilot or SAS target), `control_part`
(decouple, jettison, deploy, arm, activate, shutdown, thrust_limit, crossfeed, control_from on one
part), `control_action_group`, `node_create` / `node_list` / `node_delete`, `target_set`. Part
indices from `vessel_parts` are valid until the vessel changes: re-read after staging or docking.
Staging and decoupling run the few physics frames KSP needs to split the vessel, and
`control_attitude` with `wait_aligned_deg` runs game time until aligned; both report it
(`game_s_elapsed`, `waited_game_s`). `game_switch_vessel`, `game_checkpoint` and `game_restore` also
run a moment of game time.

## 6. The slide rule

| Tool | Gives |
|---|---|
| `compute_calc` | any formula: `{"expression": "sqrt(mu*(2/r - 1/a))", "variables": {"mu": ..., "r": ..., "a": ...}}` |
| `compute_orbit` | circular/periapsis/apoapsis speeds, period, SMA |
| `compute_hohmann` | Δv1, Δv2, transfer time, phase angle |
| `compute_rocket` | rocket equation both ways, burn time, half-Δv time, TWR on a body |
| `compute_descent` | stopping distance and time, hover TWR, feasibility |
| `compute_ascent_estimate` | orbital speed, rotation assist, a labelled loss range |
| `compute_maneuver` | node parameters for circularize, set_apsis, deorbit_to_periapsis, plane_change, hohmann_to_body, return_from_moon, capture_at_periapsis |
| `compute_node_search` | refines a node against KSP's own patched conics (game paused while it searches) |
| `compute_transfer_window` | Lambert window: departure time, flight time, v∞, ejection Δv |
| `compute_terrain` | highest terrain over a region or along the predicted track |

## 7. Bookkeeping

- `game_checkpoint` names say what they hold: `pad-<craft>`, `lko-before-tmi`, `mun-orbit-pre-deorbit`.
- Journal kinds: plan (phase intent and success criteria), decision (action, expectation, abort
  condition), observation, anomaly, result (outcome against prediction).
- `lessons_search` before each phase; `lesson_add` when something surprised you.
- `capcom_say` for the human player; `capcom_inbox` at phase boundaries.
- Specialists via the Agent tool: `booster-engineer` (vehicle from requirements) and `fido` (maneuver
  plans with the math). Give them the numbers; verify what they return.

## 8. Units and traps

- Results are SI with unit suffixes: `_m`, `_mps`, `_s`, `_deg`, `_t`, `_kn`, `_pa`. Telemetry
  trigger metrics use the units in their descriptions (mass in kg, thrust in N, pressure in Pa).
- Engine performance at a pressure is quoted per atmosphere (1 atm = 101,325 Pa).
- UT is absolute game time; many results also give "time to" in seconds from now. Do not mix them.
- On a hyperbolic (escaping or arriving) orbit `apoapsis_altitude` is negative and
  `time_to_apoapsis` and `period` are not finite; they jump when the orbit becomes bound. Trigger on
  `eccentricity`, `periapsis_altitude`, or `next_periapsis_altitude` there. A trigger whose metric is
  not finite (NaN) never fires, so pair it with `max_game_s`.
- Part titles and some vessel names are localized; use internal part names and vessel identity.
- After staging or undocking the active vessel can change; `telemetry` shows which vessel you have.
