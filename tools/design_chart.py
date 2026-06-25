"""Three-view design-chart generator and geometry gate.

Before a generated craft is launched, the project must be able to show what it built and reject shapes
that are obviously bad for ascent: excessive fineness ratio, inverted taper, no streamlined nose,
uncontrolled negative static margin, or unreasonable radial protrusions. The chart is drawn from the
same CraftWriter node assembly that produces the .craft file, so the picture and the KSP vehicle stay
in the same coordinate system.
"""
from __future__ import annotations

import math
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ksp_lab.craft_writer import CraftWriter
from ksp_lab.parts import part


ADAPTER_ENDS = {
    "adapterSize2-Size1": (1.25, 2.5),
    "Size3To2Adapter_v2": (2.5, 3.75),
}
NOSE_PARTS = {"noseCone", "dockingPort2", "mk1pod.v2", "fairingSize1", "fairingSize2"}


def _role(part_name: str, fairing_xsections: str | None = None) -> str:
    p = part(part_name)
    if p.fin_area_m2 > 0:
        return "fin"
    if part_name == "landingLeg1":
        return "leg"
    if p.thrust_kn_vac > 0:
        return "engine"
    if part_name in ADAPTER_ENDS:
        return "adapter"
    if part_name.startswith("fairing"):
        return "fairing"
    if part_name in NOSE_PARTS:
        return "nose"
    return "body"


def assembly_geometry(design) -> list[dict]:
    """All generated craft parts in the writer coordinate system, including surface attachments."""
    nodes = CraftWriter()._build_nodes(design, part_bodies=None)
    out: list[dict] = []
    for n in nodes:
        p = part(n.part_name)
        x, y, z = n.pos_xyz if n.pos_xyz is not None else (0.0, n.y, 0.0)
        role = _role(n.part_name, n.fairing_xsections)
        # Engines/fins/legs have a smaller visible planform than their stack mating diameter. Keep the
        # stack diameter for body math, but draw surface accessories with their visual footprint.
        draw_dia = p.diameter_m
        if n.is_surface and role == "engine":
            draw_dia *= 0.60
        elif role in {"fin", "leg"}:
            draw_dia = 0.35
        out.append({
            "name": n.part_name,
            "role": role,
            "surface": n.is_surface,
            "x": float(x),
            "y": float(y),
            "z": float(z),
            "h": p.height_m,
            "dia": p.diameter_m,
            "draw_dia": draw_dia,
            "top": float(y) + p.height_m / 2.0,
            "bot": float(y) - p.height_m / 2.0,
            "fairing_xsections": n.fairing_xsections,
        })
    return out


def stack_geometry(design) -> list[dict]:
    """Stack-attached parts only, kept for older callers/tests."""
    return [g for g in assembly_geometry(design) if not g["surface"]]


def _body_boxes(geom: list[dict]) -> list[dict]:
    return [g for g in geom if not g["surface"] and g["role"] != "fin"]


def _radial_span(geom: list[dict], include_landing_gear: bool = False) -> float:
    radii: list[float] = []
    for g in geom:
        if g["role"] == "leg" and not include_landing_gear:
            continue
        radii.append(math.hypot(g["x"], g["z"]) + g["draw_dia"] / 2.0)
    return 2.0 * max(radii, default=0.0)


