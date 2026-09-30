"""Compute tools through the registry, offline: a fixture body cache for the pure calculators and a
small fake of kRPC's vessel / orbit / maneuver-node API (driven by the same Kepler math) for the
live planners, the node search, and terrain sampling."""

import contextlib
import json
import math
import struct

import pytest

from astra import registry
from astra.errors import AstraError
from astra.physics import orbits
from astra.physics.orbits import KeplerOrbit, vadd, vcross, vdot, vnorm, vscale, vsub, vunit
from astra.tools import compute

# Body records exactly as compute._read_body stores them (read from KSP 1.12.5, profiles thinned).
BODIES = {'Sun': {'name': 'Sun',
                  'mu_m3ps2': 1.172332795e+18,
                  'radius_m': 261600000.0,
                  'soi_m': None,
                  'rotational_period_s': 432000.0,
                  'parent': None,
                  'satellites': ['Moho', 'Eve', 'Kerbin', 'Duna', 'Dres', 'Jool', 'Eeloo'],
                  'has_atmosphere': True,
                  'atmosphere_depth_m': 600000.0,
                  'surface_gravity_mps2': 17.13656476,
                  'has_solid_surface': False,
                  'atmosphere_profile': [[0.0, 16000.0, 0.0007249286157], [120000.0, 198.7070739, 1.141696973e-05],
                                         [240000.0, 157.475844, 7.209280081e-06], [360000.0, 90.09166062, 4.054189359e-06],
                                         [480000.0, 5.916431546, 2.620080628e-07], [600000.0, 0.0, 0.0]]},
          'Kerbin': {'name': 'Kerbin',
                     'mu_m3ps2': 3531600000000.0,
                     'radius_m': 600000.0,
                     'soi_m': 84159286.48,
                     'rotational_period_s': 21549.42518,
                     'parent': 'Sun',
                     'satellites': ['Mun', 'Minmus'],
                     'has_atmosphere': True,
                     'atmosphere_depth_m': 70000.0,
                     'surface_gravity_mps2': 9.813351144,
                     'has_solid_surface': True,
                     'atmosphere_profile': [[0.0, 101324.9969, 1.139922941], [14000.0, 8180.893898, 0.1311019255],
                                            [28000.0, 573.7024546, 0.008435972043], [42000.0, 58.42912197, 0.0007484589504],
                                            [56000.0, 5.214891396, 8.114461976e-05], [70000.0, 0.0, 0.0]],
                     'orbit': {'sma_m': 13599840260.0,
                               'eccentricity': 0.0,
                               'inclination_rad': 0.0,
                               'lan_rad': 0.0,
                               'argpe_rad': 0.0,
                               'mean_anomaly_at_epoch_rad': 3.140000105,
                               'epoch_s': 0.0}},
          'Mun': {'name': 'Mun',
                  'mu_m3ps2': 65138397520.0,
                  'radius_m': 200000.0,
                  'soi_m': 2429559.117,
                  'rotational_period_s': 138984.3766,
                  'parent': 'Kerbin',
                  'satellites': [],
                  'has_atmosphere': False,
                  'atmosphere_depth_m': 0.0,
                  'surface_gravity_mps2': 1.629016228,
                  'has_solid_surface': True,
                  'orbit': {'sma_m': 12000000.0,
                            'eccentricity': 0.0,
                            'inclination_rad': 0.0,
                            'lan_rad': 0.0,
                            'argpe_rad': 0.0,
                            'mean_anomaly_at_epoch_rad': 1.700000048,
                            'epoch_s': 0.0}},
          'Minmus': {'name': 'Minmus',
                     'mu_m3ps2': 1765800026.0,
                     'radius_m': 60000.0,
                     'soi_m': 2247428.388,
                     'rotational_period_s': 40400.0,
                     'parent': 'Kerbin',
                     'satellites': [],
                     'has_atmosphere': False,
                     'atmosphere_depth_m': 0.0,
                     'surface_gravity_mps2': 0.4906675645,
                     'has_solid_surface': True,
                     'orbit': {'sma_m': 47000000.0,
                               'eccentricity': 0.0,
                               'inclination_rad': 0.1047197551,
                               'lan_rad': 1.361356817,
                               'argpe_rad': 0.6632251158,
                               'mean_anomaly_at_epoch_rad': 0.8999999762,
                               'epoch_s': 0.0}},
          'Duna': {'name': 'Duna',
                   'mu_m3ps2': 301363212000.0,
                   'radius_m': 320000.0,
                   'soi_m': 47921949.37,
                   'rotational_period_s': 65517.85938,
                   'parent': 'Sun',
                   'satellites': ['Ike'],
                   'has_atmosphere': True,
                   'atmosphere_depth_m': 50000.0,
                   'surface_gravity_mps2': 2.94400546,
                   'has_solid_surface': True,
                   'atmosphere_profile': [[0.0, 6755.000114, 0.1333392381], [10000.0, 1797.130108, 0.04049061399],
                                          [20000.0, 240.9999967, 0.007278991206], [30000.0, 32.89720416, 0.001134231308],
                                          [40000.0, 4.917612299, 0.0001695496615], [50000.0, 0.0, 0.0]],
                   'orbit': {'sma_m': 20726155260.0,
                             'eccentricity': 0.05099999905,
                             'inclination_rad': 0.001047197528,
                             'lan_rad': 2.364921136,
                             'argpe_rad': 0.0,
                             'mean_anomaly_at_epoch_rad': 3.140000105,
                             'epoch_s': 0.0}},
          'Ike': {'name': 'Ike',
                  'mu_m3ps2': 18568368570.0,
                  'radius_m': 130000.0,
                  'soi_m': 1049598.939,
                  'rotational_period_s': 65517.86213,
                  'parent': 'Duna',
                  'satellites': [],
                  'has_atmosphere': False,
                  'atmosphere_depth_m': 0.0,
                  'surface_gravity_mps2': 1.099095362,
                  'has_solid_surface': True,
                  'orbit': {'sma_m': 3200000.0,
                            'eccentricity': 0.02999999933,
                            'inclination_rad': 0.003490658556,
                            'lan_rad': 0.0,
                            'argpe_rad': 0.0,
                            'mean_anomaly_at_epoch_rad': 1.700000048,
                            'epoch_s': 0.0}},
          'Eve': {'name': 'Eve',
                  'mu_m3ps2': 8171730229000.0,
                  'radius_m': 700000.0,
                  'soi_m': 85109364.74,
                  'rotational_period_s': 80500.0,
                  'parent': 'Sun',
                  'satellites': ['Gilly'],
                  'has_atmosphere': True,
                  'atmosphere_depth_m': 90000.0,
                  'surface_gravity_mps2': 16.68269741,
                  'has_solid_surface': True,
                  'atmosphere_profile': [[0.0, 506625.0, 6.172240243], [18000.0, 61639.38522, 1.1976654],
                                         [36000.0, 4923.890114, 0.126787178], [54000.0, 61.46151572, 0.001707756124],
                                         [72000.0, 3.094864078, 9.496083833e-05], [90000.0, 0.0, 0.0]],
                  'orbit': {'sma_m': 9832684544.0,
                            'eccentricity': 0.009999999776,
                            'inclination_rad': 0.03665191263,
                            'lan_rad': 0.2617993878,
                            'argpe_rad': 0.0,
                            'mean_anomaly_at_epoch_rad': 3.140000105,
                            'epoch_s': 0.0}}}


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """No game: the tools must fall back to the cache written here."""
    cache = tmp_path / "bodies.json"
    cache.write_text(json.dumps({"format": 1, "bodies": BODIES}), encoding="utf-8")
    monkeypatch.setattr(compute, "_live_link", lambda: None)
    monkeypatch.setattr(compute, "_cache_file", lambda: cache)
    monkeypatch.setattr(compute, "_terrain_file", lambda: tmp_path / "terrain.json")
    return cache


