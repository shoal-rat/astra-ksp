"""Reflex engine tests against a small simulated vessel (vertical 1-D flight, fuel, staging)."""

import math

import pytest

from astra.errors import AstraError
from astra.reflex import engine as engine_mod
from astra.reflex import events as events_mod
from astra.reflex import descent, laws, triggers, vec
from astra.reflex.descent import DescentGuidance, DescentThrottle

G = 9.81


class _Frame:
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, _Frame) and other.name == self.name

    def __hash__(self):
        return hash(self.name)


class _Body:
    name = "Kerbin"
    non_rotating_reference_frame = _Frame("nrf")
    reference_frame = _Frame("brf")


class _AutoPilot:
    def __init__(self):
        self.engaged = False
        self.reference_frame = None
        self.target_direction = None
        self.target_roll = None
        self.error = 0.5

    def engage(self):
        self.engaged = True

    def disengage(self):
        self.engaged = False


class _Stage:
    def __init__(self, fuel_kg, dry_kg, thrust_n):
        self.fuel, self.dry, self.thrust = fuel_kg, dry_kg, thrust_n


class _Engine:
    def __init__(self, sim, idx):
        self.sim, self.idx = sim, idx
        self.part = type("P", (), {"name": f"engine{idx}", "title": f"Engine {idx}", "decouple_stage": idx})()

    @property
    def active(self):
        return self.idx == self.sim.stage_idx

    @property
    def has_fuel(self):
        return self.sim.stages[self.idx].fuel > 0

    throttle_locked = False


class _Parts:
    def __init__(self, sim):
        self.sim = sim

    @property
    def all(self):
        return list(range(self.sim.part_count))

    @property
    def engines(self):
        return [_Engine(self.sim, i) for i in range(len(self.sim.stages))]

    def in_stage(self, stage):
        # the engine of the next stage ignites when stage `stage` is activated
        nxt = self.sim.stage_idx + 1
        if nxt < len(self.sim.stages):
            e = _Engine(self.sim, nxt)
            e.part.engine = object()
            return [e.part]
        return []

    def in_decouple_stage(self, stage):
        return [type("P", (), {"resources": type("R", (), {"all": []})()})()]


class _Control:
    def __init__(self, sim):
        self.sim = sim
        self.throttle = 0.0
        self.sas = False
        self.legs = False

    @property
    def current_stage(self):
        return 10 - self.sim.stage_idx

    @property
    def nodes(self):
        return []

    def activate_next_stage(self):
        self.sim.stage_idx += 1
        self.sim.part_count -= 3


class Sim:
    """Vertical rocket. Each probe read advances the game clock by DT."""

    DT = 0.1

    def __init__(self, stages, alt=0.0, vs=0.0):
        self.stages = stages
        self.stage_idx = 0
        self.alt, self.vs, self.ut = alt, vs, 1000.0
        self.part_count = 12
        self.control = _Control(self)
        self.auto_pilot = _AutoPilot()
        self.parts = _Parts(self)
        self.orbit = type("O", (), {"body": _Body()})()
        self._object_id = 7
        self.surface_reference_frame = _Frame("srf")
        self.orbital_reference_frame = _Frame("orf")
        self.situation = "flying"

    @property
    def mass(self):
        return sum(s.fuel + s.dry for s in self.stages[self.stage_idx:])

    # body-frame vectors for the descent laws (x is up; the vessel stays upright)
    def position(self, frame):
        return (600000.0 + self.alt, 0.0, 0.0)

    def velocity(self, frame):
        return (self.vs, 0.0, 0.0)

    def direction(self, frame):
        return (1.0, 0.0, 0.0)

    def thrust(self):
        st = self.stages[self.stage_idx] if self.stage_idx < len(self.stages) else None
        if st is None or st.fuel <= 0:
            return 0.0, 0.0
        return st.thrust * self.control.throttle, st.thrust

    def step(self):
        thrust, avail = self.thrust()
        mass = self.mass
        a = thrust / mass - G
        self.vs += a * self.DT
        self.alt = max(0.0, self.alt + self.vs * self.DT)
        if self.alt == 0.0 and self.vs < 0:
            self.vs = 0.0
            self.situation = "landed"
        st = self.stages[self.stage_idx] if self.stage_idx < len(self.stages) else None
        if st is not None and thrust > 0:
            st.fuel = max(0.0, st.fuel - thrust / (300 * 9.80665) * self.DT)
        self.ut += self.DT
        return thrust, avail

    def metrics(self):
        thrust, avail = self.step()
        mass = self.mass
        return {"ut": self.ut, "met": self.ut - 1000, "altitude": self.alt, "surface_altitude": self.alt,
                "vertical_speed": self.vs, "surface_speed": abs(self.vs), "horizontal_speed": 0.0,
                "orbital_speed": abs(self.vs), "apoapsis_altitude": self.alt + max(self.vs, 0) ** 2 / (2 * G),
                "periapsis_altitude": -600000.0, "time_to_apoapsis": max(self.vs, 0) / G,
                "time_to_periapsis": 100.0, "mass": mass, "thrust": thrust, "available_thrust": avail,
                "local_g": G, "twr": thrust / (mass * G), "max_twr": avail / (mass * G),
                "throttle": self.control.throttle, "stage": self.control.current_stage,
                "situation": self.situation, "body": "Kerbin", "dynamic_pressure": 0.0,
                "atmosphere_density": 0.0, "part_count": self.part_count, "electric_charge": 1.0,
                "pitch": 90.0, "heading": 90.0, "eccentricity": 1.0, "inclination": 0.0,
                "node_time_to": float("nan"), "node_remaining_dv": float("nan"),
                "next_periapsis_altitude": float("nan"), "max_temp_fraction": float("nan")}


