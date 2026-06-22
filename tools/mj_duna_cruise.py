"""Fly a comsat from a HIGH Kerbin parking orbit to a Duna encounter, all burns by MechJeb.

Prereqs proven live: (a) Kerbin caps rails warp at 50x in LKO but gives 100000x above ~2000 km, so the
comsat must already be in a high circular orbit (warps the ~55-day window in seconds); (b) the probe
core can't hold prograde via stock SAS, so MechJeb flies every burn; (c) /mj-plan computes the precise
ejection node. Flow: plan ejection -> fast-warp to node -> MechJeb burns -> confirm Duna encounter ->
warp across the cruise to Duna's SOI -> report arrival periapsis for an aerobraked capture.

    PYTHONPATH=src python tools/mj_duna_cruise.py configs/local-ksp.yaml
"""
from __future__ import annotations

import sys
import time

import yaml

import krpc
from ksp_lab.bridge_client import BridgeClient


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def patches(orbit, depth: int = 8):
    b, o = [], orbit
    for _ in range(depth):
        if o is None:
            break
        try:
            b.append(o.body.name)
            o = o.next_orbit
        except Exception:
            break
    return b


def ignite(v):
    for e in v.parts.engines:
        try:
            e.active = True
        except Exception:
            pass


def refuel(bridge, name):
    try:
        bridge._request("POST", "/vessel/refuel", json={"vesselName": name, "fraction": "1.0"})
    except Exception:
        pass


def warp_chunks(sc, v, target_ut, chunk_s, label):
    while sc.ut < target_ut - 60:
        step = min(target_ut, sc.ut + chunk_s)
        try:
            sc.warp_to(step)
        except Exception as exc:
            log(f"  warp_to failed: {exc}")
            break
        log(f"  {label}: UT={sc.ut:.0f} remain={(target_ut - sc.ut)/86400:.2f}d body={v.orbit.body.name}")
        if "Duna" in patches(v.orbit):
            return


def main() -> int:
    cfg = yaml.safe_load(open(sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml", encoding="utf-8"))
    bridge = BridgeClient(**cfg["bridge"])
    kc = cfg["krpc"]
    c = krpc.connect(name="duna-cruise", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center
    v = sc.active_vessel
    log(f"start: {v.name} {v.orbit.body.name} {v.orbit.apoapsis_altitude/1000:.0f}x{v.orbit.periapsis_altitude/1000:.0f}km")
    if v.orbit.body.name != "Kerbin":
        log("not at Kerbin — abort")
        return 2

    # 1) Plan the ejection.
    bridge.mj_plan(target="Duna", operation="interplanetary")
    time.sleep(2)
    if not v.control.nodes:
        log("no ejection node — abort")
        return 2
    n = v.control.nodes[0]
    log(f"ejection node: dv={n.delta_v:.0f} m/s in {(n.ut - sc.ut)/86400:.1f} days")

    # 2) Fast-warp to the node.
    warp_chunks(sc, v, n.ut - 120, 5 * 86400, "warp->node")

    # 3) MechJeb flies the ejection.
    ignite(v)
    refuel(bridge, v.name)
    log(f"eject -> {bridge.mj_execute_node()}")
    t0, last = time.monotonic(), ""
    while time.monotonic() - t0 < 400:
        time.sleep(6)
        ignite(v)
        pb = patches(v.orbit)
        msg = f"nodes={len(v.control.nodes)} body={v.orbit.body.name} patches={pb}"
        if msg != last:
            log("  " + msg)
            last = msg
        if "Duna" in pb:
            log("=== DUNA ENCOUNTER set by the ejection ===")
            break
        if len(v.control.nodes) == 0 and v.orbit.body.name == "Sun":
            log("  ejected into solar orbit (no direct Duna patch yet)")
            break

    # 4) Cruise: warp forward until we enter Duna's SOI.
    log("cruising toward Duna ...")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 600 and v.orbit.body.name != "Duna":
        try:
            sc.warp_to(sc.ut + 8 * 86400)
        except Exception as exc:
            log(f"  cruise warp: {exc}")
        log(f"  body={v.orbit.body.name} patches={patches(v.orbit)} pe={v.orbit.periapsis_altitude/1000:.0f}km")
        time.sleep(2)

    if v.orbit.body.name == "Duna":
        log(f"=== ARRIVED AT DUNA: pe={v.orbit.periapsis_altitude/1000:.1f}km ap={v.orbit.apoapsis_altitude/1000:.0f}km ===")
    else:
        log(f"cruise paused: body={v.orbit.body.name} patches={patches(v.orbit)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
