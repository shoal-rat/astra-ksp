from __future__ import annotations

from dataclasses import dataclass

from ksp_lab import bodies
from tools.commander import design_for_target


@dataclass(slots=True)
class FakeOrbit:
    body: "FakeBody"
    semi_major_axis: float


@dataclass(slots=True)
class FakeBody:
    name: str
    gravitational_parameter: float
    equatorial_radius: float
    surface_gravity: float
    atmosphere_depth: float
    has_atmosphere: bool
    rotational_speed: float
    orbit: FakeOrbit | None = None


class FakeSpaceCenter:
    def __init__(self) -> None:
        sun = FakeBody("Sun", bodies.SUN.mu, bodies.SUN.radius_m, 0.0, 0.0, False, 0.0)
        kerbin = FakeBody(
            "Kerbin",
            bodies.KERBIN.mu,
            bodies.KERBIN.radius_m,
            bodies.KERBIN.surface_g,
            bodies.KERBIN.atmosphere_top_m,
            True,
            bodies.KERBIN.rotational_speed_mps / bodies.KERBIN.radius_m,
        )
        duna = FakeBody(
            "Duna",
            bodies.DUNA.mu,
            bodies.DUNA.radius_m,
            bodies.DUNA.surface_g,
            bodies.DUNA.atmosphere_top_m,
            True,
            bodies.DUNA.rotational_speed_mps / bodies.DUNA.radius_m,
        )
        kerbin.orbit = FakeOrbit(sun, bodies.KERBIN.orbit_radius_m)
        duna.orbit = FakeOrbit(sun, bodies.DUNA.orbit_radius_m)
        self.bodies = {"Sun": sun, "Kerbin": kerbin, "Duna": duna}


def test_commander_duna_round_trip_reserves_a_dedicated_orbit_insertion_stage():
    design = design_for_target(FakeSpaceCenter(), "Duna", crew=1, want_return=True, name="AI-DUNA-CMDR")

    roles = [stage.role for stage in design.stages]
    assert roles[:3] == ["launch", "orbit_insertion", "trans_target_injection"], design.notes
    assert design.stages[1].engine != "liquidEngine3.v2", design.notes
    assert "orbit_insertion: need" in design.notes
