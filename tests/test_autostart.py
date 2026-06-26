"""Fully OFFLINE tests for the autonomous KSP startup state machine.

Every external effect is injected with a fake:
  * the bridge socket probe never touches a real socket,
  * the Steam launch never spawns a process,
  * the bridge client is faked (load_save is checked, never HTTP'd) — except one test that mocks the
    HTTP layer of the REAL BridgeClient to prove load_save posts the right path/body.

Nothing here launches Steam or restarts KSP.
"""

from __future__ import annotations

import json

import pytest

from ksp_lab.astra import autostart
from ksp_lab.astra.autostart import (
    BRIDGE_HOST,
    BRIDGE_PORT,
    STEAM_RUN_URI,
    ReadyState,
    ensure_ksp_ready,
    permissions_manifest,
)
from ksp_lab.bridge_client import BridgeClient, BridgeError


# ----------------------------------------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------------------------------------

def _config(tmp_path, *, with_exe=True):
    """A config dict pointing paths.ksp_install at a temp dir that does/doesn't contain the exe."""
    install = tmp_path / "ksp"
    install.mkdir()
    if with_exe:
        (install / autostart.KSP_EXE_NAME).write_text("stub", encoding="utf-8")
    return {
        "paths": {"ksp_install": str(install)},
        "bridge": {"base_url": f"http://{BRIDGE_HOST}:{BRIDGE_PORT}", "timeout_s": 30},
    }


