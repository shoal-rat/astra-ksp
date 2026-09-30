"""The live link to the game: one kRPC connection, the bridge, scene/vessel guards, and streams.

kRPC remote objects go stale when the scene changes or the active vessel switches, so tools always
fetch the active vessel fresh through :func:`vessel` and never hold one across calls. Streams are
cached per (scene, vessel) and dropped automatically when either changes.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any, Callable

from astra.bridge import Bridge
from astra.config import CONFIG
from astra.errors import BridgeError, NoVessel, NotConnected, WrongScene

try:  # kRPC is required for live play but not for offline tests of pure tools.
    import krpc  # type: ignore
except ImportError:  # pragma: no cover
    krpc = None  # type: ignore


class KSP:
    def __init__(self) -> None:
        self._conn = None
        self._last_health = 0.0
        self._streams: dict[tuple, Any] = {}
        self._stream_owner: tuple | None = None
        self._lock = threading.RLock()
        self.bridge = Bridge()

    # -- connection ---------------------------------------------------------------------------

    @property
    def conn(self):
        with self._lock:
            if self._conn is not None and time.monotonic() - self._last_health > 2.0:
                try:
                    self._conn.krpc.get_status()
                    self._last_health = time.monotonic()
                except Exception:  # noqa: BLE001 — any failure means the link is dead
                    self._drop()
            if self._conn is None:
                self._connect()
            return self._conn

    def _connect(self, attempts: int = 3) -> None:
        if krpc is None:
            raise NotConnected("the krpc package is not installed", "pip install krpc==0.5.4")
        last: Exception | None = None
        for i in range(attempts):
            try:
                self._conn = krpc.connect(name="astra", address=CONFIG.krpc_host,
                                          rpc_port=CONFIG.krpc_rpc_port, stream_port=CONFIG.krpc_stream_port)
                self._last_health = time.monotonic()
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                if i + 1 < attempts:
                    time.sleep(1.0 + i)
        raise NotConnected(
            f"cannot reach kRPC at {CONFIG.krpc_host}:{CONFIG.krpc_rpc_port} ({last})",
            "kRPC only serves once a save is loaded. Call game_status; if the game sits at the main "
            "menu, call game_load_save; if KSP is not running, run `astra up`.")

    def _drop(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._conn = None
        self._streams.clear()
        self._stream_owner = None

    def try_conn(self, timeout: float = 0.5):
        """The live connection or None, without retries or sleeps (for tools that work offline)."""
        import socket

        with self._lock:
            if self._conn is not None:
                if time.monotonic() - self._last_health <= 2.0:
                    return self._conn
                try:
                    self._conn.krpc.get_status()
                    self._last_health = time.monotonic()
                    return self._conn
                except Exception:  # noqa: BLE001 — dead link: drop it, then probe once below
                    self._drop()
        try:
            with socket.create_connection((CONFIG.krpc_host, CONFIG.krpc_rpc_port), timeout=timeout):
                pass
        except OSError:
            return None
        try:
            with self._lock:
                self._connect(attempts=1)
            return self._conn
        except NotConnected:
            return None

    def connected(self) -> bool:
        try:
            _ = self.conn
            return True
        except NotConnected:
            return False

    # -- scene + vessel -----------------------------------------------------------------------

    @property
    def sc(self):
        return self.conn.space_center

    def scene(self) -> str:
        """'flight', 'space_center', 'tracking_station', 'editor_vab', 'editor_sph', ..."""
        return str(self.conn.krpc.current_game_scene).split(".")[-1]

    def require_flight(self) -> None:
        s = self.scene()
        if s != "flight":
            raise WrongScene(f"this needs the flight scene; the game is in '{s}'",
                             "launch a craft (game_launch) or switch to a vessel (game_switch_vessel).")

    def vessel(self):
        self.require_flight()
        try:
            v = self.sc.active_vessel
        except Exception as exc:  # noqa: BLE001
            raise NoVessel(f"no active vessel ({exc})", "game_list_vessels then game_switch_vessel") from exc
        if v is None:
            raise NoVessel("no active vessel", "game_list_vessels then game_switch_vessel")
        return v

    # -- pause --------------------------------------------------------------------------------

    @property
    def paused(self) -> bool:
        if self.bridge.up():
            try:
                return bool(self.bridge.get("/pause", timeout=5.0).get("paused"))
            except (BridgeError, NotConnected):
                pass
        return bool(self.conn.krpc.paused)

    def set_paused(self, value: bool) -> None:
        """Pause/resume without KSP's pause menu (bridge POST /pause); kRPC's `paused` opens the ESC
        menu, which covers the view and the camera, so it is only the fallback."""
        if self.scene() == "flight" and self.bridge.up():
            try:
                reply = self.bridge.post("/pause", {"paused": bool(value)}, timeout=10.0)
                if bool(reply.get("paused")) == bool(value):
                    return
            except (BridgeError, NotConnected):
                pass
        self.conn.krpc.paused = bool(value)

    def hold_for_deliberation(self) -> bool:
        """Pause if the policy says the sim should wait while the AI thinks. Returns paused state."""
        if CONFIG.pause_between_commands and self.scene() == "flight":
            self.set_paused(True)
            return True
        return self.paused

    @contextlib.contextmanager
    def running(self, settle_s: float = 0.0):
        """Let the sim tick for the duration of the block, then restore the pause state.

        kRPC applies control inputs on the next physics tick, and calls such as
        `activate_next_stage()` or `Decoupler.decouple()` wait for physics frames: made while the
        game is paused they block forever. Wrap them in `with ksp().running():`.
        """
        was_paused = self.paused
        if was_paused:
            self.set_paused(False)
        try:
            yield
            if settle_s > 0:
                time.sleep(settle_s)
        finally:
            if was_paused:
                self.set_paused(True)

    # -- streams ------------------------------------------------------------------------------

    def stream(self, key: str, fn: Callable, *args: Any) -> Any:
        """A cached kRPC stream for the active vessel. ``stream('alt', flight.mean_altitude)`` style
        getters are passed as (callable, *args) exactly like ``conn.add_stream``."""
        v = self.vessel()
        owner = (self.scene(), v._object_id)
        if owner != self._stream_owner:
            for s in self._streams.values():
                try:
                    s.remove()
                except Exception:  # noqa: BLE001
                    pass
            self._streams.clear()
            self._stream_owner = owner
        s = self._streams.get((key,))
        if s is None:
            s = self.conn.add_stream(fn, *args)
            self._streams[(key,)] = s
        return s

    def drop_streams(self) -> None:
        """Forget cached streams (after a load/revert that may reuse the same vessel handle)."""
        for s in self._streams.values():
            try:
                s.remove()
            except Exception:  # noqa: BLE001
                pass
        self._streams.clear()
        self._stream_owner = None

    def body(self, name: str | None = None):
        if name is None:
            return self.vessel().orbit.body
        bodies = self.sc.bodies
        for k, b in bodies.items():
            if k.lower() == name.lower():
                return b
        raise KeyError(f"unknown body {name!r}; known: {sorted(bodies)}")


_KSP: KSP | None = None


def ksp() -> KSP:
    global _KSP
    if _KSP is None:
        _KSP = KSP()
    return _KSP
