"""Maneuver-node execution: the astronaut's standard burn skill, parameterized by the AI."""

from __future__ import annotations

import math
from typing import Any

from astra.errors import AstraError
from astra.ksp import ksp
from astra.reflex import vec
from astra.reflex.context import Ctx
from astra.reflex.engine import fly
from astra.reflex.laws import NodeDirection, ThrottleLaw

G0 = 9.80665


class NodeBurnThrottle(ThrottleLaw):
    """Full throttle (up to max) until the remaining Δv is within `feather_s` seconds of full
    acceleration, then proportional down to `min_throttle`. Stops at `tolerance_mps` remaining,
    on overshoot (remaining vector reverses), or when the remaining Δv grows (wrong-way burn)."""

    finishes = True

    def __init__(self, index: int, max_throttle: float, feather_s: float, min_throttle: float, tolerance_mps: float):
        self.index, self.max_t, self.feather_s = index, max_throttle, feather_s
        self.min_t, self.tol = min_throttle, tolerance_mps
        self.describe = f"node burn (max {max_throttle:g}, feather {feather_s:g}s, tol {tolerance_mps:g} m/s)"
        self._done: str | None = None
        self.applied = 0.0
        self._last_ut: float | None = None
        self._dir0: vec.Vec | None = None
        self._r0: float | None = None
        self._burning_since: float | None = None
        self.remaining = math.nan

    def _node(self, ctx: Ctx):
        nodes = ctx.vessel.control.nodes
        return nodes[self.index] if len(nodes) > self.index else None

    def start(self, ctx, m):
        node = self._node(ctx)
        if node is None:
            raise AstraError(f"no maneuver node {self.index}")
        rem = node.remaining_burn_vector(ctx.nrf)
        self._dir0 = vec.unit(rem)
        self._r0 = vec.norm(rem)

    def update(self, ctx, m):
        node = self._node(ctx)
        if node is None:
            self._done = "node disappeared"
            return 0.0
        try:
            rem = node.remaining_burn_vector(ctx.nrf)
        except Exception:  # noqa: BLE001
            rem = node.burn_vector(ctx.nrf)
        r = vec.norm(rem)
        self.remaining = r
        ut, thrust, mass = m.get("ut"), m.get("thrust", 0.0), m.get("mass", 0.0)
        if self._last_ut is not None and ut and mass:
            self.applied += thrust / mass * max(0.0, ut - self._last_ut)  # integrate in game time
        self._last_ut = ut
        if r < self.tol:
            self._done = f"remaining Δv {r:.2f} m/s below tolerance"
            return 0.0
        if self._dir0 is not None and r > 1e-6 and vec.dot(vec.unit(rem), self._dir0) < 0.0:
            self._done = f"overshoot: burn vector reversed (remaining {r:.2f} m/s)"
            return 0.0
        thr_now = m.get("throttle", 0.0) or 0.0
        if thr_now > 0.3 and self._burning_since is None:
            self._burning_since = ut
        if self._burning_since is not None and ut - self._burning_since > 3.0 and self._r0 and r > self._r0 * 1.05:
            self._done = (f"wrong-way burn: remaining Δv grew {self._r0:.1f} -> {r:.1f} m/s; the control point may "
                          f"not face the thrust direction")
            return 0.0
        a_full = (m.get("available_thrust", 0.0) or 0.0) * self.max_t / mass if mass else 0.0
        if a_full <= 0:
            return self.max_t
        if r > a_full * self.feather_s:
            return self.max_t
        return max(self.min_t, self.max_t * r / (a_full * self.feather_s))

    def done(self, ctx, m):
        return self._done


def thrust_axis_offset_deg(vessel) -> float | None:
    """Angle between the control part's forward axis and the summed thrust of active engines."""
    ref = vessel.reference_frame
    total = (0.0, 0.0, 0.0)
    for eng in vessel.parts.engines:
        if not eng.active:
            continue
        for th in eng.thrusters:
            try:
                d = th.thrust_direction(ref)
            except Exception:  # noqa: BLE001 — just after a save loads, thrust transforms are not set up yet
                return None
            total = vec.add(total, d)
    if vec.norm(total) < 1e-9:
        return None
    # kRPC's thrust_direction is the direction of the force (opposite the exhaust); the vessel's
    # forward (control) axis is +y in vessel.reference_frame.
    return vec.angle_deg(total, (0.0, 1.0, 0.0))


def burn_time_s(mass_kg: float, thrust_n: float, isp_s: float, dv: float) -> float:
    ve = isp_s * G0
    m1 = mass_kg * math.exp(-dv / ve)
    return (mass_kg - m1) * ve / thrust_n