def call(name, **args):
    return registry.validate_and_call(name, args)


# ---------------------------------------------------------------------------------------------
# pure calculators on the cache


def test_calc_tool(offline):
    out = call("compute_calc", expression="dv = isp*g0*log(m0/m1); dv", variables={"isp": 320, "m0": 10, "m1": 5})
    assert out["value"] == pytest.approx(320 * 9.80665 * math.log(2))
    with pytest.raises(AstraError):
        call("compute_calc", expression="__import__('os').system('x')")


def test_orbit_tool_uses_cache_and_warns(offline):
    out = call("compute_orbit", body="kerbin", periapsis_alt_m=80_000, apoapsis_alt_m=80_000)
    assert out["periapsis"]["speed_mps"] == pytest.approx(2278.93, abs=0.05)
    assert out["period_s"] == pytest.approx(2 * math.pi * math.sqrt(680_000 ** 3 / 3.5316e12), rel=1e-6)
    assert out["source"].startswith("cache") and out["warnings"] == []
    low = call("compute_orbit", body="Kerbin", periapsis_alt_m=50_000, apoapsis_alt_m=100_000, at_alt_m=75_000)
    assert any("atmosphere" in w for w in low["warnings"])
    assert low["at_altitude"]["flight_path_angle_deg"] > 0
    with pytest.raises(AstraError, match="unknown body"):
        call("compute_orbit", body="Jupiter", periapsis_alt_m=1, apoapsis_alt_m=2)
    with pytest.raises(AstraError):
        call("compute_orbit", body="Kerbin", periapsis_alt_m=100_000, apoapsis_alt_m=80_000)


def test_hohmann_tool_to_the_mun(offline):
    out = call("compute_hohmann", body="Kerbin", from_alt_m=80_000, target_body="Mun", capture_alt_m=10_000)
    assert out["departure_burn_mps"] == pytest.approx(856.36, abs=0.1)
    assert out["phase_angle_deg"] == pytest.approx(110.88, abs=0.05)
    assert out["arrival"]["v_inf_mps"] == pytest.approx(364.8, abs=0.2)
    assert out["arrival"]["capture_dv_mps"] == pytest.approx(311.1, abs=0.3)
    plain = call("compute_hohmann", body="Kerbin", from_alt_m=80_000, to_alt_m=11_400_000)
    assert plain["departure_burn_mps"] == pytest.approx(out["departure_burn_mps"])
    with pytest.raises(AstraError, match="orbits"):
        call("compute_hohmann", body="Kerbin", from_alt_m=80_000, target_body="Ike")
    with pytest.raises(AstraError):
        call("compute_hohmann", body="Kerbin", from_alt_m=80_000, to_alt_m=1e6, capture_alt_m=1e4)


def test_rocket_tool_combinations(offline):
    out = call("compute_rocket", isp_s=345, mass_start_t=10, dv_mps=1000, thrust_kn=200, body="Kerbin")
    assert out["mass_end_t"] == pytest.approx(10 * math.exp(-1000 / (345 * 9.80665)))
    assert out["burn_time_s"] == pytest.approx(43.3, abs=0.1)
    assert out["twr_start"] == pytest.approx(200_000 / (10_000 * 9.81), rel=2e-3)
    need = call("compute_rocket", dv_mps=1000, mass_start_t=10, mass_end_t=7)
    assert need["isp_required_s"] == pytest.approx(1000 / (9.80665 * math.log(10 / 7)))
    assert call("compute_rocket", isp_s=300)["exhaust_velocity_mps"] == pytest.approx(300 * 9.80665)
    with pytest.raises(AstraError, match="nothing to compute"):
        call("compute_rocket", thrust_kn=100)


def test_descent_tool_mun(offline):
    out = call("compute_descent", body="Mun", altitude_m=3000, vertical_speed_mps=-40, horizontal_speed_mps=200,
               mass_t=4, thrust_kn=60, isp_s=320, reaction_time_s=1)
    g_here = 6.5138398e10 / (200_000 + 3000) ** 2  # gravity at the craft's altitude, not the surface
    assert out["twr_full"] == pytest.approx(60_000 / (4_000 * g_here), rel=1e-6)
    assert out["feasible"] and 0 < out["burn_start_altitude_m"] < 3000
    assert 0 < out["time_to_burn_start_s"] < out["free_fall_impact_s"]
    # coasting that long must leave just the altitude the burn needs from the new state
    t, g = out["time_to_burn_start_s"], out["gravity_mps2"]
    later = call("compute_descent", body="Mun", altitude_m=3000 - 40 * t - 0.5 * g * t * t,
                 vertical_speed_mps=-40 - g * t, horizontal_speed_mps=200, mass_t=4, thrust_kn=60, isp_s=320,
                 reaction_time_s=1)
    assert abs(later["margin_m"]) < 15
    weak = call("compute_descent", body="Mun", altitude_m=500, vertical_speed_mps=-20, horizontal_speed_mps=0,
                mass_t=10, thrust_kn=10)
    assert weak["feasible"] is False


def test_ascent_estimate_brackets_known_costs(offline):
    kerbin = call("compute_ascent_estimate", body="Kerbin", orbit_alt_m=80_000)
    lo, hi = kerbin["estimate_band_mps"]
    assert kerbin["ideal_dv_mps"] == pytest.approx(2398, abs=5)
    assert kerbin["ideal_dv_mps"] < lo < 3400 < hi
    assert "ESTIMATE" in kerbin["label"] and any("drag" in a for a in kerbin["assumptions"])
    one = call("compute_ascent_estimate", body="Kerbin", orbit_alt_m=80_000, liftoff_twr=1.6)
    assert len(one["by_twr"]) == 1 and one["estimate_band_mps"][0] < one["estimate_band_mps"][1]
    mun = call("compute_ascent_estimate", body="Mun", orbit_alt_m=15_000, liftoff_twr=3)
    assert 550 < mun["ideal_dv_mps"] == mun["estimate_band_mps"][0] < mun["estimate_band_mps"][1] < 800
    (offline.parent / "terrain.json").write_text(json.dumps({"Mun": {"max_m": 7000.0}}), encoding="utf-8")
    hilly = call("compute_ascent_estimate", body="Mun", orbit_alt_m=15_000, liftoff_twr=3)
    lo, hi = hilly["estimate_band_mps"]
    assert mun["ideal_dv_mps"] < lo < hi and hi > mun["estimate_band_mps"][1]  # clearing 7 km costs more
    polar = call("compute_ascent_estimate", body="Kerbin", orbit_alt_m=80_000, inclination_deg=90)
    assert polar["ideal_dv_mps"] > kerbin["ideal_dv_mps"] + 150  # no rotation credit going north
    with pytest.raises(AstraError):
        call("compute_ascent_estimate", body="Kerbin", orbit_alt_m=80_000, launch_latitude_deg=30, inclination_deg=10)


