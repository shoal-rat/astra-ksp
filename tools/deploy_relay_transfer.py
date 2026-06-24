"""Deploy an RA-100 relay to a CIRCULAR orbit around ANOTHER BODY via a transfer — the Mun today,
Ike/Duna on the same skeleton later.

Composed almost entirely from proven code (per the design workflow):
  - launch_to_lko(..., target_alt_km=100): the hardened ascent to a ~100 km Kerbin PARKING orbit (booster
    force-separated, RTG+Z-1k bus so the coast is EC-survivable). Passing 100 km shrinks the insertion
    stage to a ~250 m/s trim, conserving the upper's ~3600 m/s for the transfer.
  - the Mun transfer + retro-capture from mj_to_mun (TMI grid-search node -> MechJeb executes -> warp to
    the Mun SOI -> warp to periapsis -> pure-retrograde capture). The refuel-before-capture CHEAT is NOT
    used here — the bus flies on its own propellant (it has ~2500 m/s of margin over the ~1100 m/s job).
  - commission(): jettison fairing, deploy RA-100 dish + solar, set vessel type = Relay.

All long warps use sc.warp_to (NOT MechJeb autowarp, which STALLS on far nodes).

    PYTHONPATH=src python tools/deploy_relay_transfer.py configs/local-ksp.yaml Mun 750 AI-Mun-Relay
"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import krpc
import yaml

from ksp_lab.bridge_client import BridgeClient
from ksp_lab.runner import AutomationRunner
from ksp_lab.flight_controller import KrpcFlightController
from ksp_lab.telemetry import TelemetryRecorder

import deploy_relay
from mj_to_mun import _retro_capture, _wait_node_done


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _incl_log(v, tag: str) -> None:
    """Log the orbit inclination (deg) to locate where a heliocentric plane error enters the chain.
    Around Kerbin (0 axial tilt, equatorial LKO == the ecliptic) this should stay ~0 through the in-plane
    raise/lower; a jump means that phase tilts the orbit and carries through to the Duna miss."""
    try:
        log(f"  [DIAG] {tag}: incl {math.degrees(v.orbit.inclination):.2f} deg (around {v.orbit.body.name})")
    except Exception:
        pass


def _predicted_periapsis_at(v, body_name: str):
    """The predicted periapsis ALTITUDE (m) at the future encounter with body_name, walking the patched
    conics, or None if there is no such encounter yet. Used to catch a sub-surface (impact) closest
    approach BEFORE warping into the SOI."""
    try:
        o = v.orbit
        for _ in range(6):
            nxt = o.next_orbit
            if nxt is None:
                return None
            if nxt.body.name == body_name:
                return nxt.periapsis_altitude
            o = nxt
    except Exception:
        return None
    return None


def _circularize_at_apoapsis(conn, sc, v, max_s: float = 200.0) -> None:
    """Warp to apoapsis, then burn PROGRADE (autopilot in the body's non-rotating frame, tracking the
    velocity vector — a basic probe core can't hold SAS prograde, and MechJeb's node executor stalls on a
    far node) until the orbit is ~circular. The RTG keeps it controllable through any eclipse."""
    o = v.orbit
    ttap = o.time_to_apoapsis
    if ttap and 0 < ttap < 1e7:
        log(f"  warping {ttap:.0f}s to apoapsis to circularise ...")
        sc.warp_to(sc.ut + ttap - 20.0)
        time.sleep(2)
    ref = v.orbit.body.non_rotating_reference_frame
    ap = v.auto_pilot
    ap.reference_frame = ref
    ap.target_direction = v.velocity(ref)
    ap.engage()
    time.sleep(6)
    v.control.throttle = 1.0
    t0 = time.monotonic()
    best = 9.0
    last = ""
    while time.monotonic() - t0 < max_s:
        ap.target_direction = v.velocity(ref)        # keep tracking prograde
        ecc = v.orbit.eccentricity
        pe = v.orbit.periapsis_altitude
        apo = v.orbit.apoapsis_altitude
        m = f"circularise: pe {pe/1000:.0f}k ap {apo/1000:.0f}k ecc {ecc:.3f}"
        if m != last:
            log("  " + m); last = m
        if ecc < 0.02 or pe > apo * 0.97 or ecc > best + 0.004:   # circular, or ecc bottomed out
            break
        best = min(best, ecc)
        time.sleep(0.4)
    v.control.throttle = 0.0
    try:
        ap.disengage()
    except Exception:
        pass
    time.sleep(1)


def _raise_apoapsis(sc, v, target_ap_m: float, max_s: float = 320.0) -> None:
    """Burn PROGRADE to raise the apoapsis to ~target_ap_m, KEEPING the periapsis low. This lets the long
    interplanetary-window wait rails-warp at the high-altitude cap (100,000x near the SOI edge) instead of
    the ~50x cap at 100 km LKO, while the low periapsis still gives an efficient Oberth ejection later."""
    ref = v.orbit.body.non_rotating_reference_frame
    ap = v.auto_pilot
    ap.reference_frame = ref
    ap.target_direction = v.velocity(ref)
    ap.engage()
    time.sleep(5)
    soi_alt = v.orbit.body.sphere_of_influence - v.orbit.body.equatorial_radius
    t0 = time.monotonic()
    last = ""
    while time.monotonic() - t0 < max_s:
        ap.target_direction = v.velocity(ref)
        apo = v.orbit.apoapsis_altitude
        # Stop at the target, if already hyperbolic (apo<0 = escaped), or if nearing the SOI edge.
        if apo < 0 or apo >= target_ap_m or apo > soi_alt * 0.9:
            break
        # FEATHER the throttle as the apoapsis nears the target — it grows EXPONENTIALLY near escape, so a
        # full-throttle burn overshoots past the SOI and the craft escapes (the first attempt did). Slow the
        # final approach so the burn cuts off close to the target.
        frac = apo / target_ap_m
        v.control.throttle = 1.0 if frac < 0.5 else (0.2 if frac < 0.85 else 0.04)
        m = f"raising apoapsis {apo/1000:.0f}k km"
        if m != last:
            log("  " + m); last = m
        time.sleep(0.3)
    v.control.throttle = 0.0
    try:
        ap.disengage()
    except Exception:
        pass
    time.sleep(1)


def transfer_to_mun(conn, sc, bridge, v, name: str, target_alt_km: float) -> bool:
    """Kerbin parking orbit -> Mun: plan the TMI node (grid search, retry for a window), MechJeb executes
    it, warp to the Mun SOI then to periapsis, and CAPTURE with a pure-retrograde burn (no refuel)."""
    ctrl = KrpcFlightController(cfg["krpc"])
    rec = TelemetryRecorder(Path("runs") / f"transfer-{name}.jsonl")
    start = time.monotonic()
    sc.rails_warp_factor = 0
    try:
        v.control.remove_nodes()
    except Exception:
        pass
    node = None
    for attempt in range(4):
        node = ctrl._find_mun_transfer_node(conn, v, rec, start, transfer_profile="capture")
        if node is not None:
            break
        log(f"  no Mun transfer window (attempt {attempt + 1}); warping ahead a fraction of an orbit ...")
        sc.warp_to(sc.ut + max(300.0, v.orbit.period / 6.0))
    if node is None:
        log("  FAILED: no Mun transfer node found"); return False
    log(f"  TMI node: dv~{node.prograde:.0f} m/s at T+{node.ut - sc.ut:.0f}s")
    bridge.mj_execute_node()                          # the TMI node is near -> MechJeb autowarp is fine
    _wait_node_done(bridge, timeout_s=900.0, label="TMI")
    # FINE-TUNE the Mun closest approach BEFORE committing. The grid-search TMI can leave a periapsis that
    # is SUB-SURFACE (an impact — what destroyed the first AI-Mun-Relay-A at -40 km). MechJeb's course
    # correction places an optimal node to raise the closest approach; verify it, and ABORT rather than
    # warp into an impact.
    for attempt in range(3):
        pred = _predicted_periapsis_at(v, "Mun")
        if pred is not None and pred > 25_000.0:
            log(f"  Mun closest-approach periapsis {pred/1000:.0f} km — safe")
            break
        shown = f"{pred/1000:.0f} km" if pred is not None else "no encounter"
        log(f"  Mun closest approach {shown} -> course-correcting (attempt {attempt + 1}) ...")
        try:
            r = bridge.mj_plan(target="Mun", operation="correction")
            log(f"    correction node dv~{r.get('dv', 0):.0f} m/s")
            bridge.mj_execute_node()
            _wait_node_done(bridge, timeout_s=400.0, label="correction")
        except Exception as exc:
            log(f"    correction failed: {exc}")
            break
    pred = _predicted_periapsis_at(v, "Mun")
    if pred is None or pred < 5_000.0:
        log(f"  ABORT: Mun periapsis still unsafe ({round(pred/1000) if pred else pred} km) — not warping into an impact")
        return False
    # coast/warp to the Mun SOI
    if v.orbit.body.name != "Mun":
        try:
            soi_dt = v.orbit.time_to_soi_change
            if soi_dt and 0 < soi_dt < 1e7:
                log(f"  warping {soi_dt:.0f}s to the Mun SOI ...")
                sc.warp_to(sc.ut + soi_dt + 20.0)
        except Exception as exc:
            log(f"  SOI warp note: {exc}")
    time.sleep(3)
    if v.orbit.body.name != "Mun":
        log(f"  did not enter the Mun SOI (still {v.orbit.body.name})"); return False
    # warp to periapsis, then capture there (most efficient; burning at the SOI edge buries periapsis)
    ttp = v.orbit.time_to_periapsis
    if ttp and 0 < ttp < 1e7:
        log(f"  in Mun SOI; warping {ttp:.0f}s to periapsis ({v.orbit.periapsis_altitude/1000:.0f} km) ...")
        sc.warp_to(sc.ut + ttp - 25.0)
        time.sleep(2)
    # CAPTURE — pure retrograde burn, NO refuel. The bound-orbit CEILING must be ABOVE the ENCOUNTER
    # periapsis (which can be well above the requested target — e.g. 2196 km), so the burn STOPS at a
    # near-circular orbit THERE instead of burning past circular and driving the periapsis into the surface
    # (the A2 failure: it over-burned pe 2196k -> -24k). Capturing near the encounter periapsis is fine for
    # a relay — a higher Mun orbit just gives wider coverage.
    enc_pe = v.orbit.periapsis_altitude
    ap_target_m = max(150_000.0, enc_pe * 1.3)
    log(f"  capturing near the encounter periapsis ~{enc_pe/1000:.0f} km (bound ceiling {ap_target_m/1000:.0f} km)")
    _retro_capture(conn, sc, v, log, ap_target_m=ap_target_m, pe_floor_m=20_000.0)
    return v.orbit.body.name == "Mun" and v.orbit.periapsis_altitude > 8_000.0


def _lower_to_lko(conn, sc, bridge, v, target_apo_alt_m: float = 130_000.0) -> None:
    """Drop the high warp-orbit apoapsis back to a low ~circular orbit (keeping the ~100 km periapsis) with a
    PRECISE retrograde maneuver node at periapsis, executed by MechJeb. mj_plan's interplanetary transfer
    expects a low orbit; from a ~6,900 km apoapsis it returns a retrograde node that lowers the orbit instead
    of escaping. A hand-rolled retrograde burn OVERSHOT (drove the periapsis into the atmosphere), so compute
    the exact Δv from vis-viva and let MechJeb's node executor place it precisely. The upper carries ~4,400
    m/s; this ~800 m/s round-trip (raise for warp, lower for ejection) is affordable."""
    body = v.orbit.body
    mu = body.gravitational_parameter
    r_pe = v.orbit.periapsis                                  # radius at periapsis (m)
    a_now = v.orbit.semi_major_axis
    a_target = (r_pe + body.equatorial_radius + target_apo_alt_m) / 2.0
    v_now = (mu * (2.0 / r_pe - 1.0 / a_now)) ** 0.5
    v_target = (mu * (2.0 / r_pe - 1.0 / a_target)) ** 0.5
    dv = v_target - v_now                                     # negative = retrograde, lowers the apoapsis
    try:
        v.control.remove_nodes()
    except Exception:
        pass
    v.control.add_node(sc.ut + v.orbit.time_to_periapsis, prograde=dv)
    try:
        bridge.mj_execute_node()
        _wait_node_done(bridge, timeout_s=600.0, label="lower-to-LKO")
    finally:
        try:
            v.control.remove_nodes()
        except Exception:
            pass
    log(f"  lowered to ~{v.orbit.periapsis_altitude/1000:.0f}x{v.orbit.apoapsis_altitude/1000:.0f} km for a clean ejection")


def _vcross(a, b):
    return (a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2], a[0]*b[1] - a[1]*b[0])


def _vnorm(a):
    m = (a[0]*a[0] + a[1]*a[1] + a[2]*a[2]) ** 0.5
    return (a[0]/m, a[1]/m, a[2]/m) if m > 1e-9 else (0.0, 1.0, 0.0)


def _execute_node_manually(conn, sc, v, max_burn_s: float = 220.0, max_throttle: float = 1.0) -> None:
    """Hand-fly the first maneuver node with a kRPC burn. Two problems this works around: (1) MechJeb's node
    executor silently SKIPS close correction nodes (auto-warps to a far ejection node and burns it, but leaves
    a ~6-min node un-burned until the wait times out); (2) kRPC's node.direction()/burn_vector() throw a NULL
    WorldBurnVector on a fresh post-escape patch. So build the burn direction from the node's prograde/normal/
    radial SCALARS (always readable) and the live velocity/position, warp to ~8 s before it, and cut the burn
    off when the measured velocity change reaches the node's Δv magnitude."""
    if not v.control.nodes:
        return
    node = v.control.nodes[0]
    ref = v.orbit.body.non_rotating_reference_frame
    dvp, dvn, dvr = node.prograde, node.normal, node.radial          # scalars, readable even when patch null
    dv_total = (dvp*dvp + dvn*dvn + dvr*dvr) ** 0.5
    if dv_total < 0.3:
        try:
            node.remove()
        except Exception:
            pass
        return
    if node.time_to > 20.0:
        sc.warp_to(sc.ut + node.time_to - 8.0)
        time.sleep(2)

    def _burn_dir():
        r = v.position(ref)
        vel = v.velocity(ref)
        pro = _vnorm(vel)
        nrm = _vnorm(_vcross(r, vel))
        rad = _vnorm(_vcross(pro, nrm))                              # prograde x normal = radial-out
        return _vnorm((dvp*pro[0] + dvn*nrm[0] + dvr*rad[0],
                       dvp*pro[1] + dvn*nrm[1] + dvr*rad[1],
                       dvp*pro[2] + dvn*nrm[2] + dvr*rad[2]))

    ap = v.auto_pilot
    ap.reference_frame = ref
    ap.target_direction = _burn_dir()
    ap.engage()
    # SETTLE: wait until the craft actually points along the burn vector before igniting, so the burn starts
    # on-axis. Under heavy game lag MechJeb's executor drifts ~2 deg off the (in-plane) node during a long
    # ejection burn, tilting the heliocentric orbit; aligning first + a capped throttle keeps it on-axis.
    for _ in range(60):
        bd = _burn_dir()
        fwd = v.direction(ref)
        dot = max(-1.0, min(1.0, fwd[0]*bd[0] + fwd[1]*bd[1] + fwd[2]*bd[2]))
        if math.degrees(math.acos(dot)) < 1.5:
            break
        ap.target_direction = bd
        time.sleep(0.5)
    # Track Δv by INTEGRATING engine acceleration (thrust/mass * throttle * dt), NOT a velocity-delta: a big
    # ejection burn rotates the velocity vector a lot as the orbit stretches and then crosses the Kerbin SOI
    # (changing the orbit frame), both of which break a (cur-v0) projection -> it under-counts and over-burns
    # to max_burn_s (validated: a 1020 m/s ejection ate ~260 LF). Engine-accel integration is frame- and
    # rotation-independent and counts only the burn's Δv (no gravity contamination).
    applied = 0.0
    t0 = time.monotonic()
    ut_prev = sc.ut                                          # integrate over GAME time, not wall-clock
    v.control.throttle = min(max_throttle, 1.0)
    while time.monotonic() - t0 < max_burn_s:
        ap.target_direction = _burn_dir()
        ut_now = sc.ut
        # Δv = ∫(thrust·throttle/mass) over PHYSICS time. Under ~3x game lag a wall-clock dt over-counts the
        # time the thrust actually acted (game advances < wall-clock), cutting the burn short -> a large Duna
        # phasing miss. sc.ut is the physics clock, so this integral is lag-independent and accurate.
        applied += (v.available_thrust * v.control.throttle / max(v.mass, 1.0)) * max(0.0, ut_now - ut_prev)
        ut_prev = ut_now
        rem = dv_total - applied
        if rem < 0.3:
            break
        v.control.throttle = min(max_throttle, 1.0) if rem > 15.0 else min(max_throttle, 0.1)
        time.sleep(0.1)
    v.control.throttle = 0.0
    try:
        ap.disengage()
    except Exception:
        pass
    try:
        node.remove()
    except Exception:
        pass


