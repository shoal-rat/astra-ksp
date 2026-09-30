# Anomalies: diagnosing failures and recovering

Keywords: anomaly, failure, diagnosis, diagnose, abort, off-nominal, emergency, flameout, no thrust, tumble, tumbling, lost control, breakup, part lost, power, electric charge, recovery, checkpoint, restore, revert, contingency

Something did not go as predicted. The game is already paused (control only comes back to you
between reflexes), so there is time. The worst move is to repeat the same action and hope.

## 1. Method

1. **Stop and look.** `telemetry(detail="full")`: situation, orbit, attitude error, thrust, engines,
   resources, hottest parts, electric charge, crew. `vessel_stages` for what propulsion is left,
   `vessel_parts` for the state of specific parts, `camera_look` to see it, `game_log` for exceptions.
   Read the reflex report you just got: `stopped_by`, `events`, the trace.
2. **State the discrepancy precisely.** Predicted X, observed Y, since when (trace).
3. **List causes that fit all the evidence**, then find the reading that tells them apart (the table
   below). Confirm before acting on a guess.
4. **Assess the options** with numbers: what Δv and time are left (`vessel_stages`), what the safe
   states are (a stable orbit, a return trajectory, the ground), what each option costs.
5. **Decide and journal** (`journal_note(kind="anomaly")` then `decision`): continue with a changed
   plan, abort to a safe state, or `game_restore` a checkpoint. If you restore, say so; a restored
   attempt is a retry, not a success.
6. **Learn.** If it was non-obvious, `lesson_add` with the symptom, the cause, and the fix.

Do not repeat a failed action unchanged. If the same step fails twice, change the approach, the
parameters, or the vehicle.

## 2. Symptoms and their usual causes

| Symptom | Candidate causes | Tell them apart with |
|---|---|---|
| Throttle up, thrust 0 | rails warp still active; engine not staged/active; propellant starvation (connected tank empty, crossfeed off, wrong propellant); no ignition left | warp state (`game_status`), engine state and propellant (`vessel_parts`), `vessel_stages` |
| Thrust reads 0 right after a warp, orbit changing | KSP's thrust readout lags a few seconds after rails warp | semi-major axis or apsides moving, g-force > 0 |
| Thrust > 0, g-force ~0, orbit not changing | exhaust hitting a part of the same vessel (shroud, fairing) | `camera_look`, part layout below the engine |
| Prograde burn lowers apoapsis; remaining Δv grows | misalignment: attitude not converged, or the control part is not along the thrust axis | autopilot error, control-from part vs engine direction |
| Attitude error never converges | too little torque for the moment of inertia; aerodynamic torque at high q; no electric charge; SAS and autopilot fighting | electric charge, dynamic pressure, which control mode is on |
| `part_lost` without staging | overheating, aerodynamic breakup, collision, an autostager firing a decoupler | hottest parts, q and angle of attack in the trace, which parts are gone |
| Active vessel is now a spent stage or debris | staging or decoupling switched focus to the heavier piece | `game_list_vessels`; switch back by identity (crew, parts, orbit), not by name |
| Periapsis decaying in a "stable" orbit | periapsis inside the atmosphere; unthrottleable engine still burning | periapsis vs atmosphere depth, thrust during the coast |
| Predicted encounter missing after a burn | patched conics not settled yet; a real miss | wait a few seconds of game time, read again |
| Electric charge falling toward 0 | shadow, panels retracted or pointing away, reaction wheels saturating | resources, solar panel state, time to sunlight |
| Probe not responding | no charge, no CommNet link with a probe-only vessel | control state, comms signal |
| Stage out of fuel early | propellant in a different stage; engine/tank propellant mismatch | `vessel_stages` per-stage propellant; engine propellants |
| Tool error "timed out waiting for the Unity main thread" | game paused, loading, or lagging (the job may still run later) | `game_status` before retrying; never blindly repeat a launch or load |
| Launch refused / nothing happens | pad occupied, no control source, craft not in the active save | the tool's hint, `game_list_vessels`, `craft_list` |

## 3. Abort options by phase (derive the numbers each time)

- **Ascent.** Low in the atmosphere with a crew pod: separate the capsule and descend under parachutes
  (over water if possible). Higher up with enough Δv left: abort to orbit (any stable periapsis) and
  plan from there.
- **Parking orbit.** It is a safe state. Re-plan the rest of the mission against the Δv left.
- **Transfer.** Prefer a trajectory that returns to the parent (free return for moon trips); correct
  early while it is cheap.
- **Capture failed.** If still inside the sphere of influence, burn again at the next periapsis; if
  escaping, compute a correction back toward home or toward a capture on the next pass.
- **Landing.** If braking cannot stop the vessel in time, burn upward at full thrust while altitude
  allows and go back to orbit; landing somewhere else is better than not landing.
- **Surface.** Tilted or damaged lander: do not ignite until the tilt and engine state are understood.
- **Entry.** Little can be changed once in the air; the decisions are made before (corridor, what is
  jettisoned, chutes).

## 4. Keep the options open

- `game_checkpoint` before each irreversible or risky step, with a name that says what it holds.
- Keep Δv margin until the phase that needs it; spend reserves deliberately.
- A stranded crew in a stable orbit is recoverable; a crew on a botched entry is not. Uncrewed first
  when a procedure is new.
