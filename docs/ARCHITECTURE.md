# ASTRA architecture

ASTRA turns a live Kerbal Space Program 1 game into a set of **tools** that an AI uses as the flight
crew and mission control. The AI observes, computes, decides, commands, and verifies in a loop.
There are **no mission scripts** anywhere in the project: no ascent program, no transfer driver,
no landing sequence. Tools are *capabilities* (instruments, calculators, controls, short-horizon
reflexes). Every parameter that encodes a mission decision comes from the AI at call time.

```
 AI crew (Claude Code session, `astra mission` Agent SDK runner, or any MCP client)
        │  MCP (stdio)                         │ CLI: astra call <tool> k=v ...
        ▼                                      ▼
 astra.server ──► astra.registry (tools, schemas, game lock, flight-recorder hooks)
                        │
     ┌──────────────────┼───────────────────────────────────────────────┐
     │ tools/observe  tools/compute  tools/design  tools/game  tools/control │
     │ tools/fly      tools/autopilot tools/crew   tools/logbook             │
     └──────┬─────────────┬──────────────┬──────────────┬────────────────┘
            │             │              │              │
   astra.telemetry   astra.physics   astra.craft    astra.reflex
   (streams,metrics) (pure math)     (catalog,spec, (control loop,
            │                         writer, stage  triggers, burns,
            │                         sim)           descent)
            ▼                                          │
   astra.ksp (one kRPC connection, scene/vessel guards, pause) ◄─┘
   astra.bridge (HTTP client) ──► csharp/KspAutomationBridge (KSP plugin, :48500)
```

## Time model: stop-the-world deliberation

LLM round trips take seconds to tens of seconds; KSP runs in real time. ASTRA resolves this by
**pausing the game whenever control returns to the AI** (`CONFIG.pause_between_commands`, on by
default; `KSP.hold_for_deliberation()`). Game time advances inside `fly_*` reflexes and warps,
`mj_*` autopilots run with `watch`, and the EVA/crew tools, which run it until the kerbal stands,
arrives or is seated (a `crew_walk` can run minutes). A few commands run the moment KSP needs to
finish what they started: staging and decoupling (`control_stage`, `control_part`, a few physics
frames to split the vessel), `control_attitude` with `wait_aligned_deg`, `game_switch_vessel`,
`game_restore` (1 s so the loaded vessel unpacks) and `game_checkpoint` (0.2 s, only when called
paused, so a just-switched vessel is active before the save). Telemetry reads, node edits, and
control changes all work while paused (verified live). The AI can therefore take as long as it
needs to compute, and fast dynamics are handled by reflexes that run the control loop at ~20 Hz
until an AI-chosen trigger fires.

## Package layout and ownership

| Path | Contents |
|---|---|
| `src/astra/registry.py` | `@tool(group)` decorator, `TOOLS`, `GAME_LOCK`, `Picture`, result rounding, `call`, `validate_and_call`, hooks |
| `src/astra/server.py` | MCP server (mcp>=2.2 `MCPServer`), wraps every tool to run in a thread; errors -> `ToolError` |
| `src/astra/cli.py` | `astra serve \| tools \| call \| mission [--record] \| up \| newsave \| record \| bridge` |
| `src/astra/ksp.py` | `ksp()` singleton: `.conn`, `.sc`, `.scene()`, `.require_flight()`, `.vessel()`, `.stream()`, `.paused`, `.set_paused()`, `.hold_for_deliberation()`, `.body()`, `.bridge` |
| `src/astra/bridge.py` | `Bridge.get/post` (flat JSON bodies; values stringified) |
| `src/astra/journal.py` | mission folders, automatic tool-call log (`log.jsonl`), journal notes, lessons store |
| `src/astra/launcher.py` | `astra up` (start KSP, load save), `astra newsave`, `astra bridge build\|install` |
| `src/astra/telemetry.py` | metric catalog + stream-backed sampler used by observe tools and reflexes |
| `src/astra/physics/` | pure orbital mechanics / rocket math / Lambert. No kRPC imports. |
| `src/astra/craft/` | ConfigNode parser/serializer, part catalog, craft spec, stage simulator, .craft writer |
| `src/astra/reflex/` | control-loop engine, trigger language, burn executor, powered-descent controller |
| `src/astra/tools/*.py` | the tool functions, grouped as below |
| `src/astra/agent/runner.py` | `astra mission "<goal>"`: Claude Agent SDK crew with only the astra tools; `--record` films it through `media.Session` |
| `src/astra/media.py` | Filming (`astra record`, `astra mission --record`), not a crew tool: bridge recorder control (`/record/*`); tool-call marks through `registry.add_start_hook`/`add_hook`, posted only while the marker file `.cache/recording.json` says a recording started from ASTRA runs (read-only instruments are skipped); a camera `Director` thread on its own kRPC connection that moves only the camera, only while game time runs and `HOLD_FILE` (`.cache/director.hold`) does not exist; `Session` starts and stops all three. `.cache` is `ASTRA_CACHE_DIR`. Video needs ffmpeg: the `media` extra (`imageio-ffmpeg`) or one on PATH |
| `knowledge/doctrine.md` | operating contract for the AI crew (read via `journal_read_doctrine`) |
| `knowledge/playbooks/*.md` | physics notes per phase (read via `playbook`) |
| `knowledge/lessons.md` | lessons learned in flight (read/append via `lessons_search`/`lesson_add`) |
| `csharp/KspAutomationBridge/` | KSP plugin: what kRPC cannot do (part DB with geometry, EVA, MechJeb, scene control, CAPCOM panel) |

