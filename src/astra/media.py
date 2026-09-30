"""Filming flights: the bridge's recorder, event marks, and an automatic camera director.

Not a crew tool. Recording is production tooling around a flight: `astra record` (or
`astra mission --record`) starts the bridge recorder, marks each tool call (not the read-only instruments) in `marks.csv` so the
footage can be cut and captioned by event, and runs a director that frames the active vessel for
the phase of flight (a slow orbit around the vehicle, close on a kerbal, low over the ground on
landing). The director only moves the camera while game time runs; it never touches flight controls.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from astra.config import CONFIG, REPO_ROOT

try:
    import krpc
except ImportError:  # pragma: no cover
    krpc = None


HOLD_FILE = CONFIG.cache_dir / "director.hold"  # while it exists the director leaves the camera alone
# Exists while a recording started here runs. The tool-call marks read it (a file check, no network),
# because the process that records (`astra record`, the mission runner) is not the MCP server whose
# tool calls get marked.
MARKER_FILE = CONFIG.cache_dir / "recording.json"


def ffmpeg_exe() -> str | None:
    """The ffmpeg binary: imageio-ffmpeg's bundled build, else one on PATH."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


def _bridge():
    from astra.bridge import Bridge

    return Bridge()


def recordings_dir() -> Path:
    d = REPO_ROOT / "recordings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def start(folder: Path, *, fps: float = 15.0, width: int = 1728, height: int | None = None, crf: int = 20,
          include_ui: bool = True, video: bool = True) -> dict:
    """Start the bridge recorder into `folder` (H.264 video.mkv with ffmpeg, else JPEG frames)."""
    body: dict[str, Any] = {"dir": str(Path(folder).resolve()), "fps": fps, "width": width, "crf": crf,
                            "includeUi": include_ui}
    if height:
        body["height"] = height
    exe = ffmpeg_exe() if video else None
    if exe:
        body["ffmpeg"] = exe
    started = _bridge().post("/record/start", body, timeout=30.0)
    if video and not exe:
        started["warning"] = ('no ffmpeg found (pip install -e ".[media]", or put ffmpeg on PATH): recording JPEG '
                              "frames, which cost KSP's main thread ~20-40 ms each")
    _write_marker(body["dir"])
    return started


def stop() -> dict:
    try:
        return _bridge().post("/record/stop", {}, timeout=120.0)
    finally:
        _clear_marker()  # stopped, failed or unreachable: nothing records for the marks any more


def status() -> dict:
    return _bridge().get("/record/status", timeout=15.0)


def _write_marker(folder: str) -> None:
    try:
        MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        MARKER_FILE.write_text(json.dumps({"dir": folder, "pid": os.getpid()}), encoding="utf-8")
    except OSError:
        pass  # marks are best effort


def _clear_marker() -> None:
    try:
        MARKER_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# A marker the bridge showed idle or unreachable is skipped until `until` (a new recording writes a
# new marker, with a new mtime). Consecutive failures back off 1, 2, 4 ... 60 s, so one slow reply
# costs a second of marks, and a stale marker with KSP gone costs a probe a minute, not one per call.
_mute: dict[str, Any] = {"marker": None, "until": 0.0, "failures": 0}


def _live_marker() -> int | None:
    """The marker's version (mtime) while there is a recording to mark, else None. No network I/O."""
    try:
        version = MARKER_FILE.stat().st_mtime_ns
    except OSError:
        return None
    if _mute["marker"] == version and time.monotonic() < _mute["until"]:
        return None
    return version


def mark(label: str, detail: str = "") -> None:
    """Label the current frame of a recording started with start() (no-op otherwise; never raises)."""
    version = _live_marker()
    if version is None:
        return
    try:
        reply = _bridge().post("/record/mark", {"label": label, "detail": detail[:400]}, timeout=2.0)
    except Exception:  # noqa: BLE001 — marks are best effort: bridge busy or gone
        n = _mute["failures"] if _mute["marker"] == version else 0
        _mute.update(marker=version, until=time.monotonic() + min(60.0, 2.0 ** n), failures=n + 1)
        return
    _mute["failures"] = 0
    if not reply.get("recording"):  # stopped behind our back (low disk, KSP quit, a killed recorder)
        _mute.update(marker=version, until=time.monotonic() + 3600.0)


# -- tool-call marks -----------------------------------------------------------------------------

_QUIET = {"telemetry", "game_status", "orbit_info", "vessel_stages", "vessel_parts", "body_info",
          "crew_status", "crew_roster", "mj_status", "capcom_inbox", "node_list", "target_info"}


