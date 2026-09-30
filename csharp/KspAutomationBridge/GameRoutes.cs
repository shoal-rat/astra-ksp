using System;
using System.Collections.Generic;
using System.IO;
using UnityEngine;
using JObj = System.Collections.Generic.Dictionary<string, object>;

namespace KspAutomationBridge
{
    /// <summary>Game-level routes: status, saves and scenes, vessel and part listings.</summary>
    internal static class GameRoutes
    {
        private static readonly string[] ReportedLaunchSites = { "LaunchPad", "Runway" };

        // A requested scene change that has not finished loading yet (main thread only). Guards against
        // a retried request loading the scene twice.
        private static string _pendingSceneChange;
        private static DateTime _pendingSinceUtc;
        private const double SceneChangeGiveUpS = 180.0;
        private const int StateMainThreadWaitMs = 3000;

        public static void Register(Router r)
        {
            SceneLoadedHook.Add(delegate { _pendingSceneChange = null; });
            SceneLoadWatch.Install();
            r.Inline("GET", "/ping", Ping, "Liveness without the main thread: version, raw scene, queue depth.");
            r.Inline("GET", "/state", State, "Game snapshot: scene, save, UT, warp, pause, active vessel, editor, launch sites, MechJeb.");
            r.Main("GET", "/pause", 15000, GetPause, "The game's true paused state (whoever paused it) and whether KSP's pause menu is open.");
            r.Main("POST", "/pause", 15000, SetPause, "Pause or resume the flight scene WITHOUT KSP's pause menu {paused: bool}.");
            r.Main("GET", "/vessels", 15000, Vessels, "Every vessel in the game with persistentId, type, situation, body.");
            r.Main("GET", "/vessel-parts", 15000, VesselParts, "Parts of a loaded vessel with index (= kRPC parts.all order), persistentId, crew, ports.");
            r.Main("POST", "/load-save", 30000, LoadSave, "Load a save (from the main menu or any scene) into the space center or flight.");
            r.Main("POST", "/space-center", 30000, SpaceCenter, "Go to the space center, optionally saving first.");
            r.Main("POST", "/fly-vessel", 30000, FlyVessel, "Take control of a vessel by persistentId or exact name.");
            r.Main("POST", "/revert", 30000, Revert, "Revert the flight to launch or to the editor.");
            r.Main("POST", "/craft/load", 30000, LoadCraft, "Open a saved .craft in the VAB/SPH editor.");
        }

        // ------------------------------------------------------------------ status

        private static JObj Ping(BridgeRequest req)
        {
            JObj d = new JObj();
            d["bridgeVersion"] = AutomationBridgeAddon.BridgeVersion;
            d["scene"] = HighLogic.LoadedScene.ToString(); // a racy read of a static field: fine for liveness
            d["queueDepth"] = AutomationBridgeAddon.Jobs.Depth;
            d["uptime_s"] = (DateTime.UtcNow - AutomationBridgeAddon.StartedUtc).TotalSeconds;
            d["mechjebAvailable"] = MechJebInfo.Available;
            return d;
        }

        private static JObj State(BridgeRequest req)
        {
            // Short enough that the degraded answer arrives before a caller's own ~5 s timeout.
            BridgeResult r = AutomationBridgeAddon.Jobs.Run("GET /state", delegate { return Router.Execute(StateSnapshot, req); },
                StateMainThreadWaitMs, req.ClientGone);
            if (r.Ok)
            {
                return r.Data;
            }
            if (r.Status == 504)
            {
                JObj d = Ping(req);
                d["busy"] = true;
                d["note"] = "The main thread is busy (scene loading?); only liveness fields are available. " + r.Error;
                return d;
            }
            throw new BridgeException(r.Status, r.Error, r.Hint);
        }

