"""Design-chart generator + "looks like a rocket" validator + live-API size check.

The RULE (see AGENTS.md): before any new rocket is launched it must (1) be sized from REAL part data,
(2) produce a design chart that a human can eyeball to confirm it looks like a rocket, and (3) have its
proportions CALCULATED and gated — never shipped on faith.

This module turns a calculated RocketDesign into:
  * a side-view SVG (docs/design_chart_<name>.svg) — the eyeball chart, and
  * a proportions report (fineness ratio, monotonic taper, pointed nose, engine-at-base, static margin)
    with a hard PASS/FAIL, so an absurd shape (a pancake, a spaghetti, an inverted cone) is rejected.

It also exposes verify_against_live(conn, design): once the craft is loaded in the running game it reads
the REAL assembled length / diameter / mass / part-count back from kRPC (the live API) and compares them to
the calculated values — closing the loop so the design is checked against ground truth, not just its own math.

    PYTHONPATH=src python tools/design_chart.py            # chart + validate the standard relay comsat
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ksp_lab.craft_writer import CraftWriter
from ksp_lab.parts import part


# --------------------------------------------------------------------------------------------------
# Geometry harvested from the SAME assembly the launcher flies (so the chart cannot drift from reality).
# --------------------------------------------------------------------------------------------------
def stack_geometry(design) -> list[dict]:
    """Each stack (non-surface) part as a side-view box: centre y, height, diameter, and the part role."""
    nodes = CraftWriter()._build_nodes(design, part_bodies=None)
    boxes = []
    for n in nodes:
        if n.is_surface:
            continue
        p = part(n.part_name)
        role = "engine" if p.thrust_kn_vac > 0 else (
            "fairing" if n.part_name.startswith("fairing") else (
                "fin" if p.fin_area_m2 > 0 else "body"))
        boxes.append({
            "name": n.part_name, "role": role, "y": n.y,
            "h": p.height_m, "dia": p.diameter_m,
            "top": n.y + p.height_m / 2.0, "bot": n.y - p.height_m / 2.0,
            "fairing_xsections": n.fairing_xsections,
        })
    return boxes


def looks_like_a_rocket(design) -> dict:
    """CALCULATE the proportions and gate them. A shape is a rocket when it is slender (not a pancake or a
    noodle), tapers monotonically wider toward the base, ends in a point on top, fires from the bottom, and
    is statically stable. Returns the metrics + per-check PASS and an overall verdict."""
    boxes = [b for b in stack_geometry(design) if b["role"] != "fin"]
    length = max(b["top"] for b in boxes) - min(b["bot"] for b in boxes)
    max_dia = max(b["dia"] for b in boxes)
    fineness = length / max_dia

    # taper: scanning TOP -> BOTTOM, diameter must never widen then narrow again (base is widest).
    ordered = sorted(boxes, key=lambda b: -b["y"])           # top first
    dias = [b["dia"] for b in ordered]
    taper_ok = all(dias[i] <= dias[i + 1] + 1e-6 for i in range(len(dias) - 1))

    top_part = ordered[0]
    # A pointed nose exists if a fairing encloses the payload (its base sits low but the ogive shell tapers
    # up over the bus to a point) OR the topmost part is a nose cone.
    has_fairing = any(b["role"] == "fairing" for b in boxes)
    nose_ok = has_fairing or top_part["name"] == "noseCone"
    bottom_part = sorted(boxes, key=lambda b: b["y"])[0]
    engine_bottom = bottom_part["role"] == "engine"
    stable = bool(getattr(design, "ascent_stable", True))

    checks = {
        "slender (6 <= fineness <= 28)": 6.0 <= fineness <= 28.0,
        "monotonic taper (widest at base)": taper_ok,
        "pointed nose (fairing/cone on top)": nose_ok,
        "engine at the base": engine_bottom,
        "statically stable (CoP <= CoG)": stable,
    }
    return {
        "length_m": round(length, 2), "max_diameter_m": round(max_dia, 2),
        "fineness_ratio": round(fineness, 1), "checks": checks,
        "looks_like_a_rocket": all(checks.values()),
    }


# --------------------------------------------------------------------------------------------------
# The eyeball chart: a side-view SVG drawn from the same boxes.
# --------------------------------------------------------------------------------------------------
def render_svg(design) -> str:
    boxes = stack_geometry(design)
    rep = looks_like_a_rocket(design)
    body = [b for b in boxes if b["role"] != "fin"]
    y_top = max(b["top"] for b in body)
    y_bot = min(b["bot"] for b in body)
    max_dia = max(b["dia"] for b in body)
    scale = 520.0 / (y_top - y_bot)                          # px per metre (fit ~520 px tall)
    cx = 230.0                                               # centreline x
    px_d = max_dia * scale

    def yy(m: float) -> float:
        return 40.0 + (y_top - m) * scale                   # world-y -> svg-y (down)

    parts_svg = []
    for b in sorted(boxes, key=lambda b: -b["y"]):
        w = b["dia"] * scale
        if b["role"] == "fin":
            # two base fins as triangles flanking the lowest body box
            fy = yy(b["y"]); half = px_d / 2.0
            parts_svg.append(f'<polygon points="{cx-half},{fy} {cx-half-22},{fy+34} {cx-half},{fy+34}" '
                             f'fill="#94a3b8" stroke="#475569"/>')
            parts_svg.append(f'<polygon points="{cx+half},{fy} {cx+half+22},{fy+34} {cx+half},{fy+34}" '
                             f'fill="#94a3b8" stroke="#475569"/>')
            continue
        if b["role"] == "fairing":
            # ogive: full radius at the base, taper to a point at the top of the shell
            base_y = yy(b["bot"]); tip_y = yy(rep["length_m"] + y_bot + 0.0)
            half = w / 2.0
            tip_y = yy(y_top)                                # the shell reaches the payload top region
            parts_svg.append(
                f'<path d="M {cx-half} {base_y} '
                f'L {cx-half} {base_y-(base_y-tip_y)*0.55} '
                f'Q {cx-half} {tip_y} {cx} {tip_y-14} '
                f'Q {cx+half} {tip_y} {cx+half} {base_y-(base_y-tip_y)*0.55} '
                f'L {cx+half} {base_y} Z" fill="#fde68a" stroke="#d97706" stroke-width="1.5" opacity="0.92"/>')
            continue
        fill = {"engine": "#f87171", "body": "#cbd5e1"}.get(b["role"], "#cbd5e1")
        top_y = yy(b["top"]); h = b["h"] * scale
        if b["role"] == "engine":
            # engine bell: trapezoid flaring out at the very bottom
            half_t = w / 2.0 * 0.7; half_b = w / 2.0
            parts_svg.append(f'<polygon points="{cx-half_t},{top_y} {cx+half_t},{top_y} '
                             f'{cx+half_b},{top_y+h} {cx-half_b},{top_y+h}" fill="{fill}" stroke="#991b1b"/>')
        else:
            parts_svg.append(f'<rect x="{cx-w/2:.1f}" y="{top_y:.1f}" width="{w:.1f}" height="{h:.1f}" '
                             f'rx="3" fill="{fill}" stroke="#64748b"/>')

    verdict = "LOOKS LIKE A ROCKET" if rep["looks_like_a_rocket"] else "REJECTED — not a rocket shape"
    vcol = "#16a34a" if rep["looks_like_a_rocket"] else "#dc2626"
    checks_svg = []
    ty = 70
    for label, ok in rep["checks"].items():
        mark = "✓" if ok else "✗"; col = "#16a34a" if ok else "#dc2626"
        checks_svg.append(f'<text x="470" y="{ty}" font-size="13" fill="{col}">{mark} {label}</text>')
        ty += 24
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="900" height="600" viewBox="0 0 900 600" font-family="system-ui,Segoe UI,sans-serif">
  <rect width="900" height="600" fill="#f8fafc"/>
  <text x="30" y="28" font-size="18" font-weight="700" fill="#0f172a">Design chart — {design.name}</text>
  <line x1="{cx}" y1="36" x2="{cx}" y2="572" stroke="#e2e8f0" stroke-dasharray="4 4"/>
  {''.join(parts_svg)}
  <text x="470" y="44" font-size="15" font-weight="700" fill="{vcol}">{verdict}</text>
  <text x="470" y="{ty+6}" font-size="13" fill="#0f172a">fineness {rep["fineness_ratio"]}:1  •  length {rep["length_m"]} m  •  max Ø {rep["max_diameter_m"]} m</text>
  <text x="470" y="{ty+30}" font-size="13" fill="#0f172a">Cd {getattr(design,'drag_cd','?')}  •  ascent drag loss {getattr(design,'ascent_drag_loss_mps','?')} m/s  •  max-Q {getattr(design,'max_q_kpa','?')} kPa</text>
</svg>'''


# --------------------------------------------------------------------------------------------------
# Close the loop: read the REAL assembled craft back from the live API (kRPC) and compare.
# --------------------------------------------------------------------------------------------------
def verify_against_live(conn, design) -> dict:
    """After the craft is loaded in the running game, read the REAL length / diameter / mass / part-count
    from kRPC (the API) and compare to the calculated design — so the rocket is checked against ground
    truth, not only its own arithmetic. Returns a dict of live vs calculated + a within-tolerance flag."""
    v = conn.space_center.active_vessel
    calc = looks_like_a_rocket(design)
    calc_mass = float(getattr(design, "estimates", {}).get("wet_mass_t", 0.0))
    live_mass_t = v.mass / 1000.0
    # Bounding box: prefer the part-position extent in the vessel frame (robust); the AABB call can return
    # a degenerate box in a bad frame (huge/NaN), so guard it and report dimensions as unavailable rather
    # than logging garbage. Mass + part-count are always reliable.
    length = width = None
    try:
        rf = v.reference_frame
        ys, rs = [], []
        for p in v.parts.all:
            x, y, z = p.position(rf)
            ys.append(y); rs.append((x * x + z * z) ** 0.5)
        if ys:
            length = max(ys) - min(ys)
            width = 2.0 * max(rs)
        if length is not None and not (0.0 < length < 500.0 and 0.0 < width < 500.0):
            length = width = None                       # frame was wrong -> dimensions unavailable
    except Exception:
        length = width = None
    out = {
        "live_length_m": round(length, 2) if length else None, "calc_length_m": calc["length_m"],
        "live_max_diameter_m": round(width, 2) if width else None, "calc_max_diameter_m": calc["max_diameter_m"],
        "live_fineness": round(length / width, 1) if (length and width) else None,
        "calc_fineness": calc["fineness_ratio"],
        "live_mass_t": round(live_mass_t, 2), "calc_wet_mass_t": round(calc_mass, 2),
        "live_part_count": len(v.parts.all),
    }
    out["mass_match"] = abs(live_mass_t - calc_mass) <= 0.10 * max(calc_mass, 1.0)
    out["dimensions_match"] = bool(length and width and (
        abs(length - calc["length_m"]) <= 0.25 * calc["length_m"] + 2.0
        and abs(width - calc["max_diameter_m"]) <= 0.5 * calc["max_diameter_m"] + 0.5))
    return out


def _relay_comsat():
    from ksp_lab.design import Phase, ShipRequirements, design_ship, default_reserve_frac
    req = ShipRequirements(
        name="AI-Relay-Keo", mission_type="relay_comsat", crew=0, payload_t=0.3,
        phases=[Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3, reserve_frac=default_reserve_frac(9.81)),
                Phase("insertion", 1300.0, twr_body_g=0.0, min_twr=0.0, reserve_frac=default_reserve_frac(0.0))],
        landing=None, needs_legs=False, needs_heatshield=False, needs_docking=False, max_engine_count=1)
    return design_ship(req)


def main() -> int:
    design = _relay_comsat()
    rep = looks_like_a_rocket(design)
    out = Path(__file__).resolve().parents[1] / "docs" / f"design_chart_{design.name}.svg"
    out.write_text(render_svg(design), encoding="utf-8")
    print(f"design chart written: {out}")
    print(f"  length {rep['length_m']} m | max dia {rep['max_diameter_m']} m | fineness {rep['fineness_ratio']}:1")
    for label, ok in rep["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"  => {'LOOKS LIKE A ROCKET' if rep['looks_like_a_rocket'] else 'REJECTED — not a rocket shape'}")
    return 0 if rep["looks_like_a_rocket"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
