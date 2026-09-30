"""Stage Δv simulation checked against hand computation."""

import json
import math
from pathlib import Path

import pytest

from astra.craft import stagesim
from astra.craft.catalog import Catalog
from astra.craft.geometry import place
from astra.craft.spec import parse_spec
from astra.craft.staging import assign_stages
from astra.craft.stagesim import G0, SimEngine, SimPart, simulate

FIXTURES = Path(__file__).parent / "fixtures"
LFO = [("LiquidFuel", 0.9, "STACK_PRIORITY_SEARCH"), ("Oxidizer", 1.1, "STACK_PRIORITY_SEARCH")]
DENS = {"LiquidFuel": 0.005, "Oxidizer": 0.005, "SolidFuel": 0.0075}


@pytest.fixture(scope="module")
def cat():
    return Catalog.from_json(json.loads((FIXTURES / "parts_subset.json").read_text(encoding="utf-8")))


def swivel(limit=1.0):
    return SimEngine(215.0, 320.0, 250.0, list(LFO), limit)


def hammer():
    return SimEngine(227.0, 195.0, 170.0, [("SolidFuel", 1.0, "NO_FLOW")])


def link(parts):
    """Chain parts in order (each the parent of the next) and return them keyed."""
    for a, b in zip(parts, parts[1:]):
        a.neighbors.append(b.key)
        b.neighbors.append(a.key)
    return parts


def core(stage=0, tank_lf=360.0, tank_ox=440.0, limit=1.0):
    probe = SimPart("probe", "probe", 0.1, {}, dict(DENS))
    tank = SimPart("tank", "tank", 0.5, {"LiquidFuel": tank_lf, "Oxidizer": tank_ox}, dict(DENS))
    eng = SimPart("eng", "eng", 1.5, {}, dict(DENS), activation_stage=stage, engines=[swivel(limit)])
    return link([probe, tank, eng])


def test_single_stage_matches_rocket_equation():
    results = simulate(core(), 1)
    assert [r.stage for r in results] == [1, 0]
    assert results[0].dv_vac_mps == 0 and results[0].start_mass_t == pytest.approx(6.1)
    r = results[1]
    assert r.start_mass_t == pytest.approx(6.1) and r.end_mass_t == pytest.approx(2.1)
    assert r.dv_vac_mps == pytest.approx(320 * G0 * math.log(6.1 / 2.1))
    assert r.dv_mps == pytest.approx(250 * G0 * math.log(6.1 / 2.1))
    assert r.burn_s == pytest.approx(4.0 / (215 / (320 * G0)))
    assert r.thrust_vac_kn == 215 and r.thrust_kn == pytest.approx(215 * 250 / 320)
    assert r.isp(True) == pytest.approx(320) and r.isp(False) == pytest.approx(250)
    assert r.propellant_used_t == pytest.approx({"LiquidFuel": 1.8, "Oxidizer": 2.2})


def test_thrust_limiter_stretches_burn_but_not_dv():
    full, half = simulate(core(), 0)[0], simulate(core(limit=0.5), 0)[0]
    assert half.dv_vac_mps == pytest.approx(full.dv_vac_mps)
    assert half.burn_s == pytest.approx(2 * full.burn_s)
    assert half.thrust_vac_kn == pytest.approx(107.5)


def test_parallel_boosters_separate_while_the_core_keeps_fuel():
    parts = core(stage=2)
    tank = parts[1]
    for i in range(2):
        dec = SimPart(f"dec{i}", "dec", 0.05, {}, dict(DENS), crossfeed=False, decouple_stage=1)
        srb = SimPart(f"srb{i}", "srb", 0.75, {"SolidFuel": 375.0}, dict(DENS), activation_stage=2,
                      decouple_stage=1, engines=[hammer()])
        link([tank, dec, srb])
        parts += [dec, srb]
    s2, s1, s0 = simulate(parts, 2)
    srb_flow, core_flow = 227 / (195 * G0), 215 / (320 * G0)
    t_srb = 375 * 0.0075 / srb_flow
    m0 = 6.1 + 2 * (0.05 + 0.75 + 2.8125)
    m1 = m0 - 2 * 2.8125 - core_flow * t_srb
    assert s2.burn_s == pytest.approx(t_srb)
    assert s2.start_mass_t == pytest.approx(m0) and s2.end_mass_t == pytest.approx(m1)
    f_vac, f_asl = 215 + 2 * 227, 215 * 250 / 320 + 2 * 227 * 170 / 195
    flow = core_flow + 2 * srb_flow
    assert s2.dv_vac_mps == pytest.approx(f_vac / flow * math.log(m0 / m1))
    assert s2.dv_mps == pytest.approx(f_asl / flow * math.log(m0 / m1))
    assert s1.drops == ["dec", "dec", "srb", "srb"]
    assert s1.to_dict(None)["drops"] == ["dec x2", "srb x2"]
    m1_start = m1 - 2 * 0.8
    assert s1.start_mass_t == pytest.approx(m1_start) and s1.end_mass_t == pytest.approx(2.1)
    assert s1.dv_vac_mps == pytest.approx(320 * G0 * math.log(m1_start / 2.1))
    assert s0.dv_vac_mps == 0