def test_transfer_window_offline(offline):
    with pytest.raises(AstraError, match="earliest_ut"):
        call("compute_transfer_window", origin="Kerbin", target="Duna", parking_alt_m=80_000, minimize="departure")
    out = call("compute_transfer_window", origin="Kerbin", target="Duna", parking_alt_m=80_000,
               minimize="total", arrival_alt_m=60_000, earliest_ut=0, include_porkchop=True)
    assert 750 < out["v_inf_departure_mps"] < 1100  # prograde (a retrograde arc would be ~20 km/s)
    assert 950 < out["ejection_dv_mps"] < 1200
    assert 0.5 * out["hohmann_reference"]["tof_s"] <= out["tof_s"] <= 1.5 * out["hohmann_reference"]["tof_s"]
    assert 20 < out["phase_angle_at_departure_deg"] < 70  # Duna leads Kerbin by ~44 deg for a Hohmann
    # outward transfer: v_inf ~ along Kerbin's motion, so the burn sits ~arccos(-1/e) ~ 150 deg behind prograde
    assert -175 < out["ejection"]["burn_angle_from_origin_prograde_deg"] < -125
    assert out["capture_dv_mps"] > 0 and out["source"].startswith("cache")
    assert len(out["porkchop"]["cost_mps"]) == 24
    with pytest.raises(AstraError, match="same parent"):
        call("compute_transfer_window", origin="Kerbin", target="Ike", parking_alt_m=80_000,
             minimize="departure", earliest_ut=0)


def test_no_cache_and_no_game_is_explained(tmp_path, monkeypatch):
    monkeypatch.setattr(compute, "_live_link", lambda: None)
    monkeypatch.setattr(compute, "_cache_file", lambda: tmp_path / "missing.json")
    with pytest.raises(AstraError, match="no body constants"):
        call("compute_orbit", body="Kerbin", periapsis_alt_m=80_000, apoapsis_alt_m=80_000)


def test_every_compute_tool_is_documented():
    specs = [s for s in registry.TOOLS.values() if s.group == "compute"]
    assert {s.name for s in specs} == {
        "compute_calc", "compute_orbit", "compute_hohmann", "compute_rocket", "compute_descent",
        "compute_ascent_estimate", "compute_maneuver", "compute_node_search", "compute_transfer_window",
        "compute_terrain"}
    for s in specs:
        assert s.summary and len(s.summary) < 140
        for p in s.signature.parameters.values():
            meta = getattr(p.annotation, "__metadata__", ())
            assert any(getattr(m, "description", None) for m in meta), f"{s.name}.{p.name} lacks a description"
        schema = registry.arg_model(s).model_json_schema()
        assert schema["type"] == "object"


# ---------------------------------------------------------------------------------------------
# a fake kRPC flight scene for the live planners


def _k(v):
    """element frame (right-handed, z up) -> kRPC axes"""
    return (v[0], v[2], v[1])


class FakeBody:
    def __init__(self, name, parent=None, rotation_period=None):
        rec = BODIES[name]
        self.name, self._rec = name, rec
        self.gravitational_parameter, self.equatorial_radius = rec["mu_m3ps2"], rec["radius_m"]
        self.sphere_of_influence = rec["soi_m"] or math.inf
        self.has_atmosphere, self.atmosphere_depth = rec["has_atmosphere"], rec["atmosphere_depth_m"]
        self.rotational_period = rotation_period or rec["rotational_period_s"]
        self.surface_gravity, self.has_solid_surface = rec["surface_gravity_mps2"], rec["has_solid_surface"]
        self.satellites = []
        self.reference_frame = ("rotating", name)
        self.orbit = None
        if parent is not None:
            o = rec["orbit"]
            self.orbit = FakeOrbit(parent, KeplerOrbit(parent.gravitational_parameter, o["sma_m"], o["eccentricity"],
                                                       o["inclination_rad"], o["lan_rad"], o["argpe_rad"],
                                                       o["mean_anomaly_at_epoch_rad"], o["epoch_s"]))
        self.terrain = lambda lat, lon: 0.0
        self.now = lambda: 0.0

    def __eq__(self, other):
        return isinstance(other, FakeBody) and other.name == self.name

    __hash__ = object.__hash__

    def pressure_at(self, h):
        return 0.0

    def density_at(self, h):
        return 0.0

    def surface_height(self, lat, lon):
        return self.terrain(lat, lon)


class FakeOrbit:
    """kRPC Orbit look-alike over one Kepler conic (no SOI transitions)."""

    def __init__(self, body, kep):
        self.body, self.k = body, kep
        self.semi_major_axis, self.eccentricity, self.inclination = kep.a, kep.e, kep.inc
        self.longitude_of_ascending_node, self.argument_of_periapsis = kep.lan, kep.argpe
        self.mean_anomaly_at_epoch, self.epoch = kep.mean_anomaly_at_epoch, kep.epoch
        self.time_to_soi_change, self.next_orbit = math.nan, None

    periapsis = property(lambda s: s.k.a * (1 - s.k.e))
    apoapsis = property(lambda s: s.k.a * (1 + s.k.e))
    periapsis_altitude = property(lambda s: s.periapsis - s.body.equatorial_radius)
    apoapsis_altitude = property(lambda s: s.apoapsis - s.body.equatorial_radius)

    def normal(self):
        r, v = self.k.state_at(self.epoch)
        return vunit(vcross(r, v))

    def relative_inclination(self, other):
        return orbits.vangle(self.normal(), other.normal())

    def true_anomaly_at_an(self, other):
        """Standard AN (ĥ_other x ĥ_self), as a true anomaly in the direction of motion: what KSP's
        FinePrint OrbitUtilities.AngleOfAscendingNode returns (via kRPC, clamped to +-pi)."""
        h, n = self.normal(), vcross(other.normal(), self.normal())
        k = self.k
        p, _ = orbits.state_from_elements(k.mu, k.a, k.e, k.inc, k.lan, k.argpe, 0.0)
        return orbits.wrap_pi(math.atan2(vdot(vcross(p, n), h), vdot(p, n)))

    def true_anomaly_at_dn(self, other):
        return orbits.wrap_pi(self.true_anomaly_at_an(other) + math.pi)

    def distance_at_closest_approach(self, other):
        return min(vnorm(vsub(self.k.state_at(t)[0], other.k.state_at(t)[0])) for t in range(0, 200_000, 500))

    def position_at(self, ut, frame):
        r, _ = self.k.state_at(ut)
        theta = 2 * math.pi * (self.body.now() / self.body.rotational_period)  # body turned east by theta so far
        c, s = math.cos(-theta), math.sin(-theta)
        return _k((c * r[0] - s * r[1], s * r[0] + c * r[1], r[2]))


