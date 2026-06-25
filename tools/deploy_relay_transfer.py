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
    time.sleep(4)                                            # let patched-conic predictions settle after warp
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


def _search_duna_periapsis_lower(sc, v, target_pe=300_000.0):
    """In the Duna SOI (on the hyperbolic approach), grid-search a CHEAP node early in the approach that
    drops the Duna periapsis toward ~target_pe. The mid-transfer correction lands the encounter at the SOI
    EDGE (pe ~22,000-43,000 km) where a retro-capture has no Oberth and costs ~the full v_inf (> the fuel).
    A radial burn far from periapsis has high leverage on the periapsis, so lowering it here to ~300 km makes
    the capture cheap (Oberth). Scores the candidate node's resulting Duna periapsis directly (we are already
    in Duna's SOI, so node.orbit is the Duna approach)."""
    sc.rails_warp_factor = 0
    time.sleep(2)
    duna = sc.bodies["Duna"]
    mu, r_duna = duna.gravitational_parameter, duna.equatorial_radius
    a = v.orbit.semi_major_axis
    v_inf = (mu / abs(a)) ** 0.5 if a and a < 0 else 0.0     # hyperbolic excess speed (~constant on approach)
    ttp = v.orbit.time_to_periapsis or 0.0
    ut = sc.ut + max(60.0, ttp * 0.05)                       # early in the approach -> high periapsis leverage
    best, best_score, n = None, float("inf"), 0
    for pg in (-40.0, -20.0, 0.0, 20.0, 40.0):
        for rad in _frange(-500.0, 500.0, 50.0):
            for nrm in (-60.0, 0.0, 60.0):
                node = v.control.add_node(float(ut), prograde=float(pg), radial=float(rad), normal=float(nrm))
                try:
                    n += 1
                    pe = float(node.orbit.periapsis_altitude)
                    if pe < 2_500_000.0:                     # FLOOR at ~2,500 km (near the ring target, well above
                        continue                             # the ~50 km atmosphere): a deep periapsis makes the
                    # capture burn so Oberth-powerful that one lag-frame over-burns into the surface (AI-Duna-Ring-X
                    # lowered to 129 km then crashed). With the launch-window fuel surplus we don't need the extra
                    # Oberth — a gentle ~2,500 km capture is plenty affordable and can't overshoot to the ground.
                    r_pe = r_duna + pe
                    node_dv = (pg*pg + rad*rad + nrm*nrm) ** 0.5
                    # estimated Oberth capture Δv at this periapsis (hyperbolic -> just-bound); low pe = cheap
                    cap_dv = (v_inf*v_inf + 2.0*mu/r_pe) ** 0.5 - (2.0*mu/r_pe) ** 0.5
                    score = node_dv + cap_dv                # minimize the TOTAL lower-then-capture Δv
                    if score < best_score:
                        best_score = score
                        best = {"ut": float(ut), "prograde": float(pg), "radial": float(rad), "normal": float(nrm), "pe": pe, "total_dv": score}
                finally:
                    try:
                        node.remove()
                    except Exception:
                        pass
    if best is not None:
        log(f"  periapsis-lower ({n} nodes): dv=({best['prograde']:.0f},{best['radial']:.0f},{best['normal']:.0f}) -> Duna pe {best['pe']/1000:.0f} km")
    return best