def _duna_closest_approach_km(sc, v):
    """Min heliocentric distance between the craft and Duna over the next ~120 game-days, plus its UT.
    Independent of patched-conic encounter detection, so convergence is visible even before an SOI hit forms
    (a 16x-SOI *phasing* miss reads None from _predicted_periapsis_at — this shows the real number)."""
    helio = sc.bodies["Sun"].non_rotating_reference_frame
    duna = sc.bodies["Duna"]
    soi = float(duna.sphere_of_influence)
    best_d, best_ut = float("inf"), sc.ut
    step = 6 * 3600.0
    for i in range(int(120 * 6 * 3600.0 / step) + 1):
        ut = sc.ut + i * step
        try:
            vp = v.orbit.position_at(ut, helio)
            dp = duna.orbit.position_at(ut, helio)
            d = ((vp[0]-dp[0])**2 + (vp[1]-dp[1])**2 + (vp[2]-dp[2])**2) ** 0.5
        except Exception:
            continue
        if d < best_d:
            best_d, best_ut = d, ut
    tag = "INSIDE SOI" if best_d < soi else f"miss {best_d/soi:.1f}x SOI"
    log(f"  [DIAG] Duna closest approach {best_d/1000:,.0f} km (SOI {soi/1000:,.0f} km -> {tag})")
    return best_d, best_ut


