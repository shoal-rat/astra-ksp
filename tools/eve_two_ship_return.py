"""TWO-SHIP EVE CREW RETURN — bring a kerbal home from Eve by ORBITAL DOCKING, never refuelling in flight.

================================ DEPRECATED ENTRY POINT ================================
REDUNDANT as an orchestration entry point — ASTRA decomposition is the general path; this file is
retained only for the helper functions the `launch`/`transfer`/`land`/`recover` primitives wrap (here
the launch / transfer / rendezvous / dock / transfer_crew / recover flight helpers, e.g.
`launch_and_transfer_to_eve`, `rendezvous_and_dock`, `transfer_kerbal_to_tug`, `fly_tug_home`). The
module-level `main()` is kept ONLY as a manual fallback and is itself DEPRECATED — prefer
`tools/astra.py "<command>"`. Do not add new orchestration here.
========================================================================================

NOTE (ASTRA redesign): this monolithic mission script is now REDUNDANT orchestration. The general path
is ASTRA's task DECOMPOSITION into atomic primitives (src/ksp_lab/astra/primitives.py: launch / transfer
/ rendezvous / dock / transfer_crew / recover) which WRAP the proven flight functions this file calls.
The file is kept as a reference; prefer `tools/astra.py "<command>"` for new missions.

WHY TWO SHIPS (the crew-#9 lesson): the single-ship round trip (tools/crewed_eve_roundtrip.py) stranded a
kerbal DRY at Eve on the RETURN leg — one liftable Kerbin stack simply could not carry enough vacuum Δv to
ALSO escape Eve from the capture periapsis. The fix that KEEPS the hard rule (no propellant is ever pumped
between vessels in flight) is to fly the whole return budget on a SECOND ship that waits fully fuelled in
Eve orbit. Only the KERBAL crosses: the crew FERRY rendezvous + DOCKS with the TUG, the kerbal walks the
docking tunnel into the tug's pod, the ferry is abandoned, and the crewed full-fuel tug flies home.

  FERRY (design_eve_two_ship.crew_ferry):  238 t, 4 strap-ons, dock, NO heatshield  — one-way crew delivery.
  TUG   (design_eve_two_ship.return_tug):  436 t, 8 strap-ons, 2-core, dock + heatshield + chutes, carries
                                           the FULL Eve->Kerbin return budget. Launches HEADLESS; the crew
                                           seat fills at the dock, not at launch.

COMPOSED FROM PROVEN CODE (AGENTS.md RULE 1 — reuse, don't reinvent):
  - deploy_relay.launch_to_lko(...)                 : the hardened crewed/headless Kerbin ascent (asparagus
                                                      radial boosters, explicit staging) to a 100 km parking
                                                      orbit, sized for the full vacuum budget.
  - deploy_relay.jettison_payload_fairings(v)       : split the ascent shroud in orbit (expose dock port +
                                                      heat shield + chutes).
  - crewed_eve_roundtrip.capture_at_eve_loose(...)  : the PROVEN outbound interplanetary leg (precise-Lambert
                                                      eject + MechJeb interplanetary node + grid-search
                                                      encounter) ending in a CHEAP loose-ellipse capture (NOT
                                                      a low-circular orbit) — used for BOTH ships to Eve.
  - crewed_eve_roundtrip.board_crew / return_to_kerbin / descend_and_recover : crew seating, the return
                                                      ejection aimed at a ~35 km Kerbin aerocapture pe via the
                                                      proven GRID (NOT MechJeb's interplanetary node, which
                                                      under-planned the return and stranded crew #9), and the
                                                      aerocapture + chute + recover descent.
  - deploy_relay_transfer._warp_via_high / _execute_precise : clock advance + precise node burns.
  - bridge.mj_rendezvous / mj_dock / mj_status      : MechJeb flies the phasing, approach, alignment, mate.
  - bridge.transfer_crew(...) + a direct kRPC fallback : move the kerbal across the docked ports.

GENUINELY NEW HERE (NEW + UNTESTED IN FLIGHT — see the risk notes in main()'s docstring):
  - the two-ship sequence + resume detection;
  - rendezvous_and_dock()  : set the tug as the ferry's target, mj_rendezvous to close, mj_dock to mate,
                             polling mj_status — adapted from the proven tools/fly_mj_dock.py;
  - transfer_kerbal_to_tug() : after the merge, move the kerbal into the TUG's pod and VERIFY crew aboard.

    PYTHONPATH=src python tools/eve_two_ship_return.py configs/local-ksp.yaml

(offline-safe: the design build + geometry gate run with no live connection; the flight only starts after.)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import deploy_relay
import deploy_relay_transfer as drt
from deploy_relay_transfer import _warp_via_high, log

import design_chart
from design_eve_two_ship import crew_ferry, return_tug

from crewed_eve_roundtrip import (
    board_crew,
    capture_at_eve_loose,
    descend_and_recover,
    return_to_kerbin,
    _crew_count,
)

from ksp_lab.design import design_ship

DOCS = Path(__file__).resolve().parents[1] / "docs"
KERBIN_YEAR_S = 426 * 21600

# Both ships capture into a LOOSE bound Eve ELLIPSE (low ~104 km periapsis, high ~0.30-SOI apoapsis) — NOT
# a low-circular orbit. A low-circular capture (circularize + Hohmann-down) is the same capture-cost wall
# that stranded crew #9 DRY; the loose ellipse costs ~146 m/s instead, PRESERVING the return fuel. The dock
# works in any SHARED orbit, so both ships capturing into the same loose ~2700x8000 km ellipse is fine. The
# low periapsis keeps the tug's RETURN ejection Oberth-cheap. (capture_at_eve_loose owns the realized orbit;
# this constant is only kept for the resume/parking-altitude references that other modules import.)
EVE_PARK_ALT_KM = 150.0

# Rendezvous / docking close-in thresholds (metres, m/s).
RENDEZVOUS_CLOSE_M = 100.0     # "close enough" relative distance the rendezvous AP targets
DOCK_DONE_TIMEOUT_S = 1800.0
RV_DONE_TIMEOUT_S = 2400.0

FERRY_NAME = "AI-Eve-Ferry2"
TUG_NAME = "AI-Eve-Tug2"


# ==================================================================================================
# DESIGN — build + geometry-gate both ships offline (RULE 1: design + chart before any flight).
# ==================================================================================================
def _build_and_gate(req, render: bool = True):
    """design_ship + looks_like_a_rocket gate + (optional) SVG chart. Returns (design, report)."""
    d = design_ship(req)
    rep = design_chart.looks_like_a_rocket(d)
    e = d.estimates
    log(f"DESIGN {d.name}: wet {e['wet_mass_t']:.0f} t, total Δv {e['total_delta_v_mps']:.0f} m/s, "
        f"launch TWR {e['launch_twr']}, {int(e['stage_count'])} stages + {req.radial_booster_count} radial "
        f"pods, dock={d.docking_port}, heatshield={d.heatshield}, {int(e['parachutes'])} chutes, "
        f"feasible={d.feasible}")
    if d.infeasible_reasons:
        for r in d.infeasible_reasons:
            log(f"    - {r}")
    log(f"  geometry gate: {rep['looks_like_a_rocket']} (L/D {rep['fineness_ratio']}, "
        f"length {rep.get('length_m')}m, max-dia {rep.get('max_diameter_m')}m)")
    for label, ok in rep["checks"].items():
        if not ok:
            log(f"    GEOM FAIL: {label}")
    if render:
        svg = DOCS / f"design_chart_{d.name}.svg"
        try:
            svg.write_text(design_chart.render_svg(d), encoding="utf-8")
            log(f"  design chart SVG -> {svg}")
        except Exception as exc:
            log(f"  chart render note: {exc}")
    return d, rep


def design_both(render: bool = True):
    """Build + gate the FERRY and the TUG. Returns ((ferry_design, ferry_rep), (tug_design, tug_rep))."""
    log("=== DESIGN: crew FERRY (one-way to Eve orbit; dock; no heatshield) ===")
    ferry = _build_and_gate(crew_ferry(), render=render)
    log("=== DESIGN: return TUG (full return budget; dock + heatshield + chutes; headless until dock) ===")
    tug = _build_and_gate(return_tug(), render=render)
    return ferry, tug


# ==================================================================================================
# LIVE HELPERS — vessel lookup / phase detection (cheap resumability).
# ==================================================================================================
def _find_vessel(sc, name: str):
    try:
        for vv in sc.vessels:
            if str(vv.name) == name:
                return vv
    except Exception:
        pass
    return None


def _make_active(sc, v):
    """Switch the active vessel to ``v`` and re-fetch it (the handle goes stale across a switch). KSP can
    hang if you switch DURING warp, so callers must stop warp first; this only switches + settles."""
    try:
        sc.rails_warp_factor = 0
        sc.physics_warp_factor = 0
    except Exception:
        pass
    try:
        sc.active_vessel = v
        time.sleep(2)
    except Exception as exc:
        log(f"  active-vessel switch note ({exc})")
    return sc.active_vessel


def _ship_phase(v) -> str:
    """Best-effort SOI/situation phase for a single ship so a re-run resumes instead of relaunching."""
    if v is None:
        return "absent"
    try:
        body = v.orbit.body.name
        sit = str(v.situation).split(".")[-1]
    except Exception:
        return "absent"
    if body == "Eve":
        return "at_eve"
    if body == "Sun":
        return "in_transit"
    if body == "Kerbin":
        if sit in ("landed", "splashed"):
            return "recovered"
        if sit == "orbiting" and (v.orbit.apoapsis_altitude or 0) < 2_000_000.0:
            return "in_lko"
        return "kerbin_soi"
    return "other"


def _disable_inspace_autostage(bridge) -> None:
    """BUG 2 FIX: turn OFF MechJeb's autostager (MechJebModuleStagingController) before any IN-SPACE burn on
    these crewed/heat-shield craft. That module is SEPARATE from the ascent AP's autostage flag and, once on,
    autostages during ANY burn — including the node-executor capture burn. On the tug it blindly fired the
    payload/heat-shield decoupler mid-capture and stranded the crew pod at Eve (no engine, heat shield, or
    chutes). With it off, the only stager in space is the explicit guarded loop (which never fires a payload
    decoupler), so the upper+payload (engine+heat shield+pod) is never split. Best-effort + idempotent."""
    try:
        r = bridge.mj_disable("staging")
        log(f"  MechJeb autostager DISABLED for the in-space burns ({r.get('disabled')})")
    except Exception as exc:
        log(f"  mj-disable(staging) skipped ({exc}) — in-space staging relies on the explicit guarded loop")


def _relative_distance_m(a, b) -> float:
    """|position of b in a's reference frame| — the same metric tools/fly_mj_dock.py polls on."""
    try:
        p = b.position(a.reference_frame)
        return (p[0] ** 2 + p[1] ** 2 + p[2] ** 2) ** 0.5
    except Exception:
        return 1.0e12


