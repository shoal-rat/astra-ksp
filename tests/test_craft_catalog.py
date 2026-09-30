"""Part catalog: cfg parsing, live overlays, the extended bridge contract, search, caching."""

import json
from pathlib import Path

import pytest

from astra.craft.catalog import (Catalog, CatalogError, EngineMode, FloatCurve, PartInfo, bulkhead_diameter,
                                 classify, drag_cube_bounds, english_title, live_name, overlay_basic_live,
                                 part_from_bridge_full, part_from_cfg, resource_defs_from)
from astra.craft.confignode import ConfigNode

FIXTURES = Path(__file__).parent / "fixtures"
KSP = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Kerbal Space Program")

RESOURCES = """
RESOURCE_DEFINITION
{
  name = LiquidFuel
  density = 0.005
  flowMode = STACK_PRIORITY_SEARCH
}
RESOURCE_DEFINITION
{
  name = Oxidizer
  density = 0.005
  flowMode = STACK_PRIORITY_SEARCH
}
RESOURCE_DEFINITION
{
  name = SolidFuel
  density = 0.0075
  flowMode = NO_FLOW
}
"""

LEGACY_ENGINE = """
PART
{
	name = liquidEngine2
	scale = 0.1
	node_stack_top = 0.0, 7.21461, 0.0, 0.0, 1.0, 0.0
	node_stack_bottom = 0.0, -5.74338, 0.0, 0.0, -1.0, 0.0, 2
	title = #autoLOC_500401 //#autoLOC_500401 = LV-T45 "Swivel" Liquid Fuel Engine
	category = Engine
	attachRules = 1,0,1,0,0
	mass = 1.5
	bulkheadProfiles = size1
	MODULE
	{
		name = ModuleEngines
		maxThrust = 215
		minThrust = 0
		PROPELLANT
		{
			name = LiquidFuel
			ratio = 0.9
		}
		PROPELLANT
		{
			name = Oxidizer
			ratio = 1.1
		}
		atmosphereCurve
		{
			key = 0 320
			key = 1 250
			key = 6 0.001
		}
	}
	MODULE
	{
		name = ModuleGimbal
		gimbalRange = 3
	}
}
"""

RADIAL_DECOUPLER = """
PART
{
	name = radialDecoupler2
	rescaleFactor = 1
	node_attach = -0.03, 0.0, 0.0, 1.0, 0.0, 0.0
	title = TT-70 Radial Decoupler
	category = Coupling
	attachRules = 0,1,0,1,0
	mass = 0.05
	stagingIcon = DECOUPLER_HOR
	bulkheadProfiles = srf
	fuelCrossFeed = False
	MODULE
	{
		name = ModuleAnchoredDecoupler
		ejectionForce = 260
		explosiveNodeID = srf
	}
	MODULE
	{
		name = ModuleToggleCrossfeed
		crossfeedStatus = false
	}
}
"""


@pytest.fixture(scope="module")
def defs():
    return resource_defs_from(ConfigNode.parse(RESOURCES).nodes("RESOURCE_DEFINITION"))


@pytest.fixture(scope="module")
def fixture_catalog():
    return Catalog.from_json(json.loads((FIXTURES / "parts_subset.json").read_text(encoding="utf-8")))