def _wait_until_sun_orbit(sc, v, max_loops: int = 8) -> bool:
    """After the ejection burn the craft is escaping but kRPC still reports body=Kerbin until it physically
    crosses the SOI; warp across the boundary so all targeting runs in the heliocentric (Sun) frame."""
    for _ in range(max_loops):
        if v.orbit.body.name == "Sun":
            return True
        try:
            dt = v.orbit.time_to_soi_change
            if dt and 0 < dt < 1e9:
                sc.warp_to(sc.ut + dt + 60.0)
                time.sleep(2)
            else:
                break
        except Exception:
            break
    return v.orbit.body.name == "Sun"


def _score_duna_node(node, duna):
    """Score a heliocentric correction node by its Duna closest approach (kRPC patched conics), mirroring
    flight_controller._score_mun_transfer_node. Lower = better; any real encounter beats any miss."""
    closest = float("inf")
    try:
        closest = float(node.orbit.distance_at_closest_approach(duna.orbit))
    except Exception:
        pass
    encounter, duna_pe = False, float("inf")
    try:
        nxt = node.orbit.next_orbit
        if str(nxt.body.name) == "Duna":
            encounter, duna_pe = True, float(nxt.periapsis_altitude)
    except Exception:
        pass
    if encounter:
        tgt = 100_000.0
        if 60_000.0 <= duna_pe <= 250_000.0:
            score = abs(duna_pe - tgt)
        elif duna_pe < 60_000.0:                            # below/into Duna's ~50 km atmosphere
            score = abs(duna_pe - tgt) + 50_000_000.0
        else:
            score = abs(duna_pe - tgt) + 2_000_000.0
    else:
        score = closest + 1.0e10                            # no encounter: drive the closest approach down
    return {"score": score, "encounter": encounter, "duna_pe_m": duna_pe, "closest_m": closest}


