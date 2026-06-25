"""CREWED EVE ORBITAL ROUND-TRIP — an Apollo-8-of-Eve. One kerbal flies Kerbin -> Eve, CAPTURES into
a bound Eve orbit, WAITS for the return window, EJECTS back toward Kerbin, AEROBRAKES into Kerbin's
atmosphere, descends on parachutes, and is RECOVERED alive.

================================ DEPRECATED ENTRY POINT ================================
REDUNDANT as an orchestration entry point — ASTRA decomposition is the general path; this file is
retained only for the helper functions the `launch`/`transfer`/`land`/`recover` primitives wrap (here:
`capture_at_eve_loose`, wrapped by the `transfer` primitive's loose mode, and `descend_and_recover`,
wrapped by the `recover` primitive). The module-level `main()` is kept ONLY as a manual fallback and is
itself DEPRECATED — prefer `tools/astra.py "<command>"`. Do not add new orchestration here.
========================================================================================

NOTE (ASTRA redesign): this monolithic mission script is now REDUNDANT orchestration. The general path
is ASTRA's task DECOMPOSITION into atomic primitives (src/ksp_lab/astra/primitives.py). This file's
proven flight functions are WRAPPED by primitives — capture_at_eve_loose by the `transfer` primitive's
loose mode, descend_and_recover by the `recover` primitive — so prefer `tools/astra.py "<command>"`. The
functions below remain the validated flight code the primitives call; only the top-level main() bundling
is superseded.

WHY ORBITAL, NOT A LANDING: tools/design_eve_crewed.py proved the lab cannot mass-close a Kerbin
vehicle that DELIVERS a crewed Eve ascent vehicle to Eve's surface (the delivery stack is a 1300-2600 t
TWR<0.8 noodle the gate rejects). So the achievable "send people to Eve and bring them back" is the
ORBITAL round trip: capture into Eve orbit (never land), then come home. This script flies exactly that.

HARD RULE: NO in-flight refuelling. The crew vehicle flies the whole mission on its own propellant; the
vacuum stage is budgeted (below) to cover Kerbin->Eve injection + Eve capture + the Eve->Kerbin return
ejection + the grid-search corrections, with margin.

COMPOSED FROM PROVEN CODE (per the design workflow — reuse, don't reinvent):
  - deploy_relay.launch_to_lko(...)            : the hardened Kerbin ascent (asparagus radial boosters,
                                                 explicit staging, force-separation) to a 100 km parking orbit.
  - deploy_relay_transfer's precise-Lambert ejection + MechJeb interplanetary node + grid-search
    encounter machinery (_warp_via_high / _execute_node_manually / _wait_until_sun_orbit /
    _search_duna_correction_grid / _frange / _predicted_periapsis_at) : reused by BOTH the outbound
    Eve capture and the RETURN leg. (We do NOT call transfer_to_body itself — it circularizes + Hohmanns
    DOWN to the target altitude, the costly burn that ran the vehicle dry; see capture_at_eve_loose.)
  - mj_to_mun._retro_capture(...) : the proven single retrograde-burn capture into a bound ellipse,
    reused for the CHEAP loose Eve capture.
  - transfer_planner.find_transfer_window(Eve, Kerbin) : the precise return-window date.

GENUINELY NEW HERE (the parts no existing tool covered):
  - design_crew_vehicle()  : the crewed ORBITAL round-trip ship (Mk1 pod + heatshield + Kerbin chutes,
                             vacuum stage budgeted for the whole interplanetary job).
  - board_crew()           : seat a real kerbal in the Mk1 pod after the headless launch (the pod lifts
                             off EMPTY on the probe-core control source) via the bridge /spawn-crew
                             endpoint, and VERIFY crew_count == 1 — so people actually fly to Eve.
  - capture_at_eve_loose() : Kerbin->Eve capture into a LOOSE bound ellipse (low periapsis, apoapsis
                             ~0.30 SOI) for ~146 m/s, instead of a circularize + Hohmann-down to a low
                             orbit. The cheap capture the vacuum budget assumed.
  - return_to_kerbin()     : eject from Eve toward KERBIN, then on arrival AEROBRAKE (target a ~35 km
                             Kerbin periapsis so the atmosphere captures the craft) instead of a
                             propulsive circularization — the return-leg variant of transfer_to_body.
  - descend_and_recover()  : after aerocapture, lower into the atmosphere, deploy chutes when safe,
                             confirm a landed/splashed crewed vessel, and recover it.

    PYTHONPATH=src python tools/crewed_eve_roundtrip.py configs/local-ksp.yaml AI-Eve-Crew
"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import design_chart
import deploy_relay
import deploy_relay_transfer as drt
from deploy_relay_transfer import (
    _execute_node_manually,
    _execute_precise,
    _frange,
    _predicted_periapsis_at,
    _search_duna_correction_grid,
    _wait_until_sun_orbit,
    _warp_via_high,
    log,
)

from mj_to_mun import _retro_capture

from ksp_lab import astro
from ksp_lab.bodies import EVE, KERBIN, SUN
from ksp_lab.design import (
    LandingSite,
    Phase,
    ShipRequirements,
    default_reserve_frac,
    design_ship,
)

DOCS = Path(__file__).resolve().parents[1] / "docs"
KERBIN_YEAR_S = 426 * 21600
# Kerbin aerobrake/aerocapture target periapsis: deep enough that the atmosphere captures a returning
# interplanetary craft in one pass (a ~35-40 km periapsis bleeds the ~3.4 km/s of arrival excess), but
# NOT so deep it overheats — the heat shield faces forward and 35 km is the community-standard survivable
# Kerbin aerocapture band for a capsule from an interplanetary return.
KERBIN_AEROBRAKE_PE_M = 35_000.0
# Chute-safe deploy guard (stock Mk16 rips above ~250 m/s or above ~5 km in thick air; below both is safe).
CHUTE_SAFE_SPEED_MPS = 250.0
CHUTE_SAFE_ALT_M = 5_000.0


# ==================================================================================================
# 1) DESIGN — the crewed ORBITAL round-trip vehicle (calculated, never guessed).
# ==================================================================================================
def _vacuum_budget_mps() -> dict:
    """Honest vacuum-stage Δv budget for the whole interplanetary job, every term from astro over the
    measured Eve/Kerbin/Sun catalogue. We CAPTURE LOOSELY at Eve (a cheap elliptical capture, not a
    costly low-circular one) and eject home from that same low periapsis, which is what makes the round
    trip close on one liftable stack.

    The IDEAL terms are tiny (the elliptical capture/eject are ~150 m/s each at the ideal periapsis pass),
    but the PROVEN Eve relay learned the hard way that real grid-search corrections cost FAR more than the
    ideal (a 2.5x-SOI ejection miss needs 200-500 m/s to fine-tune; the return needs the same). So we add
    a generous correction allowance to each interplanetary leg and a global margin — the dry-relay lesson."""
    r_k_park = KERBIN.radius_m + 100_000.0
    r_eve_low = EVE.low_orbit_radius_m()
    ap_ceiling = EVE.radius_m + 0.30 * EVE.soi_m            # modest elliptical capture apoapsis (~0.30 SOI)

    # Outbound: Kerbin parking orbit -> Eve transfer ejection (Oberth at Kerbin).
    k2e_eject = astro.interplanetary_departure(
        SUN.mu, KERBIN.mu, KERBIN.orbit_radius_m, EVE.orbit_radius_m, r_k_park)["ejection_dv"]

    # Eve arrival v_inf and the CHEAP elliptical capture (drop the hyperbola to a high ellipse).
    v_inf_arr = astro.transfer_arrival_excess_speed(SUN.mu, KERBIN.orbit_radius_m, EVE.orbit_radius_m)
    a_hyp = -EVE.mu / (v_inf_arr * v_inf_arr)               # arrival hyperbola SMA (negative)
    eve_capture = astro.capture_dv(EVE.mu, r_eve_low, a_hyp, ap_ceiling)

    # Return: Eve -> Kerbin ejection from the elliptical capture's low periapsis (fast there -> cheap push).
    e2k = astro.interplanetary_departure(SUN.mu, EVE.mu, EVE.orbit_radius_m, KERBIN.orbit_radius_m, r_eve_low)
    v_inf_ret = e2k["v_infinity"]
    v_pe_ell = astro.vis_viva_speed(EVE.mu, r_eve_low, (r_eve_low + ap_ceiling) / 2.0)
    v_hyp_ret = math.sqrt(v_inf_ret * v_inf_ret + 2.0 * EVE.mu / r_eve_low)
    eve_eject = max(0.0, v_hyp_ret - v_pe_ell)

    # CORRECTION allowances — the dominant real cost the ideal terms hide. Each interplanetary leg's
    # grid-search course-correction (establish the encounter, then deepen the periapsis) runs ~300-500
    # m/s in practice; budget 450 outbound + 450 return. Kerbin arrival is FREE (aerobrake, no burn).
    correction_out = 450.0
    correction_ret = 450.0

    terms = {
        "k2e_eject": k2e_eject,
        "eve_capture_elliptical": eve_capture,
        "correction_outbound": correction_out,
        "eve_eject_return": eve_eject,
        "correction_return": correction_ret,
    }
    ideal_sum = sum(terms.values())
    # Round the budget UP to a round figure with margin over the calculated sum (the prompt's ~3600 m/s
    # honest floor). The stock whole-tank quantum makes the realized stage a bit bigger anyway.
    budget = max(3600.0, math.ceil((ideal_sum * 1.10) / 100.0) * 100.0)
    terms["ideal_sum"] = ideal_sum
    terms["budget"] = budget
    return terms


def _kerbin_landing_site() -> "LandingSite":
    """Kerbin re-entry landing spec (Kerbin's OWN gravity + sea-level density; ~6 m/s touchdown). Shared
    by the design AND the launch writer so the WRITTEN crewed craft carries the same chute set the design
    sized — see launch_to_lko's crew/needs_heatshield/landing threading."""
    return LandingSite(body_g=KERBIN.surface_g, surface_rho=KERBIN.surface_rho,
                       target_touchdown_mps=6.0)


def design_crew_vehicle(name: str, render: bool = True):
    """Build + (optionally) render the crewed Eve orbital round-trip vehicle. Returns (design, report)."""
    budget = _vacuum_budget_mps()
    log("CALCULATED vacuum-stage Δv budget (astro over the Eve/Kerbin/Sun catalogue):")
    log(f"  Kerbin->Eve ejection        : {budget['k2e_eject']:.0f} m/s")
    log(f"  Eve elliptical capture      : {budget['eve_capture_elliptical']:.0f} m/s  (loose ~0.30 SOI apoapsis)")
    log(f"  outbound grid correction    : {budget['correction_outbound']:.0f} m/s  (allowance — real cost the ideal hides)")
    log(f"  Eve->Kerbin return ejection : {budget['eve_eject_return']:.0f} m/s  (from the elliptical periapsis)")
    log(f"  return grid correction      : {budget['correction_return']:.0f} m/s  (allowance)")
    log(f"  Kerbin arrival              : 0 m/s  (FREE aerobrake — heat shield forward)")
    log(f"  -> ideal sum {budget['ideal_sum']:.0f}, sized vacuum budget {budget['budget']:.0f} m/s (+margin)")

    # Kerbin re-entry landing site: Kerbin's OWN gravity + sea-level density (the chutes land at Kerbin,
    # not Eve). target 6 m/s splashdown/touchdown.
    kerbin_landing = _kerbin_landing_site()
    req = ShipRequirements(
        name=name,
        mission_type="eve_crewed_orbital_round_trip",
        crew=1,                                            # ONE kerbal (Mk1 pod) keeps the mass liftable
        payload_t=0.2,                                     # small science/supplies package
        phases=[
            # FIRE ORDER: Kerbin booster first (asparagus), then the vacuum interplanetary stage.
            Phase("kerbin_booster", 4200.0, twr_body_g=KERBIN.surface_g, min_twr=1.3,
                  reserve_frac=default_reserve_frac(KERBIN.surface_g)),
            Phase("vacuum", budget["budget"], twr_body_g=9.81, min_twr=0.5,
                  reserve_frac=default_reserve_frac(0.0)),
        ],
        landing=kerbin_landing,                            # chutes sized for KERBIN re-entry
        needs_heatshield=True,                             # forward heat shield for Eve-return aerocapture
        needs_legs=False,                                  # splashdown / chute land — no legs needed
        needs_docking=False,
        max_engine_count=1,                                # single core engine + radial pods (Eve pattern)
        radial_booster_count=4,                            # 4 asparagus strap-ons lift the heavy upper
    )
    d = design_ship(req)
    rep = design_chart.looks_like_a_rocket(d)
    est = d.estimates
    log(f"DESIGN {d.name}: wet {est['wet_mass_t']:.1f} t, total Δv {est['total_delta_v_mps']:.0f} m/s "
        f"(booster Δv {est.get('booster_delta_v_mps', 0):.0f}), launch TWR {est['launch_twr']}, "
        f"{int(est['stage_count'])} stages + {req.radial_booster_count} radial pods, "
        f"{int(est['parachutes'])} chutes, feasible={d.feasible}")
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


# ==================================================================================================
# 1b) OUTBOUND CAPTURE — Kerbin->Eve, into a LOOSE bound ELLIPSE (cheap), never a low-circular orbit.
#
# WHY NOT transfer_to_body(): that helper circularizes at the encounter periapsis and then HOHMANNS
# DOWN to the requested low altitude (~104 km) — an enormous Δv the budget never paid for, which is
# what ran the crew vehicle DRY (it dropped from the natural ~7,600 km encounter to a 104 km low orbit
# at e=0.63). The budget assumed a CHEAP elliptical capture (~146 m/s): drop the arrival hyperbola to
# a bound ellipse with a LOW periapsis (~104 km, for an Oberth-cheap RETURN ejection) and a HIGH
# apoapsis (~0.30 SOI). So we reuse transfer_to_body's PROVEN ejection + grid-search-establish, then
# capture with the PROVEN _retro_capture into that loose ellipse — no circularize, no Hohmann-down.
# ==================================================================================================
def _eve_capture_apoapsis_ceiling_m() -> float:
    """Apoapsis ALTITUDE (above Eve's surface) of the loose capture ellipse — the SAME 0.30*SOI ceiling
    the vacuum budget's eve_capture term assumed, so the realized capture matches the sized Δv."""
    return 0.30 * EVE.soi_m


def capture_at_eve_loose(conn, sc, bridge, v) -> bool:
    """Kerbin parking orbit -> a LOOSE bound Eve orbit. Ejection + grid-search encounter establishment
    are transfer_to_body's proven machinery (reused verbatim, aiming the encounter periapsis LOW so the
    return ejection is Oberth-cheap); the capture is a single retrograde burn at periapsis that drops the
    hyperbola to a bound ellipse (apoapsis ~0.30 SOI), NOT a circularization + Hohmann-down. Returns True
    once bound at Eve with a low, safe periapsis."""
    from ksp_lab import transfer_planner as _tp
    target_name = "Eve"
    target = sc.bodies[target_name]
    # BUG 2 FIX (shared in-space safety): turn OFF MechJeb's autostager before this interplanetary leg. It
    # autostages during ANY burn — including the capture burn here — and on a crewed/heat-shield craft it
    # blindly fired the payload/heat-shield decoupler mid-capture and stranded the crew pod. Disabling it
    # makes the explicit guarded ascent loop the SOLE stager, so no decoupler ever fires in space.
    try:
        r = bridge.mj_disable("staging")
        log(f"  MechJeb autostager DISABLED for the in-space capture ({r.get('disabled')})")
    except Exception as exc:
        log(f"  mj-disable(staging) skipped ({exc})")
    try:
        sc.target_body = target
    except Exception as exc:
        log(f"  could not set target ({exc})")

    # Aim the encounter periapsis at Eve's LOW orbit altitude (~104 km) — low periapsis = Oberth-cheap
    # capture AND Oberth-cheap return ejection. The loose apoapsis (0.30 SOI) is set by the capture burn.
    want_pe = EVE.low_orbit_radius_m() - EVE.radius_m
    pe_floor = EVE.atmosphere_top_m + 5_000.0                 # capture periapsis must clear Eve's 90 km air
    ap_ceiling_m = _eve_capture_apoapsis_ceiling_m()

    # 1) EJECT (precise Lambert cross-check, then MechJeb interplanetary node) — same as transfer_to_body.
    win = {"ut_dep": sc.ut, "tof": 0.40 * KERBIN_YEAR_S, "vinf_mag": 0.0}
    try:
        _plan = _tp.plan_transfer(sc, v, v.orbit.body.name, target_name)
        win = _plan["window"]
        log(f"  Lambert cross-check: |vinf| {win['vinf_mag']:.0f} m/s, tof {win['tof']/KERBIN_YEAR_S:.2f} yr")
    except Exception as exc:
        log(f"  Lambert cross-check skipped ({exc})")
    rr = bridge.mj_plan(target=target_name, operation="interplanetary")
    if not (rr.get("planned") and v.control.nodes):
        log(f"  EVE EJECT FAILED: MechJeb interplanetary transfer produced no node ({rr})"); return False
    log(f"  EJECT -> Eve (MechJeb interplanetary): {rr.get('dv', 0):.0f} m/s at UT {round(rr.get('ut', 0))}")
    _nd0 = v.control.nodes[0] if v.control.nodes else None
    if _nd0 is not None and (_nd0.ut - sc.ut) > 6 * 3600.0 and v.orbit.periapsis_altitude < 2_000_000.0:
        log(f"  ejection node {(_nd0.ut - sc.ut)/(6*3600):.0f} Kerbin-days out in a low orbit -> "
            f"advancing the clock via a high vessel ...")
        _warp_via_high(sc, _nd0.ut)
        sc.active_vessel = v
        time.sleep(2)
        v = sc.active_vessel
    _execute_node_manually(conn, sc, v, max_burn_s=400.0, max_throttle=1.0)
    if not _wait_until_sun_orbit(sc, v):
        log(f"  did not escape Kerbin SOI on the outbound (still {v.orbit.body.name})"); return False

    # 2) ESTABLISH the Eve encounter with the PROVEN grid search, aiming the periapsis LOW (~104 km).
    try:
        sc.target_body = target
    except Exception:
        pass
    atmo_top = EVE.atmosphere_top_m + 5_000.0
    for attempt in range(1, 4):
        if v.orbit.body.name == target_name or (0 < (v.orbit.time_to_soi_change or 0) < 1e9):
            break
        mid_ut = sc.ut + 0.25 * win["tof"]
        if mid_ut > sc.ut + 120.0 and v.orbit.body.name != target_name:
            sc.warp_to(mid_ut)
            time.sleep(2)
        node_ut = sc.ut + 6.0 * 3600.0
        coarse = _search_duna_correction_grid(
            sc, v, node_ut, pg_width=600.0, pg_step=50.0, rad_vals=(0.0,),
            nrm_vals=(-300.0, -100.0, 0.0, 100.0, 300.0),
            target_name=target_name, want_pe_m=want_pe, atmo_top_m=atmo_top)
        if coarse is None:
            log(f"  Eve grid {attempt}: no candidate; retrying")
            continue
        best = _search_duna_correction_grid(
            sc, v, node_ut, seed_prograde=coarse["prograde"], pg_width=80.0, pg_step=15.0, rad_vals=(0.0,),
            nrm_vals=tuple(_frange(coarse["normal"] - 80.0, coarse["normal"] + 80.0, 20.0)),
            target_name=target_name, want_pe_m=want_pe, atmo_top_m=atmo_top) or coarse
        v.control.remove_nodes()
        v.control.add_node(best["ut"], prograde=best["prograde"], radial=best["radial"], normal=best["normal"])
        tag = (f"pe {best.get('duna_pe_m', 0)/1000:.0f} km" if best.get("encounter")
               else f"closest {best.get('closest_m', 0)/1e6:.0f} Mm")
        log(f"  EVE ESTABLISH (grid) {attempt}: aim {tag} via "
            f"({best['prograde']:.0f},{best['radial']:.0f},{best['normal']:.0f})")
        _nd = v.control.nodes[0] if v.control.nodes else None
        if _nd is not None and (_nd.ut - sc.ut) > 6.0 * 3600.0:
            _warp_via_high(sc, _nd.ut, buffer_s=300.0)
            sc.active_vessel = v
            time.sleep(3)
            v = sc.active_vessel
        try:
            bridge.mj_execute_node(autowarp=True)
        except Exception as exc:
            log(f"  mj-execute-node error ({exc})")
        for _w in range(80):
            if not v.control.nodes:
                break
            time.sleep(3)
        if v.control.nodes:
            log("  Eve node not consumed in time; clearing and proceeding")
            v.control.remove_nodes()

    # 3) Coast to Eve's SOI.
    for _ in range(4):
        if v.orbit.body.name == target_name:
            break
        dt = v.orbit.time_to_soi_change
        if dt and 0 < dt < 1e9:
            log(f"  coasting {dt/(6*3600):.0f} Kerbin-days to the Eve SOI ...")
            sc.warp_to(sc.ut + dt + 30.0)
            time.sleep(3)
        else:
            break
    if v.orbit.body.name != target_name:
        log(f"  EVE CAPTURE ABORT: never entered the Eve SOI (still {v.orbit.body.name})"); return False

    # 4) Warp to periapsis, then LOOSE-capture there: a single retrograde burn that drops the hyperbola
    #    to a bound ellipse (apoapsis ~0.30 SOI) while PRESERVING the low periapsis. NO circularize, NO
    #    Hohmann-down — that is the whole BUG-2 fix: keep the capture cheap (~146 m/s budgeted).
    ttp = v.orbit.time_to_periapsis
    if ttp and 0 < ttp < 1e7:
        log(f"  in Eve SOI; pe {v.orbit.periapsis_altitude/1000:.0f} km; warping {ttp/(6*3600):.0f} d to periapsis ...")
        sc.warp_to(sc.ut + ttp - 20.0)
        time.sleep(2)
    enc_pe = v.orbit.periapsis_altitude
    ap_target_m = max(ap_ceiling_m, enc_pe * 1.3)
    log(f"  LOOSE-capturing to a bound Eve ellipse: periapsis ~{enc_pe/1000:.0f} km, "
        f"apoapsis ceiling {ap_target_m/1000:.0f} km (~0.30 SOI) — no circularize, no Hohmann-down")
    _retro_capture(conn, sc, v, log, ap_target_m=ap_target_m, pe_floor_m=pe_floor, max_s=400.0)
    o = v.orbit
    log(f"  EVE CAPTURE: {o.periapsis_altitude/1000:.0f}x{o.apoapsis_altitude/1000:.0f} km "
        f"(e={o.eccentricity:.2f})")
    return o.body.name == target_name and 0 < o.apoapsis_altitude and o.periapsis_altitude > EVE.atmosphere_top_m


# ==================================================================================================
# 2) RETURN LEG — eject from Eve toward Kerbin, then AEROBRAKE at Kerbin (the new part).
#
# This is transfer_to_body run BACKWARD (Eve -> Kerbin) with the arrival changed: instead of a
# propulsive capture + Hohmann-to-sync, the craft targets a SHALLOW Kerbin periapsis so the atmosphere
# captures it for FREE (no return-capture fuel — the budget assumed this). The ejection + grid-correction
# machinery is the SAME proven code transfer_to_body uses; only the arrival handling differs.
# ==================================================================================================
def _kerbin_periapsis_after_node(node):
    """Periapsis ALTITUDE of the resulting Kerbin-SOI orbit for a candidate heliocentric node (walk the
    patched-conic next_orbit chain), or None if it yields no Kerbin encounter."""
    try:
        o = node.orbit
        for _ in range(4):
            nxt = o.next_orbit
            if nxt is None:
                return None
            if nxt.body.name == "Kerbin":
                return nxt.periapsis_altitude
            o = nxt
    except Exception:
        return None
    return None


def return_to_kerbin(conn, sc, bridge, v) -> bool:
    """From a bound Eve orbit: eject toward Kerbin (precise Lambert window cross-check + MechJeb
    interplanetary ejection), establish the Kerbin encounter with the proven grid search aimed at a
    ~35 km periapsis (so the atmosphere aerocaptures), coast to Kerbin's SOI. Returns True once inside
    Kerbin's SOI on an atmosphere-grazing approach. NO capture burn — the air does the braking."""
    from ksp_lab import transfer_planner as _tp
    target_name = "Kerbin"
    target = sc.bodies[target_name]
    try:
        sc.target_body = target
    except Exception as exc:
        log(f"  could not set target ({exc})")

    # 1) Cross-check the precise Lambert return window, then DELEGATE the ejection node to MechJeb's
    #    interplanetary transfer (the proven path that establishes a real encounter). Same as the
    #    outbound transfer_to_body, just dep=Eve / tgt=Kerbin.
    win = {"ut_dep": sc.ut, "tof": 0.40 * KERBIN_YEAR_S, "vinf_mag": 0.0}
    try:
        _plan = _tp.plan_transfer(sc, v, v.orbit.body.name, target_name)
        win = _plan["window"]
        log(f"  Lambert return cross-check: |vinf| {win['vinf_mag']:.0f} m/s, tof {win['tof']/KERBIN_YEAR_S:.2f} yr")
    except Exception as exc:
        log(f"  Lambert cross-check skipped ({exc})")
    rr = bridge.mj_plan(target=target_name, operation="interplanetary")
    if not (rr.get("planned") and v.control.nodes):
        log(f"  RETURN EJECT FAILED: MechJeb interplanetary transfer produced no node ({rr})")
        return False
    log(f"  EJECT -> Kerbin (MechJeb interplanetary): {rr.get('dv', 0):.0f} m/s at UT {round(rr.get('ut', 0))}")

    # If the ejection node is far out and we're low, advance the clock via a high vessel (beats the warp cap).
    _nd0 = v.control.nodes[0] if v.control.nodes else None
    if _nd0 is not None and (_nd0.ut - sc.ut) > 6 * 3600.0 and v.orbit.periapsis_altitude < 2_000_000.0:
        log(f"  return ejection node {(_nd0.ut - sc.ut)/(6*3600):.0f} Kerbin-days out -> advancing via a high vessel ...")
        _warp_via_high(sc, _nd0.ut)
        sc.active_vessel = v
        time.sleep(2)
        v = sc.active_vessel
    _execute_node_manually(conn, sc, v, max_burn_s=400.0, max_throttle=1.0)
    if not _wait_until_sun_orbit(sc, v):
        log(f"  did not escape Eve SOI on the return (still {v.orbit.body.name})")
        return False

    # 2) ESTABLISH the Kerbin encounter with the PROVEN grid search, aimed at a ~35 km periapsis so the
    #    atmosphere captures. atmo_top_m is set ABOVE the aerobrake target so a node landing in the
    #    [35 km, ~atmosphere top] band is REWARDED (it's the goal), not penalized like a surface impact.
    try:
        sc.target_body = target
    except Exception:
        pass
    want_pe = KERBIN_AEROBRAKE_PE_M
    # We WANT to graze the atmosphere; only a SUB-25 km periapsis risks too-deep/too-hot entry, so the
    # grid's hard penalty floor is 25 km (below that = too steep), and the want is 35 km.
    atmo_floor = 25_000.0
    for attempt in range(1, 4):
        if v.orbit.body.name == target_name or (0 < (v.orbit.time_to_soi_change or 0) < 1e9):
            break
        mid_ut = sc.ut + 0.25 * win["tof"]
        if mid_ut > sc.ut + 120.0 and v.orbit.body.name != target_name:
            sc.warp_to(mid_ut)
            time.sleep(2)
        node_ut = sc.ut + 6.0 * 3600.0
        coarse = _search_duna_correction_grid(
            sc, v, node_ut, pg_width=600.0, pg_step=50.0, rad_vals=(0.0,),
            nrm_vals=(-300.0, -100.0, 0.0, 100.0, 300.0),
            target_name=target_name, want_pe_m=want_pe, atmo_top_m=atmo_floor)
        if coarse is None:
            log(f"  return grid {attempt}: no candidate; retrying")
            continue
        best = _search_duna_correction_grid(
            sc, v, node_ut, seed_prograde=coarse["prograde"], pg_width=80.0, pg_step=15.0, rad_vals=(0.0,),
            nrm_vals=tuple(_frange(coarse["normal"] - 80.0, coarse["normal"] + 80.0, 20.0)),
            target_name=target_name, want_pe_m=want_pe, atmo_top_m=atmo_floor) or coarse
        v.control.remove_nodes()
        v.control.add_node(best["ut"], prograde=best["prograde"], radial=best["radial"], normal=best["normal"])
        tag = (f"pe {best.get('duna_pe_m', 0)/1000:.0f} km" if best.get("encounter")
               else f"closest {best.get('closest_m', 0)/1e6:.0f} Mm")
        log(f"  RETURN ESTABLISH (grid) {attempt}: aim {tag} via "
            f"({best['prograde']:.0f},{best['radial']:.0f},{best['normal']:.0f})")
        _nd = v.control.nodes[0] if v.control.nodes else None
        if _nd is not None and (_nd.ut - sc.ut) > 6.0 * 3600.0:
            _warp_via_high(sc, _nd.ut, buffer_s=300.0)
            sc.active_vessel = v
            time.sleep(3)
            v = sc.active_vessel
        try:
            bridge.mj_execute_node(autowarp=True)
        except Exception as exc:
            log(f"  mj-execute-node error ({exc})")
        for _w in range(80):
            if not v.control.nodes:
                break
            time.sleep(3)
        if v.control.nodes:
            log("  return node not consumed in time; clearing and proceeding")
            v.control.remove_nodes()

    # 3) Coast to Kerbin's SOI.
    for _ in range(4):
        if v.orbit.body.name == target_name:
            break
        dt = v.orbit.time_to_soi_change
        if dt and 0 < dt < 1e9:
            log(f"  coasting {dt/(6*3600):.0f} Kerbin-days to the Kerbin SOI ...")
            sc.warp_to(sc.ut + dt + 30.0)
            time.sleep(3)
        else:
            break
    if v.orbit.body.name != target_name:
        log(f"  RETURN ABORT: never entered the Kerbin SOI (still {v.orbit.body.name})")
        return False

    # 4) In the Kerbin SOI. Read the encounter periapsis; if it's ABOVE the atmosphere (a high-graze the
    #    mid-transfer correction left), trim it down to the aerobrake target with a cheap retro/radial burn
    #    so the air actually captures. If it's already in the [atmo_floor, atmosphere_top] band, leave it.
    pe = v.orbit.periapsis_altitude
    log(f"  IN KERBIN SOI: arrival periapsis {pe/1000:.0f} km, apoapsis "
        f"{v.orbit.apoapsis_altitude/1000:.0f} km, e={v.orbit.eccentricity:.2f}")
    if pe > KERBIN.atmosphere_top_m:
        log(f"  periapsis {pe/1000:.0f} km is above the {KERBIN.atmosphere_top_m/1000:.0f} km atmosphere — "
            f"lowering to the {KERBIN_AEROBRAKE_PE_M/1000:.0f} km aerobrake target ...")
        if not _lower_kerbin_periapsis(conn, sc, bridge, v, KERBIN_AEROBRAKE_PE_M):
            log("  WARNING: periapsis-lower didn't reach the atmosphere; descent guard will retry from periapsis")
    return v.orbit.body.name == target_name


def _lower_kerbin_periapsis(conn, sc, bridge, v, target_pe_m: float) -> bool:
    """Lower the Kerbin periapsis to ~target_pe_m with a precise retro maneuver node at apoapsis (or now,
    if there is no apoapsis on a hyperbolic arrival). Vis-viva Δv, flown by MechJeb's precise executor —
    the same pattern deploy_relay_transfer._lower_to_lko uses, retargeted to a deep aerobrake periapsis."""
    body = v.orbit.body
    mu = body.gravitational_parameter
    o = v.orbit
    # Burn at apoapsis if we have a bound apoapsis (highest leverage on periapsis); else burn now.
    burn_ut = sc.ut + 5.0
    r_burn = o.radius
    tta = o.time_to_apoapsis
    if o.apoapsis_altitude and o.apoapsis_altitude > 0 and tta and 0 < tta < 1e7:
        burn_ut = sc.ut + tta
        r_burn = o.apoapsis
    r_pe_target = body.equatorial_radius + target_pe_m
    a_now = o.semi_major_axis
    a_target = (r_burn + r_pe_target) / 2.0
    v_now = math.sqrt(mu * (2.0 / r_burn - 1.0 / a_now))
    v_target = math.sqrt(mu * (2.0 / r_burn - 1.0 / a_target))
    dv = v_target - v_now                                    # negative = retrograde, lowers the periapsis
    try:
        v.control.remove_nodes()
    except Exception:
        pass
    v.control.add_node(burn_ut, prograde=dv, normal=0.0, radial=0.0)
    _execute_precise(conn, sc, bridge, v)
    try:
        v.control.remove_nodes()
    except Exception:
        pass
    pe = v.orbit.periapsis_altitude
    log(f"  Kerbin periapsis now {pe/1000:.0f} km (target {target_pe_m/1000:.0f} km)")
    return pe <= KERBIN.atmosphere_top_m


# ==================================================================================================
# 3) DESCEND + RECOVER at Kerbin (the other new part).
# ==================================================================================================
def _surface_speed(v) -> float:
    try:
        return float(v.flight(v.orbit.body.reference_frame).speed)
    except Exception:
        try:
            return float(v.orbit.speed)
        except Exception:
            return 9e9


def descend_and_recover(conn, sc, v, max_passes: int = 6) -> bool:
    """After Kerbin aerocapture: ride the atmosphere down (warp toward periapsis between passes if still
    high/fast), DEPLOY parachutes once the safe-deploy guard clears (below ~250 m/s AND below ~5 km, or
    when already in thick low air), confirm the crewed vessel is landed/splashed with the crew alive, and
    RECOVER it. The heat shield faces forward through entry (point retrograde-ish via the pod's own SAS
    is left to the game's reentry-stable capsule shape; we keep hands off attitude so the shield leads)."""
    body = v.orbit.body
    # If we're on a fast bound/elliptical orbit after the first aerocapture pass, warp to the next
    # periapsis so the next atmospheric pass (and eventual descent) happens promptly rather than coasting
    # a full orbit in real time. Each loop checks: landed? deploy chutes? warp to the next pass?
    for _pass in range(max_passes):
        sit = str(v.situation).split(".")[-1]
        if sit in ("landed", "splashed"):
            break
        alt = v.flight().mean_altitude
        spd = _surface_speed(v)
        log(f"  descent pass {_pass+1}: alt {alt/1000:.1f} km, surface speed {spd:.0f} m/s, "
            f"pe {v.orbit.periapsis_altitude/1000:.0f} km, situation {sit}")
        # SAFE-DEPLOY GUARD: arm chutes when slow + low (they stay stowed until safe, then auto-full-deploy;
        # arming early is harmless because stock chutes hold until the safe envelope). Deploy when we're in
        # the thick low atmosphere and subsonic-ish.
        if alt < CHUTE_SAFE_ALT_M and spd < CHUTE_SAFE_SPEED_MPS:
            _deploy_chutes(v)
            # Let the chutes fully open and bring it down; poll for touchdown.
            if _wait_for_touchdown(v, timeout_s=300.0):
                break
            continue
        # Still high/fast. If on a bound orbit with periapsis inside the atmosphere, warp toward periapsis
        # for the next braking pass; if periapsis is above the atmosphere, we never capture — bail.
        if v.orbit.periapsis_altitude > body.atmosphere_depth:
            log("  periapsis above the atmosphere — not on a capturing trajectory; aborting descent")
            return False
        ttp = v.orbit.time_to_periapsis
        if ttp and 0 < ttp < 1e7 and alt > body.atmosphere_depth:
            log(f"  warping {ttp:.0f}s toward periapsis for the next atmospheric pass ...")
            sc.warp_to(sc.ut + ttp - 30.0)
            time.sleep(2)
        else:
            # Inside the atmosphere but still fast — let physics bleed speed a moment, then re-check.
            sc.rails_warp_factor = 0
            time.sleep(3)
        # Arm chutes pre-emptively once below the safe altitude even if still a touch fast (they hold).
        if v.flight().mean_altitude < CHUTE_SAFE_ALT_M:
            _deploy_chutes(v)

    sit = str(v.situation).split(".")[-1]
    if sit not in ("landed", "splashed"):
        # Final guard: ensure chutes are out and wait for touchdown.
        _deploy_chutes(v)
        _wait_for_touchdown(v, timeout_s=300.0)
        sit = str(v.situation).split(".")[-1]

    crew = _crew_count(v)
    on_kerbin = v.orbit.body.name == "Kerbin"
    log(f"  TOUCHDOWN check: situation {sit}, body {v.orbit.body.name}, crew aboard {crew}")
    if sit in ("landed", "splashed") and on_kerbin and crew >= 1:
        log(f"=== CREW DOWN SAFE on Kerbin ({sit}) with {crew} aboard — recovering ===")
        try:
            sc.recover_vessel(v)
            log("  vessel RECOVERED (crew returned to the Astronaut Complex)")
        except Exception as exc:
            log(f"  recover_vessel note ({exc}); crew is landed + alive on Kerbin (recoverable from the Tracking Station)")
        return True
    log(f"  RECOVERY FAILED: situation {sit}, body {v.orbit.body.name}, crew {crew}")
    return False


def _deploy_chutes(v) -> int:
    """Deploy every parachute that is armable but not yet deployed. Stock chutes hold (stay safe) until the
    safe envelope, so calling this early is harmless — they only fully open when slow + low."""
    n = 0
    for p in list(getattr(v.parts, "parachutes", []) or []):
        try:
            if not p.deployed:
                p.deploy()
                n += 1
        except Exception:
            pass
    if n:
        log(f"  armed/deployed {n} parachute(s) (will full-open inside the safe envelope)")
    return n


def _wait_for_touchdown(v, timeout_s: float = 300.0) -> bool:
    t0 = time.monotonic()
    last = ""
    while time.monotonic() - t0 < timeout_s:
        sit = str(v.situation).split(".")[-1]
        if sit in ("landed", "splashed"):
            log(f"  touchdown confirmed ({sit})")
            return True
        try:
            alt = v.flight().surface_altitude
            spd = _surface_speed(v)
            m = f"  descending: surf-alt {alt:.0f} m, speed {spd:.0f} m/s"
            if m != last:
                log(m)
                last = m
        except Exception:
            pass
        time.sleep(2)
    return str(v.situation).split(".")[-1] in ("landed", "splashed")


def _crew_count(v) -> int:
    try:
        return int(v.crew_count)
    except Exception:
        try:
            return len(v.crew)
        except Exception:
            return 0


def board_crew(sc, bridge, v, retries: int = 3, settle_s: float = 2.0) -> bool:
    """Put a REAL kerbal in the Mk1 pod after the HEADLESS launch. The generated crew vehicle flies on
    an inline probe core (a guaranteed headless control source through the vessel-switching warps), so
    the Mk1 pod lifts off EMPTY (kRPC reports crew_count == 0). This calls the bridge's /spawn-crew
    endpoint, which seats an available roster kerbal (or recruits one) into the first crewable part with
    a free seat — the Mk1 pod — so a person actually rides to Eve. Verifies crew_count == 1 afterward.

    Must run in flight with the crew vehicle active (it is, right after launch_to_lko). Returns True once
    at least one kerbal is confirmed aboard; logs and returns False if the seat could not be filled.
    ``settle_s`` is the post-call settle delay (real flight 2 s; 0 in tests)."""
    name = v.name
    if _crew_count(v) >= 1:
        log(f"  crew already aboard {name} (crew_count {_crew_count(v)})")
        return True
    for attempt in range(1, retries + 1):
        try:
            rr = bridge.spawn_crew(vessel=name)
            log(f"  /spawn-crew: {rr.get('message', rr)}")
        except Exception as exc:
            log(f"  /spawn-crew attempt {attempt}/{retries} error ({exc})")
            time.sleep(settle_s)
            continue
        time.sleep(settle_s)
        try:
            v = sc.active_vessel
        except Exception:
            pass
        if _crew_count(v) >= 1:
            log(f"  CREW ABOARD {name}: crew_count {_crew_count(v)} — a kerbal is riding to Eve")
            return True
        log(f"  spawn-crew attempt {attempt}/{retries} did not seat a kerbal yet (crew_count "
            f"{_crew_count(v)}); retrying ...")
        time.sleep(settle_s)
    log(f"  WARNING: could not confirm a kerbal aboard {name} after {retries} attempts "
        f"(crew_count {_crew_count(v)}) — NEEDS LIVE CHECK")
    return False


# ==================================================================================================
# 4) FLIGHT SEQUENCE — mirrors deploy_relay_transfer.main, for the crewed round trip.
# ==================================================================================================
def _current_phase(sc, v) -> str:
    """Best-effort detection of where we are, so a re-run can resume rather than relaunch. Crude but cheap:
    keyed on the active vessel's SOI and situation."""
    try:
        body = v.orbit.body.name
        sit = str(v.situation).split(".")[-1]
    except Exception:
        return "prelaunch"
    if body == "Eve":
        return "at_eve"
    if body == "Sun":
        return "in_transit"
    if body == "Kerbin":
        if sit in ("landed", "splashed"):
            return "recovered"
        if sit == "orbiting" and v.orbit.apoapsis_altitude and v.orbit.apoapsis_altitude < 2_000_000.0:
            return "in_lko"
        return "kerbin_soi"
    return "prelaunch"


def main() -> int:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    name = sys.argv[2] if len(sys.argv) > 2 else "AI-Eve-Crew"

    # DESIGN FIRST (offline-safe). Render the chart + hard-gate the shape before any flight, RULE 1.
    design, rep = design_crew_vehicle(name, render=True)
    if not design.feasible:
        log("DESIGN INFEASIBLE — refusing to fly the crew."); return 2
    if not rep["looks_like_a_rocket"]:
        log("DESIGN failed the geometry gate — refusing to fly the crew."); return 2

    # --- LIVE FLIGHT (the lab connects here; offline callers stop after the design above) ---------
    import yaml
    import krpc
    from ksp_lab.bridge_client import BridgeClient
    from ksp_lab.runner import AutomationRunner

    drt.cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))   # reused drt machinery reads drt.cfg["krpc"]
    cfg = drt.cfg
    bridge = BridgeClient(**cfg["bridge"])
    runner = AutomationRunner(cfg_path, offline=False)
    kc = cfg["krpc"]
    c = krpc.connect(name="crewed-eve", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center

    # RESUME: if our crew vehicle already exists in a later phase, pick it up instead of relaunching.
    v = None
    try:
        for vv in sc.vessels:
            if vv.name == name:
                v = vv
                break
    except Exception:
        pass
    phase = _current_phase(sc, v) if v is not None else "prelaunch"
    if v is not None and phase != "prelaunch":
        try:
            sc.active_vessel = v
            time.sleep(2)
            v = sc.active_vessel
        except Exception:
            pass
        log(f"RESUME: found {name} in phase '{phase}' — continuing from there")
        # A resumed vehicle from a pre-boarding run may still be empty (the headless launch leaves the
        # Mk1 pod empty until board_crew runs). If so, seat a kerbal now before continuing the mission.
        if _crew_count(v) < 1:
            log("RESUME: pod is empty — boarding a kerbal before continuing ...")
            if not board_crew(sc, bridge, v):
                log("RESUME crew boarding FAILED — refusing to fly an empty 'crewed' mission"); return 2

    # 0) WAIT for the Kerbin->Eve window ON THE GROUND (zero fuel) so the upper ejects directly into the
    #    transfer — the proven interplanetary pattern (no in-flight raise/lower warp). Only when prelaunch.
    if phase == "prelaunch":
        try:
            from ksp_lab import transfer_planner as _tp
            _w = _tp.find_transfer_window(sc, "Kerbin", "Eve")
            wait_to = _w["ut_dep"] - 3.0 * 3600.0
            if wait_to > sc.ut:
                yrs = (wait_to - sc.ut) / KERBIN_YEAR_S
                log(f"WAITING for the Kerbin->Eve window (|vinf| {_w['vinf_mag']:.0f} m/s): warping {yrs:.2f} "
                    f"Kerbin-yr on the ground (NO fuel) to UT {round(wait_to)} ...")
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
                log(f"window reached (UT {round(sc.ut)}); launching the crew DIRECTLY into the transfer")
        except Exception as exc:
            log(f"window pre-warp skipped ({exc}); launching now")

        # a) LAUNCH to a 100 km Kerbin parking orbit — REUSE launch_to_lko with the Eve booster recipe
        #    (single core engine + 4 radial pods; the vacuum stage sized for the full interplanetary budget).
        #    CREW=1 + forward heat shield + Kerbin chutes so the WRITTEN craft carries a crewable Mk1 pod a
        #    kerbal can board (without this the launcher wrote a headless crew=0 PROBE — crew_capacity 0).
        insertion_override = _vacuum_budget_mps()["budget"]
        log("MILESTONE: launching the crew to LKO ...")
        if not deploy_relay.launch_to_lko(sc, cfg, runner, bridge, name, 100.0,
                                          insertion_dv_override=insertion_override,
                                          booster_max_engines=1, radial_booster_count=4,
                                          crew=1, needs_heatshield=True,
                                          landing=_kerbin_landing_site()):
            log("launch to parking orbit FAILED"); return 2
        time.sleep(3)
        v = sc.active_vessel
        log(f"MILESTONE: IN LKO {round(v.orbit.periapsis_altitude/1000)}x"
            f"{round(v.orbit.apoapsis_altitude/1000)} km — outbound to Eve")
        # JETTISON THE ASCENT PAYLOAD FAIRING now, in orbit. The crewed capsule rode up shrouded (the
        # fairing was the fix for the blunt-capsule ascent drag/instability that burned the vehicle dry
        # suborbital); above the atmosphere the shroud must split away so the forward heat shield + chutes
        # are EXPOSED for the Eve aerocapture and the Kerbin-return reentry. Removing the shell leaves the
        # pod + heat shield + chutes intact (the payload decoupler is a separate part, never fired here).
        try:
            _fj = deploy_relay.jettison_payload_fairings(v)
            if _fj:
                log(f"  jettisoned {_fj} payload fairing(s) — capsule heat shield + chutes now exposed for reentry")
                time.sleep(2)
                v = sc.active_vessel
        except Exception as _exc:
            log(f"  fairing jettison skipped ({_exc})")
        # BOARD A KERBAL. The launch is headless (probe-core control source) so the Mk1 pod lifts off
        # EMPTY; seat a real kerbal now, in LKO, BEFORE the interplanetary legs — "send people to Eve"
        # requires a person aboard. Verifies crew_count == 1.
        log("MILESTONE: boarding a kerbal into the Mk1 pod (headless launch left it empty) ...")
        if not board_crew(sc, bridge, v):
            log("CREW BOARDING FAILED — refusing to fly an EMPTY 'crewed' mission to Eve"); return 2
        phase = "in_lko"

    # b) OUTBOUND: Kerbin -> Eve, CAPTURE into a LOOSE bound ELLIPSE (cheap). We do NOT call
    #    transfer_to_body here: it circularizes at the encounter periapsis and then Hohmanns DOWN to the
    #    requested low altitude — far more Δv than budgeted (it ran the crew vehicle dry at e=0.63). The
    #    budget paid for a ~146 m/s elliptical capture (low periapsis for an Oberth-cheap return ejection,
    #    apoapsis ~0.30 SOI), which capture_at_eve_loose flies via the proven _retro_capture.
    if phase in ("in_lko", "in_transit"):
        log(f"MILESTONE: transferring to Eve (LOOSE elliptical capture, apoapsis ~0.30 SOI / "
            f"{round(_eve_capture_apoapsis_ceiling_m()/1000)} km, low periapsis) ...")
        if not capture_at_eve_loose(c, sc, bridge, v):
            log("Eve transfer/capture FAILED"); return 2
        v = sc.active_vessel
        log(f"MILESTONE: CAPTURED AT EVE {round(v.orbit.periapsis_altitude/1000)}x"
            f"{round(v.orbit.apoapsis_altitude/1000)} km (e={v.orbit.eccentricity:.2f}) — crew in Eve orbit")
        phase = "at_eve"
        try:
            sc.save("persistent")
        except Exception:
            pass

    # c) WAIT for the Eve->Kerbin return window, advancing the clock via warp_via_high (beats the orbit cap).
    if phase == "at_eve":
        try:
            from ksp_lab import transfer_planner as _tp
            _w = _tp.find_transfer_window(sc, "Eve", "Kerbin")
            wait_to = _w["ut_dep"] - 3.0 * 3600.0
            if wait_to > sc.ut:
                yrs = (wait_to - sc.ut) / KERBIN_YEAR_S
                log(f"MILESTONE: RETURN WINDOW (Eve->Kerbin, |vinf| {_w['vinf_mag']:.0f} m/s) in {yrs:.2f} "
                    f"Kerbin-yr; warping the clock forward ...")
                _warp_via_high(sc, _w["ut_dep"], buffer_s=3.0 * 3600.0)
                # warp_via_high may have switched to a high vessel; switch BACK to the crew before any burn.
                sc.active_vessel = v
                time.sleep(3)
                v = sc.active_vessel
                log(f"  return window reached (UT {round(sc.ut)})")
        except Exception as exc:
            log(f"  return-window warp skipped ({exc}); proceeding to eject now")

        # d) RETURN LEG: eject toward Kerbin + aerobrake (the new code).
        log("MILESTONE: ejecting from Eve toward Kerbin (return leg) ...")
        if not return_to_kerbin(c, sc, bridge, v):
            log("Eve->Kerbin return/aerocapture setup FAILED"); return 2
        v = sc.active_vessel
        log(f"MILESTONE: IN KERBIN SOI, aerobrake periapsis {round(v.orbit.periapsis_altitude/1000)} km — descending")
        phase = "kerbin_soi"
        try:
            sc.save("persistent")
        except Exception:
            pass

    # e) DESCEND + RECOVER at Kerbin (chutes + recover).
    if phase in ("kerbin_soi",):
        log("MILESTONE: AEROBRAKE + chute descent at Kerbin ...")
        if not descend_and_recover(c, sc, v):
            log("descent/recovery FAILED"); return 2
        phase = "recovered"

    if phase == "recovered":
        log(f"=== MISSION COMPLETE: {name} flew Kerbin -> Eve orbit -> Kerbin and the CREW IS HOME SAFE ===")
        try:
            sc.save("persistent")
        except Exception:
            pass
        return 0

    log(f"ended in phase '{phase}' without completing — inspect the log above")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
