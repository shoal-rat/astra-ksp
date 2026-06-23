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
from ksp_lab.duna import build_duna_comsat
from ksp_lab.runner import AutomationRunner


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


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
    # unlike a short single-stage probe (margin ~0.2 m, would flip). The bus adds the RA-100 relay + a
    # service-bay FAIRING + nose cone + CoP-sized fins. design.staging_plan records the per-stage masses.
    from ksp_lab.design import Phase, ShipRequirements, design_ship
    req = ShipRequirements(
        name=name, mission_type="relay_comsat", crew=0, payload_t=0.3,
        phases=[Phase("booster", 3500.0, twr_body_g=9.81, min_twr=1.5),
                Phase("insertion", 1300.0, twr_body_g=0.0, min_twr=0.0)],
        landing=None, needs_legs=False, needs_heatshield=False, needs_docking=False, max_engine_count=1,
    )
    d = design_ship(req)
    if not d.feasible:
        log(f"DESIGN INFEASIBLE — refusing to launch: {d.infeasible_reasons}")
        return False
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
    t0 = time.monotonic()
    dry_count = 0
    while time.monotonic() - t0 < 1200.0:
        # FULL thrust (no refuel-through-ascent — that throttles the engine to ~74% and a heavy stack
        # then sits at TWR~1 and falls back). The consecutive-dry guard below handles the crossfeed
        # transient instead, so the booster flies at full thrust and only drops when GENUINELY spent.
        try:
            kv2 = ksc.active_vessel
            active = [e for e in kv2.parts.engines if e.active]
            # Require the stage to read dry for 3 CONSECUTIVE polls before dropping it — a single-frame
            # has_fuel=False (the tank-crossfeed transient) dropped the booster at 19 km last time and
            # the stack fell back. Genuine burnout persists; a transient does not.
            if active and all((not e.has_fuel) for e in active):
                dry_count += 1
            else:
                dry_count = 0
            if dry_count >= 3:
                kv2.control.activate_next_stage(); time.sleep(1)
                for e in kv2.parts.engines:
                    try:
                        if e.has_fuel:
                            e.active = True
                    except Exception:
                        pass
                dry_count = 0
                log("  staged to next (confirmed dry)")
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
    """Bring the relay online WITHOUT refuelling: extend the RA-100 dish + the solar panels so it has a
    live CommNet link and recharges its own EC from sunlight (the legitimate alternative to topping off
    electric charge). Set the vessel type to Relay so it forwards other craft's signals."""
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
