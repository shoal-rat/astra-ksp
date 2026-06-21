---
name: astra-skills-index
description: Index of the ASTRA skill playbook and the intended runtime flow — start at 00-orchestration, then dispatch the per-phase skills in flight order.
---

# ASTRA Skills — the LLM mission playbook

ASTRA is an LLM-native autonomous Kerbal Space Program 1 mission agent. At runtime it receives ONE
line of natural language and flies a full spaceflight mission by following these skills and calling
the project's Python flight primitives (`src/ksp_lab/flight_controller.py`, `guidance.py`) through the
proven single-phase drivers (`tools/fly_*.py`). The skills are LLM context: exact numbers, formulas,
primitive names, and telemetry markers over prose.

Ground truth (read before flying): `docs/GENERALIZED_AEROSPACE_METHODOLOGY.md`,
`src/ksp_lab/flight_controller.py`, `src/ksp_lab/guidance.py`, `src/ksp_lab/astra/{agent,ledger,
knowledge,interpreter}.py`.

## Files

| skill | one line |
|---|---|
| `00-orchestration.md` | Top-level loop: parse NL → decompose phases → dispatch executor → diagnose failure vs ledger → fix ONE root cause → retry to the success marker → record. PM/executor split. **Start here.** |
| `mission-planning.md` | NL → target body, ordered capabilities, Δv budget, crew/return requirement. |
| `craft-design.md` | Craft-generation contract: probe control, real serialization, decoupler inverse-stage = render_index−1, est TWR ≥ ~1.4, inline reaction wheels, passive fins, requirement-gated heat shield/legs/docking port; Δv/TWR/staging math. |
| `ascent-to-orbit.md` | Gravity-turn ascent to a Kerbin parking orbit; measured-thrust autostage; body-frame liveness. |
| `trans-munar-injection.md` | TMI: Hohmann seed, ~3-period phase-angle search, stage mid-burn, re-align don't flip. |
| `soi-capture.md` | Retrograde-on-gimbal capture; relay-band high orbit vs low lander capture; grazing-periapsis correction. |
| `hoverslam-landing.md` | Falcon-9 freefall → ignite on v_ref(h) curve → brake on surface-retrograde → terminal flare < 70 m. |
| `rendezvous-and-docking.md` | Same-body phasing, RCS proximity ops, docking-port mate (merge = crew transfer), undock; `/transfer-crew` bridge endpoint. |
| `return-and-reentry.md` | Mun ascent → trans-Kerbin injection → coast to Kerbin SOI → reentry behind a heat shield → parachute recovery. |

## Runtime flow

```
NL line ─▶ mission-planning ─▶ ordered capabilities
                                   │
   00-orchestration drives the loop, per capability, in flight order:
   relay ─▶ hls_land_return ─▶ crew_return
                                   │
   each capability is a phase state machine (craft-design feeds the executor up front):
   ascent-to-orbit ─▶ trans-munar-injection ─▶ soi-capture
        ├─ relay:            soi-capture(relay band) ─▶ relay shaping ─▶ artemis_mun_relay_deployed
        ├─ hls_land_return:  ─▶ hoverslam-landing ─▶ surface science ─▶ Mun ascent ─▶ artemis_hls_returned_to_mun_orbit
        └─ crew_return:      ─▶ (rendezvous-and-docking) ─▶ return-and-reentry ─▶ recovered
                                   │
   on a failure marker: diagnose vs the experience ledger (astra/ledger.py SEED_RULES),
   fix ONE root cause, retry (bounded), record every attempt to runs/astra_experience.jsonl.
```

Terminal success chain: `artemis_mun_relay_deployed` → `artemis_hls_parked_in_mun_orbit` →
`mun_landed` → `mun_surface_science_completed` → `artemis_hls_returned_to_mun_orbit` →
`artemis_orion_waiting_in_mun_orbit` → `recovered`.

## Notes on two phases the skills reference but the driver handles inline

- **Surface science** (`_perform_mun_surface_science`): once `situation == landed` on the Mun, trigger
  every science-keyword part event; if none exist, record a modeled crew surface sample →
  `mun_surface_science_completed`.
- **Relay orbit shaping** (`_shape_mun_relay_orbit`): after relay-band capture, raise apoapsis to the
  target (~1,000 km) and periapsis (~200 km) within the band [700–2,150 km apoapsis, ≥ 120 km
  periapsis] → `artemis_mun_relay_deployed`. (Mun-stationary is impossible: sync radius 2.97 Mm > SOI
  2.43 Mm — a high elliptical orbit is substituted.)
