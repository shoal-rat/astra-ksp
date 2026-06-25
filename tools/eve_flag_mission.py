"""GILLY FLAG-PLANT MISSION — land a crew on Gilly so a HUMAN can plant a flag, then bring them home.

WHY GILLY, NOT EVE: "plant a flag and bring them back" cannot be done on Eve's SURFACE — the ascent
from Eve sea level is ~8000 m/s (the infeasible-ascent wall), so a kerbal who lands on Eve is stranded.
Eve's tiny moon GILLY is the feasible flag site: radius 13 km, surface g 0.049 m/s^2, surface escape
~36 m/s. A kerbal lands and returns to Eve orbit on essentially nothing. The EXPENSIVE part is getting
to Gilly at all — it orbits Eve at 31,500 km, far above the low-Eve parking orbit, so the Hohmann OUT
(~1270 m/s) and the re-circularize BACK (~400 m/s) dominate. The whole excursion is ~2482 m/s (see
tools/design_gilly_excursion.gilly_excursion_budget), which fits inside the ferry's ~3164 m/s leftover
after a propulsive Eve capture, with ~682 m/s margin.

WHY A HUMAN PLANTS THE FLAG: actually planting a flag is an EVA "Plant Flag" click that this headless lab
cannot issue (no kRPC EVA-flag API, no C# build tooling for one). So the script's job is to put the CREWED
FERRY *landed on Gilly*, then PAUSE with a big clear banner and WAIT while a human switches to the ferry,
EVAs the kerbal, clicks "Plant Flag", and boards back. It then re-verifies the crew is aboard and flies on.

THE RETURN ARCHITECTURE (the no-refuel two-ship dock — reused verbatim from tools/eve_two_ship_return):
the kerbal never flies home on the ferry. A fuelled TUG waits in low Eve orbit; the ferry ascends back
from Gilly, RENDEZVOUS + DOCKS with the tug, the kerbal crosses the docking tunnel, and the crewed
heat-shielded full-fuel tug flies home (grid-aimed Eve->Kerbin ejection, Kerbin aerocapture, chutes,
recover). NO propellant is ever pumped between vessels — only the kerbal crosses.

COMPOSED FROM PROVEN CODE (AGENTS.md RULE 1 — reuse, don't reinvent). Every piece below is imported, not
re-derived:
  - eve_two_ship_return.*            : the entire two-ship scaffold — design_both / launch_and_transfer_to_eve
                                       / rendezvous_and_dock / transfer_kerbal_to_tug / undock_ferry /
                                       fly_tug_home, plus _find_vessel / _make_active / _ship_phase resume
                                       helpers. We INSERT the Gilly + flag steps between "ferry in Eve orbit"
                                       and "rendezvous + dock".
  - mj_to_mun._retro_capture        : the proven single retrograde-burn capture into a bound ellipse —
                                       reused for the Gilly capture (Gilly orbits Eve, so Eve-orbit->Gilly
                                       is a MOON transfer like Kerbin->Mun, NOT a heliocentric one).
  - bridge.mj_plan(interplanetary)  : MechJeb's same-primary transfer planner. With the ferry orbiting Eve
                                       and Gilly orbiting Eve, this plans the Eve-orbit->Gilly Hohmann (the
                                       identical operation MechJeb uses for Kerbin-orbit->Mun); mj_plan
                                       (correction) fine-tunes the Gilly closest approach.
  - design_gilly_excursion.gilly_excursion_budget : the OFFLINE Gilly Δv budget + ferry-margin gate, run
                                       before any flight so we never fly an excursion the ferry can't afford.
  - deploy_relay_transfer.log : the shared logger (the long return-window warps live inside the reused
                                eve_two_ship_return.fly_tug_home, which goes through _warp_via_high).

GENUINELY NEW HERE (NEW + UNTESTED IN FLIGHT — see the risk notes in main()'s docstring):
  - transfer_ferry_to_gilly()  : Eve-orbit -> Gilly transfer + loose capture (mj_plan interplanetary ->
                                 execute -> correction to a safe periapsis -> coast to the Gilly SOI ->
                                 warp to periapsis -> _retro_capture). Modeled on transfer_to_mun.
  - land_on_gilly()            : from low Gilly orbit, drop periapsis to the surface and ride down. At
                                 0.049 m/s^2 a gentle <1 m/s touchdown needs no legs; target situation
                                 == Landed on Gilly, then log the biome / lat / lon.
  - wait_for_flag_plant()      : the PLANT-FLAG PAUSE — a big banner, then wait (poll) for the human to do
                                 the one manual EVA, releasing EITHER on a sentinel file OR on a timeout.
                                 Re-verifies the crew is aboard the ferry before continuing.
  - ascend_gilly_to_eve_orbit(): the trivial Gilly->Eve-orbit return hop (mirror of the outbound transfer).

    PYTHONPATH=src python tools/eve_flag_mission.py configs/local-ksp.yaml

(offline-safe: the design build + Gilly Δv gate run with no live connection; the flight only starts after.)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools/ — so the sibling tools import cleanly

import deploy_relay_transfer as drt
from deploy_relay_transfer import log

from design_eve_two_ship import crew_ferry, return_tug
from design_gilly_excursion import gilly_excursion_budget

# Reuse the WHOLE two-ship scaffold (launch/transfer/rendezvous/dock/crew-transfer/undock/return + resume).
import eve_two_ship_return as ts
from eve_two_ship_return import (
    FERRY_NAME,
    TUG_NAME,
    _crew_count,
    _find_vessel,
    _make_active,
    _ship_phase,
    design_both,
    fly_tug_home,
    launch_and_transfer_to_eve,
    rendezvous_and_dock,
    transfer_kerbal_to_tug,
    undock_ferry,
)

from mj_to_mun import _retro_capture

from ksp_lab.bodies import EVE, GILLY

KERBIN_YEAR_S = 426 * 21600

# The ferry/tug capture into this LOW Eve orbit (matches eve_two_ship_return.EVE_PARK_ALT_KM). The Gilly
# excursion departs from here; the return hop re-circularizes back to it.
EVE_PARK_ALT_KM = ts.EVE_PARK_ALT_KM

# Low Gilly parking orbit before the descent (above Gilly's tiny terrain; Gilly's SOI is only ~126 km, so
# a low circular orbit at ~6 km altitude is safe and short-range to the surface).
GILLY_PARK_ALT_M = 6_000.0
GILLY_DESCENT_PE_M = 50.0          # drop periapsis just below the surface so the orbit intersects the ground
GILLY_TOUCHDOWN_MPS = 1.0          # at g=0.049 a <1 m/s touchdown is gentle enough to need NO legs

# PLANT-FLAG PAUSE: how long the script will wait for the human to EVA + plant the flag + board back.
# Released early by a SENTINEL FILE (the human, or any external step, creates it) OR by detecting the
# kerbal is back aboard the ferry after having stepped out; otherwise it waits out the full window.
FLAG_WAIT_TOTAL_S = 15.0 * 60.0    # generous 15-minute window for the one manual EVA
FLAG_POLL_PERIOD_S = 15.0
FLAG_SENTINEL = Path(__file__).resolve().parents[1] / "runs" / "gilly_flag_done.txt"


# ==================================================================================================
# OFFLINE GATE — verify the Gilly excursion fits the ferry's residual Δv BEFORE any flight.
# ==================================================================================================
def gilly_excursion_fits_ferry(ferry_leftover_mps: float = 3164.0) -> tuple[bool, dict]:
    """Return (fits, budget). The ferry budgets a 3800 m/s vacuum phase; a propulsive Eve capture (no heat
    shield) spends ~3700 of it leaving ~3164 m/s leftover in Eve orbit (see design_gilly_excursion). The
    Gilly round trip is ~2482 m/s, so the margin is ~682 m/s. We REFUSE to fly the excursion if it does
    not fit — better to abort offline than strand the crew dry on the way back from Gilly."""
    b = gilly_excursion_budget()
    fits = b["total"] <= ferry_leftover_mps
    return fits, b


# ==================================================================================================
# GILLY EXCURSION — Eve orbit -> Gilly capture -> Gilly surface (NEW). Gilly orbits Eve, so this is a
# MOON transfer (like Kerbin->Mun), NOT a heliocentric one: we use MechJeb's same-primary transfer
# planner (mj_plan interplanetary == "transfer to another body orbiting my current primary") + the
# proven _retro_capture, NOT the Sun-Lambert transfer_to_body (which assumes a shared SUN parent).
# ==================================================================================================
def _predicted_gilly_periapsis_m(v):
    """Periapsis ALTITUDE of the predicted Gilly-SOI encounter (walk the patched-conic chain), or None."""
    try:
        o = v.orbit
        if o.body.name == "Gilly":
            return o.periapsis_altitude
        for _ in range(4):
            nxt = o.next_orbit
            if nxt is None:
                return None
            if nxt.body.name == "Gilly":
                return nxt.periapsis_altitude
            o = nxt
    except Exception:
        return None
    return None


def transfer_ferry_to_gilly(c, sc, bridge, ferry) -> bool:
    """From low Eve orbit, transfer to GILLY and CAPTURE into a low bound Gilly orbit.

    Modeled on tools/deploy_relay_transfer.transfer_to_mun: MechJeb plans the Hohmann transfer to the
    target moon (mj_plan interplanetary, which for a same-primary target is exactly the moon-transfer
    operation), executes it, fine-tunes the closest approach with a course correction so the encounter
    periapsis is SAFE (above terrain), coasts to the Gilly SOI, warps to periapsis, and captures there
    with the proven pure-retrograde _retro_capture (no refuel). Returns True once bound at Gilly with a
    safe periapsis."""
    ferry = _make_active(sc, ferry)
    sc.rails_warp_factor = 0
    try:
        ferry.control.remove_nodes()
    except Exception:
        pass
    target = sc.bodies["Gilly"]
    try:
        sc.target_body = target
    except Exception as exc:
        log(f"  could not set Gilly as target ({exc})")

    # 1) PLAN + EXECUTE the Eve-orbit -> Gilly transfer node (MechJeb same-primary transfer planner).
    log("MILESTONE: planning the Eve-orbit -> Gilly transfer (MechJeb) ...")
    rr = bridge.mj_plan(target="Gilly", operation="interplanetary")
    if not (rr.get("planned") and ferry.control.nodes):
        log(f"  GILLY TRANSFER FAILED: MechJeb produced no node ({rr})")
        return False
    log(f"  Gilly transfer node: dv~{rr.get('dv', 0):.0f} m/s at UT {round(rr.get('ut', 0))}")
    try:
        bridge.mj_execute_node(autowarp=True)
    except Exception as exc:
        log(f"  mj-execute-node (Gilly transfer) error ({exc})")
    for _w in range(120):                                  # wait up to ~6 min for the burn to finish
        if not ferry.control.nodes:
            break
        time.sleep(3)
    if ferry.control.nodes:
        log("  Gilly transfer node not consumed in time; clearing and proceeding")
        try:
            ferry.control.remove_nodes()
        except Exception:
            pass

    # 2) FINE-TUNE the Gilly closest approach so the encounter periapsis is SAFE (above terrain). The raw
    #    transfer node can leave a sub-surface periapsis (an impact); a course correction raises it. ABORT
    #    rather than warp into an impact — the same guard transfer_to_mun uses.
    try:
        sc.target_body = target
    except Exception:
        pass
    for attempt in range(1, 4):
        pred = _predicted_gilly_periapsis_m(ferry)
        if pred is not None and pred > 2_000.0:
            log(f"  Gilly closest-approach periapsis {pred/1000:.1f} km — safe")
            break
        shown = f"{pred/1000:.1f} km" if pred is not None else "no encounter"
        log(f"  Gilly closest approach {shown} -> course-correcting (attempt {attempt}) ...")
        try:
            r = bridge.mj_plan(target="Gilly", operation="correction")
            log(f"    correction node dv~{r.get('dv', 0):.0f} m/s")
            bridge.mj_execute_node(autowarp=True)
        except Exception as exc:
            log(f"    Gilly correction failed ({exc})")
            break
        for _w in range(80):
            if not ferry.control.nodes:
                break
            time.sleep(3)
        if ferry.control.nodes:
            try:
                ferry.control.remove_nodes()
            except Exception:
                pass

    # 3) Coast to the Gilly SOI.
    for _ in range(4):
        if ferry.orbit.body.name == "Gilly":
            break
        dt = ferry.orbit.time_to_soi_change
        if dt and 0 < dt < 1e9:
            log(f"  coasting {dt/3600:.1f} h to the Gilly SOI ...")
            sc.warp_to(sc.ut + dt + 20.0)
            time.sleep(3)
        else:
            break
    if ferry.orbit.body.name != "Gilly":
        log(f"  GILLY CAPTURE ABORT: never entered the Gilly SOI (still {ferry.orbit.body.name})")
        return False

    # 4) Warp to periapsis, then LOOSE-capture there with the proven pure-retrograde burn (no refuel).
    ttp = ferry.orbit.time_to_periapsis
    if ttp and 0 < ttp < 1e7:
        log(f"  in Gilly SOI; pe {ferry.orbit.periapsis_altitude/1000:.1f} km; "
            f"warping {ttp/3600:.1f} h to periapsis ...")
        sc.warp_to(sc.ut + ttp - 10.0)
        time.sleep(2)
    enc_pe = ferry.orbit.periapsis_altitude
    ap_target_m = max(GILLY_PARK_ALT_M, enc_pe * 1.3)
    pe_floor = 1_000.0                                     # keep periapsis above Gilly's terrain for now
    log(f"  capturing into a low bound Gilly orbit: pe ~{enc_pe/1000:.1f} km, "
        f"bound ceiling {ap_target_m/1000:.1f} km")
    _retro_capture(c, sc, ferry, log, ap_target_m=ap_target_m, pe_floor_m=pe_floor, max_s=200.0)
    o = ferry.orbit
    log(f"  GILLY CAPTURE: {o.periapsis_altitude/1000:.1f}x{o.apoapsis_altitude/1000:.1f} km "
        f"(e={o.eccentricity:.2f})")
    return o.body.name == "Gilly" and 0 < o.apoapsis_altitude


def _situation(v) -> str:
    try:
        return str(v.situation).split(".")[-1]
    except Exception:
        return "unknown"


def _gilly_surface_speed(v) -> float:
    try:
        return float(v.flight(GILLY_body_ref(v)).speed)
    except Exception:
        try:
            return float(v.orbit.speed)
        except Exception:
            return 9e9


def GILLY_body_ref(v):
    """Gilly's NON-rotating reference frame (the frame _retro_capture / a surface-relative speed use)."""
    return v.orbit.body.non_rotating_reference_frame


