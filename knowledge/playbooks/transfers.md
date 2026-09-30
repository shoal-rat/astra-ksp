# Transfers: to a moon, to another planet, and back

Keywords: transfer, moon transfer, tmi, trans munar injection, mun, minmus, encounter, phase angle, window, transfer window, interplanetary, duna, eve, jool, ejection, lambert, porkchop, mid-course correction, correction, free return, return, soi

## 1. Moon transfers (the target orbits the body you orbit)

**Seed analytically.** From a circular parking orbit r1 to the moon's orbit radius r2:
- Δv1 = v(r1, a_t) − √(μ/r1), a_t = (r1 + r2)/2; transfer time t_tr = π√(a_t³/μ).
- The moon must lead by θ = π − n_t·t_tr at the burn, n_t = √(μ/r2³) its mean motion.
- Waiting time until that geometry: t_wait = (θ_now − θ) / (n_ship − n_t), taken modulo the synodic
  period 2π/|n_ship − n_t|.
- `compute_maneuver(kind="hohmann_to_body")` does this from the live state and returns node parameters.

**Refine against KSP's patched conics.** The analytic seed ignores the moon's gravity and the finite
burn. `compute_node_search` places trial nodes, reads the predicted encounter, and removes them with
the game paused. Give it an objective you derived:

- **Target periapsis at the moon.** For a landing: above the highest terrain on the track
  (`compute_terrain`) by a margin sized to your execution error (evidence: one Mun injection lost
  ~19 km of planned periapsis between plan and execution; your own residuals from earlier burns are
  better evidence). For a relay: the orbit the relay wants, high is fine. For a crewed flight: prefer
  candidates whose trajectory after the flyby returns to a survivable parent periapsis (a free
  return), so a failed capture is not fatal.
- **Constraints.** The parent-body periapsis stays safe; no unintended passage through another moon's
  sphere of influence (the planners ignore it, the game does not).
- An encounter merely appearing is not enough: a grazing pass near the edge of the sphere of influence
  makes capture cost almost the full arrival v∞. Stop an injection burn on a capture-grade predicted
  periapsis, not on the first encounter (`fly_burn` with `stop_when` on `next_periapsis_altitude`).

**Returning from a moon.** Escape the moon so that the parent-relative velocity lowers the parent
periapsis into the corridor you want (atmospheric entry corridor, or a parking orbit).
`compute_maneuver(kind="return_from_moon")` seeds it; refine with `compute_node_search` against the
parent periapsis. The escape burn is prograde relative to the moon, timed so that the escape
asymptote points against the moon's own orbital velocity: your parent-relative speed drops and the
parent periapsis falls.

## 2. Interplanetary transfers (sibling planets around the Sun)

**Window.** `compute_transfer_window(origin, target, parking_alt_m, earliest_ut, search_days)` runs a
Lambert porkchop on live ephemerides and returns departure UT, flight time, departure v∞ (vector and
magnitude), and the ejection Δv. The Hohmann phase angle is a seed only: real orbits are eccentric and
inclined, and in live tests it was days off.

**Ejection from the parking orbit.**
- Δv = √(v∞² + 2μ/r) − √(μ/r): burning deep in the gravity well (a low circular parking orbit) is
  the Oberth effect working for you. Eccentric or high parking orbits make the ejection worse and
  constrain where it can happen (a MechJeb transfer planned from a high eccentric orbit came out as a
  retrograde, orbit-lowering node).
- Geometry: the escape hyperbola has e = 1 + r·v∞²/μ, and its outgoing asymptote sits at true anomaly
  ν∞ = arccos(−1/e) from periapsis. The burn point is the v∞ direction rotated back by ν∞ around the
  orbit normal. (Using arccos(+1/e) gave misses of hundreds of sphere-of-influence radii.)
- Two clocks: the heliocentric departure time and the in-orbit burn time differ by up to hours.
  Re-solve the window anchored at the planned node until it moves less than one parking period.
- Keep the ejection in the plane: align before ignition and burn at full throttle. A 2° heliocentric
  inclination error became ~720 Mm out of plane at Duna, fixable only with a very large normal burn.
