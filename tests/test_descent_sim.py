"""Closed-loop landings of the real descent guidance in the offline simulator (tests/descent_sim.py).

The approach reproduces the Mun showcase flights: a retrograde equatorial pass from a ~44 km orbit
deorbited to a -10 km periapsis, handed to fly_descent at 14 km (565 m/s horizontal, -69 m/s
vertical), over terrain that rises under the braking zone.
"""

import math

import pytest

from tests.descent_sim import Body, Lander, Sim, guidance


def rising(lon: float) -> float:
    """1850 m plains rising 300 m across the braking zone (westward travel), with 40 m bumps."""
    x = min(1.0, max(0.0, (-lon - 18.0) / 10.0))
    return 1850.0 + 300.0 * (3 * x * x - 2 * x ** 3) + 40.0 * math.sin(lon * 7.0)


def ridge(lon: float) -> float:
    """Plains with a 700 m ridge right under the late braking path."""
    return 1850.0 + 700.0 * math.exp(-((lon + 26.0) / 1.2) ** 2)


MUN = dict(radius_m=200_000.0, surface_g=1.63)
APPROACH = dict(altitude_m=14_000.0, lon_deg=0.0, horizontal_mps=565.0, vertical_mps=-69.0)


def fly(terrain, mass_kg: float, **guide_overrides):
    body = Body(terrain=terrain, **MUN)
    lander = Lander(mass_kg=mass_kg, thrust_n=60_000.0, propellant_kg=mass_kg * 0.55)
    return Sim(body, lander, **APPROACH).run(guidance(**guide_overrides))


@pytest.mark.parametrize("mass_kg, label", [
    (4_465.0, "Lander 2, TWR ~8"),
    (6_770.0, "Lander 3, TWR ~5.4 (crashed with the constant-deceleration model)"),
    (9_000.0, "heavy lander, TWR ~4.1"),
])
def test_soft_landing_over_rising_terrain(mass_kg, label):
    r = fly(rising, mass_kg)
    assert r.contact, label
    assert abs(r.vertical_mps) <= 2.5, (label, r)
    assert r.horizontal_mps <= 1.0, (label, r)
    phases = [p for _, p in r.phase_log]
    assert phases[:3] == ["coast", "brake", "terminal"], (label, r.phase_log)


def test_soft_landing_over_a_ridge():
    r = fly(ridge, 6_770.0)
    assert r.soft, r


def test_more_reserve_starts_braking_higher():
    early = fly(rising, 6_770.0, reserve=0.35)
    late = fly(rising, 6_770.0, reserve=0.10)
    t_brake = lambda r: next(t for t, p in r.phase_log if p == "brake")  # noqa: E731
    assert t_brake(early) < t_brake(late)
    assert early.soft and late.soft


def flat(lon: float) -> float:
    return 0.0


@pytest.mark.parametrize("altitude_m, hs, vs, mass_kg", [
    (2_000.0, 450.0, -69.0, 6_770.0),    # late hand-off at TWR ~5.4
    (10_000.0, 565.0, -120.0, 15_000.0),  # steep, heavy: TWR ~2.45
    (2_000.0, 565.0, 0.0, 15_000.0),      # grazing level pass, TWR ~2.45
])
def test_late_or_low_twr_handoff_lands_height_first(altitude_m, hs, vs, mass_kg):
    """Regression (adversarial review, 2026-09-30): when even a full-thrust retrograde burn cannot hold
    the gate, surface-retrograde braking crashed with most of the propellant left; the brake must fly
    height-first and hand over to the terminal law only once it is slow."""
    body = Body(terrain=flat, **MUN)
    lander = Lander(mass_kg=mass_kg, thrust_n=60_000.0, propellant_kg=mass_kg * 0.55)
    r = Sim(body, lander, altitude_m=altitude_m, lon_deg=0.0, horizontal_mps=hs, vertical_mps=vs).run(guidance())
    assert r.soft, r


def test_duna_like_entry_handoff():
    """A Duna-weight lander (TWR ~3 at g 2.94) handed over at 6 km and 260 m/s after aerobraking."""
    body = Body(terrain=flat, radius_m=320_000.0, surface_g=2.94)
    lander = Lander(mass_kg=6_800.0, thrust_n=60_000.0, propellant_kg=3_500.0)
    r = Sim(body, lander, altitude_m=6_000.0, lon_deg=0.0, horizontal_mps=200.0, vertical_mps=-160.0).run(guidance())
    assert r.soft, r


def late_handoff(slew_dps, pointing_off_deg, **guide_overrides):
    """2 km, 450 m/s, -69 m/s over flat ground at TWR ~5.4: braking must start at once."""
    body = Body(terrain=flat, **MUN)
    lander = Lander(mass_kg=6_770.0, thrust_n=60_000.0, propellant_kg=6_770.0 * 0.55, slew_dps=slew_dps)
    sim = Sim(body, lander, altitude_m=2_000.0, lon_deg=0.0, horizontal_mps=450.0, vertical_mps=-69.0,
              pointing_off_deg=pointing_off_deg)
    guide = guidance(**guide_overrides)
    return sim.run(guide), guide


@pytest.mark.parametrize("slew_dps, pointing_off_deg", [(15.0, 180.0), (10.0, 120.0)])
def test_mispointed_late_handoff_holds_the_brake_until_turned(slew_dps, pointing_off_deg):
    """Regression (review 2026-09-30): handed over still in its deorbit attitude, the lander lit the
    brake at once and burned the wrong way while the autopilot turned it (impact at -116 m/s from
    180 deg at 15 deg/s). The brake now waits for the turn."""
    r, guide = late_handoff(slew_dps, pointing_off_deg)
    assert r.soft, r
    assert r.wrong_way_throttle_s == 0.0, r
    assert guide.brake_held_s > 1.0  # the gate did the work: the guidance wanted full thrust at once


@pytest.mark.parametrize("slew_dps", [10.0, 15.0, 30.0])
def test_pointing_gate_does_not_starve_an_aligned_lander(slew_dps):
    """A slow-turning lander lags the braking command as it swings; once aligned it keeps full thrust
    (gating on that lag crashed aligned landers in the slew-limited simulator)."""
    r, _ = late_handoff(slew_dps, 0.0)
    assert r.soft, r
    body = Body(terrain=rising, **MUN)
    lander = Lander(mass_kg=6_770.0, thrust_n=60_000.0, propellant_kg=6_770.0 * 0.55, slew_dps=slew_dps)
    r = Sim(body, lander, pointing_off_deg=180.0, **APPROACH).run(guidance())
    assert r.soft, r  # a high hand-off turns while it coasts


def test_touchdown_settles_on_sas_with_the_engine_off():
    r = fly(rising, 6_770.0, legs_alt_m=40.0)
    assert r.soft, r
    assert r.touchdown_reported is not None
    assert r.touchdown_reported["vertical_mps"] == pytest.approx(r.vertical_mps)
    assert r.throttle_after_contact == 0.0
    settle_s = guidance().settle_s
    assert r.done_s is not None and settle_s <= r.done_s - r.time_s < settle_s + 0.2, r
    assert r.done_detail.startswith("touchdown")
    assert not r.autopilot_engaged and r.sas
    assert "SAS holding the settled attitude" in r.attitude_report
    assert r.legs_h_m is not None and 39.0 < r.legs_h_m <= 40.0
