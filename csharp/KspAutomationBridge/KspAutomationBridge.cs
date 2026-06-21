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
using UnityEngine;

namespace KspAutomationBridge
{
    [KSPAddon(KSPAddon.Startup.EveryScene, true)]
    public sealed class AutomationBridgeAddon : MonoBehaviour
    {
        private const int DefaultPort = 48500;
        private static readonly Regex CraftNameRegex = new Regex(@"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$", RegexOptions.Compiled);
        private static AutomationBridgeAddon _instance;

        private readonly ConcurrentQueue<MainThreadWork> _queue = new ConcurrentQueue<MainThreadWork>();
        private TcpListener _listener;
        private Thread _listenerThread;
        private volatile bool _running;
        private string _lastCraftName = "";
        private string _lastCraftPath = "";
        private string _lastError = "";

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
        }

        public void OnDestroy()
        {
            StopServer();
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