        private static JObj StateSnapshot(BridgeRequest req)
        {
            Game game = HighLogic.CurrentGame;
            bool flight = HighLogic.LoadedSceneIsFlight;
            JObj d = new JObj();
            d["bridgeVersion"] = AutomationBridgeAddon.BridgeVersion;
            d["kspVersion"] = Versioning.GetVersionStringFull();
            d["scene"] = HighLogic.LoadedScene.ToString();
            d["loadedSceneIsFlight"] = flight;
            d["loadedSceneIsEditor"] = HighLogic.LoadedSceneIsEditor;
            d["saveFolder"] = game != null ? HighLogic.SaveFolder : null;
            d["gameTitle"] = game != null ? game.Title : null;
            d["gameMode"] = game != null ? game.Mode.ToString() : null;
            d["ut"] = game != null ? (object)Util.SafeUt() : null;
            d["warpRate"] = TimeWarp.fetch != null ? TimeWarp.CurrentRate : 1f;
            d["warpMode"] = TimeWarp.fetch != null ? TimeWarp.WarpMode.ToString() : null;
            d["paused"] = GamePausedNow(); // true whoever paused: POST /pause, kRPC (pause menu), a dialog
            d["pauseMenuOpen"] = flight && PauseMenuOpen();

            Vessel active = flight ? FlightGlobals.ActiveVessel : null;
            if (active != null)
            {
                JObj av = Util.VesselRef(active);
                av["partCount"] = active.parts != null ? active.parts.Count : 0;
                av["crewCount"] = active.GetCrewCount();
                av["isEva"] = active.isEVA;
                av["landedAt"] = active.landedAt;
                av["currentStage"] = active.currentStage;
                d["activeVessel"] = av;
            }
            else
            {
                d["activeVessel"] = null;
            }

            d["editor"] = EditorSnapshot();
            d["sceneChangePending"] = PendingSceneChange();
            d["launchSites"] = game != null ? LaunchSites(game, req.Str("site")) : null;

            JObj mj = new JObj();
            mj["available"] = MechJebInfo.Available;
            mj["version"] = MechJebInfo.Version;
            d["mechjeb"] = mj;
            d["mechjebAvailable"] = MechJebInfo.Available;

            d["queueDepth"] = AutomationBridgeAddon.Jobs.Depth;
            d["abandonedJobs"] = AutomationBridgeAddon.Jobs.AbandonedCount;
            d["lastError"] = BridgeLog.LastError;
            d["lastErrorUtc"] = BridgeLog.LastErrorUtc;
            d["capcom"] = CapcomLog.Summary();
            return d;
        }

        private static JObj EditorSnapshot()
        {
            if (!HighLogic.LoadedSceneIsEditor)
            {
                return null;
            }
            JObj e = new JObj();
            EditorLogic logic = EditorLogic.fetch;
            ShipConstruct ship = logic != null ? logic.ship : null;
            int partCount = ship != null && ship.parts != null ? ship.parts.Count : 0;
            e["facility"] = EditorDriver.editorFacility.ToString();
            e["craftName"] = ship != null ? ship.shipName : null;
            e["partCount"] = partCount;
            e["loaded"] = partCount > 0; // the scene reports EDITOR seconds before a loaded craft's parts exist
            return e;
        }

        private static JObj LaunchSites(Game game, string extraSite)
        {
            List<string> sites = new List<string>(ReportedLaunchSites);
            if (!string.IsNullOrEmpty(extraSite) && !sites.Contains(extraSite))
            {
                sites.Add(extraSite);
            }
            JObj result = new JObj();
            foreach (string site in sites)
            {
                List<object> blockers = new List<object>();
                if (HighLogic.LoadedSceneIsFlight && FlightGlobals.Vessels != null)
                {
                    foreach (Vessel v in FlightGlobals.Vessels)
                    {
                        if (v != null && v.LandedOrSplashed && v.landedAt != null && v.landedAt.Contains(site))
                        {
                            blockers.Add(Blocker(v.vesselName, v.persistentId, v.vesselType));
                        }
                    }
                }
                else if (game.flightState != null)
                {
                    foreach (ProtoVessel pv in ShipConstruction.FindVesselsLandedAt(game.flightState, site))
                    {
                        blockers.Add(Blocker(pv.vesselName, pv.persistentId, pv.vesselType));
                    }
                }
                JObj s = new JObj();
                s["clear"] = blockers.Count == 0;
                s["blockers"] = blockers;
                result[site] = s;
            }
            return result;
        }

        private static JObj Blocker(string name, uint persistentId, VesselType type)
        {
            JObj b = new JObj();
            b["name"] = name;
            b["persistentId"] = persistentId;
            b["type"] = type.ToString();
            return b;
        }

        // ------------------------------------------------------------------ pause

        /// <summary>The true paused state, whoever paused (main thread).</summary>
        internal static bool GamePausedNow()
        {
            return PausePlanner.IsPaused(HighLogic.LoadedSceneIsFlight, FlightDriver.Pause, Time.timeScale);
        }

