"""Plan the GILLY flag-plant excursion for the crewed Eve round-trip — OFFLINE Δv planner + optional lander.

WHY GILLY, NOT EVE: "plant a flag and bring them back" cannot be done on Eve's SURFACE — the ascent from
Eve sea level is ~8000 m/s, the infeasible-ascent wall, so a kerbal who lands on Eve is stranded. Eve's
moon GILLY is the feasible flag site: radius 13 km, surface g 0.049 m/s^2, surface escape ~36 m/s. A
kerbal lands and returns to orbit on essentially nothing.

WHAT IS ACTUALLY EXPENSIVE: the descent/ascent at Gilly are trivial (~30 m/s each), but Gilly orbits Eve
at 31,500 km — far above the ~100 km low-Eve parking orbit the ferry/tug sit in. The Hohmann OUT to
Gilly's altitude (~1270 m/s) and the re-circularize on the way BACK (~400 m/s) dominate. The round trip
is ~2500 m/s, NOT the ~300-500 m/s a naive "Gilly gravity is tiny" estimate suggests.

This module computes that budget from the SAME astro/bodies helpers the rest of the lab uses (vis-viva /
Hohmann / capture-from-excess — nothing guessed), checks it against the ferry's leftover Δv in Eve orbit,
and can emit a dedicated tiny Gilly lander three-view for the fallback architecture. It does NOT touch
kRPC or fly anything — pure planning.

    PYTHONPATH=src python tools/design_gilly_excursion.py            # print the budget + ferry margin
    PYTHONPATH=src python tools/design_gilly_excursion.py --lander   # also design + render the lander PNG
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ksp_lab import astro as A
from ksp_lab.bodies import EVE, GILLY
from ksp_lab.design import LandingSite, Phase, ShipRequirements, default_reserve_frac, design_ship

# Parking radii used by the planner. The ferry/tug live in a low Eve orbit; the excursion ship captures
# into a low Gilly orbit before the final hop to the surface.
EVE_PARK_ALT_M = 100_000.0          # low Eve orbit (well clear of the 90 km atmosphere top)
GILLY_PARK_ALT_M = 6_000.0          # low Gilly orbit before descent
DESCENT_LOSS_FACTOR = 1.30          # gravity/steering margin on the (tiny) Gilly descent + ascent burns


def gilly_excursion_budget(
    eve_park_alt_m: float = EVE_PARK_ALT_M,
    gilly_park_alt_m: float = GILLY_PARK_ALT_M,
) -> dict[str, float]:
    """Δv (m/s) for the round trip: low Eve orbit -> Gilly surface -> low Eve orbit.

    Every term is computed, none guessed:
      depart_to_gilly  : Hohmann burn at low Eve orbit raising apoapsis to Gilly's orbital radius.
      gilly_capture    : kill the arrival v_infinity into a low circular Gilly orbit.
      descent          : drop the low Gilly orbit to the surface (~circular speed) + a gravity margin.
      ascent           : surface back to low Gilly orbit (mirror of descent).
      gilly_escape     : leave Gilly's SOI back onto the Eve-centred transfer (mirror of capture).
      recirc_at_eve    : re-circularize at low Eve orbit on arrival back (the Hohmann arrive burn).
    """
    r_eve_park = EVE.radius_m + eve_park_alt_m
    r_gilly_orbit = GILLY.orbit_radius_m
    r_gilly_park = GILLY.radius_m + gilly_park_alt_m

    dv_depart, dv_recirc_eve, t_xfer = A.hohmann(EVE.mu, r_eve_park, r_gilly_orbit)
    v_inf = A.transfer_arrival_excess_speed(EVE.mu, r_eve_park, r_gilly_orbit)
    dv_capture = A.capture_from_excess(GILLY.mu, r_gilly_park, v_inf)
    dv_escape = A.capture_from_excess(GILLY.mu, r_gilly_park, v_inf)  # symmetric departure burn

    v_surf_orbit = A.circular_speed(GILLY.mu, GILLY.radius_m)
    dv_descent = v_surf_orbit * DESCENT_LOSS_FACTOR
    dv_ascent = v_surf_orbit * DESCENT_LOSS_FACTOR

    total = dv_depart + dv_capture + dv_descent + dv_ascent + dv_escape + dv_recirc_eve
    return {
        "depart_to_gilly": dv_depart,
        "gilly_capture": dv_capture,
        "descent": dv_descent,
        "ascent": dv_ascent,
        "gilly_escape": dv_escape,
        "recirc_at_eve": dv_recirc_eve,
        "transfer_time_h": t_xfer / 3600.0,
        "arrival_v_inf": v_inf,
        "surface_escape_mps": (2.0 * GILLY.mu / GILLY.radius_m) ** 0.5,
        "total": total,
    }


def gilly_lander() -> ShipRequirements:
    """FALLBACK only: a tiny dedicated Gilly lander (crew seat, legs, docking port, RCS) flown to Eve
    orbit folded behind a fairing, then detached to do the excursion while the ferry stays put.

    The recommended architecture does NOT need this — the ferry has the leftover Δv to do the excursion
    itself. This exists so the fallback has a PNG-verified shape. One small vacuum phase (~2600 m/s,
    covering the excursion budget + reserve); legs because it touches down; no heat shield (airless).

    NOTE ON THE GEOMETRY GATE: rendered bare, this lander trips the "radial protrusions within ascent
    envelope" check (the radial docking port / RCS sit outside the 1.6x-body core envelope). That gate
    models a KERBIN LAUNCH ascent — but a dedicated Gilly lander never launches bare; it rides folded
    inside a fairing on the launch stack and only deploys in Eve orbit, where max-Q / drag are zero. The
    fail is therefore a false positive for this craft's actual flight regime, and it is one more reason
    the ferry-itself excursion (no extra craft, no packaging problem) is the cleaner architecture."""
    return ShipRequirements(
        name="AI-Gilly-Lander", mission_type="crewed", crew=1, payload_t=0.1,
        phases=[
            Phase("vacuum", 2600.0, twr_body_g=GILLY.surface_g, min_twr=0.3,
                  reserve_frac=default_reserve_frac(0.0)),
        ],
        landing=LandingSite(GILLY.surface_g, 0.0), needs_legs=True,
        needs_heatshield=False, needs_docking=True, max_engine_count=1, radial_booster_count=0,
    )


def _print_budget() -> None:
    b = gilly_excursion_budget()
    print("=== GILLY FLAG-PLANT EXCURSION Δv (low Eve orbit -> Gilly surface -> low Eve orbit) ===")
    print(f"  depart to Gilly (Hohmann at low Eve orbit) : {b['depart_to_gilly']:7.1f} m/s")
    print(f"  Gilly capture (kill arrival v_inf)         : {b['gilly_capture']:7.1f} m/s")
    print(f"  descent to surface (+{DESCENT_LOSS_FACTOR:.2f} margin)         : {b['descent']:7.1f} m/s")
    print(f"  ascent to low Gilly orbit                  : {b['ascent']:7.1f} m/s")
    print(f"  Gilly escape back onto Eve transfer        : {b['gilly_escape']:7.1f} m/s")
    print(f"  recircularize at low Eve orbit             : {b['recirc_at_eve']:7.1f} m/s")
    print(f"  -------------------------------------------  -------")
    print(f"  TOTAL excursion                            : {b['total']:7.0f} m/s")
    print(f"  (transfer ~{b['transfer_time_h']:.1f} h each way; arrival v_inf {b['arrival_v_inf']:.0f} m/s; "
          f"Gilly surface escape {b['surface_escape_mps']:.0f} m/s)")
    print()
    # Ferry margin: the design budgets a 3800 m/s vacuum-insertion phase; reaching Eve orbit propulsively
    # (no heat shield) spends ~3700 m/s of it, leaving ~3100 m/s. Print the headroom against the excursion.
    ferry_leftover = 3164.0  # see tools/eve_two_ship_return + design_eve_two_ship vacuum budget analysis
    print(f"  ferry leftover in Eve orbit (propulsive capture): ~{ferry_leftover:.0f} m/s")
    print(f"  excursion need                                  : ~{b['total']:.0f} m/s")
    margin = ferry_leftover - b['total']
    verdict = "FEASIBLE — ferry can do the excursion itself" if margin > 0 else "INFEASIBLE on the ferry alone"
    print(f"  margin                                          : ~{margin:.0f} m/s  => {verdict}")


def _render_lander() -> None:
    import design_chart  # tools/ is on sys.path when run as a script
    from render_chart_png import render

    req = gilly_lander()
    d = design_ship(req)
    e = d.estimates
    print(f"\nLANDER {req.name}: wet {e['wet_mass_t']:.1f} t, launch TWR {e['launch_twr']:.2f}, "
          f"total Δv {e['total_delta_v_mps']:.0f} m/s, legs={d.landing_legs}, "
          f"dock={d.docking_port}, feasible={d.feasible}")
    docs = Path(__file__).resolve().parents[1] / "docs"
    svg_path = docs / f"design_chart_{req.name}.svg"
    svg_path.write_text(design_chart.render_svg(d), encoding="utf-8")
    png_path = render(str(svg_path))
    print(f"rendered three-view -> {png_path}")


def main(argv: list[str]) -> int:
    _print_budget()
    if "--lander" in argv:
        _render_lander()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