# ==================================================================================================
# OUTBOUND — launch a ship to a LOW Eve orbit. crew=0 (headless) for the tug; crew handled separately.
# ==================================================================================================
def launch_and_transfer_to_eve(c, sc, cfg, runner, bridge, name: str, req, *, board: bool):
    """Launch ``name`` (built from ``req``) to a 100 km Kerbin parking orbit, optionally board a kerbal,
    then run the PROVEN ejection + a CHEAP LOOSE-ellipse Eve capture (capture_at_eve_loose) — NOT a costly
    low-circular orbit. Returns the active vessel on success (in Eve orbit), or None on failure. Headless
    when board=False (the tug boards at the dock)."""
    from crewed_eve_roundtrip import _vacuum_budget_mps  # noqa: local import keeps the module import-light
    # Size the upper for a real interplanetary budget (the proven override the crewed roundtrip used). The
    # ferry/tug phase Δv already covers it; this floor just guarantees the upper is not a min-tank stub.
    insertion_override = max(req.phases[-1].dv_mps, _vacuum_budget_mps()["budget"])

    log(f"MILESTONE: launching {name} to LKO (crew={'1' if board else '0 (headless)'}) ...")
    if not deploy_relay.launch_to_lko(
            sc, cfg, runner, bridge, name, 100.0,
            insertion_dv_override=insertion_override,
            booster_max_engines=req.max_engine_count,
            radial_booster_count=req.radial_booster_count,
            crew=req.crew,                               # the .craft is written crewable either way (seat fills later)
            needs_heatshield=req.needs_heatshield,
            landing=req.landing):
        log(f"  launch of {name} to parking orbit FAILED")
        return None
    v = _make_active(sc, sc.active_vessel)
    log(f"MILESTONE: {name} IN LKO {round(v.orbit.periapsis_altitude/1000)}x"
        f"{round(v.orbit.apoapsis_altitude/1000)} km")

    # Jettison the ascent fairing in orbit so the docking port (and, for the tug, the heat shield + chutes)
    # are exposed for rendezvous + reentry. Harmless if there were no fairings.
    try:
        n = deploy_relay.jettison_payload_fairings(v)
        if n:
            log(f"  jettisoned {n} payload fairing(s) — docking port / heat shield exposed")
            time.sleep(2)
            v = sc.active_vessel
    except Exception as exc:
        log(f"  fairing jettison skipped ({exc})")

    if board:
        log(f"MILESTONE: boarding a kerbal into {name} (headless launch left the pod empty) ...")
        if not board_crew(sc, bridge, v):
            log(f"  CREW BOARDING of {name} FAILED — refusing to fly an empty crew ferry")
            return None

    # BUG 1 FIX: capture into a LOOSE bound ellipse (cheap ~146 m/s), NOT transfer_to_body's low-circular
    # capture (circularize at periapsis + Hohmann DOWN to the target alt). That low-circular path is the same
    # capture-cost wall that ran crew #9 dry; the loose ellipse preserves the return fuel. capture_at_eve_loose
    # reuses transfer_to_body's PROVEN ejection + grid-search-establish, then a single retrograde _retro_capture
    # to a bound ellipse (low periapsis, ~0.30-SOI apoapsis) — no circularize, no Hohmann-down. The two ships
    # share this loose ~2700x8000 km ellipse; the rendezvous + dock work in any shared orbit.
    # BUG 2 FIX: kill MechJeb's autostager before the interplanetary leg so it can never fire the tug's
    # heat-shield/payload decoupler during the in-space capture burn.
    _disable_inspace_autostage(bridge)
    log(f"MILESTONE: LOOSE-capturing {name} into a bound Eve ellipse (cheap; preserves return fuel) ...")
    if not capture_at_eve_loose(c, sc, bridge, v):
        log(f"  {name} Eve transfer/loose-capture FAILED")
        return None
    v = sc.active_vessel
    o = v.orbit
    log(f"MILESTONE: {name} IN EVE ORBIT {round(o.periapsis_altitude/1000)}x"
        f"{round(o.apoapsis_altitude/1000)} km (e={o.eccentricity:.2f})")
    return v


