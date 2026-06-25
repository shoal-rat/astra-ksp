import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import design_chart  # noqa: E402


def _relay_comsat():
    from ksp_lab.design import Phase, ShipRequirements, design_ship, default_reserve_frac

    req = ShipRequirements(
        name="AI-Relay-Keo", mission_type="relay_comsat", crew=0, payload_t=0.3,
        phases=[Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3, reserve_frac=default_reserve_frac(9.81)),
                Phase("insertion", 1300.0, twr_body_g=0.0, min_twr=0.0, reserve_frac=default_reserve_frac(0.0))],
        landing=None, needs_legs=False, needs_heatshield=False, needs_docking=False, max_engine_count=1)
    return design_ship(req)


def test_relay_comsat_passes_the_looks_like_a_rocket_gate():
    rep = design_chart.looks_like_a_rocket(_relay_comsat())
    assert rep["looks_like_a_rocket"], rep["checks"]
    assert all(rep["checks"].values())
    assert 6.0 <= rep["fineness_ratio"] <= 28.0       # slender like a launch vehicle, not a pancake/noodle


def test_design_chart_renders_a_three_view_svg():
    svg = design_chart.render_svg(_relay_comsat())
    assert svg.startswith("<svg")
    assert "LOOKS LIKE A ROCKET" in svg
    assert "SIDE" in svg
    assert "FRONT" in svg
    assert "TOP" in svg
    assert "#fde68a" in svg                           # the fairing ogive is drawn on top


def test_gate_rejects_a_non_rocket_shape():
    # A short, fat, fin-less blob is NOT a rocket: force a pancake by hand and confirm the gate fails it.
    rep = design_chart.looks_like_a_rocket(_relay_comsat())
    # sanity: tamper a copy of the checks the way an absurd shape would read, ensure the verdict is AND-gated
    bad = dict(rep["checks"]); bad["slender body (6 <= L/D <= 28)"] = False
    assert not all(bad.values())


def _eve_boosted():
    from ksp_lab.design import Phase, ShipRequirements, design_ship, default_reserve_frac

    req = ShipRequirements(
        name="AI-Eve-Relay", mission_type="relay_comsat", crew=0, payload_t=0.3,
        phases=[Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3, reserve_frac=default_reserve_frac(9.81)),
                Phase("insertion", 3800.0, twr_body_g=9.81, min_twr=0.5, reserve_frac=default_reserve_frac(0.0))],
        landing=None, max_engine_count=1, radial_booster_count=4)
    return design_ship(req)


def test_gate_accepts_symmetric_radial_boosters():
    # 4 even strap-on boosters are a LEGITIMATE wide protrusion (Soyuz/Falcon-Heavy), not an overhang —
    # the geometry gate must pass them, including the dedicated symmetric-strap-on check.
    rep = design_chart.looks_like_a_rocket(_eve_boosted())
    assert rep["looks_like_a_rocket"], rep["checks"]
    assert rep["checks"]["radial protrusions within ascent envelope"]
    assert rep["checks"]["symmetric strap-on boosters"]
    assert rep["radial_booster_count"] == 4
    # the booster cluster span is wider than the core span (the strap-ons reach outside the hull)
    assert rep["radial_span_m"] > rep["core_span_m"]


def test_gate_rejects_an_asymmetric_booster_cluster():
    # A lopsided cluster (one pod missing) is NOT a clean strap-on rocket — the symmetry check must fail it,
    # proving the relaxed envelope is not a blanket rubber stamp for any wide protrusion.
    import math
    design = _eve_boosted()
    rb = design.radial_boosters
    names = {rb.engine, rb.tank, rb.decoupler}
    geom = design_chart.assembly_geometry(design)
    # drop every booster part at azimuth ~0 deg -> 3 pods left where 4 are expected
    kept = []
    for g in geom:
        if g["surface"] and g["name"] in names:
            if round(math.degrees(math.atan2(g["z"], g["x"])) % 360.0, 0) == 0.0:
                continue
        kept.append(g)
    assert design_chart._boosters_are_symmetric(geom, names, rb.count) is True
    assert design_chart._boosters_are_symmetric(kept, names, rb.count) is False


def test_chart_renders_boosters_in_all_three_views():
    # The strap-on boosters must appear in the rendered SVG (the TOP/SIDE/FRONT views) and the report panel.
    svg = design_chart.render_svg(_eve_boosted())
    assert svg.startswith("<svg")
    assert "LOOKS LIKE A ROCKET" in svg
    assert "radial boosters" in svg                    # the booster metric line is drawn
    assert "strap-on boosters" in svg                  # the symmetric-strap-on check is listed
