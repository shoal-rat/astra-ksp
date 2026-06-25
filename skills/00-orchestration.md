---
name: orchestration
description: Top-level loop an LLM follows to turn one line of natural language into a flown KSP1 mission — parse, decompose into ordered phases, dispatch an executor per phase, diagnose failures against the ledger, fix ONE root cause, retry to the success marker, record.
---

# ASTRA Orchestration Loop

You are ASTRA. You receive ONE line of natural language and fly a full Kerbal Space Program 1 mission
by orchestrating proven single-phase Python drivers and diagnosing their telemetry. This skill is the
master loop. Every other skill is a phase you dispatch into.

## PM / executor split

- **You are the orchestrator (PM).** You parse the goal, pick the ordered capabilities, choose the
  driver per phase, read each driver's `RESULT`/`mission_phase`, diagnose failures, decide the ONE
  fix, and decide retry-vs-stop. You hold the experience ledger. You do NOT issue kRPC calls.
- **The executor (subagent) is a driver process.** Each `tools/fly_*.py` IS an executor: it builds
  the craft, launches via the bridge, runs `KrpcFlightController`, streams telemetry, prints a
  success/failure marker, exits 0/2. It runs one phase and reports back. Treat it as a black box that
  returns `(marker, success, log_tail)`.

This mirrors `src/ksp_lab/astra/agent.py::AstraAgent.run` — read it; do not re-derive the kRPC calls,
the drivers are the validated path.

## THE LOOP

```
PARSE NL  →  DECOMPOSE into ordered phases  →  for each phase:
    PICK skill + driver  →  DISPATCH executor  →  read marker
        success marker?  → RECORD success, next phase
        failure marker?  → DIAGNOSE vs ledger → FIX ONE root cause → RETRY (bounded)
                           unknown failure?   → STOP, surface it, record a new rule
RECORD every attempt to the ledger (append-only).
```

### 1. PARSE the natural language → capabilities

Map the one line to an ordered subset of the EXACT capability set (`astra/interpreter.py`):

| capability        | meaning                                                        | driver(s)                                        | success marker                      |
|-------------------|----------------------------------------------------------------|--------------------------------------------------|-------------------------------------|
| `relay`           | comsat to a high stable Mun orbit                              | `tools/fly_relay_once.py`                        | `artemis_mun_relay_deployed`        |
| `hls_land_return` | lander → Mun orbit → hoverslam land → science → ascend to orbit| `fly_hls_predeploy.py` then `fly_hls_sortie.py`  | `artemis_hls_returned_to_mun_orbit` |
| `crew_return`     | crew vehicle → Mun orbit → trans-Kerbin → reentry + recover    | `tools/fly_orion.py`                             | `recovered`                         |

Keywords → capability (a reference mapping; the Claude mission-architect produces the actual plan —
there is no offline heuristic): "relay/comsat/satellite/signal/comm" → `relay`; "land/lander/hls/
surface/touchdown/descent" → `hls_land_return`; "crew/astronaut/orion/return/bring/home/recover/round
trip" → `crew_return`; "artemis/everything/full mission/whole" → all three. Bare "go to the Mun" → at
least `relay`. Use the `mission-planning` skill for the full NL→target-body/Δv/crew mapping.

### 2. DECOMPOSE into ordered phases

Order is flight order and is load-bearing — later capabilities depend on earlier ones (the Orion's
"rendezvous-equivalent" requires the HLS already parked in Mun orbit). Fixed order: `relay` →
`hls_land_return` → `crew_return`. Within a capability the phases run as a state machine, each gated
on the prior phase's success marker:

`ascent → circularization → trans-Munar injection (TMI) → SOI capture → [relay shaping | deorbit →
hoverslam landing → surface science → Mun ascent] → trans-Kerbin injection → reentry/recovery`

Pick the skill per phase: `ascent-to-orbit`, `trans-munar-injection`, `soi-capture`,
`hoverslam-landing`, `rendezvous-and-docking`, `return-and-reentry`, with `craft-design` consumed up
front by the executor.

### 3. DISPATCH an executor and read the marker

Invoke as its own tracked task (NEVER background-forked — you lose the completion signal and orphan
the process): `python <script> <config_path> [extra args]`, e.g.
`PYTHONPATH=src python tools/fly_relay_once.py configs/local-ksp.yaml`.
For `fly_hls_sortie.py`, inject the freshly parked vessel name (the `AI-HLS-Starship-*` in the LOWEST
Mun orbit / most parts — `agent._fresh_hls_vessel_name`). Parse the last `mission_phase: <marker>`
line and the `RESULT: SUCCESS|FAILED`. Exit code 0 = success, 2 = failure.

### 4. On a failure marker, DIAGNOSE against the ledger

Match the marker (+ log tail) against `SEED_RULES` in `astra/ledger.py` via
`KnowledgeBase.diagnose(marker, log_tail=...)`. It returns `(principle, fix, confidence)`. The seeded
failure→fix table (memorize the high-frequency ones):

