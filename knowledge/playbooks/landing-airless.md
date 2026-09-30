# Landing on an airless body (and taking off again)

Keywords: landing, land, powered landing, powered descent, hoverslam, suicide burn, deorbit, touchdown, terrain, legs, mun, minmus, ike, gilly, micro gravity, fly_descent, surface ascent, liftoff from surface, tip over, tilt, hover, hovering, braking, drift, touchdown_drift_mps, settle, sas, pointing

With no air, the engine is the only brake. Every metre per second of orbital speed has to be
removed by thrust, and every second spent fighting gravity adds g·Δt to the bill. The art is to brake
late and hard enough to be efficient, but with enough margin that terrain and control lag cannot
catch you out.

## 1. Deorbit

- Start from a low orbit that clears the highest terrain on the ground track with margin
  (`compute_terrain` along the predicted track or over the landing region). If you are high, lower the
  apoapsis first with a computed node, then deorbit at apoapsis; a deorbit flown by a guessed attitude
  from a high orbit raised the periapsis twice in an earlier flight.
- Lower the periapsis so the trajectory meets the surface near the landing site:
  `compute_maneuver(kind="deorbit_to_periapsis")`, Δv = v(r_ap, a_old) − v(r_ap, a_new).
- **Shallow vs steep.** A periapsis just below the terrain means you arrive nearly horizontal at close
  to orbital speed: least gravity loss, most Δv-efficient, but the approach skims the terrain and the
  braking has to be precise. A deep periapsis gives a steeper, slower-closing descent with more
  vertical speed, more gravity loss, and more room for error. Choose from the terrain relief on the
  approach and your Δv margin; write the choice and the reason down.
- Large lowering burns: full throttle. A precision throttle cap made one deorbit time out.

## 2. The braking physics

For a vertical fall at speed v with maximum acceleration a_max = F/m and local gravity g:

- Stopping distance with a fraction f of full thrust: d = v² / (2(f·a_max − g)). Mass falls as fuel
  burns, so real deceleration grows; the constant-mass figure is conservative.
- The reference curve v_ref(h) = √(2(f·a_max − g)·h): stay below it and you can always stop by the
  ground. The braking burn starts when the speed meets the curve and then tracks it down.
- Hover needs local TWR > 1; `compute_descent(body, altitude_m, vertical_speed_mps,
  horizontal_speed_mps, mass_t, thrust_kn, ...)` gives stopping distance, time, hover TWR, and whether
  the stage can stop at all. If f·a_max ≤ g the stage cannot land.
- Altitudes in telemetry are from the centre of mass; `surface_altitude` is height above the terrain
  directly below, which changes as the terrain passes underneath. The lowest point of the vessel is
  below the centre of mass by a fixed offset; count it.
- Horizontal speed has to die before contact. Braking along surface retrograde kills horizontal and
  vertical speed together; switching to pure vertical while still drifting sideways preserves the
  slide (a 2 m/s sideways touchdown destroyed everything but the pod).

## 3. Choosing the descent reflex's numbers

`fly_descent` predicts every tick where a surface-retrograde burn at (1 − r)·a_max would stop
(integrating gravity, less the centrifugal relief of the horizontal speed) and holds that stop above
a gate `terminal_alt_m` over the *terrain floor*: the highest ground it samples along the track ahead,
out past the predicted stop, remembering peaks until it has passed them. It coasts while the stop
ends above the gate, then brakes with the lowest throttle (up to full) that still holds it. When even
full thrust retrograde cannot, or the lander reaches the gate still fast, it brakes height-first:
vertical thrust holds a sink it can still stop and the rest kills horizontal speed (the attitude is
then off retrograde by design). Below the gate and slow, the terminal phase sinks in proportion to
height and tilts against drift, hovers a few metres up until the drift is small, and sets down
upright. Derive its parameters for this lander and this site:

- **throttle_reserve r.** Margin on *when* braking starts, not on the burn (the burn may use full
  thrust). It covers what the predictor cannot see: throttle and attitude lag, pointing error, and
  terrain between the samples or beside the track (a crater rim off the ground track, a site on a
  slope). Relief along the track ahead is already sampled, so do not size r for it. A slow-turning
  lander, rough ground or an uncertain site → more; smooth, well-mapped ground → less. More reserve
  brakes earlier and costs fuel, and (1 − r)·a_max must still beat surface gravity or the call refuses.
- **terminal_alt_m.** Height of the vessel's lowest point over that terrain floor (or the ground
  below, if higher). High enough to cancel the residual horizontal speed with the tilt you allow:
  lateral acceleration ≈ g·tan(max_tilt) (less on a low-TWR lander, since the sink command takes its
  vertical thrust first), time ≈ v_h / that, and the vessel keeps sinking meanwhile. It also sets the
  terminal speed (terminal_rate × terminal_alt_m, capped by what the engine can stop). Every metre of
  terminal descent is near-hover and costs about g per second of it, so no higher than needed.
- **touchdown_mps.** Below the landing legs' crash tolerance with margin (read the part with
  `part_info`), lower on slopes.
- **touchdown_drift_mps.** The sideways speed at contact the stance absorbs without tipping. A foot
  that catches turns the slide into rotation about it; the lander goes over when that rotation lifts
  the centre of mass past the foot. Treating the lander as a point mass at its centre of mass (which
  errs toward caution), that happens above about v² = 2g·(L − h)·L²/h², where h is the centre-of-mass
  height above the feet, b the horizontal distance from the centre of mass to the downhill feet (on a
  slope θ, about b₀·cos θ − h·sin θ for a stance half-width b₀), and L = √(b² + h²). Choose well below
  that. A smaller value costs hover time (about g per second of hover). Sanity check: a tall 1.25 m
  stack tipped on the Mun at about 2 m/s sideways on a 4° slope.