        /// <summary>Whether KSP's flight pause menu (ESC, or kRPC's `paused`) is showing.</summary>
        internal static bool PauseMenuOpen()
        {
            try
            {
                return PauseMenu.exists && PauseMenu.isOpen; // isOpen dereferences the menu instance
            }
            catch (Exception)
            {
                return false;
            }
        }

        /// <summary>The flight scene has finished starting and no scene load is pending (see PausePlanner.FlightLoaded).</summary>
        private static bool FlightLoaded()
        {
            return PausePlanner.FlightLoaded(FlightDriver.flightStarted, FlightDriver.fetch != null, SceneLoadWatch.LoadRequested);
        }

        private static JObj PauseState()
        {
            bool flight = HighLogic.LoadedSceneIsFlight;
            JObj d = new JObj();
            d["paused"] = GamePausedNow();
            d["scene"] = HighLogic.LoadedScene.ToString();
            d["inFlight"] = flight;
            d["flightLoaded"] = flight && FlightLoaded();
            d["flightDriverPause"] = flight && FlightDriver.Pause;
            d["timeScale"] = Time.timeScale;
            d["pauseMenuOpen"] = flight && PauseMenuOpen();
            return d;
        }

        private static JObj GetPause(BridgeRequest req)
        {
            JObj d = PauseState();
            if (!HighLogic.LoadedSceneIsFlight)
            {
                d["note"] = "POST /pause acts in the flight scene only (scene is " + HighLogic.LoadedScene + ").";
            }
            return d;
        }

        /// <summary>
        /// Pauses or resumes the flight scene without KSP's pause menu (FlightDriver.SetPause, as the
        /// stock quicksave dialogs do). Resuming also works when kRPC paused (it opens the menu): the
        /// menu is closed, which resumes. Pausing while the menu is open closes the menu and keeps the
        /// pause. All of it happens inside one main-thread call, so no physics tick runs in between.
        /// </summary>
        private static JObj SetPause(BridgeRequest req)
        {
            bool want = req.RequireBool("paused");
            if (!HighLogic.LoadedSceneIsFlight)
            {
                JObj outside = PauseState();
                outside["requested"] = want;
                outside["changed"] = false;
                outside["action"] = PauseAction.None.ToString();
                outside["note"] = "Nothing changed: pausing applies to the flight scene only (scene is " + HighLogic.LoadedScene
                    + "). Outside flight the game clock only moves while you warp in the space center or tracking station.";
                return outside;
            }
            bool wasPaused = GamePausedNow();
            PauseAction action = PausePlanner.Plan(want, FlightDriver.Pause, Time.timeScale, PauseMenuOpen(), FlightLoaded());
            switch (action)
            {
                case PauseAction.Refuse:
                    // The scene's start-up (or the old scene's teardown) resets the pause and the time scale.
                    // A pending vessel switch (EVA, boarding while paused) is no reason to refuse: it leaves
                    // the pause alone and completes once physics runs again.
                    throw new BridgeException(409, "The flight scene is still loading; its start-up would undo a pause set now.",
                        "Poll GET /pause until flightLoaded is true, then retry.");
                case PauseAction.Pause:
                    FlightDriver.SetPause(true, false);
                    break;
                case PauseAction.Resume:
                    FlightDriver.SetPause(false, false);
                    break;
                case PauseAction.CloseMenu:
                    PauseMenu.Close(); // resumes: Close() calls FlightDriver.SetPause(false, true)
                    break;
                case PauseAction.CloseMenuThenPause:
                    PauseMenu.Close();
                    FlightDriver.SetPause(true, false);
                    break;
            }
            JObj d = PauseState();
            bool paused = (bool)d["paused"];
            d["requested"] = want;
            d["wasPaused"] = wasPaused;
            d["changed"] = paused != wasPaused;
            d["action"] = action.ToString();
            d["closedPauseMenu"] = action == PauseAction.CloseMenu || action == PauseAction.CloseMenuThenPause;
            if (paused != want)
            {
                d["warning"] = want
                    ? "KSP did not report the pause; GET /pause to re-check."
                    : "Still paused after resuming: something else holds time at zero (a stock dialog such as the flight results or a quicksave prompt?). Look at the game (camera_look).";
            }
            return d;
        }

        // ------------------------------------------------------------------ listings

