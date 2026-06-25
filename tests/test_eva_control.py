"""Unit tests for the precise EVA/personnel control layer.

Two things are pinned here, both WITHOUT a live KSP:
  1. The geodesic math (haversine distance + initial bearing + the direct-geodesic projection) against
     hand-checkable reference values. This is the "no guessing" guarantee — the heading/distance the
     planner computes must be the real spherical-trig answer.
  2. That ``BridgeClient`` builds the exact request payloads the C# bridge expects (every numeric field
     a STRING, because the bridge's JSON parser only reads string values), by mocking ``_request``.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ksp_lab import eva_control  # noqa: E402
from ksp_lab.bridge_client import BridgeClient, BridgeError  # noqa: E402


# --------------------------------------------------------------------------------------------------
# Geodesic math.
# --------------------------------------------------------------------------------------------------
KERBIN_R = 600_000.0  # m, stock Kerbin equatorial radius


def test_geodesic_quarter_circumference_along_equator():
    # 90 deg of longitude along the equator = a quarter of a great circle = (pi/2)*R.
    g = eva_control.geodesic(0.0, 0.0, 0.0, 90.0, KERBIN_R)
    assert g.distance_m == pytest.approx(math.pi / 2 * KERBIN_R, rel=1e-9)
    # Heading due east at the equator.
    assert g.bearing_deg == pytest.approx(90.0, abs=1e-6)


def test_geodesic_due_north_bearing_and_distance():
    # One degree of latitude north = (pi/180)*R, heading due north.
    g = eva_control.geodesic(10.0, 20.0, 11.0, 20.0, KERBIN_R)
    assert g.distance_m == pytest.approx(math.radians(1.0) * KERBIN_R, rel=1e-9)
    assert g.bearing_deg == pytest.approx(0.0, abs=1e-6)


def test_geodesic_zero_distance_for_identical_points():
    g = eva_control.geodesic(42.0, -17.0, 42.0, -17.0, KERBIN_R)
    assert g.distance_m == pytest.approx(0.0, abs=1e-6)


def test_destination_point_round_trips_with_geodesic():
    # Project 5 km on a 33 deg bearing, then measure back: distance & bearing must match the inputs.
    lat0, lon0 = 12.5, -45.0
    lat1, lon1 = eva_control.destination_point(lat0, lon0, 33.0, 5_000.0, KERBIN_R)
    back = eva_control.geodesic(lat0, lon0, lat1, lon1, KERBIN_R)
    assert back.distance_m == pytest.approx(5_000.0, rel=1e-6)
    assert back.bearing_deg == pytest.approx(33.0, abs=1e-4)


def test_destination_point_due_east_increases_longitude():
    lat1, lon1 = eva_control.destination_point(0.0, 0.0, 90.0, math.radians(1.0) * KERBIN_R, KERBIN_R)
    assert lat1 == pytest.approx(0.0, abs=1e-6)
    assert lon1 == pytest.approx(1.0, abs=1e-4)


# --------------------------------------------------------------------------------------------------
# BridgeClient payload shape — mock _request, assert the (method, path, json) it would send.
# --------------------------------------------------------------------------------------------------
class _RecordingBridge(BridgeClient):
    """Capture (method, path, payload) of every bridge call without any HTTP.

    BridgeClient is a slots dataclass, so the established test pattern (used by the round-trip suite)
    is to SUBCLASS and override _request — a normal subclass gets its own __dict__ for ``calls``.
    """

    def __init__(self, ret: dict | None = None):
        super().__init__(base_url="http://127.0.0.1:48500")
        self.calls: list[tuple[str, str, dict]] = []
        self._ret = ret or {"ok": True}

    def _request(self, method, path, **kwargs):  # type: ignore[override]
        self.calls.append((method, path, kwargs.get("json", {})))
        return self._ret


def test_eva_walk_to_latlon_payload_is_all_strings():
    br = _RecordingBridge()
    br.eva_walk_to(lat=12.5, lon=-30.25, crew="Jeb")
    method, path, payload = br.calls[0]
    assert (method, path) == ("POST", "/eva-walk-to")
    assert payload == {"lat": "12.5", "lon": "-30.25", "crew": "Jeb"}
    assert all(isinstance(v, str) for v in payload.values())


def test_eva_walk_to_vector_payload():
    br = _RecordingBridge()
    br.eva_walk_to(bearing=90.0, distance=250.0)
    _, path, payload = br.calls[0]
    assert path == "/eva-walk-to"
    assert payload == {"bearing": "90.0", "distance": "250.0"}


def test_eva_walk_to_rejects_both_or_neither():
    br = _RecordingBridge()
    with pytest.raises(BridgeError):
        br.eva_walk_to(lat=1.0, lon=2.0, bearing=3.0, distance=4.0)
    with pytest.raises(BridgeError):
        br.eva_walk_to()
    # Half a pair is also invalid (lat without lon).
    with pytest.raises(BridgeError):
        br.eva_walk_to(lat=1.0)


def test_readback_endpoints_use_correct_method_and_path():
    br = _RecordingBridge()
    br.eva_status()
    br.crew_list()
    br.crew_roster()
    br.vessel_info()
    br.parts_list(vessel="Lander")
    br.resources()
    assert [(m, p) for (m, p, _) in br.calls] == [
        ("GET", "/eva-status"),
        ("GET", "/crew-list"),
        ("GET", "/crew-roster"),
        ("POST", "/vessel-info"),
        ("POST", "/parts-list"),
        ("POST", "/resources"),
    ]
    # vessel-info with no name sends an empty body; parts-list carries the vessel substring.
    assert br.calls[3][2] == {}
    assert br.calls[4][2] == {"vessel": "Lander"}


# --------------------------------------------------------------------------------------------------
# eva_control helpers drive the bridge with the right calls.
# --------------------------------------------------------------------------------------------------
def test_walk_kerbal_to_annotates_planned_geodesic():
    # eva_status reports the current position; walk_kerbal_to must attach the locally-computed plan.
    status = {"onEva": True, "latitude": 0.0, "longitude": 0.0}

    class FakeBridge:
        def eva_status(self):
            return status

        def eva_walk_to(self, lat=None, lon=None, bearing=None, distance=None, crew=""):
            return {"ok": True, "targetLatitude": lat, "targetLongitude": lon}

    out = eva_control.walk_kerbal_to(FakeBridge(), lat=0.0, lon=90.0, body_radius_m=KERBIN_R)
    assert "plannedGeodesic" in out
    assert out["plannedGeodesic"]["distanceM"] == pytest.approx(math.pi / 2 * KERBIN_R, rel=1e-9)
    assert out["plannedGeodesic"]["bearingDeg"] == pytest.approx(90.0, abs=1e-6)


def test_walk_kerbal_by_passes_bearing_distance_through():
    seen = {}

    class FakeBridge:
        def eva_walk_to(self, lat=None, lon=None, bearing=None, distance=None, crew=""):
            seen.update(bearing=bearing, distance=distance, crew=crew)
            return {"ok": True}

    eva_control.walk_kerbal_by(FakeBridge(), bearing_deg=45.0, distance_m=120.0, crew="Bill")
    assert seen == {"bearing": 45.0, "distance": 120.0, "crew": "Bill"}


def test_plant_flag_at_walks_then_plants():
    order: list[str] = []

    class FakeBridge:
        def eva_status(self):
            return {"onEva": True, "latitude": 1.0, "longitude": 2.0}

        def eva_walk_to(self, lat=None, lon=None, bearing=None, distance=None, crew=""):
            order.append("walk")
            return {"ok": True}

        def eva_flag(self, crew=""):
            order.append("flag")
            return {"ok": True, "planted": True}

    out = eva_control.plant_flag_at(FakeBridge(), lat=1.0, lon=2.5, body_radius_m=KERBIN_R)
    assert order == ["walk", "flag"]
    assert out["planted"] is True
    assert "walkResult" in out
