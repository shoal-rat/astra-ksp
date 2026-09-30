# ASTRA rewrite brief: the AI as astronaut and mission control

**Scope.** This brief combines seven reader reports on the previous repository, plus two spot checks I made myself:
- `src/ksp_lab/data/failure_rules.json` holds 26 rules.
- `src/ksp_lab/execute.py:151` blames `/vessel/refuel` for the ~74% Mainsail thrust. The craft reader blames the donor craft's thrust limiter instead (see Open risks #2).

**Environment:** KSP 1.12.5 build 03190 running in zh-cn, Python 3.13, kRPC 0.5.4 (ports 50000/50001), MechJeb2 2.15.3, ModuleManager 4.2.2, and the bridge plugin KspAutomationBridge 0.1.0.0 on 127.0.0.1:48500. The active save is `codex-audit` (SANDBOX).

**The rule for the rewrite.** The old repo put decisions in code: constants, per-mission branches and driver scripts. The new repo puts capabilities in code and leaves decisions to the AI. Capabilities means observe, compute, build, act with guards, and verify.
- Code may hold physics constants and a small set of safety invariants:
  - never fire a decoupler that strands an engineless payload unless forced;
  - never warp past atmospheric entry;
  - never ignite while misaligned;
  - never report success without physical evidence.
- Every mission tunable is a tool argument.
- Every closed-loop controller runs as a short-horizon reflex. It takes AI-chosen parameters and returns a verified outcome together with the parameters it actually used.

Legend: **[port]** = move mostly as-is. **[rewrite]** = keep the idea, write clean code. **[discard]** = drop. `cs:` = `csharp/KspAutomationBridge/KspAutomationBridge.cs`. Other paths are relative to the repo root.

---

## 1. Salvage list

### 1.1 `bridge/` — C# plugin (rewritten, kept small)

#### 1.1.a Plugin infrastructure

- **Addon lifecycle** [port], cs:23-147.
  - Declared as `[KSPAddon(KSPAddon.Startup.EveryScene, true)] class AutomationBridgeAddon : MonoBehaviour`.
  - `Start()` enforces the `_instance` singleton, calls `DontDestroyOnLoad` and `StartServer(48500)`, and registers an ApplicationLauncher button on `onGUIApplicationLauncherReady`. `OnDestroy` stops the listener and removes the button.
  - Keep this behavior: the plugin is alive from the LOADING scene onward. KSP.log shows "Listening" at 17:51:20 and MAINMENU at 17:51:59.
  - Change: read the port from a GameData cfg (it is a const at cs:26).
- **HTTP server** [rewrite], cs:307-379, 381-636, 3386-3430.
  - Keep: the raw `TcpListener(IPAddress.Parse("127.0.0.1"), port)` (HttpListener is unreliable under Unity Mono), the background thread "KSP Automation Bridge", `ThreadPool.QueueUserWorkItem(HandleClient)`, localhost-only binding, and response headers `HTTP/1.1 {code} OK|Error`, `Content-Type: application/json; charset=utf-8`, `Content-Length`, `Connection: close`.
  - Fix, all verified live on 2026-09-29:
    - `HttpRequest.Read` reads the body as CHARS against a byte `Content-Length`. Any non-ASCII body hangs until the client disconnects and then still executes: `{"needle":"液体"}` took 6.1 s, the ASCII version 0.23 s. Read exactly `Content-Length` bytes, then decode as UTF-8.
    - Query strings return 404 (`GET /state?x=1`). Split the path from the query.
    - `HandleClient` has no try/catch and no `ReceiveTimeout`. Add both.
    - Replace the chained `Method==X && Path==Y` checks with a route dictionary.
- **Main-thread marshaling** [port], cs:31, 149-167, 638-648, 3354-3364.
  - `RunOnMainThread(Func<CommandResult> action, int timeoutMs = 30000)` enqueues a `MainThreadWork{Action, ManualResetEvent Done, CommandResult Result}` on a `ConcurrentQueue`.
  - `Update()` drains the whole queue every frame, catches exceptions into `Fail(ex.Message)` and `_lastError = ex.ToString()`, then calls `Done.Set()`. On timeout the caller gets `Fail("Timed out waiting for Unity main thread.", 504)`.
  - Fix:
    - Timed-out jobs still run later. Add an abandoned flag, and give long scene transitions (launch, load) job ids plus a job-status route.
    - Dispose the `ManualResetEvent`.
    - Add a coroutine/`WaitForEndOfFrame` path for screenshots.
  - `Update` runs while paused and during warp, but stalls during scene loads, so `queueDepth` grows.
- **Request JSON parser** [discard; replace], cs:3432-3454. The regex `"key"\s*:\s*"value"` reads string values only, and `Unescape` handles only `\"` and `\\`. Replace it with a C# 5-compatible recursive parser covering numbers, bools, null, arrays, objects and `\uXXXX`.
- **Response envelope** [rewrite], cs:3374-3384, 3456-3519.
  - Keep `{"ok":bool, "error"?:string, ...data}`. `Ok` returns 200; `Fail(error, httpStatus=400)` also uses 404/503/504.
  - Fix: add a recursive writer for nested dicts and lists; write NaN/Infinity as `null` (they are currently written bare, e.g. `/mj-status` apoapsis on escape); stop building arrays by hand with `StringBuilder`/`RawJson`; return arrays, not newline-joined strings.
- **In-game CAPCOM GUI** [rewrite], cs:39-56, 169-305, 454-476, 1067-1159.
  - Current behavior: F8 or the toolbar button toggles an IMGUI window (id `0x4B535042`) with a textbox, a "Run mission" button and a log.
  - Routes:
    - `POST /command {command}` → `{message, command, pending}`.
    - `GET /command/pending` → `{command|null, remaining}` (pops the oldest entry).
    - `POST /status {phase?, message?}` (at least one required) appends to a 200-line ring buffer `{ts "HH:mm:ss" UTC, phase, message}` → `{message, count}`.
    - `GET /status` → `{count, status:[...]}`.
  - These run on the pool thread under locks.
  - Rewrite as a two-way message thread with sequence ids (capcom_post / capcom_inbox), and drop "Run mission".
- **MechJeb access helpers** [rewrite], cs:16-19, 2033-2101.
  - MechJeb is a compile-time hard reference (`using MuMech; using MechJebLib.FuelFlowSimulation;`). There is no reflection except `EditorLogic.launchVessel()`.
  - Compile-verified members: `vessel.GetMasterMechJeb()`, `core.GetComputerModule<T>()`, `module.Users.Add(core)` / `.Remove(core)`, `EditableDouble.Val`, `core.Target` (capital T), `core.Target.Set(x)`, `core.Target.NormalTargetExists`, `FlightGlobals.fetch.SetVesselTarget(x)`.
  - `GetOptionalBool` accepts "true"/"1". `GetOptionalDouble` uses `NumberStyles.Any` with `InvariantCulture`.
  - Move MechJeb code into a separate assembly declared with `KSPAssemblyDependency`, so the core bridge survives a missing or updated MechJeb.
  - Replace `FindDockingPort` (it takes the first node whose state lacks "Docked", and counts editor "PreAttached" nodes as free) with explicit part ids.
- **Vessel resolver** [rewrite], cs:2654-2673. `ResolveVessel` does a substring match over LOADED vessels and silently falls back to the ACTIVE vessel. Replace it with persistentId addressing, a 404 on a miss, and ProtoVessel support for unloaded vessels.
- **EVA helpers** [port], cs:1668-1698, 2676-2709.
  - `ResolveEvaVessel`: the active vessel if it is an EVA and matches, else the first loaded EVA that matches.
  - `GetEvaModule`: the first `part.FindModuleImplementing<KerbalEVA>()`.
  - `EvaCrewNameMatches`: vessel-name substring or exact crew name; an empty needle matches anything.
  - Prefer explicit ids.
- **`MechJebForAll.cfg`** [port], `csharp/KspAutomationBridge/MechJebForAll.cfg:1-17`:
  `@PART[*]:HAS[@MODULE[ModuleCommand],!MODULE[MechJebCore]]:FINAL { MODULE { name = MechJebCore } }`
  Install it to `GameData/KspAutomationBridge/MechJebForAll.cfg`. It needs ModuleManager and applies to saved vessels after a reload.
- **Build: `csharp/build.sh:1-54`** [port].
  - `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe -target:library -nologo -optimize+ -out:C:\tmp\KspAutomationBridge.dll` (OUT is overridable).
  - References from `KSP_x64_Data\Managed`: Assembly-CSharp, Assembly-CSharp-firstpass, UnityEngine, UnityEngine.CoreModule, UI, IMGUIModule, InputLegacyModule, AnimationModule, TextRenderingModule, PhysicsModule. Plus `GameData\MechJeb2\Plugins\MechJeb2.dll` and `MechJebLib.dll`.
  - Add `UnityEngine.ScreenCaptureModule` and `ImageConversionModule` for screenshots.
- **Install scripts** [rewrite].
  - `scripts/build_bridge.ps1:1-86` tries msbuild, then dotnet, then a csc fallback with `/nowarn:1701,1702`.
  - `scripts/finalize_bridge_install.ps1:1-44` copies the current DLL to .bak, copies `.dll.new` over `.dll`, and deletes `.new` once `KSP_x64` has exited.
  - Merge them into one pipeline: build → stage `.dll.new` with a version/hash stamp → wait for KSP to exit → swap only if the new build is newer → restart → probe every route.
- **`KspAutomationBridge.csproj` + `Properties/AssemblyInfo.cs`** [rewrite]. The csproj claims LangVersion 7.3, but csc v4 compiles C# 5 only, and it lacks the firstpass/PhysicsModule references. The version is stuck at 0.1.0.0: stamp a build hash and expose it in `/state`.

#### 1.1.b Every endpoint (39 routes, 38 paths; route table at cs:381-636)

Format for each entry: route, body, return, main-thread timeout, verdict, source, then what to keep and what to fix.

**Scene and save**
1. `GET /state`
   - Returns `{scene, loadedSceneIsFlight, loadedSceneIsEditor, saveFolder, lastCraftName, lastCraftPath, queueDepth, lastError, activeVessel?, activeVesselSituation?}`. 30 s. [port] cs:383-386, 650-671.
   - Live response: scene SPACECENTER, saveFolder "codex-audit".
   - Extend with: UT, warp, game mode, active vessel persistentId, editor-ready (`EditorLogic.fetch.ship.parts.Count`), pad clear + blockers, build hash, MechJeb version.
   - `lastCraftName` is set when a load is ACCEPTED, not when it finishes.
2. `POST /craft/load`
   - Body `{craftName (req), building="VAB"|"SPH", craftPath=""}` → `{message:"Craft load requested.", craftName, craftPath, facility}`, or 404 if the file is missing. 60 s. [port] cs:388-392, 673-701, 1966-2000, 2018-2031.
   - Requires `HighLogic.CurrentGame`. The path is `saves/<HighLogic.SaveFolder>/Ships/<VAB|SPH>/<name>.craft`; a relative `craftPath` must stay inside that folder and end in `.craft`. Calls `EditorDriver.StartAndLoadVessel(path, facility)` asynchronously.
   - Keep the path-traversal guard. Relax the name regex `^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$` to "no path separators, no `..`". Add an editor-ready signal.
3. `POST /launch`
   - No body → `{message:"Launch requested."}` (or "Already in flight scene."). 60 s. [rewrite] cs:394-397, 703-745.
   - Invokes the non-public `EditorLogic.launchVessel()` by reflection (`Instance|Public|NonPublic`, `Type.EmptyTypes`); falls back to `EditorLogic.fetch.launchBtn.onClick.Invoke()`.
   - Returns ok when LaunchSiteClear fails, and sets no crew manifest.
   - Rewrite: pre-check the pad with `ShipConstruction.FindVesselsLandedAt(HighLogic.CurrentGame.flightState, site)`, set an explicit `VesselCrewManifest`, and return the real outcome as a job (FLIGHT reached plus new vessel id). Prefer kRPC `launch_vessel` (see §4).
4. `POST /revert`
   - No body → `{message}`. 60 s. [port] cs:399-402, 747-768.
   - If `FlightDriver.CanRevertToPrelaunch`: `RevertToPrelaunch(ShipConstruction.ShipType==SPH ? SPH : VAB)`. Else if `CanRevertToPostInit`: `RevertToLaunch()`. Else it fails with "revert is unavailable". Outside flight it returns ok "nothing to revert".
   - kRPC has no revert-to-editor.
5. `POST /save`
   - No body → `{message}`. 60 s. [rewrite] cs:404-407, 770-779. `GamePersistence.SaveGame("persistent", HighLogic.SaveFolder, SaveMode.OVERWRITE)`.
   - Rewrite as named checkpoints (`astra_<label>`) plus a checkpoint list.
