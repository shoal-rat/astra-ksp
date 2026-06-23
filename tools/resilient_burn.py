"""Resiliently execute the existing ejection node: coast to the burn point, refuel once, then hold
FULL throttle along the node vector until the craft ESCAPES Kerbin — reconnecting kRPC on any drop and
re-igniting the engines each loop. Survives the flaky-connection drops that killed the node executors."""
from __future__ import annotations
import time, yaml, krpc

CFG = yaml.safe_load(open("configs/local-ksp.yaml", encoding="utf-8"))
from ksp_lab.bridge_client import BridgeClient
B = BridgeClient(**CFG["bridge"])


def conn():
    k = CFG["krpc"]
    return krpc.connect(name="burn", address=k["host"], rpc_port=k["rpc_port"], stream_port=k["stream_port"])


def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


c = conn(); sc = c.space_center; v = sc.active_vessel


def reconnect():
    global c, sc, v
    try: c.close()
    except Exception: pass
    time.sleep(2)
    c = conn(); sc = c.space_center; v = sc.active_vessel


if not v.control.nodes:
    log("no node — abort"); raise SystemExit(2)
node_ut = v.control.nodes[0].ut
lead = 200.0
log(f"resilient burn: node off {node_ut - sc.ut:.0f}s, rem {v.control.nodes[0].remaining_delta_v:.0f} m/s, crew={v.crew_count}")

# 1) Coast to (node_ut - lead), refuelling so EC + tanks are full BEFORE the burn.
while True:
    try:
        if sc.ut >= node_ut - lead:
            break
        try: B._request("POST", "/vessel/refuel", json={"fraction": "1.0"})
        except Exception: pass
        time.sleep(2)
    except Exception as e:
        log(f"  coast drop -> reconnect ({str(e)[:40]})"); reconnect()
try: B._request("POST", "/vessel/refuel", json={"fraction": "1.0"})
except Exception: pass

# 2) Point along the node vector, then FULL throttle until escape (body becomes Sun) or node consumed.
log("burning along node vector at full throttle until escape")
burning = False
while True:
    try:
        nodes = v.control.nodes
        rem = nodes[0].remaining_delta_v if nodes else 0.0
        if not nodes or rem < 1.0:
            v.control.throttle = 0.0
            log(f"  node consumed (full burn); body={v.orbit.body.name} ecc={v.orbit.eccentricity:.2f}"); break
        for e in v.parts.engines:
            try: e.active = True
            except Exception: pass
        v.control.sas = False
        ap = v.auto_pilot
        if nodes:
            ap.reference_frame = nodes[0].reference_frame
            ap.target_direction = (0.0, 1.0, 0.0)
            ap.engage()
        if not nodes or abs(ap.error) < 10.0:
            v.control.throttle = 1.0
            burning = True
        time.sleep(0.8)
    except Exception as e:
        log(f"  burn drop -> reconnect ({str(e)[:40]})"); reconnect()
try: v.control.throttle = 0.0
except Exception: pass
try: sc.save("persistent")
except Exception: pass
log(f"done; body={v.orbit.body.name} ap={v.orbit.apoapsis_altitude/1e9:.2f}Gm — saved")
