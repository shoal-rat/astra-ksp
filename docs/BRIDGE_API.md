# KspAutomationBridge HTTP API (bridge 2.1.0)

Added in 2.1: `/eva-hop` (EVA jetpack hops; `{stop: true}` cancels walks and hops alike), the
`/record/*` video recorder, and `flightLoaded` on `/pause` (a pause is refused only while the flight
scene loads; resuming is always accepted). `/ping` and `/state` report `bridgeVersion`.

The bridge is the KSP-side plugin (`csharp/KspAutomationBridge`) for what kRPC cannot do: scene and
save control from the main menu, the loaded part database with geometry, EVA, MechJeb with every
setting explicit, the two-way CAPCOM panel, render-to-texture screenshots and video frame recording. Everything else
(telemetry, controls, nodes, staging, warp, launching craft, quicksave/quickload, crew transfer via
`transfer_crew`) goes through kRPC.

The bridge holds no mission logic and no mission numbers. Every value that encodes a decision
(altitudes, speeds, distances, which kerbal, which port) is a required parameter.

## Conventions

**Transport.** HTTP/1.1 on `127.0.0.1:48500` (override with `port = N` in
`GameData/KspAutomationBridge/PluginData/bridge.cfg`). Loopback only. Every response is JSON with
`Content-Type: application/json; charset=utf-8` and `Connection: close`. Request bodies are read as
exactly `Content-Length` bytes and decoded as UTF-8, so non-ASCII text (`"默认"`) works. Chunked
bodies are refused (411); `Expect: 100-continue` is honoured. Requests a web browser sends on behalf
of a web page are refused with 403 before anything runs: any `Origin` header, a `Sec-Fetch-Site`
other than `none`, or a `Host` that is not `127.0.0.1`/`localhost`/`[::1]` (any page can POST to
localhost without a CORS preflight, and DNS rebinding would let it read responses). Local tools
(astra, curl) send none of these; a URL typed into the browser's address bar still works.

**Envelope.** Success: `{"ok": true, ...data, "jobId": N}`. Failure:
`{"ok": false, "error": "...", "hint": "...", "jobId": N}` with an HTTP status:

| Status | Meaning |
|---|---|
| 400 | Bad or missing parameter (the message names it). |
| 404 | Unknown route, or the vessel/part/kerbal/save you named does not exist. |
| 405 | Route exists with another method. |
| 403 | Refused browser/cross-site request (see Transport). |
| 409 | Wrong game state for this call (scene, paused, autopilot already engaged, hatch blocked...). |
| 500 | Unexpected plugin exception (`GET /state` `lastError`, KSP.log has the stack trace). |
| 501 | The platform cannot do what was asked (video recording on a GPU/driver without `AsyncGPUReadback`). |
| 503 | Capability missing (MechJeb not installed, part database not loaded yet, Harmony missing). |
| 507 | Not enough free disk (a recording needs 3 GB free on its drive). |
| 499 | The client hung up before the main thread started the job; it was abandoned (nobody reads this). |
| 504 | The Unity main thread did not run the job in time (see "Jobs and timeouts"). |

`hint` says what to do next. `jobId` is present on every route that runs on the main thread.

**Parameters.** Query string and JSON body are merged (body wins). Every parameter may be sent as
its native JSON type or as a string (the Python client `astra.bridge` stringifies everything):
numbers `80000` or `"80000"`; booleans `true`/`"true"`/`"1"`/`"yes"`/`"on"` (and the negatives);
lists as a JSON array, a comma-separated string `"ascent,node"`, or a stringified Python list
`"['ascent', 'node']"`. An empty string counts as "not given". Parameters are flat (no nested
objects). Numbers must be finite: `NaN`, `Infinity` and overflowing values are a 400 (every numeric
parameter is a physical quantity, and a NaN handed to MechJeb or the EVA walker would corrupt the
vessel rather than fail).

