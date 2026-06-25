"""Validate the comprehensive, game-data-materialized stock parts catalog and the PNG-verified design
entry point.

Two things are under test here:
  1. PART 1 — the materialized catalog (src/ksp_lab/data/stock_parts.json) loads, covers far more of the
     stock roster than the old hand-list (>20 liquid engines, >10 LFO tanks, solid boosters, pods, ...),
     classifies parts into the role buckets the sizer queries, and the back-compat ``part()`` API still
     resolves the curated, hand-validated names with their authoritative masses untouched. The design
     sizer's engine/tank pools are now built FROM this catalog.
  2. PART 2 — ``design_and_verify`` is the single entry point that renders a three-view PNG and runs the
     geometry gate, returning a structured pass/fail report (the PNG-appearance constraint).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from ksp_lab import parts  # noqa: E402


# --------------------------------------------------------------------------------------------------
# PART 1 — the materialized catalog.
# --------------------------------------------------------------------------------------------------
def test_materialized_catalog_json_exists_and_loads():
    """The catalog is a COMMITTED artefact so the lab runs offline (no live KSP folder at import)."""
    assert parts.CATALOG_JSON.exists(), f"missing materialized catalog at {parts.CATALOG_JSON}"
    data = json.loads(parts.CATALOG_JSON.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and len(data) > 200, len(data)
    loaded = parts.load_catalog()
    assert len(loaded) == len(data)
    # Every loaded entry is a well-formed StockPart with the metadata the sizer queries on.
    sample = next(iter(loaded.values()))
    assert isinstance(sample, parts.StockPart)
    assert hasattr(sample, "part_type") and hasattr(sample, "stack_size")


def test_catalog_is_comprehensive_far_beyond_the_old_handlist():
    """The whole catalog (materialized + curated) must dwarf the old ~50-part hand-list and carry deep
    rocket-relevant coverage: many liquid engines, many LFO tanks, several solid boosters, pods, etc."""
    assert len(parts.STOCK_PARTS) > 200, len(parts.STOCK_PARTS)
    summary = parts.catalog_summary()
    assert summary.get("liquid_engine", 0) >= 20, summary
    assert summary.get("fuel_tank", 0) >= 10, summary
    assert summary.get("solid_booster", 0) >= 5, summary
    assert summary.get("pod", 0) >= 5, summary
    assert summary.get("decoupler", 0) >= 5, summary
    assert summary.get("nose_cone", 0) >= 3, summary
    assert summary.get("parachute", 0) >= 1, summary


def test_air_breathing_jets_are_not_in_the_rocket_engine_pool():
    """Jet engines (IntakeAir, ~10000 s 'Isp') must NOT be classified as rocket liquid_engines, or the
    sizer would put a Wheesley on a rocket. They are tagged jet_engine and kept out of the pool."""
    for p in parts.parts_of_type("liquid_engine"):
        # No chemical rocket engine has a vacuum Isp anywhere near a jet's; Nerv (800 s) is the max.
        assert p.isp_vac_s <= 900.0, (p.name, p.isp_vac_s)
    # The catalog still KNOWS about jets — they are just a separate bucket.
    jets = parts.parts_of_type("jet_engine")
    assert len(jets) >= 3, [p.name for p in jets]


def test_engines_query_filters_and_ranks_by_diameter():
    """parts.engines(diameter, atmospheric) returns the catalog engines in a diameter class, ranked —
    the query the design sizer uses to choose from ALL stock engines, not a hand-list."""
    booster_25 = parts.engines(diameter_m=2.5, atmospheric=True)
    assert len(booster_25) >= 5, [p.name for p in booster_25]
    assert all(p.diameter_m <= 2.5 + 1e-6 for p in booster_25)
    # Atmospheric ranking is ascending sea-level thrust (so the pool can be searched light -> heavy).
    asl = [p.thrust_kn_asl for p in booster_25]
    assert asl == sorted(asl)
    # The big stock heavy-lifters are reachable from the catalog (they were NOT in the old hand-list).
    all_names = {p.name for p in parts.engines(atmospheric=False)}
    assert "Size3EngineCluster" in all_names  # the Mammoth
    assert any("Size2LFB" in n for n in all_names)  # the Twin-Boar


def test_tanks_query_returns_clean_standard_stack_cylinders():
    """parts.tanks(diameter) returns straight LFO stack cylinders (not mk2/mk3 fuselages or slant
    adapters), largest propellant first — the tank pool the sizer stacks per stage."""
    t25 = parts.tanks(diameter_m=2.5)
    assert len(t25) >= 3, [p.name for p in t25]
    assert all(abs(p.diameter_m - 2.5) < 1e-6 for p in t25)
    assert all(p.liquid_fuel > 0 and p.oxidizer > 0 for p in t25)
    # No adapter / spaceplane fuselage leaked into the primary tank pool.
    assert all("adapter" not in p.name.lower() and "mk3" not in p.name.lower() for p in t25), \
        [p.name for p in t25]
    # Largest-first ordering.
    props = [p.propellant_mass_t for p in t25]
    assert props == sorted(props, reverse=True)


def test_back_compat_part_api_resolves_curated_names_with_validated_masses():
    """The curated, hand-validated parts are still resolvable through part() and their authoritative
    masses are UNCHANGED — the materialized catalog augments, never overwrites them."""
    swivel = parts.part("liquidEngine2")          # LV-T45 Swivel
    assert swivel.title == "LV-T45 Swivel"
    assert swivel.dry_mass_t == 1.5 and swivel.thrust_kn_vac == 215.0 and swivel.isp_vac_s == 320.0
    flt400 = parts.part("fuelTank")               # FL-T400
    assert flt400.dry_mass_t == 0.25 and flt400.wet_mass_t == 2.25
    # The other names the rest of the codebase imports through part() still resolve.
    for name in ("mk1pod.v2", "probeCoreOcto.v2", "parachuteSingle", "Decoupler.1",
                 "engineLargeSkipper", "Rockomax32.BW", "radialDecoupler2", "fairingSize2"):
        assert parts.part(name).name == name


def test_unknown_part_still_raises_keyerror():
    try:
        parts.part("definitely-not-a-real-part")
    except KeyError:
        return
    raise AssertionError("part() must raise KeyError for an unknown name")


def test_stage_masses_back_compat_unchanged():
    """The stage_masses() helper (imported by design.py / craft_writer) still returns the 5-tuple."""
    from ksp_lab.models import StageSpec
    s = StageSpec("test", "liquidEngine2", "fuelTank", 2, decoupler_above=True, engine_count=1)
    dry, wet, thrust_asl, isp_asl, isp_vac = parts.stage_masses(s)
    assert wet > dry > 0 and thrust_asl > 0 and isp_vac == 320.0


def test_parser_extracts_correct_engine_physics_from_cfg():
    """Spot-check the cfg PARSER on the inline LV-T45 PART text: thrust from maxThrust, Isp from the
    atmosphereCurve (key 0 = vacuum, key 1 = ASL), dry mass from `mass`, diameter from bulkheadProfiles."""
    cfg = """