        private static JObj Vessels(BridgeRequest req)
        {
            Util.RequireGame();
            List<object> list = new List<object>();
            bool includeDebris = req.Bool("includeDebris", true);
            if (FlightGlobals.Vessels != null && FlightGlobals.Vessels.Count > 0)
            {
                foreach (Vessel v in FlightGlobals.Vessels)
                {
                    if (v == null || (!includeDebris && v.vesselType == VesselType.Debris))
                    {
                        continue;
                    }
                    JObj o = Util.VesselRef(v);
                    o["active"] = v == FlightGlobals.ActiveVessel;
                    o["owned"] = IsOwned(v); // only owned vessels can be flown (/fly-vessel)
                    o["landedAt"] = v.landedAt;
                    o["crew"] = CrewNames(v);
                    list.Add(o);
                }
            }
            else if (HighLogic.CurrentGame.flightState != null)
            {
                // Scenes without FlightGlobals vessels (editor, main menu after load): read the save state.
                foreach (ProtoVessel pv in HighLogic.CurrentGame.flightState.protoVessels)
                {
                    if (pv == null || (!includeDebris && pv.vesselType == VesselType.Debris))
                    {
                        continue;
                    }
                    JObj o = new JObj();
                    o["name"] = pv.vesselName;
                    o["persistentId"] = pv.persistentId;
                    o["type"] = pv.vesselType.ToString();
                    o["situation"] = pv.situation.ToString();
                    o["loaded"] = false;
                    o["landedAt"] = pv.landedAt;
                    list.Add(o);
                }
            }
            JObj d = new JObj();
            d["count"] = list.Count;
            d["vessels"] = list;
            return d;
        }

        private static List<object> CrewNames(Vessel v)
        {
            List<object> names = new List<object>();
            try
            {
                foreach (ProtoCrewMember pcm in v.GetVesselCrew())
                {
                    if (pcm != null)
                    {
                        names.Add(pcm.name);
                    }
                }
            }
            catch (Exception)
            {
                // Unloaded vessels without crew info: leave empty.
            }
            return names;
        }

        private static JObj VesselParts(BridgeRequest req)
        {
            Util.RequireFlightScene();
            Vessel v = Util.ResolveVessel(req, "vesselPersistentId", "vessel", false) ?? FlightGlobals.ActiveVessel;
            if (v == null || !v.loaded || v.parts == null)
            {
                throw new BridgeException(409, "The vessel is not loaded; parts exist only for vessels in physics range.");
            }
            List<object> parts = new List<object>();
            for (int i = 0; i < v.parts.Count; i++)
            {
                Part p = v.parts[i];
                JObj o = Util.PartRef(p);
                o["parentIndex"] = p.parent != null ? v.parts.IndexOf(p.parent) : -1;
                o["stage"] = p.inverseStage;
                o["crewCapacity"] = p.CrewCapacity;
                List<object> crew = new List<object>();
                if (p.protoModuleCrew != null)
                {
                    foreach (ProtoCrewMember pcm in p.protoModuleCrew)
                    {
                        crew.Add(pcm.name);
                    }
                }
                o["crew"] = crew;
                ModuleDockingNode port = p.FindModuleImplementing<ModuleDockingNode>();
                if (port != null)
                {
                    JObj dp = new JObj();
                    dp["state"] = port.state;
                    dp["nodeType"] = port.nodeType;
                    o["dockingPort"] = dp;
                }
                o["isControlFrom"] = v.GetReferenceTransformPart() == p;
                parts.Add(o);
            }
            JObj d = new JObj();
            d["vessel"] = Util.VesselRef(v);
            d["count"] = parts.Count;
            d["parts"] = parts;
            return d;
        }

        // ------------------------------------------------------------------ saves and scenes

        private static JObj PendingSceneChange()
        {
            if (_pendingSceneChange == null)
            {
                return null;
            }
            double age = (DateTime.UtcNow - _pendingSinceUtc).TotalSeconds;
            if (age > SceneChangeGiveUpS)
            {
                _pendingSceneChange = null;
                return null;
            }
            JObj p = new JObj();
            p["request"] = _pendingSceneChange;
            p["since_s"] = age;
            return p;
        }

        /// <summary>Refuses a second scene change while the previous one is still loading.</summary>
        private static void BeginSceneChange(string what)
        {
            JObj pending = PendingSceneChange();
            if (pending != null)
            {
                throw new BridgeException(409, "A scene change (" + _pendingSceneChange + ") was requested "
                    + ((double)pending["since_s"]).ToString("0") + " s ago and has not finished loading.",
                    "Poll GET /state until the scene changes (sceneChangePending becomes null), then retry if still needed.");
            }
            _pendingSceneChange = what;
            _pendingSinceUtc = DateTime.UtcNow;
        }

