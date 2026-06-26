"""Validate the comprehensive, game-data-materialized stock parts catalog and the PNG-verified design
entry point.

Two things are under test here:
  1. PART 1 — the materialized catalog (src/ksp_lab/data/stock_parts.json) loads, covers far more of the
     stock roster than the old hand-list (>20 liquid engines, >10 LFO tanks, solid boosters, pods, ...),
     classifies parts into the role buckets the sizer queries, and the back-compat ``part()`` API still
     resolves the legacy names (through a pure alias rename) to their authoritative live-reconciled
     masses. There is NO curated value tier; the design sizer's engine/tank pools are built FROM this
     catalog (cfg-derived geometry + live physics), every part on equal footing.
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
    """The whole materialized catalog must dwarf the old ~50-part hand-list and carry deep rocket-relevant
    coverage: many liquid engines, many LFO tanks, several solid boosters, pods, etc. (no curated tier)."""
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


def test_back_compat_part_api_resolves_names_with_accurate_masses():
    """NO curated tier: every part now carries its real, verified cfg numbers (proven by
    tools/verify_parts.py). The headline engine/tank stats match known KSP values, and the dotted-name
    back-compat aliases the rest of the codebase imports through part() still resolve."""
    swivel = parts.part("liquidEngine2")          # LV-T45 Swivel (materialized cfg values)
    assert "Swivel" in swivel.title               # real cfg title, not a hand-typed short string
    assert swivel.dry_mass_t == 1.5 and swivel.thrust_kn_vac == 215.0 and swivel.isp_vac_s == 320.0
    flt400 = parts.part("fuelTank")               # FL-T400
    assert flt400.dry_mass_t == 0.25 and flt400.wet_mass_t == 2.25
    # Materialized headline parts that USED to be shadowed by curated literals now carry cfg values.
    assert parts.part("Size3LargeTank").diameter_m == 3.75 and parts.part("Size3LargeTank").liquid_fuel == 6480.0
    assert parts.part("Size3EngineCluster").thrust_kn_vac == 4000.0  # Mammoth, from the cfg
    # Names that ARE their own live key resolve to themselves through part().
    for name in ("mk1pod.v2", "probeCoreOcto.v2", "parachuteSingle", "Decoupler.1",
                 "Rockomax32.BW", "radialDecoupler2", "fairingSize2"):
        assert parts.part(name).name == name
    # ALIAS RENAMES are pure name redirects with NO data: a legacy import name resolves to the LIVE
    # entry, and its ``.name`` is the live loadable id (so a craft built from the alias loads in-game).
    # This is the only back-compat layer, and it overrides nothing.
    assert parts.ALIAS_RENAMES["engineLargeSkipper"] == "engineLargeSkipper.v2"
    skipper_alias = parts.part("engineLargeSkipper")
    skipper_live = parts.part("engineLargeSkipper.v2")
    assert skipper_alias.name == "engineLargeSkipper.v2"          # alias resolves to the live loadable id
    assert skipper_alias == skipper_live                          # SAME StockPart — no overriding data
    assert parts.part("RCSBlock").name == "RCSBlock.v2"
    assert parts.part("RCSBlock") == parts.part("RCSBlock.v2")


def test_catalog_keys_on_the_live_dotted_part_name_form():
    """KSP names a part with underscores in the cfg (``Rockomax16_BW``) but the running game's
    ``AvailablePart.name`` — and ``/part-database`` — report the dotted form (``Rockomax16.BW``). The
    catalog must key on the dotted live form so the same string matches the live db AND names a part the
    game can load. After reconciliation, no underscore-form duplicate of a ``.``-form part remains."""
    # live_part_name canonicalizes the cfg/persistence (underscore) form to the live (dotted) form.
    assert parts.live_part_name("Rockomax16_BW") == "Rockomax16.BW"
    assert parts.live_part_name("mk1pod_v2") == "mk1pod.v2"
    assert parts.live_part_name("liquidEngine") == "liquidEngine"   # idempotent, no separator
    # The materialized catalog keys on the live form: the dotted names resolve, the cfg underscore
    # duplicates do NOT (they were the false "missing" flags before the fix).
    catalog = parts.load_catalog()
    for live_name in ("Rockomax16.BW", "Rockomax32.BW", "liquidEngine3.v2", "mk1pod.v2"):
        assert live_name in catalog, live_name
        assert live_name.replace(".", "_") not in catalog, live_name


def test_rocket_relevant_parts_reconciled_to_live_game_masses():
    """The 14 live mismatches were fixed by taking the running game's post-load physics. The marquee one:
    the Mk1 command pod's true dry mass is 0.706 t (the game reduces crewed-pod mass), not the old curated
    0.84 t — PHYSICS from the live game, GEOMETRY derived from the cfg, with NO curated override left."""
    pod = parts.part("mk1pod.v2")
    assert pod.dry_mass_t == 0.706            # live AvailablePart.dryMassT, not the old curated 0.84
    # 1.25 m is the cfg-DERIVED diameter (the pod's bulkheadProfiles = size1), not a curated literal. The
    # taper gate that depends on it is now satisfied by the authoritative cfg geometry alone.
    assert pod.diameter_m == 1.25 and pod.stack_size == "size1"
    # The RAPIER is a multimode engine; the catalog keeps its ROCKET mode (the rocket-relevant numbers),
    # NOT the air-breathing jet primary mode the live /part-database headlines (105 kN / 3200 s).
    rapier = parts.part("RAPIER")
    assert rapier.thrust_kn_vac == 180.0 and rapier.isp_vac_s == 305.0 and rapier.isp_asl_s == 275.0


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
def test_design_pools_are_the_whole_catalog_no_curated_tier():
    """NO curated tier: design.py's engine/tank pools ARE the whole materialized catalog, every part on
    equal footing. The booster pool carries the big stock heavy-lifters the old five-engine hand-list
    never had, the tank map carries every stock LFO cylinder per diameter, and the legacy *_FULL aliases
    now point at the SAME single-tier pool (there is no separate 'full' tier any more)."""
    from ksp_lab import design as D
    # The pool reaches deep into the stock roster (Mammoth/Twin-Boar/Mainsail/Skipper all present).
    assert "Size3EngineCluster" in D.BOOSTER_ENGINES          # Mammoth
    assert any("Size2LFB" in n for n in D.BOOSTER_ENGINES)    # Twin-Boar
    assert len(D.BOOSTER_ENGINES) >= 10                       # a deep pool, not a 5-engine hand-list
    # The vacuum pool keeps the high-Isp engines a booster pool excludes (Terrier, Nerv, Poodle). The
    # catalog now keys on the live AvailablePart.name (dotted) form, so the Terrier is "liquidEngine3.v2".
    assert "nuclearEngine" in D.VACUUM_ENGINES and "liquidEngine3.v2" in D.VACUUM_ENGINES
    # Single tier: the *_FULL aliases are the SAME object as the base pool (back-compat, not a 2nd tier).
    assert D.BOOSTER_ENGINES_FULL is D.BOOSTER_ENGINES
    assert D.TANKS_BY_DIAMETER_FULL is D.TANKS_BY_DIAMETER
    # Every standard diameter has a real, multi-tank pool drawn from the catalog (incl. 3.75 m, which the
    # old hand-list-shadowed catalog returned EMPTY because the Kerbodyne tanks file under Propulsion).
    assert len(D.TANKS_BY_DIAMETER[1.25]) >= 3
    assert len(D.TANKS_BY_DIAMETER[2.5]) >= 3
    assert len(D.TANKS_BY_DIAMETER[3.75]) >= 3


def test_full_catalog_lets_a_heavy_core_reach_a_big_stock_engine():
    """No curation: a heavy single-engine core reaches straight into the whole stock roster for a big
    engine (Mammoth/Twin-Boar/Mastodon) and lifts — the catalog integration is real, not cosmetic.
    ``use_full_catalog`` is now a no-op (the full catalog is always used), so it changes nothing."""
    from ksp_lab.design import Phase, ShipRequirements, design_ship
    from ksp_lab.parts import part
    req = ShipRequirements(
        name="heavy-core", crew=0, payload_t=30.0,
        phases=[Phase("booster", 3500.0, twr_body_g=9.81, min_twr=1.4),
                Phase("insertion", 1300.0)],
        landing=None, max_engine_count=4)        # allow a small cluster so a big engine can close TWR
    d = design_ship(req)
    # The core engine is a genuine heavy-lift stock engine pulled from the full roster, sized by physics —
    # its sea-level thrust dwarfs a small launcher engine, the rocket lifts, and the design is feasible.
    booster = part(d.stages[0].engine)
    assert booster.thrust_kn_asl >= 600.0, d.stages[0].engine   # not a little Reliant/Swivel
    assert d.estimates["launch_twr"] >= 1.4
    assert d.feasible is True, d.infeasible_reasons
    # use_full_catalog is a no-op: same design either way.
    same = design_ship(req, use_full_catalog=True)
    assert same.stages[0].engine == d.stages[0].engine


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


# --------------------------------------------------------------------------------------------------
# REGRESSION: deleting the curated GEOMETRY must NOT break the renderer's monotonic-taper gate.
#
# When the catalog carries each part's TRUE cfg diameter (no curated 1.25 m draw override), the
# 0.625 m Probodobodyne OKTO is its real 0.625 m. Rooting an UNCREWED bus on it wedged a 0.625 m "neck"
# between the 1.25 m docking port / service bays above and below, inverting the taper gate (the tug
# regression). The fix roots an uncrewed craft on the genuine 1.25 m RC-001S (probeStackSmall), so the
# cfg-derived geometry reproduces a valid monotonic stack. These tests lock that in for all four canon
# designs (relay / crew ferry / return tug / Mun lander+return) so the gate can never silently regress.
# --------------------------------------------------------------------------------------------------
def _four_canon_reqs():
    from ksp_lab.design import LandingSite, Phase, ShipRequirements, default_reserve_frac
    KERBIN_G, MUN_G = 9.81, 1.63
    KERBIN_LAND = LandingSite(body_g=KERBIN_G, surface_rho=1.225, target_touchdown_mps=6.0)
    relay = ShipRequirements(
        name="relay-RA100", mission_type="relay_comsat", crew=0, payload_t=0.3,
        phases=[Phase("booster", 4200.0, twr_body_g=KERBIN_G, min_twr=1.3, reserve_frac=default_reserve_frac(KERBIN_G)),
                Phase("insertion", 1300.0, reserve_frac=default_reserve_frac(0.0))], max_engine_count=1)
    ferry = ShipRequirements(
        name="crew-ferry", mission_type="crew_ferry", crew=3, payload_t=0.0,
        phases=[Phase("booster", 3400.0, twr_body_g=KERBIN_G, min_twr=1.3, reserve_frac=default_reserve_frac(KERBIN_G)),
                Phase("circularize", 1000.0, reserve_frac=default_reserve_frac(0.0))],
        needs_heatshield=True, landing=KERBIN_LAND, max_engine_count=4)
    tug = ShipRequirements(
        name="return-tug", mission_type="return_tug", crew=0, payload_t=2.0,
        phases=[Phase("booster", 3400.0, twr_body_g=KERBIN_G, min_twr=1.3, reserve_frac=default_reserve_frac(KERBIN_G)),
                Phase("transfer", 1600.0, reserve_frac=default_reserve_frac(0.0))],
        needs_docking=True, max_engine_count=4)
    mun = ShipRequirements(
        name="mun-lander-return", mission_type="mun_lander", crew=2, payload_t=0.0,
        phases=[Phase("booster", 3400.0, twr_body_g=KERBIN_G, min_twr=1.4, reserve_frac=default_reserve_frac(KERBIN_G)),
                Phase("mun_transfer", 1200.0, reserve_frac=default_reserve_frac(0.0)),
                Phase("descent", 600.0, twr_body_g=MUN_G, min_twr=2.0, reserve_frac=default_reserve_frac(MUN_G, is_landing=True)),
                Phase("ascent_return", 1400.0, reserve_frac=default_reserve_frac(0.0))],
        needs_legs=True, needs_heatshield=True, landing=KERBIN_LAND, max_engine_count=4)
    return {"relay": relay, "ferry": ferry, "tug": tug, "mun": mun}


def test_four_canon_designs_pass_geometry_gate_on_cfg_derived_geometry():
    """relay / crew ferry / return tug / Mun lander+return all size feasibly AND pass the looks_like_a_rocket
    geometry gate using ONLY the cfg-derived part geometry (no curated diameters). The return tug is the
    regression sentinel: its uncrewed + docking bus is exactly the stack the 0.625 m OKTO root used to neck."""
    from ksp_lab.design import design_ship
    import design_chart  # noqa: E402
    for name, req in _four_canon_reqs().items():
        design = design_ship(req)
        assert design.feasible, (name, design.infeasible_reasons)
        report = design_chart.looks_like_a_rocket(design)
        assert report["looks_like_a_rocket"], (name, report["failing_checks"])
        assert report["checks"]["monotonic taper (widest toward base)"], (name, "taper inverted")


def test_uncrewed_bus_root_is_the_125m_probe_not_the_0625m_okto():
    """The uncrewed root command part is the 1.25 m RC-001S (probeStackSmall), NOT the 0.625 m OKTO — so
    the cfg-derived geometry keeps the bus a clean 1.25 m column. The hull above the upper stage must
    contain no 0.625 m 'neck' (every load-bearing body part >= 1.25 m)."""
    from ksp_lab.design import design_ship
    import design_chart  # noqa: E402
    tug = _four_canon_reqs()["tug"]
    design = design_ship(tug)
    geom = design_chart.assembly_geometry(design)
    hull = [g for g in geom if not g["surface"] and g["role"] not in ("engine", "fin", "leg")
            and g["name"] != "Decoupler.1"]
    assert all("probeCoreOcto" not in g["name"] for g in hull), [g["name"] for g in hull]
    assert any(g["name"] == "probeStackSmall" for g in hull), [g["name"] for g in hull]
    # No load-bearing hull part is the 0.625 m class — the bus is a clean 1.25 m+ column.
    necks = [g["name"] for g in hull if g["dia"] < 1.25 - 1e-6]
    assert not necks, necks