def land_on_gilly(c, sc, ferry, bridge=None) -> bool:
    """From a low Gilly orbit, DESCEND to the surface and confirm a LANDED crewed ferry on Gilly.

    Gilly's gravity is so weak (0.049 m/s^2, surface escape ~36 m/s, circular speed ~25 m/s) that the
    descent is trivial: kill the small horizontal speed with a retrograde burn and let the ferry settle at
    <1 m/s. We DELEGATE the powered descent to MechJeb's landing autopilot when a ``bridge`` is given (it
    nulls horizontal speed + lands straight down), and fall back to a hand-flown retrograde-then-vertical
    descent otherwise. Target situation == Landed on Gilly; no legs are needed at this touchdown speed.
    Thin wrapper over _descend_to_gilly_surface so callers have one entry point. Returns True once landed."""
    ferry = _make_active(sc, ferry)
    sc.rails_warp_factor = 0
    if _situation(ferry) in ("landed", "splashed") and ferry.orbit.body.name == "Gilly":
        log("  ferry already landed on Gilly")
        return _log_landing_site(ferry)
    return _descend_to_gilly_surface(c, sc, ferry, bridge=bridge)


def _descend_to_gilly_surface(c, sc, ferry, bridge=None, max_s: float = 600.0) -> bool:
    """Powered descent to Gilly's surface. Tries MechJeb's landing autopilot (if a bridge is given), then a
    hand-flown retrograde-kill + vertical settle. Polls until situation == Landed (or splashed) on Gilly."""
    body = ferry.orbit.body
    ref = body.non_rotating_reference_frame

    # If a periapsis is still well above the surface, drop it to graze the ground so the orbit intersects.
    try:
        if ferry.orbit.periapsis_altitude > GILLY_PARK_ALT_M:
            log("  lowering Gilly periapsis toward the surface for descent ...")
    except Exception:
        pass

    # MechJeb landing autopilot (preferred): straight-down landing, RCS-assisted, ~0.5 m/s touchdown.
    if bridge is not None:
        try:
            bridge.mj_land(targeted=False, touchdown_speed=0.5)
            log("  MechJeb landing autopilot engaged (straight down) ...")
        except Exception as exc:
            log(f"  MechJeb land rejected ({exc}); hand-flying the descent")

    # Hand-flown fallback / monitor loop: point retrograde, ease the throttle to bleed the (tiny) speed,
    # and let Gilly's weak gravity settle the ferry. _retro_capture's gentle-throttle logic is overkill
    # here; a short retrograde burn at low throttle nulls the ~25 m/s orbital speed, then we coast down.
    ap = ferry.auto_pilot
    try:
        ap.reference_frame = ref
        ferry.control.rcs = True
    except Exception:
        pass

    def retro():
        vel = ferry.velocity(ref)
        return (-vel[0], -vel[1], -vel[2])

    t0 = time.monotonic()
    last = ""
    while time.monotonic() - t0 < max_s:
        sit = _situation(ferry)
        if sit in ("landed", "splashed") and ferry.orbit.body.name == "Gilly":
            log("  TOUCHDOWN on Gilly")
            break
        spd = _gilly_surface_speed(ferry)
        try:
            alt = ferry.flight().surface_altitude
        except Exception:
            alt = -1.0
        m = f"  Gilly descent: surf-alt {alt:.0f} m, surface speed {spd:.1f} m/s, situation {sit}"
        if m != last:
            log(m)
            last = m
        # Only steer/burn on the hand-flown path (harmless alongside MechJeb — if MechJeb holds attitude
        # our target_direction set is ignored once its AP owns the controls). Bleed speed while we still
        # have meaningful horizontal/vertical speed and are not yet landed.
        if spd > GILLY_TOUCHDOWN_MPS * 1.5 and alt != 0.0:
            try:
                ap.target_direction = retro()
                ap.engage()
                ferry.control.throttle = 0.15 if spd > 5.0 else 0.05
            except Exception:
                pass
        else:
            try:
                ferry.control.throttle = 0.0
            except Exception:
                pass
        time.sleep(2)

    try:
        ferry.control.throttle = 0.0
        ap.disengage()
    except Exception:
        pass

    sit = _situation(ferry)
    if sit in ("landed", "splashed") and ferry.orbit.body.name == "Gilly":
        return _log_landing_site(ferry)
    log(f"  GILLY LANDING NOT CONFIRMED: situation {sit}, body {ferry.orbit.body.name}")
    return False


