namespace KspAutomationBridge
{
    /// <summary>What POST /pause must do to reach the requested state (see PausePlanner.Plan).</summary>
    internal enum PauseAction
    {
        /// <summary>Already in the requested state: change nothing.</summary>
        None,

        /// <summary>FlightDriver.SetPause(true, false): pause without KSP's pause menu.</summary>
        Pause,

        /// <summary>FlightDriver.SetPause(false, false): resume a pause that has no menu open.</summary>
        Resume,

        /// <summary>PauseMenu.Close(): the menu (e.g. opened by kRPC's `paused`) resumes the game itself.</summary>
        CloseMenu,

        /// <summary>PauseMenu.Close() then FlightDriver.SetPause(true, false): keep the pause, drop the menu.</summary>
        CloseMenuThenPause,

        /// <summary>Refuse (409): the flight scene is still loading and its start-up would undo a pause set now.</summary>
        Refuse
    }

    /// <summary>
    /// The pause decisions, kept free of Unity and KSP types so they are tested offline.
    ///
    /// Stock facts (read from Assembly-CSharp 1.12.5 IL): FlightDriver.SetPause(true, _) sets
    /// FlightDriver.pause, Time.timeScale = 0, fires onGamePause and adds the "gamePause" control lock;
    /// SetPause(false, postScreenMessage) clears them, fires onGameUnpause and restores the warp rate
    /// (postScreenMessage only affects the warp-rate message). Neither opens the pause menu.
    /// PauseMenu.Display() calls SetPause(true) and shows the menu; PauseMenu.Close() hides it and
    /// calls SetPause(false, true). kRPC's KRPC.paused setter calls exactly PauseMenu.Display/Close,
    /// and its getter follows onGamePause/onGameUnpause, so it stays correct whichever side pauses.
    /// While paused without the menu the ESC key does nothing (PauseMenu.Update only toggles its menu).
    /// </summary>
    internal static class PausePlanner
    {
        /// <summary>
        /// The game's true paused state: in flight FlightDriver.Pause (whoever set it), anywhere a
        /// zero time scale (the space-center pause menu, dialogs that stop time).
        /// </summary>
        public static bool IsPaused(bool inFlight, bool flightDriverPause, float timeScale)
        {
            return (inFlight && flightDriverPause) || timeScale == 0f;
        }

        /// <summary>
        /// Whether the flight scene has finished loading, so that nothing in its start-up can undo a pause.
        /// HighLogic.LoadScene switches LoadedScene at the request, while the old scene still runs (a
        /// flight-to-flight load such as a quickload or kRPC's load); the old FlightDriver.OnDestroy then
        /// clears the pause and sets the time scale to 1, the new FlightDriver.Awake clears flightStarted,
        /// and FlightDriver.Start clears the pause again before setting flightStarted at its end.
        /// FlightGlobals.ready is NOT part of this: a vessel switch (EVA, boarding) clears it until the next
        /// physics tick, but does not touch the pause, so it must not block pausing.
        /// </summary>
        public static bool FlightLoaded(bool flightStarted, bool flightDriverExists, bool sceneLoadRequested)
        {
            return flightStarted && flightDriverExists && !sceneLoadRequested;
        }

        /// <summary>
        /// The action that takes the flight scene from its current state to `want`. A pause is refused
        /// while the scene is still loading (see FlightLoaded); resuming is always allowed.
        /// </summary>
        public static PauseAction Plan(bool want, bool flightDriverPause, float timeScale, bool menuOpen, bool flightLoaded)
        {
            if (want)
            {
                if (!flightLoaded)
                {
                    return PauseAction.Refuse;
                }
                if (menuOpen)
                {
                    return PauseAction.CloseMenuThenPause;
                }
                return flightDriverPause && timeScale == 0f ? PauseAction.None : PauseAction.Pause;
            }
            if (menuOpen)
            {
                return PauseAction.CloseMenu;
            }
            return flightDriverPause || timeScale == 0f ? PauseAction.Resume : PauseAction.None;
        }
    }
}
