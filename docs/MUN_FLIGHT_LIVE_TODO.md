# Mun land-and-return: LIVE-flight staging TODOs

## ✅ FLOWN LIVE AND RECOVERED (2026-06-27)

A crewed Mun land-and-return was flown end-to-end against the live game and the kerbal was **recovered
alive at Kerbin**: `launch → LKO → trans-Mun injection → Mun capture → Mun landing → ascent →
trans-Kerbin return → reentry → recovered` (`fly_mun_roundtrip.py`, 7/7 steps, `complete=True`). The
six watch-items below were all exercised live. Seven real bugs surfaced and were fixed (each verified
in the live game, not just offline):

1. **Launch flew an LKO-only craft.** `primitives.launch` sized the full-mission rocket for the design
   gate but `deploy_relay.launch_to_lko` re-derived and flew a 2-phase LKO craft, so `mission_dv`/legs
   never reached the flown vehicle. Fix: thread `mission_dv` + `needs_legs` through `launch_to_lko`
   (appends the vacuum `mission` phase + legs). *Live: the flown craft now carries ~4000 m/s + legs.*
2. **Mun legs used the heliocentric Eve/interplanetary driver** (`deploy_relay_transfer.transfer_to_body`
   ejects to a **Sun** orbit and cannot reach a body inside Kerbin's SOI, nor return from one). Fix:
   route the Mun outbound + the Mun→Kerbin return through the proven `flight_controller` Mun machinery
   (`_transfer_and_capture_mun_orbit`, `_find_kerbin_return_node`/`_execute_node`/`_coast_to_kerbin_soi`);
   `land`→`_land_on_mun`, `recover`→`_recover_on_kerbin`.
3. **The lander engine's exhaust fired into its own interstage shroud** (same vessel → net thrust ZERO;
   the LV-N made `g_force=0` at full throttle). Fix: the kept **legged lander** engine is left a bare
   bell — `craft_writer` skips the shroud for it and the design-chart gate exempts it (`lander_base_engine`).
4. **Capture burn falsely aborted (`no_actual_thrust`)** — `vessel.thrust` reads 0 for several seconds
   after a high-rails-warp de-warp even while the engine fires. Fix: both no-thrust guards
   (`_capture_mun_orbit`, `_execute_mun_apsis_node`) also accept orbital-**energy** (semi-major-axis)
   change as proof of thrust (only thrust changes it; a truly blocked engine keeps it constant).
5. **Nuclear LV-N on LFO tanks → fuel starvation.** The LV-N burns LiquidFuel only, so ~55% of an LFO
   tank is dead Oxidizer it can never use and the LF runs out at ~half the calculated Δv — the lander
   starved at 13 km mid-descent and crashed. Fix: exclude oxidizer-less engines from the all-LFO engine
   pool (`design._OXIDIZERLESS_ENGINES`); a chemical (Terrier) lander uses its propellant fully.
6. **Flag plant blocked the whole mission.** The Mk1 hatch is obstructed by the heat shield below it, so
   `/eva-flag` returns null. Fix: `plant_flag` is a **bonus** step — marked `optional`, a failure logs and
   the mission presses on to the ascent + return rather than stranding the crew.
7. **Reentry periapsis too shallow.** The trans-Kerbin return leaves a ~48 km periapsis that barely
   touches the air and skips out for hundreds of passes (MechJeb won't deorbit a craft already "in" the
   70 km atmosphere). Fix: `recover` lowers a too-high periapsis to a real corridor (~25 km) at apoapsis
   before handing the descent to MechJeb.

Re-run end to end with `PYTHONPATH=src python tools/fly_mun_roundtrip.py configs/local-ksp.yaml`, or
resume a single leg against the live vessel with `--from-step N` (1-based).

---

The launch **DESIGN** is now mission-aware (`primitives.launch` + `_launch_requirements` take
`mission_dv` + `needs_legs`; `tools/fly_mun_roundtrip.py` derives them from the mission graph). That
sizes **one** vehicle with enough Δv + landing legs + heatshield/chutes for the whole Mun round-trip,
and the three-view geometry gate passes on it.

What that does **NOT** fix are the *flight-staging* gaps: whether the live craft actually **uses** that
upper-stage Δv through the legs/heatshield as it transfers, lands, ascends and recovers. Those depend on
MechJeb's live staging + the real fuel state and are **not unit-testable offline** — they were flagged
`unit_fixable: false` in the read-phase maps. Do **not** blind-rewrite the flight code. Instead, the next
**live** session must watch these, in order, and tune the flight controllers against what the game does.

