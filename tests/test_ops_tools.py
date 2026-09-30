"""Offline tests for the instrument and control tools (observe, game, control, crew, autopilot).

The game is replaced by small kRPC-like fakes: objects compare by `_object_id` like kRPC proxies,
enums print as 'Type.member', and the bridge records every request. A read-only live smoke test at
the end runs only with ASTRA_LIVE=1 and a running game.
"""

import base64
import contextlib
import dataclasses
import enum
import inspect
import io
import itertools
import math
import os
import sys
import types
from pathlib import Path

import pytest

from astra import ksp as ksp_mod
from astra import registry
from astra import telemetry as tm
from astra.errors import AstraError, BridgeError, WrongScene
from astra.tools import autopilot, control, crew, game, observe

MY_MODULES = (observe, game, control, crew, autopilot)
_ids = itertools.count(1000)


# ---------------------------------------------------------------------------------------------
# Fakes


class VesselSituation(enum.Enum):
    pre_launch = 0
    orbiting = 1
    sub_orbital = 2
    flying = 4
    landed = 5
    splashed = 6


class VesselType(enum.Enum):
    ship = 7
    debris = 1
    eva = 11
    flag = 12
    space_object = 9


class SASMode(enum.Enum):
    stability_assist = 0
    maneuver = 1
    prograde = 2
    retrograde = 3
    normal = 4
    anti_normal = 5
    radial = 6
    anti_radial = 7
    target = 8
    anti_target = 9


class SpeedMode(enum.Enum):
    orbit = 0
    surface = 1
    target = 2


class ControlState(enum.Enum):
    full = 0
    none = 2


class RosterStatus(enum.Enum):
    available = 0
    assigned = 1


class Obj:
    """Identity like a kRPC proxy: equal when the server object id is equal."""

    def __init__(self, **attrs):
        self._object_id = next(_ids)
        self.__dict__.update(attrs)

    def __eq__(self, other):
        return isinstance(other, Obj) and other._object_id == self._object_id

    def __hash__(self):
        return hash(self._object_id)


MODULE_ATTRS = ("engine", "decoupler", "parachute", "leg", "wheel", "solar_panel", "antenna", "radiator",
                "fairing", "docking_port", "launch_clamp", "rcs", "reaction_wheel", "cargo_bay", "intake", "light")


class Part(Obj):
    def __init__(self, name, parent=None, stage=-1, decouple_stage=-1, tag="", seats=0, module_names=()):
        super().__init__(name=name, title=name.upper(), tag=tag, stage=stage, decouple_stage=decouple_stage,
                         parent=parent, available_seats=seats, module_names=set(module_names),
                         temperature=300.0, max_temperature=2000.0, skin_temperature=500.0,
                         max_skin_temperature=2500.0, crossfeed=False, experiments=[], modules=[])
        for attr in MODULE_ATTRS:
            setattr(self, attr, None)


def engine(part, active=False, thrust=0.0, available=0.0):
    part.engine = Obj(part=part, active=active, has_fuel=True, thrust=thrust, available_thrust=available,
                      max_vacuum_thrust=available, thrust_limit=1.0, vacuum_specific_impulse=340.0,
                      kerbin_sea_level_specific_impulse=290.0, throttle_locked=False, can_restart=True,
                      can_shutdown=True, propellant_names=["LiquidFuel", "Oxidizer"])
    return part


def decoupler(part):
    part.decoupler = Obj(part=part, decoupled=False, staged=False, is_omni_decoupler=False, impulse=1000.0)
    return part


class Parts:
    def __init__(self, vessel):
        self.v = vessel
        self._controlling = None

    @property
    def all(self):
        return list(self.v._parts)

    def _mods(self, attr):
        return [getattr(p, attr) for p in self.v._parts if getattr(p, attr) is not None]

    def __getattr__(self, name):
        plural = {"engines": "engine", "decouplers": "decoupler", "parachutes": "parachute", "legs": "leg",
                  "wheels": "wheel", "solar_panels": "solar_panel", "antennas": "antenna",
                  "radiators": "radiator", "fairings": "fairing", "docking_ports": "docking_port",
                  "launch_clamps": "launch_clamp", "rcs": "rcs", "reaction_wheels": "reaction_wheel",
                  "cargo_bays": "cargo_bay", "intakes": "intake", "lights": "light"}
        if name in plural:
            return self._mods(plural[name])
        raise AttributeError(name)

    @property
    def experiments(self):
        return [e for p in self.v._parts for e in p.experiments]

    def with_name(self, n):
        return [p for p in self.v._parts if p.name == n]

    def with_tag(self, t):
        return [p for p in self.v._parts if p.tag == t]

    def with_title(self, t):
        return [p for p in self.v._parts if p.title == t]

    def with_module(self, m):
        return [p for p in self.v._parts if m in p.module_names]

    def in_stage(self, s):
        return [p for p in self.v._parts if p.stage == s]

    @property
    def root(self):
        return self.v._parts[0]

    @property
    def controlling(self):
        return self._controlling or self.v._parts[0]

    @controlling.setter
    def controlling(self, part):
        self._controlling = part


class Control:
    def __init__(self, vessel, stage=0, refuse_sas=()):
        self.v = vessel
        self.current_stage = stage
        self.stage_lock = False
        self.throttle = 0.0
        self.sas = False
        self._sas_mode = SASMode.stability_assist
        self.speed_mode = SpeedMode.orbit
        self.rcs = self.gear = self.legs = self.lights = self.brakes = self.abort = False
        self.state = ControlState.full
        self.source = "ControlSource.kerbal"
        self.nodes = []
        self.groups = {}
        self.refuse_sas = set(refuse_sas)
        self.on_stage = None

    @property
    def sas_mode(self):
        return self._sas_mode

    @sas_mode.setter
    def sas_mode(self, mode):
        if mode.name in self.refuse_sas:
            raise RuntimeError("Cannot set SAS mode of vessel")
        self._sas_mode = mode

    def activate_next_stage(self):
        self.current_stage -= 1
        return self.on_stage() if self.on_stage else []

    def get_action_group(self, g):
        return self.groups.get(g, False)

    def set_action_group(self, g, state):
        self.groups[g] = state

    def toggle_action_group(self, g):
        self.groups[g] = not self.groups.get(g, False)

    def add_node(self, ut, prograde=0.0, normal=0.0, radial=0.0):
        node = FakeNode(self, ut, prograde, normal, radial)
        self.nodes.append(node)
        self.nodes.sort(key=lambda n: n.ut)
        return node

    def remove_nodes(self):
        self.nodes.clear()


class Body(Obj):
    def __init__(self, name, radius=600000.0, atmosphere=70000.0, parent=None):
        super().__init__(name=name, equatorial_radius=radius, atmosphere_depth=atmosphere,
                         has_atmosphere=atmosphere > 0, reference_frame=Obj(), non_rotating_reference_frame=Obj(),
                         gravitational_parameter=3.5316e12, mass=5.29e22, surface_gravity=9.81,
                         sphere_of_influence=8.4e7, rotational_period=21549.4, rotational_speed=2.9157e-4,
                         has_solid_surface=True, flying_high_altitude_threshold=18000.0,
                         space_high_altitude_threshold=250000.0, has_atmospheric_oxygen=True, is_star=False,
                         satellites=[], orbit=None)

    def pressure_at(self, alt):
        return 101325.0 if alt == 0 else 0.0

    def density_at(self, alt):
        return 1.14 if alt == 0 else 0.0

    def surface_height(self, lat, lon):
        return 100.0 * math.cos(math.radians(lat))


class Orbit(Obj):
    def __init__(self, body, **kw):
        defaults = dict(apoapsis_altitude=90000.0, periapsis_altitude=75000.0, semi_major_axis=682500.0,
                        eccentricity=0.011, inclination=0.0, longitude_of_ascending_node=0.0,
                        argument_of_periapsis=0.0, true_anomaly=0.5, period=1880.0, time_to_apoapsis=800.0,
                        time_to_periapsis=1700.0, speed=2290.0, radius=680000.0, time_to_soi_change=math.nan,
                        next_orbit=None)
        defaults.update(kw)
        super().__init__(body=body, **defaults)

    def ut_at_true_anomaly(self, ta):
        return 5000.0 if ta >= 0 else 400.0  # the descending node already passed this orbit

    def true_anomaly_at_an(self, other):
        return 1.0

    def true_anomaly_at_dn(self, other):
        return -1.0

    def relative_inclination(self, other):
        return math.radians(2.0)

    def time_of_closest_approach(self, other):
        return 1234.0

    def distance_at_closest_approach(self, other):
        return 50.0

    def position_at(self, t, rf):  # circular, in the orbit plane
        a = 2 * math.pi * t / self.period + getattr(self, "phase", 0.0)
        return (self.radius * math.cos(a), self.radius * math.sin(a), 0.0)


class FakeNode(Obj):
    def __init__(self, control, ut, prograde, normal, radial):
        super().__init__(ut=ut, prograde=prograde, normal=normal, radial=radial,
                         reference_frame=Obj(), orbit=Orbit(control.v.orbit.body))
        self.control = control
        self.delta_v = math.sqrt(prograde ** 2 + normal ** 2 + radial ** 2)
        self.remaining_delta_v = self.delta_v
        self.time_to = ut - 1000.0

    def remove(self):
        self.control.nodes.remove(self)


class AutoPilot:
    def __init__(self):
        self.engaged = False
        self.reference_frame = None
        self.target_direction = None
        self.target_roll = None
        self.pitch_heading = None
        self.errors = iter([5.0])

    def engage(self):
        self.engaged = True

    def disengage(self):
        self.engaged = False

    def target_pitch_and_heading(self, pitch, heading):
        self.pitch_heading = (pitch, heading)

    @property
    def error(self):
        return next(self.errors, 0.1)


class Vessel(Obj):
    def __init__(self, name, parts=(), situation=VesselSituation.orbiting, vtype=VesselType.ship, body=None,
                 stage=0, crew=(), refuse_sas=()):
        body = body or Body("Kerbin")
        super().__init__(name=name, type=vtype, situation=situation, _parts=list(parts), mass=10000.0,
                         thrust=0.0, available_thrust=0.0, specific_impulse=0.0, crew_capacity=3,
                         recoverable=False, orbit=Orbit(body), orbital_reference_frame=Obj(),
                         surface_reference_frame=Obj(), surface_velocity_reference_frame=Obj(),
                         reference_frame=Obj(), crew=[Obj(name=c, trait="Pilot", experience=0.0) for c in crew],
                         auto_pilot=AutoPilot(), available_torque=((1000.0, 500.0, 1000.0), (-1000.0, -500.0, -1000.0)),
                         moment_of_inertia=(100.0, 50.0, 200.0), pos=(700000.0, 0.0, 0.0))
        self.parts = Parts(self)
        self.control = Control(self, stage, refuse_sas)
        self.resources = Obj(all=[])

    def position(self, rf):
        return self.pos

    def velocity(self, rf):
        return (0.0, 2290.0, 0.0)

    def direction(self, rf):
        return (0.0, 1.0, 0.0)

    def flight(self, rf):
        return Obj(mean_altitude=100000.0, surface_altitude=100000.0, latitude=0.0, longitude=0.0, speed=2100.0)


class FakeSC:
    SASMode = SASMode
    SpeedMode = SpeedMode
    VesselType = VesselType

    def __init__(self):
        self.vessels = []
        self.active_vessel = None
        self.bodies = {"Kerbin": Body("Kerbin"), "Mun": Body("Mun", 200000.0, 0.0)}
        self.ut = 1000.0
        self.rails_warp_factor = 0
        self.physics_warp_factor = 0
        self.warp_rate = 1.0
        self.game_mode = "GameMode.sandbox"
        self.crafts = {"VAB": ["Orbiter 1", "Probe X"], "SPH": []}
        self.launch_sites = [Obj(name="LaunchPad"), Obj(name="Runway")]
        self.kerbals = {"Jebediah Kerman": RosterStatus.available, "Bill Kerman": RosterStatus.assigned}
        self.launch_calls = []
        self.on_launch = None
        self.target_vessel = self.target_body = self.target_docking_port = None
        self.transfers = []

    def launchable_vessels(self, d):
        return list(self.crafts[d])

    def get_kerbal(self, name):
        status = self.kerbals.get(name)
        return None if status is None else Obj(name=name, roster_status=status)

    def launch_vessel(self, *args):
        self.launch_calls.append(args)
        if self.on_launch:
            self.on_launch(*args)

    def clear_target(self):
        self.target_vessel = self.target_body = self.target_docking_port = None

    def transform_direction(self, d, from_, to):
        return d

    def transfer_crew(self, member, part):
        self.transfers.append((member.name, part.name))
        part.available_seats -= 1


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.replies = {}
        self.is_up = True

    def up(self):
        return self.is_up

    def _reply(self, method, path, body):
        self.calls.append((method, path, body))
        r = self.replies.get((method, path), {})
        if isinstance(r, Exception):
            raise r
        return r(body) if callable(r) else dict(r)

    def get(self, path, timeout=None):
        return self._reply("GET", path, None)

    def post(self, path, body=None, timeout=None):
        return self._reply("POST", path, {k: v for k, v in (body or {}).items() if v is not None})