6. `POST /save/load`
   - Body `{saveFolder (req), scene="spacecenter"|"flight"}` → `{message, saveFolder, scene}`. 60 s. [rewrite, merge into one load_game] cs:409-413, 781-819, 2002-2016.
   - `ValidateSaveFolder` rejects `..`, `/` and `\` and requires `saves/<f>/persistent.sfs`. Then `GamePersistence.LoadGame("persistent", folder, true, false)`. For flight it uses `startScene=FLIGHT; game.Start()`, which is weaker than `StartAndFocusVessel`.
7. `POST /load-save`
   - Body `{saveName="persistent" (actually the save FOLDER), scene}` → `{message, saveFolder, scene, vessel?}`. 120 s. [port] cs:415-424, 821-889.
   - `LoadGame("persistent", folder, nullIfIncompatible=true, suppressIncompatibleMessage=false)`; fails if the game or `flightState` is null. For flight with a valid `activeVesselIdx`: `FlightDriver.StartAndFocusVessel(game, activeVesselIdx)`. Otherwise `startScene=SPACECENTER; game.Start()`.
   - Proven path from MAINMENU (autostart). Rename the field, and add file-name and vessel-id parameters.
8. `POST /space-center`
   - No body → `{message}`. 60 s. [port] cs:426-429, 891-901. Saves persistent (OVERWRITE), then `HighLogic.LoadScene(SPACECENTER)`. Make `save_first` an explicit argument.
9. `POST /fly-vessel`
   - Body `{vessel (exact name, req)}` → `{flying, index}`. 30 s. Works from SPACECENTER or TRACKSTATION only. [port] cs:572-578, 2605-2639.
   - Finds the index in `FlightGlobals.Vessels`, saves persistent, then `FlightDriver.StartAndFocusVessel("persistent", idx)`.
   - Address the vessel by persistentId instead.
10. `POST /reset` [discard] cs:449-452, 1052-1065. Redundant, and hardcodes VAB.

**Parts**

11. `GET /part-database`
    - Returns `{count, parts:[{name, title, category, bulkhead, crewCapacity, dryMassT, maxThrustKn?, ispVacS?, ispAslS?, resources:{name:maxAmount}}]}`. 60 s; 503 if `PartLoader.LoadedPartsList` is null; works in any scene. [port+extend] cs:626-633, 3263-3351.
    - Engine data comes from the first `ModuleEngines` only (`atmosphereCurve.Evaluate(0)` / `(1)`). Resources come from `prefab.Resources`.
    - Live: 620 parts, about 109 KB.
    - Extend with: attach nodes (id, pos, dir, size), `srfAttachNode`, `attachRules`, `fuelCrossFeed`, every engine mode with propellant ratios and gimbal, cost, TechRequired, bounds, decoupler force/isOmni, chute specs, maxTemp, crashTolerance, variants/B9 subtypes, and the module list.
12. `POST /part/resolve`
    - Body `{partId (req)}` → `{partId, resolvedName, found, availableName?, title?, prefabName?, exception?}`. 30 s; any scene. [port] cs:437-441, 998-1021.
    - `KSPUtil.GetPartName(partId)`, then `PartLoader.getPartInfoByName`. Use it to pre-validate every part id in a generated craft.
13. `POST /parts/search`
    - Body `{needle=""}` → `{count, parts:"name|title\n..."}`. 30 s. [rewrite; fold into catalog query] cs:443-447, 1023-1050. Case-insensitive substring match over name and title.

**Vessel readback (flight only)**

14. `POST /vessel-info`
    - Body `{vessel=""}` → `{vessel, body, situation, partCount, stageCount (max inverseStage+1), currentStage, totalMassT, dryMassT, resourceMassT, crewCount, crewCapacity, resources:{name:{amount,maxAmount,density}}, vacTotalDeltaV?, deltaVPending?}`. 20 s. [port] cs:608-612, 3020-3135.
    - For the active vessel it calls MechJeb `StageStats.RequestUpdate()`. Fix the resolver fallback.
15. `POST /parts-list`
    - Body `{vessel=""}` → `{vessel, count, parts:[{name, title, dryMassT, resourceMassT, stage (inverseStage), crew, crewCapacity, modules:[name]}]}`. 20 s. [port+extend] cs:614-618, 3137-3198.
    - Add flightID/persistentId, parent id, attach node, decouple stage, and live module state (ignited/flameout, decoupled, chute, port).
16. `POST /resources`
    - Body `{vessel=""}` → `{vessel, resources:{name:{amount, maxAmount, density, massT}}}`. 20 s. [port+extend] cs:620-624, 3200-3261.
    - Add per-part and per-decouple-stage totals: vessel-wide totals hide "connected tank empty, sibling tank full".

**Crew and EVA** (kRPC 0.5.4 cannot start an EVA, board, walk or plant a flag)

17. `POST /eva-go`
    - Body `{crew="", vessel=""}` → `{message, crew, evaVessel, fromVessel, body, biome, latitude, longitude}`. 30 s. [port] cs:493-499, 1436-1565.
    - `FlightEVA.fetch.spawnEVA(pcm, part, part.airlock, true)` returns a `KerbalEVA`, or null when the hatch is blocked. Biome from `ScienceUtil.GetExperimentBiome(body, lat, lon)`.
    - The landed-only rule should become a warning.
18. `POST /eva-walk-to`
    - Body `{lat & lon}` or `{bearing (degrees clockwise from north) & distance (m)}`, plus `crew=""` → `{message, evaVessel, body, fromLatitude, fromLongitude, targetLatitude, targetLongitude, targetTerrainAlt, distanceM, bearingDeg, bodyRadiusM}`. 30 s. [port] cs:584-588, 2711-2838.
    - Direct great-circle formula on `body.Radius`; `body.TerrainAltitude(lat, lon, true)`; `body.GetWorldSurfacePosition`; then `KerbalEVA.SetWaypoint((Vector3)world)`.
19. `GET /eva-status`
    - Returns `{flight, onEva, evaVessel, body, latitude, longitude, altitude, radarAltitude, srfSpeed, horizontalSrfSpeed, verticalSpeed, landed, splashed, kerbal, hasEvaModule, fuel, fuelCapacity, onLadder, jetpackDeployed, hasJetpack, fsmState (KerbalEVA.fsm.currentStateName), ladderPart}`. 15 s. [port] cs:590-594, 2840-2894. Add a kerbal argument.
20. `POST /eva-board`
    - Body `{crew=""}` → `{message, crew, toPart, toVessel, distanceM}`. 30 s. [port] cs:501-507, 1567-1698.
    - Picks the nearest crewable part with a free seat on another loaded non-EVA vessel and calls `KerbalEVA.BoardPart(part)`. Add a part-id argument, and verify via the crew list.
21. `POST /eva-flag`
    - Body `{crew=""}` → `{message, crew, planted, flagMethod, flagDetail, evaVessel, body, biome, latitude, longitude}`. Returns 400 if not planted even though the kerbal is already out. 30 s. [rewrite: split] cs:485-491, 1246-1434.
    - Calls `evaController.PlantFlag()`, falling back to any `BaseEvent` whose name/guiName contains "flag".
    - Split into eva_start and eva_plant_flag (the latter only after `fsmState` is idle). Verify by a new Flag vessel.
22. `GET /crew-list`
    - Returns `{count, crew:[{name, type, trait, level, vessel, part (localized title), seat, isEva}]}`. Flight only; 15 s. [port] cs:597-600, 2896-2952. Add part and vessel ids.
23. `GET /crew-roster`
    - Returns `{count, roster:[{name,type,trait,level,status}], available, assigned, kia, missing}`. Any scene with a game; 15 s. [port] cs:602-605, 2954-3018.
    - Enumerates `roster.Crew`, `Applicants`, `Tourist`, `Unowned`; there is no zero-argument `Kerbals()` in 1.12.5.
24. `POST /transfer-crew`
    - Body `{crew="", fromPart="", toPart="", toVessel=""}` → `{message, crew, fromPart, toPart, fromVessel, toVessel}`. 30 s. [rewrite] cs:478-483, 1700-1959.
    - Sequence: `RemoveCrewmember`; `seatIdx=-1`; `AddCrewmember`; `SpawnCrew()` on both vessels; fires `onVesselWasModified` and `onVesselChange`.
    - There is no connectivity check, so it is a teleport cheat between separate vessels. Restrict to the same vessel and address parts by persistentId, or use kRPC `transfer_crew`.
25. `POST /spawn-crew` [discard; cheat] cs:509-515, 1161-1244.

**MechJeb**
26. `POST /mj-ascent`
    - Body `{altitude="100000", inclination="0", autostage="true"}` → `{ascent, altitude, inclination, autostage}`. 30 s. [rewrite] cs:548-552, 2437-2470.
    - Sets `MechJebModuleAscentSettings.DesiredOrbitAltitude.Val`, `DesiredInclination.Val` and `Autostage` (which must be set BEFORE `MechJebModuleAscentClassicAutopilot.Users.Add(core)`).
    - Expose every setting present in the 2.15.3 DLL: AscentType, AutoPath, TurnShapeExponent, TurnEndAngle, AutoTurnStartAltitude/Velocity, AutoTurnEndAltitude, LimitAoA, AutostagePreDelay/PostDelay, AutostageLimit, ClampAutoStageThrustPct, FairingMaxDynamicPressure, SkipCircularization, LaunchPhaseAngle, LaunchLANDifference, DesiredLan, ForceRoll/VerticalRoll/TurnRoll. Echo back the effective settings.
27. `POST /mj-execute-node`
    - Body `{autowarp="true", all="false"}` → `{executing, nodes}`. Requires `maneuverNodes.Count > 0`. 30 s. [port+extend] cs:554-558, 2472-2495.
    - Sets `MechJebModuleNodeExecutor.Autowarp`, then `ExecuteAllNodes(core)` or `ExecuteOneNode(core)`. Add abort, leadTime and tolerance.
28. `POST /mj-plan`
    - Body `{target="" (body name), operation="interplanetary"|"circularize"|"plane"|"correction"}` → `{planned, operation, nodeCount, dv (first |dV|), ut}`. 30 s. [rewrite] cs:560-564, 2497-2569.
    - Calls `SetVesselTarget(body)` and `core.Target.Set(body)`, then `new OperationX().MakeNodes(vessel.orbit, UT, core.Target)` → `List<ManeuverParameters>{dV (Vector3d), UT}`. It CLEARS all existing nodes, then calls `vessel.PlaceManeuverNode(orbit, dV, UT)`.
    - Bug: circularize fails with "No target set" unless something is targeted.
    - Rewrite:
      - Add `place=false` for dry runs.
      - Allow vessel targets.
      - Expose operation parameters (TimeSelector etc.).
      - Expose every operation in the DLL: Apoapsis, Periapsis, Ellipticize, Eccentricity, SemiMajor, Inclination, Lan, Longitude, Lambert, KillRelVel, MoonReturn, AdvancedTransfer, ResonantOrbit, Generic.
29. `POST /mj-land`
    - Body `{touchdownSpeed="0.5", targeted="false", lat="0", lon="0"}` → `{landing, targeted}`. 30 s. [port+parameterize] cs:566-570, 2571-2603.
    - Sets `TouchdownSpeed.Val` and hardcodes `DeployGears=true, DeployChutes=true, RCSAdjustment=true`; turns the RCS action group on. Targeted: `core.Target.SetPositionTarget(mainBody, lat, lon)` then `LandAtPositionTarget(core)`; otherwise `LandUntargeted(core)`.
    - Add gears/chutes flags, LimitGearsStage/LimitChutesStage, StopLanding, and a `MechJebModuleLandingPredictions` readout.
30. `POST /mj-rendezvous`
    - Body `{target="" (exact vessel name), desiredDistance="100", maxPhasingOrbits="5", maxClosingSpeed="100"}` → `{enabled, target, status}`. 30 s. [port] cs:520-524, 2103-2150.
    - Verified live: closed 1078 m to 60 m. Fix the target-sync race by also calling `core.Target.Set(tv)`. Make RCS optional.
31. `POST /mj-dock`
    - Body `{target="", speedLimit="1.0", forceRol="false", overrideSafeDistance="false"}` → `{enabled, target, chaserPartCount, status}`. 30 s. [port] cs:526-530, 2152-2233.
    - Target port = first free node; own port = first free node, then `MakeReferenceTransform()` and `vessel.SetReferenceTransform(myPort.part)`. Sets `overrideTargetSize=false, drawBoundingBox=false`, RCS on.
    - Verified live: part count 21 → 42.
    - Add explicit own/target port ids, restore the control reference, and return a real docked verdict.
32. `POST /mj-disable`
    - Body `{which="all"|"dock"|"rendezvous"|"staging"}` → `{disabled}`. 30 s. [rewrite as mj_abort] cs:532-536, 2235-2271.
    - Add ascent, landing, node executor and SmartASS.
33. `GET /mj-status`
    - Returns `{flight, activeVessel, partCount, hasCore, targetExists, dockEnabled, dockStatus, rvEnabled, rvStatus, myPortState, ascentEnabled (Classic only), landingEnabled, nodeExecEnabled, nodeCount, situation, apoapsis, periapsis, body}`. 15 s. [rewrite/extend] cs:538-541, 2273-2323.
34. `GET /mj-stage-stats`
    - Returns `{flight, activeVessel, hasCore, hasStageStats, pending, vacStageCount, atmoStageCount, vacTotalDeltaV, atmoTotalDeltaV, vacTotalBurnTime, atmoTotalBurnTime, vacStats[], atmoStats[]}`. Each stage: `{index, kspStage, deltaV, burnTime, startMass, endMass, stagedMass, resourceMass, thrust, isp, maxAccel, startTwr (StartTWR(GeeASL)), maxTwr}`. 15 s. [port+extend to editor] cs:543-546, 2325-2435.
    - `RequestUpdate()` is asynchronous; poll until `pending` is false.

**Cheat**

35. `POST /vessel/refuel` [discard] cs:431-435, 903-996.

**CAPCOM**

36-39. `/command`, `/command/pending`, `GET /status`, `POST /status`: described under GUI above.

**Missing route.** `/vessel/type` does not exist. `deploy_relay.commission` posted to it and silently failed. Use kRPC's settable `vessel.type` instead.

**Leave to kRPC 0.5.4 (do not re-implement in the plugin):** save/load/quicksave/quickload, `launch_vessel` (crew list + pad recovery), `revert_to_launch`, flight-scene `screenshot`, `transfer_crew`, `ResourceTransfer`, `recover`, `raycast`, camera, `warp_to`.

**New plugin routes needed (not in kRPC):**
- editor state/ready
- editor stage simulation (MechJeb/stock)
- full part catalog
- named checkpoints and job status
- a GameEvents feed
- screenshots in any scene
- pad status/recover by id
- full-parameter MechJeb with abort, landing prediction and SmartASS
- optionally, the stock VesselDeltaV per-stage table

### 1.2 `bridge_client.py`
- `src/ksp_lab/bridge_client.py:24-290` [rewrite]. Keep one method per route. Until the parser is fixed, stringify every field (see the comment at :81); `refuel_vessel` sends `fraction` as a float, which is dropped. Encode bodies as UTF-8 bytes. Discard `mj_plan`'s default `target='Duna'` (:147).

### 1.3 `craft/` — model, writer, validator, readback (hardest to redo; exhaustive)

**Target design.** An in-memory `CraftSpec` part tree that round-trips any `.craft` through a real ConfigNode parser and serializer. The AI builds it with tools. It is written to the ACTIVE save, then read back after load and diffed against the spec.

- **`validate_craft_name` / `resolve_craft_path` / `CraftValidationError` / `CRAFT_NAME_RE`** [port], `src/ksp_lab/craft_writer.py:12-16, 110-129`.
  - `validate_craft_name(name)->str`: strips, applies `^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$`, forbids `..`, `/`, `\`.
  - `resolve_craft_path(save_vab_dir, craft_name)->Path` returns `<dir>/<name>.craft` and raises if the result escapes the directory.
  - Keep in sync with the bridge `CraftNameRegex` (cs:28), or relax both together.
- **Brace-balanced block scanner** [rewrite into a ConfigNode parser/serializer], craft_writer.py:19-69.
  - `_part_block_lines(text, part_name)` matches `part = <name>_<digits>` exactly; the digit check stops fuelTank matching fuelTank.long.
  - `_extract_part_body` returns everything from the EVENTS line up to, but excluding, the PART's closing brace.
  - Keep the scanner; drop the donor harvest (see Discard).
- **`CraftNode`** [rewrite], :72-107.
  - Current fields: part_name, uid, stage_index, y, parent, parent_node, child_node, children, srf_parent, pos_xyz, rot_quat=(0,0,0,1), fairing_xsections, interstage_shroud, lander_base_engine. `craft_id` is `f'{part_name}_{uid}'`.
  - New node: part, uid, parent, attach kind and node ids, full xyz pos and rot for every part, istg/dstg, symmetry group, crossfeed, resource and module overrides, and a KOSNameTag tag.
- **`CraftWriter.write(design, save_vab_dir, template_path=None)->Path`** [port], :133-141. Writes `<save>/Ships/VAB/<name>.craft` in UTF-8 with `newline='\n'`. The save must come from bridge `/state.saveFolder`.
- **Header** [port], :306-333.
  - Lines: `ship`, `version = 1.12.5`, a single-line `description`, `type = VAB`, `size = 1.25,10,1.25`, `steamPublishedFileId = 0`, `persistentId`, `rot`, `missionFlag = Squad/Flags/default`, `vesselType = Ship|Probe`.
  - Write NO top-level `ACTIONGROUPS` or `STAGES` block.
  - KSP recomputes `size` and reassigns `persistentId` on load, but emit a random unique persistentId anyway.
- **uid allocator (`new_node`)** [port], :336-348. Starts at 4294900000 and steps by −173. Raises on an unknown part and canonicalizes aliases to the live dotted name. Any unique 10-digit scheme works: KSP reassigns persistentId but keeps part ids as `link`/`attN` keys.
- **`_render_part`** [port; the core serializer, proven to load live], :1029-1096.
  - Emits this sequence:
    - `PART {`
    - `part = <liveName>_<uid>`, `partName = Part`, `persistentId = <uid>`
    - `pos`, `attPos = 0,0,0`, `attPos0 = <pos>`
    - `rot`, `attRot`, `attRot0`, `mir`
    - `symMethod = Radial`, `autostrutMode = Off`, `rigidAttachment = False`
    - `istg`, `resPri = 0`, `dstg = istg`
    - `sidx = -1`, `sqor = -1`, `sepI = -1`, `attm = 0`
    - `modCost = 0`, `modMass = 0`, `modSize`
    - one `link = <childId>` per child
    - `srfN` or `attN` to the parent, and one `attN` per stack child
    - then the body, and the closing brace.
  - The minimal body is EVENTS/ACTIONS/PARTDATA plus RESOURCE.
  - Improvements:
    - Surface parts get `attm = 1` and a parent-relative `attPos0`.
    - Write real node offsets in `attN`.
    - Add optional `sym =` links.
    - Add optional MODULE override blocks only for fields the AI sets: `ModuleEngines thrustPercentage`, `ModuleToggleCrossfeed crossfeedStatus`, `ModuleProceduralFairing` XSECTION list, `KOSNameTag nameTag`.
    - Write RESOURCE blocks for every resource.
- **`attN` / `srfN` formats**, established from KSP 1.12.5's own resave `saves/默认/Ships/VAB/自动存档飞船.craft`.
  - Long forms KSP writes:
    - `attN = <node>,<partId>_px|py|pz_dx|dy|dz_px0|py0|pz0_dx0|dy0|dz0`, with free nodes as `<node>,Null_0_...`.
    - `srfN = srfAttach,<parentId>,,<nodePos>,<nodeDir>,<origPos>`.
  - Short forms KSP accepts on load: `attN = <node>,<partId>_0|y|0` and `srfN = srfAttach,<parentId>`.
  - A part whose attachRules forbid surface attach (e.g. Rhino `Size3AdvancedEngine`) resaves as `srfN = ,<parent>,,0|0|0,0|1|0,0|0|0`.
  - The extended attN format with direction vectors is also accepted (the only reusable fact from `mod_craft_assets.py:91-240`).
- **`_attach(parent, child, parent_node, child_node, up=False)`** [rewrite], :1007-1014. Today it sets `child.y = parent.y ± (h_parent + h_child)/2`. Replace with child_pos = parent_pos + R_parent·node_parent − R_child·node_child, using real cfg/live node vectors. KSP re-snaps stack children from the attN offsets in the file.
- **`_attach_surface(parent, child, pos_xyz, rot_quat)`** [port with fixes], :1016-1027.
  - Place the part on the parent's real surface radius.
  - Rotation quaternion `(0, sin(yaw/2), 0, cos(yaw/2))`, with the stock convention `yaw = 180° − az`, where `az = atan2(dz, dx)`.
  - Use attm=1, add `sym` links for symmetry groups, and refuse when attachRules forbid srfAttach.
- **`_node_position`** [rewrite], :1152-1161. It returns `0|±h/2|0`; replace with the real node vector.
- **`_replace_xsections`** [port], :1098-1150. A brace-balanced replacement of the contiguous run of `XSECTION{}` blocks, including nested `ATTACHEDFLAG{}`. This fixed the `ShipConstruct.LoadShip` NullReference (commit a572856). With a ConfigNode tree it becomes "set module.XSECTION list".
- **`_resources`** [port+generalize], :1163-1184. Emits `RESOURCE { name, amount, maxAmount, flowState = True, isTweakable = True, hideFlow = False, isVisible = True, flowMode = Both }`. Extend to every resource (EC, MonoPropellant, XenonGas, Ore) and to per-part fill fractions.
- **Fairing shell** [rewrite as `craft_fairing_enclose`], :480-551.
  - Current logic: payload_top = max part top, including radial reach and the docking-port top; base_r = max(bus, stack, appendage radius) + 0.10; XSECTIONs `[(0,r),(0.6h,r),(h,0.8r),(h+2.6r,0.2)]` with two-tab indent.
  - Keep the envelope computation. Pick the fairing size from the base diameter. The profile constants are arbitrary. Fairings are stageable.
- **Inter-stage staging rule** [rewrite into the stage-assignment helper], :464-611, 667-668. An inter-stage decoupler's istg equals the istg of the engines of the stage above it (render_index − 1). One activation then drops the spent stage and lights the next one.
- **Cluster ring math** [rewrite as helper], :613-628 and `design.py:291-320`. `ring_r = max(2·r_bell, r_bell/sin(π/n))`. Warn that surface-attached satellites keep their written y while the central engine re-snaps, leaving a 0.95 m offset. Prefer engine plates or symmetric radial nodes.
- **Radial boosters and drop tanks** [rewrite], :670-739, fixed on branch commit 3ba335f.
  - Pod parts must be CHILDREN of `radialDecoupler2`.
  - Crossfeed needs a `fuelLine` part: `CModuleLinkedMesh` with `tgt = <target uid>`, plus `sym` links.
  - Decoupler yaw is `180 − az`, placed at the real hull radius.
- **Static margin / fins** [rewrite as `craft_analyze` metrics], :818-884. Computes CoM (wet-mass weighted) and CoP (h·d weighted), and margin in calibres. Rendering must not mutate the design.
- **Landing-leg tip-over metric** [rewrite as `craft_analyze` metric], :886-919. `tipover = atan(leg_radius / h_cog)`, using the landed CoG.
- **`mod_craft_assets.ksp_root_from_save_vab`** [port], `mod_craft_assets.py:17-24` (returns `parents[3]`). **`write_renamed_craft(source, target_dir, craft_name, description_prefix)->Path`** [port], :27-46, becomes `craft_clone`.
- **Advisory design views** [port].
  - `tools/design_chart.py:300-408` `looks_like_a_rocket` → metrics {length, fineness, spans, static margin, checks}.
  - `render_svg` :523-608.
  - `tools/render_chart_png.py:30-68` runs headless `chrome|msedge` with `--headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=2 --default-background-color=FFFFFFFF --screenshot=<png> --window-size=W,H file:///<svg>`, 90 s timeout.
- **`verify_against_live(conn, design)`** [port into `vessel_readback` / `craft_diff`], `design_chart.py:670-719`. Reads mass, per-part positions in `vessel.reference_frame` and part count. Tighten the tolerances (the old 15% + 1 m hid node errors). Add per-part stage/decouple_stage, direction, parent and crossfeed.
- **`runner._load_and_launch` / `_wait_for_craft_ready`** [rewrite into the launch tool], `src/ksp_lab/runner.py:284-370`. Polls `/state` for editor + `lastCraftName` + `queueDepth==0`, settles 6-10 s, launches, re-issues once after 20 s, 180 s timeout. **`runner._craft_dir`** [rewrite], :501-522: use `/state.saveFolder`, with the newest-`persistent.sfs` mtime as fallback.

### 1.4 `catalog/`
- **`StockPart`** [rewrite], `src/ksp_lab/parts.py:102-140`.
  - Current fields: name, title, dry/wet mass, cost, height (node span), diameter (widest bulkhead), ASL/vac thrust, ASL/vac Isp, LF/Ox/SF, drag_area, fin_area, category, part_type, crew_capacity, stack_size.
  - Extend with: nodes [{id,pos,dir,size}] including variant overrides; node_attach; attachRules; fuelCrossFeed; stageable; propellants and ratios; full atmosphereCurve; min/max thrust; throttleLocked; gimbalRange; wheel torque; EC/Mono/Xe capacities; ModuleCommand minimumCrew; antenna power and relay flag; maxTemp; crashTolerance; all bulkhead tokens; techRequired; entryCost.
- **`live_part_name(cfg_name)`** (underscores → dots) and **`ALIAS_RENAMES`** [port], :151-155, 176-180. Aliases: RCSBlock→RCSBlock.v2, engineLargeSkipper→engineLargeSkipper.v2, Size3To2Adapter_v2→Size3To2Adapter.v2.
- **cfg helpers** [port into the shared ConfigNode parser], :200-259: `_strip_comments`, `_split_top_blocks`, `_field` (first match only, so variant `node_stack` lines are ignored), `_float`, `_readable_title` (the English text after `//` in an `#autoLOC` title).
- **`_node_height`** [rewrite], :262-286. Multiply node offsets by `scale × rescaleFactor`; Mammoth and Twin-Boar have only a top node. Store the vectors, not just a span.
- **`_node_stack_diameters` / `_bulkhead_info`** [port], :289-341. Tables: `BULKHEAD_DIAMETER_M` :74-81 (size0 0.625, size1 1.25, size1p5 1.875, size2 2.5, size3 3.75, size4 5.0) and `NODE_SIZE_DIAMETER_M` :90-96. Keep every token, not only the max.
- **`_parse_one_engine_module` / `_engine_module` / `_classify`** [port], :354-493. Store the propellant set and ratios and the full Isp curve. Role buckets are listed in the reader report. Known quirks: `fairingSize*` → misc; `Size1p5.Tank.05` → solid_booster.
- **`parse_part_cfg` / `parse_gamedata_tree` / `materialize_catalog` / `_apply_asl_thrust`** [port], :496-602.
  - Densities in t/unit: LF/Ox 0.005, SF 0.0075, Mono 0.004, Xe 0.0001, Ore 0.010.
  - ASL thrust = vac thrust × Isp_asl / Isp_vac.
  - Make the GameData path configurable (`DEFAULT_GAMEDATA_DIRS` at :187-190 is hardcoded) and include mods.
- **`reconcile_to_live` / `rebuild_from_live` / CLI `--from-live`** [port], :605-675, 911-950. Live data is authoritative. Drop parts the game did not load. Keep the cfg rocket mode when the live Isp is above 900 s.
- **Catalog loading and queries** [port as a `Catalog` object with explicit `refresh()`].
  - Loading: `load_catalog`, `_part_to_dict`, `_part_from_dict`, `part()`, :678-790. Drop `_TUNED_OVERRIDES` :727-731.
  - Queries: `engines(diameter_m, atmospheric, include_solid, standard_stack_only)`, `tanks(diameter_m, propellant, standard_stack_only)`, `parts_of_type`, `catalog_summary`, :796-851.
- **`stock_parts.json`** [rewrite as a regenerable offline cache]. 423 entries.
- **Fixtures** [port].
  - `tools/verify_parts.py:43-188`: ENGINE_TRUTH (e.g. `liquidEngineMainsail.v2` (6.0 t, 1500 kN, 285/310 s), `nuclearEngine` (3.0, 60, 185/800)) and TANK_TRUTH; `check_krpc_sample` :194-230.
  - `tools/validate_parts_live.py:101-259`: `cross_check_part_database` and `cross_check_in_use_parts` at 1% tolerance, skipping multimode parts when Isp > 900. Becomes `catalog_selfcheck`.

### 1.5 `orbital/` — pure Python math
- **`astro.py`** [port]:
  - `G0 = 9.80665` :17
  - `circular_speed(mu, r)` :24-26
  - `vis_viva_speed(mu, r, a)` :29-33
  - `orbital_period(mu, a)` :36-38
  - `hohmann(mu, r1, r2) -> (dv1, dv2, tof)` :41-52
  - `phase_angle_for_transfer(mu, r_target, t)` :55-61
  - `oberth_ejection_dv(mu, r_park, v_inf)` :64-71
  - `interplanetary_departure(...) -> {ejection_dv, v_infinity, transfer_time_s, phase_angle_rad}` :74-90
  - `transfer_departure_excess_speed` / `transfer_arrival_excess_speed` :93-116
  - `capture_from_excess(mu, r_peri, v_inf)` :129-136
  - `capture_dv(mu, r_pe, sma_arrival, r_target_ap)` :139-148 (takes the live hyperbolic SMA)
  - `deorbit_dv(mu, r_ap, r_pe_cur, r_pe_target)` :151-156
  - `rocket_dv`, `propellant_mass_for_dv`, `twr` :163-180
  - `terminal_velocity`, `parachutes_for_touchdown` :298-323
  - `suicide_burn_altitude`, `hoverslam_reference_speed` :326-343
  - `burn_time_s(mass_t, thrust_n, dv, isp)` :372-383
  - `link_range_m`, `link_closes` :393-404
  - `solar_power_fraction` :407-412
  - `flyby_bend_angle_rad`, `gravity_assist_delta_v` :415-428
  - `dynamic_pressure` :431-434
  - Fixes: raise instead of returning 0; return an explicit "infeasible" when a_net ≤ 0; take Isp at a stated pressure.
- **Rewrites in `astro.py`:**
  - `gravity_drag_loss` / `ascent_dv` / `surface_to_orbit_dv` :189-236 → a labeled `rough_ascent_estimate` range with its assumptions.
  - `finite_burn_lead_s` :366-369 → `t_half = (m0/mdot)(1 − exp(−dv/(2 ve)))`.
  - `hoverslam_throttle` :346-363 moves into the landing reflex.
- **`guidance.py:34-192`**: the same formulas with mass in KG (astro.py uses TONNES). Keep one module and pick one unit convention. Also has `suicide_burn_distance_m` and `capture_burn_estimate`.
- **`plan.py:23-110`** planners [port]: `circularize_at_apoapsis(mu, r_ap, r_pe) -> {dv, prograde, normal, radial}`, `deorbit`, `capture`, `interplanetary_transfer`, `landing_burn`, `node_burn_time(mass_t, F, dv, isp) -> {burn_time_s, lead_s}`.
- **`_opposite_apsis_delta_v_mps(*, mu, body_radius_m, burn_altitude_m, current_opposite_altitude_m, target_opposite_altitude_m)`** [port], `flight_controller.py:1329-1347`.
- **Node planners** [port as pure functions returning `{ut, prograde, normal, radial}`], `tools/deploy_relay_transfer.py:222-250, 844-871` and `tools/crewed_eve_roundtrip.py:529-564`: circularize (valid only at an apsis), Hohmann first burn, lower apoapsis at periapsis, lower periapsis at apoapsis (or now if hyperbolic).
- **Vector helpers** [port]:
  - `transfer_planner.py:28-46`: vadd, vsub, vscale, vdot, vcross, vnorm, vunit, `rotate_about_axis` (Rodrigues).
  - `flight_controller.py:1966-2010`: `_signed_angle_around_normal`.
- **Transfer planner:**
  - `lambert_izzo(mu, r1, r2, tof, M=0, prograde=True, low_path=True) -> (v1, v2)` [port + fix], `transfer_planner.py:52-157`. Branch bug at :134: it uses `i_h[2]`, but kRPC +y is north. Solve both branches and keep the one with `dot(r1 × v1, h_departure) > 0`.
  - `body_state(body, ut, ref, eps=5.0)` [port; prefer element propagation] :163-174.
  - `hohmann_time`, `synodic_period` [port] :180-187.
  - `find_transfer_window(sc, dep, tgt, ut_now, n_coarse=160, tof_refine=True) -> {ut_dep, tof, vinf_dep, vinf_mag, c3, synodic, hohmann_tof}` [rewrite; add arrival v∞, expose the search parameters, optional porkchop] :193-277.
  - `ejection_periapsis_direction(vinf_vec, r_park, mu_dep, h_hat) -> (dir, nu_inf)` with `nu_inf = arccos(−1/e)` [port; tested at `tests/test_transfer_planner.py:8`] :291-301.
  - `plan_ejection_node` [rewrite: the normal component is wrong] :304-358.
  - `plan_transfer` two-clock loop [rewrite as a repeatable tool step] :361-381.
- **Bodies:**
  - `params_from_krpc(krpc_body)` [port → `body_info`], `bodies.py:135-155`.
  - `synchronous_altitude_m` [port] :158-168.
  - `Body` dataclass [rewrite] :19-44.
- **Geodesy:** `eva_control.geodesic(lat1, lon1, lat2, lon2, R) -> Geodesic(distance_m, bearing_deg)` and `destination_point` [port], :22-87.
- **Design kernels:**
  - `_tank_count_for_dv(dv, mass_above_t, (eng_dry, eng_wet), n_eng, tank_dry_t, tank_wet_t, isp_s)` = `ceil(M(R−1)/(tank_wet − R·tank_dry))` [port], `design.py:354-373`.
  - `staging_plan` [port on new schema] :812-865.
  - `_single_stage_ceiling` / `_split_phases` [rewrite as advice] :868-900.
  - `_size_one` / `_search_pool` / `_size_stage` [rewrite → ranked `stage_options`] :376-483.
  - `_size_radial_boosters` [rewrite] :504-613.
  - `_estimate` / `radial_booster_masses` [rewrite] :922-995.
  - Engine pools [rewrite as filters] :178-269.
  - `parachute_count` [rewrite; uses bus mass only] :341-351.
- **Δv routing:** `astra/mission_graph.py:185-306` (`_classify_transfer`, `_hierarchical_transfer_math`, `_planet_transfer_math`) [rewrite as advisory `dv_map` routed through the common ancestor].
- **Plan lint:** `astra/plan_validator.py` precondition chaining [optional rewrite as advisory `plan_lint`; never a gate].
- **Transfer seeds:** `_estimate_mun_transfer_seed` (`flight_controller.py:1827-1866`) [rewrite as a generic child-body seed, without the Mun clamps]. `_next_duna_window_ut` (`deploy_relay_transfer.py:568-593`) [rewrite as a quick two-planet estimate].

### 1.6 `session/` — kRPC connection and observation
- **Connection:** `_connect` [rewrite], `flight_controller.py:99-112`. `krpc.connect(name, address='127.0.0.1', rpc_port=50000, stream_port=50001)`.
- **Snapshots:** `execute.measure(vessel) -> {mu, body, body_radius, atmo_top, surface_g, surface_rho, r_apoapsis, r_periapsis, apoapsis_alt, periapsis_alt, sma, eccentricity, mass_t, thrust_n}` [port], `execute.py:29-48`. `execute.attitude(vessel) -> (tilt_deg, |hs|, |vs|)` [port], :71-83.
- **Logging:** `TelemetryRecorder.append` (JSONL, UTC timestamp, sort_keys) [port], `telemetry.py:10-67`. `_record_live_sample` [rewrite → stream snapshot], `flight_controller.py:4307-4332`.
- **Name matching:** `vessel_names_match(actual, wanted) -> bool` [port], `vessel_match.py:44-67`. Its suffix list includes 'debris', so callers must filter on vessel type.
- **Vessel selection:**
  - `_select_vessel` / `_vessel_is_usable` / `_reacquire_vessel` [rewrite], `flight_controller.py:114-179`.
  - `_make_active` [port], `tools/eve_two_ship_return.py:155-167`.
  - `primitives.select_vessel` [port], :125-151.
  - `_select_lander_vessel` [port as `reacquire_vessel(criteria)`], `astra/primitives.py:916-942`.
- **Enums:** `_speed_mode_value` resolver [port], from `flight_controller.py:1195-1248`.
- **Universe snapshot:** `planning_context._body_record` / `_live_universe` [port], `astra/planning_context.py:91-180`.
- **Fuel readouts:** `_fuel_fraction` / `_resource_fraction` [rewrite: per-stage], `flight_controller.py:1384-1406, 1650-1661`.
- **Relative geometry:** `_relative_distance_m`, `_closest_approach_m`, `_first_docking_port` [port], `flight_controller.py:346-352, 431-442, 839-842`.
- **Encounter checks:** `_duna_closest_approach_km` [port, generalized], `deploy_relay_transfer.py:343-364`. Patched-conic walkers [port as `predict_trajectory`]: `flight_controller.py:2012-2076`, `deploy_relay_transfer.py:52-67, 802-816`, `crewed_eve_roundtrip.py:387-401`.
- **Recorder:** `tools/_ascent_telemetry.py:1-35` [rewrite as a stream-backed recorder].

### 1.7 `flight/` — reflex controllers (run in the daemon, parameterized by the AI)
- **Attitude:**
  - `_point_at_node` [port; use `node.reference_frame` (0,1,0)], :1136-1170.
  - `_point_orbital_prograde` / `_wait_for_autopilot_alignment` [port], :1172-1193, 1349-1363.
  - `_target_local_up` / `_target_surface_pitch_heading` [port], :1102-1108, 1128-1134.
- **Throttle:** `_maneuver_node_throttle(phase, remaining_dv, mass_kg, thrust_n)` [port the general branch], :1274-1301. Full throttle until remaining < `max(2, 2.5·accel)`, then proportional with a 0.05 floor. Make feather time and floor parameters.
- **Staging facts and guards:**
  - `_stage_status` [port], :1064-1096.
  - `_should_stage` and friends [rewrite as an advisor], :1014-1062, 1098-1100.
  - From `tools/deploy_relay.py` [port]: `_depth_from_root` :27-40; `_part_is_below` (kRPC `==`) :43-65; `_root_side_keeps_engine` :68-85; `_inter_stage_decouplers` (drop `exclude_split_interface`) :88-118; `_guarded_decouple` :213-231; `_stage_below_has_fuel` (generalize to every propellant) :234-251.
  - `_separate_attached_boosters` [rewrite] :254-287.
  - Bottom-engine dry-streak separation loop [rewrite as the staging reflex] :545-599.
- **Ascent:**
  - `_ascent_has_failed(state_history, *, post_staging_part_count, payload_part_count, atmosphere_top_m, target_apoapsis_m, bad_streak=3) -> (failed, reason)` [port], `deploy_relay.py:121-210`, mirrored in `astra/replay.py:80-171`.
  - Launch start (`_start_launch_sequence`, `_next_launch_clamp_stage`, `_still_prelaunch`) [rewrite: `LaunchClamp.release()` after verifying TWR > 1], `flight_controller.py:1365-1370, 1408-1450`.
- **Separation:** `_jettison_service_section` / `_has_live_engine` [port], `astra/primitives.py:872-913`. `jettison_transfer_stage` [rewrite], :945-1013.
- **Burn executors:**
  - `_execute_node_manually(conn, sc, v, max_burn_s, max_throttle)` [port; the best executor core], `deploy_relay_transfer.py:262-340`. Builds the direction from node scalars and live r, v in `body.non_rotating_reference_frame`; integrates Δv over `sc.ut`.
  - `execute.execute_node` [rewrite: keep refuse-if-misaligned >12° and `timeout = max(timeout, 1.8·burn+30)`], `execute.py:105-169`.
  - `_execute_node` [rewrite: extract pluggable stop/abort predicates], `flight_controller.py:2078-2516`.
  - `_execute_simple_node` [merge], :493-536.
  - `_execute_precise` / `_wait_node_done` [rewrite as the MechJeb executor path], `deploy_relay_transfer.py:819-841` and `mj_to_mun.py:24-42`.
- **Burn-to-condition reflexes:**
  - `_execute_mun_apsis_node` [rewrite as `burn_until(element)` with wrong-way and no-thrust detection], :3174-3395.
  - `_circularize_simple` coast guards [rewrite], :1452-1593.
  - `_circularize_at_apoapsis` / `_raise_apoapsis` [rewrite: stop when eccentricity stops falling, `best + 0.004`; SOI-edge escape guard at 0.9·SOI], `deploy_relay_transfer.py:70-143`.
  - `_retro_capture(ap_target_m, pe_floor_m, max_s)` [rewrite as a capture `burn_until`], `mj_to_mun.py:45-96`.
  - `_capture_mun_orbit` lessons [rewrite], :2929-3172.
  - `raise_and_circularize` [rewrite], `deploy_relay.py:622-652`.
- **Node search:**
  - Objectives [rewrite into `search_nodes`], `deploy_relay_transfer.py:385-565`: `_score_duna_node`, `_search_duna_correction_grid`, `_search_duna_periapsis_lower` (total-Δv objective), `_score_ejection_node`, `_frange`.
  - Pattern search in `_optimize_intercept_node` [rewrite], `flight_controller.py:444-590`.
  - Mun search family [rewrite], :1752-1964.
- **Rendezvous and docking:**
  - `_null_relative_velocity` [port; use a non-rotating frame at km range], :592-619.
  - `_translate_to_target` (√d speed profile) [port], :625-662.
  - `_approach_and_dock` / `_orient_target_port_to_chaser` / `_undock_after_transfer` [rewrite as an RCS fallback], :664-856.
  - `fly_mj_dock` flow and docked predicate [port, minus the refuel], `tools/fly_mj_dock.py:25-160`.
  - `undock_ferry`, `_tug_pod_part`, `transfer_kerbal_to_tug` [port], `eve_two_ship_return.py:392-549`.
  - `_match_orbital_plane` [rewrite; it burns along the wrong axis], :354-429.
- **Landing and surface:**
  - `_land_on_mun` hoverslam law [rewrite], :3754-3962.
  - `execute.propulsive_landing` post-landing tilt check [rewrite], `execute.py:198-261`.
  - Mun settle detector and surface liftoff [rewrite], `flight_controller.py:1110-1126, 4002-4137`.
  - `_recover_on_kerbin` / `mj_land_vessel` [rewrite as `mj_land` plus `descent_monitor`], `flight_controller.py:4238-4305` and `tools/mj_land_vessel.py:28-114`.
  - `primitives._land_via_mechjeb` [rewrite], :499-584; `_ascend_via_mechjeb` [rewrite], :673-722; `recover` [rewrite], :1016-1077.
  - `descend_and_recover` / `_deploy_chutes` [rewrite], `crewed_eve_roundtrip.py:577-678`.
- **Warp:**
  - `_set_physics_warp` [port], :1372-1382.
  - `execute.warp_to_ut(sc, target_ut, chunk_days=30.0)` [port + safety caps], `execute.py:93-102`.
  - `_warp_via_high(sc, target_ut, buffer_s=7200)` [rewrite without hardcoded bodies], `deploy_relay_transfer.py:874-895`.
  - SOI coast helpers [rewrite], `flight_controller.py:2518-2576, 4207-4236` and `deploy_relay_transfer.py:367-382`.
- **Part actions:**
  - `jettison_payload_fairings` / `commission` [port; fix the `/vessel/type` call], `deploy_relay.py:655-700`.
  - `_perform_mun_surface_science` [rewrite on kRPC `Experiment`], :3692-3752.

### 1.8 `game/`
- **`ensure_ksp_ready(config, save_name, launch_timeout_s=300, poll_interval_s=2, probe_timeout_s=1, load_save_timeout_s=120, ...) -> ReadyState{running, bridge_up, save_loaded, detail}`** [port; add a kRPC port-50000 probe], `astra/autostart.py:68-75, 143-163, 178-190, 216-298, 301-340`.
- **`post_bridge_status(phase, message)`** [port], `astra/agent.py:25-38`.
- **Operator inbox concept** [rewrite], `tools/astra_daemon.py:25-60`.
- **`_save_cleanup`** [rewrite as `prune_save(keep_predicate, dry_run)`], `tools/_save_cleanup.py:22-103`. Brace-balanced VESSEL removal and an `activeVessel = N` re-index.
- **Connection settings and scene timeouts** [port], `configs/local-ksp.yaml:34-45`.
- **`validate_against_game`** [port as `self_check`], `tools/validate_against_game.py:75-160`.
- **Bridge debug subcommand** from `cli.py` [rewrite as a tiny dev CLI].

### 1.9 `knowledge/` and journal
- **`docs/USING_KRPC_AND_MECHJEB.md:1-347`** [port]. Remove the refuel advice (107, 121, 262-273) and update the endpoint table (95-100).
- **`docs/GENERALIZED_AEROSPACE_METHODOLOGY.md`** [port selectively]: principles P1-P16 (67-202), craft contract (207-240), prompt summary (348-357). Rewrite the phase table (243-261) and discard the stale gaps section (269-298).
- **`failure_rules.json`** [port the schema `{id, marker, symptom, cause, fix, applicable_primitives, confidence, tags}`]. 26 rules (the header says 27). Reword fixes away from old function names and Mun-only numbers, and key them on telemetry symptoms.
- **`RuleBase.query`** [port simplified], `astra/rule_base.py:45-254`.
- **`ExperienceLedger` JSONL** (utf-8, `ensure_ascii=False`) [port the pattern], `astra/ledger.py:141-189`. SEED_RULES are discarded (duplicates).
- **`KnowledgeBase.context_text`** [rewrite], `astra/knowledge.py:30-92`.
- **Metacognition** (`record_discrepancy`) [rewrite simplified into prediction/observation logging], `astra/metacognition.py:132-358`.
- **Trace replay** [rewrite for offline regression of monitors], `astra/replay.py`.
- **Notebooks** [harvest into the knowledge base]:
  - `docs/artemis_mun_engineering_notebook.md`: constants 104-107, equations 111-150, Do-Not-Repeat 378-401.
  - `docs/artemis_precision_design_notes.md:39-147`.
  - `docs/CONSTELLATION_DESIGN.md:1-44`.
  - `docs/MUN_FLIGHT_LIVE_TODO.md:11-37`.
  - `docs/AUDIT_2026-06-22.md:38-51` (backlog: `/mj-warp`, `/mj-vesselstate`).
- **Rules and personas:**
  - `AGENTS.md` RULE 2, RULE 3 and 74-79 [port].
  - `data/knowledge_base/sources.yaml` [port].
  - `skills/craft-design.md:13-59` contract [port].
  - Per-phase skills [rewrite: keep the math and failure markers].
  - `.claude/agents/ksp-commander-audit.md:17-42` philosophy [rewrite as a critic persona].
  - `astra/interpreter.py:125-176` "flight director" framing [mine for the system prompt].
  - `skills/00-orchestration.md:98-116` "fix one thing per retry" discipline [keep].
- **Evidence index (commit hashes):** 1b535a2, 1163c4c, c7f2c87, 0c38520, 9156e95, 664f5e5, 64d4aa6, 9938f3a, 007d3af, c621db2, 78bf7e9, 1b809b7, 3ba335f, 15b9d1e, bf046b2, 4a56e1d, 5f72884, 48c28ab.

### 1.10 `advisors/` — optional second model
- **`llm_cli.call_llm_cli`** [port], branch-only commit 48c28ab.
  - Codex argv: `[codex.exe, 'exec', '--sandbox', 'read-only', '--skip-git-repo-check']`.
  - Claude argv: `[claude, '-p', '--output-format', 'text']`.
  - The prompt goes on STDIN (utf-8).
- **`codex_review`** [port as optional `second_opinion`], `astra/codex_review.py`: `_resolve_codex_bin` :42-64, `_build_command` (`-i png...`, prompt on stdin) :113-119, `parse_codex_reply` :122-153, `codex_review_three_view` :156-214, 6-point checklist prompt :70-97.
- **Last-balanced-JSON extractor** from the branch's `interpreter._extract_json` [port].

---

## 2. Discard list

**A. Fixed mission programs.** These are the pattern the owner rejects: whole missions or phases hard-wired as code.
- `tools/fly_*.py` (8 files: `fly_dock`, `fly_hls_predeploy`, `fly_hls_sortie`, `fly_mun_roundtrip`, `fly_orion`, `fly_orion_to_orbit`, `fly_relay_once`, `fly_to_lko`).
- `tools/crewed_eve_roundtrip.py` (except `board_crew`'s verification idea).
- `tools/eve_two_ship_return.py` (except the crew-move and undock helpers).
- `tools/eve_flag_mission.py`: a 15-minute human-flag pause and a Gilly script.
- `tools/fly_starship.py`, which is cheat-heavy.
- `tools/commander.py` (`divide_mission`, a 6-step plan, refuel on retry).
- `tools/mj_to_mun.py` main.
- `tools/_rescue_eve_relay.py`.
- `tools/design_eve_two_ship.py` and `tools/design_gilly_excursion.py`.
- The transfer drivers `transfer_to_mun`, `transfer_to_duna`, `transfer_to_body` and `main` in `deploy_relay_transfer.py`.
- `launch_to_lko` (`deploy_relay.py:290-619`).
- `KrpcFlightController.fly`, `run_hls_surface_sortie`, `run_orion_return`, `run_dock_and_transfer`, and every `_fly_mun_*`, `_return_*` and `_transfer_and_capture_mun_orbit` path.
- The Mun recovery branches (`_correct_missed_mun_transfer`, `_top_off_mun_transfer`, `_raise_mun_periapsis`, `_correct_mun_soi_periapsis`).
- `_find_kerbin_return_node`, which brute-forces about 4,750 live nodes.
- `_descend_to_gilly_surface`.
- The legacy terminal-descent ladders (`guidance.py:208-307`).

**B. Open-loop agent scaffolding.**
- `AstraAgent`: one-shot `interpret()`, and retries that re-run identical args.
- `Interpreter`, including its keyword fallback, which contradicts the README.
- `planner.decompose` / `parse_intent` / `_apply_mission_aware_launch`, with hand-tuned margins.
- `primitives.py` as an architecture: `sys.path` hacks and a global `drt.cfg`.
- `plan_validator` as a hard gate.
- `ai_provider.ExternalDesignProvider`.
- The `cli.py` trial loop, `research.py`, `storage.TrialDatabase`.
- `AutomationRunner` (the trial loop plus the Artemis pipeline gated on phase strings).
- `skills/00-orchestration.md`, `skills/mission-planning.md` and `skills/README.md`: a fixed capability set dispatched to drivers.
- `configs/missions/*.yaml` and `scripts/run_offline_smoke.ps1`, which hardcodes its mission string.

**C. Auto-designer and blind optimizer.**
- `design.design_ship`: the monolithic feasibility gate. It produced 55% reserve and put a sea-level aerospike on a vacuum stage.
- `mission_upper_phases`, whose `twr_body_g > 5` switch means "atmospheric" and misroutes stages.
- `separation_sequence`, a canned relay script.
- `_bus_mass`.
- `budget.mission_budget`: Kerbin hardcoded as the launch body, a fixed phase ladder, atmospheric descent charged full ascent Δv.
- `HistoryOptimizer`: substring matching on failure text, +8% Δv / +0.25 TWR steps, random jitter.
- `MissionScorer`.
- The `MissionSpec` defaults (80 km, 4500 m/s).
- `TelemetrySummary`, `ScoreResult`, `TrialRecord`, `TelemetryRecorder.summarize`.
- `OfflineSurrogateController`, which fabricates success telemetry.
- `parts.payload_bus_mass` / `estimate_design`, which fakes the part count.

**D. Cheats.**
- `/vessel/refuel` and every caller: `mj_to_mun.py:168`, `fly_mj_dock.py:109`, `eve_two_ship_return.py:349`, `flight_controller.py:1595-1648, 3006-3032` ("modeled orbital refuel"), `commander.py:332, 436`, and `fly_starship.py` (launching dry, surface "ISRU").
- `/spawn-crew`: teleports or creates kerbals mid-flight.
- `/transfer-crew` across unconnected vessels.
- `_perform_mun_surface_science` reporting science "modeled" as success.

**E. Hardcoded craft policy in the writer.**
- The fixed bus (`craft_writer.py:350-462, 741-816, 921-940`): a mk1pod.v2/probe root, the HeatShield1 → Decoupler.1 → probe → kv2Pod column, always three advSasModule, always RA-100 + battery + solar + RTG, an RCS quad, chute radii and azimuths.
- The payload stand-in: ServiceBay.125.v2 copies at `round(payload_t/0.1)`.
- A 1.25 m Decoupler.1 on every stack.
- Three hardcoded adapter pairs.
- An always-fairingSize1 ogive.
- The interstage procedural shroud (`:629-666`). It killed engine thrust live.
- Aero sign-off constants.
- Mutating the design during rendering.
- Donor harvest (`_design_part_names`, `_part_source_dirs`, `_part_body_library`, :143-255): nondeterministic, reads KSP's autosave, and copies donor thrust limits, colours and variants.
- `render_from_template` with hardcoded PT Munsplorer uids.
- `HlsProjectCraftWriter`, `select_artemis_hls_craft_source`, `runner._write_artemis_orion_craft`.
- `_TUNED_OVERRIDES` (fin area 1.0/2.0, chute 489 m² presented as catalog truth).
- `ENGINE_BELL_FRAC` 0.36 and `PLATE_FACTOR` 1.5.

**F. Magic numbers used as control logic.** All become AI-chosen or derived arguments.
- Ascent:
  - Linear pitch 90°→0° over 1-40 km, cutoff at 1.10 × target (`flight_controller.py:869-910`).
  - Mun pitch ladder (:1111-1126).
  - Circularize at tta ≤ 35 s, "orbit" means pe ≥ 70 km (:1495-1554).
  - Stuck-on-pad thresholds (:939).
- Transfers:
  - TMI clamps 740-980 m/s, fallback 860 (:1774-1862).
  - Mun periapsis bands 35-120 / 45-90 / 35-180 km (:1964, 2049-2066, 2222, 2469).
  - Apoapsis caps 14.5 / 22 Mm (:1303-1306).
  - Correction grid sizes and caps: 400/700/1400/2500 m/s across commits.
  - Duna/Eve insertion lookup tables (`deploy_relay_transfer.py:1123-1137`).
  - 2,500 km periapsis floor; 6 h and 8 h gates; tof 0.40 yr (:914-1021).
- Capture:
  - Throttle ladders 0.04/0.14/0.22/1.0 (:3060-3118) and 1.0/0.30/0.07 (`mj_to_mun.py:85-90`).
  - Capture bands 20-360 km; lead floors 35/25/20 s.
- Landing:
  - Hoverslam 0.92 fraction, 80 m margin, 1.35·stop + 3000 m gate, 70 m flare, flare ladders (:3754-3962; `guidance.py:224-306`).
- Reentry:
  - pe 32→25 km (`primitives.py:1036-1039`), 35 km aerobrake.
  - Chute gate 250 m/s / 5 km as a trigger (`crewed_eve_roundtrip.py:99-102`).
  - Deorbit 0.4/0.2 × atmosphere with an 8 s sleep and 0.7 throttle (`primitives.py:533-545`).
  - Warp ladders keyed to 80 km, 250 km, 1000 km (`flight_controller.py:4291-4295`, `mj_land_vessel.py:81-83`, `execute.py:236-240`, `deploy_relay.py:646-648`).
- Rendezvous and docking gains (`flight_controller.py:382-784`).
- Execute-node constants: 2° / 12° / 30 m/s / 0.5 m/s / 120 s (`execute.py:125-160`).
- Design and budget:
  - Margins in `planner.py:24-32`; reserves in `design.py:47-54, 144, 793`.
  - Flat mission_graph Δv: aerocapture 120, recover 200, dock 30, rendezvous 80, land = deorbit + 150.
  - Body helpers: `low_orbit_radius_m` (1.15 × atm), `safe_periapsis_floor_m`, `capture_apoapsis_ceiling_m` (0.35/0.06 SOI).
- Timeouts as logic: 1800/1500/2400/900 s polls; fixed sleeps after vessel switch and staging (1, 2, 2.5 s).
- Engine ignition by the design's engine-name strings (`fly_starship.py:145-185`, `deploy_relay.py:438-470`).

**G. Static data that duplicates live data.**
- `bodies.py` catalog: 8 of 17 bodies; unknown names fall back to Kerbin; Duna rotation speed 29.36 (true value 30.69) and Ike 18.6 (true 12.47).
- Aero model (`astro.py:247-290`).
- `data/knowledge_base/principles.md` (flat 4500/7500 budgets); keep only the craft-location facts on lines 11-14.
- Artemis source notes.
- The `configs/local-ksp.yaml` `mun_relay_*` keys and the hardcoded `saves/默认` / PT Munsplorer paths.
- `DEFAULT_GAMEDATA_DIRS`.

**H. Dangerous or broken helpers.**
- `execute._ignite`: sets `active=True` on every engine each tick.
- The `_point_orbital_retrograde` fallback that targets (0,1,0), i.e. prograde, tuned to one craft (`flight_controller.py:1233-1235`).
- The SAS + fixed-sleep "aligned" logic.
- `_match_orbital_plane` (wrong burn axis).
- `launch_to_lko` recovering every landed/splashed Kerbin vessel.
- `_warp_via_high`'s hardcoded bodies.
- Throwaway "kick" connections.
- Per-phase `krpc.connect`.
- The English keyword science scan.
- `_execute_mun_apsis_node`'s invert-direction hack and 0.18 throttle cap.
- `transfer_excess_speed` alias.
- `ejection_dv` duplicate.
- `replay.py`'s fixed transfer/docking/recovery state machines.

**I. Bridge/infra leftovers.**
- JSON regex parser; `/reset`; `scripts/install_bridge.ps1` (redundant).
- The stale `KspAutomationBridge.dll.new` (a downgrade hazard).
- The `.bak*` files, which are inert.
- The five kRPC services with no backing mod (KerbalAlarmClock, RemoteTech, DockingCamera, LiDAR, InfernalRobotics).
- The duplicate "Default Server" entry in `GameData/kRPC/PluginData/settings.cfg`.
- The mandatory Codex three-view gate (`AGENTS.md:38-44`, `primitives.py:286-315`): it logs and proceeds regardless, so it cannot change the outcome.

**J. Stale documents.**
- README (claims a no-API-key CLI that is not on main).
- AUDIT trail beyond the backlog.
- `GENERALIZED_AEROSPACE_METHODOLOGY` §5 "honest gaps".
- MUN_FLIGHT_LIVE_TODO watch-list (44-108).
- The Artemis craft-sourcing sections.

---

## 3. Live lessons (deduplicated, grouped)

### 3.1 Windows, Python and machine facts
- Find Python with `Get-Command -Name python,python3,python3.13`; a bare `Get-Command python` misses the WindowsApps 3.13. Run with `PYTHONPATH=src`. The MS-Store Python cannot read files under `AppData\Local\Temp`, so put scratch scripts in `C:/tmp`.
- The console codepage is GBK. Write files and subprocess pipes with `encoding='utf-8'`, set `PYTHONIOENCODING=utf-8`, and keep log prints ASCII (Ø, ✓ and emoji crash it).
- The save folder `默认` shows as mojibake in PowerShell; Python reads it correctly from UTF-8 YAML. Saves on disk:
  - `codex-audit`: SANDBOX, 70 vessels, 259 VAB craft, currently ACTIVE.
  - `默认`: SANDBOX, 146 vessels, 270 VAB craft.
- Start KSP with `cmd /c start "" steam://run/220200` (exe `C:/Program Files (x86)/Steam/steamapps/common/Kerbal Space Program/KSP_x64.exe`). Never start a second KSP.
- Installed stack:
  - KSP 1.12.5 build 03190. The game runs zh-cn per KSP.log, although `settings.cfg` says en-us.
  - MechJeb2.dll 2.15.3.0 (KSPAssembly V2.15.3, assembly 2.15.0.0) and MechJebLib 1.0.0.0.
  - kRPC 0.5.4.
  - ModuleManager 4.2.2, Harmony, KSPCommunityFixes, B9PartSwitch 2.21.0.4, Benjee10 Orion/SLS, HLS_project, StarshipExpansionProject, Waterfall.
- Run long flights as tracked background tasks. A short shell timeout killed a reentry script mid-flight and caused a 238 m/s impact.
- With about 102 vessels in the save the game ran about 3x slower than wall clock. Pruning to 36 cut lag roughly 3x, and each interplanetary attempt still took 50-60 minutes.

### 3.2 Bridge build and install
- Plugins load only at KSP startup. Every change needs build, install, restart. The symptom of a stale DLL is "Unknown route: POST /mj-plan". Once, `build_bridge.ps1` compiled to `bin\Release` without installing (commit 61bd6d6), so after each build check the installed DLL's size and timestamp.
- A running KSP memory-maps the DLL ("user-mapped section open"). Stage `.dll.new` and swap it after `KSP_x64` exits. KSP loads only `*.dll`, so `.bak`/`.new` files are inert.
- **Downgrade hazard on disk right now:** `KspAutomationBridge.dll.new` (60,416 B, 2026-06-26 04:30) is OLDER than the installed DLL (77,312 B, 15:15). `finalize_bridge_install.ps1` does no version check, so running it would lose `/part-database`.
- The only working compiler is `csc` v4.0.30319, which is C# 5 only: no `$""`, no `?.`, no `nameof`, no expression-bodied members. Under Git Bash use `-flag` (not `/flag`), export `MSYS2_ARG_CONV_EXCL='*'` and `MSYS_NO_PATHCONV=1`, and pass absolute backslash paths.
- The version was never bumped: every DLL reports 0.1.0.0 (KSP.log "KspAutomationBridge v0.1.0.0"), so builds cannot be told apart.
- Compiling against the installed MechJeb2.dll turns renamed members into compile errors. KRPC.MechJeb's string reflection would silently disable them instead (KRPC.MechJeb is not installed anyway). Dev-build names: `core.Target` (not `core.target`), read-only `EditableDouble.Val`, PascalCase `Users`.
- `GetMasterMechJeb()` returns null without a `MechJebCore`. The `MechJebForAll.cfg` patch fixes this after a reload (confirmed near KSP.log line ~4726).

### 3.3 Bridge protocol behavior
- Only string JSON values are parsed. Numbers and bools are ignored and the defaults are used: `{"altitude":80000}` becomes 100000. `\uXXXX` is not decoded, so `ensure_ascii` does not help.
- A non-ASCII body hangs until the client times out, and then the command executes anyway.
- Query strings return 404; pass parameters in the POST body.
- A 504 "Timed out waiting for Unity main thread" means the game is paused, loading or lagging. It does not cancel the job, which runs later. Check `/state` and `queueDepth` before retrying, or you get duplicate launches or loads.
- The socket answers about 40 s before MAINMENU. Poll `/state.scene`, not the socket.
- NaN/Infinity are serialized bare.
- `ResolveVessel` (behind `/vessel-info`, `/parts-list`, `/resources`, `/spawn-crew`) returns the ACTIVE vessel with `ok:true` on a miss. Always check the returned `vessel` field.
- A route that doesn't exist fails silently when the caller wraps it in try/except (`/vessel/type` never set the Relay type).
- `/save`, `/space-center` and `/fly-vessel` overwrite `persistent.sfs`, so there is no rollback point unless you make named saves.
- Per-route timeouts are fixed at 15/20/30/60/120 s whatever the operation. Heavy-craft launches exceeded them.

### 3.4 Scenes, saves, launching
- `/craft/load` is asynchronous. `loadedSceneIsEditor` turns true about 2 s before the parts exist, and launching into that empty editor silently does nothing. `lastCraftName` is set when the load is accepted. The old workaround waited for `lastCraftName == name`, then `queueDepth == 0`, then 6-10 s; the rewrite needs an editor part-count signal.
- `/launch` returns "Launch requested." even when LaunchSiteClear fails (a vessel on the pad) or the reflected invoke is dropped; the scene just stays EDITOR.
  - Heavy craft (about 58 parts) take more than 60 s from editor to FLIGHT.
  - The old runner re-issued `/launch` once after 20 s with a 180 s timeout.
  - Right now `AI-DEBUG-KerbalX` sits in PRELAUNCH on `codex-audit`.
- Headless launches assign no crew. If an empty crewed pod is the only command part, KSP shows an un-dismissable "no crew/probe core" dialog: the flight scene never loads, or `/launch` returns 400 "Object reference not set".
  - Always include a powered probe core.
  - Set crew at launch (kRPC `launch_vessel(crew=[...])`) and check `crew_count` immediately.
  - Pods launched through `/craft/load` + `/launch` arrived EMPTY, which is why the `/spawn-crew` cheat existed.
- Craft files must go into the ACTIVE save (`HighLogic.SaveFolder` = `/state.saveFolder`). Writing to the configured `默认` while `codex-audit` is running gives 404 "Craft file does not exist".
- kRPC cannot enter flight on an existing vessel from the Space Center or Tracking Station; use `/fly-vessel`. kRPC `sc.load(name)` only loads `.sfs` files in the current save folder; switching save folders is bridge-only.
- `/load-save` must use `GamePersistence.LoadGame(string,string,bool,bool)` followed by `game.Start()`. `LoadGameCfg` takes a ConfigNode, not a name (commits 9d64e88, 2b9e9ce).
- Any scene change (launch, load, quickload, revert, space-center) invalidates every kRPC object and stream ("Instance not found"). FLIGHT-only procedures raise "Procedure not available in game scene".
- A launch or revert can reset a clock that was warped on the ground, and MechJeb then plans the next window. Compare `node_ut` with now.
- Clear only the pad. Recovering every landed vessel on Kerbin destroys user bases and rovers.

### 3.5 Craft file format
- **Launch NullReference.** `EditorLogic.FinalizeAnalytics` threw on `/launch`. Bisection on a 4-part craft pinned it on a malformed top-level `ACTIONGROUPS{}`. Missing MODULE blocks, a missing STAGES block and the staging indices were each ruled out. Never emit top-level ACTIONGROUPS or STAGES. (The Override* header fields are disputed; see Open risks.)
- A PART with no MODULE blocks still loads and flies, because modules come from the prefab. Skipper v2, Swivel v2, Reliant v2, Nerv, Mammoth, Twin-Boar, RCSBlock.v2, adapterSize2-Size1 and fairingSize3 all flew with minimal bodies.
- **Never copy module state from other craft.** Donor harvest copied, among others:
  - the Mainsail at `thrustPercentage=73.5` (from `Ariane 5.craft`);
  - the Hammer SRB at 60% (from AeroEquus);
  - Size3To2Adapter.v2 with partial LF/Ox (from Dynawing);
  - the Poodle with variant SingleBell, and colour variants on tanks.
  Set `thrustPercentage` explicitly or omit ModuleEngines.
- **KSP re-snaps stack parts on load.** Every stack child ends at `child.y = parent.y + parent_attN_offset − child_attN_offset`, using the offsets written in the file; the file's `pos` is ignored. Surface children keep their written offset relative to the parent. Satellite engines hung 0.95 m below the snapped core engine (KE-1 −26.53 vs −27.48).
- **Real attach nodes are asymmetric.** Examples (top / bottom):
  - Swivel v2: 0 / −1.63
  - Mk1 pod v2: +0.642 / −0.405
  - Mainsail v2: +1.014 / −1.957 (variant top 0.533)
  - Skipper v2: +1.013 / −1.362 (variant 0.583)
  - HeatShield1: +0.022 / −0.17
  - FL-T400: +0.9817 / −0.9125
  - fairingSize1: +0.22 / −0.20, plus interstage nodes
  On resave KSP corrected some offsets and kept wrong ones for others (HeatShield1 ±0.096), leaving inconsistent geometry.
- **Staging on load.**
  - KSP keeps `istg` for stageable parts and sets −1 for non-stageable ones (pods).
  - Tanks and fins get the `istg` of the decoupler that separates them.
  - `dstg` is kept; `sidx`/`sqor`/`sepI` are recomputed, so writing −1 is fine.
  - KSP left `attm=0` on surface parts as written, whereas stock craft use `attm=1`.
  - Always read staging back after load.
- **Inverse staging rule (proven).** An inter-stage decoupler shares the `istg` of the engines of the stage ABOVE it. Giving it its own stage's `istg` split the craft on the pad. A launch stage with no decoupler above it drags the whole stack and crossfeeds everything at sea-level Isp.
- **Every payload stageable ended up in inverse stage 0** (chutes, heat shield jettison, capsule decoupler, fairing, payload decoupler), so one activation would fire them all. The old code fired these parts individually. Design distinct stages and never blind-stage a heat-shield craft.
- **Radial boosters.**
  - Pods must be children of the radial decoupler. As siblings, firing the decoupler dropped only itself: 86 of 87 parts stayed, dragging 9 dead SSMEs.
  - TT-70 `radialDecoupler2` has `fuelCrossFeed=False` and `crossfeedStatus=False`, in every stock craft checked. Without `fuelLine` parts (`CModuleLinkedMesh tgt=`), pods feed nothing; the core burns its own fuel in parallel, and dropping the pods left it short (the climb stalled at 19 km, commit 15b9d1e).
- **Stock surface yaw.** `yaw = 180° − az`, with `az = atan2(dz, dx)` and `rot = (0, sin(yaw/2), 0, cos(yaw/2))`. Verified on radial decouplers and fins in Kerbal X, Kerbal 1-5, GDLV3 and Z-MAP. Unity is left-handed: yaw θ maps local +X to (cos θ, 0, −sin θ). The old writer's `+az` is correct only at ±90°.
- KSP does not validate surface-attach positions: legs, decouplers and pods floated metres off the hull. Compute the point from the parent's real radius and read positions back.
- A procedural fairing used as an interstage shroud under an engine bell zeroes that engine's net thrust: the plume hits a part of the same vessel (LV-N at TMI: full throttle, g_force 0). Stock engines already have `ModuleJettison` shrouds.
- XSECTION edits must be brace-balanced. A flat regex stopped at a nested `ATTACHEDFLAG{}` `}`, left a stray brace, and caused `KSPUtil.GetPartName(null)` in `ShipConstruct.LoadShip` → `/launch` 400.
- The `description` must be one line (multi-line corrupted the file, commit 1b809b7).
- Part ids are the live dotted name plus `_uid`.
- A part whose attachRules forbid surface attach resaves with an empty `srfN` node id.
- Only fly craft whose staging you generated and understand. Copied third-party craft (PT Munsplorer, ACK SLS, MUNSHIP) over-staged or under-staged under the controller, and MUNSHIP's descent engines never activated.
- The flown vehicle must be exactly the analysed one. The launch path once re-derived its own LKO-only design (commits 00a7c17, 1b535a2).

### 3.6 Parts and catalog facts
- **Part names.** cfg/persistence names use underscores; live names use dots (`Rockomax16_BW` → `Rockomax16.BW`). Legacy ids `RCSBlock`, `engineLargeSkipper` and `Size3To2Adapter_v2` do not load in 1.12.5. Category-`none` entries (liquidEngine, liquidEngine2, Size2LFB, smallRadialEngine, kerbalEVA with a meaningless 3.125 t) must be filtered out.
- **Live DB vs static catalog.** The live DB has 620 parts, including mods; the stock catalog has 423. Titles are localized zh-cn (e.g. "LV-T45“转轮”液体燃料引擎"), so key everything on internal name.
- **Engine data in `/part-database`.** Only the first `ModuleEngines` is reported: RAPIER shows 105 kN / Isp 3200 s (its jet mode).
- **B9PartSwitch resources are invisible.** `benjee10.SLS.BOLE.booster` lists only EC 120, and 42 engine parts show no fuel.
- **Pod masses.** Pods' live dry mass is 0.09-0.28 t lighter than the cfg value.
- **cfg parsing traps.**
  - Legacy `scale = 0.1` must multiply node offsets (the Reliant otherwise reads as 14 m).
  - Mammoth and Twin-Boar have only a top node.
  - Kerbodyne 3.75/5 m tanks are filed under Propulsion.
  - Variants carry alternative `node_stack` lines.
- **Engine curves.** `atmosphereCurve` key 0 is vacuum Isp and key 1 is 1 atm. `maxThrust` is vacuum thrust; ASL thrust = maxThrust × Isp(1)/Isp(0). Other pressures (Eve is about 5 atm) need the full curve.
- **True sizes.**
  - OKTO (`probeCoreOcto.v2`) is 0.625 m and RC-001S (`probeStackSmall`) 1.25 m, both 0.1 t.
  - `asasmodule1-2` is 2.5 m and `advSasModule` 1.25 m.
  - Mk2Pod is 1.875 m; crewCabin (Hitchhiker) is 2.5 m.
  - `adapterSize2-Size1` is a FUEL adapter (360 LF / 440 Ox, 4.57 t wet).
  - Heights: X200-16 1.84 m, X200-32 3.72 m, probe core 0.374 m.
- **Nerv.** The LV-N burns LiquidFuel only. On LFO tanks about 55% of the propellant is dead oxidizer; a lander starved at 13 km and crashed.
- **Sea-level thrust fraction.** Vacuum engines keep only 23-26% of thrust at 1 atm (Terrier 0.25, Nerv 0.23, Poodle 0.26). Sea-level engines keep 85-94% (Reliant 0.85, Mainsail 0.92, Vector 0.94).
- **Silent antenna fallback.** If `RelayAntenna100` was missing from the harvested library the writer fell back to `longAntenna`, and relays lost signal at Duna. Check the actual parts after load (commit 120be0c).
- **Mk16 chute.** Effective Cd·A is not in the cfg (only `fullyDeployedDrag`). 489 m² reproduces 1.2 t landing at about 6.5 m/s at Kerbin sea level. Calibrate live from observed descent speed.
- **Bounding box.** kRPC's bounding box returned degenerate ~1e18 m values in one frame. Measure length and diameter from part positions in the vessel frame; mass and part count are always reliable.

### 3.7 kRPC API semantics
- **Default `flight()` frame.** `vessel.flight()` uses the co-moving surface frame: speed and vertical_speed read ~0 all ascent, which falsely tripped a stuck-on-pad guard at ~4 km. Use `vessel.flight(vessel.orbit.body.reference_frame)`, with `body.non_rotating_reference_frame` for orbital speed. Key liveness on apoapsis.
- **Autopilot frame.** `AutoPilot.reference_frame` must not rotate with the vessel: `vessel.reference_frame` raises "Invalid reference frame; must not rotate with the vessel". A silent `except` hid this and about 13 docking attempts never pointed.
  - Allowed: `orbital_reference_frame`, `surface_reference_frame`, `surface_velocity_reference_frame`, body frames, `node.reference_frame`.
  - RCS translation (`control.right/up/forward`) IS in the vessel frame.
  - Never swallow control exceptions.
- **SAS errors.** Enabling SAS with the autopilot engaged throws "SAS cannot be enabled when the auto-pilot is engaged". Hold modes on basic probe cores throw "Cannot set SAS mode of vessel". This build has `SpeedMode.orbit`, not `.orbital`.
- **Node frames.** `node.reference_frame` has +y along the REMAINING burn, so target (0,1,0) tracks it. `burn_vector`/`remaining_burn_vector` default to `node.orbital_reference_frame`. `node.delta_v` stays fixed during the burn; `remaining_delta_v` shrinks.
- **Post-escape nodes.** On a fresh post-escape patch, `node.direction()`/`burn_vector()` throw a null WorldBurnVector. Rebuild the direction from the `prograde/normal/radial` scalars: pro = unit(v), nrm = unit(r×v), rad = pro×nrm, in `body.non_rotating_reference_frame`.
- **Node editing.** Adding or editing nodes needs the vessel ACTIVE and in flight (and a tracking-station upgrade in career mode).
- **Units and time bases.**
  - `orbital_speed_at(t)` takes seconds FROM NOW; `position_at`, `radius_at` and `true_anomaly_at_ut` take absolute UT.
  - `AlarmManager.add_alarm` takes seconds from now.
  - Every `*_at(pressure)` takes ATMOSPHERES, while `Flight.static_pressure` is in Pa.
  - Orbit angles are radians; Flight pitch/heading/roll are degrees.
- **Altitudes are measured from the CENTER OF MASS.** For touchdown, subtract the CoM-to-bottom offset from the bounding box (surface frame x = up). Use `Leg.is_grounded` / `Wheel.grounded` for contact.
- **Proxies.** Every `.part`/`.parent` access returns a fresh proxy; compare with `==` (object id), never `is`/`id()`. An `id()` walk misfired the payload separator and explained 6 failed crewed launches (commit f936f03).
- **Connection model.** One RPC lock per client and no socket timeout: `warp_to`, `AutoPilot.wait`, `launch_vessel` and `load` block that client, while streams keep flowing on the stream socket. Inputs and autopilot belong to the client that set them and are released on disconnect, so throwaway connections are harmful.
- **Streams.** A stream is bound to the object it was built from. The server drops a stream that errors, and `s()` then raises, which is useful as a "vessel gone / scene changed" signal. Rebuild streams after staging, undocking or switching vessels.
- **Expressions.** Operand types must match exactly ("Types of x and y do not match."). `mean_altitude`, Orbit values and `ut` are double; `thrust`, `mass`, `Resources.amount`, `dynamic_pressure` and `g_force` are float, so cast them. `Event.wait` must run inside `with ev.condition` and returns silently on timeout. Events are evaluated once per stream update; under heavy warp one frame covers many seconds.
- **SOI timing.** `orbit.body` stays Kerbin until the craft physically crosses the SOI. `time_to_soi_change` is NaN when there is no change, and can appear a few seconds after a burn or warp: let patched conics settle 2-4 s.
- **Closest approach.** `distance_at_closest_approach` / `time_of_closest_approach` are single-conic estimates and were misleading: a predicted "38,737 km" became a 3,866 Mm miss. Use `node.orbit.next_orbit` for encounters and sampled `position_at` for misses. `Orbit` has no `velocity_at`; a centred finite difference (±5 s) is adequate.
- **Frame conventions.** kRPC frames are left-handed with +y along the body's rotation axis, so low orbits lie in the x-z plane. Heliocentric longitude is `atan2(z, x)`. Compute orbit normals as `unit(r×v)` in a non-rotating frame.
- **Thrust readout.** `vessel.thrust` reads 0 for several seconds after leaving high warp while the engine fires (12 s during an LV-N capture, apoapsis +270 km), and a false abort followed. Confirm thrust by the change in semi-major axis (> ~5 km) or by g_force. Engines were also found deactivated after warp: re-check `engine.active`.
- **Resources.** Aggregate `vessel.resources` is misleading on staged craft (it showed 30% while the burning stage was dry). `resources_in_decouple_stage(stage, cumulative=True)` is cumulative by default; the burning stage s draws from `(s−1, cumulative=False)`.
- **Fuel flags.** `engine.has_fuel` flickers during tank-crossfeed switches, and before 0.5.2 it read true for flamed-out SRBs. Confirm with thrust.
- **Other vessels.** Parachute `.state` was unreadable on some craft. Parts, ports and modules of another vessel are unavailable until it is loaded (~2.3 km).
- **Localization.** Module GUI names are localized: use `*_by_id`. `Module.fields` throws on duplicate names.
- **Part tags.** kRPC's ModuleManager patch adds `KOSNameTag` to every part, so `part.tag` / `parts.with_tag()` are available for design-time part handles. The old repo never used them.
- **Server settings.** `settings.cfg`: `pauseServerWithGame=False` (RPCs keep working while paused), `autoAcceptConnections=True`, `maxTimePerUpdate=9800 us`, `adaptiveRateControl=True`. It lists a duplicate "Default Server" entry.
- **Misc.** Drawing: the Shabby patch on `set_Material` failed (KSP.log line 237). `sc.screenshot` works in flight only and is asynchronous.
- **Performance.** The old repo used zero streams and events and had 218 `time.sleep` loops. Each property read was a separate round trip; node searches made hundreds to about 4,750 `add_node`/`remove` calls while game time ran, which cost the TMI window.

### 3.8 Vessel identity and switching
- KSP appends localized suffixes ("AI-Eve-Crew 飞船", "探测器", "Probe"), and exact matching stranded vessels. Match tolerantly, but make sure AI-Relay-1 does not match AI-Relay-12.
- Debris inherits the parent's name. `/vessel/refuel` once hit a staged-off booster (commit cffc8fe). Identify vessels by crew, key parts (heat shield, pod), orbit and situation.
- After a decouple, kRPC may focus the HEAVIER piece. Re-select by identity (crew ≥ 1, heat-shield part, apsides nearest the pre-split orbit), verify with retries, and fail closed.
- Stop warp before any vessel switch; switching mid-warp can hang KSP. A switch needs about 2-2.5 s to settle, and KSP may auto-focus a neighbour, so re-select by name or id afterwards.
- Making a fragile uncontrolled low cabin active put it into physics, and it decayed into a fatal reentry (commit 19bb922).

### 3.9 Time warp
- `sc.warp_to(ut)` into atmospheric entry never returns: rails warp stops at the atmosphere edge long before UT. This is the literal cause of one crew death. Cap every warp target at the interface/impact UT, or step `rails_warp_factor` down by live altitude.
- Rails warp ignores throttle and engines cannot fire. A killed earlier run left a craft warping, and a "burn" did nothing (62 km Duna circle). Zero both rails and physics warp before any burn and verify thrust. Use no physics warp during powered ascent or terminal landing.
- Rails warp is capped by altitude: about 50x in 100 km LKO, where a 1.77-year wait takes about 90 real hours. High and heliocentric orbits allow 100,000x.
  - Zero-fuel workaround: stop warp, switch to a high-orbit vessel, `warp_to(target − buffer)`, switch back.
  - Better: wait for windows ON THE GROUND before launch.
  - Raising and lowering orbit for warp costs about 1,580 m/s, not 800.
- A high "warp orbit" must stay clear of the Mun's SOI (Mun at 12,000 km, SOI about 2,430 km). Apoapses of 46,332 km and 9,902 km were perturbed or ejected; about 6,900 km was clean.
- A stepped `rails_warp_factor` polled every 0.5 s overshot a node by about 25 min. Chunked `warp_to` (≤ 30 days per call) decelerates precisely; then force the factor to 0.
- `warp_to` can also hang on nodes seconds to minutes away.
- Warping inside the atmosphere applies heating instantly; two crews were lost. Never warp below about 2× the atmosphere top on descent.

### 3.10 Staging and separation
- Two stagers race. With MechJeb ascent autostage on and the script also firing decouplers, separation failed on fin geometry, boosters dropped early, and MechJeb lit the Terrier without firing the booster decoupler. Be the sole stager: `/mj-ascent autostage="false"`.
- Crossfeed transients make an engine read dry for one poll, causing premature staging. Require about 3 consecutive dry polls and key on the BOTTOM (deepest) active engine. "All active dry" never fires when a live upper engine drags a dead booster: Keo-4 was stranded at 691 km apoapsis with both decouplers unfired.
- Lighting engines directly (`engine.active=True`, by name or a blanket `has_fuel` loop) desyncs KSP's stage counter. A later `activate_next_stage` fired a spent stage and skipped the booster decoupler. Name matching also lit only 1 of 5 engines (Mainsail core + Skipper pods). After direct ignition, separate with `Decoupler.decouple()` on specific decouplers.
- After `decouple()` the vessel splits over several physics frames. Confirm by a part-count drop (poll 8 × 0.5 s), re-fetch `sc.active_vessel`, and only then light the deepest fueled unlit engine.
- **Payload guard.** A decoupler may fire only if an engine remains on the root side (walk `.parent` with `==`). Crewed stacks have two payload decouplers. A "shallowest decoupler" heuristic jettisoned the whole 45-part upper stage.
- **Fuel guard.** Never fire a decoupler with significant propellant below it (> ~5 LF) during cleanup: that drops a live split transfer stage, which stranded a Duna mission (commit 664f5e5). On booster + split craft, all decouplers can share one tree depth, so depth heuristics fail and the fuel-below guard is the robust discriminator (commit bf046b2).
- An oversized booster can reach orbit still fueled (923 LF on a Mainsail) and still attached. Force-separate spent stages at orbit insertion.
- Throttle 0 does not stop SRBs or unthrottleable engines. A coasting cargo stack reached escape. During coasts verify thrust ≈ 0; otherwise hold a retrograde guard with warp off, and abort if the orbit escapes or time_to_apoapsis goes non-finite.
- Radially surface-attached cluster engines starved mid-ascent (2526 → 580 kN) (commit 42dfedf). A renderer cluster also auto-staged early. Verify fuel flow to radial engines live.

### 3.11 Engines, fuel flow, thrust verification
- Engine starvation: thrust is 0 at full throttle while the connected tank is empty and a sibling tank is full. The cause is craft topology (crossfeed or stack order). Detect it with `Propellant.is_deprived` / `total_resource_available` and fix the design; do not refuel.
- Before declaring out-of-fuel inside a burn, stage into the next fueled stage. A TMI "out of fuel" at 159 km apoapsis happened with full Terrier stages below.
- A lander engine firing into its own shroud showed full throttle and engine thrust but g_force 0. After ignition, verify acceleration or orbital change, not the thrust readout.
- A live Mainsail made about 1,016-1,042 kN vs the 1,379 kN catalog figure. The most likely cause is the donor `thrustPercentage` 73.5 (1379 × 0.735 ≈ 1013); `execute.py:151` blames refuel. Measure live thrust.
- Pair engines with tanks that hold their propellants (Nerv → LF-only tanks).

### 3.12 Attitude and control authority
- Before igniting, wait for the autopilot error to fall below a threshold (the old code used ~2°, with 120 s max). Abort instead of firing if it is still above ~12°.
  - SAS plus a fixed ≤4 s sleep reported "aligned" while a 901 t craft was still rotating.
  - A misaligned ejection burned nearly retrograde and killed a crew (commit 3f27071).
- The autopilot aligns the CONTROL part's forward axis, not the thrust axis. On one Orion the node Δv grew while both apsides fell; on the Moonship, "retrograde" (0,−1,0) raised periapsis, and the code then flipped the constant. Check `parts.controlling` against the engines' `thrust_direction` before burning, and check the orbital effect in the first seconds.
- A prograde burn can only raise apoapsis. If apoapsis falls in the first seconds, cut throttle, re-align, and resume at low throttle (about 0.15) so the gimbal has authority. Never flip the burn vector.
- Heavy stacks need authority. About three inline 1.25 m reaction wheels (~15 kN·m) took relay TMI and capture from about 50% to reliable. Hold energy-removal burns with the autopilot re-pointed every tick so the gimbal helps; SAS-only capture went off-axis and wasted more than 1 km/s. Surface-mounting a big wheel on a small probe core clipped and destabilized it.
- Active fins (AV-R8) under a weak probe autopilot over-rotated and tumbled; passive `basicFin` low on the booster gave a stable gravity turn. A blunt exposed pod + heat shield tumbled on ascent: fair it and jettison the fairing in orbit.
- A probe at EC = 0 cannot point (ControlState none, 179° error). Batteries drained in shadow during a MechJeb autowarp stall. Carry an RTG (PB-NUK ~0.75 EC/s) and a large battery (Z-1k).
- Check `control.state` / `control.source` and the CommNet link before commanding a probe.

### 3.13 Burn execution
- Start a finite burn at `node_ut − burn/2 − settle − command delay`. Size the timeout from burn time: a 60 kN Terrier needed about 400 s for a Duna ejection and a fixed 360 s left it bound; a fixed 180 s Mun-capture lead started too early and drove periapsis to 8 km.
- Feather the throttle: full until remaining Δv < max(2, 2.5 × accel), then proportional with a 0.05 floor. A 6 m/s correction lit at high thrust overburned past the transfer band. Start small corrections at low throttle.
- Integrate applied Δv as ∫ thrust × throttle / mass d(`sc.ut`) over GAME time. Wall-clock time under ~3x lag cut burns short (3,774 Mm phasing miss). Velocity-difference measurement over-burned a 1,020 m/s ejection by about 260 LF, because the vector rotates and the frame changes at SOI.
- Stop transfer burns on the live outcome (the predicted encounter periapsis in band), not only on the remaining node Δv. Merely getting an encounter is not enough: a grazing Mun periapsis of 2.1 Mm made capture unaffordable.
- Finite-burn error erases margin (33.7 km planned became 14.4 km actual). Verify and correct after every burn.
- MechJeb's executor flew tiny normal-heavy correction nodes precisely where the hand executor failed (planned 10,631 km, got a 183 Mm closest approach). But MechJeb drifted about 2° on a long ejection under lag, while a manual burn aligned to < 1.5° held 0.10°. Capping a big burn at 0.5 throttle lost fuel to gravity loss. Pick the executor per burn and always verify node consumption and the predicted orbit.
- A direct escape-to-apoapsis burn must read `next_orbit.apoapsis` while inside Kerbin's SOI and the current apoapsis once in the Sun's. Reading an empty `next_orbit` after crossing over-burned to a 75 Gm apoapsis (commit ae9705a).
- Near escape, apoapsis grows exponentially: one feathered full-throttle step went from 62,287k to 109,407k km and escaped. A hand retrograde lowering burn overshot past circular and put periapsis at 20 km; a precise node fixed it.

### 3.14 Transfers, encounters, corrections
- The Lambert branch bug: the prograde test on z is wrong in kRPC frames and returned retrograde solutions (v∞ about 19.5 km/s) in about half of geometries.
- Ejection asymptote: ν = arccos(−1/e). Using arccos(+1/e) gave a 72° aim error and misses of 150-600x SOI (commit d07e3f7).
- The heliocentric window UT differs from the in-orbit ejection UT by 1-5 h. Iterate until the shift is under one parking period. Warp to window − (1.3 × period + 1,800 s) so you don't overshoot into the next synodic window.
- `mj_plan(interplanetary)` needs a LOW CIRCULAR parking orbit. From a 6,900 km eccentric orbit it returned a retrograde, orbit-lowering node; from 100 × 2,500 km the ejection grew to about 1,400 m/s; from 100-130 km LKO it gave about 1,018-1,058 m/s.
- MechJeb's transfer creates a real encounter where the bare Lambert ejection missed by 2.5x (Eve) to 5.3x (Duna) SOI. MechJeb's window timing, though, was about 6 days off (a 3,500 Mm phasing miss) while the Lambert porkchop matched to 0 km. Use Lambert for timing and MechJeb for the node.
- MechJeb's planner ignores the Mun: a Kerbin ejection through the Mun's SOI was deflected. Check the predicted patches for intermediate encounters.
- `OperationCourseCorrection` only refines an existing encounter. From a miss it diverged (300 → 397 → 766 → 1,502 m/s) and ran relays dry. Create the encounter with your own node search.
- Correction searches must include RADIAL components. Without them, encounters landed at the SOI edge (21,000-43,000 km periapsis at Duna), where capture costs about the full v∞.
- Correction timing is disputed. At Duna, corrections right after ejection had little leverage; at Eve, corrections done later near the SOI grew 338 → 1,502 m/s. Compute the sensitivity instead of using a rule.
- A 2° plane error is about 720 Mm out of plane at Duna. Only large normal burns fix it (−800 m/s → 453 Mm). Keep the ejection in-plane and log inclination after every phase.
- Measured crewed Kerbin→Duna cost was about 3,100 m/s: eject 1,039 + corrections ~890 + capture/Hohmann ~1,176, against 1,688 modeled. MechJeb overhead was about 25%. Transfer-stage margins of 0.45 ran dry and 0.85 was unlaunchable.
- Partially lowering a warp orbit saved about 250 m/s but pinned the ejection point; the resulting miss needed more than 790 m/s and the mission aborted (commit de71d88).
- Candidate nodes: `add_node` → read the `node.orbit.next_orbit` chain → `node.remove()`. Brute force costs game time (a 10k-node TMI search took more than 50 s and missed the window). Seed analytically, refine locally, and widen to about 3 periods only if needed.
- Keep correction candidates from lowering the parent-body periapsis into the atmosphere: one Mun correction put Kerbin periapsis at 27 km. Prefer free-return candidates for crew.
- Reference numbers:
  - From 80.5 km LKO: Kerbin ascent ~3,343 m/s.
  - Mun: TMI 856 m/s, capture 311 m/s to 10 km, phase 110.9°, transfer ~16,900-26,688 s depending on orbit.
  - Duna: eject ~1,060-1,072 m/s, v∞ ~918 (Lambert 842, tof 0.75 yr), phase 44.4°, capture 616 m/s to 57.5 km.
  - Eve: eject ~1,036 m/s, v∞ 779 (Lambert 763, tof 0.40 yr), phase −54°, propulsive capture 1,399 m/s.
  - Use these only as sanity bounds.

### 3.15 Capture and orbit shaping
- Capture by burning pure retrograde with the autopilot re-pointed at −velocity every tick in `body.non_rotating_reference_frame`. Burn at periapsis; burning at the SOI edge buries the periapsis.
- Oberth overshoot: at a deep periapsis one lag frame at high throttle took AI-Duna-Ring-X from escape to 654 × −317 km. Feather the throttle as apoapsis approaches the target, poll faster, and enforce a periapsis-floor stop. A capture target below the encounter periapsis is never reached and burned periapsis to −24 km; bound it (commit 283fc38).
- A grazing encounter makes capture cost about v∞. Lower periapsis EARLY with a mostly radial burn, and score by total Δv (node + Oberth capture + Hohmann down). Dropping a 21,000 km periapsis to about 2,500 km roughly halved a lander's capture (2,400 → 1,100 m/s). Lowering to a fixed 300 km emptied the tank.
- A loose capture (bound ellipse, apoapsis ~0.30-0.35 SOI) cost about 146 m/s at Eve, and the low periapsis makes the return ejection cheap. Circularizing then Hohmann-ing down ran a craft dry (e = 0.63). Relays only need to be bound; accept the functional band (the Mun relay went to 2,041 × 101 km).
- A Mun-stationary orbit is impossible: 2.97 Mm altitude is outside the 2.43 Mm SOI.
- On airless bodies periapsis > 0 is not safe. The Mun needed ≥ ~8-12 km, and a 0.6 km capture was rejected. The first Mun relay impacted after warping into a −40 km predicted periapsis. Derive the floor from terrain samples.
- Stop tracking circularization when eccentricity stops decreasing.

### 3.16 Rendezvous and docking
- Close km-scale gaps with `/mj-rendezvous` on the main engine (verified 1,078 → 60 m), then `/mj-dock` on RCS. RCS-only closing drained monoprop and stalled ("moving at <0.00 m/s"). Target the docking PORT.
- Docking completion is ambiguous: the autopilot disables on success, target loss and couple. Confirm by a part-count jump (21 → 42) or port state "Docked". After docking the vessels merge and the target name disappears; call `/transfer-crew` with no `toVessel`.
- MechJeb target sync: `SetVesselTarget` only sets `targetObject`, and `core.Target` updates next FixedUpdate. Also call `core.Target.Set(x)`.
- `/mj-dock` picks the first port not "Docked", including PreAttached stack ports, and never restores control-from-port.
- Hand-rolled kRPC docking never mated in about 13 attempts (partly the rotating-frame autopilot bug). The RCS axis sign mapping (`control.up = +err[2]` while vessel frame z points out of the bottom) was never calibrated.
- In the orbital frame at km range, frame rotation adds about n × d to relative velocity. Null relative velocity in a non-rotating frame.
- The target's ports read as None until it is loaded; re-query each tick. Orienting a passive target means switching to it (~2.5 s settle).
- `_match_orbital_plane` burned along `unit(n_t − n_c)`, about 90° from the correct plane-change direction.
- Re-optimizing imprecise intercept corrections made closest approach worse and lowered periapsis into the atmosphere. Constrain parent periapsis.
- The pre-dock monoprop refuel was a cheat. Budget RCS in the design.

### 3.17 Airless landing and surface operations
- **Hoverslam that worked on the Mun.**
  - Coast engine-off while surface speed < v_ref(h) = √(2 (f·F/m − g) h), with f ≈ 0.92 as catch-up reserve, then track the curve.
  - Point surface-retrograde (surface_velocity frame (0,−1,0)) while speed is significant, then local-up.
  - Below about 70 m the curve is too steep to track, so a terminal controller is required.
  - Best touchdown: −0.1 m/s vertical, 0.2 m/s horizontal.
- **Terminal descent failures.**
  - A fixed 30 m margin forced ~0.92 throttle near the ground: bounce, drift and empty tanks.
  - Hovering capped at 250 m oscillated and burned the return fuel.
  - Lateral cleanup starting at 120 m with 7.5 m/s sideways was too late (18.8 m/s impact).
  - Switching to vertical attitude while sliding kept the slide; 2 m/s sideways left only the pod.
  - Null horizontal speed before contact, and enter terminal control by about 500 m if horizontal speed exceeds 3-5 m/s.
- **Relaunch.** A touchdown KSP accepts is not necessarily relaunchable (6 m/s slid and destroyed the stage). Before relaunch:
  - throttle 0 and settle until |vs| < 0.25 and speed < 0.55 for more than 1.5 s;
  - keep legs down until real clearance (about 80 m);
  - use partial throttle first and don't stage near the ground;
  - check tilt < ~15-25°: a tipped lander at full throttle drove its engines into the ground.
- **Lander geometry.** A tall single-stack lander tipped over on Duna, burying the hatch. Design for a tip-over angle ≥ ~35° (leg span ≥ ~1.7 × CoG height; wide ≥ 2.5 m, short, low CoG), and drop the transfer stage in orbit.
- A lander with no legs tipped over and killed its crew.
- Deorbiting from a high Mun orbit with a guessed retrograde attitude raised periapsis twice. Lower apoapsis first with a calculated node. Big lowering burns need full throttle; a 0.18 cap timed out.
- In micro-g (< 0.5 m/s², Gilly/Minmus) hoverslam is unstable; use a gentle descent.
- Heavy TWR matters: a 30 t upper on a 60 kN Terrier (TWR ~0.2) crawled and timed out. About 5 m/s² worked (1,100 m/s in ~150 s). A Reliant upper at TWR ~1.1 failed twice to circularize 237 t.

### 3.18 Atmospheric entry, landing, chutes, recovery
- **MechJeb owns landings.** Handing the descent to MechJeb's landing autopilot (`/mj-land` with DeployChutes/DeployGears) beat every hand-rolled descent. Guessed chute altitudes, loop timeouts (one quit 1.7 km above chute arm) and a `warp_to(periapsis)` hang killed six kerbals.
- **Deorbit first.**
  - MechJeb's landing autopilot will not deorbit from a stable orbit above the atmosphere (a crewed Duna craft hung in a 62 km circle).
  - It will not deorbit a craft already on a shallow grazing pass (a 48 km Kerbin periapsis skipped out for hundreds of passes).
  - First lower periapsis into a real corridor (about 25-35 km at Kerbin), release SAS, then engage.
  - It won't fast-warp a long descent ellipse (from ~9,400 km). Warp-assist by stepping rails down to about atmosphere top + 10 km, then hand back.
- **Entry configuration.**
  - Keep landing legs retracted during high-speed entry (extended legs broke up a Duna entry); MechJeb deploys them.
  - Jettison the service section in vacuum so only pod + heat shield + chute reenter. A long stack tumbled, and the pod separated chuteless (confirmed twice).
  - Craft without a heat shield disintegrate at about 3 km/s. Gate the heat shield on the RETURN requirement, not on crew presence.
  - A lander aerocapturing engine-first into a foreign atmosphere with a Kerbin-sized shield facing wrong broke up. Capture propulsively above foreign air; aerocapture only at home, behind the shield.
- **Chutes.**
  - Mk16 rips above about 250 m/s and should not open above about 5 km in thick air.
  - Arming early is harmless because stock chutes hold until safe; arming only below 4.5 km and < 330 m/s failed on a steep entry. Set `deploy_altitude` / `deploy_min_pressure` from density.
- **Bodies.**
  - Duna surface density is 0.13334 kg/m³ (not the 0.0677 atm pressure). One Mk16 gives about 30 m/s there (two crews lost): use about 10 chutes or land propulsively. A propulsive Duna landing worked: aerobrake 1,300 → 400 m/s, then hoverslam to 0.0 m/s (commit c0cfd94).
  - Eve's air (~6.2 kg/m³) means about 8,000 m/s to orbit, so a crewed Eve surface round trip is infeasible. Gilly (g 0.049 m/s², escape ~36 m/s) is the feasible Eve-system flag site at about 2,480 m/s round trip from low Eve orbit.
- A rushed live recovery killed 5 kerbals who were alive. Test reentry on an uncrewed identical capsule first; stranded alive is recoverable.

### 3.19 EVA and crew
- `FlightEVA.fetch.spawnEVA(pcm, part, part.airlock, true)` returns a `KerbalEVA`, or null when no hatch is free. Mk1 hatches were blocked by a heat shield below (Mun) and by radial chutes, panels and an RTG (Duna). The "hatch is in the −180..0° azimuth hemisphere" conclusion was never re-verified live.
- `KerbalEVA.PlantFlag()` is public. `SetWaypoint(Vector3 worldPos)` is the stock walk-to. `BoardPart(Part)` boards. `KerbalRoster.Kerbals()` has no zero-arg overload.
- `/eva-flag` needs LandedOrSplashed. "planted" only means no exception was thrown; verify a new Flag vessel. Plant only when `fsmState` is idle. Treat the flag as optional so it never strands a crew.
- Confirm boarding via the crew list. Whether kRPC `CrewMember.part` is writable is unverified; `sc.transfer_crew(crew_member, target_part)` exists in 0.5.4.
- The docstring claim that flags cannot be planted headlessly is stale.

### 3.20 MechJeb specifics
- Ascent does not ignite from PRELAUNCH. Give it one kick (throttle 1 + `activate_next_stage`, or light every liftoff engine type), then MechJeb flies the gravity turn.
- `Autostage` must be set before `Users.Add`. `MechJebModuleStagingController` is separate: `/mj-land` and an autostaging ascent leave it enabled, and `autostage=false` does not clear it. It autostages during ANY burn and fired a heat-shield/payload decoupler mid-capture, stranding a crew pod at Eve. Call `/mj-disable {which:"staging"}` before in-space legs.
- The node executor won't autowarp to a distant node while orienting (steering caps warp). Warp with kRPC to node − ~45 s first. Completion is `nodeExecEnabled == false && nodeCount == 0`. It executes kRPC-placed nodes, though one ~6-minute-away node was left unburned.
- `/mj-plan` clears ALL nodes, plans from the current UT, and circularize needs a target.
- `MechJebModuleStageStats.RequestUpdate()` is async, so the first call is usually `pending`. The per-stage burn times matched the lab's rocket equation to ~1e-6 s. Read stage Δv/TWR from it.
- `/mj-disable all` cannot stop ascent, landing or the node executor. The only way today is to switch vessels or reload.
- Planning off a vessel without a MechJeb core fails (commit 124eed7).
- Classic ascent, not PVG (PVG is for RSS/RO).

### 3.21 Power, comms, constants
- CommNet link range R = √(A·B).
  - RA-2 + DSN3 = 22.4 Gm, less than the 34.3 Gm Kerbin–Duna conjunction, so signal is lost.
  - RA-100 + DSN3 = 158 Gm; RA-100 ↔ RA-100 = 100 Gm.
  - The Sun occludes regardless of power; eccentric relay orbits bunch and leave gaps.
- Synchronous altitude = (μT²/4π²)^(1/3) − R, and it must lie inside the SOI. Kerbin 2,863 km; Duna 2,880 km; Eve 10,328 km.
- Kerbin: μ = 3.5316e12, R = 600 km, SOI 84,159,286 m, atmosphere 70 km, sidereal day 21,549.425 s, v_circ at 80 km 2,278.93 m/s.
- Mun: μ = 6.5138e10, R = 200 km, SOI 2,429,559 m, g 1.629.
- Kerbin year = 426 days × 21,600 s. Deep-space solar at Duna is about 43% of Kerbin's.
- Static catalog errors found: Duna rotation speed is 30.69 m/s (not 29.36); Ike is 12.47 (not 18.6). Read everything live.

### 3.22 Design lessons
- Launch TWR: the estimator ignored ~0.5-0.8 t of accessories, and an estimated 1.24 never left the pad (apoapsis 89 m). Read the real TWR from the loaded vessel before releasing clamps; about 1.4 estimated was the working floor.
- An exposed blunt payload raised Cd from 0.232 to 0.352 and drag loss from 286 to 433 m/s, and the "10 km/s" vehicle fell back. Faired fixed it (commits 3a85df3, 4b20f29).
- A 574 t / 65 m / L:D 17.4 needle was unlaunchable. Radial boosters shortened the core and raised liftoff TWR (1.33 → 1.41-1.76). Real launchers run L:D about 4-19. Moving the crew cabin above the capsule decoupler raised the CoM, and the ascent stalled at ~39 km (commit 6744407).
- Tank sizes come in coarse steps, so raising a Δv requirement can be a no-op. Diff the resulting design (commit 3155886).
- A numeric "looks like a rocket" gate passed needles, clipped engines and floating legs. Rendering to PNG and looking at it caught them.
- LLM run-to-run variance (crew = 2, payload 2 t) cascaded into a 1000 t infeasible design. Design tools must report feasibility and mass sensitivity back; silently pinning parameters hid this (commit 5f72884).

### 3.23 Process and tooling
- One root cause per iteration. Don't patch the controller seconds before impact; let the run finish as data. Diagnose only from per-component, correct-frame, thrust-verified telemetry, and record symptom → cause → fix immediately.
- Many historical "fixes" were constants tuned to one flight, and several were committed without live re-verification (hatch azimuth, the re-tuned Mun flare). Treat old numbers as evidence, not rules.
- A 20-minute silent ascent poll existed until the fail-fast predicate. Every monitor needs abort predicates and timeouts.
- The Codex review gate, the numeric design gate and plan_validator could not change outcomes or blocked creativity. Keep reviews advisory.

---

## 4. Tool layer proposal

### 4.1 Runtime architecture
- **One long-lived Python daemon** exposed as an MCP server. It is the only process that talks to KSP. It holds:
  - a `ctl` kRPC connection for short RPCs, the per-vessel stream bundle (~25 streams at 5-20 Hz) and Expression events;
  - a `blk` kRPC connection for blocking calls: `warp_to`, `launch_vessel`, `load`/`quickload`. Never call `AutoPilot.wait`.
  - a byte-correct UTF-8 bridge client;
  - a reflex runner (at most one active reflex per vessel, abortable from `ctl`);
  - an auto-journal that logs every tool call with args, result digest, UT and scene.
- **Every tool:**
  - checks the scene before running;
  - never falls back silently (an unknown id is an error);
  - returns `{ok, ..., warnings[], ut, scene}`;
  - for state changes, returns verification fields (before/after part count, orbit, crew, active vessel).
- **After a scene change, vessel switch, staging or undock:** invalidate handles, re-acquire the active vessel, rebuild streams.
- **Latency.** An LLM decision takes about 5-30 s, while a game frame is ~20 ms. The LLM chooses parameters, plans, verifies and handles contingencies. Reflexes close every loop faster than ~10 s. While deliberating in a time-critical but uncontrolled situation, the AI may `pause_game(true)`; the kRPC server keeps answering because `pauseServerWithGame=False`.
- **Reflex contract.**
  - Inputs: AI-supplied parameters (any `'auto'` value is derived from torque/MoI, thrust/mass, body data, and the derivation is echoed back).
  - Enforces the safety invariants and has both a wall-clock and a game-time timeout.
  - Streams progress to the flight recorder.
  - Returns `{outcome, reason, effective_params, dv_applied, orbit_before, orbit_after, events[], telemetry_tail}`.
  - Wrap each reflex in `wait_until`/`monitor` so control returns to the LLM on events.

### 4.2 Where reflexes are mandatory (LLM round-trip cannot close the loop)

| Loop | Timescale | Reflex |
|---|---|---|
| Ascent attitude, throttle, max-Q, staging on flameout | 0.1-1 s; dry streak ~3 polls | `ascent` (own controller or MechJeb plus staging/health monitor) |
| Any finite burn: align gate, throttle feather, cutoff, in-burn staging, wrong-way detection | 0.1-0.5 s; Oberth overshoot in one lag frame | `execute_node`, `burn_until` |
| Capture / apoapsis lowering at deep periapsis | 0.1-0.25 s near target | `burn_until(feather)` |
| Powered descent, hoverslam, terminal flare, touchdown | 0.05-0.25 s | `hoverslam_land` |
| Surface liftoff (settle, tilt gate, clearance) | 0.25 s | `surface_liftoff` |
| Relative-velocity nulling, closing, RCS station-keeping | 0.1-0.5 s | `kill_relative_velocity`, `close_on_target`, `station_keep_rcs` |
| Atmospheric descent warp stepping and chute arming | 1-3 s | `reentry` / `descent_monitor` |
| Warp with safety caps | continuous | `warp` |
| Attitude hold on rotating vectors | every stream tick | `hold_attitude` |
| EVA walking, MechJeb rendezvous/dock/land | seconds to minutes | polling monitors are enough (`wait_until` + status) |

### 4.3 Tools

Backend: **K** = kRPC, **B** = bridge, **P** = pure Python, **F** = local files. **R** = reflex runs in the daemon.

**Observe**
- `game_state()` → `{scene, paused, ut, save_folder, game_mode, active_vessel{id,name,situation,body,control_state,control_source}, warp{rails,physics,max_rails}, editor{ready,craft,part_count,facility}, pad{site,clear,blockers[]}, bridge{up,build,queue_depth,last_error}, krpc{up,rpc_rate}, mechjeb{available,version}}` — B+K
- `flight_state(fields='all')` → `{ut, met, body, situation, mean_alt, surface_alt, bottom_clearance, radar_alt, lat, lon, vs, hs, surface_speed, orbital_speed, ap_alt, pe_alt, tta, ttp, ecc, inc_deg, sma, period, t_soi, next_body, next_pe_alt, mass, thrust, available_thrust, twr_local, throttle, g_force, q_pa, mach, static_pressure_atm, pitch, heading, roll, aoa, tilt_from_up, current_stage, sas, rcs, comm_signal}` — K streams, zero RPC cost
- `orbit_state(vessel?)` → elements plus the patch chain `[{body, pe_alt, ap_alt, ecc, inc, soi_change_ut}]` — K
- `vessel_snapshot(vessel_id?, detail='summary'|'parts')` → mass, crew, resources by decouple stage, parts `[{id, parent_id, name, title, tag, stage, decouple_stage, roles, module_states, resources}]`. Errors on an unknown id — K (+B module state)
- `staging_map()` → decouplers `[{id, stage, depth, would_strand_payload, fuel_below:{res:amt}, heatshield_below, crew_below, decoupled}]`, engines `[{id, stage, active, has_fuel, thrust, isp, propellants, deprived}]`, clamps, bottom_engine_dry_streak, per-stage `{dv_vac, dv_here, twr_here, burn_s}` — K+P
- `stage_stats(scene='flight'|'editor')` → MechJeb `vac/atmoStats`, polled until not pending — B
- `body_info(name)` → `{mu, radius, surface_g, soi, rotation_period, rot_speed, atmosphere_depth, density_profile[(alt, rho, p_atm)], has_oxygen, satellites, orbit{sma,ecc,inc,lan,argpe,period}, sync_alt|null, max_terrain_sampled}` — K
- `list_vessels(filter)` → `[{id, name, type, body, situation, ap, pe, inc, crew, has_heatshield}]` — K
- `relative_state(target)` → `{distance, rel_velocity(inertial), closing_speed, closest_approach{ut, dist}, target_loaded}` — K
- `radar_altitude(direction='down'|'velocity')` — K raycast plus bounding box
- `terrain_clearance(ut_range | 'periapsis', samples)` → `{min_clearance, max_terrain, lat, lon}` — K
- `events_poll(since_seq, types, timeout_s)` → GameEvents feed (stage, part_die, explode, crash, splashdown, situation, SOI, dock/undock, crew_killed, flameout, decouple) — B (new) + K stream detectors
- `flight_recorder(query)` → samples and events — daemon
- `mj_status()` → every module's enabled flag and status, target, nodes, landing prediction — B
- `crew_roster()` / `crew_list()` / `eva_state(kerbal)` — B
- `screenshot(view, overlays=['attitude','node','impact'], hide_ui)` → `{path|image_b64, w, h}` — K in flight (async file), B in any scene (new), computer-use fallback
- `ksp_log(since_line, filter='EXC|ERR|KspAutomationBridge')` — F

**Compute** (P unless noted; all advisory; each returns formula and assumptions)
- `orbit_calc(op, **params)`. Ops: vis_viva, circular_speed, period, hohmann, apsis_change, plane_change, deorbit, capture, oberth_ejection, phase_angle, synodic, sync_alt.
- `rocket_equation(op, ...)`. Ops: dv, prop_mass, mass_ratio, burn_time, half_dv_time.
- `engine_performance(part, pressure_atm)` → thrust, Isp, mdot, propellants — P catalog or K `*_at`
- `plan_maneuver(kind, ...)` → `{ut, prograde, normal, radial, dv, burn_s, lead_s}`. Kinds: circularize, set_opposite_apsis, hohmann_to_radius, deorbit, capture, plane_match_at_an/dn, escape. Computed from live elements; nothing placed.
- `predict_node(ut, pro, nrm, rad, keep=False)` → patches, closest approach, parent periapsis — K (add → read → remove)
- `search_nodes(objective{encounter_body, target_pe, pe_floor, parent_pe_floor, target_apo, minimize: dv|closest|total_dv_to_orbit}, ut_range, bounds{pro,nrm,rad}, seed?, max_evals=300)` → ranked candidates plus game-time cost — K+P (coarse grid then pattern refine)
- `transfer_window(dep, tgt, ut_from?, tof_bracket, grid, objective='departure'|'arrival'|'total', park_alt)` → `{ut_dep, tof, vinf_dep_vec, vinf_dep, vinf_arr, eject_dv, capture_dv(alt), phase_now, synodic, porkchop_summary}` — P + K positions (Lambert branch fixed)
- `plan_ejection(vinf_vec, ut_min, place=False)` — P+K
- `closest_approach(target, window_s, step_s)` → `{dist, ut, soi_multiple}`, propagated from live elements — P+K
- `mj_plan(operation, params, time_ref, target, place=False)` → candidate nodes — B
- `dv_map(route)` → per-leg `{dv_nominal, dv_range, formula, not_modeled[]}`. Routes through the common ancestor; says what it leaves out (e.g. corrections).
- `predict_trajectory(until='impact'|'alt:<m>'|'soi', with_drag)` → impact point, max-q, peak g, entry UT — K samples of `simulate_aerodynamic_force_at` + P integration
- `landing_calc`, `chute_calc(mass, g, rho(live), v_target, cd_area)`, `comm_link`, `flyby_calc`, `geo(distance_bearing|destination, body)`.
- `plan_lint(steps)` — optional, advisory only.
- `self_check()` → the math-vs-game table — K+B

**Design** (P + catalog; live catalog via B)
- `part_catalog(query, role, diameter, propellant, min_thrust, radial_mountable, include_hidden=False, limit)` / `part_info(name)`. Returns nodes, attachRules, crossfeed, engine modes with propellant ratios and Isp curve, variants/B9, cost, tech, bounds, chute/decoupler specs.
- `catalog_refresh(from_live=True)` → `{count, dropped, mismatches}`.
- `stage_options(payload_t, dv, min_twr, g, pressure_atm, diameters, engine_filter, max_engines, top_k)` → a ranked list (closed-form tank count), not one pick.
- `craft_new(name, root_part)`, `craft_parse(path)`, `craft_clone(source, new_name)`.
- `craft_attach_stack(craft, parent_uid, part, parent_node, child_node, stage?)` — uses real node vectors; warns on size mismatch.
- `craft_attach_radial(craft, parent_uid, part, count, azimuth_deg, height_m, standoff_m, stage?)` — stock yaw, attm=1, `sym`; refuses when srfAttach is forbidden.
- `craft_set(craft, uid, stage?, thrust_limit_pct?, crossfeed?, resources?, fairing_xsections?, tag?)`, `craft_add_fuel_line(from, to)`, `craft_fairing_enclose(base_uid, clearance, nose)`, `craft_remove(uid)`.
- `craft_analyze(craft, launch_body, target_body?)` → per-stage `{istg, activates, drops, m0, m1, dv_vac, dv_asl, twr, burn_s}`, CoM/CoP, static margin, tip-over, control sources, warnings. Pure, no mutation.
- `craft_validate(craft)` → errors and warnings:
  - part ids resolve (`/part/resolve`);
  - tree connected;
  - pods are children of their decoupler;
  - crossfeed path for every engine;
  - no mixed payload stageables in one istg;
  - control source with EC;
  - no floating parts;
  - thrust limit < 100 flagged;
  - LF-only engine on LFO tanks flagged;
  - name and single-line description;
  - no ACTIONGROUPS or STAGES.
- `render_design(craft)` → PNG + metrics for the AI to look at. `second_opinion(question, images)` → optional Codex/Claude CLI.

**Build**
- `craft_write(craft, save=None)` → path in the ACTIVE save's `Ships/VAB` — P+B
- `editor_load(craft, facility)` → job; `editor_state()` → `{ready, part_count, stage_sim}` — B (new)
- `launch(craft_name, facility='VAB', site='LaunchPad', crew=[...], recover_pad=True)` → `{vessel_id, scene, crew_seated, parts, live_mass, twr_pad, blockers}`. Uses K `launch_vessel` on `blk`, with a B fallback that pre-checks the pad and polls. Fails loudly.
- `pad_status(site)`, `recover_vessel(id)`, `terminate_vessel(id)` — K/B
- `vessel_readback()` and `craft_diff(craft, vessel)` → node-snap offsets, staging reorders, missing parts, dropped modules — K+P

**Fly** (actuators and reflexes)
- `set_controls(throttle, sas, sas_mode, rcs, gear, lights, brakes, abort, action_groups, translate, rotate, input_mode)` → `{applied, rejected{field: error}}` — K
- `point(target{prograde|retrograde|normal|antinormal|radial|antiradial|surface_retrograde|local_up|node|target|vector|pitch_heading}, tolerance_deg='auto', timeout='auto', tuning='auto', roll?)` → `{converged, error_deg, elapsed, max_ang_accel, tuning_used}` — K R-lite
- `hold_attitude(target, until?)` / `release_attitude()` — K R
- `check_control_point()` → `{control_part, forward_vs_thrust_deg}`; `set_control_part(id)` — K
- `stage(target='next'|'decoupler', decoupler_id?, force=False)` → `{fired, blocked_reason, parts_before/after, active_vessel_changed, newly_lit}`. Guarded; confirms by part-count drop — K
- `ignite(engines='stage'|ids)`, `release_clamps()` (only if TWR > 1) — K
- `deploy(kind, selector, options)`, `jettison_fairings()`, `part_action(selector, module, event_id|action_id|field_id, value)` (`*_by_id`), `run_science(selector, transmit)` — K
- `execute_node(node?, method='manual'|'mechjeb', align_deg='auto', abort_deg, feather_s, min_throttle, lead='half_burn', stop_when{encounter_pe_in, element}, abort_when{parent_pe_below, apo_falls_on_prograde_s, no_accel_s, dv_diverges}, auto_stage, dry_streak)` → `{dv_applied (game-time integrated), residual, stages_fired, orbit_after, reason}` — K(+B) **R**
- `burn_until(direction, throttle_law{full|feather on element}, until{apo_below, pe_above, ecc_below, ecc_stops_decreasing, bound, rel_speed_below}, abort{pe_below, escaping}, max_s)` — K **R**
- `ascent(mode='reflex'|'mechjeb', heading, target_apo, turn params | mj settings, max_q, auto_stage, dry_streak, fail_fast thresholds)` → `{orbit_at_cutoff, events, failure_reason}` — K(+B) **R**. `ascent_health()` is the pure predicate.
- `hoverslam_land(throttle_reserve, flare_alt, touchdown_speed, max_horizontal, retro_until_speed, legs, warp_until_alt)` — K **R**
- `surface_liftoff(calm_speed, settle_s, initial_throttle, clearance_alt, then)` — K **R**
- `reentry(entry_pe, jettison_service, legs_retracted, handoff='mj_land'|'chutes', warp_until_alt)` / `descent_monitor()` — K+B **R**
- `kill_relative_velocity(target, threshold, gain, frame='inertial')`, `close_on_target(target, stop_m, v_profile)`, `station_keep_rcs(target, offset)`, `calibrate_rcs_axes()` — K **R**
- `mj_ascent(full settings)`, `mj_execute_node(mode, lead, tolerance, autowarp)`, `mj_land(target?, touchdown_speed, gears, chutes, limit_stages)`, `mj_landing_prediction()`, `mj_rendezvous(target_id, dist, phasing_orbits, closing_speed, use_rcs)`, `mj_dock(own_port_id, target_port_id, speed_limit, force_roll, roll, override_safe)` → docked verdict, `mj_attitude(SmartASS mode)`, `mj_abort(modules)` — B
- `wait_until(conditions[], mode, timeout_wall, timeout_game, abort_on=['vessel_lost','flameout','control_lost','scene_change'])` → `{fired, ut, telemetry}` — K Expression events
- `warp(to{ut | event: apoapsis|periapsis|soi|node|atmosphere_interface|altitude|closest_approach}, lead='auto', max_rails='auto', allow_atmosphere=False)` → `{reached_ut, capped_by, stopped_reason}` — K `blk` **R**
- `advance_clock(ut, strategy='ground'|'high_vessel')` — K
- `abort_reflex()` — daemon
- `select_vessel(id|name_hint)`, `reacquire_vessel(criteria{crew_min, has_part, near_orbit, body})`, `set_target(vessel|body|port)` — K
- `eva_start(vessel, kerbal, part?)`, `eva_move(kerbal, lat, lon | bearing, distance)`, `eva_board(kerbal, part_id)`, `eva_plant_flag(kerbal, name, plaque)`, `crew_move(kerbal, to_part_id)` (same vessel), `undock(port_id)` — B / K

**Game management**
- `ksp_start(save, scene)`, `ksp_restart()`, `bridge_rebuild_install()` (build → stage → swap → restart → route probe) — F+B
- `load_game(save_folder, save_file='persistent', scene, vessel_id?)`, `checkpoint_save(label)`, `checkpoint_list()`, `checkpoint_load(label, scene)`, `quicksave()`, `quickload()`, `revert(to='launch'|'editor')`, `goto_scene(scene, save_first=False)`, `fly_vessel(vessel_id)` — B/K
- `pause_game(bool)` — K
- `prune_save(keep_predicate, dry_run=True)` → only when the save is not loaded; `.bak` backup; re-index `activeVessel` — F
- `capcom_post(message, level)`, `capcom_inbox(since_seq)` — B; `announce(text)`, `ask_human(question, options, timeout)` — K UI

**Knowledge**
- `knowledge_lookup(query|symptom, phase?, top_k)` → `[{id, symptom, cause, fix, confidence, evidence}]` over the failure rules and doctrine — F
- `doctrine(topic)` → sections of the ported notebook and methodology — F
- `log_decision(phase, decision, rationale, predicted{})` → entry id; `record_outcome(id, observed{}, verdict)` → discrepancies; `read_journal(mission?, last_n)`; `prediction_bias(quantity)`; `record_lesson(phase, symptom, cause, fix, evidence, verified_live)` — F (JSONL, utf-8)

---

## 5. Open risks (verify against the live game)
1. **kRPC `launch_vessel`** has never been used in this repo. Check:
   - that it returns only after FLIGHT loads;
   - that `crew=[...]` seats the named kerbals;
   - what `recover=True` removes;
   - its behavior with `AI-DEBUG-KerbalX` in PRELAUNCH on `codex-audit`, and on SANDBOX.
2. **Cause of Mainsail at ~74% thrust:** donor `thrustPercentage 73.5` (craft reader) vs `/vessel/refuel` side effect (`execute.py:151`). Launch a writer-generated craft with no ModuleEngines block and measure `max_thrust`.
3. **Override\* header fields.** The knowledge reader lists them with ACTIONGROUPS as NullReference causes; the craft reader shows KSP's own resave writes them. Bisect once with KSPCommunityFixes loaded. Until then, omit both.
4. **Prefab defaults.** Do parts written without RESOURCE blocks get the prefab's resources (EC, fuel), and do parts without MODULE blocks get prefab defaults (`thrustPercentage 100`, crossfeed)?
5. **Node re-snap consistency** (HeatShield1 kept its wrong offsets), and the stock `yaw = 180° − az` convention on part types other than radial decouplers and fins. Test the readback diff on a sample craft.
6. **B9PartSwitch and multi-mode engines:** how to read subtype resources and all engine modes (ModuleB9PartSwitch subtypes, MultiModeEngine) inside the plugin.
7. **Mk1 pod hatch** position and EVA blockage were never re-verified. Test `/eva-go` on the pad with candidate radial layouts.
8. **Docking verdict.** Is `ModuleDockingNode` state "Docked (docker/dockee)" reliable and not localized in zh-cn? What does "PreAttached" mean after an in-flight couple? Are MechJeb `status` strings localized?
9. **RCS axis signs** for `control.right/up/forward` relative to the vessel frame (z out of the bottom). Calibrate with pulses.
10. **`krpc.paused`:** does it show the pause overlay, block node edits or patched-conic updates, or affect the bridge's `Update` queue?
11. **Expressions over enum-typed calls** (`vessel.situation`) and event cadence under high warp.
12. **Screenshots.** kRPC's are asynchronous and flight-only; the plugin coroutine path (ScreenCaptureModule) must work in VAB, KSC and main menu with the zh-cn UI.
13. **MechJeb 2.15.3 ascent-settings members** (AutoTurnStartVelocity, ClampAutoStageThrustPct, etc.) are from DLL string inspection. Confirm by compiling. Also confirm StagingController lifetime across vessel switches and reloads, and LandingPredictions availability.
14. **MechJeb node executor** sometimes skipped close kRPC-placed nodes: reproduce and characterize. Does MechJeb's landing autopilot also refuse near-atmosphere grazing orbits on other bodies?
15. **Main-thread job cancellation fix** needs live testing on long scene transitions (launch, load) with the job-id protocol.
16. **Save safety:** can `GamePersistence.SaveGame` / kRPC `save` / `quicksave` run in unstable situations (sub-orbital, in atmosphere)? Which scenes allow them?
17. **Lambert branch fix and `plan_ejection_node` normal-component formula** (`asin(sin δ / sin ν∞)` tilt) need validation against KSP patched conics; multi-rev Lambert is untested.
18. **`ut_at_true_anomaly` / `true_anomaly_at_radius`** for computing atmosphere-interface UTs (sign and wrap), and the reference frame of `simulate_aerodynamic_force_at`.
19. **`CrewMember.part` writability** and `sc.transfer_crew` behavior in 0.5.4. Does kRPC `vessel.type` set Relay correctly?
20. **Correction-timing evidence is contradictory** (Duna vs Eve). The tool should compute sensitivity rather than follow a rule.
21. **Career-mode gating** (maneuver-node editing tier, MechJeb tech unlocks) is untested; both saves are SANDBOX.
22. **Stale `.dll.new`** must be deleted before any finalize script runs. Check that the duplicate "Default Server" entry in kRPC `settings.cfg` does not break connections.
23. **Bridge addon identity.** Confirm the new plugin's assembly name and `KSPAssemblyDependency` split don't collide with the currently installed `KspAutomationBridge.dll` during migration.
24. **Performance.** Stream count vs `maxTimePerUpdate` (9800 µs) on this machine with 70-146 vessels in the save. Consider pruning before long campaigns.
25. **Non-ASCII craft and save names** (`默认`, Chinese craft names) must be tested end to end once the byte-level parser and the relaxed name rule are in place.