## Watch-list (each item = one thing to verify LIVE, with the file to tune if it's wrong)

1. **Upper stage is actually used for TMI + capture.**
   After `launch` reaches LKO, the oversized `mission` stage (carrying ~4000 m/s) must be the stage that
   burns the Mun TMI and capture. Risk: `launch_to_lko` exits at circularize and the booster is force-
   separated, but the `transfer` primitive must then fire the **upper** (mission) stage, not a spent one.
   - Watch: at TMI, is the burning engine the mission-stage (vacuum) engine, with full tanks?
   - Tune in: `tools/deploy_relay.py` (ascent-complete force-separation, ~lines 498–563) and
     `src/ksp_lab/astra/primitives.py:transfer` / `tools/deploy_relay_transfer.py`.

2. **MechJeb stages THROUGH the legs (does not auto-stage them or the heatshield away).**
   With legs + heatshield now on the bus, confirm MechJeb's `autostage` (and the manual inter-stage
   decouple) does not jettison legs/heatshield/chutes during ascent or the TMI burn. The launch path runs
   `autostage=False` + explicit decouple; verify the explicit decouple fires only the **inter-stage**
   decoupler, never the leg/heatshield/chute parts.
   - Watch: after each separation, are legs + heatshield + chutes still attached to the crew bus?
   - Tune in: `tools/deploy_relay.py` manual-decouple (`_guarded_decouple`, bottom-dry trigger,
     ~lines 498–545) and `src/ksp_lab/craft_writer.py` decoupler inverse-stage assignment.

3. **`land` on the Mun uses the legs and lands propulsively (no atmosphere).**
   The `land` primitive falls to MechJeb landing autopilot / the gentle-descent fallback. Confirm it
   deploys the legs before touchdown and lands on the mission-stage's remaining Δv (the chutes do nothing
   on an airless body — they are for the Kerbin return only).
   - Watch: legs deployed before contact; touchdown < ~6 m/s; mission stage still has Δv for ascend.
   - Tune in: `src/ksp_lab/astra/primitives.py:land`, `src/ksp_lab/eve_flag_mission.py` descent,
     `src/ksp_lab/bridge_client.py:mj_land`.

4. **`ascend` from the Mun has the Δv the graph assumed (~671 m/s) AFTER land.**
   `flight_controller._launch_from_mun` hand-flies the surface ascent on remaining fuel. The mission
   stage must still hold ≥ the ascend budget after TMI + capture + descent (grid-search corrections can
   eat 200–500 m/s in practice). If it aborts mid-ascent, the post-LKO budget margin
   (`POST_LKO_MARGIN_FRAC` in `tools/fly_mun_roundtrip.py`, currently 5%) is too small — raise it and
   re-design.
   - Watch: remaining Δv at the moment `ascend` starts vs. the ~671 m/s budget.
   - Tune in: `src/ksp_lab/flight_controller.py:_launch_from_mun`; `POST_LKO_MARGIN_FRAC`.

5. **Kerbin return transfer + capture from the heavier crewed stack.**
   `transfer(target_body="Kerbin")` wraps the relay-sized machinery; the crewed stack (heatshield +
   chutes + pod) is heavier, so the return ejection costs more than a relay's. Confirm the mission stage
   still has the return + a capture/aerocapture margin.
   - Watch: Δv remaining at the return burn vs. the graph's ~1221 m/s return budget.
   - Tune in: `src/ksp_lab/astra/primitives.py:transfer`; consider raising `POST_LKO_MARGIN_FRAC`.

6. **`recover` finds the heatshield + chutes still attached and functional.**
   `recover` (crewed_eve_roundtrip.descend_and_recover) assumes a forward heatshield + chutes. After all
   the staging above, verify nothing decoupled them. If they were lost, the crew dies on re-entry — abort
   and fix the decouple inverse-staging (item 2) rather than flying.
   - Watch: heatshield forward + chutes present before atmospheric entry; chute deploy guard fires.
   - Tune in: `src/ksp_lab/astra/primitives.py:recover`, `src/ksp_lab/crewed_eve_roundtrip.py`.

## If Δv falls short in flight
Raise `POST_LKO_MARGIN_FRAC` in `tools/fly_mun_roundtrip.py` (5% -> 8–10%), re-run, and re-check the
design gate via `tools/design_chart.py` / `tests/test_mission_aware_design.py` (the estimated total Δv
assertion). The design path will re-size the vehicle bigger; the geometry gate must still pass.
