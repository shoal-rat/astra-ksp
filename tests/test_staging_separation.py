import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import deploy_relay  # noqa: E402


class _Part:
    def __init__(self, name, parent=None):
        self.name = name
        self.parent = parent
        self.children = []
        if parent is not None:
            parent.children.append(self)


class _Dec:
    def __init__(self, part):
        self.part = part
        self.decoupled = False


class _Eng:
    def __init__(self, part):
        self.part = part


class _Vessel:
    class _Parts:
        def __init__(self, decs, root, engines):
            self.decouplers = decs
            self.root = root
            self.engines = engines

    def __init__(self, decs, root, engines):
        self.parts = _Vessel._Parts(decs, root, engines)


class _LiveDec:
    """A decoupler with a live ``.decouple()`` that records whether it fired — for testing _guarded_decouple
    (which actually CALLS .decouple() on the safe side and must NOT call it on the protected side)."""
    def __init__(self, part):
        self.part = part
        self.decoupled = False
        self.fired = False

    def decouple(self):
        self.fired = True
        self.decoupled = True


def _stack(n_inter):
    """Build a mock part tree: probe(root) -> bus -> PAYLOAD decoupler -> upper tank -> upper engine ->
    [inter decoupler -> booster tank -> booster engine] x n_inter (each deeper than the last).

    The PAYLOAD decoupler must be protected because firing it would leave the active (root) side — the
    probe + bus — with NO engine. Each inter-stage decoupler is safe to fire because the upper engine stays
    on the root side."""
    probe = _Part("probe")
    bus = _Part("bus", probe)
    payload_dec = _Part("payload_decoupler", bus)
    upper_tank = _Part("upper_tank", payload_dec)
    upper_engine = _Part("upper_engine", upper_tank)
    engines = [upper_engine]
    cur = upper_engine
    inter_parts = []
    for i in range(n_inter):
        d = _Part(f"inter_decoupler_{i}", cur)
        inter_parts.append(d)
        bt = _Part(f"booster_tank_{i}", d)
        cur = _Part(f"booster_engine_{i}", bt)
        engines.append(cur)
    decs = [_Dec(payload_dec)] + [_Dec(ip) for ip in inter_parts]
    return _Vessel(decs, probe, [_Eng(e) for e in engines]), payload_dec, inter_parts


def test_payload_decoupler_is_never_an_ascent_separator():
    # The payload decoupler (firing it leaves the root/probe side without an engine) must NEVER be returned,
    # so the payload can never be jettisoned when the upper stage later runs dry during circularization.
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
    probe = _Part("probe")
    vessel = _Vessel([_Dec(_Part("payload_decoupler", probe))], probe, [])
    assert deploy_relay._inter_stage_decouplers(vessel) == []


def test_crewed_two_payload_decouplers_are_both_protected():
    """REGRESSION for the crewed-launch bug: the crewed vehicle has TWO high decouplers above the engine —
    the heat-shield boundary AND the upper-stage boundary. Firing EITHER keeps the engineless capsule as the
    active vessel and jettisons the whole upper stage (it coasted suborbital on every crewed launch). The old
    'protect only the shallowest' heuristic shielded one and fired the other. The engine-aware rule protects
    BOTH (root side has no engine) and fires only the genuine booster decoupler.
    Tree: pod(root) -> heatshield -> hs_dec -> probe -> fairing -> payload_dec -> upper_tank -> upper_engine
          -> inter_dec -> booster_tank -> booster_engine."""
    pod = _Part("pod")
    hs = _Part("heatshield", pod)
    hs_dec = _Part("hs_decoupler", hs)
    probe = _Part("probe", hs_dec)
    fairing = _Part("fairing", probe)
    payload_dec = _Part("payload_decoupler", fairing)
    upper_tank = _Part("upper_tank", payload_dec)
    upper_engine = _Part("upper_engine", upper_tank)
    inter_dec = _Part("inter_decoupler", upper_engine)
    booster_tank = _Part("booster_tank", inter_dec)
    booster_engine = _Part("booster_engine", booster_tank)
    decs = [_Dec(hs_dec), _Dec(payload_dec), _Dec(inter_dec)]
    vessel = _Vessel(decs, pod, [_Eng(upper_engine), _Eng(booster_engine)])
    inter = deploy_relay._inter_stage_decouplers(vessel)
    assert [d.part.name for d in inter] == ["inter_decoupler"]          # only the booster decoupler fires
    assert all(d.part not in (hs_dec, payload_dec) for d in inter)      # both payload decouplers protected


