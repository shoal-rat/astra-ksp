"""Offline regression tests for the GILLY flag-plant mission (tools/eve_flag_mission.py).

These pin the parts that run with NO live KSP connection:
  * the Gilly excursion Δv GATE — the round-trip budget must FIT the ferry's residual Δv (else the run
    refuses to fly, so the crew is never stranded dry on the way back from Gilly);
  * Gilly is a real catalogue body the planner can use (constants present in bodies.py);
  * the PLANT-FLAG PAUSE release logic — it must release on a sentinel file, on a kerbal-back-aboard
    detection, and on the timeout, and must report whether the crew is aboard at release.

The live flight (transfer/land/ascend/dock/return) is exercised only in-game; it is not unit-tested here.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import eve_flag_mission as efm  # noqa: E402
from ksp_lab.bodies import EVE, GILLY  # noqa: E402


# --------------------------------------------------------------------------------------------------
# Gilly Δv GATE — the excursion must fit the ferry's leftover, with the documented ~682 m/s margin.
# --------------------------------------------------------------------------------------------------
def test_gilly_excursion_fits_the_ferry_residual():
    fits, b = efm.gilly_excursion_fits_ferry()
    assert fits is True
    # The round trip is dominated by the Hohmann out + recirc back, ~2482 m/s total (NOT the naive
    # "Gilly gravity is tiny" few-hundred). Generous bounds guard the intent without pinning the value.
    assert 2000.0 < b["total"] < 3000.0
    # The leftover (3164) minus the need leaves a positive margin in the ~500-900 m/s band.
    margin = 3164.0 - b["total"]
    assert 400.0 < margin < 900.0


def test_gate_refuses_when_the_excursion_does_not_fit():
    # If the ferry had far less leftover (e.g. a costlier live capture), the gate must say "does not fit".
    fits_small, _ = efm.gilly_excursion_fits_ferry(ferry_leftover_mps=1000.0)
    assert fits_small is False


def test_gilly_is_a_real_catalogue_body_orbiting_eve():
    # The Eve-orbit->Gilly transfer needs Gilly in the catalogue with sane real constants (no src change
    # required if these are present). Gilly is a tiny airless moon of Eve.
    assert GILLY.parent == "Eve"
    assert GILLY.radius_m == 13_000.0
    assert GILLY.atmosphere_top_m == 0.0                 # airless — no aerobrake at Gilly
    assert GILLY.orbit_radius_m == 31_500_000.0          # orbits Eve far above the low-Eve parking orbit
    assert GILLY.surface_g < 0.1                          # trivial gravity -> trivial land + return
    # Surface escape ~36 m/s (sanity: a kerbal returns to orbit on essentially nothing).
    escape = (2.0 * GILLY.mu / GILLY.radius_m) ** 0.5
    assert 25.0 < escape < 45.0


# --------------------------------------------------------------------------------------------------
# PLANT-FLAG PAUSE — the manual-EVA wait must release on every intended condition.
# --------------------------------------------------------------------------------------------------
class _FakeVessel:
    def __init__(self, crew: int):
        self.crew_count = crew


class _FakeSC:
    """Just enough surface for wait_for_flag_plant to stop warp."""
    rails_warp_factor = 0
    physics_warp_factor = 0


def test_flag_pause_releases_on_sentinel_file(tmp_path):
    sentinel = tmp_path / "flag_done.txt"
    v = _FakeVessel(crew=1)

    def make_sentinel():
        time.sleep(0.3)
        sentinel.write_text("go", encoding="utf-8")

    threading.Thread(target=make_sentinel, daemon=True).start()
    t0 = time.monotonic()
    ok = efm.wait_for_flag_plant(_FakeSC(), v, total_s=10.0, poll_s=0.1, sentinel=sentinel)
    elapsed = time.monotonic() - t0
    assert ok is True                                    # crew aboard at release
    assert elapsed < 5.0                                 # released early on the sentinel, not the timeout


def test_flag_pause_releases_when_kerbal_reboards_after_eva(tmp_path):
    sentinel = tmp_path / "never_created.txt"
    v = _FakeVessel(crew=1)

    def eva_then_board():
        time.sleep(0.2)
        v.crew_count = 0                                 # kerbal steps out to plant the flag
        time.sleep(0.2)
        v.crew_count = 1                                 # kerbal boards back

    threading.Thread(target=eva_then_board, daemon=True).start()
    t0 = time.monotonic()
    ok = efm.wait_for_flag_plant(_FakeSC(), v, total_s=10.0, poll_s=0.1, sentinel=sentinel)
    assert ok is True
    assert time.monotonic() - t0 < 5.0
    assert v.crew_count == 1


def test_flag_pause_times_out_and_reports_crew_aboard(tmp_path):
    v = _FakeVessel(crew=1)
    ok = efm.wait_for_flag_plant(_FakeSC(), v, total_s=0.4, poll_s=0.1,
                                 sentinel=tmp_path / "absent.txt")
    assert ok is True                                    # timed out, but a kerbal is still aboard -> True


def test_flag_pause_returns_false_when_crew_not_aboard_at_release(tmp_path):
    v = _FakeVessel(crew=0)                               # kerbal still on EVA when the timer expires
    ok = efm.wait_for_flag_plant(_FakeSC(), v, total_s=0.4, poll_s=0.1,
                                 sentinel=tmp_path / "absent.txt")
    assert ok is False


def test_flag_pause_clears_a_stale_sentinel_before_waiting(tmp_path):
    # A leftover sentinel from a PRIOR run must not instantly release the pause; the function unlinks it
    # first and waits for a fresh signal (here it then times out with the crew aboard).
    sentinel = tmp_path / "stale.txt"
    sentinel.write_text("old", encoding="utf-8")
    v = _FakeVessel(crew=1)
    t0 = time.monotonic()
    ok = efm.wait_for_flag_plant(_FakeSC(), v, total_s=0.4, poll_s=0.1, sentinel=sentinel)
    # It must have waited the full (short) window, not returned in the first poll, proving the stale file
    # was cleared rather than treated as a fresh release.
    assert ok is True
    assert time.monotonic() - t0 >= 0.4
    assert not sentinel.exists()


# --------------------------------------------------------------------------------------------------
# Module hygiene — the new mission reuses the two-ship scaffold and the proven capture, no re-derivation.
# --------------------------------------------------------------------------------------------------
def test_reuses_proven_helpers_not_reinvented():
    import eve_two_ship_return as ts
    import mj_to_mun
    # The two-ship return path is reused verbatim (same function objects).
    assert efm.fly_tug_home is ts.fly_tug_home
    assert efm.rendezvous_and_dock is ts.rendezvous_and_dock
    assert efm.transfer_kerbal_to_tug is ts.transfer_kerbal_to_tug
    # The Gilly capture reuses the proven pure-retrograde capture.
    assert efm._retro_capture is mj_to_mun._retro_capture
    # The low-Eve parking altitude is shared with the two-ship orchestrator (ferry/tug co-orbital).
    assert efm.EVE_PARK_ALT_KM == ts.EVE_PARK_ALT_KM
    # Sanity: Eve is Gilly's primary, so the excursion is a moon transfer, not a heliocentric one.
    assert GILLY.parent == EVE.name
