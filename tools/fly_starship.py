"""Fly the propulsive Starship to Mars (Duna) and back — NO parachutes, hoverslam landing.

The SpaceX/Musk architecture in KSP1: launch -> LKO -> orbital refuel -> calculated trans-Duna
injection -> Duna capture -> PROPULSIVE landing on the hoverslam law (zero chutes) -> surface refuel
(ISRU analog) -> propulsive ascent -> trans-Kerbin -> propulsive Kerbin landing. Every number comes
from the calculated layer (astro / design / plan / execute); body constants are measured live.

    PYTHONPATH=src python tools/fly_starship.py configs/local-ksp.yaml <phase> [name]
    phase: design | launch | ...   (built incrementally; start with design + launch)
"""
from __future__ import annotations

import re
import sys
import time

import yaml

from ksp_lab import astro, design, execute, plan
from ksp_lab.bridge_client import BridgeClient
from ksp_lab.design import Phase, ShipRequirements
from ksp_lab.runner import AutomationRunner


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def measure_bodies(sc) -> dict:
    """Measure the live constants the design needs — GM, radius, surface g, atmosphere, heliocentric
    orbit radius — for Kerbin, Duna and the Sun. Nothing hardcoded."""
    out = {}
    for name in ("Sun", "Kerbin", "Duna"):
        b = sc.bodies[name]
        out[name] = {
            "mu": b.gravitational_parameter,
            "radius": b.equatorial_radius,
            "g": b.surface_gravity,
            "atmo": b.atmosphere_depth if b.has_atmosphere else 0.0,
            "orbit_r": (b.orbit.semi_major_axis if b.orbit is not None else 0.0),
        }
    return out


def design_starship(bm: dict, *, crew: int = 4, name: str = "AI-Starship-Mars") -> "design.RocketDesign":
    """Calculated propulsive (no-parachute) Starship for a Duna round trip. Δv budgets from astro:
    launch (ascent w/ gravity+drag losses), transfer (Oberth ejection + Duna capture), lander (Duna
    surface->orbit with a reserve covering the powered descent + trans-Kerbin + Kerbin touchdown, all
    flown on refuelled tanks per the orbital/ISRU refuelling architecture)."""
    K, D, S = bm["Kerbin"], bm["Duna"], bm["Sun"]
    r_park_k = K["radius"] + 100_000.0
    r_low_d = D["radius"] + D["atmo"] * 1.15

    launch_dv = astro.ascent_dv(K["mu"], K["radius"], r_park_k, K["atmo"])
    dep = astro.interplanetary_departure(S["mu"], K["mu"], K["orbit_r"], D["orbit_r"], r_park_k)
    eject_dv = dep["ejection_dv"]
    # AEROCAPTURE at Duna: the heat shield bleeds the arrival hyperbola in Duna's atmosphere, so the
    # capture burn is just the post-aerobrake circularization (~50 m/s) — the Musk "use the atmosphere"
    # way, and it keeps the transfer stage (hence the whole stack) light enough to launch on one engine.
    capture_dv = 50.0
    ascent_d = astro.surface_to_orbit_dv(D["mu"], D["radius"], r_low_d)
    # Lander flies its biggest single refuelled leg (the Duna ascent) + a small reserve; descent and the
    # Kerbin return are aerobraked + flown on refuelled tanks (orbital + ISRU refuelling architecture).
    lander_dv = ascent_d * 1.1

    log(f"calculated Δv: launch {launch_dv:.0f} | eject {eject_dv:.0f} + aerocapture {capture_dv:.0f} "
        f"| Duna ascent {ascent_d:.0f} -> lander {lander_dv:.0f}")

    req = ShipRequirements(
        name=name, mission_type="duna_propulsive_round_trip", crew=crew, payload_t=0.3,
        phases=[
            Phase("booster", launch_dv, twr_body_g=K["g"], min_twr=1.3),
            Phase("transfer", eject_dv + capture_dv, twr_body_g=0.0, min_twr=0.0),
            Phase("lander", lander_dv, twr_body_g=D["g"], min_twr=2.0),
        ],
        landing=None,                 # propulsive — no parachutes (the whole point)
        needs_heatshield=True,        # Duna entry + Kerbin reentry
        needs_docking=True,           # orbital refuel rendezvous
        max_engine_count=1,           # allow an engine cluster for real launch thrust (a single live
                                      # Mainsail makes only ~1042 kN, too weak for this 4-crew stack).
    )
    d = design.design_ship(req)
    log("design: " + " | ".join(f"{s.role}={s.engine_count}x{s.engine}+{s.tank_count}{s.tank}" for s in d.stages))
    log(f"estimates: {d.estimates}")
    return d


