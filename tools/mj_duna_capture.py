"""Cruise a comsat on a Duna-encounter trajectory into Duna's SOI and capture it into Duna orbit.

Capture is a retrograde burn at Duna periapsis. The comsat is EC-starved in deep space (reaction
wheels dead), so we refuel to restore ElectricCharge before pointing — the same fix that unblocked
the mid-course correction. Burn retrograde until the orbit is bound (apoapsis positive and inside
Duna's SOI). Result: a comsat in Duna orbit.

    PYTHONPATH=src python tools/mj_duna_capture.py configs/local-ksp.yaml
"""
from __future__ import annotations

import sys
import time

import yaml

import krpc
from ksp_lab.bridge_client import BridgeClient

DUNA_SOI = 4.79e7
TARGET_APO = 2.0e7  # capture to apoapsis < 20,000 km (a bound relay orbit)


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> int:
    cfg = yaml.safe_load(open(sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml", encoding="utf-8"))
    bridge = BridgeClient(**cfg["bridge"])
    kc = cfg["krpc"]
    c = krpc.connect(name="duna-cap", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
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

    try:
        bridge.mj_disable("all")
    except Exception:
        pass
    log(f"start: body={v.orbit.body.name} ap={v.orbit.apoapsis_altitude/1e6:.0f}Mm")

    # 1) Cruise to Duna's SOI using the patch time (chunked warp is too slow).
    if v.orbit.body.name != "Duna":
        tsoi = v.orbit.time_to_soi_change
        log(f"Duna SOI entry in {tsoi/86400:.1f}d — warping there")
        try:
            sc.warp_to(sc.ut + max(0.0, tsoi - 60))
        except Exception as exc:
            log(f"  cruise warp: {exc}")
        sc.rails_warp_factor = 0
        t0 = time.monotonic()
        while v.orbit.body.name != "Duna" and time.monotonic() - t0 < 120:
            try:
                sc.warp_to(sc.ut + 200)
            except Exception:
                pass
            time.sleep(1)
        sc.rails_warp_factor = 0
    if v.orbit.body.name != "Duna":
        log(f"did not reach Duna SOI (body={v.orbit.body.name}) — abort")
        c.close()
        return 2
    log(f"=== ENTERED DUNA SOI: pe={v.orbit.periapsis_altitude/1000:.0f}km ap={v.orbit.apoapsis_altitude/1e6:.0f}Mm ===")

    # 2) Warp to just before periapsis.
    if v.orbit.time_to_periapsis > 200:
        try:
            sc.warp_to(sc.ut + v.orbit.time_to_periapsis - 90)
        except Exception as exc:
            log(f"  warp-to-pe: {exc}")
    sc.rails_warp_factor = 0
    log(f"at periapsis approach: pe={v.orbit.periapsis_altitude/1000:.0f}km t_pe={v.orbit.time_to_periapsis:.0f}s")

    # 3) Capture: point retrograde (EC restored) and burn until bound.
    refuel()
    ignite()
    v.control.sas = False
    ref = v.orbit.body.non_rotating_reference_frame
    ap = v.auto_pilot
    ap.reference_frame = ref
    ap.target_direction = tuple(-x for x in v.velocity(ref))
    ap.engage()
    log("pointing retrograde (EC restored) ...")
    t1 = time.monotonic()
    while time.monotonic() - t1 < 60:
        ap.target_direction = tuple(-x for x in v.velocity(ref))
        if abs(ap.error) < 4.0:
            break
        time.sleep(1.0)
    log(f"  retro err={abs(ap.error):.1f} deg. BURNING to capture")
    v.control.throttle = 1.0
    t1, last = time.monotonic(), ""
    while time.monotonic() - t1 < 200:
        ignite()
        ap.target_direction = tuple(-x for x in v.velocity(ref))
        if int(time.monotonic() - t1) % 20 == 0:
            refuel()
        ap_alt = v.orbit.apoapsis_altitude
        bound = 0 < ap_alt < TARGET_APO
        msg = f"ap={ap_alt/1e6:.1f}Mm pe={v.orbit.periapsis_altitude/1000:.0f}km ecc={v.orbit.eccentricity:.2f} thr={v.thrust:.0f}"
        if msg != last:
            log("  " + msg)
            last = msg
        if bound:
            break
        time.sleep(0.3)
    v.control.throttle = 0.0
    try:
        ap.disengage()
    except Exception:
        pass
    ap_alt = v.orbit.apoapsis_altitude
    if 0 < ap_alt < DUNA_SOI:
        log(f"=== CAPTURED INTO DUNA ORBIT: {v.orbit.periapsis_altitude/1000:.0f} x {ap_alt/1000:.0f} km, ecc {v.orbit.eccentricity:.2f} ===")
    else:
        log(f"=== NOT yet bound: ap={ap_alt/1e6:.1f}Mm — may need more burn ===")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
