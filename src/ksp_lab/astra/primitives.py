"""ASTRA's primitive catalog — atomic, BODY-AGNOSTIC mission steps.

The old ASTRA mapped a command to one of three coarse, MUN-hardcoded bundles and ran a bespoke
monolithic driver per bundle. This module is the replacement: a registry of SMALL, atomic primitives,
each of which does exactly ONE thing (launch, transfer, land, dock, plant a flag, ...) for ANY body.

Design rules (deliberate, learned the hard way on this project):
  * WRAP, don't rewrite. Every primitive calls the PROVEN flight functions (deploy_relay.launch_to_lko,
    deploy_relay_transfer.transfer_to_body, crewed_eve_roundtrip.capture/descend, the MechJeb bridge
    autopilots, ...). The flight logic is the validated path; primitives only parameterize and sequence it.
  * BODY-AGNOSTIC. A primitive reads the launch/target body from its args + bodies.py / live kRPC, never
    a hardcoded ``body="Mun"``.
  * STRUCTURED + MARKER-LOGGED. Each primitive logs a ``mission_phase: <name>`` line and a
    ``RESULT: SUCCESS|FAIL`` line (so the agent's existing marker/`RESULT` parsing works) and returns a
    ``PrimitiveResult``.
  * FAIL FAST. A primitive returns ``ok=False`` (and the agent aborts) rather than hanging or silently
    swallowing an error. The wrapped flight functions already return booleans; we surface them.

Heavy flight modules (tools/*) are imported LAZILY inside each primitive so this module imports OFFLINE
(the test suite, ``--dry-run``) with no kRPC / requests / live craft. The catalog metadata at the bottom
(names, descriptions, param schemas) is what the LLM decomposer is shown.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..bodies import body as lookup_body
from ..vessel_match import vessel_names_match

# tools/ holds the proven drivers; primitives import from there lazily. Make it importable by path.
_TOOLS_DIR = Path(__file__).resolve().parents[3] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


def _log(msg: str) -> None:
    print(f"[ASTRA-PRIM {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ======================================================================================================
# Live context + result types
# ======================================================================================================
@dataclass
class PrimitiveContext:
    """The live flight context threaded through every primitive in a decomposed mission.

    Connected ONCE by the agent (kRPC conn + space_center + bridge + runner + cfg) and carried forward;
    each primitive may UPDATE the ``vessel`` / ``vessel_name`` / ``current_body`` / ``target_name`` state
    so the next primitive operates on the right craft. All kRPC fields are ``Any`` so this module imports
    with no krpc installed (offline)."""

    conn: Any = None
    sc: Any = None                      # space_center
    bridge: Any = None
    runner: Any = None
    cfg: dict = field(default_factory=dict)
    # mutable mission state
    vessel: Any = None                  # the active/current kRPC vessel handle
    vessel_name: str = ""               # the name the current vessel was launched under
    current_body: str = "Kerbin"        # the body the current vessel is at / launching from
    target_name: str = ""               # a named target vessel (for rendezvous/dock)
    dry_run: bool = False
    extra: dict = field(default_factory=dict)

    def refresh_vessel(self):
        """Re-read the active vessel from kRPC (after a switch/stage) and update ``current_body``."""
        if self.sc is None:
            return self.vessel
        try:
            self.vessel = self.sc.active_vessel
            self.vessel_name = str(self.vessel.name)
            self.current_body = str(self.vessel.orbit.body.name)
        except Exception:
            pass
        return self.vessel


@dataclass(slots=True)
class PrimitiveResult:
    primitive: str
    ok: bool
    marker: str = ""          # the mission_phase name this primitive ended on
    detail: str = ""
    data: dict = field(default_factory=dict)


def _emit(primitive: str, ok: bool, marker: str, detail: str = "", data: dict | None = None) -> PrimitiveResult:
    """Log the standard ``mission_phase`` + ``RESULT`` markers the agent parses, and build the result."""
    print(f"mission_phase: {marker}", flush=True)
    print(f"RESULT: {'SUCCESS' if ok else 'FAIL'}", flush=True)
    if detail:
        _log(f"{primitive} -> {marker}: {detail}")
    return PrimitiveResult(primitive=primitive, ok=ok, marker=marker, detail=detail, data=data or {})


# ======================================================================================================
# Primitive implementations. Each takes (ctx, **args) and returns a PrimitiveResult. Body-agnostic.
# ======================================================================================================
def select_vessel(ctx: PrimitiveContext, name: str) -> PrimitiveResult:
    """Make a vessel active by name, TOLERANT of KSP's localized name suffixes (the Chinese "飞船", the
    English "Probe", etc.). Matches by normalized / suffix-stripped / prefix — NOT exact equality (the bug
    that just stranded a vessel whose live name carried a localization suffix). Updates ctx state."""
    if ctx.dry_run or ctx.sc is None:
        ctx.vessel_name = name
        return _emit("select_vessel", True, "vessel_selected", f"(dry-run) would select {name!r}")
    match = None
    for vsl in ctx.sc.vessels:
        try:
            if vessel_names_match(str(vsl.name), name):
                match = vsl
                break
        except Exception:
            continue
    if match is None:
        return _emit("select_vessel", False, "vessel_not_found",
                     f"no vessel matched {name!r} (tolerant match) — stranded?")
    try:
        ctx.sc.active_vessel = match
        time.sleep(2)
        ctx.refresh_vessel()
    except Exception as exc:
        return _emit("select_vessel", False, "vessel_select_error", str(exc))
    return _emit("select_vessel", True, "vessel_selected",
                 f"active vessel is now {ctx.vessel_name!r} at {ctx.current_body}",
                 {"vessel": ctx.vessel_name, "body": ctx.current_body})


def _launch_requirements(name: str, *, target_alt_km: float, crew: int, heatshield: bool,
                         landing, radial_boosters: int, max_core_engines: int):
    """Build the SAME ShipRequirements deploy_relay.launch_to_lko sizes internally, so the design-chart
    gate (design_and_verify) reasons over the craft that will ACTUALLY be flown. Mirrors the req in
    deploy_relay.launch_to_lko (insertion Δv calculated for the target orbit, asparagus boosters,
    crewed-vs-relay command/recovery) — kept in step with that proven path."""
    import math

    from ..bodies import KERBIN
    from ..design import Phase, ShipRequirements, default_reserve_frac

    # Insertion Δv = Hohmann raise from the ~100 km parking orbit to the target + circularise + trim margin
    # (identical to launch_to_lko's calculation). A bare launch's target altitude needs little; a heavy
    # interplanetary upper is sized by the transfer() step, not here.
    r_park = KERBIN.radius_m + 100_000.0
    r_target = KERBIN.radius_m + max(0.0, float(target_alt_km)) * 1000.0
    a_tr = (r_park + r_target) / 2.0
    dv_raise = abs(math.sqrt(KERBIN.mu * (2.0 / r_park - 1.0 / a_tr)) - math.sqrt(KERBIN.mu / r_park))
    dv_circ = abs(math.sqrt(KERBIN.mu / r_target) - math.sqrt(KERBIN.mu * (2.0 / r_target - 1.0 / a_tr)))
    insertion_dv = 250.0 + dv_raise + dv_circ
    _ins_g, _ins_twr = (9.81, 0.5) if insertion_dv >= 3500.0 else (0.0, 0.0)
    _mission_type = "crewed_launch" if crew > 0 else "relay_comsat"
    return ShipRequirements(
        name=name, mission_type=_mission_type, crew=int(crew), payload_t=0.3,
        phases=[Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3,
                      reserve_frac=default_reserve_frac(9.81)),
                Phase("insertion", insertion_dv, twr_body_g=_ins_g, min_twr=_ins_twr,
                      reserve_frac=default_reserve_frac(0.0))],
        landing=landing, needs_legs=False, needs_heatshield=bool(heatshield), needs_docking=False,
        max_engine_count=int(max_core_engines),
        radial_booster_count=int(radial_boosters),
    )


def launch(ctx: PrimitiveContext, *, target_alt_km: float = 100.0, crew: int = 0, payload_t: float = 0.3,
           docking: bool = False, heatshield: bool = False, chutes: bool = False,
           radial_boosters: int = 0, max_core_engines: int = 1, name: str = "AI-Craft") -> PrimitiveResult:
    """Design + launch a craft to orbit the LAUNCH body (the body KSC sits on — Kerbin in stock).

    The design step is HARD-GATED on the three-view PNG: it calls design_chart.design_and_verify, which
    sizes the rocket, renders the orthographic side/front/top chart, RASTERIZES it to a real PNG (the
    inspectable proof), and runs the looks_like_a_rocket geometry gate. If that gate fails (or the craft
    is not a rocket), the primitive logs ``design_rejected`` + ``RESULT: FAIL`` and REFUSES to fly — the
    user's hard constraint that a craft is never launched without a PNG-verified rocket shape. Only on a
    passing gate does it call deploy_relay.launch_to_lko (the proven ascent)."""
    if ctx.dry_run:
        return _emit("launch", True, "launch_planned",
                     f"(dry-run) design+launch {name!r} to {target_alt_km:.0f} km, crew={crew}, "
                     f"radial_boosters={radial_boosters}")
    import deploy_relay
    landing = None
    if chutes:
        # Build a Kerbin-return landing spec (chutes are sized for the LAUNCH body's atmosphere).
        try:
            from ..design import LandingSite
            lb = lookup_body(ctx.current_body)
            landing = LandingSite(body_g=lb.surface_g, surface_rho=(lb.surface_rho or 1.225),
                                  target_touchdown_mps=6.0)
        except Exception:
            landing = None

    # ---- WIRING 1: three-view PNG design constraint. Build the SAME req launch_to_lko sizes, run it
    # through design_chart.design_and_verify (size -> render SVG -> rasterize PNG -> geometry gate), and
    # REFUSE to fly unless the rocket-shape gate passed AND the PNG actually rendered (ok=True).
    try:
        import design_chart
        req = _launch_requirements(name, target_alt_km=target_alt_km, crew=crew, heatshield=heatshield,
                                   landing=landing, radial_boosters=radial_boosters,
                                   max_core_engines=max_core_engines)
        out_dir = Path("docs")
        _design, png_path, design_ok, report = design_chart.design_and_verify(req, out_dir=out_dir)
    except Exception as exc:
        return _emit("launch", False, "design_error", f"{name}: design/verify raised {exc}")
    if not design_ok:
        failing = report.get("failing_checks") or []
        png_note = "no PNG rendered" if not report.get("png_rendered") else f"PNG {png_path}"
        return _emit("launch", False, "design_rejected",
                     f"{name}: three-view gate FAILED ({png_note}); failing checks: {failing}",
                     {"png_path": png_path, "failing_checks": failing,
                      "svg_path": report.get("svg_path")})
    _log(f"  design gate PASSED for {name}; three-view PNG: {png_path}")

    # insertion_dv_override stays 0 here (a plain orbit). transfer() sizes the upper for an interplanetary
    # budget when it is the next step; for a bare launch the calculated raise+circularize budget suffices.
    try:
        ok = deploy_relay.launch_to_lko(
            ctx.sc, ctx.cfg, ctx.runner, ctx.bridge, name, float(target_alt_km),
            booster_max_engines=int(max_core_engines), radial_booster_count=int(radial_boosters),
            crew=int(crew), needs_heatshield=bool(heatshield), landing=landing,
        )
    except Exception as exc:
        return _emit("launch", False, "launch_error", f"{name}: {exc}")
    if not ok:
        return _emit("launch", False, "launch_failed", f"{name}: ascent did not reach a stable orbit")
    ctx.refresh_vessel()
    ctx.vessel_name = name
    detail = f"{name} in orbit of {ctx.current_body} (design PNG: {png_path})"
    return _emit("launch", True, "launch_to_orbit", detail,
                 {"vessel": name, "body": ctx.current_body, "design_png": png_path})


def transfer(ctx: PrimitiveContext, *, target_body: str, capture_alt_km: float | None = None,
             capture_mode: str = "loose") -> PrimitiveResult:
    """Interplanetary OR moon transfer + capture, BODY-AGNOSTIC. Wraps deploy_relay_transfer.transfer_to_body
    (the proven precise-Lambert ejection + grid-search encounter + capture + Hohmann-to-altitude), with a
    'loose' retro-capture variant generalized from crewed_eve_roundtrip.capture_at_eve_loose, and an
    'aerocapture' variant that aims a low atmospheric periapsis.

    capture_mode:
      * 'circular'    -> capture + Hohmann to capture_alt_km (transfer_to_body). Default alt = synchronous.
      * 'loose'       -> cheap retro-capture into a bound ellipse (low periapsis, high apoapsis).
      * 'aerocapture' -> establish an encounter with a low ATMOSPHERIC periapsis; the air brakes (no burn)."""
    if ctx.dry_run:
        return _emit("transfer", True, "transfer_planned",
                     f"(dry-run) transfer to {target_body} (mode={capture_mode}, alt={capture_alt_km})")
    import deploy_relay_transfer as drt
    drt.cfg = ctx.cfg                                  # the reused drt machinery reads drt.cfg["krpc"]
    tb = lookup_body(target_body)
    name = ctx.vessel_name or "AI-Craft"
    v = ctx.refresh_vessel()
    try:
        if capture_mode == "loose":
            # Generalized loose retro-capture. capture_at_eve_loose is Eve-named but is just a low-periapsis
            # bound-ellipse capture; it reads the target from its body argument internally for Eve. For other
            # bodies we fall back to transfer_to_body with a high (loose) apoapsis target.
            if target_body.lower() == "eve":
                import crewed_eve_roundtrip as cer
                ok = cer.capture_at_eve_loose(ctx.conn, ctx.sc, ctx.bridge, v)
            else:
                # loose = capture near the natural encounter periapsis (cheap), don't Hohmann down.
                from ksp_lab.bodies import capture_apoapsis_ceiling_m
                alt = capture_alt_km if capture_alt_km is not None else \
                    capture_apoapsis_ceiling_m(tb, "loose") / 1000.0
                ok = drt.transfer_to_body(ctx.conn, ctx.sc, ctx.bridge, v, target_body, float(alt))
        elif capture_mode == "aerocapture":
            # Aim a low atmospheric periapsis so the atmosphere captures. Only meaningful on a body with air.
            if tb.atmosphere_top_m <= 0:
                return _emit("transfer", False, "aerocapture_airless",
                             f"{target_body} has no atmosphere — aerocapture impossible")
            alt = capture_alt_km if capture_alt_km is not None else \
                max(5.0, (tb.atmosphere_top_m * 0.45) / 1000.0)
            ok = drt.transfer_to_body(ctx.conn, ctx.sc, ctx.bridge, v, target_body, float(alt))
        else:  # 'circular'
            from ksp_lab.bodies import synchronous_altitude_m
            if capture_alt_km is not None:
                alt = float(capture_alt_km)
            else:
                sync = synchronous_altitude_m(tb)
                alt = (sync / 1000.0) if sync > 0 else max(50.0, tb.radius_m * 0.5 / 1000.0)
            if target_body.lower() == "mun":
                ok = drt.transfer_to_mun(ctx.conn, ctx.sc, ctx.bridge, v, name, float(alt))
            else:
                ok = drt.transfer_to_body(ctx.conn, ctx.sc, ctx.bridge, v, target_body, float(alt))
    except Exception as exc:
        return _emit("transfer", False, "transfer_error", f"->{target_body}: {exc}")
    ctx.refresh_vessel()
    ctx.target_name = ""
    if not ok:
        return _emit("transfer", False, "transfer_failed",
                     f"did not capture at {target_body} (now {ctx.current_body})")
    return _emit("transfer", True, "transfer_capture",
                 f"captured at {target_body} (mode {capture_mode})", {"body": ctx.current_body})


def set_orbit(ctx: PrimitiveContext, *, periapsis_km: float, apoapsis_km: float) -> PrimitiveResult:
    """Circularize / Hohmann to a target orbit around the CURRENT body. Wraps deploy_relay_transfer's
    precise _circularize_at / _hohmann_to_radius (for a circular target) or raise_and_circularize."""
    if ctx.dry_run:
        return _emit("set_orbit", True, "set_orbit_planned",
                     f"(dry-run) shape orbit to {periapsis_km:.0f}x{apoapsis_km:.0f} km")
    import deploy_relay_transfer as drt
    drt.cfg = ctx.cfg
    v = ctx.refresh_vessel()
    try:
        body = v.orbit.body
        # Target a circular orbit at the mean of the requested apsides (the proven helper circularizes).
        r_target = body.equatorial_radius + 0.5 * (periapsis_km + apoapsis_km) * 1000.0
        drt._hohmann_to_radius(ctx.conn, ctx.sc, ctx.bridge, v, r_target)
        ecc = v.orbit.eccentricity
    except Exception as exc:
        return _emit("set_orbit", False, "set_orbit_error", str(exc))
    ok = ecc < 0.25
    return _emit("set_orbit", ok, "orbit_set" if ok else "orbit_set_high_ecc",
                 f"{v.orbit.periapsis_altitude/1000:.0f}x{v.orbit.apoapsis_altitude/1000:.0f} km ecc={ecc:.3f}")


def land(ctx: PrimitiveContext, *, target_lat: float | None = None, target_lon: float | None = None) -> PrimitiveResult:
    """Land on the CURRENT body. Gravity/atmosphere come from the live body (bodies.py mirrors them).
    Wraps MechJeb's landing autopilot (bridge.mj_land) with the gentle-descent fallback used in
    eve_flag_mission._descend_to_gilly_surface."""
    if ctx.dry_run:
        where = f" at ({target_lat},{target_lon})" if target_lat is not None else ""
        return _emit("land", True, "land_planned", f"(dry-run) land on current body{where}")
    v = ctx.refresh_vessel()
    body_name = ctx.current_body
    try:
        # Prefer MechJeb's landing AP (calculates deorbit + decel burn + chute timing). For a low-gravity
        # airless body, the gentle hand-flown fallback (_descend_to_gilly_surface) is more reliable; it
        # itself tries MechJeb first then hand-flies, so it is the safe general path.
        import eve_flag_mission as efm
        targeted = target_lat is not None and target_lon is not None
        if targeted:
            try:
                ctx.bridge.mj_land(targeted=True, lat=float(target_lat), lon=float(target_lon),
                                   touchdown_speed=0.5)
            except Exception as exc:
                _log(f"  targeted mj_land rejected ({exc}); falling back to gentle descent")
        ok = efm._descend_to_gilly_surface(ctx.conn, ctx.sc, v, bridge=ctx.bridge)
    except Exception as exc:
        return _emit("land", False, "land_error", f"on {body_name}: {exc}")
    ctx.refresh_vessel()
    return _emit("land", bool(ok), "landed" if ok else "land_failed",
                 f"on {body_name}", {"body": body_name})


def ascend(ctx: PrimitiveContext, *, target_alt_km: float = 30.0) -> PrimitiveResult:
    """Ascend from the surface to orbit the CURRENT body. Wraps the proven HLS surface-ascent
    (KrpcFlightController._launch_from_mun via run_hls_surface_sortie's ascent leg), body-agnostic via the
    live body parameters."""
    if ctx.dry_run:
        return _emit("ascend", True, "ascend_planned",
                     f"(dry-run) ascend to {target_alt_km:.0f} km orbit of current body")
    v = ctx.refresh_vessel()
    body_name = ctx.current_body
    try:
        from ksp_lab.flight_controller import KrpcFlightController
        ctrl = KrpcFlightController(ctx.cfg["krpc"])
        # The proven ascent lives inside run_hls_surface_sortie; reuse its _launch_from_mun leg directly so
        # we ascend WITHOUT re-landing. It is body-agnostic (reads gravity/target from the live body).
        start = time.monotonic()
        from ksp_lab.telemetry import TelemetryRecorder
        rec = TelemetryRecorder(Path("runs") / f"ascend-{ctx.vessel_name or 'craft'}.jsonl")
        ok = ctrl._launch_from_mun(ctx.conn, v, rec, start, int(ctx.cfg.get("runner", {}).get("flight_timeout_s", 900)))
    except Exception as exc:
        return _emit("ascend", False, "ascend_error", f"from {body_name}: {exc}")
    ctx.refresh_vessel()
    return _emit("ascend", bool(ok), "ascended_to_orbit" if ok else "ascend_failed",
                 f"to orbit of {body_name}", {"body": body_name})


def plant_flag(ctx: PrimitiveContext) -> PrimitiveResult:
    """EVA a kerbal, plant the stock flag, board back. Wraps bridge.eva_flag then bridge.eva_board.

    Uses the precise EVA/personnel layer (bridge.eva_status) to VERIFY the vessel is actually on the
    surface (landed/splashed) before planting — a flag plant fails in flight/orbit and would strand the
    kerbal — and to CONFIRM the kerbal re-boarded afterward (no kerbal left on EVA). The current vessel
    must be LANDED/SPLASHED with crew."""
    if ctx.dry_run:
        return _emit("plant_flag", True, "flag_planned", "(dry-run) EVA + plant flag + board back")
    v = ctx.refresh_vessel()
    try:
        before = int(v.crew_count)
    except Exception:
        before = -1
    try:
        rr = ctx.bridge.eva_flag()
        _log(f"  eva_flag: {rr.get('flagMethod', rr)}")
    except Exception as exc:
        return _emit("plant_flag", False, "flag_error", f"eva_flag failed: {exc}")
    # Precise-layer check: the kerbal is now on EVA — confirm we are on the surface (a flag is only valid
    # landed/splashed). Best-effort: a read failure must not abort an otherwise-good plant.
    try:
        st = ctx.bridge.eva_status()
        if st.get("onEva") and not (st.get("landed") or st.get("splashed")):
            _log("  WARNING eva_status: kerbal not landed/splashed during flag plant")
    except Exception as exc:
        _log(f"  eva_status read skipped: {exc}")
    time.sleep(3)
    try:
        rb = ctx.bridge.eva_board()
        _log(f"  eva_board: {rb.get('message', rb)}")
    except Exception as exc:
        return _emit("plant_flag", False, "flag_board_error", f"eva_board failed (kerbal stranded on EVA): {exc}")
    time.sleep(3)
    # Precise-layer confirmation: nobody is still on EVA (the kerbal climbed back aboard).
    still_on_eva = False
    try:
        st2 = ctx.bridge.eva_status()
        still_on_eva = bool(st2.get("onEva"))
    except Exception:
        still_on_eva = False
    ctx.refresh_vessel()
    try:
        after = int(ctx.vessel.crew_count)
    except Exception:
        after = before
    ok = after >= 1 and (before < 0 or after >= before) and not still_on_eva
    marker = "flag_planted" if ok else ("flag_crew_on_eva" if still_on_eva else "flag_crew_not_aboard")
    return _emit("plant_flag", ok, marker,
                 f"crew aboard after board: {after}{' (kerbal STILL on EVA)' if still_on_eva else ''}")


def walk_to(ctx: PrimitiveContext, *, lat: float, lon: float) -> PrimitiveResult:
    """Walk the active EVA kerbal to an absolute surface lat/lon on the CURRENT body. Wraps the precise
    EVA layer (eva_control.walk_kerbal_to), which delegates the move to the bridge's /eva-walk-to
    (KerbalEVA.SetWaypoint) and computes the great-circle distance + bearing from the live body radius —
    exact spherical trig, never a guessed heading. The kerbal must already be on EVA (e.g. after a
    plant_flag EVA, or call the bridge's eva_go first)."""
    if ctx.dry_run:
        return _emit("walk_to", True, "walk_planned",
                     f"(dry-run) walk EVA kerbal to ({lat:.4f}, {lon:.4f}) on current body")
    from ksp_lab import eva_control
    try:
        body_radius_m = float(lookup_body(ctx.current_body).radius_m)
    except Exception:
        body_radius_m = None  # type: ignore[assignment]
    try:
        res = eva_control.walk_kerbal_to(ctx.bridge, float(lat), float(lon), body_radius_m)
    except Exception as exc:
        return _emit("walk_to", False, "walk_error", f"eva_walk_to failed: {exc}")
    planned = res.get("plannedGeodesic") if isinstance(res, dict) else None
    detail = f"walked to ({lat:.4f}, {lon:.4f})"
    if planned:
        detail += f" (planned {planned.get('distanceM', 0):.0f} m @ {planned.get('bearingDeg', 0):.0f} deg)"
    return _emit("walk_to", True, "walked_to", detail,
                 {"lat": lat, "lon": lon, "planned": planned})


def rendezvous(ctx: PrimitiveContext, *, target_name: str) -> PrimitiveResult:
    """Rendezvous the ACTIVE vessel with a named target. Wraps bridge.mj_rendezvous (MechJeb owns the
    phasing/approach), polling until the rendezvous AP disengages. See tools/fly_mj_dock.py."""
    if ctx.dry_run:
        return _emit("rendezvous", True, "rendezvous_planned", f"(dry-run) rendezvous with {target_name!r}")
    try:
        ctx.bridge.mj_rendezvous(target_name, desired_distance=60.0)
    except Exception as exc:
        return _emit("rendezvous", False, "rendezvous_error", f"mj_rendezvous rejected: {exc}")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1800.0:
        try:
            st = ctx.bridge.mj_status()
        except Exception:
            time.sleep(3); continue
        if not st.get("rvEnabled", False):
            break
        time.sleep(3)
    ctx.target_name = target_name
    return _emit("rendezvous", True, "rendezvous_done", f"closed on {target_name!r}")


def dock(ctx: PrimitiveContext, *, target_name: str) -> PrimitiveResult:
    """Dock the ACTIVE vessel to a named target's port. Wraps bridge.mj_dock (MechJeb aligns + mates),
    polling /mj-status until the part count rises or the port reports Docked. See tools/fly_mj_dock.py."""
    if ctx.dry_run:
        return _emit("dock", True, "dock_planned", f"(dry-run) dock with {target_name!r}")
    v = ctx.refresh_vessel()
    try:
        parts_before = len(v.parts.all)
    except Exception:
        parts_before = 0
    try:
        ctx.bridge.mj_dock(target_name, speed_limit=2.0)
    except Exception as exc:
        return _emit("dock", False, "dock_error", f"mj_dock rejected: {exc}")
    t0 = time.monotonic()
    docked = False
    while time.monotonic() - t0 < 1500.0:
        try:
            st = ctx.bridge.mj_status()
        except Exception:
            time.sleep(3); continue
        pc = st.get("partCount")
        port = st.get("myPortState") or ""
        if (isinstance(pc, (int, float)) and pc > parts_before) or "Docked" in port or "PreAttached" in port:
            docked = True
            break
        time.sleep(3)
    ctx.refresh_vessel()
    return _emit("dock", docked, "docked" if docked else "dock_not_confirmed",
                 f"to {target_name!r}")


def transfer_crew(ctx: PrimitiveContext, *, to_part_or_vessel: str = "") -> PrimitiveResult:
    """Move a kerbal between docked vessels. Wraps bridge.transfer_crew (the /transfer-crew endpoint
    reseats a kerbal into the named docked vessel)."""
    if ctx.dry_run:
        return _emit("transfer_crew", True, "transfer_crew_planned",
                     f"(dry-run) transfer a kerbal to {to_part_or_vessel!r}")
    try:
        rr = ctx.bridge.transfer_crew(to_part_or_vessel)
        ok = bool(rr.get("ok", True))
    except Exception as exc:
        return _emit("transfer_crew", False, "transfer_crew_error", str(exc))
    return _emit("transfer_crew", ok, "crew_transferred" if ok else "transfer_crew_failed",
                 f"to {to_part_or_vessel!r}")


def recover(ctx: PrimitiveContext) -> PrimitiveResult:
    """Descend/aerocapture + chutes + recover the active vessel & crew on the home body. Wraps
    crewed_eve_roundtrip.descend_and_recover (the proven aerobrake + chute + recover sequence)."""
    if ctx.dry_run:
        return _emit("recover", True, "recover_planned", "(dry-run) descend + chutes + recover crew")
    v = ctx.refresh_vessel()
    try:
        import crewed_eve_roundtrip as cer
        # If the craft is still on an interplanetary/return trajectory above the atmosphere, the bespoke
        # return leg owns the aerocapture; here we descend from a craft already inside the home SOI.
        ok = cer.descend_and_recover(ctx.conn, ctx.sc, v)
    except Exception as exc:
        return _emit("recover", False, "recover_error", str(exc))
    return _emit("recover", bool(ok), "recovered" if ok else "recover_failed",
                 "crew down safe and recovered" if ok else "did not recover")


def commission_relay(ctx: PrimitiveContext) -> PrimitiveResult:
    """Bring the current vessel online as a relay: jettison fairing, deploy antenna + solar, set vessel
    type Relay. Wraps deploy_relay.commission."""
    if ctx.dry_run:
        return _emit("commission_relay", True, "commission_planned",
                     "(dry-run) deploy antenna/solar + set type Relay")
    v = ctx.refresh_vessel()
    try:
        import deploy_relay
        deploy_relay.commission(ctx.bridge, v)
    except Exception as exc:
        return _emit("commission_relay", False, "commission_error", str(exc))
    return _emit("commission_relay", True, "relay_commissioned",
                 f"{ctx.vessel_name or 'craft'} forwarding the network")


# ======================================================================================================
# Catalog metadata (shown to the LLM decomposer) + dispatch table.
# ======================================================================================================
@dataclass(slots=True)
class PrimitiveSpec:
    name: str
    fn: Callable[..., PrimitiveResult]
    description: str
    params: dict           # JSON-schema-ish: {arg: {"type": ..., "default": ..., "desc": ...}}
    wraps: str             # the proven function(s) this primitive wraps (for the report + docs)


CATALOG: dict[str, PrimitiveSpec] = {}


def _register(spec: PrimitiveSpec) -> None:
    CATALOG[spec.name] = spec


_register(PrimitiveSpec(
    "select_vessel", select_vessel,
    "Make a vessel active by name, tolerant of KSP localized name suffixes (e.g. Chinese 飞船). "
    "Use to operate on a previously launched/parked craft.",
    {"name": {"type": "str", "desc": "vessel name (prefix/normalized match, not exact)"}},
    "flight_controller._select_vessel (tolerant via vessel_match.vessel_names_match)",
))
_register(PrimitiveSpec(
    "launch", launch,
    "Design and launch a craft to orbit the LAUNCH body (Kerbin in stock). Set crew>0 for a crewed pod, "
    "heatshield/chutes for re-entry, radial_boosters/max_core_engines for a heavy upper.",
    {"target_alt_km": {"type": "float", "default": 100.0, "desc": "parking/target orbit altitude (km)"},
     "crew": {"type": "int", "default": 0, "desc": "kerbals aboard (0 = headless probe)"},
     "payload_t": {"type": "float", "default": 0.3, "desc": "payload mass (t)"},
     "docking": {"type": "bool", "default": False, "desc": "carry a docking port"},
     "heatshield": {"type": "bool", "default": False, "desc": "forward heat shield for re-entry"},
     "chutes": {"type": "bool", "default": False, "desc": "parachutes for landing"},
     "radial_boosters": {"type": "int", "default": 0, "desc": "asparagus strap-on pods (heavy uppers)"},
     "max_core_engines": {"type": "int", "default": 1, "desc": "core-stage engine count"},
     "name": {"type": "str", "default": "AI-Craft", "desc": "craft name"}},
    "deploy_relay.launch_to_lko",
))
_register(PrimitiveSpec(
    "transfer", transfer,
    "Transfer to and capture at ANOTHER body (moon or planet), body-agnostic. capture_mode: 'circular' "
    "(capture + Hohmann to altitude), 'loose' (cheap bound ellipse), 'aerocapture' (low atmospheric "
    "periapsis, the air brakes).",
    {"target_body": {"type": "str", "desc": "destination body (any in bodies.py)"},
     "capture_alt_km": {"type": "float|null", "default": None, "desc": "capture/circular altitude (km); "
                        "default = synchronous (circular) or loose ceiling"},
     "capture_mode": {"type": "enum[loose,circular,aerocapture]", "default": "loose",
                      "desc": "how to capture"}},
    "deploy_relay_transfer.transfer_to_body / transfer_to_mun + crewed_eve_roundtrip.capture_at_eve_loose",
))
_register(PrimitiveSpec(
    "set_orbit", set_orbit,
    "Circularize / Hohmann the current orbit to a target periapsis x apoapsis around the CURRENT body.",
    {"periapsis_km": {"type": "float", "desc": "target periapsis (km)"},
     "apoapsis_km": {"type": "float", "desc": "target apoapsis (km)"}},
    "deploy_relay_transfer._hohmann_to_radius / _circularize_at",
))
_register(PrimitiveSpec(
    "land", land,
    "Land the active vessel on the CURRENT body (gravity/atmosphere read live). Optional target lat/lon.",
    {"target_lat": {"type": "float|null", "default": None, "desc": "target latitude (deg)"},
     "target_lon": {"type": "float|null", "default": None, "desc": "target longitude (deg)"}},
    "bridge.mj_land + eve_flag_mission._descend_to_gilly_surface (gentle fallback)",
))
_register(PrimitiveSpec(
    "ascend", ascend,
    "Ascend from the surface to orbit the CURRENT body.",
    {"target_alt_km": {"type": "float", "default": 30.0, "desc": "target orbit altitude (km)"}},
    "flight_controller._launch_from_mun (body-agnostic surface ascent)",
))
_register(PrimitiveSpec(
    "plant_flag", plant_flag,
    "EVA a kerbal from a landed vessel, plant the stock flag, and board them back (verifies the vessel is "
    "landed/splashed before planting and that the kerbal re-boarded, via the precise eva_status layer).",
    {},
    "bridge.eva_flag + bridge.eva_board + bridge.eva_status",
))
_register(PrimitiveSpec(
    "walk_to", walk_to,
    "Walk the active EVA kerbal to an absolute surface lat/lon on the CURRENT body (precise great-circle "
    "move via the stock waypoint engine). The kerbal must already be on EVA.",
    {"lat": {"type": "float", "desc": "destination latitude (deg)"},
     "lon": {"type": "float", "desc": "destination longitude (deg)"}},
    "eva_control.walk_kerbal_to (bridge.eva_walk_to + bridge.eva_status)",
))
_register(PrimitiveSpec(
    "rendezvous", rendezvous,
    "Rendezvous the active vessel with a named target vessel (MechJeb owns the phasing/approach).",
    {"target_name": {"type": "str", "desc": "the target vessel's name"}},
    "bridge.mj_rendezvous",
))
_register(PrimitiveSpec(
    "dock", dock,
    "Dock the active vessel to a named target vessel's port (MechJeb aligns + mates).",
    {"target_name": {"type": "str", "desc": "the target vessel's name"}},
    "bridge.mj_dock",
))
_register(PrimitiveSpec(
    "transfer_crew", transfer_crew,
    "Move a kerbal between docked vessels.",
    {"to_part_or_vessel": {"type": "str", "default": "", "desc": "destination vessel/part name"}},
    "bridge.transfer_crew (/transfer-crew)",
))
_register(PrimitiveSpec(
    "recover", recover,
    "Descend (chutes/aerocapture) and recover the active vessel and its crew on the home body.",
    {},
    "crewed_eve_roundtrip.descend_and_recover",
))
_register(PrimitiveSpec(
    "commission_relay", commission_relay,
    "Bring the current vessel online as a comms relay: deploy antenna + solar, set vessel type Relay.",
    {},
    "deploy_relay.commission",
))


def run_primitive(ctx: PrimitiveContext, name: str, args: dict | None = None) -> PrimitiveResult:
    """Dispatch a single primitive by name with its args, against the live context."""
    spec = CATALOG.get(name)
    if spec is None:
        return _emit(name, False, "unknown_primitive", f"no primitive named {name!r}")
    try:
        return spec.fn(ctx, **(args or {}))
    except TypeError as exc:
        return _emit(name, False, "bad_args", f"{name}({args}): {exc}")


def catalog_for_prompt() -> list[dict]:
    """The catalog as plain dicts (name + description + params + wraps) for the LLM decomposer prompt."""
    return [
        {"primitive": s.name, "description": s.description, "params": s.params, "wraps": s.wraps}
        for s in CATALOG.values()
    ]
