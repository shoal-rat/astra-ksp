# Rocket design: from requirements to a verified vehicle

Keywords: design, craft, vehicle, rocket, staging, stage, delta-v, dv budget, twr, thrust to weight, parts, engine, tank, booster, upper stage, lander, capsule, heat shield, parachutes, fins, fairing, reaction wheel, electric charge, vab, spec, design_check, dv, delta v, power, battery, solar, rtg, relay, comms, commnet, antenna

The vehicle is sized from the mission, not from habit. Work in this order: what each phase needs,
which stage flies it, which parts deliver it, then check the whole stack and fix what the checks find.

## 1. The Δv budget, phase by phase

Write the mission as a list of phases and give each one a Δv number with the formula or tool that
produced it. Read every body constant live (`body_info`), never from memory.

| Phase | How to compute | Tool |
|---|---|---|
| Ascent from an atmospheric body | orbital speed at the parking altitude − rotation assist + losses (gravity, drag, steering). The loss term is an estimate; take its range, not one number | `compute_ascent_estimate` |
| Ascent from an airless body | v_circ at a terrain-clearing altitude − rotation assist + gravity loss ≈ g·t_burn for the vertical part | `compute_orbit`, `compute_rocket`, `compute_terrain` |
| Orbit changes | vis-viva: v = √(μ(2/r − 1/a)); Hohmann Δv1, Δv2 | `compute_orbit`, `compute_hohmann` |
| Moon transfer / return | Hohmann to the moon's orbit radius; return = escape the moon toward a parent periapsis | `compute_maneuver` (hohmann_to_body, return_from_moon) |
| Interplanetary departure | Δv = √(v∞² + 2μ/r_park) − √(μ/r_park) from the Lambert v∞ | `compute_transfer_window` |
| Capture | Δv = √(v∞² + 2μ/r_p) − √(μ(2/r_p − 1/a_target)) | `compute_maneuver` (capture_at_periapsis) |
| Powered landing on an airless body | orbital speed at the deorbit altitude + gravity loss during braking + hover/terminal time × g | `compute_descent` |
| Landing on an atmospheric body | mostly aerobraking + parachutes; propulsive reserve only for the final metres if chutes cannot slow it enough | `compute_descent`, see atmospheric-entry |

Margins are decisions. Choose one per phase from what can go wrong in that phase and write the
reason in the journal: finite-burn and pointing errors, mid-course corrections, a hover to pick a
landing spot, a capture that has to be redone. Evidence from earlier flights: an interplanetary
transfer that was ~1.7 km/s on paper cost ~3.1 km/s in practice (corrections plus capture), and a Mun
encounter periapsis lost ~19 km to execution error. Use evidence like that to size margins, not a
fixed percentage.

## 2. Stage math

- Rocket equation: Δv = Isp·g0·ln(m0/m1), g0 = 9.80665 m/s². Mass ratio needed: R = e^(Δv/(Isp·g0)).
- Tanks needed for a stage: with M the mass above plus the engines, tank wet mass w and dry mass d,
  the number of tanks is ⌈M(R − 1)/(w − R·d)⌉. If the denominator is ≤ 0 the stage cannot reach the
  Δv with those tanks at any size: split the phase over more stages or change the engine.
- Single-stage ceiling: Δv_max ≈ Isp·g0·ln(1/ε), ε = dry/wet mass of the tankage. A phase that asks
  for most of this ceiling needs another stage. For identical Isp and ε, equal Δv per stage is best.
- Isp and thrust depend on ambient pressure. Evaluate each stage where it will actually burn:
  sea-level values for the first stage on its launch body, vacuum values above the atmosphere. At
  constant fuel flow, thrust(p) = thrust_vac · Isp(p)/Isp_vac. Vacuum engines keep only a fraction of
  their thrust in thick air; a dense atmosphere (read its pressure with `body_info`) punishes them.