def test_lf_only_engine_leaves_oxidizer_behind():
    tank = SimPart("tank", "tank", 0.25, {"LiquidFuel": 180.0, "Oxidizer": 220.0}, dict(DENS))
    nerv = SimPart("nerv", "nerv", 3.0, {}, dict(DENS), activation_stage=0,
                   engines=[SimEngine(60.0, 800.0, 185.0, [("LiquidFuel", 0.9, "STACK_PRIORITY_SEARCH")])])
    (r,) = simulate(link([tank, nerv]), 0)
    assert r.propellant_used_t == pytest.approx({"LiquidFuel": 0.9})
    assert r.end_mass_t == pytest.approx(3.25 + 1.1)
    assert r.dv_vac_mps == pytest.approx(800 * G0 * math.log(5.25 / 4.35))


@pytest.mark.parametrize("crossfeed", [False, True])
def test_drop_tank_feeds_only_through_crossfeed(crossfeed):
    parts = core(stage=2)
    dec = SimPart("dec", "dec", 0.05, {}, dict(DENS), crossfeed=crossfeed, decouple_stage=1)
    drop = SimPart("drop", "drop", 0.5, {"LiquidFuel": 360.0, "Oxidizer": 440.0}, dict(DENS), decouple_stage=1)
    link([parts[1], dec, drop])
    s2, s1, _ = simulate(parts + [dec, drop], 2)
    if crossfeed:  # the drop tank drains first (it is dropped sooner), then staging drops it
        assert s2.end_mass_t == pytest.approx(6.1 + 0.55)
        assert s2.dv_vac_mps == pytest.approx(320 * G0 * math.log(10.65 / 6.65))
        assert s1.start_mass_t == pytest.approx(6.1)
        assert s1.dv_vac_mps == pytest.approx(320 * G0 * math.log(6.1 / 2.1))
    else:  # the core cannot reach it: it is dead weight until dropped, full
        assert s2.end_mass_t == pytest.approx(2.1 + 4.55)
        assert s2.dv_vac_mps == pytest.approx(320 * G0 * math.log(10.65 / 6.65))
        assert s1.dv_vac_mps == 0 and s1.start_mass_t == pytest.approx(2.1)


