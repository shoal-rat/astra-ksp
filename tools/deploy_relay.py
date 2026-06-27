"""Deploy an RA-100 relay comsat to a CIRCULAR orbit at a target altitude in the CURRENT launch body's
SOI (Kerbin keostationary ring, or a Kerbin parking orbit before a Mun/interplanetary transfer).

Reuses the PROVEN Starship launch sequence (clear pad -> write craft -> MechJeb ascent -> direct
booster ignition -> explicit staging -> LKO), then RAISES apoapsis to the target altitude and
CIRCULARIZES there with the calculated executors. Finally sets the vessel type to Relay so it forwards
the network. Every relay carries an RA-100 (the craft_writer bus default, now harvested correctly).

    PYTHONPATH=src python tools/deploy_relay.py configs/local-ksp.yaml <target_alt_km> <name>
"""
from __future__ import annotations

import sys
import time

import yaml

from ksp_lab import execute, plan
from ksp_lab.bridge_client import BridgeClient
from ksp_lab.runner import AutomationRunner


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _depth_from_root(part) -> int:
    """Tree depth of a part from the root command part. The PAYLOAD decoupler (between the relay bus and
    the final stage) is the SHALLOWEST decoupler — closest to the root; the inter-stage decouplers that
    drop spent boosters are DEEPER, down toward the engines."""
    d, p, seen = 0, part, set()
    while True:
        try:
            parent = p.parent
        except Exception:
            break
        if parent is None or id(parent) in seen:
            break
        seen.add(id(parent)); p = parent; d += 1
    return d


def _part_is_below(part, ancestor) -> bool:
    """True if ``ancestor`` lies on ``part``'s parent-chain (i.e. ``part`` is on the FAR side of ``ancestor``
    from the root — it would be JETTISONED when ``ancestor`` is decoupled). Walks UP via ``.parent`` (one
    fetch per step, like ``_depth_from_root``) and uses kRPC's own ``==`` (Python ``id()``/``is`` are
    UNRELIABLE on kRPC proxies — each ``.part``/``.parent`` access returns a fresh object, which is the bug
    that made the first version of this cross the decoupler and mis-fire the payload separator)."""
    p = part
    guard = 0
    while p is not None and guard < 1000:
        try:
            par = p.parent
        except Exception:
            return False
        if par is None:
            return False
        try:
            if par == ancestor:
                return True
        except Exception:
            return False
        p = par
        guard += 1
    return False


def _root_side_keeps_engine(vessel, dec) -> bool:
    """True if firing ``dec`` leaves the ACTIVE (root/pod) side still holding an engine — i.e. ``dec`` drops a
    SPENT stage and keeps propulsion. False means firing it strands the engineless PAYLOAD as the active
    vessel (the crewed bug: the heat-shield decoupler kept the capsule and jettisoned the whole upper stage).
    An engine is on the kept side exactly when it is NOT below ``dec`` (its parent-chain doesn't pass through
    ``dec``). If EVERY engine is below ``dec``, firing it leaves no propulsion -> protect it."""
    try:
        dec_part = dec.part
        engines = list(vessel.parts.engines)
    except Exception:
        return True                       # can't tell -> treat as inter-stage (old behaviour)
    for e in engines:
        try:
            if not _part_is_below(e.part, dec_part):
                return True               # this engine stays on the kept side -> safe inter-stage decoupler
        except Exception:
            pass
    return False


def _inter_stage_decouplers(vessel) -> list:
    """The decouplers ascent staging may fire, DEEPEST-first (the booster decoupler before any upper one),
    EXCLUDING any decoupler that would strand the engineless payload. An inter-stage decoupler drops a SPENT
    stage while the active (root/pod) side KEEPS an engine (``_root_side_keeps_engine``); a PAYLOAD decoupler
    leaves the payload with no propulsion and must never fire during ascent. The crewed vehicle has TWO such
    payload decouplers (heat-shield boundary + upper boundary), which the old shallowest-only heuristic
    mis-fired — dropping the whole upper stage. Returns [] when nothing is safe to fire."""
    try:
        decs = list(vessel.parts.decouplers)
    except Exception:
        return []
    if len(decs) <= 1:
        return []
    inter = [dd for dd in decs if _root_side_keeps_engine(vessel, dd)]
    if not inter:
        # Defensive fallback (no decoupler keeps an engine on the root side — shouldn't happen): protect only
        # the shallowest, the prior behaviour, so a conventional stack can still stage.
        payload = min(decs, key=lambda dd: _depth_from_root(dd.part))
        inter = [dd for dd in decs if dd is not payload]
    inter.sort(key=lambda dd: -_depth_from_root(dd.part))           # deepest (lowest booster) fires first
    return inter