def test_cfg_node_offsets_use_scale_times_default_rescale(defs):
    p = part_from_cfg(ConfigNode.parse(LEGACY_ENGINE).node("PART"), defs)
    assert p.name == "liquidEngine2"
    top, bottom = p.node("top"), p.node("bottom")
    assert top.pos[1] == pytest.approx(7.21461 * 0.1 * 1.25)  # 0.9018263, as KSP writes in Kerbal 1-5
    assert bottom.pos[1] == pytest.approx(-5.74338 * 0.125)
    assert top.size == 1 and bottom.size == 2 and bottom.diameter_m == 2.5
    assert top.dir == [0.0, 1.0, 0.0]
    assert p.title == p.title_en == 'LV-T45 "Swivel" Liquid Fuel Engine'
    assert p.attach_rules["stack"] and not p.attach_rules["srfAttach"] and not p.attach_rules["allowSrfAttach"]
    assert p.role == "liquid_engine" and p.stageable
    (e,) = p.engines
    assert e.thrust_vac_kn == 215 and e.gimbal_deg == 3
    assert e.isp_vac == 320 and e.isp_asl == 250
    assert e.thrust_kn(1.0) == pytest.approx(215 * 250 / 320)
    assert e.mass_flow_tps == pytest.approx(215 / (320 * 9.80665))
    assert [(x["name"], x["ratio"]) for x in e.propellants] == [("LiquidFuel", 0.9), ("Oxidizer", 1.1)]


def test_cfg_radial_decoupler(defs):
    p = part_from_cfg(ConfigNode.parse(RADIAL_DECOUPLER).node("PART"), defs)
    assert p.role == "radial_decoupler"
    assert p.decoupler == {"ejection_force": 260.0, "is_omni": False, "explosive_node": "srf", "radial": True}
    assert p.crossfeed is False and p.crossfeed_toggle is True
    assert p.srf_node.pos == [-0.03, 0.0, 0.0] and p.srf_node.dir == [1.0, 0.0, 0.0]
    assert p.surface_attachable and p.stageable
    assert p.nodes == [] and p.diameters_m == []


def test_float_curve_matches_unity_auto_tangents():
    f = FloatCurve([[0, 320], [1, 250], [6, 0.001]])
    assert f(0) == 320 and f(1) == 250 and f(6) == pytest.approx(0.001)
    assert f(-1) == 320 and f(9) == pytest.approx(0.001)  # clamped
    # first key flat (added alone), second key slope -70 (added last): Hermite midpoint
    assert f(0.5) == pytest.approx(0.5 * 320 + 0.5 * 250 + 0.125 * 70)
    explicit = FloatCurve([[0, 1, 0, 0], [1, 2, 0, 0]])
    assert explicit(0.25) == pytest.approx(1 + 3 * 0.0625 - 2 * 0.015625)


def test_engine_mode_thrust_scales_with_isp():
    e = EngineMode("", "ModuleEngines", "LiquidFuel", 60.0, 0.0, [[0, 800], [1, 185]], [{"name": "LiquidFuel", "ratio": 0.9}])
    assert e.thrust_kn(0.0) == 60.0
    assert e.thrust_kn(1.0) == pytest.approx(60 * 185 / 800)
    assert not e.air_breathing


def test_helpers():
    assert live_name("liquidEngine3_v2") == "liquidEngine3.v2"
    assert bulkhead_diameter("size1p5") == 1.875 and bulkhead_diameter("size0") == 0.625
    assert bulkhead_diameter("size3") == 3.75 and bulkhead_diameter("srf") is None
    v = ConfigNode.parse("title = #autoLOC_1 //#autoLOC_1 = Mk16 Parachute\n").get_value("title")
    assert english_title(v) == "Mk16 Parachute"
    assert english_title("Plain Title") == "Plain Title"


@pytest.mark.parametrize("modules,resources,crew,engine_props,role", [
    (["ModuleCommand"], {}, 1, None, "command_pod"),
    (["ModuleCommand", "ModuleReactionWheel"], {}, 0, None, "probe_core"),
    (["ModuleEngines"], {}, 0, ["SolidFuel"], "solid_booster"),
    (["ModuleEngines"], {}, 0, ["LiquidFuel"], "liquid_engine"),
    (["ModuleEnginesFX"], {}, 0, ["XenonGas", "ElectricCharge"], "ion_engine"),
    (["ModuleEnginesFX"], {}, 0, ["IntakeAir", "LiquidFuel"], "jet_engine"),
    (["ModuleParachute"], {}, 0, None, "parachute"),
    (["ModuleProceduralFairing"], {}, 0, None, "fairing"),
    ([], {"LiquidFuel": 1, "Oxidizer": 1}, 0, None, "fuel_tank"),
    ([], {"MonoPropellant": 1}, 0, None, "monoprop_tank"),
    ([], {"ElectricCharge": 1}, 0, None, "battery"),
    (["CModuleFuelLine"], {}, 0, None, "fuel_line"),
])
def test_classify(modules, resources, crew, engine_props, role):
    p = PartInfo("x", modules=modules, crew=crew,
                 resources={k: {"amount": v, "max": v, "density_t": 0.0} for k, v in resources.items()})
    if engine_props:
        p.engines = [EngineMode("", "ModuleEngines", "", 1.0, 0.0, [[0, 100]], [{"name": n, "ratio": 1.0} for n in engine_props])]
    assert classify(p) == role


