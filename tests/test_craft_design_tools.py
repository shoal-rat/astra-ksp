"""The design tools through the registry, with the fixture catalog and no running game."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import astra.tools.design as design
from astra import registry
from astra.craft.catalog import Catalog
from astra.craft.spec import SpecError
from astra.errors import AstraError, NotConnected

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def cat():
    return Catalog.from_json(json.loads((FIXTURES / "parts_subset.json").read_text(encoding="utf-8")))


@pytest.fixture(autouse=True)
def offline(monkeypatch, cat, tmp_path):
    monkeypatch.setattr(design, "_catalog", lambda: cat)
    monkeypatch.setattr(design, "_body_constants", lambda body, p: (
        {"name": body or "Kerbin", "g_mps2": 9.81, "sea_level_pressure_atm": 1.0,
         "pressure_atm": 1.0 if p is None else p}, []))
    monkeypatch.setattr(design, "_active_save", lambda: "testsave")
    monkeypatch.setattr(design, "CONFIG", SimpleNamespace(saves_dir=tmp_path / "saves", ksp_dir=tmp_path / "ksp"))


def rocket(**changes):
    parts = [
        {"id": "probe", "part": "probeStackSmall"},
        {"id": "tank", "part": "fuelTank.long", "parent": "probe", "node": "bottom"},
        {"id": "eng", "part": "liquidEngine2.v2", "parent": "tank", "node": "bottom"},
    ]
    for p in parts:
        p.update(changes.get(p["id"], {}))
    return {"name": "Tool Test", "parts": parts + changes.get("extra", [])}


def test_tools_are_registered_with_schemas():
    for name, group in (("parts_search", "design"), ("part_info", "design"), ("design_check", "design"),
                        ("design_build", "design"), ("design_from_craft", "design"), ("craft_list", "design"),
                        ("vessel_stages", "observe")):
        spec = registry.TOOLS[name]
        assert spec.group == group and spec.summary
        schema = registry.arg_model(spec).model_json_schema()
        for prop in schema.get("properties", {}).values():
            assert prop.get("description"), (name, prop)


def test_parts_search_and_part_info():
    out = registry.validate_and_call("parts_search", {"query": "terrier"})
    assert out["parts"][0]["name"] == "liquidEngine3.v2"
    row = out["parts"][0]
    assert row["thrust_vac_kn"] == 60 and row["isp_vac_s"] == 345 and row["propellants"] == {"LiquidFuel": 0.9, "Oxidizer": 1.1}
    with pytest.raises(AstraError):
        registry.validate_and_call("parts_search", {"role": "rocket"})
    info = registry.validate_and_call("part_info", {"name": "fuelTank"})
    assert [n["id"] for n in info["nodes"]] == ["top", "bottom"] and info["radius_m"] == 0.625
    eng = registry.validate_and_call("part_info", {"name": "liquidEngine2_v2"})
    assert eng["engines"][0]["thrust_asl_kn"] == pytest.approx(215 * 250 / 320)


def test_design_check_report():
    r = registry.validate_and_call("design_check", {"spec": rocket()})
    assert r["ok"] and r["parts"] == 3
    assert r["wet_mass_t"] == pytest.approx(r["dry_mass_t"] + 4.0)
    (stage,) = [s for s in r["stages"] if s.get("dv_vac_mps")]
    assert stage["event"] == "launch" and stage["twr_start"] == pytest.approx(stage["thrust_kn"] / (r["wet_mass_t"] * 9.81))
    assert r["total_dv_vac_mps"] == pytest.approx(stage["dv_vac_mps"])
    assert r["height_m"] > 3.5 and 1.25 <= r["max_diameter_m"] < 1.5  # the Swivel bell is a little wider
    assert not [w for w in r["warnings"] if not w.startswith("info")]
    full = registry.validate_and_call("design_check", {"spec": json.dumps(rocket()), "detail": "full"})
    assert [row["key"] for row in full["placement"]] == ["probe", "tank", "eng"] and full["spec"]["name"] == "Tool Test"


def test_design_check_warnings():
    spec = rocket(
        eng={"part": "nuclearEngine", "thrust_limit_pct": 80},
        extra=[
            {"id": "empty_dec", "part": "radialDecoupler2", "parent": "tank", "surface": {"azimuth_deg": 0}},
            {"id": "drop_dec", "part": "radialDecoupler2", "parent": "tank", "surface": {"azimuth_deg": 180}},
            {"id": "drop", "part": "fuelTank", "parent": "drop_dec", "surface": {}},
            {"id": "far_fin", "part": "basicFin", "parent": "tank", "surface": {"azimuth_deg": 90, "radius_m": 1.4}},
            {"id": "side_dec", "part": "radialDecoupler2", "parent": "tank", "surface": {"azimuth_deg": 270}},
            {"id": "side_pod", "part": "fuelTank", "parent": "tank", "surface": {"azimuth_deg": 272, "radius_m": 1.3}},
            {"id": "side_fin", "part": "basicFin", "parent": "side_dec", "surface": {}},
        ])
    r = registry.validate_and_call("design_check", {"spec": spec})
    text = "\n".join(r["warnings"])
    assert "empty_dec" in text and "holds nothing" in text
    assert "burns LiquidFuel only" in text
    assert "drop_dec" in text and "crossfeed" in text
    assert "thrust limiter 80" in text
    assert "TWR" in text and "will not lift off" in text
    assert "far_fin floats 0.77 m" in text  # 1.4 m vs the 0.625 m skin
    assert "side_pod hangs on tank beside side_dec" in text


def test_design_check_rejects_bad_specs():
    with pytest.raises(SpecError) as err:
        registry.validate_and_call("design_check", {"spec": rocket(eng={"node": "side"})})
    assert err.value.issues[0].part == "eng" and "top" in err.value.issues[0].fix


def test_design_check_counts_crew_mass(cat):
    crewed = {"name": "Crewed", "parts": [{"id": "pod", "part": "mk1pod.v2"},
                                          {"id": "tank", "part": "fuelTank", "parent": "pod", "node": "bottom"}]}
    empty = registry.validate_and_call("design_check", {"spec": crewed})
    with_crew = registry.validate_and_call("design_check", {"spec": crewed, "crew": 2})
    assert any("have no seat" in n for n in with_crew.get("notes", []))
    kerbal = cat.kerbal_mass_t or 0.0
    assert with_crew["wet_mass_t"] == pytest.approx(empty["wet_mass_t"] + kerbal)


def test_design_build_writes_into_the_active_save(tmp_path):
    r = registry.validate_and_call("design_build", {"spec": rocket()})
    path = Path(r["path"])
    assert path == tmp_path / "saves" / "testsave" / "Ships" / "VAB" / "Tool Test.craft" and path.exists()
    assert r["craft"] == "Tool Test" and r["save"] == "testsave" and r["stages"]
    with pytest.raises(AstraError):
        registry.validate_and_call("design_build", {"spec": rocket()})
    again = registry.validate_and_call("design_build", {"spec": rocket(), "overwrite": True})
    assert again["path"] == r["path"]


def test_design_from_craft_and_craft_list(monkeypatch):
    out = registry.validate_and_call("design_from_craft", {"craft": str(FIXTURES / "Kerbal 1-5.craft")})
    assert out["spec"]["name"] == "Kerbal 1-5" and out["parts"] == 18
    assert any("RCSBlock.v2" in n for n in out["notes"])
    ids = {p["id"]: p for p in out["spec"]["parts"]}
    assert ids["radialDecoupler2"]["symmetry"] == 2 and ids["basicFin"]["symmetry"] == 4
    assert ids["liquidEngine2"]["thrust_limit_pct"] == 60  # the stock craft's limiter, made explicit
    monkeypatch.setattr(design, "_craft_dirs", lambda: [("stock", FIXTURES)])
    listing = registry.validate_and_call("craft_list", {})
    names = {row["name"]: row for row in listing["stock_craft"]}
    assert names["Kerbal 1-5"]["parts"] == 27
    by_name = registry.validate_and_call("design_from_craft", {"craft": "kerbal 1-5"})
    assert by_name["source"].endswith("Kerbal 1-5.craft")
    with pytest.raises(AstraError):
        registry.validate_and_call("design_from_craft", {"craft": "No Such Craft"})


def test_body_constants_without_krpc(monkeypatch):
    monkeypatch.undo()  # the real helper, with a game that is not reachable

    class Down:
        def scene(self):
            raise NotConnected("down")

        def body(self, name):
            raise NotConnected("down")

    monkeypatch.setattr(design, "ksp", lambda: Down())
    consts, notes = design._body_constants(None, None)
    assert consts["g_mps2"] is None and consts["pressure_atm"] == 0.0 and notes


def test_vessel_stages_with_a_fake_vessel(monkeypatch, cat):
    def res(name, amount):
        return SimpleNamespace(name=name, amount=amount, density=5.0)

    engine = SimpleNamespace(active=True, propellant_ratios={"LiquidFuel": 0.9, "Oxidizer": 1.1},
                             max_vacuum_thrust=215000.0, vacuum_specific_impulse=320.0,
                             kerbin_sea_level_specific_impulse=250.0, thrust_limit=1.0,
                             specific_impulse_at=lambda p: 320 + (250 - 320) * p)
    tank = SimpleNamespace(_object_id=1, name="fuelTank.long", title="FL-T800", dry_mass=500.0, crossfeed=True,
                           stage=-1, decouple_stage=-1, engine=None, fuel_lines_from=[], parent=None,
                           resources=SimpleNamespace(all=[res("LiquidFuel", 360.0), res("Oxidizer", 440.0)]))
    eng = SimpleNamespace(_object_id=2, name="liquidEngine2.v2", title="LV-T45", dry_mass=1500.0, crossfeed=True,
                          stage=0, decouple_stage=-1, engine=engine, fuel_lines_from=[], parent=tank, children=[],
                          resources=SimpleNamespace(all=[]))
    tank.children = [eng]
    body = SimpleNamespace(name="Kerbin", gravitational_parameter=3.5316e12)
    vessel = SimpleNamespace(name="Fake", mass=6000.0, parts=SimpleNamespace(all=[tank, eng]),
                             orbit=SimpleNamespace(body=body, radius=600000.0 + 1000.0),
                             flight=lambda *a: SimpleNamespace(static_pressure=101325.0 * 0.5),
                             control=SimpleNamespace(current_stage=0))
    flow = SimpleNamespace(flow_mode=lambda name: "ResourceFlowMode.adjacent")
    fake = SimpleNamespace(vessel=lambda: vessel, sc=SimpleNamespace(Resources=flow),
                           bridge=SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(NotConnected("no"))))
    monkeypatch.setattr(design, "ksp", lambda: fake)
    out = registry.validate_and_call("vessel_stages", {})
    assert out["current_stage"] == 0 and out["pressure_atm"] == pytest.approx(0.5)
    (st,) = out["stages"]
    import math
    assert st["dv_vac_mps"] == pytest.approx(320 * 9.80665 * math.log(6.0 / 2.0))
    assert st["dv_mps"] == pytest.approx(285 * 9.80665 * math.log(6.0 / 2.0))
    g = 3.5316e12 / 601000.0 ** 2
    assert out["local_g_mps2"] == pytest.approx(g)
    assert st["twr_vac_start"] == pytest.approx(215 / (6.0 * g))
    assert "mechjeb" not in out


# --- review additions ------------------------------------------------------------------------------

_REAL_ACTIVE_SAVE = design._active_save  # captured before the autouse fixture replaces it


@pytest.mark.parametrize("folder", ["../../Windows", "a/b", r"c:\x", r"..\x", "..", ""])
def test_active_save_refuses_folders_outside_saves(monkeypatch, folder):
    fake = SimpleNamespace(bridge=SimpleNamespace(get=lambda *a, **k: {"saveFolder": folder}))
    monkeypatch.setattr(design, "ksp", lambda: fake)
    with pytest.raises(AstraError):
        _REAL_ACTIVE_SAVE()


def test_active_save_accepts_plain_and_localized_folders(monkeypatch):
    fake = SimpleNamespace(bridge=SimpleNamespace(get=lambda *a, **k: {"saveFolder": "默认"}))
    monkeypatch.setattr(design, "ksp", lambda: fake)
    assert _REAL_ACTIVE_SAVE() == "默认"


def test_parts_search_rejects_unknown_propellant_family():
    with pytest.raises(AstraError) as err:
        registry.validate_and_call("parts_search", {"propellant": "kerosene"})
    assert "LFO" in str(err.value)
    assert registry.validate_and_call("parts_search", {"propellant": "lf", "role": "liquid_engine"})["count"] >= 1


def test_tag_on_a_part_without_a_name_tag_module_is_flagged(cat, monkeypatch):
    import copy
    probe = copy.deepcopy(cat.get("probeStackSmall"))
    probe.modules = [m for m in probe.modules if m != "KOSNameTag"]
    parts = dict(cat.parts, probeStackSmall=probe)
    no_tag_cat = Catalog(parts, cat.resource_defs, cat.source)
    monkeypatch.setattr(design, "_catalog", lambda: no_tag_cat)
    r = registry.validate_and_call("design_check", {"spec": rocket(probe={"tag": "brain"}, tank={"tag": "main"})})
    flagged = [w for w in r["warnings"] if "KOSNameTag" in w]
    assert len(flagged) == 1 and flagged[0].startswith("probe:")