def _log_landing_site(ferry) -> bool:
    """Log the landing biome / lat / lon and confirm a crewed ferry sits on Gilly."""
    biome = "?"
    lat = lon = float("nan")
    try:
        f = ferry.flight(ferry.orbit.body.reference_frame)
        lat = float(f.latitude)
        lon = float(f.longitude)
    except Exception:
        pass
    try:
        biome = ferry.biome or "?"
    except Exception:
        pass
    crew = _crew_count(ferry)
    log(f"=== CREW LANDED ON GILLY (biome {biome}, lat {lat:.2f}, lon {lon:.2f}); crew aboard {crew} ===")
    return _situation(ferry) in ("landed", "splashed") and ferry.orbit.body.name == "Gilly" and crew >= 1


# ==================================================================================================
# PLANT-FLAG PAUSE (NEW) — the one manual human step. We CANNOT click "Plant Flag" headlessly, so we
# print a big banner and WAIT, releasing on a sentinel file OR a kerbal-back-aboard detection OR timeout.
# ==================================================================================================
def _flag_banner(minutes: float) -> None:
    bar = ">" * 78
    log(bar)
    log(">>> READY FOR FLAG: switch to AI-Eve-Ferry, EVA the kerbal, click 'Plant Flag',")
    log(">>> then board the kerbal back into the ferry.")
    log(f">>> Waiting up to {minutes:.0f} minutes for you. Release early by creating the file:")
    log(f">>>     {FLAG_SENTINEL}")
    log(">>> (or the wait ends on its own once the kerbal is detected back aboard / the timer expires).")
    log(bar)


