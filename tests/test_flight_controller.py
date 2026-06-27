from ksp_lab.flight_controller import KrpcFlightController


class FakePart:
    def __init__(self, stage: int, name: str = "", title: str = "", tag: str = ""):
        self.stage = stage
        self.name = name
        self.title = title
        self.tag = tag


class FakeEngine:
    def __init__(self, stage: int, active: bool = False, has_fuel: bool = True):
        self.part = FakePart(stage)
        self.active = active
        self.has_fuel = has_fuel
        self.available_thrust = 120_000.0 if active and has_fuel else 0.0
        self.thrust = self.available_thrust


class FakeParts:
    def __init__(self, engines=None, part_count: int = 1, parts=None):
        self.engines = engines or []
        self.all = parts if parts is not None else [object() for _ in range(part_count)]


class FakeControl:
    def __init__(self, current_stage: int):
        self.current_stage = current_stage
        self.stage_count = 0
        self.throttle = 0.0
        self.sas = False

    def activate_next_stage(self):
        self.stage_count += 1
        self.current_stage -= 1


class FakeAutoPilot:
    def __init__(self):
        self.error = 0.0
        self.reference_frame = None
        self.target_direction = None
        self.engaged = False
        self.disengaged = False

    def engage(self):
        self.engaged = True

    def disengage(self):
        self.disengaged = True
        self.engaged = False


class FakeVessel:
    def __init__(
        self,
        name: str = "AI-HLS-Test",
        current_stage: int = 0,
        engines=None,
        part_count: int = 1,
        parts=None,
        mass: float = 10_000.0,
        vessel_type: str = "Probe",
        situation: str = "VesselSituation.flying",
    ):
        self.name = name
        self.control = FakeControl(current_stage)
        self.parts = FakeParts(engines, part_count, parts)
        self.mass = mass
        self.vessel_type = vessel_type
        self.situation = situation
        self.available_thrust = sum(float(engine.available_thrust) for engine in self.parts.engines)
        self.auto_pilot = FakeAutoPilot()
        self.orbital_reference_frame = object()


class FakeSpaceCenter:
    def __init__(self, active_vessel, vessels):
        self.active_vessel = active_vessel
        self.vessels = vessels


class FakeConnection:
    def __init__(self, active_vessel, vessels):
        self.space_center = FakeSpaceCenter(active_vessel, vessels)


class FakeNode:
    def __init__(self, prograde: float = 0.0, radial: float = 0.0, normal: float = 0.0):
        self.prograde = prograde
        self.radial = radial
        self.normal = normal


class FakeMun:
    sphere_of_influence = 2_429_559.1


class FakeSpeedModeCurrent:
    orbit = "orbit-mode"


class FakeSpeedModeLegacy:
    orbital = "orbital-mode"


class FakeSpaceCenterCurrentEnum:
    SpeedMode = FakeSpeedModeCurrent


class FakeSpaceCenterLegacyEnum:
    SpeedMode = FakeSpeedModeLegacy


def test_should_stage_through_separator_when_next_stage_has_fueled_engine():
    controller = KrpcFlightController({"min_autostage_stage": 2})
    vessel = FakeVessel(current_stage=2, engines=[FakeEngine(stage=1, active=False, has_fuel=True)])

    assert controller._should_stage(vessel)


def test_should_stage_current_engine_stage_even_below_safety_floor():
    controller = KrpcFlightController({"min_autostage_stage": 2})
    vessel = FakeVessel(current_stage=1, engines=[FakeEngine(stage=1, active=False, has_fuel=True)])

    assert controller._should_stage(vessel)


def test_should_not_stage_parachute_or_empty_stage_below_safety_floor():
    controller = KrpcFlightController({"min_autostage_stage": 2})
    vessel = FakeVessel(current_stage=1, engines=[])

    assert not controller._should_stage(vessel)