class FakeProbe:
    def __init__(self, sim):
        self.sim = sim
        self._engine_key = 0

    def invalidate(self):
        pass

    def read_extended(self, include_temp=True):
        return self.sim.metrics()

    def read(self):
        return self.sim.metrics()

    def engines(self):
        self._engine_key = self.sim.stage_idx
        st = self.sim.stages[self.sim.stage_idx] if self.sim.stage_idx < len(self.sim.stages) else None
        if st is None:
            return []
        return [{"part": f"engine{self.sim.stage_idx}", "has_fuel": st.fuel > 0, "thrust": 0.0,
                 "available_thrust": st.thrust, "solid": False}]


class FakeStream:
    def __init__(self, fn, *args):
        self.fn, self.args = fn, args

    def __call__(self):
        return self.fn(*self.args)

    def remove(self):
        pass


class FakeConn:
    def add_stream(self, fn, *args):
        return FakeStream(fn, *args)


class FakeKSP:
    def __init__(self, sim):
        self.sim = sim
        self.conn = FakeConn()
        self.sc = type("SC", (), {"rails_warp_factor": 0, "physics_warp_factor": 0})()
        self.paused = True

    def require_flight(self):
        pass

    def vessel(self):
        return self.sim

    def set_paused(self, v):
        self.paused = v

    def hold_for_deliberation(self):
        self.paused = True
        return True

    def scene(self):
        return "flight"


@pytest.fixture
def rig(monkeypatch):
    def make(stages, **kw):
        sim = Sim(stages, **kw)
        k = FakeKSP(sim)
        monkeypatch.setattr(engine_mod, "ksp", lambda: k)
        monkeypatch.setattr(engine_mod, "probe", lambda: FakeProbe(sim))
        monkeypatch.setattr(engine_mod, "TICK_S", 0.0)
        monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)
        return sim, k
    return make


def test_trigger_stops_climb(rig):
    sim, k = rig([_Stage(4000, 1000, 150000)])
    rep = engine_mod.fly(until=[{"metric": "altitude", "op": ">=", "value": 500}], throttle=1.0,
                         attitude="keep", max_game_s=200)
    assert rep["stopped_by"]["kind"] == "trigger"
    assert 500 <= sim.alt < 600
    assert rep["paused"] is True and k.paused is True
    assert rep["trace"] and rep["trace_fields"][0] == "t"


def test_twr_law_holds_ratio(rig):
    sim, _ = rig([_Stage(4000, 1000, 150000)])
    rep = engine_mod.fly(until=[], throttle={"mode": "twr", "twr": 1.5}, max_game_s=5)
    assert rep["stopped_by"]["kind"] == "timeout"
    assert rep["final"]["twr"] == pytest.approx(1.5, rel=0.05)


