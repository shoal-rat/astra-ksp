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
from .models import RadialBoosterSpec, RocketDesign, StageSpec
from .parts import engines as catalog_engines
from .parts import part, payload_bus_mass
from .parts import tanks as catalog_tanks


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


def _is_vacuum_transfer_phase(phase: Phase) -> bool:
    """A true vacuum transfer/capture leg — the flexible stage that carries the MISSION-LEVEL reserve.
    It fires only in space (no ascent gravity to fight: twr_body_g <= 5) and is NOT a powered landing
    (a hoverslam has a high min_twr and is sized to the surface, so it must not absorb the contingency)."""
    return phase.twr_body_g <= 5.0 and phase.min_twr <= 0.0


def _mission_reserve_phase_name(phases: list[Phase]) -> str | None:
    """Pick the single phase that carries the whole-mission contingency reserve: the FIRST vacuum
    transfer/capture leg (re-taskable propellant). Falls back to the last phase if none is a clean
    vacuum leg, so the reserve is always banked SOMEWHERE on a real (non-empty) vehicle."""
    if not phases:
        return None
    for ph in phases:
        if _is_vacuum_transfer_phase(ph):
            return ph.name
    return phases[-1].name


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
    # MISSION-LEVEL FUEL RESERVE — margin "for unforeseen needs" on TOP of the per-stage role reserves.
    # Each Phase already carries its own role margin (ascent ~12%, vacuum ~7%, landing ~10%); this is an
    # ADDITIONAL whole-mission contingency the vehicle hauls so it can absorb the unplanned: a sloppier
    # gravity turn, a steeper-than-nominal capture, a missed node re-plan, a longer hover before touchdown.
    # It is folded into the VACUUM transfer/capture stage's sizing (the most flexible leg — that propellant
    # can be re-tasked to whichever burn actually needs it), so the finished vehicle leaves the pad with
    # this slice of Δv banked. Default 5%. Set 0.0 to size with per-stage reserves only.
    mission_reserve_frac: float = 0.05
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
    # RADIAL BOOSTERS: number of symmetric strap-on booster pods to add to the LAUNCH stage (0 = a
    # single-core rocket). Each pod is its own tank+engine on a radial decoupler that fires at T0 and
    # jettisons when spent — the asparagus / Soyuz / Falcon-Heavy pattern. Use this when the launch
    # stage is too heavy to lift on the core alone (a heavy interplanetary upper makes a ~200 t rocket
    # that hangs at low TWR on one core). The pods are SIZED from physics (rocket equation + combined
    # TWR), never guessed; they carry a portion of the ascent Δv so the core flies on lighter after drop.
    radial_booster_count: int = 0


# --------------------------------------------------------------------------------------------------
# Diameter-laddered, cluster-fit-aware stage sizing.
#
# A real rocket keeps a consistent (non-increasing-upward) diameter and never hangs engines outside
# the tank. The old sizer minimised wet mass with a fixed 1-tank-per-engine map, which always picked
# the finest-granularity 1.25 m tank and stacked a dozen of them into a needle, then clustered engines
# in a ring WIDER than the tank. The new sizer instead chooses, per stage, the NARROWEST standard
# diameter that (a) is >= the diameter of every stage above it (monotonic taper, base widest),
# (b) holds the stage's propellant without exceeding a per-stage fineness budget (no needles), and
# (c) can mount enough engines WITHIN the tank radius to meet the TWR. An engine wider than its tank,
# or a cluster that overflows the tank, is rejected outright — never drawn hanging in mid-air.
# --------------------------------------------------------------------------------------------------
DIAMETERS = (1.25, 2.5, 3.75)                  # standard stack diameters, narrow -> wide

# --------------------------------------------------------------------------------------------------
# FULL-CATALOG engine/tank pools — NO curated tier. The sizer's engine and tank candidates are drawn
# DIRECTLY from the whole materialized stock catalog (parts.engines / parts.tanks), every part on equal
# footing. There is no hand-picked short list searched first: a stage is sized by physics over ALL stock
# chemical engines and ALL stock LFO cylinder tanks in the standard stack diameters at once. The catalog
# is verified accurate by tools/verify_parts.py, which is what makes "no curation" safe.
#
# We restrict the pools to the three standard stack diameters the geometry gate + craft_writer understand
# (0.625 m and 5 m stock parts EXIST in the catalog but the renderer is not built for them yet), and to
# CHEMICAL rocket engines only — jets, ion, and monoprop thrusters are tagged into separate buckets by
# parts.engines() so they never size a chemical stage. Rankings: BOOSTER engines by sea-level thrust
# (ascending, so the search walks light->heavy), VACUUM engines by vacuum Isp; tanks by propellant mass
# (largest first). The build queries the catalog once at import; the sizer then walks these ordered lists.
# --------------------------------------------------------------------------------------------------


def _build_tanks_by_diameter() -> dict[float, list[str]]:
    """The candidate LFO stack tanks per standard diameter, largest propellant first, FROM THE WHOLE
    materialized catalog (every stock LFO cylinder in that diameter class — no curated subset)."""
    return {dia: [p.name for p in catalog_tanks(diameter_m=dia, propellant="lfo")] for dia in DIAMETERS}


# A real sea-level BOOSTER keeps most of its thrust at the surface — its ASL/vac thrust ratio is high
# (Reliant 0.85, Mainsail 0.92, Vector 0.94). A VACUUM engine (Terrier 0.25, Nerv 0.23, Poodle 0.26)
# collapses to a fraction of its thrust in atmosphere, so it is the WRONG part to lift a rocket off the
# pad even though it technically produces some sea-level thrust. Gate the booster pool on this ratio so
# the atmospheric role draws only genuine pad engines — physics, not a hand-picked list.
MIN_BOOSTER_ASL_RATIO = 0.6