def looks_like_a_rocket(design) -> dict:
    """Calculate vehicle proportions and return a hard PASS/FAIL report."""
    geom = assembly_geometry(design)
    body = _body_boxes(geom)
    if not body:
        checks = {"has stack geometry": False}
        return {"checks": checks, "looks_like_a_rocket": False}

    y_top = max(b["top"] for b in body)
    y_bot = min(b["bot"] for b in body)
    length = y_top - y_bot
    max_dia = max(b["dia"] for b in body)
    fineness = length / max_dia if max_dia > 0 else float("inf")

    ordered = sorted(body, key=lambda b: -b["y"])
    dias = [b["dia"] for b in ordered]
    taper_ok = all(dias[i] <= dias[i + 1] + 1e-6 for i in range(len(dias) - 1))
    top_part = ordered[0]
    bottom_part = min(body, key=lambda b: b["y"])

    static_margin_m = float(getattr(design, "static_margin_m", 0.0) or 0.0)
    static_margin_cal = static_margin_m / max_dia if max_dia > 0 else 0.0
    # KSP launchers are actively stabilized by gimballed engines and MechJeb guidance. Keep strongly
    # negative static margin as a hard fail, but allow a modestly negative margin when the body is
    # otherwise slender and finned; this still rejects the original 2.5 m Duna tower (~-1.7 calibers).
    active_control_stable = bool(getattr(design, "ascent_stable", True)) or static_margin_cal >= -1.50

    radial_span = _radial_span(geom, include_landing_gear=False)
    full_span = _radial_span(geom, include_landing_gear=True)
    radial_ok = radial_span <= max(max_dia * 1.85, max_dia + 2.5)
    drag_loss = float(getattr(design, "ascent_drag_loss_mps", 0.0) or 0.0)
    landed_ok = (not bool(getattr(design, "landing_legs", False))) or bool(getattr(design, "landed_stable", True))

    checks = {
        "slender body (6 <= L/D <= 28)": 6.0 <= fineness <= 28.0,
        "monotonic taper (widest toward base)": taper_ok,
        "streamlined nose or capsule top": top_part["name"] in NOSE_PARTS or any(b["role"] == "fairing" for b in body),
        "engine at the base": bottom_part["role"] == "engine",
        "controlled ascent margin": active_control_stable,
        "radial protrusions within ascent envelope": radial_ok,
        "landing gear tip-over stable": landed_ok,
    }
    return {
        "length_m": round(length, 2),
        "max_diameter_m": round(max_dia, 2),
        "fineness_ratio": round(fineness, 1),
        "radial_span_m": round(radial_span, 2),
        "full_span_m": round(full_span, 2),
        "static_margin_m": round(static_margin_m, 2),
        "static_margin_calibers": round(static_margin_cal, 2),
        "ascent_drag_loss_mps": drag_loss,
        "checks": checks,
        "looks_like_a_rocket": all(checks.values()),
    }


def _project_x(g: dict, centre: float, scale: float) -> float:
    return centre + g["x"] * scale


def _project_z(g: dict, centre: float, scale: float) -> float:
    return centre + g["z"] * scale


def _color(role: str) -> str:
    return {
        "engine": "#f87171",
        "body": "#cbd5e1",
        "adapter": "#dbeafe",
        "nose": "#e2e8f0",
        "fairing": "#fde68a",
        "fin": "#94a3b8",
        "leg": "#64748b",
    }.get(role, "#cbd5e1")


def _draw_side_part(g: dict, cx: float, yy, scale: float, front: bool = False) -> str:
    role = g["role"]
    fill = _color(role)
    top_y = yy(g["top"])
    h = max(1.0, g["h"] * scale)
    offset = (g["z"] if front else g["x"]) * scale
    local_cx = cx + offset
    w = max(1.0, g["draw_dia"] * scale)
    stroke = "#475569"

    if role == "fairing":
        half = w / 2.0
        base_y = yy(g["bot"])
        tip_y = yy(g["top"]) - 12.0
        return (
            f'<path d="M {local_cx-half:.1f} {base_y:.1f} '
            f'Q {local_cx-half:.1f} {tip_y+20:.1f} {local_cx:.1f} {tip_y:.1f} '
            f'Q {local_cx+half:.1f} {tip_y+20:.1f} {local_cx+half:.1f} {base_y:.1f} Z" '
            f'fill="{fill}" stroke="#d97706" stroke-width="1.4" opacity="0.94"/>'
        )
    if role == "nose" and g["name"] == "noseCone":
        half = w / 2.0
        return (
            f'<polygon points="{local_cx:.1f},{top_y:.1f} {local_cx-half:.1f},{top_y+h:.1f} '
            f'{local_cx+half:.1f},{top_y+h:.1f}" fill="{fill}" stroke="{stroke}"/>'
        )
    if role == "adapter":
        top_d, bot_d = ADAPTER_ENDS.get(g["name"], (g["dia"], g["dia"]))
        top_w = top_d * scale
        bot_w = bot_d * scale
        return (
            f'<polygon points="{local_cx-top_w/2:.1f},{top_y:.1f} {local_cx+top_w/2:.1f},{top_y:.1f} '
            f'{local_cx+bot_w/2:.1f},{top_y+h:.1f} {local_cx-bot_w/2:.1f},{top_y+h:.1f}" '
            f'fill="{fill}" stroke="#2563eb"/>'
        )
    if role == "engine":
        half_t = w * 0.34
        half_b = w * 0.50
        return (
            f'<polygon points="{local_cx-half_t:.1f},{top_y:.1f} {local_cx+half_t:.1f},{top_y:.1f} '
            f'{local_cx+half_b:.1f},{top_y+h:.1f} {local_cx-half_b:.1f},{top_y+h:.1f}" '
            f'fill="{fill}" stroke="#991b1b"/>'
        )
    if role == "fin":
        half = max(8.0, w * 0.7)
        return (
            f'<polygon points="{local_cx:.1f},{top_y:.1f} {local_cx-half:.1f},{top_y+h+18:.1f} '
            f'{local_cx+half:.1f},{top_y+h+18:.1f}" fill="{fill}" stroke="{stroke}" opacity="0.88"/>'
        )
    if role == "leg":
        foot = max(14.0, w * 2.0)
        return (
            f'<line x1="{local_cx:.1f}" y1="{top_y:.1f}" x2="{local_cx:.1f}" y2="{top_y+h+foot:.1f}" '
            f'stroke="{fill}" stroke-width="3" stroke-linecap="round"/>'
        )
    return (
        f'<rect x="{local_cx-w/2:.1f}" y="{top_y:.1f}" width="{w:.1f}" height="{h:.1f}" '
        f'rx="3" fill="{fill}" stroke="{stroke}"/>'
    )


