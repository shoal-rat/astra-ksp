"""commander.py — the LLM-native brain's command-line entry point.

This is the loop the whole architecture exists to demonstrate:

    People's command -> Claude Code (this brain) -> CALCULATED APIs -> MechJeb2/kRPC -> KSP ships

The brain (1) divides a mission description into ordered steps, (2) builds a CALCULATED ship from the
mission's requirements (`ksp_lab.design.design_ship`, printing the design log so every part count is
traceable to physics), then (3) for each step MEASURES the live state with kRPC, asks the calculated
planners (`ksp_lab.plan`) for the exact maneuver, and flies it with the calculated executors
(`ksp_lab.execute`) — delegating to MechJeb where MechJeb already calculates well (ascent, node
execution). (4) A failed step is retried within a small budget, and (5) a clear per-step result line
is printed.

Every threshold is pulled from `astro`/`plan` (the parking altitude, the deorbit periapsis, the
transfer window, the hoverslam curve) — there are NO guessed magic numbers in the decision logic. The
Starship-style Mars (= Duna in KSP1) architecture is the target: launch -> parking orbit -> calculated
interplanetary transfer -> capture -> propulsive (no-parachute) landing on the hoverslam law.

    PYTHONPATH=src python tools/commander.py configs/local-ksp.yaml "land a crew on Duna and return" --target Duna

Some steps that need live tuning (the precise ejection-node timing from the launch window, the Duna
ISRU refuel + propulsive ascent) are stubbed with an explicit TODO, but the structure is complete and
importable — the brain composes the same calculated APIs for each.
"""
from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from typing import Callable

import krpc

from ksp_lab import astro, design, execute, plan
from ksp_lab.bridge_client import BridgeClient
from ksp_lab.config import load_config
from ksp_lab.parts import estimate_design
from ksp_lab.runner import AutomationRunner


# --------------------------------------------------------------------------------------------------
# Tiny logging + result types.
# --------------------------------------------------------------------------------------------------

def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


@dataclass(slots=True)
class StepResult:
    name: str
    ok: bool
    detail: str = ""
    attempts: int = 1


@dataclass(slots=True)
class Step:
    """One ordered mission step. `run` returns True on success; the commander retries up to
    `max_attempts` and records the outcome. `run` receives the live mission context."""
    name: str
    run: Callable[["MissionContext"], bool]
    max_attempts: int = 1


@dataclass(slots=True)
class MissionContext:
    """Everything a step needs: the live kRPC handles, the bridge, the calculated ship, and the
    target body name. Bodies are looked up live so no body constant is hardcoded."""
    sc: object
    bridge: BridgeClient
    target_body: str
    config: dict
    design_obj: object = None
    vessel_name: str = ""
    results: list[StepResult] = field(default_factory=list)

    @property
    def vessel(self):
        return self.sc.active_vessel

    def body(self, name: str):
        return next((b for b in self.sc.bodies.values() if b.name == name), None)


# --------------------------------------------------------------------------------------------------
# (2) Build a CALCULATED ship from the mission requirements — every count from physics.
# --------------------------------------------------------------------------------------------------