def test_start_sequence_releases_clamp_stage_below_active_engine_stage(monkeypatch):
    controller = KrpcFlightController({"max_launch_start_stages": 2})
    vessel = FakeVessel(
        current_stage=4,
        engines=[FakeEngine(stage=4, active=True, has_fuel=True)],
        parts=[FakePart(stage=3, name="launchClamp1")],
        situation="VesselSituation.pre_launch",
    )
    monkeypatch.setattr("ksp_lab.flight_controller.time.sleep", lambda _seconds: None)

    controller._start_launch_sequence(vessel)

    assert vessel.control.stage_count == 1
    assert vessel.control.current_stage == 3
    assert vessel.control.throttle == 1.0


def test_reacquire_vessel_selects_usable_named_powered_candidate():
    old = FakeVessel(name="AI-HLS-Test", part_count=0, mass=0.0)
    debris = FakeVessel(name="AI-HLS-Test debris", part_count=5, mass=1_000.0, vessel_type="Debris")
    powered = FakeVessel(
        name="AI-HLS-Test Probe",
        engines=[FakeEngine(stage=0, active=True, has_fuel=True)],
        part_count=8,
        mass=12_000.0,
    )
    conn = FakeConnection(old, [old, debris, powered])

    selected = KrpcFlightController._reacquire_vessel(conn, old, "AI-HLS-Test")

    assert selected is powered
    assert conn.space_center.active_vessel is powered


def test_orbital_retrograde_fallback_uses_corrected_vector(monkeypatch):
    controller = KrpcFlightController({})
    vessel = FakeVessel()
    monkeypatch.setattr("ksp_lab.flight_controller.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        KrpcFlightController,
        "_speed_mode_value",
        staticmethod(lambda *_args: (_ for _ in ()).throw(AttributeError("missing speed mode"))),
    )

    assert controller._point_orbital_retrograde(vessel)

    assert vessel.auto_pilot.reference_frame is vessel.orbital_reference_frame
    assert vessel.auto_pilot.target_direction == (0, 1, 0)
    assert vessel.auto_pilot.engaged


def test_speed_mode_value_accepts_current_and_legacy_krpc_names():
    assert KrpcFlightController._speed_mode_value(FakeSpaceCenterCurrentEnum, "orbit", "orbital") == "orbit-mode"
    assert KrpcFlightController._speed_mode_value(FakeSpaceCenterLegacyEnum, "orbit", "orbital") == "orbital-mode"


def test_node_lateral_delta_v_detects_radial_correction():
    assert KrpcFlightController._node_lateral_delta_v(FakeNode(radial=-40.0)) == 40.0
    assert KrpcFlightController._node_lateral_delta_v(FakeNode(radial=3.0, normal=4.0)) == 5.0
    assert KrpcFlightController._node_lateral_delta_v(FakeNode()) == 0.0


def test_pure_prograde_mun_correction_uses_direct_orbital_marker():
    controller = KrpcFlightController({"direct_correction_lateral_threshold_mps": 1.0})

    assert controller._use_direct_prograde_correction("mun_transfer_correction", FakeNode(prograde=-6.0))
    assert not controller._use_direct_prograde_correction(
        "mun_transfer_correction",
        FakeNode(prograde=-6.0, radial=4.0),
    )
    assert not controller._use_direct_prograde_correction("trans_mun_injection", FakeNode(prograde=850.0))


def test_mun_transfer_correction_throttle_is_real_not_starved():
    # DYNAMIC throttle law (replaces the old starved 0.004–0.025 correction cap that could not move a
    # 12 Mm orbit in the node window). accel = 2e6/2e5 = 10 m/s^2; feather_dv = max(1.5, 10*2.5) = 25;
    # throttle = 6/25 = 0.24, bounded to [0.12, 0.45].
    controller = KrpcFlightController({})

    throttle = controller._maneuver_node_throttle(
        "mun_transfer_correction",
        remaining_delta_v_mps=6.0,
        vessel_mass_kg=200_000.0,
        thrust_n=2_000_000.0,
    )
    assert 0.12 <= throttle <= 0.45            # a REAL correction throttle, not the old starved <=0.025
    # The main TMI burn runs FULL throttle far from the node ...
    assert controller._maneuver_node_throttle(
        "trans_mun_injection",
        remaining_delta_v_mps=850.0,
        vessel_mass_kg=200_000.0,
        thrust_n=2_000_000.0,
    ) == 1.0
    # ... and FEATHERS proportionally as it nears the node (small remaining Δv -> below full throttle).
    near = controller._maneuver_node_throttle(
        "trans_mun_injection",
        remaining_delta_v_mps=2.0,
        vessel_mass_kg=200_000.0,
        thrust_n=2_000_000.0,
    )
    assert 0.05 <= near < 1.0


