"""ASTRA MISSION GRAPH — a rigorous, mathematically-grounded model of a decomposed plan.

The LLM mission-architect (interpreter.py) returns an ORDERED list of primitive steps
``[{"primitive": ..., "args": {...}}, ...]``. Flying that list blindly — or, worse, silently
"trimming" hallucinated parameters — is how a plan that says "land on Duna" before it has
*transferred* to Duna gets flown into the ground. This module turns the flat step list into a
DIRECTED GRAPH of NODES whose edges are the flow of a symbolic WORLD STATE (current body,
situation, crew aboard, cumulative Δv spent), and attaches to every node:

  * a PRECONDITION  — the world state the step REQUIRES to be valid (at-body, in-orbit/landed,
                      has-crew, cumulative Δv so far). e.g. ``land`` requires being in orbit of the
                      body it lands on; ``plant_flag`` requires being landed with crew.
  * a POSTCONDITION — the resulting world state the step produces (the body it leaves you at, the
                      situation, the crew, the new cumulative Δv). The NEXT node's precondition is
                      checked against THIS.
  * a CALCULATED COST — the per-step Δv, computed from real orbital mechanics (astro.py / bodies.py
                      / transfer_planner.py), NOT a guessed magic number; plus, for a transfer, the
                      launch/transfer WINDOW (``window_ut``), the wait to it (``wait_s``) and the
                      time of flight.

The math, per primitive:
  * ``launch``     -> ascent Δv from the launch body's surface to a low parking orbit
                      (astro.ascent_dv: vis-viva orbital speed + the body's calculated gravity+drag
                      losses, minus the free rotational-surface credit).
  * ``transfer``   -> Hohmann transfer. For a MOON (same parent as the departure body) the window is
                      a PHASE ANGLE (astro.phase_angle_for_transfer) and the Δv is the Kerbin-centric
                      ejection + the arrival-vinf capture (astro.transfer_arrival_excess_speed +
                      capture_from_excess). For a SUN-to-SUN planet transfer the window is the next
                      departure UT (transfer_planner.find_transfer_window when a live ``sc`` is given,
                      else a closed-form synodic/phase-angle estimate), the ejection is the Oberth burn
                      (astro.oberth_ejection_dv) and capture is the arrival-vinf capture.
  * ``set_orbit``  -> Hohmann Δv between the current low orbit and the requested apsis (astro.hohmann).
  * ``land``/``ascend`` -> the body's surface<->low-orbit Δv (astro.surface_to_orbit_dv for an airless
                      body; astro.ascent_dv for an atmospheric one, with descent symmetric to ascent).
  * ``recover``    -> a deorbit + descent budget on the home body (small with an atmosphere — chutes /
                      aerobrake do the work — larger if propulsive).
  * everything else (plant_flag, walk_to, dock, rendezvous, commission_relay, transfer_crew,
                      select_vessel) is a state/logic step with ~0 Δv but a real precondition.

EVERYTHING here runs OFFLINE: the closed-form helpers in astro.py take plain floats (mu/radius/…)
straight from bodies.py, so the graph builds in the test suite and in ``--dry-run`` with no kRPC.
A live ``sc`` (kRPC space_center) may be passed to use the precise Lambert ``find_transfer_window``
for planet transfers; without it the synodic closed form is used.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from .. import astro
from ..bodies import Body, body as lookup_body, parent_of

# Mission Δv reserve fraction applied to the summed budget (the validator's resource check uses it):
# real missions never plan to burn the tanks dry. 8% is the project's default crewed reserve.
MISSION_RESERVE_FRAC = 0.08


class Situation(str, Enum):
    """The coarse vehicle situation the symbolic state tracks (a subset of kRPC's situations that the
    precondition/postcondition logic actually reasons about)."""

    PRELAUNCH = "prelaunch"     # on the pad at the launch body, not yet flown
    ORBIT = "orbit"             # in a (parking/established) orbit of ``body``
    LANDED = "landed"           # on the surface of ``body``
    SUBORBITAL = "suborbital"   # on a transfer/return trajectory (between SOIs / pre-capture)


@dataclass(slots=True)
class WorldState:
    """The symbolic universe state that flows node -> node along the graph.

    It is NOT the live kRPC state: it is what the PLAN logically implies at each step, so the
    validator can reason about reachability (you cannot land before you are in orbit of a body) and
    budget (cumulative Δv) entirely offline, before anything flies."""

    body: str = "Kerbin"                 # the body the vehicle is currently at / orbiting / landed on
    situation: Situation = Situation.PRELAUNCH
    crew: int = 0                        # kerbals aboard
    has_vehicle: bool = False            # a launched/selected vehicle exists (False before launch)
    cumulative_dv: float = 0.0           # Σ Δv spent so far (m/s)
    visited: tuple[str, ...] = ()        # ordered bodies the plan has been at (for the return check)

    def copy(self) -> "WorldState":
        return WorldState(self.body, self.situation, self.crew, self.has_vehicle,
                          self.cumulative_dv, self.visited)


@dataclass(slots=True)
class Condition:
    """A precondition (or postcondition) as a small set of REQUIRED facts about a WorldState. Only the
    non-None fields are asserted; the validator turns an unmet field into a specific error string."""

    body: str | None = None              # must be at this body
    situation: Situation | None = None   # must be in this situation
    needs_vehicle: bool = False          # a vehicle must exist
    min_crew: int | None = None          # at least this many kerbals aboard
    note: str = ""                       # human-readable summary for the log

    def unmet(self, state: WorldState) -> list[str]:
        """Return the list of specific reasons ``state`` fails this condition (empty == satisfied)."""
        reasons: list[str] = []
        if self.needs_vehicle and not state.has_vehicle:
            reasons.append("no vehicle exists yet (nothing launched/selected)")
        if self.body is not None and state.body.lower() != self.body.lower():
            reasons.append(f"requires being at {self.body}, but state is at {state.body}")
        if self.situation is not None and state.situation != self.situation:
            reasons.append(
                f"requires situation '{self.situation.value}', but state is '{state.situation.value}'")
        if self.min_crew is not None and state.crew < self.min_crew:
            reasons.append(f"requires >= {self.min_crew} crew aboard, but state has {state.crew}")
        return reasons


@dataclass(slots=True)
class MissionNode:
    """One node of the mission graph: a primitive plus its computed pre/post state and cost."""

    index: int
    primitive: str
    args: dict
    precondition: Condition
    postcondition: Condition
    state_in: WorldState
    state_out: WorldState
    dv_mps: float = 0.0                  # the calculated per-step Δv
    window_ut: float | None = None       # absolute departure UT for a transfer (None if N/A)
    wait_s: float | None = None          # wait until the window (s) for a transfer (None if N/A)
    tof_s: float | None = None           # transfer time of flight (s)
    target_body: str | None = None       # the body a transfer/on-body step refers to
    calc: str = ""                       # short human-readable note on HOW the cost was computed
    errors: list[str] = field(default_factory=list)   # build-time per-node problems (e.g. bad window)

    def summary(self) -> str:
        bits = [f"{self.index}. {self.primitive}"]
        if self.target_body:
            bits.append(f"->{self.target_body}")
        bits.append(f"Δv={self.dv_mps:.0f} m/s")
        if self.window_ut is not None:
            bits.append(f"window_ut={self.window_ut:.0f}")
        if self.wait_s is not None:
            bits.append(f"wait={self.wait_s/21600:.1f}d")
        if self.tof_s is not None:
            bits.append(f"tof={self.tof_s/21600:.1f}d")
        return "  ".join(bits)


@dataclass(slots=True)
class MissionGraph:
    """The full directed graph: the ordered nodes plus the launch body and the rolled-up budget."""

    launch_body: str
    nodes: list[MissionNode]
    total_dv: float = 0.0
    final_state: WorldState = field(default_factory=WorldState)
    vehicle_dv: float | None = None

    def edges(self) -> list[tuple[int, int]]:
        """The directed edges (node i -> node i+1): the plan is a linear chain of state transitions."""
        return [(self.nodes[i].index, self.nodes[i + 1].index) for i in range(len(self.nodes) - 1)]

    def required_dv_with_reserve(self) -> float:
        return self.total_dv * (1.0 + MISSION_RESERVE_FRAC)

    def render(self) -> str:
        lines = [f"MISSION GRAPH (launch={self.launch_body}, nodes={len(self.nodes)}):"]
        for n in self.nodes:
            lines.append("  " + n.summary())
            if n.calc:
                lines.append(f"       calc: {n.calc}")
            for e in n.errors:
                lines.append(f"       ERROR: {e}")
        lines.append(f"  TOTAL Δv = {self.total_dv:.0f} m/s "
                     f"(+{MISSION_RESERVE_FRAC*100:.0f}% reserve = {self.required_dv_with_reserve():.0f} m/s)")
        if self.vehicle_dv is not None:
            lines.append(f"  VEHICLE Δv = {self.vehicle_dv:.0f} m/s")
        return "\n".join(lines)


# --------------------------------------------------------------------------------------------------- #
# Per-primitive math. Each returns (dv, window_ut, wait_s, tof_s, target_body, calc, node_errors) and
# is given the INCOMING world state so it can use the right central body. Everything closed-form.
# --------------------------------------------------------------------------------------------------- #

def _classify_transfer(dep: Body, tgt: Body) -> str:
    """Classify a transfer by the bodies' hierarchy:
      * 'moon'    — the target ORBITS the departure body (Kerbin->Mun): a single-SOI Hohmann from a low
                    parking orbit of ``dep`` out to the moon's orbit radius, capturing at the moon.
      * 'return'  — the departure body ORBITS the target (Mun->Kerbin): eject from the moon's orbit and
                    capture/aerobrake at the parent.
      * 'sibling' — both orbit the SAME parent, neither is the other's parent (Kerbin->Duna, both orbit
                    the Sun): a parent(Sun)-centric Hohmann with an Oberth ejection + arrival capture.
    """
    if parent_of(tgt).name == dep.name:
        return "moon"
    if parent_of(dep).name == tgt.name:
        return "return"
    return "sibling"


def _hierarchical_transfer_math(dep: Body, tgt: Body, kind: str) -> tuple[float, float, float, str]:
    """Δv, phase-angle (rad), tof (s), calc-note for a transfer WITHIN one SOI (a moon transfer
    Kerbin->Mun, or a return Mun->Kerbin). The central body is the common parent; the craft departs from a
    low orbit of ``dep`` and arrives in a low orbit of ``tgt``.

    For a moon transfer (kind='moon') the central body is ``dep`` itself (the moon orbits it): r1 = the
    departure parking radius about ``dep``, r2 = the moon's orbit radius. For a return (kind='return') the
    central body is ``tgt`` (the departure body orbits it): r1 = the departure body's orbit radius about
    ``tgt``, r2 = a low orbit of ``tgt``. Both are vis-viva Hohmanns — no magic numbers."""
    if kind == "moon":
        central = dep
        r1 = dep.low_orbit_radius_m()       # depart from a low parking orbit about dep
        r2 = tgt.orbit_radius_m             # arrive at the moon's orbit radius about dep
        dep_dv, _arr, tof = astro.hohmann(central.mu, r1, r2)
        # Arrival v_infinity at the moon's SOI relative to the moon -> capture into a low moon orbit.
        vinf_arr = astro.transfer_arrival_excess_speed(central.mu, r1, r2)
        capture_dv = astro.capture_from_excess(tgt.mu, tgt.low_orbit_radius_m(), vinf_arr)
        dv = dep_dv + capture_dv
        phase = astro.phase_angle_for_transfer(central.mu, r2, tof)
        calc = (f"moon Hohmann about {central.name}: eject {dep_dv:.0f} + capture {capture_dv:.0f} "
                f"(vinf_arr {vinf_arr:.0f}); phase {math.degrees(phase):.0f}deg, tof {tof/3600:.1f}h")
        return dv, phase, tof, calc
    # kind == 'return': depart the moon's orbit, drop to a low orbit of the parent.
    central = tgt
    r1 = dep.orbit_radius_m                 # the departure moon's orbit radius about the parent
    r2 = central.low_orbit_radius_m()       # a low orbit of the parent (home)
    dep_dv, arr_dv, tof = astro.hohmann(central.mu, r1, r2)
    dv = dep_dv + arr_dv
    phase = astro.phase_angle_for_transfer(central.mu, r2, tof)
    calc = (f"return Hohmann about {central.name}: lower {dep_dv:.0f} + arrive {arr_dv:.0f}; "
            f"tof {tof/3600:.1f}h")
    return dv, phase, tof, calc


# Post-aerobrake CIRCULARIZATION Δv: arriving at a body WITH an atmosphere behind a heat shield, the
# atmosphere cancels the arrival v_inf for free; only a small burn at apoapsis is needed to lift the
# periapsis out of the air into a bound orbit. Replaces the full propulsive capture for an aerocapture.
AEROCAPTURE_CIRCULARIZE_DV_MPS = 120.0


def _planet_transfer_math(dep: Body, tgt: Body, *, sc=None, ut_now: float = 0.0, aerocapture: bool = False
                          ) -> tuple[float, float | None, float | None, float, str, list[str]]:
    """Δv, window_ut, wait_s, tof, calc, errors for a SUN-to-SUN planet transfer (e.g. Kerbin->Duna).

    Departure ejection = Oberth burn from a low parking orbit of ``dep`` for the heliocentric v_inf;
    capture = arrival-v_inf capture into a low orbit of ``tgt``. The WINDOW is the next departure UT:
    with a live ``sc`` we call the precise Lambert ``transfer_planner.find_transfer_window``; offline we
    use the closed-form synodic estimate (next time the phase angle lines up).

    AEROCAPTURE: when ``aerocapture`` (the target has air and the plan arrives behind a heat shield), the
    propulsive capture is replaced by a small post-aerobrake circularization — the model that makes a
    Duna/Eve arrival, and a Kerbin return, far cheaper than a full burn (the model was over-sizing the
    vehicle into infeasibility)."""
    sun = parent_of(dep)
    r_park = dep.low_orbit_radius_m()
    r1 = dep.orbit_radius_m
    r2 = tgt.orbit_radius_m
    vinf_dep = astro.transfer_departure_excess_speed(sun.mu, r1, r2)
    eject_dv = astro.oberth_ejection_dv(dep.mu, r_park, vinf_dep)
    vinf_arr = astro.transfer_arrival_excess_speed(sun.mu, r1, r2)
    if aerocapture and tgt.atmosphere_top_m > 0:
        capture_dv = AEROCAPTURE_CIRCULARIZE_DV_MPS
    else:
        capture_dv = astro.capture_from_excess(tgt.mu, tgt.low_orbit_radius_m(), vinf_arr)
    dv = eject_dv + capture_dv
    tof = astro.hohmann(sun.mu, r1, r2)[2]

    errors: list[str] = []
    window_ut: float | None = None
    wait_s: float | None = None

    if sc is not None:
        # Live precise window via Lambert over real Kepler positions (transfer_planner). Best-effort:
        # any live failure falls through to the closed form rather than aborting the graph build.
        try:
            from .. import transfer_planner as tp
            w = tp.find_transfer_window(sc, dep.name, tgt.name, ut_now=ut_now)
            window_ut = float(w["ut_dep"])
            tof = float(w.get("tof", tof))
            wait_s = window_ut - ut_now
            calc = (f"planet transfer (live Lambert): dep_ut {window_ut:.0f}, |vinf| {w['vinf_mag']:.0f}, "
                    f"eject {eject_dv:.0f} + capture {capture_dv:.0f}, tof {tof/21600:.1f}d")
            if wait_s is not None and wait_s < -tof:
                errors.append(f"transfer window UT {window_ut:.0f} is in the past relative to now {ut_now:.0f}")
            return dv, window_ut, wait_s, tof, calc, errors
        except Exception as exc:  # offline / live read failed -> closed-form estimate below
            errors.append(f"live window unavailable ({exc}); used closed-form estimate")

    # Closed-form synodic window: wait until the target leads the departure body by the required phase
    # angle, then depart. Without the bodies' current true anomalies we cannot pin an absolute UT, so
    # we report the *required* phase angle as the window and the synodic period as the wait bound.
    T_dep = astro.orbital_period(sun.mu, r1)
    T_tgt = astro.orbital_period(sun.mu, r2)
    synodic = (1.0 / abs(1.0 / T_dep - 1.0 / T_tgt)) if T_dep != T_tgt else float("inf")
    phase = astro.phase_angle_for_transfer(sun.mu, r2, tof)
    # A worst-case wait is one synodic period; the absolute UT cannot be fixed offline, so window_ut
    # stays None and the validator only sanity-checks that tof/synodic are finite & positive.
    wait_s = synodic if math.isfinite(synodic) else None
    calc = (f"planet transfer (closed form): required phase {math.degrees(phase):.0f}deg, "
            f"synodic {synodic/21600:.0f}d, eject {eject_dv:.0f} (vinf_dep {vinf_dep:.0f}) "
            f"+ capture {capture_dv:.0f} (vinf_arr {vinf_arr:.0f}), tof {tof/21600:.1f}d")
    if not math.isfinite(tof) or tof <= 0:
        errors.append("transfer time of flight is non-positive/undefined")
    if synodic <= 0:
        errors.append("transfer synodic period is non-positive")
    return dv, window_ut, wait_s, tof, calc, errors


# --------------------------------------------------------------------------------------------------- #
# Node builders — one per primitive. Each takes (state_in, args, *, sc, ut_now) and returns a
# fully-populated MissionNode (precondition checked against state_in by the validator, not here).
# --------------------------------------------------------------------------------------------------- #

def _build_launch(idx, state_in, args, **_kw) -> MissionNode:
    body = state_in.body  # launch from the current launch body (Kerbin in stock)
    b = lookup_body(body)
    crew = int(args.get("crew", 0) or 0)
    alt_km = float(args.get("target_alt_km", 100.0) or 100.0)
    r_orbit = b.radius_m + alt_km * 1000.0
    if b.atmosphere_top_m > 0:
        dv = astro.ascent_dv(b.mu, b.radius_m, r_orbit, b.atmosphere_top_m,
                             b.rotational_speed_mps, b.surface_rho)
    else:
        dv = astro.surface_to_orbit_dv(b.mu, b.radius_m, r_orbit)
    pre = Condition(note=f"launch from {body} (pad / no vehicle needed)")
    state_out = state_in.copy()
    state_out.has_vehicle = True
    state_out.crew = crew
    state_out.body = body
    state_out.situation = Situation.ORBIT
    state_out.cumulative_dv += dv
    if body not in state_out.visited:
        state_out.visited = state_out.visited + (body,)
    post = Condition(body=body, situation=Situation.ORBIT, needs_vehicle=True,
                     note=f"in orbit of {body}, crew {crew}")
    return MissionNode(idx, "launch", args, pre, post, state_in, state_out, dv_mps=dv,
                       target_body=body,
                       calc=f"ascent to {alt_km:.0f} km of {body}: astro.ascent_dv = {dv:.0f} m/s")


def _build_transfer(idx, state_in, args, *, sc=None, ut_now=0.0, **_kw) -> MissionNode:
    tgt_name = str(args.get("target_body") or "")
    dep = lookup_body(state_in.body)
    tgt = lookup_body(tgt_name)
    pre = Condition(situation=Situation.ORBIT, needs_vehicle=True,
                    note=f"must be in orbit of {state_in.body} to transfer to {tgt_name}")
    node_errors: list[str] = []
    if not tgt_name:
        node_errors.append("transfer has no target_body")
    kind = _classify_transfer(dep, tgt)
    if kind in ("moon", "return"):
        dv, _phase, tof, calc = _hierarchical_transfer_math(dep, tgt, kind)
        window_ut, wait_s = None, None
        calc = f"{state_in.body}->{tgt_name} ({kind}): " + calc
    else:  # 'sibling' — a Sun-to-Sun planet transfer
        aerocap = (str(args.get("capture_mode") or "").lower() == "aerocapture" and tgt.atmosphere_top_m > 0)
        dv, window_ut, wait_s, tof, calc, errs = _planet_transfer_math(
            dep, tgt, sc=sc, ut_now=ut_now, aerocapture=aerocap)
        node_errors += errs
        calc = f"{state_in.body}->{tgt_name} (planet{', aerocapture' if aerocap else ''}): " + calc
    state_out = state_in.copy()
    state_out.body = tgt_name or state_in.body
    state_out.situation = Situation.ORBIT      # transfer() captures into an orbit of the target
    state_out.cumulative_dv += dv
    if state_out.body not in state_out.visited:
        state_out.visited = state_out.visited + (state_out.body,)
    post = Condition(body=tgt_name or None, situation=Situation.ORBIT, needs_vehicle=True,
                     note=f"captured in orbit of {tgt_name}")
    return MissionNode(idx, "transfer", args, pre, post, state_in, state_out, dv_mps=dv,
                       window_ut=window_ut, wait_s=wait_s, tof_s=tof, target_body=tgt_name,
                       calc=calc, errors=node_errors)


def _build_set_orbit(idx, state_in, args, **_kw) -> MissionNode:
    b = lookup_body(state_in.body)
    pe_km = float(args.get("periapsis_km", 0.0) or 0.0)
    ap_km = float(args.get("apoapsis_km", 0.0) or 0.0)
    r_low = b.low_orbit_radius_m()
    r_target = b.radius_m + 0.5 * (pe_km + ap_km) * 1000.0
    dep, arr, _t = astro.hohmann(b.mu, r_low, max(r_target, b.radius_m + 1000.0))
    dv = dep + arr
    pre = Condition(situation=Situation.ORBIT, needs_vehicle=True,
                    note=f"must already be in orbit of {state_in.body} to reshape it")
    state_out = state_in.copy()
    state_out.cumulative_dv += dv
    post = Condition(body=state_in.body, situation=Situation.ORBIT, needs_vehicle=True,
                     note=f"orbit reshaped to {pe_km:.0f}x{ap_km:.0f} km of {state_in.body}")
    return MissionNode(idx, "set_orbit", args, pre, post, state_in, state_out, dv_mps=dv,
                       target_body=state_in.body,
                       calc=f"Hohmann to {pe_km:.0f}x{ap_km:.0f} km of {state_in.body}: "
                            f"astro.hohmann = {dv:.0f} m/s")


def _surface_orbit_dv(b: Body) -> float:
    """Δv between the surface and a low orbit of ``b`` — the cost of both ``land`` and ``ascend``.
    Atmospheric body: the full ascent budget (descent is aerobraked but we size the symmetric worst
    case for the resource check); airless: the propulsive surface<->orbit budget."""
    r_low = b.low_orbit_radius_m()
    if b.atmosphere_top_m > 0:
        return astro.ascent_dv(b.mu, b.radius_m, r_low, b.atmosphere_top_m,
                               b.rotational_speed_mps, b.surface_rho)
    return astro.surface_to_orbit_dv(b.mu, b.radius_m, r_low)


def _build_land(idx, state_in, args, **_kw) -> MissionNode:
    b = lookup_body(state_in.body)
    # Atmospheric descent is mostly aerobraked: a low de-orbit + terminal burn, not the full ascent
    # budget. Airless: the full propulsive descent (symmetric to ascent).
    if b.atmosphere_top_m > 0:
        dv = astro.deorbit_dv(b.mu, b.low_orbit_radius_m(), b.low_orbit_radius_m(),
                              b.radius_m + max(0.0, b.atmosphere_top_m * 0.5)) + 150.0
        how = "atmospheric (deorbit + chute/terminal)"
    else:
        dv = _surface_orbit_dv(b)
        how = "airless (propulsive descent, symmetric to ascent)"
    pre = Condition(body=state_in.body, situation=Situation.ORBIT, needs_vehicle=True,
                    note=f"must be in orbit of {state_in.body} to land on it")
    state_out = state_in.copy()
    state_out.situation = Situation.LANDED
    state_out.cumulative_dv += dv
    post = Condition(body=state_in.body, situation=Situation.LANDED, needs_vehicle=True,
                     note=f"landed on {state_in.body}")
    return MissionNode(idx, "land", args, pre, post, state_in, state_out, dv_mps=dv,
                       target_body=state_in.body,
                       calc=f"descent to {state_in.body} surface, {how} = {dv:.0f} m/s")


def _build_ascend(idx, state_in, args, **_kw) -> MissionNode:
    b = lookup_body(state_in.body)
    dv = _surface_orbit_dv(b)
    pre = Condition(body=state_in.body, situation=Situation.LANDED, needs_vehicle=True,
                    note=f"must be landed on {state_in.body} to ascend")
    state_out = state_in.copy()
    state_out.situation = Situation.ORBIT
    state_out.cumulative_dv += dv
    post = Condition(body=state_in.body, situation=Situation.ORBIT, needs_vehicle=True,
                     note=f"ascended to orbit of {state_in.body}")
    return MissionNode(idx, "ascend", args, pre, post, state_in, state_out, dv_mps=dv,
                       target_body=state_in.body,
                       calc=f"surface->orbit of {state_in.body}: astro = {dv:.0f} m/s")


def _build_recover(idx, state_in, args, *, launch_body="Kerbin", **_kw) -> MissionNode:
    b = lookup_body(state_in.body)
    # Recovery is on the HOME body. Atmospheric home (Kerbin): aerobrake + chutes, tiny propulsive Δv.
    # The precondition is being at the home body (the validator also accepts a craft already returning
    # into the home SOI). A small deorbit allowance keeps the budget honest.
    if b.atmosphere_top_m > 0:
        dv = 200.0
        how = "aerobrake + chutes (home atmosphere)"
    else:
        dv = _surface_orbit_dv(b)
        how = "propulsive landing (airless home)"
    pre = Condition(body=launch_body, needs_vehicle=True,
                    note=f"must be at/returning to the home body {launch_body} to recover")
    state_out = state_in.copy()
    state_out.situation = Situation.LANDED
    state_out.cumulative_dv += dv
    post = Condition(body=launch_body, situation=Situation.LANDED, needs_vehicle=True,
                     note=f"recovered on {launch_body}")
    return MissionNode(idx, "recover", args, pre, post, state_in, state_out, dv_mps=dv,
                       target_body=launch_body,
                       calc=f"recover on {launch_body}: {how} = {dv:.0f} m/s")


def _build_plant_flag(idx, state_in, args, **_kw) -> MissionNode:
    pre = Condition(situation=Situation.LANDED, needs_vehicle=True, min_crew=1,
                    note="must be LANDED with crew aboard to EVA + plant a flag")
    state_out = state_in.copy()  # zero Δv, no situation change
    post = Condition(situation=Situation.LANDED, needs_vehicle=True, min_crew=1,
                     note=f"flag planted on {state_in.body}, crew re-boarded")
    return MissionNode(idx, "plant_flag", args, pre, post, state_in, state_out, dv_mps=0.0,
                       target_body=state_in.body, calc="EVA flag plant: 0 Δv (logic step)")


def _build_walk_to(idx, state_in, args, **_kw) -> MissionNode:
    pre = Condition(situation=Situation.LANDED, needs_vehicle=True, min_crew=1,
                    note="must be LANDED with a kerbal on EVA to walk")
    state_out = state_in.copy()
    post = Condition(situation=Situation.LANDED, needs_vehicle=True,
                     note=f"walked on {state_in.body} surface")
    return MissionNode(idx, "walk_to", args, pre, post, state_in, state_out, dv_mps=0.0,
                       target_body=state_in.body, calc="EVA walk: 0 Δv (logic step)")


def _build_dock(idx, state_in, args, **_kw) -> MissionNode:
    pre = Condition(situation=Situation.ORBIT, needs_vehicle=True,
                    note=f"must be in orbit (same SOI as the target) to dock at {state_in.body}")
    state_out = state_in.copy()
    state_out.cumulative_dv += 30.0  # small rendezvous/docking trim budget
    post = Condition(situation=Situation.ORBIT, needs_vehicle=True, note="docked")
    return MissionNode(idx, "dock", args, pre, post, state_in, state_out, dv_mps=30.0,
                       target_body=state_in.body, calc="docking trim: ~30 m/s")


def _build_rendezvous(idx, state_in, args, **_kw) -> MissionNode:
    pre = Condition(situation=Situation.ORBIT, needs_vehicle=True,
                    note=f"must be in orbit (same SOI as the target) to rendezvous at {state_in.body}")
    state_out = state_in.copy()
    state_out.cumulative_dv += 80.0  # phasing budget
    post = Condition(situation=Situation.ORBIT, needs_vehicle=True, note="rendezvous closed")
    return MissionNode(idx, "rendezvous", args, pre, post, state_in, state_out, dv_mps=80.0,
                       target_body=state_in.body, calc="rendezvous phasing: ~80 m/s")


def _build_logic(primitive, requires_orbit=False):
    """Factory for the remaining zero-Δv logic primitives (commission_relay, transfer_crew,
    select_vessel) whose precondition is just 'a vehicle exists' (and optionally 'in orbit')."""

    def _b(idx, state_in, args, **_kw) -> MissionNode:
        if primitive == "select_vessel":
            # select_vessel BOOTSTRAPS a vehicle: it needs no prior vehicle, and asserts one exists after.
            pre = Condition(note="select an existing vessel by name")
            state_out = state_in.copy()
            state_out.has_vehicle = True
            post = Condition(needs_vehicle=True, note="a vessel is selected/active")
        else:
            pre = Condition(needs_vehicle=True,
                            situation=(Situation.ORBIT if requires_orbit else None),
                            note=f"{primitive} needs an active vehicle")
            state_out = state_in.copy()
            post = Condition(needs_vehicle=True, note=f"{primitive} done")
        return MissionNode(idx, primitive, args, pre, post, state_in, state_out, dv_mps=0.0,
                           target_body=state_in.body, calc=f"{primitive}: 0 Δv (logic step)")

    return _b


# Dispatch table: primitive name -> node builder.
_BUILDERS = {
    "launch": _build_launch,
    "transfer": _build_transfer,
    "set_orbit": _build_set_orbit,
    "land": _build_land,
    "ascend": _build_ascend,
    "recover": _build_recover,
    "plant_flag": _build_plant_flag,
    "walk_to": _build_walk_to,
    "dock": _build_dock,
    "rendezvous": _build_rendezvous,
    "commission_relay": _build_logic("commission_relay", requires_orbit=True),
    "transfer_crew": _build_logic("transfer_crew"),
    "select_vessel": _build_logic("select_vessel"),
    # Drop the spent transfer stage in the parking orbit before descent (split-stage round-trip). Zero Δv,
    # ORBIT in -> ORBIT out, so the following land() ORBIT precondition is satisfied.
    "jettison_transfer_stage": _build_logic("jettison_transfer_stage", requires_orbit=True),
}


def build_mission_graph(steps: list[dict], *, launch_body: str = "Kerbin",
                        vehicle_dv: float | None = None, sc=None, ut_now: float = 0.0) -> MissionGraph:
    """Turn the LLM's ordered primitive steps into a directed MISSION GRAPH.

    Each step becomes a MissionNode carrying its precondition, postcondition, the symbolic world state
    flowing into and out of it, and a CALCULATED Δv (+ window/tof for transfers). The symbolic state is
    threaded node->node so the validator can check reachability and the cumulative budget. Build never
    raises on a bad plan — bad steps are recorded as node-level errors and surfaced by validate_plan; an
    unknown primitive becomes a node flagged with an error (so the validator reports it, not a crash).

    ``sc`` (a live kRPC space_center) is optional: when given, planet transfers use the precise Lambert
    window; offline the closed-form synodic estimate is used. Everything else is closed-form and offline.
    """
    state = WorldState(body=launch_body, situation=Situation.PRELAUNCH, visited=())
    nodes: list[MissionNode] = []
    for i, step in enumerate(steps or [], start=1):
        primitive = step.get("primitive", "")
        args = step.get("args", {}) or {}
        builder = _BUILDERS.get(primitive)
        if builder is None:
            # Unknown primitive: a passthrough node that carries the state unchanged but flags an error.
            pre = Condition(note=f"unknown primitive {primitive!r}")
            node = MissionNode(i, primitive or "<empty>", args, pre, pre, state.copy(), state.copy(),
                               dv_mps=0.0, calc="unknown primitive — no math",
                               errors=[f"unknown primitive {primitive!r} (not in the catalog)"])
        else:
            node = builder(i, state.copy(), args, sc=sc, ut_now=ut_now, launch_body=launch_body)
        nodes.append(node)
        state = node.state_out
    total_dv = sum(n.dv_mps for n in nodes)
    return MissionGraph(launch_body=launch_body, nodes=nodes, total_dv=total_dv,
                        final_state=state, vehicle_dv=vehicle_dv)