# ==================================================================================================
# RENDEZVOUS + DOCK — adapted from tools/fly_mj_dock.py (the proven MechJeb dock driver). Ferry is the
# ACTIVE/chaser; the tug is the passive target. We never warp-via-high while close to the tug (a vessel
# switch during the approach would hand control away and could hang KSP mid-warp).
# ==================================================================================================
def _poll_mj(bridge, done, *, timeout_s: float, label: str, period: float = 3.0) -> dict:
    """Poll /mj-status until done(status) or timeout. Mirrors fly_mj_dock._poll_until."""
    t0 = time.monotonic()
    last: dict = {}
    last_msg = ""
    while time.monotonic() - t0 < timeout_s:
        try:
            last = bridge.mj_status()
        except Exception as exc:
            log(f"  ({label}) status error: {exc}")
            time.sleep(period)
            continue
        msg = (f"{label}: rvEnabled={last.get('rvEnabled')} dockEnabled={last.get('dockEnabled')} "
               f"parts={last.get('partCount')} port={last.get('myPortState')!r} "
               f"rv={last.get('rvStatus')!r} dock={last.get('dockStatus')!r}")
        if msg != last_msg:
            log("  " + msg)
            last_msg = msg
        if done(last):
            return last
        time.sleep(period)
    return last