def _frange(lo: float, hi: float, step: float):
    out, x = [], lo
    while x <= hi + 1e-9:
        out.append(round(x, 3))
        x += step
    return out


def _score_ejection_node(node, duna):
    """Duna closest approach for a candidate LKO EJECTION node: walk the patches to the post-escape Sun orbit
    and score it (the ejection node's next patch is heliocentric, where distance_at_closest_approach works)."""
    closest, enc, pe = float("inf"), False, float("inf")
    try:
        o = node.orbit
        for _ in range(5):
            nx = o.next_orbit
            if nx is None:
                break
            bn = str(nx.body.name)
            if bn == "Duna":
                enc, pe, closest = True, float(nx.periapsis_altitude), 0.0
                break
            if bn == "Sun":
                try:
                    closest = float(nx.distance_at_closest_approach(duna.orbit))
                except Exception:
                    pass
            o = nx
    except Exception:
        pass
    if enc:
        score = abs(pe - 100_000.0) + (0.0 if pe >= 60_000.0 else 50_000_000.0)
    else:
        score = closest + 1.0e10
    return {"score": score, "encounter": enc, "duna_pe_m": pe, "closest_m": closest}


def _search_duna_ejection_prograde(sc, v, ut, base_pg, base_rad, base_nrm, pg_width=400.0, pg_step=25.0):
    """Tune the EJECTION prograde (around mj_plan's rough dv) to minimize the Duna closest approach BEFORE
    burning — the Mun-leg pattern adapted to the heliocentric patch. mj_plan hands back a rough Hohmann window
    (~6 days off) -> a ~3500 Mm miss too big to mid-course correct within the fuel; tuning the ejection itself
    lands a small miss the existing correction grid closes cheaply. Returns the best prograde."""
    duna = sc.bodies["Duna"]
    sc.rails_warp_factor = 0
    time.sleep(1)
    best, best_score, n = None, float("inf"), 0
    for pg in _frange(base_pg - pg_width, base_pg + pg_width, pg_step):
        node = v.control.add_node(float(ut), prograde=float(pg), radial=float(base_rad), normal=float(base_nrm))
        try:
            cand = _score_ejection_node(node, duna)
            n += 1
            if cand["score"] < best_score:
                best_score = cand["score"]
                best = {"prograde": float(pg), **cand}
        finally:
            try:
                node.remove()
            except Exception:
                pass
    if best is not None:
        tag = f"enc pe {best['duna_pe_m']/1000:.0f} km" if best["encounter"] else f"closest {best['closest_m']/1e6:.0f} Mm"
        log(f"  ejection-tune ({n} nodes): prograde {base_pg:.0f} -> {best['prograde']:.0f} m/s -> {tag}")
    return best