- **terminal_rate** (sink per metre of height, 1/s): its inverse is the time constant of the final
  descent. Keep that several times longer than the time the vessel needs to change attitude
  (angular acceleration = torque / moment of inertia), so the drift correction keeps up.
- **max_tilt_deg and tilt_gain_deg_per_mps** (both > 0). Drift above max_tilt/gain falls at about
  g·tan(max_tilt) m/s per second; below it, it decays exponentially at a rate of about g·gain per
  second (gain in rad per m/s).
  Size them so the drift you expect at the gate dies within about 1/terminal_rate; a large gain on a
  slow-turning lander oscillates, because the drift correction is only as fast as the attitude.
- **sink_gain** (1/s): gain of the terminal vertical-speed loop; its inverse is that loop's time
  constant. Keep that time constant longer than the throttle and attitude response, or the throttle
  chatters near the ground.
- **legs_alt_m.** Deploy the legs with a few seconds of fall to spare above the terminal phase.
- **max_game_s.** Longer than the free-fall time from where you start plus the braking and terminal
  phases, the hover and the 4 s settle; `compute_descent` gives the pieces. A timeout cuts the
  throttle in the air.

Journal these numbers with their reasoning before the call, then compare the report (touchdown
vertical and horizontal speed, fuel used, `hover_to_cancel_drift_s`, `brake_held_for_pointing_s`)
with the plan.

## 4. Flying it

1. `game_checkpoint` in orbit.
2. Deorbit node → `fly_burn`; check the new trajectory meets the surface where you meant.
3. Coast: `fly_warp` with `floor_alt_m` set to a height above the terrain you computed (the warp
   refuses to descend past it). No physics warp during braking. Warp keeps the attitude of the
   deorbit burn: before a low hand-off, turn surface-retrograde first (`control_attitude`).
4. `fly_descent` with your parameters once the vessel is falling toward the surface. It takes the
   attitude over at once (SAS off). On a late hand-off braking starts at once, but braking thrust is
   held back until the vessel points within about 10° of the braking direction (none beyond 45°), so
   turn time is braking time lost; a hand-off with a coast ahead turns during the coast.
5. After contact the reflex cuts the engine, runs 4 more game seconds, and leaves the autopilot off
   and SAS on, holding the settled attitude (a held direction would torque the lander over its legs).
   Then `telemetry` until vertical and horizontal speeds stay near zero, check the tilt (pitch close
   to 90° means upright) and that the legs are grounded. If the report says the settle was cut short
   (an interlock at contact), look before doing anything else.

MechJeb alternative: `mj_land` (targeted or untargeted) with explicit touchdown speed; watch it with
triggers, and deorbit it yourself first if it will not.

**Micro-gravity (Gilly, Minmus, small moons).** When a_max is many times g, the braking law becomes
twitchy and small throttle steps are large accelerations. Limit engine thrust (`control_part`
thrust_limit) so a_max is a modest multiple of g, and descend gently; the hoverslam law was unstable
below ~0.5 m/s² gravity in earlier flights. Escape speed can be tiny (Gilly ≈ 36 m/s): a hard bounce
can put you back in orbit.

## 5. Taking off again

- Before ignition: landed situation, speeds settled near zero for a while, tilt small (a tilted lander
  at full throttle drives its engines into the ground), crew aboard, legs still deployed.
- The ascent from an airless body has no drag: rise only until the terrain is cleared, then pitch
  over hard toward the orbit direction to cut gravity loss. Target a periapsis above the highest
  terrain on the track.
- Do not stage near the ground; retract the legs once clear.

## What goes wrong, and how it shows

| Telemetry | Meaning | Response |
|---|---|---|
| Throttle pinned at 1 while speed stays above the curve | braking started late (warp step, terrain higher than assumed) | if the descent report says it cannot stop: abort upward with full thrust while altitude allows |
| Large `brake_held_for_pointing_s` in the descent report | handed over pointing away from retrograde, or a slow-turning lander | turn retrograde before the hand-off; hand off higher next time |
| Throttle oscillating 0/1, vertical speed bouncing | bang-bang control near the ground | larger terminal altitude, lower gains, or thrust limit (micro-gravity) |
| Horizontal speed not decreasing in the terminal phase | tilt limit too small for the drift | more tilt allowed, higher terminal altitude next time |
| Thrust > 0 but g-force ~0 | exhaust blocked by the vessel's own shroud | no fix in flight except jettisoning the obstruction |
| Tilt after landing well away from upright, or sliding | slope, lateral speed at contact, narrow stance | do not ignite; assess; fly a short hop only if safe |

## Sanity checks (not targets)

- Circular speed at 10 km: Mun ≈ 557 m/s, Minmus ≈ 159 m/s, Gilly ≈ 19 m/s; Ike at 15 km ≈ 358 m/s.
- Surface gravity: Mun 1.63, Minmus 0.49, Ike 1.10, Gilly 0.049 m/s² (read them live).
- A Mun landing from low orbit typically costs somewhat more than the orbital speed (gravity loss
  during braking); if a plan needs far less than the orbital speed, something is missing.
