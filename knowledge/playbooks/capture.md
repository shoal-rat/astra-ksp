# Capture: turning an encounter into an orbit

Keywords: capture, orbit insertion, soi, sphere of influence, arrival, hyperbolic, v infinity, periapsis, retrograde burn, oberth, loose capture, elliptical capture, overshoot, capture burn

## 1. The physics

On arrival the vessel is on a hyperbola with excess speed v∞ (read it from the patch chain or
compute it from the transfer). At periapsis radius r_p its speed is v_p = √(v∞² + 2μ/r_p).

- Capture into an orbit with apoapsis radius r_a (periapsis r_p):
  Δv = v_p − √(μ(2/r_p − 2/(r_p + r_a))). `compute_maneuver(kind="capture_at_periapsis")`.
- The deeper the periapsis, the cheaper the capture (Oberth): the same Δv changes the orbital
  energy by v·Δv. The floor is set by terrain (`compute_terrain`) or the atmosphere (`body_info`) plus
  your execution margin, unless you mean to aerobrake (see atmospheric-entry).
- A **loose capture** (bound, high apoapsis) costs little more than removing the hyperbolic excess;
  circularizing low costs much more. Decide by what comes next: a relay may want a high orbit anyway;
  a lander wants a low orbit, but can lower the apoapsis later at periapsis; a vehicle that will leave
  again benefits from staying elliptical (its later ejection starts from a fast periapsis). Evidence:
  a loose capture at Eve (apoapsis ~0.3 of the SOI radius) cost ~146 m/s, while circularizing and
  lowering ran another vessel dry.
- Minimize the **total**: correction to set the periapsis + capture + any later orbit shaping. A
  grazing encounter periapsis near the SOI edge makes capture cost almost the full v∞; lowering the
  periapsis early in the approach with a mostly radial burn roughly halved one lander's capture cost.
  `compute_node_search` can score candidates by that total.

## 2. Sensitivity near the end of the burn

At periapsis, from vis-viva, a = 1/(2/r_p − v²/μ) and r_a = 2a − r_p, so

    d(r_a)/dv = 4·a²·v / μ.

Near escape (a very large) the apoapsis is extremely sensitive to the last metres per second: one
lag frame at high throttle took a vessel from escape to a periapsis below the surface. Feather the
throttle as the apoapsis approaches its target, and put a periapsis floor in the stop triggers. Use
the relation to choose `feather_s`: pick it so that the Δv delivered in the last feather interval
changes the apoapsis by much less than your tolerance.

**Which element to drive.** While the orbit is still hyperbolic, kRPC itself reports a *negative*
apoapsis (a < 0, so r_a = a(1 + e) < 0), and time to apoapsis and period are not finite. ASTRA's
telemetry therefore reports the apoapsis as unbounded while e ≥ 1 (null in reports, +∞ inside
triggers and throttle laws): an `apoapsis_altitude <= target` trigger cannot fire before capture, and
an `approach` law on the apoapsis burns at full throttle until the orbit is bound, then feathers
toward the target. Driving the **eccentricity** is the smoother alternative: it falls continuously
through 1 during a retrograde burn. For the orbit you want, e_target = (r_a − r_p)/(r_a + r_p)
(radii from the body's centre), and at periapsis de/dv = 2·r_p·v/μ, which gives the eccentricity
change per m/s for choosing `feather_s`. Alternatively fly two segments: until `eccentricity < 1`
(bound), then an apoapsis approach from the bound orbit.

## 3. Flying the capture

1. Before the SOI change: check the predicted periapsis at the target (`orbit_info` patch chain) and
   correct early if it is too deep or too shallow.
2. After the SOI change: read the real hyperbola, the time to periapsis, and the mass and thrust of
   the burning stage. Compute Δv, burn time, and half-Δv lead with `compute_rocket`.
3. Make sure nothing can stage behind your back during the burn: MechJeb's staging controller has
   fired heat-shield and payload decouplers mid-capture before. `auto_stage` stays off unless you
   decided otherwise.
4. `game_checkpoint`.
5. Warp to the burn start minus turning time (`fly_warp(to="periapsis", offset_s=...)`), then either:
   - create a node from the computed capture and run `fly_burn` with `stop_when` on
     `eccentricity <= e_target` and `periapsis_altitude <= floor`; or
   - `fly_until` with attitude `{"mode": "retrograde", "frame": "orbital"}` (re-pointed every tick,
     so the thrust stays exactly against the velocity and the periapsis is preserved), throttle
     `{"mode": "approach", "metric": "eccentricity", "target": <e_target>, "feather_s": <s>}`, and
     triggers on `eccentricity <= e_target` and the periapsis floor. Never stop a burn that starts on
     a hyperbola on `apoapsis_altitude` (see section 2).
6. Verify the orbit, journal the result against the plan, and adjust the orbit at the next apsis if
   needed.

Burn at periapsis, not at the SOI edge: burning early wastes the Oberth effect and pulls the
periapsis down.

## What goes wrong, and how it shows

| Telemetry | Meaning | Response |
|---|---|---|
| Capture reflex stops right as the orbit becomes bound, with the apoapsis far too high | the throttle feathered too late for the Oberth effect at a deep periapsis (apoapsis falls from +∞ very fast) | drive `eccentricity`, or use a longer `feather_s`; continue from the bound orbit |
| Apoapsis not dropping, throttle up, thrust 0 after warp | thrust readout lag after warp, or warp still on | confirm by SMA change; stop warp; check engines |
| Periapsis dropping while the apoapsis drops | thrust not pure retrograde (fixed attitude, drifting, burning off-periapsis) | track retrograde every tick; if the periapsis nears the floor, cut |
| Apoapsis overshoots far below target, periapsis inside terrain | Oberth overshoot at the end of the burn | feather earlier; if already happened, raise the periapsis at apoapsis right away |
| `part_lost`/`staged` during the capture | an autostager fired | stop; confirm what is left; disable the autostager |
| Eccentricity stays ≥ 1 at burnout | not enough Δv or burn too far from periapsis | plan a second burn at the next periapsis if still inside the SOI, or a correction |

## Sanity checks (ideal; not targets)

- Mun arrival from a Hohmann transfer: v∞ ≈ 365 m/s; capture into a 20 km circular orbit ≈ 310 m/s
  (circular speed there ≈ 544 m/s).
- Minmus: v∞ ≈ 228 m/s; capture into 20 km circular ≈ 160 m/s.
- Duna: v∞ ≈ 826 m/s; propulsive capture to a circle just above the atmosphere ≈ 616 m/s.
- Eve: v∞ ≈ 845 m/s; propulsive capture to a low circle ≈ 1,400 m/s, which is why loose capture or
  aerocapture matters there.