def _search_duna_correction_grid(sc, v, mid_ut, seed_prograde=0.0, pg_width=140.0, pg_step=20.0,
                                 rad_vals=(-30.0, 0.0, 30.0), nrm_vals=(-15.0, 0.0, 15.0), ut_offsets=(0.0,)):
    """Deterministic grid-search for a mid-course node that drops the Duna closest approach into the SOI —
    the SAME pattern as the working Mun transfer. MechJeb's OperationCourseCorrection only REFINES an
    existing encounter, so it can never CREATE one from a heliocentric near-miss; this evaluates each
    candidate node's patched-conic Duna approach directly and keeps only the best."""
    duna = sc.bodies["Duna"]
    sc.rails_warp_factor = 0
    time.sleep(2)                                            # let patched-conic predictions settle after warp
    best, best_score, n = None, float("inf"), 0
    for ut_off in ut_offsets:
        ut = mid_ut + ut_off
        for pg in _frange(seed_prograde - pg_width, seed_prograde + pg_width, pg_step):
            for rad in rad_vals:
                for nrm in nrm_vals:
                    node = v.control.add_node(float(ut), prograde=float(pg), radial=float(rad), normal=float(nrm))
                    try:
                        cand = _score_duna_node(node, duna)
                        n += 1
                        if cand["score"] < best_score:
                            best_score = cand["score"]
                            best = {"ut": float(ut), "prograde": float(pg), "radial": float(rad), "normal": float(nrm), **cand}
                    finally:
                        try:
                            node.remove()
                        except Exception:
                            pass
    if best is not None:
        tag = f"encounter pe {best['duna_pe_m']/1000:.0f} km" if best["encounter"] else f"closest {best['closest_m']/1e6:.0f} Mm"
        log(f"  grid-search ({n} nodes): best dv=({best['prograde']:.0f},{best['radial']:.0f},{best['normal']:.0f}) -> {tag}")
    return best