def _next_duna_window_ut(sc):
    """Analytical UT of the next Kerbin->Duna Hohmann departure, so we can WARP ON THE GROUND to it BEFORE
    committing any propellant (the user's fix: waiting for the window on the pad costs no fuel, whereas launching
    early forces the ~1,580 m/s in-flight raise/lower warp). Standard heliocentric phase-angle calculation.

    At departure Duna must LEAD Kerbin by  phi* = pi - omega_Duna * t_transfer  (so it arrives at the transfer
    apoapsis when the craft does). The current lead phi shrinks at the synodic rate (Kerbin is faster); the wait
    is the time for phi to reach phi*."""
    import math
    sun = sc.bodies["Sun"]; kerbin = sc.bodies["Kerbin"]; duna = sc.bodies["Duna"]
    ok, od = kerbin.orbit, duna.orbit
    R_k, R_d = ok.semi_major_axis, od.semi_major_axis
    a_t = 0.5 * (R_k + R_d)
    t_tr = math.pi * math.sqrt(a_t ** 3 / sun.gravitational_parameter)     # Hohmann transfer time
    omega_k = 2.0 * math.pi / ok.period
    omega_d = 2.0 * math.pi / od.period
    phi_star = math.pi - omega_d * t_tr                                    # required Duna lead at departure
    phi_star = (phi_star + math.pi) % (2.0 * math.pi) - math.pi
    pk = kerbin.position(sun.reference_frame); pd = duna.position(sun.reference_frame)
    lon_k = math.atan2(pk[2], pk[0]); lon_d = math.atan2(pd[2], pd[0])     # heliocentric longitudes (x-z plane)
    phi = lon_d - lon_k                                                    # current Duna lead
    rel = omega_k - omega_d                                               # > 0: the lead shrinks at this rate
    if rel <= 0:
        return sc.ut
    d = ((phi - phi_star) % (2.0 * math.pi))                              # how far the lead must shrink
    return sc.ut + d / rel


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
    # Threshold 8 h (not 6): the ground pad-warp lands us within ~5 h of the window, and the comsat-vs-LKO window
    # mismatch (~1 h, orbital phase) can push the residual just over 6 h. Up to ~8 h we LKO-warp at 50x (~8 min
    # game) instead of paying the ~1,580 m/s raise/lower round-trip — only a truly far window needs the raise.
    if wait > 8 * 3600:
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
    # 3b) Lower the high warp orbit back to a FULL CIRCULAR LKO before re-planning. This costs the raise's ~790
    # m/s of periapsis speed (and leaves the Duna capture ~250 m/s short — see below), BUT it is REQUIRED for a
    # well-aimed ejection: on a circular orbit mj_plan ejects at the IDEAL phase (any point), leaving a small
    # phasing miss the correction grid closes with ~50-80 m/s. A partial-lower to an eccentric ~100x450 km orbit
    # DOES save ~250 m/s of fuel (validated: AI-Duna-Ring-V reached the heliocentric leg with 56 LF vs U's 38),
    # but it CONSTRAINS the ejection to the orbit's fixed periapsis instead of the ideal point -> a geometry error
    # the grid CANNOT close (V's miss needed >790 m/s prograde and still sat at 4,116 Mm -> ABORT). So the fuel
    # shortfall must be closed elsewhere (a larger upper stage), NOT by under-lowering. mj_plan also returns a
    # RETROGRADE node from the high warp orbit, so the full lower is needed regardless.
    if v.orbit.apoapsis_altitude > 400_000.0:
        log(f"  lowering the {v.orbit.apoapsis_altitude/1000:.0f} km warp orbit back to LKO for a clean (well-aimed) ejection ...")
        _lower_to_lko(conn, sc, bridge, v)
        _incl_log(v, "post-lower LKO")                        # last in-Kerbin reading before the ejection plans
    log(f"  reached the window (UT {round(sc.ut)}); planning the PRECISE Lambert ejection")
    # 4) PRECISE ejection from the validated transfer_planner (Lambert porkchop over real positions). The v_inf
    # gives the EXACT ejection node: periapsis = v_inf rotated back by the asymptote true anomaly nu=arccos(-1/e),
    # placed at the next parking-orbit alignment (two-clock fix). VALIDATED offline: 256 Mm = 5.3x-SOI timed miss
    # (vs mj_plan's ~70x), the residual being just the small out-of-plane v_inf component. The correction grid
    # below closes the 5.3x to a clean encounter. mj_plan stays only as a fallback.
    try:
        from ksp_lab import transfer_planner as _tp
        _plan = _tp.plan_transfer(sc, v, v.orbit.body.name, "Duna")
        _nd = _plan["ejection"]; _w = _plan["window"]; _yr = 426 * 21600
        log(f"  PRECISE window: dep in {(_w['ut_dep']-sc.ut)/3600:.2f} h, tof {_w['tof']/_yr:.2f} yr, |vinf| {_w['vinf_mag']:.0f} m/s")
        log(f"  PRECISE ejection: prograde {_nd['prograde']:.0f} normal {_nd['normal']:.0f} m/s at T+{(_nd['ut']-sc.ut)/3600:.2f} h")
        v.control.remove_nodes()
        v.control.add_node(_nd["ut"], prograde=_nd["prograde"], normal=_nd["normal"], radial=_nd["radial"])
    except Exception as exc:
        log(f"  precise plan failed ({exc}); mj_plan fallback")
        try:
            bridge.mj_plan(target="Duna", operation="interplanetary")
        except Exception as exc2:
            log(f"  ejection re-plan FAILED: {exc2}"); return False
    # Execute OURSELVES (ALIGN to the burn vector before igniting), NOT MechJeb's executor (it drifts ~2 deg
    # off-axis under lag, tilting the heliocentric orbit). FULL throttle keeps the burn impulsive.
    _execute_node_manually(conn, sc, v, max_burn_s=400.0, max_throttle=1.0)
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
    # RETRY the correction up to 3x: the grid's distance_at_closest_approach PREDICTION is noisy near the SOI
    # edge, so a predicted GRAZING encounter (high pe) sometimes doesn't materialise once burned (AI-Duna-Ring-Z
    # burned a predicted 38,737 km grazing node -> actual 3,866 Mm miss -> abort). Instead of aborting on the
    # first miss, VERIFY the encounter after each burn and, if it didn't take, re-search + re-correct from the
    # new (closer) position. The bigger upper carries enough Δv for 2-3 small corrections + the capture.
    soi = v.orbit.body.sphere_of_influence
    pred = None
    for attempt in range(1, 4):
        mid_ut = sc.ut + max(600.0, (v.orbit.time_to_apoapsis or 0.0) * 0.2)
        # STAGED search: a cheap 1-D prograde sweep localizes the encounter (the miss is mostly along-track
        # PHASING), then a WIDE radial+normal refine places the periapsis (radial sets how deep the approach
        # passes Duna; a wide range finds a LOW Oberth-friendly periapsis, not a fragile SOI-edge graze).
        coarse = _search_duna_correction_grid(sc, v, mid_ut, pg_width=520.0, pg_step=30.0, rad_vals=(0.0,), nrm_vals=(0.0,))
        seed = coarse["prograde"] if coarse else 0.0
        best = _search_duna_correction_grid(sc, v, mid_ut, seed_prograde=seed, pg_width=40.0, pg_step=10.0,
                                            rad_vals=(-200.0, -140.0, -90.0, -45.0, 0.0, 45.0, 90.0, 140.0, 200.0),
                                            nrm_vals=(-15.0, 0.0, 15.0))
        if best is not None and not best["encounter"]:       # wider sweep (more UTs + prograde) if still missing
            log("  no encounter after refine — wider prograde + UT sweep ...")
            rem = v.orbit.time_to_apoapsis or 0.0
            coarse2 = _search_duna_correction_grid(sc, v, mid_ut, pg_width=800.0, pg_step=30.0, rad_vals=(0.0,),
                                                   nrm_vals=(0.0,), ut_offsets=(-rem * 0.2, 0.0, rem * 0.2))
            seed2 = coarse2["prograde"] if coarse2 else seed
            best = _search_duna_correction_grid(sc, v, mid_ut, seed_prograde=seed2, pg_width=40.0, pg_step=10.0,
                                                rad_vals=(-250.0, -180.0, -120.0, -60.0, 0.0, 60.0, 120.0, 180.0, 250.0),
                                                nrm_vals=(-20.0, 0.0, 20.0))
        if best is None or not best["encounter"] or best["duna_pe_m"] < 60_000.0:
            shown = f"{best['duna_pe_m']/1000:.0f} km" if (best and best["encounter"]) else "no encounter"
            log(f"  correction attempt {attempt}/3: no safe encounter found ({shown})")
            if attempt < 3:
                continue
            log("  ABORT: grid-search could not establish a safe Duna encounter"); return False
        graze = " (grazing — fragile, will verify)" if best["duna_pe_m"] > 0.6 * soi else ""
        log(f"  correction {attempt}/3 dv=({best['prograde']:.0f},{best['radial']:.0f},{best['normal']:.0f}) m/s -> Duna pe {best['duna_pe_m']/1000:.0f} km{graze}; burning ...")
        v.control.remove_nodes()
        v.control.add_node(best["ut"], prograde=best["prograde"], radial=best["radial"], normal=best["normal"])
        _execute_node_manually(conn, sc, v)
        _duna_closest_approach_km(sc, v)                      # after the burn — should read INSIDE SOI
        pred = _predicted_periapsis_at(v, "Duna")
        if pred is not None and pred >= 60_000.0:
            log(f"  Duna encounter established — periapsis {pred/1000:.0f} km (above the ~50 km atmosphere)")
            break
        log(f"  correction attempt {attempt}/3 didn't take (pred {round(pred/1000) if pred else pred} km) — re-searching ...")
        pred = None
    if pred is None or pred < 60_000.0:
        log("  ABORT: no Duna encounter after 3 correction attempts"); return False
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
    # 5b) If the encounter periapsis is HIGH (a shallow SOI-edge graze the mid-transfer correction can't
    # deepen), LOWER it now with a cheap radial burn early in the approach so the capture gets Oberth.
    if v.orbit.periapsis_altitude > 2_000_000.0:
        log(f"  Duna periapsis {v.orbit.periapsis_altitude/1000:.0f} km too high for an Oberth capture — lowering ...")
        plow = _search_duna_periapsis_lower(sc, v)
        if plow is not None and plow["pe"] < v.orbit.periapsis_altitude * 0.85:
            v.control.remove_nodes()
            v.control.add_node(plow["ut"], prograde=plow["prograde"], radial=plow["radial"], normal=plow["normal"])
            _execute_node_manually(conn, sc, v, max_burn_s=200.0, max_throttle=1.0)
            log(f"  Duna periapsis now {v.orbit.periapsis_altitude/1000:.0f} km, LF {v.resources.amount('LiquidFuel'):.0f}")
    # 6) Warp to periapsis, then retro-capture ABOVE the atmosphere (pe floor 60 km > Duna's ~50 km top).
    ttp = v.orbit.time_to_periapsis
    if ttp and 0 < ttp < 1e7:
        log(f"  in Duna SOI; warping {ttp:.0f}s to periapsis ({v.orbit.periapsis_altitude/1000:.0f} km) ...")
        sc.warp_to(sc.ut + ttp - 30.0)
        time.sleep(2)
    enc_pe = v.orbit.periapsis_altitude
    # LOOSE capture (high apoapsis, ~0.35 of Duna's SOI): capturing the hyperbolic arrival to a TIGHT low
    # orbit costs ~600 m/s, but the relay only needs a BOUND Duna orbit — a high eccentric one is cheap
    # (~250-350 m/s) and actually gives good apoapsis dwell for comms. This frees the Δv the rough-window
    # phasing correction needs. (Circularising to the 2640 km ring target is a no-refuel follow-up.)
    ap_target_m = max(enc_pe * 1.3, 0.35 * v.orbit.body.sphere_of_influence)
    log(f"  LOOSE-capturing to a bound Duna orbit: periapsis ~{enc_pe/1000:.0f} km, apoapsis ceiling {ap_target_m/1000:.0f} km")
    _retro_capture(conn, sc, v, log, ap_target_m=ap_target_m, pe_floor_m=1_200_000.0, max_s=400.0)
    return v.orbit.body.name == "Duna" and v.orbit.periapsis_altitude > 55_000.0