- Burn time: t = (m0·ve/F)(1 − e^(−Δv/ve)), ve = Isp·g0. `compute_rocket` does both directions.
- `compute_calc` evaluates anything else, e.g. `{"expression": "ceil(M*(R-1)/(w-R*d))", "variables": {...}}`.

## 3. Thrust-to-weight per phase

TWR = F / (m·g_body), with g_body = μ/r² for the body the stage works on. What each phase needs:

- **Atmospheric liftoff.** TWR must exceed 1 by enough to accelerate: every second spent near
  vertical costs ≈ g·Δt of gravity loss, so low TWR bleeds Δv; very high TWR drives dynamic pressure
  and heating up early. Compute the full stack's liftoff TWR with sea-level thrust and the real total
  mass, including every accessory part. A craft estimated at TWR 1.24 never left the pad because
  ~0.5–0.8 t of accessories were missing from the estimate; the pad reading (`vessel_stages`) is the
  number that counts.
- **Vacuum burns.** TWR matters through burn time. Compare the burn time with the time the maneuver
  point is "good": a burn much longer than a small fraction of the orbital period spreads over the arc
  (cosine and gravity losses), and a long ejection or capture burn with a very weak engine can run the
  stage dry before the maneuver is done (evidence: a ~0.2 TWR upper stage crawled through an ejection
  and stranded the mission).
- **Powered landing.** Need a_max = F/m well above local g for the braking burn: the stopping
  distance is v²/(2(f·a_max − g)) with f the fraction of thrust you plan to use. The lower the excess
  acceleration, the longer and more gravity-lossy the braking. In micro-gravity, the opposite problem:
  a_max ≫ g makes fine control hard; limit thrust per engine (`control_part` thrust_limit).
- **Surface ascent (return).** Same as liftoff but with local g and no drag on airless bodies.

## 4. Physical checklist (each item has cost a mission)

- **Control source without crew.** A probe core with electric charge, even on crewed craft. A crewed
  pod alone gives no control if the crew is not seated.
- **Power for the whole mission.** Longest shadow or coast × consumption (reaction wheels, probe
  core, antennas). Solar flux falls with (r_home/r)² around the Sun; deep-space probes need a big
  battery or RTGs. A probe with no charge cannot point (control state none, 180° pointing error).
- **Communications (probes, relays).** CommNet link range between two antennas is √(P_A·P_B) of their
  power ratings (the ground station counts as one antenna, its power set by the tracking station
  level). Compare it with the longest distance the link must span (e.g. the two planets at
  conjunction: sum of their orbit radii) and remember the Sun blocks the line regardless of power.
  Take antenna power from the part catalog (`part_info`) when it lists it; a relay ring wants
  near-circular, evenly spaced orbits.
- **Attitude authority.** Angular acceleration α = torque/moment of inertia; the time to turn θ from
  rest is about 2√(θ/α). Heavy upper stacks need inline reaction wheels sized to that; three inline
  wheels turned an unreliable heavy relay stack into a reliable one. Engine gimbal helps only while
  the engine burns.
- **Aerodynamic stability on ascent.** Centre of pressure behind centre of mass: passive fins low on
  the first stage; no active control surfaces driven by a weak probe autopilot (they over-rotated and
  tumbled a stack). A blunt payload (pod + heat shield) raises drag a lot: in one case the ascent drag
  loss went from ~290 to ~430 m/s and the vehicle fell back; put it under a fairing and jettison in
  vacuum. Very long thin stacks are hard to fly; radial boosters shorten the core.
- **Fuel topology.** Check each engine's propellants against the tanks that feed it (a LiquidFuel-only
  nuclear engine on LFO tanks leaves ~55 % of the load as dead oxidizer). Radial decouplers do not
  crossfeed by default: strap-on pods burn their own fuel in parallel and an engine-less drop tank
  feeds nothing unless crossfeed or fuel lines are set. Radial pods must be children of their
  decoupler or firing it drops only the decoupler.