def transfer_to_duna(conn, sc, bridge, v, name: str, target_alt_km: float) -> bool:
    """Kerbin parking orbit -> Duna: mj_plan interplanetary ejection at the NEXT window, warp to it, eject,
    course-check the closest approach, warp the ~75-day coast to the Duna SOI, then retro-capture ABOVE
    Duna's ~50 km atmosphere. The RTG holds control through the long coast. No refuel."""
    from ksp_lab.bodies import DUNA
    sc.rails_warp_factor = 0
    try:
        v.control.remove_nodes()
    except Exception:
        pass
    _incl_log(v, "post-ascent LKO")                          # baseline plane (should be ~equatorial/ecliptic)
    # 1) Plan the ejection just to read the WINDOW time, then drop the node (we re-plan after the warp).
    try:
        r = bridge.mj_plan(target="Duna", operation="interplanetary")
    except Exception as exc:
        log(f"  Duna ejection plan FAILED: {exc}"); return False
    node_ut = r.get("ut", sc.ut)
    wait = node_ut - sc.ut
    log(f"  Duna window in {wait:.0f}s (~{wait/(426*21600):.2f} Kerbin-yr); ejection ~{r.get('dv', 0):.0f} m/s")
    try:
        v.control.remove_nodes()
    except Exception:
        pass
    # 2) RAISE the apoapsis near the SOI edge so the long wait rails-warps at the high-altitude cap — the
    # 100 km LKO cap is only 50x, which would make a 1.77-yr wait ~90 real HOURS. Keep the periapsis low so
    # the later ejection is still an efficient Oberth burn (and the raise Δv is recovered there).
    if wait > 6 * 3600:
        from ksp_lab.bodies import MUN, KERBIN
        soi = v.orbit.body.sphere_of_influence
        mun_alt = MUN.orbit_radius_m - KERBIN.radius_m            # the Mun's orbital altitude (~11,400 km)
        # Stay below the Mun's SOI (~2,430 km radius reaches down to ~9,000 km), NOT just its orbit: even an
        # apoapsis of 9,900 km got flung out of Kerbin SOI by a Mun flyby. ~6,900 km clears the Mun's SOI by
        # ~2,000 km and is still high enough for a fast rails-warp cap (the earlier high orbits showed cap 7).
        target_ap = min(soi * 0.55, mun_alt - 4_500_000.0)
        log(f"  raising apoapsis to ~{target_ap/1000:.0f} km (below the Mun) so the {wait/(426*21600):.2f}-yr wait warps fast ...")
        _raise_apoapsis(sc, v, target_ap)
        log(f"  apoapsis {v.orbit.apoapsis_altitude/1000:.0f} km / periapsis {v.orbit.periapsis_altitude/1000:.0f} km")
        _incl_log(v, "post-raise")                            # the raise is prograde -> should NOT tilt the plane
    # 3) Warp to NEAR the window (fast now: most of the long orbit sits at the high-warp altitude band).
    # Leave > 1 high-orbit PERIOD of buffer: the lower-to-LKO step below warps to the next periapsis (up to
    # ~1 period of game-time), so warping all the way to node_ut - 1800 would OVERSHOOT the window during the
    # lower and force mj_plan onto the NEXT synodic window (~2 yr later, un-warpable from LKO at cap 3).
    period = 2.0 * math.pi * (v.orbit.semi_major_axis ** 3 / v.orbit.body.gravitational_parameter) ** 0.5
    warp_target = node_ut - (period * 1.3 + 1800.0)
    if warp_target > sc.ut:
        sc.warp_to(warp_target)
    time.sleep(2)
    # 3b) Lower the high warp orbit back to ~LKO BEFORE re-planning. mj_plan's OperationInterplanetaryTransfer
    # expects a low circular orbit; from the high warp orbit (6,900 km apoapsis) it returned a RETROGRADE node
    # that LOWERED the orbit instead of escaping (validated: 6906 -> 416 km, no Duna encounter -> abort). The
    # warp strategy is what makes the long wait practical; we just don't eject from the raised orbit. The
    # upper carries ~4,400 m/s so the ~800 m/s round-trip (raise for warp, lower for ejection) is affordable.
    if v.orbit.apoapsis_altitude > 400_000.0:
        log(f"  lowering the {v.orbit.apoapsis_altitude/1000:.0f} km warp orbit back to LKO for a clean ejection ...")
        _lower_to_lko(conn, sc, bridge, v)
        _incl_log(v, "post-lower LKO")                        # last in-Kerbin reading before the ejection plans
    log(f"  reached the window (UT {round(sc.ut)}); re-planning the ejection from the current orbit")
    # 4) Re-plan the ejection from the (now low) orbit and execute.
    try:
        r2 = bridge.mj_plan(target="Duna", operation="interplanetary")
        log(f"  ejection re-planned: dv~{r2.get('dv', 0):.0f} m/s at T+{r2.get('ut', sc.ut) - sc.ut:.0f}s")
    except Exception as exc:
        log(f"  ejection re-plan FAILED: {exc}"); return False
    # TUNE the ejection prograde for a PRECISE Duna intercept BEFORE burning — mj_plan's window is a rough
    # Hohmann (~6 days off) leaving a ~3500 Mm miss that costs more correction Δv than the fuel allows. The
    # ejection-tune grid lands a small miss the later correction grid closes cheaply (the Mun-leg method).
    _nodes = v.control.nodes
    if _nodes:
        _bn = _nodes[0]
        _bu, _bpg, _brad, _bnrm = _bn.ut, _bn.prograde, _bn.radial, _bn.normal
        _bestej = _search_duna_ejection_prograde(sc, v, _bu, _bpg, _brad, _bnrm)
        if _bestej is None or (not _bestej["encounter"] and _bestej["closest_m"] > 5.0e8):
            _bestej = _search_duna_ejection_prograde(sc, v, _bu, _bpg, _brad, _bnrm, pg_width=750.0, pg_step=25.0)
        if _bestej is not None:
            v.control.remove_nodes()
            v.control.add_node(_bu, prograde=_bestej["prograde"], radial=_brad, normal=_bnrm)
    # Execute the ejection OURSELVES (ALIGN to the burn vector before igniting), NOT MechJeb's executor: the
    # node is in-plane (normal ~= 0), but MechJeb starts the burn off-axis under the game lag and drifts ~2 deg
    # over the 1025 m/s burn, tilting the heliocentric orbit -> a ~785 Mm Duna miss. Aligning first held it to
    # 0.1 deg (validated). FULL throttle (not capped): a slow capped burn spends a long arc at periapsis and
    # gravity-loss ate ALL the fuel; full throttle keeps the burn impulsive so ~30 LF survives for the capture.
    _execute_node_manually(conn, sc, v, max_burn_s=300.0, max_throttle=1.0)
    # 3c) ESTABLISH the Duna encounter with a deterministic GRID-SEARCH (the SAME method as the working Mun
    # transfer). MechJeb's OperationCourseCorrection only REFINES an existing encounter — on the heliocentric
    # near-miss the ejection leaves (~hundreds of Mm phasing miss) it has nothing to refine, which is why
    # every mj_plan(correction) attempt returned "no encounter". Instead: cross into the Sun's SOI, coast to
    # ~mid-transfer for leverage, grid-search a small node (prograde/radial/normal) that drops the Duna
    # closest approach into the SOI, and burn only the best one. ~120 m/s closes a 785 Mm miss from midpoint.
    if not _wait_until_sun_orbit(sc, v):
        log(f"  did not reach heliocentric orbit after ejection (still {v.orbit.body.name}) — ABORT"); return False
    _duna_closest_approach_km(sc, v)                          # baseline, before any correction
    # DIAGNOSTIC: the dominant miss is a PLANE error — a fresh ejection left the craft at 2.09 deg heliocentric
    # inclination vs Duna's 0.06 deg (a ~2 deg error = ~720 Mm out-of-plane at 20.7 Gm), too large for the
    # remaining ~740 m/s to plane-match AND capture. Log it through the run to find where the tilt enters
    # (equatorial-LKO drift in raise/lower, or an un-plane-matched mj_plan ejection).
    try:
        rel_i = abs(math.degrees(v.orbit.inclination) - math.degrees(sc.bodies["Duna"].orbit.inclination))
        log(f"  [DIAG] heliocentric incl {math.degrees(v.orbit.inclination):.2f} deg vs Duna {math.degrees(sc.bodies['Duna'].orbit.inclination):.2f} deg -> rel {rel_i:.2f} deg")
    except Exception:
        pass
    tta = v.orbit.time_to_apoapsis or 0.0
    if 0 < tta < 1e9:
        log(f"  coasting ~{tta*0.5/(6*3600):.0f} days to mid-transfer for correction leverage ...")
        sc.warp_to(sc.ut + tta * 0.5)
        time.sleep(2)
    mid_ut = sc.ut + max(600.0, (v.orbit.time_to_apoapsis or 0.0) * 0.2)
    # STAGED search (the miss is mostly along-track PHASING -> prograde dominates): a cheap 1-D prograde sweep
    # localizes the encounter, then a small radial+normal refine places the periapsis. Keeps the node count
    # (and correction Δv) low instead of a 1000-node 4-D brute grid.
    coarse = _search_duna_correction_grid(sc, v, mid_ut, pg_width=220.0, pg_step=20.0, rad_vals=(0.0,), nrm_vals=(0.0,))
    seed = coarse["prograde"] if coarse else 0.0
    best = _search_duna_correction_grid(sc, v, mid_ut, seed_prograde=seed, pg_width=30.0, pg_step=10.0,
                                        rad_vals=(-30.0, 0.0, 30.0), nrm_vals=(-15.0, 0.0, 15.0))
    if best is not None and not best["encounter"]:           # wider sweep (more UTs + prograde) if still missing
        log("  no encounter after refine — wider prograde + UT sweep ...")
        rem = v.orbit.time_to_apoapsis or 0.0
        coarse2 = _search_duna_correction_grid(sc, v, mid_ut, pg_width=420.0, pg_step=25.0, rad_vals=(0.0,),
                                               nrm_vals=(0.0,), ut_offsets=(-rem * 0.2, 0.0, rem * 0.2))
        seed2 = coarse2["prograde"] if coarse2 else seed
        best = _search_duna_correction_grid(sc, v, mid_ut, seed_prograde=seed2, pg_width=40.0, pg_step=10.0,
                                            rad_vals=(-40.0, -20.0, 0.0, 20.0, 40.0), nrm_vals=(-20.0, 0.0, 20.0))
    if best is None or not best["encounter"] or best["duna_pe_m"] < 60_000.0:
        shown = f"{best['duna_pe_m']/1000:.0f} km" if (best and best["encounter"]) else "no encounter"
        log(f"  ABORT: grid-search could not establish a safe Duna encounter ({shown})"); return False
    log(f"  mid-course correction dv=({best['prograde']:.0f},{best['radial']:.0f},{best['normal']:.0f}) m/s -> Duna pe {best['duna_pe_m']/1000:.0f} km; burning ...")
    v.control.remove_nodes()
    v.control.add_node(best["ut"], prograde=best["prograde"], radial=best["radial"], normal=best["normal"])
    _execute_node_manually(conn, sc, v)
    _duna_closest_approach_km(sc, v)                          # after the burn — should read INSIDE SOI
    pred = _predicted_periapsis_at(v, "Duna")
    if pred is None or pred < 60_000.0:
        log(f"  ABORT: post-correction Duna periapsis unsafe ({round(pred/1000) if pred else pred} km)"); return False
    log(f"  Duna encounter established — periapsis {pred/1000:.0f} km (above the ~50 km atmosphere)")
    # 5) Warp the coast to the Duna SOI.
    if v.orbit.body.name != "Duna":
        try:
            soi_dt = v.orbit.time_to_soi_change
            if soi_dt and 0 < soi_dt < 1e9:
                log(f"  coasting {soi_dt:.0f}s (~{soi_dt/(6*3600):.0f} Kerbin-days) to the Duna SOI ...")
                sc.warp_to(sc.ut + soi_dt + 30.0)
        except Exception as exc:
            log(f"  SOI warp note: {exc}")
    time.sleep(3)
    if v.orbit.body.name != "Duna":
        log(f"  did not enter the Duna SOI (still {v.orbit.body.name})"); return False
    # 6) Warp to periapsis, then retro-capture ABOVE the atmosphere (pe floor 60 km > Duna's ~50 km top).
    ttp = v.orbit.time_to_periapsis
    if ttp and 0 < ttp < 1e7:
        log(f"  in Duna SOI; warping {ttp:.0f}s to periapsis ({v.orbit.periapsis_altitude/1000:.0f} km) ...")
        sc.warp_to(sc.ut + ttp - 30.0)
        time.sleep(2)
    enc_pe = v.orbit.periapsis_altitude
    ap_target_m = max(80_000.0, enc_pe * 1.3)
    log(f"  capturing near the encounter periapsis ~{enc_pe/1000:.0f} km (bound ceiling {ap_target_m/1000:.0f} km)")
    _retro_capture(conn, sc, v, log, ap_target_m=ap_target_m, pe_floor_m=60_000.0, max_s=400.0)
    return v.orbit.body.name == "Duna" and v.orbit.periapsis_altitude > 55_000.0