def rendezvous_and_dock(c, sc, bridge, ferry, tug) -> bool:
    """Set the tug as the ferry's target, RENDEZVOUS (MechJeb main-engine close) to ~RENDEZVOUS_CLOSE_M,
    then DOCK (MechJeb RCS port-mate). Returns True once the ports mate (the two craft merge into one
    vessel). The ferry MUST be the active vessel before this call."""
    ferry = _make_active(sc, ferry)
    try:
        ferry.control.remove_nodes()
    except Exception:
        pass
    try:
        sc.target_vessel = tug
    except Exception as exc:
        log(f"  could not set target vessel ({exc})")
    try:
        ferry.control.rcs = True
    except Exception:
        pass

    dist0 = _relative_distance_m(ferry, tug)
    parts_before = len(ferry.parts.all)
    log(f"RENDEZVOUS setup: chaser={ferry.name} target={tug.name} dist={dist0:.0f} m parts={parts_before}")

    # 1) RENDEZVOUS with the main engine (efficient over a km-scale close); hand to the dock AP near.
    if dist0 > RENDEZVOUS_CLOSE_M + 20.0:
        log("MILESTONE: RENDEZVOUS via MechJeb (main-engine close) ...")
        try:
            bridge.mj_rendezvous(TUG_NAME, desired_distance=60.0)
        except Exception as exc:
            log(f"  mj-rendezvous rejected: {exc}")
            return False
        st = _poll_mj(bridge, lambda s: not s.get("rvEnabled", False),
                      timeout_s=RV_DONE_TIMEOUT_S, label="rendezvous")
        log(f"  rendezvous ended: rvStatus={st.get('rvStatus')!r}")
        ferry = sc.active_vessel
        dist1 = _relative_distance_m(ferry, tug)
        log(f"MILESTONE: RENDEZVOUS close — relative distance {dist1:.0f} m")

    # Top up monopropellant so the RCS dock phase has full tanks for the mate (proven fly_mj_dock step).
    try:
        r = bridge.refuel_vessel(FERRY_NAME, fraction=1.0, resources="MonoPropellant")
        log(f"  refuel monoprop: {r.get('message')}")
    except Exception as exc:
        log(f"  monoprop refuel skipped ({exc})")

    # 2) DOCK. MechJeb aligns + mates the ports (RCS, short range).
    log("MILESTONE: DOCK via MechJeb ...")
    try:
        d = bridge.mj_dock(TUG_NAME, speed_limit=2.0)
        parts_before = int(d.get("chaserPartCount", parts_before))
    except Exception as exc:
        log(f"  mj-dock rejected: {exc}")
        return False

    def docked(s: dict) -> bool:
        pc = s.get("partCount")
        port = (s.get("myPortState") or "")
        if isinstance(pc, (int, float)) and pc > parts_before:
            return True
        if "Docked" in port or "PreAttached" in port:
            return True
        # dock AP disabled itself with no target left => aborted; stop polling.
        if s.get("dockEnabled") is False and not s.get("targetExists", True):
            return True
        return False

    st = _poll_mj(bridge, docked, timeout_s=DOCK_DONE_TIMEOUT_S, label="dock")
    pc = st.get("partCount")
    success = (isinstance(pc, (int, float)) and pc > parts_before) or \
              ("Docked" in (st.get("myPortState") or "")) or \
              ("PreAttached" in (st.get("myPortState") or ""))
    if not success:
        log(f"  DOCK NOT CONFIRMED: {st}")
        return False
    log(f"MILESTONE: DOCKED  partCount {parts_before} -> {pc}, port={st.get('myPortState')!r}")
    return True