class FakeKSP:
    def __init__(self):
        self.sc = FakeSC()
        self.bridge = FakeBridge()
        self._scene = "flight"
        self.paused = True
        self.pause_log = []

    def scene(self):
        return self._scene

    def require_flight(self):
        if self._scene != "flight":
            raise WrongScene("this needs the flight scene", "hint")

    def vessel(self):
        self.require_flight()
        return self.sc.active_vessel

    def set_paused(self, value):
        self.paused = bool(value)
        self.pause_log.append(bool(value))

    def hold_for_deliberation(self):
        if self._scene == "flight":
            self.set_paused(True)
        return self.paused

    @contextlib.contextmanager
    def running(self, settle_s=0.0):
        was_paused = self.paused
        if was_paused:
            self.set_paused(False)
        try:
            yield
        finally:
            if was_paused:
                self.set_paused(True)


@pytest.fixture
def k(monkeypatch):
    fake = FakeKSP()
    monkeypatch.setattr(ksp_mod, "_KSP", fake)
    monkeypatch.setattr(tm, "_PROBE", None)
    monkeypatch.setattr(game, "_krpc_up", lambda: True)
    monkeypatch.setattr(game.time, "sleep", lambda s: None)
    return fake


def fly_vessel(k, vessel):
    k.sc.vessels.append(vessel)
    k.sc.active_vessel = vessel
    return vessel


def call(tool_name, /, **args):
    return registry.validate_and_call(tool_name, args)


# ---------------------------------------------------------------------------------------------
# Conventions


def test_tools_registered_with_described_parameters():
    mine = {n: s for n, s in registry.TOOLS.items() if sys.modules[s.fn.__module__] in MY_MODULES}
    groups = {s.group for s in mine.values()}
    assert groups == {"observe", "game", "control", "crew", "autopilot"}
    assert {"telemetry", "vessel_parts", "body_info", "orbit_info", "target_info", "camera_look", "game_status",
            "game_launch", "control_stage", "control_attitude", "node_create", "crew_eva", "mj_ascent",
            "mj_abort"} <= set(mine)
    for name, spec in mine.items():
        assert spec.summary, name
        assert spec.group == sys.modules[spec.fn.__module__].__name__.rsplit(".", 1)[1] or spec.group in {
            "observe", "game", "control", "crew", "autopilot"}
        for p in spec.signature.parameters.values():
            descriptions = [getattr(m, "description", None) for m in getattr(p.annotation, "__metadata__", ())]
            assert any(descriptions), f"{name}.{p.name} has no description"
        registry.arg_model(spec).model_json_schema()
    for mod in MY_MODULES:
        assert "from __future__" not in inspect.getsource(mod)


@pytest.mark.parametrize("tool_name,params", [
    ("mj_ascent", ["altitude_m", "inclination_deg", "auto_path", "autostage", "skip_circularization", "limit_aoa", "watch"]),
    ("mj_execute_node", ["all_nodes", "autowarp", "lead_time_s", "autostage", "watch"]),
    ("mj_land", ["targeted", "touchdown_speed_mps", "deploy_gears", "deploy_chutes", "watch"]),
    ("mj_rendezvous", ["target", "desired_distance_m", "max_phasing_orbits", "max_closing_speed_mps", "watch"]),
    ("mj_dock", ["own_port", "target_port", "speed_limit_mps", "force_roll", "override_safe_distance", "watch"]),
    ("game_launch", ["craft", "site", "crew"]),
    ("node_create", ["ut", "prograde_mps", "normal_mps", "radial_mps"]),
])
def test_mission_parameters_have_no_defaults(tool_name, params):
    sig = registry.TOOLS[tool_name].signature
    for p in params:
        assert sig.parameters[p].default is inspect.Parameter.empty, f"{tool_name}.{p} must be chosen by the AI"


# ---------------------------------------------------------------------------------------------
# Name matching and resolvers


def test_match_names_tiers():
    assert observe.match_names(["Relay", "relay"], "relay") == [1]  # exact beats case-insensitive
    assert observe.match_names(["AI-Eve-Crew 飞船", "AI-Eve-Relay 飞船"], "ai-eve-crew") == [0]
    assert observe.match_names(["Lander飞船"], "Lander") == [0]  # CJK suffix may be glued on
    assert observe.match_names(["AI-Relay-1 Probe", "AI-Relay-12 Probe"], "AI-Relay-1") == [0]
    assert observe.match_names(["Keo 4", "Keo 45"], "keo") == [0, 1]  # prefix at a separator only
    assert observe.match_names(["AI-Relay-1", "AI-Relay-2"], "relay") == [0, 1]  # substring, ambiguous
    assert observe.match_names(["Mun Lander"], "Duna") == []
    assert observe._strip_suffix("airship") == "airship"  # ASCII suffix needs a separator
    assert observe._strip_suffix("x probe") == "x"


def test_pick_name_errors_list_candidates():
    with pytest.raises(AstraError) as amb:
        observe.pick_name(["AI-Relay-1", "AI-Relay-2", "Other"], "Relay", "vessel")
    assert "ambiguous" in amb.value.message and "AI-Relay-1" in amb.value.hint and "AI-Relay-2" in amb.value.hint
    with pytest.raises(AstraError) as none:
        observe.pick_name(["Alpha", "Beta"], "Gamma", "craft")
    assert "Alpha" in none.value.hint and "Beta" in none.value.hint


def test_resolve_vessel_by_id_name_and_exclusion(k):
    a = fly_vessel(k, Vessel("#autoLOC_501232"))
    b = Vessel("Relay 飞船")
    debris = Vessel("Relay 飞船 Debris", vtype=VesselType.debris)
    k.sc.vessels += [b, debris]
    assert observe.resolve_vessel(f"#{b._object_id}") == b
    assert observe.resolve_vessel("#autoLOC_501232") == a  # a stock name that starts with '#'
    assert observe.resolve_vessel("relay") == b  # suffix-stripped tier beats the debris prefix match
    with pytest.raises(AstraError) as err:
        observe.resolve_vessel("#autoLOC_501232", exclude=a)
    assert "no vessel" in err.value.message
    with pytest.raises(AstraError):
        observe.resolve_vessel("#999999")


def _stack_vessel():
    pod = Part("mk1pod.v2", seats=1, module_names={"ModuleCommand"})
    dec = decoupler(Part("Decoupler.1", parent=pod, stage=1, decouple_stage=1, tag="sep"))
    tank = Part("fuelTank", parent=dec, decouple_stage=1)
    eng = engine(Part("liquidEngine2.v2", parent=tank, stage=2, decouple_stage=1), active=True, available=200e3)
    tank2 = Part("fuelTank", parent=pod)
    return Vessel("Stack", [pod, dec, tank, eng, tank2], stage=2)


def test_resolve_part_by_idx_name_tag_and_ambiguity(k):
    v = fly_vessel(k, _stack_vessel())
    assert observe.resolve_part(v, 3)[1] == 3
    assert observe.resolve_part(v, " 1 ")[1] == 1
    assert observe.resolve_part(v, "liquidEngine2.v2")[1] == 3
    assert observe.resolve_part(v, "sep")[1] == 1
    assert observe.resolve_part(v, "MK1POD.V2")[1] == 0  # case-insensitive fallback
    with pytest.raises(AstraError) as amb:
        observe.resolve_part(v, "fuelTank")
    assert "idx 2" in amb.value.hint and "idx 4" in amb.value.hint
    with pytest.raises(AstraError) as rng:
        observe.resolve_part(v, 9)
    assert "out of range" in rng.value.message and "vessel_parts" in rng.value.hint
    with pytest.raises(AstraError):
        observe.resolve_part(v, "nothing")
    with pytest.raises(AstraError):
        observe.resolve_part(v, True)


# ---------------------------------------------------------------------------------------------
# observe


def test_vessel_parts_structure_and_filters(k):
    v = fly_vessel(k, _stack_vessel())
    v.resources = Obj(all=[Obj(part=v._parts[2], name="LiquidFuel", amount=90.0, max=180.0),
                           Obj(part=v._parts[0], name="ElectricCharge", amount=50.0, max=50.0)])
    out = call("vessel_parts")
    assert out["part_count"] == 5 and out["root_idx"] == 0 and out["controlling_idx"] == 0
    rows = {r["idx"]: r for r in out["parts"]}
    assert rows[3]["parent"] == 2 and rows[3]["engine"]["active"] is True
    assert rows[3]["engine"]["available_thrust_kn"] == 200.0
    assert rows[1]["decoupler"]["decoupled"] is False and rows[1]["tag"] == "sep"
    assert rows[0]["command"] == {"free_seats": 1}
    assert rows[2]["resources"] == {"LiquidFuel": [90.0, 180.0]}
    assert rows[0]["temp_frac"] == pytest.approx(0.2)
    engines = call("vessel_parts", kind="engine")
    assert [r["idx"] for r in engines["parts"]] == [3]
    assert [r["idx"] for r in call("vessel_parts", kind="tank")["parts"]] == [2]
    staged = call("vessel_parts", stage=1)
    assert {r["idx"] for r in staged["parts"]} == {1, 2, 3}
    with pytest.raises(AstraError) as err:
        call("vessel_parts", kind="wings")
    assert "engine" in err.value.hint


def test_telemetry_wrapper_adds_warp_control_and_retries(k, monkeypatch):
    fly_vessel(k, Vessel("V"))
    calls = []

    def snapshot(detail):
        calls.append(detail)
        if len(calls) == 1:
            raise RuntimeError("Instance not found")  # stale stream after staging
        return {"control": {}, "propulsion": {"throttle": 0.0, "thrust_kn": 120.0}}
    monkeypatch.setattr(tm, "snapshot", snapshot)
    out = call("telemetry", detail="full")
    assert calls == ["full", "full"]
    assert out["warp"]["rails_factor"] == 0 and out["control"]["state"] == "full"
    assert "solid" in out["note"]


def test_sample_max_terrain_finds_a_narrow_peak():
    def height(lat, lon):  # a 6767 m peak about 10 km wide on a gentle 1 km plateau
        d2 = (lat - 61.6) ** 2 + (lon - 46.35) ** 2
        return 1000.0 + 5767.0 * math.exp(-d2 / 0.3)
    found = observe.sample_max_terrain(height, 1.0)
    assert found["max_m"] == pytest.approx(6767.0, rel=0.002)
    assert found["lat_deg"] == pytest.approx(61.6, abs=0.05) and found["lon_deg"] == pytest.approx(46.35, abs=0.05)


def test_body_info_caches_terrain(k, monkeypatch, tmp_path):
    path = tmp_path / "terrain.json"
    monkeypatch.setattr(observe, "_terrain_cache_path", lambda: path)
    mun = k.sc.bodies["Mun"]
    samples = []
    mun.surface_height = lambda lat, lon: samples.append(1) or 7000.0 - abs(lat) - abs(lon)
    first = call("body_info", body="mun")
    assert first["body"] == "Mun" and first["atmosphere"] is None
    assert first["max_terrain"]["max_m"] == pytest.approx(7000.0, abs=5.0)
    count = len(samples)
    assert call("body_info", body="Mun")["max_terrain"] == first["max_terrain"] and len(samples) == count
    kerbin = call("body_info", body="Kerbin")
    assert kerbin["atmosphere"]["sea_level_pressure_atm"] == pytest.approx(1.0)
    assert kerbin["equator_surface_speed_mps"] == pytest.approx(2.9157e-4 * 600000.0)
    k._scene = "space_center"
    with pytest.raises(WrongScene):
        call("body_info")


