# Artemis-Style Mun Engineering Notebook

Updated: 2026-06-20

## Mission Target

Replicate the useful engineering structure of an Artemis lunar landing inside KSP1:

1. SLS/Orion analogue launches crew from Kerbin.
2. Transfer stack performs trans-Mun injection.
3. Orion analogue remains the Kerbin-return system.
4. HLS analogue performs Mun descent, landing, ascent, rendezvous-equivalent return, and Kerbin recovery.
5. A separate relay launch places a high Mun communications satellite before crew operations.
6. Stable crewed surface science is recorded before HLS ascent.

Current NASA source note: Artemis III is listed by NASA as a 2027 crewed demonstration in low Earth orbit to test commercial landers. NASA's Artemis overview lists Artemis IV as the first Artemis lunar landing target in early 2028. For KSP, the controller should model the landing architecture, not depend on the Artemis III mission number.

## Source Notes

- NASA Artemis overview: Orion carries/sustains crew and returns them safely to Earth; SLS launches Orion; commercial landers carry crew from lunar orbit to the surface and back; Artemis IV is currently the first Artemis lunar landing target. Source: https://www.nasa.gov/humans-in-space/artemis/
- NASA Artemis III page: Artemis III is currently described as a 2027 crewed demonstration to test critical systems and one or both HLS landers in low Earth orbit. Source: https://www.nasa.gov/mission/artemis-iii/
- NASA SLS overview: SLS is the high-energy launch vehicle for Orion, astronauts, and cargo toward the Moon. Source: https://www.nasa.gov/humans-in-space/space-launch-system/
- NASA Orion reference guide: Orion has a crew module, service module, and launch abort system; the service module supplies propulsion and critical supplies. Source: https://www.nasa.gov/reference/orion-spacecraft/
- NASA HLS reference: the lander is launched uncrewed to lunar orbit to wait for Orion; two crew transfer from Orion to the lander, descend, then return to lunar orbit and head home in Orion. Source: https://www.nasa.gov/reference/human-landing-systems-2/
- NASA/SpaceX HLS sequence: Starship HLS docks directly with Orion for early missions; two astronauts transfer to HLS while two remain in Orion, then HLS returns them to Orion in lunar orbit. Source: https://www.nasa.gov/directorates/esdmd/artemis-campaign-development-division/human-landing-system-program/nasa-spacex-illustrate-key-moments-of-artemis-lunar-lander-mission/
- NASA Artemis II trajectory visualization: Orion follows a loop around the Moon and returns on a free-return trajectory using Earth/Moon gravity. Source: https://svs.gsfc.nasa.gov/5610/
- NASA Artemis II mission profile: Orion performs multiple maneuvers, orbits Earth twice, raises to a high Earth orbit, then enters a lunar free-return trajectory. Source: https://www.nasa.gov/missions/artemis/nasas-first-flight-with-crew-important-step-on-long-term-return-to-the-moon-missions-to-mars/
- NASA SLS Block 1B reference: SLS/Orion design emphasizes high departure energy, TLI, stable checkout/abort windows, and later Block 1B circular-orbit flexible TLI timing. Source: https://www.nasa.gov/reference/sls-space-launch-system-block-1b/
- NASA HLS update paper: Starship HLS must support Orion docking, lunar descent, surface support, ascent, rendezvous/docking with Orion, and LEO cryogenic refueling as an enabling operation. Source: https://ntrs.nasa.gov/api/citations/20240012719/downloads/HLS%20Update%20Kent%20Chojnacki%20IEEE%20Aero%202025%20v2.pdf
- Public Moonship craft source: Matt Lowne video description linked a public `MUNSHIP.craft` download, now stored locally as `work/downloads/mega/MUNSHIP.craft` and used as the first live Starship/Moonship HLS analogue source. Source: https://www.youtube.com/watch?v=OJCCDIBmrBI
- ESA Artemis III page is retained only as historical/architecture context because ESA notes the page will be updated; it describes the older Orion + Starship HLS + NRHO surface mission architecture. Source: https://www.esa.int/Science_Exploration/Human_and_Robotic_Exploration/Orion/Artemis_III

## Split Artemis Architecture For KSP1

The lab now treats an Artemis request as a two-vehicle architecture instead of a single all-in-one Mun ship.

| Vehicle | NASA role | KSP role | Required success gate |
| --- | --- | --- | --- |
| Mun relay satellite | Communications support | Launch first as a separate cargo mission into a high stable Mun relay orbit | `artemis_mun_relay_deployed` |
| Starship HLS analogue | Predeployed lunar lander | Launch after relay, park in low Mun orbit, perform descent/ascent only | `artemis_hls_returned_to_mun_orbit` |
| SLS/Orion analogue | Crew launch and Earth return | Launch after HLS is parked, capture in Mun orbit, return to Kerbin with heat shield/parachute | `recovered` |

Current live-control limitation: docking and crew transfer are not automated yet. Until that is added, the controller treats crew transfer as a rendezvous-equivalent phase: both vehicles must be alive and in Mun orbit before HLS surface sortie and Orion return are allowed to score.

Important controller convention: `target_orbit_m` remains the Kerbin parking orbit target for launch guidance. Artemis phase launches must keep this at 80 km or higher; the desired low Mun parking orbit is a separate mission-phase target, not the ascent target.

Mun relay constraint: a true Mun-stationary orbit is physically outside the Mun sphere of influence in stock KSP. Using Mun sidereal period of about 138,984 s and Mun GM about 6.51384e10 m^3/s^2 gives stationary radius about 3.17 Mm from Mun center, or about 2.97 Mm altitude. The Mun SOI is only about 2.43 Mm, so the project uses a stable high relay orbit instead, currently targeting roughly 200 km x 1,000 km.

Current live HLS craft source order:

1. `work/downloads/mega/MUNSHIP.craft`, a public Starship/Moonship craft from Matt Lowne's video description. This loaded and launched through the bridge as `AI-HLS-Starship-MUNSHIP-01`.
2. `Ships/VAB/Kerbal Landing System.craft` from Artemis Construction Kit. This is known-loadable but is a low-thrust lander-only fallback, not a full Starship/Super Heavy predeployment stack.
3. Experimental HLS Project generation only if no complete craft source exists. Do not prefer this for live trials until generation moves into KSP or includes complete persisted module/editor state.

Current craft-generation limitation: the metadata design for Orion/SLS is generated, but the live craft file is template-seeded from a known-launchable craft until the generated craft writer has complete KSP staging metadata for heavy stacks.

## Player Comment Integrated

User-supplied player guidance:

- RSS/RO/RP-1 has more realistic Orion/SLS component sets and can recreate Artemis with higher fidelity.
- It also raises the debugging cost sharply because the physics, inclination targeting, engines, and operations become less forgiving.
- Artemis-style operations should not be modeled as launch, one parking orbit, then immediate TMI only. Real profiles use several Earth orbits, orbit changes, and carefully timed departure.
- Orion-class lunar trajectories should prefer a free-return path before committing to lunar capture.

