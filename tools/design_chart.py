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


def assembly_geometry(design, part_bodies: dict | None = None) -> list[dict]:
    """All generated craft parts in the writer coordinate system, including surface attachments.

    ``part_bodies`` must mirror what ``CraftWriter.write()`` will actually emit. When a real harvested
    part-body library is supplied, optional parts whose serialization was NOT harvested (payload
    service bays, the inter-stage adapters when no donor craft carries them, ...) are DROPPED from the
    build here exactly as ``can_emit()`` drops them from the ``.craft`` file. Passing ``None``
    (offline/tests) keeps every part, which is consistent because the offline writer also emits every
    part. Threading the SAME library the writer uses is what keeps the prelaunch chart in step with the
    launched vessel — otherwise the chart counts phantom parts the craft never carries and over-reports
    length (the 21.6 m chart vs 12.9 m live discrepancy was ~6.5 m of un-harvested service bays +
    adapters that the live craft silently dropped)."""
    nodes = CraftWriter()._build_nodes(design, part_bodies=part_bodies)
    out: list[dict] = []
    for n in nodes:
        p = part(n.part_name)
        x, y, z = n.pos_xyz if n.pos_xyz is not None else (0.0, n.y, 0.0)
        role = _role(n.part_name, n.fairing_xsections)
        # Draw engines at their BELL width (~0.72x the mounting node), the size that actually packs into a
        # base cluster — so the chart shows the real engine section and `_radial_span` measures the true
        # bell footprint (an over-wide cluster still shows; the old 0.60 shrink hid it inconsistently).
        # Fins/legs are genuinely thin plates/struts; their radial POSITION is honest so a floating leg shows.
        draw_dia = p.diameter_m
        if role == "engine":
            draw_dia = p.diameter_m * 0.72
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
    # A payload fairing's ogive shell extrudes UPWARD from its base over every part above it (the
    # payload bus). Record that shell extent on the fairing dict so the chart can draw the real shroud
    # ENCLOSING the payload — not the tiny 0.42 m base part, which made the payload look exposed.
    for f in out:
        if f["role"] == "fairing" and not f["surface"]:
            enclosed = [g for g in out if not g["surface"] and g["role"] != "fairing" and g["y"] > f["y"]]
            f["shell_top"] = max((g["top"] for g in enclosed), default=f["top"])
            f["encloses"] = max((g["dia"] for g in enclosed), default=f["dia"])
    return out


def stack_geometry(design, part_bodies: dict | None = None) -> list[dict]:
    """Stack-attached parts only, kept for older callers/tests."""
    return [g for g in assembly_geometry(design, part_bodies) if not g["surface"]]


def _body_boxes(geom: list[dict]) -> list[dict]:
    return [g for g in geom if not g["surface"] and g["role"] != "fin"]


def _radial_span(geom: list[dict], include_landing_gear: bool = False,
                 exclude_names: set | None = None) -> float:
    radii: list[float] = []
    for g in geom:
        if g["role"] == "leg" and not include_landing_gear:
            continue
        if exclude_names is not None and g["name"] in exclude_names:
            continue
        radii.append(math.hypot(g["x"], g["z"]) + g["draw_dia"] / 2.0)
    return 2.0 * max(radii, default=0.0)


def _booster_part_names(design) -> set:
    """The part names that make up the radial (strap-on) booster pods, or an empty set for a single
    core. Used so the geometry gate can treat the symmetric cluster as legitimate, not as overhang."""
    rb = getattr(design, "radial_boosters", None)
    if rb is None or getattr(rb, "count", 0) <= 0:
        return set()
    return {n for n in (rb.engine, rb.tank, rb.decoupler) if n}  # drop "" engine for a drop-tank pod


