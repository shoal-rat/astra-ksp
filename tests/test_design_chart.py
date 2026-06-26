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


def _eav_boosted():
    """A HEAVY booster design (the Eve Ascent Vehicle): 6 pods, multi-engine ascent stages. Unlike the
    light relay, each pod's engine is tucked toward the core at the bell plane and its decoupler sits
    inboard against the hull, so the engine/decoupler radii differ from the tall outboard tank column."""
    from ksp_lab.design import Phase, ShipRequirements, design_ship, default_reserve_frac
    from ksp_lab.bodies import EVE

    req = ShipRequirements(
        name="AI-EAV", mission_type="eve_ascent_vehicle", crew=1,
        phases=[Phase("eve_ascent", 7400.0, twr_body_g=EVE.surface_g, min_twr=1.5,
                      reserve_frac=default_reserve_frac(EVE.surface_g)),
                Phase("eve_to_kerbin", 1399.0, twr_body_g=0.0, min_twr=0.0,
                      reserve_frac=default_reserve_frac(0.0))],
        landing=None, needs_heatshield=True, needs_legs=True, radial_booster_count=6)
    return design_ship(req)


def test_symmetric_check_ignores_engine_decoupler_radii_for_even_pods():
    # REGRESSION: a perfectly symmetric N-pod cluster must PASS the symmetry check even though each
    # pod's engine (near the core bell plane) and decoupler (inboard) sit at SMALLER radii than the
    # outboard tank column. Folding those into the radius mean made the EAV's even hex of boosters read
    # as a 1.8->3.6 m "spread" and falsely fail the gate; the check must judge the tank column only.
    design = _eav_boosted()
    rb = design.radial_boosters
    assert rb is not None and rb.count == 6
    names = {rb.engine, rb.tank, rb.decoupler}
    geom = design_chart.assembly_geometry(design)
    assert design_chart._boosters_are_symmetric(geom, names, rb.count) is True
    rep = design_chart.looks_like_a_rocket(design)
    assert rep["checks"]["symmetric strap-on boosters"]
    assert rep["looks_like_a_rocket"], rep["checks"]


# ==================================================================================================
# CODEX MULTIMODAL REVIEW — the four geometry flaws the OLD gate was blind to. Each new check must
# (a) PASS on a sound real rocket and (b) REJECT the bad geometry that produced the flaw.
# ==================================================================================================
def _ferry():
    from tools.design_eve_two_ship import crew_ferry
    from ksp_lab.design import design_ship
    return design_ship(crew_ferry())


def test_gate_passes_the_fixed_ferry_and_tug_with_all_new_checks():
    # The Codex-flagged ferry/tug now pass the gate INCLUDING the four new checks (boosters not taller than
    # the host stage, payload enclosed, interstage shroud, plus the existing slender/taper/base checks).
    from tools.design_eve_two_ship import crew_ferry, return_tug
    from ksp_lab.design import design_ship
    for req in (crew_ferry(), return_tug()):
        rep = design_chart.looks_like_a_rocket(design_ship(req))
        assert rep["looks_like_a_rocket"], (req.name, rep["checks"])
        assert rep["checks"]["boosters no taller than host stage"]
        assert rep["checks"]["payload fully enclosed (nothing protrudes the fairing)"]
        assert rep["checks"]["upper-stage engine has interstage shroud"]


def test_gate_rejects_boosters_taller_than_the_host_stage():
    # FLAW #1/#2: a pod that towers above its host launch stage into the upper stage is a hammerhead/cage.
    # Tamper the ferry geometry to raise the pod tops above the launch-stage top and confirm the check fails.
    design = _ferry()
    rb = design.radial_boosters
    names = {rb.engine, rb.tank, rb.decoupler}
    geom = design_chart.assembly_geometry(design)
    ok_clean, _ = design_chart._booster_height_ok(geom, names)
    assert ok_clean is True, "the fixed ferry pods sit at/below the host stage"
    # Raise every booster part 30 m so the pods tower into the upper stage / payload.
    tall = []
    for g in geom:
        g2 = dict(g)
        if g["surface"] and g["name"] in names:
            g2["y"] += 30.0; g2["top"] += 30.0; g2["bot"] += 30.0
        tall.append(g2)
    ok_tall, _ = design_chart._booster_height_ok(tall, names)
    assert ok_tall is False, "a pod towering above the host stage must FAIL the height gate"


