"""Unit tests for AutomationRunner's editor -> FLIGHT launch sequence (no live KSP).

The automated runner used to do ``load_craft -> wait loadedSceneIsEditor -> launch -> wait
loadedSceneIsFlight``. That timed out LIVE because:

  1. ``load_craft`` populates the editor ship a few frames AFTER ``loadedSceneIsEditor`` flips True, so
     the launch fired against a still-empty editor and silently no-opped.
  2. ``launch()`` returns "Launch requested." even when the scene never transitions (a dropped reflected
     invoke, or KSP's pre-flight ``LaunchSiteClear`` failing because a vessel is already on the pad), and
     a heavy craft's editor->FLIGHT transition is slow.

The fix waits for the craft to be REALLY loaded (editor scene + ``lastCraftName`` == this craft +
``queueDepth`` drained + settle), gives the flight transition a generous timeout, and RE-ISSUES launch
once if the scene is still EDITOR after a grace window. These tests drive ``_load_and_launch`` /
``_wait_for_craft_ready`` with a scripted fake bridge (no HTTP, no kRPC) and assert that behavior.
"""
from __future__ import annotations

import pytest

from ksp_lab.runner import AutomationRunner


# Fast, deterministic config: tiny settles/polls so the tests run in well under a second, but the LOGIC
# (wait-for-craft-ready, relaunch retry, generous timeout) is exactly the production path.
FAST_RUNNER_CFG = {
    "post_load_settle_s": 0.0,
    "scene_poll_s": 0.0,
    "craft_ready_timeout_s": 0.2,
    "launch_transition_timeout_s": 0.2,
    "relaunch_after_s": 0.0,
    "scene_transition_timeout_s": 0.2,
}


def _runner() -> AutomationRunner:
    """An AutomationRunner with only the ``config`` the launch helpers read — no DB / disk side effects."""
    runner = AutomationRunner.__new__(AutomationRunner)
    runner.config = {"runner": dict(FAST_RUNNER_CFG)}
    return runner


class FakeBridge:
    """A scripted stand-in for BridgeClient.

    ``states`` is a queue of state dicts returned by successive ``state()`` calls (the last one repeats
    once exhausted). ``load_craft`` / ``launch`` record that they were called. ``on_launch`` (optional)
    is invoked each time ``launch()`` fires so a test can mutate the upcoming states (e.g. only transition
    to flight after the SECOND launch, modelling a silent first no-op)."""

    def __init__(self, states, on_launch=None):
        self._states = list(states)
        self.on_launch = on_launch
        self.load_calls: list[str] = []
        self.launch_calls = 0

    def load_craft(self, craft_name: str):
        self.load_calls.append(craft_name)
        return {"ok": True, "message": "Craft load requested."}

    def launch(self):
        self.launch_calls += 1
        if self.on_launch is not None:
            self.on_launch(self)
        return {"ok": True, "message": "Launch requested."}

    def state(self):
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0] if self._states else {}


def _editor(craft="AI-Mun-1", queue=0):
    return {
        "scene": "EDITOR",
        "loadedSceneIsEditor": True,
        "loadedSceneIsFlight": False,
        "lastCraftName": craft,
        "queueDepth": queue,
    }


def _flight(vessel="AI-Mun-1"):
    return {
        "scene": "FLIGHT",
        "loadedSceneIsEditor": False,
        "loadedSceneIsFlight": True,
        "activeVessel": vessel,
    }


# --- _wait_for_craft_ready ------------------------------------------------------------------------

def test_wait_for_craft_ready_blocks_until_craft_recorded():
    # The editor scene is up immediately, but the craft (lastCraftName) only matches after the load
    # finishes a frame later — and the command queue is still draining on the first poll.
    bridge = FakeBridge(states=[
        {"scene": "EDITOR", "loadedSceneIsEditor": True, "lastCraftName": "", "queueDepth": 1},
        {"scene": "EDITOR", "loadedSceneIsEditor": True, "lastCraftName": "stale", "queueDepth": 0},
        _editor("AI-Mun-1", queue=0),
    ])
    _runner()._wait_for_craft_ready(bridge, "AI-Mun-1")  # returns without raising


def test_wait_for_craft_ready_times_out_if_craft_never_loads():
    # Editor stays empty (wrong craft) forever -> must raise, naming the craft + last state.
    bridge = FakeBridge(states=[_editor("some-other-craft")])
    with pytest.raises(TimeoutError) as exc:
        _runner()._wait_for_craft_ready(bridge, "AI-Mun-1")
    assert "AI-Mun-1" in str(exc.value)


# --- _load_and_launch -----------------------------------------------------------------------------

def test_load_and_launch_reaches_flight_first_try():
    bridge = FakeBridge(states=[_editor(), _flight()])
    _runner()._load_and_launch(bridge, "AI-Mun-1")
    assert bridge.load_calls == ["AI-Mun-1"]
    assert bridge.launch_calls == 1


def test_load_and_launch_reissues_launch_on_silent_noop():
    # First launch silently no-ops (scene stays EDITOR); the second launch transitions to flight. With
    # relaunch_after_s=0 the retry fires on the first post-launch poll that still reads EDITOR.
    def on_launch(b: "FakeBridge"):
        if b.launch_calls >= 2:
            b._states = [_flight()]

    bridge = FakeBridge(
        states=[_editor(), _editor(), _editor()],  # craft-ready, then stays EDITOR until relaunch
        on_launch=on_launch,
    )
    _runner()._load_and_launch(bridge, "AI-Mun-1")
    assert bridge.launch_calls == 2  # re-issued exactly once


def test_load_and_launch_times_out_with_launchsite_hint():
    # Scene never leaves EDITOR (e.g. LaunchSiteClear FAIL — pad occupied). Must raise, mention the
    # LaunchSiteClear cause, and only relaunch once.
    bridge = FakeBridge(states=[_editor()])
    with pytest.raises(TimeoutError) as exc:
        _runner()._load_and_launch(bridge, "AI-Mun-1")
    msg = str(exc.value)
    assert "LaunchSiteClear" in msg
    assert "FLIGHT" in msg
    assert bridge.launch_calls == 2  # initial + one retry, never more


# --- _wait_for_bridge_state per-call timeout ------------------------------------------------------

def test_wait_for_bridge_state_respects_explicit_timeout():
    bridge = FakeBridge(states=[_editor()])  # never reaches flight
    with pytest.raises(TimeoutError):
        _runner()._wait_for_bridge_state(bridge, "loadedSceneIsFlight", True, timeout_s=0.05)
