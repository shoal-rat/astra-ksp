"""Prove the crew capability end-to-end via the bridge: launch a crewed craft, SPAWN astronauts into
it, then TRANSFER one between modules. Uses only the KspAutomationBridge HTTP API (no kRPC), so it is
robust across the launch scene transition.

    PYTHONPATH=src python tools/crew_demo.py configs/local-ksp.yaml
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from copy import deepcopy
from uuid import uuid4

from ksp_lab.artemis import build_artemis_architecture
from ksp_lab.bridge_client import BridgeClient
from ksp_lab.mission import MissionPlanner
from ksp_lab.parts import estimate_design
from ksp_lab.runner import AutomationRunner

BRIDGE = "http://127.0.0.1:48500"


def call(path, body=None, method="POST"):
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(BRIDGE + path, data=data,
                                 headers={"content-type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> int:
    config = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    runner = AutomationRunner(config, offline=False)
    o = deepcopy(build_artemis_architecture(MissionPlanner().interpret("crew")).vehicle("orion").design)
    o.name = f"AI-CrewDemo-{uuid4().hex[:6]}"
    o.estimates = estimate_design(o)
    runner.writer.write(o, runner._craft_dir(), template_path=None)
    bridge = BridgeClient(**runner.config["bridge"])
    log(f"launching crewed craft {o.name} (pod + crew cabin) to the pad ...")
    runner._load_and_launch(bridge, o.name)
    time.sleep(6)  # let the pad scene settle before touching crew

    # ADD astronauts: seat roster kerbals into the craft's free crewable seats.
    spawned = []
    for i in (1, 2, 3):
        r = call("/spawn-crew", {})
        msg = r.get("message") or r.get("error")
        log(f"  spawn-crew #{i}: ok={r.get('ok')} — {msg}")
        if r.get("ok"):
            spawned.append(r.get("crew"))
        else:
            break
    log(f"astronauts aboard: {spawned}")

    # TRANSFER one astronaut to another module (bridge moves the first crew member to a free seat in
    # a DIFFERENT crewable part).
    rt = call("/transfer-crew", {})
    log(f"  transfer-crew: ok={rt.get('ok')} — {rt.get('message') or rt.get('error')}")

    ok = len(spawned) >= 2 and bool(rt.get("ok"))
    log(f"RESULT: {'SUCCESS — astronauts ADDED to the ship and TRANSFERRED between modules' if ok else 'INCOMPLETE'}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
