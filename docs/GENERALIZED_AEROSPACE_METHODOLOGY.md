# Generalized Autonomous Aerospace Methodology

A domain-transferable playbook for "autonomously design + fly a rocket to achieve an
orbital / landing / return goal." Extracted from the KSP1 Artemis replication project (relay to high
Mun orbit, HLS land-and-return, Orion Kerbin→Mun-orbit→Kerbin recovery — all flown live via kRPC +
an HTTP bridge). Read this as the agent's prior knowledge. KSP-specific values are kept as concrete
instantiations, but every rule is stated so it transfers to other bodies, sims, or real flight
software.

Source of truth for this project's specifics: `AEROSPACE_AGENT_LOG.md` (master log),
`docs/artemis_mun_engineering_notebook.md`, `docs/artemis_precision_design_notes.md`.

---

## 1. The generalized loop

The project did NOT converge by planning harder up front. It converged by running cheap, isolated,
single-phase live flights and promoting each failure into a permanent rule. The loop:

```
CALCULATE  →  GENERATE  →  FLY ONE PHASE  →  DIAGNOSE FROM TELEMETRY  →  FIX ONE ROOT CAUSE  →  RECORD  →  RETRY
   (Δv/TWR/      (.craft)     (live, isolated,    (per-part, not             (the controller,        (append a       (revert,
    torque/                    smallest unit)      aggregate, state)          NOT the craft mid-air)  "Do Not Repeat") relaunch)
```

Governing principles, each load-bearing:

- **Calculate before you build or burn.** Seed every maneuver from vis-viva / Hohmann / suicide-burn
  math (Section 4), then refine with the sim's own predictor (KSP patched conics). Never brute-force
  live (the 10k-node live TMI search wasted launch windows; the energy-seeded grid replaced it).
- **Isolate the phase under test.** The win came from per-phase drivers (`fly_relay_once.py`,
  `fly_hls_predeploy.py`, `fly_hls_sortie.py`, `fly_orion.py`), not the full orchestrator. A 16-flight
  relay arc is only affordable if each flight tests one thing. **General rule: build single-phase
  entry points that resume from a persisted prior-phase state; debug the failing phase, not the
  mission.**
- **One root cause per iteration.** Binary-search to the actual cause before changing anything (the
  craft-launch NullReference was isolated on a *minimal 4-part craft*; MODULE blocks, STAGES, and
  staging indices were each ruled out one at a time before the real cause — a malformed
  `ACTIONGROUPS`/`Override*` header — was found). Changing two things at once destroys the signal.
- **Diagnose from the right altitude of data.** Aggregate telemetry lies. "Out of fuel" was false —
  per-part `resources_in_decouple_stage` showed full lower stages while the spent top stage read
  empty; "stuck on pad" was a co-moving reference-frame artifact. **General rule: trust per-component,
  state-aware, correct-reference-frame reads; verify actual thrust/Δv movement before believing a
  summary number.**
- **Never patch the controller in the last seconds before impact.** Let the bad run finish as data,
  revert, fix offline, relaunch. Live edits during terminal descent corrupt the lesson.
- **Record immediately, as rules not narration.** Every failure became a "New rule" + a "Do Not
  Repeat" line. That ledger is the actual product — it is what lets a future run skip 15 flights.

### State that MUST persist between attempts (so a future run continues fast)

1. **The rule ledger** — "Fixes & experiences," "New rule," and "Do Not Repeat" lists. This is the
   compounding asset; treat it as append-only institutional memory.
2. **Measured body constants, read live from the sim, never from memory** (KSP: Kerbin μ=3.5316e12,
   R=600 km, SOI=84,159 km, atmo top 70 km, sidereal 21,549 s; Mun μ=6.5138e10, R=200 km,
   SOI=2,429,559 m, no atmosphere, period 138,984 s). Re-read on any environment change.
3. **The proven craft design + its launch-safe serialization recipe** (Section 3) — the expensive part
   is making a generated craft actually launch; once solved, freeze it.
4. **Per-phase success markers and the parked-vehicle handle** (e.g. `artemis_mun_relay_deployed`,
   the `AI-HLS-Starship-*` vessel name) so the next phase resumes against real orbital state instead
   of re-flying the prior phase.
5. **Telemetry artifacts** — `runs/<phase>-<id>/*.telemetry.jsonl` — keyed by phase so a post-mortem
   can replay exactly what the controller saw.