# --- FAIL-FAST ascent abort detection -------------------------------------------------------------
# The launch loop used to poll for ~20 minutes (1200 s) waiting for apoapsis to reach the parking
# target, even after the ascent had already FAILED: the vehicle broke up (part count collapsed to ~the
# payload), or it was falling back below the atmosphere (apoapsis decaying, vertical speed negative), or
# it had crashed (landed/splashed after liftoff). Nothing logged and no monitor fired — the process just
# sat "alive" doing nothing until the timeout (this stranded the heavy tug for 20 minutes). The helper
# below inspects a small rolling state-history and ABORTS loudly the moment a real failure mode is seen,
# long before any timeout. It is a PURE function (no kRPC) so it can be unit-tested with a fake state
# sequence.
_FAILFAST_BAD_STREAK = 3          # consecutive bad polls before an abort fires (rides out a 1-frame transient)
_FAILFAST_PARTS_FRAC = 0.5        # part count below this fraction of the post-staging count = broke up


def _ascent_has_failed(state_history, *, post_staging_part_count: int, payload_part_count: int,
                       atmosphere_top_m: float = 70_000.0, target_apoapsis_m: float = 100_000.0,
                       bad_streak: int = _FAILFAST_BAD_STREAK):
    """PURE ascent-failure predicate. Returns ``(failed: bool, reason: str)``.

    ``state_history`` is a list of per-poll snapshots (oldest first), each a dict with keys:
        part_count    (int)   live active-vessel part count
        apoapsis_m    (float) apoapsis ALTITUDE in metres
        vertical_speed_mps (float) surface-frame vertical speed (negative = falling)
        situation     (str)   kRPC situation, e.g. "Vessel.Situation.flying" / "...landed"
        engine_lit    (bool)  is any engine currently lit/active
    Only the most recent poll plus a short tail are consulted. ``post_staging_part_count`` is the part
    count right after the booster separated (the live vehicle that SHOULD reach orbit); ``payload_part_count``
    is the bare comsat/crew payload count (what's left if it breaks up). Abort fires when ANY of these
    failure modes holds for the LAST ``bad_streak`` consecutive polls (so a single-frame glitch is ignored):

      1. PART-COUNT COLLAPSE: part_count < ~half the post-staging count (or down to roughly payload-only)
         — the vehicle broke up.
      2. FALLING BACK: below the atmosphere top, vertical speed negative, AND apoapsis strictly DECAYING
         across the streak — it is descending and cannot reach orbit.
      3. CRASHED: situation is landed/splashed AFTER liftoff (we only reach this loop post-liftoff).
      4. DIVERGING: apoapsis strictly decreasing every poll across the streak while the engine is lit and
         apoapsis is still well short of target — the burn is losing ground, not gaining it.
    """
    if not state_history:
        return False, ""
    n = max(1, int(bad_streak))
    if len(state_history) < n:
        return False, ""
    tail = state_history[-n:]
    cur = tail[-1]

    def _sit(s) -> str:
        return str(s.get("situation", "")).split(".")[-1].lower()

    # 3. CRASHED — landed/splashed after liftoff. A single confirmed reading is decisive (you don't
    #    "un-crash"), but require the streak so a pre-liftoff/pad frame can't trip it.
    if all(_sit(s) in ("landed", "splashed") for s in tail):
        return True, (f"situation '{_sit(cur)}' after liftoff — vehicle crashed "
                      f"(apoapsis {cur.get('apoapsis_m', 0.0)/1000:.0f} km)")

    # 1. PART-COUNT COLLAPSE — broke up. Threshold = max(half the post-staging count, payload+1) so a
    #    normal booster separation (a controlled, expected drop) never trips it, but a break-up down to
    #    the bare payload does.
    if post_staging_part_count and post_staging_part_count > 0:
        collapse_threshold = max(int(post_staging_part_count * _FAILFAST_PARTS_FRAC),
                                 int(payload_part_count) + 1)
        if all(int(s.get("part_count", post_staging_part_count)) < collapse_threshold for s in tail):
            return True, (f"part count {int(cur.get('part_count', 0))} << {int(post_staging_part_count)} "
                          f"(threshold {collapse_threshold}) — vehicle broke up")

    near_target = cur.get("apoapsis_m", 0.0) >= 0.95 * target_apoapsis_m

    # 2. FALLING BACK — below the atmosphere, descending, apoapsis decaying across the whole streak.
    apo_strictly_decaying = all(
        tail[i].get("apoapsis_m", 0.0) < tail[i - 1].get("apoapsis_m", 0.0) for i in range(1, len(tail))
    )
    if (not near_target
            and apo_strictly_decaying
            and all(s.get("apoapsis_m", 0.0) < atmosphere_top_m for s in tail)
            and all(s.get("vertical_speed_mps", 0.0) < 0.0 for s in tail)):
        return True, (f"apoapsis decaying {tail[0].get('apoapsis_m', 0.0)/1000:.0f}->"
                      f"{cur.get('apoapsis_m', 0.0)/1000:.0f} km, vspd {cur.get('vertical_speed_mps', 0.0):.0f}, "
                      f"below atmosphere — falling back")

    # 4. DIVERGING — apoapsis strictly decreasing every poll while the engine is lit and still well short
    #    of target. Distinct from (2): can be ABOVE the atmosphere yet still losing apoapsis under burn
    #    (e.g. pointed wrong / tumbling), which never recovers to orbit.
    if (not near_target
            and apo_strictly_decaying
            and all(s.get("engine_lit", False) for s in tail)
            and cur.get("apoapsis_m", 0.0) < 0.7 * target_apoapsis_m):
        return True, (f"apoapsis diverging {tail[0].get('apoapsis_m', 0.0)/1000:.0f}->"
                      f"{cur.get('apoapsis_m', 0.0)/1000:.0f} km under power, still <{0.7*target_apoapsis_m/1000:.0f} km "
                      f"— ascent losing ground")

    return False, ""