## Tool conventions (every tool module must follow these)

- A tool is a **synchronous** function decorated with `@tool("<group>")` from `astra.registry`.
  Use `needs_game=False` only for tools that never touch the game (pure math, reading files).
- Parameters use `typing.Annotated[T, pydantic.Field(description=...)]`. Every parameter has a
  description written for the AI: unit, meaning, and how to choose it. **No parameter default may
  encode a mission decision** (target altitudes, burn times, thresholds that depend on the vehicle).
  Defaults are allowed only for presentation/format choices and for genuinely neutral values
  (e.g. `detail="brief"`, `limit=20`, `keep_node=False`).
- Do **not** use `from __future__ import annotations` in tool modules (schemas are built from
  evaluated annotations). Use `X | None` syntax (Python 3.11+).
- The docstring's first paragraph is a one-line summary; the rest explains behavior, what the
  result contains, and pitfalls. The MCP description is `[group] <docstring>`.
- Return plain JSON-able data (dict/list/str/number/bool) with SI units, and name fields with unit
  suffixes where ambiguous (`_m`, `_mps`, `_s`, `_deg`, `_t`, `_kn`, `_pa`). `registry.to_jsonable`
  rounds floats to ~6 significant digits (never coarser than 0.1). Return a `Picture` for images.
- Raise `astra.errors.AstraError(message, hint)` (or a subclass) for expected failures; the hint
  tells the AI what to do next. Never return `{"error": ...}` as a success.
- Tools that touch the game fetch the active vessel fresh each call via `ksp().vessel()`; never
  cache kRPC objects across calls. Compare kRPC proxies with `==`, never `is`.
- A tool that advances game time (all `fly_*`, `mj_*` with watch, warps, the EVA/crew tools,
  staging and decoupling, `control_attitude` waiting for alignment, `game_switch_vessel`,
  `game_restore`) must end by calling `ksp().hold_for_deliberation()` so the sim waits while the AI
  thinks, report `paused`, and report how much game time ran when it can be more than a moment
  (`game_s_elapsed` or a field named for it). Run time with `with ksp().running():` and hold in a
  `finally`. The one exception is `game_checkpoint`'s 0.2 s settle: it runs only when the game was
  already paused and restores that pause, so its result carries no `paused`.
- Tools that command the vessel must leave it in a known state and report that state (e.g. after a
  burn: throttle 0, autopilot disengaged or holding, as stated in the result).

## Tool catalog

Names are final; modules own the tools listed for them. Exact parameters (several tools grew
extra, optional ones during review) are in the generated `docs/TOOLS.md` (`astra tools --markdown`).

**observe** (`tools/observe.py`)
- `telemetry(detail: "brief"|"full")` — flight state snapshot (see `astra.telemetry`).
- `vessel_stages()` — per KSP stage: parts activated, engines (thrust/Isp vac+ASL, propellants),
  propellant available, masses, Δv (vac and at current pressure), TWR (here and vac), burn time;
  computed by `astra.craft.stagesim` over live parts; plus MechJeb stage stats when available.