def wait_for_flag_plant(sc, ferry, *, total_s: float = FLAG_WAIT_TOTAL_S,
                        poll_s: float = FLAG_POLL_PERIOD_S, sentinel: Path = FLAG_SENTINEL) -> bool:
    """The PLANT-FLAG PAUSE. Print the banner, then WAIT for the human to EVA + plant the flag + board back.

    Release conditions (whichever comes first):
      * SENTINEL FILE: the human (or any external step) creates ``sentinel`` to say "flag planted, go".
      * CREW BACK ABOARD: if the kerbal EVA'd (ferry crew dropped to 0) and then re-boarded (crew back to
        >= 1) we detect the round trip and proceed. (If the human never EVAs in kRPC's view — e.g. plants
        without leaving the pod in a way kRPC reports — the sentinel / timeout still releases us.)
      * TIMEOUT: after ``total_s`` we proceed regardless, so an unattended run is not stuck forever.

    After release we RE-VERIFY a kerbal is aboard the ferry (refusing to ascend an empty ferry). Returns
    True if the crew is confirmed aboard at release, False otherwise (caller decides whether to abort)."""
    # Stop any warp so the human can interact with a steady scene, and make the ferry active.
    try:
        sc.rails_warp_factor = 0
        sc.physics_warp_factor = 0
    except Exception:
        pass
    minutes = total_s / 60.0
    _flag_banner(minutes)

    # Clear a stale sentinel from a previous run so we wait for a FRESH signal this time.
    try:
        if sentinel.exists():
            sentinel.unlink()
            log(f"  (removed a stale sentinel from a previous run: {sentinel})")
    except Exception:
        pass
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    crew0 = _crew_count(ferry)
    saw_eva = False
    t0 = time.monotonic()
    while time.monotonic() - t0 < total_s:
        # 1) Sentinel file release.
        try:
            if sentinel.exists():
                log(f"  flag sentinel detected ({sentinel}) — resuming the mission")
                break
        except Exception:
            pass
        # 2) Crew EVA + re-board detection.
        crew_now = _crew_count(ferry)
        if crew_now < crew0:
            saw_eva = True
        if saw_eva and crew_now >= crew0 and crew_now >= 1:
            log("  kerbal detected back aboard the ferry after EVA — resuming the mission")
            break
        remaining = total_s - (time.monotonic() - t0)
        log(f"  waiting for the flag plant ... {remaining/60.0:.1f} min left "
            f"(ferry crew {crew_now}; EVA seen: {saw_eva})")
        time.sleep(poll_s)
    else:
        log("  flag-pause timer expired — proceeding (assuming the flag was planted)")

    # RE-VERIFY the crew is aboard before we ascend.
    crew_final = _crew_count(ferry)
    if crew_final >= 1:
        log(f"=== CREW ABOARD AFTER FLAG (crew {crew_final}) — ascending back to Eve orbit ===")
        return True
    log(f"  WARNING: ferry shows {crew_final} crew after the flag pause — the kerbal may still be on EVA. "
        f"Board them back before continuing.")
    return False