def _build_engine_pool(atmospheric: bool) -> list[str]:
    """Every stock chemical rocket engine in the standard stack diameters, ranked for the role (sea-level
    thrust ascending for boosters, vacuum Isp for upper stages). Drawn straight from the full catalog.

    Two physics filters keep the pool sane without curating it:
      * STANDARD STACK DIAMETER (>= 1.25 m): 0.625 m micro-engines (Ant/Spark/Cub) exist in the catalog
        and pass the ``<= dia`` filter, but craft_writer has no 0.625 m stack geometry, so a 0.625 m
        engine under a 1.25 m+ tank reads as a needle/overhang. Held back until the renderer supports them.
      * BOOSTER ROLE -> good sea-level thrust ratio: an atmospheric (liftoff) stage must use an engine
        whose ASL thrust is a real fraction of its vacuum thrust (>= MIN_BOOSTER_ASL_RATIO). This drops
        vacuum engines (Terrier/Nerv/Poodle/Wolfhound) from the BOOSTER pool — they still appear in the
        VACUUM pool, where their high Isp is exactly what an upper stage wants."""
    names: list[str] = []
    seen: set[str] = set()
    for dia in DIAMETERS:
        for p in catalog_engines(diameter_m=dia, atmospheric=atmospheric):
            if p.diameter_m < DIAMETERS[0] - 1e-6:      # sub-1.25 m micro-engine: renderer not built for it
                continue
            if atmospheric and p.thrust_kn_vac > 0 and (p.thrust_kn_asl / p.thrust_kn_vac) < MIN_BOOSTER_ASL_RATIO:
                continue                                # vacuum engine: wrong part to lift off the pad
            if p.name not in seen:
                seen.add(p.name)
                names.append(p.name)
    return names


# The full-catalog pools the sizer searches — ONE tier, all stock parts equal. ``BOOSTER_ENGINES`` is
# every sea-level engine (ranked by ASL thrust), ``VACUUM_ENGINES`` every engine ranked by vacuum Isp,
# and ``TANKS_BY_DIAMETER`` every stock LFO cylinder per diameter. The historic ``*_FULL`` aliases are
# kept pointing at the same lists so any external importer that referenced them still works (there is no
# longer a separate "full" tier — the base pool already IS the whole catalog).
TANKS_BY_DIAMETER = _build_tanks_by_diameter()
BOOSTER_ENGINES = _build_engine_pool(atmospheric=True)
VACUUM_ENGINES = _build_engine_pool(atmospheric=False)
TANKS_BY_DIAMETER_FULL = TANKS_BY_DIAMETER
BOOSTER_ENGINES_FULL = BOOSTER_ENGINES
VACUUM_ENGINES_FULL = VACUUM_ENGINES

# A stage taller than this many calibers (stack height / diameter) is a "needle" — widen it. Real
# stages run ~3-6 calibers. The whole-vehicle L/D is gated separately (design-chart geometry gate).
PER_STAGE_FINENESS = 6.0


def engine_pool(phase: "Phase", full: bool = False) -> list[str]:
    """The role-correct engine candidates from the FULL catalog: BOOSTER (sea-level thrust) for an
    in-atmosphere liftoff stage, VACUUM (high Isp) for a stage that only fires in space. ``full`` is
    retained for API compatibility but is a no-op now — the pool is always the whole stock catalog."""
    return BOOSTER_ENGINES if phase.twr_body_g > 5.0 else VACUUM_ENGINES


def _unit_tank_for_engine(eng_name: str) -> str:
    """The largest stock tank matching an engine's own diameter — used for the single-stage Δv ceiling.
    Falls to the next standard diameter that HAS tanks (a 1.25 m engine maps to the 1.25 m tanks; an
    engine whose own diameter class has no LFO cylinder, e.g. a 0.625 m engine, borrows the 1.25 m pool)."""
    dia = part(eng_name).diameter_m
    pool = TANKS_BY_DIAMETER.get(dia) or []
    if not pool:
        for d in DIAMETERS:                            # nearest standard diameter with a tank
            if TANKS_BY_DIAMETER.get(d):
                pool = TANKS_BY_DIAMETER[d]
                break
    return pool[0]


# An engine BELL is far narrower than its stack-mounting node (a 2.5 m Mainsail's bell is ~1.5 m), so
# real rockets pack several bells onto a mounting PLATE at the base (Saturn-V F-1 cluster, Falcon-9
# octaweb). We model the bell radius as a fraction of the mounting diameter and let a cluster spread
# onto a plate up to PLATE_FACTOR x the tank radius — wide enough for heavy-lift thrust, bounded so it
# still reads as one engine section (never the old ring hung far off the side).
ENGINE_BELL_FRAC = 0.36
PLATE_FACTOR = 1.5


def engine_bell_radius(eng_name: str) -> float:
    return part(eng_name).diameter_m * ENGINE_BELL_FRAC


def cluster_ring_radius(n_ring: int, r_bell: float) -> float:
    """Tightest radius for ``n_ring`` engine bells ringed around a central bell with no overlaps:
    each ring bell must clear the central one (>= 2*r_bell) and its neighbours (>= r_bell/sin(pi/n))."""
    if n_ring <= 0:
        return 0.0
    adjacent = r_bell / math.sin(math.pi / n_ring) if n_ring >= 2 else 0.0
    return max(2.0 * r_bell, adjacent)


