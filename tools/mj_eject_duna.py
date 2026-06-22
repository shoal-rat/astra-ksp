"""Eject a comsat from Kerbin orbit toward Duna using MechJeb's interplanetary maneuver planner.

Payoff of the /mj-plan endpoint: MechJeb computes the porkchop-optimal ejection NODE (precise angle +
timing) that a hand-rolled prograde burn cannot — why earlier comsats hit Duna's orbital RADIUS but
missed the planet. Verified live: /mj-plan places the textbook ~1060 m/s Kerbin->Duna node.

Flow: raise apoapsis high (Kerbin caps rails warp at 50x in LKO; you need a high orbit to warp the
~55-day window in minutes) using reliable SAS-prograde pointing -> re-plan from the raised orbit ->
chunked warp to the node -> MechJeb flies the burn -> confirm a Duna ENCOUNTER in the patches.

    PYTHONPATH=src python tools/mj_eject_duna.py configs/local-ksp.yaml [vessel_substr]
"""
from __future__ import annotations

import sys
import time

import yaml

import krpc
from ksp_lab.bridge_client import BridgeClient


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def patch_bodies(orbit, depth: int = 8):
    bodies, o = [], orbit
    for _ in range(depth):
        if o is None:
            break
        try:
            bodies.append(o.body.name)
            o = o.next_orbit
        except Exception:
            break
    return bodies


def refuel(bridge, name: str) -> None:
    try:
        bridge._request("POST", "/vessel/refuel", json={"vesselName": name, "fraction": "1.0"})
    except Exception:
        pass


def ignite(v) -> float:
    for e in v.parts.engines:
        try:
            e.active = True
        except Exception:
            pass
    return v.available_thrust


def raise_apoapsis(v, sc, target_m: float) -> None:
    """Burn prograde (held by SAS, which points reliably — autopilot wasted ~80% of thrust on bad
    pointing) until apoapsis >= target. Periapsis stays low so the later ejection keeps its Oberth."""
    v.control.throttle = 0.0
    v.control.sas = True
    time.sleep(0.3)
    try:
        v.control.sas_mode = sc.SASMode.prograde
    except Exception:
        pass
    time.sleep(5)  # let SAS settle onto prograde
    ignite(v)
    if v.available_thrust < 1.0:
        log("  no thrust — staging once to expose the bus engine")
        v.control.activate_next_stage()
        time.sleep(2)
        ignite(v)
    v.control.throttle = 1.0
    log(f"  orbit-raise burn: avail={v.available_thrust/1000:.0f} kN, target apo {target_m/1000:.0f} km")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 300 and v.orbit.apoapsis_altitude < target_m:
        time.sleep(1)
    v.control.throttle = 0.0
    log(f"  apoapsis now {v.orbit.apoapsis_altitude/1000:.0f} km, pe {v.orbit.periapsis_altitude/1000:.0f} km")


def plan(bridge, v, sc):
    bridge.mj_plan(target="Duna", operation="interplanetary")
    time.sleep(2)
    nodes = v.control.nodes
    if not nodes:
        return None, None
    n = nodes[0]
    return n, (n.ut - sc.ut) / 86400.0


def chunked_warp(sc, v, target_ut):
    """Warp to target_ut in <=6h game-chunks so progress is visible and a stall is detectable."""
    while sc.ut < target_ut - 60:
        step = min(target_ut, sc.ut + 6 * 3600)
        try:
            sc.warp_to(step)
        except Exception as exc:
            log(f"  warp_to chunk failed: {exc}")
            break
        log(f"  warped: UT={sc.ut:.0f}  remaining={(target_ut - sc.ut)/86400:.2f} d  apoAlt={v.orbit.apoapsis_altitude/1000:.0f}km")


def main() -> int:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
    bridge = BridgeClient(**cfg["bridge"])
    kc = cfg["krpc"]
    c = krpc.connect(name="eject-duna", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center
    v = sc.active_vessel
    log(f"vessel={v.name} body={v.orbit.body.name} ap={v.orbit.apoapsis_altitude/1000:.0f}km pe={v.orbit.periapsis_altitude/1000:.0f}km")
    if v.orbit.body.name != "Kerbin":
        log("not in Kerbin orbit — abort")
        return 2

    # 1) Raise apoapsis for fast warp (LKO is 50x; a high orbit warps the window in minutes).
    if v.orbit.apoapsis_altitude < 2_400_000:
        raise_apoapsis(v, sc, 2_500_000)

    # 2) Plan the ejection from the raised orbit (exercises the /mj-plan target fix).
    log("calling /mj-plan(target=Duna) ...")
    n, dt_days = plan(bridge, v, sc)
    if n is None:
        log("  MechJeb placed NO node — abort")
        return 2
    log(f"  EJECTION NODE: dv={n.delta_v:.0f} m/s, in {dt_days:.1f} days. patches={patch_bodies(v.orbit)}")

    # 3) Warp to just before the node.
    chunked_warp(sc, v, n.ut - 120)

    # 4) Fly the node with MechJeb.
    ignite(v)
    refuel(bridge, v.name)
    try:
        er = bridge.mj_execute_node()
        log(f"  mj-execute-node -> {er}")
    except Exception as exc:
        log(f"  mj-execute-node: {exc}")

    # 5) Monitor for a Duna encounter.
    t0, last = time.monotonic(), ""
    while time.monotonic() - t0 < 1500:
        time.sleep(8)
        try:
            ignite(v)
            nodes = v.control.nodes
            bodies = patch_bodies(v.orbit)
            msg = f"nodes_left={len(nodes)} body={v.orbit.body.name} patches={bodies}"
            if msg != last:
                log("  " + msg)
                last = msg
            if "Duna" in bodies:
                log("=== DUNA ENCOUNTER CONFIRMED — /mj-plan ejection flown correctly! ===")
                return 0
        except Exception as exc:
            log(f"  monitor: {exc}")
    log("=== window elapsed; see patches above ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