def _boosters_are_symmetric(geom: list[dict], booster_names: set, expected_count: int) -> bool:
    """A legitimate strap-on cluster is N pods evenly spaced around the axis at a common radius — NOT a
    lopsided overhang. Verify it: take the booster TANK parts (one column per pod), confirm there are
    `expected_count` of them at (nearly) equal radius and roughly even azimuths. Returns True for a clean
    symmetric cluster, False for an asymmetric/garbled one (which should still fail the gate)."""
    if not booster_names or expected_count <= 0:
        return False
    pods = [g for g in geom if g["surface"] and g["name"] in booster_names]
    # Group by azimuth to count distinct pods (parts in the same pod share an angle).
    angles = sorted({round(math.degrees(math.atan2(g["z"], g["x"])) % 360.0, 0) for g in pods})
    if len(angles) != expected_count:
        return False
    # RADIUS check on the pod TANK COLUMN only. A pod is a clean vertical stack of `tank_count` tanks,
    # so the tank part is the MOST FREQUENT name in the cluster; its parts all sit at the pod's outboard
    # radius. The pod's engine (tucked toward the core at the bell plane) and its single radial decoupler
    # (mounted inboard, against the core hull) legitimately sit at SMALLER radii — folding them into the
    # mean made a perfectly symmetric ring read as a 1.8->3.6 m "spread" and falsely fail the gate. Judge
    # the ring on the tank column, exactly as this function's contract ("take the booster TANK parts").
    from collections import Counter
    tank_name = Counter(g["name"] for g in pods).most_common(1)[0][0]
    radii = [math.hypot(g["x"], g["z"]) for g in pods
             if g["name"] == tank_name and math.hypot(g["x"], g["z"]) > 1e-6]
    if not radii:
        return False
    r_mean = sum(radii) / len(radii)
    # all pod TANK parts within 25% of the mean radius (a tight, even ring)
    if any(abs(r - r_mean) > 0.25 * r_mean + 0.5 for r in radii):
        return False
    # azimuths roughly evenly spaced: each gap close to 360/N
    gaps = [(angles[(i + 1) % len(angles)] - angles[i]) % 360.0 for i in range(len(angles))]
    target = 360.0 / expected_count
    return all(abs(g - target) <= 0.35 * target for g in gaps)


