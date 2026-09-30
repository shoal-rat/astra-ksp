# Orbital maneuvers: changing an orbit you are already in

Keywords: orbit, orbit change, circularize, circularization, apoapsis, periapsis, apsis change, hohmann, bi-elliptic, plane change, inclination, phasing, maneuver node, node, burn, finite burn, burn time, lead time, fly_burn, mj_execute_node, feather

## 1. The relations to compute with

- Vis-viva: v = √(μ(2/r − 1/a)), a = (r_ap + r_pe)/2 (radii from the body's centre = altitude + R).
  `compute_orbit` evaluates speeds, period, and SMA for any apsides.
- A burn changes the orbit on the **opposite** side. Prograde at periapsis raises the apoapsis;
  retrograde at apoapsis lowers the periapsis. The Δv to move the opposite apsis from r_old to r_new
  while burning at radius r: Δv = v(r, a_new) − v(r, a_old).
- **Circularize at an apsis r:** Δv = √(μ/r) − v(r, a). `compute_maneuver(kind="circularize")`.
- **Hohmann between circles r1 → r2:** Δv1 = v(r1, a_t) − √(μ/r1), Δv2 = √(μ/r2) − v(r2, a_t),
  a_t = (r1 + r2)/2, transfer time π√(a_t³/μ). `compute_hohmann`. For r2/r1 above ≈ 11.9 a
  bi-elliptic transfer is cheaper in Δv (and much slower).
- **Plane change** by Δi at speed v: Δv = 2·v·sin(Δi/2), along the orbit normal (normal or
  antinormal), at the ascending or descending node relative to the plane you want. It is cheapest
  where v is smallest (near apoapsis); combine it with another burn there when you can. The burn
  direction is the orbit normal at the node, not the difference of the two planes' normal vectors
  (that vector points nearly along the velocity for small angles, a bug that cost the old code).
  `compute_maneuver(kind="plane_change")`.
- **Period** T = 2π√(a³/μ); **phasing**: to catch up with a target that leads you by an angle θ on
  the same orbit (period T) after k revolutions of your own, fly an orbit with period
  T_ph = T·(1 − θ/(2πk)); to let a target that trails you by θ catch up, T_ph = T·(1 + θ/(2πk))
  (shorter = lower, to gain; longer = higher, to fall back). Then return to the original orbit.
- `compute_calc` for anything not covered, with the formula written out.

## 2. Finite burns

A node is an instantaneous impulse; a real burn takes time.

- Burn time: t_b = (m0·ve/F)(1 − e^(−Δv/ve)), ve = Isp·g0. Start the burn so that half the Δv is
  delivered by the node time: t_half = (m0·ve/F)(1 − e^(−Δv/(2ve))), which is slightly more than
  t_b/2 because the vehicle gets lighter. `compute_rocket` computes both from live mass, thrust, Isp.
- A burn long compared with the time spent near the apsis (a sizeable fraction of the orbital period)
  loses efficiency and lands off target. Split it (e.g. raise the apoapsis over several periapsis
  passes) or accept and correct afterwards.
- Pointing: the autopilot aims the **control part**, not the engines. If the control part is not
  aligned with the thrust axis, a "prograde" burn goes somewhere else. `fly_burn` refuses when the
  offset is large; fix it with `control_part` (control_from) on a part aligned with the engines.
- Align before ignition and burn at full throttle for large burns (a slow, throttled-down ejection lost
  its fuel to gravity loss). Feather at the end: throttle down once the remaining Δv is a couple of
  seconds of full acceleration, so the cut-off does not overshoot. Choose `feather_s`,
  `min_throttle`, and `tolerance_mps` from the stage's acceleration F/m: a high-thrust stage doing a
  small correction needs a low `max_throttle` so the burn lasts long enough to control.
- When precision matters more than the node's Δv (an encounter periapsis, a capture apoapsis), stop on
  the outcome: `fly_burn(stop_when=[{"metric": "next_periapsis_altitude", "op": "<=", "value": ...}])`
  or fly the burn with `fly_until` and an `approach` throttle law on the orbital element.

## 3. The routine

1. Read the orbit (`orbit_info`), mass and thrust of the burning stage (`vessel_stages`).
2. Compute the maneuver (`compute_maneuver`, `compute_hohmann`, or `compute_node_search` when the
   result depends on patched conics), create it (`node_create`), and read back the predicted orbit
   with `orbit_info`/`node_list`. The prediction is KSP's own; it is the thing to trust.
3. Journal the decision: Δv, burn time, expected orbit, abort condition.
4. `fly_burn` (it warps to the start minus a margin for turning, aligns, burns, feathers, reports the
   applied Δv integrated over game time). Or `mj_execute_node` with explicit settings.
5. Verify: compare the new orbit with the prediction and note the residual. Plan a correction if the
   error matters; find out why before the next big burn if it is large.

Choosing the executor: evidence from earlier flights says MechJeb's node executor drifted ~2°
off-axis on a long burn under game lag (a big out-of-plane error at Duna) and once skipped a node a
few minutes away, while a hand-rolled executor once botched a tiny normal-heavy correction that
MechJeb flew well. Whichever you use, verify the orbit afterwards.

## What goes wrong, and how it shows

| Telemetry | Meaning | Response |
|---|---|---|
| Throttle up but thrust 0 for a few seconds right after a warp | KSP's thrust readout lags after rails warp | confirm with orbit change (SMA, apsides) or g-force before aborting |
| Thrust 0 and nothing changing | still in rails warp, engine not active, starved | stop warp, check engines (`vessel_parts`), stage or fix feed |
| A prograde burn lowers the apoapsis | pointing wrong: misaligned start or control part not along thrust | cut, re-align (check the control point), resume gently; never flip the burn vector |
| Remaining Δv grows during the burn | burning the wrong way | cut immediately |
| Burn ends short, engine out | stage empty: propellant sits in the next stage | stage into the fuelled stage and continue (decide, then act) |
| Periapsis dropping toward the atmosphere during a correction | the correction has a retrograde/radial-in part you did not intend | stop and re-plan with a periapsis constraint |

## Sanity checks (not targets)

- Kerbin 80 km circular ≈ 2,279 m/s, period ≈ 31 min. A 1° plane change there ≈ 40 m/s; 10° ≈ 400 m/s.
- Circularizing from a ballistic arc whose apoapsis is at 80 km with a periapsis deep inside the
  atmosphere typically needs several hundred m/s; if a plan says far more or far less, recheck it.