---

## 2. Reusable engineering principles

Each promotes a concrete project fix to a general rule. Format: **Symptom → Root cause → General rule
→ KSP instantiation.**

**P1 — A vehicle with no usable control source cannot be commanded headless.**
Symptom: launch returns OK but flight scene never appears, OR a 400 `Object reference not set`.
Root cause: the only command part was an empty crewed pod; the headless launch path skips crew
assignment, so there is no control source and the sim blocks with a "no crew/probe core" prompt a
script cannot dismiss. General rule: **every autonomously launched vehicle must carry a guaranteed
control source (probe/avionics core with power) independent of crew assignment.** KSP: include
`probeCoreOcto.v2`; fly even crew analogues as `crewed=False` so they launch headless.

**P2 — Generated vehicle definitions must match the real serialization the loader expects, with no
extraneous metadata.** Symptom: every generated craft NullReferences at launch finalization
regardless of parts. Root cause: the generator emitted a top-level `ACTIONGROUPS{}` block and
`Override*` header fields that real authored craft omit (likely tightened by KSPCommunityFixes);
hand-written minimal parts also lacked persisted module state. General rule: **generate to the exact
schema of known-good authored files — omit fields real files omit, and splice real per-component
serialization rather than hand-rolling minimal stubs.** KSP: `render()` drops `ACTIONGROUPS`/`Override*`
and splices each part's real `MODULE`/`RESOURCE` body harvested from stock `Ships/VAB` craft
(`_part_body_library`).

**P3 — Separation/staging events must fire one step AFTER the element they release, never with it.**
Symptom: craft splits on the pad. Root cause: each inter-stage decoupler was assigned the same
activation stage as the engine it sits above, so it fired at ignition. General rule: **a separator
activates exactly one stage later than the stage it tops; compute it, don't guess.** KSP: decoupler
inverse-stage = `render_index - 1` (fires AFTER its engine, not during).

**P4 — Read motion in the correct, non-co-moving reference frame.** Symptom: vertical/surface speed
read 0 the whole ascent → zeroed telemetry + false "stuck on pad." Root cause: the default flight
frame is the vessel's own surface frame, which is co-moving, so speeds collapse to 0. General rule:
**compute velocity in the central body's inertial frame, and key liveness checks on a quantity that
cannot be faked by a stale read.** KSP: `vessel.flight(vessel.orbit.body.reference_frame)`; key the
stuck-detector on apoapsis < 1000 m, not a single mean-altitude sample.

**P5 — Weak controllers must not drive high-authority aero surfaces.** Symptom: stack over-rotates
and tumbles on launch. Root cause: a weak probe autopilot commanding large active control surfaces
over-controls. General rule: **match actuator authority to controller authority; prefer passive
stabilization when the controller is weak.** KSP: passive `basicFin` at 4 cardinal positions low on
the booster (CoP behind CoM), NOT active `R8winglet`.

**P6 — A burn executor must stage into the next fueled element before declaring "out of fuel."**
Symptom: `<phase>_out_of_fuel` at 30% aggregate fuel. Root cause: the loop broke the instant active
thrust < 1 N, before its own staging check — the spent top stage was empty while full stages sat
below it, decoupled. General rule: **on thrust loss during any burn, attempt to stage into a fueled
element and continue; only declare empty when no fueled stage remains.** KSP: `_execute_node` tries
`_should_stage`/`activate_next_stage` then `continue`; marker `<phase>_staged_to_continue_burn`.

**P7 — A finite injection burn can only move energy one way; falling energy means mis-alignment, not
wrong sign.** Symptom: a prograde trans-lunar burn *lowers* apoapsis. Root cause: the burn ignited
off-axis because attitude wasn't truly aligned. General rule: **if the first seconds of a burn move
orbital energy the wrong way, re-align to the node vector at low throttle and resume — do NOT flip
the burn vector; and never let a correction drive the return periapsis into the atmosphere.** KSP:
point at `node.remaining_burn_vector`, wait for a bounded alignment error, resume at gimbal-trim
throttle; abort safely after a few tries.