def execute_node(*, node_index: int, max_throttle: float, lead_fraction: float, align_deg: float,
                 max_late_s: float, align_margin_s: float, feather_s: float, min_throttle: float,
                 tolerance_mps: float, auto_stage: Any, stop_when: list[dict] | None,
                 remove_node: bool, max_axis_offset_deg: float) -> dict:
    k = ksp()
    k.require_flight()
    v = k.vessel()
    nodes = v.control.nodes
    if len(nodes) <= node_index:
        raise AstraError(f"there is no maneuver node {node_index} ({len(nodes)} planned)",
                         "create one with node_create (compute it first with compute_maneuver)")
    node = nodes[node_index]
    dv = node.remaining_delta_v
    thrust = v.available_thrust * max_throttle
    isp = v.specific_impulse or v.vacuum_specific_impulse
    if thrust <= 0 or not isp:
        raise AstraError("no active engine can thrust right now",
                         "stage (control_stage) or activate an engine (control_part) first; check vessel_stages")
    offset = thrust_axis_offset_deg(v)
    if offset is not None and offset > max_axis_offset_deg:
        raise AstraError(f"the control point faces {offset:.1f}° away from the thrust axis",
                         "the autopilot aims the control part's forward axis; set a control part aligned with "
                         "the engines (control_part action=control_from) before burning")
    bt = burn_time_s(v.mass, thrust, isp, dv)
    start_ut = node.ut - bt * lead_fraction
    sc = k.sc
    report: dict[str, Any] = {"node_dv_mps": dv, "burn_time_est_s": bt, "start_ut": start_ut,
                              "thrust_axis_offset_deg": offset}

    # Warp close to the start (the burn itself never runs under warp).
    lead_needed = align_margin_s
    if start_ut - sc.ut > lead_needed + 30:
        v.control.throttle = 0.0
        k.set_paused(False)
        sc.warp_to(start_ut - lead_needed)
        sc.rails_warp_factor = 0
        report["warped_to_ut"] = sc.ut

    # Align and coast to the start time.
    state = {"aligned_since": None}

    def ready(m: dict) -> str | None:
        # Start on time once within align_deg (the burn keeps tracking the vector). Burning late with
        # a node's inertially fixed vector bends the orbit, so give up instead after max_late_s.
        err = m.get("autopilot_error")
        ut = m["ut"]
        if err is not None and err <= align_deg:
            state["aligned_since"] = state["aligned_since"] or ut
        else:
            state["aligned_since"] = None
        steady = state["aligned_since"] is not None and ut - state["aligned_since"] >= 0.5
        if ut >= start_ut and steady:
            return f"aligned at burn start (error {err:.2f} deg, {max(0.0, ut - start_ut):.1f} s late)"
        if ut >= start_ut + max_late_s:
            return f"not aligned within {max_late_s:g} s of the planned start (error {err})"
        return None

    align = fly(attitude_law=NodeDirection(node_index), throttle=0.0, extra_stop=ready,
                max_game_s=max(5.0, start_ut - sc.ut) + max_late_s + 5, label="align",
                interlocks={"flameout": False, "soi_change": True}, trace_points=4, pause_at_end=False)
    report["align"] = {"stopped_by": align["stopped_by"], "game_s": align["game_s"]}
    if not str(align["stopped_by"].get("detail", "")).startswith("aligned"):
        report["result"] = ("not burned: could not align in time; the node is now stale. Re-plan it (the "
                            "vessel has moved on), give more align_margin_s, or relax align_deg")
        report["paused"] = k.hold_for_deliberation()
        return report

    # Burn.
    a_full = thrust / v.mass
    law = NodeBurnThrottle(node_index, max_throttle, feather_s, min_throttle, tolerance_mps)
    burn = fly(throttle_law=law, attitude_law=NodeDirection(node_index, freeze_below_mps=max(tolerance_mps * 5, a_full * 0.5)),
               until=stop_when, auto_stage=auto_stage, exit_throttle="cut", label="burn",
               max_game_s=bt * 2.5 + 60, interlocks={"soi_change": False})
    report["burn"] = burn
    report["applied_dv_mps"] = law.applied
    report["remaining_dv_mps"] = law.remaining
    v = k.vessel()
    nodes = v.control.nodes
    if remove_node and len(nodes) > node_index:
        nodes[node_index].remove()
        report["node_removed"] = True
    report["paused"] = k.hold_for_deliberation()
    return report

