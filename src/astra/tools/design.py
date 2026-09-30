"""Engineering desk: find parts, analyse a part tree, write .craft files, study existing craft.

The AI designs; these tools look parts up, place them with real node geometry, work out staging,
simulate per-stage Δv/TWR, flag problems, and write the craft into the active save. Nothing here
picks parts or numbers for a mission.
"""

import math
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from astra.config import CONFIG
from astra.craft import stagesim
from astra.craft.catalog import PROPELLANT_FAMILIES, ROLES, get_catalog
from astra.craft.confignode import ConfigNode, read_text
from astra.craft.geometry import Placement, place, surface_radius
from astra.craft.spec import parse_spec
from astra.craft.staging import assign_stages
from astra.craft.writer import describe_placement, spec_from_craft, write_craft
from astra.errors import AstraError, NotConnected
from astra.ksp import ksp
from astra.registry import tool

ATM_PA = 101325.0

SPEC_HELP = (
    "Craft spec (JSON object): {\"name\": str, \"description\": one line, \"stages\": \"auto\"|\"manual\", "
    "\"vessel_type\": \"Ship\"|\"Probe\"|..., \"parts\": [...]}. Each part: {\"id\": unique, \"part\": catalog name, "
    "\"parent\": id (omit for the single root, normally the command part), then EITHER \"node\": parent node id "
    "(stack; optional \"child_node\", default = the child's node facing it) OR \"surface\": {\"azimuth_deg\": angle "
    "around the parent's axis (0 = +X, 90 = +Z), \"height_m\": along the parent's axis from its origin, optional "
    "\"radius_m\" (default: the parent's skin), optional \"tilt_deg\" (90 = top face)}; on a surface-attached parent "
    "(a radial decoupler) omit azimuth_deg to use its outer face. Optional: \"symmetry\": N (surface only; copies "
    "the part and its subtree around the parent), \"stage\": inverse stage override, \"thrust_limit_pct\", "
    "\"crossfeed\": bool (parts with a crossfeed toggle, e.g. radial decouplers), \"resources\": {name: fill 0..1}, "
    "\"fairing\": {\"clearance_m\", \"nose\": ogive|cone|blunt|flat, \"nose_length_m\"} or {\"xsections\": [[h, r], ...]}, "
    "\"tag\": name tag for finding the part in flight}.")


def _catalog():
    return get_catalog(bridge=ksp().bridge)


# ---------------------------------------------------------------------------------------------
# Parts


@tool("design", needs_game=False)
def parts_search(
    query: Annotated[str | None, Field(description="Words matched against the part name first (e.g. 'Terrier' "
                                             "matches the English title, 'liquidEngine3' the name), then the "
                                             "localized title and tags. Omit to filter only.")] = None,
    role: Annotated[str | None, Field(description=f"Role filter, one of: {', '.join(ROLES)}.")] = None,
    diameter_m: Annotated[float | None, Field(description="Stack diameter in metres the part must fit "
                                                          "(0.625, 1.25, 1.875, 2.5, 3.75, 5 ...).")] = None,
    propellant: Annotated[str | None, Field(description="Engines burning / tanks holding: LFO, LF (liquid fuel only, "
                                                        "e.g. nuclear engines), Solid, Mono, Xenon, Air.")] = None,
    min_thrust_kn: Annotated[float | None, Field(description="Minimum vacuum thrust in kN (engines).")] = None,
    surface_attachable: Annotated[bool | None, Field(description="true: only parts that can be surface-attached; "
                                                                 "false: only parts that cannot.")] = None,
    include_hidden: Annotated[bool, Field(description="Include retired (category 'none') and not-loaded parts.")] = False,
    limit: Annotated[int, Field(description="Maximum rows returned.", ge=1, le=200)] = 20,
) -> dict:
    """Search the part catalog (live game data when the bridge is up, part configs otherwise).

    Returns compact rows: name (use it in specs), English title, role, stack diameters, dry and wet
    mass (t), cost, resources, engine thrust (vac/ASL kN), Isp (vac/ASL s) and propellant ratios, node
    ids, surface attachability. Use part_info for attach-node geometry, rules and engine curves.
    """
    cat = _catalog()
    if role is not None and role not in ROLES:
        raise AstraError(f"unknown role {role!r}", f"use one of {list(ROLES)}")
    if propellant is not None and propellant.strip().lower() not in {k.lower() for k in PROPELLANT_FAMILIES}:
        raise AstraError(f"unknown propellant family {propellant!r}", f"use one of {list(PROPELLANT_FAMILIES)}")
    rows = cat.search(query, role=role, diameter_m=diameter_m, propellant=propellant, min_thrust_kn=min_thrust_kn,
                      surface_attachable=surface_attachable, include_hidden=include_hidden, limit=limit)
    return {"count": len(rows), "catalog_source": cat.source, "parts": [p.summary() for p in rows]}