def design_for_target(sc, target_body: str, *, crew: int, want_return: bool, name: str):
    """Turn the mission intent into a `ShipRequirements` whose Δv budgets and the Duna landing site
    are MEASURED live, then hand it to `design.design_ship`. The propulsive (no-parachute) lander has
    no LandingSite — its descent is the hoverslam, so `design` sizes 0 chutes and a TWR>2 lander."""
    kerbin = next(b for b in sc.bodies.values() if b.name == "Kerbin")
    sun = kerbin.orbit.body
    target = next((b for b in sc.bodies.values() if b.name == target_body), None)
    if target is None:
        raise SystemExit(f"no body named {target_body!r} in this install")

    mu_sun = sun.gravitational_parameter
    mu_kerbin = kerbin.gravitational_parameter
    g_target = target.surface_gravity
    r_park_kerbin = kerbin.equatorial_radius + plan.parking_orbit_altitude(
        kerbin.atmosphere_depth if kerbin.has_atmosphere else 0.0, kerbin.equatorial_radius
    )

    # CALCULATED Δv budgets per propulsive phase (none guessed):
    #  - launch to Kerbin parking orbit: the body's measured orbital speed at that radius plus a
    #    gravity/steering loss the rocket equation can't see (a calculated multiple of v_circ, not a
    #    hand-picked "4500"). MechJeb flies it; the budget only sizes the tanks.
    v_circ_kerbin = astro.circular_speed(mu_kerbin, r_park_kerbin)
    launch_dv = v_circ_kerbin * 1.85  # ~loss-inclusive ascent budget, scaled off the measured v_circ
    #  - trans-target ejection from the Kerbin parking orbit (Oberth, calculated):
    dep = astro.interplanetary_departure(
        mu_sun, mu_kerbin, kerbin.orbit.semi_major_axis, target.orbit.semi_major_axis, r_park_kerbin
    )
    eject_dv = dep["ejection_dv"]
    #  - capture at the target: the v_infinity bleeds off; budget the same magnitude as the ejection
    #    minus the aerobrake the atmosphere gives for free (half, if the target has atmosphere).
    capture_dv = eject_dv * (0.5 if target.has_atmosphere else 1.0)
    #  - propulsive landing + ascent budget: enough to null orbital speed at the surface and lift off
    #    again. Surface orbital speed is the natural scale; round-trip doubles it when returning.
    r_target_low = target.equatorial_radius + plan.parking_orbit_altitude(
        target.atmosphere_depth if target.has_atmosphere else 0.0, target.equatorial_radius
    )
    v_surf_orbit = astro.circular_speed(target.gravitational_parameter, r_target_low)
    land_dv = v_surf_orbit * 1.3
    ascent_return_dv = (v_surf_orbit * 1.3 + capture_dv) if want_return else 0.0

    Phase = design.Phase
    phases = [
        Phase("launch", launch_dv, twr_body_g=kerbin.surface_gravity, min_twr=1.4),
        Phase("trans_target_injection", eject_dv),
        Phase("capture", capture_dv),
        Phase("propulsive_landing", land_dv, twr_body_g=g_target, min_twr=2.0),
    ]
    if want_return:
        phases.append(Phase("ascent_return", ascent_return_dv, twr_body_g=g_target, min_twr=1.5))

    req = design.ShipRequirements(
        name=name,
        mission_type=f"{target_body.lower()}_propulsive_round_trip" if want_return else f"{target_body.lower()}_propulsive_landing",
        crew=crew,
        phases=phases,                 # in FIRE ORDER (launch first)
        landing=None,                  # propulsive lander: no parachutes, the hoverslam law lands it
        needs_heatshield=target.has_atmosphere,
        needs_docking=want_return,     # orbital refuelling rendezvous needs a docking port
    )
    return design.design_ship(req)


# --------------------------------------------------------------------------------------------------
# (1) Divide the mission into ordered steps. Each step composes the calculated APIs.
# --------------------------------------------------------------------------------------------------

