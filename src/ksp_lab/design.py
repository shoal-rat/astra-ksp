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
    dv_mps: float                 # Δv REQUIREMENT (calculated by plan.py / astro.py from live state)
    twr_body_g: float = 0.0       # surface gravity where TWR matters (launch/landing); 0 = vacuum
    min_twr: float = 0.0          # required TWR at ignition mass (launch ~1.4, powered landing ~2.0)
    min_diameter_m: float = 0.0   # force a wider tank for this stage (2.5 m for a low-CoG lander base)
    # FUEL RESERVE: the stage is sized for dv_mps*(1+reserve_frac) so it carries propellant BEYOND the
    # nominal requirement (a real rocket never plans to burn to depletion). Defaults are the standard
    # per-role margins (ascent fights the most gravity/steering loss); callers override via design_reserve().
    reserve_frac: float = 0.05

    def design_dv(self) -> float:
        """The Δv the stage is actually SIZED for = requirement + reserve."""
        return self.dv_mps * (1.0 + max(0.0, self.reserve_frac))


def default_reserve_frac(twr_body_g: float, is_landing: bool = False) -> float:
    """Standard fuel-reserve fraction by role: ascent/landing burns lose the most to gravity/steering/
    throttle dispersion + must never run dry, vacuum transfers least. +2% unusable residual folded in."""
    if is_landing:
        return 0.10            # 8% suicide-burn margin + 2% residual: the hoverslam must not flame out
    if twr_body_g > 5.0:
        return 0.12            # ascent: 10% gravity/drag/steering + 2% residual
    return 0.07               # vacuum transfer/capture: 5% + 2% residual


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


# Engines are selected by ROLE, not one flat ladder (a vacuum Terrier as a booster gives TWR<1 and the
# rocket won't lift; a low-Isp Mainsail as an upper stage wastes Δv). A stage that fires in the
# ATMOSPHERE (phase.twr_body_g > 5 -> Kerbin/Eve liftoff) draws from the BOOSTER pool (sized on
# SEA-LEVEL thrust); a vacuum/upper/landing stage draws from the VACUUM pool (high Isp, modest thrust).
# Each pool is ordered small -> large so the designer still picks the LIGHTEST that meets TWR + Δv.
BOOSTER_ENGINES = ["liquidEngine", "liquidEngine2", "engineLargeSkipper", "liquidEngineMainsail.v2"]  # Reliant/Swivel/Skipper/Mainsail
VACUUM_ENGINES = ["liquidEngine3.v2", "engineLargeSkipper", "liquidEngineMainsail.v2"]                # Terrier (Isp 345) then thrustier


