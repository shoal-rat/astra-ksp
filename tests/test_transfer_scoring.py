from ksp_lab.flight_controller import KrpcFlightController


class Body:
    def __init__(self, name):
        self.name = name


class Orbit:
    def __init__(self, body_name, periapsis_altitude=0.0, next_orbit=None):
        self.body = Body(body_name)
        self.periapsis_altitude = periapsis_altitude
        self.next_orbit = next_orbit

    def distance_at_closest_approach(self, other):
        return 1_000_000.0


class Node:
    def __init__(self, orbit):
        self.orbit = orbit


class Mun:
    orbit = Orbit("Mun")


def test_orion_transfer_scoring_prefers_free_return_candidate():
    free_return = Node(Orbit("Kerbin", next_orbit=Orbit("Mun", 60_000.0, Orbit("Kerbin", 35_000.0))))
    no_return = Node(Orbit("Kerbin", next_orbit=Orbit("Mun", 60_000.0)))

    free_score = KrpcFlightController._score_mun_transfer_node(
        free_return,
        Mun(),
        transfer_profile="orion_free_return",
    )
    no_return_score = KrpcFlightController._score_mun_transfer_node(
        no_return,
        Mun(),
        transfer_profile="orion_free_return",
    )

    assert free_score["free_return"]
    assert not no_return_score["free_return"]
    assert free_score["score"] < no_return_score["score"]
