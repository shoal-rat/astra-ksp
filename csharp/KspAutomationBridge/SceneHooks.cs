using System;

namespace KspAutomationBridge
{
    /// <summary>
    /// Subscribes callbacks to GameEvents.onLevelWasLoaded. KSP's EventData.Add reads the delegate's
    /// Target to name the subscriber and throws a NullReferenceException for static delegates (any
    /// anonymous method that captures nothing), so every subscription goes through an instance here.
    /// </summary>
    internal sealed class SceneLoadedHook
    {
        private readonly Action _action;

        private SceneLoadedHook(Action action)
        {
            _action = action;
        }

        private void OnLevelWasLoaded(GameScenes scene)
        {
            _action();
        }

        internal static void Add(Action action)
        {
            GameEvents.onLevelWasLoaded.Add(new SceneLoadedHook(action).OnLevelWasLoaded);
        }
    }

    /// <summary>
    /// Whether KSP was asked to load a scene that has not finished loading, whoever asked (the bridge,
    /// kRPC's load, the player's menus). HighLogic.LoadScene fires onGameSceneLoadRequested and switches
    /// LoadedScene at the request; onLevelWasLoaded fires when the new scene is up (not for the loading
    /// buffer in between).
    /// </summary>
    internal sealed class SceneLoadWatch
    {
        // A load that never reports back (KSP failed mid-load) must not block callers forever.
        private const double GiveUpS = 180.0;
        private static readonly object Gate = new object();
        private static bool _requested;
        private static DateTime _requestedUtc;

        /// <summary>True from a scene load request until that scene has loaded.</summary>
        internal static bool LoadRequested
        {
            get
            {
                lock (Gate)
                {
                    return _requested && (DateTime.UtcNow - _requestedUtc).TotalSeconds < GiveUpS;
                }
            }
        }

        private void OnLoadRequested(GameScenes scene)
        {
            lock (Gate)
            {
                _requested = true;
                _requestedUtc = DateTime.UtcNow;
            }
        }

        private void OnLevelWasLoaded(GameScenes scene)
        {
            lock (Gate)
            {
                _requested = false;
            }
        }

        internal static void Install()
        {
            SceneLoadWatch watch = new SceneLoadWatch();
            GameEvents.onGameSceneLoadRequested.Add(watch.OnLoadRequested);
            GameEvents.onLevelWasLoaded.Add(watch.OnLevelWasLoaded);
        }
    }
}