class FakeNode:
    # Node-frame sign conventions (unverified live); tests flip them to prove the planners ask KSP.
    NORMAL_SIGN = 1.0  # +1: node "normal" = physical orbit normal r x v
    RADIAL_SIGN = 1.0  # +1: node "radial" = outward, perpendicular to prograde
    # Solver timing. KSP 1.12.5 + kRPC update node.orbit inside the edit RPC (the default here).
    # "frame": only while the game runs (a deferred per-frame update); "never": the prediction is stuck.
    SOLVER = "sync"

    def __init__(self, vessel, ut, prograde, normal, radial):
        self.vessel, self.ut, self.prograde, self.normal, self.radial = vessel, ut, prograde, normal, radial
        self._solved = (ut, prograde, normal, radial) if self.SOLVER == "sync" else (ut, 0.0, 0.0, 0.0)

    @property
    def orbit(self):
        if self.SOLVER == "sync" or (self.SOLVER == "frame" and not self.vessel.game.paused):
            self._solved = (self.ut, self.prograde, self.normal, self.radial)
        ut, prograde, normal, radial = self._solved
        k = self.vessel.orbit.k
        r, v = k.state_at(ut)
        p_hat, n_hat = vunit(v), vscale(vunit(vcross(r, v)), self.NORMAL_SIGN)
        rad_hat = vscale(vunit(vsub(vunit(r), vscale(p_hat, vdot(vunit(r), p_hat)))), self.RADIAL_SIGN)
        v2 = vadd(v, vadd(vscale(p_hat, prograde), vadd(vscale(n_hat, normal), vscale(rad_hat, radial))))
        el = orbits.classical_elements(k.mu, r, v2)
        m = orbits.true_to_mean(el["nu"], el["e"])
        return FakeOrbit(self.vessel.orbit.body, KeplerOrbit(k.mu, el["a"], el["e"], el["inc"], el["lan"], el["argpe"],
                                                             m, ut))

    def remove(self):
        self.vessel.control.nodes.remove(self)


class FakeControl:
    def __init__(self, vessel):
        self.vessel, self.nodes, self.added = vessel, [], 0

    def add_node(self, ut, prograde=0.0, normal=0.0, radial=0.0):
        # kRPC's Control.AddNode takes the Δv components as 32-bit floats (the Node setters take doubles)
        f32 = [struct.unpack("f", struct.pack("f", x))[0] for x in (prograde, normal, radial)]
        n = FakeNode(self.vessel, ut, *f32)
        self.nodes.append(n)
        self.added += 1
        return n


class FakeVessel:
    def __init__(self, body, kep):
        self.name, self.orbit = "Test Craft", FakeOrbit(body, kep)
        self.control = FakeControl(self)
        self.available_thrust, self.specific_impulse, self.mass = 60_000.0, 320.0, 5_000.0
        self.game = None  # the FakeKSP, for the pause state


class FakeSC:
    def __init__(self, bodies, vessel, ut):
        self.bodies, self.vessels, self.ut = bodies, [vessel], ut


class FakeKSP:
    def __init__(self, bodies, vessel, ut=1000.0):
        self.sc, self._vessel, self.paused, self.pause_calls = FakeSC(bodies, vessel, ut), vessel, False, []
        self.run_calls = 0
        vessel.game = self
        for b in bodies.values():
            b.now = lambda: self.sc.ut

    def set_paused(self, value):
        self.pause_calls.append(value)
        self.paused = value

    @contextlib.contextmanager
    def running(self, settle_s=0.0):  # same contract as astra.ksp.KSP.running
        self.run_calls += 1
        was = self.paused
        if was:
            self.set_paused(False)
        try:
            yield
        finally:
            if was:
                self.set_paused(True)

    def vessel(self):
        return self._vessel

    def scene(self):
        return "flight"

    def body(self, name=None):
        return self._vessel.orbit.body if name is None else self.sc.bodies[name]


def make_game(monkeypatch, craft_body="Kerbin", **kep):
    kerbin = FakeBody("Kerbin")
    mun = FakeBody("Mun", parent=kerbin)
    bodies = {"Kerbin": kerbin, "Mun": mun}
    body = bodies[craft_body]
    vessel = FakeVessel(body, KeplerOrbit(body.gravitational_parameter, **kep))
    game = FakeKSP(bodies, vessel)
    monkeypatch.setattr(compute, "ksp", lambda: game)
    return game


def assert_clean(game):
    assert game.sc.vessels[0].control.nodes == [], "a temporary node was left behind"
    assert game.paused is False and game.pause_calls[-1] is False, "the previous pause state was not restored"


R_K = 600_000.0


def test_circularize_and_set_apsis(offline, monkeypatch):
    a = R_K + 140_000
    game = make_game(monkeypatch, a=a, e=60_000 / a, mean_anomaly_at_epoch=0.5, epoch=1000.0)
    out = call("compute_maneuver", kind="circularize", at="apoapsis")
    assert out["prograde_mps"] > 0 and out["radial_mps"] == pytest.approx(0, abs=1e-6)
    p = out["predicted"]["ksp"][0]
    assert p["periapsis_alt_m"] == pytest.approx(200_000, abs=1) and p["apoapsis_alt_m"] == pytest.approx(200_000, abs=1)
    assert out["burn_time_s"] > 0 and out["half_dv_time_s"] > out["burn_time_s"] / 2
    assert_clean(game)
    raise_ = call("compute_maneuver", kind="set_apsis", at="periapsis", target_alt_m=500_000)
    assert raise_["predicted"]["ksp"][0]["apoapsis_alt_m"] == pytest.approx(500_000, abs=1)
    anywhere = call("compute_maneuver", kind="set_apsis", at="ut", ut=1600.0, target_alt_m=400_000)
    assert anywhere["predicted"]["ksp"][0]["apoapsis_alt_m"] == pytest.approx(400_000, abs=1)
    circ = call("compute_maneuver", kind="circularize", at="ut", ut=1600.0)
    pk = circ["predicted"]["ksp"][0]
    assert pk["apoapsis_alt_m"] - pk["periapsis_alt_m"] < 1 and abs(circ["radial_mps"]) > 1
    assert_clean(game)


def test_deorbit_and_argument_validation(offline, monkeypatch):
    game = make_game(monkeypatch, a=R_K + 100_000, e=0.0)
    out = call("compute_maneuver", kind="deorbit_to_periapsis", at="apoapsis", target_alt_m=30_000)
    assert out["prograde_mps"] < 0
    assert out["predicted"]["ksp"][0]["periapsis_alt_m"] == pytest.approx(30_000, abs=1)
    assert out["ut"] < out["predicted"]["model"]["atmosphere_interface_ut"]
    for bad in ({"kind": "set_apsis", "at": "apoapsis"},
                {"kind": "circularize", "at": "apoapsis", "target_alt_m": 1e5},
                {"kind": "circularize", "at": "ascending_node"},
                {"kind": "circularize", "at": "apoapsis", "ut": 5000.0},
                {"kind": "plane_change", "at": "nearest_node"},
                {"kind": "deorbit_to_periapsis", "at": "apoapsis", "target_alt_m": 200_000}):
        with pytest.raises(AstraError):
            call("compute_maneuver", **bad)
    assert_clean(game)


def test_plane_change_to_inclination(offline, monkeypatch):
    game = make_game(monkeypatch, a=R_K + 100_000, e=0.001, inc=math.radians(5), lan=1.0, argpe=0.4,
                     mean_anomaly_at_epoch=2.0, epoch=1000.0)
    for goal, at in ((10.0, "ascending_node"), (10.0, "descending_node"), (2.0, "nearest_node")):
        out = call("compute_maneuver", kind="plane_change", at=at, target_inclination_deg=goal)
        assert out["predicted"]["ksp"][0]["inclination_deg"] == pytest.approx(goal, abs=0.01)
        assert out["dv_mps"] == pytest.approx(2 * 2246 * math.sin(math.radians(abs(goal - 5)) / 2), rel=0.01)
    assert_clean(game)