- `vessel_parts(kind: str|None, stage: int|None)` — parts with `idx` (index into
  `vessel.parts.all`, valid until the vessel changes), name, title, tag, stage, decouple_stage,
  parent idx, module state (engine active/has_fuel/thrust, decoupler decoupled, chute state, leg
  state, solar/antenna state, fairing jettisoned), resources, temperature fraction.
- `body_info(body: str|None)` — live constants: GM, radius, SOI, rotation period/speed,
  atmosphere depth and sea-level pressure/density, surface gravity, orbit around parent,
  satellites, high/low space thresholds, and a sampled max terrain height (cached per body).
- `orbit_info(of: str)` — "vessel" | "target" | body/vessel name: elements, apsides, times, and the
  patched-conic chain (next patches: body, periapsis, SOI change UT).
- `target_info()` — target distance, relative speed, closest approach (sampled + kRPC), phase
  angle, relative inclination, AN/DN times.
- `camera_look(width: int)` — screenshot of the game as an image (kRPC `screenshot`; bridge
  render-to-texture fallback).

**compute** (`tools/compute.py`, pure math in `astra.physics`)
- `compute_calc(expression, variables)` — safe math evaluator (math functions, constants).
- `compute_orbit(body, periapsis_alt_m, apoapsis_alt_m, at_alt_m)` — vis-viva speeds, period, SMA.
- `compute_hohmann(body, from_alt_m, to_alt_m)` — Δv1, Δv2, transfer time, phase angle.
- `compute_rocket(...)` — rocket equation both ways, burn time, TWR on a body.
- `compute_descent(body, altitude_m, vertical_speed_mps, horizontal_speed_mps, mass_t, thrust_kn, ...)`
  — suicide-burn distance/time, hover TWR, feasibility.
- `compute_ascent_estimate(body, orbit_alt_m)` — orbital speed, rotation assist, a *labeled* rough
  loss range (not a fixed number).
- `compute_maneuver(kind, ...)` — live-state planners returning node parameters without creating a
  node: circularize (apo/peri), set_apsis, deorbit_to_periapsis, plane_change (match target or
  inclination at AN/DN), hohmann_to_body (moon transfer from current orbit: phase angle -> UT),
  return_from_moon, capture_at_periapsis.
- `compute_node_search(objective, ut_min, ut_max, bounds, max_evals)` — refine a node against
  KSP's own patched conics (add node, read `orbit.next_orbit` chain, remove), with the game paused.