Decision for this stock-KSP branch: keep RSS/RO/RP-1 as a future separate mod profile, not an in-place change to the current save. Add the free-return idea to Orion transfer scoring in stock KSP: when KSP patched conics expose a post-Mun Kerbin return orbit, prefer candidates whose Kerbin periapsis is in a recoverable band.

NASA validation of the player comment: Artemis II does not immediately circularize once and burn for the Moon. NASA describes two Earth orbits, multiple maneuvers, a high-Earth orbit, and a lunar free-return path. In KSP terms, the Orion/SLS controller should allow several Kerbin parking/phasing revolutions, score TMI candidates for free-return safety, and trigger finite burns from calculated lead times rather than a hard-coded "burn now" rule.

## RSS/RO/RP-1 Branch State

Created a separate clean KSP 1.12.5 copy at:

```text
work/KSP_RSSRO
```

Installed through CKAN instance `KSP_RSSRO`:

- RP-1 Express v2.0
- RP-1 v4.4.0.0
- Realism Overhaul v18.0.0.0
- Real Solar System v20.1.3.0
- ROEngines, ROCapsules, ROTanks, ROHeatshields, ROSolar
- MechJeb2, kOS, Lunar Transfer Planner, Transfer Window Planner
- kRPC v0.5.4
- Low graphics RSS textures

Automation bridge copied into the modded GameData. The modded profile config is `configs/rssro-ksp.yaml`.

Do not run the stock Kerbin/Mun flight controller against this profile. Required next work for RSS/RO/RP-1:

- Read Earth and Moon constants from kRPC after first modded launch.
- Replace Kerbin parking orbit, Mun transfer, and Mun capture constants with Earth/Moon equivalents.
- Account for launch site latitude and target inclination.
- Generate RO procedural tanks/engines instead of stock stack craft.
- Handle RO ignition limits, ullage, throttleability, pressure-fed engines, and boiloff.
- Prefer free-return trajectories for Orion before lunar capture.

## KSP1 Body Constants From kRPC

These values were read from the active KSP game through kRPC, not copied from memory.

| Body | Radius m | mu m^3/s^2 | Surface g m/s^2 | SOI m | Atmosphere |
| --- | ---: | ---: | ---: | ---: | --- |
| Kerbin | 600000.0 | 3531599999999.9995 | 9.81335114437652 | 84159286.47963049 | yes, 70000 m |
| Mun | 200000.0 | 65138397520.78069 | 1.629016227964847 | 2429559.1165647474 | no |

## Equations Used By The Controller

Circular speed:

```text
v_circ = sqrt(mu / r)
```

Vis-viva speed:

```text
v = sqrt(mu * (2 / r - 1 / a))
```

Finite burn duration, first order:

```text
t_burn = mass * delta_v / thrust
trigger_UT = node_UT - t_burn / 2 - settle_time - command_delay
```

Powered landing trigger:

```text
a_net = thrust / mass - local_gravity
d_stop = speed^2 / (2 * a_net) + speed * (command_delay + settle_time) + margin
```

Vertical landing throttle:

```text
throttle = (local_gravity + max(0, target_vertical_speed - current_vertical_speed) / response_time) / max_accel
```

Kerbin-Mun transfer seed:

```text
a_transfer = (r_parking + r_mun) / 2
dv_tmi = sqrt(mu * (2 / r_parking - 1 / a_transfer)) - sqrt(mu / r_parking)
t_transfer = pi * sqrt(a_transfer^3 / mu)
phase_target = pi - sqrt(mu / r_mun^3) * t_transfer
```

The controller now stores these in `src/ksp_lab/guidance.py` and has unit tests in `tests/test_guidance.py`.

## Current Trial Lessons

Moonship craft source test: `AI-HLS-Starship-MUNSHIP-01`.

- Source craft `MUNSHIP.craft` has 238 parts and a Starship/Super Heavy/Moonship shape, including 15 Vector engines, 3 Wolfhound/AJ10-like engines, large tanks, landing legs, ladders, fairings, and crew aboard.
- It loaded in the VAB and launched to FLIGHT/PRELAUNCH through the bridge without the `ShipConstruct.LoadShip` crash that affected the hand-written HLS Project craft.
- kRPC aggregate vessel resources were misleading for this craft because many tanks are in unstaged/decoupled groups. Per-part resources confirmed the tanks are fueled; use per-part or stage-aware resource inspection before scoring fuel.
- The craft starts with engines inactive and custom staging. Do not run a full ascent blindly; first validate staged engine activation, launch clamp release, and TWR after staging.
- The existing ACK `Kerbal Landing System.craft` loaded and launched, but live inspection showed it is a low-thrust lander-only craft. Keep it as a fallback/reference, not the primary predeployment stack.

Craft serialization error to avoid:

- KSP part identifiers in craft files can differ from config filenames; for HLS Project parts, valid craft IDs were `HLS.NOSE.CONE`, `HLS.MAIN.TANK`, and `draco.hls`, not the raw underscore names.
- Even with correct part IDs, minimal hand-written PART blocks can omit persisted module/editor state. That caused load/analytics instability. Prefer copying a KSP-authored craft file or generating through a KSP-side plugin API.
- Stale `AI-*` PRELAUNCH vessels block new launches with `LaunchSiteClear` failures. Recover old recoverable AI vessels before every bridge launch.
- PowerShell output can display the Chinese save folder name as mojibake, but Python decoded `默认` correctly from UTF-8 YAML. When embedding the name directly in PowerShell here-docs, use `\u9ed8\u8ba4`.

Latest data run: `trial-0001-529717a0`.

- TMI planner selected a non-impact Mun periapsis of 42067.8 m with 790 m/s prograde, which fixed the previous direct-impact transfer bug.
- The simple capture rule stopped immediately after weak capture at about 1.88 Mm Mun apoapsis. That created too much descent energy and horizontal velocity.
- New rule for the next run: do not accept weak capture unless fuel is dangerously low. Continue the retrograde capture burn until Mun apoapsis is roughly 20-320 km and periapsis is above the surface.
- Landing must not hover. If vertical speed is safer than target and altitude is still above final-contact height, throttle must go to zero and let the lander settle.
- Powered ascent and low-altitude landing control should stay at physics warp 0. Coasts and long waits may use 4x/rails warp.

Follow-up data run: `trial-0001-f1ecae56`.

- Capture tightened apoapsis correctly, but accepted a periapsis near 0.6 km.
- That is unsafe because Mun terrain is not a perfect radius. "Periapsis above zero" is not a landing-safe orbit.
- New rule: transfer search rejects planned Mun periapsis below 12 km, and capture rejects actual periapsis below 8 km once apoapsis is low.
- Transfer search now includes radial correction as well as prograde timing.

Planner data run: `trial-0001-2d5bcf6f`.

