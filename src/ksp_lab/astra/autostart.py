"""Fully autonomous KSP startup for ASTRA — locate, launch, wait, load-save, ready (NO human).

This module lets the agent bring Kerbal Space Program from "not even running" to "a save is
loaded and the automation bridge answers" without a person ever touching the keyboard. The user
directive that authorizes it:

    "grant this agent the necessary permissions to locate and launch KSP, load the save file, and
     complete all the setup steps — all without human intervention."

----------------------------------------------------------------------------------------------------
AUTONOMOUS-SETUP FLOW (the state machine ensure_ksp_ready drives)
----------------------------------------------------------------------------------------------------

    locate  ->  launch  ->  poll  ->  load-save  ->  ready

  1. LOCATE   Read the KSP install dir from config (``paths.ksp_install``) or fall back to the known
              Steam path ``C:/Program Files (x86)/Steam/steamapps/common/Kerbal Space Program``, then
              verify the executable (``KSP_x64.exe``) actually exists on disk. A missing exe fails
              fast with a clear ``detail`` rather than launching nothing.

  2. RUNNING? Probe the bridge TCP socket (127.0.0.1:48500). If it already accepts a connection, KSP
              is already up and we SHORT-CIRCUIT straight to the load-save step (never launch a second
              copy).

  3. LAUNCH   If the bridge is down, start the game through Steam with ``steam://run/220200`` (Steam
              app id 220200), via ``cmd /c start "" steam://run/220200`` so Windows hands the URL to
              Steam. We do not spawn the exe directly — going through Steam keeps DRM/launcher state
              correct and matches how a human would start it.

  4. POLL     Poll the bridge socket up to ``launch_timeout_s`` seconds (KSP + the addon's HTTP
              listener take a while to come up). Times out gracefully into a ReadyState that says so.

  5. LOAD     Once the bridge answers, if a ``save_name`` was requested, call
              ``bridge.load_save(save_name)`` -> the new C# ``/load-save`` endpoint loads the save from
              the MAIN MENU (no human clicking "Resume Saved Game") and lands at the Space Center.

  6. READY    Return a ``ReadyState`` describing exactly how far we got (running / bridge_up /
              save_loaded) plus a human-readable ``detail`` for logs.

----------------------------------------------------------------------------------------------------
TESTABILITY
----------------------------------------------------------------------------------------------------

Every external effect is INJECTABLE so the whole thing runs offline in CI:

  * ``socket_probe``   -> callable(host, port, timeout) -> bool   (is the bridge up?)
  * ``launcher``       -> callable(steam_uri) -> None             (fire the Steam launch)
  * ``bridge_factory`` -> callable(base_url, timeout_s) -> bridge (something with ``.load_save``)
  * ``sleep``          -> callable(seconds) -> None               (poll backoff; patched to no-op)
  * ``clock``          -> callable() -> float                     (monotonic time for the timeout)

The defaults are the real socket / subprocess / BridgeClient, so production callers just pass a
config. ``tests/test_autostart.py`` injects fakes and never launches Steam or restarts KSP.
"""

from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, sleep as _sleep
from typing import Any, Callable, Mapping

from ksp_lab.bridge_client import BridgeClient, BridgeError


# ---- Constants (the things the user granted permission to touch) ----------------------------------

STEAM_APP_ID = "220200"
STEAM_RUN_URI = "steam://run/" + STEAM_APP_ID
DEFAULT_KSP_INSTALL = r"C:/Program Files (x86)/Steam/steamapps/common/Kerbal Space Program"
KSP_EXE_NAME = "KSP_x64.exe"
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 48500


# ----------------------------------------------------------------------------------------------------
# Permissions manifest — an explicit, auditable statement of what this module is ALLOWED to do
# autonomously (and, just as importantly, what it must NOT do). Surfaced via ``permissions_manifest()``
# so a caller / log can record the grant the user made.
# ----------------------------------------------------------------------------------------------------

