"""Rescue a relay stuck in LKO with a far-future ejection node (the 50x-warp-cap bug): advance the
clock via a HIGH-altitude vessel (not altitude-limited -> warps at 100,000x), execute the existing
ejection node, then finish the transfer (escape -> MechJeb course-correction -> capture -> Hohmann to
the synchronous radius) reusing the committed transfer helpers.

    python tools/_rescue_eve_relay.py AI-Eve-Relay-1b Eve 10328
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import krpc  # noqa: E402
import yaml  # noqa: E402

from ksp_lab.bridge_client import BridgeClient  # noqa: E402
import deploy_relay_transfer as drt  # noqa: E402


def warp_via_high(sc, target_ut: float, buffer_s: float = 2.0 * 3600.0):
    """Advance the clock to ``target_ut - buffer`` by warping from the highest-altitude vessel (Sun/
    Duna/Eve orbit), which is NOT altitude-capped, so the multi-month warp runs at the 100,000x rate
    instead of the 50x LKO cap. Returns to no vessel switch if none found."""
    hi = [v for v in sc.vessels
          if v.orbit.body.name in ("Sun", "Duna", "Eve") and str(v.situation).split(".")[-1] == "orbiting"]
    hi.sort(key=lambda x: -(x.orbit.periapsis_altitude or 0.0))
    for cand in hi:
        try:
            sc.active_vessel = cand
            time.sleep(2)
            break
        except Exception:
            continue
    sc.rails_warp_factor = 0
    sc.warp_to(max(sc.ut + 5.0, target_ut - buffer_s))
    time.sleep(2)


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "AI-Eve-Relay-1b"
    target_name = sys.argv[2] if len(sys.argv) > 2 else "Eve"
    target_alt_km = float(sys.argv[3]) if len(sys.argv) > 3 else 10328.0
    cfg = yaml.safe_load(open("configs/local-ksp.yaml", encoding="utf-8"))
    kc = cfg["krpc"]
    bridge = BridgeClient(**cfg["bridge"])
    c = krpc.connect(name="rescue", address=kc["host"], rpc_port=kc["rpc_port"], stream_port=kc["stream_port"])
    sc = c.space_center
    sc.rails_warp_factor = 0  # clear any leftover warp (you cannot switch vessels mid-warp -> it hangs)
    time.sleep(1)
    v = next((x for x in sc.vessels if x.name == name), None)
    if v is None:
        drt.log(f"no vessel named {name!r}"); return 2
    sc.active_vessel = v
    time.sleep(2)
    v = sc.active_vessel
    nodes = v.control.nodes
    if nodes:
        nd = nodes[0]
        drt.log(f"{name}: existing ejection node dv {nd.delta_v:.0f} m/s at UT {round(nd.ut)} "
                f"(t-{round(nd.ut - sc.ut)}s); advancing clock via a high vessel ...")
        warp_via_high(sc, nd.ut)
        sc.active_vessel = v
        time.sleep(2)
        v = sc.active_vessel
        drt.log(f"  clock now UT {round(sc.ut)}; node t-{round(v.control.nodes[0].time_to)}s; executing ejection ...")
        drt._execute_node_manually(c, sc, v, max_burn_s=400.0, max_throttle=1.0)
    else:
        drt.log(f"{name}: no ejection node — re-planning via MechJeb interplanetary ...")
        try:
            sc.target_body = sc.bodies[target_name]
        except Exception:
            pass
        rr = bridge.mj_plan(target=target_name, operation="interplanetary")
        if rr.get("planned") and v.control.nodes:
            drt._execute_node_manually(c, sc, v, max_burn_s=400.0, max_throttle=1.0)
        else:
            drt.log(f"  could not plan ejection ({rr})"); return 2

    if not drt._wait_until_sun_orbit(sc, v):
        drt.log(f"  did not escape the departure SOI (still {v.orbit.body.name})"); return 2
    # Finish the transfer with the committed body-agnostic logic: MechJeb correction -> capture -> Hohmann.
    target = sc.bodies[target_name]
    r_target = target.equatorial_radius + target_alt_km * 1000.0
    try:
        sc.target_body = target
    except Exception:
        pass
    # arrival ~ now + a Hohmann-ish coast; correct as we near the SOI.
    for i in range(1, 4):
        if v.orbit.body.name == target_name:
            break
        dt = v.orbit.time_to_soi_change
        if dt and 0 < dt < 1e9:
            warp_via_high(sc, sc.ut + dt * (0.6 if i == 1 else 0.92))
            sc.active_vessel = v; time.sleep(2); v = sc.active_vessel
        if v.orbit.body.name == target_name:
            break
        try:
            rr = bridge.mj_plan(target=target_name, operation="correction")
            if rr.get("planned") and v.control.nodes:
                drt.log(f"  CORRECTION {i} (MechJeb): {rr.get('dv', 0):.0f} m/s")
                drt._execute_node_manually(c, sc, v, max_burn_s=150.0, max_throttle=0.8)
        except Exception as exc:
            drt.log(f"  MechJeb correction {i} unavailable ({exc})")
    # coast to SOI
    for _ in range(4):
        if v.orbit.body.name == target_name:
            break
        dt = v.orbit.time_to_soi_change
        if dt and 0 < dt < 1e9:
            warp_via_high(sc, sc.ut + dt + 30.0)
            sc.active_vessel = v; time.sleep(2); v = sc.active_vessel
        else:
            break
    if v.orbit.body.name != target_name:
        drt.log(f"  ABORT: never entered the {target_name} SOI (still {v.orbit.body.name})"); return 2
    ttp = v.orbit.time_to_periapsis
    if ttp and 0 < ttp < 1e7:
        drt.log(f"  in {target_name} SOI; pe {v.orbit.periapsis_altitude/1000:.0f} km; warping to periapsis ...")
        sc.warp_to(sc.ut + ttp - 20.0); time.sleep(2)
    drt._circularize_at(c, sc, bridge, v, "capture periapsis")
    drt.log(f"  captured {v.orbit.periapsis_altitude/1000:.0f}x{v.orbit.apoapsis_altitude/1000:.0f} km; "
            f"Hohmann to {target_alt_km:.0f} km synchronous ...")
    drt._hohmann_to_radius(c, sc, bridge, v, r_target)
    o = v.orbit
    drt.log(f"  SYNCHRONOUS at {target_name}: {o.periapsis_altitude/1000:.0f}x{o.apoapsis_altitude/1000:.0f} km "
            f"(target {target_alt_km:.0f}, e={o.eccentricity:.3f})")
    return 0 if (o.body.name == target_name and o.eccentricity < 0.25) else 2


if __name__ == "__main__":
    raise SystemExit(main())