        /// <summary>Forgets a scene change that KSP refused or that threw before it started.</summary>
        private static void CancelSceneChange()
        {
            _pendingSceneChange = null;
        }

        private static JObj LoadSave(BridgeRequest req)
        {
            if (HighLogic.LoadedScene == GameScenes.LOADING || HighLogic.LoadedScene == GameScenes.LOADINGBUFFER)
            {
                throw new BridgeException(409, "KSP is still loading (scene " + HighLogic.LoadedScene + ").",
                    "Poll GET /ping until scene is MAINMENU, then retry.");
            }
            // 'saveName' is the pre-2.0 spelling of the folder parameter, kept for older clients.
            string folder = req.Str("saveFolder") ?? req.Str("saveName");
            if (folder == null)
            {
                throw new BridgeException(400, "Missing required parameter 'saveFolder'.");
            }
            Util.RequireSegment(folder, "save folder");
            string file = req.Str("saveFile", "persistent");
            if (file.EndsWith(".sfs", StringComparison.OrdinalIgnoreCase))
            {
                file = file.Substring(0, file.Length - 4);
            }
            Util.RequireSegment(file, "save file");
            string scene = req.Choice("scene", new[] { "spacecenter", "flight" }, "spacecenter");
            uint? vesselId = req.UInt("vesselPersistentId");

            string sfs = Path.Combine(Path.Combine(Util.SavesRoot, folder), file + ".sfs");
            if (!File.Exists(sfs))
            {
                throw new BridgeException(404, "Save file not found: saves/" + folder + "/" + file + ".sfs",
                    "Check the folder under the KSP saves directory.");
            }

            Game game = GamePersistence.LoadGame(file, folder, true, false);
            if (game == null || game.flightState == null)
            {
                throw new BridgeException(500, "KSP could not load saves/" + folder + "/" + file + ".sfs (incompatible or corrupt).");
            }
            // Every stock load path hands the loaded file to onGameStatePostLoad (the message system and
            // Breaking Ground's deployed science rebuild their state from it); LoadGame does not keep it.
            ConfigNode loadedRoot = GamePersistence.LoadSFSFile(file, folder);

            int focusIdx = -1;
            ProtoVessel focus = null;
            if (scene == "flight")
            {
                List<ProtoVessel> pvs = game.flightState.protoVessels;
                if (vesselId.HasValue)
                {
                    for (int i = 0; i < pvs.Count; i++)
                    {
                        if (pvs[i] != null && pvs[i].persistentId == vesselId.Value)
                        {
                            focusIdx = i;
                            break;
                        }
                    }
                    if (focusIdx < 0)
                    {
                        throw new BridgeException(404, "The save has no vessel with persistentId " + vesselId.Value + ".");
                    }
                }
                else
                {
                    focusIdx = game.flightState.activeVesselIdx;
                    if (focusIdx < 0 || focusIdx >= pvs.Count)
                    {
                        throw new BridgeException(409, "The save has no active vessel to resume in flight.",
                            "Pass vesselPersistentId, or load with scene=spacecenter.");
                    }
                }
                focus = pvs[focusIdx];
            }

            // Mirrors the stock load dialog (MainMenu.OnLoadDialogPipelineFinished): install the game,
            // refresh scenario modules, persist it as 'persistent', fire onGameStatePostLoad, then start
            // the requested scene.
            BeginSceneChange("load-save " + folder + "/" + file + " -> " + scene);
            try
            {
                HighLogic.CurrentGame = game;
                HighLogic.SaveFolder = folder;
                GamePersistence.UpdateScenarioModules(game);
                GamePersistence.SaveGame(game, "persistent", folder, SaveMode.OVERWRITE);
                if (loadedRoot != null)
                {
                    GameEvents.onGameStatePostLoad.Fire(loadedRoot);
                }
                if (focus != null)
                {
                    FlightDriver.StartAndFocusVessel(game, focusIdx);
                }
                else
                {
                    game.startScene = GameScenes.SPACECENTER;
                    game.Start();
                }
            }
            catch
            {
                CancelSceneChange();
                throw;
            }

            JObj d = new JObj();
            d["requested"] = true;
            d["saveFolder"] = folder;
            d["saveFile"] = file;
            d["scene"] = scene;
            if (focus != null)
            {
                JObj v = new JObj();
                v["name"] = focus.vesselName;
                v["persistentId"] = focus.persistentId;
                d["vessel"] = v;
            }
            d["note"] = "The scene change is asynchronous: poll GET /state until scene is "
                + (scene == "flight" ? "FLIGHT" : "SPACECENTER") + ".";
            return d;
        }