- Orbit insertion succeeded again: about 70.5 km periapsis and 158 km apoapsis.
- The broad live kRPC brute-force TMI search was too slow. It spent more than 50 seconds after the first planning row while the game clock kept running.
- New rule: seed TMI from Hohmann delta-v and the current Mun phase angle, then run only a small KSP patched-conic refinement grid. Use the broad sweep only as a fallback, not as the primary planner.

Capture data run: `trial-0001-f728d8a5`.

- Calculation-seeded TMI worked: selected node was about 799 m/s prograde plus 80 m/s radial, with predicted Mun periapsis about 33.7 km.
- Actual Mun SOI periapsis was about 14.4 km, so finite burn execution and patched-conic drift cost roughly 19 km of clearance.
- The capture burn used a fixed 180 s lead for low periapsis, which was too early. Burning retrograde far before periapsis drove the periapsis down to about 8.0 km while apoapsis was still about 477 km.
- New rule: aim TMI at about 60 km Mun periapsis, reject planned periapsis below 20 km, and start capture around half the estimated burn time plus command margin instead of using a fixed 180 s lead.

Transfer execution data run: `trial-0001-a01108f1`.

- Higher target worked in the planner: selected Mun periapsis about 72.3 km.
- The finite TMI burn still missed the encounter; final Kerbin apoapsis was about 11.38 Mm and `time_to_soi_change` was unavailable.
- Live validation showed `time_to_soi_change` can appear after a short patched-conic settle delay. New rule: after TMI, do not immediately fail when the maneuver-node prediction and actual patched conics disagree. Recheck for a few seconds; if the vessel is in a high Kerbin transfer orbit and SOI is still absent, point prograde and perform a controlled top-off burn until Mun SOI appears or an apoapsis safety cap is reached.

Terminal landing data run: `trial-0001-114ce9b2`.

- Launch, TMI, SOI entry, Mun capture, and deorbit all worked.
- Mun capture ended with about 319 km apoapsis, 57.8 km periapsis, and about 49% fuel. Descent reduced horizontal speed nearly to zero, but final hover logic let lateral speed rise again near 50 m altitude.
- Touchdown vertical speed was safe, about 1.6 m/s, but total touchdown speed was about 12.4 m/s, so the landing was correctly rejected as hard.
- New rule: below about 80 m, keep retrograde attitude until horizontal speed is under 3-5 m/s. Do not limit/cut throttle near the ground unless horizontal speed is already safe. If lateral speed remains above 4-6 m/s below 35-80 m, hold/slow descent and keep translating instead of accepting contact.

Terminal hover data run: `trial-0001-83933bcd`.

- Launch, transfer, SOI entry, capture, and deorbit all worked again. Capture reached about 319 km apoapsis and 50 km periapsis with about 48% fuel.
- The terminal patch cleaned up lateral speed near 80 m, but below 50 m the old suicide-burn throttle clamps still forced 0.92 throttle because the stopping-distance helper includes a fixed 30 m safety margin.
- That produced vertical bounce, reintroduced lateral speed, wasted the remaining descent/ascent fuel, and ended in a hard Mun contact at about 40 m/s after the active stage ran dry.
- New rule: below about 120 m, bypass the suicide-burn max-throttle clamps. Use a capped terminal hover controller with small proportional throttle, commit to descending at about 3, 2, 1.2, then 0.7 m/s, and only allow strong throttle again if vertical speed is actually dangerous.

Terminal lateral-cleanup data run: `trial-0001-7a3621a1`.

- Launch, transfer, Mun SOI entry, capture, and deorbit worked again.
- The lander reached about 120 m with roughly 24% fuel and about 8.1 m/s horizontal speed, then eased vertical descent successfully.
- Near 15 m altitude the controller briefly reduced horizontal speed below 7 m/s, but it switched to pure vertical hold too early when horizontal speed dipped under 8 m/s.
- That preserved or amplified sideways motion in the final seconds; contact was still unsafe and the vessel lost thrust/parts at the surface.
- New rule: do not switch to pure vertical attitude just because total speed is near the touchdown limit. Stay retrograde until horizontal speed is below about 3.5 m/s during approach, or below about 5.5 m/s in the last 25 m with a calm vertical rate. When lateral speed is still high below 80 m, slow the descent target so retrograde thrust has time to translate the vehicle before touchdown.

Successful Mun landing / failed ascent handoff run: `trial-0001-1575536f`.

- The patched landing controller worked: touchdown on the Mun was accepted at about 6.12 m/s with about 17% fuel remaining and the engine still available.
- The next failure moved to the return-ascent handoff. The ascent routine immediately retracted landing legs and applied throttle while the vessel was still settling at about 2 m surface altitude.
- Surface speed jumped from about 6 m/s to more than 20 m/s in under a second, then the vessel lost thrust/parts and the trial failed as `mun_return_ascent_out_of_fuel`.
- New rule: after Mun touchdown, hold throttle at zero for a short settle period, keep landing legs deployed through liftoff, hold vertical pitch until clear of terrain, and only retract legs above a safe positive altitude.

Second successful Mun landing / unstable return liftoff run: `trial-0001-929019f3`.

- Landing was again accepted, this time at about 5.91 m/s touchdown with about 19% fuel remaining.
- The settle period worked: surface speed dropped below 1 m/s before return throttle-up.
- Full-throttle liftoff still created large surface-relative speed while the craft was effectively at terrain clearance, and the vessel lost thrust/parts before climbing away.
- New rule: a landing that is safe by KSP contact tolerance is not necessarily stable enough for relaunch. Terminal landing must reduce horizontal speed close to zero before touchdown, not merely below 6-8 m/s. Return liftoff should use partial throttle until the craft has positive terrain clearance, then increase power.

Third Mun landing / tilted touchdown run: `trial-0001-1c866f52`.

- Terminal settings improved the settle speed after touchdown, but the vehicle still contacted while the body was effectively in a retrograde/tilted landing attitude.
- At about 9 m altitude it still had roughly 8 m/s horizontal speed, so the final contact could be survivable but not upright enough for a reusable ascent.
- Partial-throttle ascent then produced ground sliding instead of clean vertical liftoff and destroyed/lost the active stage.
- New rule: terminal guidance must perform a deliberate lateral-kill burn before touchdown. Start terminal control higher, use a throttle floor when low-altitude horizontal speed remains high, and wait for calm settle before return ascent.

Overcorrected terminal-hover run: `trial-0001-424621e6`.

- Starting terminal landing control at 250 m made the lander hover/oscillate between roughly 150-230 m and spend the fuel that should have been reserved for touchdown and return.
- Fixed low-altitude throttle floors then kept pushing while the vehicle was already close to the ground, increasing lateral speed to about 18 m/s and ending in `mun_landing_out_of_fuel`.
- New rule: do not enter the capped terminal hover controller at 250 m. Use the normal descent controller until about 120 m, then use only modest lateral-kill floors so final control does not overshoot sideways.