def main() -> int:
    global cfg
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    target_body = sys.argv[2] if len(sys.argv) > 2 else "Mun"
    target_alt_km = float(sys.argv[3]) if len(sys.argv) > 3 else 750.0
    name = sys.argv[4] if len(sys.argv) > 4 else "AI-Mun-Relay"
    cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
    bridge = BridgeClient(**cfg["bridge"])
    runner = AutomationRunner(cfg_path, offline=False)
    kc = cfg["krpc"]
    c = krpc.connect(name="deploy-transfer", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center

    # 1) Launch to a 100 km Kerbin PARKING orbit (proven ascent + booster force-separation).
    if not deploy_relay.launch_to_lko(sc, cfg, runner, bridge, name, 100.0):
        log("launch to parking orbit FAILED"); return 2
    time.sleep(3)
    v = sc.active_vessel
    log(f"in parking orbit {round(v.orbit.periapsis_altitude/1000)}x{round(v.orbit.apoapsis_altitude/1000)} km; "
        f"transferring to {target_body} (target {target_alt_km:.0f} km)")

    # 2) Transfer + capture.
    if target_body == "Mun":
        if not transfer_to_mun(c, sc, bridge, v, name, target_alt_km):
            log("Mun transfer/capture FAILED"); return 2
    elif target_body == "Duna":
        if not transfer_to_duna(c, sc, bridge, v, name, target_alt_km):
            log("Duna transfer/capture FAILED"); return 2
    else:
        log(f"target {target_body} not yet wired (Ike to follow on this skeleton)"); return 2

    # 3) Circularise in the target SOI, then commission.
    log(f"  captured {round(v.orbit.periapsis_altitude/1000)}x{round(v.orbit.apoapsis_altitude/1000)} km "
        f"{v.orbit.body.name}; circularising")
    _circularize_at_apoapsis(c, sc, v)
    log(f"  circular {round(v.orbit.periapsis_altitude/1000)}x{round(v.orbit.apoapsis_altitude/1000)} km "
        f"ecc {v.orbit.eccentricity:.3f}")
    deploy_relay.commission(bridge, v)
    log(f"=== {target_body} RELAY {name} DEPLOYED: {round(v.orbit.periapsis_altitude/1000)}x"
        f"{round(v.orbit.apoapsis_altitude/1000)} km {v.orbit.body.name} ===")
    try:
        sc.save("persistent")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