def test_hohmann_to_body_and_errors_leave_no_node(offline, monkeypatch):
    game = make_game(monkeypatch, a=R_K + 80_000, e=0.0, mean_anomaly_at_epoch=0.3, epoch=0.0)
    out = call("compute_maneuver", kind="hohmann_to_body", target="Mun")
    assert out["prograde_mps"] == pytest.approx(856.4, abs=0.5)
    assert out["predicted"]["model"]["phase_angle_at_burn_deg"] == pytest.approx(110.9, abs=0.1)
    assert out["predicted"]["model"]["relative_inclination_deg"] == pytest.approx(0.0, abs=1e-6)
    assert out["ut"] >= 1000.0
    assert_clean(game)
    with pytest.raises(AstraError):
        call("compute_maneuver", kind="hohmann_to_body", target="Kerbin")
    assert_clean(game)


def test_return_from_moon_and_capture(offline, monkeypatch):
    game = make_game(monkeypatch, craft_body="Mun", a=220_000.0, e=0.0, inc=0.05, epoch=1000.0)
    out = call("compute_maneuver", kind="return_from_moon", target_alt_m=35_000)
    assert 250 < out["prograde_mps"] < 320
    assert out["predicted"]["model"]["parent_periapsis_alt_m"] == pytest.approx(35_000, abs=100)
    assert_clean(game)
    # arriving on a hyperbola around the Mun, 30 km periapsis ahead: capture into a circular orbit
    r_pe = 230_000.0
    v_inf = 500.0
    a = -6.5138398e10 / v_inf ** 2
    game = make_game(monkeypatch, craft_body="Mun", a=a, e=1 - r_pe / a, mean_anomaly_at_epoch=-0.5, epoch=1000.0)
    cap = call("compute_maneuver", kind="capture_at_periapsis", target_alt_m=30_000)
    assert cap["prograde_mps"] < 0
    assert cap["predicted"]["ksp"][0]["eccentricity"] == pytest.approx(0, abs=1e-6)
    assert_clean(game)


def test_node_search_converges_on_patched_conics(offline, monkeypatch):
    game = make_game(monkeypatch, a=R_K + 100_000, e=0.0, mean_anomaly_at_epoch=0.0, epoch=1000.0)
    t = 1500.0
    want = R_K + 1_000_000
    dv_exact = orbits.speed_for_apsis(3.5316e12, R_K + 100_000, 0.0, want) - orbits.circular_speed(3.5316e12, R_K + 100_000)
    out = call("compute_node_search", objective={"target_apoapsis_m": 1_000_000, "tolerance_m": 50, "minimize": "error"},
               ut_min=t, ut_max=t, bounds={"prograde": [0, 600], "radial": [-50, 50]}, max_evals=300)
    assert out["objective_met"]
    assert out["best"]["prograde_mps"] == pytest.approx(dv_exact, abs=1.0)
    assert out["evaluations"] <= 300
    assert_clean(game)
    # minimize dv under a floor: lower the periapsis as far as allowed, no further
    out = call("compute_node_search", objective={"periapsis_floor_m": 60_000, "target_periapsis_m": 60_000,
                                                 "tolerance_m": 200, "minimize": "dv"},
               ut_min=t, ut_max=t + 600, bounds={"prograde": [-100, 0]}, max_evals=250)
    assert out["objective_met"] and out["scored_patch"]["periapsis_alt_m"] >= 60_000
    assert out["scored_patch"]["periapsis_alt_m"] < 60_200
    assert_clean(game)
    with pytest.raises(AstraError, match="tolerance_m"):
        call("compute_node_search", objective={"target_apoapsis_m": 1e6, "minimize": "error"},
             ut_min=t, ut_max=t, bounds={"prograde": [0, 400]}, max_evals=10)


def test_terrain_region_caches_the_body_maximum(offline, monkeypatch):
    game = make_game(monkeypatch, a=R_K + 100_000, e=0.0)
    kerbin = game.sc.bodies["Kerbin"]
    kerbin.terrain = lambda lat, lon: 6000 * math.exp(-((lat - 10) ** 2 + (lon - 40) ** 2) / 50)
    monkeypatch.setattr(compute, "_live_link", lambda: game)
    out = call("compute_terrain", mode="region", body="Kerbin", step_deg=1.0)
    assert out["max_terrain_m"] == pytest.approx(6000, rel=1e-6)
    assert (out["max_at"]["lat_deg"], out["max_at"]["lon_deg"]) == (10, 40)
    assert out["body_max_terrain"]["updated_cache"] is True
    shared = json.loads((offline.parent / "terrain.json").read_text(encoding="utf-8"))["Kerbin"]
    assert shared["max_m"] == pytest.approx(6000) and {"lat_deg", "lon_deg", "samples", "method"} <= set(shared)
    # a coarser scan that finds a lower peak must not overwrite the better (higher) lower bound
    coarse = call("compute_terrain", mode="region", body="Kerbin", step_deg=7.0)
    assert coarse["max_terrain_m"] < 6000 and coarse["body_max_terrain"]["updated_cache"] is False
    monkeypatch.setattr(compute, "_live_link", lambda: None)
    cached = call("compute_terrain", mode="region", body="kerbin")
    assert cached["max_terrain_m"] == pytest.approx(6000) and cached["source"].startswith("cache")
    warn = call("compute_orbit", body="Kerbin", periapsis_alt_m=5000, apoapsis_alt_m=90_000)
    assert any("atmosphere" in w for w in warn["warnings"])
    with pytest.raises(AstraError):
        call("compute_terrain", mode="region", body="Mun")  # nothing cached for the Mun and no game


def test_terrain_ground_track_accounts_for_rotation(offline, monkeypatch):
    # sub-orbital equatorial arc from 80 km with periapsis below ground: flat terrain at 0 m
    r0 = R_K + 80_000
    a = (r0 + R_K - 200_000) / 2
    game = make_game(monkeypatch, a=a, e=r0 / a - 1, mean_anomaly_at_epoch=math.pi, epoch=1000.0)
    game.sc.ut = 1000.0
    out = call("compute_terrain", mode="ground_track", ut_end=1000.0 + 1200, samples=400)
    hit = out["first_ground_contact"]
    craft = KeplerOrbit(3.5316e12, a, r0 / a - 1, mean_anomaly_at_epoch=math.pi, epoch=1000.0)
    r, _ = craft.state_at(hit["ut"])
    assert vnorm(r) == pytest.approx(R_K, abs=300)
    rot = 360 * (hit["ut"] - 0.0) / game.sc.bodies["Kerbin"].rotational_period  # the body turned since UT 0
    expect = (math.degrees(math.atan2(r[1], r[0])) - rot + 180) % 360 - 180
    assert hit["lon_deg"] == pytest.approx(expect, abs=0.2)
    assert out["min_clearance_m"] < 0


# ---------------------------------------------------------------------------------------------
# regressions from the adversarial review


def test_node_search_rejects_misspelled_keys(offline, monkeypatch):
    """A dropped `target_periapsis` used to leave only the encounter, reported as 'objective met'."""
    from pydantic import ValidationError
    make_game(monkeypatch, a=R_K + 100_000, e=0.0)
    base = dict(ut_min=1500.0, ut_max=1500.0, bounds={"prograde": [0, 900]}, max_evals=20)
    with pytest.raises(ValidationError, match="target_periapsis"):
        call("compute_node_search", objective={"encounter_body": "Mun", "target_periapsis": 30_000,
                                               "tolerance_m": 500, "minimize": "dv"}, **base)
    with pytest.raises(ValidationError, match="Prograde"):
        call("compute_node_search", objective={"encounter_body": "Mun", "minimize": "dv"},
             **{**base, "bounds": {"Prograde": [0, 900]}})
    with pytest.raises(ValidationError):
        call("compute_node_search", objective={"encounter_body": "Mun", "minimize": "dv"},
             seed={"UT": 1500.0}, **base)
    schema = registry.arg_model(registry.TOOLS["compute_node_search"]).model_json_schema()
    assert schema["$defs"]["NodeObjective"]["additionalProperties"] is False


