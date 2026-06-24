import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import deploy_relay  # noqa: E402


class _Part:
    def __init__(self, name, parent=None):
        self.name = name
        self.parent = parent


class _Dec:
    def __init__(self, part):
        self.part = part
        self.decoupled = False


class _Vessel:
    class _Parts:
        def __init__(self, decs):
            self.decouplers = decs

    def __init__(self, decs):
        self.parts = _Vessel._Parts(decs)


def _stack(n_inter):
    """Build a mock part tree: probe(root) -> bus -> PAYLOAD decoupler -> upper tank -> upper engine ->
    [inter decoupler -> booster tank -> booster engine] x n_inter (each deeper than the last)."""
    cur = _Part("probe")
    cur = _Part("bus", cur)
    payload_dec = _Part("payload_decoupler", cur)
    cur = _Part("upper_tank", payload_dec)
    cur = _Part("upper_engine", cur)
    inter_parts = []
    for i in range(n_inter):
        d = _Part(f"inter_decoupler_{i}", cur)
        inter_parts.append(d)
        cur = _Part(f"booster_tank_{i}", d)
        cur = _Part(f"booster_engine_{i}", cur)
    decs = [_Dec(payload_dec)] + [_Dec(ip) for ip in inter_parts]
    return _Vessel(decs), payload_dec, inter_parts


def test_payload_decoupler_is_never_an_ascent_separator():
    # The shallowest decoupler (between the comsat bus and the final stage) must NEVER be returned, so the
    # payload can never be jettisoned when the upper stage later runs dry during circularization.
    vessel, payload_dec, inter_parts = _stack(1)
    inter = deploy_relay._inter_stage_decouplers(vessel)
    assert len(inter) == 1
    assert inter[0].part is inter_parts[0]                 # the booster decoupler
    assert all(d.part is not payload_dec for d in inter)   # never the payload decoupler


def test_inter_stage_decouplers_fire_deepest_first():
    # Two boosters -> two inter-stage decouplers; the deeper (lower) one must fire first.
    vessel, payload_dec, inter_parts = _stack(2)
    inter = deploy_relay._inter_stage_decouplers(vessel)
    assert len(inter) == 2
    assert deploy_relay._depth_from_root(inter[0].part) > deploy_relay._depth_from_root(inter[1].part)
    assert all(d.part is not payload_dec for d in inter)


def test_lone_payload_decoupler_yields_no_ascent_separators():
    # A craft with only the payload decoupler (a single stage) has nothing to separate during ascent.
    vessel = _Vessel([_Dec(_Part("payload_decoupler", _Part("probe")))])
    assert deploy_relay._inter_stage_decouplers(vessel) == []