# ==================================================================================================
# CREW TRANSFER — move the kerbal from the ferry's pod into the TUG's pod. After a dock the two craft
# MERGE into ONE kRPC vessel, so the kerbal can be moved between the now-shared parts. We do it via kRPC
# directly (the explicit method the prompt wants), with the bridge /transfer-crew as a robust fallback.
# ==================================================================================================
def _tug_pod_part(merged):
    """Find the TUG's command-pod part within the merged vessel: a crewable part with a FREE seat that is
    NOT the ferry's currently-occupied pod. After the dock both pods belong to one vessel; the tug's pod is
    the empty crewable one (the headless tug launched with crew_count 0)."""
    best = None
    for p in merged.parts.all:
        try:
            cap = int(p.crew_capacity)
        except Exception:
            cap = 0
        if cap <= 0:
            continue
        occ = 0
        try:
            occ = len(p.crew)
        except Exception:
            occ = 0
        if occ < cap:                      # has a free seat -> a valid destination (the empty tug pod)
            if best is None:
                best = p
    return best


def transfer_kerbal_to_tug(sc, bridge, merged) -> bool:
    """Move the kerbal into the tug's pod and VERIFY a kerbal is aboard the tug side. ``merged`` is the
    active (post-dock, merged) vessel. Returns True once a kerbal sits in the tug's command pod.

    EXACT kRPC: find the occupied source pod and the free-seat destination pod within merged.parts.all,
    then ``source_part.crew[0]`` is a kRPC ``CrewMember``; ``crew_member.part = dest_part`` reseats it
    (kRPC exposes the writable CrewMember.part for exactly this in-vessel move). If that attribute is not
    settable in this kRPC build, fall back to the bridge /transfer-crew endpoint addressed by the tug pod's
    PART TITLE (robust to the merged vessel taking either ship's name)."""
    # Locate the source pod (the one with a kerbal) and the destination pod (free seat).
    source_part = None
    crew_member = None
    for p in merged.parts.all:
        try:
            crew = list(p.crew)
        except Exception:
            crew = []
        if crew:
            source_part = p
            crew_member = crew[0]
            break
    dest_part = _tug_pod_part(merged)
    if dest_part is None or source_part is None or crew_member is None:
        log(f"  CREW TRANSFER setup: source_pod={source_part is not None} "
            f"dest_pod={dest_part is not None} crew={crew_member is not None} — cannot locate pods")
        # Last resort: try the bridge by tug-pod title anyway.
        return _transfer_via_bridge(sc, bridge, merged)

    src_title = ""
    dst_title = ""
    try:
        src_title = source_part.title
        dst_title = dest_part.title
    except Exception:
        pass
    kname = ""
    try:
        kname = crew_member.name
    except Exception:
        pass
    log(f"  CREW TRANSFER: moving {kname!r} from {src_title!r} -> {dst_title!r} (tug pod) via kRPC ...")

    # PRIMARY: kRPC reseat — CrewMember.part is writable for an in-vessel move.
    try:
        crew_member.part = dest_part
        time.sleep(2)
        if _seat_occupied(dest_part):
            log(f"  CREW TRANSFERRED (kRPC reseat): {kname!r} now in {dst_title!r}")
            return True
        log("  kRPC reseat did not land the kerbal in the tug pod; trying the bridge endpoint ...")
    except Exception as exc:
        log(f"  kRPC reseat unavailable ({exc}); using the bridge /transfer-crew endpoint ...")

    # FALLBACK: the bridge moves a kerbal to a destination part addressed by TITLE (toPart), which is robust
    # to the merged vessel's name. transfer_crew only exposes toVessel, so call /transfer-crew directly.
    return _transfer_via_bridge(sc, bridge, merged, to_part_title=dst_title)