def test_basic_live_overlay_is_authoritative(defs):
    p = part_from_cfg(ConfigNode.parse(LEGACY_ENGINE).node("PART"), defs)
    overlay_basic_live(p, {"name": "liquidEngine2", "title": "LV-T45（本地化）", "category": "none", "crewCapacity": 0,
                           "dryMassT": 1.25, "maxThrustKn": 200, "ispVacS": 320, "ispAslS": 250, "resources": {}}, defs)
    assert p.loaded is True and p.source == "cfg+live"
    assert p.mass_t == 1.25 and p.cfg_mass_t == 1.5
    assert p.engines[0].thrust_vac_kn == 200  # matched by vacuum Isp
    assert p.title_en == 'LV-T45 "Swivel" Liquid Fuel Engine' and p.title.startswith("LV-T45")


def test_extended_bridge_contract(defs):
    entry = {
        "name": "liquidEngine3.v2", "title": "LV-909", "category": "Engine", "cost": 390, "mass_t": 0.5,
        "crewCapacity": 0, "bulkhead": "size1", "tags": "terrier",
        "bounds": {"size": [1.25, 1.2, 1.25], "center": [0, -0.4, 0]},
        "nodes": [{"id": "top", "pos": [0, 0, 0], "dir": [0, 1, 0], "size": 1},
                  {"id": "bottom", "pos": [0, -0.83, 0], "dir": [0, -1, 0], "size": 1}],
        "srfNode": None,
        "attachRules": {"stack": True, "srfAttach": False, "allowStack": True, "allowSrfAttach": False,
                        "allowCollision": False},
        "fuelCrossFeed": True, "stageable": True, "stagingIcon": "LIQUID_ENGINE",
        "resources": {},
        "engines": [{"id": "", "type": "LiquidFuel", "maxThrust_kn": 60, "minThrust_kn": 0, "isp_vac": 345,
                     "isp_asl": 85, "atmosphereCurve": [[0, 345], [1, 85], [3, 0.001]],
                     "propellants": [{"name": "LiquidFuel", "ratio": 0.9}, {"name": "Oxidizer", "ratio": 1.1}],
                     "throttleLocked": False, "gimbal_deg": 4}],
        "decoupler": None, "parachute": None, "command": None, "reactionWheel": None,
        "modules": ["ModuleJettison", "ModuleEnginesFX", "ModuleGimbal"], "maxTemp": 2000, "techRequired": "basicRocketry",
        "b9Tank": {"tankMass_t": 0.05, "addedMass_t": 0.01},
    }
    p = part_from_bridge_full(entry, defs)
    assert p.source == "bridge-full" and p.loaded
    assert p.node("bottom").pos == [0.0, -0.83, 0.0]
    assert p.bounds == {"size": [1.25, 1.2, 1.25], "center": [0.0, -0.4, 0.0]}
    assert p.role == "liquid_engine" and p.engines[0].isp_asl == 85 and p.engines[0].gimbal_deg == 4
    assert p.engines[0].module == "ModuleEnginesFX" and p.engines[0].engine_type == "LiquidFuel"
    assert p.mass_t == pytest.approx(0.56) and p.tech_required == "basicRocketry"
    assert p.srf_node is None and not p.surface_attachable
    entry["attachRules"] = {k: str(v).lower() for k, v in entry["attachRules"].items()}  # string-valued bridge
    assert part_from_bridge_full(entry, defs).attach_rules["stack"] is True