def test_orbit_patch_chain_reports_encounter():
    kerbin, mun = Body("Kerbin"), Body("Mun", 200000.0, 0.0)
    mun.gravitational_parameter = 6.5138e10
    # hyperbolic Mun patch entered at UT 4600, 0.2 rad of mean anomaly before periapsis
    enc = Orbit(mun, periapsis_altitude=15000.0, apoapsis_altitude=-1e6, time_to_soi_change=math.nan,
                semi_major_axis=-400000.0, eccentricity=1.54, epoch=4600.0, mean_anomaly_at_epoch=-0.2)
    o = Orbit(kerbin, time_to_soi_change=3600.0, next_orbit=enc)
    chain = observe.patch_chain(o, 1000.0)
    assert len(chain) == 1
    assert chain[0]["body"] == "Mun" and chain[0]["start_ut"] == 4600.0 and chain[0]["soi_change_ut"] is None
    n = math.sqrt(6.5138e10 / 400000.0 ** 3)
    assert chain[0]["periapsis_alt_m"] == 15000.0 and chain[0]["periapsis_ut"] == pytest.approx(4600.0 + 0.2 / n)
    summary = observe.orbit_summary(Orbit(kerbin), 1000.0)
    assert summary["time_to_soi_change_s"] is None and summary["body_atmosphere_depth_m"] == 70000.0


def test_periapsis_ut_is_the_next_pass_not_a_past_one():
    kerbin = Body("Kerbin")
    ell = Orbit(kerbin, semi_major_axis=700000.0, eccentricity=0.1, epoch=4600.0, mean_anomaly_at_epoch=1.0)
    n = math.sqrt(kerbin.gravitational_parameter / 700000.0 ** 3)
    period = 2 * math.pi / n
    t = observe.periapsis_ut(ell, 4600.0)
    assert t == pytest.approx(4600.0 - 1.0 / n + period) and t >= 4600.0  # kRPC's ut_at_true_anomaly gives the past one
    assert observe.periapsis_ut(Orbit(kerbin, semi_major_axis=math.nan, epoch=0.0, mean_anomaly_at_epoch=0.0), 0.0) is None


def test_phase_angle_and_sampled_closest_approach():
    assert observe.phase_angle_deg((1, 0, 0), (0, 1, 0), (0, 1, 0)) == pytest.approx(90.0)
    assert observe.phase_angle_deg((1, 0, 0), (0, 1, 0), (0, -1, 0)) == pytest.approx(-90.0)
    assert observe.phase_angle_deg((1, 0, 0), (0, -1, 0), (0, -1, 0)) == pytest.approx(90.0)  # direction of motion
    t, d = observe.closest_approach_sampled(lambda t: abs(t - 33.3) + 5.0, 0.0, 100.0)
    assert t == pytest.approx(33.3, abs=0.05) and d == pytest.approx(5.0, abs=0.05)


def test_camera_look_uses_bridge_image_then_falls_back(k, monkeypatch, tmp_path):
    from PIL import Image

    monkeypatch.setattr(observe, "CONFIG", dataclasses.replace(observe.CONFIG, cache_dir=tmp_path))
    fly_vessel(k, Vessel("Cam"))
    buf = io.BytesIO()
    Image.new("RGB", (1920, 1080), (10, 20, 30)).save(buf, "PNG")
    k.bridge.replies[("POST", "/screenshot")] = {"image_b64": base64.b64encode(buf.getvalue()).decode()}
    pic = call("camera_look", width=640)
    assert isinstance(pic, registry.Picture) and pic.path.suffix == ".jpg"
    with Image.open(pic.path) as img:
        assert img.size == (640, 360)
    assert "via bridge" in pic.caption and "vessel=Cam" in pic.caption
    k.bridge.replies[("POST", "/screenshot")] = BridgeError("bridge /screenshot failed: Unknown route")
    shots = []

    def krpc_screenshot(path, scale):
        shots.append(k.paused)
        Image.new("RGB", (800, 600)).save(path, "PNG")
    k.sc.screenshot = krpc_screenshot
    monkeypatch.setattr(observe.time, "sleep", lambda s: None)
    paused_pic = call("camera_look", width=400)
    assert "via krpc" in paused_pic.caption and "pause menu" in paused_pic.caption and shots == [True]
    clean = call("camera_look", width=400, clean_frame=True)
    assert shots[-1] is False and k.paused is True and "pause menu" not in clean.caption
    k._scene = "space_center"
    with pytest.raises(WrongScene) as err:
        call("camera_look", width=640)
    assert "Unknown route" in err.value.hint


def test_target_info_relative_navigation(k):
    v = fly_vessel(k, Vessel("Chaser"))
    t = Vessel("Station")
    t.pos = (700000.0, 100.0, 0.0)
    t.velocity = lambda rf: (0.0, 2291.0, 0.0)
    t.orbit = Orbit(v.orbit.body, radius=680100.0)
    t.orbit.phase = 0.2
    k.sc.vessels.append(t)
    k.sc.target_vessel = t
    out = call("target_info")
    assert out["kind"] == "vessel" and out["distance_m"] == pytest.approx(100.0)
    assert out["relative"]["position_m"] == {"prograde": 100.0, "normal": 0.0, "radial_out": 0.0}
    assert out["relative"]["velocity_mps"]["prograde"] == pytest.approx(1.0)
    assert out["closing_speed_mps"] == pytest.approx(-1.0)  # opening
    assert out["same_soi"] and out["phase_angle_deg"] > 0  # ahead of us
    assert out["relative_inclination_deg"] == pytest.approx(2.0)
    assert out["ascending_node"]["time_to_s"] == pytest.approx(4000.0)
    assert out["descending_node"]["ut"] == pytest.approx(400.0 + 1880.0)  # rolled to the next orbit
    sampled = out["closest_approach_sampled"]
    chord = math.sqrt(680000.0 ** 2 + 680100.0 ** 2 - 2 * 680000.0 * 680100.0 * math.cos(0.2))
    assert sampled["window_s"] == 1880.0 and sampled["distance_m"] == pytest.approx(chord)
    assert out["closest_approach_krpc"]["distance_m"] == 50.0
    k.sc.target_vessel = None
    with pytest.raises(AstraError):
        call("target_info")


# ---------------------------------------------------------------------------------------------
# game


def test_game_status_never_raises_when_everything_is_down(k, monkeypatch):
    monkeypatch.setattr(game, "_krpc_up", lambda: False)
    k.bridge.is_up = False
    out = call("game_status")
    assert out["krpc_up"] is False and out["bridge_up"] is False and "astra up" in out["next_step"]
    k.bridge.is_up = True
    k.bridge.replies[("GET", "/state")] = {"scene": "MAINMENU", "saveFolder": "astra"}
    out = call("game_status")
    assert out["bridge"]["scene"] == "MAINMENU" and "game_load_save" in out["next_step"]


def test_game_status_in_flight(k):
    fly_vessel(k, Vessel("Orbiter", crew=["Jebediah Kerman"]))
    k.bridge.replies[("GET", "/state")] = {"scene": "FLIGHT", "saveFolder": "astra"}
    out = call("game_status")
    assert out["krpc_up"] and out["scene"] == "flight" and out["save"] == "astra"
    assert out["active_vessel"]["name"] == "Orbiter" and out["active_vessel"]["crew"] == ["Jebediah Kerman"]
    assert out["launchable_craft"] == {"VAB": 2, "SPH": 0}


def test_game_launch_validates_and_passes_an_explicit_crew_list(k):
    k._scene = "space_center"

    def on_launch(directory, name, site, recover, crew_list, flag):
        k._scene = "flight"
        fly_vessel(k, Vessel(f"{name} 飞船", situation=VesselSituation.pre_launch, crew=crew_list))
    k.sc.on_launch = on_launch
    out = call("game_launch", craft="orbiter", site="launchpad", crew=["Jebediah Kerman"])
    assert k.sc.launch_calls == [("VAB", "Orbiter 1", "LaunchPad", True, ["Jebediah Kerman"], "")]
    assert out["vessel"]["name"] == "Orbiter 1 飞船" and out["vessel"]["situation"] == "pre_launch"
    assert "crew_missing" not in out and out["paused"] is True
    k._scene = "space_center"
    call("game_launch", craft="Probe X", site="LaunchPad", crew=[])
    assert k.sc.launch_calls[-1][4] == []  # [] = KSP's default crew; None would not serialize
    k._scene = "space_center"
    with pytest.raises(AstraError) as busy:
        call("game_launch", craft="Orbiter 1", site="LaunchPad", crew=["Bill Kerman"])
    assert "assigned" in busy.value.message
    with pytest.raises(AstraError) as site:
        call("game_launch", craft="Orbiter 1", site="Moon Base", crew=[])
    assert "LaunchPad" in site.value.hint
    k._scene = "flight"
    with pytest.raises(WrongScene):
        call("game_launch", craft="Orbiter 1", site="LaunchPad", crew=[])


def test_game_switch_vessel_goes_through_the_bridge_by_persistent_id(k):
    a = fly_vessel(k, Vessel("Alpha"))
    b = Vessel("Bravo")
    twin = Vessel("Bravo", situation=VesselSituation.landed)  # same name: only the id is unambiguous
    k.sc.vessels += [b, twin]
    k.sc.rails_warp_factor = 4
    k.bridge.replies[("GET", "/vessels")] = {"vessels": [
        {"name": "Alpha", "persistentId": 1, "type": "Ship", "situation": "ORBITING", "body": "Kerbin"},
        {"name": "Bravo", "persistentId": 2, "type": "Ship", "situation": "ORBITING", "body": "Kerbin"},
        {"name": "Bravo", "persistentId": 3, "type": "Ship", "situation": "LANDED", "body": "Kerbin"}]}

    def fly(body):
        assert k.paused is False  # the switch completes while time runs
        k.sc.active_vessel = b
        return {"requested": True}
    k.bridge.replies[("POST", "/fly-vessel")] = fly
    out = call("game_switch_vessel", name=f"#{b._object_id}")
    assert k.bridge.calls[-1] == ("POST", "/fly-vessel", {"persistentId": 2})
    assert k.sc.active_vessel == b and k.sc.rails_warp_factor == 0
    assert out["switched"] and out["active_vessel"]["name"] == "Bravo" and k.paused is True
    assert call("game_switch_vessel", name=f"#{b._object_id}")["switched"] is False
    assert a != b


def test_game_switch_vessel_between_namesakes_waits_for_the_real_switch(k, monkeypatch):
    fly_vessel(k, Vessel("Bravo"))
    twin = Vessel("Bravo", situation=VesselSituation.landed)
    k.sc.vessels.append(twin)
    k.bridge.replies[("POST", "/fly-vessel")] = {"requested": True}  # KSP never switches
    real_wait = game.wait_for
    monkeypatch.setattr(game, "wait_for", lambda pred, timeout_s, interval_s=0.5: real_wait(pred, 0.01, 0.0))
    with pytest.raises(AstraError) as err:  # the vessel we left has the same name: not proof of a switch
        call("game_switch_vessel", name=f"#{twin._object_id}")
    assert "did not switch" in err.value.message and k.paused is True


def test_game_switch_vessel_without_bridge_only_in_physics_range(k):
    fly_vessel(k, Vessel("Alpha"))
    near, far = Vessel("Near"), Vessel("Far")
    near.pos, far.pos = (1500.0, 0.0, 0.0), (90000.0, 0.0, 0.0)
    k.sc.vessels += [near, far]
    k.bridge.is_up = False
    with pytest.raises(AstraError) as err:
        call("game_switch_vessel", name="Far")  # kRPC's setter would wait forever if KSP refused
    assert "bridge" in err.value.hint and k.sc.active_vessel.name == "Alpha"
    out = call("game_switch_vessel", name="Near")
    assert out["switched"] and k.sc.active_vessel == near and k.paused is True


def test_game_space_center_refuses_inside_atmosphere(k):
    v = fly_vessel(k, Vessel("Glider", situation=VesselSituation.flying))
    v.flight = lambda rf: Obj(mean_altitude=12000.0)
    with pytest.raises(AstraError) as err:
        call("game_space_center")
    assert "atmosphere" in err.value.message


def test_checkpoint_names_and_saved_ut(tmp_path):
    assert game._checkpoint_name("lko_1") == "astra_lko_1"
    with pytest.raises(AstraError):
        game._checkpoint_name("../evil")
    sfs = tmp_path / "astra_lko_1.sfs"
    sfs.write_text("GAME\n{\n\tFLIGHTSTATE\n\t{\n\t\tversion = 1.12.5\n\t\tUT = 1988968809.44\n\t}\n}\n", encoding="utf-8")
    assert game.saved_ut(sfs) == pytest.approx(1988968809.44)
    rows = game.list_checkpoints(tmp_path)
    assert rows[0]["name"] == "lko_1" and rows[0]["ut"] == pytest.approx(1988968809.44)


