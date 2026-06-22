"""Salvage a comsat that ejected toward Duna but fell short: warp to solar orbit, grid-search a
mid-course correction (prograde x normal — the B-plane) that drops the Duna periapsis into the
aerobrake band, execute it with MechJeb (engines held active so it doesn't fall short again), then
cruise to Duna and let the atmosphere capture it.

    PYTHONPATH=src python tools/mj_duna_salvage.py configs/local-ksp.yaml
"""
from __future__ import annotations

import sys
import time

import yaml

import krpc
from ksp_lab.bridge_client import BridgeClient


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def ignite(v):
    for e in v.parts.engines:
        try:
            e.active = True
        except Exception:
            pass


def duna_periapsis(v):
    """Return Duna periapsis altitude (m) if the trajectory enters Duna's SOI, else None."""
    o = v.orbit
    for _ in range(8):
        if o is None:
            break
        try:
            if o.body.name == "Duna":
                return o.periapsis_altitude
            o = o.next_orbit
        except Exception:
            break
    return None


def clear_nodes(v):
    for nd in list(v.control.nodes):
        try:
            nd.remove()
        except Exception:
            pass


def main() -> int:
    cfg = yaml.safe_load(open(sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml", encoding="utf-8"))
    bridge = BridgeClient(**cfg["bridge"])
    kc = cfg["krpc"]
    c = krpc.connect(name="salvage", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center
    v = sc.active_vessel
    try:
        bridge.mj_disable("all")
    except Exception:
        pass
    sc.rails_warp_factor = 0
    log(f"start: {v.name} body={v.orbit.body.name} ap={v.orbit.apoapsis_altitude/1e9:.2f}Gm")

    # 1) Warp out of Kerbin's SOI into solar orbit (correction geometry is clean in deep space).
    t0 = time.monotonic()
    while v.orbit.body.name == "Kerbin" and time.monotonic() - t0 < 240:
        try:
            sc.warp_to(sc.ut + 6 * 3600)
        except Exception as exc:
            log(f"  soi-exit warp: {exc}")
            break
        log(f"  exiting Kerbin SOI: body={v.orbit.body.name} ut={sc.ut:.0f}")
    log(f"now in {v.orbit.body.name} orbit: ap={v.orbit.apoapsis_altitude/1e9:.2f}Gm pe={v.orbit.periapsis_altitude/1e9:.2f}Gm")

    # 2) Grid-search a prograde x normal correction that gives a low Duna periapsis.
    node_ut = sc.ut + 6 * 3600  # a correction ~6h ahead, well clear of the current point
    best = None  # (abs(pe-15km), pg, nm, pe)
    log("searching prograde x normal correction for a Duna encounter ...")
    for pg in range(-60, 101, 10):
        for nm in range(-60, 61, 10):
            clear_nodes(v)
            v.control.add_node(node_ut, prograde=float(pg), normal=float(nm), radial=0.0)
            time.sleep(0.25)
            pe = duna_periapsis(v)
            if pe is not None:
                score = abs(pe - 15000.0)
                if best is None or score < best[0]:
                    best = (score, pg, nm, pe)
                    log(f"  hit: pg={pg} nm={nm} Duna_pe={pe/1000:.0f}km")
    clear_nodes(v)
    if best is None:
        log("no prograde x normal correction reached Duna's SOI — needs radial too / re-eject")
        c.close()
        return 2
    _, pg, nm, pe = best
    log(f"BEST correction: prograde={pg} normal={nm} -> Duna periapsis {pe/1000:.0f}km")

    # 3) Place the best node and fly it with MechJeb, holding engines active (no warp during burn).
    v.control.add_node(node_ut, prograde=float(pg), normal=float(nm), radial=0.0)
    if node_ut - sc.ut > 200:
        try:
            sc.warp_to(node_ut - 60)
        except Exception as exc:
            log(f"  warp-to-node: {exc}")
    ignite(v)
    time.sleep(1)
    log(f"execute correction -> {bridge.mj_execute_node()}")
    t0, last = time.monotonic(), ""
    while time.monotonic() - t0 < 240:
        ignite(v)
        nd = v.control.nodes
        rem = nd[0].remaining_delta_v if nd else 0.0
        pe = duna_periapsis(v)
        msg = f"rem_dv={rem:.0f} Duna_pe={'%.0fkm'%(pe/1000) if pe else '-'}"
        if msg != last:
            log("  " + msg)
            last = msg
        if not nd or rem < 0.4:
            break
        time.sleep(0.5)
    try:
        bridge.mj_disable("all")
    except Exception:
        pass
    pe = duna_periapsis(v)
    if pe is not None:
        log(f"=== CORRECTION DONE — Duna encounter set, periapsis {pe/1000:.0f}km ===")
    else:
        log("=== correction done but Duna periapsis lost — re-check ===")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
