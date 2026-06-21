---
name: rendezvous-and-docking
description: Same-body rendezvous, RCS proximity ops, docking-port mate, and crew transfer between two orbiting craft — Hohmann phasing to close the angle, close approach, null lateral drift, mate ports (merge = crew transfer), then undock.
---

# Rendezvous and Docking

> **PREFERRED METHOD — delegate to MechJeb. Do not hand-roll rendezvous/docking guidance.** MechJeb's
> Rendezvous + Docking autopilots fly the whole phasing → approach → port-alignment → mate sequence
> reliably; the agent only sets the target, the params, and starts them. Driver:
> `tools/fly_mj_dock.py configs/local-ksp.yaml <CHASER> <TARGET>`. Endpoints: `BridgeClient.mj_rendezvous`,
> `mj_dock`, `mj_status`. Requires the MechJeb-for-all patch + a rebuilt bridge — see
> [docs/USING_KRPC_AND_MECHJEB.md](../docs/USING_KRPC_AND_MECHJEB.md). This is the architecture: the LLM
> orchestrates, MechJeb/kRPC do the control. The hand-rolled method below is retained only as a
> fallback / teaching reference and never reliably mated two ports.

Bring a chaser (e.g. Orion) to a target (e.g. parked HLS) in the SAME body's orbit, dock, transfer
crew, undock. Both craft must be built `docking_port=True` (Clamp-O-Tron `dockingPort2` + `RCSBlock`
+ RCS tank — see `craft-design`).

## Hand-rolled fallback (legacy; superseded by MechJeb)

Primitives: `run_dock_and_transfer(...)`, `_approach_and_dock(...)`, `_first_docking_port(...)` in
`flight_controller.py`. Driver: `tools/fly_dock.py`.

> Honest scope: the hand-rolled path got a chaser to ~2 m but NEVER mated the ports, across ~13 live
> attempts — the root cause was the kRPC autopilot silently rejecting the vessel-fixed reference frame
> (it needs a non-rotating frame), plus the passive target's port facing an arbitrary direction and
> weak RCS losing to orbital drift at close range. That is precisely the class of problem MechJeb
> already solves, which is why the MechJeb path above is now preferred.

## METHOD

1. **Phase to close the angle (same-body Hohmann).** If the chaser leads/trails the target, drop (or
   raise) into a phasing orbit whose period differs, so the angular gap closes, then circularize back
   onto the target's orbit when the gap is ~0. Δv and timing from the Hohmann/vis-viva math
   (`guidance.hohmann_*`, `vis_viva_speed_mps`): a slightly lower orbit has a shorter period and
   catches up; a higher one falls behind.
2. **Set target + enable RCS.** `sc.active_vessel = chaser`; `sc.target_vessel = target`;
   `chaser.control.rcs = True`, `sas = False`. Marker `dock_rendezvous_start`.
3. **Read relative state in the chaser frame** (`_approach_and_dock`): `ref = chaser.reference_frame`;
   `rel_pos = target.position(ref)`, `rel_vel = target.velocity(ref)`;
   `distance = ||rel_pos||`.
4. **Point the docking axis at the target.** `ap.reference_frame = ref`;
   `ap.target_direction = rel_pos`; `ap.engage()`.
5. **Translate in with RCS, null lateral drift.** Desired closing speed
   `v_des = clamp(distance·0.08, 0.3, 3.0)` m/s (slow when near). Drive relative velocity toward
   `unit(rel_pos)·v_des`: `err = unit·v_des − rel_vel`; command
   `control.right = clamp(err_x·0.5, ±1)`, `control.up = clamp(err_z·0.5, ±1)`,
   `control.forward = clamp(err_y·0.5, ±1)`. Axes: forward = +y (nose), right = +x, up = +z. Phase is
   `dock_closing` (≥ 50 m) → `dock_final_approach` (< 50 m). Loop every 0.25 s.
6. **Mate the ports.** Docked when `chaser_port.state` (from `vessel.parts.docking_ports[0]`) ends in
   `"docked"`, or `distance < 0.4 m` → `dock_ports_mated`. KSP MERGES the two vessels on dock.
7. **Crew transfer.** Once merged, crew can move freely between docked modules — that IS the transfer
   → `dock_crew_transfer_complete`. To move specific kerbals explicitly, call the bridge endpoint
   `/transfer-crew` (move a kerbal between the now-merged parts); merge alone already satisfies the
   "crew went between vehicles" requirement.
8. **Undock** (`_undock_after_transfer`): `port.undock()` → `dock_undocked`; small back-away pulse
   (`control.forward = −0.5` for 2 s) → `dock_and_transfer_complete`.

## MATH

- Phasing period to close angle θ over N synodic cycles: pick `a_phase` so the period difference
  `ΔT` accumulates θ before re-circularizing; seed from `T = 2π·sqrt(a³/μ)` and
  `Δv = vis_viva(μ, r, a_phase) − v_circ(r)`.
- Closing speed schedule: `v_des = clamp(0.08·distance, 0.3, 3.0)` m/s.
- Translation gain: `control.<axis> = clamp(0.5·(unit·v_des − rel_vel)_axis, ±1)`.

## WORKED EXAMPLE

Chaser 5 km behind the target, both in a ~100 km Mun orbit (`v_circ ≈ 466 m/s`). Distance 5,000 m →
`v_des = clamp(0.08·5000, 0.3, 3.0) = 3.0 m/s` (capped) closing. At 40 m → `v_des = clamp(3.2,..)=3.0`,
phase `dock_final_approach`. At 8 m → `v_des = 0.64 m/s`; RCS nulls `rel_vel` lateral components to ~0.
`chaser.parts.docking_ports[0].state` reports `docked` at contact → ports mated → vessels merge →
crew transfer implicit → undock + back-away → `dock_and_transfer_complete`.

## SUCCESS / FAILURE MARKERS

- SUCCESS: `dock_ports_mated` → `dock_crew_transfer_complete` → `dock_and_transfer_complete`.
- `dock_not_completed` → never closed (started too far, or no docking port — check both craft were
  built `docking_port=True`); arrange matching/closer launch orbits, or do the phasing burn first.
- `dock_undock_failed` → port wouldn't release; benign for the transfer (crew already moved on merge).
- No `docking_ports` on a vessel → it wasn't built with `docking_port=True`; rebuild via `craft-design`.