def test_drag_cube_bounds(tmp_path):
    (tmp_path / "PartDatabase.cfg").write_text(
        "PART\n{\n\turl = Squad/Parts/FuelTank/fuelTankT400/fuelTank\n\tDRAG_CUBE\n\t{\n"
        "\t\tcube = Default, 2.4,0.77,0.72, 2.4,0.77,0.72, 1.2,0.91,0.18, 1.2,0.91,0.18, 2.4,0.77,0.72, "
        "2.4,0.77,0.72, 0,0.0346,0, 1.25,1.949,1.25\n\t}\n}\n", encoding="utf-8")
    assert drag_cube_bounds(tmp_path / "PartDatabase.cfg") == {
        "fuelTank": {"center": [0.0, 0.0346, 0.0], "size": [1.25, 1.949, 1.25]}}


def test_search_and_lookup(fixture_catalog):
    cat = fixture_catalog
    assert cat.search("terrier")[0].name == "liquidEngine3.v2"
    assert cat.search("liquidEngine3.v2")[0].name == "liquidEngine3.v2"
    nerv = [p.name for p in cat.search(role="liquid_engine", propellant="LF")]
    assert nerv == ["nuclearEngine"]
    radial = {p.name for p in cat.search(role="radial_decoupler")}
    assert {"radialDecoupler", "radialDecoupler2"} <= radial
    big = cat.search(role="liquid_engine", min_thrust_kn=1000)
    assert big and all(p.engines[0].thrust_vac_kn >= 1000 for p in big)
    wide = cat.search(diameter_m=2.5, limit=100)
    assert wide and all(any(abs(d - 2.5) < 0.05 for d in p.diameters_m) for p in wide)
    assert "liquidEngine2" not in {p.name for p in cat.search("swivel")}  # retired (category none) is hidden
    assert "liquidEngine2" in {p.name for p in cat.search("swivel", include_hidden=True)}
    assert all(p.surface_attachable for p in cat.search(surface_attachable=True, limit=100))
    assert cat.get("liquidEngine3_v2").name == "liquidEngine3.v2"
    with pytest.raises(CatalogError) as err:
        cat.get("liquidEngine3.v3")
    assert "liquidEngine3.v2" in str(err.value)


class FakeBridge:
    def __init__(self, full=None, basic=None, up=True):
        self.full, self.basic, self._up = full, basic, up
        self.calls = []

    def up(self):
        return self._up

    def post(self, path, body=None, timeout=None):
        self.calls.append(("POST", path, body))
        if self.full is None:
            raise RuntimeError("Unknown route: POST /part-database")
        return {"count": len(self.full), "parts": self.full}

    def get(self, path, timeout=None):
        self.calls.append(("GET", path))
        return {"count": len(self.basic or []), "parts": self.basic or []}


def _mini_gamedata(root: Path) -> Path:
    gd = root / "GameData"
    (gd / "Squad" / "Resources").mkdir(parents=True)
    (gd / "Squad" / "Resources" / "res.cfg").write_text(RESOURCES, encoding="utf-8")
    (gd / "Squad" / "Parts").mkdir(parents=True)
    (gd / "Squad" / "Parts" / "engine.cfg").write_text(LEGACY_ENGINE, encoding="utf-8")
    (gd / "Squad" / "Parts" / "dec.cfg").write_text(RADIAL_DECOUPLER, encoding="utf-8")
    return gd