def test_node_search_needs_something_to_achieve(offline, monkeypatch):
    game = make_game(monkeypatch, a=R_K + 100_000, e=0.0)
    with pytest.raises(AstraError, match="nothing to achieve"):
        call("compute_node_search", objective={"minimize": "dv"}, ut_min=1500.0, ut_max=1500.0,
             bounds={"prograde": [0, 100]}, max_evals=10)
    assert game.sc.vessels[0].control.nodes == []


def test_descent_rejects_bad_inputs_with_hints(offline):
    good = dict(body="Mun", altitude_m=3000, vertical_speed_mps=-40, horizontal_speed_mps=200, mass_t=4, thrust_kn=60)
    for bad in ({"thrust_kn": 0}, {"mass_t": 0}, {"mass_t": -1}, {"isp_s": 0}, {"reaction_time_s": -1},
                {"vertical_speed_mps": math.nan}, {"horizontal_speed_mps": math.inf}):
        with pytest.raises(AstraError):  # a bare ValueError (or a 12 s NaN spin) before the review
            call("compute_descent", **{**good, **bad})
    weak = call("compute_descent", **{**good, "thrust_kn": 5})  # cannot even hover: early return path
    assert weak["feasible"] is False and weak["assumptions"] and weak["source"].startswith("cache")


def test_descent_gravity_uses_terrain_height(offline):
    mu, r = 6.5138398e10, 200_000.0
    high = call("compute_descent", body="Mun", altitude_m=1000, vertical_speed_mps=-20, horizontal_speed_mps=0,
                mass_t=4, thrust_kn=60, terrain_height_m=6000)
    assert high["gravity_mps2"] == pytest.approx(mu / (r + 7000) ** 2, rel=1e-6)
    datum = call("compute_descent", body="Mun", altitude_m=1000, vertical_speed_mps=-20, horizontal_speed_mps=0,
                 mass_t=4, thrust_kn=60)
    assert datum["gravity_mps2"] > high["gravity_mps2"]  # the conservative default
    assert any("sea level" in a for a in datum["assumptions"])


def test_ascent_estimate_validates_and_warns(offline):
    with pytest.raises(AstraError, match="latitude"):  # a ZeroDivisionError escaped at the pole
        call("compute_ascent_estimate", body="Kerbin", orbit_alt_m=80_000, launch_latitude_deg=90)
    with pytest.raises(AstraError, match="solid surface"):
        call("compute_ascent_estimate", body="Sun", orbit_alt_m=1e7)
    with pytest.raises(AstraError, match="inclination"):
        call("compute_ascent_estimate", body="Kerbin", orbit_alt_m=80_000, inclination_deg=200)
    low = call("compute_ascent_estimate", body="Kerbin", orbit_alt_m=50_000)
    assert any("atmosphere" in w for w in low["warnings"])
    assert call("compute_ascent_estimate", body="Kerbin", orbit_alt_m=80_000)["warnings"] == []


def test_circularize_radial_sign_is_confirmed_by_ksp(offline, monkeypatch):
    a = R_K + 140_000
    game = make_game(monkeypatch, a=a, e=60_000 / a, mean_anomaly_at_epoch=0.5, epoch=1000.0)
    monkeypatch.setattr(FakeNode, "RADIAL_SIGN", -1.0)  # KSP's radial opposite to the model's guess
    out = call("compute_maneuver", kind="circularize", at="ut", ut=1600.0)
    pk = out["predicted"]["ksp"][0]
    assert pk["apoapsis_alt_m"] - pk["periapsis_alt_m"] < 1
    assert any("radial sign chosen by KSP" in n for n in out["notes"])
    monkeypatch.setattr(FakeNode, "RADIAL_SIGN", 1.0)
    same = call("compute_maneuver", kind="circularize", at="ut", ut=1600.0)
    assert same["radial_mps"] == pytest.approx(-out["radial_mps"]) and not same["notes"]
    assert_clean(game)


def test_plane_change_normal_sign_is_confirmed_by_ksp(offline, monkeypatch):
    game = make_game(monkeypatch, a=R_K + 100_000, e=0.001, inc=math.radians(5), lan=1.0, argpe=0.4,
                     mean_anomaly_at_epoch=2.0, epoch=1000.0)
    monkeypatch.setattr(FakeNode, "NORMAL_SIGN", -1.0)
    out = call("compute_maneuver", kind="plane_change", at="ascending_node", target_inclination_deg=10.0)
    assert out["predicted"]["ksp"][0]["inclination_deg"] == pytest.approx(10.0, abs=0.01)
    assert any("normal sign chosen by KSP" in n for n in out["notes"])
    assert_clean(game)


def test_planning_reports_a_node_it_could_not_remove(offline, monkeypatch):
    game = make_game(monkeypatch, a=R_K + 100_000, e=0.0)

    def stuck(self):
        raise RuntimeError("node is locked")
    monkeypatch.setattr(FakeNode, "remove", stuck)
    with pytest.raises(AstraError, match="could not be removed") as info:
        call("compute_maneuver", kind="circularize", at="apoapsis")
    assert "node_delete" in info.value.hint
    assert game.paused is False  # the pause is restored even so

    def boom(*_a, **_k):
        raise AstraError("prediction exploded", "retry")
    monkeypatch.setattr(compute, "_patches", boom)
    with pytest.raises(AstraError) as info:  # the original failure is kept, the leftover node added
        call("compute_maneuver", kind="circularize", at="apoapsis")
    assert "prediction exploded" in info.value.message and "could not be removed" in info.value.message


def test_terrain_region_rejects_a_zero_step(offline, monkeypatch):
    from pydantic import ValidationError
    game = make_game(monkeypatch, a=R_K + 100_000, e=0.0)
    monkeypatch.setattr(compute, "_live_link", lambda: game)
    for step in (0.0, -1.0):  # ZeroDivisionError / a -inf "maximum" written to the shared cache
        with pytest.raises(ValidationError):
            call("compute_terrain", mode="region", body="Kerbin", step_deg=step)


def test_terrain_track_checks_the_window_and_samples_paused(offline, monkeypatch):
    game = make_game(monkeypatch, a=R_K + 100_000, e=0.0)
    game.sc.ut = 1000.0
    out = call("compute_terrain", mode="ground_track", ut_end=1300.0, samples=10)
    assert out["samples"] == 10 and game.pause_calls == [True, False] and game.paused is False
    game.sc.vessels[0].orbit.time_to_soi_change = 100.0  # leaves the SOI at UT 1100
    with pytest.raises(AstraError, match="leaves"):
        call("compute_terrain", mode="ground_track", ut_start=1200.0, ut_end=1300.0)
    clipped = call("compute_terrain", mode="ground_track", ut_end=1300.0, samples=10)
    assert clipped["ut_end"] == pytest.approx(1100.0) and clipped["note"] == "clipped at the SOI change"