def test_game_log_filters_and_keeps_stack_traces(monkeypatch, tmp_path):
    log = tmp_path / "KSP.log"
    log.write_text("\n".join([
        "[LOG 10:00:00.000] hello",
        "[EXC 10:00:01.000] NullReferenceException: Object reference not set",
        "\tEditorLogic.FinalizeAnalytics () (at <x>:0)",
        "\tEditorLogic.launchVessel () (at <x>:0)",
        "[LOG 10:00:02.000] [AsteroidSpawner]: No new objects",
        "[ERR 10:00:03.000] Cannot find fx group",
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(game, "_log_path", lambda: log)
    out = call("game_log")
    assert out["matched"] == 2
    assert "FinalizeAnalytics" in out["entries"][0] and out["entries"][1].startswith("[ERR")
    assert call("game_log", pattern="asteroid", lines=5)["entries"] == ["[LOG 10:00:02.000] [AsteroidSpawner]: No new objects"]
    with pytest.raises(AstraError):
        call("game_log", pattern="(")
    assert game.tail_lines(log, 2)[-1].startswith("[ERR")


# ---------------------------------------------------------------------------------------------
# control


def test_separation_plan_keeps_the_controlling_side():
    # 0 pod <- 1 decoupler <- 2 tank <- 3 engine; 2 <- 4 radial decoupler <- 5 booster engine
    parent = {0: None, 1: 0, 2: 1, 3: 2, 4: 2, 5: 4}
    booster = control.separation_plan(parent, [4], {3, 5}, keep=0)
    assert booster["dropped"] == {4, 5} and booster["engines_kept"] == {3}
    payload = control.separation_plan(parent, [1], {3, 5}, keep=0)
    assert payload["kept"] == {0} and payload["engines_kept"] == set()
    from_below = control.separation_plan(parent, [1], {3, 5}, keep=3)
    assert from_below["dropped"] == {0} and from_below["engines_kept"] == {3, 5}


def test_control_stage_warns_and_reports_separation(k):
    v = fly_vessel(k, _stack_vessel())
    debris = Vessel("Stack Debris", vtype=VesselType.debris)
    k.sc.rails_warp_factor = 3

    def on_stage():
        assert k.paused is False  # kRPC's staging call never returns while the game is paused
        v._parts = [p for p in v._parts if p.name == "mk1pod.v2" or p.name == "fuelTank" and p.parent == v._parts[0]]
        return [debris]
    v.control.on_stage = on_stage
    out = call("control_stage")
    assert out["stage_before"] == 2 and out["stage_after"] == 1
    assert out["activated_parts"] == [{"idx": 1, "name": "Decoupler.1"}]
    assert out["warnings"] and "no engine remains" in out["warnings"][0]
    assert out["parts_before"] == 5 and out["parts_after"] == 2 and out["separation_confirmed"] is True
    assert out["jettisoned_vessels"] == ["Stack Debris"]
    assert out["paused"] is True and k.pause_log.count(False) == 1 and k.pause_log[-1] is True and k.sc.rails_warp_factor == 0


def test_control_stage_reports_newly_lit_engines(k):
    pod = Part("probeCoreOcto.v2", module_names={"ModuleCommand"})
    eng = engine(Part("liquidEngine3.v2", parent=pod, stage=1), active=False, available=60e3)
    v = fly_vessel(k, Vessel("Probe", [pod, eng], stage=2))

    def on_stage():
        assert k.paused is False
        eng.engine.active = True
        return []
    v.control.on_stage = on_stage
    out = call("control_stage")
    assert out["warnings"] == [] and "separation_confirmed" not in out and k.paused is True
    assert out["engines_lit"] == [{"name": "liquidEngine3.v2", "available_thrust_kn": 60.0, "solid": False}]
    v.control.current_stage = 0
    with pytest.raises(AstraError):
        call("control_stage")
    v.control.current_stage, v.control.stage_lock = 2, True
    with pytest.raises(AstraError) as locked:
        call("control_stage")
    assert "locked" in locked.value.message


def test_control_set_reports_refused_sas_mode(k):
    v = fly_vessel(k, Vessel("Probe", refuse_sas={"retrograde"}))
    v.thrust = 150e3
    ok = call("control_set", throttle=0.0, rcs=True)
    assert ok["state"]["throttle"] == 0.0 and ok["state"]["rcs"] is True
    assert any("solid" in w for w in ok["warnings"])
    v.auto_pilot.engaged = True
    with pytest.raises(AstraError) as err:
        call("control_set", sas=True, sas_mode="retrograde")
    assert "sas_mode" in err.value.message and "sas=True" in err.value.hint and "control_attitude" in err.value.hint
    assert v.auto_pilot.engaged is False and v.control.sas is True
    with pytest.raises(AstraError):
        call("control_set", throttle=1.5)
    call("control_set", sas=True, sas_mode="radial_in", speed_mode="surface")
    assert v.control.sas_mode == SASMode.anti_radial and v.control.speed_mode == SpeedMode.surface


def test_control_attitude_frames_and_validation(k):
    v = fly_vessel(k, Vessel("Ship"))
    out = call("control_attitude", mode="prograde", frame="orbital", roll_deg=0.0)
    ap = v.auto_pilot
    assert ap.engaged and ap.reference_frame == v.orbital_reference_frame and ap.target_direction == (0.0, 1.0, 0.0)
    assert ap.target_roll == 0.0 and v.control.sas is False and out["tracks"] is True
    assert out["authority"]["pitch_degps2"] == pytest.approx(math.degrees(10.0))
    assert out["authority"]["turn_90deg_min_s"] == pytest.approx(2 * math.sqrt(90 / math.degrees(5.0)))
    call("control_attitude", mode="retrograde", frame="surface")
    assert ap.reference_frame == v.surface_velocity_reference_frame and ap.target_direction == (0.0, -1.0, 0.0)
    assert math.isnan(ap.target_roll)
    v.pos = (-700000.0, 0.0, 0.0)  # frame x axis points toward the body here
    call("control_attitude", mode="radial_out")
    assert ap.target_direction == (-1.0, 0.0, 0.0)
    call("control_attitude", mode="hold", pitch_deg=45.0, heading_deg=90.0)
    assert ap.reference_frame == v.surface_reference_frame and ap.pitch_heading == (45.0, 90.0)
    for bad in (dict(mode="prograde"), dict(mode="hold", pitch_deg=10.0), dict(mode="vector", frame="orbital"),
                dict(mode="node"), dict(mode="sideways"), dict(mode="up", wait_aligned_deg=1.0)):
        with pytest.raises(AstraError):
            call("control_attitude", **bad)


def test_control_attitude_waits_for_alignment_then_pauses(k):
    v = fly_vessel(k, Vessel("Ship"))
    v.auto_pilot.errors = iter([30.0, 8.0, 0.9, 0.8, 0.7])
    out = call("control_attitude", mode="normal", wait_aligned_deg=1.0, timeout_s=60.0)
    assert out["aligned"] is True and out["error_deg"] == 0.7
    assert k.pause_log == [False, True] and out["paused"] is True


def test_control_attitude_sas_mode_refused(k):
    v = fly_vessel(k, Vessel("Probe", refuse_sas={"prograde"}))
    with pytest.raises(AstraError) as err:
        call("control_attitude", mode="sas:prograde")
    assert "autopilot" in err.value.hint
    out = call("control_attitude", mode="sas:stability_assist")
    assert v.control.sas is True and "stability_assist" in out["state"]
    call("control_attitude", mode="off")
    assert v.control.sas is False and v.auto_pilot.engaged is False


def test_control_part_actions(k):
    v = fly_vessel(k, _stack_vessel())
    out = call("control_part", action="thrust_limit", part=3, value=50)
    assert v._parts[3].engine.thrust_limit == 0.5 and out["state"]["thrust_limit_pct"] == 50.0
    small = call("control_part", action="thrust_limit", part="liquidEngine2.v2", value=0.5)
    assert v._parts[3].engine.thrust_limit == 0.005 and small["warnings"]
    with pytest.raises(AstraError):
        call("control_part", action="thrust_limit", part=3)
    with pytest.raises(AstraError) as err:
        call("control_part", action="arm", part=3)
    assert "engine" in err.value.hint  # lists the modules the part does have
    off = call("control_part", action="shutdown", part=3)
    assert v._parts[3].engine.active is False and off["module"] == "engine"
    lit = call("control_part", action="activate", part=3)
    assert lit["warnings"] and "stage counter" in lit["warnings"][0]
    call("control_part", action="control_from", part=0)
    assert v.parts.controlling == v._parts[0]


def test_control_part_decouple_warns_about_payload_side(k, monkeypatch):
    v = fly_vessel(k, _stack_vessel())
    fired = []
    v._parts[1].decoupler.decouple = lambda: fired.append(k.paused) or v._parts.__delitem__(slice(1, 4))
    out = call("control_part", action="decouple", part="sep")
    assert fired == [False] and out["parts_before"] == 5 and out["parts_after"] == 2
    assert "no engine remains" in out["warnings"][0] and out["paused"] is True and k.paused is True


def test_action_groups(k):
    v = fly_vessel(k, Vessel("Ship"))
    out = call("control_action_group", group=10, state="on")
    assert v.control.groups[0] is True and out == {"group": 10, "before": False, "after": True}
    assert call("control_action_group", group="3", state="toggle")["after"] is True
    assert call("control_action_group", group="Gear", state="toggle") == {"group": "gear", "before": False, "after": True}
    with pytest.raises(AstraError):
        call("control_action_group", group=11, state="on")


def test_nodes_create_list_delete(k):
    v = fly_vessel(k, Vessel("Ship"))
    v.available_thrust, v.specific_impulse, v.mass = 200e3, 300.0, 10e3
    with pytest.raises(AstraError):
        call("node_create", ut=900.0, prograde_mps=10.0, normal_mps=0.0, radial_mps=0.0)
    out = call("node_create", ut=1500.0, prograde_mps=100.0, normal_mps=0.0, radial_mps=0.0)
    ve = 300.0 * control.G0
    assert out["index"] == 0 and out["delta_v_mps"] == pytest.approx(100.0)
    assert out["burn"]["burn_time_s"] == pytest.approx(10e3 * ve / 200e3 * (1 - math.exp(-100.0 / ve)))
    call("node_create", ut=1200.0, prograde_mps=0.0, normal_mps=5.0, radial_mps=0.0)
    listed = call("node_list")["nodes"]
    assert [n["ut"] for n in listed] == [1200.0, 1500.0]
    assert call("node_delete", index=0) == {"removed": 1, "remaining": 1}
    with pytest.raises(AstraError):
        call("node_delete", index=5)
    assert call("node_delete", index="all")["remaining"] == 0
    v.available_thrust = 0.0
    assert call("node_create", ut=1300.0, prograde_mps=1.0, normal_mps=0.0, radial_mps=0.0)["burn"]["burn_time_s"] is None


def test_target_set_body_vessel_and_clear(k):
    v = fly_vessel(k, Vessel("Chaser"))
    t = Vessel("Station 空间站")
    t.pos = (700100.0, 0.0, 0.0)
    k.sc.vessels.append(t)
    k.sc.bodies["Mun"].position = lambda rf: (0.0, 1.2e7, 0.0)
    assert call("target_set", name="mun")["kind"] == "body" and k.sc.target_body == k.sc.bodies["Mun"]
    k.sc.clear_target()
    out = call("target_set", name="station")
    assert k.sc.target_vessel == t and out["distance_m"] == pytest.approx(100.0)
    assert call("target_set", name=None) == {"target": None} and k.sc.target_vessel is None
    with pytest.raises(AstraError):
        call("target_set", name="Chaser")  # a vessel cannot target itself
    assert v != t


# ---------------------------------------------------------------------------------------------
# crew


@pytest.fixture
def clock(k, monkeypatch):
    """Fake real time for the wait loops: each sleep advances it, and game time (UT) runs with it while
    the fake game is unpaused. Without it a timeout path spins for real seconds and never sees UT move."""
    now = {"t": 0.0}

    def sleep(s):
        now["t"] += s
        if not k.paused:
            k.sc.ut += s
    monkeypatch.setattr(game.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(game.time, "sleep", sleep)
    return now


def test_crew_walk_needs_exactly_one_destination_form(k):
    for bad in (dict(), dict(lat_deg=1.0), dict(lat_deg=1.0, lon_deg=2.0, bearing_deg=90.0, distance_m=10.0),
                dict(bearing_deg=90.0), dict(bearing_deg=90.0, distance_m=-5.0), dict(lat_deg=95.0, lon_deg=0.0)):
        with pytest.raises(AstraError):
            call("crew_walk", kerbal="Jeb", **bad)


def _eva_record(name, situation="LANDED", active=True, **extra):
    """A kerbal as the bridge's /eva-status reports it (kRPC's vessel list never shows EVA kerbals)."""
    return {"name": name, "vessel": {"name": name, "type": "EVA", "situation": situation},
            "isActiveVessel": active, "body": "Mun", "biome": "Midlands", "latitude": 1.5, "longitude": -20.25,
            "altitude_m": 612.0, "radarAltitude_m": 0.3, "surfaceSpeed_mps": 0.01, "fsmState": "Idle (Grounded)",
            "flagItems": 1, "canPlantFlag": True, **extra}


def test_run_until_bounds_game_time_even_when_the_check_fails(k, clock):
    assert crew._run_until(k, lambda: None, 3.0) == "timeout"
    assert 3.0 <= k.sc.ut - 1000.0 < 3.5 and k.paused is True

    def bridge_down():
        raise BridgeError("bridge /eva-status failed")
    ut0 = k.sc.ut
    assert crew._run_until(k, bridge_down, 2.0) == "timeout"
    assert 2.0 <= k.sc.ut - ut0 < 2.5 and k.paused is True
    assert crew._run_until(k, lambda: {"name": "Jeb"}, 2.0) == {"name": "Jeb"}


def test_standing_and_on_ground_tests():
    assert crew._standing(_eva_record("Jeb", landed=True))
    assert not crew._standing(_eva_record("Jeb", fsmState="Low G Bound (Grounded - Arcade/FPS)"))  # mid-bounce
    assert not crew._standing(_eva_record("Jeb", fsmState="Idle (Floating)"))
    assert crew._standing(_eva_record("Jeb", situation="SPLASHED", splashed=True, fsmState="Swim (Idle)"))
    # At the launch site KSP keeps PRELAUNCH while the kerbal stands: the Landed flag decides.
    assert crew._standing(_eva_record("Jeb", situation="PRELAUNCH", landed=True, splashed=False))
    assert not crew._on_ground(_eva_record("Jeb", situation="LANDED", landed=False, splashed=False))  # on a ladder
    assert crew._on_ground(_eva_record("Jeb", situation="PRELAUNCH"))  # a record without the flags


def _eva_from(k, vessel, **record):
    """Fly `vessel` with the bridge's /eva-go putting its kerbal outside as `record` describes."""
    fly_vessel(k, vessel)
    kerbals = []
    k.bridge.replies[("GET", "/eva-status")] = lambda body: {"kerbals": list(kerbals)}

    def eva_go(body):
        kerbals.append(_eva_record(body["crew"], **record))
        k.sc.active_vessel = Vessel(body["crew"], vtype=VesselType.eva, situation=vessel.situation)
        return {"biome": "Midlands", "warnings": [], "fromPart": {"index": 0, "name": "mk1pod"}}
    k.bridge.replies[("POST", "/eva-go")] = eva_go

    def hop(body):
        kerbals[-1]["hop"] = {"state": "released", "distance_m": 0.5}
        return {"jetpackFuel": 5.0}
    k.bridge.replies[("POST", "/eva-hop")] = hop
    return kerbals


def _posted(k, path):
    return [c[2] for c in k.bridge.calls if c[1] == path]


def test_crew_eva_verifies_through_the_bridge(k):
    _eva_from(k, Vessel("Lander", situation=VesselSituation.landed, crew=["Jebediah Kerman", "Bill Kerman"]))
    out = call("crew_eva", kerbal="jeb")
    assert ("POST", "/eva-go", {"crew": "Jebediah Kerman"}) in k.bridge.calls
    assert out["verified"] is True and out["eva"]["name"] == "Jebediah Kerman"
    # From the ground, time ran until the kerbal stood; paused again.
    assert False in k.pause_log and k.pause_log[-1] is True and out["paused"] is True
    assert out["eva"]["situation"] == "landed" and out["biome"] == "Midlands"
    assert out["active_vessel"] == "Jebediah Kerman" and not _posted(k, "/eva-hop")


def test_crew_eva_from_the_water_settles_when_the_kerbal_swims(k, clock):
    _eva_from(k, Vessel("Capsule", situation=VesselSituation.splashed, crew=["Jebediah Kerman"]),
              situation="SPLASHED", splashed=True, landed=False, fsmState="Swim (Idle)")
    out = call("crew_eva", kerbal="jeb")
    assert out["verified"] is True and out.get("game_s_elapsed", 0.0) < 1.0 and out["paused"] is True


def test_crew_eva_hop_clear_flies_straight_out_from_the_crew_part(k, clock):
    lander = Vessel("Lander", [Part("mk1pod", seats=1)], situation=VesselSituation.landed, crew=["Jebediah Kerman"])
    # The vessel's position (flight) is lon 0; the pod's centre is 1 m east of it and the kerbal came
    # out of a hatch facing west, back over the vessel: straight out is west, not away from the vessel.
    lander.parts.all[0].position = lambda rf: (1.0, 2.0, 3.0)
    lander.orbit.body.latitude_at_position = lambda pos, rf: 0.0
    lander.orbit.body.longitude_at_position = lambda pos, rf: 0.0003
    _eva_from(k, lander, latitude=0.0, longitude=0.0002)
    out = call("crew_eva", kerbal="jeb", hop_clear_m=6.0)
    [hop] = _posted(k, "/eva-hop")
    assert hop["bearing"] == pytest.approx(270.0) and hop["distance"] == 6.0 and hop["rise"] == 1.0
    assert hop["maxS"] == pytest.approx(10.0 + 6.0 / 1.2 + 2.0)  # derived from the distance, not fixed
    assert out["hop"]["hop"]["state"] == "released" and out["verified"] is True and out["paused"] is True

    k.bridge.calls.clear()  # without a usable part position: from the vessel's position to the kerbal
    lander.orbit.body.longitude_at_position = None
    lander.crew.append(Obj(name="Bill Kerman", trait="Pilot", experience=0.0))
    k.sc.active_vessel = lander
    call("crew_eva", kerbal="bill", hop_clear_m=6.0)
    assert _posted(k, "/eva-hop")[0]["bearing"] == pytest.approx(90.0)


def test_crew_eva_reports_a_hop_that_cannot_fly_instead_of_raising(k, clock):
    kerbals = _eva_from(k, Vessel("Lander", situation=VesselSituation.landed, crew=["Jebediah Kerman"]),
                        latitude=0.0, longitude=0.0001)
    k.bridge.replies[("POST", "/eva-hop")] = BridgeError("bridge /eva-hop failed: Jebediah Kerman carries no jetpack.")
    out = call("crew_eva", kerbal="jeb", hop_clear_m=6.0)
    assert out["verified"] is True and "no jetpack" in out["hop_error"] and out["eva"]["name"] == "Jebediah Kerman"
    assert kerbals and out["paused"] is True  # the kerbal is outside: the result says so

    for record, reason in ((dict(hasJetpack=False), "no jetpack"), (dict(jetpackFuel=0.0), "propellant"),
                           (dict(longitude=0.000001), "no direction")):  # came out 0.1 m from the vessel centre
        k.bridge.calls.clear()
        _eva_from(k, Vessel("Lander", situation=VesselSituation.landed, crew=["Jebediah Kerman"]),
                  **{"latitude": 0.0, "longitude": 0.0001, **record})
        out = call("crew_eva", kerbal="jeb", hop_clear_m=6.0)
        assert reason in out["hop_skipped"] and not _posted(k, "/eva-hop") and out["verified"] is True


def test_crew_eva_hop_clear_needs_the_ground_and_a_position(k):
    _eva_from(k, Vessel("Orbiter", situation=VesselSituation.orbiting, crew=["Jebediah Kerman"]))
    with pytest.raises(AstraError, match="on the ground"):
        call("crew_eva", kerbal="jeb", hop_clear_m=6.0)
    lander = Vessel("Lander", situation=VesselSituation.landed, crew=["Jebediah Kerman"])

    def no_flight(rf):
        raise RuntimeError("stale proxy")
    lander.flight = no_flight
    _eva_from(k, lander)
    with pytest.raises(AstraError, match="still aboard"):
        call("crew_eva", kerbal="jeb", hop_clear_m=6.0)
    assert not _posted(k, "/eva-go")  # refused before anything irreversible


def test_crew_hop_bearings_and_guards(k, clock):
    fly_vessel(k, Vessel("Jebediah Kerman", vtype=VesselType.eva, situation=VesselSituation.landed))
    k.sc.vessels.append(Vessel("Lander", situation=VesselSituation.landed))  # its flight() is at lat 0, lon 0
    rec = _eva_record("Jebediah Kerman", latitude=0.0, longitude=0.0001)  # due east of the lander
    k.bridge.replies[("GET", "/eva-status")] = lambda body: {"kerbals": [rec]}

    def hop(body):
        rec["hop"] = {"state": "released"}
        return {"jetpackFuel": 5.0}
    k.bridge.replies[("POST", "/eva-hop")] = hop
    out = call("crew_hop", kerbal="jeb", distance_m=8.0, rise_m=2.0, away_from="lander")
    [posted] = _posted(k, "/eva-hop")
    assert posted["bearing"] == pytest.approx(90.0) and posted["rise"] == 2.0
    assert posted["maxS"] == pytest.approx(10.0 + 8.0 / 1.2 + 4.0)
    assert out["bearing_deg"] == pytest.approx(90.0) and out["hop"]["state"] == "released"
    assert False in k.pause_log and out["paused"] is True
    call("crew_hop", kerbal="jeb", distance_m=3.0, rise_m=0.5, bearing_deg=-90.0)
    assert _posted(k, "/eva-hop")[-1]["bearing"] == pytest.approx(270.0)
    for forms in (dict(), dict(bearing_deg=90.0, away_from="Lander")):
        with pytest.raises(AstraError, match="either"):
            call("crew_hop", kerbal="jeb", distance_m=3.0, rise_m=1.0, **forms)
    # The cruise height depends on the obstacle: the AI must choose it.
    assert registry.TOOLS["crew_hop"].signature.parameters["rise_m"].default is inspect.Parameter.empty

    # Floating in orbit (or moving fast over the ground) the hop controller would burn the pack at
    # full thrust for its whole bound: refused without posting.
    k.bridge.calls.clear()
    for record, needle in ((dict(situation="ORBITING", surfaceSpeed_mps=170.0), "orbiting"),
                           (dict(situation="FLYING", surfaceSpeed_mps=12.0), "12 m/s"),
                           (dict(hasJetpack=False), "no jetpack")):
        rec = _eva_record("Jebediah Kerman", **record)
        with pytest.raises(AstraError, match=needle):
            call("crew_hop", kerbal="jeb", distance_m=5.0, rise_m=1.0, bearing_deg=90.0)
    assert not _posted(k, "/eva-hop")


def _walker(k, **record):
    """A kerbal whose walk ends as `script` says (by game time), and the /eva-walk-to it obeys."""
    fly_vessel(k, Vessel("Jebediah Kerman", vtype=VesselType.eva, situation=VesselSituation.landed))
    rec = _eva_record("Jebediah Kerman", walk=None, **record)
    script = {"end_ut": math.inf, "end": "arrived", "gone_ut": math.inf}

    def status(body):
        if k.sc.ut >= script["gone_ut"]:
            return {"kerbals": []}
        if (rec["walk"] or {}).get("state") == "walking" and k.sc.ut >= script["end_ut"]:
            rec["walk"] = {"state": script["end"]}
        return {"kerbals": [dict(rec)]}

    def walk_to(body):
        rec["walk"] = {"state": "stopped" if body.get("stop") else "walking"}
        return {"targetLatitude": 1.6, "targetLongitude": -20.25, "distance_m": 12.0}
    k.bridge.replies[("GET", "/eva-status")] = status
    k.bridge.replies[("POST", "/eva-walk-to")] = walk_to
    return script


def test_crew_walk_runs_until_arrived_or_stalled_then_pauses(k, clock):
    script = _walker(k)
    for end in ("arrived", "stalled"):
        script.update(end_ut=k.sc.ut + 5.0, end=end)
        out = call("crew_walk", kerbal="jeb", bearing_deg=0.0, distance_m=12.0)
        assert out["walk"]["state"] == end and 5.0 <= out["game_s_elapsed"] < 5.5
        assert out["paused"] is True and k.paused is True and "timed_out" not in out
    assert not any(b.get("stop") for b in _posted(k, "/eva-walk-to"))


def test_crew_walk_timeout_cancels_the_order(k, clock):
    _walker(k)
    out = call("crew_walk", kerbal="jeb", bearing_deg=0.0, distance_m=12.0, max_game_s=4.0)
    assert _posted(k, "/eva-walk-to")[-1] == {"crew": "Jebediah Kerman", "stop": True}
    assert out["timed_out"] is True and out["walk"]["state"] == "stopped"
    assert 4.0 <= out["game_s_elapsed"] < 4.5 and out["paused"] is True


def test_crew_walk_kerbal_lost_mid_walk_ends_on_the_game_time_bound(k, clock):
    script = _walker(k)
    script["gone_ut"] = k.sc.ut + 2.0  # killed in a fall, or dropped out of physics range
    out = call("crew_walk", kerbal="jeb", bearing_deg=0.0, distance_m=12.0, max_game_s=10.0)
    assert out["lost"] is True and out["walk"] is None and out["game_s_elapsed"] < 2.5
    assert not any(b.get("stop") for b in _posted(k, "/eva-walk-to"))  # nothing to stop
    assert out["paused"] is True and clock["t"] < 5.0  # no run-on to the real-time bound


def test_crew_walk_and_flag_use_the_bridge_ground_test(k, clock):
    _walker(k, situation="PRELAUNCH", landed=True, splashed=False)  # stepped out at the launch site
    out = call("crew_walk", kerbal="jeb", bearing_deg=0.0, distance_m=12.0, wait=False)
    assert out["target"]["distance_m"] == 12.0
    _walker(k, situation="FLYING", landed=False, splashed=False, radarAltitude_m=30.0)
    with pytest.raises(AstraError, match="not standing on the ground"):
        call("crew_walk", kerbal="jeb", bearing_deg=0.0, distance_m=12.0)
    with pytest.raises(AstraError, match="standing on the ground"):
        call("crew_plant_flag", kerbal="jeb", name="Up High")


def test_crew_status_reads_the_bridge(k):
    fly_vessel(k, Vessel("Valentina Kerman", vtype=VesselType.eva, situation=VesselSituation.landed))
    near = {"persistentId": 5, "index": 0, "title": "Mk1 Pod", "vessel": {"name": "Lander"},
            "distance_m": 0.8, "withinBoardingReach": True}
    k.bridge.replies[("GET", "/eva-status")] = {"kerbals": [
        _eva_record("Bill Kerman", active=False), _eva_record("Valentina Kerman", situation="PRELAUNCH", nearestHatch=near)]}
    out = call("crew_status")  # None = the active kerbal
    assert out["name"] == "Valentina Kerman" and out["situation"] == "pre_launch" and out["on_ground"] is True
    assert out["state"] == "Idle (Grounded)" and out["can_plant_flag"] is True
    assert out["nearest_hatch"] == {"part": "Mk1 Pod", "index": 0, "vessel": "Lander", "distance_m": 0.8,
                                    "within_boarding_reach": True}
    assert call("crew_status", kerbal="bill")["name"] == "Bill Kerman"
    k.bridge.replies[("GET", "/eva-status")] = {"kerbals": []}
    with pytest.raises(AstraError):
        call("crew_status", kerbal="bill")


def test_crew_board_and_plant_flag_verify_outcomes(k):
    lander = Vessel("Lander", situation=VesselSituation.landed)
    fly_vessel(k, Vessel("Jebediah Kerman", vtype=VesselType.eva, situation=VesselSituation.landed))
    k.sc.vessels.append(lander)
    kerbals = [_eva_record("Jebediah Kerman")]
    vessels = [{"name": "Lander", "persistentId": 77, "type": "Ship", "situation": "LANDED", "body": "Mun"},
               {"name": "Old Flag", "persistentId": 3, "type": "Flag", "situation": "LANDED", "body": "Mun"}]
    k.bridge.replies[("GET", "/eva-status")] = lambda body: {"kerbals": list(kerbals)}
    k.bridge.replies[("GET", "/vessels")] = lambda body: {"vessels": list(vessels)}

    def plant(body):
        # The bridge accepts a plant while paused but only queues it (pending): the tool posts it
        # while game time runs, because the animation and the naming need it.
        assert not k.paused, "crew_plant_flag must post while game time runs"
        vessels.append({"name": body["siteName"], "persistentId": 99, "type": "Flag", "situation": "LANDED", "body": "Mun"})
        return {"watchId": 1, "state": "named", "verified": True}
    k.bridge.replies[("POST", "/eva-plant-flag")] = plant
    flag = call("crew_plant_flag", kerbal="jeb", name="First Steps")
    assert ("POST", "/eva-plant-flag", {"crew": "Jebediah Kerman", "siteName": "First Steps", "plaque": "",
                                        "waitS": crew._FLAG_WAIT_S}) in k.bridge.calls
    assert flag["planted"] is True and flag["named"] is True and flag["flag"]["name"] == "First Steps"
    assert flag["flag"]["body"] == "Mun" and flag["flag"]["lat_deg"] == 1.5
    assert k.paused is True and flag["paused"] is True and "note" not in flag

    hatch = {"persistentId": 4242, "index": 0, "name": "mk1pod.v2", "title": "Mk1 Pod"}
    k.bridge.replies[("POST", "/eva-status")] = {"kerbal": {"hatchPart": None, "nearestHatch": {
        **hatch, "vessel": {"name": "Lander", "persistentId": 77}, "distance_m": 1.2, "withinBoardingReach": True}}}

    def board(body):
        kerbals.clear()
        lander.crew.append(Obj(name=body["crew"], trait="Pilot", experience=0.0))
        k.sc.active_vessel = lander
        return {"boarded": True}
    k.bridge.replies[("POST", "/eva-board")] = board
    out = call("crew_board", kerbal="Jebediah Kerman", part="nearest")
    assert ("POST", "/eva-board", {"crew": "Jebediah Kerman", "partId": 4242}) in k.bridge.calls  # by persistentId
    assert out["boarded"] is True and out["vessel"] == "Lander" and out["crew"] == ["Jebediah Kerman"]
    assert out["part"]["name"] == "mk1pod.v2" and out["paused"] is True


def _flag_site(k, **record):
    """A kerbal on the Mun, the bridge's flag vessels, and its flag watches (GET /eva-status)."""
    fly_vessel(k, Vessel("Jebediah Kerman", vtype=VesselType.eva, situation=VesselSituation.landed))
    site = {"rec": _eva_record("Jebediah Kerman", **record), "watches": [],
            "vessels": [{"name": "Lander", "persistentId": 77, "type": "Ship", "situation": "LANDED", "body": "Mun"}]}
    k.bridge.replies[("GET", "/eva-status")] = lambda body: {"kerbals": [site["rec"]], "flagPlanting": site["watches"]}
    k.bridge.replies[("GET", "/vessels")] = lambda body: {"vessels": list(site["vessels"])}
    return site


def test_crew_plant_flag_lets_a_bouncing_kerbal_stand_first(k, clock):
    site = _flag_site(k, situation="FLYING", landed=False, radarAltitude_m=0.8, fsmState="Low G Bound (floating)")
    ut_down = k.sc.ut + 2.0

    def status(body):  # comes down after 2 s of game time
        if k.sc.ut >= ut_down:
            site["rec"].update(vessel={"name": "Jebediah Kerman", "situation": "LANDED"}, landed=True,
                               fsmState="Idle (Grounded)")
        return {"kerbals": [site["rec"]], "flagPlanting": []}
    k.bridge.replies[("GET", "/eva-status")] = status

    def plant(body):
        site["vessels"].append({"name": body["siteName"], "persistentId": 99, "type": "Flag", "situation": "LANDED"})
        return {"watchId": 1, "state": "named", "verified": True}
    k.bridge.replies[("POST", "/eva-plant-flag")] = plant
    out = call("crew_plant_flag", kerbal="jeb", name="Bounce")
    assert out["planted"] is True and out["named"] is True and out["game_s_elapsed"] < 1.0
    assert k.sc.ut >= ut_down and out["paused"] is True  # the settle ran game time before ut0


def test_crew_plant_flag_names_what_blocks_it(k, clock):
    _flag_site(k, canPlantFlag=False, plantBlocker="not standing still (Walk (Arcade))")
    with pytest.raises(AstraError, match="not standing still") as blocked:
        call("crew_plant_flag", kerbal="jeb", name="Nope")
    assert "can_plant_flag" in blocked.value.hint and not _posted(k, "/eva-plant-flag") and k.paused is True


def test_crew_plant_flag_at_the_launch_site(k, clock):
    site = _flag_site(k, situation="PRELAUNCH", landed=True, splashed=False)
    k.bridge.replies[("POST", "/eva-plant-flag")] = lambda body: site["vessels"].append(
        {"name": body["siteName"], "persistentId": 99, "type": "Flag", "situation": "PRELAUNCH"}) or {"verified": True}
    assert call("crew_plant_flag", kerbal="jeb", name="Pad Flag")["planted"] is True


def test_crew_plant_flag_on_a_slow_game_waits_for_the_name(k, clock):
    site = _flag_site(k)
    watch = {"watchId": 7, "state": "placing", "verified": False}

    def plant(body):  # the flag stands under KSP's default name; the bridge names it later
        site["vessels"].append({"name": "Jebediah Kerman's Flag", "persistentId": 99, "type": "Flag",
                                "situation": "LANDED", "body": "Mun"})
        site["watches"] = [watch]
        return {**watch, "pending": True, "note": "Still planting after 20 s"}
    k.bridge.replies[("POST", "/eva-plant-flag")] = plant
    out = call("crew_plant_flag", kerbal="jeb", name="First Steps")  # never named within the bound
    assert out["planted"] is True and out["named"] is False and out["plant_state"] == "placing"
    assert out["flag"]["name"] == "Jebediah Kerman's Flag" and "not named it yet" in out["note"]
    assert crew._FLAG_WAIT_S <= out["game_s_elapsed"] < crew._FLAG_WAIT_S + 1.0 and out["paused"] is True

    site["vessels"] = site["vessels"][:1]
    ut_named = k.sc.ut + 2.0

    def status(body):  # named after 2 s of game time
        if k.sc.ut >= ut_named and watch["state"] != "named":
            watch.update(state="named", verified=True, flag={"name": "First Steps", "plaque": "We came"})
            site["vessels"][-1]["name"] = "First Steps"
        return {"kerbals": [site["rec"]], "flagPlanting": site["watches"]}
    k.bridge.replies[("GET", "/eva-status")] = status
    out = call("crew_plant_flag", kerbal="jeb", name="First Steps", plaque="We came")
    assert out["named"] is True and out["flag"]["name"] == "First Steps" and out["flag"]["plaque"] == "We came"
    assert 2.0 <= out["game_s_elapsed"] < 3.0 and "note" not in out


def test_crew_board_refused_by_ksp_runs_no_game_time(k, clock):
    fly_vessel(k, Vessel("Jebediah Kerman", vtype=VesselType.eva, situation=VesselSituation.landed))
    k.bridge.replies[("GET", "/eva-status")] = {"kerbals": [_eva_record("Jebediah Kerman")]}
    k.bridge.replies[("POST", "/eva-status")] = {"kerbal": {"hatchPart": {
        "persistentId": 4242, "index": 0, "name": "mk1pod.v2"}, "hatchVessel": {"name": "Lander", "persistentId": 77}}}
    k.bridge.replies[("POST", "/eva-board")] = {
        "boarded": False, "distanceToHatch_m": 0.8,
        "note": "Not seated yet: KSP is probably waiting on a dialog about carried science."}
    out = call("crew_board", kerbal="jeb", part="nearest")
    assert out["boarded"] is False and False not in k.pause_log  # a dialog is open: time cannot help
    assert "carried science" in out["note"] and "crew_walk" not in out["note"] and "camera_look" in out["note"]
    assert out["paused"] is True


def test_crew_board_named_part_and_out_of_reach(k, monkeypatch):
    fly_vessel(k, Vessel("Valentina Kerman", vtype=VesselType.eva, situation=VesselSituation.landed))
    k.bridge.replies[("GET", "/eva-status")] = {"kerbals": [_eva_record("Valentina Kerman")]}
    monkeypatch.setattr(crew, "_settle", lambda k, done, real_s: (None, 0.0))  # the fake never boards
    near = {"persistentId": 5, "index": 0, "name": "mk1pod.v2", "vessel": {"name": "Base", "persistentId": 9},
            "distance_m": 14.0, "withinBoardingReach": False}
    k.bridge.replies[("POST", "/eva-status")] = {"kerbal": {"hatchPart": None, "nearestHatch": near}}
    with pytest.raises(AstraError) as far:
        call("crew_board", kerbal="Valentina Kerman", part="nearest")
    assert "14.0 m" in far.value.message and "crew_walk" in far.value.hint
    k.bridge.replies[("GET", "/vessel-parts?vesselPersistentId=9")] = {"parts": [
        {"index": 0, "persistentId": 5, "name": "mk1pod.v2", "crewCapacity": 1, "crew": ["Bob Kerman"]},
        {"index": 3, "persistentId": 6, "name": "crewCabin", "crewCapacity": 4, "crew": []},
        {"index": 4, "persistentId": 7, "name": "fuelTank", "crewCapacity": 0, "crew": []}]}
    k.bridge.replies[("POST", "/eva-board")] = {"note": "boarding"}
    def boarded_part():
        return next(c[2]["partId"] for c in reversed(k.bridge.calls) if c[1] == "/eva-board")
    call("crew_board", kerbal="Valentina Kerman", part="crewcabin")
    assert boarded_part() == 6
    call("crew_board", kerbal="Valentina Kerman", part="0")
    assert boarded_part() == 5
    with pytest.raises(AstraError) as missing:
        call("crew_board", kerbal="Valentina Kerman", part="fuelTank")
    assert "crewCabin" in missing.value.hint


def test_crew_transfer_checks_seats(k):
    pod = Part("mk1-3pod", seats=0, module_names={"ModuleCommand"})
    cabin = Part("crewCabin", parent=pod, seats=2, module_names={"ModuleCommand"})
    fly_vessel(k, Vessel("Station", [pod, cabin], crew=["Bob Kerman"]))
    with pytest.raises(AstraError) as full:
        call("crew_transfer", kerbal="Bob", to_part=0)
    assert "free seat" in full.value.message
    transfer, paused_during = k.sc.transfer_crew, []
    k.sc.transfer_crew = lambda member, part: paused_during.append(k.paused) or transfer(member, part)
    out = call("crew_transfer", kerbal="Bob", to_part="crewCabin")
    assert k.sc.transfers == [("Bob Kerman", "crewCabin")] and paused_during == [False] and k.paused is True
    assert out["moved"] is True and out["free_seats_before"] == 2 and out["free_seats_after"] == 1
    assert out["paused"] is True


# ---------------------------------------------------------------------------------------------
# autopilot


def test_module_enabled_reads_both_status_shapes():
    assert autopilot.module_enabled({"modules": {"ascent": {"enabled": True}}}, "ascent") is True
    assert autopilot.module_enabled({"modules": {"dock": False}}, "dock") is False
    assert autopilot.module_enabled({"ascentEnabled": False, "rvEnabled": True}, "rendezvous") is True
    assert autopilot.module_enabled({"ascentEnabled": False}, "ascent") is False
    assert autopilot.module_enabled({"flight": True}, "smartass") is None
    # the bridge reports SmartASS by its target mode, not an enabled flag
    assert autopilot.module_enabled({"modules": {"smartass": {"target": "PROGRADE"}}}, "smartass") is True
    assert autopilot.module_enabled({"modules": {"smartass": {"target": "OFF"}}}, "smartass") is False


ASCENT_MANUAL = dict(altitude_m=80000.0, inclination_deg=0.0, auto_path=False, autostage=False,
                     skip_circularization=False, limit_aoa=False, watch=False, turn_start_altitude_m=500.0,
                     turn_start_velocity_mps=60.0, turn_end_altitude_m=45000.0, turn_end_angle_deg=0.0,
                     turn_shape_exponent=0.4)
ASCENT_AUTO = {**ASCENT_MANUAL, "auto_path": True, "turn_start_altitude_m": None, "turn_start_velocity_mps": None,
               "turn_end_altitude_m": None, "turn_end_angle_deg": None, "turn_shape_exponent": None,
               "auto_turn_percent": 0.05, "auto_turn_speed_factor": 18.5}


@pytest.mark.parametrize("change,needle", [
    (dict(turn_end_angle_deg=None), "turn shape"),
    (dict(auto_path=True), "leave the turn_* arguments out"),
    (dict(turn_shape_exponent=1.5), "0..1"),
    (dict(auto_turn_percent=0.05), "leave auto_turn_* out"),
    (dict(limit_aoa=True), "max_aoa_deg"),
    (dict(autostage=True), "autostage_pre_delay_s"),
    (dict(watch=True), "max_game_s"),
])
def test_mj_ascent_rejects_incomplete_settings(k, change, needle):
    fly_vessel(k, Vessel("Rocket", situation=VesselSituation.pre_launch))
    with pytest.raises(AstraError) as err:
        call("mj_ascent", **{**ASCENT_MANUAL, **change})
    assert needle in err.value.message
    assert not any(path == "/mj-ascent" for _, path, _ in k.bridge.calls)


def test_mj_ascent_auto_path_needs_its_own_settings(k):
    fly_vessel(k, Vessel("Rocket", situation=VesselSituation.pre_launch))
    with pytest.raises(AstraError) as err:  # otherwise MechJeb silently reuses whatever it stored last
        call("mj_ascent", **{**ASCENT_AUTO, "auto_turn_speed_factor": None})
    assert "auto_turn_speed_factor" in err.value.message
    with pytest.raises(AstraError):
        call("mj_ascent", **{**ASCENT_AUTO, "auto_turn_percent": 5.0})


def test_mj_ascent_sends_every_setting_and_kicks_ignition(k):
    pod = Part("mk1pod.v2", module_names={"ModuleCommand"})
    eng = engine(Part("liquidEngine2.v2", parent=pod, stage=1), active=False, available=200e3)
    v = fly_vessel(k, Vessel("Rocket", [pod, eng], situation=VesselSituation.pre_launch, stage=2))
    staged_while = []
    v.control.on_stage = lambda: staged_while.append(k.paused) or setattr(eng.engine, "active", True) or []
    k.bridge.replies[("GET", "/mj-status")] = {"hasCore": True}
    k.bridge.replies[("POST", "/mj-ascent")] = lambda body: {"engaged": True, "effective": {}}
    out = call("mj_ascent", **ASCENT_MANUAL)
    method, path, body = next(c for c in k.bridge.calls if c[1] == "/mj-ascent")
    assert body == {"ascentType": "classic", "altitude": 80000.0, "inclination": 0.0, "autoPath": False,
                    "autostage": False, "skipCircularization": False, "limitAoA": False, "turnStartAltitude": 500.0,
                    "turnStartVelocity": 60.0, "turnEndAltitude": 45000.0, "turnEndAngle": 0.0,
                    "turnShapeExponent": 0.4}
    assert out["ignition"]["staged"] is True and out["ignition"]["engines_lit"] == ["liquidEngine2.v2"]
    assert staged_while == [False]  # the ignition kick runs with game time (staging blocks while paused)
    assert v.control.current_stage == 1 and out["paused"] is True and "note" in out
    k.bridge.calls.clear()
    out = call("mj_ascent", **ASCENT_AUTO)
    assert out["ignition"]["staged"] is False  # an engine is already running
    body = next(c for c in k.bridge.calls if c[1] == "/mj-ascent")[2]
    assert "turnStartAltitude" not in body and body["autoPath"] is True
    assert body["autoTurnPercent"] == 0.05 and body["autoTurnSpeedFactor"] == 18.5


def test_mj_ascent_refuses_when_mechjeb_does_not_engage(k):
    fly_vessel(k, Vessel("Rocket", situation=VesselSituation.pre_launch))
    k.bridge.replies[("GET", "/mj-status")] = {"hasCore": True}
    k.bridge.replies[("POST", "/mj-ascent")] = {"engaged": False, "status": "Off"}
    with pytest.raises(AstraError) as err:
        call("mj_ascent", **ASCENT_MANUAL)
    assert "did not engage" in err.value.message and k.sc.active_vessel.control.current_stage == 0


def test_mj_execute_node_watch_stops_when_mechjeb_goes_idle(k, monkeypatch):
    v = fly_vessel(k, Vessel("Ship"))
    v.control.add_node(1600.0, prograde=50.0)
    k.bridge.replies[("GET", "/mj-status")] = {"hasCore": True, "modules": {"node": {"enabled": True}}}
    k.bridge.replies[("POST", "/mj-execute-node")] = {"executing": True}
    seen = {}
    clock = itertools.count(0.0, 1.0)
    monkeypatch.setattr(autopilot.time, "monotonic", lambda: next(clock))

    def fake_fly(**kw):
        seen.update(kw)
        first = kw["extra_stop"]({})  # MechJeb still burning
        k.bridge.replies[("GET", "/mj-status")] = {"hasCore": True, "modules": {"node": {"enabled": False}}}
        reason = kw["extra_stop"]({})
        v.control.nodes.clear()
        return {"stopped_by": {"kind": "condition", "detail": reason}, "game_s": 42.0, "first": first}
    monkeypatch.setitem(sys.modules, "astra.reflex.engine", types.SimpleNamespace(fly=fake_fly))
    interlocks = {"flameout": False}
    out = call("mj_execute_node", all_nodes=False, autowarp=True, lead_time_s=30.0, autostage=False,
               watch=True, max_game_s=600.0, interlocks=interlocks)
    body = next(c for c in k.bridge.calls if c[1] == "/mj-execute-node")[2]
    assert body == {"all": False, "autowarp": True, "leadTime": 30.0, "autostage": False}  # no ignored 'tolerance'
    assert seen["hands_off"] is True and seen["until"] == [] and seen["max_game_s"] == 600.0
    assert seen["interlocks"] == interlocks
    assert out["watch"]["first"] is None and out["watch"]["mechjeb_finished"] is True
    assert out["watch"]["stopped_by"]["detail"] == "mechjeb_done"
    assert len(out["nodes_before"]) == 1 and out["nodes_after"] == [] and out["paused"] is True
    with pytest.raises(AstraError):
        call("mj_execute_node", all_nodes=False, autowarp=True, lead_time_s=30.0, autostage=False, watch=False)


def test_mj_watch_needs_to_see_the_module_engaged(k, monkeypatch):
    v = fly_vessel(k, Vessel("Ship"))
    v.control.add_node(1600.0, prograde=50.0)
    k.bridge.replies[("GET", "/mj-status")] = {"hasCore": True, "modules": {"node": {"enabled": False}}}
    clock = itertools.count(0.0, 1.0)
    monkeypatch.setattr(autopilot.time, "monotonic", lambda: next(clock))

    def fake_fly(**kw):
        reasons = [kw["extra_stop"]({}) for _ in range(6)]
        return {"stopped_by": {"kind": "condition", "detail": reasons[-1]}, "reasons": reasons}
    monkeypatch.setitem(sys.modules, "astra.reflex.engine", types.SimpleNamespace(fly=fake_fly))
    out = call("mj_execute_node", all_nodes=False, autowarp=False, lead_time_s=10.0, autostage=False,
               watch=True, max_game_s=100.0)
    # an idle reading before MechJeb was ever seen engaged is not "done"
    assert out["watch"]["reasons"][:5] == [None] * 5 and out["watch"]["reasons"][5] == "mechjeb_never_engaged"
    assert out["watch"]["mechjeb_finished"] is False


def test_mj_watch_note_when_an_interlock_ends_it_early(k, monkeypatch):
    fly_vessel(k, Vessel("Lander"))
    k.bridge.replies[("GET", "/mj-status")] = {"hasCore": True, "modules": {"landing": {"enabled": True}}}
    k.bridge.replies[("POST", "/mj-land")] = {"landing": True}

    def fake_fly(**kw):
        kw["extra_stop"]({})
        return {"stopped_by": {"kind": "interlock", "detail": "impact: ground in 8 s"}}
    monkeypatch.setitem(sys.modules, "astra.reflex.engine", types.SimpleNamespace(fly=fake_fly))
    out = call("mj_land", targeted=False, touchdown_speed_mps=1.5, deploy_gears=True, deploy_chutes=False,
               watch=True, max_game_s=900.0)
    assert out["watch"]["mechjeb_finished"] is False and "still engaged" in out["watch"]["note"]
    assert "mj_abort(['landing'])" in out["watch"]["note"]


def test_mj_rendezvous_names_a_repeated_target_by_persistent_id(k):
    v = fly_vessel(k, Vessel("Chaser"))
    station = Vessel("Station")
    old = Vessel("Station", situation=VesselSituation.landed)
    k.sc.vessels += [station, old]
    k.bridge.replies[("GET", "/mj-status")] = {"hasCore": True}
    k.bridge.replies[("GET", "/vessels")] = {"vessels": [
        {"name": "Station", "persistentId": 11, "type": "Ship", "situation": "ORBITING", "body": "Kerbin"},
        {"name": "Station", "persistentId": 12, "type": "Ship", "situation": "LANDED", "body": "Kerbin"}]}
    k.bridge.replies[("POST", "/mj-rendezvous")] = {"enabled": True}
    call("mj_rendezvous", target=f"#{station._object_id}", desired_distance_m=100.0, max_phasing_orbits=5.0,
         max_closing_speed_mps=50.0, watch=False)
    body = next(c for c in k.bridge.calls if c[1] == "/mj-rendezvous")[2]
    assert body["targetPersistentId"] == 11 and "target" not in body
    assert k.sc.target_vessel == station and v != station


def _docking_pair(k):
    own_port = Part("dockingPort2", module_names={"ModuleDockingNode"})
    own_port.docking_port = Obj(part=own_port, state="DockingPortState.ready")
    v = fly_vessel(k, Vessel("Ferry", [Part("probeCoreOcto.v2"), own_port]))
    tport = Part("dockingPort2")
    tport.docking_port = Obj(part=tport, state="DockingPortState.ready")
    tv = Vessel("Station", [Part("mk1-3pod"), Part("fuelTank"), tport])
    k.sc.vessels.append(tv)
    k.bridge.replies[("GET", "/mj-status")] = {"hasCore": True}
    k.bridge.replies[("GET", "/vessels")] = {"vessels": [
        {"name": "Station", "persistentId": 31, "type": "Ship", "situation": "ORBITING", "body": "Kerbin"}]}
    k.bridge.replies[("GET", "/vessel-parts")] = {"parts": [
        {"index": 0, "persistentId": 100, "name": "probeCoreOcto.v2"},
        {"index": 1, "persistentId": 101, "name": "dockingPort2"}]}
    k.bridge.replies[("GET", "/vessel-parts?vesselPersistentId=31")] = {"parts": [
        {"index": 0, "persistentId": 200, "name": "mk1-3pod"}, {"index": 1, "persistentId": 201, "name": "fuelTank"},
        {"index": 2, "persistentId": 202, "name": "dockingPort2"}]}
    k.bridge.replies[("POST", "/mj-dock")] = {"enabled": True}
    return v, tv


DOCK = dict(own_port=1, target_port=2, target="Station", speed_limit_mps=1.0, force_roll=False,
            override_safe_distance=False, watch=False)


def test_mj_dock_sends_the_chosen_ports_by_persistent_id(k):
    _docking_pair(k)
    out = call("mj_dock", **DOCK)
    body = next(c for c in k.bridge.calls if c[1] == "/mj-dock")[2]
    assert body == {"ownPortPartId": 101, "targetPortPartId": 202, "speedLimit": 1.0, "forceRoll": False,
                    "overrideSafeDistance": False}
    assert out["target_port"] == {"idx": 2, "name": "dockingPort2", "vessel": "Station"}
    call("mj_dock", **{**DOCK, "override_safe_distance": True, "safe_distance_m": 30.0})
    assert k.bridge.calls[-1][2]["safeDistance"] == 30.0
    with pytest.raises(AstraError) as err:  # the bridge requires the distance when overriding
        call("mj_dock", **{**DOCK, "override_safe_distance": True})
    assert "safe_distance_m" in err.value.message
    with pytest.raises(AstraError):
        call("mj_dock", **{**DOCK, "target_port": 1})  # a fuel tank is not a docking port


def test_mj_dock_refuses_when_bridge_and_krpc_part_lists_disagree(k):
    _docking_pair(k)
    k.bridge.replies[("GET", "/vessel-parts?vesselPersistentId=31")] = {"parts": [
        {"index": 2, "persistentId": 202, "name": "fuelTank"}]}
    with pytest.raises(AstraError) as err:
        call("mj_dock", **DOCK)
    assert "does not match" in err.value.message
    assert not any(path == "/mj-dock" for _, path, _ in k.bridge.calls)


def test_mj_abort_verifies_modules(k):
    fly_vessel(k, Vessel("Ship"))
    k.bridge.replies[("GET", "/mj-status")] = {"modules": {"staging": {"enabled": True}, "dock": {"enabled": False}}}
    out = call("mj_abort", modules=["staging", "dock"])
    assert k.bridge.calls[0] == ("POST", "/mj-abort", {"modules": "staging,dock"})
    assert out["still_enabled"] == ["staging"]
    with pytest.raises(AstraError):
        call("mj_abort", modules=[])


def test_mj_land_requires_coordinates_when_targeted(k):
    fly_vessel(k, Vessel("Lander"))
    with pytest.raises(AstraError):
        call("mj_land", targeted=True, touchdown_speed_mps=1.0, deploy_gears=True, deploy_chutes=False, watch=False)
    k.bridge.replies[("GET", "/mj-status")] = {"hasCore": True}
    out = call("mj_land", targeted=False, touchdown_speed_mps=1.5, deploy_gears=True, deploy_chutes=False, watch=False)
    body = next(c for c in k.bridge.calls if c[1] == "/mj-land")[2]
    assert body == {"targeted": False, "touchdownSpeed": 1.5, "deployGears": True, "deployChutes": False}
    assert out["facts"]["note"] == "periapsis is above the atmosphere"


# ---------------------------------------------------------------------------------------------
# Review regressions: game, observe, control


@pytest.mark.parametrize("bad", ["../other", "a/b", "a\\b", "C:save", " astra", ""])
def test_game_load_save_rejects_paths(k, bad):
    with pytest.raises(AstraError) as err:
        call("game_load_save", save=bad, scene="space_center")
    assert "save folder" in err.value.message
    assert k.bridge.calls == []


def test_game_revert_to_editor_goes_through_the_bridge(k):
    fly_vessel(k, Vessel("Rocket", situation=VesselSituation.pre_launch))

    def revert(body):
        k._scene = "editor_vab"
        return {"to": "editor", "requested": True, "facility": "VAB"}
    k.bridge.replies[("POST", "/revert")] = revert
    out = call("game_revert", to="editor")
    assert k.bridge.calls[-1] == ("POST", "/revert", {"to": "editor"})
    assert out == {"reverted_to": "editor", "scene": "editor_vab", "facility": "VAB"}
    k._scene = "flight"
    k.bridge.replies[("POST", "/revert")] = BridgeError("bridge /revert failed: Unknown route: POST /revert")
    with pytest.raises(AstraError) as err:
        call("game_revert", to="editor")
    assert "game_revert('launch')" in err.value.hint


def test_game_list_vessels_validates_kinds(k):
    fly_vessel(k, Vessel("Ship"))
    k.sc.vessels.append(Vessel("Junk", vtype=VesselType.debris))
    assert [r["name"] for r in call("game_list_vessels", kinds=["debris"])["vessels"]] == ["Junk"]
    with pytest.raises(AstraError) as err:
        call("game_list_vessels", kinds=["rocket"])
    assert "debris" in err.value.hint


def test_bridge_vessel_id_matches_and_gives_up_when_ambiguous(k):
    a, b = Vessel("Twin"), Vessel("Twin")
    k.bridge.replies[("GET", "/vessels")] = {"vessels": [
        {"name": "Twin", "persistentId": 1, "type": "Ship", "situation": "ORBITING", "body": "Kerbin"},
        {"name": "Twin", "persistentId": 2, "type": "Ship", "situation": "ORBITING", "body": "Kerbin"}]}
    assert observe.bridge_vessel_id(a) is None and observe.bridge_vessel_ref(b) == {"name": "Twin"}
    k.bridge.replies[("GET", "/vessels")] = {"vessels": [
        {"name": "Twin", "persistentId": 1, "type": "Ship", "situation": "SUB_ORBITAL", "body": "Kerbin"},
        {"name": "Twin", "persistentId": 2, "type": "Ship", "situation": "ORBITING", "body": "Kerbin"}]}
    assert observe.bridge_vessel_id(a) == 2
    k.bridge.is_up = False
    assert observe.bridge_vessel_id(a) is None


def test_control_attitude_sas_stability_assist_replaces_an_old_mode(k):
    v = fly_vessel(k, Vessel("Ship"))
    v.control.sas, v.control._sas_mode = True, SASMode.prograde
    out = call("control_attitude", mode="sas:stability_assist")
    assert v.control.sas_mode == SASMode.stability_assist and "stability_assist" in out["state"]


def test_control_attitude_rejects_a_wait_before_changing_anything(k):
    v = fly_vessel(k, Vessel("Ship"))
    v.auto_pilot.engaged = True
    for mode in ("off", "sas:stability_assist"):
        with pytest.raises(AstraError) as err:
            call("control_attitude", mode=mode, wait_aligned_deg=1.0, timeout_s=30.0)
        assert "no pointing target" in err.value.message
    assert v.auto_pilot.engaged is True and v.control.sas is False and k.pause_log == []


def test_camera_look_never_deletes_a_file_it_did_not_write(k, monkeypatch, tmp_path):
    from PIL import Image

    monkeypatch.setattr(observe, "CONFIG", dataclasses.replace(observe.CONFIG, cache_dir=tmp_path / "cache"))
    fly_vessel(k, Vessel("Cam"))
    elsewhere = tmp_path / "keep.png"
    Image.new("RGB", (320, 200)).save(elsewhere, "PNG")
    k.bridge.replies[("POST", "/screenshot")] = {"path": str(elsewhere)}
    pic = call("camera_look", width=160)
    assert elsewhere.exists() and pic.path.exists() and "via bridge" in pic.caption


def test_body_info_survives_a_terrain_sampling_failure(k, monkeypatch, tmp_path):
    monkeypatch.setattr(observe, "_terrain_cache_path", lambda: tmp_path / "terrain.json")

    def broken(lat, lon):
        raise RuntimeError("Procedure not available")
    k.sc.bodies["Mun"].surface_height = broken
    out = call("body_info", body="Mun")
    assert out["radius_m"] == 200000.0 and "terrain sampling failed" in out["max_terrain"]["error"]


# ---------------------------------------------------------------------------------------------
# Live smoke test (read-only): ASTRA_LIVE=1 with KSP running and kRPC reachable.


@pytest.mark.skipif(os.environ.get("ASTRA_LIVE") != "1", reason="set ASTRA_LIVE=1 to run against a live game")
def test_live_read_only_smoke():
    ksp_mod._KSP = None
    status = registry.validate_and_call("game_status", {})
    assert status["krpc_up"] is True, status
    kerbin = registry.validate_and_call("body_info", {"body": "Kerbin"})
    assert kerbin["radius_m"] == pytest.approx(600000.0) and kerbin["atmosphere"]["depth_m"] == pytest.approx(70000.0)
    crafts = ksp_mod.ksp().sc.launchable_vessels("VAB")
    assert isinstance(crafts, list)


def test_checkpoint_scene_from_sidecar_or_save(tmp_path):
    from astra.tools import game

    sfs = tmp_path / "astra_x.sfs"
    sfs.write_text("GAME\n{\n\tFLIGHTSTATE\n\t{\n\t\tUT = 5\n\t}\n}\n", encoding="utf-8")
    assert game._checkpoint_scene(sfs) == "space_center"  # nothing to fly in it
    sfs.write_text("GAME\n{\n\tFLIGHTSTATE\n\t{\n\t\tVESSEL\n\t\t{\n\t\t}\n\t}\n}\n", encoding="utf-8")
    assert game._checkpoint_scene(sfs) == "flight"
    sfs.with_suffix(".astra.json").write_text('{"scene": "space_center"}', encoding="utf-8")
    assert game._checkpoint_scene(sfs) == "space_center"  # the sidecar wins
