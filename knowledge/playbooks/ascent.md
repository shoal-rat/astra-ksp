# Ascent: from the pad to a parking orbit

Keywords: launch, liftoff, ascent, gravity turn, pitch program, pitch over, kick, max q, dynamic pressure, meco, orbit insertion, parking orbit, launch azimuth, inclination, mj_ascent

The ascent turns chemical energy into orbital speed while losing as little as possible to gravity,
drag, and steering. The target is a stable orbit, in the plane you need, with the least Δv spent.

## 1. Choose the orbit you are going to

- **Stable means clear of the atmosphere (or terrain).** Read the atmosphere depth with `body_info`;
  on an airless body use the highest terrain along the track (`compute_terrain`). A periapsis above
  that is a permanent orbit in KSP; there is no drag above the atmosphere's edge.
- **How far above** is a trade: higher costs more Δv on the way up and costs Oberth efficiency for a
  later ejection (a low circular parking orbit is best for departures); it also allows faster rails
  warp (warp limits scale with altitude) and more margin for an imprecise insertion. Decide from what
  the next phase needs and write the reason down.
- **Inclination.** The lowest inclination reachable directly is the launch site's latitude. For an
  inclination i from latitude φ, the inertial launch azimuth β satisfies sin β = cos i / cos φ.
  Correct it for the ground's eastward speed v_eq·cos φ (v_eq = 2πR/T_rotation):
  tan β_launch = (v_orbit·sin β − v_eq·cos φ) / (v_orbit·cos β). Due east (heading 90°) for an
  equatorial orbit gets the full rotation assist. Plane changes later cost 2·v·sin(Δi/2): fix the
  plane during the ascent, not after.
- `compute_ascent_estimate(body, orbit_alt_m)` gives the orbital speed, the rotation assist, and a
  labelled range for losses. Losses depend on your vehicle; the range is a check, not a budget line.

## 2. The physics of a good ascent

- **Gravity loss** ≈ ∫ g·sin γ dt (γ = flight-path angle above the horizon). Flying vertical for long
  is expensive; turning horizontal early and accelerating hard reduces it.
- **Drag loss** ≈ ∫ D/m dt with D = q·Cd·A and q = ½ρv². Going fast low in the thick air is
  expensive; climbing out of the dense layers before building speed reduces it.
- **Steering loss** appears when thrust is not along velocity (angle of attack). Keep the angle of
  attack small whenever q is large: aerodynamic torque at high q is what flips rockets.
- The **gravity turn** balances these: after a short vertical rise, pitch over slightly and then let
  the velocity vector fall under gravity while thrust follows it (zero angle of attack). The turn
  rate is dγ/dt = −g·cos γ / v: slow vehicles turn fast, fast-accelerating vehicles turn slowly. So a
  high-TWR vehicle needs a larger or earlier pitch-over; a low-TWR vehicle a smaller, gentler one, or
  it will lie down too early and fall back.
- **Density profile.** Density falls roughly exponentially, ρ ≈ ρ0·e^(−h/H). Get ρ(h) from
  `body_info` and the local scale height from two samples: H = Δh / ln(ρ1/ρ2). Most drag is lost
  within the first couple of scale heights; by the time density is a few percent of sea level, drag
  is small and the vehicle should be well into its turn.

## 3. Deriving the steering for this vehicle

There is no universal pitch profile. Derive yours from the vehicle's thrust-to-weight and the
body's atmosphere, fly it in segments, and correct from what the telemetry shows.

1. **Vertical rise.** Climb straight up until the vehicle has enough speed that a small pitch-over will
   not be aerodynamically unstable and has cleared the launch structures. The speed is the choice;
   the check is that the angle of attack stays small right after the kick.
2. **Pitch-over (kick).** A few degrees toward the launch azimuth. Size it with the turn-rate relation
   above: the higher the TWR, the larger the kick needed to end up horizontal near the edge of the
   dense air. If unsure, start small; a too-small kick is corrected by flying the next segment with a
   pitch clamp, while a too-large one low in the atmosphere costs more.
3. **Follow the velocity.** Attitude `{"mode": "prograde", "frame": "surface", "heading": <azimuth>,
   "min_pitch": <floor>}` flies a gravity turn with zero angle of attack. The `min_pitch` floor stops
   the vehicle from lying down too early while it is still low; relax it as it climbs. Alternatively
   supply your own table with `{"mode": "pitch_program", "by": "altitude", "points": [[h, pitch], ...]}`
   built from the density profile and TWR.