Near-stable touchdown / engine-loss run: `trial-0001-5ab82fdb`.

- The corrected descent avoided the high-altitude hover and reached the surface with about 24% fuel remaining, but touchdown still had about 2.0 m/s horizontal motion.
- KSP later left only the crew pod as the active landed vessel, so a 2 m/s sideways touchdown is still too destructive for a return mission even if the total contact speed is low.
- New rule: below about 80 m, do not merely slow descent when horizontal speed remains above 1-2 m/s. Hold vertical rate near zero and keep a modest retrograde throttle floor until lateral motion is nearly gone; do not cut throttle below 8 m unless horizontal speed is already under about 1 m/s.

Transfer overburn run: `trial-0001-57d38b12`.

- The selected TMI node predicted a Mun encounter but with only about 23.8 km planned Mun periapsis. Finite-burn execution error erased that margin, and the actual burn continued to a roughly 31.7 Mm Kerbin apoapsis with no Mun SOI.
- New rule: planned Mun periapsis below 45 km is not safe enough for this craft/controller. Reject those nodes, and during TMI stop burning once a real Mun SOI appears or once apoapsis exceeds the top-off band without an SOI.

Premature SOI-stop run: `trial-0001-b5648002`.

- The stricter planner selected a good nominal TMI node, about 60.0 km planned Mun periapsis, but the new burn guard stopped as soon as any Mun SOI appeared.
- That SOI was a grazing pass with about 2.1 Mm Mun periapsis. Capture from that far out required too much propellant and the craft ran dry during `mun_capture_burn`.
- New rule: a detected Mun SOI is not enough to stop TMI. Stop early only when the live predicted Mun periapsis is capture-grade, roughly 35-180 km; otherwise continue toward the node residual unless the no-SOI apoapsis cap is reached.

Late lateral-kill run: `trial-0001-69c5ed51`.

- Transfer and capture worked, but the descent reached about 200 m terrain altitude with roughly 7.5 m/s horizontal speed and only about 24% fuel.
- Waiting until 120 m to enter terminal lateral-kill mode was too late. The vehicle hit at about 18.8 m/s total speed and 17.5 m/s horizontal speed.
- New rule: when horizontal speed is still above 3-5 m/s, enter the capped terminal controller by about 500 m, use a shallow descent target, and keep a throttle floor. The PT Munsplorer template also needs more lander reserve for hover-and-kill terminal guidance plus return.

## Trigger Rules To Keep

- Artemis architecture: do not treat the mission as one immediate circular-orbit-to-Mun transfer. Predeploy the HLS/Starship analogue first, then launch the SLS/Orion analogue after the lander is parked and verified.
- Artemis Orion transfer: prefer a free-return trajectory when possible. Score the node by the post-Mun Kerbin periapsis as well as the Mun encounter periapsis, because a crewed vehicle must preserve a passive abort path.
- Artemis parking phase: allow several Kerbin parking-orbit revolutions and small phasing/plane/energy corrections before committing to TMI. This matches the real Artemis flow better than a single rushed burn.
- Artemis checkout/free-return note from user-supplied gamer comment: do not model Artemis as "circularize and immediately transfer." Keep a stable parking/check-out phase, then target a free-return or near-free-return path for crewed Orion/SLS. The controller should solve shutdown timing from node residual/orbital energy instead of fixed burn duration, matching the historical problem of early direct-ascent lunar probes.
- Artemis finite burns: use energy/vis-viva calculations to seed delta-v and trigger UT, then refine with KSP patched-conic nodes. Do not use a fixed shutdown time when thrust, mass, or remaining node delta-v are available.
- HLS craft source: prefer the verified downloaded Moonship craft, then ACK KLS as fallback. Do not return to hand-written minimal HLS Project craft files for live trials until serialization is complete.
- RSS/RO/RP-1 branch: expect much stricter launch azimuth, inclination, ullage, ignition, and finite-burn timing constraints. Do not run the stock Kerbin/Mun controller against RSS/RO without a dedicated Earth/Moon guidance profile.
- Launch and gravity turn: no physics warp during powered atmospheric ascent.
- Circularization: trigger burn around apoapsis using finite-burn lead time.
- TMI: search a window of future nodes and score by actual Mun encounter periapsis, targeting about 60 km and heavily penalizing low periapsis.
- TMI search must be calculation-seeded first; do not create thousands of live maneuver nodes while KSP time advances.
- Post-TMI: verify `time_to_soi_change`. If no Mun SOI appears but apoapsis is near Mun distance, use a small prograde top-off correction before declaring a miss.
- Mun capture: estimate periapsis arrival speed with vis-viva, estimate required retrograde delta-v, and trigger near half burn time plus delay. Do not use a fixed multi-minute lead for low periapsis.
- Mun landing: deorbit only from a tight capture orbit; use surface-speed suicide-burn distance to start the main braking phase; below final approach, control vertical speed and stop trying to null every horizontal meter per second.
- Terminal Mun landing: final touchdown is allowed only after horizontal speed is safely low; below 80 m, lateral cleanup has priority over immediate contact.
- Template craft: for the PT Munsplorer live template, boost lander propellant reserve so stable touchdown and return testing are not starved by a single hover cycle.
- Return: launch east from Mun, circularize low Mun orbit, plan a Kerbin-return node, enter Kerbin atmosphere, deploy parachutes below safe altitude, and record recovery.
- Relay: launch a separate cargo/relay stack and shape the final Mun relay orbit with calculated apsis-change burns. Do not chase a true Mun-stationary orbit because it is outside the Mun SOI.
- Science: after stable HLS touchdown, run available science modules when present; otherwise record the crewed surface-sample phase as modeled science until EVA automation is implemented.

## Launch Blocker: Craft Has No Controllable Command Module (player-confirmed)

Date: 2026-06-21. This is the live regression that broke every recent run before any flight could happen.