- `compute_transfer_window(origin, target, parking_alt_m, earliest_ut, search_days)` — Lambert
  porkchop (fixed prograde-branch test for kRPC's y-up frames), ejection Δv and angle.
- `compute_terrain(body, ...)` — max terrain height over a region or along the predicted ground
  track (samples `body.surface_height`).

**design** (`tools/design.py`, library in `astra.craft`)
- `parts_search(query, role, diameter_m, propellant, min_thrust_kn, surface_attachable, limit)`
- `part_info(name)` — full catalog entry incl. attach nodes, attach rules, engine modes.
- `design_check(spec)` — builds the part tree, staging, and stage table; returns masses, per-stage
  Δv (vac/ASL on a chosen body), TWR, burn times, height, CoM, warnings (size mismatch, no control
  source, engine without fuel path, LF-only engine on LFO tanks, radial parts not under a decoupler,
  multiple payload stageables in one stage, thrust limiter < 100, etc.).
- `design_build(spec, overwrite)` — `design_check` + write `<active save>/Ships/VAB/<name>.craft`.
- `design_from_craft(craft)` — parse an existing .craft (stock or saved) into a spec to study/modify.
- `craft_list()` — craft files available to launch (save VAB/SPH and stock).

**game** (`tools/game.py`)
- `game_status()` (no game needed) — kRPC/bridge up?, scene, paused, UT, save, active vessel,
  active mission.
- `game_load_save(save, scene)` — works from the main menu (bridge `/load-save`).
- `game_checkpoint(name)`, `game_restore(name)`, `game_revert(to: "launch"|"editor")`.
- `game_launch(craft, site, crew)` — kRPC `launch_vessel` (pass `crew` list explicitly; recovers
  pad blockers); verifies the vessel exists, reports crew seated and parts count.
- `game_list_vessels()`, `game_switch_vessel(name)`, `game_recover()`, `game_space_center()`,
  `game_pause(paused)`, `game_log(lines, pattern)` (tail KSP.log for exceptions).

**control** (`tools/control.py`) — commands that act at once; only `control_stage` and decoupling in
`control_part` (the physics frames KSP needs to split the vessel) and `control_attitude` with
`wait_aligned_deg` run game time
- `control_set(throttle, sas, sas_mode, rcs, gear, legs, lights, brakes)`
- `control_stage()` — activate next stage; report stage number change, engines lit, parts dropped,
  and a warning (not a block) if the stage would leave no engine on the command side.
- `control_attitude(mode, ...)` — engage autopilot/SAS target that persists until changed.
- `control_part(action, part, value)` — decouple / jettison / deploy / arm / activate / shutdown /
  thrust_limit / crossfeed / control_from on a specific part (`idx` from `vessel_parts`, or name).
- `control_action_group(group, state)`
- `node_create(ut, prograde_mps, normal_mps, radial_mps)`, `node_list()`, `node_delete(index)`
- `target_set(name)` (vessel, body, or `None` to clear)

**fly** (`tools/fly.py`, engine in `astra.reflex`)
- `fly_until(until, throttle, attitude, auto_stage, interlocks, max_game_s, max_real_s)`
- `fly_burn(node_index, max_throttle, align_deg, feather_s, tolerance_mps, auto_stage, stop_when)`
- `fly_warp(to, offset_s, ut, seconds, floor_alt_m, max_rails_rate)` — `to` in
  ut|in|apoapsis|periapsis|soi|node (`seconds` with `in`); refuses to warp past atmospheric entry,
  and on an airless body below `floor_alt_m` above the terrain; a landed, splashed or pre-launch
  vessel may rails-warp on the ground; rails 0 afterwards.
- `fly_descent(touchdown_mps, terminal_alt_m, throttle_reserve, touchdown_drift_mps, terminal_rate,
  max_tilt_deg, tilt_gain_deg_per_mps, sink_gain, legs_alt_m, max_game_s)` — powered landing reflex:
  a suicide-burn predictor (the lowest retrograde throttle that still stops the vessel at
  `terminal_alt_m` over a terrain floor sampled ahead along the track, peaks remembered; height-first
  braking when retrograde cannot hold that gate), then a terminal sink and drift law with a hover
  until the drift is below `touchdown_drift_mps`. Braking thrust is held back until the vessel points
  near the braking direction. The tuning gains are arguments; the hover, hold and flare heights,
  the 4 s settle after contact and the look-ahead distance are derived inside `astra.reflex.descent`.

**autopilot** (`tools/autopilot.py`) — MechJeb through the bridge; every setting explicit
- `mj_ascent(...)`, `mj_execute_node(...)`, `mj_land(...)`, `mj_rendezvous(...)`, `mj_dock(...)`,
  `mj_plan(operation, params, place)`, `mj_status()`, `mj_abort(modules)`, `mj_stage_stats()`.
  Enabling tools take `watch: bool` + `max_game_s`: when watching, the reflex engine runs hands-off
  with interlocks until MechJeb reports the module idle.

**crew** (`tools/crew.py`) — every tool but `crew_roster` and `crew_status` runs game time
- `crew_roster()`, `crew_eva(kerbal, hop_clear_m)`, `crew_status(kerbal)`,
  `crew_walk(kerbal, lat_deg, lon_deg | bearing_deg, distance_m, wait, max_game_s)`,
  `crew_hop(kerbal, distance_m, rise_m, bearing_deg | away_from)` (EVA jetpack; `rise_m` required),
  `crew_plant_flag(kerbal, name, plaque)`, `crew_board(kerbal, part)`, `crew_transfer(kerbal, to_part)`.
  EVA kerbals and flags are invisible to kRPC's vessel list: the crew tools read them from the
  bridge (`/eva-status`, `/vessels`). `crew_eva` runs time until the kerbal stands (after the hop
  with `hop_clear_m`; a hop that cannot fly is reported as `hop_skipped`/`hop_error`, the kerbal is
  outside either way); `crew_walk` with `wait` until the walk ends (default bound 30 s +
  distance / 0.4 m/s; `timed_out`, `lost`); `crew_hop` until the kerbal stands again (bound about
  25 s + distance / 1.2 m/s + 2 s per m of `rise_m`; refused in orbit, at speed, without a jetpack
  or propellant, and by the bridge where the pack can neither lift the kerbal nor glide it down from
  a height); `crew_plant_flag` until KSP names the flag (`named`, `plant_state`). Walking and
  hopping need Harmony in KSP (bridge 503 without it).

**journal** (`tools/logbook.py`)
- `mission_start(goal)`, `mission_end(outcome, summary)`, `journal_note(kind, text)`,
  `journal_read_doctrine()`, `playbook(topic)`, `lessons_search(query)`, `lesson_add(text, tags)`,
  `capcom_inbox()`, `capcom_say(text)` (in-game CAPCOM panel via the bridge).

## Craft spec (design tools)

The AI writes a part tree. The first part is the root (usually the command part).

```json
{
  "name": "Orbiter 1",
  "description": "one line",
  "parts": [
    {"id": "chute", "part": "parachuteSingle", "parent": "pod", "node": "top"},
    {"id": "pod",   "part": "mk1pod.v2"},
    {"id": "shield","part": "HeatShield1", "parent": "pod", "node": "bottom"},
    {"id": "tank",  "part": "fuelTank", "parent": "shield", "node": "bottom"},
    {"id": "eng",   "part": "liquidEngine3.v2", "parent": "tank", "node": "bottom"},
    {"id": "dec",   "part": "radialDecoupler2", "parent": "tank",
     "surface": {"azimuth_deg": 90, "height_m": 0.0}, "symmetry": 3, "crossfeed": true},
    {"id": "srb",   "part": "solidBooster.v2", "parent": "dec", "surface": {}},
    {"id": "fins",  "part": "basicFin", "parent": "tank", "surface": {"azimuth_deg": 0, "height_m": -0.7}, "symmetry": 4}
  ],
  "stages": "auto"
}
```

- Root = the part without `parent` (exactly one).
- `node`: attach to the parent's attach node (`top`, `bottom`, or any node id from `part_info`);
  the child uses its opposite node unless `child_node` is given. Positions come from **real attach
  node offsets** (live prefab data), never ±height/2.
- `surface`: surface-attach at `azimuth_deg` around the parent's axis and `height_m` along it,
  placed on the parent's real surface radius; `symmetry: N` replicates the part and its whole
  subtree N times around the parent axis (stock yaw convention `yaw = 180° - azimuth`, `sym` links).
- Optional per part: `stage` (explicit inverse stage), `thrust_limit_pct`, `crossfeed`,
  `resources` (`{name: fraction 0..1}`), `fairing` (`{"clearance_m", "nose"}` for procedural fairing
  bases).
- `stages: "auto"` assigns inverse stages from the tree (launch engines + parallel boosters first;
  booster decouplers next; each stack decoupler shares the stage of the engines it exposes; chutes
  last). The stage table is returned for the AI to review; explicit `stage` values override it.

## Reflex engine (`astra.reflex`)

`fly_until` runs a ~20 Hz loop on kRPC streams:

1. Warp off (unless physics warp requested), apply the commanded throttle law and attitude law,
   unpause.
2. Each tick: sample metrics; update control laws; detect events (flameout with a debounce so the
   crossfeed transient does not read as dry, staging, part loss, SOI change, situation change,
   vessel change, overheating, impact risk, loss of control); run `auto_stage` if the AI enabled it;
   evaluate the AI's triggers; check interlocks; enforce `max_game_s` / `max_real_s`.
3. On exit: apply the exit throttle policy, pause for deliberation, return
   `{stopped_by, game_s, real_s, events[], trace[], state}`.

Trigger language (any-of): `{"metric": "apoapsis_altitude", "op": ">=", "value": 80000}` or
`{"event": "flameout"}`. Metrics are the names in `astra.telemetry.METRICS`.

Throttle laws: a number; `{"mode": "twr", "twr": 1.7}`; `{"mode": "approach", "metric": "...",
"target": x, "feather_s": 3, "min": 0.02}`; optional `"max_q_pa"` cap on any law; `"keep"`.

Attitude laws: `hold` (pitch/heading/roll), `pitch_program` (pitch vs altitude or speed table,
supplied by the AI), `prograde`/`retrograde` (surface or orbital, optional pitch clamp), `normal`,
`antinormal`, `radial_out`, `radial_in`, `node`, `target`, `anti_target`, `up`, `vector` (with
frame), `sas` (a SAS mode), `free`, `keep`. Autopilot reference frames are never vessel-fixed.

## Bridge plugin (`csharp/KspAutomationBridge`)

Bridge 2.1.0 (`/ping` and `/state` report `bridgeVersion`). C# 5 (in-box `csc` v4), 21 source files
plus `Properties/AssemblyInfo.cs`, HTTP on 127.0.0.1:48500 with a main-thread job queue (a job that
times out before it starts is abandoned, never run late). Full reference with every parameter,
status code and return field: `docs/BRIDGE_API.md`; `GET /routes` lists what the installed build
serves.

- Liveness and state: `/ping` (no main thread), `/state` (scene, save, UT, pause and pause-menu
  state, active vessel, editor, launch sites, MechJeb), `/job`, `/routes`.
- Pause without KSP's pause menu: `GET|POST /pause` (`FlightDriver.SetPause`). `astra.ksp` uses it
  and falls back to kRPC, whose `paused` opens the ESC menu. A pause is refused (409) only while the
  flight scene is still loading (`flightLoaded` false); resuming is always accepted, so a vessel
  switch made while paused (EVA, boarding) can finish.
- Scenes and saves: `/load-save` (main menu or any scene; any save file, e.g. checkpoints),
  `/space-center`, `/fly-vessel`, `/revert` (to launch or editor), `/craft/load`.
- Vessels: `/vessels` (persistentId, ownership), `/vessel-parts` (index = kRPC `parts.all` order).
- Part catalog: `POST /part-database {"detail": "full"}` — attach nodes, surface node, attach
  rules, crossfeed, bounds, every engine mode (module, thrust, Isp curve with tangents,
  propellants, gimbal), decoupler, parachute, command, reaction-wheel data; `/part/resolve`.
- Crew and EVA: `/crew-roster`, `/crew-list`, `/transfer-crew` (within one vessel), `/eva-go`,
  `/eva-walk-to` (really walks), `/eva-hop` (a short jetpack flight beside the start, then the pack
  is stowed; a lift below 1.3x local gravity is refused unless the kerbal starts at least 1 m up
  with half of gravity, and then glides), `/eva-status`,
  `/eva-board` (hatch reach), `/eva-plant-flag`, `/eva-flag`. Stock EVA/flag/boarding rules are
  enforced. Walking and hopping drive the stock EVA controller through a Harmony postfix on
  `KerbalEVA.HandleMovementInput` (the pure control math is in `EvaMath.cs`): they need Harmony
  (`GameData/000_Harmony`) and answer 503 without it. A walk and a hop replace each other.
- Recording: `/record/start`, `/record/stop`, `/record/status`, `/record/mark`. Scene cameras at the
  end of rendered frames, read back with `AsyncGPUReadback` and piped to an ffmpeg process (H.264
  `video.mkv`), or JPEG frames without ffmpeg; `frames.csv` (UT and vessel state per frame) and
  `marks.csv` (labeled events). Frame numbering, the index rows and the writer thread are in the
  Unity-free `FrameQueue.cs`. By default no frames are taken while the game is paused, so the AI's
  deliberation never appears in the footage. `astra.media` drives it.
- MechJeb: `/mj-ascent`, `/mj-execute-node`, `/mj-land`, `/mj-landing-prediction`, `/mj-rendezvous`,
  `/mj-dock`, `/mj-plan` (dry run or place), `/mj-abort`, `/mj-status`, `/mj-stage-stats`; every
  setting explicit and echoed back.
- `/capcom`, `/capcom/inbox`: the in-game CAPCOM panel (F8), a two-way thread between the crew
  and the player. `/screenshot`: render-to-texture PNG that works with the window unfocused.
- Removed from the old plugin: `/vessel/refuel`, `/spawn-crew`, cross-vessel `/transfer-crew`,
  the `/command` queue, `/reset`, `/launch`, `/save`, and the old read-back routes.

Startup isolates every optional subsystem, so a failure in one (e.g. the EVA walker patch) is
logged and never keeps the HTTP server from listening. KSP's `EventData.Add` rejects static
delegates: subscribe to GameEvents through instance methods (`SceneLoadedHook`).

## What the old project got wrong (and this one must not do)

- A one-shot LLM plan of coarse primitives, each wrapping a hardcoded mission driver with fixed
  numbers (pitch ladders, apoapsis bands, throttle ladders, capture thresholds). No observe/decide
  loop. The AI here decides each step from live telemetry.
- Craft writer with a hardcoded mission bus and ±h/2 node geometry, harvesting module state from
  random stock craft (e.g. a Mainsail at 73.5 % thrust). Here the AI chooses every part and the
  writer uses real node offsets and prefab defaults.
- Cheats (`/vessel/refuel`, `/spawn-crew`). Gone.