# --------------------------------------------------------------------------------------------------
# BODY-AGNOSTIC interplanetary transfer + capture + circularize to a target altitude. Works for ANY
# Sun-orbiting target (Duna, Eve, ...) using the VALIDATED precise Lambert ejection + the RELIABLE
# next_orbit.periapsis correction metric (kRPC distance_at_closest_approach is a misleading single-conic
# estimate — see the math audit). Aims the encounter periapsis at the synchronous radius so a single
# retrograde burn circularizes the relay there.
# --------------------------------------------------------------------------------------------------
def _encounter_periapsis(node, target_name: str):
    """Periapsis ALTITUDE of the resulting target-SOI orbit (follow the patched-conic next_orbit chain),
    or None if this node yields no encounter. The reliable metric, not distance_at_closest_approach."""
    try:
        o = node.orbit
        for _ in range(4):
            nxt = o.next_orbit
            if nxt is None:
                return None
            if nxt.body.name == target_name:
                return nxt.periapsis_altitude
            o = nxt
    except Exception:
        return None
    return None


def _search_correction(sc, v, target_name: str, ut: float, want_pe_m: float,
                       pg_w: float, pg_s: float, nrm_w: float, nrm_s: float):
    """Grid-search a small (prograde, normal) correction at ``ut`` whose target-SOI encounter periapsis
    is CLOSEST to ``want_pe_m`` (the synchronous altitude). Body-agnostic. Returns best node dict / None."""
    best = None
    for pg in _frange(-pg_w, pg_w, pg_s):
        for nrm in _frange(-nrm_w, nrm_w, nrm_s):
            v.control.remove_nodes()
            node = v.control.add_node(ut, prograde=pg, normal=nrm, radial=0.0)
            pe = _encounter_periapsis(node, target_name)
            v.control.remove_nodes()
            if pe is None:
                continue
            score = abs(pe - want_pe_m)
            if best is None or score < best["score"]:
                best = {"ut": ut, "prograde": pg, "normal": nrm, "radial": 0.0, "pe": pe, "score": score}
    return best