**P8 — Attitude authority is the recurring root cause of wasted Δv.** Symptom: TMI/capture burns
ignite off-axis, ~50% of runs miss SOI, >1 km/s wasted without capturing. Root cause: a command core
alone cannot rotate a heavy upper stack fast enough to track a maneuver/retrograde marker before a
finite burn. General rule: **heavy upper stages need dedicated reaction-wheel torque sized to the
stack BEFORE any finite burn; mount it inline, never surface-clipped.** KSP: stack ~3 inline
`asasmodule1-2` (~15 kN·m total) in the bus. This single fix took relay SOI-arrival from ~50% to
reliable.

**P9 — Track a moving attitude target with the gimbal (autopilot), not reaction wheels alone.**
Symptom: SAS-only capture cannot hold the fast-moving retrograde marker on a heavy craft; >1 km/s
wasted, no capture. Root cause: reaction-wheel torque is too slow for a fast-slewing marker on a
massive vehicle. General rule: **for energy-removal burns, hold the marker with engine-gimbal
autopilot and re-point every control tick.** KSP: capture re-points retrograde each second under
autopilot+gimbal.

**P10 — Target the orbit the payload actually wants; don't fight precision you don't need.** Symptom:
TMI precision fights to hit a 60 km periapsis it doesn't require. Root cause: the success criterion
was over-tight for the payload's real mission. General rule: **define the success band by the
payload's functional need and capture into it directly.** KSP: a relay WANTS a high orbit — its band
is apoapsis 250–2150 km, periapsis ≥50 km — so capture the high-periapsis encounter directly
(`mun_orbit_captured_relay_band`) instead of forcing a low periapsis.

**P11 — Make the pipeline robust to execution variance; don't assume deterministic burns.** Symptom:
TMI outcome varies run-to-run (encounter periapsis spread 168 km … 2.18 Mm). Root cause: finite-burn
+ patched-conic drift is inherently noisy. General rule: **design acceptance bands and re-check/
top-off logic around expected execution variance instead of a single deterministic target;
re-verify predictions after a short settle before declaring a miss.** KSP: relay-band capture
tolerates the periapsis spread; after TMI, recheck `time_to_soi_change` for a few seconds before
failing.

**P12 — Use the right throttle profile for the burn TYPE; a precision cap is not a maneuver cap.**
Symptom: a deorbit reusing a precision-apsis routine times out at ~1% fuel spent. Root cause: that
routine capped throttle at 0.18 for fine apsis tweaks; on a low-thrust vehicle that cap + a 90 s burn
limit can't complete a large lowering burn. General rule: **parameterize throttle/time limits by burn
class — full authority for large energy changes, capped proportional for precision/terminal.** KSP:
`_execute_mun_apsis_node` gained `max_throttle`; deorbit passes `max_throttle=1.0, max_burn_s=200`.

**P13 — Terminal landing requires a controllable lander with CLEAN staging and a dedicated descent
engine that actually activates.** Symptom: a 901 t realistic stack reached orbit but never landed —
fuel stayed constant through descent (descent engine never fired), so it fell at ~680 m/s and
crashed. Root cause: complex custom multi-engine staging never activated a powered-descent engine.
General rule: **the lander must be controllable (probe core), have stable legs, and have a clean
stage order whose active element after jettison IS the descent/ascent engine; reject any craft whose
descent engine doesn't ignite on command.** KSP: a `render()` lander (probe core, 3 reaction wheels,
4 `landingLeg1`, one Terrier as the active stage after booster/transfer drop) landed at −0.1 m/s
vertical / 0.2 m/s horizontal; the unlandable MUNSHIP was retired.

**P14 — A headless return vehicle still needs reentry survival hardware.** Symptom: an uncrewed return
craft has no heat shield. Root cause: heat-shield generation was gated on `crewed`, but the return
craft flies `crewed=False` to launch headless. General rule: **decouple "needs reentry protection"
from "carries crew"; gate survival hardware on the return requirement.** KSP: `render()` adds
`HeatShield1` + parachute when `crewed OR heatshield`; the uncrewed Orion reentered (−683 m/s) and
recovered at −1.4 m/s.

**P15 — Leave TWR margin for un-estimated accessory mass.** Symptom: estimate said TWR 1.24 but the
craft ignited and never left the pad (apoapsis 89 m, `ascent_stuck_on_pad`). Root cause: the mass
estimator ignores accessories (heat shield, reaction wheels, nose, chute, fins, probe ≈ 0.5–0.8 t),
so actual launch TWR ran below the estimate and dipped under 1. General rule: **size the first stage
to an ESTIMATED TWR margin that absorbs un-modeled mass — target est TWR ≥ ~1.4.** KSP: lightening
upper stages (transfer Rockomax32→16, service 3→2 tanks) lifted est TWR 1.24→1.55 and it flew; relay/
HLS/Orion all launch reliably at est TWR ≥ 1.44.