- Stop on the outcome: while still inside the origin's sphere of influence, read the escape patch
  (`orbit_info` shows the patch chain); after the SOI change, read the heliocentric orbit itself.

**Waiting for a window.** Rails warp is limited by altitude, so waiting in a low orbit can take
hours of real time. Cheaper options: wait on the ground before launch (costs no propellant), or wait
in a high orbit that stays clear of every moon's sphere of influence (a waiting orbit reaching toward
a moon was flung out by a flyby). Raising and later lowering an orbit only to warp faster costs about
twice the raise. Never switch vessels while warping.

**Mid-course correction.** After leaving the origin's sphere of influence:
- Measure the miss honestly: KSP's single-conic closest-approach estimate can be badly wrong near a
  sphere-of-influence edge; the encounter shown in the patch chain (periapsis at the target) is the
  reliable signal once it exists. `target_info` samples the closest approach.
- Search corrections with prograde, normal, **and radial** components (`compute_node_search`); radial
  sets the approach depth cheaply far from the target. Evaluate the sensitivity of the arrival
  periapsis to Δv at a few candidate times and pick the cheapest; evidence showed both "early is
  cheap" and "mid-course has more leverage" depending on geometry, so measure rather than assume.
- MechJeb's course-correction planner only refines an existing encounter; asked to create one from a
  miss it diverged and burned hundreds of m/s.
- After each correction burn, let the patched-conic prediction settle for a few seconds before judging
  it, then verify.

**Budget honestly.** An ideal Hohmann ejection plus capture is the floor. A real crewed Duna transfer
cost ~3.1 km/s (ejection ~1.04, corrections ~0.89, capture and circularization ~1.18) against
~1.7 km/s on paper. Precise ejection and cheap capture choices (see capture, atmospheric-entry for
aerocapture) matter more than bigger tanks.

## 3. Flying a transfer with the tools

1. `orbit_info` (vessel and target), `body_info` (both bodies), `vessel_stages` (can this stage do it,
   and how long is the burn?).
2. Seed, refine, create the node; read the predicted chain; journal the decision with the target
   periapsis and the abort plan.
3. `game_checkpoint` before a large injection or ejection.
4. `fly_burn` with `stop_when` on the outcome metric; verify the patch chain after it.
5. `fly_warp(to="soi", offset_s=...)` toward the sphere-of-influence change; re-check the encounter
   after arrival (the prediction was made with the old patch).

## What goes wrong, and how it shows

| Telemetry | Meaning | Response |
|---|---|---|
| No SOI change predicted right after a burn | predictions not settled yet, or a real miss | wait a few seconds of game time, read again, then correct |
| Encounter periapsis below the surface or inside the atmosphere | aimed too deep | correct early (radial), while it is cheap |
| Encounter periapsis near the SOI radius | grazing pass; capture will cost ~v∞ | correct toward a deeper periapsis now |
| Apoapsis falling during a prograde injection | misaligned start | cut, re-align, resume; guard the parking periapsis |
| Parent periapsis falling into the atmosphere after a correction | correction had an unintended component | stop and re-plan with that constraint |

## Sanity checks (ideal, from an 80 km Kerbin orbit; not targets)

| Destination | Ejection Δv | Phase angle | Flight time | Arrival v∞ |
|---|---|---|---|---|
| Mun | ≈ 856 m/s | ≈ 111° | ≈ 7.4 h | ≈ 365 m/s |
| Minmus (orbit inclined 6°: plane change extra) | ≈ 921 m/s | ≈ 115° | ≈ 54 h | ≈ 228 m/s |
| Eve | ≈ 1,040 m/s | ≈ −54° | ≈ 170 d | ≈ 845 m/s |
| Duna | ≈ 1,070 m/s | ≈ 44° | ≈ 300 d | ≈ 826 m/s |
| Jool | ≈ 1,930 m/s | ≈ 97° | ≈ 1,120 d | ≈ 1,760 m/s |

Days here are Kerbin days of 6 h; a Kerbin year is ≈ 426 of them. Synodic periods: Duna ≈ 910 d, Eve ≈ 680 d,
Jool ≈ 470 d. A Lambert window can differ noticeably from these circular-orbit values.