- **Staging.** A stack decoupler shares the stage of the engines it exposes (it fires as those engines
  light). A decoupler in the same stage as the engines below it splits the craft on the pad. Keep
  payload, heat-shield, fairing, and parachute events in separate stages so one stage activation
  cannot fire several of them at once. Review the stage table `design_check` returns line by line.
- **Engine exhaust.** A lander engine enclosed by its own interstage shroud (same vessel) produces
  zero net thrust (g-force 0 at full throttle). Keep kept engines' bells bare.
- **Landers.** Legs wide relative to the centre-of-mass height (tip-over angle = atan(half leg span /
  CoM height); a tall narrow lander tipped over on Duna and buried its hatch). The descent engine must
  be the active stage after the transfer stage is dropped.
- **Return through an atmosphere.** Heat shield when the entry speed is orbital or faster (a craft
  without one broke up at ~3 km/s). Parachute count from the landing-site density (see
  atmospheric-entry). The capsule that reenters should be short: pod, shield, chutes.
- **Docking.** A port on each vessel, RCS with enough monopropellant for the final approach.
- **EVA.** Keep the hatch clear: a heat shield right below a Mk1 pod or radial parts over the hatch
  make EVA fail ("no free hatch"). Test EVA on the pad if it matters.

## 5. Working the design tools

1. `parts_search` by role, diameter, propellant, minimum thrust; `part_info` for attach nodes,
   masses, engine modes and propellants. Key on internal names; titles are localized.
2. Write the spec (part tree, docs/ARCHITECTURE.md). `design_check` returns masses, per-stage Δv (vac
   and at the pressure you choose), TWR on the body you choose, burn times, height, CoM, and warnings.
3. Iterate until every phase meets its Δv and TWR with your margin. Tank sizes come in coarse steps:
   diff the resulting design after every change; a requirement change can be a no-op.
4. `design_build` writes the craft into the active save; `game_launch` puts it on the pad with the
   crew you name. **Verify on the pad**: `vessel_stages` against the design (masses, Δv, TWR, stage
   order), `vessel_parts` for the staging of decouplers and chutes, `camera_look` to see it. The
   vehicle you fly must be the vehicle you analysed.
5. `booster-engineer` (subagent) can run this loop for you; check its stage table yourself.

## What goes wrong, and how it shows

| Telemetry | Likely design cause |
|---|---|
| Apoapsis stays near the pad altitude, vertical speed ~0 at full throttle | liftoff TWR ≤ 1 (mass underestimated or sea-level thrust overestimated) |
| Angle of attack grows at high dynamic pressure, then part loss | aerodynamically unstable stack, active fins, exposed blunt payload |
| Upper stage burn far longer than planned, apoapsis crawling | stage TWR too low for the job |
| Thrust 0 at full throttle with fuel on board | fuel topology: tank not connected, crossfeed off, wrong propellant |
| Thrust > 0 but g-force ~0 | exhaust blocked by a part of the same vessel |
| Engine flames out at about half the computed Δv | engine/tank propellant mismatch |
| Craft splits at ignition | decoupler staged with the engines below it |
| Pointing error never converges, electric charge falling | too little torque or power |

## Sanity checks (not targets; recompute for your case)

- Kerbin: circular speed at 80 km ≈ 2,279 m/s; equatorial rotation ≈ 175 m/s; a stock rocket to a low
  orbit typically spends ~3.2–3.5 km/s. kRPC reports sea-level density ≈ 1.14 kg/m³.
- Ideal Hohmann from an 80 km Kerbin orbit: Mun ≈ 856 m/s, Minmus ≈ 921 m/s (plane change extra);
  capture into a 20 km Mun circle ≈ 310 m/s; Minmus ≈ 160 m/s.
- Ideal ejections from an 80 km Kerbin orbit: Duna ≈ 1,070 m/s, Eve ≈ 1,040 m/s, Jool ≈ 1,930 m/s.
- If a design's total Δv comes out several times these ranges, or its liftoff TWR below 1 or above
  ~3, re-check the inputs before believing it.
