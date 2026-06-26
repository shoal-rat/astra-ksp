"""Unit tests for the FAIL-FAST ascent abort predicate in tools/deploy_relay.py.

The launch loop used to poll for ~20 minutes waiting for apoapsis to reach the parking target even after
the ascent had already FAILED (vehicle broke up, fell back below the atmosphere, or crashed), logging
nothing while it sat "alive" — which stranded the heavy tug for 20 minutes. ``_ascent_has_failed`` is the
pure predicate that detects those real failure modes from a small rolling state-history, so the loop can
abort LOUDLY and immediately. These tests drive it with fake-vessel state sequences (no kRPC):

  * part-count collapse (break-up)        -> abort
  * decaying apoapsis + falling back        -> abort
  * landed/splashed after liftoff (crash)   -> abort
  * diverging powered ascent                -> abort
  * a normal climbing ascent                -> does NOT abort
  * a normal booster separation (part drop) -> does NOT abort

Each state dict carries: part_count, apoapsis_m, vertical_speed_mps, situation, engine_lit.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import deploy_relay  # noqa: E402

# A representative full-stack vehicle: ~49 parts at liftoff, ~3-part bare payload.
POST_STAGING = 49
PAYLOAD = 3
ATMO_TOP = 70_000.0
TARGET = 100_000.0


def _state(part_count, apoapsis_m, vspd, situation="Vessel.Situation.flying", engine_lit=True):
    return {
        "part_count": part_count,
        "apoapsis_m": apoapsis_m,
        "vertical_speed_mps": vspd,
        "situation": situation,
        "engine_lit": engine_lit,
    }


def _run(history):
    return deploy_relay._ascent_has_failed(
        history,
        post_staging_part_count=POST_STAGING,
        payload_part_count=PAYLOAD,
        atmosphere_top_m=ATMO_TOP,
        target_apoapsis_m=TARGET,
    )


# --- ABORT cases -----------------------------------------------------------------------------------

def test_part_count_collapse_aborts():
    # Vehicle broke up: part count drops from 49 to ~the bare payload (9 << 49) for 3 consecutive polls.
    history = [
        _state(49, 40_000, 120),
        _state(9, 40_500, 60),     # broke up
        _state(9, 38_000, -10),
        _state(9, 35_000, -40),
    ]
    failed, reason = _run(history)
    assert failed
    assert "broke up" in reason
    assert "9" in reason and "49" in reason


def test_decaying_apoapsis_falling_back_aborts():
    # Below the atmosphere, vertical speed negative, apoapsis decaying across consecutive polls: it is
    # falling back and cannot reach orbit. (The 69->66 km, vspd -83 case from the bug report.)
    history = [
        _state(POST_STAGING, 72_000, 5),     # was climbing
        _state(POST_STAGING, 69_000, -40),   # now decaying + descending + below atmosphere
        _state(POST_STAGING, 67_500, -70),
        _state(POST_STAGING, 66_000, -83),
    ]
    failed, reason = _run(history)
    assert failed
    assert "falling back" in reason
    assert "66" in reason


def test_landed_after_liftoff_aborts():
    # Crashed: situation reads landed/splashed after liftoff.
    history = [
        _state(POST_STAGING, 12_000, -50, situation="Vessel.Situation.flying"),
        _state(POST_STAGING, 2_000, -90, situation="Vessel.Situation.landed"),
        _state(POST_STAGING, 0, 0, situation="Vessel.Situation.landed"),
        _state(POST_STAGING, 0, 0, situation="Vessel.Situation.landed"),
    ]
    failed, reason = _run(history)
    assert failed
    assert "crashed" in reason


def test_splashed_after_liftoff_aborts():
    history = [
        _state(POST_STAGING, 0, 0, situation="Vessel.Situation.splashed"),
        _state(POST_STAGING, 0, 0, situation="Vessel.Situation.splashed"),
        _state(POST_STAGING, 0, 0, situation="Vessel.Situation.splashed"),
    ]
    failed, reason = _run(history)
    assert failed
    assert "crashed" in reason


def test_diverging_powered_ascent_aborts():
    # Above the atmosphere but apoapsis strictly losing ground under power, still far short of target
    # (e.g. pointed wrong / tumbling): never recovers to orbit.
    history = [
        _state(POST_STAGING, 60_000, 5, engine_lit=True),
        _state(POST_STAGING, 55_000, 2, engine_lit=True),
        _state(POST_STAGING, 50_000, 1, engine_lit=True),
        _state(POST_STAGING, 45_000, 0, engine_lit=True),
    ]
    failed, reason = _run(history)
    assert failed
    assert "diverging" in reason or "losing ground" in reason


# --- NON-abort cases (the success / normal paths must NOT trip) ------------------------------------

def test_normal_climbing_ascent_does_not_abort():
    # Apoapsis rising, positive vertical speed, full part count: a healthy ascent.
    history = [
        _state(POST_STAGING, 30_000, 200),
        _state(POST_STAGING, 50_000, 180),
        _state(POST_STAGING, 75_000, 150),
        _state(POST_STAGING, 95_000, 90),
    ]
    failed, reason = _run(history)
    assert not failed
    assert reason == ""


def test_normal_booster_separation_does_not_abort():
    # A controlled, expected staging drop (49 -> 30 parts) is well above the half-count collapse
    # threshold and apoapsis keeps rising — must NOT be mistaken for a break-up.
    history = [
        _state(49, 60_000, 150),
        _state(49, 65_000, 140),
        _state(30, 70_000, 120),   # booster dropped (expected), still climbing
        _state(30, 80_000, 100),
    ]
    failed, reason = _run(history)
    assert not failed


def test_coasting_above_atmosphere_does_not_abort():
    # Past the atmosphere, engine idle, apoapsis flat/slightly settling near target while coasting to the
    # circularise node: this is normal and must NOT abort (near-target guard + not below atmosphere).
    history = [
        _state(POST_STAGING, 101_000, 5, engine_lit=False),
        _state(POST_STAGING, 100_500, -2, engine_lit=False),
        _state(POST_STAGING, 100_200, -3, engine_lit=False),
    ]
    failed, reason = _run(history)
    assert not failed


def test_single_bad_frame_does_not_abort():
    # A single transient bad poll inside an otherwise healthy climb must NOT trip the abort — the streak
    # requirement rides out one-frame glitches.
    history = [
        _state(POST_STAGING, 50_000, 180),
        _state(9, 49_000, -5),         # one glitchy frame (part-count read blip / momentary dip)
        _state(POST_STAGING, 70_000, 150),
        _state(POST_STAGING, 90_000, 100),
    ]
    failed, reason = _run(history)
    assert not failed


def test_short_history_does_not_abort():
    # Fewer polls than the streak length: never enough evidence to abort.
    failed, reason = _run([_state(9, 40_000, -50)])
    assert not failed
    failed, reason = _run([])
    assert not failed
