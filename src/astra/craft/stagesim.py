"""Per-stage Δv simulation in the manner of MechJeb's FuelFlowSimulation.

The simulator walks the stages from the current one down to 0. At each stage it drops the parts
decoupled there, lights the engines activated so far, and burns: every burning engine draws each
propellant, in its ratio (by volume, KSP units), from the parts KSP's flow rules let it reach:

* ``NO_FLOW`` (SolidFuel): only the engine's own part;
* stack flow (LiquidFuel/Oxidizer: ``STACK_PRIORITY_SEARCH`` / ``STAGE_STACK_FLOW*``): parts reachable
  through attachments whose parts allow crossfeed (decouplers block unless their crossfeed is on;
  fuel lines feed one way), highest priority first — parts that are dropped sooner drain first;
* vessel flow (MonoPropellant, XenonGas: ``STAGE_PRIORITY_FLOW``/``ALL_VESSEL*``): any attached part.

A stage ends when the engines and tanks that the next stage drops are spent (so parallel boosters
separate while the core keeps its fuel), or when nothing can burn. Fuel flow is set by vacuum
thrust and Isp (KSP keeps it constant), so Δv at pressure ``p`` uses the same burn with Isp(p).

Massless propellants (ElectricCharge) are assumed available; air-breathing engines are skipped.
Build the part list with :func:`parts_from_placement` (a design) or :func:`parts_from_vessel`
(a live kRPC vessel); both feed :func:`simulate`.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

G0 = 9.80665
STACK_MODES = {"STACK_PRIORITY_SEARCH", "STAGE_STACK_FLOW", "STAGE_STACK_FLOW_BALANCE"}
VESSEL_MODES = {"ALL_VESSEL", "ALL_VESSEL_BALANCE", "STAGE_PRIORITY_FLOW", "STAGE_PRIORITY_FLOW_BALANCE"}
PRIORITY_MODES = {"STACK_PRIORITY_SEARCH", "STAGE_STACK_FLOW", "STAGE_STACK_FLOW_BALANCE",
                  "STAGE_PRIORITY_FLOW", "STAGE_PRIORITY_FLOW_BALANCE"}
_EPS = 1e-9


@dataclass
class SimEngine:
    thrust_vac_kn: float  # at 100 % thrust limit
    isp_vac_s: float
    isp_s: float  # at the simulation pressure
    propellants: list[tuple[str, float, str]]  # (resource, ratio, flow mode)
    limit: float = 1.0  # thrust limiter 0..1

    @property
    def mass_flow_tps(self) -> float:
        return self.thrust_vac_kn * self.limit / (self.isp_vac_s * G0) if self.isp_vac_s > 0 else 0.0

    @property
    def thrust_p_kn(self) -> float:
        return self.thrust_vac_kn * self.limit * self.isp_s / self.isp_vac_s if self.isp_vac_s > 0 else 0.0


@dataclass
class SimPart:
    key: str
    label: str
    dry_mass_t: float
    resources: dict[str, float]  # current amount, units
    densities: dict[str, float]  # tonnes per unit
    neighbors: list[str] = field(default_factory=list)  # attached parts (both directions)
    fuel_sources: list[str] = field(default_factory=list)  # parts feeding this one through fuel lines
    crossfeed: bool = True
    activation_stage: int = -1
    decouple_stage: int = -1
    engines: list[SimEngine] = field(default_factory=list)


@dataclass
class StageResult:
    stage: int
    start_mass_t: float
    end_mass_t: float
    burn_s: float = 0.0
    dv_vac_mps: float = 0.0
    dv_mps: float = 0.0  # at the simulation pressure
    thrust_vac_kn: float = 0.0  # at stage start
    thrust_kn: float = 0.0  # at the simulation pressure, stage start
    engines: list[str] = field(default_factory=list)
    drops: list[str] = field(default_factory=list)
    activates: list[str] = field(default_factory=list)
    propellant_used_t: dict[str, float] = field(default_factory=dict)

    def isp(self, vac: bool) -> float:
        m = self.start_mass_t - self.end_mass_t
        if m <= 0 or self.burn_s <= 0:
            return 0.0
        dv = self.dv_vac_mps if vac else self.dv_mps
        return dv / (G0 * math.log(self.start_mass_t / self.end_mass_t))

    def to_dict(self, gravity: float | None) -> dict[str, Any]:
        d: dict[str, Any] = {"stage": self.stage, "start_mass_t": self.start_mass_t, "end_mass_t": self.end_mass_t,
                             "dv_vac_mps": self.dv_vac_mps, "dv_mps": self.dv_mps, "burn_s": self.burn_s}
        if self.engines:
            d.update(thrust_vac_kn=self.thrust_vac_kn, thrust_kn=self.thrust_kn,
                     isp_vac_s=self.isp(True), isp_s=self.isp(False), engines=counted(self.engines))
            if gravity:
                d["twr_start"] = self.thrust_kn / (self.start_mass_t * gravity)
                d["twr_end"] = self.thrust_kn / (self.end_mass_t * gravity) if self.end_mass_t > 0 else None
        if self.activates:
            d["activates"] = counted(self.activates)
        if self.drops:
            d["drops"] = counted(self.drops)
        if self.propellant_used_t:
            d["propellant_used_t"] = self.propellant_used_t
        return d


def counted(labels: list[str]) -> list[str]:
    """['srb', 'srb', 'core'] -> ['core', 'srb x2']"""
    return [f"{k} x{n}" if n > 1 else k for k, n in sorted(Counter(labels).items())]


def _mass(parts: dict[str, SimPart]) -> float:
    return sum(p.dry_mass_t + sum(a * p.densities.get(r, 0.0) for r, a in p.resources.items())
               for p in parts.values())


def _reachable(start: SimPart, parts: dict[str, SimPart]) -> list[SimPart]:
    seen = {start.key}
    order = [start]
    todo = [start]
    while todo:
        p = todo.pop()
        if p is not start and not p.crossfeed:
            continue  # its own tanks can be drawn, but fuel does not pass through it
        for k in p.neighbors + p.fuel_sources:
            if k in parts and k not in seen:
                seen.add(k)
                order.append(parts[k])
                todo.append(parts[k])
    return order


def _sources(engine_part: SimPart, resource: str, mode: str, parts: dict[str, SimPart]) -> list[SimPart]:
    if mode == "NO_FLOW":
        pool = [engine_part]
    elif mode in VESSEL_MODES:
        pool = list(parts.values())
    else:
        pool = _reachable(engine_part, parts)
    pool = [p for p in pool if p.resources.get(resource, 0.0) > _EPS]
    if pool and mode in PRIORITY_MODES:
        top = max(p.decouple_stage for p in pool)
        pool = [p for p in pool if p.decouple_stage == top]
    return pool


def simulate(parts: list[SimPart], start_stage: int, *, max_steps: int = 10000) -> list[StageResult]:
    """Run stages ``start_stage`` .. 0. ``start_stage`` is the stage whose engines are burning now
    (a design on the pad: one more than its highest stage number, i.e. nothing lit)."""
    alive = {p.key: p for p in parts}
    results: list[StageResult] = []
    for s in range(start_stage, -1, -1):
        dropped = [p for p in alive.values() if p.decouple_stage >= s]
        for p in dropped:
            del alive[p.key]
        res = StageResult(stage=s, start_mass_t=_mass(alive), end_mass_t=0.0,
                          drops=sorted(p.label for p in dropped),
                          activates=sorted(p.label for p in alive.values() if p.activation_stage == s))
        engines = [(p, e) for p in alive.values() if 0 <= s <= p.activation_stage for e in p.engines]
        next_drop = {p.key for p in alive.values() if p.decouple_stage == s - 1} if s > 0 else set()
        first = True
        involved = False  # does the next stage drop an engine or tank this burn uses?
        for _ in range(max_steps):
            draws: dict[tuple[str, str], float] = {}  # (part, resource) -> units/s
            burning: list[tuple[SimPart, SimEngine]] = []
            for p, e in engines:
                plan = []
                ok = e.mass_flow_tps > 0
                mass_ratio = sum(ratio * _density(p, parts, r) for r, ratio, _ in e.propellants)
                for r, ratio, mode in e.propellants:
                    if ok and _density(p, parts, r) <= 0:
                        continue  # massless (ElectricCharge): assumed available
                    src = _sources(p, r, mode, alive) if ok else []
                    if not src:
                        ok = False
                        break
                    plan.append((r, ratio, src))
                if not ok or mass_ratio <= 0:
                    continue
                k = e.mass_flow_tps / mass_ratio  # KSP units per second per unit of ratio
                for r, ratio, src in plan:
                    total = sum(q.resources[r] for q in src)
                    for q in src:  # draw in proportion to what each tank holds
                        draws[(q.key, r)] = draws.get((q.key, r), 0.0) + k * ratio * q.resources[r] / total
                burning.append((p, e))
            uses_drop = any(q in next_drop for q, _ in draws) or any(p.key in next_drop for p, _ in burning)
            if first:
                res.thrust_vac_kn = sum(e.thrust_vac_kn * e.limit for _, e in burning)
                res.thrust_kn = sum(e.thrust_p_kn for _, e in burning)
                res.engines = sorted(p.label for p, _ in burning)
                involved = uses_drop
                first = False
            if not burning:
                break
            if involved and not uses_drop:
                break  # what the next stage drops is spent: stage now (e.g. boosters out, core still fuelled)
            dt = min(alive[q].resources[r] / rate for (q, r), rate in draws.items() if rate > 0)
            flow = sum(e.mass_flow_tps for _, e in burning)
            f_vac = sum(e.thrust_vac_kn * e.limit for _, e in burning)
            f_p = sum(e.thrust_p_kn for _, e in burning)
            m0 = _mass(alive)
            for (q, r), rate in draws.items():
                part = alive[q]
                used = min(part.resources[r], rate * dt)
                part.resources[r] -= used
                if part.resources[r] < 1e-7:
                    part.resources[r] = 0.0
                res.propellant_used_t[r] = res.propellant_used_t.get(r, 0.0) + used * part.densities.get(r, 0.0)
            m1 = _mass(alive)
            if m1 < m0 and flow > 0:
                ln = math.log(m0 / m1)
                res.dv_vac_mps += f_vac / flow * ln
                res.dv_mps += f_p / flow * ln
            res.burn_s += dt
        res.end_mass_t = _mass(alive)
        results.append(res)
    return results


def _density(p: SimPart, parts: list[SimPart] | dict, resource: str) -> float:
    d = p.densities.get(resource)
    if d is not None:
        return d
    pool = parts.values() if isinstance(parts, dict) else parts
    return next((q.densities[resource] for q in pool if resource in q.densities), 0.0)


def summarize(results: list[StageResult], gravity: float | None, keep_empty: bool = False) -> dict[str, Any]:
    rows = [r.to_dict(gravity) for r in results
            if keep_empty or r.dv_vac_mps > 0 or r.drops or r.activates]
    return {"stages": rows,
            "total_dv_vac_mps": sum(r.dv_vac_mps for r in results),
            "total_dv_mps": sum(r.dv_mps for r in results)}


# ---------------------------------------------------------------------------------------------
# Builders


def parts_from_placement(placement: Any, catalog: Any, pressure_atm: float) -> list[SimPart]:
    """SimParts for a placed design (staging must already be assigned)."""
    keyed = {id(p): f"p{i}" for i, p in enumerate(placement.parts)}
    out = []
    for p in placement.parts:
        key = keyed[id(p)]
        neighbors = [keyed[id(c)] for c in p.children]
        if p.parent is not None:
            neighbors.append(keyed[id(p.parent)])
        crossfeed = p.spec.crossfeed if p.spec.crossfeed is not None else p.info.crossfeed
        engines = []
        limit = (p.spec.thrust_limit_pct if p.spec.thrust_limit_pct is not None else 100.0) / 100.0
        for mode in p.info.engines[:1]:  # the default (first) mode is the one active at launch
            if mode.air_breathing:
                continue
            props = [(pr["name"], float(pr["ratio"]), pr.get("flow_mode") or catalog.flow_mode(pr["name"]))
                     for pr in mode.propellants]
            engines.append(SimEngine(mode.thrust_vac_kn, mode.isp_vac, mode.isp(pressure_atm), props, limit))
        amounts = {name: amount for name, (amount, _) in p.resource_amounts().items()}
        out.append(SimPart(key=key, label=p.spec.id, dry_mass_t=p.info.mass_t, resources=amounts,
                           densities={n: r["density_t"] for n, r in p.info.resources.items()},
                           neighbors=neighbors, crossfeed=crossfeed,
                           activation_stage=p.istg if p.info.engines else -1,
                           decouple_stage=_clamp_release(p.info.role, p.istg, p.dstg), engines=engines))
    # densities for propellants the engine part itself does not hold
    for sp in out:
        for e in sp.engines:
            for r, _, _ in e.propellants:
                sp.densities.setdefault(r, catalog.density(r))
    return out


def _clamp_release(role: str, activation_stage: int, decouple_stage: int) -> int:
    """Launch clamps stay on the pad when they fire, so the vessel sheds their mass in their own
    activation stage (as MechJeb's simulation does); every other part keeps its decouple stage."""
    if role == "launch_clamp" and activation_stage >= 0:
        return max(decouple_stage, activation_stage)
    return decouple_stage


_KRPC_FLOW = {"vessel": "ALL_VESSEL", "stage": "STAGE_PRIORITY_FLOW", "adjacent": "STACK_PRIORITY_SEARCH",
              "none": "NO_FLOW"}


def parts_from_vessel(vessel: Any, pressure_atm: float, current_stage: int,
                      flow_mode: Any = None, catalog: Any = None) -> list[SimPart]:
    """SimParts for a live kRPC vessel. Engines already running count as activated in
    ``current_stage``; inactive ones light at their own stage. ``flow_mode(resource) -> enum|str``
    is kRPC's ``Resources.flow_mode``. kRPC masses (kg) are converted to tonnes.

    kRPC reports Isp at a pressure only for active engines, so for the others the part's Isp curve
    comes from ``catalog`` (the engine mode whose vacuum Isp matches)."""
    all_parts = list(vessel.parts.all)
    key = {p._object_id: f"p{i}" for i, p in enumerate(all_parts)}
    modes: dict[str, str] = {}

    def mode_of(name: str) -> str:
        if name not in modes:
            try:
                raw = flow_mode(name) if flow_mode else None
            except Exception:  # noqa: BLE001
                raw = None
            text = str(raw).split(".")[-1].lower() if raw is not None else "adjacent"
            modes[name] = _KRPC_FLOW.get(text, str(raw) if raw else "STACK_PRIORITY_SEARCH")
        return modes[name]

    out = []
    for p in all_parts:
        amounts, dens = {}, {}
        for r in p.resources.all:
            amounts[r.name] = float(r.amount)
            dens[r.name] = float(r.density) / 1000.0
        neighbors = [key[c._object_id] for c in p.children if c._object_id in key]
        parent = p.parent
        if parent is not None and parent._object_id in key:
            neighbors.append(key[parent._object_id])
        sources = [key[q._object_id] for q in p.fuel_lines_from if q._object_id in key]
        engines = []
        activation = -1
        eng = p.engine
        if eng is not None:
            ratios = dict(eng.propellant_ratios)
            activation = max(int(p.stage), current_stage if eng.active else -1)
            if "IntakeAir" not in ratios and eng.max_vacuum_thrust > 0:
                isp_vac, isp_p = _live_isp(eng, p.name, pressure_atm, catalog)
                engines.append(SimEngine(
                    thrust_vac_kn=eng.max_vacuum_thrust / 1000.0, isp_vac_s=isp_vac, isp_s=isp_p,
                    propellants=[(n, float(r), mode_of(n)) for n, r in ratios.items()],
                    limit=float(eng.thrust_limit)))
        decouple = int(p.decouple_stage)
        info = catalog.resolve(p.name) if catalog is not None else None
        if info is not None and info.role == "launch_clamp":
            decouple = _clamp_release(info.role, int(p.stage), decouple)
        out.append(SimPart(key=key[p._object_id], label=f"{p.title or p.name} [{p.name}]",
                           dry_mass_t=float(p.dry_mass) / 1000.0, resources=amounts,
                           densities=dens, neighbors=neighbors, fuel_sources=sources, crossfeed=bool(p.crossfeed),
                           activation_stage=activation, decouple_stage=decouple, engines=engines))
    return out


def _live_isp(eng: Any, part_name: str, pressure_atm: float, catalog: Any) -> tuple[float, float]:
    """(vacuum Isp, Isp at ``pressure_atm``) for a live engine, active or not."""
    isp_vac = float(eng.vacuum_specific_impulse)
    isp_p = float(eng.specific_impulse_at(pressure_atm)) if eng.active else 0.0
    if isp_p > 0 and isp_vac > 0:
        return isp_vac, isp_p
    info = catalog.resolve(part_name) if catalog is not None else None
    modes = info.engines if info is not None else []
    mode = min(modes, key=lambda m: abs(m.isp_vac - isp_vac), default=None) if isp_vac > 0 else (modes[0] if modes else None)
    if mode is not None and mode.isp_vac > 0:
        scale = isp_vac / mode.isp_vac if isp_vac > 0 else 1.0
        return isp_vac or mode.isp_vac, mode.isp(pressure_atm) * scale
    asl = float(eng.kerbin_sea_level_specific_impulse)
    return isp_vac, isp_vac + (asl - isp_vac) * min(max(pressure_atm, 0.0), 1.0)


# ---------------------------------------------------------------------------------------------
# One-call entry points


def from_spec(spec: Any, catalog: Any, pressure_atm: float = 0.0, gravity_mps2: float | None = None) -> dict[str, Any]:
    """Stage table of a craft spec (dict, JSON text or CraftSpec): place, stage, simulate."""
    from astra.craft.geometry import place
    from astra.craft.spec import CraftSpec, parse_spec
    from astra.craft.staging import assign_stages

    parsed = spec if isinstance(spec, CraftSpec) else parse_spec(spec)
    placement = place(parsed, catalog)
    assign_stages(placement)
    parts = parts_from_placement(placement, catalog, pressure_atm)
    top = max([p.istg for p in placement.parts] + [p.dstg for p in placement.parts] + [0])
    return summarize(simulate(parts, top + 1), gravity_mps2)


def from_live_vessel(vessel: Any, pressure_atm: float, gravity_mps2: float | None, *, flow_mode: Any = None,
                     catalog: Any = None) -> tuple[list[StageResult], dict[str, Any]]:
    """Simulate a live kRPC vessel from its current stage; returns the raw results and a summary."""
    current = int(vessel.control.current_stage)
    results = simulate(parts_from_vessel(vessel, pressure_atm, current, flow_mode=flow_mode, catalog=catalog), current)
    return results, summarize(results, gravity_mps2)