def test_build_cache_and_refresh_policy(tmp_path):
    gd = _mini_gamedata(tmp_path)
    cache = tmp_path / "cache" / "parts.json"
    cat = Catalog.load(gamedata=gd, cache_path=cache)
    assert cat.source == "cfg" and len(cat) == 2 and cache.exists()
    assert cat.get("liquidEngine2").title_en.startswith("LV-T45")
    # a bridge with basic data upgrades a cfg-only cache
    basic = [{"name": "liquidEngine2", "title": "T", "dryMassT": 1.4, "maxThrustKn": 215, "ispVacS": 320,
              "ispAslS": 250, "resources": {}}]
    live = Catalog.load(gamedata=gd, cache_path=cache, bridge=FakeBridge(basic=basic))
    assert live.source == "cfg+live" and live.get("liquidEngine2").mass_t == 1.4
    assert live.get("radialDecoupler2").loaded is False  # the game did not report it
    # the live cache is reused while GameData is unchanged, even if the bridge goes away
    again = Catalog.load(gamedata=gd, cache_path=cache, bridge=FakeBridge(up=False))
    assert again.source == "cfg+live"
    # editing a cfg changes the fingerprint and forces a rebuild
    (gd / "Squad" / "Parts" / "engine.cfg").write_text(LEGACY_ENGINE.replace("mass = 1.5", "mass = 1.6"), encoding="utf-8")
    rebuilt = Catalog.load(gamedata=gd, cache_path=cache)
    assert rebuilt.source == "cfg" and rebuilt.get("liquidEngine2").mass_t == 1.6


def test_build_prefers_extended_bridge_and_keeps_cfg_extras(tmp_path):
    gd = _mini_gamedata(tmp_path)
    full = [{"name": "liquidEngine2", "title": "本地化", "category": "none", "mass_t": 1.5, "nodes": [
        {"id": "top", "pos": [0, 0.9, 0], "dir": [0, 1, 0], "size": 1}], "engines": [], "modules": ["ModuleEngines"]}]
    bridge = FakeBridge(full=full)
    cat = Catalog.build(gd, bridge=bridge, craft_dirs=[])
    assert cat.source == "bridge-full" and list(cat.parts) == ["liquidEngine2"]
    p = cat.get("liquidEngine2")
    assert p.node("top").pos == [0.0, 0.9, 0.0]
    assert p.title_en.startswith("LV-T45") and p.cfg_mass_t == 1.5
    assert ("POST", "/part-database", {"detail": "full"}) in bridge.calls


@pytest.mark.skipif(not (KSP / "GameData" / "Squad").exists(), reason="KSP install not present")
def test_real_install_geometry_facts(tmp_path):
    cat = Catalog.build(KSP / "GameData", bridge=None)
    assert len(cat) > 400
    assert cat.get("fuelTank").node("top").pos[1] == pytest.approx(0.981725)
    assert cat.get("fuelTank").node("bottom").pos[1] == pytest.approx(-0.9125)
    assert cat.get("mk1pod.v2").node("bottom").pos[1] == pytest.approx(-0.4050379, abs=1e-6)
    assert cat.get("liquidEngine2.v2").node("bottom").pos[1] == pytest.approx(-1.63)
    assert cat.get("parachuteSingle").node("bottom").pos[1] == pytest.approx(-0.120649 * 0.125, abs=1e-6)
    assert cat.get("liquidEngine2").node("top").pos[1] == pytest.approx(0.9018263, abs=1e-6)
    assert cat.get("radialDecoupler2").outer_face_m == pytest.approx(0.6176, abs=0.002)
    assert [p["name"] for p in cat.get("nuclearEngine").engines[0].propellants] == ["LiquidFuel"]
    assert len(cat.get("RAPIER").engines) == 2
    dec = cat.get("Decoupler.1")
    assert dec.crossfeed is False and dec.crossfeed_toggle and dec.decoupler["explosive_node"] == "top"
    assert cat.get("HeatShield1").decoupler["is_omni"] and cat.get("HeatShield1").role == "heat_shield"
    assert cat.get("fuelTank").bounds["size"][0] == pytest.approx(1.25, abs=0.01)
    assert cat.get("fuelTank").title_en == "FL-T400 Fuel Tank"