def _guarded_decouple(vessel, dec) -> bool:
    """Fire ``dec`` ONLY if the active (root/pod) side keeps an engine (``_root_side_keeps_engine``); a
    decoupler that would strand the engineless PAYLOAD (the tug's heat-shield / payload boundary) is NEVER
    fired. This is the SINGLE choke point every decouple — ascent AND in-space — passes through, so the
    crewed/heat-shield decoupler can never be split off no matter which staging path calls it (the in-space
    capture-burn bug that left the crew pod stranded at Eve with no engine, heat shield, or chutes). Returns
    True if the decouple was actually fired, False if it was protected/failed."""
    try:
        if not _root_side_keeps_engine(vessel, dec):
            return False                              # PROTECTED — firing strands the engineless payload
    except Exception:
        # If we genuinely cannot tell (no engine list etc.), fall back to the caller's own filtering rather
        # than firing blind; the caller only passes pre-filtered inter-stage decouplers anyway.
        return False
    try:
        dec.decouple()
        return True
    except Exception:
        return False


def _separate_attached_boosters(ksc, inter_decs) -> int:
    """Fire EVERY still-un-fired inter-stage decoupler (drop the spent/attached booster stack), confirming
    each by a part-count drop over a few physics frames, then make sure the upper stage is lit. The payload
    decoupler is never in inter_decs, AND every fire is gated by ``_guarded_decouple`` (root side keeps an
    engine), so the comsat / crew payload is never jettisoned. Returns the number fired."""
    fired = 0
    for dd in list(inter_decs):
        try:
            if dd.decoupled:
                continue
            before = len(ksc.active_vessel.parts.all)
            if not _guarded_decouple(ksc.active_vessel, dd):   # GUARD: never fire a payload/heat-shield decoupler
                continue
            fired += 1
            for _ in range(8):                       # KSP splits the vessel over several physics frames
                time.sleep(0.5)
                if len(ksc.active_vessel.parts.all) < before:
                    break
        except Exception:
            pass
    try:
        v = ksc.active_vessel
        up = [e for e in v.parts.engines if e.has_fuel and not e.active]
        up.sort(key=lambda e: -_depth_from_root(e.part))   # deepest fueled, still-unlit engine = the upper
        if up:
            up[0].active = True
        # Idle the throttle whether we just lit the upper or MechJeb already had it lit, so it doesn't burn
        # uncontrolled at orbit; the raise/circularise node executor sets the throttle.
        v.control.throttle = 0.0
    except Exception:
        pass
    return fired