@tool("design", needs_game=False)
def part_info(
    name: Annotated[str, Field(description="Part name as in parts_search (dotted live name; the cfg spelling "
                                           "with underscores also works).")],
) -> dict:
    """Everything the catalog knows about one part: nodes, attach rules, engines, decoupler, mass.

    Includes stack attach nodes (id, position m, direction, size), the surface-attach node, attach
    rules (stack/srfAttach/allowStack/allowSrfAttach...), effective crossfeed and whether it has a
    crossfeed toggle, resources with capacities and densities, every engine mode with its Isp curve
    (pressure atm -> s), ASL thrust, propellant ratios and gimbal, decoupler/parachute/command/
    reaction-wheel data, bounds, variants that move nodes, the estimated body radius, and the source.
    Node heights are measured from the part origin; use them for surface.height_m.
    """
    cat = _catalog()
    p = cat.get(name)
    d = p.to_dict()
    d["wet_mass_t"] = p.wet_mass_t
    d["diameters_m"] = p.diameters_m
    d["radius_m"] = p.radius_m
    d["propellant_family"] = p.propellant_family
    for e, row in zip(p.engines, d["engines"]):
        row.update(isp_vac_s=e.isp_vac, isp_asl_s=e.isp_asl, thrust_asl_kn=e.thrust_kn(1.0),
                   mass_flow_tps=e.mass_flow_tps)
    if not p.variants:
        d.pop("variants")
    if cat.kerbal_mass_t and p.crew:
        d["kerbal_mass_t"] = cat.kerbal_mass_t
    return d


# ---------------------------------------------------------------------------------------------
# Design analysis


def _body_constants(body: str | None, pressure_atm: float | None) -> tuple[dict[str, Any], list[str]]:
    """Surface gravity and sea-level pressure of a body, read live through kRPC."""
    notes: list[str] = []
    k = ksp()
    try:
        name = body
        if name is None:
            try:
                name = k.vessel().orbit.body.name if k.scene() == "flight" else "Kerbin"
            except NotConnected:
                raise
            except AstraError:  # flight scene without an active vessel
                name = "Kerbin"
        b = k.body(name)
    except KeyError as exc:
        raise AstraError(str(exc), "use a body name from body_info") from exc
    except NotConnected:
        notes.append("kRPC is not reachable, so body constants are unknown: TWR is omitted and the second "
                     "Δv column uses pressure_atm (vacuum if not given). Load a save to get them.")
        return {"name": body, "g_mps2": None, "pressure_atm": pressure_atm or 0.0}, notes
    g = b.surface_gravity
    sea = b.pressure_at(0.0) / ATM_PA if b.has_atmosphere else 0.0
    return {"name": b.name, "g_mps2": g, "sea_level_pressure_atm": sea,
            "pressure_atm": sea if pressure_atm is None else pressure_atm}, notes


def _analyse(spec_data: dict | str, body: str | None, pressure_atm: float | None, crew: int) -> dict[str, Any]:
    cat = _catalog()
    spec = parse_spec(spec_data)
    placement = place(spec, cat)
    table = assign_stages(placement)
    consts, notes = _body_constants(body, pressure_atm)
    g, p_atm = consts["g_mps2"], consts["pressure_atm"]

    sim_parts = stagesim.parts_from_placement(placement, cat, p_atm)
    kerbal = cat.kerbal_mass_t or 0.0
    if crew and not kerbal:
        notes.append("the catalog has no live masses, so kerbal mass is unknown; crew adds no mass here")
    seated = 0
    for sp, pl in zip(sim_parts, placement.parts):
        seats = min(crew - seated, pl.info.crew)
        sp.dry_mass_t += seats * kerbal
        seated += seats
    if seated < crew:
        notes.append(f"{crew - seated} of {crew} kerbals have no seat in this design")
    wet = sum(sp.dry_mass_t + sum(a * sp.densities.get(r, 0.0) for r, a in sp.resources.items()) for sp in sim_parts)
    top = max([p.istg for p in placement.parts] + [p.dstg for p in placement.parts] + [0])
    results = stagesim.simulate(sim_parts, top + 1)  # consumes the SimParts' resources
    rows = _merge(table, results, g)

    ext = placement.extents()
    com = placement.com()
    warnings = placement.warnings + _warnings(placement, results, rows, cat)
    return {
        "spec": spec, "placement": placement, "notes": notes,
        "report": {
            "name": spec.name, "parts": len(placement.parts), "wet_mass_t": wet,
            "dry_mass_t": sum(p.info.mass_t for p in placement.parts) + seated * kerbal,
            "height_m": ext["height_m"], "max_diameter_m": ext["max_diameter_m"],
            "com_height_m": com[1] - ext["bottom_y"],
            "body": consts, "stages": rows,
            "total_dv_vac_mps": sum(r.dv_vac_mps for r in results),
            "total_dv_mps": sum(r.dv_mps for r in results),
            "warnings": warnings,
        },
    }