def _live_moon_game(monkeypatch):
    """A live fake: a craft in a 20 km orbit of the Mun, the Mun and Minmus around Kerbin."""
    game = make_game(monkeypatch, craft_body="Mun", a=220_000.0, e=0.0, epoch=1000.0)
    kerbin = game.sc.bodies["Kerbin"]
    game.sc.bodies["Minmus"] = FakeBody("Minmus", parent=kerbin)
    kerbin.non_rotating_reference_frame = ("non-rotating", "Kerbin")
    game.sc.active_vessel = game.sc.vessels[0]
    for b in game.sc.bodies.values():
        b.now = lambda: game.sc.ut
    monkeypatch.setattr(compute, "_live_link", lambda: game)
    return game


def test_transfer_window_live_refuses_a_past_start(offline, monkeypatch):
    game = _live_moon_game(monkeypatch)
    with pytest.raises(AstraError, match="in the past"):
        call("compute_transfer_window", origin="Mun", target="Minmus", parking_alt_m=20_000, minimize="departure",
             earliest_ut=game.sc.ut - 100)
    out = call("compute_transfer_window", origin="Mun", target="Minmus", parking_alt_m=20_000, minimize="departure",
               search_days=20)
    assert out["source"] == "live game (kRPC)" and out["departure_ut"] >= game.sc.ut


def test_ejection_seed_is_never_in_the_past(offline, monkeypatch):
    from astra.physics import lambert
    game = _live_moon_game(monkeypatch)
    now = game.sc.ut
    mun, minmus = compute.Body.from_record(BODIES["Mun"]), compute.Body.from_record(BODIES["Minmus"])
    eph_o = compute._krpc_axes(mun.kepler(3.5316e12))
    eph_t = compute._krpc_axes(minmus.kepler(3.5316e12))

    def window(dep):
        return lambert.Transfer(dep, 3e5, (0.0, 0.0, 300.0), (0.0, 0.0, 200.0), 0.0)

    soon = compute._live_ejection_extras(game, mun, minmus, eph_o, eph_t, window(now + 1.0))
    assert "ejection_node_seed" not in soon and "before now" in soon["ejection_note"]
    later = compute._live_ejection_extras(game, mun, minmus, eph_o, eph_t, window(now + 50_000.0))
    seed = later["ejection_node_seed"]
    assert seed["ut"] >= now and seed["prograde_mps"] > 0


# ---------------------------------------------------------------------------------------------
# regressions from the second adversarial review


def test_live_link_asks_try_conn_and_never_probes_the_port(monkeypatch):
    import socket

    def no_raw_probe(*_a, **_k):
        raise AssertionError("compute must not open its own TCP probe")
    monkeypatch.setattr(socket, "create_connection", no_raw_probe)

    class Link:
        def __init__(self, conn):
            self.conn_obj, self.calls = conn, []

        def try_conn(self, timeout=0.5):
            self.calls.append(timeout)
            return self.conn_obj
    down = Link(None)
    monkeypatch.setattr(compute, "ksp", lambda: down)
    assert compute._live_link() is None and down.calls == [0.5]
    up = Link(object())
    monkeypatch.setattr(compute, "ksp", lambda: up)
    assert compute._live_link() is up


def test_paused_node_edits_are_read_without_running_the_game(offline, monkeypatch):
    """KSP/kRPC re-solve the node inside the edit RPC: planning never unpauses a paused game."""
    a = R_K + 140_000
    game = make_game(monkeypatch, a=a, e=60_000 / a, mean_anomaly_at_epoch=0.5, epoch=1000.0)
    game.paused = True  # the crew is deliberating
    out = call("compute_maneuver", kind="circularize", at="ut", ut=1600.0)
    assert out["predicted"]["ksp"][0]["eccentricity"] == pytest.approx(0, abs=1e-6)
    assert game.run_calls == 0 and game.pause_calls == [] and game.paused is True
    assert not any("the game ran" in n for n in out["notes"])
    assert game.sc.vessels[0].control.nodes == []


def test_a_deferred_solver_gets_a_brief_tick_and_the_pause_is_restored(offline, monkeypatch):
    a = R_K + 140_000
    game = make_game(monkeypatch, a=a, e=60_000 / a, mean_anomaly_at_epoch=0.5, epoch=1000.0)
    monkeypatch.setattr(FakeNode, "SOLVER", "frame")  # a solver that only updates on a running frame
    out = call("compute_maneuver", kind="set_apsis", at="periapsis", target_alt_m=500_000)
    assert out["predicted"]["ksp"][0]["apoapsis_alt_m"] == pytest.approx(500_000, abs=1)
    assert game.run_calls >= 1 and any("the game ran" in n for n in out["notes"])
    assert_clean(game)  # node removed, and the game (unpaused before the call) is unpaused again
    game.paused, game.pause_calls = True, []
    call("compute_maneuver", kind="set_apsis", at="periapsis", target_alt_m=400_000)
    assert game.paused is True and game.pause_calls[-1] is True  # a paused game stays paused


def test_a_stuck_prediction_is_an_error_not_a_wrong_answer(offline, monkeypatch):
    a = R_K + 140_000
    game = make_game(monkeypatch, a=a, e=60_000 / a, mean_anomaly_at_epoch=0.5, epoch=1000.0)
    monkeypatch.setattr(FakeNode, "SOLVER", "never")
    monkeypatch.setattr(compute._Trial, "REFRESH_S", 0.1)
    with pytest.raises(AstraError, match="did not match"):
        call("compute_maneuver", kind="set_apsis", at="periapsis", target_alt_m=500_000)
    assert_clean(game)


def test_float32_add_node_is_not_mistaken_for_a_stale_prediction(monkeypatch):
    """Slow high orbit + big Δv: add_node's float32 rounding alone used to exceed the energy check."""
    game = make_game(monkeypatch, craft_body="Mun", a=2_000_000.0, e=0.0, epoch=1000.0)
    vessel = game.sc.vessels[0]
    trial = compute._Trial(game, vessel, vessel.orbit.k, check_before_ut=math.inf)
    node = trial.set(1500.0, 1500.00006, 0.0, -45.678901)
    assert abs(node.prograde - 1500.00006) > 5e-5  # really rounded by the fake's float32 path
    assert game.run_calls == 0 and trial.ticked_s == 0.0
    trial.remove()


def test_trial_node_sends_only_the_values_that_changed():
    class Node:
        def __init__(self):
            object.__setattr__(self, "sets", [])

        def __setattr__(self, name, value):
            self.sets.append(name)
            object.__setattr__(self, name, value)

    class Control:
        def add_node(self, ut, prograde=0.0, normal=0.0, radial=0.0):
            self.node = Node()
            return self.node

    class Vessel:
        control = Control()
    vessel = Vessel()
    trial = compute._Trial(None, vessel, KeplerOrbit(3.5316e12, 7e5, 0.0), check_before_ut=-math.inf)
    trial.set(100.0, 1.0, 2.0, 3.0)
    node = vessel.control.node
    trial.set(100.0, 1.0, 2.0, 3.0)
    assert node.sets == []
    trial.set(100.0, 5.0, 2.0, 3.0)
    assert node.sets == ["prograde"]
    trial.set(200.0, 5.0, 2.0, -3.0)
    assert node.sets == ["prograde", "ut", "radial"]


def test_node_search_reports_its_best_point_when_the_budget_runs_out(offline, monkeypatch):
    """Budget 2: the seed, then one improving probe. The improvement used to be dropped."""
    game = make_game(monkeypatch, a=R_K + 100_000, e=0.0, mean_anomaly_at_epoch=0.0, epoch=1000.0)
    out = call("compute_node_search", objective={"target_apoapsis_m": 1_000_000, "tolerance_m": 50, "minimize": "error"},
               ut_min=1500.0, ut_max=1500.0, bounds={"prograde": [0, 600]}, max_evals=2)
    assert out["evaluations"] == 2 and out["stopped_by"] == "max_evals"
    assert out["seed"]["prograde"] == pytest.approx(300)
    assert out["best"]["prograde_mps"] == pytest.approx(330)  # the better of the two, not the seed
    assert_clean(game)