**P16 — Terminal descent is proportional, not bang-bang; kill lateral speed before vertical contact.**
Symptom (recurring across ~12 landing trials): suicide-burn max-throttle clamps near the ground
bounce the lander, reintroduce lateral speed, and end in a hard or tilted touchdown; switching to
pure-vertical too early preserves sliding. Root cause: a fixed suicide-burn safety margin forced
near-full throttle in the hover regime, and attitude was released before lateral motion was killed.
General rule: **below a terminal altitude, leave suicide-burn clamps for a capped proportional hover
controller (descend ~3→2→1.2→0.7 m/s); stay retrograde until horizontal speed is ~<3.5 m/s; do not
cut throttle near the surface while sliding; do not relaunch from a touchdown that never settles.**
KSP: enter capped terminal control by ~120 m (earlier, ~500 m, if horizontal speed is still high);
hold a short settle and keep legs deployed through liftoff.

**Operational meta-principles** (from the gotchas):
- After a controller process dies mid-flight, the vehicle is left live; **revert, and if it reverts
  to the pad, recover the pre-launch vessel** (KSP: bridge `/revert` → `/space-center` →
  `vessel.recover()`) or the next launch hits a "site not clear" block. Stale orbiting test vehicles
  are harmless but accumulate.
- **Launch each flight as its own tracked task**, never nested/background-forked, or you lose the
  completion signal and orphan the process.

---

## 3. The craft-generation contract

What makes a *generated* vehicle actually launch and fly. A craft that violates any clause below
either won't launch or won't fly its profile. (KSP entry point: `craft_writer.render()`.)

1. **Controllable command source.** Every craft carries a probe/avionics core with electric charge as
   root, independent of crew (P1). Crew analogues still fly probe-controlled for headless launch.
2. **Real serialization, no extraneous headers.** Emit only fields real authored files contain — no
   top-level `ACTIONGROUPS`/`Override*` blocks (P2). Splice each part's real `MODULE`/`RESOURCE` body
   from known-good authored craft; fall back to a minimal body only offline.
3. **Correct separation math.** Each inter-stage separator activates one stage later than the stage it
   tops (P3: inverse-stage = `render_index - 1`). Put a decoupler above the launch and transfer
   stages; none above the stage that must stay with the payload/lander. Keep crossfeed off so each
   stage drains independently and autostage is predictable.
4. **TWR margin for un-estimated mass.** First-stage estimated TWR ≥ ~1.4 (P15), because the estimator
   ignores accessory mass (heat shield, reaction wheels, nose, chute, fins, probe ≈ 0.5–0.8 t).
5. **Attitude authority built in.** ~3 inline reaction wheels in the bus for any heavy upper stack
   (P8), mounted inline (not surface-clipped).
6. **Passive aero stability.** Passive base fins at the 4 cardinal positions low on the booster so CoP
   sits behind CoM (P5); not active control surfaces under a weak autopilot.
7. **Mission-specific hardware, gated on REQUIREMENT not crew.** Landing legs + a descent engine that
   is the active stage after jettison for landers (P13); heat shield + parachute when
   `crewed OR heatshield` for return craft (P14); comms/power bus (antenna, solar, battery) radially
   mounted for negligible ascent drag on a satellite.
8. **Estimable budget.** Every part has mass/cost in the parts table so Δv/TWR estimates are
   meaningful (KSP relay ≈ 9.3–10 km/s, TWR ≈ 1.6). Mark accessory mass as un-estimated and absorb it
   via clause 4.

If a generated craft can't be made launch-safe in time, the documented fallback is to seed from a
known-loadable authored craft and graft a complete probe core into it — but generated-and-launch-safe
is strictly preferred because it gives the design model and the live craft identical staging (the
PT-Munsplorer seeding failed precisely because its staging didn't match the controller's autostage
model: it over-staged on ascent and under-staged on TMI).

---

## 4. The flight phases as a state machine

Each phase is gated on the prior phase's success marker; on failure, revert and re-fly only the
failing phase. Format: **phase — gating success condition — known failure mode(s).** Equations are in
the body-constants block below; trigger from calculation, refine with the sim's predictor, verify
actual motion.