PERMISSIONS: dict[str, Any] = {
    "granted_by": "user directive 2026-06-25: locate, launch, load save, complete setup — no human",
    "allowed": [
        f"Launch Steam application id {STEAM_APP_ID} (Kerbal Space Program) via '{STEAM_RUN_URI}'.",
        "Read the KSP install directory from config or the known Steam path, and stat the executable.",
        f"Open a local TCP connection to the automation bridge at {BRIDGE_HOST}:{BRIDGE_PORT} to "
        "detect whether KSP is running.",
        "Call the bridge /load-save endpoint to load a named save from the main menu (no human click).",
    ],
    "forbidden": [
        "Kill or force-restart an already-running KSP process (only ever launches when none is up).",
        "Connect the bridge client to any non-localhost host (BridgeClient enforces this).",
        "Install or overwrite the bridge DLL / GameData (the main agent owns DLL installation).",
        "Delete, overwrite, or write save files (only READS a save by loading it).",
        "Modify Steam settings, accounts, or any other Steam app.",
    ],
}


def permissions_manifest() -> dict[str, Any]:
    """Return a copy of the autonomous-setup permissions manifest (what ASTRA may do at startup)."""
    return {
        "granted_by": PERMISSIONS["granted_by"],
        "allowed": list(PERMISSIONS["allowed"]),
        "forbidden": list(PERMISSIONS["forbidden"]),
    }


# ----------------------------------------------------------------------------------------------------
# Result type
# ----------------------------------------------------------------------------------------------------

@dataclass(slots=True)
class ReadyState:
    """Outcome of an ensure_ksp_ready run.

    running      KSP was already up, or we successfully launched it and the bridge came up.
    bridge_up    The automation bridge socket answered (the agent can now drive the game).
    save_loaded  A save was requested AND loaded successfully (False if no save_name was given).
    detail       Human-readable summary of what happened / why it stopped.
    save_result  The raw bridge /load-save response (when a save was loaded), else None.
    """

    running: bool
    bridge_up: bool
    save_loaded: bool
    detail: str
    save_result: dict | None = field(default=None)

    @property
    def ok(self) -> bool:
        """True iff the bridge is up (the minimum bar for "the agent can now act")."""
        return self.bridge_up


# ----------------------------------------------------------------------------------------------------
# Default (real) external effects — all overridable for tests
# ----------------------------------------------------------------------------------------------------