def max_cluster_in_tank(r_bell: float, r_tank: float) -> int:
    """How many engine bells of radius ``r_bell`` fit as one central + a symmetric ring on a mounting
    plate of radius ``PLATE_FACTOR*r_tank``. Returns 0 if even a single bell is wider than the tank."""
    if r_bell > r_tank + 1e-9:
        return 0
    plate_r = r_tank * PLATE_FACTOR
    best = 1
    for n_ring in range(1, 13):
        if cluster_ring_radius(n_ring, r_bell) + r_bell <= plate_r + 1e-9:
            best = 1 + n_ring
        else:
            break
    return best


def _bus_mass(req: ShipRequirements) -> float:
    """Mass of the command bus + payload + the calculated recovery hardware (heat shield + chutes)."""
    base = payload_bus_mass(req.payload_t, req.crew > 0)  # command + 1 chute + (heatshield if crewed)
    # payload_bus_mass already bundles one chute + a heatshield-if-crewed; we override chutes below by
    # CALCULATING the count, and add crew-cabin mass for extra seats and docking hardware here.
    extra = 0.0
    if req.crew > 1:
        # Extra seats for crew beyond the command pod's one. Use the KV-2 'Onion' reentry module (kv2Pod):
        # a genuine 1.25 m, 2-seat pod, so the crew section stays a clean 1.25 m column. (The Mk2 Command
        # Pod "Mk2Pod" is a 1.875 m Making-History part — its true cfg diameter would break the bus's
        # monotonic taper; the cfg-named "crewCabin" is the 2.5 m Hitchhiker, also wrong here.) kv2Pod
        # seats 2, matching the old ~2-per-cabin assumption.
        extra += part("kv2Pod").wet_mass_t * math.ceil((req.crew - 1) / 2.0)
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
    # Cap at the 9999 sentinel: when the mass-above is so large that the closed form asks for an
    # astronomical count (a diverging cascade, e.g. a mis-specified moon-as-planet ejection Δv), the
    # stage is hopelessly infeasible — the `ok` gate rejects anything >= 9000, and the cap keeps the
    # number finite so the renderer never tries to build 1e28 tank nodes and hang.
    return min(9999, max(1, math.ceil(M * (R - 1.0) / denom)))


def _size_one(eng_name: str, tank_name: str, dv: float, mass_above_t: float, phase: Phase,
              max_engine_count: int = 8) -> dict | None:
    """Solve (engine_count, tank_count) for one engine+tank pairing: closed-form tank count for `dv`,
    plus a clustering fixed-point so thrust meets min_twr at the ignition mass — capped both at
    `max_engine_count` AND at how many engines geometrically FIT under the tank (so a cluster never
    overflows the hull). Returns None when the engine is wider than the tank."""
    eng = part(eng_name)
    tank = part(tank_name)
    fit_cap = max_cluster_in_tank(engine_bell_radius(eng_name), tank.diameter_m / 2.0)
    if fit_cap < 1:
        return None                                # engine bell wider than the tank -> not a candidate
    cap = min(max_engine_count, fit_cap)
    thrust_one = (eng.thrust_kn_asl if phase.twr_body_g > 5.0 and eng.thrust_kn_asl > 0 else eng.thrust_kn_vac) * 1000.0
    n_eng, tanks = 1, 1
    for _ in range(8):
        tanks = _tank_count_for_dv(dv, mass_above_t, (eng.dry_mass_t, eng.wet_mass_t), n_eng,
                                   tank.dry_mass_t, tank.wet_mass_t, eng.isp_vac_s)
        m0 = mass_above_t + eng.wet_mass_t * n_eng + tank.wet_mass_t * tanks
        if phase.min_twr > 0 and phase.twr_body_g > 0 and thrust_one > 0:
            need = min(cap, math.ceil(phase.min_twr * m0 * 1000.0 * phase.twr_body_g / thrust_one))
            if need > n_eng:
                n_eng = need
                continue
        break
    dry = eng.dry_mass_t * n_eng + tank.dry_mass_t * tanks
    wet = eng.wet_mass_t * n_eng + tank.wet_mass_t * tanks
    m0 = mass_above_t + wet
    achieved_twr = astro.twr(thrust_one * n_eng, m0, phase.twr_body_g) if phase.twr_body_g > 0 else math.inf
    actual_dv = astro.rocket_dv(eng.isp_vac_s, m0, mass_above_t + dry)
    stage_h = tank.height_m * tanks + eng.height_m
    fineness = stage_h / tank.diameter_m if tank.diameter_m > 0 else 1e9
    twr_ok = phase.min_twr <= 0 or achieved_twr >= phase.min_twr
    return {
        "engine": eng_name, "engine_count": n_eng, "tank": tank_name, "tanks": tanks,
        "diameter_m": tank.diameter_m, "fit_cap": fit_cap,
        "twr": round(achieved_twr, 2), "stage_dv": round(actual_dv, 0), "m0_t": round(m0, 2),
        "wet_t": round(wet, 2), "parts": n_eng + tanks, "fineness": round(fineness, 2),
        "twr_ok": twr_ok,
        # dv_ok = the stage is properly TANKED (reaches its Δv at a finite tank count), independent of TWR.
        # The asparagus launch core is allowed to hang at low solo TWR (boosters lift it), but must still be
        # dv_ok; every other stage must be fully `ok` (dv AND solo TWR).
        "dv_ok": tanks < 9000 and actual_dv >= dv * 0.995,
        "ok": tanks < 9000 and actual_dv >= dv * 0.995 and twr_ok,
    }