def _merge(table: list[dict[str, Any]], results: list[stagesim.StageResult], g: float | None) -> list[dict[str, Any]]:
    by_stage = {r.stage: r for r in results}
    rows = []
    for t in table:
        row = dict(t)
        r = by_stage.get(t["stage"])
        if r is not None:
            sim = r.to_dict(g)
            for key in ("activates", "drops", "stage"):
                sim.pop(key, None)
            row.update(sim)
        rows.append(row)
    known = {t["stage"] for t in table}
    for r in results:
        if r.stage not in known and r.dv_vac_mps > 0:
            sim = r.to_dict(g)
            sim["note"] = "engines still burning from earlier stages"
            rows.append(sim)
    rows.sort(key=lambda x: -x["stage"])
    return rows


def _warnings(placement: Placement, results: list[stagesim.StageResult], rows: list[dict[str, Any]],
              cat: Any) -> list[str]:
    out: list[str] = []
    parts = placement.parts
    seen: set[str] = set()

    def once(key: str, text: str) -> None:
        if key not in seen:
            seen.add(key)
            out.append(text)

    for p in parts:
        s = p.spec.surface
        if p.attach == "surface" and s is not None and s.radius_m is not None and abs(s.tilt_deg) < 45:
            skin = surface_radius(p.parent.info, s.height_m)[0] if s.azimuth_deg is not None else None
            if skin is not None and s.radius_m > skin + 0.1:
                once(f"float:{p.spec.id}", f"{p.spec.id} floats {s.radius_m - skin:.2f} m off {p.parent.spec.id}'s "
                     f"skin (radius_m {s.radius_m:g} vs {skin:.2f}): KSP joins it anyway, but it will look and fly odd")
        if p.attach == "stack":
            pn, cn = p.parent.info.node(p.parent_node), p.info.node(p.child_node)
            if pn and cn and pn.size != cn.size:
                once(f"size:{p.spec.id}", f"{p.spec.id} ({p.info.name}, node size {cn.diameter_m} m) sits on "
                     f"{p.parent.spec.id}'s {pn.diameter_m} m node: a diameter step (use an adapter if unintended)")
        if p.spec.thrust_limit_pct is not None and p.spec.thrust_limit_pct < 100:
            once(f"limit:{p.spec.id}", f"{p.spec.id}: thrust limiter {p.spec.thrust_limit_pct:g} %")
        if p.spec.tag is not None and p.info.modules and "KOSNameTag" not in p.info.modules:
            once(f"tag:{p.spec.id}", f"{p.spec.id}: {p.info.name} has no KOSNameTag module (kRPC/kOS add it), so "
                 f"the tag {p.spec.tag!r} will not show in flight; find the part by name or idx instead")
        if p.info.role == "radial_decoupler" and not p.children:
            once(f"empty:{p.spec.id}", f"{p.spec.id} ({p.info.name}) holds nothing: make the booster/pod a child of "
                 "the decoupler (parent = the decoupler), or it will stay on the core when the decoupler fires")
        if p.info.role == "heat_shield" and p.info.decoupler is not None:
            once(f"shield:{p.spec.id}", f"info: {p.spec.id} ({p.info.name}) can be released, but KSP ships heat-shield "
                 "decouplers with staging disabled: it is not in the stage stack; jettison it with control_part "
                 "(action decouple) if you want it gone")
        elif p.info.stageable and p.istg < 0 and p.info.role not in ("docking_port",):
            once(f"unstaged:{p.spec.id}", f"{p.spec.id} ({p.info.role}) is stageable but in no stage; give it 'stage'")
    for p in parts:  # a pod hung on the core next to its decoupler instead of on it
        decs = [c for c in p.children if c.attach == "surface" and c.info.role == "radial_decoupler"]
        for o in p.children:
            if o.attach != "surface" or o.info.role == "radial_decoupler" or not (
                    o.info.engines or any(a > 0 for a, _ in o.resource_amounts().values())):
                continue
            for d in decs:
                gap = abs((o.azimuth_deg - d.azimuth_deg + 180.0) % 360.0 - 180.0)
                if gap < 15.0 and _radial(o, p) > _radial(d, p):
                    once(f"beside:{o.spec.id}", f"{o.spec.id} hangs on {p.spec.id} beside {d.spec.id} instead of on it: "
                         f"make {d.spec.id} its parent, or firing {d.spec.id} will not release it")
    command = [p for p in parts if p.info.role in ("command_pod", "probe_core")]
    if not command:
        out.append("no command part (pod or probe core): the craft cannot be controlled")
    elif all(p.info.role == "command_pod" for p in command):
        out.append("control comes only from crewed pods: launch with crew (game_launch crew=...) or add a probe core")
    root = placement.root
    if root.info.role not in ("command_pod", "probe_core"):
        out.append(f"the root ({root.spec.id}) is not a command part: after separations KSP keeps the root's side "
                   "as the vessel, so make the command part the root")
    elif all(p.dstg >= 0 for p in command):
        out.append("every command part is dropped at some stage: control is lost afterwards")

    burned = {label for r in results for label in r.engines}
    for p in parts:
        if p.info.engines and p.spec.id not in burned and not p.info.engines[0].air_breathing:
            once(f"dry:{p.spec.id}", f"{p.spec.id} ({p.info.name}) never gets propellant in the simulation: check its "
                 "stage and that a tank reaches it (decouplers block fuel unless crossfeed is on)")
        if p.info.engines:
            props = set(p.info.engines[0].propellant_names)
            if props == {"LiquidFuel"}:
                ox = _reachable_amount(p, placement, "Oxidizer")
                if ox > 0:
                    once(f"lf:{p.spec.id}", f"{p.spec.id} ({p.info.name}) burns LiquidFuel only, but its tanks carry "
                         f"{ox:.0f} units of Oxidizer it cannot use (dead mass): use LF-only tanks")
    for p in parts:
        if p.info.role == "radial_decoupler" and p.children:
            sub = placement.subtree(p)[1:]
            has_engine = any(q.info.engines for q in sub)
            has_fuel = any(amount > 0 for q in sub for name, (amount, _) in q.resource_amounts().items()
                           if name in ("LiquidFuel", "Oxidizer"))
            crossfeed = p.spec.crossfeed if p.spec.crossfeed is not None else p.info.crossfeed
            if has_fuel and not has_engine and not crossfeed:
                once(f"drop:{p.spec.id}", f"{p.spec.id}: the tanks it holds have no engine and crossfeed is off, so "
                     "they feed nothing: set \"crossfeed\": true on the decoupler")
    out.extend(_steering_warnings(placement, results))
    out.extend(_power_warnings(placement))
    for r in rows:
        if "warning" in r:
            out.append(f"stage {r['stage']}: {r['warning']}")
    launch = next((r for r in rows if r.get("engines")), None)
    if launch is not None and launch.get("twr_start") is not None and launch["twr_start"] < 1.0:
        out.append(f"launch stage {launch['stage']} TWR {launch['twr_start']:.2f} < 1: it will not lift off here")
    ext = placement.extents()
    if ext["max_diameter_m"] > 0 and ext["height_m"] / ext["max_diameter_m"] > 15:
        out.append(f"info: slender stack (height/diameter {ext['height_m'] / ext['max_diameter_m']:.1f}); "
                   "watch for flex and aerodynamic instability")
    return out