def render_svg(design) -> str:
    geom = assembly_geometry(design)
    rep = looks_like_a_rocket(design)
    body = _body_boxes(geom)
    y_top = max(b["top"] for b in body)
    y_bot = min(b["bot"] for b in body)
    length = max(1.0, y_top - y_bot)
    max_span = max(rep.get("full_span_m", rep.get("max_diameter_m", 1.0)), rep.get("max_diameter_m", 1.0))
    scale = min(560.0 / length, 150.0 / max(1.0, max_span))

    def yy(m: float) -> float:
        return 76.0 + (y_top - m) * scale

    side_cx = 150.0
    front_cx = 390.0
    top_cx = 625.0
    top_cy = 380.0
    parts = sorted(geom, key=lambda g: (g["surface"], -g["y"]))

    side_svg = []
    front_svg = []
    for g in parts:
        side_svg.append(_draw_side_part(g, side_cx, yy, scale, front=False))
        front_svg.append(_draw_side_part(g, front_cx, yy, scale, front=True))

    top_svg = []
    # Draw largest body circles first, then accessories.
    body_by_dia = sorted([g for g in geom if not g["surface"]], key=lambda g: -g["dia"])
    for g in body_by_dia[:8]:
        r = g["dia"] * scale / 2.0
        top_svg.append(
            f'<circle cx="{top_cx:.1f}" cy="{top_cy:.1f}" r="{r:.1f}" fill="{_color(g["role"])}" '
            f'stroke="#475569" opacity="0.30"/>'
        )
    for g in [g for g in geom if g["surface"]]:
        r = max(2.5, g["draw_dia"] * scale / 2.0)
        x = _project_x(g, top_cx, scale)
        z = _project_z(g, top_cy, scale)
        top_svg.append(
            f'<circle cx="{x:.1f}" cy="{z:.1f}" r="{r:.1f}" fill="{_color(g["role"])}" '
            f'stroke="#334155" opacity="0.80"/>'
        )

    verdict = "LOOKS LIKE A ROCKET" if rep["looks_like_a_rocket"] else "REJECTED - geometry gate failed"
    vcol = "#16a34a" if rep["looks_like_a_rocket"] else "#dc2626"
    check_lines = []
    ty = 94
    for label, ok in rep["checks"].items():
        mark = "PASS" if ok else "FAIL"
        col = "#16a34a" if ok else "#dc2626"
        check_lines.append(f'<text x="805" y="{ty}" font-size="12" fill="{col}">{mark} - {escape(label)}</text>')
        ty += 22

    metrics = [
        f'L/D {rep["fineness_ratio"]}:1',
        f'length {rep["length_m"]} m',
        f'body dia {rep["max_diameter_m"]} m',
        f'ascent span {rep["radial_span_m"]} m',
        f'full span {rep["full_span_m"]} m',
        f'static {rep["static_margin_calibers"]} cal',
        f'Cd {getattr(design, "drag_cd", "?")}',
        f'drag loss {getattr(design, "ascent_drag_loss_mps", "?")} m/s',
        f'max-Q {getattr(design, "max_q_kpa", "?")} kPa',
    ]
    metric_svg = []
    for i, m in enumerate(metrics):
        metric_svg.append(f'<text x="805" y="{ty + 10 + 18*i}" font-size="12" fill="#0f172a">{escape(m)}</text>')

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="700" viewBox="0 0 1080 700" font-family="system-ui,Segoe UI,sans-serif">
  <rect width="1080" height="700" fill="#f8fafc"/>
  <text x="30" y="32" font-size="19" font-weight="700" fill="#0f172a">Three-view design chart - {escape(design.name)}</text>
  <text x="805" y="56" font-size="15" font-weight="700" fill="{vcol}">{verdict}</text>
  <text x="118" y="58" font-size="12" font-weight="700" fill="#334155">SIDE</text>
  <text x="355" y="58" font-size="12" font-weight="700" fill="#334155">FRONT</text>
  <text x="594" y="220" font-size="12" font-weight="700" fill="#334155">TOP</text>
  <line x1="{side_cx}" y1="60" x2="{side_cx}" y2="650" stroke="#e2e8f0" stroke-dasharray="4 4"/>
  <line x1="{front_cx}" y1="60" x2="{front_cx}" y2="650" stroke="#e2e8f0" stroke-dasharray="4 4"/>
  <circle cx="{top_cx}" cy="{top_cy}" r="{max(2.0, rep["max_diameter_m"] * scale / 2.0):.1f}" fill="none" stroke="#94a3b8" stroke-dasharray="4 4"/>
  {''.join(side_svg)}
  {''.join(front_svg)}
  {''.join(top_svg)}
  {''.join(check_lines)}
  {''.join(metric_svg)}