def _search_pool(eng_names: list[str], tank_map: dict[float, list[str]], diam_list: list[float],
                 dv: float, mass_above_t: float, phase: Phase,
                 max_engine_count: int) -> tuple[dict | None, dict[float, dict], list[dict]]:
    """Search one (engine pool, tank map) tier across the candidate diameters. Returns (chosen-or-None,
    per-diameter best feasible build, all candidates). ``chosen`` is the narrowest-diameter non-needle
    feasible build, or None when no feasible non-needle build exists in this tier."""
    per_dia: dict[float, dict] = {}
    any_cand: list[dict] = []
    for dia in diam_list:
        cands = []
        for tank_name in tank_map[dia]:
            for eng_name in eng_names:
                if part(eng_name).diameter_m > dia + 1e-6:
                    continue                       # engine wider than this tank
                m = _size_one(eng_name, tank_name, dv, mass_above_t, phase, max_engine_count)
                if m is not None:
                    cands.append(m)
        any_cand.extend(cands)
        feas = [m for m in cands if m["ok"]]
        if feas:
            per_dia[dia] = min(feas, key=lambda m: m["wet_t"])
    chosen = None
    for dia in diam_list:                          # narrowest diameter with a non-needle feasible build
        m = per_dia.get(dia)
        if m and m["fineness"] <= PER_STAGE_FINENESS:
            chosen = m
            break
    return chosen, per_dia, any_cand


def _size_stage(dv: float, mass_above_t: float, phase: Phase, max_engine_count: int = 8,
                dia_floor: float = 0.0, use_full_catalog: bool = False) -> tuple[StageSpec, dict]:
    """Pick the stage from the WHOLE stock catalog — no curated tier. Search every (tank, engine) pairing
    at each standard diameter >= the floor (the widest stage above, so taper stays monotonic), each with a
    closed-form tank count + cluster-fit, and choose the NARROWEST diameter whose lightest feasible build
    is NOT a needle (fineness <= PER_STAGE_FINENESS). The fineness budget kills the 1.25 m noodle;
    cluster-fit kills the engine overhang; the floor guarantees the base is the widest.

    Selection is pure physics over the full roster: the booster role draws every stock sea-level engine
    (ranked by ASL thrust) on every stock LFO cylinder tank, the vacuum role every engine ranked by Isp;
    the lightest feasible non-needle build at the narrowest diameter wins. ``use_full_catalog`` is kept
    for API compatibility but is now a no-op — there is only ONE tier, and it is the whole catalog."""
    floor = max(dia_floor, phase.min_diameter_m)
    diam_list = [d for d in DIAMETERS if d >= floor - 1e-6] or [DIAMETERS[-1]]

    chosen, per_dia, any_cand = _search_pool(engine_pool(phase), TANKS_BY_DIAMETER,
                                             diam_list, dv, mass_above_t, phase, max_engine_count)

    if chosen is None and per_dia:                 # all feasible builds are needles -> widest (least slender)
        chosen = per_dia[max(per_dia)]
    if chosen is None:                             # nothing feasible at any diameter -> least-bad (gate fails it)
        pool = any_cand or [{
            "engine": engine_pool(phase)[-1], "engine_count": 1,
            "tank": TANKS_BY_DIAMETER[diam_list[-1]][0], "tanks": 9999, "diameter_m": diam_list[-1],
            "fit_cap": 1, "twr": 0.0, "stage_dv": 0.0, "m0_t": 0.0, "wet_t": 9e9, "parts": 0,
            "fineness": 9e9, "twr_ok": False, "ok": False}]
        dv_ok = [m for m in pool if m["stage_dv"] >= dv * 0.995]
        chosen = max(dv_ok or pool, key=lambda m: m["twr"])
    spec = StageSpec(phase.name, chosen["engine"], chosen["tank"], chosen["tanks"],
                     decoupler_above=True, engine_count=chosen["engine_count"],
                     diameter_m=chosen.get("diameter_m", 1.25))
    return spec, chosen


# --------------------------------------------------------------------------------------------------
# RADIAL (strap-on) BOOSTERS — the asparagus / Soyuz / Falcon-Heavy ascent.
#
# When the launch stage is too heavy to lift on its core alone (a heavy interplanetary upper makes a
# ~200 t rocket that either has too-low liftoff TWR or hangs at circularization), bolt N symmetric tank+
# engine pods to the SIDE of the core on radial decouplers. They ignite WITH the core at T0, so the
# combined liftoff thrust = core + N*pod; they burn their own propellant in parallel (adding a chunk of
# the ascent Δv); then they jettison together when spent, so the core flies on WITHOUT their dead mass.
#
# Sizing is closed-form physics, never guessed:
#   * pod engine: one sea-level engine per pod, chosen as the lightest booster-pool engine that — across
#     N pods plus the core — clears the combined liftoff TWR floor at the full wet stack.
#   * pod tanks: the whole-tank count whose N-pod propellant delivers ~`dv_share` of the launch-phase Δv
#     at liftoff (rocket equation over the full liftoff mass — a conservative parallel-burn estimate; the
#     real parallel burn does at least this well because the core also thrusts).
#   * the radial decoupler mass is folded into each pod's dead mass.
# Returns (RadialBoosterSpec, metrics) or (None, {}) if no booster engine can close the TWR.
# --------------------------------------------------------------------------------------------------
BOOSTER_DV_SHARE = 0.45            # fraction of the launch-phase Δv the strap-ons carry before drop