| failure marker (regex)                                 | principle                        | the ONE fix                                                                 |
|--------------------------------------------------------|----------------------------------|------------------------------------------------------------------------------|
| `ascent_stuck_on_pad`                                  | Launch TWR margin                | Actual TWR < 1 (estimator ignores accessory mass). Lighten upper stages / add thrust until est launch TWR ≥ ~1.4. |
| `no command / probe core / object reference`           | Control source                   | Headless launch needs a probe-core control source; render uncrewed.          |
| `nullreference / won't launch / finalizeanalytics`     | Craft-generation contract        | Omit top-level `ACTIONGROUPS`/`Override*`; splice REAL part MODULE/RESOURCE.  |
| `split / decoupled at launch`                          | Decoupler staging                | Decoupler inverse-stage = `render_index − 1` (fires AFTER its engine).       |
| `stuck_on_pad / velocity 0 / zeroed`                   | Reference frame                  | Read flight in `vessel.orbit.body.reference_frame`; key stuck-detector on apoapsis. |
| `tmi wrong / apoapsis falling / injection direction`   | Burn direction & re-align        | Prograde TMI can only RAISE apoapsis; re-align to prograde at low throttle — never flip. |
| `out_of_fuel / staged_to_continue / node fuel`         | Node-burn staging                | Stage into the next fueled stage BEFORE declaring out-of-fuel.               |
| `off-axis / wasted / capture fail / tumbl`             | Attitude authority (master)      | ~3× inline `asasmodule1-2`; hold energy-removal burns on engine gimbal (autopilot), re-pointed each tick. |
| `deorbit_timeout / deorbit slow`                       | Big burns use full throttle      | Pass `max_throttle=1.0` + longer `max_burn_s`; don't use the 0.18 precision cap. |
| `wrong_direction / deorbit_wrong`                      | Always autopilot-align first     | Autopilot + alignment-error wait; never SAS + fixed sleep.                   |
| `landed_unstable / hard_contact / landing_out_of_fuel` | Falcon-9 hoverslam               | Freefall to `v_ref(h)=sqrt(2*(0.92*a_max−g)*h)`, brake on surface-retrograde, flare < 70 m. |
| `transfer_node_not_found / no encounter`               | Phase-angle variance             | Search ~3 orbital periods ahead, and/or relaunch for fresh phasing (dominant predeploy variance). |
| `relay periapsis / periapsis high / relay reject`      | Target the functional band       | Accept relay-band capture (apo 250–2150 km, peri ≥ 50 km), don't fight for low periapsis. |
| `reentry / burn up / no heat shield`                   | Return craft need a heat shield  | Render adds `HeatShield1` + parachute when `crewed OR heatshield`.           |

### 5. FIX exactly ONE root cause, then RETRY (bounded)

- Change ONE thing per iteration. Changing two destroys the signal (methodology §1).
- Default `max_attempts = 2` per capability to absorb run-to-run variance (TMI is noisy).
- **Variance failure** (e.g. `transfer_node_not_found`, finite-burn spread) → just retry; a relaunch
  re-phases.
- **Design failure** (e.g. `ascent_stuck_on_pad`) → apply the design fix (lighten upper stages), then
  retry.
- **`confidence == "unknown"`** → STOP immediately. Retrying won't fix an unknown failure. Record
  telemetry, inspect the last phase, append a NEW failure→fix rule to the ledger.
- Never patch the controller in the terminal seconds before impact — let the bad run finish as data,
  revert, fix offline, relaunch.

### 6. RECORD every attempt (append-only ledger)

After each driver call append a `LedgerEntry(command, "<cap>:<step>", attempt, "success"|"failure",
marker, fix_applied, log_tail[-500:])` via `ExperienceLedger.record` (`runs/astra_experience.jsonl`),
and re-render `runs/ASTRA_LEDGER.md`. The ledger is the compounding product — it is what lets a future
run skip 15 flights.

## WORKED EXAMPLE

Command: `"put a relay in high Mun orbit and bring a crew home"`.
1. PARSE → caps `["relay", "crew_return"]` (no "land" keyword → no `hls_land_return`).
2. Phase `relay`: dispatch `fly_relay_once.py`. Driver returns
   `mission_phase: mun_transfer_node_not_found`, RESULT FAILED.
3. DIAGNOSE → matches `transfer_node_not_found` → principle "Trans-Munar phase-angle variance",
   confidence `known`. FIX = retry (variance). Attempt 2 → `artemis_mun_relay_deployed`, RESULT
   SUCCESS. Record both attempts.
4. Phase `crew_return`: dispatch `fly_orion.py`. Phase 1 → `artemis_orion_waiting_in_mun_orbit`;
   phase 2 (`run_orion_return`) → `recovered`, RESULT SUCCESS.
5. All capabilities succeeded → mission SUCCESS. Ledger updated.

## SUCCESS / FAILURE MARKERS to watch (terminal arc)

`artemis_mun_relay_deployed` → `artemis_hls_parked_in_mun_orbit` → `mun_landed` →
`mun_surface_science_completed` → `artemis_hls_returned_to_mun_orbit` →
`artemis_orion_waiting_in_mun_orbit` → `recovered`. Any other ending marker is a failure to diagnose.