def looks_like_a_rocket(design, part_bodies: dict | None = None) -> dict:
    """Calculate vehicle proportions and return a hard PASS/FAIL report.

    Pass the same ``part_bodies`` the writer will use so the report describes the craft that actually
    launches (see ``assembly_geometry``)."""
    geom = assembly_geometry(design, part_bodies)
    body = _body_boxes(geom)
    if not body:
        checks = {"has stack geometry": False}
        return {"checks": checks, "looks_like_a_rocket": False}

    y_top = max(b["top"] for b in body)
    y_bot = min(b["bot"] for b in body)
    length = y_top - y_bot
    # Part-ORIGIN span (centre of the top box to centre of the bottom box). kRPC's verify_against_live
    # measures live part ORIGINS (Part.position), so this — not the box-envelope ``length`` above, which
    # is ~half a part-height longer at each end — is the apples-to-apples number to compare against live.
    origin_length = max(b["y"] for b in body) - min(b["y"] for b in body)
    max_dia = max(b["dia"] for b in body)
    fineness = length / max_dia if max_dia > 0 else float("inf")

    ordered = sorted(body, key=lambda b: -b["y"])
    # Taper is a HULL property: judge it on the load-bearing tanks/adapters/command/nose only. Engines
    # and inter-stage decouplers are NARROWER than the tank they tuck under (a 1.25 m Terrier beneath a
    # 2.5 m stage, a 1.25 m TD-12 between two stages), so including them reads as a false narrowing. The
    # real hull profile (tanks + conical adapters) is what must be non-increasing upward.
    hull = [b for b in ordered if b["name"] != "Decoupler.1" and b["role"] != "engine"]
    dias = [b["dia"] for b in hull]
    taper_ok = all(dias[i] <= dias[i + 1] + 1e-6 for i in range(len(dias) - 1))
    top_part = ordered[0]
    bottom_part = min(body, key=lambda b: b["y"])

    static_margin_m = float(getattr(design, "static_margin_m", 0.0) or 0.0)
    static_margin_cal = static_margin_m / max_dia if max_dia > 0 else 0.0
    # KSP launchers are actively stabilized by gimballed engines and MechJeb guidance. Keep strongly
    # negative static margin as a hard fail, but allow a modestly negative margin when the body is
    # otherwise slender and finned; this still rejects the original 2.5 m Duna tower (~-1.7 calibers).
    active_control_stable = bool(getattr(design, "ascent_stable", True)) or static_margin_cal >= -1.50

    full_span = _radial_span(geom, include_landing_gear=True)
    # RADIAL (strap-on) BOOSTERS are a LEGITIMATE wide protrusion — a Soyuz/Falcon-Heavy clusters tank+
    # engine pods well outside the core hull, on purpose. So judge the ascent envelope on the CORE only
    # (boosters excluded), then accept the booster cluster separately IF it is symmetric (N even pods at a
    # common radius), not a lopsided overhang. The full span is reported for reference.
    booster_names = _booster_part_names(design)
    rb_count = int(getattr(getattr(design, "radial_boosters", None), "count", 0) or 0)
    core_span = _radial_span(geom, include_landing_gear=False, exclude_names=booster_names)
    radial_span = _radial_span(geom, include_landing_gear=False)
    # Core ascent envelope: with engines clustered INSIDE the tank, the only core protrusions are fins
    # (~0.4 m past the hull). Anything wider than ~1.6x the body diameter is a core overhang and FAILS.
    core_radial_ok = core_span <= max(max_dia * 1.6, max_dia + 1.2)
    # Booster cluster: legitimate when the pods are SYMMETRIC and the whole cluster stays within a sane
    # strap-on envelope (~core + 2 full pod diameters of reach, i.e. <= ~4x the core diameter — a Soyuz is
    # ~3x). A symmetric ring up to that bound is a real rocket; a lopsided or runaway cluster still fails.
    boosters_present = bool(booster_names) and rb_count > 0
    boosters_symmetric = (not boosters_present) or _boosters_are_symmetric(geom, booster_names, rb_count)
    booster_envelope_ok = (not boosters_present) or (
        boosters_symmetric and radial_span <= max(max_dia * 4.0, max_dia + 12.0))
    radial_ok = core_radial_ok and booster_envelope_ok
    drag_loss = float(getattr(design, "ascent_drag_loss_mps", 0.0) or 0.0)
    landed_ok = (not bool(getattr(design, "landing_legs", False))) or bool(getattr(design, "landed_stable", True))

    checks = {
        # Real launchers run L/D ~8 (Saturn V ~11) to ~19 (Falcon 9 = 18.9). Cap at 19: accepts a
        # legitimately tall slender stack (a single-launch propulsive interplanetary round-trip) while
        # rejecting the old 22-24:1 noodles. A better mission profile (chutes/aerobrake) shortens landers.
        "slender body (4 <= L/D <= 19)": 4.0 <= fineness <= 19.0,
        "monotonic taper (widest toward base)": taper_ok,
        "payload housed (fairing or capsule top)": top_part["name"] in NOSE_PARTS or any(b["role"] == "fairing" for b in body),
        "engine at the base": bottom_part["role"] == "engine",
        "controlled ascent margin": active_control_stable,
        # The envelope check now passes the CORE protrusion bound AND (if any) a SYMMETRIC strap-on
        # cluster — so 4 even radial boosters read as a real rocket, while a lopsided overhang still fails.
        "radial protrusions within ascent envelope": radial_ok,
        "landing gear tip-over stable": landed_ok,
    }
    if boosters_present:
        # Surface the strap-on verdict explicitly so a bad (asymmetric) cluster is visible in the report.
        checks["symmetric strap-on boosters"] = boosters_symmetric
    return {
        "length_m": round(length, 2),
        "origin_length_m": round(origin_length, 2),
        "max_diameter_m": round(max_dia, 2),
        "fineness_ratio": round(fineness, 1),
        "radial_span_m": round(radial_span, 2),
        "core_span_m": round(core_span, 2),
        "full_span_m": round(full_span, 2),
        "radial_booster_count": rb_count,
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
        # Draw the real ogive SHROUD: a cylinder as wide as the payload it encloses, from the fairing
        # base up over the bus to a rounded nose above it. Semi-transparent so the enclosed payload shows
        # faintly INSIDE — proving it is housed, not riding exposed on the nose.
        shell_dia = max(g["dia"], g.get("encloses", g["dia"]))
        half = shell_dia * scale / 2.0
        base_y = yy(g["bot"])
        shoulder_m = g.get("shell_top", g["top"])
        shoulder_y = yy(shoulder_m)
        tip_y = shoulder_y - max(18.0, half * 1.3)
        return (
            f'<path d="M {local_cx-half:.1f} {base_y:.1f} '
            f'L {local_cx-half:.1f} {shoulder_y:.1f} '
            f'Q {local_cx-half:.1f} {tip_y+14:.1f} {local_cx:.1f} {tip_y:.1f} '
            f'Q {local_cx+half:.1f} {tip_y+14:.1f} {local_cx+half:.1f} {shoulder_y:.1f} '
            f'L {local_cx+half:.1f} {base_y:.1f} Z" '
            f'fill="{fill}" stroke="#d97706" stroke-width="1.4" opacity="0.55"/>'
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


def render_svg(design, part_bodies: dict | None = None) -> str:
    geom = assembly_geometry(design, part_bodies)
    rep = looks_like_a_rocket(design, part_bodies)
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
        f'core span {rep.get("core_span_m", rep["radial_span_m"])} m',
        f'full span {rep["full_span_m"]} m',
        f'static {rep["static_margin_calibers"]} cal',
        f'Cd {getattr(design, "drag_cd", "?")}',
        f'drag loss {getattr(design, "ascent_drag_loss_mps", "?")} m/s',
        f'max-Q {getattr(design, "max_q_kpa", "?")} kPa',
    ]
    if rep.get("radial_booster_count", 0):
        metrics.insert(4, f'{rep["radial_booster_count"]}x radial boosters (span {rep["radial_span_m"]} m)')
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


def design_and_verify(req, *, out_dir, part_bodies: dict | None = None,
                      use_full_catalog: bool = False) -> tuple:
    """THE single entry point a caller MUST use so a design is never flown without a PNG-verified rocket
    shape. Enforces the three-view-PNG appearance constraint end to end:

        1. run ``design_ship(req)`` to build the calculated RocketDesign;
        2. render its THREE-VIEW orthographic SVG (side / front / top) from the same CraftWriter node
           assembly that writes the .craft file;
        3. RASTERIZE that SVG to a real PNG via ``tools/render_chart_png.render`` (headless Chrome/Edge),
           so the shape is proven as an image, not just trusted from the XML coordinates;
        4. run the ``looks_like_a_rocket`` geometry GATE and return whether it passed.

    The render is the PROOF (an inspectable picture); the gate is the PASS/FAIL. Returns
    ``(design, png_path, ok, report)`` where:
        * ``design``   – the RocketDesign that was sized,
        * ``png_path`` – absolute path to the rasterized three-view PNG (or None if no browser is present),
        * ``ok``       – True only if the geometry gate passed AND the PNG actually rendered,
        * ``report``   – the ``looks_like_a_rocket`` dict, augmented with ``svg_path``, ``png_path``,
                         ``png_rendered`` and ``failing_checks`` (the list of gate checks that FAILED).

    If the PNG renderer is unavailable (no Chrome/Edge), the SVG + gate result are still returned and
    ``png_rendered`` is False (so a caller can see the gate verdict but knows the image proof is missing)."""
    from ksp_lab.design import design_ship

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    design = design_ship(req, use_full_catalog=use_full_catalog) if use_full_catalog else design_ship(req)
    report = looks_like_a_rocket(design, part_bodies)
    svg = render_svg(design, part_bodies)
    safe_name = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(design.name)) or "design"
    svg_path = out_dir / f"design_chart_{safe_name}.svg"
    svg_path.write_text(svg, encoding="utf-8")

    png_path = None
    png_rendered = False
    try:
        # Import the rasterizer lazily so the module still loads where no browser/renderer exists.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import render_chart_png  # tools/render_chart_png.py
        png_path = render_chart_png.render(str(svg_path))
        png_rendered = bool(png_path) and Path(png_path).exists() and Path(png_path).stat().st_size > 0
    except Exception as exc:  # no Chrome/Edge, render timeout, etc. — keep the SVG + gate result.
        report = dict(report)
        report["png_error"] = f"{type(exc).__name__}: {exc}"

    failing = [label for label, passed in report.get("checks", {}).items() if not passed]
    gate_ok = bool(report.get("looks_like_a_rocket", False))
    report = dict(report)
    report["svg_path"] = str(svg_path)
    report["png_path"] = str(png_path) if png_path else None
    report["png_rendered"] = png_rendered
    report["failing_checks"] = failing
    # A design is only verified-ok when the geometry gate passes AND the appearance was actually
    # rasterized to a PNG (the proof). A passing gate with no PNG is reported but NOT ok=True.
    ok = gate_ok and png_rendered
    return design, (str(png_path) if png_path else None), ok, report