**Ids.** Vessels are addressed by `persistentId` (uint32, stable across saves) or, where noted, an
exact name (an ambiguous name is a 409 listing the candidates; nothing ever falls back to "the
active vessel" or "the first match"). Parts are addressed by `persistentId`, or by `index`: the
position in the vessel's part list, which is the same order as kRPC `vessel.parts.all`.
`GET /vessels` and `GET /vessel-parts` list both. Kerbals are addressed by exact name
(case-insensitive).

**Units.** SI throughout: metres, seconds, m/s, tonnes (`_t`), kN (`_kn`), Pa, degrees for angles
and latitude/longitude. Key suffixes carry the unit where it is not obvious. MechJeb settings are
echoed under MechJeb's own names; their units are listed with each endpoint. NaN and Infinity are
serialized as `null`.

**Jobs and timeouts.** Anything touching KSP runs as a job on the Unity main thread (drained every
frame, also while the game is paused). The HTTP thread waits up to the route's timeout
(`GET /routes` lists it). If the job has not started by then, it is **abandoned and never runs**
(504, "abandoned and will NOT run"): retrying is safe. The same happens when the client closes its
connection before the job started (its own timeout was shorter than the route's), so a client that
gave up never leaves a job to run later; a client that half-closes its socket after sending counts
as gone. If the job started but is still running, the 504 says it "will still complete": poll
`GET /job?id=N` instead of retrying. Scene changes are requests:
the call returns once KSP accepted it; poll `GET /state` until `scene` changes. A second
scene-changing call while one is loading is refused (409, `sceneChangePending` in `/state`).

**Game time.** ASTRA keeps the game paused while the AI thinks. `POST /pause` pauses the flight
scene without KSP's pause menu (kRPC's `paused` opens the ESC menu, which covers the view); it also
resumes a pause set by kRPC, and kRPC's `paused` reads correctly whichever side paused. Calls that
need game time to have an effect (walking, jetpack hops, planting a flag, MechJeb autopilots, MechJeb
predictions and stage stats) only progress while the game runs; the tools that call them unpause,
wait/poll, and pause again. The recorder takes no frames while the game is paused (`skipPaused`).

## Endpoint index

| Method | Path | Thread | Purpose |
|---|---|---|---|
| GET | `/ping` | inline | Liveness without the main thread |
| GET | `/routes` | inline | Self-description of every route |
| GET | `/job?id=N` | inline | Outcome of a recent main-thread job |
| GET | `/state` | main (3 s, degrades) | Game snapshot |
| GET/POST | `/pause` | main | Pause/resume flight without the pause menu; true paused state |
| GET | `/vessels` | main | All vessels with ids |
| GET | `/vessel-parts` | main | Parts of a loaded vessel with ids and indices |
| POST | `/load-save` | main | Load a save into space center or flight |
| POST | `/space-center` | main | Go to the space center |
| POST | `/fly-vessel` | main | Take control of a vessel |
| POST | `/revert` | main | Revert flight to launch or editor |
| POST | `/craft/load` | main | Open a saved craft in the editor |
| GET | `/part-database` | main | Part catalog, basic (pre-2.0 schema) |
| POST | `/part-database` | main | Part catalog, `detail=basic\|full` |
| POST | `/part/resolve` | main | Resolve a craft part id to a loaded part |
| GET | `/crew-roster` | main | Whole roster with status and seat |
| GET | `/crew-list` | main | Kerbals aboard loaded vessels |
| POST | `/transfer-crew` | main | Move a kerbal within one vessel |
| POST | `/eva-go` | main | Send a kerbal on EVA |
| POST | `/eva-walk-to` | main | Walk an EVA kerbal to a surface point |
| POST | `/eva-hop` | main | Jetpack hop beside the start, then drop |
| GET/POST | `/eva-status` | main | EVA kerbals: position, FSM state, walk, hatch in reach |
| POST | `/eva-board` | main | Board a crew part through its hatch |
| POST | `/eva-plant-flag` | inline + main | Plant a flag, verify and name it |
| POST | `/eva-flag` | inline + main | EVA, settle, plant (convenience) |
| POST | `/mj-ascent` | main | MechJeb ascent autopilot |
| POST | `/mj-execute-node` | main | MechJeb node executor |
| POST | `/mj-land` | main | MechJeb landing autopilot |
| GET/POST | `/mj-landing-prediction` | main | MechJeb landing prediction |
| POST | `/mj-rendezvous` | main | MechJeb rendezvous autopilot |
| POST | `/mj-dock` | main | MechJeb docking autopilot |
| POST | `/mj-plan` | main | MechJeb maneuver planner (dry run or place) |
| POST | `/mj-abort` | main | Disable MechJeb modules |
| GET | `/mj-status` | main | All MechJeb module states |
| GET/POST | `/mj-stage-stats` | main | MechJeb per-stage delta-v (flight or editor) |
| POST | `/capcom` | inline | AI message to the in-game CAPCOM panel |
| GET | `/capcom` | inline | Thread tail |
| GET/POST | `/capcom/inbox` | inline | Player messages since a sequence number |
| POST | `/screenshot` | main | Render the view to a PNG |
| POST | `/record/start` | main | Start recording video frames |
| POST | `/record/stop` | main | Stop recording and flush frames |
| GET | `/record/status` | main | Recording state and counters |
| POST | `/record/mark` | main | Label the current frame in `marks.csv` |

## System

### GET /ping
Answers without the main thread (works while a scene loads). Returns `bridgeVersion`, `scene`
(racy read), `queueDepth`, `uptime_s`, `mechjebAvailable`. The socket answers ~40 s before the main
menu is usable: wait for `scene == "MAINMENU"` (or later) before loading a save.

### GET /routes
`{count, routes: [{method, path, mainThread, timeout_s, summary}]}`.

### GET /job?id=N
`{id, name, state: queued|running|finished|abandoned, enqueuedUtc, result?: {ok, status, error, data}}`.
The last 128 jobs are kept.

## Game and scenes

### GET /state
Query: `site` (optional launch site name to report besides `LaunchPad` and `Runway`).

Returns `bridgeVersion`, `kspVersion`, `scene` (`MAINMENU`, `SPACECENTER`, `EDITOR`, `FLIGHT`,
`TRACKSTATION`, `LOADING`...), `loadedSceneIsFlight`, `loadedSceneIsEditor`, `saveFolder`,
`gameTitle`, `gameMode` (`SANDBOX`, `CAREER`, `SCIENCE_SANDBOX`...), `ut` (s), `warpRate`,
`warpMode` (`HIGH` rails / `LOW` physics), `paused` (the true state whoever paused: in flight
`FlightDriver.Pause`, set by `POST /pause`, kRPC's pause menu or a stock dialog; anywhere a time
scale of 0), `pauseMenuOpen` (KSP's ESC/kRPC pause menu is showing),
`activeVessel` (`{name, persistentId, type, situation, body, loaded, partCount, crewCount, isEva,
landedAt, currentStage}` or null), `editor` (`{facility, craftName, partCount, loaded}` or null;
`loaded` becomes true only when the craft's parts exist, about 2 s after the scene reports EDITOR),
`sceneChangePending` (`{request, since_s}` or null), `launchSites`
(`{LaunchPad: {clear, blockers: [{name, persistentId, type}]}, Runway: {...}}`, null without a
save), `mechjeb` (`{available, version}`), `mechjebAvailable`, `queueDepth`, `abandonedJobs`,
`lastError`, `lastErrorUtc`, `capcom` (`{lastSeq, lastPlayerSeq}`: compare with the last seen
`lastPlayerSeq` to notice new player messages cheaply).

If the main thread does not answer within 3 s (scene loading), the `/ping` fields come back with
`busy: true` and a `note` instead of an error (3 s so the answer beats a caller's ~5 s timeout).

### GET or POST /pause
POST `paused` (bool, required): pause or resume the flight scene **without KSP's pause menu**.

| Situation | What POST does |
|---|---|
| `paused: true`, game running | `FlightDriver.SetPause(true, false)`: time scale 0, `onGamePause`, input lock `gamePause`; no menu. |
| `paused: true`, pause menu open (kRPC paused) | `PauseMenu.Close()` then `SetPause(true, false)`: still paused, menu gone. |
| `paused: true`, already paused without the menu | nothing (`action: "None"`). |
| `paused: false`, pause menu open | `PauseMenu.Close()` (it resumes the game itself). |
| `paused: false`, paused without the menu | `FlightDriver.SetPause(false, false)`: restores the warp rate, no warp message. |
| `paused: false`, running | nothing. |

Each call happens inside one main-thread job, so no physics tick runs between closing the menu and
re-pausing. Returns `{paused, scene, inFlight, flightLoaded, flightDriverPause, timeScale,
pauseMenuOpen, requested, wasPaused, changed, action (None|Pause|Resume|CloseMenu|CloseMenuThenPause),
closedPauseMenu, warning?}`; `paused` is read back after acting (a `warning` says why it differs from
`requested`, e.g. a stock dialog still holding time at zero).

`paused: true` is a 409 while the flight scene is still loading, because its start-up would undo the
pause: flight start-up not finished (`FlightDriver.flightStarted` false), no `FlightDriver` yet, or a
scene load requested and not finished (given up after 180 s). `flightLoaded` is false then; the hint
says "Poll GET /pause until flightLoaded is true, then retry." `paused: false` is always accepted
(`action` is never a refusal), and a vessel switch (EVA, boarding) does not count as loading: a switch
made while paused leaves the pause alone and finishes once the game runs again. Outside flight
nothing changes: `ok` with `changed: false`, `paused` as found (normally false; true only while a
space-center menu stops time) and a `note`.

GET returns the same state fields (`paused, scene, inFlight, flightLoaded, flightDriverPause,
timeScale, pauseMenuOpen`, plus a `note` outside flight) without acting. `paused` is the true state
whoever paused (`POST /pause`, kRPC's `paused`, the player's ESC, a stock dialog); `/state.paused` is
the same value.

Pitfalls: while paused this way the ESC key does nothing (KSP's pause menu only toggles its own
menu), so a human at the keyboard resumes through the AI, kRPC or `POST /pause`. kRPC's `paused`
getter follows KSP's pause events, so it reads `true` after `POST /pause`, and kRPC's
`paused = False` (which calls `PauseMenu.Close()`) also resumes it.

### GET /vessels
Query: `includeDebris` (bool, default true). Needs a loaded save.
`{count, vessels: [{name, persistentId, type, situation, body, loaded, active, owned, landedAt, crew: [names]}]}`.
`owned` is false for asteroids, comets and vessels the space program does not own (they cannot be
flown). Outside flight/space center/tracking station the list comes from the save state (no body,
crew or owned).

### GET /vessel-parts
Query: `vesselPersistentId` or `vessel` (exact name); default the active vessel. Flight only; the
vessel must be loaded. `{vessel, count, parts: [{index, persistentId, flightId, name, title,
parentIndex, stage (inverse stage), crewCapacity, crew: [names], dockingPort?: {state, nodeType},
isControlFrom}]}`. `index` equals the kRPC `vessel.parts.all` index.

### POST /load-save
Works from the main menu and from any loaded scene.

| Param | Type | Req | Meaning |
|---|---|---|---|
| `saveFolder` | string | yes | Folder under `KSP/saves` (non-ASCII fine). `saveName` is accepted as a pre-2.0 alias. |
| `saveFile` | string | no | File name without `.sfs`; default `persistent`. Use a checkpoint name to restore it. |
| `scene` | `spacecenter`\|`flight` | no | Default `spacecenter`. |
| `vesselPersistentId` | uint | no | With `scene=flight`: focus this vessel instead of the save's active one. |

Returns `{requested, saveFolder, saveFile, scene, vessel?: {name, persistentId}, note}`. Like the
stock load dialog, the loaded game is written back as `persistent.sfs` before the scene starts (so a
restored checkpoint becomes the current save) and `onGameStatePostLoad` is fired with the loaded file
(stock listeners such as the message system and Breaking Ground's deployed science rebuild from it).
409 while KSP is still loading or another scene change is pending; 404 if the file is missing.

### POST /space-center
| Param | Type | Req | Meaning |
|---|---|---|---|
| `saveFirst` | bool | yes from flight (elsewhere default false) | Write `persistent.sfs` before leaving (the stock button's behaviour). |

Leaving flight keeps or discards the flight, so `saveFirst` is required there (400 without it). With
`saveFirst=false` everything since the last save/load is discarded (stock "exit without saving"):
`discardedUnsavedFlight: true`. With `saveFirst=true` KSP's clear-to-save rule
applies (409 while moving over the ground, thrusting in atmosphere, etc.). Returns `{requested,
saved, discardedUnsavedFlight, note}`.

### POST /fly-vessel
| Param | Type | Req | Meaning |
|---|---|---|---|
| `persistentId` | uint | one of | Vessel to control. |
| `name` | string | one of | Exact vessel name (`vessel` accepted as alias). |

From the space center / tracking station: saves `persistent` and loads flight focused on the vessel
(the tracking station "Fly" path). In flight: switches in place if loaded, else KSP saves and reloads
flight. Only vessels the space program owns can be flown (409 for asteroids, comets and unowned
vessels, as the tracking station's Fly button refuses them; `/vessels` shows `owned`). Returns
`{vessel, requested, sceneReload, note?}`.

### POST /revert
`to`: `launch`|`editor` (required). Flight only; 409 if KSP says the revert is unavailable. The
editor is the one the craft was built in. (kRPC has `revert_to_launch` but no revert to editor.)

### POST /craft/load
| Param | Type | Req | Meaning |
|---|---|---|---|
| `craftName` | string | yes | File name in `saves/<save>/Ships/<facility>/` (with or without `.craft`). |
| `facility` | `VAB`\|`SPH` | no | Default `VAB` (`building` accepted as alias). |

Opens the craft in the editor (for inspection or editor stage stats). Asynchronous: poll
`/state.editor.loaded`. Launching is done with kRPC `launch_vessel` (crew manifest, pad recovery).

## Parts

### GET /part-database
The pre-2.0 schema, kept for compatibility: `{count, detail: "basic", parts: [{name, title, category,
bulkhead, crewCapacity, dryMassT, maxThrustKn?, ispVacS?, ispAslS?, resources: {Name: maxAmount}}]}`
(first engine module only). Query `names` restricts to a comma list.

### POST /part-database
| Param | Type | Req | Meaning |
|---|---|---|---|
| `detail` | `basic`\|`full` | no | Default `basic`. |
| `names` | list | no | Restrict to these internal part names (e.g. `"fuelTank,mk1pod.v2"`). |

Works in any scene once the game has loaded (503 during `LOADING`). Titles are localized (zh-cn on
this install); match parts by `name`. Category `none` entries (deprecated parts, `kerbalEVA`) are
included; filter them when designing. `missing` lists requested names that do not exist; `errors`
lists parts whose entry could not be built.

Full per-part schema:

```json
{
  "name": "liquidEngine2.v2", "title": "...", "category": "Engine", "cost": 1200, "mass_t": 1.5,
  "crewCapacity": 0, "bulkhead": "size1", "tags": "...", "techRequired": "generalRocketry",
  "bounds": {"size": [x, y, z], "center": [x, y, z], "source": "renderers|colliders|none"},
  "nodes": [{"id": "top", "pos": [x, y, z], "dir": [x, y, z], "size": 1}],
  "srfNode": {"pos": [x, y, z], "dir": [x, y, z]},
  "attachRules": {"stack": true, "srfAttach": true, "allowStack": true, "allowSrfAttach": true,
                  "allowCollision": false, "allowDock": false, "allowRotate": true, "allowRoot": true},
  "fuelCrossFeed": true, "stageable": true, "stagingIcon": "LIQUID_ENGINE",
  "resources": {"LiquidFuel": {"amount": 180, "max": 180, "density": 0.005}},
  "b9Tank": {"moduleId": "...", "subtype": "...", "tankType": "...", "volume": 0, "tankMass_t": 0,
             "addedMass_t": 0, "resourcesAdded": ["SolidFuel"], "note": "..."},
  "engines": [{"id": "basicEngine", "module": "ModuleEngines", "type": "LiquidFuel", "maxThrust_kn": 215,
               "minThrust_kn": 0, "isp_vac": 320, "isp_asl": 250,
               "atmosphereCurve": [[0, 320, inT, outT], [1, 250, inT, outT], [3, 0.001, inT, outT]],
               "propellants": [{"name": "LiquidFuel", "ratio": 0.9}, {"name": "Oxidizer", "ratio": 1.1}],
               "throttleLocked": false, "gimbal_deg": 3,
               "velCurve?": [[mach, mult, inTangent, outTangent]], "atmCurve?": [[density, mult, inTangent, outTangent]]}],
  "decoupler": {"ejectionForce": 250, "isOmni": false, "explosiveNodeId": "bottom", "radial": false},
  "parachute": {"semiDeployedDrag": 1, "fullyDeployedDrag": 500, "minAirPressureToOpen": 0.04, "deployAltitude": 1000},
  "command": {"minimumCrew": 1},
  "reactionWheel": {"pitch": 5, "yaw": 5, "roll": 5},
  "modules": ["ModuleEngines", "..."],
  "maxTemp": 2000, "skinMaxTemp": 2000, "crashTolerance": 7
}
```

- `bounds`: axis-aligned box in part-local metres (y is the part axis) around the prefab's visible
  meshes (inactive/disabled meshes skipped); colliders if a part has no mesh renderers.
- `nodes` / `srfNode`: `AttachNode.position` and `orientation` from the prefab (already scaled by
  `rescaleFactor`); `srfNode` is null when the part has none.
- `stageable`: prefab staging icon, or any IStageSeparator / engine / decoupler / parachute /
  procedural-fairing module.
- `engines`: every `ModuleEngines` (including `ModuleEnginesFX`), so multi-mode engines (RAPIER)
  list each mode by `id`, in the part's module order. `module` is the module's config name
  (`ModuleEngines` or `ModuleEnginesFX`); `type` is the engine type (`LiquidFuel`, `SolidBooster`,
  `Turbine`...), not the module class. `isp_vac`/`isp_asl` are the curve at 0 and 1 atm.
  `atmosphereCurve` keys are `[pressure_atm, isp_s, inTangent, outTangent]`: the first two positions
  are the pre-2.0 `[time, value]` pair, the tangents are Unity's key slopes (value per unit time), so
  the curve can be evaluated exactly between keys as a cubic Hermite spline. A tangent KSP stores as
  infinite (a step key) is `null`. `velCurve`/`atmCurve` (same 4-number keys) appear only for engines
  that use them (jets). `gimbal_deg` is the part's `ModuleGimbal.gimbalRange`.
- `decoupler.radial` is true for `ModuleAnchoredDecoupler`; `parachute`, `command`, `reactionWheel`,
  `decoupler` are null when the module is absent.
- `b9Tank` (only on B9PartSwitch tanks whose default subtype adds resources the prefab lacks): the
  resources are reconstructed from the part config and `B9_TANK_TYPE` (default subtype = explicit
  `currentSubtype`, else the highest `defaultSubtypePriority`; volume = `baseVolume` x
  `volumeMultiplier` + `volumeAdded`). `tankMass_t`/`addedMass_t` are NOT included in `mass_t`. B9
  engine/module data overrides (e.g. per-subtype `maxThrust`) are not applied.
- `techRequired` is an addition to the requested schema (career planning).

### POST /part/resolve
`partId` (required): a craft-file part id (`fuelTank_4294`) or name. Returns `{partId, resolvedName,
found, availableName?, title?, category?}`.

## Crew

### GET /crew-roster
Needs a loaded save (any scene). `{count, roster: [{name, type, trait, level, experience, status,
courage, stupidity, badass, veteran, gender, location: {vessel, vesselPersistentId,
partPersistentId, partName, seat, isEva} | null}], available, assigned, kia, missing}`. Pass the
names to kRPC `launch_vessel(crew=[...])` to crew a launch.

### GET /crew-list
Flight. Kerbals aboard loaded vessels: `{count, crew: [{name, type, trait, level, vessel,
vesselPersistentId, isActiveVessel, part: {persistentId, flightId, index, name, title}, seat, isEva}]}`.

### POST /transfer-crew
| Param | Type | Req | Meaning |
|---|---|---|---|
| `crew` | string | yes | Kerbal name. |
| `toPartId` | uint | one of | Destination part persistentId. |
| `toPartIndex` | int | one of | Destination part index in the kerbal's vessel. |

Only within one vessel (after docking the two craft are one vessel); 409 across vessels, when the
part is full, or when either part has `crewTransferAvailable` off (the stock transfer button and
dialog refuse those parts too). Same steps as the stock transfer dialog (the active vessel's IVA is
respawned one frame later, as stock does).
Returns `{crew, fromPart, toPart, vessel, verified, note}`.

## EVA

### POST /eva-go
`crew` (required). Spawns the kerbal through its part's hatch (other hatches are tried if that one
is blocked). No landed-only rule of the bridge's own: `warnings` explain floating free in orbit or
moving through atmosphere. The game's own EVA rules apply, as the crew portrait's EVA button applies
them (`FlightEVA.spawnEVA` itself checks none of them): 409 when EVA is disabled in the difficulty
settings, for tourists and inactive kerbals, parts flagged `NoAutoEVA`, and in career before the
Astronaut Complex upgrade anywhere but landed/splashed on the home body. Also 409 during time warp,
for parts without a hatch, or when KSP refuses (hatch obstructed). The EVA kerbal becomes the active
vessel. Returns `{crew, evaVessel, fromVessel, fromPart, body, biome, latitude, longitude,
activeVessel, warnings}`.

### POST /eva-walk-to
| Param | Type | Req | Meaning |
|---|---|---|---|
| `crew` | string | yes | EVA kerbal. |
| `lat`, `lon` | deg | one of | Target point (`lat` within -90..90; `lon` is normalized to -180..180). |
| `bearing`, `distance` | deg, m | one of | Target from the kerbal's position (bearing clockwise from north, great circle on the body sphere; `distance` > 0). |
| `arrivalRadius` | m | no (1) | Horizontal distance at which the walk counts as arrived (> 0). |
| `stop` | bool | no | `true` cancels the kerbal's walk **and** hop (the other parameters are ignored). |

KSP has no walk-to. The bridge drives the stock walk: a Harmony postfix on
`KerbalEVA.HandleMovementInput` replaces the movement request (`tgtRpos`) and the facing derived from
it (`tgtFwd`, `tgtUp`) with the direction to the target every physics tick, as
`KerbalEVA.SetWaypoint` does, until within `arrivalRadius`. The kerbal turns and walks in a straight
line (bounds in low gravity); a walk that gets no 0.5 m closer in 12 s of game time ends `stalled`
(an obstacle or a slope too steep). Game time the kerbal spends packed or on rails (a jump in UT of
more than 1 s between ticks) does not count toward those 12 s. Needs Harmony
(`GameData/000_Harmony`), else 503 with the reason. Starting a walk ends a hop in progress (the pack is
stowed and the hop ends `replaced by walk`; `warnings` says so), and a new walk replaces the kerbal's
previous walk order. Returns `{crew, body, fromLatitude, fromLongitude, targetLatitude,
targetLongitude, targetTerrainAlt_m, distance_m, bearing_deg, bodyRadius_m, arrivalRadius_m, walking,
warnings, note}`; with `stop: true` it returns `{crew, stopped, walk, hop}` (`stopped` false when
neither was running; `walk` and `hop` as in `/eva-status`). Progress: `/eva-status` `walk.state`
(`walking`, `arrived`, `stalled`, `stopped`, `replaced by hop`, `left <body>`, `error: ...`) and
`walk.distance_m`. Walking needs the game running and is reliable for the active kerbal (KSP handles
movement input only for the vessel under control). Orders, and the outcomes `/eva-status` reports,
are dropped on every scene load (a restore or quickload starts another timeline).

### POST /eva-hop
| Param | Type | Req | Meaning |
|---|---|---|---|
| `crew` | string | yes | EVA kerbal (with a jetpack and EVA propellant). |
| `bearing`, `distance` | deg, m | yes | Where to fly from the kerbal's position (0 < distance <= 50, else 400). |
| `rise` | m | no (1) | Cruise height above the start altitude, 0..10 (else 400). |
| `maxS` | s | no (25) | Game-time bound, 0 < maxS <= 120 (else 400); the hop ends `timed out` after it. Counts only simulated time: jumps in UT of more than 1 s between ticks (the kerbal packed or on rails) are not counted. |
| `stop` | bool | no | `true` cancels the kerbal's hop **and** walk, as on `/eva-walk-to` (the other parameters are ignored). |

A short jetpack flight, driven through the same Harmony postfix as walking: each physics tick the
pack request (`packTgtRPos`, the world-space thrust fraction that `UpdatePackLinear` turns into
`linPower` x `thrustPercentage`) comes from a velocity controller with gravity compensation: toward
the target at up to 1.5 m/s (0.8/s x the remaining distance on the final approach) while holding the
cruise height. Within 0.6 m of the target the pack is stowed and the kerbal drops (`hop.state` =
`released`). Use it to leave a lander without standing or walking on it: in low gravity a kerbal
pushing off a light lander can tip it over. The bridge does not check the kerbal's situation: the
controller steers toward a point on the ground, so start a hop only on or just above the ground
(ASTRA's `crew_hop` refuses in orbit and at speed).

Refusals: 503 without Harmony (or when this KSP build lacks `KerbalEVA.packTgtRPos`/`linPower`); 409
without a jetpack, without EVA propellant, and when the pack can neither lift nor glide the kerbal. A
lift needs a full-thrust acceleration (`linPower` x `thrustPercentage`/100 / kerbal mass) of at least
1.3x the local gravity; with the stock pack and a lightly loaded kerbal that rules out Kerbin, Eve,
Laythe, Tylo, Duna and Moho. A weaker pack may still glide: from at least 1 m above the terrain (a
hatch, a tank) with at least half of gravity it saturates toward the target while sinking slowly
(`mode` = `glide`; on Duna the stock pack gives about 0.94 g), and ends `glided down short of the
target` if it reaches the ground first. The message gives the pack, gravity and height. Starting a hop ends the kerbal's walk order
(`walk.state` = `replaced by hop`).

Returns `{crew, body, fromLatitude, fromLongitude, targetLatitude, targetLongitude, cruiseAltitude_m,
bearing_deg, distance_m, jetpackFuel, jetpackAccel_mps2, gravity_mps2, mode, maxS, note}`
(`cruiseAltitude_m` above the datum; `jetpackFuel` before the hop); with `stop: true`,
`{crew, stopped, walk, hop}`. Progress: `/eva-status` `hop` (`{state, targetLatitude,
targetLongitude, cruiseAltitude_m, distance_m, peakAltitude_m, elapsed_s}`, `elapsed_s` in real
seconds), `state` one of `flying`, `released`, `timed out`, `stopped`, `replaced by walk`, `cannot
lift: jetpack X m/s^2 < gravity Y m/s^2` (full thrust below 1x gravity, checked every tick, so a
heavier kerbal or a lowered thrust setting ends the hop instead of burning propellant until the
timeout), `left <body>`, `error: ...`. Every end stows the pack.

### GET or POST /eva-status
`crew` (optional). Returns `{count, kerbals: [...], paused, kerbal?}` for every EVA kerbal in
physics range; with `crew`, `kerbal` is that one (404 with the reason if not on EVA). Per kerbal:
`name, vessel, isActiveVessel, body, biome, latitude, longitude, altitude_m, radarAltitude_m,
surfaceSpeed_mps, horizontalSpeed_mps, verticalSpeed_mps, landed, splashed, fsmState` (e.g.
`Idle (Grounded)`, `Walk (Arcade)`, `Ladder (Idle)`), `onLadder, ladderPart, hatchPart` (the crew part
whose hatch trigger the kerbal is touching, or null), `hatchVessel, hasJetpack, jetpackDeployed,
jetpackFuel, jetpackFuelCapacity, flagItems, canPlantFlag, plantBlocker` (why not, or null), `walk`
(see above), `hop` (see `/eva-hop`), `nearestHatch`
(`{persistentId, index, name, title, vessel, distance_m, withinBoardingReach}` for the closest crew
part with a free seat on another loaded vessel). `walk` and `hop` are the running order or the last
outcome (null if none since the last scene load). Top level `flagPlanting` lists the recent flag
watches (see `/eva-plant-flag`).

### POST /eva-board
| Param | Type | Req | Meaning |
|---|---|---|---|
| `crew` | string | yes | EVA kerbal. |
| `partId` | uint | no | Crew part to board. |
| `part` | string | no | Internal part name (`mk1pod.v2`): the nearest such crew part with a hatch and a free seat. |

Without `partId`/`part`: the part whose hatch trigger the kerbal touches, else the nearest crew part
with a hatch and a free seat. Boarding goes through a hatch: allowed when the kerbal touches that
part's hatch trigger or is within 5 m of it (about one ladder height: the climb a player would
make, which the bridge cannot drive). Farther away is a 409 with the distance; walk closer first.
409 if the part is full or boarding is disabled in the difficulty settings. Returns `{crew, part,
vessel, distanceToHatch_m, boarded, note}`. `boarded` is read right after `KerbalEVA.BoardPart`, which
seats the kerbal at once unless KSP stops to ask what to do with carried science (a dialog in the
game) or the kerbal's inventory does not fit in the part (a screen message); then `boarded: false`
and the kerbal is still on EVA. KSP finishes switching to the boarded vessel over the next frames.

### POST /eva-plant-flag
| Param | Type | Req | Meaning |
|---|---|---|---|
| `crew` | string | yes | EVA kerbal standing on the ground. |
| `siteName` | string | no | Flag site (vessel) name; default KSP's. `name` is accepted as an alias. |
| `plaque` | string | no | Plaque text. |
| `waitS` | s | no (20, 0..120) | How long this call waits for the flag while the game runs; 0 returns at once. |

Checks first (no flag item is spent on a refusal), the stock rules of `KerbalEVA.CanPlantFlag` plus
two of the bridge's: `flagItems > 0`, landed, ground contact, not ragdolling, not in construction
mode, the career Astronaut Complex level that unlocks flags, and a kerbal FSM state that accepts the
plant command (`canPlantFlag`/`plantBlocker` in `/eva-status`). The stock button's active-vessel rule
is not applied: any EVA kerbal in physics range may plant. Then commands the plant and
starts a watch that the bridge advances every frame: it waits for a new vessel of type `Flag`,
answers KSP's naming dialog with `siteName`/`plaque` (as its OK button would, firing
`afterFlagPlanted`), and gives up after 30 s of game time without a flag. KSP creates the flag vessel
when the plant animation starts and opens the naming dialog only when it ends, so the bridge waits
for the dialog up to 30 s of game time; only if none appears (or a KSP update renamed the dialog's
private members, then after 5 s) does it set name and plaque directly (`namedVia: "direct"`). The plant works when commanded while paused: it
completes, and is named, whenever the game next runs.

Returns the watch: `{watchId, crew, state, verified, flag, namedVia, requestedSiteName,
requestedPlaque, body, biome, detail, elapsed_s, paused, commanded, pending?, note?}`. `state` is
`planting` (no flag yet), `placing` (flag exists, not named yet), `named` (`verified: true`, `flag` =
`{name, persistentId, type, situation, body, loaded, latitude, longitude, plaque, placedBy}`), `lost`
or `expired` (both a 409 with `detail`). If the game is paused, or the flag is not named within
`waitS`, the call returns `pending: true`; follow it in `/eva-status` `flagPlanting`. The HTTP call can
last up to `waitS`: give the client a longer timeout.

### POST /eva-flag
Convenience: `/eva-go`, then waits up to `settleS` (s, default 30) for the kerbal to stand idle on
the ground, then `/eva-plant-flag` (same params). If the game is paused the kerbal is left outside and
the call fails with 409 (unpause and call `/eva-plant-flag`). Returns the plant result plus `eva`
(the `/eva-go` result).

## MechJeb (2.15.3, hard-referenced)

All `/mj-*` routes: 503 without MechJeb; flight with an active vessel carrying a MechJebCore
(`MechJebForAll.cfg` adds one to every command part), else 409. Engaging routes refuse (409) when
the module is already engaged: `/mj-abort` it first. Every response echoes `applied` (what the call
set) and `effective` (read back from MechJeb afterwards), so no persisted MechJeb state stays hidden.
MechJeb only acts while the game runs.

### POST /mj-ascent
Required: `ascentType` (`classic`|`pvg`: which guidance flies the ascent), `altitude` (m, target
orbit altitude), `inclination` (deg), `autostage` (bool; set before engaging because MechJeb reads it
on enable). Optional (left as MechJeb has them when absent, and always echoed):

| Param | Unit | MechJeb field |
|---|---|---|
| `autoPath` | bool | AutoPath (MechJeb derives the turn from the body) |
| `autoTurnPercent` | fraction 0..1 | AutoTurnPerc |
| `autoTurnSpeedFactor` | - | AutoTurnSpdFactor |
| `turnStartAltitude` | m | TurnStartAltitude |
| `turnStartVelocity` | m/s | TurnStartVelocity |
| `turnEndAltitude` | m | TurnEndAltitude |
| `turnEndAngle` | deg | TurnEndAngle |
| `turnShapeExponent` | fraction 0..1 (GUI shows %) | TurnShapeExponent |
| `limitAoA` | bool | LimitAoA |
| `maxAoA` | deg | MaxAoA |
| `aoaFadeoutPressure` | Pa | AOALimitFadeoutPressure |
| `correctiveSteering` | bool | CorrectiveSteering |
| `correctiveSteeringGain` | - | CorrectiveSteeringGain |
| `forceRoll` | bool | ForceRoll |
| `verticalRoll`, `turnRoll` | deg | VerticalRoll, TurnRoll |
| `rollAltitude` | m | RollAltitude |
| `skipCircularization` | bool | SkipCircularization |
| `desiredLan` | deg | DesiredLan |
| `launchPhaseAngle`, `launchLanDifference` | deg | LaunchPhaseAngle, LaunchLANDifference |
| `autodeploySolarPanels`, `autoDeployAntennas` | bool | AutodeploySolarPanels, AutoDeployAntennas |
| `limitQaEnabled` | bool | LimitQaEnabled (PVG) |
| `limitQa` | Pa·rad | LimitQa (PVG) |
| `autostagePreDelay`, `autostagePostDelay` | s | Staging controller |
| `autostageLimit` | stage number | AutostageLimit (do not autostage below this stage) |
| `clampAutoStageThrustPct` | fraction 0..1 | ClampAutoStageThrustPct |
| `fairingMaxDynamicPressure` | Pa | FairingMaxDynamicPressure |
| `fairingMinAltitude` | m | FairingMinAltitude |
| `fairingMaxAerothermalFlux` | W/m² | FairingMaxAerothermalFlux |
| `hotStaging`, `dropSolids` | bool | HotStaging, DropSolids |
| `hotStagingLeadTime`, `dropSolidsLeadTime` | s | HotStagingLeadTime, DropSolidsLeadTime |
| `engage` | bool (default true) | false = configure only |

Returns `{engaged, applied, effective, status, situation, note?}`. `effective` also carries
`autoTurnStartAltitude`, `autoTurnStartVelocity`, `autoTurnEndAltitude` (what autoPath computes).
MechJeb does not ignite from PRELAUNCH: throttle up and stage once (kRPC) to start. Launch-to-plane
countdowns (timed launch) are not exposed: compute the launch time and warp to it. With
`autostage=true` the ascent autopilot claims MechJeb's staging controller and releases its claim
when it disengages; other claims (e.g. MechJeb's own Utilities-window autostage, which MechJeb
persists per vessel) keep it on. Check `/mj-status` `modules.staging.enabled` after the ascent and
release it with `/mj-abort {modules: staging}` before burns that must not stage.

### POST /mj-execute-node
| Param | Type | Req | Meaning |
|---|---|---|---|
| `all` | bool | yes | Execute all nodes (true) or the next one. |
| `autowarp` | bool | yes | Let MechJeb warp to the burn. |
| `leadTime` | s | no | Seconds before the burn to be aligned (MechJeb default 3). |
| `rcsOnly` | bool | no | Burn with RCS only. |
| `killRollRotation` | bool | no | Damp roll while burning. |
| `autostage` | bool | no | true: autostage during this execution only (the claim is released when the executor goes idle, since MechJeb never releases it); false: release every claim on MechJeb's staging controller before the burn. |
| `tolerance` | - | no | Not a setting in 2.15.3 (the executor ends the burn itself): ignored with a warning. |

409 without a maneuver node. Returns `{executing, applied, effective: {autowarp, leadTime, rcsOnly,
killRollRotation, autostage, autostageReleasedAfterBurn}, state (IDLE|WARPALIGN|LEAD|BURN), nodes,
timeToNode_s, warnings}`.
MechJeb does not rails-warp while still turning toward the node: for a distant node, warp to shortly
before it first. Progress: `/mj-status` `modules.node`.

### POST /mj-land
| Param | Type | Req | Meaning |
|---|---|---|---|
| `targeted` | bool | yes | Land at `lat`/`lon` (true) or wherever the trajectory goes. |
| `lat`, `lon` | deg | if targeted | Target on the current body. |
| `touchdownSpeed` | m/s | yes | Final descent speed. |
| `deployGears` | bool | yes | Let MechJeb extend gear/legs. |
| `deployChutes` | bool | yes | Let MechJeb deploy parachutes. |
| `limitGearsStage` | int | no | Do not deploy gear below this stage. |
| `limitChutesStage` | int | no | Do not deploy chutes below this stage. |
| `rcsAdjustment` | bool | no | Use RCS for fine corrections. |

The RCS action group is not touched (reported as `rcsActionGroup`). Returns `{landing, applied,
effective, target, status, rcsActionGroup}`. MechJeb will not fast-warp a very long descent ellipse.

### GET or POST /mj-landing-prediction
POST params (optional): `deployChutes` (bool), `limitChutesStage` (int) for the predictor.
Enables MechJeb's landing predictor (it simulates in the background while the game runs). Returns
`{enabled, deployChutes, limitChutesStage, pending}`; when not pending also `outcome` (`LANDED`,
`AEROBRAKED`, `NO_REENTRY`, `TIMED_OUT`, `ERROR`), `body`, `endUT`, `timeToEnd_s`, `latitude`,
`longitude`, `endAltitudeASL_m`, `maxDragGees`, `deltaVExpended_mps`, `aerobrake`,
`simulatedAtUT`. `/mj-abort {modules: predictor}` stops it.

### POST /mj-rendezvous
| Param | Type | Req | Meaning |
|---|---|---|---|
| `targetPersistentId` / `target` | uint / exact name | yes | Target vessel. |
| `desiredDistance` | m | yes | Final separation. |
| `maxPhasingOrbits` | orbits | yes | Phasing orbits MechJeb may spend. |
| `maxClosingSpeed` | m/s | yes | Closing-speed cap. |
| `rcs` | bool | no | Set the RCS action group. |

The target is set on MechJeb directly (no stale-target race). Returns `{enabled, target, applied,
rcsActionGroup, targetInfo: {exists, name, type, distance_m, relativeSpeed_mps, position}, status}`.
Close km-scale gaps with this before `/mj-dock`.

### POST /mj-dock
| Param | Type | Req | Meaning |
|---|---|---|---|
| `targetPortPartId` | uint | no | Target docking port part. |
| `targetPersistentId` / `target` | uint / name | no | Target vessel. |
| `ownPortPartId` / `ownPortPartIndex` | uint / int | no | Own port. |
| `speedLimit` | m/s | yes | Approach speed limit. |
| `forceRoll` | bool | yes | Hold a roll angle while docking. |
| `roll` | deg | if forceRoll | That angle. |
| `overrideSafeDistance` | bool | yes | Override MechJeb's safe distance. |
| `safeDistance` | m | if override | That distance. |
| `overrideTargetSize` | bool | no (false) | Override the target size. |
| `targetSize` | m | if override | That size. |
| `rcs` | bool | no | Set the RCS action group (MechJeb docks on RCS). |

Port selection (reported in `portSelection`): target = `targetPortPartId`, else the game's current
target if it is a free docking port (of the given target vessel, if one is given; e.g. set with kRPC
`target_docking_port`), else the target vessel's only free port. Own = `ownPortPart*`, else the
control-from part if it is a free docking port (kRPC `parts.controlling`), else the vessel's only free
port. Free = port state `Ready` (a stack-mounted `PreAttached` port is not free). Several free ports
and nothing chosen = 400 listing them. Sets control-from to the own port (`previousControlFrom` is
returned so it can be restored; if MechJeb refuses the target, control-from is put back before the
409). Returns `{enabled, ownPort, targetPort, portSelection, targetVessel,
previousControlFrom, partCountBefore, applied, effective, status, warnings, note}`. The autopilot
also switches itself off on success: confirm by own port state `Docked (...)` or a jump in part
count (`/mj-status`).

### POST /mj-plan
| Param | Type | Req | Meaning |
|---|---|---|---|
| `operation` | see below | yes | Planner operation. |
| `place` | bool | no (false) | false = dry run (return nodes); true = create them. |
| `planFrom` | `current`\|`last_node` | no (`current`) | Plan from the current orbit or after the last existing node. |
| `replaceExisting` | bool | no | With `place=true`, delete existing nodes first. |
| `targetBody` / `targetPersistentId` / `target` | | op-dependent | Target (body name, vessel id, or vessel/body name). Set on MechJeb and the game. |
| `timeRef` | see below | if the op has a time selector | When to burn. |
| `leadTime` | s | if `timeRef=x_from_now` | Seconds from now. |
| `atAltitude` | m | if `timeRef=altitude` | Burn at this altitude. |

Operations and their parameters (all required unless noted):

| operation | params | notes |
|---|---|---|
| `circularize` | - | |
| `apoapsis` | `newApoapsis` (m) | |
| `periapsis` | `newPeriapsis` (m) | |
| `ellipticize` | `newApoapsis`, `newPeriapsis` (m) | |
| `eccentricity` | `newEccentricity` | |
| `semi_major` | `newSemiMajorAxis` (m) | |
| `inclination` | `newInclination` (deg) | |
| `lan` | `newLan` (deg) | Written to MechJeb's target longitude field (shared with the landing position target). |
| `longitude` | `newLongitude` (deg) | Same field. |
| `plane` | target | Match planes with the target. |
| `kill_rel_vel` | target | |
| `hohmann` | target, `rendezvous` (bool), `capture` (bool); optional `planCapture` (bool, default = capture), `coplanar` (bool), `lagTime` (s) | Bi-impulsive transfer to a target in the same SOI. |
| `lambert` | target, `interceptInterval` (s) | Intercept after that time. |
| `interplanetary` | target body, `waitForPhaseAngle` (bool) | Burn time computed. |
| `course_correction` | target; `finalPeriapsis` (m) for a body, `interceptDistance` (m) for a vessel | Refines an existing encounter only. |
| `moon_return` | `moonReturnAltitude` (m) | Burn time computed. |
| `resonant_orbit` | `resonanceNumerator`, `resonanceDenominator` (int) | |

`timeRef` values are validated per operation (the error lists the allowed ones): `apoapsis`,
`periapsis`, `x_from_now`, `altitude`, `eq_ascending`, `eq_descending`, `rel_ascending`,
`rel_descending`, `closest_approach`, `eq_highest_ad`, `eq_nearest_ad`, `rel_highest_ad`,
`rel_nearest_ad`, `computed`. Operations that compute their own time reject `timeRef`.

Returns `{operation, planFrom, target, settings (every parameter used, incl. timeRef and
allowedTimeRefs), nodes: [{ut, timeTo_s, dv_mps, prograde_mps, normal_mps, radial_mps,
componentsExact}], placed, plannerMessage?, vesselNodes? (after placing)}`. Components of the second
and later nodes are computed on the orbit perturbed by the earlier burns without SOI changes
(`componentsExact: false`), and so are those of a first node whose time lies past an SOI change or
impact that ends the planning patch; placed nodes (`vesselNodes`) are exact (MechJeb places each on
the patch the vessel is on at that time). With `place=true`, existing nodes
and `planFrom=current` without `replaceExisting` is a 409 (the plan would not match them). The
advanced transfer (porkchop) operation is not exposed: it computes asynchronously in MechJeb's GUI.

### POST /mj-abort
`modules` (required list): `all`, `ascent`, `landing`, `node`, `rendezvous`, `dock`, `staging`,
`smartass`, `predictor`. Each module is really disabled (all users cleared; landing via
`StopLanding`, node via `Abort`, SmartASS set to OFF); `all` also releases MechJeb's attitude
control. `zeroThrottle` (bool, optional) sets the main throttle to 0. Returns `{modules: {name:
{wasEnabled, enabled}}, attitudeControllerEnabled, throttleControllerEnabled, mainThrottle, note}`.

### GET /mj-status
Returns `{flight}`; in flight also `hasCore`, `vessel` (`{..., partCount, apoapsisAlt_m,
periapsisAlt_m, controlFrom}`), `dockingPorts` (`[{persistentId, flightId, index, name, title,
state, nodeType}]`), `nodes`, and with a core `version`, `modules` (`ascent {enabled, type,
status}`, `landing {enabled, status, landAtTarget}`, `node {enabled, state, autowarp}`,
`rendezvous {enabled, status}`, `dock {enabled, status, step}`, `staging {enabled, status}`,
`smartass {target, mode}`, `predictor {enabled}`, `attitude {enabled}`, `thrust {enabled}`),
`target`.

### GET or POST /mj-stage-stats
Flight (active vessel) or editor (the first MechJebCore in the ship). POST params, editor only:
`body` (body for TWR and atmosphere), `altitude` (m, for the atmospheric figures), `mach`. Returns
`{scene, pending, body, atmoAltitude_m, mach, liveAtmosphere, vac, atmo}` where `vac`/`atmo` =
`{stages: [{index, kspStage, deltaV_mps, burnTime_s, startMass_t, endMass_t, stagedMass_t,
resourceMass_t, thrust_kn, isp_s, maxAccel_mps2, startTwr, maxTwr}], totalDeltaV_mps,
totalBurnTime_s}`. MechJeb simulates asynchronously: the first call is usually `pending: true`;
poll again (with the game running in flight).

## CAPCOM

The in-game panel (IMGUI window "ASTRA CAPCOM", toggled with F8 or the toolbar button) shows a
scrolling two-way thread: AI messages (white, yellow `warn`, red `alert`) and the player's messages
(cyan). The player types into the box and presses Enter or Send. `warn`/`alert` messages also
flash on screen; an `alert` opens the panel. While the text box has focus, game controls are
locked so typing cannot stage or throttle; hovering over the panel in the editor blocks part
picking. The panel header shows how long ago the AI last read the inbox.

### POST /capcom
`text` (required, trimmed, capped at 4000 chars), `level` (`info`|`warn`|`alert`, default `info`).
Returns `{seq, level, shownOnScreen}`.

### GET /capcom
`limit` (default 50, max 1000). `{messages: [{seq, from: ai|player, level, text, utc, ut}], lastSeq}`
(oldest first). `ut` is the game time for player messages, null for AI messages.

### GET or POST /capcom/inbox
`since` (sequence number, default 0). Player messages with `seq > since`:
`{messages, count, lastSeq}`. Keep the highest `seq` seen and pass it next time.

## Screenshot

### POST /screenshot
| Param | Type | Req | Meaning |
|---|---|---|---|
| `path` | string | yes | Absolute path ending in `.png` (directories are created). |
| `width` | px | no (1280; 64..4096) | Image width; height keeps the screen aspect. |
| `includeUi` | bool | no (false) | Also render the UI cameras. |

Renders every enabled scene camera (galaxy, scaled space, local/main, in depth order; skipping
cameras that render into their own textures and, unless asked, UI cameras) into a RenderTexture and
writes a PNG: works in every scene and while the window is unfocused or covered (the game must still
be updating; KSP runs in the background by default). Returns `{path, width, height, bytes, scene,
cameras, ut}`.

### POST /record/start
| Param | Type | Req | Meaning |
|---|---|---|---|
| `dir` | string | yes | Absolute folder for the recording (created; must not hold one already). |
| `ffmpeg` | string | no | Path to an ffmpeg executable: record H.264 into `video.mkv` (recommended). Without it, JPEG frames. ASTRA passes imageio-ffmpeg's bundled build (the `media` extra) or one on PATH. |
| `crf` | int | no (20; clamped to 0..51) | Video mode: x264 quality (lower is better). |
| `preset` | string | no (`veryfast`) | Video mode: x264 speed preset, one of `ultrafast`, `superfast`, `veryfast`, `faster`, `fast`, `medium` (anything else is a 400). |
| `fps` | number | no (15; clamped to 1..60) | Frames per real second at most. |
| `width` | px | no (screen width; clamped to 64..3840) | Frame width. Sizes are made even. |
| `height` | px | no (width x the screen aspect; clamped to 64..2160) | Frame height, to choose another aspect. |
| `quality` | 10..100 | no (85) | JPEG quality. |
| `includeUi` | bool | no (true) | Also render the UI cameras (navball, staging, altimeter). |
| `skipPaused` | bool | no (true) | Take no frames while the game is paused, or while UT has not advanced for more than 0.1 s of real time (frozen: loading, a dialog). |

A coroutine on the addon captures at the end of rendered frames, at most `fps` times per real second:
the same camera set as `/screenshot`, rendered into one reused RenderTexture. Video mode reads the
frame back asynchronously (`AsyncGPUReadback`, no GPU stall; ~2-3 ms of main-thread time per frame)
and pipes raw RGBA to an ffmpeg process that encodes H.264 into `video.mkv` in its own process (a
Matroska file stays readable if KSP dies mid-recording). JPEG mode encodes on the main thread
(~20-40 ms per frame) and writes `frame_000001.jpg`, ... from a background thread. Frames are dropped,
not queued without bound, if the encoder or disk falls behind. Only flight, Space Center and Tracking Station are recorded.
Each frame gets a row in `frames.csv`: `frame, real_s, ut, scene, warp, vessel, body, situation,
altitude, radar_altitude, surface_speed, orbital_speed, vertical_speed, eva`. Recording stops by
itself when free disk falls below 3 GB (checked every 150 frames; the stop happens on the next
rendered frame and frames read back after the check are discarded), when ffmpeg exits, and when KSP
quits; `stopReason` says which (`requested`, `disk space below 3 GB`, `ffmpeg exited (code): <last
ffmpeg message>`).

Errors: 400 for a relative `dir`, an `ffmpeg` path that does not exist, or an unknown `preset`; 409 if
already recording, if the folder already holds a recording (`video.mkv` or `frame_*.jpg`), or while
the previous recording's writer is still flushing its last frames ("The previous recording is still
writing its last frames into <dir>": `POST /record/stop` again to wait for it); 507 with less than
3 GB free on the recording drive; 501 with `ffmpeg` when the GPU/driver has no `AsyncGPUReadback`
(omit `ffmpeg` to record JPEG frames). `frames.csv` and `marks.csv` are opened before ffmpeg starts:
a start that fails part-way leaves nothing running or open (only the two CSV files with their header
rows may remain). Returns the status below.

### POST /record/stop
Stops, completes the frames still being read back, waits up to 30 s for the writer to put the queued
frames into the output, then closes ffmpeg's input and waits up to 60 s for it to finish `video.mkv`.
Returns the status below. If the writer did not finish in time, `flushing` is true, `lastError` says
"the writer did not finish within 30 s; POST /record/stop again to wait for it", and `/record/start`
refuses (409) until it is done; a second `POST /record/stop` waits for it again. When nothing records
or flushes it returns the status with `note: "not recording"`.

### GET /record/status
`{recording, mode (video|jpeg), dir, output, width, height, fps, crf, quality, includeUi, skipPaused,
frames (captured and numbered, each with a frames.csv row), written, failedWrites (frames with a
frames.csv row that are missing from the output), flushing (stopped, writer still running), megabytes
(raw data handed to the writer), dropped (the encoder or disk fell behind, or a readback failed),
skipped (paused, frozen, or not a recorded scene), avgCaptureMs, seconds, stopReason, lastError}`. `lastError` also carries writer
failures ("writing frame N failed: ...") and ffmpeg's exit message.

### POST /record/mark
`label` (required), `detail`. Appends `frame, real_s, ut, label, detail` to `marks.csv` so video can be
cut and captioned by event. `frame` is the last frame captured at or before the mark (frames still
being read back are completed first). Returns `{recording, frame}`; a no-op returning
`{recording: false}` when not recording. While a recording it started runs, ASTRA (`astra.media`)
marks the start and end of every tool call except the read-only instruments (telemetry, game_status,
orbit_info, vessel_stages, vessel_parts, body_info, crew_status, crew_roster, mj_status, capcom_inbox,
node_list, target_info) as `start:<tool>` / `end:<tool>`; a recording started by another client gets
no tool-call marks.

## Removed in 2.0

| Old endpoint | Why | Use instead |
|---|---|---|
| `POST /vessel/refuel` | Cheat (propellant from nothing) | Design adequate budgets; kRPC `ResourceTransfer` between connected parts |
| `POST /spawn-crew` | Cheat (teleports/creates kerbals) | kRPC `launch_vessel(crew=[...])` with names from `/crew-roster` |
| cross-vessel `/transfer-crew` | Cheat (no hatch path) | Same-vessel `/transfer-crew`, or EVA + `/eva-board` |
| `POST /command`, `GET /command/pending` | One-shot mission queue | `/capcom/inbox` |
| `GET/POST /status` | Agent log ring | `/capcom` |
| `POST /reset` | Hardcoded VAB revert | `/revert`, `/space-center` |
| `POST /save/load` | Duplicate | `/load-save` |
| `POST /save` | Overwrote `persistent` only | kRPC `save(name)` / `quicksave`, or `/space-center saveFirst` |
| `POST /launch` | Silent no-op on a blocked pad, empty pods | kRPC `launch_vessel(..., recover=True, crew=[...])` |
| `POST /parts/search` | Unstructured | `POST /part-database` + local search |
| `POST /vessel-info`, `/parts-list`, `/resources` | Silent fallback to the active vessel | kRPC; `/vessel-parts` for ids |
| `POST /mj-disable` | Could not stop ascent/landing/node | `/mj-abort` |

## Build, install, verify

`astra bridge build` compiles every `*.cs` in `csharp/KspAutomationBridge` plus `Properties/*.cs`
with the in-box C# 5 compiler (`csharp/build/KspAutomationBridge.dll`). Sources stay ASCII (csc reads
BOM-less files in the system code page). `astra bridge install` copies the DLL and
`MechJebForAll.cfg` into `GameData/KspAutomationBridge` and needs KSP closed (the loaded DLL is
locked); restart KSP afterwards. The transport layer (`Json.cs`, `Http.cs`, `MainThread.cs`,
`Capcom.cs`) and the pure helpers (`HarmonyReflect.cs`, `PausePlanner.cs`, `EvaMath.cs` for the hop
controller, `FrameQueue.cs` for the recorder's numbering, index rows and writer thread) have offline tests:
`pytest csharp/KspAutomationBridge/Tests/test_bridge_host.py`. Keep those files free of Unity and KSP
types: the tests compile them into a console host.