| Phase | Gating success condition | Known failure mode |
|---|---|---|
| **Ascent** (gravity turn, no time-warp under power) | Apoapsis reaches parking target (KSP ≥80 km) with margin fuel; stage on measured low thrust only | Stuck-on-pad (low est TWR P15, or stale-frame false read P4); tumble (active fins P5); straight-up loft (over-staging dropping the gimballed engine early) |
| **Circularization** | Periapsis above atmosphere; non-escaping orbit | Burning during the "coast" if a stage has unthrottleable thrust → escape (force throttle 0, MEASURE thrust, hold retrograde guard if it persists) |
| **Transfer injection (TMI/TLI)** | Live predicted target-body periapsis in capture-grade band; SOI encounter appears | Energy falls = off-axis (P7, re-align don't flip); stop too early on a grazing SOI (capture becomes unaffordable); over/under-burn from finite-burn drift (P11) |
| **SOI capture** | Captured into the payload's target band; not escaping | SAS-only can't hold retrograde on a heavy craft (P9); weak capture at 1–2 Mm apoapsis makes landing too expensive; warp/rails left on → no real thrust (force rails+warp 0, verify thrust early) |
| **Deorbit** | Periapsis lowered to a landing-safe value (≥ a terrain-clearance floor, KSP ~8–12 km), retrograde at apoapsis | Precision-cap throttle times out the burn (P12, use full throttle); wrong-direction burn from a SAS+fixed-sleep shortcut (always autopilot + alignment-error wait); lower too-high apoapsis first, then deorbit |
| **Powered descent / landing** | Touchdown with vertical AND horizontal speed near zero, vehicle upright and intact | Bang-bang suicide-burn bounce; lateral slide; tilted touchdown; pure-vertical switch too early (all P16) — solved by a capped proportional terminal controller entered early enough |
| **Surface ops** | Science/objective recorded after a STABLE settle | (Modeled when no science modules — record the limitation) |
| **Return ascent** | Back to a stable orbit; engine/legs intact | Throttle-up before settle slides/destroys the craft (settle first, keep legs deployed, partial throttle until positive terrain clearance) |
| **Return injection (TEI)** | Return-body SOI; periapsis in a recoverable reentry band, never below the atmospheric floor | A correction lowers return periapsis into the atmosphere mid-coast (P7 — every correction must preserve a non-atmospheric periapsis and abort if it doesn't) |
| **Reentry / recovery** | `recovered` at a survivable speed | No heat shield on a headless return craft (P14); chute deploy too high/low |

KSP terminal markers reached live: `artemis_mun_relay_deployed` → `artemis_hls_parked_in_mun_orbit`
→ `mun_landed` → `mun_surface_science_completed` → `artemis_hls_returned_to_mun_orbit` →
`artemis_orion_waiting_in_mun_orbit` → `recovered`.

---

## 5. What's still modeled vs. real (honest gaps)

These are real limitations, stated at true severity, not polished away.

- **Crew transfer is MODELED, not flown.** There is NO docking and NO crew-transfer automation. The
  architecture *represents* the astronaut transfer as "both vehicles alive in Mun orbit at the same
  time" (the Orion's `artemis_orion_waiting_in_mun_orbit` is a rendezvous-EQUIVALENT, not a rendezvous)
  plus a recovered return capsule. A literal Artemis milestone-3 requires
  rendezvous + dock + crew-transfer between Orion and the parked HLS — not done. **Severity: high if
  the goal is literal Artemis fidelity; the headline "crew went to the Moon and came home" is an
  architectural model, not a docked transfer.**
- **The landing is a suicide-burn, not a precomputed/optimal-control descent.** It works
  (−0.1 m/s touchdown) but it took ~12 trials of hand-tuned thresholds (P16). It is reactive
  proportional control, NOT a Falcon-9-style precomputed optimal trajectory; robustness to a new
  lander mass/thrust is not guaranteed without re-tuning.
- **Visual/structural realism is functional, not faithful.** The `render()` craft fly but do not look
  like Starship/Orion/SLS. Diameter steps (0.625 m probe → 1.25 m reaction wheels → 2.5 m tanks) lack
  fairings/nose cones. The one genuinely realistic craft (the 238-part, 901 t MUNSHIP) reached orbit
  but could NOT land because its custom staging never activated a descent engine (P13) — realism and
  controllability are currently in tension.
- **TMI has run-to-run variance** (encounter periapsis 168 km … 2.18 Mm). It is made *robust* by
  acceptance bands (P10/P11), not made *precise*. A tighter node-selection + correction loop is still
  open work.
- **No single end-to-end live run.** The proven path is the four per-phase drivers; the unified
  `_run_artemis` orchestrator predates the render-lander landing fix and has not been re-validated
  end-to-end. "Mission complete" = four chained phase successes, not one continuous flight.
- **Estimator ignores accessory mass** (P15) — handled by a TWR margin, but it means the design's own
  Δv/TWR numbers are optimistic; always treat them as lower-confidence upper bounds.
- **A high (Mun-)stationary relay orbit is physically impossible in this environment** (sync radius
  2.97 Mm > SOI 2.43 Mm); the project substitutes a high elliptical relay orbit. Some "ideal"
  geometries are simply unreachable and must be detected and substituted, not chased.

---

## 6. How to generalize to other bodies / goals

The architecture, the loop, the craft contract, and the failure taxonomy are **invariant**. What
changes is a parameter block read live from the environment.

**Re-read per body (never from memory):** μ (gravitational parameter), radius, SOI radius, surface g,
rotation/sidereal period, atmosphere presence + top altitude. (KSP reads these from kRPC; for real
flight, from the mission's reference ephemeris.)

**Recompute from those constants** (formulas are environment-independent):

- Parking-orbit circular speed `v = sqrt(μ/r)`; vis-viva `v = sqrt(μ(2/r − 1/a))`.
- Transfer Δv via Hohmann seed `a = (r_park + r_target)/2`; phase-angle lead from transfer time
  `t = π·sqrt(a³/μ)`.
- Capture Δv from v-infinity and target periapsis; finite-burn lead `= burn/2 + settle + command_delay`.
- Suicide-burn distance `d = v²/(2·(T/m − g_local)) + v·command_delay + margin`, scaled by the body's
  surface g (a higher-g or atmospheric body changes both the descent budget and the trigger altitude).

**What shifts with the destination:**
- **Δv budget** scales with the target's depth in the gravity well + transfer energy (KSP Mun ≈ 5.5 km/s
  round-trip; size craft Δv well above the two-body ideal to cover gravity/steering/finite-burn/drag
  losses).
- **TWR target** rises with launch-body surface g and atmosphere; keep the est-TWR ≥ ~1.4 margin (P15)
  regardless.
- **Atmosphere present?** changes ascent (gravity-turn profile, drag, fairings), enables aerocapture/
  parachute recovery, and forbids it on airless bodies (airless = pure powered descent + suicide burn,
  no chute landing).
- **Stationary-orbit feasibility** depends on whether sync radius < SOI; if not, substitute a high
  elliptical / constellation orbit (P10-style functional-band thinking).
- **Reentry hardware** is required only when the return body has atmosphere; gate it on the return
  requirement, not on crew (P14).

**What stays the same across ANY body/goal:**
- The CALCULATE→GENERATE→FLY-ONE-PHASE→DIAGNOSE→FIX-ONE→RECORD→RETRY loop and its persisted state
  (Section 1).
- The craft-generation contract (Section 3): control source, real serialization, separation math one
  stage late, TWR margin, inline reaction wheels, passive aero, requirement-gated hardware.
- The phase state machine and its gating markers (Section 4); each phase still gates the next.
- The 16 engineering principles (Section 2) — they are about controllers, serialization, attitude,
  staging, throttle profiles, and data altitude, none of which depend on which body you orbit.
- Diagnose from per-component, correct-frame, thrust-verified data; never patch the controller in the
  terminal seconds; promote every failure into the permanent rule ledger.

---

### One-paragraph agent prompt-summary

To autonomously fly a rocket to an orbit/landing/return goal: read the body constants live; compute
Δv/TWR/torque/burn-timing before building anything; generate a launch-safe craft (probe control,
real serialization with no extraneous headers, separators one stage late, est TWR ≥ 1.4, inline
reaction wheels, passive fins, requirement-gated heat shield/legs/descent engine); fly ONE phase at a
time via a resumable driver; gate each phase on its success marker; diagnose only from per-component,
correct-reference-frame, thrust-verified telemetry; fix exactly one root cause per iteration; never
edit the controller during terminal descent; and append every failure to a permanent rule ledger that
is the real, compounding product of the work.