def test_planned_safe_tmi_uses_wider_apoapsis_cap():
    controller = KrpcFlightController(
        {
            "tmi_apoapsis_cap_m": 14_500_000.0,
            "tmi_planned_safe_apoapsis_cap_m": 22_000_000.0,
        }
    )

    assert controller._tmi_apoapsis_cap_m(False) == 14_500_000.0
    assert controller._tmi_apoapsis_cap_m(True) == 22_000_000.0


def test_correction_closest_worsening_detects_bad_late_burn():
    assert KrpcFlightController._correction_closest_is_worsening(
        closest_approach_m=2_640_000.0,
        best_closest_approach_m=2_300_000.0,
        mun_soi_m=2_429_559.1,
        node_time_to_s=-115.0,
        seconds_since_best=30.0,
        min_node_lag_s=60.0,
        min_seconds_since_best_s=18.0,
        worsening_margin_m=120_000.0,
    )


def test_correction_closest_worsening_ignores_recoverable_close_pass():
    assert not KrpcFlightController._correction_closest_is_worsening(
        closest_approach_m=2_300_000.0,
        best_closest_approach_m=2_250_000.0,
        mun_soi_m=2_429_559.1,
        node_time_to_s=-115.0,
        seconds_since_best=30.0,
    )


def test_safe_mun_transfer_candidate_rejects_reentry_periapsis():
    unsafe = {
        "encounter": True,
        "mun_periapsis_m": 60_000.0,
        "kerbin_periapsis_m": 27_000.0,
    }
    safe = {
        "encounter": True,
        "mun_periapsis_m": 60_000.0,
        "kerbin_periapsis_m": 82_000.0,
    }

    assert not KrpcFlightController._is_safe_mun_transfer_candidate(
        unsafe,
        FakeMun(),
        min_kerbin_periapsis_m=70_000.0,
    )
    assert KrpcFlightController._is_safe_mun_transfer_candidate(
        safe,
        FakeMun(),
        min_kerbin_periapsis_m=70_000.0,
    )


def test_opposite_apsis_delta_v_lowers_high_mun_apoapsis():
    delta_v = KrpcFlightController._opposite_apsis_delta_v_mps(
        mu=65_138_398_000.0,
        body_radius_m=200_000.0,
        burn_altitude_m=106_825.0,
        current_opposite_altitude_m=1_919_427.0,
        target_opposite_altitude_m=360_000.0,
    )

    assert -95.0 < delta_v < -75.0


def test_opposite_apsis_delta_v_deorbits_from_mun_apoapsis():
    delta_v = KrpcFlightController._opposite_apsis_delta_v_mps(
        mu=65_138_398_000.0,
        body_radius_m=200_000.0,
        burn_altitude_m=360_000.0,
        current_opposite_altitude_m=106_825.0,
        target_opposite_altitude_m=-5_000.0,
    )

    assert -50.0 < delta_v < -30.0


def test_opposite_apsis_delta_v_raises_relay_apoapsis():
    delta_v = KrpcFlightController._opposite_apsis_delta_v_mps(
        mu=65_138_398_000.0,
        body_radius_m=200_000.0,
        burn_altitude_m=80_000.0,
        current_opposite_altitude_m=360_000.0,
        target_opposite_altitude_m=1_000_000.0,
    )

    assert 45.0 < delta_v < 70.0