- Symptom A (newest run `artemis-0001-4640f128`): bridge `/launch` returns ok, but the flight scene never appears, so the runner fails with `TimeoutError: Timed out waiting for bridge state loadedSceneIsFlight=True`.
- Symptom B (runs `d03abb84`, `77b98add`, `fd7d937e`, `16c7064f`, `818674e0`): bridge `/launch` returns `HTTP 400` with an `Object reference not set` inner exception from `EditorLogic.launchVessel()`.
- Root cause (confirmed by the player watching the KSP screen): the craft launched with **no controllable command module**. KSP raises its "this vessel has no available crew or probe core to control it" launch prompt, which a headless automation launch cannot click. The modal either blocks the scene transition (Symptom A) or trips the reflective `launchVessel()` invoke (Symptom B).
- Why it happens here: the relay (`crewed = false`, an uncrewed satellite) is template-seeded from `PT Series Munsplorer.craft`, which carries only a crewed `mk1pod.v2` and **no probe core**. Headless `EditorLogic.launchVessel()` does not run the editor crew-assignment dialog, so the pod comes up empty and the vessel has no control source. An uncrewed relay must be probe-controlled regardless.
- Fix rule: **every generated/seeded craft must carry an uncrewed probe-core control source (with electric charge) so it is always controllable at a headless launch, independent of crew assignment.** Crewed analogues keep their pod but must also have a probe core (or guaranteed crew) so an empty-pod launch still controls. Never rely on KSP auto-crewing during bridge launches.
- Important nuance found while fixing this: a pure generated stack (`craft_writer.render()`) DOES include `probeCoreOcto.v2`, but its minimal hand-written PART blocks omit MODULE/editor state, so launch instead NullReferences inside `EditorLogic.FinalizeAnalytics()` (the same failure as the old hand-written HLS Project craft). So the relay cannot just be a generated stack, and it cannot just be the PT Munsplorer template either. It needs **both**: a known-loadable template (full serialization) **and** a grafted complete probe core.
- Resolution (implemented + live-verified 2026-06-21): `runner._write_artemis_relay_craft` now calls `mod_craft_assets.write_artemis_relay_craft`, which seeds the relay from the PT Munsplorer template (full serialization) and grafts in a complete `probeCoreOcto.v2` PART block copied from the stock `Ships/VAB/ComSat Lx.craft` (all MODULE/RESOURCE state intact), surface-attached to a fuel tank with `srfN = srfAttach`. The resulting craft loaded, launched to FLIGHT, and showed two `ModuleCommand` parts with full control authority through the bridge. Unit test: `tests/test_artemis.py::test_artemis_relay_live_craft_seeds_template_and_grafts_probe_core`.

## Relay Live-Flight Findings (2026-06-21, after the launch fix)

Two live relay flights once the command-module launch blocker was fixed (`relay-only-edb3ae1a`, `relay-only-7a4e945d`):

- Flight 1: launched cleanly (fix works end-to-end), ascended to a 91x72 km Kerbin parking orbit with ~33% fuel, then `trans_mun_injection_out_of_fuel` at only 159 km apoapsis. Post-mortem via kRPC `resources_in_decouple_stage`: the active engine was the spent launch Swivel (`liquidEngine2`, decouple-stage 6, LF 0/720) while the Terrier stages below were **full** (stage 4 LF 180/180, stage 2 LF 135/135). The aggregate read 30% fuel ("out of fuel" was a lie). Root cause: `_execute_node`'s burn loop declared `<phase>_out_of_fuel` and broke the instant `available_thrust < 1`, BEFORE its own staging check ran, so it never decoupled the empty launch stage to ignite the Terriers.
  - Fix (applied): in `_execute_node`, when thrust drops, first try `_should_stage`/`activate_next_stage` to ignite the next fueled stage and `continue` the burn; only declare out-of-fuel when no fueled stage remains. New telemetry marker: `<phase>_staged_to_continue_burn`. This matches the existing "stage when available_thrust < 1" rule, which was only being applied during ascent, not during node burns.
- Flight 2 (same craft, with the staging fix): the failure moved to ascent and the staging fix never got to fire. The craft burned ~96% of fuel reaching only an 88 km apoapsis on a deeply suborbital arc (periapsis -107 km), then ran dry during circularization. Flight 1's gravity turn built horizontal velocity (coast periapsis +23 km); flight 2's did not (coast periapsis -107 km) - it went nearly straight up. Same craft, same code, opposite outcome.
- Conclusion: **PT Series Munsplorer is the wrong craft for the relay.** Its multi-stage crewed-lander layout makes the controller's `_should_stage` autostage unreliable - it under-staged at TMI (flight 1) and over-staged during ascent (flight 2, prematurely dropping the gimballed launch engine so the weak Terrier lofted the craft straight up). It was only ever a band-aid to get full serialization. The relay needs a SIMPLE expendable rocket whose staging matches the controller's model.
- Next-step options for a controller-matched relay craft: (a) fix `craft_writer.render()` to emit complete per-part MODULE serialization so the generated 3-stage stack (mainsail/skipper/terrier, ~9.3 km/s) loads without the `EditorLogic.FinalizeAnalytics` NullReference - the highest-value fix because it gives the whole project proper generated craft; or (b) author one dedicated simple probe relay craft in the VAB and seed from it. Do not keep brute-forcing live flights of the PT Munsplorer relay.

## render() Serialization Fixed (2026-06-21)

Chose option (a) and fixed it. The relay now uses `craft_writer.render()` (simple expendable probe-core stack matching the controller), and render() was made launch-safe:

- Real root cause of the `EditorLogic.FinalizeAnalytics` NullReference (isolated by binary-search on a minimal 4-part generated craft): render() emitted a **malformed top-level `ACTIONGROUPS { ... }` block plus `Override*` header fields** (OverrideDefault/OverrideActionControl/OverrideAxisControl/OverrideGroupNames) that real KSP-authored craft do not have. With them present, every generated craft NullReferenced at launch finalization, regardless of parts/modules. Removing them (and per-part `sameVesselCollision`) made a minimal generated craft launch to FLIGHT. This was NOT the missing MODULE blocks, the STAGES block, or the staging indices - those were ruled out one at a time first.
- Secondary hardening: render() now also splices each part's real MODULE/RESOURCE serialization, harvested from stock `Ships/VAB` craft (`craft_writer._part_body_library` / `_extract_part_body`), so engines/decouplers/probe carry full module state. Falls back to the minimal body when no source craft are available (offline tests). Payload service bays are skipped when unsourceable (a relay's payload is its probe core).
- Verified live: the production relay craft (13 parts, probeCoreOcto.v2 command module, 3 engines, no ACTIONGROUPS) loads and launches to FLIGHT with control authority through the bridge. 47/47 unit tests pass. Suspect KSPCommunityFixes tightened editor analytics so the previously-tolerated malformed ACTIONGROUPS now NullReferences.
- The probe-graft-into-PT-Munsplorer helper (`mod_craft_assets.write_artemis_relay_craft`) was removed; render() supersedes it.

## Engineered Relay Satellite + Aero Fins (2026-06-21)

Per player guidance ("a relay needs comm modules, solar, batteries; think spatially about appearance, aerodynamics, fuel"), `render()` now builds a properly-equipped satellite, not a bare stack. Added radial/surface attachment to render() (`CraftNode.srf_parent` + `_attach_surface`, emitting `srfN = srfAttach` with explicit pos/rot) and:

- Avionics/power/comms bus on the command module: `longAntenna` (comm link), 2x `solarPanels5` (power), `batteryBankMini` (storage), mounted radially so they add negligible ascent drag.
- Four `R8winglet` base fins at 4 cardinal positions (Y-rotations 0/90/180/270, radius 1.5 m, low on the bottom tank) to put the centre-of-pressure behind the centre-of-mass for a stable gravity turn.
- All new parts are harvested from the stock `ComSat Lx` serialization (full MODULE state) via `_part_body_library`, so they do not re-trigger the launch NullReference. Part masses/costs added to `parts.STOCK_PARTS` for the dV/TWR budget (relay still ~9.3 km/s, TWR 1.62).
- Verified live: the engineered relay launches and kRPC reports 1 command module, 2 antennas (ModuleDataTransmitter), 2 solar panels (ModuleDeployableSolarPanel), 4 control-surface fins (ModuleControlSurface). 47/47 unit tests pass.