def _circularize_here(conn, sc, v, log):
    """At the current point (intended: the target periapsis = synchronous radius), burn to circularize:
    node prograde = v_circ - v_now (retrograde capture). Returns the achieved eccentricity."""
    o = v.orbit
    mu = o.body.gravitational_parameter
    r = o.radius
    v_circ = math.sqrt(mu / r)
    dv = v_circ - o.speed                              # negative => retrograde capture burn
    v.control.remove_nodes()
    v.control.add_node(sc.ut + 1.0, prograde=dv, normal=0.0, radial=0.0)
    _execute_node_manually(conn, sc, v, max_burn_s=400.0, max_throttle=1.0)
    return v.orbit.eccentricity


def transfer_to_body(conn, sc, bridge, v, target_name: str, target_alt_km: float) -> bool:
    """Eject (precise Lambert) -> correct to a synchronous-altitude encounter -> coast to SOI -> capture
    + circularize at the synchronous radius. Body-agnostic; the relay ends in a near-circular orbit at
    ``target_alt_km`` around ``target_name``."""
    from ksp_lab import transfer_planner as _tp
    target = sc.bodies[target_name]
    R_t = target.equatorial_radius
    want_pe = target_alt_km * 1000.0
    # 1) PRECISE ejection (validated body-agnostic Lambert; node placed at the parking-orbit periapsis).
    plan = _tp.plan_transfer(sc, v, v.orbit.body.name, target_name)
    nd, win = plan["ejection"], plan["window"]
    v.control.remove_nodes()
    v.control.add_node(nd["ut"], prograde=nd["prograde"], normal=nd["normal"], radial=nd["radial"])
    log(f"  EJECT -> {target_name}: {nd['dv']:.0f} m/s at UT {round(nd['ut'])} "
        f"(Lambert |vinf| {win['vinf_mag']:.0f} m/s, tof {win['tof']/(426*21600):.2f} yr)")
    _execute_node_manually(conn, sc, v, max_burn_s=400.0, max_throttle=1.0)
    if not _wait_until_sun_orbit(sc, v):
        log(f"  did not escape Kerbin SOI after the ejection (still {v.orbit.body.name})"); return False
    # 2) Mid-course correction: aim the encounter periapsis at the synchronous radius.
    for attempt in range(1, 4):
        mid_ut = sc.ut + max(6 * 3600.0, 0.06 * win["tof"])
        best = _search_correction(sc, v, target_name, mid_ut, want_pe, 300.0, 30.0, 150.0, 30.0)
        if best is None:
            best = _search_correction(sc, v, target_name, mid_ut, want_pe, 900.0, 45.0, 500.0, 50.0)
        if best is None:
            log(f"  correction {attempt}/3: no {target_name} encounter in grid; coasting a bit and retrying ...")
            sc.warp_to(sc.ut + 5.0 * 6 * 3600.0); continue
        v.control.remove_nodes()
        v.control.add_node(best["ut"], prograde=best["prograde"], normal=best["normal"], radial=best["radial"])
        log(f"  CORRECTION {attempt}: aim encounter pe {best['pe']/1000:.0f} km (want {target_alt_km:.0f}) "
            f"-> pg {best['prograde']:.0f} nrm {best['normal']:.0f}")
        _execute_node_manually(conn, sc, v, max_burn_s=150.0, max_throttle=0.7)
        if 0 < (v.orbit.time_to_soi_change or 0) < 1e9:
            break
    # 3) Coast to the target SOI.
    for _ in range(3):
        if v.orbit.body.name == target_name:
            break
        soi_dt = v.orbit.time_to_soi_change
        if soi_dt and 0 < soi_dt < 1e9:
            log(f"  coasting {soi_dt/(6*3600):.0f} Kerbin-days to the {target_name} SOI ...")
            sc.warp_to(sc.ut + soi_dt + 30.0); time.sleep(3)
        else:
            break
    if v.orbit.body.name != target_name:
        log(f"  ABORT: never entered the {target_name} SOI (still {v.orbit.body.name})"); return False
    # 4) Warp to periapsis (intended = synchronous radius) and circularize.
    ttp = v.orbit.time_to_periapsis
    if ttp and 0 < ttp < 1e7:
        log(f"  in {target_name} SOI; pe {v.orbit.periapsis_altitude/1000:.0f} km; warping {ttp/(6*3600):.0f} d to periapsis ...")
        sc.warp_to(sc.ut + ttp - 30.0); time.sleep(2)
    ecc = _circularize_here(conn, sc, v, log)
    alt = (v.orbit.apoapsis_altitude + v.orbit.periapsis_altitude) / 2000.0
    log(f"  CAPTURED at {target_name}: ~{alt:.0f} km circular (e={ecc:.3f}), target {target_alt_km:.0f} km")
    return v.orbit.body.name == target_name and ecc < 0.2


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

    # 0) For Duna: WAIT for the transfer window ON THE GROUND before spending any propellant. Launching early
    # forces the ~1,580 m/s in-flight raise+lower warp (the only way to time-warp a 1-2 yr wait from LKO at the
    # 50x cap); warping to the window on the pad costs ZERO fuel and lets the upper eject DIRECTLY into the
    # transfer (AI-Duna-Ring-X caught a near-window launch by luck and kept the WHOLE upper for the capture).
    # We advance the game via a stable orbiting vessel, then launch_to_lko fires fresh ~5 h before the window.
    _interplanetary = False
    try:
        _interplanetary = (sc.bodies[target_body].orbit.body.name == "Sun")
    except Exception:
        pass
    if _interplanetary:
        try:
            # PRECISE Lambert window (validated, body-agnostic, no MechJeb needed). Warp the GROUND to it so
            # the upper ejects DIRECTLY into the transfer (no ~1,580 m/s in-flight raise/lower warp).
            from ksp_lab import transfer_planner as _tp
            _w = _tp.find_transfer_window(sc, "Kerbin", target_body)
            win = _w["ut_dep"]
            wait_to = win - 3.0 * 3600.0                       # ~3 h early; the precise ejection node lands within ~1 LKO period
            if wait_to > sc.ut:
                yrs = (wait_to - sc.ut) / (426.0 * 21600.0)
                log(f"WAITING for the Kerbin->{target_body} window (precise Lambert, |vinf| {_w['vinf_mag']:.0f} m/s): "
                    f"warping {yrs:.2f} Kerbin-yr on the ground (NO fuel) to UT {round(wait_to)} ...")
                # Warp from the HIGHEST-altitude vessel so the multi-year warp runs at the 100,000x cap, not the
                # 50x LKO cap (a high/heliocentric craft is not altitude-limited). Sun-orbiting asteroids or the
                # high Duna relays work; pick the highest periapsis.
                hi = [vv for vv in sc.vessels if vv.orbit.body.name in ("Sun", "Duna", "Eve")
                      and str(vv.situation).split(".")[-1] == "orbiting"]
                hi.sort(key=lambda x: -(x.orbit.periapsis_altitude or 0.0))
                for cand in hi:
                    try:
                        sc.active_vessel = cand; time.sleep(2); break
                    except Exception:
                        continue
                sc.rails_warp_factor = 0
                sc.warp_to(wait_to); time.sleep(2)
                log(f"window reached (UT {round(sc.ut)}); launching DIRECTLY into the transfer")
        except Exception as exc:
            log(f"window pre-warp skipped ({exc}); launching now (in-flight raise/lower warp fallback)")

    # 1) Launch to a 100 km Kerbin PARKING orbit (proven ascent + booster force-separation). For Duna, the upper
    # must afford the FULL interplanetary budget (~3,785 m/s: warp raise ~790 + lower ~790 + ejection ~1,025 +
    # correction ~80 + Oberth periapsis-lower ~250 + capture ~600). The default min-tank upper (1x fuelTank.long,
    # 360 LF) delivers only ~3,540 -> the capture ran ~245 m/s dry (AI-Duna-Ring-U/T/W). The tank quantises 1x->2x
    # with no middle, and a 2x upper (720 LF) needs a 2-Mainsail booster to lift it at TWR>=1.3 (verified feasible
    # + passes the rocket-shape gate). 720 LF is ample margin (overkill is unavoidable at this quantum, but it
    # captures cleanly). Mun is close enough to keep the proven 1-engine default upper.
    # Per-target upper-stage budget (the ground pre-warp removes the in-flight raise/lower, so the upper
    # only needs LKO-circ + ejection + correction + capture). Eve: 562 + 1025 + 80 + 621 ≈ 2290 (calc'd
    # by tools eve-plan; +margin). Any other interplanetary target falls back to a generous default.
    insertion_override = {"Duna": 4400.0, "Eve": 2400.0}.get(target_body, 0.0)
    if insertion_override == 0.0 and target_body not in ("Mun",):
        try:
            insertion_override = 2600.0 if sc.bodies[target_body].orbit.body.name == "Sun" else 0.0
        except Exception:
            pass
    booster_engines = 2 if target_body in ("Duna", "Eve") else 1
    if not deploy_relay.launch_to_lko(sc, cfg, runner, bridge, name, 100.0,
                                      insertion_dv_override=insertion_override, booster_max_engines=booster_engines):
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
    elif sc.bodies[target_body].orbit.body.name == "Sun":
        # Any interplanetary target (Eve, ...) via the BODY-AGNOSTIC precise transfer + capture.
        if not transfer_to_body(c, sc, bridge, v, target_body, target_alt_km):
            log(f"{target_body} transfer/capture FAILED"); return 2
    else:
        log(f"target {target_body} not yet wired (a moon other than Mun)"); return 2

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