def test_flameout_interlock_returns_control(rig):
    sim, _ = rig([_Stage(300, 1000, 150000), _Stage(2000, 500, 60000)])
    rep = engine_mod.fly(until=[{"metric": "altitude", "op": ">=", "value": 1e7}], throttle=1.0, max_game_s=300)
    assert rep["stopped_by"]["kind"] == "interlock"
    assert "flameout" in rep["stopped_by"]["detail"] or "all_engines_out" in rep["stopped_by"]["detail"]
    assert rep["stages_fired"] == 0


def test_auto_stage_continues_on_safe_stage(rig):
    sim, _ = rig([_Stage(300, 1000, 150000), _Stage(2000, 500, 60000)])
    rep = engine_mod.fly(until=[{"metric": "altitude", "op": ">=", "value": 2000}], throttle=1.0,
                         auto_stage=True, max_game_s=300)
    assert rep["stages_fired"] == 1
    assert sim.stage_idx == 1
    assert rep["stopped_by"]["kind"] == "trigger"
    assert any(e["type"] == "auto_staged" for e in rep["events"])


def test_debounce_ignores_brief_dry_reading():
    class P:
        _engine_key = 1

        def __init__(self):
            self.dry = True

        def engines(self):
            return [{"part": "e", "has_fuel": not self.dry, "thrust": 1, "available_thrust": 1, "solid": False}]

    p = P()
    det = events_mod.EventDetector(p, events_mod.merge_interlocks({"dry_debounce_s": 0.5}))
    base = {"stage": 3, "body": "Kerbin", "situation": "flying", "part_count": 10, "throttle": 1.0,
            "available_thrust": 1.0}
    assert not det.update({**base, "ut": 0.0}, None, False)
    p.dry = False  # crossfeed transient over
    assert not det.update({**base, "ut": 0.3}, None, False)
    p.dry = True
    det.update({**base, "ut": 1.0}, None, False)
    ev = det.update({**base, "ut": 1.6}, None, False)
    assert {e["type"] for e in ev} == {"flameout", "all_engines_out"}  # the only engine is dry


def test_part_loss_vs_separation():
    class P:
        _engine_key = 1

        def engines(self):
            return []

    det = events_mod.EventDetector(P(), events_mod.merge_interlocks(None))
    base = {"body": "Kerbin", "situation": "flying", "throttle": 0.0, "available_thrust": 0.0}
    det.update({**base, "ut": 0.0, "stage": 3, "part_count": 20}, None, False)
    ev = det.update({**base, "ut": 0.5, "stage": 2, "part_count": 14}, None, False)
    assert {"staged", "separated"} <= {e["type"] for e in ev}
    ev = det.update({**base, "ut": 10.0, "stage": 2, "part_count": 9}, None, False)
    assert [e["type"] for e in ev] == ["part_lost"]
    assert "part_lost" in det.interlock({**base, "ut": 10.0}, ev, False)


def test_impact_interlock():
    det = events_mod.EventDetector(None, events_mod.merge_interlocks(None))
    m = {"vertical_speed": -80.0, "surface_altitude": 500.0, "situation": "flying", "ut": 1.0}
    assert det.interlock(m, [], False).startswith("impact")
    m2 = {"vertical_speed": -6.0, "surface_altitude": 30.0, "situation": "flying", "ut": 1.0}
    assert det.interlock(m2, [], False) is None  # parachute-speed descent is fine


def test_trigger_parsing_errors():
    with pytest.raises(AstraError):
        triggers.parse_until([{"metric": "altitudez", "op": ">", "value": 1}])
    with pytest.raises(AstraError):
        triggers.parse_until([{"metric": "altitude", "op": "=>", "value": 1}])
    with pytest.raises(AstraError):
        triggers.parse_until([{"event": "boom"}])
    t = triggers.parse_until([{"metric": "situation", "op": "==", "value": "landed"}])[0]
    assert t.check({"situation": "landed"}, []) is not None
    assert t.check({"situation": "flying"}, []) is None


def test_no_stop_condition_is_rejected(rig):
    rig([_Stage(1000, 1000, 100000)])
    with pytest.raises(AstraError):
        engine_mod.fly(until=[], throttle=1.0)