def test_parse_attn_formats():
    from astra.craft.catalog import parse_attn

    assert parse_attn("bottom,Decoupler.1_4294606904_0|-0.4050379|0") == (
        "bottom", "Decoupler.1_4294606904", [0.0, -0.4050379, 0.0], None)
    node, pid, pos, direction = parse_attn("nodeTop,benjee10.orion.chute_4293989074_0|0.74|-0.0002_0|1|-0.0003_0|0.74|0_0|1|0")
    assert (node, pid, pos, direction) == ("nodeTop", "benjee10.orion.chute_4293989074", [0.0, 0.74, -0.0002], [0.0, 1.0, -0.0003])
    assert parse_attn("top2,Null_0_0|1.1|0_0|1|0_0|1.1|0_0|1|0")[1:3] == ("Null_0", [0.0, 1.1, 0.0])
    assert parse_attn("top,fuelTank_123") == ("top", "fuelTank_123", None, None)


def test_model_transform_nodes_are_learned_from_saved_craft(tmp_path, defs):
    from astra.craft.catalog import learn_from_craft
    from astra.craft.spec import check_spec, parse_spec

    cfg = ("PART\n{\n\tname = mod_tank\n\tattachRules = 1,1,1,1,0\n\tmass = 1\n\tbulkheadProfiles = size2\n"
           "\tNODE\n\t{\n\t\tname = top\n\t\ttransform = topNode\n\t\tsize = 2\n\t}\n"
           "\tNODE\n\t{\n\t\tname = bottom\n\t\ttransform = bottomNode\n\t\tsize = 2\n\t}\n}\n")
    tank = part_from_cfg(ConfigNode.parse(cfg).node("PART"), defs)
    assert tank.name == "mod.tank" and [(n.id, n.known, n.size) for n in tank.nodes] == [("top", False, 2), ("bottom", False, 2)]
    cat = Catalog({tank.name: tank}, defs)
    spec = parse_spec({"name": "Mod", "parts": [{"id": "a", "part": "mod.tank"},
                                                {"id": "b", "part": "mod.tank", "parent": "a", "node": "bottom"}]})
    assert any("model transform" in i.problem for i in check_spec(spec, cat))
    ships = tmp_path / "VAB"
    ships.mkdir()
    (ships / "saved.craft").write_text(
        "ship = x\nPART\n{\n\tpart = mod.tank_4294000001\n"
        "\tattN = top,Null_0_0|2.25|0_0|1|0_0|2.25|0_0|1|0\n"
        "\tattN = bottom,mod.tank_4294000002_0|-2.25|0_0|-1|0_0|-2.25|0_0|-1|0\n}\n", encoding="utf-8")
    learn_from_craft(cat, [ships])
    assert tank.node("top").pos == [0.0, 2.25, 0.0] and tank.node("top").known
    assert tank.node("bottom").dir == [0.0, -1.0, 0.0]
    assert check_spec(spec, cat) == []


# --- review additions: bridge tangents, module names, robust curves -----------------------------


def test_float_curve_uses_explicit_tangents_steps_on_infinite_ones_and_survives_nan():
    f = FloatCurve([[0, 320, 0, -70], [1, 250, -30, 0]])  # [time, value, inTangent, outTangent]
    # Hermite midpoint: v0/2 + v1/2 + (m0 - m1) * dt / 8 with m0 = out of key 0, m1 = in of key 1
    assert f(0.5) == pytest.approx(0.5 * 320 + 0.5 * 250 + 0.125 * (-70) - 0.125 * (-30))
    import math
    for bad in (None, math.inf, -math.inf):  # the bridge writes non-finite tangents as JSON null
        assert FloatCurve([[0, 320, 0, bad], [1, 250, 0, 0]])(0.5) == 320  # Unity: stepped segment
        assert FloatCurve([[0, 320, 0, 0], [1, 250, bad, 0]])(0.999) == 320
    assert math.isnan(FloatCurve([[0, 320], [1, 250]])(math.nan))  # used to raise IndexError