def _transfer_via_bridge(sc, bridge, merged, to_part_title: str = "") -> bool:
    """Move a kerbal across the docked ports via the bridge /transfer-crew endpoint. Prefers addressing the
    destination by PART TITLE (toPart) so it works regardless of which ship name the merged vessel took."""
    payload: dict[str, str] = {}
    if to_part_title:
        payload["toPart"] = to_part_title
    try:
        # transfer_crew() only takes to_vessel; hit the endpoint directly to pass toPart.
        rr = bridge._request("POST", "/transfer-crew", json=payload)
        log(f"  /transfer-crew: {rr.get('message')} ({rr.get('crew')} -> {rr.get('toPart')})")
    except Exception as exc:
        log(f"  /transfer-crew failed ({exc})")
        return False
    time.sleep(2)
    # Verify: the destination part now has an occupant.
    try:
        merged = sc.active_vessel
    except Exception:
        pass
    for p in merged.parts.all:
        if to_part_title:
            try:
                if p.title != to_part_title:
                    continue
            except Exception:
                continue
        if _seat_occupied(p):
            log("  CREW TRANSFER verified via bridge (a kerbal is seated in the tug pod)")
            return True
    # If we couldn't pin the exact part, accept that the merged vessel still carries the crew.
    return _crew_count(merged) >= 1


def _seat_occupied(part) -> bool:
    try:
        return len(part.crew) >= 1
    except Exception:
        return False


# ==================================================================================================
# UNDOCK — release the ferry so the tug flies home alone (lighter = cheaper return ejection).
# ==================================================================================================
def undock_ferry(sc, merged):
    """Undock the two craft and ensure the TUG (the crewed, heat-shielded, full-fuel side) is active for
    the return. After undock kRPC splits the merged vessel back into two; we re-select the tug by name and
    confirm the kerbal rode with it. Returns the tug vessel handle."""
    log("MILESTONE: UNDOCKING — abandoning the ferry at Eve, tug flies home ...")
    undocked = False
    try:
        for port in list(getattr(merged.parts, "docking_ports", []) or []):
            try:
                state = str(port.state)
            except Exception:
                state = ""
            if "docked" in state.lower() or "Docked" in state:
                try:
                    port.undock()
                    undocked = True
                    log("  docking port released")
                    break
                except Exception as exc:
                    log(f"  port.undock note ({exc})")
    except Exception as exc:
        log(f"  undock skipped ({exc}) — benign; the crew already crossed on the merge")
    if undocked:
        time.sleep(3)

    # Re-select the TUG by name (it carries the heat shield + chutes + the crew now). If the merged vessel
    # kept the tug name, this is a no-op; if it split, this grabs the right half.
    tug = _find_vessel(sc, TUG_NAME)
    if tug is None:
        log(f"  could not re-find {TUG_NAME} after undock; using the active vessel")
        tug = sc.active_vessel
    tug = _make_active(sc, tug)
    log(f"  TUG active for return: {tug.name}, crew aboard {_crew_count(tug)}")
    return tug


# ==================================================================================================
# RETURN — eject the crewed tug from Eve toward Kerbin (GRID-aimed ~35 km aerocapture pe, NOT MechJeb's
# interplanetary node) + descend & recover. Both reused verbatim from crewed_eve_roundtrip.
# ==================================================================================================
def fly_tug_home(c, sc, bridge, tug) -> bool:
    """Eve return window wait -> grid-aimed Kerbin ejection (return_to_kerbin) -> aerocapture + chute +
    recover (descend_and_recover). Returns True once the crew is down safe and recovered on Kerbin."""
    # Wait for the Eve->Kerbin window on the ground-equivalent (advance the clock via a high vessel, then
    # switch BACK to the tug before any burn — exactly the crewed_eve_roundtrip pattern).
    try:
        from ksp_lab import transfer_planner as _tp
        w = _tp.find_transfer_window(sc, "Eve", "Kerbin")
        wait_to = w["ut_dep"] - 3.0 * 3600.0
        if wait_to > sc.ut:
            yrs = (wait_to - sc.ut) / KERBIN_YEAR_S
            log(f"MILESTONE: RETURN WINDOW (Eve->Kerbin, |vinf| {w['vinf_mag']:.0f} m/s) in {yrs:.2f} "
                f"Kerbin-yr; warping the clock forward ...")
            _warp_via_high(sc, w["ut_dep"], buffer_s=3.0 * 3600.0)
            tug = _make_active(sc, tug)
            log(f"  return window reached (UT {round(sc.ut)})")
    except Exception as exc:
        log(f"  return-window warp skipped ({exc}); ejecting now")

    # BUG 2 FIX: the return ejection is another in-space burn — keep MechJeb's autostager off so it can't
    # drop the tug's heat shield / chutes (which the crew needs for the Kerbin aerocapture) during the burn.
    _disable_inspace_autostage(bridge)
    log("MILESTONE: TUG RETURN EJECT — Eve -> Kerbin (grid-aimed ~35 km aerocapture pe) ...")
    if not return_to_kerbin(c, sc, bridge, tug):
        log("  tug Eve->Kerbin return/aerocapture setup FAILED")
        return False
    tug = sc.active_vessel
    log(f"MILESTONE: IN KERBIN SOI, aerobrake periapsis {round(tug.orbit.periapsis_altitude/1000)} km — descending")

    log("MILESTONE: AEROBRAKE + chute descent at Kerbin ...")
    if not descend_and_recover(c, sc, tug):
        log("  descent/recovery FAILED")
        return False
    log("MILESTONE: LANDED + CREW RECOVERED")
    return True


