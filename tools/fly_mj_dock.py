"""Autonomous rendezvous + dock + crew transfer, delegating the hard control to MechJeb.

The LLM/agent layer does NOT compute guidance — it sets the chaser active, tells MechJeb (via the
KspAutomationBridge /mj-* endpoints) to rendezvous then dock, polls /mj-status until the ports mate,
and transfers crew. MechJeb flies the phasing, approach, alignment, and port mating.

    PYTHONPATH=src python tools/fly_mj_dock.py configs/local-ksp.yaml <CHASER> <TARGET>

Requires: the rebuilt KspAutomationBridge.dll (with /mj-* endpoints) installed AND the
MechJebForAll.cfg patch installed, then KSP reloaded so every command pod carries a MechJebCore.
"""
from __future__ import annotations

import sys
import time

from ksp_lab.bridge_client import BridgeClient
from ksp_lab.config import load_config
from ksp_lab.flight_controller import KrpcFlightController


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _poll_until(bridge: BridgeClient, done, *, timeout_s: float, label: str, period: float = 3.0):
    """Poll /mj-status until done(status) is truthy or timeout. Returns the last status dict."""
    t0 = time.monotonic()
    last = {}
    last_msg = ""
    while time.monotonic() - t0 < timeout_s:
        try:
            last = bridge.mj_status()
        except Exception as exc:  # transient bridge hiccup; keep polling
            log(f"  ({label}) status error: {exc}")
            time.sleep(period)
            continue
        msg = f"{label}: dist? dockEnabled={last.get('dockEnabled')} rvEnabled={last.get('rvEnabled')} " \
              f"parts={last.get('partCount')} port={last.get('myPortState')!r} " \
              f"dock={last.get('dockStatus')!r} rv={last.get('rvStatus')!r}"
        if msg != last_msg:
            log("  " + msg)
            last_msg = msg
        if done(last):
            return last
        time.sleep(period)
    return last


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "configs/local-ksp.yaml"
    chaser_name = sys.argv[2] if len(sys.argv) > 2 else "AI-Crew-Chaser2"
    target_name = sys.argv[3] if len(sys.argv) > 3 else "AI-Crew-Target"

    cfg = load_config(__import__("pathlib").Path(config_path).resolve())
    ctrl = KrpcFlightController(cfg["krpc"])
    bridge = BridgeClient(**cfg["bridge"])
    conn = ctrl._connect("mjdock")
    sc = conn.space_center

    chaser = ctrl._select_vessel(conn, chaser_name)
    target = ctrl._select_vessel(conn, target_name)
    sc.active_vessel = chaser
    try:
        sc.rails_warp_factor = 0
        sc.physics_warp_factor = 0
        chaser.control.remove_nodes()
    except Exception:
        pass
    chaser.control.rcs = True
    try:
        sc.target_vessel = target
    except Exception:
        pass

    dist0 = ctrl._relative_distance_m(chaser, target)
    crew0 = chaser.crew_count
    parts0 = len(chaser.parts.all)
    log(f"chaser={chaser_name} target={target_name} dist={dist0:.0f} m crew={crew0} parts={parts0}")

    # Seat at least 2 real astronauts to transfer, if the chaser launched headless/empty.
    if crew0 < 1:
        for _ in range(2):
            try:
                r = bridge.spawn_crew()
                log(f"  spawn-crew: {r.get('message') or r.get('crew')}")
            except Exception as exc:
                log(f"  spawn-crew failed: {exc}")
                break

    # 1) RENDEZVOUS to close the distance with the MAIN engine (efficient). RCS docking alone would
    # drain monoprop over a km-scale approach, so close to ~60 m first, then hand to the docking AP.
    if dist0 > 120.0:
        log("RENDEZVOUS via MechJeb (main-engine close) ...")
        try:
            bridge.mj_rendezvous(target_name, desired_distance=60.0)
        except Exception as exc:
            log(f"  mj-rendezvous rejected: {exc}")
            return 2
        st = _poll_until(
            bridge,
            lambda s: not s.get("rvEnabled", False),
            timeout_s=1800.0, label="rendezvous",
        )
        log(f"  rendezvous ended: rvStatus={st.get('rvStatus')!r}")

    # Top up monopropellant so the RCS docking phase has full tanks for the final mate.
    try:
        r = bridge.refuel_vessel(chaser_name, fraction=1.0, resources="MonoPropellant")
        log(f"  refuel monoprop: {r.get('message')}")
    except Exception as exc:
        log(f"  refuel skipped: {exc}")

    # 2) DOCK. MechJeb aligns the ports and mates them (RCS, short range).
    log("DOCK via MechJeb ...")
    try:
        d = bridge.mj_dock(target_name, speed_limit=2.0)
        parts_before = int(d.get("chaserPartCount", parts0))
    except Exception as exc:
        log(f"  mj-dock rejected: {exc}")
        return 2

    def docked(s: dict) -> bool:
        pc = s.get("partCount")
        port = (s.get("myPortState") or "")
        if isinstance(pc, (int, float)) and pc > parts_before:
            return True
        if "Docked" in port or "PreAttached" in port:
            return True
        # dock AP turned itself off without a physical dock => aborted; stop polling
        if s.get("dockEnabled") is False and not s.get("targetExists", True):
            return True
        return False

    st = _poll_until(bridge, docked, timeout_s=1500.0, label="dock", period=3.0)
    pc = st.get("partCount")
    success = (isinstance(pc, (int, float)) and pc > parts_before) or \
              ("Docked" in (st.get("myPortState") or "")) or \
              ("PreAttached" in (st.get("myPortState") or ""))

    if not success:
        log(f"DOCK NOT CONFIRMED: {st}")
        return 2
    log(f"DOCKED [OK]  partCount {parts_before} -> {pc}, port={st.get('myPortState')!r}")

    # 3) CREW TRANSFER (the two craft are now one vessel; move a kerbal across).
    try:
        r = bridge.transfer_crew(target_name)
        log(f"  transfer-crew: ok={r.get('ok')} — {r.get('message') or r.get('error')}")
    except Exception as exc:
        log(f"  transfer-crew failed: {exc}")

    log("=== MECHJEB DOCK + TRANSFER COMPLETE ===")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