def test_curve_keys_normalizes_bridge_and_string_keys():
    from astra.craft.catalog import curve_keys
    import math
    keys = curve_keys([[0, "345", 0, -250.5], "1 85 -200 -200", [3, 0.001, None, 0], ["x", 1], [math.nan, 2], 7])
    assert keys[:2] == [[0.0, 345.0, 0.0, -250.5], [1.0, 85.0, -200.0, -200.0]]
    assert keys[2][:2] == [3.0, 0.001] and keys[2][2] == math.inf and len(keys) == 3
    assert curve_keys(None) == [] and curve_keys("0 1") == []


def test_extended_bridge_engine_module_and_tangents_are_used(defs):
    entry = {
        "name": "twinModeEngine", "mass_t": 1.0, "nodes": [], "attachRules": {"stack": True},
        # an extra ModuleEngines before the one that thrusts: order-based guessing picks the wrong class
        "modules": ["ModuleEngines", "ModuleEnginesFX"],
        "engines": [{"id": "Main", "module": "ModuleEnginesFX", "type": "LiquidFuel", "maxThrust_kn": 100,
                     "atmosphereCurve": [[0, 300, 0, -40], [1, 260, -40, 0]],
                     "propellants": [{"name": "LiquidFuel", "ratio": 0.9}, {"name": "Oxidizer", "ratio": 1.1}]}],
    }
    p = part_from_bridge_full(entry, defs)
    e = p.engines[0]
    assert e.module == "ModuleEnginesFX" and e.id == "Main"
    assert e.isp_curve == [[0.0, 300.0, 0.0, -40.0], [1.0, 260.0, -40.0, 0.0]]
    assert e.isp(0.5) == pytest.approx(280.0)  # linear here: both tangents equal the chord slope
    del entry["engines"][0]["module"]  # an old bridge: fall back to the n-th engine module
    assert part_from_bridge_full(entry, defs).engines[0].module == "ModuleEngines"


TANGENT_ENGINE = LEGACY_ENGINE.replace("key = 0 320\n", "key = 0 320 0 -60\n").replace(
    "key = 1 250\n", "key = 1 250 -60 -60\n").replace("name = ModuleEngines\n", "name = ModuleEnginesFX\n")


def test_build_keeps_bridge_tangents_and_module_over_cfg_guesses(tmp_path):
    gd = _mini_gamedata(tmp_path)
    (gd / "Squad" / "Parts" / "engine.cfg").write_text(TANGENT_ENGINE, encoding="utf-8")
    live_curve = [[0, 320, 0, -75], [1, 250, -75, -50], [6, 0.001, 0, 0]]
    full = [{"name": "liquidEngine2", "mass_t": 1.5, "modules": ["ModuleEngines"],
             "nodes": [{"id": "top", "pos": [0, 0.9, 0], "dir": [0, 1, 0], "size": 1}],
             "engines": [{"id": "", "module": "ModuleEngines", "type": "LiquidFuel", "maxThrust_kn": 215,
                          "atmosphereCurve": live_curve,
                          "propellants": [{"name": "LiquidFuel", "ratio": 0.9}]}]}]
    e = Catalog.build(gd, bridge=FakeBridge(full=full), craft_dirs=[]).get("liquidEngine2").engines[0]
    assert e.isp_curve == [[float(c) for c in k] for k in live_curve]  # the loaded prefab curve, not the cfg's
    assert e.module == "ModuleEngines"  # named by the bridge, not the cfg's ModuleEnginesFX
    # an old bridge (no tangents, no module): the cfg supplies both, as before
    for k in full[0]["engines"][0]["atmosphereCurve"]:
        del k[2:]
    del full[0]["engines"][0]["module"]
    e = Catalog.build(gd, bridge=FakeBridge(full=full), craft_dirs=[]).get("liquidEngine2").engines[0]
    assert e.isp_curve[0] == [0.0, 320.0, 0.0, -60.0] and e.module == "ModuleEnginesFX"