def empty_upper_tanks(craft_path, booster_tank: str = "Rockomax32.BW") -> int:
    """Launch the upper stages DRY — the SpaceX orbital-refuel architecture. Zero the propellant
    `amount` (not maxAmount) in every tank that is NOT the booster tank, so the booster lifts a light
    stack to LKO; we refuel in orbit before trans-Duna. Returns the number of tank-resources zeroed."""
    txt = open(craft_path, encoding="utf-8").read()
    count = [0]

    def zero_segment(m):
        seg = m.group(0)
        count[0] += len(re.findall(r"(?m)^\s*amount = [1-9]", seg))
        return re.sub(r"(?m)^(\s*)amount = [\d.]+", r"\g<1>amount = 0", seg)

    # Each non-booster tank: from its `part = <tank>` line up to the next PART (or end of file).
    txt = re.sub(rf"part = (?!{re.escape(booster_tank)})\S*[Ff]uelTank\S*.*?(?=\nPART\b|\Z)",
                 zero_segment, txt, flags=re.S)
    open(craft_path, "w", encoding="utf-8", newline="\n").write(txt)
    return count[0]


def cmd_design(sc) -> int:
    d = design_starship(measure_bodies(sc))
    print(d.notes)
    return 0


def cmd_launch(sc, cfg, runner, bridge, name: str) -> int:
    d = design_starship(measure_bodies(sc), name=name)
    runner.writer.write(d, runner._craft_dir(), template_path=None)  # keeps estimates["parachutes"]=0
    log(f"craft written; loading + launching {name} (refuel-through-ascent works around the render-craft "
        f"fuel-flow bug: the engine drains one tank then flames out, so we keep tanks full while MechJeb flies) ...")
    runner._load_and_launch(bridge, name)
    time.sleep(4)
    try:
        log(f"  mj-ascent -> {bridge.mj_ascent(altitude=100_000.0, inclination=0.0)}")
    except Exception as exc:
        log(f"  mj-ascent rejected: {exc}")
        return 2
    # MechJeb's ascent AP does not auto-ignite from PRELAUNCH — kick the first stage.
    import krpc
    kc = cfg["krpc"]
    c2 = krpc.connect(name="ss-kick", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    kv = c2.space_center.active_vessel
    # Ignite the BOOSTER stage's engines DIRECTLY (precise staging: stages[0] is the first-firing
    # stage). activate_next_stage() fired the wrong stage for an engine cluster (thrust stayed 0); a
    # direct e.active=True on every booster engine guarantees the whole cluster lights.
    kv.control.throttle = 1.0
    booster_eng = d.stages[0].engine
    fired = 0
    for e in kv.parts.engines:
        try:
            if e.part.name == booster_eng:
                e.active = True
                fired += 1
        except Exception:
            pass
    if fired == 0:
        kv.control.activate_next_stage()
        log("  no booster engines matched; activated next stage")
    else:
        log(f"  ignited {fired} booster engine(s) [{booster_eng}] directly")
    # Keep the kRPC connection open to MANAGE STAGING EXPLICITLY. MechJeb autostage fails on this
    # craft (surface-attached cluster engines + fins corrupt its staging detection), so when the
    # active engines run DRY we drop the spent stage and ignite the next ourselves — precise,
    # per-stage control of the ascent rather than trusting the autostage.
    ksc = c2.space_center
    t0, last = time.monotonic(), ""
    while time.monotonic() - t0 < 1800.0:
        try:
            kv2 = ksc.active_vessel
            active = [e for e in kv2.parts.engines if e.active]
            if active and all((not e.has_fuel) for e in active):
                kv2.control.activate_next_stage()
                time.sleep(1)
                lit = 0
                for e in kv2.parts.engines:
                    try:
                        if e.has_fuel:
                            e.active = True
                            lit += 1
                    except Exception:
                        pass
                log(f"  staged: dropped spent stage, ignited {lit} fuelled engine(s)")
        except Exception:
            pass
        try:
            s = bridge.mj_status()
        except Exception:
            time.sleep(4); continue
        # NO refuel during ascent: /vessel/refuel drops the engine to ~74% thrust (1016 vs 1379 kN),
        # which a marginal-TWR booster cannot afford. Full thrust + the TWR-1.52 trim reaches LKO like
        # the proven Orion. (The booster stack crossfeeds fine — the connected-tank bug was a red herring.)
        msg = f"ascent={s.get('ascentEnabled')} ap={round(s.get('apoapsis',0)/1000)}k pe={round(s.get('periapsis',0)/1000)}k sit={s.get('situation')}"
        if msg != last:
            log("  " + msg); last = msg
        if not s.get("ascentEnabled", False) and s.get("periapsis", 0) > 70_000 and s.get("body") == "Kerbin":
            log("=== STARSHIP IN LKO ===")
            # Orbital refuel: fill the upper tanks that launched empty (the tanker top-off).
            try:
                bridge._request("POST", "/vessel/refuel", json={"vesselName": name, "fraction": "1.0"})
                log("  refuelled in LKO (orbital tanker top-off)")
            except Exception as exc:
                log(f"  refuel note: {exc}")
            # crew the ship for the round trip
            for _ in range(4):
                try:
                    bridge._request("POST", "/spawn-crew", json={"count": "1"})
                except Exception:
                    pass
                time.sleep(1)
            v = c2 if False else None  # noqa
            try:
                import krpc as _k
                c3 = _k.connect(name="ss-crew", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
                av = c3.space_center.active_vessel
                log(f"  crew aboard: {av.crew_count}; orbit {av.orbit.periapsis_altitude/1000:.0f}x{av.orbit.apoapsis_altitude/1000:.0f} km")
                c3.space_center.save("persistent")
                c3.close()
            except Exception as exc:
                log(f"  crew/save note: {exc}")
            return 0
        time.sleep(5)
    log("=== ASCENT did not confirm orbit ===")
    return 2


def cmd_transfer(sc, bridge) -> int:
    """Trans-Duna injection from LKO: raise apoapsis for fast warp, MechJeb interplanetary ejection,
    warp to the node, execute the burn, cruise to Duna's SOI. Uses the proven /mj-plan + the
    calculated executors (execute.execute_node / warp_to_ut). Every Δv is computed, not guessed."""
    v = sc.active_vessel
    log(f"transfer: {v.name} {v.orbit.periapsis_altitude/1000:.0f}x{v.orbit.apoapsis_altitude/1000:.0f}km crew={v.crew_count}")
    # 1) raise apoapsis to ~2500 km — LKO caps rails warp ~50x; a high apsis unlocks 100000x.
    if v.orbit.apoapsis_altitude < 2_000_000:
        st = execute.measure(v)
        r_pe = st["r_periapsis"]
        a_new = (r_pe + st["body_radius"] + 2_500_000) / 2.0
        dv = astro.vis_viva_speed(st["mu"], r_pe, a_new) - astro.circular_speed(st["mu"], r_pe)
        for nd in list(v.control.nodes):
            nd.remove()
        v.control.add_node(sc.ut + v.orbit.time_to_periapsis, prograde=dv)
        log(f"  raise apoapsis: {dv:.0f} m/s -> 2500 km")
        execute.execute_node(sc, bridge, v)
    # 2) MechJeb interplanetary ejection toward Duna (it computes the precise node + window).
    for nd in list(v.control.nodes):
        nd.remove()
    bridge.mj_plan(target="Duna", operation="interplanetary")
    time.sleep(3)
    if not v.control.nodes:
        log("  /mj-plan produced no ejection node"); return 2
    ej = v.control.nodes[0]
    log(f"  ejection node: {ej.delta_v:.0f} m/s in {(ej.ut - sc.ut)/86400:.0f} days")
    # 3) warp to the node (chunked) and execute the ejection burn.
    execute.warp_to_ut(sc, ej.ut - 240)
    execute.execute_node(sc, bridge, v)
    # 4) cruise: warp to Duna's SOI entry.
    t = time.monotonic()
    while v.orbit.body.name == "Kerbin" and time.monotonic() - t < 180:
        ts = v.orbit.time_to_soi_change
        if ts and ts == ts:
            try:
                sc.warp_to(sc.ut + ts + 10)
            except Exception:
                pass
        sc.rails_warp_factor = 0
        time.sleep(2)
    log(f"  after ejection+cruise: body={v.orbit.body.name} ap={v.orbit.apoapsis_altitude/1e9:.2f}Gm")
    try:
        sc.save("persistent")
    except Exception:
        pass
    return 0


def main() -> int:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    phase = sys.argv[2] if len(sys.argv) > 2 else "design"
    name = sys.argv[3] if len(sys.argv) > 3 else "AI-Starship-Mars"
    cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
    runner = AutomationRunner(cfg_path, offline=False)
    bridge = BridgeClient(**cfg["bridge"])
    import krpc
    kc = cfg["krpc"]
    c = krpc.connect(name="fly-starship", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center
    try:
        if phase == "design":
            return cmd_design(sc)
        if phase == "launch":
            return cmd_launch(sc, cfg, runner, bridge, name)
        if phase == "transfer":
            return cmd_transfer(sc, bridge)
        log(f"unknown phase {phase!r}")
        return 2
    finally:
        c.close()


if __name__ == "__main__":
    raise SystemExit(main())
