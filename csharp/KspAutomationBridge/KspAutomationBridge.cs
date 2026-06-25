// NOTE FOR MAINTAINERS: This file is compiled into the KspAutomationBridge.dll plugin.
// Any change here (new endpoints, the in-game GUI, crew transfer) only takes effect after
// you rebuild the mod with scripts/build_bridge.ps1 and then restart / reload KSP so the
// freshly built DLL is picked up. Edits to this .cs alone do nothing until a rebuild+reload.
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using KSP.UI.Screens;
using MechJebLib.FuelFlowSimulation;
using MuMech;
using UnityEngine;

namespace KspAutomationBridge
{
    [KSPAddon(KSPAddon.Startup.EveryScene, true)]
    public sealed class AutomationBridgeAddon : MonoBehaviour
    {
        private const int DefaultPort = 48500;
        private const int StatusBufferCap = 200;
        private static readonly Regex CraftNameRegex = new Regex(@"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$", RegexOptions.Compiled);
        private static AutomationBridgeAddon _instance;

        private readonly ConcurrentQueue<MainThreadWork> _queue = new ConcurrentQueue<MainThreadWork>();
        private TcpListener _listener;
        private Thread _listenerThread;
        private volatile bool _running;
        private string _lastCraftName = "";
        private string _lastCraftPath = "";
        private string _lastError = "";

        // Player-typed mission commands waiting for the external agent to pick up via GET /command/pending.
        // Written by both POST /command (background thread) and the in-game "Run mission" button (main thread).
        private readonly ConcurrentQueue<string> _pendingCommands = new ConcurrentQueue<string>();

        // Ring buffer of recent agent status lines (phase + message). Shown live in the GUI panel and
        // returned by GET /status. Guarded by its own lock because both the listener threads (POST /status)
        // and the main thread (OnGUI render) touch it.
        private readonly object _statusLock = new object();
        private readonly List<StatusLine> _status = new List<StatusLine>(StatusBufferCap);

        // ---- In-game GUI state (only ever touched on the Unity main thread: OnGUI / Update) ----
        private bool _windowVisible;
        private Rect _windowRect = new Rect(60f, 60f, 460f, 420f);
        private string _commandInput = "";
        private Vector2 _logScroll = Vector2.zero;
        private bool _logAutoScroll = true;
        private int _lastStatusCountForScroll = -1;
        private ApplicationLauncherButton _appButton;

        public void Start()
        {
            if (_instance != null && _instance != this)
            {
                Destroy(gameObject);
                return;
            }

            _instance = this;
            DontDestroyOnLoad(gameObject);
            StartServer(DefaultPort);

            // Try to register an app-launcher (toolbar) button. If the launcher is not ready yet,
            // we subscribe to onGUIApplicationLauncherReady; the F8 hotkey works regardless.
            try
            {
                GameEvents.onGUIApplicationLauncherReady.Add(AddAppLauncherButton);
                AddAppLauncherButton();
            }
            catch (Exception ex)
            {
                Debug.LogWarning("[KspAutomationBridge] Could not hook app launcher: " + ex.Message);
            }
        }

        public void OnDestroy()
        {
            StopServer();
            try
            {
                GameEvents.onGUIApplicationLauncherReady.Remove(AddAppLauncherButton);
                RemoveAppLauncherButton();
            }
            catch (Exception)
            {
                // Ignore launcher teardown races.
            }
        }

        private void AddAppLauncherButton()
        {
            try
            {
                if (_appButton != null || ApplicationLauncher.Instance == null)
                {
                    return;
                }

                Texture2D icon = MakeButtonIcon();
                _appButton = ApplicationLauncher.Instance.AddModApplication(
                    OnAppButtonToggle,
                    OnAppButtonToggle,
                    null, null, null, null,
                    ApplicationLauncher.AppScenes.ALWAYS,
                    icon);
            }
            catch (Exception ex)
            {
                Debug.LogWarning("[KspAutomationBridge] AddModApplication failed: " + ex.Message);
            }
        }

        private void RemoveAppLauncherButton()
        {
            if (_appButton != null && ApplicationLauncher.Instance != null)
            {
                ApplicationLauncher.Instance.RemoveModApplication(_appButton);
            }
            _appButton = null;
        }

        private void OnAppButtonToggle()
        {
            _windowVisible = !_windowVisible;
        }

        private static Texture2D MakeButtonIcon()
        {
            // Tiny solid-colour 38x38 icon so the toolbar button is visible without shipping an asset.
            Texture2D tex = new Texture2D(38, 38, TextureFormat.RGBA32, false);
            Color fill = new Color(0.20f, 0.65f, 0.95f, 1f);
            Color[] pixels = new Color[38 * 38];
            for (int i = 0; i < pixels.Length; i++)
            {
                pixels[i] = fill;
            }
            tex.SetPixels(pixels);
            tex.Apply();
            return tex;
        }

        public void Update()
        {
            MainThreadWork work;
            while (_queue.TryDequeue(out work))
            {
                try
                {
                    work.Result = work.Action();
                }
                catch (Exception ex)
                {
                    _lastError = ex.ToString();
                    work.Result = CommandResult.Fail(ex.Message);
                }
                finally
                {
                    work.Done.Set();
                }
            }

            // F8 toggles the GUI window (fallback / always-available alongside the toolbar button).
            try
            {
                if (Input.GetKeyDown(KeyCode.F8))
                {
                    _windowVisible = !_windowVisible;
                }
            }
            catch (Exception)
            {
                // Never let input polling kill the addon.
            }
        }

        // OnGUI runs every IMGUI frame on the main thread. Everything here is wrapped so a render
        // glitch can never throw out of the addon. No KSP simulation calls happen here; the only
        // shared state read is the status ring buffer (under its lock) and the only writes are GUI
        // fields plus enqueuing a pending command (thread-safe queue).
        public void OnGUI()
        {
            if (!_windowVisible)
            {
                return;
            }

            try
            {
                _windowRect = GUILayout.Window(0x4B535042, _windowRect, DrawWindow, "KSP Automation Bridge");
            }
            catch (Exception ex)
            {
                // Swallow: OnGUI must never throw. Record for /state diagnostics.
                _lastError = "OnGUI: " + ex.Message;
            }
        }

        private void DrawWindow(int windowId)
        {
            try
            {
                // Header: connection / listener state.
                string conn = _running
                    ? "Listening on http://127.0.0.1:" + DefaultPort
                    : "Bridge listener STOPPED";
                GUILayout.BeginHorizontal();
                GUILayout.Label(conn);
                GUILayout.FlexibleSpace();
                int pending = _pendingCommands.Count;
                GUILayout.Label("pending: " + pending);
                GUILayout.EndHorizontal();

                GUILayout.Space(4f);
                GUILayout.Label("Mission command:");
                _commandInput = GUILayout.TextField(_commandInput ?? "", GUILayout.MinHeight(22f));

                GUILayout.BeginHorizontal();
                if (GUILayout.Button("Run mission", GUILayout.Height(26f)))
                {
                    string cmd = (_commandInput ?? "").Trim();
                    if (cmd.Length > 0)
                    {
                        // Same store the external agent drains via GET /command/pending.
                        _pendingCommands.Enqueue(cmd);
                        AppendStatus("queued", "Player queued mission: " + cmd);
                        _commandInput = "";
                    }
                }
                if (GUILayout.Button("Clear log", GUILayout.Height(26f), GUILayout.Width(80f)))
                {
                    lock (_statusLock)
                    {
                        _status.Clear();
                    }
                }
                _logAutoScroll = GUILayout.Toggle(_logAutoScroll, "auto-scroll", GUILayout.Width(90f));
                GUILayout.EndHorizontal();

                GUILayout.Space(4f);
                GUILayout.Label("Agent status:");

                // Snapshot the ring buffer under lock, then render outside the lock.
                StatusLine[] lines;
                lock (_statusLock)
                {
                    lines = _status.ToArray();
                }

                // Auto-scroll to the bottom whenever new lines arrived since the last frame.
                if (_logAutoScroll && lines.Length != _lastStatusCountForScroll)
                {
                    _logScroll.y = float.MaxValue;
                    _lastStatusCountForScroll = lines.Length;
                }

                _logScroll = GUILayout.BeginScrollView(_logScroll, GUILayout.MinHeight(200f));
                if (lines.Length == 0)
                {
                    GUILayout.Label("(no status yet)");
                }
                else
                {
                    foreach (StatusLine line in lines)
                    {
                        GUILayout.Label("[" + line.Phase + "] " + line.Message);
                    }
                }
                GUILayout.EndScrollView();

                GUILayout.Space(2f);
                GUILayout.Label("Toggle: toolbar button or F8. Build: scripts/build_bridge.ps1 then reload KSP.");

                // Let the player drag the window by its title bar.
                GUI.DragWindow(new Rect(0f, 0f, 10000f, 22f));
            }
            catch (Exception ex)
            {
                _lastError = "DrawWindow: " + ex.Message;
            }
        }

        private void AppendStatus(string phase, string message)
        {
            StatusLine line = new StatusLine
            {
                Phase = phase ?? "",
                Message = message ?? "",
                TimestampUtc = DateTime.UtcNow.ToString("HH:mm:ss", CultureInfo.InvariantCulture)
            };
            lock (_statusLock)
            {
                _status.Add(line);
                if (_status.Count > StatusBufferCap)
                {
                    _status.RemoveRange(0, _status.Count - StatusBufferCap);
                }
            }
        }

        private void StartServer(int port)
        {
            if (_running)
            {
                return;
            }

            _listener = new TcpListener(IPAddress.Parse("127.0.0.1"), port);
            _listener.Start();
            _running = true;
            _listenerThread = new Thread(ListenLoop) { IsBackground = true, Name = "KSP Automation Bridge" };
            _listenerThread.Start();
            Debug.Log("[KspAutomationBridge] Listening on http://127.0.0.1:" + port);
        }

        private void StopServer()
        {
            _running = false;
            try
            {
                if (_listener != null)
                {
                    _listener.Stop();
                }
            }
            catch (Exception)
            {
                // Ignore shutdown races.
            }
        }

        private void ListenLoop()
        {
            while (_running)
            {
                try
                {
                    TcpClient client = _listener.AcceptTcpClient();
                    ThreadPool.QueueUserWorkItem(_ => HandleClient(client));
                }
                catch (SocketException)
                {
                    if (_running)
                    {
                        Debug.LogWarning("[KspAutomationBridge] Socket error while accepting client.");
                    }
                }
                catch (Exception ex)
                {
                    Debug.LogError("[KspAutomationBridge] Listener error: " + ex);
                }
            }
        }

        private void HandleClient(TcpClient client)
        {
            using (client)
            using (NetworkStream stream = client.GetStream())
            {
                HttpRequest request = HttpRequest.Read(stream);
                CommandResult result = Route(request);
                byte[] body = Encoding.UTF8.GetBytes(result.ToJson());
                string status = result.HttpStatus + " " + (result.HttpStatus == 200 ? "OK" : "Error");
                string headers =
                    "HTTP/1.1 " + status + "\r\n" +
                    "Content-Type: application/json; charset=utf-8\r\n" +
                    "Content-Length: " + body.Length + "\r\n" +
                    "Connection: close\r\n\r\n";
                byte[] headerBytes = Encoding.ASCII.GetBytes(headers);
                stream.Write(headerBytes, 0, headerBytes.Length);
                stream.Write(body, 0, body.Length);
            }
        }

        private CommandResult Route(HttpRequest request)
        {
            if (request.Method == "GET" && request.Path == "/state")
            {
                return RunOnMainThread(StateCommand);
            }

            if (request.Method == "POST" && request.Path == "/craft/load")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => LoadCraftCommand(fields), 60000);
            }

            if (request.Method == "POST" && request.Path == "/launch")
            {
                return RunOnMainThread(LaunchCommand, 60000);
            }

            if (request.Method == "POST" && request.Path == "/revert")
            {
                return RunOnMainThread(RevertCommand, 60000);
            }

            if (request.Method == "POST" && request.Path == "/save")
            {
                return RunOnMainThread(SaveCommand, 60000);
            }

