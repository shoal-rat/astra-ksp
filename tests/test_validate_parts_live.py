"""Unit tests for tools/validate_parts_live.py — the LIVE catalog-vs-game cross-check logic.

These exercise the PURE compare functions with mocked /part-database + /parts-list payloads and a tiny
hand-built catalog. NO live bridge / kRPC calls are made (the live orchestration in ``run()`` is not
invoked here). We assert that a clean catalog reports all MATCH and zero MISMATCH, and that a seeded
drift (mass, thrust, Isp, capacity) is reported as a MISMATCH naming the field.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from ksp_lab.bridge_client import BridgeError  # noqa: E402
from ksp_lab.parts import StockPart  # noqa: E402
import validate_parts_live as V  # noqa: E402


def _engine(name="testEngine", dry=1.25, thr_vac=240.0, isp_vac=310.0, isp_asl=265.0):
    return StockPart(name=name, title="Test Engine", dry_mass_t=dry, wet_mass_t=dry, cost=1000.0,
                     thrust_kn_vac=thr_vac, isp_vac_s=isp_vac, isp_asl_s=isp_asl,
                     part_type="liquid_engine")


def _tank(name="testTank", dry=0.25, lf=180.0, ox=220.0):
    wet = dry + lf * 0.005 + ox * 0.005  # LF/Ox density 0.005 t/unit
    return StockPart(name=name, title="Test Tank", dry_mass_t=dry, wet_mass_t=wet, cost=500.0,
                     liquid_fuel=lf, oxidizer=ox, part_type="fuel_tank")


def _live_engine(name="testEngine", dry=1.25, thr_vac=240.0, isp_vac=310.0, isp_asl=265.0):
    return {"name": name, "title": "Test Engine", "category": "Engine", "bulkhead": "size1",
            "crewCapacity": 0, "dryMassT": dry, "maxThrustKn": thr_vac,
            "ispVacS": isp_vac, "ispAslS": isp_asl, "resources": {}}


def _live_tank(name="testTank", dry=0.25, lf=180.0, ox=220.0):
    return {"name": name, "title": "Test Tank", "category": "FuelTank", "bulkhead": "size1",
            "crewCapacity": 0, "dryMassT": dry,
            "resources": {"LiquidFuel": lf, "Oxidizer": ox}}


# --------------------------------------------------------------------------------------------------
# _close tolerance helper.
# --------------------------------------------------------------------------------------------------
def test_close_within_one_percent_matches():
    assert V._close(240.0, 240.0)
    assert V._close(240.0, 241.0)          # 0.42% < 1%
    assert V._close(0.0, 0.0)


def test_close_beyond_one_percent_mismatches():
    assert not V._close(240.0, 250.0)      # ~4% > 1%
    assert not V._close(1.25, 1.40)


# --------------------------------------------------------------------------------------------------
# PATH A — cross_check_part_database.
# --------------------------------------------------------------------------------------------------
def test_part_database_clean_match_reports_all_match_no_mismatch():
    catalog = {"testEngine": _engine(), "testTank": _tank()}
    live = [_live_engine(), _live_tank()]
    rep = V.cross_check_part_database(live, catalog)
    assert rep.mismatches == [], rep.mismatches
    # engine: dry + thrust + ispVac + ispAsl = 4 ; tank: dry + LF + Ox = 3
    assert rep.matches == 7, rep.matches


def test_part_database_seeded_mass_mismatch_is_reported():
    catalog = {"testEngine": _engine(dry=1.25)}
    live = [_live_engine(dry=1.55)]   # 24% heavier than catalog -> mismatch
    rep = V.cross_check_part_database(live, catalog)
    assert any("testEngine.dry_mass_t" in m for m in rep.mismatches), rep.mismatches
    assert len(rep.mismatches) == 1, rep.mismatches


def test_part_database_seeded_thrust_and_isp_mismatch_reported():
    catalog = {"testEngine": _engine(thr_vac=240.0, isp_vac=310.0)}
    live = [_live_engine(thr_vac=300.0, isp_vac=345.0)]   # both drifted
    rep = V.cross_check_part_database(live, catalog)
    assert any("thrust_kn_vac" in m for m in rep.mismatches), rep.mismatches
    assert any("isp_vac_s" in m for m in rep.mismatches), rep.mismatches


def test_part_database_seeded_capacity_mismatch_reported():
    catalog = {"testTank": _tank(lf=180.0, ox=220.0)}
    live = [_live_tank(lf=200.0, ox=220.0)]   # LF capacity drifted
    rep = V.cross_check_part_database(live, catalog)
    assert any("testTank.liquid_fuel" in m for m in rep.mismatches), rep.mismatches


def test_part_database_catalog_part_absent_from_live_is_skip_not_fail():
    catalog = {"missingEngine": _engine(name="missingEngine")}
    live = []   # game did not load it
    rep = V.cross_check_part_database(live, catalog)
    assert rep.mismatches == []
    assert any("missingEngine" in s and "NOT in live" in s for s in rep.skips), rep.skips


def test_part_database_ignores_non_rocket_relevant_parts():
    fin = StockPart(name="aFin", title="Fin", dry_mass_t=0.08, wet_mass_t=0.08, cost=10.0,
                    part_type="fin")
    catalog = {"aFin": fin}
    # Even with a wildly wrong live mass, a non-rocket-relevant type is never compared.
    live = [{"name": "aFin", "dryMassT": 9.99, "resources": {}}]
    rep = V.cross_check_part_database(live, catalog)
    assert rep.matches == 0 and rep.mismatches == []


def test_part_database_underscore_catalog_key_matches_dotted_live_name():
    """KSP's cfg/persistence name (Rockomax16_BW) and the live AvailablePart.name (Rockomax16.BW) differ
    only in the _/. separator. A legacy underscore-form catalog key must still reconcile to its dotted
    live twin via canonicalization, not be a false 'in catalog but NOT in live' miss."""
    catalog = {"Rockomax16_BW": _tank(name="Rockomax16_BW", dry=1.0, lf=720.0, ox=880.0)}
    live = [_live_tank(name="Rockomax16.BW", dry=1.0, lf=720.0, ox=880.0)]
    rep = V.cross_check_part_database(live, catalog)
    assert rep.mismatches == [], rep.mismatches
    assert not any("NOT in live" in s for s in rep.skips), rep.skips
    assert rep.matches == 3   # dry + LF + Ox all matched across the name-form boundary


def test_part_database_multimode_engine_skips_jet_mode_isp_compare():
    """A multimode engine (the RAPIER) headlines its air-breathing JET mode in /part-database: a low
    thrust and an "Isp" of ~3200 s that is a velocity curve, not a vacuum Isp. The catalog stores the
    ROCKET mode (the rocket-relevant numbers). The cross-check must SKIP the thrust/Isp compare for such
    a part (live Isp > 900 s while the catalog holds a normal rocket Isp) — never flag a MISMATCH —
    while still comparing the dry mass."""
    catalog = {"RAPIER": _engine(name="RAPIER", dry=2.0, thr_vac=180.0, isp_vac=305.0, isp_asl=275.0)}
    live = [_live_engine(name="RAPIER", dry=2.0, thr_vac=105.0, isp_vac=3200.0, isp_asl=3200.0)]
    rep = V.cross_check_part_database(live, catalog)
    assert rep.mismatches == [], rep.mismatches            # the mode disagreement is NOT a failure
    assert any("RAPIER" in s and "multimode" in s for s in rep.skips), rep.skips
    assert rep.matches == 1                                # only the dry mass was compared


def test_part_database_pure_jet_still_compares_and_matches():
    """A PURE jet (not multimode) reports the SAME high 'Isp' in both catalog and live, so it must still
    be compared and MATCH — the multimode skip must not swallow genuine single-mode air-breathers."""
    jet = _engine(name="JetEngine", dry=1.8, thr_vac=120.0, isp_vac=10500.0, isp_asl=10500.0)
    catalog = {"JetEngine": jet}
    live = [_live_engine(name="JetEngine", dry=1.8, thr_vac=120.0, isp_vac=10500.0, isp_asl=10500.0)]
    rep = V.cross_check_part_database(live, catalog)
    assert rep.mismatches == [], rep.mismatches
    assert not any("multimode" in s for s in rep.skips), rep.skips
    assert rep.matches == 4   # dry + thrust + ispVac + ispAsl all compared and matched


# --------------------------------------------------------------------------------------------------
# PATH B — cross_check_in_use_parts (fallback, no new endpoint needed).
# --------------------------------------------------------------------------------------------------
def test_in_use_parts_clean_match():
    catalog = {"testEngine": _engine(dry=1.25), "testTank": _tank(dry=0.25)}
    parts_list = [
        {"name": "testEngine", "dryMassT": 1.25, "resourceMassT": 0.0},
        {"name": "testTank", "dryMassT": 0.25, "resourceMassT": 1.0},   # partially fuelled
    ]
    rep = V.cross_check_in_use_parts(parts_list, catalog)
    assert rep.mismatches == [], rep.mismatches
    assert rep.matches >= 2


def test_in_use_parts_seeded_dry_mass_mismatch():
    catalog = {"testEngine": _engine(dry=1.25)}
    parts_list = [{"name": "testEngine", "dryMassT": 1.90, "resourceMassT": 0.0}]
    rep = V.cross_check_in_use_parts(parts_list, catalog)
    assert any("testEngine.dry_mass_t" in m for m in rep.mismatches), rep.mismatches


def test_in_use_parts_total_exceeding_catalog_wet_is_mismatch():
    tank = _tank(dry=0.25, lf=180.0, ox=220.0)   # wet ~2.25 t
    catalog = {"testTank": tank}
    # Live total mass far exceeds the catalog wet mass -> the catalog under-counts capacity.
    parts_list = [{"name": "testTank", "dryMassT": 0.25, "resourceMassT": 5.0}]
    rep = V.cross_check_in_use_parts(parts_list, catalog)
    assert any("testTank.wet_mass_t" in m and "EXCEEDS" in m for m in rep.mismatches), rep.mismatches


def test_in_use_part_unknown_to_catalog_is_skip():
    catalog = {}
    parts_list = [{"name": "modPart", "dryMassT": 1.0, "resourceMassT": 0.0}]
    rep = V.cross_check_in_use_parts(parts_list, catalog)
    assert rep.mismatches == []
    assert any("modPart" in s and "NOT in catalog" in s for s in rep.skips), rep.skips


# --------------------------------------------------------------------------------------------------
# 404 detection — so the tool can tell "old DLL" apart from "bridge down".
# --------------------------------------------------------------------------------------------------
def test_is_404_detects_endpoint_not_installed():
    err = BridgeError("KSP bridge request failed: GET .../part-database: HTTP 404: Unknown route")
    assert V._is_404(err)


def test_is_404_false_for_other_errors():
    assert not V._is_404(BridgeError("connection refused"))
    assert not V._is_404(BridgeError("HTTP 400: parts-list requires flight."))
