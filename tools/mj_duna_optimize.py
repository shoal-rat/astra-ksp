"""Find and fly a mid-course correction to Duna by directly minimizing the closest-approach distance.

MechJeb's OperationCourseCorrection only fine-tunes an already-close approach; when an ejection falls
short the phase miss is far larger than Duna's SOI, so we optimize a correction node ourselves:
precompute Duna's path, then coordinate-descend the node's (prograde, normal, radial) to minimize the
sampled closest approach until it drops inside Duna's SOI, then fly it with MechJeb (engines held
active) and let the trajectory enter Duna's SOI.

    PYTHONPATH=src python tools/mj_duna_optimize.py configs/local-ksp.yaml
"""
from __future__ import annotations

import sys
import time

import yaml

import krpc
from ksp_lab.bridge_client import BridgeClient

DUNA_SOI = 4.79e7  # m


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> int:
    cfg = yaml.safe_load(open(sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml", encoding="utf-8"))
    bridge = BridgeClient(**cfg["bridge"])
    kc = cfg["krpc"]
    c = krpc.connect(name="duna-opt", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center
    v = sc.active_vessel
    try:
        bridge.mj_disable("all")
    except Exception:
        pass
    sun = sc.bodies["Sun"]
    duna = sc.bodies["Duna"]
    ref = sun.non_rotating_reference_frame
    for nd in list(v.control.nodes):
        nd.remove()
    log(f"comsat in {v.orbit.body.name} orbit, period {v.orbit.period/86400:.0f}d")

    def clear():
        for n in list(v.control.nodes):
            try:
                n.remove()
            except Exception:
                pass

    def sample_min(orbit, t0, t1, n, duna_cache=None):
        md, tbest = 1e18, t0
        for i in range(n + 1):
            ut = t0 + (t1 - t0) * i / n
            cp = orbit.position_at(ut, ref)
            dp = duna_cache[i] if duna_cache else duna.orbit.position_at(ut, ref)
            d = ((cp[0] - dp[0]) ** 2 + (cp[1] - dp[1]) ** 2 + (cp[2] - dp[2]) ** 2) ** 0.5
            if d < md:
                md, tbest = d, ut
        # refine around the minimum (the approach sweeps past in hours; coarse sampling misses it)
        w = (t1 - t0) / n
        for _ in range(2):
            for i in range(21):
                ut = tbest - w + 2.0 * w * i / 20.0
                cp = orbit.position_at(ut, ref)
                dp = duna.orbit.position_at(ut, ref)
                d = ((cp[0] - dp[0]) ** 2 + (cp[1] - dp[1]) ** 2 + (cp[2] - dp[2]) ** 2) ** 0.5
                if d < md:
                    md, tbest = d, ut
            w /= 10.0
        return md, tbest

    # Phase 1: coarse closest approach over one period (locate the approach window).
    t0 = sc.ut
    per = v.orbit.period
    md0, tb = sample_min(v.orbit, t0 + 3600, t0 + per, 240)
    log(f"current closest approach: {md0/1e6:.1f} Mm at +{(tb-t0)/86400:.0f}d (need < {DUNA_SOI/1e6:.0f} Mm)")

    # Phase 2: coordinate-descend a correction node around that window.
    node_ut = t0 + 200  # only ~200s ahead so MechJeb burns it after a short wait (no warp needed)
    win0, win1 = tb - 25 * 86400, tb + 25 * 86400
    N = 60
    duna_cache = [duna.orbit.position_at(win0 + (win1 - win0) * i / N, ref) for i in range(N + 1)]

    def obj(pg, nm, rd):
        clear()
        node = v.control.add_node(node_ut, prograde=float(pg), normal=float(nm), radial=float(rd))
        time.sleep(0.05)
        md, _ = sample_min(node.orbit, win0, win1, N, duna_cache)
        return md

    # coarse grid pre-search so the descent doesn't stall in a flat region near (0,0,0)
    best, bestmd = [0.0, 0.0, 0.0], obj(0.0, 0.0, 0.0)
    for pg in (-400, 0, 400):
        for nm in (-400, 0, 400):
            for rd in (-400, 0, 400):
                md = obj(pg, nm, rd)
                if md < bestmd:
                    bestmd, best = md, [float(pg), float(nm), float(rd)]
    log(f"  grid start=({best[0]:.0f},{best[1]:.0f},{best[2]:.0f}) closest={bestmd/1e6:.2f} Mm")
    step = 300.0
    while step > 1.0:
        improved = False
        for axis in range(3):
            for delta in (step, -step):
                trial = list(best)
                trial[axis] += delta
                md = obj(*trial)
                if md < bestmd:
                    bestmd, best, improved = md, trial, True
        log(f"  step={step:.0f}: best dv=({best[0]:.0f},{best[1]:.0f},{best[2]:.0f}) closest={bestmd/1e6:.2f} Mm")
        if not improved:
            step /= 2.0
        if bestmd < 1.5e7:  # comfortably inside SOI with a low periapsis
            break

    log(f"OPTIMIZED correction: prograde={best[0]:.0f} normal={best[1]:.0f} radial={best[2]:.0f} -> closest {bestmd/1e6:.2f} Mm")
    if bestmd > DUNA_SOI:
        log("could not get inside Duna's SOI — leaving node off")
        clear()
        c.close()
        return 2

    # Fly the node with the kRPC direct-burn pattern. Root cause of every earlier failure: the comsat
    # is EC-starved in deep solar orbit (EC=0 -> reaction wheels dead -> can't point). Refuel restores
    # EC (and the engine alternator sustains it during the burn); then the autopilot points in seconds.
    def refuel():
        try:
            bridge._request("POST", "/vessel/refuel", json={"vesselName": v.name, "fraction": "1.0"})
        except Exception:
            pass

    clear()
    node = v.control.add_node(node_ut, prograde=best[0], normal=best[1], radial=best[2])
    refuel()
    v.control.sas = False
    v.control.rcs = False
    for e in v.parts.engines:
        try:
            e.active = True
        except Exception:
            pass
    ap = v.auto_pilot
    ap.reference_frame = node.reference_frame
    ap.target_direction = (0.0, 1.0, 0.0)
    ap.engage()
    log("pointing (EC restored) ...")
    t1 = time.monotonic()
    while time.monotonic() - t1 < 60:
        ap.target_direction = (0.0, 1.0, 0.0)
        if abs(ap.error) < 3.0:
            break
        time.sleep(1.0)
    log(f"  err={abs(ap.error):.1f} deg, node t-{node.time_to:.0f}s")
    if abs(ap.error) > 10.0:
        log("  cannot point — abort")
        c.close()
        return 2
    # wait until ~half the burn before the node, keeping pointed + EC topped up
    while node.time_to > 30:
        ap.target_direction = (0.0, 1.0, 0.0)
        if int(node.time_to) % 30 == 0:
            refuel()
        time.sleep(1.0)
    log("BURNING")
    refuel()
    v.control.throttle = 1.0
    t1, last = time.monotonic(), ""
    while time.monotonic() - t1 < 180:
        for e in v.parts.engines:
            try:
                e.active = True
            except Exception:
                pass
        ap.target_direction = (0.0, 1.0, 0.0)
        try:
            rem = node.remaining_delta_v
        except Exception:
            rem = 0.0
        if rem < 40.0:
            v.control.throttle = max(0.08, rem / 40.0)
        msg = f"rem={rem:.0f} thr={v.thrust:.0f} EC={v.resources.amount('ElectricCharge'):.0f}"
        if msg != last:
            log("  " + msg)
            last = msg
        if rem < 1.5:
            break
        time.sleep(0.2)
    v.control.throttle = 0.0
    try:
        ap.disengage()
        node.remove()
    except Exception:
        pass
    # report the resulting Duna closest approach
    pe = None
    o = v.orbit
    for _ in range(8):
        if o is None:
            break
        try:
            if o.body.name == "Duna":
                pe = o.periapsis_altitude
                break
            o = o.next_orbit
        except Exception:
            break
    log(f"=== correction flown. Duna periapsis = {('%.0fkm'%(pe/1000)) if pe is not None else 'not in patches (re-check closest approach)'} ===")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