def _short(value: Any, limit: int = 160) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _outcome(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ("stopped_by", "verified", "planted", "boarded", "touchdown", "result", "status"):
        if key in result:
            return f"{key}={_short(result[key], 120)}"
    return ""


def _on_start(name: str, args: dict) -> None:
    try:
        if name not in _QUIET and _live_marker() is not None:
            mark(f"start:{name}", _short(args))
    except Exception:  # noqa: BLE001 — a mark must never break or fail a tool call
        pass


def _on_end(name: str, args: dict, ok: bool, payload: Any, seconds: float) -> None:
    try:
        if name not in _QUIET and _live_marker() is not None:
            mark(f"end:{name}", ("ok " + _outcome(payload)) if ok else f"error {payload}")
    except Exception:  # noqa: BLE001
        pass


_marks_installed = False


def install_marks() -> None:
    """Mark the start and end of every (non-trivial) tool call in the recording.

    Cheap to install everywhere: while nothing records, a hook costs one file check."""
    global _marks_installed
    if not _marks_installed:
        from astra import registry

        registry.add_start_hook(_on_start)
        registry.add_hook(_on_end)
        _marks_installed = True


# -- camera director -----------------------------------------------------------------------------


class Director(threading.Thread):
    """Frames the active vessel through kRPC's camera on its own connection.

    Every tick while game time runs it eases the camera distance and pitch toward a shot chosen by
    phase, and turns the heading slowly so the footage is never static:

    - kerbal on EVA: close (5 m), eye level
    - on the ground: 2.5x the vessel size, slightly above
    - low and descending (< 3 km radar altitude): low angle, the ground in frame
    - in the atmosphere: 2x size, trailing
    - in space: 3x size, from above, slow orbit

    While `HOLD_FILE` exists (a photo session, a hand-framed shot) it leaves the camera alone.
    """

    def __init__(self, rate_hz: float = 10.0, turn_dps: float = 2.0) -> None:
        super().__init__(name="astra-director", daemon=True)
        self.rate_hz = rate_hz
        self.turn_dps = turn_dps
        self._halt = threading.Event()
        self.shot = ""
        self.errors = 0

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        conn = None
        sizes: dict[Any, float] = {}
        heading = None
        phase_at = 0.0
        phase: tuple[str, float, float, float] = ("", 20.0, 10.0, self.turn_dps)
        cam = None
        last = time.monotonic()
        while not self._halt.is_set():
            time.sleep(1.0 / self.rate_hz)
            now = time.monotonic()
            # A slow tick (sizing a new vessel, an error back-off) must not become one big camera step.
            dt, last = min(now - last, 2.0 / self.rate_hz), now
            try:
                if conn is None:
                    conn = krpc.connect(name="astra-director", address=CONFIG.krpc_host,
                                        rpc_port=CONFIG.krpc_rpc_port, stream_port=None)  # no streams used
                    cam = heading = None
                if conn.krpc.paused or HOLD_FILE.exists():
                    heading = None  # re-read the camera when taking over again
                    continue
                sc = conn.space_center
                if cam is None:
                    cam = sc.camera
                mode = str(cam.mode).split(".")[-1]
                if mode in ("map", "iva"):
                    continue
                if now - phase_at > 1.0:
                    phase = self._phase(sc, sizes)
                    phase_at = now
                    self.shot = phase[0]
                _, dist, pitch, turn = phase
                if heading is None:
                    heading = cam.heading
                heading = (heading + turn * dt + 180.0) % 360.0 - 180.0
                k = 1.0 - math.exp(-dt / 1.5)  # ease toward the shot over ~1.5 s
                p, d = cam.pitch, cam.distance  # one read each: every read is a round trip
                cam.heading = heading
                cam.pitch = p + (pitch - p) * k
                cam.distance = d + (dist - d) * k
            except Exception:  # noqa: BLE001 — scene changes, vessel switches: retry calmly
                self.errors += 1
                cam = heading = None
                try:
                    if conn is not None and not self._game_up(conn):
                        conn.close()
                        conn = None
                except Exception:  # noqa: BLE001
                    conn = None
                time.sleep(1.0)
                last = time.monotonic()
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _game_up(conn: Any) -> bool:
        try:
            conn.krpc.get_status()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _phase(self, sc: Any, sizes: dict) -> tuple[str, float, float, float]:
        v = sc.active_vessel
        vtype = str(v.type).split(".")[-1]
        situation = str(v.situation).split(".")[-1]
        key = (v._object_id, len(v.parts.all))  # staging keeps the vessel id but changes its size
        if key not in sizes:
            from astra.telemetry import vessel_box

            box = vessel_box(v, v.reference_frame)
            size = max(h - l for l, h in zip(*box)) if box else 5.0
            sizes[key] = min(80.0, max(1.0, size))  # frame a sane size whatever a part reports
        size = sizes[key]
        if vtype == "eva":
            return ("eva", 5.0, 6.0, self.turn_dps * 1.5)
        if situation in ("landed", "splashed", "pre_launch"):
            return ("ground", max(8.0, 2.5 * size), 8.0, self.turn_dps)
        fl = v.flight(v.orbit.body.reference_frame)
        radar = fl.surface_altitude
        atmosphere = v.orbit.body.has_atmosphere and fl.mean_altitude < v.orbit.body.atmosphere_depth
        if radar < 3000.0 and fl.vertical_speed < -0.5:
            return ("landing", max(10.0, 2.5 * size), 4.0, self.turn_dps * 0.5)
        if atmosphere:
            return ("atmosphere", max(10.0, 2.0 * size), 6.0, self.turn_dps * 0.75)
        return ("space", max(15.0, 3.0 * size), 18.0, self.turn_dps)


class Session:
    """Recording + marks + director, started and stopped together."""

    def __init__(self, folder: Path, *, director: bool = True, **record_opts: Any) -> None:
        self.folder = Path(folder)
        self.record_opts = record_opts
        self.director = Director() if director and krpc is not None else None
        self.started: dict = {}

    def __enter__(self) -> "Session":
        self.started = start(self.folder, **self.record_opts)
        install_marks()
        if self.director:
            self.director.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self, *, stop_recorder: bool = True) -> dict:
        """Stop the director and the recording; {"error": ...} when the stop failed.

        stop_recorder=False when the bridge is gone (KSP quit): the recorder stopped with the game.
        """
        try:
            if self.director:
                self.director.stop()  # it finishes its tick while the recorder stops
            result: dict = {"note": "bridge gone; the recorder stopped with KSP"}
            if stop_recorder:
                try:
                    result = stop()
                except Exception as e:  # noqa: BLE001
                    result = {"error": str(e)}
            if self.director and self.director.is_alive():
                self.director.join(timeout=5.0)
            return result
        finally:
            _clear_marker()