def engine_pool(phase: "Phase") -> list[str]:
    """The role-correct engine candidates for a phase: BOOSTER (sea-level thrust) for an in-atmosphere
    liftoff stage, VACUUM (high Isp) for a stage that only fires in space."""
    return BOOSTER_ENGINES if phase.twr_body_g > 5.0 else VACUUM_ENGINES
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
    candidates = [_size_one(e, dv, mass_above_t, phase, max_engine_count) for e in engine_pool(phase)]
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
    metrics_rev: list[dict] = []
    # ADD-A-STAGE: split any phase that exceeds the single-stage Δv ceiling into equal-Δv sub-stages
    # BEFORE sizing, so no single stage is asked for more Δv than its engine+tank can physically deliver.
    phases = _split_phases(req.phases)
    log: list[str] = [f"bus(command+crew+heatshield+payload)={bus:.2f}t"]
    if len(phases) != len(req.phases):
        log.append(f"add-a-stage: split {len(req.phases)} requested phases into {len(phases)} stages (Δv ceiling)")
    # Process phases last-firing first so each lower stage carries the wet mass of the ones above it.
    for phase in reversed(phases):
        # Size the stage for the requirement PLUS its fuel reserve, so it carries propellant beyond
        # burn-to-depletion (a real rocket never plans to land on empty tanks).
        spec, metrics = _size_stage(phase.design_dv(), mass_above, phase, req.max_engine_count)
        stages_rev.append(spec)
        metrics_rev.append(metrics)
        mass_above += metrics["wet_t"] + part("Decoupler.1").wet_mass_t
        log.append(
            f"{phase.name}: need {phase.dv_mps:.0f}m/s (+{phase.reserve_frac*100:.0f}% reserve -> size {phase.design_dv():.0f}) "
            f"twr>={phase.min_twr} -> {metrics['engine_count']}x {metrics['engine']} + {metrics['tanks']} {metrics['tank']} "
            f"= {metrics['stage_dv']:.0f}m/s, twr {metrics['twr']}, m0 {metrics['m0_t']}t"
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

    # FEASIBILITY GATE — REJECT (do not silently ship) a rocket that cannot fly. This is the fix for the
    # pad-hang / fall-back failures: the physics was always computed; it just was never enforced.
    metrics = list(reversed(metrics_rev))
    reasons: list[str] = []
    bad_stages = [design.stages[i].role for i, m in enumerate(metrics) if not m.get("ok", True)]
    if bad_stages:
        reasons.append(f"stage(s) {bad_stages} cannot meet their Δv+TWR within the engine/cluster cap "
                       f"(under-thrust or under-tanked)")
    lt = est["launch_twr"]
    if lt < 1.2:
        reasons.append(f"liftoff TWR {lt} < 1.2 — under-thrust: the rocket hangs / falls back (add engines"
                       f" via max_engine_count, a thrustier booster engine, or cut mass)")
    elif lt > 2.4:
        reasons.append(f"WARN liftoff TWR {lt} > 2.4 — over-thrust: wasted engine mass + drag/flip risk")
    required_dv = sum(p.dv_mps for p in req.phases)
    # RESERVE FLOOR (not a short-tolerance): the design must carry at least 5% Δv beyond the bare
    # requirement, so it never plans to burn to depletion.
    if est["total_delta_v_mps"] < required_dv * 1.05:
        reasons.append(f"total Δv {est['total_delta_v_mps']} < required+reserve {required_dv*1.05:.0f} m/s "
                       f"(req {required_dv:.0f} + 5% floor) — no fuel reserve")
    # UNIFORM DIAMETER (aerodynamics): the stack fires bottom-up (stages[0] = booster at the base), so
    # diameters should be NON-INCREASING upward — the booster is the widest. A stage wider than the one
    # below it leaves an exposed flat shoulder (drag + a node an adapter should smooth).
    for i in range(1, len(design.stages)):
        if design.stages[i].diameter_m > design.stages[i - 1].diameter_m + 1e-6:
            reasons.append(f"WARN non-uniform diameter: {design.stages[i].role} {design.stages[i].diameter_m}m sits "
                           f"above the narrower {design.stages[i-1].role} {design.stages[i-1].diameter_m}m — insert an adapter")
    hard = [r for r in reasons if not r.startswith("WARN")]
    design.feasible = len(hard) == 0
    design.infeasible_reasons = reasons
    if reasons:
        design.notes += ("\n\nFEASIBILITY: " + ("PASS (warnings)" if design.feasible else "FAIL — DO NOT LAUNCH")
                         + ":\n  " + "\n  ".join(reasons))
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
    # design.stages were built from the add-a-stage-SPLIT phases, so align to those (not req.phases,
    # which may be fewer after a split).
    phases = _split_phases(req.phases)
    plan: list[dict] = []
    for i, stage in enumerate(design.stages):
        dry, wet, thrust_asl, _isp_asl, isp_vac = stage_masses(stage)
        mass_above = bus + sum(stage_wet[i + 1:])          # flies on after this stage's separator fires
        m0, m1 = mass_above + wet, mass_above + dry
        phase = phases[i] if i < len(phases) else phases[-1]
        g = phase.twr_body_g or 9.81
        # ASL thrust for an in-atmosphere stage (booster), vacuum thrust otherwise — matches _size_one.
        thrust_n = (thrust_asl if phase.twr_body_g > 5.0 else
                    part(stage.engine).thrust_kn_vac * max(1, stage.engine_count)) * 1000.0
        is_last = i == len(design.stages) - 1
        # Structural coefficient eps = inert/(inert+propellant) = stage_dry/stage_wet (the stage's OWN
        # masses), and the single-stage Δv CEILING = Isp*g0*ln(1/eps) = the most Δv this stage's engine+
        # tank can ever deliver (at infinite tank count). A phase needing more than this MUST be split
        # into more stages (the add-a-stage trigger; see _split_phases).
        eps = dry / wet if wet > 0 else 1.0
        dv_ceiling = isp_vac * astro.G0 * math.log(1.0 / eps) if 0.0 < eps < 1.0 else 0.0
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
            "struct_coeff_eps": round(eps, 3),
            "single_stage_dv_ceiling_mps": round(dv_ceiling, 0),
            "separator": "TD-12 Decoupler (fires when spent)" if stage.decoupler_above else "none — stays with payload",
            "separation_trigger": ("final stage — no separation" if is_last
                                   else "propellant exhausted -> decouple this stage -> ignite next"),
        })
    return plan