PART
{
    name = testSwivel
    category = Engine
    title = #autoLOC_X //#autoLOC_X = Test Swivel Engine
    mass = 1.5
    cost = 1200
    bulkheadProfiles = size1
    node_stack_top = 0.0, 0.8, 0.0
    node_stack_bottom = 0.0, -0.8, 0.0
    MODULE
    {
        name = ModuleEngines
        maxThrust = 215
        EngineType = LiquidFuel
        PROPELLANT { name = LiquidFuel }
        PROPELLANT { name = Oxidizer }
        atmosphereCurve { key = 0 320 \n key = 1 250 }
    }
}
"""
    out = parts.parse_part_cfg(cfg)
    assert len(out) == 1
    p = out[0]
    assert p.name == "testSwivel" and p.title == "Test Swivel Engine"
    assert p.part_type == "liquid_engine"
    assert p.thrust_kn_vac == 215.0
    assert p.isp_vac_s == 320.0 and p.isp_asl_s == 250.0
    assert p.dry_mass_t == 1.5
    assert abs(p.diameter_m - 1.25) < 1e-6 and p.stack_size == "size1"


# --------------------------------------------------------------------------------------------------
# PART 1 — the design sizer now draws from the full catalog.
# --------------------------------------------------------------------------------------------------
def test_design_pools_are_built_from_the_catalog():
    """design.py's engine/tank pools are catalog-driven: the full-catalog tiers include stock parts the
    old five-engine / three-tank hand-lists never carried."""
    from ksp_lab import design as D
    assert len(D.BOOSTER_ENGINES_FULL) > len(D.BOOSTER_ENGINES)
    assert "Size3EngineCluster" in D.BOOSTER_ENGINES_FULL  # the Mammoth reached the booster pool
    # The full tank map carries more 2.5 m tanks than the curated list (the Jumbo-64 / X200-8 / v2s).
    assert len(D.TANKS_BY_DIAMETER_FULL[2.5]) > len(D.TANKS_BY_DIAMETER[2.5])


def test_full_catalog_lets_a_heavy_core_reach_a_bigger_stock_engine():
    """With use_full_catalog, a heavy single-engine core that no curated engine can lift reaches into the
    whole stock roster for a bigger engine (the Mammoth) — the catalog integration is real, not cosmetic.
    Default (curated-only) behaviour is unchanged."""
    from ksp_lab.design import Phase, ShipRequirements, design_ship
    req = ShipRequirements(
        name="heavy-core", crew=0, payload_t=30.0,
        phases=[Phase("booster", 3500.0, twr_body_g=9.81, min_twr=1.5),
                Phase("insertion", 1300.0)],
        landing=None, max_engine_count=1)
    curated = design_ship(req)                          # default: curated only
    full = design_ship(req, use_full_catalog=True)
    assert full.stages[0].engine != curated.stages[0].engine
    assert full.estimates["launch_twr"] > curated.estimates["launch_twr"]


# --------------------------------------------------------------------------------------------------
# PART 2 — the PNG-verified design entry point.
# --------------------------------------------------------------------------------------------------
def _relay_req():
    from ksp_lab.design import Phase, ShipRequirements, default_reserve_frac
    return ShipRequirements(
        name="AI-Catalog-Test", mission_type="relay_comsat", crew=0, payload_t=0.3,
        phases=[Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3, reserve_frac=default_reserve_frac(9.81)),
                Phase("insertion", 1300.0, twr_body_g=0.0, min_twr=0.0, reserve_frac=default_reserve_frac(0.0))],
        landing=None, needs_legs=False, needs_heatshield=False, needs_docking=False, max_engine_count=1)


def test_design_and_verify_returns_a_gate_result(tmp_path):
    """design_and_verify runs design_ship -> renders the three-view SVG -> (rasterizes to PNG) -> runs the
    geometry gate, returning (design, png_path, ok, report). The gate result must always be present even
    when no browser is installed; the SVG is always written; ok requires BOTH the gate AND a real PNG."""
    import design_chart  # noqa: E402

    design, png_path, ok, report = design_chart.design_and_verify(_relay_req(), out_dir=tmp_path)
    # The design was built and the gate ran.
    assert design is not None and design.name == "AI-Catalog-Test"
    assert "checks" in report and "looks_like_a_rocket" in report
    assert isinstance(report["failing_checks"], list)
    assert "png_rendered" in report
    # The three-view SVG is always emitted (the chart source the PNG is rendered from).
    svg = Path(report["svg_path"])
    assert svg.exists() and svg.read_text(encoding="utf-8").lstrip().startswith("<svg")
    # ok is True only when the gate passed AND the PNG actually rendered.
    assert ok == (report["looks_like_a_rocket"] and report["png_rendered"])
    if report["png_rendered"]:
        assert png_path and Path(png_path).exists() and Path(png_path).stat().st_size > 0
        # This relay launcher is a known-good shape, so when the PNG renders the gate passes (ok=True).
        assert ok is True
        assert report["failing_checks"] == []
