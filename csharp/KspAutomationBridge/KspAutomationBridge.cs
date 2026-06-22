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
            HighLogic.LoadScene(GameScenes.SPACECENTER);
            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "message", "Save load requested." },
                { "saveFolder", saveFolder }
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
            // Classic ascent path = stock-friendly; PVG is for RSS/RO.
            MechJebModuleAscentClassicAutopilot ap = core.GetComputerModule<MechJebModuleAscentClassicAutopilot>();
            if (ap == null)
                return CommandResult.Fail("Classic ascent autopilot not present.");
            ap.Users.Add(core);
            return CommandResult.Ok(new Dictionary<string, object>
            {
                { "ascent", true },
                { "altitude", GetOptionalDouble(fields, "altitude", 100000.0) },
                { "inclination", GetOptionalDouble(fields, "inclination", 0.0) }
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
                FlightGlobals.fetch.SetVesselTarget(body);
            }
            if (core.Target == null || !core.Target.NormalTargetExists)
                return CommandResult.Fail("No target set (pass target=Duna).");

            string operation = GetOptional(fields, "operation", "interplanetary").ToLowerInvariant();
            Operation op;
            if (operation == "interplanetary")
                op = new OperationInterplanetaryTransfer();
            else if (operation == "circularize")
                op = new OperationCircularize();
            else if (operation == "plane")
                op = new OperationPlane();
            else
                return CommandResult.Fail("Unknown operation '" + operation + "' (interplanetary|circularize|plane).");

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