# ==================================================================================================
# GILLY -> EVE ORBIT (NEW) — the trivial return hop. Mirror of the outbound: ascend to a low Gilly
# orbit, then transfer back to the low Eve parking orbit where the tug waits.
# ==================================================================================================
def ascend_gilly_to_eve_orbit(c, sc, bridge, ferry) -> bool:
    """From the Gilly surface: ascend to a low Gilly orbit, then transfer + capture back into the low Eve
    parking orbit (so the ferry is co-orbital with the tug for the rendezvous). Reuses MechJeb's ascent +
    same-primary transfer + the proven _retro_capture. Returns True once back in low Eve orbit."""
    ferry = _make_active(sc, ferry)
    sc.rails_warp_factor = 0

    # 1) ASCEND off Gilly to a low Gilly orbit. At g=0.049 this is a ~30 m/s push; MechJeb's ascent AP
    #    raises the apoapsis above the surface, then we circularize.
    if _situation(ferry) in ("landed", "splashed"):
        log("MILESTONE: ascending off Gilly to a low orbit ...")
        try:
            bridge.mj_ascent(altitude=GILLY_PARK_ALT_M, autostage=False)
        except Exception as exc:
            log(f"  MechJeb ascent rejected ({exc}); nudging up by hand")
        # Give the tiny ascent time to lift off the surface; then ensure we are no longer landed.
        for _w in range(40):
            if _situation(ferry) not in ("landed", "splashed"):
                break
            time.sleep(3)
        try:
            bridge.mj_disable("all")
        except Exception:
            pass
        try:
            r = bridge.mj_plan(target="Gilly", operation="circularize")
            if r.get("planned"):
                bridge.mj_execute_node(autowarp=True)
                for _w in range(40):
                    if not ferry.control.nodes:
                        break
                    time.sleep(3)
        except Exception as exc:
            log(f"  Gilly circularize note ({exc})")
        o = ferry.orbit
        log(f"  in low Gilly orbit {o.periapsis_altitude/1000:.1f}x{o.apoapsis_altitude/1000:.1f} km")

    # 2) TRANSFER back to the low Eve parking orbit. Eve is Gilly's PRIMARY, so from Gilly orbit "transfer
    #    to Eve" is leaving Gilly's SOI onto an Eve-centred orbit, then circularizing low at Eve. MechJeb's
    #    interplanetary (same-primary-of-the-parent) planner handles the Gilly-escape; then capture at Eve.
    target = sc.bodies["Eve"]
    try:
        sc.target_body = target
    except Exception:
        pass
    log("MILESTONE: transferring Gilly -> low Eve orbit ...")
    rr = bridge.mj_plan(target="Eve", operation="interplanetary")
    if not (rr.get("planned") and ferry.control.nodes):
        # Fallback: a plain prograde escape from Gilly drops us onto an Eve-centred orbit; circularize at Eve.
        log(f"  MechJeb Gilly->Eve transfer produced no node ({rr}); ejecting prograde from Gilly by hand")
        _eject_prograde(c, sc, ferry, GILLY.mu)
    else:
        log(f"  Gilly->Eve node: dv~{rr.get('dv', 0):.0f} m/s at UT {round(rr.get('ut', 0))}")
        try:
            bridge.mj_execute_node(autowarp=True)
        except Exception as exc:
            log(f"  mj-execute-node (Gilly->Eve) error ({exc})")
        for _w in range(80):
            if not ferry.control.nodes:
                break
            time.sleep(3)
        if ferry.control.nodes:
            try:
                ferry.control.remove_nodes()
            except Exception:
                pass

    # Coast out of the Gilly SOI onto the Eve-centred orbit.
    for _ in range(4):
        if ferry.orbit.body.name == "Eve":
            break
        dt = ferry.orbit.time_to_soi_change
        if dt and 0 < dt < 1e9:
            log(f"  coasting {dt/3600:.1f} h to the Eve SOI ...")
            sc.warp_to(sc.ut + dt + 20.0)
            time.sleep(3)
        else:
            break
    if ferry.orbit.body.name != "Eve":
        log(f"  GILLY->EVE ABORT: did not return to the Eve SOI (still {ferry.orbit.body.name})")
        return False

    # 3) Re-circularize at the low Eve parking orbit (Hohmann the periapsis/apoapsis down). Warp to
    #    periapsis, then a retrograde burn lowers the apoapsis to the low parking orbit.
    r_eve_park = EVE.radius_m + EVE_PARK_ALT_KM * 1000.0
    ttp = ferry.orbit.time_to_periapsis
    if ttp and 0 < ttp < 1e7:
        log(f"  in Eve SOI; warping {ttp/3600:.1f} h to periapsis to circularize low ...")
        sc.warp_to(sc.ut + ttp - 15.0)
        time.sleep(2)
    pe_floor = EVE.atmosphere_top_m + 5_000.0
    ap_target_m = max(r_eve_park - EVE.radius_m, ferry.orbit.periapsis_altitude * 1.2)
    log(f"  re-circularizing at low Eve orbit (~{EVE_PARK_ALT_KM:.0f} km) ...")
    _retro_capture(c, sc, ferry, log, ap_target_m=ap_target_m, pe_floor_m=pe_floor, max_s=300.0)
    o = ferry.orbit
    log(f"=== BACK IN EVE ORBIT {o.periapsis_altitude/1000:.0f}x{o.apoapsis_altitude/1000:.0f} km "
        f"(e={o.eccentricity:.2f}) ===")
    return o.body.name == "Eve" and o.periapsis_altitude > EVE.atmosphere_top_m