def test_node_search_miss_toward_the_star_does_not_crash():
    kerbin = FakeBody("Kerbin")
    sun = FakeBody("Sun")
    search = compute._NodeSearch(None, {"minimize": "dv"}, sun, {}, [], 1.0, 1)
    parked = FakeOrbit(kerbin, KeplerOrbit(kerbin.gravitational_parameter, 2e6, 0.1))
    miss = search._miss([parked])
    assert miss["cost"] > search.MISS  # scored by how far the apoapsis is from leaving Kerbin's SOI


def test_maneuver_at_a_past_ut_never_places_a_node(offline, monkeypatch):
    """KSP deletes nodes placed in the past; a trial node there used to surface as 'could not be removed'."""
    a = R_K + 140_000
    game = make_game(monkeypatch, a=a, e=60_000 / a, mean_anomaly_at_epoch=0.5, epoch=1000.0)
    with pytest.raises(AstraError, match="in the past"):
        call("compute_maneuver", kind="circularize", at="ut", ut=900.0)
    game.sc.vessels[0].orbit.time_to_soi_change = 500.0  # leaves the SOI at UT 1500
    with pytest.raises(AstraError, match="SOI change"):
        call("compute_maneuver", kind="circularize", at="ut", ut=1600.0)
    assert game.sc.vessels[0].control.added == 0
    assert_clean(game)


def test_non_finite_inputs_are_clean_errors(offline, monkeypatch):
    for name, args in (("compute_orbit", dict(body="Kerbin", periapsis_alt_m=80_000, apoapsis_alt_m=math.inf)),
                       ("compute_hohmann", dict(body="Kerbin", from_alt_m=math.nan, to_alt_m=1e6)),
                       ("compute_ascent_estimate", dict(body="Kerbin", orbit_alt_m=math.inf)),
                       ("compute_ascent_estimate", dict(body="Kerbin", orbit_alt_m=80_000, liftoff_twr=math.inf)),
                       ("compute_rocket", dict(isp_s=300, mass_start_t=math.inf, mass_end_t=1)),
                       ("compute_transfer_window", dict(origin="Kerbin", target="Duna", parking_alt_m=80_000,
                                                        minimize="departure", earliest_ut=0,
                                                        tof_range_days=(10, math.inf)))):
        with pytest.raises(AstraError, match="finite"):  # a bare ValueError escaped before
            call(name, **args)
    game = make_game(monkeypatch, a=R_K + 100_000, e=0.0)
    with pytest.raises(AstraError, match="finite"):
        call("compute_node_search", objective={"target_apoapsis_m": 1e6, "tolerance_m": 50, "minimize": "error"},
             ut_min=1500.0, ut_max=1500.0, bounds={"prograde": [0, math.inf]}, max_evals=10)
    with pytest.raises(AstraError, match="finite"):
        call("compute_maneuver", kind="circularize", at="ut", ut=math.inf)
    assert game.sc.vessels[0].control.added == 0
    with pytest.raises(AstraError, match="same body"):
        call("compute_transfer_window", origin="Kerbin", target="kerbin", parking_alt_m=80_000,
             minimize="departure", earliest_ut=0)


def test_undefined_vessel_orbit_is_refused(offline, monkeypatch):
    game = make_game(monkeypatch, a=math.nan, e=0.0)
    with pytest.raises(AstraError, match="no defined orbit"):
        call("compute_maneuver", kind="circularize", at="apoapsis")
    assert_clean(game)


def test_existing_nodes_are_reported(offline, monkeypatch):
    game = make_game(monkeypatch, a=R_K + 100_000, e=0.0, mean_anomaly_at_epoch=0.0, epoch=1000.0)
    ctrl = game.sc.vessels[0].control
    mine = ctrl.add_node(90_000.0, prograde=10.0)
    out = call("compute_maneuver", kind="set_apsis", at="ut", ut=1500.0, target_alt_m=300_000)
    assert any("after this burn" in n for n in out["notes"])
    assert ctrl.nodes == [mine]  # the crew's node stays; only the trial node went
    found = call("compute_node_search", objective={"target_apoapsis_m": 300_000, "tolerance_m": 100, "minimize": "error"},
                 ut_min=1500.0, ut_max=1500.0, bounds={"prograde": [0, 200]}, max_evals=60)
    assert any("after this burn" in n for n in found["notes"]) and ctrl.nodes == [mine]


@pytest.mark.parametrize("normal_sign", [1.0, -1.0])
def test_plane_change_to_match_a_target_plane(offline, monkeypatch, normal_sign):
    """Target mode was untested: node times come from kRPC's true_anomaly_at_an/dn."""
    game = make_game(monkeypatch, a=R_K + 100_000, e=0.01, inc=math.radians(3), lan=0.4, argpe=1.1,
                     mean_anomaly_at_epoch=2.0, epoch=1000.0)
    kerbin = game.sc.bodies["Kerbin"]
    station = FakeVessel(kerbin, KeplerOrbit(kerbin.gravitational_parameter, R_K + 250_000, 0.0,
                                             math.radians(8), 2.0, 0.0, 0.5, 1000.0))
    station.name = "Station"
    game.sc.vessels.append(station)
    monkeypatch.setattr(FakeNode, "NORMAL_SIGN", normal_sign)
    rel0 = math.degrees(game.sc.vessels[0].orbit.relative_inclination(station.orbit))
    for at in ("ascending_node", "descending_node", "cheapest_node"):
        out = call("compute_maneuver", kind="plane_change", at=at, target="station")
        assert out["burn_point"] in ("ascending_node", "descending_node")
        assert out["predicted"]["model"]["plane_rotation_deg"] == pytest.approx(rel0, abs=1e-6)
        assert out["predicted"]["ksp_check"]["relative_inclination_after_deg"] == pytest.approx(0, abs=0.01)
    inc = call("compute_maneuver", kind="plane_change", at="nearest_node", target_inclination_deg=6.0)
    assert inc["predicted"]["ksp_check"]["inclination_after_deg"] == pytest.approx(6.0, abs=0.01)
    assert game.sc.vessels[0].control.nodes == [] and station.control.nodes == []


def test_descent_labels_the_flat_ground_error_at_orbital_speed(offline):
    fast = call("compute_descent", body="Mun", altitude_m=15_000, vertical_speed_mps=-10, horizontal_speed_mps=550,
                mass_t=5, thrust_kn=60, isp_s=320)
    assert any("centrifugal" in a for a in fast["assumptions"])
    slow = call("compute_descent", body="Mun", altitude_m=500, vertical_speed_mps=-20, horizontal_speed_mps=5,
                mass_t=5, thrust_kn=60)
    assert not any("centrifugal" in a for a in slow["assumptions"])


def test_transfer_window_warns_about_low_parking_and_arrival_altitudes(offline):
    out = call("compute_transfer_window", origin="Kerbin", target="Duna", parking_alt_m=50_000,
               minimize="total", arrival_alt_m=20_000, earliest_ut=0, search_days=5)
    assert len(out["altitude_warnings"]) == 2 and all("atmosphere" in w for w in out["altitude_warnings"])