def _single_stage_ceiling(phase: Phase) -> float:
    """The most Δv a SINGLE stage can deliver for this phase's role: max over the engine pool of
    Isp_vac*g0*ln(tank_wet/tank_dry) — the rocket-equation limit at infinite tank count for that engine's
    paired tank. A phase needing more than this cannot close on one stage at ANY tank count."""
    best = 0.0
    for e in engine_pool(phase):
        tk = part(TANK_FOR_ENGINE[e])
        eng = part(e)
        if tk.dry_mass_t > 0 and tk.wet_mass_t > tk.dry_mass_t:
            best = max(best, eng.isp_vac_s * astro.G0 * math.log(tk.wet_mass_t / tk.dry_mass_t))
    return best


def _split_phases(phases: list[Phase]) -> list[Phase]:
    """ADD-A-STAGE optimization: split any phase whose (reserved) Δv exceeds the single-stage ceiling
    into N equal-Δv sub-phases — the restricted-staging optimum (equal Ve, equal eps => equal Δv per
    stage). Each sub-phase becomes its own stage with its own separator. The FIRST-firing sub-stage
    (bottom) keeps the role's TWR floor; the upper sub-stages fly in vacuum. Most missions don't trigger
    this (LKO ~3.4 km/s << a ~6.7 km/s ceiling); it catches the big legs (a Duna round-trip lander)."""
    out: list[Phase] = []
    for ph in phases:
        ceil = _single_stage_ceiling(ph)
        need = ph.design_dv()
        if ceil > 0 and need > ceil * 0.95:
            n = max(2, math.ceil(need / (ceil * 0.9)))
            for k in range(n):                       # k=0 fires first (bottom) -> keeps the TWR floor
                out.append(Phase(f"{ph.name}{k+1}", ph.dv_mps / n,
                                 twr_body_g=ph.twr_body_g if k == 0 else 0.0,
                                 min_twr=ph.min_twr if k == 0 else 0.0,
                                 min_diameter_m=ph.min_diameter_m, reserve_frac=ph.reserve_frac))
        else:
            out.append(ph)
    return out


def separation_sequence(design: RocketDesign, req: ShipRequirements) -> list[str]:
    """The ordered ascent / SEPARATION / deploy EVENTS with the control logic that triggers each — the
    'establish the separation sequence' deliverable. Separator PLACEMENT is by craft_writer: each
    inter-stage TD-12 decoupler is given KSP inverse-stage = render_index-1 so it fires AFTER its stage
    is spent (never at liftoff); the controller fires the next event on a MEASURED condition (booster
    propellant exhausted -> the consecutive-dry guard -> decouple + ignite the next engine). Payload
    solar/antennas deploy ONLY after orbit + fairing jettison, never exposed at launch."""
    plan = staging_plan(design, req)
    ev: list[str] = ["T0 LIFTOFF: ignite stage 1 (booster) at full thrust; payload STOWED in the fairing"]
    for p in plan:
        if str(p["separator"]).startswith("TD-12"):
            ev.append(f"MECO stage {p['stage']}: propellant exhausted (consecutive-dry guard, 3 polls) -> FIRE "
                      f"its TD-12 separator -> drop {p['burnout_mass_t']} t -> ignite stage {p['stage']+1}")
    ev.append("FAIRING JETTISON: only when altitude > 70 km (above the atmosphere) AND the upper stage is burning")
    ev.append("SECO / ORBIT INSERTION: circularise at the parking orbit")
    ev.append("PAYLOAD: detumble (reaction wheels) -> DEPLOY solar panels -> DEPLOY + point antenna -> commission as Relay")
    return ev


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
