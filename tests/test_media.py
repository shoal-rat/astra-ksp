"""Filming (astra.media, `astra record`, `astra mission --record`), tested offline with a fake bridge.

Nothing here talks to KSP: the bridge is a fake object, the marker file lives in a temp folder, and
the camera director gets a fake kRPC module.
"""

import argparse
import json
import os
import signal
import time
import types

import pytest

from astra import cli, media, registry
from astra.agent import runner
from astra.config import CONFIG
from astra.errors import BridgeError, NotConnected

GOAL = "Film a probe reaching orbit"


class FakeBridge:
    """Answers bridge requests from `replies` (path -> dict, exception, or callable)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.replies: dict[str, object] = {"/record/start": {"recording": True, "mode": "video", "width": 1728},
                                           "/record/mark": {"recording": True},
                                           "/record/stop": {"recording": False, "frames": 10, "output": "v.mkv"},
                                           "/record/status": {"recording": True}}

    def _answer(self, method: str, path: str, body: dict | None) -> dict:
        self.calls.append((method, path, body))
        reply = self.replies.get(path, {})
        if callable(reply):
            reply = reply()
        if isinstance(reply, Exception):
            raise reply
        return dict(reply)

    def get(self, path: str, timeout: float | None = None) -> dict:
        return self._answer("GET", path, None)

    def post(self, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        return self._answer("POST", path, body)

    def paths(self, method: str | None = None) -> list[str]:
        return [p for m, p, _ in self.calls if method in (None, m)]

    def labels(self) -> list[str]:
        return [b["label"] for _, p, b in self.calls if p == "/record/mark"]


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    fake = FakeBridge()
    monkeypatch.setattr(media, "_bridge", lambda: fake)
    monkeypatch.setattr(media, "MARKER_FILE", tmp_path / "cache" / "recording.json")
    monkeypatch.setattr(media, "HOLD_FILE", tmp_path / "cache" / "director.hold")
    monkeypatch.setattr(media, "_mute", {"marker": None, "until": 0.0, "failures": 0})
    monkeypatch.setattr(media, "_marks_installed", False)
    monkeypatch.setattr(media, "ffmpeg_exe", lambda: "C:/tools/ffmpeg.exe")
    monkeypatch.setattr(registry, "_START_HOOKS", [])  # hooks installed here stay in this test
    monkeypatch.setattr(registry, "_HOOKS", [])
    return fake


@pytest.fixture
def probe_tools():
    """Register throwaway tools; they are removed again after the test."""
    added: list[str] = []

    def add(name, fn, needs_game=False):
        registry.tool("compute", name=name, needs_game=needs_game)(fn)
        added.append(name)

    yield add
    for name in added:
        registry.TOOLS.pop(name, None)


def _add(a: int, b: int) -> dict:
    return {"sum": a + b}


def _boom(a: int) -> dict:
    raise ValueError("boom")


def _recording(bridge, tmp_path, name="rec") -> None:
    media.start(tmp_path / name)
    bridge.calls.clear()


# ---------------------------------------------------------------------------------------------
# registry hooks


def test_broken_hooks_never_break_a_tool_call(bridge, probe_tools):
    probe_tools("zz_media_add", _add)
    probe_tools("zz_media_boom", _boom)
    seen: list[tuple] = []

    def bad_start(name, args):
        raise RuntimeError("start hook broke")

    def bad_end(name, args, ok, payload, seconds):
        seen.append((name, ok, payload))
        raise RuntimeError("end hook broke")

    registry.add_start_hook(bad_start)
    registry.add_hook(bad_end)
    assert registry.call("zz_media_add", {"a": 1, "b": 2}) == {"sum": 3}
    with pytest.raises(ValueError, match="boom"):
        registry.call("zz_media_boom", {"a": 1})
    assert seen[0] == ("zz_media_add", True, {"sum": 3})
    assert seen[1][:2] == ("zz_media_boom", False) and isinstance(seen[1][2], ValueError)


# ---------------------------------------------------------------------------------------------
# tool-call marks


def test_idle_marks_make_no_bridge_call(bridge, probe_tools):
    probe_tools("zz_media_add", _add)
    media.install_marks()
    for _ in range(3):
        assert registry.call("zz_media_add", {"a": 1, "b": 2}) == {"sum": 3}
    media.mark("free text")
    assert bridge.calls == []  # no recording marker: not even a status probe


def test_marks_follow_tool_calls_while_a_recording_runs(bridge, probe_tools, tmp_path):
    probe_tools("zz_media_add", _add)
    probe_tools("zz_media_boom", _boom)
    media.install_marks()
    _recording(bridge, tmp_path)
    registry.call("zz_media_add", {"a": 1, "b": 2})
    with pytest.raises(ValueError):
        registry.call("zz_media_boom", {"a": 1})
    assert bridge.labels() == ["start:zz_media_add", "end:zz_media_add", "start:zz_media_boom", "end:zz_media_boom"]
    assert bridge.paths("GET") == []  # the mark itself is the probe
    ends = [b["detail"] for _, p, b in bridge.calls if b and b["label"].startswith("end:")]
    assert ends[1].startswith("error") and "boom" in ends[1]


def test_quiet_tools_are_not_marked(bridge, probe_tools, tmp_path):
    probe_tools("zz_media_add", _add)
    media.install_marks()
    _recording(bridge, tmp_path)
    media._on_start("telemetry", {})
    media._on_end("telemetry", {}, True, {}, 0.1)
    assert bridge.calls == []


def test_a_marker_the_bridge_shows_idle_is_muted_until_a_new_recording(bridge, tmp_path):
    _recording(bridge, tmp_path)
    bridge.replies["/record/mark"] = {"recording": False}  # stopped behind our back (low disk)
    media.mark("a")
    media.mark("b")
    assert bridge.labels() == ["a"]
    st = media.MARKER_FILE.stat()  # a new recording writes a new marker
    os.utime(media.MARKER_FILE, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    bridge.replies["/record/mark"] = {"recording": True}
    media.mark("c")
    media.mark("d")
    assert bridge.labels() == ["a", "c", "d"]


def test_a_failed_mark_mutes_marks_only_briefly(bridge, tmp_path):
    _recording(bridge, tmp_path)
    bridge.replies["/record/mark"] = NotConnected("main thread busy")
    media.mark("slow")
    assert 0.0 < media._mute["until"] - time.monotonic() <= 1.0  # one slow reply: about a second
    media.mark("skipped")
    assert bridge.labels() == ["slow"]
    for expected in (2.0, 4.0, 8.0):  # the bridge stays away: back off, so idle calls stay fast
        media._mute["until"] = 0.0
        media.mark("retry")
        assert expected - 0.5 < media._mute["until"] - time.monotonic() <= expected
    media._mute["failures"] = 20
    media._mute["until"] = 0.0
    media.mark("retry")
    assert media._mute["until"] - time.monotonic() <= 60.0
    bridge.replies["/record/mark"] = {"recording": True}
    media._mute["until"] = 0.0
    media.mark("back")
    media.mark("again")
    assert bridge.labels()[-2:] == ["back", "again"] and media._mute["failures"] == 0


def test_mark_hooks_swallow_any_error(bridge, tmp_path):
    _recording(bridge, tmp_path)
    bridge.replies["/record/mark"] = RuntimeError("unexpected")
    media._on_start("fly_burn", {"x": object()})
    media._on_end("fly_burn", {}, False, RuntimeError("tool failed"), 1.0)


# ---------------------------------------------------------------------------------------------
# start / stop / session


def test_start_writes_the_marker_and_stop_clears_it_even_when_stop_fails(bridge, tmp_path):
    out = media.start(tmp_path / "rec")
    body = bridge.calls[-1][2]
    assert body["ffmpeg"] == "C:/tools/ffmpeg.exe" and "warning" not in out
    assert json.loads(media.MARKER_FILE.read_text(encoding="utf-8"))["dir"] == str((tmp_path / "rec").resolve())
    bridge.replies["/record/stop"] = NotConnected("KSP quit")
    with pytest.raises(NotConnected):
        media.stop()
    assert not media.MARKER_FILE.exists()


def test_start_warns_when_it_falls_back_to_jpeg(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "ffmpeg_exe", lambda: None)
    out = media.start(tmp_path / "a")
    assert "ffmpeg" not in bridge.calls[-1][2] and "JPEG" in out["warning"]
    assert "warning" not in media.start(tmp_path / "b", video=False)  # JPEG asked for: no warning


def test_session_close_stops_the_recorder_and_clears_the_marker(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "krpc", types.SimpleNamespace(connect=None))  # a director that never ran
    session = media.Session(tmp_path / "rec")
    assert session.close()["frames"] == 10  # never started: no join on an unstarted thread
    with media.Session(tmp_path / "rec2", director=False) as s:
        assert media.MARKER_FILE.exists() and s.started is not None
    assert not media.MARKER_FILE.exists()
    bridge.calls.clear()
    s = media.Session(tmp_path / "rec3", director=False).__enter__()
    assert "note" in s.close(stop_recorder=False)
    assert "/record/stop" not in bridge.paths() and not media.MARKER_FILE.exists()
    bridge.replies["/record/stop"] = NotConnected("gone")
    s = media.Session(tmp_path / "rec4", director=False).__enter__()
    assert "gone" in s.close()["error"]


# ---------------------------------------------------------------------------------------------
# camera director


class _Enum:
    def __init__(self, text: str) -> None:
        self.text = text

    def __str__(self) -> str:
        return self.text


def _vessel(vtype="ship", situation="flying", radar=5000.0, altitude=5000.0, vs=0.0, atmosphere=True):
    ns = types.SimpleNamespace
    body = ns(has_atmosphere=atmosphere, atmosphere_depth=70000.0, reference_frame="body")
    flight = ns(surface_altitude=radar, mean_altitude=altitude, vertical_speed=vs)
    return ns(type=_Enum(f"VesselType.{vtype}"), situation=_Enum(f"VesselSituation.{situation}"),
              _object_id=7, parts=ns(all=[1, 2, 3]), reference_frame="vessel",
              orbit=ns(body=body), flight=lambda frame: flight)


@pytest.mark.parametrize("vessel, shot", [
    (_vessel(vtype="eva", situation="landed"), "eva"),
    (_vessel(situation="pre_launch"), "ground"),
    (_vessel(situation="splashed"), "ground"),
    (_vessel(radar=800.0, vs=-5.0), "landing"),
    (_vessel(radar=8000.0, altitude=8000.0, vs=-5.0), "atmosphere"),
    (_vessel(radar=90000.0, altitude=90000.0), "space"),
    (_vessel(radar=5000.0, altitude=5000.0, atmosphere=False), "space"),
])
def test_director_picks_the_shot_for_the_phase(vessel, shot, monkeypatch):
    import astra.telemetry

    monkeypatch.setattr(astra.telemetry, "vessel_box", lambda v, frame: ([0.0, 0.0, 0.0], [2.0, 10.0, 2.0]))
    director = media.Director()
    name, dist, pitch, turn = director._phase(types.SimpleNamespace(active_vessel=vessel), {})
    assert name == shot and dist > 0


class _Camera:
    def __init__(self) -> None:
        self.mode = _Enum("CameraMode.automatic")
        self.values = {"heading": 0.0, "pitch": 30.0, "distance": 50.0}
        self.reads = {"pitch": 0, "distance": 0}
        self.writes: list[tuple[str, float]] = []

    def __getattr__(self, name):
        if name in ("heading", "pitch", "distance"):
            if name in self.reads:
                self.reads[name] += 1
            return self.values[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in ("heading", "pitch", "distance"):
            self.values[name] = value
            self.writes.append((name, value))
        else:
            object.__setattr__(self, name, value)


def _fake_krpc(paused: bool):
    cam = _Camera()
    conn = types.SimpleNamespace(krpc=types.SimpleNamespace(paused=paused, get_status=lambda: None),
                                 space_center=types.SimpleNamespace(camera=cam), close=lambda: None)
    connects: list[dict] = []
    module = types.SimpleNamespace(connect=lambda **kw: connects.append(kw) or conn)
    return module, cam, connects


def _run_director(director: media.Director, until, timeout_s: float = 5.0) -> None:
    director.start()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not until():
        time.sleep(0.01)
    director.stop()
    director.join(timeout=5.0)
    assert not director.is_alive()


def test_director_leaves_the_camera_alone_while_paused(bridge, monkeypatch):
    module, cam, connects = _fake_krpc(paused=True)
    monkeypatch.setattr(media, "krpc", module)
    director = media.Director(rate_hz=50.0)
    _run_director(director, until=lambda: False, timeout_s=0.3)
    assert connects and cam.writes == [] and director.errors == 0


def test_director_moves_smoothly_through_a_slow_tick(bridge, monkeypatch):
    module, cam, connects = _fake_krpc(paused=False)
    monkeypatch.setattr(media, "krpc", module)
    rate, turn = 50.0, 2.0
    director = media.Director(rate_hz=rate, turn_dps=turn)
    slow = {"first": True}

    def phase(sc, sizes):
        if slow["first"]:  # sizing a freshly staged vessel: one RPC per part
            slow["first"] = False
            time.sleep(0.3)
        return ("space", 20.0, 10.0, turn)

    monkeypatch.setattr(director, "_phase", phase)
    headings = lambda: [v for name, v in cam.writes if name == "heading"]  # noqa: E731
    _run_director(director, until=lambda: len(headings()) >= 8)
    steps = [b - a for a, b in zip([0.0] + headings(), headings())]
    assert len(steps) >= 8 and max(steps) <= turn * 2.0 / rate + 1e-9  # no catch-up jump after the slow tick
    assert cam.reads["pitch"] == len(headings()) and cam.reads["distance"] == len(headings())  # one read a tick
    assert connects[0]["stream_port"] is None


# ---------------------------------------------------------------------------------------------
# astra record


def _record_ns(tmp_path, **kw) -> argparse.Namespace:
    base = dict(status=False, stop=False, dir=str(tmp_path / "rec"), no_director=True, fps=15.0, width=1728,
                height=None, crf=20, no_ui=False, jpeg=False)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


def _last_json(text: str) -> dict:
    return json.loads(text.strip().splitlines()[-1])


def test_record_stops_when_the_recording_ends_elsewhere(bridge, tmp_path, no_sleep, capsys):
    handler = signal.getsignal(signal.SIGINT)
    bridge.replies["/record/status"] = {"recording": False}  # astra record --stop, or low disk
    assert cli.cmd_record(_record_ns(tmp_path)) == 0
    out = _last_json(capsys.readouterr().out)
    assert out["stopped"]["frames"] == 10 and out["bridge_gone"] is False
    assert bridge.paths("POST") == ["/record/start", "/record/stop"] and not media.MARKER_FILE.exists()
    assert signal.getsignal(signal.SIGINT) is handler


def test_record_gives_up_when_ksp_goes_away(bridge, tmp_path, no_sleep, capsys, monkeypatch):
    monkeypatch.setattr(cli, "RECORD_BRIDGE_GONE_S", 0.0)
    bridge.replies["/record/status"] = NotConnected("bridge unreachable")
    assert cli.cmd_record(_record_ns(tmp_path)) == 0
    out = _last_json(capsys.readouterr().out)
    assert out["bridge_gone"] is True and "/record/stop" not in bridge.paths()  # the recorder stopped with KSP
    assert not media.MARKER_FILE.exists()


def test_record_rides_out_a_short_bridge_outage(bridge, tmp_path, no_sleep, capsys):
    replies = iter([NotConnected("restarting"), {"recording": True}, {"recording": False}])
    bridge.replies["/record/status"] = lambda: next(replies)
    assert cli.cmd_record(_record_ns(tmp_path)) == 0
    assert _last_json(capsys.readouterr().out)["bridge_gone"] is False
    assert bridge.paths("GET").count("/record/status") == 3


def test_an_interrupt_while_starting_still_stops_the_recorder(bridge, tmp_path, no_sleep, capsys):
    handlers: list = []

    def started_then_ctrl_c():
        handlers.extend(signal.getsignal(s) for s in (signal.SIGINT, getattr(signal, "SIGBREAK", signal.SIGINT),
                                                      signal.SIGTERM))
        handlers[0](signal.SIGINT, None)  # Ctrl-C lands during POST /record/start
        return {"recording": True, "mode": "video"}

    bridge.replies["/record/start"] = started_then_ctrl_c
    assert cli.cmd_record(_record_ns(tmp_path)) == 0
    assert bridge.paths() == ["/record/start", "/record/stop"]
    assert handlers[0] is handlers[1] is handlers[2]  # Ctrl-Break and termination stop it the same way


def test_record_reports_bridge_errors_without_a_traceback(bridge, tmp_path, no_sleep, capsys):
    handler = signal.getsignal(signal.SIGINT)
    bridge.replies["/record/start"] = BridgeError("bridge /record/start failed: Already recording into X.")
    assert cli.cmd_record(_record_ns(tmp_path)) == 2
    assert "Already recording" in capsys.readouterr().err
    assert "/record/stop" not in bridge.paths()  # not ours to stop
    assert signal.getsignal(signal.SIGINT) is handler
    bridge.replies["/record/status"] = NotConnected("bridge unreachable")
    assert cli.cmd_record(_record_ns(tmp_path, status=True)) == 2
    assert "ERROR" in capsys.readouterr().err


def test_record_stop_clears_the_marker(bridge, tmp_path, capsys):
    media.start(tmp_path / "rec")
    assert cli.cmd_record(_record_ns(tmp_path, stop=True)) == 0
    assert not media.MARKER_FILE.exists()


# ---------------------------------------------------------------------------------------------
# astra mission --record


@pytest.fixture
def mission_env(tmp_path, monkeypatch):
    old = CONFIG.missions_dir
    object.__setattr__(CONFIG, "missions_dir", tmp_path / "missions")

    async def fly(goal, options, first, max_turns, budget, console, transcript):
        return 1, None

    monkeypatch.setattr(runner, "game_reachable", lambda: (True, True))
    monkeypatch.setattr(runner, "detach_from_parent_session", lambda: [])
    monkeypatch.setattr(runner, "leave_game_paused", lambda: "game paused")
    monkeypatch.setattr(runner, "_fly", fly)
    yield
    object.__setattr__(CONFIG, "missions_dir", old)


def _filming(close):
    class Filming:
        def __init__(self, folder, **kw):
            self.started: dict = {}

        def __enter__(self):
            self.started = {"mode": "jpeg", "width": 2, "height": 2, "warning": "no ffmpeg found"}
            return self

        def close(self):
            return close()

    return Filming


def test_mission_files_the_transcript_even_if_stopping_the_recorder_is_interrupted(
        mission_env, tmp_path, monkeypatch, capsys):
    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(media, "Session", _filming(interrupted))
    assert runner.run_mission(GOAL, record=str(tmp_path / "rec")) == 1
    out = capsys.readouterr().out
    assert "recording: no ffmpeg found" in out
    assert "stop failed (interrupted" in out and "transcript:" in out


def test_mission_prints_why_the_recorder_did_not_stop(mission_env, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(media, "Session", _filming(lambda: {"error": "KSP bridge unreachable"}))
    assert runner.run_mission(GOAL, record=str(tmp_path / "rec")) == 1
    out = capsys.readouterr().out
    assert "recording: stop failed (KSP bridge unreachable)" in out and "None frames" not in out