def launch_to_lko(sc, cfg, runner, bridge, name: str, target_alt_km: float,
                  insertion_dv_override: float = 0.0, booster_max_engines: int = 1,
                  radial_booster_count: int = 0, *, crew: int = 0,
                  needs_heatshield: bool = False, landing=None,
                  mission_dv: float = 0.0, needs_legs: bool = False) -> bool:
    """Proven launch: clear pad, write the RA-100 comsat craft, MechJeb ascent, direct booster
    ignition + explicit staging, until a stable ~100 km parking orbit. The insertion stage is sized for
    the eventual TARGET orbit so it has the propellant to raise + circularise there.
    insertion_dv_override > 0 sizes the upper for an explicit Δv budget instead (used by the interplanetary
    transfers, whose upper must afford the warp raise+lower + ejection + correction + Oberth capture ~3,800 m/s,
    NOT just a Kerbin-orbit insertion — the default min-tank upper left the Duna capture ~250 m/s short).

    CREWED launch (crew>0): the WRITTEN craft must carry a crewable Mk1 command pod, a forward heat shield
    (needs_heatshield) and chutes (landing) so a kerbal can board and ride. Without threading these, this
    function re-derived a crew=0 PROBE design — so the .craft topped out on a probeCoreOcto with NO crewable
    seat (kRPC crew_capacity==0) and /spawn-crew had nowhere to seat a kerbal. The asparagus booster/upper
    sizing + staging + flight logic below are unchanged; only the command/recovery requirements differ."""
    import krpc
    import math
    from ksp_lab.bodies import KERBIN
    for vsl in list(sc.vessels):
        try:
            if vsl.orbit.body.name == "Kerbin" and str(vsl.situation).split(".")[-1] in ("landed", "pre_launch", "splashed"):
                vsl.recover()
        except Exception:
            pass
    # CALCULATED insertion Δv for the ACTUAL target orbit (general): the Hohmann raise from the ~100 km
    # parking orbit to the target + circularisation there, plus a trim margin. A keostationary target
    # (2863 km) needs ~1075 m/s of raise+circularise; a low parking orbit before a Mun/interplanetary
    # transfer needs almost none. (The stock tank quantum leaves the keo design unchanged — the upper
    # carries ~3600 m/s either way — but this adapts the requirement for other targets. The real keo
    # shortfall fix is the CLEAN STAGING below: a booster that fails to separate/deliver drains this
    # upper stage on the ascent, which is what stranded Keo-3 short of keo.)
    r_park = KERBIN.radius_m + 100_000.0
    r_target = KERBIN.radius_m + max(0.0, target_alt_km) * 1000.0
    a_tr = (r_park + r_target) / 2.0
    dv_raise = abs(math.sqrt(KERBIN.mu * (2.0 / r_park - 1.0 / a_tr)) - math.sqrt(KERBIN.mu / r_park))
    dv_circ = abs(math.sqrt(KERBIN.mu / r_target) - math.sqrt(KERBIN.mu * (2.0 / r_target - 1.0 / a_tr)))
    insertion_dv = 250.0 + dv_raise + dv_circ          # +250 m/s to trim the parking orbit + slack
    if insertion_dv_override > 0.0:                     # interplanetary transfer: size for the FULL transfer budget
        insertion_dv = insertion_dv_override
    # CALCULATED 2-stage relay (light, properly staged, flies on its OWN propellant — NO refuel):
    #   booster: sized by the rocket equation to reach near-orbital, engine picked for liftoff TWR in window
    #   insertion: sized above for the LKO -> target raise + circularise
    # Tall enough that the CoP sits a full caliber below the CoG (aerodynamically STABLE). The bus rides the
    # RA-100 relay inside a real PROCEDURAL FAIRING (ogive shell, jettisoned in orbit before deploy) + fins.
    from ksp_lab.design import Phase, ShipRequirements, design_ship, default_reserve_frac
    # A HEAVY interplanetary upper (large override -> a ~30 t insertion stage) must CIRCULARISE before it
    # falls back from apoapsis, so it needs real thrust — a slow Terrier (60 kN) leaves it suborbital
    # (the Eve-relay #8 failure). Give a big upper a TWR floor so the sizer picks a Reliant; a light
    # comsat upper still circularises fine on the Terrier (no floor).
    # NOTE: a thrustier crewed upper (Skipper via min_twr 1.3) was tried and DID NOT fix the crewed-launch
    # failure — the blocker is ASCENT AERODYNAMICS (the blunt exposed heat-shield/pod tumbles), not upper
    # thrust. Left at the proven relay floor; the real fix is a payload fairing over the crewed pod for ascent.
    _ins_g, _ins_twr = (9.81, 0.5) if insertion_dv >= 3500.0 else (0.0, 0.0)
    # CREWED vs uncrewed command/recovery. A crewed launch (crew>0) MUST write a craft with a crewable
    # Mk1 pod + forward heat shield + chutes so a kerbal can board and survive re-entry; an uncrewed relay
    # rides a headless probe core in a fairing. mission_type reflects which so downstream code can tell.
    _mission_type = "crewed_launch" if crew > 0 else "relay_comsat"
    # Booster sized to reach NEAR-orbital on its own (atmospheric Isp + ~1200 m/s gravity/drag loss eat
    # ~3400 of this), so the weak high-Isp upper only has to circularise + raise to the target.
    _phases = [Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3,            # 1.2-1.8 is the window
                     reserve_frac=default_reserve_frac(9.81)),                   # +12% ascent reserve
               Phase("insertion", insertion_dv, twr_body_g=_ins_g, min_twr=_ins_twr,
                     reserve_frac=default_reserve_frac(0.0))]                    # +7% vacuum reserve
    # MISSION-AWARE (opt-in): a crewed SINGLE-VEHICLE deep-space mission (a Mun land-and-return) must carry
    # the POST-LKO Δv budget (TMI + capture + land + ascend + return + re-entry) on the SAME craft, so when
    # mission_dv>0 we APPEND a vacuum "mission" phase (design.py splits it across stages if it exceeds the
    # single-stage Δv ceiling) and force landing legs on. mission_dv==0 keeps the relay/Eve LKO craft
    # BYTE-FOR-BYTE unchanged. This mirrors primitives._launch_requirements so the WRITTEN+FLOWN craft is the
    # SAME 3-phase legged vehicle the design-chart gate approved — previously the gate sized a full-mission
    # craft but launch_to_lko re-derived and flew an LKO-only one (the crewed Mun round-trip sizing blocker).
    _mission_dv = max(0.0, float(mission_dv))
    if _mission_dv > 0.0:
        _phases.append(Phase("mission", _mission_dv, twr_body_g=0.0, min_twr=0.0,
                             reserve_frac=default_reserve_frac(0.0)))
    req = ShipRequirements(
        name=name, mission_type=_mission_type, crew=crew, payload_t=0.3,
        phases=_phases,
        landing=landing, needs_legs=bool(needs_legs), needs_heatshield=needs_heatshield, needs_docking=False,
        max_engine_count=booster_max_engines,
        # RADIAL BOOSTERS: a heavy interplanetary upper (Eve's ~3800 m/s sync insertion) makes a ~200 t
        # rocket that hangs at low TWR on a single core. radial_booster_count>0 straps N tank+engine pods
        # to the launch core (asparagus); design.py sizes them so combined liftoff TWR clears the floor
        # and the core flies on lighter after they jettison. 0 = the proven single-core launcher.
        radial_booster_count=radial_booster_count,
    )
    log(f"  insertion stage sized for {target_alt_km:.0f} km target: {insertion_dv:.0f} m/s "
        f"(raise {dv_raise:.0f} + circ {dv_circ:.0f} + 250 trim)")
    d = design_ship(req)
    if not d.feasible:
        log(f"DESIGN INFEASIBLE — refusing to launch: {d.infeasible_reasons}")
        return False
    # RULE 1 (AGENTS.md): write the design chart + HARD-GATE the shape before flying anything.
    import design_chart
    from pathlib import Path as _Path
    # Resolve the SAME harvested part-body library write() will use, so the chart/gate model the parts
    # that will ACTUALLY be launched. Optional parts with no harvested serialization (service bays, the
    # inter-stage adapters when no donor craft carries them) are dropped from BOTH the craft and the
    # chart; without threading this the chart counted phantom parts and over-reported length (21.6 m
    # chart vs 12.9 m live). _part_body_library returns None offline, which keeps every part (consistent
    # with the offline writer).
    craft_dir = runner._craft_dir()
    try:
        part_bodies = runner.writer._part_body_library(d, craft_dir)
    except Exception:
        part_bodies = None
    shape = design_chart.looks_like_a_rocket(d, part_bodies=part_bodies)
    chart = _Path(__file__).resolve().parents[1] / "docs" / f"design_chart_{name}.svg"
    try:
        chart.write_text(design_chart.render_svg(d, part_bodies=part_bodies), encoding="utf-8")
    except Exception:
        pass
    log(f"DESIGN CHART {chart.name}: fineness {shape['fineness_ratio']}:1, length {shape['length_m']}m, "
        f"max-dia {shape['max_diameter_m']}m -> {'LOOKS LIKE A ROCKET' if shape['looks_like_a_rocket'] else 'REJECTED'}")
    if not shape["looks_like_a_rocket"]:
        for _k, _ok in shape["checks"].items():
            if not _ok:
                log(f"   shape FAIL: {_k}")
        return False
    from ksp_lab.design import separation_sequence
    log("SEPARATION SEQUENCE + control logic (separators placed by calculated inverse-stage):")
    for e in separation_sequence(d, req):
        log("   - " + e)
    runner.writer.write(d, craft_dir, template_path=None)
    _rb = d.radial_boosters
    _rb_str = (f" + {_rb.count}x[{_rb.engine_count}x{_rb.engine}+{_rb.tank_count}{_rb.tank}] radial boosters "
               f"(TWR {d.estimates['launch_twr']}, +{d.estimates.get('booster_delta_v_mps', 0):.0f} m/s)" if _rb else "")
    log(f"craft written ({name}): S1 {d.stages[0].engine_count}x{d.stages[0].engine} S2 {d.stages[1].engine}{_rb_str}; "
        f"aero Cd={d.drag_cd} dragloss={d.ascent_drag_loss_mps}m/s margin={d.static_margin_m}m stable={d.ascent_stable}; launching ...")
    runner._load_and_launch(bridge, name)
    time.sleep(4)
    try:
        # autostage=False: MechJeb flies the gravity turn + circularises but does NOT autostage. The
        # explicit-decouple loop below (inter-stage decouplers only, payload decoupler protected) is the
        # SOLE stager, so the two never race — fixes the intermittent "no separation" on the relay's fin
        # geometry and the early booster drop on a tank-crossfeed transient.
        log(f"  mj-ascent -> {bridge.mj_ascent(altitude=100_000.0, inclination=0.0, autostage=False)}")
    except Exception as exc:
        log(f"  mj-ascent rejected: {exc}"); return False
    kc = cfg["krpc"]
    c2 = krpc.connect(name="relay-kick", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    kv = c2.space_center.active_vessel
    # RULE 1: read the REAL assembled craft back from the live API (kRPC) and compare to the calculation.
    try:
        live = design_chart.verify_against_live(c2, d, part_bodies=part_bodies)
        log(f"LIVE-API check: mass {live['live_mass_t']}t (calc {live['calc_wet_mass_t']}t, "
            f"match={live['mass_match']}), {live['live_part_count']} parts; "
            f"length {live['live_length_m']}m (calc {live['calc_length_m']}m, "
            f"envelope {live['calc_envelope_length_m']}m), "
            f"max-dia {live['live_max_diameter_m']}m (calc {live['calc_max_diameter_m']}m), "
            f"dims_match={live['dimensions_match']}")
    except Exception as exc:
        log(f"  live-API size check skipped: {exc}")
    kv.control.throttle = 1.0
    # Ignite the LAUNCH-stage engine AND the radial-booster engine at T0. When the core and the strap-on
    # pods use the SAME engine (relay: Skipper core + Skipper pods) a single name matches all of them;
    # but a heavier vehicle gets a thrustier CORE (crewed: Mainsail core + Skipper pods), so matching only
    # the core type lit just 1 of 5 engines and the rocket lifted on ~1/5 thrust and failed. Include the
    # radial-booster engine type so every liftoff engine fires.
    ignite_prefixes = [d.stages[0].engine]
    _rb = getattr(d, "radial_boosters", None)
    if _rb is not None and getattr(_rb, "count", 0) > 0:
        ignite_prefixes.append(_rb.engine)
    fired = 0
    for e in kv.parts.engines:
        try:
            if any(e.part.name.startswith(p) for p in ignite_prefixes):   # ".v2" suffix tolerated
                e.active = True; fired += 1
        except Exception:
            pass
    if fired == 0:
        kv.control.activate_next_stage(); log("  no booster match; activated next stage")
    else:
        log(f"  ignited {fired} booster engine(s) directly")
    ksc = c2.space_center
    # Pre-identify the ascent SEPARATORS once, while every decoupler is still attached: the inter-stage
    # decouplers ONLY (deepest/booster-most first), EXCLUDING the payload decoupler. We fire these
    # EXPLICITLY rather than trusting control.activate_next_stage() (the by-name booster ignition desyncs
    # KSP's stage counter, so it can fire the already-spent stage and skip the booster decoupler) or
    # MechJeb autostage (which intermittently skips the booster decoupler on this fin geometry). That
    # guarantees a spent stage is physically separated BEFORE the next engine lights — the fix for "the
    # next engine just heats the attached tank with no thrust gain" — and the comsat is never jettisoned.
    inter_decs = _inter_stage_decouplers(kv)
    log(f"  ascent separators: {len(inter_decs)} inter-stage decoupler(s) to fire explicitly "
        f"(payload decoupler protected — never fired during ascent)")
    # FAIL-FAST baselines (captured once, on the fully-stacked vehicle at liftoff): the part count that
    # SHOULD survive to orbit (post-staging = full stack minus the booster stages we will drop) and the
    # bare PAYLOAD count (what's left if it breaks up). post_staging is the full stack minus one part per
    # inter-stage decoupler's booster subtree only as a floor; we don't know the exact subtree sizes
    # offline, so we use the full liftoff count as the post-staging reference and let the half-fraction
    # threshold absorb the normal staging drop. payload ~= the comsat/crew bus = stack minus everything
    # below the payload decoupler; we approximate it as a small floor so the collapse threshold never
    # dips below it. The atmosphere top comes from the launch body (Kerbin, 70 km).
    try:
        liftoff_part_count = len(kv.parts.all)
    except Exception:
        liftoff_part_count = 0
    post_staging_part_count = liftoff_part_count
    payload_part_count = 3                  # conservative floor: a comsat/crew bus is at least a few parts
    atmosphere_top_m = KERBIN.atmosphere_top_m if KERBIN.atmosphere_top_m > 0 else 70_000.0
    state_history: list = []                 # rolling per-poll snapshots for the fail-fast predicate
    t0 = time.monotonic()
    dry_count = 0
    while time.monotonic() - t0 < 900.0:     # backstop only: the fail-fast checks abort real failures long before this
        # FULL thrust (no refuel-through-ascent). The consecutive-dry guard handles the crossfeed transient
        # so the booster flies at full thrust and only drops when GENUINELY spent.
        try:
            kv2 = ksc.active_vessel
            active = [e for e in kv2.parts.engines if e.active]
            # FAIL-FAST snapshot + abort. Record a cheap per-poll state (part count, apoapsis, vertical
            # speed, situation, engine-lit) and feed the rolling history to the PURE _ascent_has_failed
            # predicate. If a real failure mode (break-up, falling back below the atmosphere, crash, or a
            # diverging powered ascent) holds for _FAILFAST_BAD_STREAK consecutive polls, log it LOUD and
            # RETURN False NOW — no more silent 20-minute polling. The reads are wrapped so a transient
            # kRPC hiccup just skips this poll's snapshot rather than killing the loop.
            try:
                _vspd = 0.0
                try:
                    _fl = kv2.flight(kv2.orbit.body.reference_frame)
                    _vspd = float(_fl.vertical_speed)
                except Exception:
                    _vspd = 0.0
                state_history.append({
                    "part_count": len(kv2.parts.all),
                    "apoapsis_m": float(kv2.orbit.apoapsis_altitude),
                    "vertical_speed_mps": _vspd,
                    "situation": str(kv2.situation),
                    "engine_lit": bool(active),
                })
                if len(state_history) > 8:
                    del state_history[:-8]
                _failed, _reason = _ascent_has_failed(
                    state_history,
                    post_staging_part_count=post_staging_part_count,
                    payload_part_count=payload_part_count,
                    atmosphere_top_m=atmosphere_top_m,
                    target_apoapsis_m=100_000.0,
                )
                if _failed:
                    log("mission_phase: launch_failed")
                    log(f"  ABORT: {_reason}")
                    log("RESULT: FAIL")
                    c2.close(); return False
            except Exception:
                pass
            # Trigger on the BOTTOM (deepest) active engine being dry — the current booster. Keying on the
            # bottom (NOT "all active engines dry") is essential: MechJeb autostage may light an UPPER engine
            # WITHOUT separating, leaving a mixed [dead booster, live upper] set; an "all dry" test never
            # fires and the upper burns against the still-attached dead booster — exactly how Keo-4 stranded
            # at ap 691 km dragging the whole booster (25 parts, both engines active, both decouplers unfired).
            # 3 consecutive dry polls = genuine burnout, not a single-frame tank-crossfeed transient.
            bottom_dry = False
            if active:
                bottom = max(active, key=lambda e: _depth_from_root(e.part))
                bottom_dry = not bottom.has_fuel
            dry_count = dry_count + 1 if bottom_dry else 0
            # inter-stage decouplers still UN-FIRED (MechJeb may already have fired one); deepest-first.
            # Recomputed each poll from .decoupled (not popped), so an unconfirmed fire simply retries and an
            # already-MechJeb-fired one is skipped.
            pending = []
            for _d in inter_decs:
                try:
                    if not _d.decoupled:
                        pending.append(_d)
                except Exception:
                    pass
            if dry_count >= 3 and pending:
                # The bottom stage is spent and STILL ATTACHED (its decoupler is un-fired). Fire that
                # decoupler EXPLICITLY and CONFIRM the separation by a part-count drop. The payload decoupler
                # is never in this list, AND _guarded_decouple re-checks that the root side keeps an engine,
                # so a spent UPPER stage near orbit can never jettison the comsat / crew payload.
                dec = pending[0]
                before = len(kv2.parts.all)
                if _guarded_decouple(kv2, dec):       # GUARD: never fire a payload/heat-shield decoupler
                    log("  fired inter-stage decoupler explicitly (spent bottom stage was still attached)")
                else:
                    log("  skipped a protected decoupler (root side would lose its engine) — not fired")
                sep = False
                for _ in range(8):                        # KSP splits the vessel over several physics frames
                    time.sleep(0.5)
                    try:
                        if len(ksc.active_vessel.parts.all) < before:
                            sep = True
                            break
                    except Exception:
                        pass
                kv3 = ksc.active_vessel
                if sep:
                    # Make sure the next stage down is lit (MechJeb usually already lit it -> a no-op here).
                    # The deepest fueled, still-unlit engine = the next stage; never a blanket ignite-all.
                    nxt = [e for e in kv3.parts.engines if e.has_fuel and not e.active]
                    nxt.sort(key=lambda e: -_depth_from_root(e.part))
                    if nxt:
                        try:
                            nxt[0].active = True
                        except Exception:
                            pass
                    log(f"  SEPARATED (dropped {before - len(kv3.parts.all)} parts) -> next stage burning")
                # If not confirmed, `dec` stays un-decoupled -> recomputed back into `pending` -> retried.
                dry_count = 0
        except Exception:
            pass
        try:
            s = bridge.mj_status()
        except Exception:
            time.sleep(4); continue
        if not s.get("ascentEnabled", False) and s.get("periapsis", 0) > 70_000 and s.get("body") == "Kerbin":
            # Ascent complete. The OVER-SIZED booster can reach orbit with fuel still in it (it never flames
            # out, so the on-dry separation above never triggers) — so DROP any still-attached booster NOW,
            # before the raise, and ignite the upper. Otherwise the raise drags the dead booster and strands
            # the relay short of keo (Keo-4/5 ended ~100x692 km dragging the whole booster). The payload
            # decoupler is never in inter_decs, so the comsat is never jettisoned.
            dropped = _separate_attached_boosters(ksc, inter_decs)
            if dropped:
                log(f"  booster STILL ATTACHED at orbit -> force-separated {dropped} stage(s) + ignited upper")
            log(f"  IN LKO: {round(s.get('periapsis',0)/1000)}x{round(s.get('apoapsis',0)/1000)} km "
                f"(booster dropped — raise/circularise runs on the upper's own propellant, no refuel)")
            c2.close(); return True
        time.sleep(3)
    c2.close(); return False


def raise_and_circularize(sc, bridge, target_alt_m: float) -> None:
    """Raise apoapsis to the target altitude, MANUALLY warp to that apoapsis, then circularize there."""
    v = sc.active_vessel
    st = execute.measure(v)
    r_target = st["body_radius"] + target_alt_m
    # prograde dv (vis-viva) to raise apoapsis to r_target, added as an immediate node.
    import math
    mu = st["mu"]; r = st["r_periapsis"]
    v_now = math.sqrt(mu * (2.0 / r - 1.0 / v.orbit.semi_major_axis))
    a_new = (r + r_target) / 2.0
    v_new = math.sqrt(mu * (2.0 / r - 1.0 / a_new))
    for nd in list(v.control.nodes):
        nd.remove()
    v.control.add_node(sc.ut + 12.0, prograde=v_new - v_now)
    log(f"  raise apoapsis to {target_alt_m/1000:.0f} km: {v_new - v_now:.0f} m/s")
    execute.execute_node(sc, bridge, v)
    time.sleep(2)
    # MANUALLY rails-warp to near apoapsis so the circularise node is IMMEDIATE. MechJeb's autowarp
    # intermittently STALLS on a far (tens-of-minutes) apoapsis node and hangs the deploy until the probe
    # drains its battery in shadow and loses control — what killed Keo-6's circularise. On-rails warp needs
    # no EC and is deterministic; a near node then burns reliably.
    t0 = time.monotonic()
    while v.orbit.time_to_apoapsis > 45 and time.monotonic() - t0 < 90:
        ttap = v.orbit.time_to_apoapsis
        sc.rails_warp_factor = 6 if ttap > 1500 else (4 if ttap > 400 else (2 if ttap > 90 else 1))
        time.sleep(0.7)
    sc.rails_warp_factor = 0
    time.sleep(2)
    log(f"  warped to apoapsis (ttap {v.orbit.time_to_apoapsis:.0f}s); circularising")
    execute.circularize(sc, bridge, v)
    log(f"  circularized: {v.orbit.periapsis_altitude/1000:.0f}x{v.orbit.apoapsis_altitude/1000:.0f} km ecc={v.orbit.eccentricity:.3f}")


def jettison_payload_fairings(v) -> int:
    """Split away EVERY un-jettisoned procedural payload fairing on the vessel, in orbit. Returns the
    count jettisoned. This removes ONLY the ogive shell — the parts it shrouded (a relay bus, or a CREWED
    Mk1 pod + forward heat shield + chutes) stay attached and intact. The PAYLOAD DECOUPLER is a separate
    Decoupler.1 part, untouched here, so jettisoning the fairing never separates the payload. Used both to
    expose a relay bus before dish/solar deploy AND to uncover the crewed capsule's heat shield + chutes for
    the Eve aerocapture / Kerbin-return reentry once the vehicle is safely above the atmosphere."""
    jettisoned = 0
    for fr in list(getattr(v.parts, "fairings", []) or []):
        try:
            if not fr.jettisoned:
                fr.jettison(); jettisoned += 1
        except Exception:
            pass
    return jettisoned


def commission(bridge, v) -> None:
    """Bring the relay online WITHOUT refuelling: JETTISON the payload fairing, then extend the RA-100
    dish + the solar panels so it has a live CommNet link and recharges its own EC from sunlight (the
    legitimate alternative to topping off electric charge). Set the vessel type to Relay so it forwards
    other craft's signals. The fairing protected the bus through max-Q; the dish/solar deploy ONLY once
    the shroud is gone (a real comsat sequence)."""
    # Jettison the procedural fairing first — the ogive shroud must split away before anything deploys.
    jettisoned = jettison_payload_fairings(v)
    if jettisoned:
        log(f"  jettisoned {jettisoned} payload fairing(s) — bus exposed, clear to deploy")
        time.sleep(2)
    deployed_a = deployed_s = 0
    for a in v.parts.antennas:
        try:
            if a.deployable and not a.deployed:
                a.deployed = True; deployed_a += 1
        except Exception:
            pass
    for sp in v.parts.solar_panels:
        try:
            if sp.deployable and not sp.deployed:
                sp.deployed = True; deployed_s += 1
        except Exception:
            pass
    log(f"  commissioned: deployed {deployed_a} antenna(s) + {deployed_s} solar panel(s) (self-powered, no EC refuel)")
    try:
        bridge._request("POST", "/vessel/type", json={"type": "Relay"})
    except Exception:
        pass


def main() -> int:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    target_alt_km = float(sys.argv[2]) if len(sys.argv) > 2 else 2863.0
    name = sys.argv[3] if len(sys.argv) > 3 else "AI-Relay-Keo"
    cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
    bridge = BridgeClient(**cfg["bridge"])
    runner = AutomationRunner(cfg_path, offline=False)
    import krpc
    kc = cfg["krpc"]
    c = krpc.connect(name="deploy-relay", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center
    if not launch_to_lko(sc, cfg, runner, bridge, name, target_alt_km):
        log("launch FAILED"); return 2
    time.sleep(3)
    raise_and_circularize(sc, bridge, target_alt_km * 1000.0)
    v = sc.active_vessel
    commission(bridge, v)
    log(f"=== RELAY {name} DEPLOYED: {v.orbit.body.name} {v.orbit.periapsis_altitude/1000:.0f}x{v.orbit.apoapsis_altitude/1000:.0f} km ===")
    try: sc.save("persistent")
    except Exception: pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())