def _launch_core_stack_height(core_metrics: dict) -> float:
    """Height of the LAUNCH (first-firing) core stage's tank+engine stack — the Falcon-Heavy reference
    ceiling for a strap-on pod. A radial booster must reach AT MOST the top of this stack and NO higher,
    so the core's upper stage + payload stay clear above the pods (no hammerhead/cage). Computed from the
    core's chosen tank height * count + its engine height (the same stack the renderer draws)."""
    return part(core_metrics["tank"]).height_m * core_metrics["tanks"] + part(core_metrics["engine"]).height_m


def _size_radial_boosters(phase: Phase, core_metrics: dict, mass_above_t: float, count: int,
                          max_engine_count: int = 8, dv_share: float = BOOSTER_DV_SHARE,
                          target_twr: float = 0.0, use_full_catalog: bool = False
                          ) -> tuple[RadialBoosterSpec | None, dict]:
    """Size `count` symmetric strap-on pods so (core + boosters) liftoff TWR >= the floor AND the pods
    carry ~`dv_share` of the launch-phase Δv. All counts from the rocket equation + TWR; see the block
    comment above for the model. `core_metrics` is the launch stage's own `_size_one` dict.

    HEIGHT CLAMP (Falcon-Heavy rule): a pod's total stack height (engine + tank column) must NOT exceed
    the launch-stage CORE stack height — the side boosters reach ~the top of the first (booster) stage and
    no higher, leaving the core's upper stage + payload clear ABOVE them. So the dv-share sizes the pod,
    but the tank count is then CLAMPED to whatever fits under that height ceiling. If the clamped pods
    cannot deliver the full share, the core covers the rest (it was sized for 1-share but always carries
    its own per-role reserve); the metrics report the ACTUAL clamped booster_dv so the estimate/feasibility
    gate see the real (possibly reduced) contribution and a caller can add MORE pods rather than taller ones."""
    if count <= 0:
        return None, {}
    core_wet = core_metrics["wet_t"]
    core = part(core_metrics["engine"])
    core_thrust_n = (core.thrust_kn_asl * core_metrics["engine_count"]) * 1000.0
    g = phase.twr_body_g or astro.G0
    core_stack_h = _launch_core_stack_height(core_metrics)
    # The COMBINED (core + strap-ons) liftoff must clear a real launch floor. The launch-phase min_twr can
    # be a low CORE figure (the core alone is allowed to hang at ~1.3 because the strap-ons do the lifting);
    # the assembled rocket still needs >= 1.4 at the pad. Size the pods to the larger of the two so the
    # finished stack reliably lifts (the feasibility gate + estimate then read >= 1.4).
    twr_floor = target_twr or max(phase.min_twr or 0.0, 1.4)
    dv_target = phase.design_dv() * max(0.0, dv_share)
    dec = part("radialDecoupler2")

    def _height_cap_tanks(eng, tank) -> int:
        """Most whole pod tanks that keep the pod (engine + tank column) at/under the core stack height."""
        avail = core_stack_h - eng.height_m
        return max(1, int(avail // tank.height_m)) if tank.height_m > 0 else 1

    # Evaluate ONE booster engine: pair it with the widest tank no wider than the engine's own diameter
    # (a booster pod is a clean single stack), solve the tank count for dv_target, check the combined
    # liftoff TWR, and return a candidate dict (or None if it cannot lift the stack even strapped on).
    def _eval_pod_engine(eng_name: str) -> dict | None:
        eng = part(eng_name)
        thrust_one = (eng.thrust_kn_asl if eng.thrust_kn_asl > 0 else eng.thrust_kn_vac) * 1000.0
        if thrust_one <= 0:
            return None
        tank_name = _unit_tank_for_engine(eng_name)
        tank = part(tank_name)
        height_cap = _height_cap_tanks(eng, tank)
        # Solve the whole-tank count so N pods deliver dv_target at liftoff against the FULL wet stack
        # (mass_above + core + all N pods) — a conservative parallel-burn estimate. Fixed-point iterate.
        tanks = 1
        for _ in range(12):
            A0 = mass_above_t + core_wet + count * (eng.wet_mass_t + dec.wet_mass_t)
            A1 = mass_above_t + core_wet + count * (eng.dry_mass_t + dec.dry_mass_t)
            ve = eng.isp_asl_s * astro.G0 if eng.isp_asl_s > 0 else eng.isp_vac_s * astro.G0
            R = math.exp(dv_target / ve) if ve > 0 else 1e9
            denom = count * (tank.wet_mass_t - R * tank.dry_mass_t)
            need = math.ceil((R * A1 - A0) / denom) if denom > 0 else tanks
            need = max(1, min(40, need))
            if need == tanks:
                break
            tanks = need
        # FALCON-HEAVY HEIGHT CLAMP: a pod may not be taller than the launch core stack. Clamp the
        # tank column to the height cap (add more PODS or thrust for more Δv, never a taller pod).
        clamped = tanks > height_cap
        tanks = min(tanks, height_cap)
        pod_dry = eng.dry_mass_t + tank.dry_mass_t * tanks + dec.dry_mass_t
        pod_wet = eng.wet_mass_t + tank.wet_mass_t * tanks + dec.wet_mass_t
        m0 = mass_above_t + core_wet + count * pod_wet
        combined_thrust_n = core_thrust_n + count * thrust_one
        combined_twr = astro.twr(combined_thrust_n, m0, g)
        if combined_twr < twr_floor - 1e-6:
            return None                                 # this engine can't lift the stack even strapped on
        # Δv the boosters actually deliver (rocket equation, booster propellant vs the full liftoff mass).
        m1 = m0 - count * (pod_wet - pod_dry)
        booster_dv = astro.rocket_dv(eng.isp_asl_s or eng.isp_vac_s, m0, m1)
        pod_height = eng.height_m + tank.height_m * tanks
        return {
            "engine": eng_name, "engine_count": 1, "tank": tank_name, "tanks": tanks,
            "count": count, "diameter_m": tank.diameter_m, "pod_wet_t": round(pod_wet, 2),
            "pod_dry_t": round(pod_dry, 2), "combined_twr": round(combined_twr, 2),
            "booster_dv": round(booster_dv, 0), "liftoff_mass_t": round(m0, 2),
            "total_wet_t": round(count * pod_wet, 2),
            "pod_height_m": round(pod_height, 2), "core_stack_h_m": round(core_stack_h, 2),
            "height_clamped": clamped, "height_cap_tanks": height_cap,
        }

    # Search the WHOLE booster-engine catalog (every stock sea-level engine, no curated tier) and keep the
    # LIGHTEST feasible pod that clears the combined liftoff TWR — physics over the full roster, so a heavy
    # core can reach a Mammoth/Twin-Boar/Vector strap-on exactly when it needs the thrust.
    best: dict | None = None
    for eng_name in BOOSTER_ENGINES:
        cand = _eval_pod_engine(eng_name)
        if cand is not None and (best is None or cand["total_wet_t"] < best["total_wet_t"]):
            best = cand
    if best is None:
        return None, {}
    spec = RadialBoosterSpec(count=count, engine=best["engine"], tank=best["tank"],
                             tank_count=best["tanks"], engine_count=1,
                             decoupler="radialDecoupler2", diameter_m=best["diameter_m"])
    return spec, best


def design_ship(req: ShipRequirements, use_full_catalog: bool = False) -> RocketDesign:
    """Build a RocketDesign whose every part count is calculated from `req`.

    Process: enumerate the bus (command + crew + heat shield + CALCULATED chutes + docking) -> size each
    propulsive stage from the TOP (last-firing) down by inverting the rocket equation for its Δv while
    carrying the mass above it -> verify TWR per stage. Returns the design with an `estimates` block and
    a `design_log` in notes so every number is traceable.

    There is NO curated tier: every stage is sized from the WHOLE materialized stock catalog (every stock
    liquid engine and LFO tank parsed from GameData, on equal footing), so a heavy core naturally reaches a
    Mammoth/Twin-Boar/Vector or a Jumbo-64/Kerbodyne tank when the physics calls for it. ``use_full_catalog``
    is retained for API compatibility but is now a no-op (the full catalog is always used).
    """
    bus = _bus_mass(req)
    # ADD-A-STAGE: split any phase that exceeds the single-stage Δv ceiling into equal-Δv sub-stages
    # BEFORE sizing, so no single stage is asked for more Δv than its engine+tank can physically deliver.
    phases = _split_phases(req.phases)
    log: list[str] = [f"bus(command+crew+heatshield+payload)={bus:.2f}t"]
    if len(phases) != len(req.phases):
        log.append(f"add-a-stage: split {len(req.phases)} requested phases into {len(phases)} stages (Δv ceiling)")
    # Process phases last-firing first (top stage first) so each lower stage carries the wet mass of
    # the ones above it. Track the widest diameter seen so far as a FLOOR for the next (lower) stage —
    # this guarantees a monotonic non-increasing-upward taper (the base is always the widest).
    launch_phase_name = phases[0].name if phases else None
    # MISSION-LEVEL RESERVE: fold the whole-mission contingency (req.mission_reserve_frac, default 5%) into
    # ONE phase — the first vacuum transfer/capture leg (the re-taskable stage). It is applied to a single
    # stage so the contingency is banked exactly once, on top of that stage's own per-role reserve.
    mission_reserve = max(0.0, req.mission_reserve_frac)
    mission_reserve_phase = _mission_reserve_phase_name(phases) if mission_reserve > 0 else None
    if mission_reserve_phase is not None:
        log.append(f"mission reserve: +{mission_reserve*100:.0f}% contingency folded into the '{mission_reserve_phase}' "
                   f"vacuum stage (for unforeseen needs, on top of its per-role reserve)")

    # ASPARAGUS Δv SPLIT (with the Falcon-Heavy height clamp). When strap-on boosters are requested, the
    # CORE launch stage only has to deliver the REMAINDER of the launch-phase Δv — the boosters carry the
    # rest in parallel, then drop. Sizing the core for (1 - share) of the launch Δv is what makes the
    # asparagus rocket lighter than a single core. BUT the pods are now HEIGHT-CLAMPED to the core stack
    # (no tower/cage), so a clamped pod may deliver LESS than the nominal 45% share. We therefore solve a
    # small FIXED POINT: size the core for (1 - share), size the clamped boosters, measure the Δv they
    # ACTUALLY deliver, recompute share = delivered/launch_dv, and resize — converging in 2-3 passes so the
    # core always covers exactly the remainder the (height-limited) boosters cannot. This keeps the physics
    # closing after the clamp: shorter pods => smaller share => a bigger core, never a Δv shortfall.
    def _size_all(core_dv_factor: float):
        nonlocal log
        stages_rev_l: list[StageSpec] = []
        metrics_rev_l: list[dict] = []
        pass_log: list[str] = []
        dia_floor = 1.25                             # the payload bus rides on a 1.25 m core
        m_above = bus
        launch_m_above = bus
        for phase in reversed(phases):
            # Size the stage for the requirement PLUS its fuel reserve, so it carries propellant beyond
            # burn-to-depletion. The first-firing (launch) stage carries only its core share with boosters.
            is_launch = phase.name == launch_phase_name
            carries_mission_reserve = phase.name == mission_reserve_phase
            mission_factor = (1.0 + mission_reserve) if carries_mission_reserve else 1.0
            size_dv = phase.design_dv() * (core_dv_factor if is_launch else 1.0) * mission_factor
            spec, metrics = _size_stage(size_dv, m_above, phase, req.max_engine_count, dia_floor,
                                        use_full_catalog=use_full_catalog)
            dia_floor = max(dia_floor, spec.diameter_m)
            stages_rev_l.append(spec)
            metrics_rev_l.append(metrics)
            launch_m_above = m_above
            m_above += metrics["wet_t"] + part("Decoupler.1").wet_mass_t
            mr_note = f" +{mission_reserve*100:.0f}% mission" if carries_mission_reserve else ""
            pass_log.append(
                f"{phase.name}: need {phase.dv_mps:.0f}m/s (+{phase.reserve_frac*100:.0f}% reserve{mr_note} -> size {size_dv:.0f}) "
                f"twr>={phase.min_twr} -> {metrics['engine_count']}x {metrics['engine']} + {metrics['tanks']} {metrics['tank']} "
                f"= {metrics['stage_dv']:.0f}m/s, twr {metrics['twr']}, m0 {metrics['m0_t']}t"
            )
        rb_l: RadialBoosterSpec | None = None
        bm_l: dict = {}
        if req.radial_booster_count > 0 and phases:
            rb_l, bm_l = _size_radial_boosters(
                phases[0], metrics_rev_l[-1], launch_m_above, req.radial_booster_count,
                req.max_engine_count, use_full_catalog=use_full_catalog)
        return stages_rev_l, metrics_rev_l, pass_log, rb_l, bm_l

    radial_boosters: RadialBoosterSpec | None = None
    booster_metrics: dict = {}
    if req.radial_booster_count > 0 and phases:
        launch_design_dv = phases[0].design_dv()
        # Iterate the asparagus split to convergence. Start from the nominal share, then track the share
        # the height-clamped pods ACTUALLY deliver. Clamp the share to [0, 0.9] so the core never vanishes.
        core_dv_factor = 1.0 - BOOSTER_DV_SHARE
        for _ in range(5):
            stages_rev, metrics_rev, pass_log, radial_boosters, booster_metrics = _size_all(core_dv_factor)
            delivered = float(booster_metrics.get("booster_dv", 0.0)) if radial_boosters is not None else 0.0
            eff_share = min(0.9, max(0.0, delivered / launch_design_dv)) if launch_design_dv > 0 else 0.0
            new_factor = 1.0 - eff_share
            if abs(new_factor - core_dv_factor) <= 0.02:
                core_dv_factor = new_factor
                stages_rev, metrics_rev, pass_log, radial_boosters, booster_metrics = _size_all(core_dv_factor)
                break
            core_dv_factor = new_factor
        log.extend(pass_log)
        if radial_boosters is not None:
            clamp_note = (f", pod height {booster_metrics.get('pod_height_m')}m <= core stack "
                          f"{booster_metrics.get('core_stack_h_m')}m"
                          + (" (CLAMPED to fit)" if booster_metrics.get("height_clamped") else ""))
            log.append(
                f"radial boosters: {radial_boosters.count}x [{radial_boosters.engine_count}x {radial_boosters.engine} "
                f"+ {radial_boosters.tank_count} {radial_boosters.tank}] on {radial_boosters.decoupler} "
                f"-> combined liftoff TWR {booster_metrics['combined_twr']}, +{booster_metrics['booster_dv']:.0f} m/s "
                f"ascent Δv ({(1.0-core_dv_factor)*100:.0f}% of launch Δv; core carries the rest), "
                f"liftoff mass {booster_metrics['liftoff_mass_t']}t (jettison when spent){clamp_note}"
            )
        else:
            log.append(f"radial boosters: requested {req.radial_booster_count} but no booster engine could "
                       f"close the combined liftoff TWR — single core")
    else:
        stages_rev, metrics_rev, pass_log, radial_boosters, booster_metrics = _size_all(1.0)
        log.extend(pass_log)
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
        radial_boosters=radial_boosters,
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
    # ASPARAGUS CORE TWR EXEMPTION: with strap-on boosters, the LAUNCH (first-firing) core is deliberately
    # sized to hang at LOW standalone TWR — the height-clamped pods carry the liftoff thrust in parallel, so
    # the COMBINED liftoff TWR (checked below via est["launch_twr"], which _size_radial_boosters guarantees
    # >= 1.4) is the real launchability test. So for the booster-assisted core, require only that it meets
    # its Δv and is tanked (not its solo TWR); every other stage must still pass its full Δv+TWR `ok`.
    boosters_present = design.radial_boosters is not None and design.radial_boosters.count > 0
    reasons: list[str] = []
    bad_stages = []
    for i, m in enumerate(metrics):
        # The booster-assisted launch core (stages[0]) only needs to be Δv+tanked (dv_ok) — its solo TWR is
        # intentionally low because the strap-ons carry the liftoff thrust (combined TWR checked below). A
        # normal stage needs full `ok` (Δv AND its own TWR).
        is_launch_core = boosters_present and i == 0
        stage_ok = m.get("dv_ok", True) if is_launch_core else m.get("ok", True)
        if not stage_ok:
            bad_stages.append(design.stages[i].role)
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
        tk = part(_unit_tank_for_engine(e))
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


def radial_booster_masses(rb: "RadialBoosterSpec | None") -> tuple[float, float, float]:
    """Aggregate (dry_t, wet_t, asl_thrust_kN) of ALL `count` strap-on pods together, each pod =
    engine_count engines + tank_count tanks + 1 radial decoupler. A DROP-TANK pod (no engine) contributes
    only its tank + decoupler mass and ZERO thrust. Returns (0,0,0) when there are none."""
    if rb is None or rb.count <= 0:
        return 0.0, 0.0, 0.0
    drop = getattr(rb, "is_drop_tank", False)
    eng = None if drop else part(rb.engine)
    tank = part(rb.tank)
    dec = part(rb.decoupler)
    eng_dry = eng.dry_mass_t * rb.engine_count if eng else 0.0
    eng_wet = eng.wet_mass_t * rb.engine_count if eng else 0.0
    pod_dry = eng_dry + tank.dry_mass_t * rb.tank_count + dec.dry_mass_t
    pod_wet = eng_wet + tank.wet_mass_t * rb.tank_count + dec.wet_mass_t
    pod_thrust_asl = (eng.thrust_kn_asl * rb.engine_count) if eng else 0.0
    return rb.count * pod_dry, rb.count * pod_wet, rb.count * pod_thrust_asl


def _estimate(design: RocketDesign, req: ShipRequirements, n_chute: int) -> dict[str, float]:
    """Total wet mass, per-stage and total Δv (rocket equation), launch TWR, chute count — calculated.

    With RADIAL BOOSTERS the launch TWR uses the COMBINED (core + N pods) liftoff thrust at the full wet
    stack (pods + core + everything above), and the total Δv adds the boosters' parallel-burn contribution
    — so a heavy upper that hangs on a single core reads as launchable once the strap-ons are sized in."""
    from .parts import stage_masses
    bus = _bus_mass(req)
    stage_wet = [stage_masses(s)[1] for s in design.stages]
    rb_dry, rb_wet, rb_thrust_asl = radial_booster_masses(design.radial_boosters)
    total_dv = 0.0
    launch_twr = 0.0
    for i, stage in enumerate(design.stages):
        dry, wet, thrust_asl, isp_asl, isp_vac = stage_masses(stage)
        mass_above = bus + sum(stage_wet[i + 1:])
        m0, m1 = mass_above + wet, mass_above + dry
        total_dv += astro.rocket_dv(isp_vac, m0, m1)
        if i == 0:
            g = req.phases[0].twr_body_g or 9.81
            # Combined liftoff: core + booster thrust against the full wet stack INCLUDING the pods.
            m0_liftoff = m0 + rb_wet
            launch_twr = astro.twr((thrust_asl * 1000.0) + (rb_thrust_asl * 1000.0), m0_liftoff, g)
    # Add the strap-ons' own ascent Δv (rocket equation: their propellant vs the full liftoff stack — a
    # conservative parallel-burn estimate, since the core also thrusts during the parallel burn).
    booster_dv = 0.0
    if design.radial_boosters is not None and rb_wet > 0 and not design.radial_boosters.is_drop_tank:
        # A POWERED booster pod adds its own parallel-burn ascent Δv. A DROP-TANK pod has no engine — it
        # crossfeeds into the CORE, so its propellant is already counted in the core stage's Δv (it does
        # not contribute a separate booster_dv term).
        rb = design.radial_boosters
        eng = part(rb.engine)
        m0_liftoff = bus + sum(stage_wet) + rb_wet
        m1_booster = m0_liftoff - (rb_wet - rb_dry)            # boosters burned out, core still full
        booster_dv = astro.rocket_dv(eng.isp_asl_s or eng.isp_vac_s, m0_liftoff, m1_booster)
        total_dv += booster_dv
    # FUEL RESERVE, made VISIBLE: the bare mission REQUIREMENT is sum(phase.dv_mps); everything the vehicle
    # carries beyond that — the per-stage role reserves + the whole-mission contingency — is the reserve.
    # usable_dv = the nominal requirement (what the planned burns consume); reserve_dv = margin in the tanks
    # for unforeseen needs. reserve_frac is reserve as a fraction of the requirement.
    required_dv = sum(p.dv_mps for p in req.phases)
    total_dv_r = round(total_dv, 0)
    usable_dv = round(min(required_dv, total_dv_r), 0)
    reserve_dv = round(max(0.0, total_dv_r - usable_dv), 0)
    return {
        "wet_mass_t": round(bus + sum(stage_wet) + rb_wet, 2),
        "total_delta_v_mps": total_dv_r,
        "required_delta_v_mps": round(required_dv, 0),
        "usable_dv_mps": usable_dv,
        "reserve_dv_mps": reserve_dv,
        "reserve_frac": round(reserve_dv / required_dv, 3) if required_dv > 0 else 0.0,
        "mission_reserve_frac": round(max(0.0, req.mission_reserve_frac), 3),
        "launch_twr": round(launch_twr, 2),
        "booster_delta_v_mps": round(booster_dv, 0),
        "parachutes": float(n_chute),
        "stage_count": float(len(design.stages)),
    }