## Ascent Controller FIXED + Relay Reaches Mun SOI (2026-06-21)

The relay now launches, ascends efficiently, stages, circularizes, does TMI, and reaches Mun SOI. Four root causes fixed (each isolated by a live flight):

1. Ascent read flight in the wrong reference frame: `vessel.flight()` defaults to the vessel's own surface frame (co-moving), so `vertical_speed`/`speed` read ~0 the whole ascent (zeroed telemetry + false `ascent_stuck_on_pad`). Fixed: ascent uses `vessel.flight(vessel.orbit.body.reference_frame)` like the landing phases.
2. `ascent_stuck_on_pad` keyed on a single `mean_altitude` read (false-fired at 4 km). Re-keyed on apoapsis < 1000 m + real speeds.
3. Aero stability: active control-surface fins (AV-R8) + a weak probe-core autopilot over-rotated and tumbled the stack (flight 4 reached only 4 km). Switched to PASSIVE `basicFin` base fins -> stable gravity turn.
4. **Staging (the big one):** `craft_writer.render()` gave each inter-stage decoupler the SAME inverse-stage as the parts in its own stage, so the launch-stage decoupler fired at lift-off and split the craft on the pad. AND the launch stage originally had no decoupler above it, so the Mainsail dragged the whole stack and cross-fed the entire fuel load at sea-level Isp (-> 88 km suborbital on 86% of fuel). Fixed: relay design now has decouplers above the launch and transfer stages (none above the bus), and render() sets each decoupler's inverse-stage to render_index-1 so it fires one stage AFTER its engine. Result: clean serial staging (Mainsail -> drop -> Skipper -> drop -> Terrier bus), reaching a 242x70 km Kerbin orbit with ~60% fuel and then Mun SOI.

