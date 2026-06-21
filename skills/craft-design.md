---
name: craft-design
description: The craft-generation contract — what makes a generated KSP1 vehicle actually launch and fly its profile (probe control, real serialization, decoupler one stage late, est TWR ≥ ~1.4, inline reaction wheels, passive fins, requirement-gated heat shield/legs/docking port) plus the Δv/TWR/staging math.
---

# Craft Design (the generation contract)

A generated craft that violates any clause below either won't launch or won't fly its profile. Entry
point: `CraftWriter.render(design)` / `.write(design, save_vab_dir, template_path=None)` in
`src/ksp_lab/craft_writer.py`; mass/Δv/TWR estimate: `estimate_design(design)` in
`src/ksp_lab/parts.py`. Architecture defaults: `src/ksp_lab/artemis.py`.

## THE CONTRACT (each clause is a hard-won rule)

1. **Controllable command source (P1).** Root part is a probe core with electric charge —
   `probeCoreOcto.v2` (0.1 t) — independent of crew. Crew analogues fly `crewed=False` so the
   headless launch path works (a bare crewed pod blocks launch: "no crew/probe core" prompt a script
   can't dismiss). Marker if violated: `object reference not set` / no flight scene.
2. **Real serialization, no extraneous headers (P2).** `render()` MUST omit the top-level
   `ACTIONGROUPS{}` block and `Override*` header fields (real authored craft omit them; a malformed
   ACTIONGROUPS NullReferenced every generated craft). Splice each part's real `MODULE`/`RESOURCE`
   body from stock VAB craft; minimal stub only offline. Marker if violated: `NullReference` at launch
   finalization.
3. **Decoupler one stage late (P3).** Each inter-stage decoupler activates exactly one inverse-stage
   LATER than the stage it tops: `inverse_stage = render_index − 1` (`craft_writer.py` ~line 338:
   `new_node("Decoupler.1", max(0, render_index - 1))`). It fires AFTER its engine, not during.
   Launch + transfer stages each get a decoupler; the bus/lander stage stays attached. Keep crossfeed
   off so each stage drains independently and autostage is predictable. Marker if violated:
   `split` / decoupled on the pad.
4. **TWR margin for un-estimated mass (P15).** Target est launch TWR ≥ ~1.4 because the estimator
   ignores accessories (heat shield, reaction wheels, nose, chute, fins, probe ≈ 0.5–0.8 t), so actual
   TWR runs below the estimate. Lightening upper stages (transfer Rockomax32→16, service 3→2 tanks)
   lifted est TWR 1.24→1.55 and it flew. Relay/HLS/Orion all launch at est TWR ≥ 1.44. Marker if
   violated: `ascent_stuck_on_pad` (apoapsis stuck ~89 m).
5. **Attitude authority built in (P8).** ~3 inline `asasmodule1-2` reaction wheels (~15 kN·m total)
   in the bus for any heavy upper stack — mounted INLINE, never surface-clipped. This single fix took
   relay SOI-arrival from ~50% to reliable. `render()` adds them by default.
6. **Passive aero stability (P5).** Passive `basicFin` (0.01 t) at the 4 cardinal positions low on the
   booster so CoP sits behind CoM; NOT active `R8winglet` under a weak probe autopilot (over-rotates,
   tumbles).
7. **Requirement-gated hardware (P13/P14).** Landing legs (`landingLeg1`) + a descent engine that is
   the active stage after jettison for landers; `HeatShield1` + parachute when `crewed OR heatshield`
   for return craft; relay antenna/solar/battery radially mounted. Gate on the REQUIREMENT, not crew —
   the uncrewed Orion carries `heatshield=True`.
8. **Estimable budget.** Every part has mass/cost in `STOCK_PARTS` so Δv/TWR are meaningful.

`render()` flags on `RocketDesign` (models.py): `crewed`, `heatshield`, `docking_port`. A docking
craft (relay/HLS/Orion that must dock) sets `docking_port=True` → render adds a `dockingPort2`
(Clamp-O-Tron) + `RCSBlock` + RCS tank.

## THE MATH

- **Δv per stage (rocket equation):** `Δv = Isp · g0 · ln(m_wet / m_dry)`, `g0 = 9.80665`. Sum over
  stages for total. (`parts.estimate_design`.)
- **Launch TWR:** `TWR = thrust_asl_N / (m_total_wet_kg · g_launch_body)`. Kerbin `g = 9.81`. First
  stage must give est TWR ≥ ~1.4 → real ≥ ~1.05 after accessory mass.
- **Staging:** `n` engine stages → decouplers at inverse-stage `render_index − 1` for stages 1..n−1;
  the final (lander/bus) stage has NO decoupler above it. Autostage drops a stage when available
  thrust < 1 N AND a lower fueled engine exists (see `ascent-to-orbit`).

## WORKED EXAMPLE (Orion return craft)

- Probe core root (`crewed=False`), `heatshield=True` (return requirement), `docking_port=True`.
- est launch TWR ≈ 1.55 (the value the project flies Orion at — comfortably over the 1.4 floor).
- Stages: booster (decoupler at inverse-stage 0) → transfer (decoupler at inverse-stage 1) → service
  bus + Terrier (NO decoupler, stays with the capsule). 3× `asasmodule1-2` inline in the bus.
- `HeatShield1` + `parachuteSingle` on the capsule so reentry survives (−683 m/s entry, recovered at
  −1.4 m/s in the proven run).
- Sanity TWR: if total wet ≈ 18 t and booster thrust ≈ 280 kN → `280000 / (18000·9.81) ≈ 1.59` ✓.

## SUCCESS / FAILURE MARKERS

Good: the craft loads, launches headless, and reaches the parking target. Bad (and their fixes via the
contract above): `ascent_stuck_on_pad` (clause 4), `NullReference`/won't launch (clause 2), `split`
(clause 3), no-control/`object reference` (clause 1), tumble (clause 6), capture wasted >1 km/s
(clause 5), no heat shield on return (clause 7). If a generated craft can't be made launch-safe in
time, the documented fallback is to seed from a known-loadable authored craft and graft a complete
probe core — but generated-and-launch-safe is strictly preferred (identical staging for design model
and live craft).