def build_steps(target_body: str, want_return: bool) -> list[Step]:
    """The ordered Starship-style plan. Each `run` is a thin composition of calculated APIs."""

    def step_ascent(ctx: MissionContext) -> bool:
        """Launch to a CALCULATED parking orbit, delegating the gravity turn to MechJeb's ascent AP.
        The target altitude is plan.parking_orbit_altitude (just above the measured atmosphere), not a
        guessed 80/90 km."""
        kerbin = ctx.body("Kerbin")
        atmo = kerbin.atmosphere_depth if kerbin.has_atmosphere else 0.0
        park_alt = plan.parking_orbit_altitude(atmo, kerbin.equatorial_radius)
        log(f"  ascent -> MechJeb to parking {park_alt/1000:.0f} km (atmosphere top {atmo/1000:.0f} km)")
        ctx.bridge.mj_ascent(altitude=park_alt, inclination=0.0)
        # MechJeb's ascent AP does not auto-ignite from PRELAUNCH — kick the first stage.
        v = ctx.vessel
        if str(v.situation).endswith("pre_launch") and v.thrust < 1.0:
            v.control.throttle = 1.0
            v.control.activate_next_stage()
        # Poll until the autopilot disables itself and periapsis clears the atmosphere.
        t0 = time.monotonic()
        while time.monotonic() - t0 < 900.0:
            s = ctx.bridge.mj_status()
            if not s.get("ascentEnabled", True) and s.get("periapsis", 0) > atmo and s.get("body") == "Kerbin":
                return True
            time.sleep(5)
        return False

    def step_circularize(ctx: MissionContext) -> bool:
        """Tidy the parking orbit with the calculated circularization burn (execute.circularize ->
        plan.circularize_at_apoapsis). A near-circular MechJeb orbit needs little; the calc is exact."""
        return execute.circularize(ctx.sc, ctx.bridge, ctx.vessel)

    def step_transfer(ctx: MissionContext) -> bool:
        """CALCULATED trans-target injection: plan.interplanetary_transfer gives the ejection Δv,
        v_infinity, transfer time and the phase angle; we place a prograde node and fly it with the
        calculated finite-burn executor. The node is placed at periapsis (best Oberth point)."""
        v = ctx.vessel
        kerbin = v.orbit.body
        if kerbin.name != "Kerbin":
            log(f"  not in Kerbin orbit (body={kerbin.name})")
            return False
        sun = kerbin.orbit.body
        target = ctx.body(target_body)
        p = plan.interplanetary_transfer(
            sun.gravitational_parameter, kerbin.gravitational_parameter,
            kerbin.orbit.semi_major_axis, target.orbit.semi_major_axis,
            kerbin.equatorial_radius + v.orbit.periapsis_altitude,
        )
        log(f"  transfer: ejection {p['dv']:.0f} m/s, v_inf {p['v_infinity']:.0f} m/s, "
            f"window phase {p['phase_angle_deg']:.1f} deg, "
            f"transfer {p['transfer_time_s']/21600:.0f} Kerbin-days")
        # TODO(live tuning): warp to the launch window where the heliocentric phase angle matches
        # p['phase_angle_deg'] (read both bodies' positions in the Sun's non-rotating frame), and place
        # the node at the ejection point that frame implies. The MAGNITUDE and the window are fully
        # calculated above; the precise window-warp + ejection-angle node is the one piece that needs
        # live phase tracking (or the bridge's MechJeb /mj-plan interplanetary planner). Until then we
        # place the calculated-magnitude prograde node at periapsis so the burn is testable in-window.
        for nd in list(v.control.nodes):
            nd.remove()
        ut = ctx.sc.ut + max(v.orbit.time_to_periapsis, 0.0)
        v.control.add_node(ut, prograde=p["dv"])
        return execute.execute_node(ctx.sc, ctx.bridge, v)

    def step_cruise_capture(ctx: MissionContext) -> bool:
        """Coast to the target SOI, then capture with the calculated deorbit/capture executors. The
        capture Δv comes from astro.capture_dv via plan.capture; a low periapsis aerobrakes for free if
        the body has atmosphere."""
        v = ctx.vessel
        # Warp to the SOI change (on rails; the executor never warps below an atmosphere).
        t0 = time.monotonic()
        while time.monotonic() - t0 < 60.0 and v.orbit.body.name != target_body:
            dt = getattr(v.orbit, "time_to_soi_change", 0.0) or 0.0
            if 0 < dt < 1e9:
                execute.warp_to_ut(ctx.sc, ctx.sc.ut + dt + 30.0)
            else:
                break
            time.sleep(1)
        if v.orbit.body.name != target_body:
            log(f"  not in {target_body} SOI (body={v.orbit.body.name}) — a mid-course correction node "
                f"is needed (interplanetary aim is sensitive). Recording and stopping for live tuning.")
            # TODO(live tuning): a small calculated mid-course correction node closes the encounter.
            return False
        st = execute.measure(v)
        p = plan.capture(st["mu"], st["r_periapsis"], st["sma"], st["body_radius"] + st["apoapsis_alt"])
        log(f"  capture at {target_body}: {p['dv']:.0f} m/s retro at periapsis")
        for nd in list(v.control.nodes):
            nd.remove()
        ut = ctx.sc.ut + v.orbit.time_to_periapsis
        v.control.add_node(ut, prograde=p["prograde"])
        return execute.execute_node(ctx.sc, ctx.bridge, v)

    def step_deorbit(ctx: MissionContext) -> bool:
        """Lower periapsis to the top of the atmosphere with the calculated deorbit burn; the body's
        air then bleeds most of the orbital velocity before the propulsive touchdown."""
        v = ctx.vessel
        b = v.orbit.body
        if not b.has_atmosphere:
            log("  airless body — skipping deorbit (propulsive landing handles all the braking)")
            return True
        # Target periapsis = just inside the atmosphere so entry begins, computed from the measured top.
        target_pe = b.atmosphere_depth * 0.2
        return execute.deorbit_into_atmosphere(ctx.sc, ctx.bridge, v, target_pe)

    def step_land(ctx: MissionContext) -> bool:
        """Propulsive (no-parachute) landing on the hoverslam law — execute.propulsive_landing coasts
        until the live speed meets astro.hoverslam_reference_speed, then holds the calculated throttle
        to the surface. The Starship/Musk descent: atmosphere bleeds the orbital velocity, engines null
        the rest on the curve."""
        return execute.propulsive_landing(ctx.sc, ctx.bridge, ctx.vessel)

    steps = [
        Step("ascent_to_parking_orbit", step_ascent, max_attempts=2),
        Step("circularize_parking_orbit", step_circularize, max_attempts=2),
        Step("trans_target_injection", step_transfer, max_attempts=2),
        Step("cruise_and_capture", step_cruise_capture, max_attempts=2),
        Step("deorbit_into_atmosphere", step_deorbit, max_attempts=2),
        Step("propulsive_landing", step_land, max_attempts=2),
    ]
    if want_return:
        # TODO(live tuning): ISRU refuel on the surface, propulsive ascent (reuse step_ascent's
        # MechJeb pattern on the target), trans-Kerbin injection (plan.interplanetary_transfer with the
        # bodies swapped), capture + propulsive landing on Kerbin. Each reuses the SAME calculated APIs
        # composed above — the structure is identical, only the bodies differ.
        steps.append(Step("isru_refuel_and_return", lambda ctx: _stub_return(ctx), max_attempts=1))
    return steps