class FakeProbe:
    """Socket probe that returns the next value from a scripted sequence (last value repeats)."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def __call__(self, host, port, timeout):
        self.calls += 1
        assert host == BRIDGE_HOST and port == BRIDGE_PORT
        idx = min(self.calls - 1, len(self._results) - 1)
        return self._results[idx]


class FakeLauncher:
    def __init__(self):
        self.uris = []

    def __call__(self, uri):
        self.uris.append(uri)


class FakeBridge:
    def __init__(self, base_url, timeout_s, *, result=None, error=None):
        self.base_url = base_url
        self.timeout_s = timeout_s
        self._result = result or {"ok": True, "scene": "spacecenter", "saveFolder": "默认"}
        self._error = error
        self.load_save_calls = []

    def load_save(self, save_name="persistent", *, timeout=120):
        self.load_save_calls.append((save_name, timeout))
        if self._error is not None:
            raise self._error
        return self._result


class FakeClock:
    """Monotonic clock that advances by a fixed step on every read, so timeout loops terminate."""

    def __init__(self, step=1.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        now = self.t
        self.t += self.step
        return now


def _no_sleep(_seconds):  # pragma: no cover - trivial
    pass


# ----------------------------------------------------------------------------------------------------
# locate
# ----------------------------------------------------------------------------------------------------

def test_locate_raises_when_exe_missing(tmp_path):
    cfg = _config(tmp_path, with_exe=False)
    with pytest.raises(FileNotFoundError) as ei:
        ensure_ksp_ready(cfg, socket_probe=FakeProbe([True]), launcher=FakeLauncher(),
                         bridge_factory=lambda u, t: FakeBridge(u, t))
    assert autostart.KSP_EXE_NAME in str(ei.value)


def test_locate_falls_back_to_default_path(tmp_path, monkeypatch):
    # No paths.ksp_install -> uses DEFAULT_KSP_INSTALL. Point that default at a temp dir with the exe.
    install = tmp_path / "default-ksp"
    install.mkdir()
    (install / autostart.KSP_EXE_NAME).write_text("stub", encoding="utf-8")
    monkeypatch.setattr(autostart, "DEFAULT_KSP_INSTALL", str(install))
    exe = autostart.locate_ksp({"paths": {}})
    assert exe.name == autostart.KSP_EXE_NAME


# ----------------------------------------------------------------------------------------------------
# already-running short-circuit
# ----------------------------------------------------------------------------------------------------

def test_already_running_short_circuits_without_launch(tmp_path):
    probe = FakeProbe([True])  # bridge already up on the very first probe
    launcher = FakeLauncher()
    state = ensure_ksp_ready(
        _config(tmp_path),
        socket_probe=probe,
        launcher=launcher,
        bridge_factory=lambda u, t: FakeBridge(u, t),
        sleep=_no_sleep,
        clock=FakeClock(),
    )
    assert isinstance(state, ReadyState)
    assert state.running and state.bridge_up and state.ok
    assert state.save_loaded is False  # no save_name was requested
    assert launcher.uris == []  # NEVER launched a second copy
    assert probe.calls == 1


def test_already_running_loads_save_when_requested(tmp_path):
    probe = FakeProbe([True])
    bridge = FakeBridge("x", 1)
    state = ensure_ksp_ready(
        _config(tmp_path),
        save_name="默认",
        socket_probe=probe,
        launcher=FakeLauncher(),
        bridge_factory=lambda u, t: bridge,
        sleep=_no_sleep,
        clock=FakeClock(),
    )
    assert state.save_loaded is True
    assert state.save_result == {"ok": True, "scene": "spacecenter", "saveFolder": "默认"}
    assert bridge.load_save_calls == [("默认", 120)]


# ----------------------------------------------------------------------------------------------------
# not running -> launch -> poll -> load
# ----------------------------------------------------------------------------------------------------

def test_not_running_launches_polls_then_loads_save(tmp_path):
    # First probe False (so we launch), then False again (still booting), then True (bridge up).
    probe = FakeProbe([False, False, True])
    launcher = FakeLauncher()
    bridge = FakeBridge("x", 1)
    state = ensure_ksp_ready(
        _config(tmp_path),
        save_name="persistent",
        socket_probe=probe,
        launcher=launcher,
        bridge_factory=lambda u, t: bridge,
        sleep=_no_sleep,
        clock=FakeClock(step=1.0),
        launch_timeout_s=60.0,
        poll_interval_s=2.0,
    )
    assert launcher.uris == [STEAM_RUN_URI]  # launched exactly once via the Steam app-id URI
    assert state.running and state.bridge_up and state.save_loaded
    assert bridge.load_save_calls == [("persistent", 120)]
    assert "save 'persistent' loaded" in state.detail


def test_not_running_launch_then_up_without_save(tmp_path):
    probe = FakeProbe([False, True])
    launcher = FakeLauncher()
    state = ensure_ksp_ready(
        _config(tmp_path),
        socket_probe=probe,
        launcher=launcher,
        bridge_factory=lambda u, t: FakeBridge(u, t),
        sleep=_no_sleep,
        clock=FakeClock(),
    )
    assert state.bridge_up and not state.save_loaded
    assert launcher.uris == [STEAM_RUN_URI]


# ----------------------------------------------------------------------------------------------------
# launch timeout
# ----------------------------------------------------------------------------------------------------

def test_launch_timeout_returns_not_running(tmp_path):
    # Bridge never comes up. The FakeClock advances 1s per read; with launch_timeout_s=3 the loop
    # exits after the deadline and we get a clear not-running ReadyState.
    probe = FakeProbe([False])
    launcher = FakeLauncher()
    state = ensure_ksp_ready(
        _config(tmp_path),
        save_name="persistent",
        socket_probe=probe,
        launcher=launcher,
        bridge_factory=lambda u, t: FakeBridge(u, t),
        sleep=_no_sleep,
        clock=FakeClock(step=1.0),
        launch_timeout_s=3.0,
        poll_interval_s=2.0,
    )
    assert state.running is False
    assert state.bridge_up is False
    assert state.save_loaded is False
    assert not state.ok
    assert "did not come up" in state.detail
    assert launcher.uris == [STEAM_RUN_URI]  # we did try to launch


def test_load_save_failure_is_reported_not_raised(tmp_path):
    probe = FakeProbe([True])
    bridge = FakeBridge("x", 1, error=BridgeError("boom"))
    state = ensure_ksp_ready(
        _config(tmp_path),
        save_name="persistent",
        socket_probe=probe,
        launcher=FakeLauncher(),
        bridge_factory=lambda u, t: bridge,
        sleep=_no_sleep,
        clock=FakeClock(),
    )
    assert state.bridge_up is True  # bridge was up
    assert state.save_loaded is False  # but the load failed
    assert "failed" in state.detail


# ----------------------------------------------------------------------------------------------------
# load_save posts the right path/body — mock the REAL BridgeClient HTTP layer
# ----------------------------------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_load_save_posts_correct_path_and_body(monkeypatch):
    captured = {}

    def fake_request(method, url, timeout=None, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["timeout"] = timeout
        captured["data"] = kwargs.get("data")
        return _FakeResponse({"ok": True, "scene": "spacecenter", "saveFolder": "默认"})

    monkeypatch.setattr("ksp_lab.bridge_client.requests.request", fake_request)

    client = BridgeClient(base_url=f"http://{BRIDGE_HOST}:{BRIDGE_PORT}", timeout_s=30)
    result = client.load_save("默认", timeout=99)

    assert result["ok"] is True
    assert captured["method"] == "POST"
    assert captured["url"] == f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/load-save"
    assert captured["timeout"] == 99  # per-call timeout override honored
    body = json.loads(captured["data"].decode("utf-8"))
    assert body == {"saveName": "默认"}


def test_reload_save_still_targets_old_endpoint(monkeypatch):
    captured = {}

    def fake_request(method, url, timeout=None, **kwargs):
        captured["url"] = url
        captured["data"] = kwargs.get("data")
        return _FakeResponse({"ok": True})

    monkeypatch.setattr("ksp_lab.bridge_client.requests.request", fake_request)
    client = BridgeClient(base_url=f"http://{BRIDGE_HOST}:{BRIDGE_PORT}", timeout_s=30)
    client.reload_save("默认", scene="flight")
    assert captured["url"].endswith("/save/load")
    assert json.loads(captured["data"].decode("utf-8")) == {"saveFolder": "默认", "scene": "flight"}


# ----------------------------------------------------------------------------------------------------
# permissions manifest
# ----------------------------------------------------------------------------------------------------

def test_permissions_manifest_documents_grant():
    manifest = permissions_manifest()
    assert "granted_by" in manifest
    assert any("220200" in line for line in manifest["allowed"])  # launch the right Steam app
    assert any("load-save" in line.lower() or "load a named save" in line.lower()
               for line in manifest["allowed"])
    # It must be an honest manifest: forbidden actions are spelled out too.
    assert any("non-localhost" in line.lower() for line in manifest["forbidden"])
    # Returned copy must be independent (mutating it can't corrupt the module constant).
    manifest["allowed"].append("tampered")
    assert "tampered" not in permissions_manifest()["allowed"]