def _steering_warnings(placement: Placement, results: list[stagesim.StageResult]) -> list[str]:
    """Stages that burn without any gimbaling engine rely on reaction wheels (and fins) alone."""
    label_to_part = {p.spec.id: p for p in placement.parts}
    wheel_knm = sum(max(p.info.reaction_wheel.get("pitch_knm", 0.0), p.info.reaction_wheel.get("yaw_knm", 0.0))
                    for p in placement.parts if p.info.reaction_wheel)
    out = []
    for r in results:
        if r.dv_vac_mps <= 0 or not r.engines:
            continue
        engines = [label_to_part.get(lbl.split(" x")[0]) for lbl in r.engines]
        engines = [e for e in engines if e is not None and e.info.engines]
        if engines and all((e.info.engines[0].gimbal_deg or 0.0) <= 0.0 for e in engines):
            out.append(f"stage {r.stage}: none of its engines gimbal ({', '.join(sorted({e.info.name for e in engines}))}); "
                       f"steering relies on {wheel_knm:.1f} kN*m of reaction wheels for {r.start_mass_t:.1f} t. "
                       "An atmospheric stage like this may not follow a gravity turn: add a gimbaled engine, "
                       "more wheel torque, or control surfaces")
    return out


def _power_warnings(placement: Placement) -> list[str]:
    """Stored ElectricCharge with no way to make more: wheels and probe cores drain it on a long coast."""
    # The long coasts happen after the last engine stage lights: judge the vessel that remains then
    # (a booster's alternator is gone by the time the craft coasts in orbit).
    engine_stages = [p.istg for p in placement.parts if p.info.engines and p.istg >= 0]
    last = min(engine_stages) if engine_stages else 0
    kept = [p for p in placement.parts if p.dstg < last]
    stored = sum(amount for p in kept for name, (amount, _) in p.resource_amounts().items()
                 if name == "ElectricCharge")
    makers = [p for p in kept
              if p.info.role in ("solar_panel", "generator")
              or any(m in ("ModuleAlternator", "ModuleGenerator", "ModuleDeployableSolarPanel")
                     for m in (p.info.modules or []))]
    if stored > 0 and not makers:
        return [f"power: after stage {last} lights, the vessel carries {stored:.0f} ElectricCharge and nothing "
                "generates more (no solar panel, generator, or alternator on the engines still attached). "
                "Reaction wheels and probe cores drain it within minutes of attitude "
                "holding; after that only thrust-vectoring steers. Add batteries plus solar panels, an RTG, or "
                "an engine with an alternator for any coast longer than a few minutes"]
    return []