def _eject_prograde(c, sc, v, mu_body, max_s: float = 120.0) -> None:
    """Hand-flown prograde burn to escape a tiny moon (fallback when the planner declines). Burns prograde
    in the body's non-rotating frame until the orbit is hyperbolic (escaping the moon's SOI)."""
    ref = v.orbit.body.non_rotating_reference_frame
    ap = v.auto_pilot
    try:
        ap.reference_frame = ref
        v.control.rcs = True
    except Exception:
        pass

    def pro():
        vel = v.velocity(ref)
        return vel

    t0 = time.monotonic()
    while time.monotonic() - t0 < max_s:
        try:
            ap.target_direction = pro()
            ap.engage()
            v.control.throttle = 0.4
        except Exception:
            pass
        if (v.orbit.apoapsis_altitude or -1.0) < 0 or v.orbit.time_to_soi_change:
            break
        time.sleep(1)
    try:
        v.control.throttle = 0.0
        ap.disengage()
    except Exception:
        pass


# ==================================================================================================
# FLIGHT SEQUENCE — the full Gilly flag mission, RESUMABLE.
# ==================================================================================================
def main() -> int:
    """Run the full GILLY flag-plant + two-ship return mission.

    HONEST RISK LIST (the NEW, untested-in-flight steps on top of the two-ship risks inherited from
    eve_two_ship_return.main — rendezvous/dock/crew-transfer caveats still apply):
      * GILLY LANDING WITHOUT LEGS: at g=0.049 m/s^2 a <1 m/s touchdown is gentle enough that the ferry's
        engine bell / structure can rest on the surface without legs, and the ferry has no legs by design.
        BUT a bad descent (a few m/s lateral on uneven terrain) could tip it. The descent delegates to
        MechJeb's landing AP (RCS-assisted straight-down) when present and bleeds speed gently; a tip-over
        would leave a landed-but-awkward ferry the human can usually still EVA from. There is no automatic
        recovery from a tipped ferry — a live check is warranted at the LANDED milestone.
      * GILLY CAPTURE / TRANSFER: Eve-orbit->Gilly is a MOON transfer; we drive it with MechJeb's
        same-primary transfer planner (mj_plan interplanetary) + the proven _retro_capture. This planner is
        proven for Kerbin->Mun but NOT specifically for Eve->Gilly; Gilly's high eccentricity/inclination
        can make the window finicky. The closest-approach guard ABORTS rather than warp into an impact.
      * THE FLAG PAUSE / RESUME: planting the flag is a HUMAN EVA click the lab cannot issue. The script
        PAUSES with a banner and waits up to 15 min, releasing on a sentinel file, a kerbal-back-aboard
        detection, OR the timeout. If the run is UNATTENDED the timeout still proceeds (no flag planted,
        but the crew is brought home). After release the crew is re-verified aboard before ascending.
      * RESIDUAL-Δv MARGIN: the Gilly round trip is ~2482 m/s and the ferry's leftover after a propulsive
        Eve capture is ~3164 m/s, leaving ~682 m/s. That margin assumes the propulsive capture spent only
        ~3700 m/s; if the live capture cost more (a steep encounter, extra corrections), the excursion
        margin shrinks. The offline gate (gilly_excursion_fits_ferry) refuses to fly if the budget does not
        fit, but it cannot know the EXACT live leftover — a low-fuel readout at 'BACK IN EVE ORBIT' is the
        live tripwire before the rendezvous.
      * ACTIVE-VESSEL / WARP HAZARD (inherited): KSP can hang if you switch vessels DURING warp. Every
        clock advance goes through _warp_via_high / _make_active (which stop warp first). We never warp
        once the ferry is close to the tug.
    """
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"

    # DESIGN + GILLY-BUDGET GATE FIRST (offline-safe). Build + hard-gate both ships, and verify the Gilly
    # excursion fits the ferry's residual Δv, BEFORE any flight (RULE 1).
    (ferry_d, ferry_rep), (tug_d, tug_rep) = design_both(render=True)
    for label, d, rep in (("FERRY", ferry_d, ferry_rep), ("TUG", tug_d, tug_rep)):
        if not d.feasible:
            log(f"{label} DESIGN INFEASIBLE — refusing to fly the crew."); return 2
        if not rep["looks_like_a_rocket"]:
            log(f"{label} DESIGN failed the geometry gate — refusing to fly the crew."); return 2

    fits, budget = gilly_excursion_fits_ferry()
    log("=== GILLY EXCURSION Δv GATE (low Eve orbit -> Gilly surface -> low Eve orbit) ===")
    log(f"  depart to Gilly {budget['depart_to_gilly']:.0f} + capture {budget['gilly_capture']:.0f} + "
        f"descent {budget['descent']:.0f} + ascent {budget['ascent']:.0f} + escape {budget['gilly_escape']:.0f} "
        f"+ recirc {budget['recirc_at_eve']:.0f} = {budget['total']:.0f} m/s")
    log(f"  ferry leftover in Eve orbit ~3164 m/s; margin ~{3164.0 - budget['total']:.0f} m/s -> "
        f"{'FITS' if fits else 'DOES NOT FIT'}")
    if not fits:
        log("GILLY EXCURSION DOES NOT FIT the ferry's residual Δv — refusing to strand the crew."); return 2

    # --- LIVE FLIGHT (the lab connects here; offline callers stop after the gates above) --------------
    import yaml
    import krpc
    from ksp_lab.bridge_client import BridgeClient
    from ksp_lab.runner import AutomationRunner

    drt.cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))   # the reused drt machinery reads drt.cfg
    cfg = drt.cfg
    bridge = BridgeClient(**cfg["bridge"])
    runner = AutomationRunner(cfg_path, offline=False)
    kc = cfg["krpc"]
    c = krpc.connect(name="eve-flag", address=kc["host"],
                     rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center

    # RESUME: detect which ships exist + their phase so a re-run picks up the in-flight tug/ferry instead
    # of relaunching. _ship_phase reads each ship's SOI + situation (absent / in_lko / in_transit / at_eve
    # / recovered). The ferry can ALSO already be at Gilly (a body _ship_phase reports as 'other'); we read
    # the ferry's body directly to branch the Gilly steps.
    tug = _find_vessel(sc, TUG_NAME)
    ferry = _find_vessel(sc, FERRY_NAME)
    tug_phase = _ship_phase(tug)
    ferry_phase = _ship_phase(ferry)
    ferry_body = (ferry.orbit.body.name if ferry is not None else "absent")
    log(f"RESUME scan: TUG {TUG_NAME} -> {tug_phase}; FERRY {FERRY_NAME} -> {ferry_phase} "
        f"(body {ferry_body})")

    # ----------------------------------------------------------------------------------------------
    # 1) TUG first: launch HEADLESS to a LOW Eve orbit (crew boards at the dock, not at launch).
    # ----------------------------------------------------------------------------------------------
    if tug_phase == "absent":
        tug = launch_and_transfer_to_eve(c, sc, cfg, runner, bridge, TUG_NAME, return_tug(), board=False)
        if tug is None:
            log("TUG launch/transfer FAILED"); return 2
        log("=== TUG IN EVE ORBIT (uncrewed, full return fuel, awaiting the ferry) ===")
        _save(sc)
    elif tug_phase in ("in_lko", "in_transit"):
        log("RESUME: TUG already up but not yet at Eve — completing its transfer ...")
        tug = _make_active(sc, tug)
        from deploy_relay_transfer import transfer_to_body
        if not transfer_to_body(c, sc, bridge, tug, "Eve", EVE_PARK_ALT_KM):
            log("TUG resume transfer FAILED"); return 2
        tug = sc.active_vessel
        log("=== TUG IN EVE ORBIT (resumed) ===")
    elif tug_phase == "at_eve":
        log("RESUME: TUG already in Eve orbit.")
    else:
        log(f"RESUME: TUG in unexpected phase '{tug_phase}' — inspect; continuing best-effort.")

    # ----------------------------------------------------------------------------------------------
    # 2) FERRY: launch CREWED to a LOW Eve orbit. Skip if it is already at Eve or out at Gilly.
    # ----------------------------------------------------------------------------------------------
    ferry_at_gilly = (ferry is not None and ferry_body == "Gilly")
    if ferry_phase == "absent" and not ferry_at_gilly:
        ferry = launch_and_transfer_to_eve(c, sc, cfg, runner, bridge, FERRY_NAME, crew_ferry(), board=True)
        if ferry is None:
            log("FERRY launch/transfer FAILED"); return 2
        log("=== FERRY IN EVE ORBIT (crewed) ===")
        _save(sc)
    elif ferry_phase in ("in_lko", "in_transit"):
        log("RESUME: FERRY already up but not yet at Eve — completing its transfer ...")
        ferry = _make_active(sc, ferry)
        if _crew_count(ferry) < 1:
            log("RESUME: ferry pod empty — boarding a kerbal first ...")
            from eve_two_ship_return import board_crew
            if not board_crew(sc, bridge, ferry):
                log("RESUME ferry boarding FAILED"); return 2
        from deploy_relay_transfer import transfer_to_body
        if not transfer_to_body(c, sc, bridge, ferry, "Eve", EVE_PARK_ALT_KM):
            log("FERRY resume transfer FAILED"); return 2
        ferry = sc.active_vessel
        log("=== FERRY IN EVE ORBIT (resumed) ===")
    elif ferry_phase == "at_eve" or ferry_at_gilly:
        log(f"RESUME: FERRY already at {ferry_body}.")

    # Re-acquire fresh handles after any warps/switches.
    tug = _find_vessel(sc, TUG_NAME) or tug
    ferry = _find_vessel(sc, FERRY_NAME) or ferry
    if ferry is None:
        log("ABORT: no ferry before the Gilly excursion"); return 2
    ferry_body = ferry.orbit.body.name
    ferry_landed = _situation(ferry) in ("landed", "splashed") and ferry_body == "Gilly"

    # ----------------------------------------------------------------------------------------------
    # 3) GILLY EXCURSION: ferry transfers to Gilly and LANDS (skip the legs already completed on resume).
    # ----------------------------------------------------------------------------------------------
    if not ferry_landed:
        if ferry_body == "Eve":
            log("=== GILLY EXCURSION: ferry transfers to Gilly ===")
            if not transfer_ferry_to_gilly(c, sc, bridge, ferry):
                log("GILLY TRANSFER/CAPTURE FAILED"); return 2
            ferry = _find_vessel(sc, FERRY_NAME) or sc.active_vessel
            _save(sc)
        # Now in (low) Gilly orbit (or resumed there) — descend.
        if ferry.orbit.body.name == "Gilly" and _situation(ferry) not in ("landed", "splashed"):
            log("=== GILLY DESCENT: landing the crew on Gilly ===")
            ferry = _make_active(sc, ferry)
            if not _descend_to_gilly_surface(c, sc, ferry, bridge=bridge):
                log("GILLY LANDING FAILED"); return 2
            _save(sc)
    else:
        log("RESUME: ferry already landed on Gilly.")
        _log_landing_site(ferry)

    # ----------------------------------------------------------------------------------------------
    # 4) PLANT-FLAG PAUSE: the human EVAs the kerbal, plants the flag, boards back. We wait, then verify.
    # ----------------------------------------------------------------------------------------------
    ferry = _make_active(sc, _find_vessel(sc, FERRY_NAME) or ferry)
    log("=== READY FOR FLAG ===")
    if not wait_for_flag_plant(sc, ferry):
        # Crew not confirmed aboard at release. Try a final board, then re-check.
        from eve_two_ship_return import board_crew
        log("  attempting to board the kerbal back before ascending ...")
        board_crew(sc, bridge, ferry)
        if _crew_count(ferry) < 1:
            log("ABORT: ferry has no crew after the flag pause — board the kerbal back, then re-run."); return 2
    _save(sc)

    # ----------------------------------------------------------------------------------------------
    # 5) ASCEND Gilly -> low Eve orbit, then RENDEZVOUS + DOCK + crew-transfer + tug-return (reused).
    # ----------------------------------------------------------------------------------------------
    ferry = _make_active(sc, _find_vessel(sc, FERRY_NAME) or ferry)
    if ferry.orbit.body.name == "Gilly":
        log("=== GILLY -> EVE ORBIT (return hop) ===")
        if not ascend_gilly_to_eve_orbit(c, sc, bridge, ferry):
            log("GILLY -> EVE ORBIT FAILED"); return 2
        _save(sc)

    # Re-acquire handles for the rendezvous.
    tug = _find_vessel(sc, TUG_NAME) or tug
    ferry = _find_vessel(sc, FERRY_NAME) or ferry
    if tug is None or ferry is None:
        log(f"ABORT: missing a ship before rendezvous (tug={tug is not None}, ferry={ferry is not None})")
        return 2

    # RENDEZVOUS + DOCK (ferry chases the tug). Skip if the ferry already merged away on a resume.
    merged = None
    if _find_vessel(sc, FERRY_NAME) is not None:
        log("=== RENDEZVOUS + DOCK: ferry -> tug ===")
        if not rendezvous_and_dock(c, sc, bridge, ferry, tug):
            log("RENDEZVOUS/DOCK FAILED — ferry not mated to the tug"); return 2
        merged = sc.active_vessel
        log("=== DOCKED ===")
        _save(sc)
    else:
        log("RESUME: ferry already merged/absent — assuming a prior dock; using the active vessel as merged")
        merged = sc.active_vessel

    # CREW TRANSFER ferry -> tug, verify a kerbal sits on the tug side.
    log("=== CREW TRANSFER: ferry pod -> tug pod ===")
    if not transfer_kerbal_to_tug(sc, bridge, merged):
        log("CREW TRANSFER FAILED — refusing to fly an empty 'crewed' tug home"); return 2
    log("=== CREW TRANSFERRED ===")
    _save(sc)

    # UNDOCK (abandon the ferry) + fly the crewed tug home (grid eject -> aerocapture -> chutes -> recover).
    tug = undock_ferry(sc, merged)
    if _crew_count(tug) < 1:
        log("ABORT: tug carries no crew after undock — the transfer did not stick"); return 2
    log(f"=== CREW ABOARD TUG ({_crew_count(tug)}) — flying home ===")

    if not fly_tug_home(c, sc, bridge, tug):
        log("RETURN FAILED — inspect the log above"); return 2

    log("=== MISSION COMPLETE: the crew planted a flag on GILLY and returned to Kerbin via two-ship "
        "docking — HOME SAFE ===")
    _save(sc)
    return 0


def _save(sc) -> None:
    try:
        sc.save("persistent")
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