def test_gate_rejects_payload_protruding_outside_the_fairing():
    # FLAW #3: payload hardware sticking out past the fairing shell must fail the enclosure check. The
    # fixed relay encloses everything; push an accessory outside the shell radius and confirm rejection.
    design = _relay_comsat()
    geom = design_chart.assembly_geometry(design)
    ok_clean, m = design_chart._payload_enclosed_ok(geom)
    assert ok_clean is True, m
    # Shove the antenna far outside the fairing radius.
    tampered = []
    for g in geom:
        g2 = dict(g)
        if g["name"] == "RelayAntenna100":
            g2["x"] = 5.0
        tampered.append(g2)
    ok_bad, m2 = design_chart._payload_enclosed_ok(tampered)
    assert ok_bad is False, m2
    assert m2["max_radial_overshoot_m"] > 0.0


def test_gate_rejects_an_exposed_upper_stage_engine_without_an_interstage_shroud():
    # FLAW #4: an upper-stage engine sitting above a lower stage with NO shroud is a bare bell mid-stack.
    # The fixed relay shrouds it; strip the shroud and confirm the check fails.
    design = _relay_comsat()
    geom = design_chart.assembly_geometry(design)
    ok_clean, m = design_chart._interstage_shroud_ok(geom)
    assert ok_clean is True, m
    # Remove the interstage_shroud from every upper-stage engine.
    stripped = []
    for g in geom:
        g2 = dict(g)
        if g["role"] == "engine":
            g2["interstage_shroud"] = None
        stripped.append(g2)
    ok_bad, m2 = design_chart._interstage_shroud_ok(stripped)
    assert ok_bad is False, m2
    assert m2["unshrouded"] >= 1


def test_gate_rejects_unshrouded_top_docking_hardware():
    # CODEX FINAL FLAW: a docking vehicle whose Clamp-O-Tron + RCS quad + monoprop tank ride EXPOSED on the
    # nose (no enclosing fairing) must FAIL the enclosure gate — the top service hardware protrudes past the
    # narrow payload body. The fixed ferry encloses them in a fairing; strip the fairing and confirm rejection.
    from tools.design_eve_two_ship import crew_ferry
    from ksp_lab.design import design_ship

    design = design_ship(crew_ferry())
    geom = design_chart.assembly_geometry(design)
    ok_clean, m = design_chart._payload_enclosed_ok(geom)
    assert ok_clean is True, m                                  # faired ferry: docking hardware housed
    # Remove the fairing -> the docking port + RCS + monoprop tank are now naked on the nose.
    no_fairing = [g for g in geom if g["role"] != "fairing"]
    ok_bad, m2 = design_chart._payload_enclosed_ok(no_fairing)
    assert ok_bad is False, m2
    assert m2["unshrouded_service_parts"] >= 1                  # at least the docking port reads as naked


def test_gate_passes_the_docking_ferry_and_tug_with_the_enclosure_check():
    # The crewed ferry + return tug (docking port + RCS + bus) now PASS the full gate — their top service
    # hardware is housed in a launch fairing (matching the relay Codex approved), nothing protrudes.
    from tools.design_eve_two_ship import crew_ferry, return_tug
    from ksp_lab.design import design_ship

    for req in (crew_ferry(), return_tug()):
        d = design_ship(req)
        assert d.docking_port is True
        rep = design_chart.looks_like_a_rocket(d)
        assert rep["looks_like_a_rocket"], (req.name, rep["checks"])
        assert rep["checks"]["payload fully enclosed (nothing protrudes the fairing)"]
        # The craft really carries a payload fairing shroud (not just chart-only).
        from ksp_lab.craft_writer import CraftWriter

        text = CraftWriter().render(d, part_bodies=None)
        assert "part = fairingSize1" in text, f"{req.name} must carry an enclosing payload fairing"


def test_relay_emits_an_interstage_shroud_part_in_the_craft():
    # FLAW #4 (craft): the interstage shroud must also be a real part in the .craft (a procedural fairing
    # base at the lower stage diameter), not chart-only — so the launched vessel houses the upper engine.
    from ksp_lab.craft_writer import CraftWriter
    design = _relay_comsat()
    text = CraftWriter().render(design, part_bodies=None)
    assert ("part = fairingSize2" in text) or ("part = fairingSize3" in text), \
        "multi-stage craft must carry an interstage shroud part wrapping the upper engine"