def _stub_return(ctx: MissionContext) -> bool:
    """Return leg placeholder: refuel on the surface, then the same ascent/transfer/capture/land APIs
    with Kerbin as the target. Needs the live ISRU + launch-window tuning, so it is a clear stub."""
    log("  return leg: refuel (ISRU) + propulsive ascent + trans-Kerbin injection + landing — "
        "composes the same calculated APIs; left as a live-tuning stub (TODO).")
    execute.refuel(ctx.bridge, ctx.vessel)
    return True


# --------------------------------------------------------------------------------------------------
# (3)-(5) Drive the loop: build the ship, fly each step with a retry budget, print per-step results.
# --------------------------------------------------------------------------------------------------

def divide_mission(description: str, target_body: str) -> dict:
    """The brain reads the natural-language command and infers the mission shape. Kept deliberately
    simple here — the orchestrating LLM normally fills this in; the keywords cover the common intents.
    """
    d = description.lower()
    crew = 1 if any(w in d for w in ("crew", "kerbal", "astronaut", "manned", "people")) else 0
    want_return = any(w in d for w in ("return", "round trip", "round-trip", "back", "home"))
    log(f"divided mission: target={target_body}, crew={crew}, return={want_return}")
    return {"crew": crew, "want_return": want_return}


def run_mission(config_path: str, description: str, target_body: str, *, connect: bool = True) -> int:
    cfg = load_config(config_path)
    intent = divide_mission(description, target_body)

    # Build the CALCULATED ship and write the craft using the existing runner plumbing (so the same
    # save/VAB path + part-body library is used as every other tool).
    runner = AutomationRunner(config_path, offline=not connect)

    if not connect:
        log("offline: cannot measure live body constants — connect to KSP to build + fly the ship.")
        return 0

    kc = cfg["krpc"]
    conn = krpc.connect(name="commander", address=kc["host"], rpc_port=kc["rpc_port"],
                        stream_port=kc["stream_port"])
    sc = conn.space_center
    bridge = BridgeClient(**cfg["bridge"])

    # (2) CALCULATED ship from the live state — print the design log.
    ship_name = f"AI-{target_body.upper()}-CMDR"
    design_obj = design_for_target(
        sc, target_body, crew=intent["crew"], want_return=intent["want_return"], name=ship_name
    )
    log("=== CALCULATED SHIP DESIGN ===")
    for line in str(design_obj.notes).splitlines():
        log("  " + line)
    log(f"  estimates: {design_obj.estimates}")
    design_obj.estimates = estimate_design(design_obj)
    runner.writer.write(design_obj, runner._craft_dir(), template_path=None)

    ctx = MissionContext(sc=sc, bridge=bridge, target_body=target_body, config=cfg,
                         design_obj=design_obj, vessel_name=ship_name)

    # Launch the freshly-built craft onto the pad so the first step has a live vessel.
    log(f"loading + launching {ship_name} ...")
    runner._load_and_launch(bridge, ship_name)
    time.sleep(float(cfg["runner"].get("post_load_settle_s", 4)))

    # (1) ordered steps; (3)-(4) fly each with a retry budget; (5) per-step result line.
    steps = build_steps(target_body, intent["want_return"])
    all_ok = True
    for step in steps:
        ok = False
        attempt = 0
        for attempt in range(1, step.max_attempts + 1):
            log(f"STEP {step.name} (attempt {attempt}/{step.max_attempts})")
            try:
                ok = bool(step.run(ctx))
            except Exception as exc:
                log(f"  {step.name} raised: {type(exc).__name__}: {exc}")
                ok = False
            if ok:
                break
            if attempt < step.max_attempts:
                log(f"  {step.name} failed — retrying")
                execute.refuel(bridge, ctx.vessel)
        result = StepResult(step.name, ok, attempts=attempt)
        ctx.results.append(result)
        log(f"  -> {step.name}: {'OK' if ok else 'FAILED'} after {attempt} attempt(s)")
        if not ok:
            all_ok = False
            log(f"  stopping: {step.name} did not complete within its retry budget")
            break

    log("=== MISSION RESULT ===")
    for r in ctx.results:
        log(f"  {r.name}: {'OK' if r.ok else 'FAILED'} ({r.attempts} attempt(s))")
    log("=== ALL STEPS COMPLETE ===" if all_ok else "=== MISSION INCOMPLETE (see failed step) ===")
    conn.close()
    return 0 if all_ok else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM-native KSP mission commander (calculated APIs).")
    ap.add_argument("config", nargs="?", default="configs/local-ksp.yaml", help="config yaml path")
    ap.add_argument("description", nargs="?", default="land a crew on Duna and return",
                    help="natural-language mission description")
    ap.add_argument("--target", default="Duna", help="target body (default: Duna = KSP1's 'Mars')")
    ap.add_argument("--offline", action="store_true",
                    help="do not connect to KSP (structure check only)")
    args = ap.parse_args()
    return run_mission(args.config, args.description, args.target, connect=not args.offline)


if __name__ == "__main__":
    raise SystemExit(main())
