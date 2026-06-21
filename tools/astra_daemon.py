"""ASTRA daemon — bridge the in-game KSP command box to the autonomous agent.

This is the glue that makes the project agent-native: the player types a mission in the
KspAutomationBridge in-game window and presses "Run mission"; this daemon polls the bridge for that
command (GET /command/pending), runs ASTRA on it, and streams live status back to the in-game panel
(POST /status). The player watches the mission fly without leaving the game.

    PYTHONPATH=src python tools/astra_daemon.py configs/local-ksp.yaml

Runs until interrupted. Safe to leave running; it only acts when a command is queued.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

from ksp_lab.astra import AstraAgent
from ksp_lab.astra.agent import post_bridge_status

BRIDGE = "http://127.0.0.1:48500"


def poll_command() -> str | None:
    try:
        with urllib.request.urlopen(BRIDGE + "/command/pending", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        cmd = data.get("command")
        return cmd if cmd else None
    except Exception:
        return None


def main() -> int:
    config = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    print("[astra-daemon] online — polling the KSP in-game command box. Type a mission and press Run.",
          flush=True)
    post_bridge_status("daemon", "ASTRA online — type a mission and press Run mission")
    idle = 0
    while True:
        cmd = poll_command()
        if cmd:
            print(f"[astra-daemon] received from KSP: {cmd!r}", flush=True)
            post_bridge_status("mission", f"received: {cmd}")
            try:
                agent = AstraAgent(config, max_attempts=2)
                result = agent.run(cmd)
                verdict = "SUCCESS" if result.success else "INCOMPLETE"
                post_bridge_status("mission", f"{verdict} — {cmd}")
                print(f"[astra-daemon] {verdict}: {cmd}", flush=True)
            except Exception as exc:  # never let the daemon die on one bad mission
                post_bridge_status("error", str(exc)[:140])
                print(f"[astra-daemon] error: {exc}", flush=True)
            idle = 0
        else:
            idle += 1
            if idle % 20 == 0:
                post_bridge_status("daemon", "idle — waiting for a mission")
        time.sleep(3)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[astra-daemon] stopped.")