            if (request.Method == "POST" && request.Path == "/save/load")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => LoadSaveCommand(fields), 60000);
            }

            if (request.Method == "POST" && request.Path == "/space-center")
            {
                return RunOnMainThread(SpaceCenterCommand, 60000);
            }

            if (request.Method == "POST" && request.Path == "/vessel/refuel")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => RefuelVesselCommand(fields), 30000);
            }

            if (request.Method == "POST" && request.Path == "/part/resolve")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => ResolvePartCommand(fields), 30000);
            }

            if (request.Method == "POST" && request.Path == "/parts/search")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => SearchPartsCommand(fields), 30000);
            }

            if (request.Method == "POST" && request.Path == "/reset")
            {
                return RunOnMainThread(ResetCommand, 60000);
            }

            // ---- Player mission command queue (no KSP API: thread-safe queue, handle inline) ----
            if (request.Method == "POST" && request.Path == "/command")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return EnqueueCommand(fields);
            }

            if (request.Method == "GET" && request.Path == "/command/pending")
            {
                return DequeuePendingCommand();
            }

            // ---- Agent status ring buffer (no KSP API: lock-protected list, handle inline) ----
            if (request.Method == "POST" && request.Path == "/status")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return PostStatus(fields);
            }

            if (request.Method == "GET" && request.Path == "/status")
            {
                return GetStatus();
            }

            // ---- Crew transfer: touches the KSP API, MUST run on the main thread ----
            if (request.Method == "POST" && request.Path == "/transfer-crew")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => TransferCrewCommand(fields), 30000);
            }

            // ---- EVA + plant flag: put a seated kerbal on EVA on a LANDED vessel and plant the stock
            // flag headlessly. Touches the KSP API (FlightEVA + KerbalEVA) -> MUST run on the main thread.
            if (request.Method == "POST" && request.Path == "/eva-flag")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => EvaPlantFlagCommand(fields), 30000);
            }

            // ---- EVA-only: put a seated kerbal on EVA WITHOUT planting a flag (e.g. to walk to a
            // ladder, take surface science, or set up a board). Touches FlightEVA -> main thread.
            if (request.Method == "POST" && request.Path == "/eva-go")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => EvaGoCommand(fields), 30000);
            }

            // ---- Re-board: send the active (or named) EVA kerbal back into the nearest crewable part
            // with a free seat. Closes the loop after /eva-go or /eva-flag. KerbalEVA.BoardPart -> main thread.
            if (request.Method == "POST" && request.Path == "/eva-board")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => EvaBoardCommand(fields), 30000);
            }

            // ---- Crew spawn: seat a kerbal from the roster into an empty crewable part (a headless
            // launch leaves crewed pods empty). Lets the agent put REAL people aboard before a dock.
            if (request.Method == "POST" && request.Path == "/spawn-crew")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => SpawnCrewCommand(fields), 30000);
            }

            // ---- MechJeb autopilots: delegate the HARD control (rendezvous, docking) to MechJeb
            // instead of hand-rolling guidance. All touch the KSP + MechJeb API -> main thread. The
            // enable handlers return immediately; the agent polls GET /mj-status for completion.
            if (request.Method == "POST" && request.Path == "/mj-rendezvous")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => MjRendezvousCommand(fields), 30000);
            }

            if (request.Method == "POST" && request.Path == "/mj-dock")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => MjDockCommand(fields), 30000);
            }

            if (request.Method == "POST" && request.Path == "/mj-disable")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => MjDisableCommand(fields), 30000);
            }

            if (request.Method == "GET" && request.Path == "/mj-status")
            {
                return RunOnMainThread(() => MjStatusCommand(), 15000);
            }

            if (request.Method == "GET" && request.Path == "/mj-stage-stats")
            {
                return RunOnMainThread(() => MjStageStatsCommand(), 15000);
            }

            if (request.Method == "POST" && request.Path == "/mj-ascent")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => MjAscentCommand(fields), 30000);
            }

            if (request.Method == "POST" && request.Path == "/mj-execute-node")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => MjExecuteNodeCommand(fields), 30000);
            }

            if (request.Method == "POST" && request.Path == "/mj-plan")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => MjPlanCommand(fields), 30000);
            }

            if (request.Method == "POST" && request.Path == "/mj-land")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => MjLandCommand(fields), 30000);
            }

            // Enter FLIGHT on an existing orbital vessel from the Space Center / Tracking Station.
            // kRPC can't do this; it removes the per-phase computer-use "click Fly" friction.
            if (request.Method == "POST" && request.Path == "/fly-vessel")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => FlyVesselCommand(fields), 30000);
            }

            // ---- Precise EVA movement: drive the active EVA kerbal toward a surface target. We compute
            // the world-space target from {lat,lon} with CelestialBody.GetWorldSurfacePosition (the body's
            // OWN geodesy, so the destination is exact) and hand it to KerbalEVA.SetWaypoint, which is the
            // stock engine's own walk/jet pathing — movement is "error-free" because the game does it, not us.
            if (request.Method == "POST" && request.Path == "/eva-walk-to")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => EvaWalkToCommand(fields), 30000);
            }

            // ---- EVA read-back: position (lat/lon/alt), surface velocity, ladder/ground state, fuel.
            if (request.Method == "GET" && request.Path == "/eva-status")
            {
                return RunOnMainThread(EvaStatusCommand, 15000);
            }

            // ---- Comprehensive crew/personnel read-back ----
            if (request.Method == "GET" && request.Path == "/crew-list")
            {
                return RunOnMainThread(CrewListCommand, 15000);
            }

            if (request.Method == "GET" && request.Path == "/crew-roster")
            {
                return RunOnMainThread(CrewRosterCommand, 15000);
            }

            // ---- Game-data read-back for the LLM planner's own calculations ----
            if (request.Method == "POST" && request.Path == "/vessel-info")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => VesselInfoCommand(fields), 20000);
            }

            if (request.Method == "POST" && request.Path == "/parts-list")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => PartsListCommand(fields), 20000);
            }

            if (request.Method == "POST" && request.Path == "/resources")
            {
                Dictionary<string, string> fields = JsonObject.Parse(request.Body);
                return RunOnMainThread(() => ResourcesCommand(fields), 20000);
            }

            return CommandResult.Fail("Unknown route: " + request.Method + " " + request.Path, 404);
        }

        private CommandResult RunOnMainThread(Func<CommandResult> action, int timeoutMs = 30000)
        {
            MainThreadWork work = new MainThreadWork(action);
            _queue.Enqueue(work);
            if (!work.Done.WaitOne(timeoutMs))
            {
                return CommandResult.Fail("Timed out waiting for Unity main thread.", 504);
            }

            return work.Result ?? CommandResult.Fail("Command returned no result.");
        }

        private CommandResult StateCommand()
        {
            Dictionary<string, object> data = new Dictionary<string, object>
            {
                { "scene", HighLogic.LoadedScene.ToString() },
                { "loadedSceneIsFlight", HighLogic.LoadedSceneIsFlight },
                { "loadedSceneIsEditor", HighLogic.LoadedSceneIsEditor },
                { "saveFolder", HighLogic.SaveFolder ?? "" },
                { "lastCraftName", _lastCraftName },
                { "lastCraftPath", _lastCraftPath },
                { "queueDepth", _queue.Count },
                { "lastError", _lastError }
            };

            if (HighLogic.LoadedSceneIsFlight && FlightGlobals.ActiveVessel != null)
            {
                data["activeVessel"] = FlightGlobals.ActiveVessel.vesselName;
                data["activeVesselSituation"] = FlightGlobals.ActiveVessel.situation.ToString();
            }

            return CommandResult.Ok(data);
        }

        private CommandResult LoadCraftCommand(Dictionary<string, string> fields)
        {
            if (HighLogic.CurrentGame == null)
            {
                return CommandResult.Fail("No KSP save is loaded. Load a sandbox/science/career save first.");
            }

            string craftName = GetRequired(fields, "craftName");
            ValidateCraftName(craftName);
            EditorFacility facility = ParseFacility(GetOptional(fields, "building", "VAB"));
            string craftPath = ResolveCraftPath(craftName, GetOptional(fields, "craftPath", ""), facility);
            if (!File.Exists(craftPath))
            {
                return CommandResult.Fail("Craft file does not exist: " + craftPath, 404);
            }

            _lastCraftName = craftName;
            _lastCraftPath = craftPath;
            _lastError = "";
            EditorDriver.StartAndLoadVessel(craftPath, facility);

            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "message", "Craft load requested." },
                { "craftName", craftName },
                { "craftPath", craftPath },
                { "facility", facility.ToString() }
            });
        }

        private CommandResult LaunchCommand()
        {
            if (HighLogic.LoadedSceneIsFlight)
            {
                return CommandResult.Ok(new Dictionary<string, object> { { "message", "Already in flight scene." } });
            }

            if (!HighLogic.LoadedSceneIsEditor || EditorLogic.fetch == null)
            {
                return CommandResult.Fail("Launch requires the editor scene with a loaded craft.");
            }

            MethodInfo noArgLaunch = typeof(EditorLogic).GetMethod(
                "launchVessel",
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                null,
                Type.EmptyTypes,
                null);
            if (noArgLaunch != null)
            {
                try
                {
                    _lastError = "";
                    noArgLaunch.Invoke(EditorLogic.fetch, new object[0]);
                    return CommandResult.Ok(new Dictionary<string, object> { { "message", "Launch requested." } });
                }
                catch (TargetInvocationException ex)
                {
                    Exception inner = ex.InnerException ?? ex;
                    _lastError = inner.ToString();
                    return CommandResult.Fail(inner.Message);
                }
            }

            if (EditorLogic.fetch.launchBtn == null)
            {
                return CommandResult.Fail("Editor launch method and launch button are unavailable.");
            }

            _lastError = "";
            EditorLogic.fetch.launchBtn.onClick.Invoke();
            return CommandResult.Ok(new Dictionary<string, object> { { "message", "Launch requested." } });
        }

        private CommandResult RevertCommand()
        {
            if (!HighLogic.LoadedSceneIsFlight)
            {
                return CommandResult.Ok(new Dictionary<string, object> { { "message", "Not in flight; nothing to revert." } });
            }

            if (FlightDriver.CanRevertToPrelaunch)
            {
                EditorFacility facility = ShipConstruction.ShipType == EditorFacility.SPH ? EditorFacility.SPH : EditorFacility.VAB;
                FlightDriver.RevertToPrelaunch(facility);
                return CommandResult.Ok(new Dictionary<string, object> { { "message", "Revert to editor requested." } });
            }

            if (FlightDriver.CanRevertToPostInit)
            {
                FlightDriver.RevertToLaunch();
                return CommandResult.Ok(new Dictionary<string, object> { { "message", "Revert to launch requested." } });
            }

            return CommandResult.Fail("KSP reports that revert is unavailable.");
        }

        private CommandResult SaveCommand()
        {
            if (HighLogic.CurrentGame == null || string.IsNullOrEmpty(HighLogic.SaveFolder))
            {
                return CommandResult.Fail("No KSP save is loaded. Load a sandbox/science/career save first.");
            }

            GamePersistence.SaveGame("persistent", HighLogic.SaveFolder, SaveMode.OVERWRITE);
            return CommandResult.Ok(new Dictionary<string, object> { { "message", "Persistent save written." } });
        }

        private CommandResult LoadSaveCommand(Dictionary<string, string> fields)
        {
            string saveFolder = GetRequired(fields, "saveFolder");
            ValidateSaveFolder(saveFolder);
            string saveDir = Path.GetFullPath(Path.Combine(KSPUtil.ApplicationRootPath, "saves", saveFolder));
            string persistentPath = Path.Combine(saveDir, "persistent.sfs");
            if (!File.Exists(persistentPath))
            {
                return CommandResult.Fail("Persistent save does not exist: " + persistentPath, 404);
            }

            Game game = GamePersistence.LoadGame("persistent", saveFolder, true, false);
            if (game == null)
            {
                return CommandResult.Fail("KSP failed to load save: " + saveFolder);
            }

            HighLogic.CurrentGame = game;
            HighLogic.SaveFolder = saveFolder;
            string scene = GetOptional(fields, "scene", "spacecenter").ToLowerInvariant();
            if (scene == "flight")
            {
                // Resume the recorded active vessel directly in flight, so an in-space vessel can be
                // re-controlled after a bridge rebuild without the tracking station (which the bridge
                // and kRPC can't drive). game.Start() loads the saved active vessel into the FLIGHT scene.
                game.startScene = GameScenes.FLIGHT;
                game.Start();
            }
            else
            {
                HighLogic.LoadScene(GameScenes.SPACECENTER);
            }
            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "message", "Save load requested." },
                { "saveFolder", saveFolder },
                { "scene", scene }
            });
        }

        private CommandResult SpaceCenterCommand()
        {
            if (HighLogic.CurrentGame == null || string.IsNullOrEmpty(HighLogic.SaveFolder))
            {
                return CommandResult.Fail("No KSP save is loaded. Load a sandbox/science/career save first.");
            }

            GamePersistence.SaveGame("persistent", HighLogic.SaveFolder, SaveMode.OVERWRITE);
            HighLogic.LoadScene(GameScenes.SPACECENTER);
            return CommandResult.Ok(new Dictionary<string, object> { { "message", "Space center requested without reverting active vessels." } });
        }

        private CommandResult RefuelVesselCommand(Dictionary<string, string> fields)
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
            {
                return CommandResult.Fail("Refuel requires an active vessel in flight.");
            }

            Vessel vessel = FlightGlobals.ActiveVessel;
            string vesselName = GetOptional(fields, "vesselName", "");
            if (!string.IsNullOrEmpty(vesselName))
            {
                ValidateCraftName(vesselName);
                vessel = null;
                foreach (Vessel candidate in FlightGlobals.Vessels)
                {
                    if (candidate != null && string.Equals(candidate.vesselName, vesselName, StringComparison.Ordinal))
                    {
                        vessel = candidate;
                        break;
                    }
                }

                if (vessel == null)
                {
                    return CommandResult.Fail("Vessel not found: " + vesselName, 404);
                }
            }

            double fraction = 1.0;
            string fractionText = GetOptional(fields, "fraction", "1.0");
            double.TryParse(fractionText, NumberStyles.Float, CultureInfo.InvariantCulture, out fraction);
            fraction = Math.Max(0.0, Math.Min(1.0, fraction));

            HashSet<string> allowed = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                "LiquidFuel",
                "Oxidizer",
                "MonoPropellant",
                "ElectricCharge",
                "SolidFuel",
                "LqdHydrogen",
                "LqdMethane",
                "Methane",
                "LqdOxygen"
            };
            string resourcesText = GetOptional(fields, "resources", "");
            if (!string.IsNullOrEmpty(resourcesText))
            {
                allowed.Clear();
                string[] names = resourcesText.Split(',');
                foreach (string raw in names)
                {
                    string name = raw.Trim();
                    if (name.Length > 0)
                    {
                        allowed.Add(name);
                    }
                }
            }

            int resourcesFilled = 0;
            double beforeAmount = 0.0;
            double afterAmount = 0.0;
            foreach (Part part in vessel.parts)
            {
                if (part == null || part.Resources == null)
                {
                    continue;
                }

                foreach (PartResource resource in part.Resources)
                {
                    if (resource == null || !allowed.Contains(resource.resourceName) || resource.maxAmount <= 0.0)
                    {
                        continue;
                    }

                    beforeAmount += resource.amount;
                    resource.amount = Math.Min(resource.maxAmount, resource.maxAmount * fraction);
                    afterAmount += resource.amount;
                    resourcesFilled++;
                }
            }

            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "message", "Vessel resources filled." },
                { "vesselName", vessel.vesselName },
                { "resourcesFilled", resourcesFilled },
                { "beforeAmount", beforeAmount },
                { "afterAmount", afterAmount },
                { "fraction", fraction }
            });
        }

        private CommandResult ResolvePartCommand(Dictionary<string, string> fields)
        {
            string partId = GetRequired(fields, "partId");
            Dictionary<string, object> data = new Dictionary<string, object> { { "partId", partId } };
            try
            {
                string resolvedName = KSPUtil.GetPartName(partId);
                AvailablePart available = PartLoader.getPartInfoByName(resolvedName);
                data["resolvedName"] = resolvedName;
                data["found"] = available != null;
                if (available != null)
                {
                    data["availableName"] = available.name;
                    data["title"] = available.title;
                    data["prefabName"] = available.partPrefab != null ? available.partPrefab.name : "";
                }
            }
            catch (Exception ex)
            {
                data["exception"] = ex.ToString();
            }

            return CommandResult.Ok(data);
        }

        private CommandResult SearchPartsCommand(Dictionary<string, string> fields)
        {
            string needle = GetOptional(fields, "needle", "");
            StringBuilder names = new StringBuilder();
            int count = 0;
            foreach (AvailablePart part in PartLoader.LoadedPartsList)
            {
                string name = part.name ?? "";
                string title = part.title ?? "";
                if (needle.Length == 0 ||
                    name.IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0 ||
                    title.IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    if (count > 0)
                    {
                        names.Append("\n");
                    }
                    names.Append(name).Append("|").Append(title);
                    count++;
                }
            }

            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "count", count },
                { "parts", names.ToString() }
            });
        }

        private CommandResult ResetCommand()
        {
            if (HighLogic.LoadedSceneIsFlight)
            {
                if (FlightDriver.CanRevertToPrelaunch)
                {
                    FlightDriver.RevertToPrelaunch(EditorFacility.VAB);
                    return CommandResult.Ok(new Dictionary<string, object> { { "message", "Reset via revert to VAB requested." } });
                }
            }

            HighLogic.LoadScene(GameScenes.SPACECENTER);
            return CommandResult.Ok(new Dictionary<string, object> { { "message", "Reset to space center requested." } });
        }

        // POST /command  body {"command": "..."}  -> enqueue a pending mission command.
        private CommandResult EnqueueCommand(Dictionary<string, string> fields)
        {
            string command = GetOptional(fields, "command", "").Trim();
            if (command.Length == 0)
            {
                return CommandResult.Fail("Missing required field: command");
            }

            _pendingCommands.Enqueue(command);
            AppendStatus("queued", "Command queued via HTTP: " + command);
            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "message", "Command queued." },
                { "command", command },
                { "pending", _pendingCommands.Count }
            });
        }

        // GET /command/pending  -> pop the oldest queued command, or {"command": null} if empty.
        private CommandResult DequeuePendingCommand()
        {
            string command;
            if (_pendingCommands.TryDequeue(out command))
            {
                return CommandResult.Ok(new Dictionary<string, object>
                {
                    { "command", command },
                    { "remaining", _pendingCommands.Count }
                });
            }

            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "command", null },
                { "remaining", 0 }
            });
        }

        // POST /status  body {"phase": "...", "message": "..."}  -> append to the ring buffer.
        private CommandResult PostStatus(Dictionary<string, string> fields)
        {
            string phase = GetOptional(fields, "phase", "");
            string message = GetOptional(fields, "message", "");
            if (phase.Length == 0 && message.Length == 0)
            {
                return CommandResult.Fail("Provide at least one of: phase, message.");
            }

            AppendStatus(phase, message);
            int count;
            lock (_statusLock)
            {
                count = _status.Count;
            }

            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "message", "Status appended." },
                { "count", count }
            });
        }

        // GET /status  -> recent status lines as a JSON array.
        private CommandResult GetStatus()
        {
            StatusLine[] lines;
            lock (_statusLock)
            {
                lines = _status.ToArray();
            }

            StringBuilder sb = new StringBuilder();
            sb.Append("[");
            for (int i = 0; i < lines.Length; i++)
            {
                if (i > 0)
                {
                    sb.Append(",");
                }
                sb.Append("{\"ts\":\"").Append(JsonEscape(lines[i].TimestampUtc))
                  .Append("\",\"phase\":\"").Append(JsonEscape(lines[i].Phase))
                  .Append("\",\"message\":\"").Append(JsonEscape(lines[i].Message))
                  .Append("\"}");
            }
            sb.Append("]");

            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "count", lines.Length },
                { "status", new RawJson(sb.ToString()) }
            });
        }

        // POST /spawn-crew {"vessel"?: name} -> seat a roster kerbal into the first empty crewable
        // seat (a headless launch leaves crewed pods empty). MUST run on the main thread.
        private CommandResult SpawnCrewCommand(Dictionary<string, string> fields)
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
            {
                return CommandResult.Fail("Crew spawn requires an active vessel in flight.");
            }
            string vesselName = GetOptional(fields, "vessel", "").Trim();
            Vessel target = FlightGlobals.ActiveVessel;
            if (vesselName.Length > 0 && FlightGlobals.Vessels != null)
            {
                foreach (Vessel v in FlightGlobals.Vessels)
                {
                    if (v != null && v.loaded && v.vesselName != null &&
                        v.vesselName.IndexOf(vesselName, StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        target = v;
                        break;
                    }
                }
            }
            Part destPart = null;
            if (target != null && target.parts != null)
            {
                foreach (Part part in target.parts)
                {
                    if (part != null && part.CrewCapacity > 0 &&
                        part.protoModuleCrew != null && part.protoModuleCrew.Count < part.CrewCapacity)
                    {
                        destPart = part;
                        break;
                    }
                }
            }
            if (destPart == null)
            {
                return CommandResult.Fail("No crewable part with a free seat found.");
            }
            ProtoCrewMember pcm = null;
            try
            {
                KerbalRoster roster = HighLogic.CurrentGame.CrewRoster;
                foreach (ProtoCrewMember c in roster.Crew)
                {
                    if (c != null && c.rosterStatus == ProtoCrewMember.RosterStatus.Available &&
                        c.type == ProtoCrewMember.KerbalType.Crew)
                    {
                        pcm = c;
                        break;
                    }
                }
                if (pcm == null)
                {
                    pcm = roster.GetNewKerbal(ProtoCrewMember.KerbalType.Crew);
                }
            }
            catch (Exception ex)
            {
                return CommandResult.Fail("Could not get a kerbal from the roster: " + ex.Message);
            }
            if (pcm == null)
            {
                return CommandResult.Fail("No available kerbal to spawn.");
            }
            try
            {
                pcm.rosterStatus = ProtoCrewMember.RosterStatus.Assigned;
                destPart.AddCrewmember(pcm);
                target.SpawnCrew();
                GameEvents.onVesselWasModified.Fire(target);
                GameEvents.onVesselChange.Fire(target);
            }
            catch (Exception ex)
            {
                return CommandResult.Fail("Spawn failed: " + ex.Message);
            }
            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "message", "Spawned " + pcm.name + " into " + destPart.partInfo.title +
                             " on " + target.vesselName },
                { "crew", pcm.name },
            });
        }

        // POST /eva-flag {"crew"?: name} -> on the ACTIVE vessel (must be landed/splashed), put the
        // named (or first found) seated kerbal on EVA and plant the stock flag headlessly. MUST run on
        // the main thread (FlightEVA + KerbalEVA touch the live scene). The kerbal stays on EVA after
        // planting; the human / automation can board them back (the mission's flag-pause handles that).
        private CommandResult EvaPlantFlagCommand(Dictionary<string, string> fields)
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
            {
                return CommandResult.Fail("EVA flag requires an active vessel in flight.");
            }

            Vessel v = FlightGlobals.ActiveVessel;
            if (!v.LandedOrSplashed)
            {
                return CommandResult.Fail("Active vessel must be landed/splashed to plant a flag.");
            }

            // Find the first crewable part that actually holds a kerbal (optionally matched by name).
            string crewName = GetOptional(fields, "crew", "").Trim();
            Part evaPart = null;
            ProtoCrewMember pcm = null;
            if (v.parts != null)
            {
                foreach (Part part in v.parts)
                {
                    if (part == null || part.protoModuleCrew == null || part.protoModuleCrew.Count == 0)
                    {
                        continue;
                    }
                    foreach (ProtoCrewMember candidate in part.protoModuleCrew)
                    {
                        if (candidate == null)
                        {
                            continue;
                        }
                        if (crewName.Length == 0 ||
                            string.Equals(candidate.name, crewName, StringComparison.OrdinalIgnoreCase))
                        {
                            evaPart = part;
                            pcm = candidate;
                            break;
                        }
                    }
                    if (pcm != null)
                    {
                        break;
                    }
                }
            }
            if (pcm == null || evaPart == null)
            {
                return CommandResult.Fail(crewName.Length > 0
                    ? "Crew member not found in a crewed part: " + crewName
                    : "No seated crew member found on the active vessel to send on EVA.", 404);
            }

            // Put the kerbal on EVA. spawnEVA returns the KerbalEVA module (NOT a Vessel) in this build;
            // the 4-arg overload takes the airlock transform + a tryAllHatches fallback flag.
            KerbalEVA evaController;
            try
            {
                if (FlightEVA.fetch == null)
                {
                    return CommandResult.Fail("FlightEVA.fetch is null; cannot spawn EVA.");
                }
                evaController = FlightEVA.fetch.spawnEVA(pcm, evaPart, evaPart.airlock, true);
            }
            catch (Exception ex)
            {
                _lastError = ex.ToString();
                return CommandResult.Fail("spawnEVA threw: " + ex.Message);
            }
            if (evaController == null)
            {
                return CommandResult.Fail("spawnEVA returned null (no free hatch / airlock blocked?).");
            }

            Vessel evaVessel = evaController.vessel;
            string kerbalName = pcm.name;

            // Plant the flag. Preferred path: KerbalEVA.PlantFlag() is a public method in this build and
            // is the exact stock action. Fallback (robust across builds): search the part's BaseEvents for
            // the event whose name/guiName contains "flag" (case-insensitive) and Invoke() it. We try the
            // direct method first, then the Events search, and report which one fired so it can be verified
            // live. NOTE: only an in-game test confirms the flag actually appears — the stock plant is a
            // KFSM-driven animated action, so even a clean Invoke here may need the scene to settle a frame.
            string flagMethod = "";
            string flagDetail = "";
            bool planted = false;

            // 1) Direct public PlantFlag().
            try
            {
                evaController.PlantFlag();
                planted = true;
                flagMethod = "KerbalEVA.PlantFlag()";
            }
            catch (Exception ex)
            {
                flagDetail = "PlantFlag() threw: " + ex.Message;
            }

            // 2) Fallback: iterate the EVA module's BaseEvents for a "flag" event and invoke it.
            if (!planted)
            {
                try
                {
                    BaseEventList events = evaController.Events;
                    if (events != null)
                    {
                        foreach (BaseEvent ev in events)
                        {
                            if (ev == null)
                            {
                                continue;
                            }
                            string evName = ev.name ?? "";
                            string evGui = ev.guiName ?? "";
                            if (evName.IndexOf("flag", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                evGui.IndexOf("flag", StringComparison.OrdinalIgnoreCase) >= 0)
                            {
                                ev.Invoke();
                                planted = true;
                                flagMethod = "Events.Invoke";
                                flagDetail = "name='" + evName + "' guiName='" + evGui + "'";
                                break;
                            }
                        }
                        if (!planted)
                        {
                            // Surface what events WERE available so a live check can pick the right one.
                            StringBuilder avail = new StringBuilder();
                            foreach (BaseEvent ev in events)
                            {
                                if (ev == null) { continue; }
                                if (avail.Length > 0) { avail.Append("; "); }
                                avail.Append((ev.name ?? "") + "/" + (ev.guiName ?? ""));
                            }
                            flagDetail += " | no flag event found. available events: " + avail;
                        }
                    }
                    else
                    {
                        flagDetail += " | KerbalEVA.Events was null.";
                    }
                }
                catch (Exception ex)
                {
                    flagDetail += " | Events search threw: " + ex.Message;
                }
            }

            // Landed location for the report (biome + lat/lon of the EVA kerbal).
            string biome = "";
            double lat = 0.0;
            double lon = 0.0;
            try
            {
                if (evaVessel != null && evaVessel.mainBody != null)
                {
                    lat = evaVessel.latitude;
                    lon = evaVessel.longitude;
                    biome = ScienceUtil.GetExperimentBiome(evaVessel.mainBody, lat, lon) ?? "";
                }
            }
            catch (Exception)
            {
                // Biome lookup is best-effort; never fail the command on it.
            }

            AppendStatus("flag", (planted ? "Planted flag via " + flagMethod + " by " : "EVA OK but flag NOT confirmed for ") + kerbalName);

            Dictionary<string, object> data = new Dictionary<string, object>
            {
                { "message", planted
                    ? kerbalName + " went EVA and the flag-plant action fired (" + flagMethod + "). VERIFY the flag in-game."
                    : kerbalName + " went EVA but no flag-plant action could be invoked. See flagDetail." },
                { "crew", kerbalName },
                { "planted", planted },
                { "flagMethod", flagMethod },
                { "flagDetail", flagDetail },
                { "evaVessel", evaVessel != null ? evaVessel.vesselName : "" },
                { "body", evaVessel != null && evaVessel.mainBody != null ? evaVessel.mainBody.bodyName : "" },
                { "biome", biome },
                { "latitude", lat },
                { "longitude", lon }
            };
            return planted ? CommandResult.Ok(data) : CommandResult.Fail(kerbalName + " EVA succeeded but flag-plant could not be invoked. " + flagDetail);
        }

        // POST /eva-go {"crew"?: name, "vessel"?: name} -> put the named (or first found) seated kerbal
        // on EVA on a LANDED/SPLASHED vessel WITHOUT planting a flag. Returns the new EVA vessel name +
        // the kerbal's body/biome/lat/lon. The kerbal stays on EVA; call /eva-board to re-board. MUST run
        // on the main thread (FlightEVA touches the live scene).
        private CommandResult EvaGoCommand(Dictionary<string, string> fields)
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
            {
                return CommandResult.Fail("EVA requires an active vessel in flight.");
            }

            // Resolve the source vessel (optionally by name substring; default = active vessel).
            string vesselName = GetOptional(fields, "vessel", "").Trim();
            Vessel v = FlightGlobals.ActiveVessel;
            if (vesselName.Length > 0 && FlightGlobals.Vessels != null)
            {
                foreach (Vessel cand in FlightGlobals.Vessels)
                {
                    if (cand != null && cand.loaded && cand.vesselName != null &&
                        cand.vesselName.IndexOf(vesselName, StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        v = cand;
                        break;
                    }
                }
            }
            if (v == null)
            {
                return CommandResult.Fail("No vessel resolved for EVA.", 404);
            }
            if (v.isEVA)
            {
                return CommandResult.Fail("Target vessel is already an EVA kerbal.");
            }
            if (!v.LandedOrSplashed)
            {
                return CommandResult.Fail("Vessel must be landed/splashed to send a kerbal on EVA.");
            }

            // Find the first seated kerbal (optionally matched by name) and the part it sits in.
            string crewName = GetOptional(fields, "crew", "").Trim();
            Part evaPart = null;
            ProtoCrewMember pcm = null;
            if (v.parts != null)
            {
                foreach (Part part in v.parts)
                {
                    if (part == null || part.protoModuleCrew == null || part.protoModuleCrew.Count == 0)
                    {
                        continue;
                    }
                    foreach (ProtoCrewMember candidate in part.protoModuleCrew)
                    {
                        if (candidate == null)
                        {
                            continue;
                        }
                        if (crewName.Length == 0 ||
                            string.Equals(candidate.name, crewName, StringComparison.OrdinalIgnoreCase))
                        {
                            evaPart = part;
                            pcm = candidate;
                            break;
                        }
                    }
                    if (pcm != null)
                    {
                        break;
                    }
                }
            }
            if (pcm == null || evaPart == null)
            {
                return CommandResult.Fail(crewName.Length > 0
                    ? "Crew member not found in a crewed part: " + crewName
                    : "No seated crew member found to send on EVA.", 404);
            }

            KerbalEVA evaController;
            try
            {
                if (FlightEVA.fetch == null)
                {
                    return CommandResult.Fail("FlightEVA.fetch is null; cannot spawn EVA.");
                }
                evaController = FlightEVA.fetch.spawnEVA(pcm, evaPart, evaPart.airlock, true);
            }
            catch (Exception ex)
            {
                _lastError = ex.ToString();
                return CommandResult.Fail("spawnEVA threw: " + ex.Message);
            }
            if (evaController == null)
            {
                return CommandResult.Fail("spawnEVA returned null (no free hatch / airlock blocked?).");
            }

            Vessel evaVessel = evaController.vessel;
            string kerbalName = pcm.name;

            string biome = "";
            double lat = 0.0;
            double lon = 0.0;
            try
            {
                if (evaVessel != null && evaVessel.mainBody != null)
                {
                    lat = evaVessel.latitude;
                    lon = evaVessel.longitude;
                    biome = ScienceUtil.GetExperimentBiome(evaVessel.mainBody, lat, lon) ?? "";
                }
            }
            catch (Exception)
            {
                // Biome lookup is best-effort.
            }

            AppendStatus("eva", "EVA " + kerbalName + " from " + (evaPart.partInfo != null ? evaPart.partInfo.title : evaPart.name));
            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "message", kerbalName + " is now on EVA." },
                { "crew", kerbalName },
                { "evaVessel", evaVessel != null ? evaVessel.vesselName : "" },
                { "fromVessel", v.vesselName },
                { "body", evaVessel != null && evaVessel.mainBody != null ? evaVessel.mainBody.bodyName : "" },
                { "biome", biome },
                { "latitude", lat },
                { "longitude", lon }
            });
        }

        // POST /eva-board {"crew"?: name} -> re-board the active (or named) EVA kerbal into the nearest
        // crewable part with a free seat. "Nearest" = smallest world-space distance from the EVA kerbal to
        // any candidate part across the loaded vessels. Uses KerbalEVA.BoardPart(Part). MUST run on the
        // main thread.
        private CommandResult EvaBoardCommand(Dictionary<string, string> fields)
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
            {
                return CommandResult.Fail("EVA board requires an active vessel in flight.");
            }

            string crewName = GetOptional(fields, "crew", "").Trim();

            // Resolve the EVA kerbal: prefer the active vessel if it IS an EVA; else search loaded vessels
            // for an EVA whose crew name matches (or the first EVA if no name given).
            Vessel evaVessel = null;
            if (FlightGlobals.ActiveVessel.isEVA && EvaCrewNameMatches(FlightGlobals.ActiveVessel, crewName))
            {
                evaVessel = FlightGlobals.ActiveVessel;
            }
            if (evaVessel == null && FlightGlobals.Vessels != null)
            {
                foreach (Vessel cand in FlightGlobals.Vessels)
                {
                    if (cand != null && cand.loaded && cand.isEVA && EvaCrewNameMatches(cand, crewName))
                    {
                        evaVessel = cand;
                        break;
                    }
                }
            }
            if (evaVessel == null || evaVessel.evaController == null)
            {
                return CommandResult.Fail(crewName.Length > 0
                    ? "No EVA kerbal named '" + crewName + "' found to board."
                    : "No EVA kerbal found to board.", 404);
            }

            KerbalEVA eva = evaVessel.evaController;
            string kerbalName = evaVessel.vesselName;

            // Find the nearest crewable part with a free seat across all loaded vessels (excluding the EVA
            // vessel itself). Distance = world-space part position to the EVA kerbal.
            Vector3 evaPos = evaVessel.GetWorldPos3D();
            Part target = null;
            double best = double.MaxValue;
            if (FlightGlobals.Vessels != null)
            {
                foreach (Vessel v in FlightGlobals.Vessels)
                {
                    if (v == null || !v.loaded || v == evaVessel || v.isEVA || v.parts == null)
                    {
                        continue;
                    }
                    foreach (Part part in v.parts)
                    {
                        if (part == null || part.CrewCapacity <= 0 || part.protoModuleCrew == null)
                        {
                            continue;
                        }
                        if (part.protoModuleCrew.Count >= part.CrewCapacity)
                        {
                            continue;
                        }
                        double d = (part.transform.position - evaPos).magnitude;
                        if (d < best)
                        {
                            best = d;
                            target = part;
                        }
                    }
                }
            }
            if (target == null)
            {
                return CommandResult.Fail("No crewable part with a free seat found to board into.", 404);
            }

            string toTitle = target.partInfo != null ? target.partInfo.title : target.name;
            string toVessel = target.vessel != null ? target.vessel.vesselName : "";
            try
            {
                eva.BoardPart(target);
            }
            catch (Exception ex)
            {
                _lastError = ex.ToString();
                return CommandResult.Fail("BoardPart threw: " + ex.Message);
            }

            AppendStatus("eva", "Boarded " + kerbalName + " -> " + toTitle);
            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "message", kerbalName + " boarded " + toTitle + " on " + toVessel + "." },
                { "crew", kerbalName },
                { "toPart", toTitle },
                { "toVessel", toVessel },
                { "distanceM", best == double.MaxValue ? 0.0 : best }
            });
        }

        // True if the EVA vessel's kerbal name matches the (optional) needle. Empty needle matches any.
        private static bool EvaCrewNameMatches(Vessel evaVessel, string needle)
        {
            if (needle.Length == 0)
            {
                return true;
            }
            if (evaVessel == null)
            {
                return false;
            }
            // The EVA vessel name is the kerbal's name; the seated proto-crew also carries the name.
            if (evaVessel.vesselName != null &&
                evaVessel.vesselName.IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0)
            {
                return true;
            }
            List<ProtoCrewMember> crew = evaVessel.GetVesselCrew();
            if (crew != null)
            {
                foreach (ProtoCrewMember c in crew)
                {
                    if (c != null && c.name != null &&
                        string.Equals(c.name, needle, StringComparison.OrdinalIgnoreCase))
                    {
                        return true;
                    }
                }
            }
            return false;
        }

        // POST /transfer-crew  -> move a kerbal between crewed parts (after docking, both craft are
        // one vessel) or between two separate-but-docked vessels addressed by name.
        // MUST be invoked on the main thread (called via RunOnMainThread).
        private CommandResult TransferCrewCommand(Dictionary<string, string> fields)
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
            {
                return CommandResult.Fail("Crew transfer requires an active vessel in flight.");
            }

            string crewName = GetOptional(fields, "crew", "").Trim();
            string fromPartName = GetOptional(fields, "fromPart", "").Trim();
            string toPartName = GetOptional(fields, "toPart", "").Trim();
            string toVesselName = GetOptional(fields, "toVessel", "").Trim();

            // Resolve the kerbal to move and the part it currently sits in.
            ProtoCrewMember pcm = null;
            Part sourcePart = null;
            Vessel sourceVessel = null;

            // Search every loaded vessel for the named crew member (or the first crew member if
            // only fromPart/no name was supplied).
            List<Vessel> searchVessels = new List<Vessel>();
            if (FlightGlobals.Vessels != null)
            {
                foreach (Vessel v in FlightGlobals.Vessels)
                {
                    if (v != null && v.loaded)
                    {
                        searchVessels.Add(v);
                    }
                }
            }
            if (searchVessels.Count == 0)
            {
                searchVessels.Add(FlightGlobals.ActiveVessel);
            }

            foreach (Vessel v in searchVessels)
            {
                if (v == null || v.parts == null)
                {
                    continue;
                }

                foreach (Part part in v.parts)
                {
                    if (part == null || part.protoModuleCrew == null || part.protoModuleCrew.Count == 0)
                    {
                        continue;
                    }

                    // If a specific source part was named, restrict to it.
                    if (fromPartName.Length > 0 && !PartMatches(part, fromPartName))
                    {
                        continue;
                    }

                    foreach (ProtoCrewMember candidate in part.protoModuleCrew)
                    {
                        if (candidate == null)
                        {
                            continue;
                        }

                        if (crewName.Length == 0 ||
                            string.Equals(candidate.name, crewName, StringComparison.OrdinalIgnoreCase))
                        {
                            pcm = candidate;
                            sourcePart = part;
                            sourceVessel = v;
                            break;
                        }
                    }

                    if (pcm != null)
                    {
                        break;
                    }
                }

                if (pcm != null)
                {
                    break;
                }
            }

            if (pcm == null || sourcePart == null)
            {
                return CommandResult.Fail(crewName.Length > 0
                    ? "Crew member not found in a crewed part: " + crewName
                    : "No crew member found to transfer (no crewed parts matched).", 404);
            }

            // Resolve the destination part. Priority: explicit toPart name; else first part with a
            // free seat on the named toVessel; else first part with a free seat on the active vessel
            // that is not the source part.
            Part destPart = null;

            if (toPartName.Length > 0)
            {
                destPart = FindPartByName(searchVessels, toPartName, sourcePart);
                if (destPart == null)
                {
                    return CommandResult.Fail("Destination part not found: " + toPartName, 404);
                }
            }
            else if (toVesselName.Length > 0)
            {
                Vessel destVessel = null;
                foreach (Vessel v in searchVessels)
                {
                    if (v != null && string.Equals(v.vesselName, toVesselName, StringComparison.OrdinalIgnoreCase))
                    {
                        destVessel = v;
                        break;
                    }
                }
                if (destVessel == null)
                {
                    return CommandResult.Fail("Destination vessel not found: " + toVesselName, 404);
                }
                destPart = FindFirstSeat(destVessel, sourcePart);
                if (destPart == null)
                {
                    return CommandResult.Fail("No free crew seat on destination vessel: " + toVesselName);
                }
            }
            else
            {
                // Default: any free seat on the active vessel that is not the source part.
                destPart = FindFirstSeat(FlightGlobals.ActiveVessel, sourcePart);
                if (destPart == null)
                {
                    return CommandResult.Fail("No destination part/vessel given and no free seat found on the active vessel.");
                }
            }

            if (destPart == sourcePart)
            {
                return CommandResult.Fail("Source and destination part are the same; nothing to do.");
            }

            // Capacity check on destination.
            if (destPart.protoModuleCrew != null && destPart.CrewCapacity > 0 &&
                destPart.protoModuleCrew.Count >= destPart.CrewCapacity)
            {
                return CommandResult.Fail("Destination part is full: " +
                    destPart.partInfo.title + " (" + destPart.protoModuleCrew.Count + "/" + destPart.CrewCapacity + ").");
            }
            if (destPart.CrewCapacity <= 0)
            {
                return CommandResult.Fail("Destination part has no crew capacity: " + destPart.partInfo.title + ".");
            }

            Vessel destVesselFinal = destPart.vessel;
            string movedName = pcm.name;
            string fromTitle = sourcePart.partInfo != null ? sourcePart.partInfo.title : sourcePart.name;
            string toTitle = destPart.partInfo != null ? destPart.partInfo.title : destPart.name;

            try
            {
                // Core move. seatIdx = -1 lets SpawnCrew pick a free seat in the destination part.
                sourcePart.RemoveCrewmember(pcm);
                pcm.seatIdx = -1;
                destPart.AddCrewmember(pcm);

                // Refresh internal seats/portraits on both ends.
                sourcePart.vessel.SpawnCrew();
                if (destVesselFinal != null && destVesselFinal != sourcePart.vessel)
                {
                    destVesselFinal.SpawnCrew();
                }

                // Tell the game the vessels changed so UI / portraits / map update.
                GameEvents.onVesselWasModified.Fire(sourcePart.vessel);
                if (destVesselFinal != null && destVesselFinal != sourcePart.vessel)
                {
                    GameEvents.onVesselWasModified.Fire(destVesselFinal);
                }
                if (FlightGlobals.ActiveVessel != null)
                {
                    GameEvents.onVesselChange.Fire(FlightGlobals.ActiveVessel);
                }
            }
            catch (Exception ex)
            {
                _lastError = ex.ToString();
                return CommandResult.Fail("Crew transfer failed mid-move: " + ex.Message);
            }

            AppendStatus("crew", "Transferred " + movedName + " -> " + toTitle);
            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "message", "Crew transferred." },
                { "crew", movedName },
                { "fromPart", fromTitle },
                { "toPart", toTitle },
                { "fromVessel", sourceVessel != null ? sourceVessel.vesselName : "" },
                { "toVessel", destVesselFinal != null ? destVesselFinal.vesselName : "" }
            });
        }

        private static bool PartMatches(Part part, string needle)
        {
            if (part == null)
            {
                return false;
            }

            // Match by craft-tag name, prefab name, or part title (case-insensitive).
            string title = part.partInfo != null ? part.partInfo.title : "";
            return string.Equals(part.name, needle, StringComparison.OrdinalIgnoreCase) ||
                   (title.Length > 0 && string.Equals(title, needle, StringComparison.OrdinalIgnoreCase)) ||
                   (title.Length > 0 && title.IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0);
        }

        private static Part FindPartByName(List<Vessel> vessels, string needle, Part exclude)
        {
            foreach (Vessel v in vessels)
            {
                if (v == null || v.parts == null)
                {
                    continue;
                }
                foreach (Part part in v.parts)
                {
                    if (part == null || part == exclude || part.CrewCapacity <= 0)
                    {
                        continue;
                    }
                    if (PartMatches(part, needle))
                    {
                        return part;
                    }
                }
            }
            return null;
        }

        private static Part FindFirstSeat(Vessel vessel, Part exclude)
        {
            if (vessel == null || vessel.parts == null)
            {
                return null;
            }
            foreach (Part part in vessel.parts)
            {
                if (part == null || part == exclude || part.CrewCapacity <= 0)
                {
                    continue;
                }
                int occupied = part.protoModuleCrew != null ? part.protoModuleCrew.Count : 0;
                if (occupied < part.CrewCapacity)
                {
                    return part;
                }
            }
            return null;
        }

        private static string JsonEscape(string value)
        {
            return (value ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n");
        }

        private static string ResolveCraftPath(string craftName, string requestedPath, EditorFacility facility)
        {
            string folder = facility == EditorFacility.SPH ? "SPH" : "VAB";
            string baseDir = Path.GetFullPath(Path.Combine(KSPUtil.ApplicationRootPath, "saves", HighLogic.SaveFolder, "Ships", folder));
            string baseWithSeparator = baseDir.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar;
            string candidate = string.IsNullOrEmpty(requestedPath)
                ? Path.Combine(baseDir, craftName + ".craft")
                : requestedPath;

            if (!Path.IsPathRooted(candidate))
            {
                candidate = Path.Combine(baseDir, candidate);
            }

            string full = Path.GetFullPath(candidate);
            if (!full.StartsWith(baseWithSeparator, StringComparison.OrdinalIgnoreCase))
            {
                throw new ArgumentException("Craft path escapes the selected save Ships/" + folder + " directory.");
            }

            if (!full.EndsWith(".craft", StringComparison.OrdinalIgnoreCase))
            {
                throw new ArgumentException("Craft path must end with .craft.");
            }

            return full;
        }

        private static void ValidateCraftName(string craftName)
        {
            if (string.IsNullOrEmpty(craftName) || !CraftNameRegex.IsMatch(craftName) || craftName.Contains(".."))
            {
                throw new ArgumentException("Invalid craftName. Use 1-80 safe filename characters.");
            }
        }

        private static void ValidateSaveFolder(string saveFolder)
        {
            if (string.IsNullOrEmpty(saveFolder) || saveFolder.Contains("..") || saveFolder.Contains("/") || saveFolder.Contains("\\"))
            {
                throw new ArgumentException("Invalid saveFolder. Use an existing save folder name, not a path.");
            }

            string savesRoot = Path.GetFullPath(Path.Combine(KSPUtil.ApplicationRootPath, "saves"));
            string baseWithSeparator = savesRoot.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar;
            string full = Path.GetFullPath(Path.Combine(savesRoot, saveFolder));
            if (!full.StartsWith(baseWithSeparator, StringComparison.OrdinalIgnoreCase))
            {
                throw new ArgumentException("Save folder escapes the KSP saves directory.");
            }
        }

        private static EditorFacility ParseFacility(string value)
        {
            return string.Equals(value, "SPH", StringComparison.OrdinalIgnoreCase) ? EditorFacility.SPH : EditorFacility.VAB;
        }

        private static string GetRequired(Dictionary<string, string> fields, string key)
        {
            string value;
            if (!fields.TryGetValue(key, out value) || string.IsNullOrEmpty(value))
            {
                throw new ArgumentException("Missing required field: " + key);
            }
            return value;
        }

        // ================= MechJeb integration =================
        // We drive MechJeb 2's own Rendezvous + Docking autopilots (compiled against the installed
        // MechJeb2.dll) rather than reinventing guidance. MechJeb must be present on the craft: ship
        // the "MechJeb for all command pods" ModuleManager patch (GameData) so GetMasterMechJeb() is
        // non-null. Member casing is for the installed dev build (Users PascalCase; lowercase params).

        private static MechJebCore GetMasterCore(Vessel vessel)
        {
            return vessel == null ? null : vessel.GetMasterMechJeb();
        }

        private static Vessel FindVesselByName(string name)
        {
            if (FlightGlobals.Vessels == null || string.IsNullOrEmpty(name))
            {
                return null;
            }
            foreach (Vessel v in FlightGlobals.Vessels)
            {
                if (v != null && string.Equals(v.vesselName, name, StringComparison.OrdinalIgnoreCase))
                {
                    return v;
                }
            }
            return null;
        }

        private static ModuleDockingNode FindDockingPort(Vessel vessel, bool freeOnly)
        {
            if (vessel == null || vessel.parts == null)
            {
                return null;
            }
            foreach (Part p in vessel.parts)
            {
                ModuleDockingNode node = p.FindModuleImplementing<ModuleDockingNode>();
                if (node == null)
                {
                    continue;
                }
                if (freeOnly && node.state != null && node.state.IndexOf("Docked", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    continue;
                }
                return node;
            }
            return null;
        }

        private static bool GetOptionalBool(Dictionary<string, string> fields, string key, bool fallback)
        {
            string s;
            if (fields.TryGetValue(key, out s))
            {
                return string.Equals(s, "true", StringComparison.OrdinalIgnoreCase) || s == "1";
            }
            return fallback;
        }

        private static double GetOptionalDouble(Dictionary<string, string> fields, string key, double fallback)
        {
            string s;
            double d;
            if (fields.TryGetValue(key, out s) && double.TryParse(s, NumberStyles.Any, CultureInfo.InvariantCulture, out d))
            {
                return d;
            }
            return fallback;
        }

        private CommandResult MjRendezvousCommand(Dictionary<string, string> fields)
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
            {
                return CommandResult.Fail("MechJeb rendezvous requires an active vessel in flight.");
            }
            Vessel vessel = FlightGlobals.ActiveVessel;
            MechJebCore core = GetMasterCore(vessel);
            if (core == null)
            {
                return CommandResult.Fail("No MechJeb core on the active vessel (install the MechJeb-for-all patch and reload).");
            }

            string targetName = GetOptional(fields, "target", "").Trim();
            if (targetName.Length > 0)
            {
                Vessel tv = FindVesselByName(targetName);
                if (tv == null)
                {
                    return CommandResult.Fail("Target vessel not found: " + targetName);
                }
                FlightGlobals.fetch.SetVesselTarget(tv);
            }
            if (core.Target == null || !core.Target.NormalTargetExists)
            {
                return CommandResult.Fail("No valid target set for rendezvous.");
            }

            MechJebModuleRendezvousAutopilot ap = core.GetComputerModule<MechJebModuleRendezvousAutopilot>();
            if (ap == null)
            {
                return CommandResult.Fail("MechJebModuleRendezvousAutopilot not present in this MechJeb build.");
            }
            ap.desiredDistance.Val = GetOptionalDouble(fields, "desiredDistance", 100.0);
            ap.maxPhasingOrbits.Val = GetOptionalDouble(fields, "maxPhasingOrbits", 5.0);
            ap.maxClosingSpeed.Val = GetOptionalDouble(fields, "maxClosingSpeed", 100.0);

            vessel.ActionGroups.SetGroup(KSPActionGroup.RCS, true);
            ap.Users.Add(core);

            Dictionary<string, object> data = new Dictionary<string, object>
            {
                { "enabled", true },
                { "target", targetName },
                { "status", ap.status ?? "" }
            };
            return CommandResult.Ok(data);
        }

        private CommandResult MjDockCommand(Dictionary<string, string> fields)
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
            {
                return CommandResult.Fail("MechJeb dock requires an active vessel in flight.");
            }
            Vessel vessel = FlightGlobals.ActiveVessel;
            MechJebCore core = GetMasterCore(vessel);
            if (core == null)
            {
                return CommandResult.Fail("No MechJeb core on the active vessel (install the MechJeb-for-all patch and reload).");
            }

            // Resolve and set the TARGET docking port (a ModuleDockingNode gives MechJeb the dock axis).
            string targetName = GetOptional(fields, "target", "").Trim();
            ModuleDockingNode targetPort = null;
            if (targetName.Length > 0)
            {
                Vessel tv = FindVesselByName(targetName);
                if (tv == null)
                {
                    return CommandResult.Fail("Target vessel not found: " + targetName);
                }
                targetPort = FindDockingPort(tv, true);
                if (targetPort == null)
                {
                    return CommandResult.Fail("No free docking port on target: " + targetName);
                }
                FlightGlobals.fetch.SetVesselTarget(targetPort);
            }
            else
            {
                ITargetable cur = FlightGlobals.fetch.VesselTarget;
                if (cur is ModuleDockingNode)
                {
                    targetPort = (ModuleDockingNode)cur;
                }
                else if (cur != null && cur.GetVessel() != null)
                {
                    targetPort = FindDockingPort(cur.GetVessel(), true);
                    if (targetPort != null)
                    {
                        FlightGlobals.fetch.SetVesselTarget(targetPort);
                    }
                }
            }

            // Set OUR control reference to the docking port we dock WITH (correct relative geometry).
            ModuleDockingNode myPort = FindDockingPort(vessel, true);
            if (myPort != null)
            {
                myPort.MakeReferenceTransform();
                vessel.SetReferenceTransform(myPort.part);
            }

            MechJebModuleDockingAutopilot dock = core.GetComputerModule<MechJebModuleDockingAutopilot>();
            if (dock == null)
            {
                return CommandResult.Fail("MechJebModuleDockingAutopilot not present in this MechJeb build.");
            }
            dock.speedLimit = GetOptionalDouble(fields, "speedLimit", 1.0);
            dock.forceRol = GetOptionalBool(fields, "forceRol", false);
            dock.overrideSafeDistance = GetOptionalBool(fields, "overrideSafeDistance", false);
            dock.overrideTargetSize = false;
            dock.drawBoundingBox = false;

            if (core.Target == null || !core.Target.NormalTargetExists)
            {
                return CommandResult.Fail("No valid target docking port set; aborting before enable.");
            }
            vessel.ActionGroups.SetGroup(KSPActionGroup.RCS, true);
            dock.Users.Add(core);

            Dictionary<string, object> data = new Dictionary<string, object>
            {
                { "enabled", true },
                { "target", targetName },
                { "chaserPartCount", vessel.parts.Count },
                { "status", dock.status ?? "" }
            };
            return CommandResult.Ok(data);
        }

        private CommandResult MjDisableCommand(Dictionary<string, string> fields)
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
            {
                return CommandResult.Fail("Requires an active vessel in flight.");
            }
            MechJebCore core = GetMasterCore(FlightGlobals.ActiveVessel);
            if (core == null)
            {
                return CommandResult.Fail("No MechJeb core on the active vessel.");
            }
            string which = GetOptional(fields, "which", "all").Trim().ToLowerInvariant();
            if (which == "dock" || which == "all")
            {
                MechJebModuleDockingAutopilot m = core.GetComputerModule<MechJebModuleDockingAutopilot>();
                if (m != null) { m.Users.Remove(core); }
            }
            if (which == "rendezvous" || which == "all")
            {
                MechJebModuleRendezvousAutopilot m = core.GetComputerModule<MechJebModuleRendezvousAutopilot>();
                if (m != null) { m.Users.Remove(core); }
            }
            // STAGING: turn OFF MechJeb's autostager (MechJebModuleStagingController, a.k.a. core.staging). It
            // is a SEPARATE module from the ascent AP's settings.Autostage flag, and once enabled it autostages
            // during ANY burn — including the in-space node-executor capture burn. On a crewed/heat-shield tug
            // it blindly fired the payload/heat-shield decoupler mid-capture and stranded the crew pod (no
            // engine, heat shield, or chutes). Disabling it here makes the Python explicit-guarded loop the
            // SOLE stager, so no decoupler ever fires in space. (Drop our Users token — the SAME disable
            // mechanism the dock/rendezvous branches above use — so the autostager is released regardless of
            // how it was switched on.)
            if (which == "staging" || which == "all")
            {
                MechJebModuleStagingController stg = core.GetComputerModule<MechJebModuleStagingController>();
                if (stg != null) { stg.Users.Remove(core); }
            }
            return CommandResult.Ok(new Dictionary<string, object> { { "disabled", which } });
        }

        private CommandResult MjStatusCommand()
        {
            Dictionary<string, object> data = new Dictionary<string, object>();
            bool flight = HighLogic.LoadedSceneIsFlight && FlightGlobals.ActiveVessel != null;
            data["flight"] = flight;
            if (!flight)
            {
                return CommandResult.Ok(data);
            }
            Vessel vessel = FlightGlobals.ActiveVessel;
            data["activeVessel"] = vessel.vesselName ?? "";
            data["partCount"] = vessel.parts == null ? 0 : vessel.parts.Count;

            MechJebCore core = GetMasterCore(vessel);
            data["hasCore"] = core != null;
            if (core == null)
            {
                return CommandResult.Ok(data);
            }
            data["targetExists"] = core.Target != null && core.Target.NormalTargetExists;

            MechJebModuleDockingAutopilot dock = core.GetComputerModule<MechJebModuleDockingAutopilot>();
            if (dock != null)
            {
                data["dockEnabled"] = dock.Users.Count > 0;
                data["dockStatus"] = dock.status ?? "";
            }
            MechJebModuleRendezvousAutopilot rv = core.GetComputerModule<MechJebModuleRendezvousAutopilot>();
            if (rv != null)
            {
                data["rvEnabled"] = rv.Users.Count > 0;
                data["rvStatus"] = rv.status ?? "";
            }

            ModuleDockingNode myPort = FindDockingPort(vessel, false);
            data["myPortState"] = myPort != null && myPort.state != null ? myPort.state : "";

            // Ascent / landing / node-executor progress (for the mission driver to poll on).
            MechJebModuleAscentClassicAutopilot asc = core.GetComputerModule<MechJebModuleAscentClassicAutopilot>();
            if (asc != null) { data["ascentEnabled"] = asc.Users.Count > 0; }
            MechJebModuleLandingAutopilot land = core.GetComputerModule<MechJebModuleLandingAutopilot>();
            if (land != null) { data["landingEnabled"] = land.Users.Count > 0; }
            MechJebModuleNodeExecutor nex = core.GetComputerModule<MechJebModuleNodeExecutor>();
            if (nex != null) { data["nodeExecEnabled"] = nex.Users.Count > 0; }
            data["nodeCount"] = vessel.patchedConicSolver != null ? vessel.patchedConicSolver.maneuverNodes.Count : 0;
            data["situation"] = vessel.situation.ToString();
            data["apoapsis"] = vessel.orbit != null ? vessel.orbit.ApA : 0.0;
            data["periapsis"] = vessel.orbit != null ? vessel.orbit.PeA : 0.0;
            data["body"] = vessel.orbit != null && vessel.orbit.referenceBody != null ? vessel.orbit.referenceBody.bodyName : "";
            return CommandResult.Ok(data);
        }

        private CommandResult MjStageStatsCommand()
        {
            Dictionary<string, object> data = new Dictionary<string, object>();
            bool flight = HighLogic.LoadedSceneIsFlight && FlightGlobals.ActiveVessel != null;
            data["flight"] = flight;
            if (!flight)
            {
                return CommandResult.Ok(data);
            }

            Vessel vessel = FlightGlobals.ActiveVessel;
            data["activeVessel"] = vessel.vesselName ?? "";
            MechJebCore core = GetMasterCore(vessel);
            data["hasCore"] = core != null;
            if (core == null)
            {
                return CommandResult.Ok(data);
            }

            MechJebModuleStageStats stats = core.GetComputerModule<MechJebModuleStageStats>();
            data["hasStageStats"] = stats != null;
            if (stats == null)
            {
                return CommandResult.Ok(data);
            }

            stats.RequestUpdate();
            double geeASL = vessel.mainBody != null ? vessel.mainBody.GeeASL : 1.0;
            data["pending"] = stats.VacStats.Count == 0 && stats.AtmoStats.Count == 0;
            data["vacStageCount"] = stats.VacStats.Count;
            data["atmoStageCount"] = stats.AtmoStats.Count;
            data["vacTotalDeltaV"] = FuelStatsDeltaV(stats.VacStats);
            data["atmoTotalDeltaV"] = FuelStatsDeltaV(stats.AtmoStats);
            data["vacTotalBurnTime"] = FuelStatsBurnTime(stats.VacStats);
            data["atmoTotalBurnTime"] = FuelStatsBurnTime(stats.AtmoStats);
            data["vacStats"] = new RawJson(FuelStatsListJson(stats.VacStats, geeASL));
            data["atmoStats"] = new RawJson(FuelStatsListJson(stats.AtmoStats, geeASL));
            return CommandResult.Ok(data);
        }

        private static double FuelStatsDeltaV(List<FuelStats> stats)
        {
            double total = 0.0;
            for (int i = 0; i < stats.Count; i++)
            {
                total += stats[i].DeltaV;
            }
            return total;
        }

        private static double FuelStatsBurnTime(List<FuelStats> stats)
        {
            double total = 0.0;
            for (int i = 0; i < stats.Count; i++)
            {
                total += stats[i].DeltaTime;
            }
            return total;
        }

        private static string FuelStatsListJson(List<FuelStats> stats, double geeASL)
        {
            StringBuilder sb = new StringBuilder();
            sb.Append("[");
            for (int i = 0; i < stats.Count; i++)
            {
                if (i > 0)
                {
                    sb.Append(",");
                }
                AppendFuelStatsJson(sb, stats[i], i, geeASL);
            }
            sb.Append("]");
            return sb.ToString();
        }

        private static void AppendFuelStatsJson(StringBuilder sb, FuelStats stage, int index, double geeASL)
        {
            sb.Append("{");
            AppendJsonNumber(sb, "index", index, false);
            AppendJsonNumber(sb, "kspStage", stage.KSPStage, true);
            AppendJsonNumber(sb, "deltaV", stage.DeltaV, true);
            AppendJsonNumber(sb, "burnTime", stage.DeltaTime, true);
            AppendJsonNumber(sb, "startMass", stage.StartMass, true);
            AppendJsonNumber(sb, "endMass", stage.EndMass, true);
            AppendJsonNumber(sb, "stagedMass", stage.StagedMass, true);
            AppendJsonNumber(sb, "resourceMass", stage.ResourceMass, true);
            AppendJsonNumber(sb, "thrust", stage.Thrust, true);
            AppendJsonNumber(sb, "isp", stage.Isp, true);
            AppendJsonNumber(sb, "maxAccel", stage.MaxAccel, true);
            AppendJsonNumber(sb, "startTwr", stage.StartTWR(geeASL), true);
            AppendJsonNumber(sb, "maxTwr", stage.MaxTWR(geeASL), true);
            sb.Append("}");
        }

        private static void AppendJsonNumber(StringBuilder sb, string name, double value, bool leadingComma)
        {
            if (leadingComma)
            {
                sb.Append(",");
            }
            sb.Append("\"").Append(name).Append("\":");
            if (double.IsNaN(value) || double.IsInfinity(value))
            {
                sb.Append("null");
            }
            else
            {
                sb.Append(Convert.ToString(value, CultureInfo.InvariantCulture));
            }
        }

        private CommandResult MjAscentCommand(Dictionary<string, string> fields)
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
                return CommandResult.Fail("MechJeb ascent requires an active vessel in flight.");
            Vessel vessel = FlightGlobals.ActiveVessel;
            MechJebCore core = GetMasterCore(vessel);
            if (core == null)
                return CommandResult.Fail("No MechJeb core on the active vessel.");
            MechJebModuleAscentSettings settings = core.GetComputerModule<MechJebModuleAscentSettings>();
            if (settings == null)
                return CommandResult.Fail("MechJebModuleAscentSettings not present.");
            settings.DesiredOrbitAltitude.Val = GetOptionalDouble(fields, "altitude", 100000.0);
            settings.DesiredInclination.Val = GetOptionalDouble(fields, "inclination", 0.0);
            // Autostage control: MechJebModuleAscentSettings.Autostage is a plain bool property (verified
            // against MechJeb2 2.15.0.0). The ascent AP's OnModuleEnabled reads it and only registers with
            // Core.Staging (MechJebModuleStagingController) when true. Setting it false BEFORE enabling the
            // AP means MechJeb does NOT autostage, so the Python explicit-decouple loop is the SOLE stager
            // — eliminating the two-stagers race that intermittently mis-detected the relay's fin geometry
            // ("no separation") and could drop the booster early on a tank-crossfeed transient.
            bool autostage = GetOptionalBool(fields, "autostage", true);
            settings.Autostage = autostage;
            // Classic ascent path = stock-friendly; PVG is for RSS/RO.
            MechJebModuleAscentClassicAutopilot ap = core.GetComputerModule<MechJebModuleAscentClassicAutopilot>();
            if (ap == null)
                return CommandResult.Fail("Classic ascent autopilot not present.");
            ap.Users.Add(core);   // enables the AP -> OnModuleEnabled runs HERE, reading the Autostage set above
            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "ascent", true },
                { "altitude", GetOptionalDouble(fields, "altitude", 100000.0) },
                { "inclination", GetOptionalDouble(fields, "inclination", 0.0) },
                { "autostage", autostage }
            });
        }

        private CommandResult MjExecuteNodeCommand(Dictionary<string, string> fields)
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
                return CommandResult.Fail("Requires an active vessel in flight.");
            Vessel vessel = FlightGlobals.ActiveVessel;
            MechJebCore core = GetMasterCore(vessel);
            if (core == null)
                return CommandResult.Fail("No MechJeb core on the active vessel.");
            if (vessel.patchedConicSolver == null || vessel.patchedConicSolver.maneuverNodes.Count == 0)
                return CommandResult.Fail("No maneuver node to execute (create one via kRPC first).");
            MechJebModuleNodeExecutor ex = core.GetComputerModule<MechJebModuleNodeExecutor>();
            if (ex == null)
                return CommandResult.Fail("MechJebModuleNodeExecutor not present.");
            ex.Autowarp = GetOptionalBool(fields, "autowarp", true);
            if (GetOptionalBool(fields, "all", false))
                ex.ExecuteAllNodes(core);
            else
                ex.ExecuteOneNode(core);
            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "executing", true },
                { "nodes", vessel.patchedConicSolver.maneuverNodes.Count }
            });
        }

        // Plan a maneuver node with MechJeb's maneuver planner (the interplanetary transfer computes
        // the precise ejection ANGLE + timing a hand-rolled prograde burn can't — that is the blocker
        // to reaching Duna orbit). The kRPC side then fires /mj-execute-node to fly it.
        private CommandResult MjPlanCommand(Dictionary<string, string> fields)
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
                return CommandResult.Fail("Requires an active vessel in flight.");
            Vessel vessel = FlightGlobals.ActiveVessel;
            MechJebCore core = GetMasterCore(vessel);
            if (core == null)
                return CommandResult.Fail("No MechJeb core on the active vessel.");

            string targetName = GetOptional(fields, "target", "");
            if (targetName.Length > 0)
            {
                CelestialBody body = null;
                foreach (CelestialBody b in FlightGlobals.Bodies)
                {
                    if (string.Equals(b.bodyName, targetName, StringComparison.OrdinalIgnoreCase)) { body = b; break; }
                }
                if (body == null)
                    return CommandResult.Fail("Target body not found: " + targetName);
                // Set MechJeb's target controller DIRECTLY. FlightGlobals.SetVesselTarget only updates
                // vessel.targetObject; MechJeb's core.Target syncs from it on its next OnFixedUpdate, so
                // checking NormalTargetExists in the same frame reads stale null. core.Target.Set both
                // updates MechJeb and sets the vessel target, so the planner sees Duna immediately.
                FlightGlobals.fetch.SetVesselTarget(body);
                core.Target.Set(body);
            }
            if (core.Target == null || core.Target.Target == null)
                return CommandResult.Fail("No target set (pass target=Duna).");

            string operation = GetOptional(fields, "operation", "interplanetary").ToLowerInvariant();
            Operation op;
            if (operation == "interplanetary")
                op = new OperationInterplanetaryTransfer();
            else if (operation == "circularize")
                op = new OperationCircularize();
            else if (operation == "plane")
                op = new OperationPlane();
            else if (operation == "correction")
                op = new OperationCourseCorrection();  // mid-course fine-tune of closest approach to the target
            else
                return CommandResult.Fail("Unknown operation '" + operation + "' (interplanetary|circularize|plane|correction).");

            double ut = Planetarium.GetUniversalTime();
            List<ManeuverParameters> maneuvers;
            try
            {
                maneuvers = op.MakeNodes(vessel.orbit, ut, core.Target);
            }
            catch (Exception ex)
            {
                return CommandResult.Fail("Planner MakeNodes failed: " + ex.Message);
            }
            if (maneuvers == null || maneuvers.Count == 0)
                return CommandResult.Fail("Planner produced no node (no transfer window / bad geometry).");

            if (vessel.patchedConicSolver != null)
                vessel.patchedConicSolver.maneuverNodes.Clear();
            foreach (ManeuverParameters m in maneuvers)
                vessel.PlaceManeuverNode(vessel.orbit, m.dV, m.UT);

            ManeuverParameters firstNode = maneuvers[0];
            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "planned", true },
                { "operation", operation },
                { "nodeCount", maneuvers.Count },
                { "dv", firstNode.dV.magnitude },
                { "ut", firstNode.UT }
            });
        }

        private CommandResult MjLandCommand(Dictionary<string, string> fields)
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
                return CommandResult.Fail("MechJeb land requires an active vessel in flight.");
            Vessel vessel = FlightGlobals.ActiveVessel;
            MechJebCore core = GetMasterCore(vessel);
            if (core == null)
                return CommandResult.Fail("No MechJeb core on the active vessel.");
            MechJebModuleLandingAutopilot land = core.GetComputerModule<MechJebModuleLandingAutopilot>();
            if (land == null)
                return CommandResult.Fail("MechJebModuleLandingAutopilot not present.");
            land.TouchdownSpeed.Val = GetOptionalDouble(fields, "touchdownSpeed", 0.5);
            land.DeployGears = true;
            land.DeployChutes = true;
            land.RCSAdjustment = true;
            vessel.ActionGroups.SetGroup(KSPActionGroup.RCS, true);
            bool targeted = GetOptionalBool(fields, "targeted", false);
            if (targeted)
            {
                double lat = GetOptionalDouble(fields, "lat", 0.0);
                double lon = GetOptionalDouble(fields, "lon", 0.0);
                core.Target.SetPositionTarget(vessel.mainBody, lat, lon);
                land.LandAtPositionTarget(core);
            }
            else
            {
                land.LandUntargeted(core);
            }
            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "landing", true }, { "targeted", targeted }
            });
        }

        private CommandResult FlyVesselCommand(Dictionary<string, string> fields)
        {
            if (HighLogic.LoadedScene != GameScenes.SPACECENTER && HighLogic.LoadedScene != GameScenes.TRACKSTATION)
            {
                return CommandResult.Fail("fly-vessel only works from the Space Center / Tracking Station scene.");
            }
            string name = GetOptional(fields, "vessel", "").Trim();
            if (name.Length == 0)
            {
                return CommandResult.Fail("Provide a 'vessel' name.");
            }
            int idx = -1;
            for (int i = 0; i < FlightGlobals.Vessels.Count; i++)
            {
                Vessel vv = FlightGlobals.Vessels[i];
                if (vv != null && string.Equals(vv.vesselName, name, StringComparison.OrdinalIgnoreCase))
                {
                    idx = i;
                    break;
                }
            }
            if (idx < 0)
            {
                return CommandResult.Fail("Vessel not found: " + name);
            }
            // Persist current state, then load the flight scene focused on that vessel (the same call
            // the Tracking Station "Fly" button uses).
            GamePersistence.SaveGame("persistent", HighLogic.SaveFolder, SaveMode.OVERWRITE);
            FlightDriver.StartAndFocusVessel("persistent", idx);
            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "flying", name },
                { "index", idx }
            });
        }

        private static string GetOptional(Dictionary<string, string> fields, string key, string fallback)
        {
            string value;
            return fields.TryGetValue(key, out value) ? value : fallback;
        }

        // ================= EVA movement + personnel + game-data read-back =================
        // All of the following touch the live KSP scene and are invoked via RunOnMainThread. Each one
        // is wrapped so a thrown exception becomes a CommandResult.Fail rather than killing the main
        // thread. Every number is pulled from the KSP API (body radius, masses, resources) — nothing is
        // hardcoded — so the LLM planner can reason over the SAME numbers the game uses.

        // Resolve a vessel by an optional name substring; empty/absent -> the active vessel.
        private static Vessel ResolveVessel(string nameSubstr)
        {
            Vessel active = FlightGlobals.ActiveVessel;
            if (string.IsNullOrEmpty(nameSubstr))
            {
                return active;
            }
            if (FlightGlobals.Vessels != null)
            {
                foreach (Vessel v in FlightGlobals.Vessels)
                {
                    if (v != null && v.loaded && v.vesselName != null &&
                        v.vesselName.IndexOf(nameSubstr, StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        return v;
                    }
                }
            }
            return active;
        }

        // Find the active (or named) EVA kerbal vessel. Prefers the active vessel if it IS an EVA.
        private static Vessel ResolveEvaVessel(string crewName)
        {
            Vessel active = FlightGlobals.ActiveVessel;
            if (active != null && active.isEVA && EvaCrewNameMatches(active, crewName))
            {
                return active;
            }
            if (FlightGlobals.Vessels != null)
            {
                foreach (Vessel v in FlightGlobals.Vessels)
                {
                    if (v != null && v.loaded && v.isEVA && EvaCrewNameMatches(v, crewName))
                    {
                        return v;
                    }
                }
            }
            return null;
        }

        private static KerbalEVA GetEvaModule(Vessel evaVessel)
        {
            if (evaVessel == null || evaVessel.parts == null)
            {
                return null;
            }
            foreach (Part p in evaVessel.parts)
            {
                if (p == null) { continue; }
                KerbalEVA eva = p.FindModuleImplementing<KerbalEVA>();
                if (eva != null) { return eva; }
            }
            return null;
        }

        // Great-circle (haversine) distance + initial bearing between two lat/lon points on a sphere of
        // the given radius. Pure math, identical to the formula the Python helper uses, returned so the
        // planner can sanity-check the move it asked for. lat/lon in DEGREES, radius in METRES.
        private static void Geodesic(double lat1Deg, double lon1Deg, double lat2Deg, double lon2Deg,
                                     double radiusM, out double distanceM, out double bearingDeg)
        {
            double toRad = Math.PI / 180.0;
            double phi1 = lat1Deg * toRad;
            double phi2 = lat2Deg * toRad;
            double dPhi = (lat2Deg - lat1Deg) * toRad;
            double dLam = (lon2Deg - lon1Deg) * toRad;
            double a = Math.Sin(dPhi / 2.0) * Math.Sin(dPhi / 2.0) +
                       Math.Cos(phi1) * Math.Cos(phi2) * Math.Sin(dLam / 2.0) * Math.Sin(dLam / 2.0);
            double c = 2.0 * Math.Atan2(Math.Sqrt(a), Math.Sqrt(Math.Max(0.0, 1.0 - a)));
            distanceM = radiusM * c;
            double y = Math.Sin(dLam) * Math.Cos(phi2);
            double x = Math.Cos(phi1) * Math.Sin(phi2) - Math.Sin(phi1) * Math.Cos(phi2) * Math.Cos(dLam);
            double brng = Math.Atan2(y, x) / toRad;   // -180..180
            bearingDeg = (brng + 360.0) % 360.0;       // 0..360, clockwise from north
        }

        // POST /eva-walk-to {lat, lon, [crew]} OR {bearing, distance, [crew]} -> drive the active (or
        // named) EVA kerbal toward a surface target. The target world position is computed from the body's
        // OWN geodesy (GetWorldSurfacePosition at the target lat/lon and the terrain altitude there), then
        // handed to KerbalEVA.SetWaypoint so the stock engine walks/jets the kerbal precisely. When the
        // caller gives bearing+distance instead of lat/lon, we project the destination along the great
        // circle from the kerbal's current position using the body radius — no guessing, exact spherical
        // trig. Returns the computed target lat/lon, geodesic distance and bearing so the planner can verify.
        private CommandResult EvaWalkToCommand(Dictionary<string, string> fields)
        {
            try
            {
                if (!HighLogic.LoadedSceneIsFlight)
                {
                    return CommandResult.Fail("EVA walk requires flight.");
                }
                string crewName = GetOptional(fields, "crew", "").Trim();
                Vessel eva = ResolveEvaVessel(crewName);
                if (eva == null)
                {
                    return CommandResult.Fail("No EVA kerbal found to move (call /eva-go first).", 404);
                }
                KerbalEVA module = GetEvaModule(eva);
                if (module == null)
                {
                    return CommandResult.Fail("EVA vessel has no KerbalEVA module.");
                }
                CelestialBody body = eva.mainBody;
                if (body == null)
                {
                    return CommandResult.Fail("EVA kerbal has no main body.");
                }

                double radius = body.Radius;
                double curLat = eva.latitude;
                double curLon = eva.longitude;

                bool haveLatLon = fields.ContainsKey("lat") && fields.ContainsKey("lon");
                bool haveVector = fields.ContainsKey("bearing") && fields.ContainsKey("distance");
                double tgtLat;
                double tgtLon;

                if (haveLatLon)
                {
                    tgtLat = GetOptionalDouble(fields, "lat", curLat);
                    tgtLon = GetOptionalDouble(fields, "lon", curLon);
                }
                else if (haveVector)
                {
                    // Project a destination along the great circle: given an initial bearing and a surface
                    // distance, compute the endpoint lat/lon on the sphere (standard direct geodesic).
                    double bearing = GetOptionalDouble(fields, "bearing", 0.0) * Math.PI / 180.0;
                    double dist = GetOptionalDouble(fields, "distance", 0.0);
                    double angular = dist / radius;       // central angle
                    double phi1 = curLat * Math.PI / 180.0;
                    double lam1 = curLon * Math.PI / 180.0;
                    double phi2 = Math.Asin(Math.Sin(phi1) * Math.Cos(angular) +
                                            Math.Cos(phi1) * Math.Sin(angular) * Math.Cos(bearing));
                    double lam2 = lam1 + Math.Atan2(Math.Sin(bearing) * Math.Sin(angular) * Math.Cos(phi1),
                                                    Math.Cos(angular) - Math.Sin(phi1) * Math.Sin(phi2));
                    tgtLat = phi2 * 180.0 / Math.PI;
                    tgtLon = ((lam2 * 180.0 / Math.PI) + 540.0) % 360.0 - 180.0;
                }
                else
                {
                    return CommandResult.Fail("Provide either {lat,lon} or {bearing,distance}.");
                }

                // The body's own surface position at the target (terrain altitude from the body, never guessed).
                double tgtAlt = body.TerrainAltitude(tgtLat, tgtLon, true);
                Vector3d worldTarget = body.GetWorldSurfacePosition(tgtLat, tgtLon, tgtAlt);

                double distM;
                double bearingDeg;
                Geodesic(curLat, curLon, tgtLat, tgtLon, radius, out distM, out bearingDeg);

                try
                {
                    module.SetWaypoint((Vector3)worldTarget);
                }
                catch (Exception ex)
                {
                    return CommandResult.Fail("KerbalEVA.SetWaypoint threw: " + ex.Message);
                }

                AppendStatus("eva", "Walk " + eva.vesselName + " -> " + tgtLat.ToString("F4") + "," +
                                    tgtLon.ToString("F4") + " (" + distM.ToString("F1") + " m)");
                return CommandResult.Ok(new Dictionary<string, object>
                {
                    { "message", eva.vesselName + " is walking to the target. VERIFY arrival in-game (stock pathing)." },
                    { "evaVessel", eva.vesselName ?? "" },
                    { "body", body.bodyName ?? "" },
                    { "fromLatitude", curLat },
                    { "fromLongitude", curLon },
                    { "targetLatitude", tgtLat },
                    { "targetLongitude", tgtLon },
                    { "targetTerrainAlt", tgtAlt },
                    { "distanceM", distM },
                    { "bearingDeg", bearingDeg },
                    { "bodyRadiusM", radius }
                });
            }
            catch (Exception ex)
            {
                _lastError = ex.ToString();
                return CommandResult.Fail("eva-walk-to failed: " + ex.Message);
            }
        }

        // GET /eva-status -> position (lat/lon/alt), surface velocity, ladder/ground state, fuel of the
        // active (or first) EVA kerbal. Read-only.
        private CommandResult EvaStatusCommand()
        {
            try
            {
                Dictionary<string, object> data = new Dictionary<string, object>();
                bool flight = HighLogic.LoadedSceneIsFlight;
                data["flight"] = flight;
                if (!flight)
                {
                    return CommandResult.Ok(data);
                }
                Vessel eva = ResolveEvaVessel("");
                data["onEva"] = eva != null;
                if (eva == null)
                {
                    return CommandResult.Ok(data);
                }
                data["evaVessel"] = eva.vesselName ?? "";
                data["body"] = eva.mainBody != null ? eva.mainBody.bodyName : "";
                data["latitude"] = eva.latitude;
                data["longitude"] = eva.longitude;
                data["altitude"] = eva.altitude;
                data["radarAltitude"] = eva.radarAltitude;
                data["srfSpeed"] = eva.srfSpeed;
                data["horizontalSrfSpeed"] = eva.horizontalSrfSpeed;
                data["verticalSpeed"] = eva.verticalSpeed;
                data["landed"] = eva.Landed;
                data["splashed"] = eva.Splashed;
                List<ProtoCrewMember> crew = eva.GetVesselCrew();
                data["kerbal"] = (crew != null && crew.Count > 0 && crew[0] != null) ? crew[0].name : (eva.vesselName ?? "");

                KerbalEVA module = GetEvaModule(eva);
                data["hasEvaModule"] = module != null;
                if (module != null)
                {
                    data["fuel"] = module.Fuel;
                    data["fuelCapacity"] = module.FuelCapacity;
                    data["onLadder"] = module.OnALadder;
                    data["jetpackDeployed"] = module.JetpackDeployed;
                    data["hasJetpack"] = module.HasJetpack;
                    data["fsmState"] = module.fsm != null && module.fsm.currentStateName != null
                        ? module.fsm.currentStateName : "";
                    data["ladderPart"] = module.LadderPart != null && module.LadderPart.partInfo != null
                        ? module.LadderPart.partInfo.title : "";
                }
                return CommandResult.Ok(data);
            }
            catch (Exception ex)
            {
                _lastError = ex.ToString();
                return CommandResult.Fail("eva-status failed: " + ex.Message);
            }
        }

        // GET /crew-list -> every kerbal currently in the loaded scene: name, type, trait, level, the
        // vessel + part + seat index they occupy (or "EVA"). Read-only.
        private CommandResult CrewListCommand()
        {
            try
            {
                if (!HighLogic.LoadedSceneIsFlight)
                {
                    return CommandResult.Fail("crew-list requires flight.");
                }
                StringBuilder sb = new StringBuilder();
                sb.Append("[");
                int count = 0;
                if (FlightGlobals.Vessels != null)
                {
                    foreach (Vessel v in FlightGlobals.Vessels)
                    {
                        if (v == null || !v.loaded || v.parts == null) { continue; }
                        foreach (Part part in v.parts)
                        {
                            if (part == null || part.protoModuleCrew == null || part.protoModuleCrew.Count == 0)
                            {
                                continue;
                            }
                            string partTitle = part.partInfo != null ? part.partInfo.title : part.name;
                            foreach (ProtoCrewMember pcm in part.protoModuleCrew)
                            {
                                if (pcm == null) { continue; }
                                if (count > 0) { sb.Append(","); }
                                sb.Append("{");
                                sb.Append("\"name\":\"").Append(JsonEscape(pcm.name)).Append("\",");
                                sb.Append("\"type\":\"").Append(JsonEscape(pcm.type.ToString())).Append("\",");
                                sb.Append("\"trait\":\"").Append(JsonEscape(pcm.trait ?? "")).Append("\",");
                                sb.Append("\"level\":").Append(pcm.experienceLevel.ToString(CultureInfo.InvariantCulture)).Append(",");
                                sb.Append("\"vessel\":\"").Append(JsonEscape(v.vesselName ?? "")).Append("\",");
                                sb.Append("\"part\":\"").Append(JsonEscape(partTitle)).Append("\",");
                                sb.Append("\"seat\":").Append(pcm.seatIdx.ToString(CultureInfo.InvariantCulture)).Append(",");
                                sb.Append("\"isEva\":").Append(v.isEVA ? "true" : "false");
                                sb.Append("}");
                                count++;
                            }
                        }
                    }
                }
                sb.Append("]");
                return CommandResult.Ok(new Dictionary<string, object>
                {
                    { "count", count },
                    { "crew", new RawJson(sb.ToString()) }
                });
            }
            catch (Exception ex)
            {
                _lastError = ex.ToString();
                return CommandResult.Fail("crew-list failed: " + ex.Message);
            }
        }

        // GET /crew-roster -> the whole game roster (Available/Assigned/Dead/Missing) from
        // HighLogic.CurrentGame.CrewRoster, with counts. Read-only.
        private CommandResult CrewRosterCommand()
        {
            try
            {
                if (HighLogic.CurrentGame == null || HighLogic.CurrentGame.CrewRoster == null)
                {
                    return CommandResult.Fail("No crew roster (no game loaded).");
                }
                KerbalRoster roster = HighLogic.CurrentGame.CrewRoster;
                // KerbalRoster.Kerbals() has NO zero-arg overload in this build; the typed IEnumerable
                // properties (Crew/Applicants/Tourist/Unowned) cover every kerbal in the roster.
                StringBuilder sb = new StringBuilder();
                sb.Append("[");
                int count = 0;
                List<IEnumerable<ProtoCrewMember>> groups = new List<IEnumerable<ProtoCrewMember>>();
                groups.Add(roster.Crew);
                groups.Add(roster.Applicants);
                groups.Add(roster.Tourist);
                groups.Add(roster.Unowned);
                HashSet<string> seen = new HashSet<string>();
                foreach (IEnumerable<ProtoCrewMember> group in groups)
                {
                    if (group == null) { continue; }
                    foreach (ProtoCrewMember pcm in group)
                    {
                        if (pcm == null || pcm.name == null || seen.Contains(pcm.name)) { continue; }
                        seen.Add(pcm.name);
                        if (count > 0) { sb.Append(","); }
                        sb.Append("{");
                        sb.Append("\"name\":\"").Append(JsonEscape(pcm.name)).Append("\",");
                        sb.Append("\"type\":\"").Append(JsonEscape(pcm.type.ToString())).Append("\",");
                        sb.Append("\"trait\":\"").Append(JsonEscape(pcm.trait ?? "")).Append("\",");
                        sb.Append("\"level\":").Append(pcm.experienceLevel.ToString(CultureInfo.InvariantCulture)).Append(",");
                        sb.Append("\"status\":\"").Append(JsonEscape(pcm.rosterStatus.ToString())).Append("\"");
                        sb.Append("}");
                        count++;
                    }
                }
                sb.Append("]");
                Dictionary<string, object> data = new Dictionary<string, object>
                {
                    { "count", count },
                    { "roster", new RawJson(sb.ToString()) }
                };
                try
                {
                    data["available"] = roster.GetAvailableCrewCount();
                    data["assigned"] = roster.GetAssignedCrewCount();
                    data["kia"] = roster.GetKIACrewCount();
                    data["missing"] = roster.GetMissingCrewCount();
                }
                catch (Exception)
                {
                    // counts are best-effort
                }
                return CommandResult.Ok(data);
            }
            catch (Exception ex)
            {
                _lastError = ex.ToString();
                return CommandResult.Fail("crew-roster failed: " + ex.Message);
            }
        }

        // POST /vessel-info {vessel?} -> mass (total/dry/resource), part/stage counts, crew, and the
        // aggregated resource totals (LiquidFuel/Oxidizer/MonoProp/EC + everything else). If MechJeb
        // stage-stats are available on the ACTIVE vessel, the vacuum total Δv is included. Read-only.
        private CommandResult VesselInfoCommand(Dictionary<string, string> fields)
        {
            try
            {
                if (!HighLogic.LoadedSceneIsFlight)
                {
                    return CommandResult.Fail("vessel-info requires flight.");
                }
                Vessel v = ResolveVessel(GetOptional(fields, "vessel", "").Trim());
                if (v == null)
                {
                    return CommandResult.Fail("No vessel resolved.", 404);
                }

                double totalMass = v.totalMass;          // tonnes
                double resourceMass = 0.0;
                int partCount = v.parts != null ? v.parts.Count : 0;
                int maxStage = 0;
                Dictionary<string, double[]> res = new Dictionary<string, double[]>(); // name -> {amount, max, density}

                if (v.parts != null)
                {
                    foreach (Part part in v.parts)
                    {
                        if (part == null) { continue; }
                        if (part.inverseStage > maxStage) { maxStage = part.inverseStage; }
                        resourceMass += part.GetResourceMass();
                        if (part.Resources != null)
                        {
                            foreach (PartResource pr in part.Resources)
                            {
                                if (pr == null) { continue; }
                                double density = pr.info != null ? pr.info.density : 0.0;
                                double[] acc;
                                if (!res.TryGetValue(pr.resourceName, out acc))
                                {
                                    acc = new double[3];
                                    acc[2] = density;
                                    res[pr.resourceName] = acc;
                                }
                                acc[0] += pr.amount;
                                acc[1] += pr.maxAmount;
                            }
                        }
                    }
                }

                double dryMass = totalMass - resourceMass;
                List<ProtoCrewMember> crew = v.GetVesselCrew();
                int crewCount = crew != null ? crew.Count : 0;
                int crewCapacity = v.GetCrewCapacity();

                StringBuilder rs = new StringBuilder();
                rs.Append("{");
                bool firstR = true;
                foreach (KeyValuePair<string, double[]> kv in res)
                {
                    if (!firstR) { rs.Append(","); }
                    firstR = false;
                    rs.Append("\"").Append(JsonEscape(kv.Key)).Append("\":{");
                    rs.Append("\"amount\":").Append(kv.Value[0].ToString("R", CultureInfo.InvariantCulture)).Append(",");
                    rs.Append("\"maxAmount\":").Append(kv.Value[1].ToString("R", CultureInfo.InvariantCulture)).Append(",");
                    rs.Append("\"density\":").Append(kv.Value[2].ToString("R", CultureInfo.InvariantCulture));
                    rs.Append("}");
                }
                rs.Append("}");

                Dictionary<string, object> data = new Dictionary<string, object>
                {
                    { "vessel", v.vesselName ?? "" },
                    { "body", v.mainBody != null ? v.mainBody.bodyName : "" },
                    { "situation", v.situation.ToString() },
                    { "partCount", partCount },
                    { "stageCount", maxStage + 1 },
                    { "currentStage", v.currentStage },
                    { "totalMassT", totalMass },
                    { "dryMassT", dryMass },
                    { "resourceMassT", resourceMass },
                    { "crewCount", crewCount },
                    { "crewCapacity", crewCapacity },
                    { "resources", new RawJson(rs.ToString()) }
                };

                // Δv: only meaningful for the active vessel that has a MechJeb core + stage stats.
                if (v == FlightGlobals.ActiveVessel)
                {
                    try
                    {
                        MechJebCore core = GetMasterCore(v);
                        if (core != null)
                        {
                            MechJebModuleStageStats st = core.GetComputerModule<MechJebModuleStageStats>();
                            if (st != null)
                            {
                                st.RequestUpdate();
                                data["vacTotalDeltaV"] = FuelStatsDeltaV(st.VacStats);
                                data["deltaVPending"] = st.VacStats.Count == 0;
                            }
                        }
                    }
                    catch (Exception)
                    {
                        // Δv is best-effort; never fail vessel-info on it.
                    }
                }
                return CommandResult.Ok(data);
            }
            catch (Exception ex)
            {
                _lastError = ex.ToString();
                return CommandResult.Fail("vessel-info failed: " + ex.Message);
            }
        }

        // POST /parts-list {vessel?} -> every part: name, title, mass (dry + resource), inverseStage and
        // the module class names on it. Lets the planner see the staging tree and what each part does.
        private CommandResult PartsListCommand(Dictionary<string, string> fields)
        {
            try
            {
                if (!HighLogic.LoadedSceneIsFlight)
                {
                    return CommandResult.Fail("parts-list requires flight.");
                }
                Vessel v = ResolveVessel(GetOptional(fields, "vessel", "").Trim());
                if (v == null || v.parts == null)
                {
                    return CommandResult.Fail("No vessel/parts resolved.", 404);
                }
                StringBuilder sb = new StringBuilder();
                sb.Append("[");
                int count = 0;
                foreach (Part part in v.parts)
                {
                    if (part == null) { continue; }
                    if (count > 0) { sb.Append(","); }
                    string title = part.partInfo != null ? part.partInfo.title : "";
                    double dryMass = part.mass;                 // tonnes, dry
                    double resMass = part.GetResourceMass();    // tonnes, resources
                    sb.Append("{");
                    sb.Append("\"name\":\"").Append(JsonEscape(part.name ?? "")).Append("\",");
                    sb.Append("\"title\":\"").Append(JsonEscape(title)).Append("\",");
                    sb.Append("\"dryMassT\":").Append(dryMass.ToString("R", CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"resourceMassT\":").Append(resMass.ToString("R", CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"stage\":").Append(part.inverseStage.ToString(CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"crew\":").Append((part.protoModuleCrew != null ? part.protoModuleCrew.Count : 0).ToString(CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"crewCapacity\":").Append(part.CrewCapacity.ToString(CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"modules\":[");
                    if (part.Modules != null)
                    {
                        bool firstM = true;
                        foreach (PartModule pm in part.Modules)
                        {
                            if (pm == null) { continue; }
                            if (!firstM) { sb.Append(","); }
                            firstM = false;
                            sb.Append("\"").Append(JsonEscape(pm.moduleName ?? pm.ClassName ?? "")).Append("\"");
                        }
                    }
                    sb.Append("]}");
                    count++;
                }
                sb.Append("]");
                return CommandResult.Ok(new Dictionary<string, object>
                {
                    { "vessel", v.vesselName ?? "" },
                    { "count", count },
                    { "parts", new RawJson(sb.ToString()) }
                });
            }
            catch (Exception ex)
            {
                _lastError = ex.ToString();
                return CommandResult.Fail("parts-list failed: " + ex.Message);
            }
        }

        // POST /resources {vessel?} -> aggregated per-resource amount/maxAmount/mass across the vessel.
        // A focused, lighter alternative to /vessel-info for fuel/EC budgeting. Read-only.
        private CommandResult ResourcesCommand(Dictionary<string, string> fields)
        {
            try
            {
                if (!HighLogic.LoadedSceneIsFlight)
                {
                    return CommandResult.Fail("resources requires flight.");
                }
                Vessel v = ResolveVessel(GetOptional(fields, "vessel", "").Trim());
                if (v == null || v.parts == null)
                {
                    return CommandResult.Fail("No vessel/parts resolved.", 404);
                }
                Dictionary<string, double[]> res = new Dictionary<string, double[]>(); // name -> {amount, max, density}
                foreach (Part part in v.parts)
                {
                    if (part == null || part.Resources == null) { continue; }
                    foreach (PartResource pr in part.Resources)
                    {
                        if (pr == null) { continue; }
                        double density = pr.info != null ? pr.info.density : 0.0;
                        double[] acc;
                        if (!res.TryGetValue(pr.resourceName, out acc))
                        {
                            acc = new double[3];
                            acc[2] = density;
                            res[pr.resourceName] = acc;
                        }
                        acc[0] += pr.amount;
                        acc[1] += pr.maxAmount;
                    }
                }
                StringBuilder sb = new StringBuilder();
                sb.Append("{");
                bool first = true;
                foreach (KeyValuePair<string, double[]> kv in res)
                {
                    if (!first) { sb.Append(","); }
                    first = false;
                    double massT = kv.Value[0] * kv.Value[2]; // amount * density (tonnes)
                    sb.Append("\"").Append(JsonEscape(kv.Key)).Append("\":{");
                    sb.Append("\"amount\":").Append(kv.Value[0].ToString("R", CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"maxAmount\":").Append(kv.Value[1].ToString("R", CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"density\":").Append(kv.Value[2].ToString("R", CultureInfo.InvariantCulture)).Append(",");
                    sb.Append("\"massT\":").Append(massT.ToString("R", CultureInfo.InvariantCulture));
                    sb.Append("}");
                }
                sb.Append("}");
                return CommandResult.Ok(new Dictionary<string, object>
                {
                    { "vessel", v.vesselName ?? "" },
                    { "resources", new RawJson(sb.ToString()) }
                });
            }
            catch (Exception ex)
            {
                _lastError = ex.ToString();
                return CommandResult.Fail("resources failed: " + ex.Message);
            }
        }
    }

    internal sealed class MainThreadWork
    {
        public readonly Func<CommandResult> Action;
        public readonly ManualResetEvent Done = new ManualResetEvent(false);
        public CommandResult Result;

        public MainThreadWork(Func<CommandResult> action)
        {
            Action = action;
        }
    }

    // One line in the agent-status ring buffer shown in the GUI and returned by GET /status.
    internal struct StatusLine
    {
        public string TimestampUtc;
        public string Phase;
        public string Message;
    }

    // Marker for a value that is already valid JSON and must be emitted verbatim
    // (not quoted/escaped) inside CommandResult.ToJson().
    internal sealed class RawJson
    {
        public readonly string Json;

        public RawJson(string json)
        {
            Json = json ?? "null";
        }
    }

    internal sealed class HttpRequest
    {
        public string Method;
        public string Path;
        public string Body;

        public static HttpRequest Read(NetworkStream stream)
        {
            StreamReader reader = new StreamReader(stream, Encoding.UTF8, false, 4096, true);
            string requestLine = reader.ReadLine() ?? "";
            string[] parts = requestLine.Split(' ');
            string method = parts.Length > 0 ? parts[0] : "";
            string path = parts.Length > 1 ? parts[1] : "";
            int contentLength = 0;
            string line;
            while (!string.IsNullOrEmpty(line = reader.ReadLine()))
            {
                int colon = line.IndexOf(':');
                if (colon <= 0)
                {
                    continue;
                }
                string name = line.Substring(0, colon).Trim();
                string value = line.Substring(colon + 1).Trim();
                if (string.Equals(name, "Content-Length", StringComparison.OrdinalIgnoreCase))
                {
                    int.TryParse(value, out contentLength);
                }
            }

            char[] buffer = new char[contentLength];
            int read = 0;
            while (read < contentLength)
            {
                int n = reader.Read(buffer, read, contentLength - read);
                if (n <= 0)
                {
                    break;
                }
                read += n;
            }

            return new HttpRequest { Method = method, Path = path, Body = new string(buffer, 0, read) };
        }
    }

    internal static class JsonObject
    {
        private static readonly Regex Pair = new Regex("\"(?<key>[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*)\"\\s*:\\s*\"(?<value>[^\"\\\\]*(?:\\\\.[^\"\\\\]*)*)\"", RegexOptions.Compiled);

        public static Dictionary<string, string> Parse(string body)
        {
            Dictionary<string, string> result = new Dictionary<string, string>();
            if (string.IsNullOrEmpty(body))
            {
                return result;
            }
            foreach (Match match in Pair.Matches(body))
            {
                result[Unescape(match.Groups["key"].Value)] = Unescape(match.Groups["value"].Value);
            }
            return result;
        }

        private static string Unescape(string value)
        {
            return value.Replace("\\\"", "\"").Replace("\\\\", "\\");
        }
    }

    internal sealed class CommandResult
    {
        public bool OkValue;
        public int HttpStatus;
        public string Error;
        public Dictionary<string, object> Data;

        public static CommandResult Ok(Dictionary<string, object> data)
        {
            return new CommandResult { OkValue = true, HttpStatus = 200, Data = data ?? new Dictionary<string, object>() };
        }

        public static CommandResult Fail(string error, int httpStatus = 400)
        {
            return new CommandResult { OkValue = false, HttpStatus = httpStatus, Error = error, Data = new Dictionary<string, object>() };
        }

        public string ToJson()
        {
            StringBuilder sb = new StringBuilder();
            sb.Append("{\"ok\":").Append(OkValue ? "true" : "false");
            if (!OkValue)
            {
                sb.Append(",\"error\":\"").Append(Escape(Error ?? "")).Append("\"");
            }
            foreach (KeyValuePair<string, object> item in Data)
            {
                sb.Append(",\"").Append(Escape(item.Key)).Append("\":");
                AppendJsonValue(sb, item.Value);
            }
            sb.Append("}");
            return sb.ToString();
        }

        private static void AppendJsonValue(StringBuilder sb, object value)
        {
            if (value == null)
            {
                sb.Append("null");
            }
            else if (value is RawJson)
            {
                // Pre-serialized JSON (e.g. an array of status objects): emit verbatim.
                sb.Append(((RawJson)value).Json);
            }
            else if (value is bool)
            {
                sb.Append((bool)value ? "true" : "false");
            }
            else if (value is int || value is long || value is float || value is double)
            {
                sb.Append(Convert.ToString(value, System.Globalization.CultureInfo.InvariantCulture));
            }
            else
            {
                sb.Append("\"").Append(Escape(Convert.ToString(value))).Append("\"");
            }
        }

        private static string Escape(string value)
        {
            return (value ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n");
        }
    }
}