4. **Watch these, every segment:**
   - `time_to_apoapsis`: if it keeps shrinking while the apoapsis is still far below target, the
     vehicle is too flat (it will fall back): pitch up. If it grows very large while high, it is too
     steep: pitch down.
   - `dynamic_pressure` and `angle_of_attack`: large q with a growing angle of attack is a flip in the
     making. Cap q with `"max_q_pa"` on the throttle law if the vehicle cannot hold attitude at its
     natural max-Q; choose the cap from where the attitude error started to grow.
   - `apoapsis_altitude` rising steadily; `vertical_speed` positive until the apoapsis nears target.
5. **Cut-off.** Stop the burn when the apoapsis reaches the target, feathering in with
   `{"mode": "approach", "metric": "apoapsis_altitude", "target": <m>, "feather_s": <s>}`. While still
   inside the atmosphere drag keeps lowering the apoapsis during the coast; check it again when the
   vehicle leaves the atmosphere and trim if needed.
6. **Circularize.** `compute_maneuver(kind="circularize", ...)` at the apoapsis: Δv = v_circ(r_ap) −
   v(r_ap) from vis-viva. Create the node, check the predicted orbit, and execute with `fly_burn`. The
   burn should start about half its duration before the apsis (`compute_rocket` gives burn time and the
   half-Δv time for the live mass).

Fly the ascent as short `fly_until` segments low in the atmosphere (things change fast there) and
longer ones higher up. Typical triggers: a speed or altitude you chose for the next steering change,
`apoapsis_altitude >= target`, `time_to_apoapsis <= floor`, `{"event": "flameout"}`,
`{"event": "part_lost"}`. Always set `max_game_s`.

## 4. Staging during the ascent

- The reflex stops on flameout unless you enabled `auto_stage`. Enable it only when you have already
  decided that the next stage should fire on burnout, and journal that decision.
- Tank crossfeed transients can make an engine read dry for an instant; the engine debounces this, but
  still confirm a real burnout (propellant in the stage ~0) before dropping anything.
- Separation must leave an engine on the command side. Look at the stage table (`vessel_stages`)
  before staging, then confirm the part count dropped and the next engine lit (thrust > 0).
- Solid boosters cannot be throttled or shut down: plan the cut-off around their burnout, and never
  assume throttle 0 means no thrust during a coast while they are attached.
- An oversized lower stage can reach orbit with fuel left. Decide deliberately whether to keep using
  it or drop it before the next burn; do not drag a dead stage into a transfer.

## 5. MechJeb as the pilot

`mj_ascent` flies a proven gravity turn to an altitude and inclination you set, with every setting
explicit. Known behaviour: it does not ignite the first stage from pre-launch (stage once yourself);
if you do your own staging, turn its autostage off, or two stagers race; its staging controller can
stay armed after the ascent and fire decouplers during later burns, so release it (`mj_abort` with
the staging module) before in-space burns. Watch it with triggers and check the result like any
maneuver.

## What goes wrong, and how it shows

| Telemetry | Meaning | Response |
|---|---|---|
| Apoapsis stuck near the pad, vertical speed ~0, throttle 1 | TWR ≤ 1 or no thrust | stop; check thrust and mass; redesign or lighten |
| Apoapsis falling while under power, still low | vehicle too flat, falling back | pitch up; if unrecoverable, abort |
| Angle of attack growing with high q, heading wandering | aerodynamic instability | throttle down (max-q cap), smaller steering changes; redesign fins/fairing |
| `part_lost` without staging | breakup (aero, heat, collision) | stop, read full telemetry, assess what is left |
| Thrust present during a coast after throttle 0 | unthrottleable engine still burning | hold a safe attitude, watch the apoapsis; drop it if it pushes toward escape |
| Speeds reading ~0 during an obvious climb | reading in a co-moving frame (tool bug) | trust apoapsis and altitude trends; report it |

## Sanity checks (not targets; recompute for your case)

- Kerbin: circular speed at 80 km ≈ 2,279 m/s; equatorial rotation ≈ 175 m/s; atmosphere 70 km.
  kRPC density: ≈ 1.14 kg/m³ at sea level, ≈ 25 % of that at 10 km, ≈ 3.5 % at 20 km, ≈ 0.5 % at
  30 km (scale height ≈ 5–7 km).
- Stock rockets usually spend ~3.2–3.5 km/s to reach a low Kerbin orbit; if your ascent used far
  more, look for drag (blunt payload), too steep a climb, or a low-TWR upper stage.
- Airless ascent: roughly the circular speed at a low terrain-clearing altitude plus a modest gravity
  loss (Mun circular speed at 10 km ≈ 557 m/s; Minmus ≈ 159 m/s).
