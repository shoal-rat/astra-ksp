"""Resiliently warp to the EXISTING ejection node and burn it. The long interplanetary rails warp
drops the kRPC connection (an unguarded sc.ut then kills the driver) — here every kRPC call is wrapped
so a drop just reconnects and resumes, using the node already on the vessel (no costly re-plan that
jumps to the next synodic window)."""
from __future__ import annotations
import time, yaml, krpc
from ksp_lab.bridge_client import BridgeClient
from ksp_lab import execute

CFG = yaml.safe_load(open("configs/local-ksp.yaml", encoding="utf-8"))


def conn():
    k = CFG["krpc"]
    return krpc.connect(name="eject", address=k["host"], rpc_port=k["rpc_port"], stream_port=k["stream_port"])


def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


c = conn(); sc = c.space_center; v = sc.active_vessel
if not v.control.nodes:
    log("no ejection node on the vessel — abort"); raise SystemExit(2)
target = v.control.nodes[0].ut - 240.0
log(f"resilient warp: node off {(v.control.nodes[0].ut - sc.ut)/86400:.1f} d, crew={v.crew_count}")
while True:
    try:
        now = sc.ut
        if now >= target - 30.0:
            break
        sc.warp_to(min(target, now + 10 * 86400.0))
    except Exception as e:
        log(f"  warp drop -> reconnect ({str(e)[:50]})")
        time.sleep(3)
        try: c.close()
        except Exception: pass
        try:
            c = conn(); sc = c.space_center; v = sc.active_vessel
        except Exception as e2:
            log(f"  reconnect failed, retry ({str(e2)[:40]})"); time.sleep(5); continue
try: sc.rails_warp_factor = 0
except Exception: pass
time.sleep(2)
log(f"warp done; node in {(v.control.nodes[0].ut - sc.ut):.0f}s — executing ejection burn")
b = BridgeClient(**CFG["bridge"])
ok = execute.execute_node(sc, b, v)
log(f"ejection burn ok={ok}; body={v.orbit.body.name} ap={v.orbit.apoapsis_altitude/1e9:.2f} Gm")
try: sc.save("persistent")
except Exception: pass
log("saved persistent")
