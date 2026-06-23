"""Calculated Δv budgets for a MissionSpec — the single physics source the planner and optimizer share.

`mission.py` needs ONE number (`delta_v_budget_mps`, the whole stack's Δv requirement). `optimizer.py`
needs the SAME budget broken into ordered propulsive PHASES so it can hand each one to
`design.design_ship` and size tanks by the rocket equation. Both come from here, so the mission's
budget and the design's target are the same physics — never two drifting magic numbers.

Every phase Δv is derived in `astro.py` from the stock-body catalogue in `bodies.py` (the same
GM/radius/atmosphere kRPC measures live). For a Mun landing+return this reproduces the canonical
~6.1 km/s; for Kerbin LKO ~3.4 km/s.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import astro, bodies
from .models import MissionSpec


@dataclass(slots=True)
class PhaseBudget:
    """One propulsive phase: its name, calculated Δv, and the gravity field + min-TWR that matter for
    sizing engines (launch and powered landing are TWR-constrained; vacuum burns are not)."""
    name: str
    dv_mps: float
    twr_body_g: float = 0.0   # surface gravity where TWR matters (0 = pure vacuum burn)
    min_twr: float = 0.0


@dataclass(slots=True)
class MissionBudget:
    target_body: str
    phases: list[PhaseBudget] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_mps(self) -> float:
        return sum(p.dv_mps for p in self.phases)


# Min-TWR requirements are physics feasibility floors, not tuning: a launch from an atmosphere needs
# TWR>1 to leave the pad (1.4 gives an efficient gravity turn); a powered (hoverslam) landing needs
# TWR>~2 so the suicide burn is short; vacuum transfer/capture/ejection burns have no TWR floor.
_LAUNCH_MIN_TWR = 1.4
_LANDING_MIN_TWR = 2.0


def mission_budget(mission: MissionSpec) -> MissionBudget:
    """Build the ordered, calculated Δv phases for `mission`.

    Composition (only the phases the mission needs):
      ascent  : surface -> low parking orbit of the launch body (Kerbin), with gravity+drag losses
      transfer: Hohmann ejection toward the target body
      capture : drop the arrival hyperbola into low orbit of the target
      descent : powered landing from low target orbit to the surface (if require_landing)
      ascent2 : surface -> low target orbit (if require_landing and require_return)
      return  : ejection back toward the launch body (if require_return); re-entry is a free aerobrake
    """
    launch = bodies.KERBIN  # the lab launches from Kerbin (KSC) in every mission
    target = bodies.body(mission.target_body)
    r_park = launch.low_orbit_radius_m()

    phases: list[PhaseBudget] = []
    notes: list[str] = []

    # --- ascent to low Kerbin orbit (always) ---
    asc = astro.ascent_dv(launch.mu, launch.radius_m, r_park, launch.atmosphere_top_m,
                          launch.rotational_speed_mps)
    phases.append(PhaseBudget("ascent_to_orbit", asc, launch.surface_g, _LAUNCH_MIN_TWR))
    notes.append(f"ascent {launch.name} surface->{r_park - launch.radius_m:.0f}m orbit = {asc:.0f} m/s")

    interplanetary = target.name != launch.name and target.name != "Kerbin"

    if interplanetary:
        if target.parent.lower() == launch.name.lower():
            # A moon of the launch body (e.g. Mun about Kerbin): transfer is a Hohmann about Kerbin
            # to the moon's orbit, capture from the SOI excess speed into low moon orbit.
            dv_tmi, _, _ = astro.hohmann(launch.mu, r_park, target.orbit_radius_m)
            v_inf = astro.transfer_excess_speed(launch.mu, r_park, target.orbit_radius_m)
            r_moon_park = target.low_orbit_radius_m()
            dv_cap = astro.capture_from_excess(target.mu, r_moon_park, v_inf)
            phases.append(PhaseBudget(f"transfer_to_{target.name.lower()}", dv_tmi))
            phases.append(PhaseBudget(f"capture_{target.name.lower()}_orbit", dv_cap))
            notes.append(f"TMI {dv_tmi:.0f} m/s, capture {dv_cap:.0f} m/s (v_inf {v_inf:.0f})")
            arrival = target
            r_arrival_park = r_moon_park
            ret_v_inf = v_inf
        else:
            # An interplanetary target (e.g. Duna about the Sun): Oberth ejection from Kerbin orbit.
            sun = bodies.parent_of(launch)
            dep = astro.interplanetary_departure(
                sun.mu, launch.mu, launch.orbit_radius_m, target.orbit_radius_m, r_park)
            dv_eject = dep["ejection_dv"]
            r_arrival_park = target.low_orbit_radius_m()
            # Capture at the target from the heliocentric arrival excess (symmetric Hohmann leg).
            v_inf_arr = astro.transfer_excess_speed(sun.mu, target.orbit_radius_m, launch.orbit_radius_m)
            dv_cap = astro.capture_from_excess(target.mu, r_arrival_park, v_inf_arr)
            phases.append(PhaseBudget(f"transfer_to_{target.name.lower()}", dv_eject))
            phases.append(PhaseBudget(f"capture_{target.name.lower()}_orbit", dv_cap))
            notes.append(f"ejection {dv_eject:.0f} m/s, capture {dv_cap:.0f} m/s")
            arrival = target
            # Return ejection from the target back toward Kerbin uses the same heliocentric excess.
            ret_v_inf = astro.transfer_excess_speed(sun.mu, launch.orbit_radius_m, target.orbit_radius_m)
    else:
        arrival = launch
        r_arrival_park = r_park
        ret_v_inf = 0.0

    # --- powered descent + surface ascent on the target ---
    if mission.require_landing and interplanetary:
        if arrival.atmosphere_top_m > 0:
            dv_desc = astro.ascent_dv(arrival.mu, arrival.radius_m, r_arrival_park,
                                      arrival.atmosphere_top_m, arrival.rotational_speed_mps)
        else:
            dv_desc = astro.surface_to_orbit_dv(arrival.mu, arrival.radius_m, r_arrival_park)
        phases.append(PhaseBudget(f"descent_to_{arrival.name.lower()}", dv_desc,
                                  arrival.surface_g, _LANDING_MIN_TWR))
        notes.append(f"powered descent {arrival.name} = {dv_desc:.0f} m/s")
        if mission.require_return:
            dv_asc2 = astro.surface_to_orbit_dv(arrival.mu, arrival.radius_m, r_arrival_park) \
                if arrival.atmosphere_top_m <= 0 else dv_desc
            phases.append(PhaseBudget(f"ascent_from_{arrival.name.lower()}", dv_asc2,
                                      arrival.surface_g, _LAUNCH_MIN_TWR))
            notes.append(f"surface ascent {arrival.name} = {dv_asc2:.0f} m/s")

    # --- return ejection toward Kerbin (re-entry braked by atmosphere = free) ---
    if mission.require_return and interplanetary:
        dv_ret = astro.oberth_ejection_dv(arrival.mu, r_arrival_park, ret_v_inf)
        phases.append(PhaseBudget(f"return_from_{arrival.name.lower()}", dv_ret))
        notes.append(f"return ejection {arrival.name}->Kerbin = {dv_ret:.0f} m/s (re-entry aerobraked)")

    return MissionBudget(target_body=target.name, phases=phases, notes=notes)


def total_budget_mps(mission: MissionSpec) -> int:
    """Whole-stack Δv REQUIREMENT for the mission (the bare sum of the calculated phase budgets),
    rounded to an int for MissionSpec.delta_v_budget_mps.

    This is exactly the Δv the stack must deliver. The optimizer sizes the design to these same phases
    plus a small reserve, so the built design's total Δv always clears this requirement — the mission
    budget and the design's target are one physics, not two drifting magic numbers."""
    return int(round(mission_budget(mission).total_mps))