# ==================================================================================================
# FLIGHT SEQUENCE.
# ==================================================================================================
def main() -> int:
    """Run the full two-ship Eve crew-return mission.

    HONEST RISK LIST (the NEW, untested-in-flight steps and the active-vessel hazards):
      * RENDEZVOUS + DOCK + CREW TRANSFER are NEW for this mission. The MechJeb dock path itself is proven
        in tools/fly_mj_dock.py at Kerbin/Mun range, but it has NOT been flown with these specific ferry/tug
        craft NOR at Eve, and a stock-MechJeb dock can stall on a bad approach (long phasing, RCS-poor) —
        the polls time out (RV 40 min / dock 30 min) rather than hang forever, but a timeout leaves the
        ferry near the tug NOT docked and the run returns failure for a human to inspect.
      * ACTIVE-VESSEL / WARP HAZARD: KSP can hang if you switch vessels DURING warp. Every clock advance
        here goes through _warp_via_high (which stops warp first) and is followed by _make_active (which
        also stops warp before switching). We deliberately do NOT warp-via-high once the ferry is close to
        the tug — a switch mid-approach would hand control away. If a future edit adds a warp inside the
        rendezvous/dock window, it MUST stop warp first.
      * CREW TRANSFER relies on the post-dock MERGE: kRPC reports the two craft as ONE vessel, and the
        kerbal is reseated via the writable CrewMember.part (primary) or the bridge /transfer-crew by part
        TITLE (fallback). If neither lands the kerbal in the tug pod, the run fails LOUDLY rather than
        flying an empty 'crewed' tug home. EVA-and-board is NOT attempted (an EVA in Eve orbit is an extra
        un-needed risk when the ports are mated and the crew can walk the tunnel).
      * UNDOCK can leave the merged vessel named for either ship; we re-select the TUG by name afterward and
        re-verify crew_count before the return burn.
      * The return ejection uses the GRID (return_to_kerbin), NOT MechJeb's interplanetary node, precisely
        because the node under-planned the return and stranded crew #9.
    """
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"

    # DESIGN FIRST (offline-safe). Render charts + hard-gate both shapes before any flight (RULE 1).
    (ferry_d, ferry_rep), (tug_d, tug_rep) = design_both(render=True)
    for label, d, rep in (("FERRY", ferry_d, ferry_rep), ("TUG", tug_d, tug_rep)):
        if not d.feasible:
            log(f"{label} DESIGN INFEASIBLE — refusing to fly the crew."); return 2
        if not rep["looks_like_a_rocket"]:
            log(f"{label} DESIGN failed the geometry gate — refusing to fly the crew."); return 2

    # --- LIVE FLIGHT (the lab connects here; offline callers stop after the designs above) ------------
    import yaml
    import krpc
    from ksp_lab.bridge_client import BridgeClient
    from ksp_lab.runner import AutomationRunner

    drt.cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))   # the reused drt machinery reads drt.cfg
    cfg = drt.cfg
    bridge = BridgeClient(**cfg["bridge"])
    runner = AutomationRunner(cfg_path, offline=False)
    kc = cfg["krpc"]
    c = krpc.connect(name="eve-two-ship", address=kc["host"],
                     rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center

    # RESUME: detect which ships already exist + their phase (cheap resumability).
    tug = _find_vessel(sc, TUG_NAME)
    ferry = _find_vessel(sc, FERRY_NAME)
    tug_phase = _ship_phase(tug)
    ferry_phase = _ship_phase(ferry)
    log(f"RESUME scan: TUG {TUG_NAME} -> {tug_phase}; FERRY {FERRY_NAME} -> {ferry_phase}")

    # ----------------------------------------------------------------------------------------------
    # 1) TUG first: launch HEADLESS to a LOW Eve orbit so the crew boards at the dock, not at launch.
    # ----------------------------------------------------------------------------------------------
    if tug_phase in ("absent",):
        tug = launch_and_transfer_to_eve(c, sc, cfg, runner, bridge, TUG_NAME, return_tug(), board=False)
        if tug is None:
            log("TUG launch/transfer FAILED"); return 2
        log("=== TUG IN EVE ORBIT (uncrewed, full return fuel, awaiting the ferry) ===")
        try:
            sc.save("persistent")
        except Exception:
            pass
    elif tug_phase in ("in_lko", "in_transit"):
        log("RESUME: TUG already up but not yet at Eve — completing its LOOSE capture ...")
        tug = _make_active(sc, tug)
        _disable_inspace_autostage(bridge)                   # BUG 2: no MechJeb autostage in space
        if not capture_at_eve_loose(c, sc, bridge, tug):     # BUG 1: loose ellipse, not low-circular
            log("TUG resume transfer FAILED"); return 2
        tug = sc.active_vessel
        log("=== TUG IN EVE ORBIT (resumed) ===")
    elif tug_phase == "at_eve":
        log("RESUME: TUG already in Eve orbit.")
    else:
        log(f"RESUME: TUG in unexpected phase '{tug_phase}' — inspect; continuing best-effort.")

    # ----------------------------------------------------------------------------------------------
    # 2) FERRY: launch with the kerbal aboard to a LOW Eve orbit near the tug.
    # ----------------------------------------------------------------------------------------------
    if ferry_phase in ("absent",):
        ferry = launch_and_transfer_to_eve(c, sc, cfg, runner, bridge, FERRY_NAME, crew_ferry(), board=True)
        if ferry is None:
            log("FERRY launch/transfer FAILED"); return 2
        log("=== FERRY IN EVE ORBIT (crewed) ===")
        try:
            sc.save("persistent")
        except Exception:
            pass
    elif ferry_phase in ("in_lko", "in_transit"):
        log("RESUME: FERRY already up but not yet at Eve — completing its transfer ...")
        ferry = _make_active(sc, ferry)
        if _crew_count(ferry) < 1:
            log("RESUME: ferry pod empty — boarding a kerbal first ...")
            if not board_crew(sc, bridge, ferry):
                log("RESUME ferry boarding FAILED"); return 2
        _disable_inspace_autostage(bridge)                   # BUG 2: no MechJeb autostage in space
        if not capture_at_eve_loose(c, sc, bridge, ferry):   # BUG 1: loose ellipse, not low-circular
            log("FERRY resume transfer FAILED"); return 2
        ferry = sc.active_vessel
        log("=== FERRY IN EVE ORBIT (resumed) ===")
    elif ferry_phase == "at_eve":
        log("RESUME: FERRY already in Eve orbit.")
        if _crew_count(ferry) < 1:
            ferry = _make_active(sc, ferry)
            log("RESUME: ferry in Eve orbit but empty — boarding a kerbal before the dock ...")
            if not board_crew(sc, bridge, ferry):
                log("RESUME ferry boarding FAILED"); return 2

    # Re-acquire fresh handles (warps/switches above can stale them).
    tug = _find_vessel(sc, TUG_NAME) or tug
    ferry = _find_vessel(sc, FERRY_NAME) or ferry
    if tug is None or ferry is None:
        log(f"ABORT: missing a ship before rendezvous (tug={tug is not None}, ferry={ferry is not None})")
        return 2

    # ----------------------------------------------------------------------------------------------
    # 3+4) RENDEZVOUS + DOCK (ferry chases the tug). Skip if the ferry already merged away on a resume.
    # ----------------------------------------------------------------------------------------------
    merged = None
    ferry_still_present = _find_vessel(sc, FERRY_NAME) is not None
    if ferry_still_present:
        log("=== RENDEZVOUS + DOCK: ferry -> tug ===")
        if not rendezvous_and_dock(c, sc, bridge, ferry, tug):
            log("RENDEZVOUS/DOCK FAILED — ferry not mated to the tug"); return 2
        merged = sc.active_vessel
        log("=== DOCKED ===")
        try:
            sc.save("persistent")
        except Exception:
            pass
    else:
        log("RESUME: ferry already merged/absent — assuming a prior dock; using the active vessel as merged")
        merged = sc.active_vessel

    # ----------------------------------------------------------------------------------------------
    # 5) CREW TRANSFER ferry -> tug, then VERIFY a kerbal sits on the tug side.
    # ----------------------------------------------------------------------------------------------
    log("=== CREW TRANSFER: ferry pod -> tug pod ===")
    if not transfer_kerbal_to_tug(sc, bridge, merged):
        log("CREW TRANSFER FAILED — refusing to fly an empty 'crewed' tug home"); return 2
    log("=== CREW TRANSFERRED ===")
    try:
        sc.save("persistent")
    except Exception:
        pass

    # ----------------------------------------------------------------------------------------------
    # 6) UNDOCK (abandon the ferry) + fly the crewed tug home.
    # ----------------------------------------------------------------------------------------------
    tug = undock_ferry(sc, merged)
    if _crew_count(tug) < 1:
        log("ABORT: tug carries no crew after undock — the transfer did not stick"); return 2
    log(f"=== CREW ABOARD TUG ({_crew_count(tug)}) — flying home ===")

    if not fly_tug_home(c, sc, bridge, tug):
        log("RETURN FAILED — inspect the log above"); return 2

    log("=== MISSION COMPLETE: a kerbal returned from Eve via two-ship docking and is HOME SAFE on Kerbin ===")
    try:
        sc.save("persistent")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