def test_design_simulation_from_placement(cat):
    spec = parse_spec({"name": "Sim", "parts": [
        {"id": "probe", "part": "probeCoreOcto.v2"},
        {"id": "tank", "part": "fuelTank.long", "parent": "probe", "node": "bottom"},
        {"id": "eng", "part": "liquidEngine2.v2", "parent": "tank", "node": "bottom", "thrust_limit_pct": 50},
    ]})
    placement = place(spec, cat)
    assign_stages(placement)
    sim_parts = stagesim.parts_from_placement(placement, cat, pressure_atm=1.0)
    (r,) = [x for x in simulate(sim_parts, 1) if x.dv_vac_mps > 0]
    dry = sum(p.info.mass_t for p in placement.parts)
    wet = dry + 4.0
    assert r.start_mass_t == pytest.approx(wet) and r.end_mass_t == pytest.approx(dry)
    assert r.dv_vac_mps == pytest.approx(320 * G0 * math.log(wet / dry))
    assert r.dv_mps == pytest.approx(250 * G0 * math.log(wet / dry))
    assert r.thrust_vac_kn == pytest.approx(107.5)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_live_vessel_builder_converts_units_and_fills_inactive_isp(cat):
    tank = _Obj(_object_id=1, name="fuelTank", title="FL-T400", dry_mass=250.0, crossfeed=True, stage=-1,
                decouple_stage=-1, engine=None, fuel_lines_from=[], children=[],
                resources=_Obj(all=[_Obj(name="LiquidFuel", amount=180.0, density=5.0),
                                    _Obj(name="Oxidizer", amount=220.0, density=5.0)]))
    engine = _Obj(active=False, propellant_ratios={"LiquidFuel": 0.9, "Oxidizer": 1.1}, max_vacuum_thrust=60000.0,
                  vacuum_specific_impulse=345.0, kerbin_sea_level_specific_impulse=0.0, thrust_limit=1.0,
                  specific_impulse_at=lambda p: 0.0)
    eng = _Obj(_object_id=2, name="liquidEngine3.v2", title="LV-909", dry_mass=500.0, crossfeed=True, stage=0,
               decouple_stage=-1, engine=engine, fuel_lines_from=[], children=[], parent=tank,
               resources=_Obj(all=[]))
    tank.children, tank.parent = [eng], None
    vessel = _Obj(parts=_Obj(all=[tank, eng]))
    modes = {"LiquidFuel": "ResourceFlowMode.adjacent", "Oxidizer": "ResourceFlowMode.adjacent"}
    parts = stagesim.parts_from_vessel(vessel, 1.0, current_stage=1, flow_mode=modes.get, catalog=cat)
    t, e = parts
    assert t.dry_mass_t == 0.25 and t.densities == {"LiquidFuel": 0.005, "Oxidizer": 0.005}
    assert t.neighbors == ["p1"] and e.neighbors == ["p0"]
    (se,) = e.engines
    assert se.thrust_vac_kn == 60.0 and se.isp_vac_s == 345.0
    assert se.isp_s == pytest.approx(cat.get("liquidEngine3.v2").engines[0].isp(1.0))  # from the part's curve
    assert se.propellants[0] == ("LiquidFuel", 0.9, "STACK_PRIORITY_SEARCH")
    assert e.activation_stage == 0
    engine.active = True
    parts = stagesim.parts_from_vessel(vessel, 1.0, current_stage=1, flow_mode=modes.get, catalog=cat)
    assert parts[1].activation_stage == 1  # already burning: counts as lit in the current stage
    s1, s0 = simulate(parts, 1)
    assert s1.dv_vac_mps > 0 and s0.dv_vac_mps == 0


def test_from_spec_entry_point(cat):
    spec = {"name": "Entry", "parts": [
        {"id": "probe", "part": "probeStackSmall"},
        {"id": "tank", "part": "fuelTank.long", "parent": "probe", "node": "bottom"},
        {"id": "eng", "part": "liquidEngine2.v2", "parent": "tank", "node": "bottom"}]}
    out = stagesim.from_spec(spec, cat, pressure_atm=0.0, gravity_mps2=9.81)
    dry = cat.get("probeStackSmall").mass_t + 0.5 + 1.5
    assert out["total_dv_vac_mps"] == pytest.approx(320 * G0 * math.log((dry + 4.0) / dry))
    assert out["total_dv_mps"] == pytest.approx(out["total_dv_vac_mps"])  # vacuum column at 0 atm
    (row,) = [r for r in out["stages"] if r["dv_vac_mps"] > 0]
    assert row["twr_start"] == pytest.approx(215 / ((dry + 4.0) * 9.81))


def test_launch_clamps_stay_on_the_pad(cat):
    base = {"name": "Clamped", "parts": [
        {"id": "probe", "part": "probeStackSmall"},
        {"id": "tank", "part": "fuelTank.long", "parent": "probe", "node": "bottom"},
        {"id": "eng", "part": "liquidEngine2.v2", "parent": "tank", "node": "bottom"},
    ]}
    free = stagesim.from_spec(base, cat, 1.0, 9.81)["stages"]
    clamped = dict(base, parts=base["parts"] + [
        {"id": "clamp", "part": "launchClamp1", "parent": "tank", "surface": {"azimuth_deg": 0}, "symmetry": 2}])
    held = stagesim.from_spec(clamped, cat, 1.0, 9.81)["stages"]
    launch_free = next(s for s in free if s.get("engines"))
    launch_held = next(s for s in held if s.get("engines"))
    assert launch_held["start_mass_t"] == pytest.approx(launch_free["start_mass_t"])  # clamps not lifted
    assert launch_held["dv_vac_mps"] == pytest.approx(launch_free["dv_vac_mps"])
    assert launch_held["drops"] == ["clamp x2"]