def _default_socket_probe(host: str, port: int, timeout: float) -> bool:
    """Return True iff a TCP connection to ``host:port`` succeeds within ``timeout`` seconds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _default_launcher(steam_uri: str) -> None:
    """Launch a Steam URI on Windows without blocking. ``start "" <uri>`` hands the URI to Steam."""
    # shell start needs the empty-title arg so a quoted URI is not consumed as the window title.
    subprocess.Popen(
        ["cmd", "/c", "start", "", steam_uri],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _default_bridge_factory(base_url: str, timeout_s: int) -> BridgeClient:
    return BridgeClient(base_url=base_url, timeout_s=timeout_s)


# ----------------------------------------------------------------------------------------------------
# Locate
# ----------------------------------------------------------------------------------------------------

def _resolve_install_dir(config: Mapping[str, Any]) -> Path:
    paths = config.get("paths") if isinstance(config, Mapping) else None
    configured = ""
    if isinstance(paths, Mapping):
        configured = (paths.get("ksp_install") or "").strip()
    return Path(configured) if configured else Path(DEFAULT_KSP_INSTALL)


def locate_ksp(config: Mapping[str, Any]) -> Path:
    """Return the verified path to the KSP executable, or raise FileNotFoundError with a clear message.

    Looks at ``config['paths']['ksp_install']`` first, then the known Steam install path.
    """
    install_dir = _resolve_install_dir(config)
    exe = install_dir / KSP_EXE_NAME
    if not exe.exists():
        raise FileNotFoundError(
            f"KSP executable not found at {exe}. Set paths.ksp_install in the config to the KSP "
            f"install directory (the folder containing {KSP_EXE_NAME})."
        )
    return exe


def _bridge_base_url(config: Mapping[str, Any]) -> str:
    bridge = config.get("bridge") if isinstance(config, Mapping) else None
    if isinstance(bridge, Mapping):
        url = bridge.get("base_url")
        if url:
            return str(url)
    return f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"


def _bridge_timeout_s(config: Mapping[str, Any]) -> int:
    bridge = config.get("bridge") if isinstance(config, Mapping) else None
    if isinstance(bridge, Mapping):
        try:
            return int(bridge.get("timeout_s") or 30)
        except (TypeError, ValueError):
            return 30
    return 30


# ----------------------------------------------------------------------------------------------------
# The state machine
# ----------------------------------------------------------------------------------------------------

def ensure_ksp_ready(
    config: Mapping[str, Any],
    *,
    save_name: str | None = None,
    launch_timeout_s: float = 300.0,
    poll_interval_s: float = 2.0,
    probe_timeout_s: float = 1.0,
    load_save_timeout_s: int = 120,
    socket_probe: Callable[[str, int, float], bool] | None = None,
    launcher: Callable[[str], None] | None = None,
    bridge_factory: Callable[[str, int], Any] | None = None,
    sleep: Callable[[float], None] = _sleep,
    clock: Callable[[], float] = monotonic,
) -> ReadyState:
    """Bring KSP from any state to "bridge up (+ save loaded)" with no human intervention.

    See the module docstring for the full locate -> launch -> poll -> load-save -> ready flow. All
    external effects (``socket_probe``, ``launcher``, ``bridge_factory``, ``sleep``, ``clock``) are
    injectable so this runs fully offline under test.

    Returns a :class:`ReadyState`. Never raises for the "KSP didn't come up" case — that is reported
    as ``ReadyState(running=False, bridge_up=False, ...)`` with a clear ``detail``. A genuinely
    misconfigured install (missing exe) DOES raise FileNotFoundError from the locate step.
    """
    probe = socket_probe or _default_socket_probe
    launch = launcher or _default_launcher
    make_bridge = bridge_factory or _default_bridge_factory

    base_url = _bridge_base_url(config)
    bridge_timeout = _bridge_timeout_s(config)

    # ---- 1+2. LOCATE + already-running short-circuit -------------------------------------------------
    # We always locate first so a broken install fails loudly even if we'd otherwise short-circuit.
    locate_ksp(config)

    if probe(BRIDGE_HOST, BRIDGE_PORT, probe_timeout_s):
        return _load_if_requested(
            running=True,
            launched=False,
            save_name=save_name,
            base_url=base_url,
            bridge_timeout=bridge_timeout,
            load_save_timeout_s=load_save_timeout_s,
            make_bridge=make_bridge,
        )

    # ---- 3. LAUNCH ----------------------------------------------------------------------------------
    launch(STEAM_RUN_URI)

    # ---- 4. POLL ------------------------------------------------------------------------------------
    deadline = clock() + launch_timeout_s
    bridge_up = False
    # Probe once immediately, then back off and re-probe until the deadline.
    while True:
        if probe(BRIDGE_HOST, BRIDGE_PORT, probe_timeout_s):
            bridge_up = True
            break
        if clock() >= deadline:
            break
        sleep(poll_interval_s)

    if not bridge_up:
        return ReadyState(
            running=False,
            bridge_up=False,
            save_loaded=False,
            detail=(
                f"Launched KSP via {STEAM_RUN_URI} but the bridge at {BRIDGE_HOST}:{BRIDGE_PORT} did "
                f"not come up within {launch_timeout_s:.0f}s. KSP may still be loading, Steam may have "
                "prompted, or the bridge DLL is not installed."
            ),
        )

    # ---- 5+6. LOAD-SAVE + READY ---------------------------------------------------------------------
    return _load_if_requested(
        running=True,
        launched=True,
        save_name=save_name,
        base_url=base_url,
        bridge_timeout=bridge_timeout,
        load_save_timeout_s=load_save_timeout_s,
        make_bridge=make_bridge,
    )


def _load_if_requested(
    *,
    running: bool,
    launched: bool,
    save_name: str | None,
    base_url: str,
    bridge_timeout: int,
    load_save_timeout_s: int,
    make_bridge: Callable[[str, int], Any],
) -> ReadyState:
    """Bridge is up. If a save was requested, load it; build the final ReadyState either way."""
    came_up = "Launched KSP and the bridge is up" if launched else "KSP already running; bridge is up"

    if not save_name:
        return ReadyState(
            running=running,
            bridge_up=True,
            save_loaded=False,
            detail=came_up + " (no save requested).",
        )

    bridge = make_bridge(base_url, bridge_timeout)
    try:
        result = bridge.load_save(save_name, timeout=load_save_timeout_s)
    except BridgeError as exc:
        return ReadyState(
            running=running,
            bridge_up=True,
            save_loaded=False,
            detail=came_up + f", but loading save '{save_name}' failed: {exc}",
        )

    scene = result.get("scene") if isinstance(result, Mapping) else None
    return ReadyState(
        running=running,
        bridge_up=True,
        save_loaded=True,
        detail=came_up + f"; save '{save_name}' loaded (scene={scene}).",
        save_result=dict(result) if isinstance(result, Mapping) else None,
    )