def _radial(part: Any, parent: Any) -> float:
    return math.hypot(part.pos[0] - parent.pos[0], part.pos[2] - parent.pos[2])


def _reachable_amount(engine_part: Any, placement: Placement, resource: str) -> float:
    """Units of ``resource`` in parts reachable from the engine through crossfeed (design data)."""
    seen = {id(engine_part)}
    todo = [engine_part]
    total = 0.0
    while todo:
        p = todo.pop()
        total += p.resource_amounts().get(resource, (0.0, 0.0))[0]
        crossfeed = p.spec.crossfeed if p.spec.crossfeed is not None else p.info.crossfeed
        if p is not engine_part and not crossfeed:
            continue
        for q in p.children + ([p.parent] if p.parent is not None else []):
            if id(q) not in seen:
                seen.add(id(q))
                todo.append(q)
    return total


@tool("design")
def design_check(
    spec: Annotated[dict[str, Any] | str, Field(description=SPEC_HELP)],
    body: Annotated[str | None, Field(description="Body for TWR and the pressure column, read live. Default: the "
                                                  "active vessel's body in flight, otherwise Kerbin.")] = None,
    pressure_atm: Annotated[float | None, Field(description="Ambient pressure in atm for the second Δv/thrust "
                                                            "column (e.g. the pressure where a stage will burn). "
                                                            "Default: the body's sea-level pressure.", ge=0)] = None,
    crew: Annotated[int, Field(description="Kerbals you will seat at launch; each adds its mass to the "
                                           "crewed parts in spec order.", ge=0)] = 0,
    detail: Annotated[Literal["brief", "full"], Field(description="'full' adds per-part positions and "
                                                                  "attachments and the normalized spec.")] = "brief",
) -> dict:
    """Build the part tree, staging and stage table for a craft spec, without writing anything.

    Returns part count, wet/dry mass (t), height, max diameter, centre-of-mass height above the
    bottom, the stage table in firing order (what each stage activates and drops, with simulated
    Δv in vacuum and at the chosen pressure, burn time, thrust, Isp, and TWR at stage start/end on
    the body), totals, and warnings (diameter steps, empty radial decouplers, unreachable propellant,
    LF-only engines on LF/Ox tanks, drop tanks without crossfeed, separators sharing a stage, thrust
    limiters, no control source, launch TWR < 1). Spec problems raise an error listing every issue.
    Stage numbers are KSP's inverse stages: the highest fires first, 0 last.
    """
    result = _analyse(spec, body, pressure_atm, crew)
    report = result["report"]
    if result["notes"]:
        report["notes"] = result["notes"]
    if detail == "full":
        report["placement"] = describe_placement(result["placement"])
        report["spec"] = result["spec"].to_dict()
    report["ok"] = True
    return report


