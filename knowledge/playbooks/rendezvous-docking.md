# Rendezvous and docking

Keywords: rendezvous, docking, dock, intercept, phasing, closest approach, relative velocity, match velocity, target, docking port, rcs, monopropellant, approach, station, mj_rendezvous, mj_dock, crew transfer

Rendezvous is orbital mechanics (get two vessels to the same place at the same time with the same
velocity); docking is short-range piloting on RCS. Do them in that order and do not let the second
job's tools do the first job (the docking autopilot closing a kilometre gap on RCS drained the
monopropellant and stalled).

## 1. Planes first

`target_info` gives the relative inclination and the times of the relative ascending and descending
nodes. The fix is a normal (or antinormal) burn at a node: Δv = 2·v·sin(Δi/2), cheapest where the
orbital speed is lowest. Match planes before phasing; a plane error left in place makes every later
burn more expensive. For a launch to a target in orbit, launch when the site passes under the
target's plane and fly the ascent in that plane (see ascent: launch azimuth).

## 2. Phasing and intercept

- Phase angle θ: how far the target leads you along the orbit (`target_info`).
- **Same orbit, target ahead by θ:** fly a lower, shorter orbit for k revolutions with period
  T_ph = T·(1 − θ/(2πk)); behind by θ: a higher, longer one. More revolutions means a smaller phasing
  burn and a longer wait. Return to the target's orbit at the meeting point.
- **Different circular orbits:** a Hohmann transfer between them meets the target if, at the burn,
  the target leads by θ = π − n_t·t_tr (n_t the target's mean motion, t_tr the transfer time). Wait
  for that geometry: t_wait = (θ_now − θ)/(n_ship − n_t) modulo the synodic period.
- Refine with `compute_node_search` against the closest approach; place the node; read the predicted
  closest approach with `target_info`. KSP's closest-approach estimate for two conics in the same SOI
  is good; for anything crossing an SOI boundary, sample it.

## 3. Matching velocity at closest approach

At the closest approach the relative velocity v_rel has to go to zero: burn along −v_rel for |v_rel|.
Start the burn half its duration before closest approach (`compute_rocket` for the time). The
`target`/`anti_target` attitudes point along the line of sight, not along −v_rel. To point against
the relative velocity:

- **SAS in target speed mode:** `control_set(speed_mode="target")`, then attitude
  `{"mode": "sas", "sas_mode": "retrograde"}`. Navball retrograde in target mode is exactly −v_rel.
  Needs a control point whose SAS offers retrograde (basic probe cores do not).
- **Autopilot vector:** `target_info` gives v_rel (target minus vessel) resolved on the vessel's
  prograde / normal / radial_out axes. Those are the axes of the `orbital` frame (y prograde, z
  normal, x radial), so a `vector` attitude in the `orbital` frame built from −v_rel works, but
  confirm the sign of the radial axis first: after a short low-throttle pulse, `target_speed` must
  fall. Do not feed these components to the `inertial` frame; its axes are different.

Stop on `target_speed`. `target_info` differences the two inertial velocities; a relative velocity
read in a rotating frame instead includes about n·d of frame motion at distance d (tens of m/s at
10 km in a low orbit).

## 4. Closing in

- Choose a closing speed you can always stop from: with braking acceleration a_b available
  (thrust/mass for the main engine, RCS force/mass on RCS), the fastest safe speed at distance d is
  √(2·a_b·d); fly well below it and slow down as d shrinks.
- Close on the main engine to tens of metres (`mj_rendezvous` does this well with its desired distance
  and closing speed set by you), then switch to RCS for the last part.
- Target the **docking port**, not the vessel (`target_set` with the port), and control from your own
  port (`control_part` control_from) so "forward" is the docking axis.
- Final approach on RCS along the target port's axis at a few tenths of a metre per second, lateral
  error nulled before the last metres. `mj_dock` flies this with a speed limit you choose. Size
  monopropellant for it; there is no refuelling.
- Parts and docking ports of the other vessel are only available once it is loaded (within a couple
  of kilometres).

## 5. Confirming the dock

Docking merges the two vessels into one: the target's name disappears and the part count jumps
(e.g. 21 → 42 in a verified run). A docking autopilot switching itself off is not proof (it also
switches off when it loses the target); confirm with the part count or a port state containing
"Docked". Crew then move inside the merged vessel with `crew_transfer`.

## What goes wrong, and how it shows

| Telemetry | Meaning | Response |
|---|---|---|
| Closest approach grows after each correction | overcorrecting, or corrections with unintended radial parts | stop, re-plan from the current orbits |
| Relative speed looks large and wanders at long range | reading it in a rotating frame | use the inertial relative velocity |
| RCS translation moves the wrong way | axis sign confusion (vessel frame: x right, y forward, z down) | test a short pulse, watch `target_distance`, then fly |
| Monopropellant falling fast, distance barely closing | RCS doing the main engine's job | back off, close with the main engine |
| Port parts missing on the target | target not loaded yet | get closer, read again |

## Sanity checks (not targets)

- A target in a circular 100 km Kerbin orbit has a period ≈ 33 min; a circular orbit 10 km lower is
  ≈ 42 s faster per lap and gains ≈ 7.7° per lap on it.
- A 1° plane mismatch at ≈ 2.3 km/s costs ≈ 40 m/s.
