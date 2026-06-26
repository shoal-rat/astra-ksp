# Mun land-and-return: LIVE-flight staging TODOs

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
