# Using kRPC + MechJeb — the agent delegates control, it does not reinvent it

> **The single most important lesson of this project.** An LLM aerospace agent should not hand-roll
> guidance, navigation, and control. Two mature tools already solve that, and solve it far better
> than any heuristic an LLM will write under time pressure: **kRPC** (telemetry + orbital math +
> scripting) and **MechJeb** (full autopilots for ascent, rendezvous, docking, landing). The agent's
> job is to **split the mission, pick the right autopilot, set its parameters, start it, and watch
> the result** — then diagnose, retry, and record what it learned. It is a *loop with an experience
> notebook*, not a physics engine.

This is the architecture the rest of the repo is being migrated to. If you are an LLM continuing this
work: **reach for kRPC/MechJeb first; only write custom control when neither covers the case, and
even then prefer composing their primitives.**

---

## 1. The division of labour

```
        ┌─────────────────────────────────────────────────────────────┐
        │  LLM agent (Claude Code)                                      │
        │  • read the one-line goal → split into mission steps          │
        │  • research how it is normally done (KSP wiki, MechJeb docs)   │
        │  • design/choose a craft that can do each step                │
        │  • for each step: pick the autopilot, set params, START it    │
        │  • POLL status → diagnose failure → adjust → RETRY             │
        │  • write the lesson into the experience notebook              │
        └───────────────┬───────────────────────────┬─────────────────┘
                        │ (decide / orchestrate)     │ (record)
            ┌───────────▼──────────┐     ┌───────────▼───────────┐
            │  kRPC (Python API)   │     │  MechJeb (autopilots) │
            │  • live telemetry    │     │  • Ascent             │
            │  • orbit prediction  │     │  • Rendezvous         │
            │  • maneuver nodes    │     │  • Docking            │
            │  • vessel/part state │     │  • Landing            │
            │  • set target        │     │  driven via the       │
            │  • warp, staging     │     │  KspAutomationBridge  │
            └──────────┬───────────┘     └───────────┬───────────┘
                       └───────────────┬─────────────┘
                          live Kerbal Space Program 1
```

**kRPC** is for *knowing and measuring* (and small deterministic control: nodes, warp, staging).
**MechJeb** is for *flying* (the hard closed-loop control). The agent stitches them together and
decides *what to do next*; it does not compute thrust vectors.

---

## 2. kRPC — what to use it for (don't recompute what it gives you)

kRPC (`pip install krpc`, server is the in-game kRPC mod) exposes the live game over RPC. Connect:

```python
import krpc
conn = krpc.connect(name="agent", address="127.0.0.1", rpc_port=50000, stream_port=50001)
sc = conn.space_center
v = sc.active_vessel
```

Use its built-ins instead of re-deriving them:

| Need | kRPC gives you (use this) | Do NOT |
| --- | --- | --- |
| Closest approach to a target | `vessel.orbit.distance_at_closest_approach(target.orbit)`, `time_of_closest_approach(...)`, `list_closest_approaches(target.orbit, n)` | hand-integrate the two orbits |
| Position/velocity at a future time | `orbit.position_at(ut, frame)`, `orbit.radius_at(ut)` | propagate Kepler yourself |
| Relative position/velocity to target | `target.position(vessel.orbital_reference_frame)`, `target.velocity(...)` | difference state vectors by hand |
| Evaluating a candidate maneuver node | `node = vessel.control.add_node(ut, prograde, radial)` then `node.orbit.distance_at_closest_approach(...)` | guess burn outcomes |
| Live flight data | `vessel.flight(frame).{mean_altitude,vertical_speed,...}` | integrate accelerometer |
| Set the target (what MechJeb reads) | `sc.target_vessel = v` / `sc.target_docking_port = port` | — |
| Time warp / staging | `sc.rails_warp_factor`, `vessel.control.activate_next_stage()` | sleep-and-hope |

### kRPC gotchas learned the hard way
- **AutoPilot reference frame must NOT rotate with the vessel.** `ap.reference_frame =
  vessel.reference_frame` throws `ValueError: Invalid reference frame; must not rotate with the
  vessel`. Use a non-rotating frame (`vessel.orbital_reference_frame`, `surface_reference_frame`, or
  a body frame). We lost ~13 docking attempts to this being silently swallowed by a `try/except` —
  the autopilot never actually pointed. (This is *the* reason hand-rolled docking never mated.)
- RCS translation control (`control.right/up/forward`) IS in the vessel-fixed frame — that part is
  correct with `vessel.reference_frame`. Only the *autopilot pointing* needs the non-rotating frame.
- A passive target's docking port faces an arbitrary direction; you cannot dock to it by flying at
  the target's centre. (MechJeb handles this for you — another reason to delegate.)
- `node.orbit` editing requires the vessel to be the **active** vessel and **in flight**.

---

## 3. MechJeb — the autopilots, driven through the bridge