def _active_save() -> str:
    try:
        state = ksp().bridge.get("/state", timeout=10)
    except NotConnected as exc:
        raise AstraError("cannot tell which save is active: the bridge is not answering",
                         "start the game with the bridge (game_status); the craft must go into the active "
                         "save's Ships/VAB folder") from exc
    save = str(state.get("saveFolder") or "").strip()
    if not save:
        raise AstraError("no save is loaded", "load a save first (game_load_save)")
    if any(c in save for c in "/\\:") or save in (".", ".."):
        raise AstraError(f"the bridge reports an unusable save folder {save!r}",
                         "check game_status; the save folder must be a plain folder name under saves/")
    return save


@tool("design")
def design_build(
    spec: Annotated[dict[str, Any] | str, Field(description=SPEC_HELP)],
    overwrite: Annotated[bool, Field(description="Replace an existing craft file with the same name.")] = False,
    body: Annotated[str | None, Field(description="Body for the TWR/pressure columns of the returned check "
                                                  "(see design_check).")] = None,
    pressure_atm: Annotated[float | None, Field(description="Pressure in atm for the second Δv column "
                                                            "(see design_check).", ge=0)] = None,
    crew: Annotated[int, Field(description="Kerbals you will seat (see design_check).", ge=0)] = 0,
) -> dict:
    """Check a craft spec and write it as <active save>/Ships/VAB/<name>.craft.

    Runs design_check (spec errors abort before writing) and writes a .craft with real node
    geometry, the resolved staging, symmetry links, resources, and only the module settings the spec
    sets (thrust limiter, crossfeed, fairing shell, name tag); every other module comes from the part
    prefabs. Returns the path, the stage table, totals, warnings and notes (e.g. computed fairing
    sections). Launch it with game_launch(craft=<name>), then compare vessel_stages with this table.
    """
    result = _analyse(spec, body, pressure_atm, crew)
    placement: Placement = result["placement"]
    save = _active_save()
    path = CONFIG.saves_dir / save / "Ships" / "VAB" / f"{placement.spec.name}.craft"
    written, notes = write_craft(placement, path, overwrite=overwrite)
    report = result["report"]
    report["notes"] = result["notes"] + notes
    report.update(path=str(written), save=save, craft=placement.spec.name)
    return report


# ---------------------------------------------------------------------------------------------
# Existing craft


def _craft_dirs() -> list[tuple[str, Path]]:
    dirs: list[tuple[str, Path]] = []
    try:
        save = _active_save()
        dirs += [(f"save:{save}", CONFIG.saves_dir / save / "Ships" / fac) for fac in ("VAB", "SPH")]
    except AstraError:
        pass
    dirs += [("stock", CONFIG.ksp_dir / "Ships" / fac) for fac in ("VAB", "SPH")]
    return dirs


def _find_craft(craft: str) -> Path:
    p = Path(craft)
    if p.suffix == ".craft" and p.exists():
        return p
    wanted = craft.removesuffix(".craft").strip().lower()
    for _, d in _craft_dirs():
        for f in sorted(d.glob("*.craft")) if d.is_dir() else []:
            if f.stem.lower() == wanted:
                return f
    for f in sorted(CONFIG.saves_dir.glob("*/Ships/*/*.craft")):
        if f.stem.lower() == wanted:
            return f
    raise AstraError(f"no craft named {craft!r}", "craft_list shows the available craft; a full .craft path also works")