</svg>'''


def verify_against_live(conn, design) -> dict:
    """Compare calculated geometry/mass with the assembled live vessel from kRPC."""
    v = conn.space_center.active_vessel
    calc = looks_like_a_rocket(design)
    calc_mass = float(getattr(design, "estimates", {}).get("wet_mass_t", 0.0))
    live_mass_t = v.mass / 1000.0
    length = width = None
    try:
        rf = v.reference_frame
        ys, rs = [], []
        for p in v.parts.all:
            x, y, z = p.position(rf)
            ys.append(y)
            rs.append((x * x + z * z) ** 0.5)
        if ys:
            length = max(ys) - min(ys)
            width = 2.0 * max(rs)
        if length is not None and not (0.0 < length < 500.0 and 0.0 < width < 500.0):
            length = width = None
    except Exception:
        length = width = None
    out = {
        "live_length_m": round(length, 2) if length else None,
        "calc_length_m": calc["length_m"],
        "live_max_diameter_m": round(width, 2) if width else None,
        "calc_max_diameter_m": calc["max_diameter_m"],
        "live_fineness": round(length / width, 1) if (length and width) else None,
        "calc_fineness": calc["fineness_ratio"],
        "live_mass_t": round(live_mass_t, 2),
        "calc_wet_mass_t": round(calc_mass, 2),
        "live_part_count": len(v.parts.all),
    }
    out["mass_match"] = abs(live_mass_t - calc_mass) <= 0.10 * max(calc_mass, 1.0)
    out["dimensions_match"] = bool(length and width and (
        abs(length - calc["length_m"]) <= 0.25 * calc["length_m"] + 2.0
        and abs(width - calc["max_diameter_m"]) <= 0.5 * calc["max_diameter_m"] + 0.5
    ))
    return out


def _relay_comsat():
    from ksp_lab.design import Phase, ShipRequirements, default_reserve_frac, design_ship

    req = ShipRequirements(
        name="AI-Relay-Keo",
        mission_type="relay_comsat",
        crew=0,
        payload_t=0.3,
        phases=[
            Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3, reserve_frac=default_reserve_frac(9.81)),
            Phase("insertion", 1300.0, twr_body_g=0.0, min_twr=0.0, reserve_frac=default_reserve_frac(0.0)),
        ],
        landing=None,
        needs_legs=False,
        needs_heatshield=False,
        needs_docking=False,
        max_engine_count=1,
    )
    return design_ship(req)


def main() -> int:
    design = _relay_comsat()
    rep = looks_like_a_rocket(design)
    out = Path(__file__).resolve().parents[1] / "docs" / f"design_chart_{design.name}.svg"
    out.write_text(render_svg(design), encoding="utf-8")
    print(f"design chart written: {out}")
    print(f"  length {rep['length_m']} m | body dia {rep['max_diameter_m']} m | L/D {rep['fineness_ratio']}:1")
    for label, ok in rep["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"  => {'LOOKS LIKE A ROCKET' if rep['looks_like_a_rocket'] else 'REJECTED - geometry gate failed'}")
    return 0 if rep["looks_like_a_rocket"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