def test_approach_law_feathers_and_finishes():
    law = laws.ApproachThrottle("apoapsis_altitude", 80000, feather_s=4.0, lo=0.02, hi=1.0)
    law.start(None, {"apoapsis_altitude": 70000})
    # 500 m/s per second of apoapsis growth at full throttle
    thr = law.update(None, {"apoapsis_altitude": 70000, "ut": 0.0, "throttle": 1.0})
    assert thr == 1.0
    law.update(None, {"apoapsis_altitude": 70500, "ut": 1.0, "throttle": 1.0})
    thr = law.update(None, {"apoapsis_altitude": 79000, "ut": 18.0, "throttle": 1.0})
    assert 0.02 <= thr < 1.0  # ~2 s left at full throttle vs 4 s feather window
    assert law.update(None, {"apoapsis_altitude": 80001, "ut": 19.0, "throttle": thr}) == 0.0
    assert law.done(None, {}) is not None


def test_pitch_program_interpolates():
    p = laws.PitchProgram([[0, 90], [10000, 45], [40000, 0]], heading=90)
    assert p.pitch_at(-5) == 90
    assert p.pitch_at(5000) == pytest.approx(67.5)
    assert p.pitch_at(25000) == pytest.approx(22.5)
    assert p.pitch_at(99999) == 0


def test_tilt_clamp():
    up = (1.0, 0.0, 0.0)
    flat = (0.0, 1.0, 0.0)
    d = vec.tilt_toward(flat, up, 10.0, None)
    elev = math.degrees(math.asin(vec.dot(d, up)))
    assert elev == pytest.approx(10.0, abs=1e-6)
    assert vec.tilt_toward((0.5, 0.5, 0.0), up, 10.0, 80.0) == pytest.approx(vec.unit((0.5, 0.5, 0.0)))


def test_descent_guidance_coasts_then_brakes():
    g = DescentGuidance(touchdown_mps=1.5, reserve=0.1, terminal_alt_m=40, terminal_rate=0.15,
                        max_tilt_deg=15, tilt_gain_deg_per_mps=4, sink_gain=0.8, legs_alt_m=None,
                        drift_max_mps=0.5)
    g.bottom = -1.0
    base = {"available_thrust": 60000.0, "mass": 3000.0, "local_g": 1.63, "horizontal_speed": 0.0,
            "situation": "flying"}
    high = {**base, "surface_altitude": 8000.0, "surface_speed": 100.0, "vertical_speed": -100.0}
    thr, _ = g.step(None, high)
    assert thr == 0.0 and g.phase == "coast"  # far above the braking point
    # needs (100^2 - 6^2) / (2 * 359) + 1.63 = 15.5 m/s^2 < 18 usable: still coasting at 400 m
    thr, _ = g.step(None, {**base, "surface_altitude": 400.0, "surface_speed": 100.0, "vertical_speed": -100.0})
    assert thr == 0.0 and g.phase == "coast"
    # at 330 m it needs ~18.9 m/s^2 >= 18 usable: braking starts near full throttle
    thr, _ = g.step(None, {**base, "surface_altitude": 330.0, "surface_speed": 100.0, "vertical_speed": -100.0})
    assert g.phase == "brake" and 0.9 < thr <= 1.0


def test_descent_guidance_never_runs_terminal_law_high_up():
    """Regression (Mun, 2026-09-30): level at 9.6 km the guidance switched to its terminal law and
    hovered for ten minutes. Above the terminal gate it may only coast or brake."""
    g = DescentGuidance(touchdown_mps=1.5, reserve=0.2, terminal_alt_m=120, terminal_rate=0.15,
                        max_tilt_deg=15, tilt_gain_deg_per_mps=4, sink_gain=0.8, legs_alt_m=None,
                        drift_max_mps=0.5)
    g.bottom = -3.0
    level = {"available_thrust": 60000.0, "mass": 4465.0, "local_g": 1.43, "horizontal_speed": 450.0,
             "situation": "sub_orbital", "surface_altitude": 9600.0, "surface_speed": 450.0, "vertical_speed": 0.0}
    thr, direction = g.step(None, level)
    assert (thr, direction, g.phase) == (0.0, None, "coast")
    thr, direction = g.step(None, {**level, "vertical_speed": 5.0})
    assert (thr, direction, g.phase) == (0.0, None, "coast")


