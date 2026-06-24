from ksp_lab.mission import MissionPlanner
from ksp_lab import astro, bodies, budget
from ksp_lab.models import MissionSpec


def test_orbit_goal_extracts_altitude_and_payload():
    mission = MissionPlanner().interpret("deliver 750 kg payload to 80 km Kerbin orbit")
    assert mission.mission_type == "kerbin_orbit"
    assert mission.target_orbit_m == 80000
    assert mission.payload_mass_t == 0.75


def test_mun_goal_is_crewed_return_profile():
    mission = MissionPlanner().interpret("crewed Mun landing and safe return")
    assert mission.mission_type == "mun_landing_return"
    assert mission.crewed
    assert mission.require_landing
    assert mission.require_return


def test_artemis_goal_uses_split_hls_orion_architecture():
    mission = MissionPlanner().interpret("replicate Artemis with SLS Orion and Starship HLS to the moon and back")
    assert mission.mission_type == "artemis_hls_orion_return"
    assert mission.crewed
    assert mission.require_landing
    assert mission.require_return
    assert mission.target_orbit_m == 80000
    assert "launch a high Mun relay satellite for signal coverage" in mission.phases
    assert "predeploy Starship HLS analogue to Mun orbit" in mission.phases
    assert "perform crewed Mun surface science" in mission.phases


def test_duna_capture_budget_uses_arrival_excess_speed():
    mission = MissionSpec(goal="Duna orbiter", mission_type="duna_orbit", target_body="Duna")
    mb = budget.mission_budget(mission)
    capture = next(p for p in mb.phases if p.name == "capture_duna_orbit")

    r_arrival = bodies.DUNA.low_orbit_radius_m()
    arrival_vinf = astro.transfer_arrival_excess_speed(
        bodies.SUN.mu, bodies.KERBIN.orbit_radius_m, bodies.DUNA.orbit_radius_m
    )
    departure_vinf = astro.transfer_departure_excess_speed(
        bodies.SUN.mu, bodies.KERBIN.orbit_radius_m, bodies.DUNA.orbit_radius_m
    )
    expected = astro.capture_from_excess(bodies.DUNA.mu, r_arrival, arrival_vinf)
    wrong_departure_side = astro.capture_from_excess(bodies.DUNA.mu, r_arrival, departure_vinf)

    assert capture.dv_mps == expected
    assert capture.dv_mps < wrong_departure_side - 25.0