def verify_against_live(conn, design, part_bodies: dict | None = None) -> dict:
    """Compare calculated geometry/mass with the assembled live vessel from kRPC.

    Pass the SAME ``part_bodies`` the writer used so ``calc`` describes the craft that was actually
    assembled (un-harvested optional parts are dropped from both) — without it the chart counts phantom
    parts and over-reports length. Two axis/measurement fixes vs the old version:
      * The live LENGTH is the largest part-origin extent across all THREE vessel-frame axes (the long
        axis), and the DIAMETER is the widest radial spread in the perpendicular plane — so it is robust
        to whichever frame axis the control part aligns the stack with, instead of assuming the y-axis.
      * ``Part.position`` is the part ORIGIN, so the live span is origin-to-origin; it is compared to
        ``calc['origin_length_m']`` (same convention), not the box-envelope ``length_m`` which is ~one
        part-height longer at each end. ``calc_envelope_length_m`` keeps the gate's end-to-end length
        for reference."""
    v = conn.space_center.active_vessel
    calc = looks_like_a_rocket(design, part_bodies)
    calc_mass = float(getattr(design, "estimates", {}).get("wet_mass_t", 0.0))
    live_mass_t = v.mass / 1000.0
    length = width = None
    try:
        rf = v.reference_frame
        pts = [p.position(rf) for p in v.parts.all]
        if pts:
            ext = [max(c[i] for c in pts) - min(c[i] for c in pts) for i in range(3)]
            axis = ext.index(max(ext))                       # long axis = largest part-origin extent
            length = ext[axis]
            perp = [i for i in range(3) if i != axis]
            width = 2.0 * max((c[perp[0]] ** 2 + c[perp[1]] ** 2) ** 0.5 for c in pts)
        if length is not None and not (0.0 < length < 500.0 and 0.0 < width < 500.0):
            length = width = None
    except Exception:
        length = width = None
    calc_origin_len = calc.get("origin_length_m", calc["length_m"])
    out = {
        "live_length_m": round(length, 2) if length else None,
        "calc_length_m": calc_origin_len,                    # origin-to-origin, matches the live measure
        "calc_envelope_length_m": calc["length_m"],          # box end-to-end (what the L/D gate uses)
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
        abs(length - calc_origin_len) <= 0.15 * calc_origin_len + 1.0
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