def test_vessel_box_ignores_parts_with_broken_bounds():
    from astra.telemetry import vessel_box

    class P:
        def __init__(self, lo, hi, pos):
            self._box, self._pos = (lo, hi), pos

        def bounding_box(self, frame):
            return self._box

        def position(self, frame):
            return self._pos

    class V:
        class parts:
            all = [P((-6e17, -6e17, -6e17), (6e17, 6e17, 6e17), (1.3, 0.0, 0.0)),  # the Mk1 pod
                   P((-3.1, -1.0, -1.0), (-0.3, 1.0, 1.0), (-1.0, 0.0, 0.0)),
                   P((0.5, -0.6, -0.6), (2.2, 0.6, 0.6), (1.9, 0.0, 0.0))]

    lo, hi = vessel_box(V, None)
    assert lo == [-3.1, -1.0, -1.0] and hi == [2.2, 1.0, 1.0]


def test_parse_attitude_modes():
    assert isinstance(laws.parse_attitude("keep"), laws.KeepAttitude)
    assert isinstance(laws.parse_attitude({"mode": "hold", "pitch": 80, "heading": 90}), laws.HoldPitchHeading)
    d = laws.parse_attitude({"mode": "prograde", "frame": "surface", "min_pitch": 5})
    assert d.frame == "surface" and d.min_pitch == 5
    with pytest.raises(AstraError):
        laws.parse_attitude({"mode": "normal", "frame": "surface"})
    with pytest.raises(AstraError):
        laws.parse_attitude({"mode": "wiggle"})


def test_approach_law_from_unbound_apoapsis():
    """Capture: apoapsis starts infinite (hyperbolic); burn full until bound, then feather to target."""
    law = laws.ApproachThrottle("apoapsis_altitude", 500000, feather_s=3.0, lo=0.02, hi=1.0)
    law.start(None, {"apoapsis_altitude": float("inf")})
    assert law.update(None, {"apoapsis_altitude": float("inf"), "ut": 0.0, "throttle": 1.0}) == 1.0
    assert law.done(None, {}) is None
    assert law.update(None, {"apoapsis_altitude": 9e6, "ut": 1.0, "throttle": 1.0}) == 1.0
    law.update(None, {"apoapsis_altitude": 5e6, "ut": 2.0, "throttle": 1.0})
    assert law.update(None, {"apoapsis_altitude": 499000, "ut": 3.0, "throttle": 1.0}) == 0.0
    assert law.done(None, {}) is not None


def test_unbound_apoapsis_trigger_does_not_fire_early():
    t = triggers.parse_until([{"metric": "apoapsis_altitude", "op": "<=", "value": 500000}])[0]
    assert t.check({"apoapsis_altitude": float("inf")}, []) is None
    assert t.check({"apoapsis_altitude": 400000.0}, []) is not None


def test_chute_context_semi_and_fresh_deploy():
    class Chute:
        def __init__(self, state, deploy_altitude=1000.0):
            self.state, self.deploy_altitude = f"ParachuteState.{state}", deploy_altitude

    class Ctx:
        def __init__(self, chutes):
            self.vessel = type("V", (), {"parts": type("P", (), {"parachutes": chutes})()})()

    semi = Ctx([Chute("semi_deployed")])
    assert engine_mod._chute_context(semi, {"surface_altitude": 2000.0}) == "expected"
    assert engine_mod._chute_context(semi, {"surface_altitude": 900.0}).startswith("parachutes:")
    full = Ctx([Chute("deployed")])
    slowing = engine_mod._chute_context(full, {"surface_altitude": 990.0, "vertical_speed": -150.0},
                                        {"vertical_speed": -162.0})
    assert slowing == "expected"
    steady = engine_mod._chute_context(full, {"surface_altitude": 500.0, "vertical_speed": -60.0},
                                       {"vertical_speed": -60.0})
    assert steady == "parachutes: deployed"
    assert engine_mod._chute_context(Ctx([]), {"surface_altitude": 500.0}) is None


