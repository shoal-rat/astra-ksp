"""LKO -> proper trans-Duna transfer, all burns via the proven direct-burn pattern.

Every burn: refuel (restores ElectricCharge — the probe is power-starved away from the Sun, dead
reaction wheels otherwise), point the kRPC autopilot at the maneuver-node vector with SAS off
(MechJeb's node executor only flies its OWN nodes), throttle directly, hold engines active (they
deactivate after rails warp). Phases: raise apoapsis (fast warp needs a high orbit — LKO caps 50x) ->
circularize -> /mj-plan eject -> warp to node -> COMPLETE the burn (no 20 m/s shortfall) -> cruise to
Duna's SOI. Then run mj_duna_capture.py.

    PYTHONPATH=src python tools/mj_duna_eject2.py configs/local-ksp.yaml
"""
from __future__ import annotations

import sys
import time

import yaml

import krpc
from ksp_lab.bridge_client import BridgeClient


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> int:
    cfg = yaml.safe_load(open(sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml", encoding="utf-8"))
    bridge = BridgeClient(**cfg["bridge"])
    kc = cfg["krpc"]
    c = krpc.connect(name="eject2", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center
    v = sc.active_vessel

    def refuel():
        try:
            bridge._request("POST", "/vessel/refuel", json={"vesselName": v.name, "fraction": "1.0"})
        except Exception:
            pass

    def ignite():
        for e in v.parts.engines:
            try:
                e.active = True
            except Exception:
                pass

    def patches():
        b, o = [], v.orbit
        for _ in range(8):
            if o is None:
                break
            try:
                b.append(o.body.name)
                o = o.next_orbit
            except Exception:
                break
        return b

    def burn_node(node):
        refuel()
        v.control.sas = False
        ap = v.auto_pilot
        ap.reference_frame = node.reference_frame
        ap.target_direction = (0.0, 1.0, 0.0)
        ap.engage()
        t = time.monotonic()
        while time.monotonic() - t < 70:
            ap.target_direction = (0.0, 1.0, 0.0)
            if abs(ap.error) < 3.0:
                break
            time.sleep(1.0)
        if abs(ap.error) > 12.0:
            log(f"  POINT FAILED err={abs(ap.error):.0f}")
            ap.disengage()
            return False
        half = node.remaining_delta_v / 8.0 / 2.0
        while node.time_to > half + 2:
            ap.target_direction = (0.0, 1.0, 0.0)
            if int(node.time_to) % 20 == 0:
                refuel()
            time.sleep(0.5)
        ignite()
        v.control.throttle = 1.0
        t = time.monotonic()
        while time.monotonic() - t < 300:
            ignite()
            ap.target_direction = (0.0, 1.0, 0.0)
            if int(time.monotonic() - t) % 15 == 0:
                refuel()
            try:
                rem = node.remaining_delta_v
            except Exception:
                rem = 0.0
            if rem < 30.0:
                v.control.throttle = max(0.08, rem / 30.0)
            if rem < 0.8:
                break
            time.sleep(0.2)
        v.control.throttle = 0.0
        ap.disengage()
        try:
            node.remove()
        except Exception:
            pass
        return True

    def warp_to_node(node, lead):
        # Chunked warp_to reaches the target UT precisely; stepped rails_warp overshoots by ~25 min
        # (the warp ramp-down lags the 0.5 s poll), which wrecks the ejection timing.
        target = node.ut - lead
        while sc.ut < target - 30:
            step = min(target, sc.ut + 30 * 86400)
            try:
                sc.warp_to(step)
            except Exception:
                break
        sc.rails_warp_factor = 0

    log(f"start: {v.name} body={v.orbit.body.name} {v.orbit.apoapsis_altitude/1000:.0f}x{v.orbit.periapsis_altitude/1000:.0f}km")
    if v.orbit.body.name != "Kerbin":
        log("not at Kerbin — abort")
        return 2

    # PHASE A: raise apoapsis to ~2500 km (bisect a prograde node), then circularize.
    if v.orbit.apoapsis_altitude < 2_400_000:
        lo, hi, n = 0.0, 1500.0, None
        for _ in range(22):
            mid = (lo + hi) / 2
            for nd in list(v.control.nodes):
                nd.remove()
            n = v.control.add_node(sc.ut + 25, prograde=mid)
            time.sleep(0.03)
            a = n.orbit.apoapsis_altitude
            if a < 0 or a > 2_600_000:
                hi = mid
            elif a < 2_400_000:
                lo = mid
            else:
                break
        log(f"  raise-apo: prograde {mid:.0f} m/s -> apo {n.orbit.apoapsis_altitude/1000:.0f}km")
        burn_node(n)
        log(f"  apoapsis now {v.orbit.apoapsis_altitude/1000:.0f}km")
    bridge.mj_plan(target="Duna", operation="circularize")  # target ignored by circularize; satisfies the handler's target check
    time.sleep(2)
    if v.control.nodes:
        warp_to_node(v.control.nodes[0], 120)
        burn_node(v.control.nodes[0])
    log(f"  parking orbit {v.orbit.apoapsis_altitude/1000:.0f}x{v.orbit.periapsis_altitude/1000:.0f}km")

    # PHASE B: eject toward Duna and COMPLETE the burn.
    bridge.mj_plan(target="Duna", operation="interplanetary")
    time.sleep(2)
    if not v.control.nodes:
        log("no ejection node — abort")
        return 2
    ej = v.control.nodes[0]
    log(f"  ejection node dv={ej.delta_v:.0f} in {(ej.ut - sc.ut)/86400:.1f}d — warping")
    warp_to_node(ej, 240)
    if not burn_node(ej):
        log("ejection burn failed")
        return 2
    t = time.monotonic()
    while time.monotonic() - t < 60 and "Duna" not in patches():
        ignite()
        time.sleep(3)
    log(f"  after ejection: patches={patches()}")

    # PHASE C: cruise to Duna's SOI.
    t = time.monotonic()
    while v.orbit.body.name == "Kerbin" and time.monotonic() - t < 120:
        ts = v.orbit.time_to_soi_change
        if ts and ts == ts:
            try:
                sc.warp_to(sc.ut + ts - 20)
            except Exception:
                pass
        sc.rails_warp_factor = 0
        time.sleep(1)
    t = time.monotonic()
    while v.orbit.body.name == "Sun" and time.monotonic() - t < 180:
        ts = v.orbit.time_to_soi_change
        if not ts or ts != ts:
            log(f"  no Duna SOI ahead (patches={patches()})")
            break
        log(f"  Duna SOI entry in {ts/86400:.1f}d")
        try:
            sc.warp_to(sc.ut + max(0.0, ts - 20))
        except Exception:
            pass
        sc.rails_warp_factor = 0
        time.sleep(2)
    sc.rails_warp_factor = 0
    log(f"=== DONE phase: body={v.orbit.body.name} pe={v.orbit.periapsis_altitude/1000:.0f}km patches={patches()} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
