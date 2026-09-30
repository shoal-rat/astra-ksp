// KspAutomationBridge: the KSP-side half of ASTRA. It exposes, over HTTP on 127.0.0.1, what kRPC
// cannot do: scene control from the main menu, the loaded part database with geometry, EVA, MechJeb
// with every setting explicit, the two-way CAPCOM panel, render-to-texture screenshots and video frame recording.
// Endpoint reference: docs/BRIDGE_API.md. Changes take effect only after a rebuild
// (`astra bridge build`), installing the DLL while KSP is closed, and restarting KSP.
using System;
using System.IO;
using System.Net;
using System.Runtime.CompilerServices;
using UnityEngine;

namespace KspAutomationBridge
{
    [KSPAddon(KSPAddon.Startup.EveryScene, true)]
    public sealed class AutomationBridgeAddon : MonoBehaviour
    {
        public const string BridgeVersion = "2.1.0";
        public const int DefaultPort = 48500;
        private const int DrainBudgetMs = 100;

        internal static readonly MainThreadQueue Jobs = new MainThreadQueue();
        internal static AutomationBridgeAddon Instance;
        internal static DateTime StartedUtc;
        internal static int Port;
        internal static bool Listening;

        private static bool _mechJebTickFailed;

        private HttpServer _server;
        private CapcomWindow _capcom;

        private static void Safely(string what, Action step)
        {
            try
            {
                step();
            }
            catch (Exception ex)
            {
                BridgeLog.RecordError(what + " failed at startup", ex);
            }
        }

        public void Start()
        {
            if (Instance != null && Instance != this)
            {
                Destroy(gameObject);
                return;
            }
            Instance = this;
            DontDestroyOnLoad(gameObject);
            StartedUtc = DateTime.UtcNow;

            BridgeLog.InfoSink = delegate(string m) { Debug.Log(m); };
            BridgeLog.WarnSink = delegate(string m) { Debug.LogWarning(m); };
            BridgeLog.ErrorSink = delegate(string m) { Debug.LogError(m); };

            // Optional subsystems must never keep the server from starting: each one is isolated.
            Safely("MechJeb detection", MechJebInfo.Detect);
            Safely("EVA walker", EvaWalker.Install);
            Port = ReadPort();

            Router router = new Router(Jobs);
            router.RegisterBuiltins();
            Safely("game routes", delegate { GameRoutes.Register(router); });
            Safely("crew routes", delegate { CrewRoutes.Register(router); });
            Safely("EVA routes", delegate { EvaRoutes.Register(router); });
            Safely("part database routes", delegate { PartDatabase.Register(router); });
            Safely("MechJeb routes", delegate { MechJebRoutes.Register(router); });
            Safely("CAPCOM routes", delegate { CapcomRoutes.Register(router); });
            Safely("screenshot routes", delegate { ScreenshotRoutes.Register(router); });
            Safely("recorder routes", delegate { Recorder.Register(router); });

            _server = new HttpServer(router);
            try
            {
                _server.Start(IPAddress.Loopback, Port);
                Listening = true;
                BridgeLog.Info("Bridge " + BridgeVersion + " listening on http://127.0.0.1:" + Port
                    + (MechJebInfo.Available ? " (MechJeb " + MechJebInfo.Version + ")" : " (MechJeb not found)"));
            }
            catch (Exception ex)
            {
                BridgeLog.RecordError("Could not listen on 127.0.0.1:" + Port, ex);
            }

            _capcom = new CapcomWindow();
            _capcom.Start();
            StartCoroutine(Recorder.Loop());
        }

        public void Update()
        {
            Jobs.Drain(DrainBudgetMs);
            FlagPlanting.Tick();
            if (MechJebInfo.Available)
            {
                TickMechJeb();
            }
            if (_capcom != null)
            {
                _capcom.Update();
            }
        }

        // Kept out of Update so MechJeb types are only resolved when MechJeb is loaded.
        [MethodImpl(MethodImplOptions.NoInlining)]
        private static void TickMechJeb()
        {
            try
            {
                MechJebOps.Tick();
            }
            catch (Exception ex)
            {
                if (!_mechJebTickFailed)
                {
                    _mechJebTickFailed = true; // log once, not every frame
                    BridgeLog.RecordError("MechJeb autostager release failed", ex);
                }
            }
        }

        public void OnGUI()
        {
            if (_capcom != null)
            {
                _capcom.OnGUI();
            }
        }

        public void OnDestroy()
        {
            if (Instance != this)
            {
                return;
            }
            Listening = false;
            Recorder.Shutdown();
            if (_server != null)
            {
                _server.Stop();
            }
            if (_capcom != null)
            {
                _capcom.Destroy();
            }
            Instance = null;
        }

        /// <summary>Port from GameData/KspAutomationBridge/PluginData/bridge.cfg (`port = N`), else 48500.</summary>
        private static int ReadPort()
        {
            try
            {
                string path = Path.Combine(KSPUtil.ApplicationRootPath, "GameData/KspAutomationBridge/PluginData/bridge.cfg");
                if (File.Exists(path))
                {
                    ConfigNode root = ConfigNode.Load(path);
                    int port;
                    if (root != null && root.HasValue("port") && int.TryParse(root.GetValue("port"), out port) && port > 0 && port < 65536)
                    {
                        return port;
                    }
                }
            }
            catch (Exception ex)
            {
                BridgeLog.Warn("Ignoring unreadable bridge.cfg: " + ex.Message);
            }
            return DefaultPort;
        }
    }

    /// <summary>
    /// Whether MechJeb2 is loaded, detected without touching any MechJeb type so this assembly keeps
    /// working (minus the /mj-* routes) when MechJeb is missing or fails to load.
    /// </summary>
    internal static class MechJebInfo
    {
        public static bool Available;
        public static string Version = "";

        public static void Detect()
        {
            try
            {
                foreach (AssemblyLoader.LoadedAssembly loaded in AssemblyLoader.loadedAssemblies)
                {
                    if (loaded != null && loaded.assembly != null && loaded.name == "MechJeb2")
                    {
                        Available = true;
                        Version = loaded.versionMajor + "." + loaded.versionMinor + "." + loaded.versionRevision;
                        return;
                    }
                }
            }
            catch (Exception ex)
            {
                BridgeLog.Warn("MechJeb detection failed: " + ex.Message);
            }
        }

        public static void Require()
        {
            if (!Available)
            {
                throw new BridgeException(503, "MechJeb2 is not installed or failed to load.",
                    "Install MechJeb2 (and the MechJebForAll.cfg patch) to use /mj-* endpoints.");
            }
        }
    }
}
