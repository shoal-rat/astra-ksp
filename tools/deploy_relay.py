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


def _inter_stage_decouplers(vessel) -> list:
    """The decouplers ascent staging may fire, DEEPEST-first (the booster decoupler before any upper one),
    EXCLUDING the payload decoupler. The payload decoupler is the SHALLOWEST decoupler (it separates the
    comsat from the final stage) — never fired during ascent, so the payload is never jettisoned when the
    upper stage later runs dry. Returns [] when there is only a payload decoupler (or none)."""
    try:
        decs = list(vessel.parts.decouplers)
    except Exception:
        return []
    if len(decs) <= 1:
        return []
    payload = min(decs, key=lambda dd: _depth_from_root(dd.part))   # shallowest = the payload decoupler
    inter = [dd for dd in decs if dd is not payload]
    inter.sort(key=lambda dd: -_depth_from_root(dd.part))           # deepest (lowest booster) fires first
    return inter


def launch_to_lko(sc, cfg, runner, bridge, name: str) -> bool:
    """Proven launch: clear pad, write the RA-100 comsat craft, MechJeb ascent, direct booster
    ignition + explicit staging, until a stable ~100 km parking orbit."""
    import krpc
    for vsl in list(sc.vessels):
        try:
            if vsl.orbit.body.name == "Kerbin" and str(vsl.situation).split(".")[-1] in ("landed", "pre_launch", "splashed"):
                vsl.recover()
        except Exception:
            pass
    # CALCULATED 2-stage relay (light, properly staged, flies on its OWN propellant — NO refuel):
    #   booster: sized by the rocket equation for ~3500 m/s to LKO, engine picked for liftoff TWR>=1.5
    #   insertion: ~1300 m/s for the raise + circularise to the target orbit
    # Tall enough that the CoP sits a full caliber below the CoG (aerodynamically STABLE, margin ~2.5 m),
    # unlike a short single-stage probe (margin ~0.2 m, would flip). The bus rides the RA-100 relay inside
    # a real PROCEDURAL FAIRING (ModuleProceduralFairing ogive shell, jettisoned in orbit before deploy) +
    # CoP-sized fins. design.staging_plan records the per-stage masses; commission() jettisons the shroud.
    from ksp_lab.design import Phase, ShipRequirements, design_ship, default_reserve_frac
    req = ShipRequirements(
        name=name, mission_type="relay_comsat", crew=0, payload_t=0.3,
        # Booster sized to reach NEAR-orbital on its own (atmospheric Isp + ~1200 m/s gravity/drag loss
        # eat ~3400 of this, leaving the craft fast + high), so the weak high-Isp upper only has to nudge
        # the apoapsis and circularise — not fly the whole second half of the ascent on 60 kN (which
        # stalled the climb). Upper Δv covers circularisation + the keo raise.
        phases=[Phase("booster", 4200.0, twr_body_g=9.81, min_twr=1.3,            # 1.2-1.8 is the window
                      reserve_frac=default_reserve_frac(9.81)),                   # +12% ascent reserve
                Phase("insertion", 1300.0, twr_body_g=0.0, min_twr=0.0,
                      reserve_frac=default_reserve_frac(0.0))],                   # +7% vacuum reserve
        landing=None, needs_legs=False, needs_heatshield=False, needs_docking=False, max_engine_count=1,
    )
    d = design_ship(req)
    if not d.feasible:
        log(f"DESIGN INFEASIBLE — refusing to launch: {d.infeasible_reasons}")
        return False
    # RULE 1 (AGENTS.md): write the design chart + HARD-GATE the shape before flying anything.
    import design_chart
    from pathlib import Path as _Path
    shape = design_chart.looks_like_a_rocket(d)
    chart = _Path(__file__).resolve().parents[1] / "docs" / f"design_chart_{name}.svg"
    try:
        chart.write_text(design_chart.render_svg(d), encoding="utf-8")
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
    runner.writer.write(d, runner._craft_dir(), template_path=None)
    log(f"craft written ({name}): S1 {d.stages[0].engine_count}x{d.stages[0].engine} S2 {d.stages[1].engine}; "
        f"aero Cd={d.drag_cd} dragloss={d.ascent_drag_loss_mps}m/s margin={d.static_margin_m}m stable={d.ascent_stable}; launching ...")
    runner._load_and_launch(bridge, name)
    time.sleep(4)
    try:
        log(f"  mj-ascent -> {bridge.mj_ascent(altitude=100_000.0, inclination=0.0)}")
    except Exception as exc:
        log(f"  mj-ascent rejected: {exc}"); return False
    kc = cfg["krpc"]
    c2 = krpc.connect(name="relay-kick", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    kv = c2.space_center.active_vessel
    # RULE 1: read the REAL assembled craft back from the live API (kRPC) and compare to the calculation.
    try:
        live = design_chart.verify_against_live(c2, d)
        log(f"LIVE-API check: mass {live['live_mass_t']}t (calc {live['calc_wet_mass_t']}t, "
            f"match={live['mass_match']}), {live['live_part_count']} parts; "
            f"length {live['live_length_m']}m (calc {live['calc_length_m']}m), "
            f"max-dia {live['live_max_diameter_m']}m (calc {live['calc_max_diameter_m']}m), "
            f"dims_match={live['dimensions_match']}")
    except Exception as exc:
        log(f"  live-API size check skipped: {exc}")
    kv.control.throttle = 1.0
    booster_eng = d.stages[0].engine
    fired = 0
    for e in kv.parts.engines:
        try:
            if e.part.name.startswith(booster_eng):   # ".v2" suffix tolerated
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
            # 3 consecutive dry polls = genuine burnout (a single-frame has_fuel=False is the tank-crossfeed
            # transient that once dropped the booster at 19 km). Only then separate.
            if active and all((not e.has_fuel) for e in active):
                dry_count += 1
            else:
                dry_count = 0
            if dry_count >= 3 and inter_decs:
                # The bottom stage is genuinely spent and (since we got here with all engines dry) STILL
                # ATTACHED. Fire its decoupler EXPLICITLY, then CONFIRM the separation by a part-count drop
                # BEFORE igniting the next engine — so the next engine can never burn against an un-separated
                # tank (the reported bug). The payload decoupler is not in this list, so it is never fired.
                dec = inter_decs.pop(0)
                before = len(kv2.parts.all)
                try:
                    if not dec.decoupled:
                        dec.decouple()
                        log("  fired inter-stage decoupler (explicit, 3-poll-confirmed dry)")
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
                    # Ignite ONLY the next stage down: the DEEPEST fueled, still-unlit engine — never every
                    # fueled engine (that lit the upper in-atmosphere and, pre-separation, cooked the tank).
                    nxt = [e for e in kv3.parts.engines if e.has_fuel and not e.active]
                    nxt.sort(key=lambda e: -_depth_from_root(e.part))
                    if nxt:
                        try:
                            nxt[0].active = True
                        except Exception:
                            pass
                    log(f"  SEPARATED (dropped {before - len(kv3.parts.all)} parts) -> ignited next stage")
                else:
                    log("  separation NOT confirmed yet — next engine HELD OFF (won't cook an attached tank)")
                dry_count = 0
        except Exception:
            pass
        try:
            s = bridge.mj_status()
        except Exception:
            time.sleep(4); continue
        if not s.get("ascentEnabled", False) and s.get("periapsis", 0) > 70_000 and s.get("body") == "Kerbin":
            log(f"  IN LKO: {round(s.get('periapsis',0)/1000)}x{round(s.get('apoapsis',0)/1000)} km "
                f"(no refuel — raise/circularise runs on the insertion stage's own propellant)")
            c2.close(); return True
        time.sleep(3)
    c2.close(); return False


def raise_and_circularize(sc, bridge, target_alt_m: float) -> None:
    """Raise apoapsis to the target altitude (immediate node, fuel is cheap via refuel) then circularize."""
    v = sc.active_vessel
    st = execute.measure(v)
    r_target = st["body_radius"] + target_alt_m
    # prograde dv (vis-viva) to raise apoapsis to r_target, added as an immediate node (fuel is cheap).
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
    execute.circularize(sc, bridge, v)
    log(f"  circularized: {v.orbit.periapsis_altitude/1000:.0f}x{v.orbit.apoapsis_altitude/1000:.0f} km ecc={v.orbit.eccentricity:.3f}")


def commission(bridge, v) -> None:
    """Bring the relay online WITHOUT refuelling: JETTISON the payload fairing, then extend the RA-100
    dish + the solar panels so it has a live CommNet link and recharges its own EC from sunlight (the
    legitimate alternative to topping off electric charge). Set the vessel type to Relay so it forwards
    other craft's signals. The fairing protected the bus through max-Q; the dish/solar deploy ONLY once
    the shroud is gone (a real comsat sequence)."""
    # Jettison the procedural fairing first — the ogive shroud must split away before anything deploys.
    jettisoned = 0
    for fr in list(getattr(v.parts, "fairings", []) or []):
        try:
            if not fr.jettisoned:
                fr.jettison(); jettisoned += 1
        except Exception:
            pass
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
    if not launch_to_lko(sc, cfg, runner, bridge, name):
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