@tool("design", needs_game=False)
def design_from_craft(
    craft: Annotated[str, Field(description="Craft name (searched in the active save's VAB/SPH, the stock Ships "
                                            "folders, then all saves) or a path to a .craft file.")],
) -> dict:
    """Read an existing .craft (stock or saved) into a craft spec you can study, modify and build.

    Symmetric copies collapse into one part with symmetry N; stack parts keep their nodes, surface
    parts get azimuth/height (radius/tilt only when they differ from the defaults); saved stage
    numbers, thrust limiters, crossfeed, fill levels, fairing sections and name tags carry over.
    Notes list what could not be expressed exactly: retired part names mapped to successors, fuel
    lines (dropped), and parts rotated or offset by hand. Rename the spec before design_build.
    """
    path = _find_craft(craft)
    cat = _catalog()
    spec, notes, _ = spec_from_craft(ConfigNode.load(path), cat)
    return {"source": str(path), "parts": len(spec["parts"]), "notes": notes, "spec": spec}


@tool("design")
def craft_list() -> dict:
    """List craft files that can be launched: the active save's VAB/SPH craft and the stock craft.

    Each entry: name (for game_launch / design_from_craft), facility, part count, file modified time.
    """
    out: dict[str, Any] = {}
    for label, d in _craft_dirs():
        rows = []
        for f in sorted(d.glob("*.craft")) if d.is_dir() else []:
            try:
                text = read_text(f)
            except OSError:
                continue
            rows.append({"name": f.stem, "facility": d.name, "parts": len(re.findall(r"^PART\s*$", text, re.M)),
                         "modified": f.stat().st_mtime})
        out.setdefault(label.split(":")[0] + "_craft", []).extend(rows)
        if label.startswith("save:"):
            out["active_save"] = label.split(":", 1)[1]
    for rows in out.values():
        if isinstance(rows, list):
            for r in rows:
                r["modified"] = _iso(r["modified"])
    return out


def _iso(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------------------------
# Live vessel stages


@tool("observe")
def vessel_stages() -> dict:
    """Per-stage Δv, TWR and burn time of the active vessel, simulated over its live parts.

    For the current stage and every later one (KSP inverse numbers, counting down): mass at start
    and end (t), Δv in vacuum and at the current static pressure (m/s), burn time (s), thrust (kN,
    vac and here), Isp, TWR at stage start/end in the local gravity (here and with vacuum thrust),
    engines burning, parts activated and dropped. Fuel flows by KSP's rules (crossfeed, fuel lines,
    stack priority), so a tank the engines cannot reach adds no Δv. When MechJeb is present its own
    stage stats are included for comparison (they may read 'pending' on the first call).
    """
    k = ksp()
    v = k.vessel()
    body = v.orbit.body
    radius = v.orbit.radius
    g_here = body.gravitational_parameter / (radius * radius)
    pressure = v.flight().static_pressure / ATM_PA
    current = v.control.current_stage
    sc = k.sc
    cat = _catalog()

    def flow_mode(name: str) -> Any:
        try:
            return sc.Resources.flow_mode(name)
        except Exception:  # noqa: BLE001 — fall back to the resource definitions on disk
            return cat.flow_mode(name)

    results, _ = stagesim.from_live_vessel(v, pressure, g_here, flow_mode=flow_mode, catalog=cat)
    rows = []
    for r in results:
        if r.dv_vac_mps <= 0 and not r.drops and not r.activates and r.stage != current:
            continue
        row = r.to_dict(g_here)
        if r.engines:
            row["twr_vac_start"] = r.thrust_vac_kn / (r.start_mass_t * g_here)
        rows.append(row)
    out: dict[str, Any] = {
        "vessel": v.name, "current_stage": current, "body": body.name, "pressure_atm": pressure,
        "local_g_mps2": g_here, "mass_t": v.mass / 1000.0, "stages": rows,
        "total_dv_vac_mps": sum(r.dv_vac_mps for r in results),
        "total_dv_mps": sum(r.dv_mps for r in results),
    }
    try:
        mj = k.bridge.get("/mj-stage-stats", timeout=10)
        if mj.get("hasStageStats") or mj.get("vacStats"):
            out["mechjeb"] = {
                "pending": mj.get("pending"),
                "vac": [{"stage": s.get("kspStage"), "dv_mps": s.get("deltaV"), "burn_s": s.get("burnTime"),
                         "start_mass_t": s.get("startMass"), "end_mass_t": s.get("endMass"),
                         "twr_start": s.get("startTwr")} for s in mj.get("vacStats") or []],
                "atmo": [{"stage": s.get("kspStage"), "dv_mps": s.get("deltaV"), "twr_start": s.get("startTwr")}
                         for s in mj.get("atmoStats") or []],
            }
    except Exception:  # noqa: BLE001 — MechJeb stats are optional
        pass
    return out

