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


def _root_side_keeps_engine(vessel, dec) -> bool:
    """True if firing ``dec`` leaves the ACTIVE (root/pod) side of the split still holding an engine — i.e.
    ``dec`` drops a SPENT stage and keeps propulsion. False means firing it would strand the engineless
    PAYLOAD as the active vessel. This is the robust test for "is this an inter-stage decoupler": the old
    'shallowest decoupler == payload' heuristic protected only ONE decoupler, but a CREWED vehicle has a
    SECOND high decoupler (the heat shield's) above the upper stage — firing it kept the capsule and
    jettisoned the ENTIRE upper stage (engine + fuel), so the crew coasted suborbital. Here we walk the part
    tree from the root WITHOUT crossing ``dec`` and check the root-side component for any engine."""
    try:
        root = vessel.parts.root
        dec_part = dec.part
    except Exception:
        return True                       # can't tell -> treat as inter-stage (old behaviour)
    if root is None or dec_part is None or root is dec_part:
        return True
    seen = {id(dec_part)}                  # never traverse THROUGH the decoupler -> isolates the root side
    stack = [root]
    root_side = set()
    while stack:
        p = stack.pop()
        if p is None or id(p) in seen:
            continue
        seen.add(id(p)); root_side.add(id(p))
        neigh = []
        try:
            if p.parent is not None:
                neigh.append(p.parent)
        except Exception:
            pass
        try:
            neigh.extend(p.children)
        except Exception:
            pass
        for n in neigh:
            if n is not None and id(n) not in seen:
                stack.append(n)
    for e in vessel.parts.engines:
        try:
            if id(e.part) in root_side:
                return True
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


def _separate_attached_boosters(ksc, inter_decs) -> int:
    """Fire EVERY still-un-fired inter-stage decoupler (drop the spent/attached booster stack), confirming
    each by a part-count drop over a few physics frames, then make sure the upper stage is lit. The payload
    decoupler is never in inter_decs, so the comsat is never jettisoned. Returns the number fired."""
    fired = 0
    for dd in list(inter_decs):
        try:
            if dd.decoupled:
                continue
            before = len(ksc.active_vessel.parts.all)
            dd.decouple()
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
                  needs_heatshield: bool = False, landing=None) -> bool:
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
    req = ShipRequirements(
        name=name, mission_type=_mission_type, crew=crew, payload_t=0.3,
        # Booster sized to reach NEAR-orbital on its own (atmospheric Isp + ~1200 m/s gravity/drag loss eat
        # ~3400 of this), so the weak high-Isp upper only has to circularise + raise to the target.
        phases=[Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3,            # 1.2-1.8 is the window
                      reserve_frac=default_reserve_frac(9.81)),                   # +12% ascent reserve
                Phase("insertion", insertion_dv, twr_body_g=_ins_g, min_twr=_ins_twr,
                      reserve_frac=default_reserve_frac(0.0))],                   # +7% vacuum reserve
        landing=landing, needs_legs=False, needs_heatshield=needs_heatshield, needs_docking=False,
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
    t0 = time.monotonic()
    dry_count = 0
    while time.monotonic() - t0 < 1200.0:
        # FULL thrust (no refuel-through-ascent). The consecutive-dry guard handles the crossfeed transient
        # so the booster flies at full thrust and only drops when GENUINELY spent.
        try:
            kv2 = ksc.active_vessel
            active = [e for e in kv2.parts.engines if e.active]
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
                # is never in this list, so a spent UPPER stage near orbit can never jettison the comsat.
                dec = pending[0]
                before = len(kv2.parts.all)
                try:
                    dec.decouple()
                    log("  fired inter-stage decoupler explicitly (spent bottom stage was still attached)")
                except Exception:
                    pass
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