class _Vec3Stream:
    """Minimal ctx for the descent laws: body-frame position, velocity and pointing streams."""

    def __init__(self, pos, vel, direction=None):
        self.vals = {"pos_brf": pos, "vel_brf": vel, "dir_brf": direction}
        self.vessel = type("V", (), {"position": None, "velocity": None, "direction": None})()
        self.brf = None
        self.srf = None
        self.body = None

    def stream(self, key, fn, *args):
        return self.vals[key]


def test_descent_hovers_low_until_drift_is_cancelled():
    g = DescentGuidance(touchdown_mps=1.5, reserve=0.2, terminal_alt_m=150, terminal_rate=0.15,
                        max_tilt_deg=30, tilt_gain_deg_per_mps=10, sink_gain=0.8, legs_alt_m=None,
                        drift_max_mps=0.5)
    g.bottom, g.phase = -3.0, "terminal"
    g._bottom_at = g._floor_at = float("inf")  # keep the preset geometry
    base = {"available_thrust": 60000.0, "mass": 3800.0, "local_g": 1.63, "situation": "flying",
            "altitude": 1700.0}
    up = (200000.0, 0.0, 0.0)
    drifting = _Vec3Stream(up, (-1.0, 0.0, 2.0))  # 2 m/s sideways, sinking 1 m/s
    m = {**base, "surface_altitude": 5.0, "surface_speed": 2.24, "vertical_speed": -1.0, "horizontal_speed": 2.0}
    thr, d = g.step(drifting, m)  # h = 2 m < hover height 3 m with 2 m/s drift: hold height, tilt against drift
    hover_thr = 1.63 / (60000.0 / 3800.0 * math.cos(math.radians(20.0)))
    assert thr > hover_thr  # pushes up to stop the sink
    assert d[2] < 0 and d[0] > 0  # tilted against +z drift, still mostly up
    settled = _Vec3Stream(up, (-1.0, 0.0, 0.2))
    thr2, d2 = g.step(settled, {**m, "surface_altitude": 4.0, "horizontal_speed": 0.2, "surface_speed": 1.02})
    assert math.degrees(math.acos(d2[0] / math.hypot(*d2))) <= 3.0 + 1e-6  # below the flare height: upright


def _descent_guidance(**kw):
    args = dict(touchdown_mps=1.5, reserve=0.2, terminal_alt_m=150, terminal_rate=0.15, max_tilt_deg=30,
                tilt_gain_deg_per_mps=10, sink_gain=0.8, legs_alt_m=None, drift_max_mps=0.5)
    args.update(kw)
    g = DescentGuidance(**args)
    g._bottom_at = g._floor_at = float("inf")  # keep the preset geometry
    return g


def test_pointing_gate_shape():
    gate = descent.pointing_gate
    assert gate(descent.ALIGN_FULL_DEG, False) == 1.0
    mid = 0.5 * (descent.ALIGN_FULL_DEG + descent.ALIGN_ZERO_DEG)
    assert gate(mid, False) == pytest.approx(0.5)
    assert gate(descent.ALIGN_ZERO_DEG, False) == 0.0
    assert gate(60.0, True) == 1.0  # once aligned, lagging a turning command keeps the brake
    assert gate(sum(descent.WRONG_WAY_DEG) / 2, True) == pytest.approx(0.5)
    assert gate(170.0, True) == 0.0


def test_descent_throttle_gates_braking_on_pointing():
    """Regression (review 2026-09-30): the brake lit along the guidance's direction whatever the
    vessel pointed at, so a lander still in its deorbit attitude burned the wrong way."""
    g = _descent_guidance(reserve=0.1, terminal_alt_m=40)
    g.bottom = -1.0
    law = DescentThrottle(g)
    m = {"available_thrust": 60000.0, "mass": 3000.0, "local_g": 1.63, "horizontal_speed": 0.0,
         "situation": "flying", "surface_altitude": 330.0, "surface_speed": 100.0, "vertical_speed": -100.0,
         "ut": 10.0}
    pos, vel = (200330.0, 0.0, 0.0), (-100.0, 0.0, 0.0)  # falling straight down: retrograde is up
    assert law.update(_Vec3Stream(pos, vel, direction=(-1.0, 0.0, 0.0)), m) == 0.0  # nose down: hold it
    assert g.phase == "brake"
    half = math.radians(0.5 * (descent.ALIGN_FULL_DEG + descent.ALIGN_ZERO_DEG))
    thr_half = law.update(_Vec3Stream(pos, vel, direction=(math.cos(half), math.sin(half), 0.0)),
                          {**m, "ut": 11.0})
    thr = law.update(_Vec3Stream(pos, vel, direction=(1.0, 0.0, 0.0)), {**m, "ut": 12.0})
    assert 0.9 < thr <= 1.0 and thr_half == pytest.approx(0.5 * thr)
    assert g.brake_held_s == pytest.approx(1.0 + 0.5)
    lag = math.radians(60.0)  # aligned once: a 60 deg lag behind a turning command keeps full thrust
    assert law.update(_Vec3Stream(pos, vel, direction=(math.cos(lag), math.sin(lag), 0.0)),
                      {**m, "ut": 13.0}) == pytest.approx(thr)