Flight 7 (relay): reached Mun SOI, `mun_capture_out_of_fuel` (Skipper transfer stage didn't decouple before capture; low-thrust Terrier captured dragging it, ~5 t short). Flight 8 (relay fuel boosted to ~10 km/s: transfer 3x Rockomax16, bus 2x FL-T800): reached Mun SOI with **81% fuel** and the node-burn staging fix fired mid-TMI (`trans_mun_injection_staged_to_continue_burn`). So fuel is no longer the constraint.

## RELAY DEPLOYED — Milestone 1 complete (2026-06-21)

Flight 16 (`relay-only-aa01045a`) deployed the Mun relay into a **2041 km x 101 km Mun orbit**
(`artemis_mun_relay_deployed`, relay_deployed=True), 84% fuel remaining. The chain that finally
worked: 3 inline `asasmodule1-2` reaction wheels (~15 kN*m) gave enough attitude torque that the TMI
burn aligned and delivered a clean ~102 km Mun periapsis; the capture held retrograde with the
autopilot+gimbal (re-pointed each second) and accepted the relay band directly
(`mun_orbit_captured_relay_band`). Key enablers (cumulative): generated-craft launch fix (no
ACTIVEGROUPS/Override header, real spliced part serialization), correct decoupler inverse-stage
(render_index-1), ascent body-frame flight reads, passive basicFin aero, node-burn staging, safe TMI
re-align (no flip), the reaction wheels, the autopilot+gimbal capture, and relay-band capture
acceptance (a relay WANTS a high orbit, so capture it directly instead of forcing a 60 km periapsis).

The TMI still has run-to-run variance, but with the reaction-wheel torque it succeeds reliably enough
and the relay-band capture tolerates the resulting periapsis spread.

## (Resolved) TMI Targeting Precision (relay reaches Mun SOI but periapsis too high) (2026-06-21)

Flight 8 reached Mun SOI with ample fuel but the encounter periapsis was 2.18 Mm (a far grazing pass near the SOI edge), so `_capture_mun_orbit` rejected it via the `max_mun_capture_periapsis_m` (1 Mm) gate. The TMI raised Kerbin apoapsis to ~12.8 Mm (slightly past the Mun) and the transfer corrections aborted (`mun_transfer_correction_closest_worsened_abort`, `..._stale_node_abort`) instead of dialing the Mun periapsis down to the ~60-200 km target. A relay wants a HIGH orbit, but 2.18 Mm exceeds even the relay's own target apoapsis band (<=2.15 Mm), so it must still arrive on a lower-periapsis encounter. This is the long-standing TMI finite-burn / patched-conic precision problem (many prior entries). Next: tighten the TMI node selection + correction loop so the live Mun periapsis lands in the capture-grade band before declaring the transfer complete; consider decoupling the spent transfer stage before the capture burn.

## (Historical) Ascent Controller Telemetry / Vessel Handling (2026-06-21)

With the craft now launchable AND well-built, the relay still does not reach Kerbin orbit. The failure is in the live ascent controller, not the craft:

- `vessel.flight()` readings are unreliable mid-ascent: telemetry repeatedly recorded `vertical_speed = 0`, `surface_speed = 0`, `throttle = 0` while altitude/apoapsis were clearly climbing and fuel was depleting (so the engine WAS firing). Measured pitch tracked correctly in one run but altitude read <500 m in another. This points to a stale/wrong active-vessel reference (kRPC), likely because staging spawns decoupled sub-vessels and the controller loses the active one.
- Consequences observed: flight 3 (render craft) climbed but the circularization/staging path threw `Unable to reacquire usable vessel after staging`; flight 4 (engineered craft) false-triggered the `ascent_stuck_on_pad` guard at t=57 s (altitude read <500 while actually ~4 km), aborted control, and the vehicle coasted up and splashed with 71% fuel unused.
- Next-step fixes to investigate (controller, not craft): make `_reacquire_vessel` robustly re-select the controlled vessel (by command-part / probe) after every stage; build the ascent `flight()` from an explicit `vessel.surface_reference_frame`; replace the `ascent_stuck_on_pad` mean-altitude test with an apoapsis/vertical-speed test that cannot false-fire on a stale read; verify actual thrust before trusting telemetry zeros. Consider AV-R8 (active control surface) vs a passive fin to avoid over-control from the weak probe core.

## Do Not Repeat

- Do not seed an Artemis launch vehicle from the PT Series Munsplorer (a crewed multi-stage Mun lander); its staging makes the autostage controller unreliable (over-stages on ascent, under-stages on transfer burns). Use a simple expendable stack whose staging matches the controller. (2026-06-21)
- Do not let a node burn (`_execute_node`) declare out-of-fuel without first attempting to stage into a lower fueled stage; transfer/capture fuel commonly sits in decoupled lower stages. (fixed 2026-06-21)
- Do not launch any generated/seeded craft without a controllable command module. An uncrewed design (relay) needs a probe core; a crewed design needs a probe core or guaranteed assigned crew. A bare crewed pod launched headless = "no control source" dialog = launch blocked. (player-confirmed 2026-06-21)
- Do not accept Mun transfer nodes with planned periapsis below about 45 km; finite-burn execution can erase tens of kilometers of margin.
- Do not accept any Mun capture orbit below 8 km periapsis; terrain clearance matters more than the mathematical body radius.
- Do not stop capture just because the vessel is barely orbiting. A 1-2 Mm Mun apoapsis makes landing much more expensive.
- Do not keep full throttle after a stage has no thrust; stage immediately when `available_thrust_n < 1`.
- Do not use 4x physics warp during powered ascent or low-altitude terminal landing.
- Do not patch the live controller during the last seconds before impact; use the run as data, then revert and launch a corrected controller.
- Do not let the live TMI planner perform a 10k+ node brute-force search; it can waste the launch window before a node is selected.
- Do not trust a selected maneuver node after a long low-thrust burn; verify the actual post-burn orbit and correct it.
- Do not switch to pure vertical attitude below 80 m while horizontal speed is still above a few m/s; it can preserve or amplify sliding speed.
- Do not let the low-altitude suicide-burn safety margin force near-full throttle during final hover. Terminal descent needs capped proportional throttle, not bang-bang braking.
- Do not retract landing legs immediately after Mun touchdown. They are part of the surface support until the return ascent has positive terrain clearance.
- Do not start Mun return ascent at full throttle from a marginally sliding or tilted touchdown. Use a short settle, then partial-throttle vertical liftoff until terrain clearance is real.
- Do not relaunch from a touchdown that never settles. Record it as an unstable landing and fix touchdown attitude/speed first.
- Do not accept a 2 m/s sideways Mun touchdown for return testing. It can leave the crew alive but the engine or lander unusable.
- Do not trust TMI maneuver-node remaining delta-v after the actual orbit has already crossed the Mun transfer band. Stop only on a capture-grade live Mun periapsis or on the no-SOI apoapsis safety cap.
- Do not stop TMI on a grazing Mun SOI. A high-SOI pass can be worse than no encounter because capture burns become unaffordable.
- Do not wait until 120 m to start lateral cleanup if horizontal speed is still above about 5 m/s; that is too late for the stock lander authority and fuel reserve.
- Do not mark the expanded project mission complete without relay deployment and surface science. The Artemis return is necessary but no longer sufficient.
- Do not accept a very high HLS "emergency" Mun capture just because fuel is below 35%; a 1-2 Mm apoapsis makes the later landing guidance fragile and wastes surface sortie fuel.

Artemis split-run data: `artemis-0001-4cce36ed`.

- HLS launch, Kerbin parking orbit, TMI, and Mun SOI entry worked.
- Mun capture reached a usable but high orbit with apoapsis about 330 km and periapsis dropping through 20-14 km.
- The old capture stop required apoapsis at or below 320 km, so it kept burning and drove periapsis below 8 km.
- New rule: accept a safe captured HLS parking orbit up to about 360 km apoapsis when periapsis is still above 12 km. A slightly high HLS parking orbit is cheaper to fix than a terrain-grazing periapsis.

Artemis split-run data: `artemis-0001-f76578b3`.

- HLS launch and Kerbin circularization worked again.
- The transfer search found a Mun encounter with planned periapsis about 40 km, but the safety gate rejected it because it still required at least 45 km.
- Existing live TMI stop logic already accepts 35-180 km live Mun periapsis. New rule: the transfer planner may accept 35-120 km planned periapsis for high-margin Artemis HLS/Orion transfer windows, while still scoring 45-90 km as the preferred band.

Artemis split-run data: `artemis-0001-59479c06`.

- HLS/Moonship launch, Kerbin orbit, modeled refuel, TMI, Mun SOI, and Mun capture succeeded. The HLS phase ended as `artemis_hls_parked_in_mun_orbit`.
- Orion/SLS reached a valid Kerbin parking orbit, but the TMI burn reduced orbital energy. Remaining node delta-v increased while apoapsis and periapsis fell, so the old fixed node-frame target was not reliable for this craft/control point.
- New rule: execute maneuver nodes by pointing at `node.remaining_burn_vector(vessel.orbital_reference_frame)` and wait for a bounded autopilot alignment before lighting engines. If the first seconds of a TMI burn still reduce orbital energy, stop and try the inverted burn vector only after realignment.

Artemis split-run data: `artemis-0001-cd697b7d`.

- The explicit node-vector burn fixed Orion TMI: Orion reached Mun SOI after HLS had already parked in Mun orbit.
- Orion capture then timed out while still on an escaping Mun trajectory. Telemetry showed no useful capture progress and the fuel fraction stayed constant through the recorded capture burn, indicating the loop may have started while warp/rails or throttle state prevented real thrust.
- New rule: after every `warp_to` before a powered capture or landing burn, force rails and physics warp to zero, wait for attitude alignment, and verify actual thrust early instead of waiting for a long timeout.

Artemis split-run data: `artemis-0001-1a377a59`.

- HLS/Moonship predeploy succeeded and the low HLS periapsis was recovered by the new periapsis-raise burn. HLS parked in Mun orbit at about 35 km periapsis and 635 km apoapsis.
- Orion/SLS reached Kerbin orbit and selected a nominal Mun encounter, but the follow-up Mun correction burn lowered Kerbin periapsis from a safe parking orbit to about 27 km.
- The controller then waited for Mun SOI while Orion reentered Kerbin atmosphere, so the trial was marked failed as `orion_mun_correction_lowered_kerbin_periapsis`.
- New rule: every Mun correction candidate must preserve a non-atmospheric Kerbin periapsis, and the live burn/coast logic must abort immediately if a correction drives periapsis below the configured safe floor.

Artemis split-run data: `artemis-0001-7538975f`.

- HLS predeploy, Orion launch, Orion TMI, and Orion Mun capture all worked in one trial.
- HLS was accepted in a high emergency Mun parking orbit at about 106.8 km periapsis and 1.92 Mm apoapsis with only about 21.5% fuel.
- The old landing deorbit burned from this high orbit using guessed retrograde attitude, twice raising periapsis instead of lowering it. The trial failed as `hls_surface_sortie:mun_landing_deorbit_wrong_direction`.
- New rule: HLS landing preparation first lowers any too-high Mun apoapsis at periapsis using a calculated apsis-change node, then deorbits at apoapsis using a calculated negative-prograde node. Capture emergency acceptance is reserved for genuinely critical fuel reserves, not 20-35% fuel.
- Expanded project rule: future trials include a separate Mun relay launch and a surface-science gate before HLS ascent.

Artemis relay expansion data: `artemis-0001-e74c7bee`.

- The relay/SLS cargo stack reached Kerbin orbit and started a nominal Mun transfer, but the first correction was only about 6 m/s and the old generic maneuver executor lit high thrust before settling into fine control.
- That small correction overburned into a much larger energy change, driving apoapsis past the Mun transfer band instead of restoring the planned encounter.
- New rule: small Mun corrections must start at precision throttle, use direct prograde/retrograde markers for near-pure prograde corrections, and abort immediately if remaining node delta-v diverges.

Artemis relay expansion data: `artemis-0001-bc4b7aed`.

- Precision throttle prevented the catastrophic correction overburn, but the controller still stopped TMI too early at the fixed 14.5 Mm apoapsis cap even though the planned node had a safe Mun encounter.
- The follow-up correction then spent more than two minutes near the stale node. Live KSP inspection showed no SOI change and closest approach about 2.64 Mm, outside the Mun SOI, while the burn kept slowly worsening the geometry.
- New rule: if a precomputed TMI node already predicts a safe Mun encounter, allow a wider live apoapsis cap before falling back to correction. During correction, track actual closest approach to Mun and stop when it enters the SOI or abort when the closest approach worsens after the node has gone stale.

Artemis relay expansion data: `artemis-0001-9e986b87`.

- The relay stack reached Kerbin orbit, completed TMI, entered Mun SOI, and captured into a stable Mun orbit of about 63 km by 643 km with roughly 19% propellant margin.
- The follow-up high-relay shaping burn was commanded as a small positive-prograde apoapsis raise, but the active stage had poor attitude response after capture. Short pulses lowered apoapsis instead of raising it, and the trial failed as `relay_predeploy:mun_relay_apoapsis_raise_wrong_direction`.
- kRPC on this install exposes `SpeedMode.orbit`, not `SpeedMode.orbital`; the old helper silently fell back to manual vectors for SAS prograde/retrograde. New rule: resolve both enum names explicitly.
- Mission-priority rule: a stable captured Mun relay orbit is acceptable for the live Artemis loop while HLS/Orion landing and return are still being debugged. High relay orbit shaping can be restored after the cargo stage has stronger attitude authority or a validated control point.

Artemis relay expansion data: `artemis-0001-c97e2f1e`.

- The SLS cargo relay stack reached a near-orbital ascent state with apoapsis around 75-76 km, but the controller transitioned to `coast_to_apoapsis` while the active stage still had measured thrust.
- Setting throttle to zero was not sufficient for this ACK cargo stack; the still-burning stage continued adding energy during the supposed coast. The craft crossed into a Kerbin escape trajectory with positive periapsis and no Mun encounter.
- New rule: every coast phase must force throttle to zero, measure actual thrust, and if thrust continues, hold a retrograde guard attitude with physics warp disabled. If `time_to_apoapsis` becomes nonfinite or the vessel is escaping, abort circularization and do not allow the follow-on Mun-transfer phase just because periapsis is above atmosphere.

Artemis relay expansion data: `artemis-0001-c0b1a6b2`.

- The relay stack reached Mun SOI with a useful periapsis around 119 km, so the corrected TMI and coast-to-SOI path worked.
- Capture then failed because the live craft was copied from the SLS Block 1B Cargo source while the design object was a liquid-only relay stack. The copied craft exposed thrust behavior and staging that did not match the optimizer's assumptions, and the capture burn increased Mun-relative speed instead of reducing it.
- New rule: precision live-control craft must match the design model. Use SLS/ACK craft as reference or visual assets, but do not drive a copied SRB-heavy craft through Mun capture unless the controller explicitly models SRB burnout, staging, and attitude. For the relay, write the generated throttleable stock stack and keep the real-looking SLS cargo craft as a source note.
- New rule: Mun capture must use the tested orbital-retrograde steering helper and a finite-burn lead of at least the computed `capture_burn_estimate.lead_time_s`; do not use raw `(0, -1, 0)` autopilot targeting for capture.

Artemis relay editor failures: `artemis-0001-77b98add` and `artemis-0001-d03abb84`.

- The minimal hand-written relay craft reached the editor but failed on `/launch` with `EditorLogic.FinalizeAnalytics` null references. Adding ordinary header metadata was not enough.
- New rule: KSP craft files used for live launches must be full KSP-authored serializations or template-seeded from a known-loadable craft, because parts need module state blocks and editor analytics metadata. The relay now seeds from the stock `PT Series Munsplorer.craft`, which is liquid-engine only and avoids the SLS cargo SRB/staging mismatch.

---

## 2026-06-21 — 🚀 FULL ARTEMIS MISSION COMPLETE (all 3 milestones, live)

> NOTE: the PT-Munsplorer seeding above is OBSOLETE. The relay/HLS/Orion now use `render()`-generated
> probe-controlled stacks (real per-part MODULE/RESOURCE serialization spliced from stock VAB craft;
> no top-level ACTIONGROUPS/Override fields; decoupler inverse-stage = render_index-1). See the
> consolidated master log `new-chat/AEROSPACE_AGENT_LOG.md` for the authoritative current state.

- **Milestone 1 — Relay:** deployed to a high Mun orbit (`artemis_mun_relay_deployed`).
- **Milestone 2 — HLS:** render() lander (probe core, 3 reaction wheels, 4 landing legs, clean
  staging) — soft touchdown (−0.1 m/s vertical), surface science, ascent back to Mun orbit
  (`artemis_hls_returned_to_mun_orbit`). The terminal Mun landing, never solved before, is solved.
  - Enabling fixes: `_point_at_node` ALWAYS uses autopilot + alignment-error wait (no SAS+fixed-sleep
    shortcut); deorbit uses full throttle (`_execute_mun_apsis_node` `max_throttle` param → 1.0,
    `max_burn_s=200`); the unlandable 901 t MUNSHIP was retired (its custom Starship staging never
    activated a descent engine).
- **Milestone 3 — Orion:** render() crew vehicle, `crewed=False` (headless-launchable) +
  `RocketDesign.heatshield=True` (render() adds HeatShield1 when crewed OR heatshield) + 3 reaction
  wheels. Full arc launch → Kerbin orbit → TMI → Mun capture (`artemis_orion_waiting_in_mun_orbit`,
  rendezvous-equiv with HLS) → trans-Kerbin injection → reentry (heat shield) → parachute →
  `recovered` (−1.4 m/s). Crew transfer is modeled.
  - TWR gotcha: `estimate_design` ignores accessory mass, so keep generated launch-stage est TWR
    ≥ ~1.4. Orion at est 1.24 never left the pad; lightening upper stages (transfer Rockomax32→16,
    service 3→2 tanks) → est 1.55 fixed it.
- **Re-run:** per-phase drivers `tools/fly_relay_once.py`, `tools/fly_hls_predeploy.py`,
  `tools/fly_hls_sortie.py <hls-vessel>`, `tools/fly_orion.py`. 47/47 tests pass.