def _tug_stack():
    """The TUG topology that the in-space capture-burn staging mis-fired and stranded the crew pod at Eve:

        pod(root) -> payload_dec -> heatshield -> chutes -> upper_engine -> inter_dec -> booster_tank
                  -> booster_engine

    Firing payload_dec leaves the active (pod) side with NO engine (everything below it — heat shield, chutes,
    upper engine, booster — is jettisoned): that is EXACTLY the live failure (pod stranded, engines=0, no heat
    shield / chutes). Firing inter_dec drops only the spent booster while the upper_engine stays on the pod
    side. Returns (vessel, payload_dec_part, inter_dec_part)."""
    pod = _Part("pod")
    payload_dec = _Part("payload_decoupler", pod)
    heatshield = _Part("heatshield", payload_dec)
    chutes = _Part("chutes", heatshield)
    upper_engine = _Part("upper_engine", chutes)
    inter_dec = _Part("inter_decoupler", upper_engine)
    booster_tank = _Part("booster_tank", inter_dec)
    booster_engine = _Part("booster_engine", booster_tank)
    decs = [_Dec(payload_dec), _Dec(inter_dec)]
    vessel = _Vessel(decs, pod, [_Eng(upper_engine), _Eng(booster_engine)])
    return vessel, payload_dec, inter_dec


def test_tug_payload_decoupler_protected_only_booster_separator_fires():
    # REGRESSION for the in-space capture-burn bug: on the tug, only the booster inter-stage decoupler may be
    # a separator. The payload decoupler (pod | heatshield+chutes+upper+booster) must NEVER be a separator —
    # firing it strands the engineless pod, which is what the live run did at Eve (2761x7995 km, engines=0).
    vessel, payload_dec, inter_dec = _tug_stack()
    inter = deploy_relay._inter_stage_decouplers(vessel)
    assert [d.part.name for d in inter] == ["inter_decoupler"]          # only the booster decoupler
    assert all(d.part is not payload_dec for d in inter)               # heat-shield/payload decoupler protected


def test_guarded_decouple_fires_the_booster_but_never_the_tug_payload_decoupler():
    # _guarded_decouple is the SINGLE choke point every decouple (ascent AND in-space) passes through. It must
    # actually fire the safe booster decoupler and REFUSE to fire the payload/heat-shield decoupler.
    vessel, payload_dec, inter_dec = _tug_stack()
    safe = _LiveDec(inter_dec)
    protected = _LiveDec(payload_dec)
    assert deploy_relay._guarded_decouple(vessel, safe) is True
    assert safe.fired is True                                          # the spent booster is dropped
    assert deploy_relay._guarded_decouple(vessel, protected) is False
    assert protected.fired is False                                    # the crew pod's engine is preserved


def test_root_side_keeps_engine_distinguishes_tug_decouplers():
    # The underlying predicate: firing the inter_dec keeps the upper engine on the pod side (True); firing the
    # payload_dec leaves no engine on the pod side (False).
    vessel, payload_dec, inter_dec = _tug_stack()
    decs = {d.part: d for d in vessel.parts.decouplers}
    assert deploy_relay._root_side_keeps_engine(vessel, decs[inter_dec]) is True
    assert deploy_relay._root_side_keeps_engine(vessel, decs[payload_dec]) is False