        private static JObj SpaceCenter(BridgeRequest req)
        {
            Util.RequireGame();
            JObj d = new JObj();
            if (HighLogic.LoadedScene == GameScenes.SPACECENTER)
            {
                d["requested"] = false;
                d["note"] = "Already at the space center.";
                return d;
            }
            bool flight = HighLogic.LoadedSceneIsFlight;
            bool? saveChoice = req.Bool("saveFirst");
            if (flight && !saveChoice.HasValue)
            {
                // Leaving flight either keeps or throws away everything since the last save: never by default.
                throw new BridgeException(400, "Missing required parameter 'saveFirst' when leaving flight.",
                    "saveFirst=true writes persistent.sfs first (KSP's clear-to-save rule applies); saveFirst=false discards everything since the last save or load.");
            }
            bool saveFirst = saveChoice.HasValue && saveChoice.Value;
            if (saveFirst && flight)
            {
                ClearToSaveStatus clear = FlightGlobals.ClearToSave();
                if (clear != ClearToSaveStatus.CLEAR)
                {
                    throw new BridgeException(409, "KSP refuses to save now: " + FlightGlobals.GetNotClearToSaveStatusReason(clear, "save") + " (" + clear + ").",
                        "Wait until the vessel is stable (not moving over the ground, not in the atmosphere under thrust), or pass saveFirst=false to leave without saving.");
                }
            }
            BeginSceneChange("space-center");
            try
            {
                if (saveFirst)
                {
                    GamePersistence.SaveGame("persistent", HighLogic.SaveFolder, SaveMode.OVERWRITE);
                }
                HighLogic.LoadScene(GameScenes.SPACECENTER);
            }
            catch
            {
                CancelSceneChange();
                throw;
            }
            d["requested"] = true;
            d["saved"] = saveFirst;
            d["discardedUnsavedFlight"] = flight && !saveFirst;
            d["note"] = flight && !saveFirst
                ? "Left flight WITHOUT saving: everything since the last save or load is discarded (like the stock 'exit without saving')."
                : "Poll GET /state until scene is SPACECENTER.";
            return d;
        }

        private static JObj FlyVessel(BridgeRequest req)
        {
            Util.RequireGame();
            GameScenes scene = HighLogic.LoadedScene;
            if (scene != GameScenes.SPACECENTER && scene != GameScenes.TRACKSTATION && scene != GameScenes.FLIGHT)
            {
                throw new BridgeException(409, "fly-vessel works from the space center, tracking station or flight (scene is " + scene + ").");
            }
            // 'vessel' is the pre-2.0 spelling of the name parameter.
            Vessel v = Util.ResolveVessel(req, "persistentId", req.Has("name") || !req.Has("vessel") ? "name" : "vessel", true);
            if (!IsOwned(v))
            {
                // The tracking station's Fly button refuses these too (SpaceTracking.FlyVessel): asteroids,
                // comets and vessels the space program does not own cannot be flown.
                throw new BridgeException(409, v.vesselName + " (" + v.vesselType + ") is not a vessel you own (discovery level "
                    + v.DiscoveryInfo.Level + "); KSP does not let you fly it.",
                    "Fly your own vessel and rendezvous with it instead.");
            }
            JObj d = new JObj();
            d["vessel"] = Util.VesselRef(v);
            if (scene == GameScenes.FLIGHT)
            {
                if (v == FlightGlobals.ActiveVessel)
                {
                    d["requested"] = false;
                    d["note"] = "Already the active vessel.";
                    return d;
                }
                // Loaded vessels switch in place; unloaded ones make KSP save 'persistent' and reload flight.
                bool wasLoaded = v.loaded;
                if (!wasLoaded)
                {
                    BeginSceneChange("fly-vessel " + v.persistentId);
                }
                bool switched;
                try
                {
                    switched = FlightGlobals.SetActiveVessel(v);
                }
                catch
                {
                    if (!wasLoaded)
                    {
                        CancelSceneChange();
                    }
                    throw;
                }
                if (!switched)
                {
                    if (!wasLoaded)
                    {
                        CancelSceneChange();
                    }
                    throw new BridgeException(409, "KSP refused to switch to " + v.vesselName + ".",
                        "Vessel switching is blocked while under acceleration or in atmosphere; stabilise first.");
                }
                d["requested"] = true;
                d["sceneReload"] = !wasLoaded;
                return d;
            }
            // Same path as the tracking station 'Fly' button (it also saves 'persistent').
            int index = FlightGlobals.Vessels.IndexOf(v);
            BeginSceneChange("fly-vessel " + v.persistentId);
            try
            {
                GamePersistence.SaveGame("persistent", HighLogic.SaveFolder, SaveMode.OVERWRITE);
                FlightDriver.StartAndFocusVessel("persistent", index);
            }
            catch
            {
                CancelSceneChange();
                throw;
            }
            d["requested"] = true;
            d["sceneReload"] = true;
            d["note"] = "Poll GET /state until scene is FLIGHT and activeVessel.persistentId matches.";
            return d;
        }