MechJeb 2 (the `MechJeb2` GameData mod) contains production-grade autopilots. We drive them from our
own in-game C# plugin, **KspAutomationBridge**, which is compiled against the *installed*
`MechJeb2.dll` (so the C# compiler validates every MechJeb member name — see §5). The agent calls
plain HTTP endpoints; the bridge enables the matching MechJeb autopilot on the main thread.

### Endpoints (POST JSON to `http://127.0.0.1:48500`, all values as strings)

| Endpoint | What it does | Body fields (defaults) |
| --- | --- | --- |
| `POST /mj-rendezvous` | Enable MechJeb Rendezvous autopilot on the active vessel toward `target` | `target`, `desiredDistance`(100), `maxPhasingOrbits`(5), `maxClosingSpeed`(100) |
| `POST /mj-dock` | Set the target's docking port + enable MechJeb Docking autopilot | `target`, `speedLimit`(1.0), `forceRol`(false) |
| `POST /mj-disable` | Stop an autopilot | `which` = `dock`/`rendezvous`/`all` |
| `GET  /mj-status` | Poll: `hasCore`, `targetExists`, `dockEnabled`, `dockStatus`, `rvEnabled`, `rvStatus`, `partCount`, `myPortState` | — |

Python wrappers live in `BridgeClient` (`mj_rendezvous`, `mj_dock`, `mj_status`, `mj_disable`). The
reference flow is `tools/fly_mj_dock.py`:

```
set chaser active (kRPC)  →  if far: /mj-rendezvous, poll until rvEnabled=false
                          →  /mj-dock, poll /mj-status until partCount jumps (vessels merge on dock)
                          →  /transfer-crew
```

### How the bridge enables a MechJeb autopilot (the canonical idiom)

```csharp
MechJebCore core = vessel.GetMasterMechJeb();              // null if no MechJeb on the craft (see §4)
var dock = core.GetComputerModule<MechJebModuleDockingAutopilot>();
dock.speedLimit = 1.0;                                     // EditableDouble params (lowercase fields)
FlightGlobals.fetch.SetVesselTarget(targetDockingPort);   // MechJeb reads core.Target every tick
dock.Users.Add(core);                                     // ENABLE = add a user to the UserPool
// ... it flies itself over many FixedUpdates ...
// completion: dock.Users.Count == 0 AND the vessel part-count jumped (a real couple), else it aborted
```

### MechJeb gotchas learned the hard way
- **`GetMasterMechJeb()` is null without a MechJeb on the craft.** Stock has no part-free mode. Ship
  the `MechJebForAll.cfg` ModuleManager patch (adds `MechJebCore` to every command pod) and reload.
- **Everything MechJeb runs on the Unity main thread.** The HTTP server is a worker thread — never
  touch MechJeb from it. The bridge marshals every MechJeb call through `RunOnMainThread`.
- **Completion is overloaded.** The docking AP disables itself on success, on target-lost, *and* on
  the couple event. `!enabled` alone is not "success" — confirm a physical dock (part-count jump or
  the port's `state` containing `Docked`).
- **Set a docking-port target, not the bare vessel** — the port gives MechJeb the docking axis.

---

## 4. The MechJeb-for-all patch

`csharp/KspAutomationBridge/MechJebForAll.cfg` (installed to `GameData/KspAutomationBridge/`):

```
@PART[*]:HAS[@MODULE[ModuleCommand],!MODULE[MechJebCore]]:FINAL { MODULE { name = MechJebCore } }
```

ModuleManager applies it to part prefabs at load, so even vessels already saved in flight gain a
`MechJebCore` when the save is reloaded after install. Without it, our render()-generated craft
(which carry no AR202 MechJeb part) cannot be driven by MechJeb.

---

## 5. Why we compile the bridge against the installed MechJeb2.dll

There is a prebuilt `KRPC.MechJeb` extension that exposes MechJeb to kRPC as `conn.mech_jeb`. We do
**not** use it: it binds to MechJeb by *reflection on string names* and, on a MechJeb dev build where
a member was renamed, it does not crash — it silently disables that procedure. Silent loss of
docking is the worst failure for an automation lab.

Instead the bridge **hard-references the installed `MechJeb2.dll`**. A renamed member then becomes a
*compile error*, not a silent runtime no-op. This is exactly how we discovered this dev build's
casing in minutes: the compiler rejected `core.target` (→ `core.Target`), `EditableDouble.val`
(→ the readonly field's `.Val` property), and confirmed the `Users` pool is PascalCase. **When you
update MechJeb and the bridge fails to build, that is the system working** — read the error, fix the
member name, rebuild (`scripts/build_bridge.ps1 -KspRoot "<KSP>"`), reinstall the DLL, reload KSP.

---

## 6. The LLM-native loop (what the ASTRA agent is)

```
receive one-line goal
  → split into mission steps (ascent, TMI, capture, rendezvous, dock, land, return…)
  → for each step:
        research how it's normally done (KSP wiki / MechJeb docs / past experience notebook)
        choose/﻿design a craft that can do it (craft_writer + estimate_design)
        pick the autopilot:  ascent→MechJeb ascent · phasing→MechJeb rendezvous ·
                             mate→MechJeb dock · touchdown→MechJeb landing ·
                             measurement/decisions→kRPC
        START it, then POLL kRPC/​/mj-status for the outcome
        if it failed: diagnose from telemetry, adjust ONE thing, retry
  → record the lesson (what failed, the fix) in the experience notebook
  → repeat until the goal's success predicate is met
```

The "mod" the owner wants is precisely this: a **loop plus an experience notebook**, where the LLM
researches and explores and the *heavy control is delegated to MechJeb/kRPC*. No bespoke guidance
heuristics. See `docs/GENERALIZED_AEROSPACE_METHODOLOGY.md` for the generalized playbook and
`skills/` for the per-phase recipes.
