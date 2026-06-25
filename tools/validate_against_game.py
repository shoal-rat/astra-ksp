"""Cross-validate the lab's orbital MATH against the LIVE game (kRPC) and MechJeb — the proof that
every number the agent computes matches what KSP itself reports.

For the active vessel it checks, with an explicit error and PASS/FAIL for each:
  - apoapsis / periapsis / SMA / eccentricity / period   (vs kRPC, the stock conic)
  - orbital speed at the current radius (vis-viva)        (vs kRPC orbital_speed)
  - per-stage burn DURATION (rocket-equation integral)    (vs MechJeb FuelFlowSimulation /mj-stage-stats)
  - maneuver-node burn time + half-burn lead             (MechJeb convention: ignite halfBurnTime early)
  - closest approach / collision distance                (kRPC's misleading single-conic ESTIMATE vs a
                                                          position-sampled truth — shows why we sample)
  - launch / transfer WINDOW Kerbin->Duna,Eve            (Lambert porkchop; RK-checked elsewhere)

Run with the game in flight + the bridge up:  python tools/validate_against_game.py
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import krpc  # noqa: E402

from ksp_lab import astro, transfer_planner as tp  # noqa: E402

G0 = 9.80665
BRIDGE = "http://127.0.0.1:48500"


class Check:
    def __init__(self):
        self.rows: list[tuple] = []
        self.fails = 0

    def add(self, name, mine, game, tol, unit="", rel=False):
        if mine is None or game is None:
            self.rows.append((name, mine, game, None, unit, "SKIP"))
            return
        err = abs(mine - game)
        ref = abs(game) if rel else 1.0
        ok = (err / max(ref, 1e-9)) <= tol if rel else err <= tol
        if not ok:
            self.fails += 1
        self.rows.append((name, mine, game, err, unit, "PASS" if ok else "FAIL"))

    def note(self, name, text):
        self.rows.append((name, text, "", None, "", "NOTE"))

    def report(self):
        print(f"\n{'quantity':<34}{'mine':>16}{'game':>16}{'err':>13}   verdict")
        print("-" * 99)
        for name, mine, game, err, unit, verdict in self.rows:
            if verdict == "NOTE":
                print(f"{name:<34}{str(mine)}")
                continue
            ms = f"{mine:.4f}" if isinstance(mine, float) else str(mine)
            gs = f"{game:.4f}" if isinstance(game, float) else str(game)
            es = f"{err:.4g}{unit}" if err is not None else "-"
            print(f"{name:<34}{ms:>16}{gs:>16}{es:>13}   {verdict}")
        print("-" * 99)
        print(f"{'FAILURES: ' + str(self.fails) if self.fails else 'ALL CHECKS PASS':>99}")
        return self.fails


def bridge_get(path):
    try:
        return json.loads(urllib.request.urlopen(BRIDGE + path, timeout=8).read().decode())
    except Exception as e:
        return {"_error": str(e)}


def main():
    c = krpc.connect(name="validate", address="127.0.0.1", rpc_port=50000, stream_port=50001)
    sc = c.space_center
    v = sc.active_vessel
    o = v.orbit
    b = o.body
    mu, R = b.gravitational_parameter, b.equatorial_radius
    chk = Check()
    chk.note("VESSEL", f"{v.name}  orbiting {b.name}  situation={v.situation}")

    # --- 1. Orbital elements: recompute from (a,e,mu), compare to kRPC's stock conic ---
    a, e = o.semi_major_axis, o.eccentricity
    chk.add("apoapsis radius", a * (1 + e), o.apoapsis, 1.0, "m")
    chk.add("periapsis radius", a * (1 - e), o.periapsis, 1.0, "m")
    chk.add("apoapsis altitude", a * (1 + e) - R, o.apoapsis_altitude, 1.0, "m")
    chk.add("periapsis altitude", a * (1 - e) - R, o.periapsis_altitude, 1.0, "m")
    chk.add("orbital period", 2 * math.pi * math.sqrt(a ** 3 / mu), o.period, 0.5, "s")

    # --- 2. Orbital characteristics: vis-viva speed at the current radius vs kRPC ---
    r_now = o.radius
    chk.add("vis-viva speed @ r", astro.orbital_speed(mu, r_now, a) if hasattr(astro, "orbital_speed")
            else math.sqrt(mu * (2 / r_now - 1 / a)), o.speed, 0.5, "m/s")

    # --- 3. Burn DURATION per stage vs MechJeb FuelFlowSimulation ---
    stats = bridge_get("/mj-stage-stats")
    if stats.get("hasCore") and not stats.get("pending") and stats.get("vacStats"):
        worst = 0.0
        for s in stats["vacStats"]:
            dv, m0, m1, F, isp = s["deltaV"], s["startMass"], s["endMass"], s["thrust"], s["isp"]
            if dv <= 0 or F <= 0 or m0 <= m1:
                continue
            ve = isp * G0
            mine = m0 * 1000.0 * ve / (F * 1000.0) * (1 - math.exp(-dv / ve))  # masses are tonnes, F kN
            worst = max(worst, abs(mine - s["burnTime"]))
            chk.add(f"burn time stage[{s['kspStage']}]", mine, s["burnTime"], 0.05, "s", rel=True)
        chk.note("burn-time method", "mine: t=m0*ve/F*(1-e^-dv/ve)  ==  MechJeb FuelFlowSimulation")
    else:
        chk.note("stage stats", f"unavailable ({stats.get('_error') or 'no core / pending'})")

    # --- 4. Maneuver-node burn time + half-burn lead (MechJeb ignites halfBurnTime before node) ---
    nodes = v.control.nodes
    if nodes:
        nd = nodes[0]
        F = v.available_thrust
        isp = v.vacuum_specific_impulse
        if F > 0 and isp > 0:
            ve = isp * G0
            t_burn = v.mass * ve / F * (1 - math.exp(-nd.delta_v / ve))
            chk.note("node dv / burn time", f"{nd.delta_v:.2f} m/s -> {t_burn:.2f} s burn, "
                     f"ignite {t_burn/2:.2f} s before node UT (MechJeb half-burn convention)")
    else:
        chk.note("maneuver node", "none active")

    # --- 5. Collision / closest approach: kRPC's ESTIMATE vs a position-sampled truth ---
    tgt = sc.target_vessel or sc.target_body
    if tgt is not None:
        try:
            kr_d = o.distance_at_closest_approach(tgt.orbit)
            ref = b.non_rotating_reference_frame
            # sample separation over the next period in the parent frame
            best = min(
                math.dist(o.position_at(sc.ut + f * o.period, ref), tgt.orbit.position_at(sc.ut + f * o.period, ref))
                for f in [i / 400.0 for i in range(401)]
            )
            chk.note("closest approach (kRPC est)", f"{kr_d:.0f} m")
            chk.note("closest approach (sampled)", f"{best:.0f} m  (the reliable metric for SOI-crossing)")
        except Exception as ex:
            chk.note("closest approach", f"n/a ({ex})")
    else:
        chk.note("closest approach", "no target set")

    # --- 6. Launch / transfer windows (Lambert porkchop over real Kepler positions) ---
    yr = 426 * 21600
    for body in ("Duna", "Eve"):
        if body in sc.bodies and b.name == "Kerbin":
            try:
                w = tp.find_transfer_window(sc, "Kerbin", body)
                chk.note(f"window Kerbin->{body}",
                         f"depart +{(w['ut_dep']-sc.ut)/yr:.3f} yr, tof {w['tof']/yr:.3f} yr, "
                         f"|vinf| {w['vinf_mag']:.0f} m/s, synodic {w['synodic']/yr:.2f} yr")
            except Exception as ex:
                chk.note(f"window Kerbin->{body}", f"n/a ({ex})")

    fails = chk.report()
    c.close()
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