        /// <summary>Whether the space program owns the vessel (DiscoveryInfo.Level == Owned), as stock requires to fly it.</summary>
        internal static bool IsOwned(Vessel v)
        {
            try
            {
                return v.DiscoveryInfo == null || v.DiscoveryInfo.Level == DiscoveryLevels.Owned;
            }
            catch (Exception)
            {
                return true; // unreadable discovery info: do not block on a diagnostic
            }
        }

        private static JObj Revert(BridgeRequest req)
        {
            Util.RequireFlightScene();
            string to = req.Choice("to", new[] { "launch", "editor" }, null);
            JObj d = new JObj();
            d["to"] = to;
            if (to == "launch")
            {
                if (!FlightDriver.CanRevertToPostInit)
                {
                    throw new BridgeException(409, "Revert to launch is not available for this flight.");
                }
                BeginSceneChange("revert to launch");
                try
                {
                    FlightDriver.RevertToLaunch();
                }
                catch
                {
                    CancelSceneChange();
                    throw;
                }
            }
            else
            {
                if (!FlightDriver.CanRevertToPrelaunch)
                {
                    throw new BridgeException(409, "Revert to the editor is not available for this flight.");
                }
                EditorFacility facility = ShipConstruction.ShipType == EditorFacility.SPH ? EditorFacility.SPH : EditorFacility.VAB;
                BeginSceneChange("revert to " + facility);
                try
                {
                    FlightDriver.RevertToPrelaunch(facility);
                }
                catch
                {
                    CancelSceneChange();
                    throw;
                }
                d["facility"] = facility.ToString();
            }
            d["requested"] = true;
            return d;
        }

        private static JObj LoadCraft(BridgeRequest req)
        {
            Util.RequireGame();
            string craftName = Util.RequireSegment(req.RequireStr("craftName"), "craft name");
            string facilityText = req.Str("facility") ?? req.Str("building") ?? "VAB";
            EditorFacility facility;
            if (string.Equals(facilityText, "VAB", StringComparison.OrdinalIgnoreCase))
            {
                facility = EditorFacility.VAB;
            }
            else if (string.Equals(facilityText, "SPH", StringComparison.OrdinalIgnoreCase))
            {
                facility = EditorFacility.SPH;
            }
            else
            {
                throw new BridgeException(400, "facility must be VAB or SPH.");
            }
            string dir = Path.Combine(Path.Combine(Path.Combine(Util.SavesRoot, HighLogic.SaveFolder), "Ships"), facility.ToString());
            string path = Path.Combine(dir, craftName.EndsWith(".craft", StringComparison.OrdinalIgnoreCase) ? craftName : craftName + ".craft");
            if (!File.Exists(path))
            {
                throw new BridgeException(404, "Craft not found: saves/" + HighLogic.SaveFolder + "/Ships/" + facility + "/" + Path.GetFileName(path));
            }
            BeginSceneChange("craft/load " + craftName);
            try
            {
                EditorDriver.StartAndLoadVessel(path, facility);
            }
            catch
            {
                CancelSceneChange();
                throw;
            }
            JObj d = new JObj();
            d["requested"] = true;
            d["craftPath"] = path;
            d["facility"] = facility.ToString();
            d["note"] = "Loading is asynchronous: poll GET /state until editor.loaded is true (partCount > 0).";
            return d;
        }
    }
}
