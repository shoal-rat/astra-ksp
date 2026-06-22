from ksp_lab.craft_writer import CraftWriter
from ksp_lab.duna import build_duna_comsat


def test_duna_comsat_is_flyable_with_interplanetary_margin():
    c = build_duna_comsat()
    e = c.estimates or {}
    # Must clear the pad (est launch TWR >= the project's ~1.4 pad-stuck threshold) and carry far more
    # than the LKO->Duna budget (~3400 ascent + ~1760 TDI+capture, much less with Duna aerobraking).
    assert e["launch_twr"] >= 1.38
    assert e["delta_v_mps"] >= 5200.0
    assert not c.crewed
    assert c.mission_type == "duna_comsat"


def test_duna_comsat_renders():
    txt = CraftWriter().render(build_duna_comsat(), part_bodies=None)
    assert txt.count("part = ") > 10