def test_descent_throttle_never_gates_the_terminal_phase():
    g = _descent_guidance()
    g.bottom, g.phase = -3.0, "terminal"
    law = DescentThrottle(g)
    m = {"available_thrust": 60000.0, "mass": 3800.0, "local_g": 1.63, "situation": "flying", "altitude": 1700.0,
         "surface_altitude": 5.0, "surface_speed": 2.24, "vertical_speed": -1.0, "horizontal_speed": 2.0}
    upside_down = _Vec3Stream((200000.0, 0.0, 0.0), (-1.0, 0.0, 2.0), direction=(-1.0, 0.0, 0.0))
    thr = law.update(upside_down, m)
    assert thr > 1.63 / (60000.0 / 3800.0)  # the thrust that stops the sink stays on


def test_fly_descent_schema_matches_the_guidance():
    import inspect

    from astra import registry
    from astra.tools import fly as _fly_tools  # noqa: F401 — registers the fly tools

    spec = registry.TOOLS["fly_descent"]
    for p in ("touchdown_mps", "terminal_alt_m", "throttle_reserve", "touchdown_drift_mps"):
        assert spec.signature.parameters[p].default is inspect.Parameter.empty, f"fly_descent.{p}"
    model = registry.arg_model(spec)
    ok = dict(touchdown_mps=1.5, terminal_alt_m=100.0, throttle_reserve=0.1, touchdown_drift_mps=0.5)
    model.model_validate(ok)
    for bad in ({"max_tilt_deg": 0.0}, {"tilt_gain_deg_per_mps": 0.0}):  # DescentGuidance refuses both
        with pytest.raises(Exception):
            model.model_validate({**ok, **bad})


@pytest.mark.parametrize("parts_lost_at_contact", [0, 2])
def test_fly_descent_settles_on_sas_and_reports_it(rig, parts_lost_at_contact):
    """The real descent laws through the engine on the 1-D rig: brake, terminal, contact, settle. With
    legs broken on contact, part_lost stops the reflex on the contact tick; the report must still say
    SAS is on and that the settle was cut short (regression, review 2026-09-30)."""
    from astra.tools.fly import fly_descent

    sim, k = rig([_Stage(400, 1000, 40000)], alt=300.0, vs=-25.0)
    step = sim.step

    def breaking_step():
        out = step()
        if sim.situation == "landed" and sim.part_count == 12:
            sim.part_count -= parts_lost_at_contact
        return out
    sim.step = breaking_step
    rep = fly_descent(touchdown_mps=1.5, terminal_alt_m=30.0, throttle_reserve=0.2, touchdown_drift_mps=0.5,
                      legs_alt_m=10.0, max_game_s=300.0)
    assert rep["phase_at_end"] == "down" and rep["touchdown"] is not None, rep
    assert rep["control"]["sas"] is True and sim.control.sas is True
    assert sim.auto_pilot.engaged is False and sim.control.legs is True
    assert rep["control"]["throttle"] == 0.0 and rep["paused"] is True
    if parts_lost_at_contact:
        assert rep["stopped_by"]["kind"] == "interlock" and "part_lost" in rep["stopped_by"]["detail"]
        assert "before the 4 s settle ended" in rep["control"]["attitude"], rep["control"]
    else:
        assert rep["stopped_by"]["kind"] == "law" and rep["stopped_by"]["detail"].startswith("touchdown")
        assert "SAS holding the settled attitude after 4" in rep["control"]["attitude"], rep["control"]
