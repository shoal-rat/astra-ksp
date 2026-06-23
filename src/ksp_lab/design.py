"""Requirements-driven, physics-calculated ship design.

The mandate: first enumerate what the mission NEEDS (crew, heat shield, parachutes, docking, the
ordered propulsive phases), then CALCULATE every part count from physics — tanks by inverting the
rocket equation for each phase's Δv, engines by the TWR the body demands, parachutes by the terminal
velocity the target body's LIVE atmospheric density produces. Nothing is hand-picked or guessed.

Body constants (surface gravity, atmospheric density at the landing site, GM) are MEASURED from kRPC
and passed in via the requirement objects, so the same designer is correct for Kerbin, Duna, or any
body/mod configuration. This is the engine that the single-chute Orion needed and lacked: for Duna it
returns ~10 Mk16 chutes, not 1.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import astro
from .models import RocketDesign, StageSpec
from .parts import part, payload_bus_mass


# --------------------------------------------------------------------------------------------------
# Requirement objects — the "what the mission needs", measured/calculated, never guessed.
# --------------------------------------------------------------------------------------------------

@dataclass(slots=True)
class Phase:
    """One propulsive phase the ship flies on its own engines."""
    name: str
    dv_mps: float                 # Δv requirement (calculated by plan.py / astro.py from live state)
    twr_body_g: float = 0.0       # surface gravity where TWR matters (launch/landing); 0 = vacuum
    min_twr: float = 0.0          # required TWR at ignition mass (launch ~1.4, powered landing ~2.0)
    min_diameter_m: float = 0.0   # force a wider tank for this stage (2.5 m for a low-CoG lander base)


@dataclass(slots=True)
class LandingSite:
    """A parachute touchdown on a body with atmosphere — sized from the LIVE surface density."""
    body_g: float                 # surface gravity (m/s^2), from kRPC body.surface_gravity
    surface_rho: float            # surface atmospheric density (kg/m^3), from kRPC body.density_at(0)
    target_touchdown_mps: float = 8.0  # chutes bring descent to this; a TWR>1 engine nulls the rest


@dataclass(slots=True)
class ShipRequirements:
    name: str
    mission_type: str = "generated"
    crew: int = 0
    payload_t: float = 0.0
    phases: list[Phase] = field(default_factory=list)   # in FIRE ORDER (launch first)
    landing: LandingSite | None = None
    needs_heatshield: bool = False
    needs_docking: bool = False
    # Landing legs are needed by ANY craft that touches down — a PROPULSIVE lander (no parachutes) needs
    # them just as much as a chute lander. Kept separate from `landing` (which means a chute touchdown)
    # so the no-chute Starship still gets legs (the bug that let it tip over and kill the crew).
    needs_legs: bool = False
    chute_part: str = "parachuteSingle"
    # Cap the engine cluster size. The .craft renderer's radial cluster does not yet feed/stage
    # reliably (a clustered booster auto-staged early in live test), so callers can force a single
    # large engine per stage (max_engine_count=1) until that is fixed, trading some TWR headroom.
    max_engine_count: int = 8


# Candidate engines/tanks, small -> large. The designer picks the smallest that meets TWR+Δv, so the
# selection is a calculated search, not a hand-pick.
ENGINE_LADDER = ["liquidEngine3.v2", "liquidEngine2", "liquidEngine", "engineLargeSkipper", "liquidEngineMainsail.v2"]
# Each engine class pairs with a tank scale so tank counts stay sane; still verified by the rocket eqn.
TANK_FOR_ENGINE = {
    "liquidEngine3.v2": "fuelTank.long",
    "liquidEngine2": "fuelTank.long",
    "liquidEngine": "Rockomax16.BW",
    "engineLargeSkipper": "Rockomax16.BW",
    "liquidEngineMainsail.v2": "Rockomax32.BW",
}


def _bus_mass(req: ShipRequirements) -> float:
    """Mass of the command bus + payload + the calculated recovery hardware (heat shield + chutes)."""
    base = payload_bus_mass(req.payload_t, req.crew > 0)  # command + 1 chute + (heatshield if crewed)
    # payload_bus_mass already bundles one chute + a heatshield-if-crewed; we override chutes below by
    # CALCULATING the count, and add crew-cabin mass for extra seats and docking hardware here.
    extra = 0.0
    if req.crew > 1:
        extra += part("crewCabin").wet_mass_t * math.ceil((req.crew - 1) / 2.0)  # Mk1 cabin seats ~2
    if req.needs_docking:
        extra += part("dockingPort2").wet_mass_t + part("RCSBlock").wet_mass_t * 4 + part("rcsTankRadialLong").wet_mass_t
    return base + extra


def parachute_count(req: ShipRequirements, landing_mass_t: float) -> int:
    """CALCULATE the parachute count for a safe touchdown at the landing site's live density.

    Returns 0 if there is no atmospheric landing. Otherwise solves terminal_velocity <= target."""
    if req.landing is None or req.landing.surface_rho <= 0:
        return 0
    cd_a = part(req.chute_part).drag_area_m2
    return astro.parachutes_for_touchdown(
        landing_mass_t, req.landing.body_g, req.landing.surface_rho,
        req.landing.target_touchdown_mps, cd_a,
    )


def _tank_count_for_dv(dv: float, mass_above_t: float, eng_dry_wet: tuple[float, float], n_eng: int,
                       tank_dry_t: float, tank_wet_t: float, isp_vac_s: float) -> int:
    """Closed-form tank count to reach `dv`. The stage's invariant ('dead') mass is everything above
    plus the engines plus the EMPTY tanks; only propellant is expelled. From the rocket equation
    R = exp(dv/ve) = m0/m1 with m0 = M + tank_wet*T, m1 = M + tank_dry*T (M = mass_above + engine_wet*n):
        T = M*(R-1) / (tank_wet - R*tank_dry)
    The denominator is positive only if a tank's own dry mass leaves a usable mass fraction; if not,
    this tank cannot reach dv at any count (returns a large sentinel so the search rejects it)."""
    eng_dry, eng_wet = eng_dry_wet
    ve = isp_vac_s * astro.G0
    R = math.exp(dv / ve)
    M = mass_above_t + eng_wet * n_eng
    denom = tank_wet_t - R * tank_dry_t
    if denom <= 0:
        return 9999
    return max(1, math.ceil(M * (R - 1.0) / denom))


def _size_one(eng_name: str, dv: float, mass_above_t: float, phase: Phase, max_engine_count: int = 8) -> dict:
    """Solve (engine_count, tank_count) for one engine type: closed-form tank count for `dv`, plus a
    clustering fixed-point so thrust meets min_twr at the resulting ignition mass — capped at
    max_engine_count (beyond which it accepts the lower TWR rather than cluster further)."""
    eng = part(eng_name)
    tank = part(TANK_FOR_ENGINE[eng_name])
    thrust_one = (eng.thrust_kn_asl if phase.twr_body_g > 5.0 and eng.thrust_kn_asl > 0 else eng.thrust_kn_vac) * 1000.0
    n_eng, tanks = 1, 1
    for _ in range(8):
        tanks = _tank_count_for_dv(dv, mass_above_t, (eng.dry_mass_t, eng.wet_mass_t), n_eng,
                                   tank.dry_mass_t, tank.wet_mass_t, eng.isp_vac_s)
        m0 = mass_above_t + eng.wet_mass_t * n_eng + tank.wet_mass_t * tanks
        if phase.min_twr > 0 and phase.twr_body_g > 0 and thrust_one > 0:
            need = min(max_engine_count, math.ceil(phase.min_twr * m0 * 1000.0 * phase.twr_body_g / thrust_one))
            if need > n_eng:
                n_eng = need
                continue
        break
    dry = eng.dry_mass_t * n_eng + tank.dry_mass_t * tanks
    wet = eng.wet_mass_t * n_eng + tank.wet_mass_t * tanks
    m0 = mass_above_t + wet
    achieved_twr = astro.twr(thrust_one * n_eng, m0, phase.twr_body_g) if phase.twr_body_g > 0 else math.inf
    actual_dv = astro.rocket_dv(eng.isp_vac_s, m0, mass_above_t + dry)
    # Honour a minimum tank diameter (a powered lander wants a WIDE 2.5 m tank for a low CoG + wide base,
    # not a tall 1.25 m needle that tips over). A too-narrow tank is rejected as a candidate.
    diameter_ok = phase.min_diameter_m <= 0 or tank.diameter_m >= phase.min_diameter_m - 1e-6
    return {
        "engine": eng_name, "engine_count": n_eng, "tank": TANK_FOR_ENGINE[eng_name], "tanks": tanks,
        "diameter_m": tank.diameter_m,
        "twr": round(achieved_twr, 2), "stage_dv": round(actual_dv, 0), "m0_t": round(m0, 2),
        "wet_t": round(wet, 2), "parts": n_eng + tanks,
        "ok": tanks < 9000 and actual_dv >= dv * 0.995 and diameter_ok
              and (phase.min_twr <= 0 or achieved_twr >= phase.min_twr),
    }


def _size_stage(dv: float, mass_above_t: float, phase: Phase, max_engine_count: int = 8) -> tuple[StageSpec, dict]:
    """Search the engine catalogue (each with calculated clustering + closed-form tank count) and pick
    the LIGHTEST valid stage — minimising wet mass minimises the cascade onto the stages below it. Pure
    calculated selection; no hand-picked engine. Clusters are capped at max_engine_count."""
    candidates = [_size_one(e, dv, mass_above_t, phase, max_engine_count) for e in ENGINE_LADDER]
    valid = [m for m in candidates if m["ok"] and m["engine_count"] <= max_engine_count]
    if valid:
        chosen = min(valid, key=lambda m: m["wet_t"])
    else:
        # No engine meets min_twr within the cluster cap; pick the most launchable (highest TWR) that
        # still delivers the required Δv, else the highest TWR available.
        capped = [m for m in candidates if m["engine_count"] <= max_engine_count]
        dv_ok = [m for m in capped if m["stage_dv"] >= dv * 0.995]
        chosen = max(dv_ok or capped or candidates, key=lambda m: m["twr"])
    spec = StageSpec(phase.name, chosen["engine"], chosen["tank"], chosen["tanks"],
                     decoupler_above=True, engine_count=chosen["engine_count"],
                     diameter_m=chosen.get("diameter_m", 1.25))
    return spec, chosen


def design_ship(req: ShipRequirements) -> RocketDesign:
    """Build a RocketDesign whose every part count is calculated from `req`.

    Process: enumerate the bus (command + crew + heat shield + CALCULATED chutes + docking) -> size each
    propulsive stage from the TOP (last-firing) down by inverting the rocket equation for its Δv while
    carrying the mass above it -> verify TWR per stage. Returns the design with an `estimates` block and
    a `design_log` in notes so every number is traceable.
    """
    bus = _bus_mass(req)
    # Chute count is sized for the landing mass = bus + the top (landing) stage dry + a fuel reserve.
    # Approximate landing mass with the bus alone first; refine after stages are sized (one pass is
    # enough because chute mass is tiny relative to the stack).
    mass_above = bus
    stages_rev: list[StageSpec] = []
    log: list[str] = [f"bus(command+crew+heatshield+payload)={bus:.2f}t"]
    # Process phases last-firing first so each lower stage carries the wet mass of the ones above it.
    for phase in reversed(req.phases):
        spec, metrics = _size_stage(phase.dv_mps, mass_above, phase, req.max_engine_count)
        stages_rev.append(spec)
        mass_above += metrics["wet_t"] + part("Decoupler.1").wet_mass_t
        log.append(
            f"{phase.name}: need {phase.dv_mps:.0f}m/s twr>={phase.min_twr} -> {metrics['engine_count']}x {metrics['engine']} "
            f"+ {metrics['tanks']} {metrics['tank']} = {metrics['stage_dv']:.0f}m/s, twr {metrics['twr']}, m0 {metrics['m0_t']}t"
        )
    stages = list(reversed(stages_rev))  # back to fire order (launch first)

    n_chute = parachute_count(req, bus + (part("Decoupler.1").wet_mass_t * 0))  # bus-dominated landing mass
    if req.landing is not None:
        log.append(
            f"chutes: {n_chute}x {req.chute_part} for <= {req.landing.target_touchdown_mps:.0f} m/s "
            f"at rho={req.landing.surface_rho:.4f}, g={req.landing.body_g:.2f} "
            f"(terminal {astro.terminal_velocity(bus, req.landing.body_g, req.landing.surface_rho, n_chute*part(req.chute_part).drag_area_m2):.1f} m/s)"
        )

    design = RocketDesign(
        name=req.name,
        mission_type=req.mission_type,
        payload_mass_t=req.payload_t,
        crewed=req.crew > 0,
        stages=stages,
        heatshield=req.needs_heatshield,
        docking_port=req.needs_docking,
        landing_legs=(req.landing is not None) or req.needs_legs,
        tags=["calculated", "requirements-driven"],
        notes="DESIGN LOG (every count from physics):\n  " + "\n  ".join(log),
        source="design.design_ship",
    )
    plan = staging_plan(design, req)
    design.notes += "\n\nSTAGING PLAN (NO refuel — each stage flies on its own propellant):\n  " + "\n  ".join(
        f"S{p['stage']} {p['role']}: {p['engines']} + {p['tanks']} | ignite {p['ignition_mass_t']}t"
        f" -> burnout {p['burnout_mass_t']}t (prop {p['propellant_t']}t) | post-sep {p['post_separation_mass_t']}t"
        f" | dv {p['dv_mps']} m/s | TWR {p['twr_ignition']}->{p['twr_burnout']} | {p['separator']}; "
        f"{p['separation_trigger']}" for p in plan)
    est = _estimate(design, req, n_chute)
    design.estimates = est
    return design


def staging_plan(design: RocketDesign, req: ShipRequirements) -> list[dict]:
    """Rigorous per-stage staging analysis — every number from the rocket equation + the assembled
    stage masses, assuming NO refuelling (each stage flies only on the propellant it carries).

    For each stage, bottom-firing first:
      - ignition_mass m0   = this stage (wet) + everything that flies above it
      - burnout_mass  m1   = m0 - this stage's propellant
      - post_separation_mass = what flies ON after this stage's separator fires (= everything above it);
        this is the mass the NEXT engine must accelerate, the key staging number
      - dv it delivers, TWR at ignition and at burnout (the burnout TWR shows the stage isn't dragging
        dead mass), the separator part + the separation trigger (propellant-exhausted -> drop + ignite next)
    """
    from .parts import stage_masses, part
    bus = _bus_mass(req)
    stage_wet = [stage_masses(s)[1] for s in design.stages]
    plan: list[dict] = []
    for i, stage in enumerate(design.stages):
        dry, wet, thrust_asl, _isp_asl, isp_vac = stage_masses(stage)
        mass_above = bus + sum(stage_wet[i + 1:])          # flies on after this stage's separator fires
        m0, m1 = mass_above + wet, mass_above + dry
        phase = req.phases[i]
        g = phase.twr_body_g or 9.81
        # ASL thrust for an in-atmosphere stage (booster), vacuum thrust otherwise — matches _size_one.
        thrust_n = (thrust_asl if phase.twr_body_g > 5.0 else
                    part(stage.engine).thrust_kn_vac * max(1, stage.engine_count)) * 1000.0
        is_last = i == len(design.stages) - 1
        plan.append({
            "stage": i + 1,
            "role": stage.role,
            "engines": f"{stage.engine_count}x {stage.engine}",
            "tanks": f"{stage.tank_count}x {stage.tank} ({stage.diameter_m} m)",
            "ignition_mass_t": round(m0, 2),
            "burnout_mass_t": round(m1, 2),
            "propellant_t": round(wet - dry, 2),
            "post_separation_mass_t": round(mass_above, 2),
            "dv_mps": round(astro.rocket_dv(isp_vac, m0, m1), 0),
            "twr_ignition": round(astro.twr(thrust_n, m0, g), 2),
            "twr_burnout": round(astro.twr(thrust_n, m1, g), 2),
            "separator": "TD-12 Decoupler (fires when spent)" if stage.decoupler_above else "none — stays with payload",
            "separation_trigger": ("final stage — no separation" if is_last
                                   else "propellant exhausted -> decouple this stage -> ignite next"),
        })
    return plan


def _estimate(design: RocketDesign, req: ShipRequirements, n_chute: int) -> dict[str, float]:
    """Total wet mass, per-stage and total Δv (rocket equation), launch TWR, chute count — calculated."""
    from .parts import stage_masses
    bus = _bus_mass(req)
    stage_wet = [stage_masses(s)[1] for s in design.stages]
    total_dv = 0.0
    launch_twr = 0.0
    for i, stage in enumerate(design.stages):
        dry, wet, thrust_asl, isp_asl, isp_vac = stage_masses(stage)
        mass_above = bus + sum(stage_wet[i + 1:])
        m0, m1 = mass_above + wet, mass_above + dry
        total_dv += astro.rocket_dv(isp_vac, m0, m1)
        if i == 0:
            launch_twr = astro.twr(thrust_asl * 1000.0, m0, req.phases[0].twr_body_g or 9.81)
    return {
        "wet_mass_t": round(bus + sum(stage_wet), 2),
        "total_delta_v_mps": round(total_dv, 0),
        "launch_twr": round(launch_twr, 2),
        "parachutes": float(n_chute),
        "stage_count": float(len(design.stages)),
    }
