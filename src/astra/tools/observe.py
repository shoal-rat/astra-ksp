"""Instruments: telemetry, vessel structure, bodies, orbits, targets, and a camera.

This module also holds the resolvers the other tool modules share: `match_names` / `pick_name`
(localization-tolerant name matching), `resolve_vessel`, `resolve_part`, `part_index`, and the
`orbit_summary` / `patch_chain` readers used wherever an orbit is reported.
"""

import base64
import json
import math
import re
import time
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from pydantic import Field

from astra import telemetry as tm
from astra.config import CONFIG
from astra.errors import AstraError, BridgeError, NotConnected, WrongScene
from astra.ksp import ksp
from astra.registry import Picture, tool


def enum_name(x: Any) -> str:
    """'VesselSituation.orbiting' -> 'orbiting'."""
    return str(x).split(".")[-1]


def finite(x: Any) -> float | None:
    """The value as a float, or None when it is NaN/inf (KSP's 'not applicable')."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 — optional readouts never fail the tool
        return default


# ---------------------------------------------------------------------------------------------
# Name matching (vessels, crafts, kerbals). KSP here runs zh-cn: launched vessels get suffixes such
# as 'AI-Eve-Crew 飞船', so exact matching strands vessels. Tiers go from strict to loose and the
# first tier that matches anything wins; more than one hit is an error that lists the candidates.

_SUFFIXES = ("宇宙飞船", "飞船", "探测器", "卫星", "空间站", "着陆器", "中继", "漫游车", "基地", "碎片",
             "spacecraft", "probe", "ship", "vessel", "relay", "lander", "station", "rover", "base",
             "debris")
_SEPARATORS = " -_:()[]#\u3000"  # 　 = ideographic space


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _strip_suffix(text: str) -> str:
    """Drop one trailing localization/vessel-type suffix from a normalized name."""
    for suf in _SUFFIXES:
        if text.endswith(suf) and len(text) > len(suf):
            head = text[: -len(suf)]
            # ASCII suffixes must be set off ('X Probe'); CJK ones may be glued on ('X飞船').
            if not suf.isascii() or head[-1] in _SEPARATORS:
                return head.rstrip(_SEPARATORS)
    return text


def _prefix_at_separator(name: str, prefix: str) -> bool:
    return name.startswith(prefix) and (len(name) == len(prefix) or name[len(prefix)] in _SEPARATORS)


def match_names(names: list[str], wanted: str) -> list[int]:
    """Indices of `names` matching `wanted`: exact, then case/space-insensitive, then ignoring a
    localization suffix, then prefix ending at a separator ('AI-Relay-1' matches 'AI-Relay-1 X' but
    not 'AI-Relay-12'), then substring. The first tier with any hit wins."""
    w = _norm(wanted)
    if not w:
        return []
    ws = _strip_suffix(w)
    normed = [_norm(n) for n in names]
    tiers: tuple[Callable[[int], bool], ...] = (
        lambda i: names[i] == wanted,
        lambda i: normed[i] == w,
        lambda i: _strip_suffix(normed[i]) == ws,
        lambda i: _prefix_at_separator(normed[i], w),
        lambda i: w in normed[i],
    )
    for test in tiers:
        hits = [i for i in range(len(names)) if test(i)]
        if hits:
            return hits
    return []


def pick_name(names: list[str], wanted: str, what: str,
              describe: Callable[[int], str] | None = None) -> int:
    """Index of the single name matching `wanted`, or an AstraError listing candidates.
    `describe(i)` labels ambiguous hits (it may cost RPCs; it is not used for the full listing)."""
    label = describe or (lambda i: names[i])
    hits = match_names(names, wanted)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        if not names:
            raise AstraError(f"no {what} matches {wanted!r}: there are none", None)
        shown = "; ".join(names[:40])
        more = f" (+{len(names) - 40} more)" if len(names) > 40 else ""
        raise AstraError(f"no {what} matches {wanted!r}", f"known: {shown}{more}")
    raise AstraError(f"{wanted!r} is ambiguous: {len(hits)} {what}s match",
                     "use the exact name or id of one of: " + "; ".join(label(i) for i in hits[:20]))


def vessel_id(v: Any) -> str:
    """A handle for a vessel that stays valid for this game session ('#<kRPC object id>')."""
    return f"#{v._object_id}"


def describe_vessel(v: Any) -> str:
    bits = [_safe(lambda: enum_name(v.type), "?"), _safe(lambda: enum_name(v.situation), "?"),
            _safe(lambda: v.orbit.body.name, "?")]
    return f"{_safe(lambda: v.name, '?')} ({vessel_id(v)}, {', '.join(bits)})"


def resolve_vessel(ref: str, *, exclude: Any = None):
    """A vessel by '#id' (from game_list_vessels) or tolerant name. `exclude` drops one vessel
    (usually the active one) from the candidates."""
    ref = (ref or "").strip()
    vessels = [v for v in ksp().sc.vessels if exclude is None or v != exclude]
    if re.fullmatch(r"#\d+", ref):  # stock craft names can themselves start with '#' ('#autoLOC_501232')
        oid = int(ref[1:])
        for v in vessels:
            if v._object_id == oid:
                return v
        raise AstraError(f"no vessel with id {ref}",
                         "ids come from game_list_vessels and are valid for the current game session")
    names = [v.name for v in vessels]
    return vessels[pick_name(names, ref, "vessel", lambda j: describe_vessel(vessels[j]))]


# ---------------------------------------------------------------------------------------------
# Bridge handles. kRPC 0.5.4 exposes no persistentId, but the bridge addresses vessels and parts by
# it (names repeat: two launches of one craft, debris). These map kRPC objects onto bridge ids.


def _enum_key(text: Any) -> str:
    """'SUB_ORBITAL' / 'sub_orbital' / 'SpaceObject' -> 'suborbital' / 'spaceobject'."""
    return str(text or "").split(".")[-1].replace("_", "").casefold()


def bridge_vessel_id(v: Any) -> int | None:
    """The bridge persistentId of kRPC vessel `v`: by exact name, then by type, situation and body
    when names repeat. None when the bridge is down or the vessel stays ambiguous."""
    bridge = ksp().bridge
    if not bridge.up():
        return None
    try:
        listing = bridge.get("/vessels", timeout=15.0).get("vessels") or []
    except (BridgeError, NotConnected):
        return None
    name = v.name
    rows = [r for r in listing if r.get("name") == name]
    if len(rows) > 1:
        want = (_enum_key(v.type), _enum_key(v.situation), _safe(lambda: v.orbit.body.name))
        rows = [r for r in rows if (_enum_key(r.get("type")), _enum_key(r.get("situation")), r.get("body")) == want]
    return int(rows[0]["persistentId"]) if len(rows) == 1 and rows[0].get("persistentId") is not None else None


def bridge_vessel_ref(v: Any) -> dict:
    """Bridge request fields naming vessel `v`: its persistentId when known, else its exact name."""
    pid = bridge_vessel_id(v)
    return {"persistentId": pid} if pid is not None else {"name": v.name}


def bridge_parts(vessel_query: dict | None = None) -> list[dict]:
    """The bridge's part list of a loaded vessel (default: the active one). Its `index` is the
    position in kRPC's parts.all, so it matches vessel_parts idx."""
    from urllib.parse import urlencode

    path = "/vessel-parts" + ("?" + urlencode(vessel_query) if vessel_query else "")
    return ksp().bridge.get(path, timeout=15.0).get("parts") or []


def bridge_part_id(rows: list[dict], idx: int, expect_name: str) -> int:
    """persistentId of the part at kRPC idx in a bridge part list, checking both lists agree."""
    row = next((r for r in rows if r.get("index") == idx), None)
    if row is None or row.get("name") != expect_name or row.get("persistentId") is None:
        raise AstraError(f"the bridge's part list does not match kRPC's at idx {idx} ({expect_name})",
                         "the vessel changed between reads; list the parts again and retry")
    return int(row["persistentId"])


# ---------------------------------------------------------------------------------------------
# Parts: `idx` is the position in vessel.parts.all. It is stable until the vessel's part list changes.


def part_index(parts: list[Any]) -> dict[int, int]:
    """kRPC object id -> idx, so module wrappers (engine.part etc.) map to idx without RPCs."""
    return {p._object_id: i for i, p in enumerate(parts)}


def resolve_part(vessel: Any, ref: int | str, parts: list[Any] | None = None) -> tuple[Any, int]:
    """(part, idx) for `ref`: an idx from vessel_parts, or a part name / name tag / title. Raises
    AstraError listing the candidates (with idx) when a name matches several parts."""
    parts = parts if parts is not None else vessel.parts.all
    if isinstance(ref, bool):
        raise AstraError("part must be an idx or a name, not a boolean", None)
    if isinstance(ref, str) and re.fullmatch(r"\s*-?\d+\s*", ref):
        ref = int(ref)
    if isinstance(ref, int):
        if 0 <= ref < len(parts):
            return parts[ref], ref
        raise AstraError(f"part idx {ref} is out of range (the vessel has {len(parts)} parts)",
                         "idx values come from vessel_parts and change after staging, decoupling "
                         "or docking; list the parts again")
    want = ref.strip()
    idx_of = part_index(parts)
    finders = (lambda: vessel.parts.with_name(want), lambda: vessel.parts.with_tag(want),
               lambda: vessel.parts.with_title(want))
    hits: list[int] = []
    for find in finders:
        hits = sorted(idx_of[p._object_id] for p in find() if p._object_id in idx_of)
        if hits:
            break
    if not hits:  # case-insensitive name or tag
        w = want.casefold()
        hits = [i for i, p in enumerate(parts)
                if p.name.casefold() == w or (_safe(lambda p=p: p.tag, "") or "").casefold() == w]
    if len(hits) == 1:
        return parts[hits[0]], hits[0]
    if not hits:
        raise AstraError(f"no part is named or tagged {ref!r}",
                         "vessel_parts lists idx, internal name and tag for every part; titles are "
                         "localized, so prefer the internal name (e.g. 'liquidEngine2.v2') or the idx")
    listing = "; ".join(f"idx {i}: {parts[i].name} (stage {parts[i].stage}, decouple_stage "
                        f"{parts[i].decouple_stage})" for i in hits[:24])
    raise AstraError(f"{len(hits)} parts match {ref!r}", f"pass the idx of the one you mean: {listing}")


def _engine_state(e: Any) -> dict:
    return {"active": e.active, "has_fuel": e.has_fuel, "thrust_kn": e.thrust / 1e3,
            "available_thrust_kn": e.available_thrust / 1e3,
            "max_vacuum_thrust_kn": e.max_vacuum_thrust / 1e3,
            "thrust_limit_pct": e.thrust_limit * 100.0, "isp_vac_s": e.vacuum_specific_impulse,
            "isp_kerbin_asl_s": e.kerbin_sea_level_specific_impulse,
            "solid": e.throttle_locked, "can_restart": e.can_restart, "can_shutdown": e.can_shutdown,
            "propellants": list(e.propellant_names)}


# kind -> (Parts attribute listing the module wrappers, state reader)
_KINDS: dict[str, tuple[str, Callable[[Any], dict]]] = {
    "engine": ("engines", _engine_state),
    "decoupler": ("decouplers", lambda d: {"decoupled": d.decoupled, "staged": d.staged,
                                           "omni": d.is_omni_decoupler, "impulse_ns": d.impulse}),
    "parachute": ("parachutes", lambda c: {"state": enum_name(c.state),
                                           "deploy_altitude_m": c.deploy_altitude,
                                           "deploy_min_pressure_atm": c.deploy_min_pressure}),
    "leg": ("legs", lambda x: {"state": enum_name(x.state), "grounded": x.is_grounded}),
    "wheel": ("wheels", lambda x: {"state": enum_name(x.state), "grounded": x.grounded}),
    "solar_panel": ("solar_panels", lambda x: {"state": enum_name(x.state), "energy_flow": x.energy_flow}),
    "antenna": ("antennas", lambda x: {"state": enum_name(x.state), "power": x.power}),
    "radiator": ("radiators", lambda x: {"state": enum_name(x.state)}),
    "fairing": ("fairings", lambda x: {"jettisoned": x.jettisoned}),
    "docking_port": ("docking_ports", lambda x: {"state": enum_name(x.state), "shielded": x.shielded}),
    "launch_clamp": ("launch_clamps", lambda x: {"present": True}),
    "rcs": ("rcs", lambda x: {"enabled": x.enabled, "has_fuel": x.has_fuel}),
    "reaction_wheel": ("reaction_wheels", lambda x: {"active": x.active, "broken": x.broken}),
    "cargo_bay": ("cargo_bays", lambda x: {"state": enum_name(x.state)}),
    "intake": ("intakes", lambda x: {"open": x.open}),
    "light": ("lights", lambda x: {"active": x.active}),
    "experiment": ("experiments", lambda x: {"name": x.name, "has_data": x.has_data,
                                             "available": x.available, "inoperable": x.inoperable}),
}
PART_KINDS = tuple(_KINDS) + ("command", "tank")
_PROPELLANTS = set(tm.PROPELLANTS)


def part_module(part: Any, kind: str) -> Any:
    """The kRPC wrapper of module `kind` on `part` (e.g. part.engine), or None."""
    if kind == "experiment":
        found = part.experiments
        return found[0] if found else None
    return getattr(part, kind)


def module_state(part: Any, kind: str) -> dict | None:
    """Live state of one module kind on a part, as vessel_parts reports it (None if absent)."""
    m = part_module(part, kind)
    return None if m is None else _KINDS[kind][1](m)


def _module_states(vessel: Any, idx_of: dict[int, int], kinds: list[str]) -> tuple[dict[int, dict], list[str]]:
    """({idx: {kind: state}}, read errors). A part lost mid-read is reported, not fatal."""
    out: dict[int, dict] = {}
    errors: list[str] = []
    for kind in kinds:
        attr, read = _KINDS[kind]
        for m in getattr(vessel.parts, attr):
            try:
                i = idx_of.get(m.part._object_id)
                if i is None:
                    continue
                if kind == "experiment":  # a part can carry several
                    out.setdefault(i, {}).setdefault(kind, []).append(read(m))
                else:
                    out.setdefault(i, {})[kind] = read(m)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{kind}: {exc}")
    return out, errors


def _part_resources(vessel: Any, idx_of: dict[int, int],
                    only: set[int] | None = None) -> dict[int, dict[str, list[float]]]:
    out: dict[int, dict[str, list[float]]] = {}
    for r in vessel.resources.all:
        i = idx_of.get(r.part._object_id)
        if i is not None and (only is None or i in only):
            out.setdefault(i, {})[r.name] = [r.amount, r.max]
    return out


@tool("observe")
def telemetry(
    detail: Annotated[Literal["brief", "full"], Field(
        description="'brief' for the flight state you need every step; 'full' adds engines, hottest "
                    "parts, resources, crew, SAS/speed modes, comms and biome.")] = "brief",
) -> dict:
    """Flight-state snapshot of the active vessel: position, velocity, orbit, attitude, aero, propulsion, controls.

    Units: metres, m/s, seconds, degrees, kN, tonnes; `ut` is universal time. Surface speeds are
    measured against the rotating body (never the co-moving frame that reads ~0). Also reports the
    time-warp state and whether the vessel can be controlled (`control.state`: full/partial/none).
    `stage_dv_rough_mps` is a single-Isp estimate; use vessel_stages for a real stage table.
    """
    k = ksp()
    k.vessel()  # scene / vessel guard with hints
    try:
        snap = tm.snapshot(detail)
    except AstraError:
        raise
    except Exception:  # noqa: BLE001 — stale streams after staging or a switch: rebuild once
        tm.probe().invalidate()
        snap = tm.snapshot(detail)
    v, sc = k.vessel(), k.sc
    snap["warp"] = {"rails_factor": _safe(lambda: sc.rails_warp_factor),
                    "physics_factor": _safe(lambda: sc.physics_warp_factor),
                    "rate": _safe(lambda: sc.warp_rate)}
    snap["control"]["state"] = _safe(lambda: enum_name(v.control.state))
    snap["control"]["source"] = _safe(lambda: enum_name(v.control.source))
    if (snap["propulsion"].get("throttle") or 0) == 0 and (snap["propulsion"].get("thrust_kn") or 0) > 0.5:
        snap["note"] = "thrust with throttle 0: solid or unthrottleable engines are burning"
    return snap


@tool("observe")
def vessel_parts(
    kind: Annotated[str | None, Field(
        description="Only parts with this module: " + ", ".join(PART_KINDS) +
                    " ('command' = pods/probe cores, 'tank' = parts holding propellant). "
                    "None lists every part.")] = None,
    stage: Annotated[int | None, Field(
        description="Only parts that activate (`stage`) or detach (`decouple_stage`) at this KSP "
                    "stage number. None = all stages.")] = None,
) -> dict:
    """Parts of the active vessel with idx, structure, staging, and live module state.

    Each part: `idx` (position in the vessel's part list; the handle control_part and other tools
    take), internal `name` (stable; titles are localized), `tag`, `stage` (activates when the
    stage counter reaches it; -1 never), `decouple_stage` (detaches at that stage; -1 never),
    `parent` idx, module state (engine active/fuel/thrust, decoupler fired, chute state, leg,
    solar, antenna, fairing, docking port...), `resources` {name: [amount, max]}, and
    `temp_frac` (hotter of core/skin over its limit). idx values change whenever the vessel gains
    or loses parts: list again after staging, decoupling or docking.
    """
    if kind is not None and kind not in PART_KINDS:
        raise AstraError(f"unknown part kind {kind!r}", "one of: " + ", ".join(PART_KINDS))
    v = ksp().vessel()
    parts = v.parts.all
    idx_of = part_index(parts)
    module_kinds = [kind] if kind in _KINDS else ([] if kind else list(_KINDS))
    modules, read_errors = _module_states(v, idx_of, module_kinds)
    command = {idx_of[p._object_id] for p in v.parts.with_module("ModuleCommand") if p._object_id in idx_of}
    if kind is None:
        selected = list(range(len(parts)))
    elif kind == "command":
        selected = sorted(command)
    elif kind == "tank":
        selected = sorted(i for i, res in _part_resources(v, idx_of).items() if _PROPELLANTS & set(res))
    else:
        selected = sorted(i for i, mods in modules.items() if kind in mods)
    resources = _part_resources(v, idx_of, None if kind is None else set(selected))

    rows = []
    for i in selected:
        p = parts[i]
        try:
            s, ds = p.stage, p.decouple_stage
            if stage is not None and stage not in (s, ds):
                continue
            parent = p.parent
            row: dict[str, Any] = {"idx": i, "name": p.name, "title": p.title}
            tag = p.tag
            if tag:
                row["tag"] = tag
            row.update(stage=s, decouple_stage=ds,
                       parent=idx_of.get(parent._object_id) if parent is not None else None)
            row.update(modules.get(i, {}))
            if i in command:
                row["command"] = {"free_seats": p.available_seats}
            if i in resources:
                row["resources"] = resources[i]
            row["temp_frac"] = max(p.temperature / p.max_temperature,
                                   p.skin_temperature / p.max_skin_temperature)
            rows.append(row)
        except Exception as exc:  # noqa: BLE001
            rows.append({"idx": i, "error": f"part unreadable ({exc})"})

    controlling = _safe(lambda: v.parts.controlling)
    root = _safe(lambda: v.parts.root)
    out = {
        "vessel": v.name, "part_count": len(parts), "current_stage": v.control.current_stage,
        "root_idx": idx_of.get(root._object_id) if root is not None else None,
        "controlling_idx": idx_of.get(controlling._object_id) if controlling is not None else None,
        "shown": len(rows), "parts": rows,
    }
    if read_errors:
        out["read_errors"] = read_errors
    return out


# ---------------------------------------------------------------------------------------------
# Bodies


def _terrain_cache_path() -> Path:
    return CONFIG.cache_dir / "terrain.json"


def sample_max_terrain(height: Callable[[float, float], float], grid_deg: float,
                       seeds: int = 40, min_step_deg: float = 0.01, max_evals: int = 200_000) -> dict:
    """Highest terrain found by a global lat/lon grid plus hill-climbing from the best cells.

    The result is a lower bound on the true maximum (narrow peaks can hide between samples)."""
    half = grid_deg / 2.0
    cells: list[tuple[float, float, float]] = []
    lat = -90.0 + half
    while lat < 90.0:
        lon = -180.0 + half
        while lon < 180.0:
            cells.append((height(lat, lon), lat, lon))
            lon += grid_deg
        lat += grid_deg
    evals = len(cells)
    cells.sort(reverse=True)
    starts: list[tuple[float, float, float]] = []
    for c in cells:  # distinct seeds, at least two cells apart
        if all(abs(c[1] - s[1]) > 1.5 * grid_deg or abs(c[2] - s[2]) > 1.5 * grid_deg for s in starts):
            starts.append(c)
        if len(starts) >= seeds:
            break
    best = cells[0]
    for h, la, lo in starts:
        step = half
        while step >= min_step_deg and evals < max_evals:
            moved = False
            for dla in (-1, 0, 1):
                for dlo in (-1, 0, 1):
                    if not (dla or dlo):
                        continue
                    nla = max(-90.0, min(90.0, la + dla * step))
                    nlo = (lo + dlo * step + 180.0) % 360.0 - 180.0
                    nh = height(nla, nlo)
                    evals += 1
                    if nh > h:
                        h, la, lo, moved = nh, nla, nlo, True
            if not moved:
                step /= 2.0
        if h > best[0]:
            best = (h, la, lo)
    return {"max_m": best[0], "lat_deg": best[1], "lon_deg": best[2], "samples": evals,
            "method": f"{grid_deg:.3g} deg global grid + hill-climb to {min_step_deg:g} deg; a lower bound"}


def max_terrain(body: Any) -> dict:
    """Sampled maximum terrain height of `body`, cached in <cache_dir>/terrain.json."""
    path = _terrain_cache_path()
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    name = body.name
    if name in cache:
        return cache[name]
    # ~10 km between samples finds the narrow peaks a coarser grid misses (verified on Kerbin).
    grid = min(4.0, max(1.0, math.degrees(10_500.0 / body.equatorial_radius)))
    found = sample_max_terrain(body.surface_height, grid)
    cache[name] = found
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return found


def body_facts(b: Any, with_terrain: bool = True) -> dict:
    r = b.equatorial_radius
    rot = b.rotational_speed
    out: dict[str, Any] = {
        "body": b.name, "is_star": b.is_star,
        "gm_m3ps2": b.gravitational_parameter, "radius_m": r, "mass_kg": b.mass,
        "surface_gravity_mps2": b.surface_gravity,
        "soi_radius_m": finite(b.sphere_of_influence),
        "rotation_period_s": b.rotational_period, "rotation_rate_radps": rot,
        "equator_surface_speed_mps": rot * r,
        "has_solid_surface": b.has_solid_surface,
        "flying_high_threshold_m": b.flying_high_altitude_threshold,
        "space_high_threshold_m": b.space_high_altitude_threshold,
    }
    if b.has_atmosphere:
        p0 = b.pressure_at(0.0)
        out["atmosphere"] = {"depth_m": b.atmosphere_depth, "sea_level_pressure_pa": p0,
                             "sea_level_pressure_atm": p0 / 101325.0,
                             "sea_level_density_kgpm3": b.density_at(0.0),
                             "has_oxygen": b.has_atmospheric_oxygen}
    else:
        out["atmosphere"] = None
    o = b.orbit
    if o is not None:
        out["orbit"] = {"parent": o.body.name, "semi_major_axis_m": o.semi_major_axis,
                        "eccentricity": o.eccentricity, "inclination_deg": math.degrees(o.inclination),
                        "period_s": o.period, "periapsis_m": o.periapsis, "apoapsis_m": o.apoapsis}
    out["satellites"] = [s.name for s in b.satellites]
    if with_terrain and b.has_solid_surface:
        try:
            out["max_terrain"] = max_terrain(b)
        except Exception as exc:  # noqa: BLE001 — the constants above are still worth returning
            out["max_terrain"] = {"error": f"terrain sampling failed: {exc}"}
    return out


@tool("observe")
def body_info(
    body: Annotated[str | None, Field(
        description="Body name (e.g. 'Kerbin', 'Mun', 'Duna'); None = the body the active vessel "
                    "is orbiting.")] = None,
) -> dict:
    """Live physical constants of a celestial body, read from the game (never from memory).

    GM, radius, mass, surface gravity, SOI radius, rotation period/rate and equatorial surface
    speed, atmosphere (depth, sea-level pressure and density, oxygen) or null, orbit around its
    parent, satellites, KSP's flying-high / space-high thresholds, and `max_terrain`: the highest
    terrain found by sampling (a lower bound; the first call for a body samples for up to ~40 s,
    later calls read the cache). Use these as inputs to compute_* tools.
    """
    k = ksp()
    if body is None:
        if k.scene() != "flight":
            raise WrongScene("no active vessel to take the body from",
                             "name the body, e.g. body_info(body='Kerbin')")
        b = k.vessel().orbit.body
    else:
        bodies = k.sc.bodies
        names = list(bodies)
        b = bodies[names[pick_name(names, body, "body")]]
    return body_facts(b)


# ---------------------------------------------------------------------------------------------
# Orbits


def orbit_summary(o: Any, ut: float) -> dict:
    """Elements and timing of one orbit patch (angles in degrees, times relative to now)."""
    body = o.body
    t_soi = finite(o.time_to_soi_change)
    return {
        "body": body.name, "apoapsis_alt_m": finite(o.apoapsis_altitude),
        "periapsis_alt_m": o.periapsis_altitude, "semi_major_axis_m": o.semi_major_axis,
        "eccentricity": o.eccentricity, "inclination_deg": math.degrees(o.inclination),
        "lan_deg": math.degrees(o.longitude_of_ascending_node),
        "arg_periapsis_deg": math.degrees(o.argument_of_periapsis),
        "true_anomaly_deg": math.degrees(o.true_anomaly),
        "period_s": finite(o.period), "time_to_apoapsis_s": finite(o.time_to_apoapsis),
        "time_to_periapsis_s": finite(o.time_to_periapsis),
        "speed_mps": o.speed, "radius_m": o.radius,
        "time_to_soi_change_s": t_soi, "soi_change_ut": ut + t_soi if t_soi is not None else None,
        "body_radius_m": body.equatorial_radius,
        "body_atmosphere_depth_m": body.atmosphere_depth if body.has_atmosphere else 0.0,
    }


def periapsis_ut(o: Any, not_before: float) -> float | None:
    """UT of the first periapsis of orbit patch `o` at or after `not_before`.

    Computed from the patch's own epoch and mean anomaly at epoch. kRPC's ut_at_true_anomaly and
    time_to_periapsis count from the current clock and the patch's internal state, which for a
    future patch is its start; ut_at_true_anomaly also returns an already-passed periapsis."""
    a = finite(o.semi_major_axis)
    if not a:
        return None
    n = math.sqrt(o.body.gravitational_parameter / abs(a) ** 3)  # mean motion, rad/s
    t = o.epoch - o.mean_anomaly_at_epoch / n
    if o.eccentricity < 1.0 and t < not_before:
        period = 2.0 * math.pi / n
        t += period * math.ceil((not_before - t) / period)
    return finite(t)


def patch_chain(o: Any, ut: float, max_patches: int = 5) -> list[dict]:
    """KSP's patched-conic continuation of `o`: one entry per future patch (encounter/escape)."""
    out = []
    start = finite(o.time_to_soi_change)
    nxt = o.next_orbit if start is not None else None
    while nxt is not None and len(out) < max_patches:
        t_soi = finite(nxt.time_to_soi_change)
        start_ut = ut + start if start is not None else ut
        out.append({
            "body": nxt.body.name, "start_ut": start_ut,
            "periapsis_alt_m": nxt.periapsis_altitude, "apoapsis_alt_m": finite(nxt.apoapsis_altitude),
            "eccentricity": nxt.eccentricity, "inclination_deg": math.degrees(nxt.inclination),
            "periapsis_ut": _safe(lambda n=nxt, s=start_ut: periapsis_ut(n, s)),
            "soi_change_ut": ut + t_soi if t_soi is not None else None,
        })
        start = t_soi
        nxt = nxt.next_orbit if t_soi is not None else None
    return out


def current_target(sc: Any) -> tuple[Any, str] | tuple[None, None]:
    port = _safe(lambda: sc.target_docking_port)
    if port is not None:
        return port, "docking_port"
    tv = _safe(lambda: sc.target_vessel)
    if tv is not None:
        return tv, "vessel"
    tb = _safe(lambda: sc.target_body)
    if tb is not None:
        return tb, "body"
    return None, None


def _target_orbit(obj: Any, kind: str):
    return obj.part.vessel.orbit if kind == "docking_port" else obj.orbit


@tool("observe")
def orbit_info(
    of: Annotated[str, Field(
        description="'vessel' (the active vessel), 'target', or the name of a body or vessel "
                    "(vessels also by '#id' from game_list_vessels).")] = "vessel",
) -> dict:
    """Orbital elements, apsides and timing, plus KSP's patched-conic chain (encounters, escapes).

    `orbit`: current patch (altitudes above sea level; `periapsis_alt_m` < 0 means the orbit
    intersects the body; hyperbolic orbits have no apoapsis/period). `patches`: the following
    patches with their body, periapsis/apoapsis, start UT and periapsis UT. This is the reliable
    encounter check; single-conic closest-approach estimates are not.
    """
    k = ksp()
    sc = k.sc
    key = of.strip()
    if key.lower() == "vessel":
        v = k.vessel()
        o, label = v.orbit, v.name
    elif key.lower() == "target":
        k.require_flight()
        obj, kind = current_target(sc)
        if obj is None:
            raise AstraError("no target is set", "target_set(name) first, or pass a body/vessel name")
        o = _target_orbit(obj, kind)
        label = obj.part.vessel.name if kind == "docking_port" else obj.name
        if o is None:
            raise AstraError(f"the target {label} does not orbit anything", None)
    else:
        bodies = sc.bodies
        body_hits = [n for n in bodies if n.casefold() == key.casefold()]
        if body_hits:
            o, label = bodies[body_hits[0]].orbit, body_hits[0]
            if o is None:
                raise AstraError(f"{label} does not orbit anything", None)
        else:
            v = resolve_vessel(key)
            o, label = v.orbit, v.name
    ut = sc.ut
    return {"of": label, "ut": ut, "orbit": orbit_summary(o, ut), "patches": patch_chain(o, ut)}


# ---------------------------------------------------------------------------------------------
# Targets


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm3(a):
    return math.sqrt(_dot(a, a))


def phase_angle_deg(r_vessel, v_vessel, r_target) -> float:
    """Angle from the vessel to the target around the common body, positive when the target is
    ahead in the direction of motion, in (-180, 180]. Handedness-independent."""
    h = _cross(r_vessel, v_vessel)
    s = _dot(_cross(r_vessel, r_target), h) / (_norm3(h) or 1.0)
    return math.degrees(math.atan2(s, _dot(r_vessel, r_target)))


def closest_approach_sampled(dist_at: Callable[[float], float], t0: float, t1: float,
                             samples: int = 120, refine: int = 24) -> tuple[float, float]:
    """(ut, distance) of the minimum of dist_at over [t0, t1]: uniform scan + golden section."""
    ts = [t0 + (t1 - t0) * i / (samples - 1) for i in range(samples)]
    ds = [dist_at(t) for t in ts]
    i = min(range(samples), key=ds.__getitem__)
    a, b = ts[max(i - 1, 0)], ts[min(i + 1, samples - 1)]
    g = (math.sqrt(5.0) - 1.0) / 2.0
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = dist_at(c), dist_at(d)
    for _ in range(refine):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = dist_at(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = dist_at(d)
    t_best, d_best = (c, fc) if fc < fd else (d, fd)
    if ds[i] < d_best:
        t_best, d_best = ts[i], ds[i]
    return t_best, d_best


def _future_ut(ut_event: float, now: float, period: float | None) -> float | None:
    if not math.isfinite(ut_event):
        return None
    if period and ut_event < now:
        ut_event += period * math.ceil((now - ut_event) / period)
    return ut_event if ut_event >= now else None


@tool("observe")
def target_info() -> dict:
    """Relative navigation to the current target: distance, relative velocity, closest approach, phase, planes.

    `relative`: target minus vessel, position (m) and velocity (m/s) resolved along the vessel's
    prograde / normal / radial_out axes, plus `closing_speed_mps` (> 0 closing). When both orbit
    the same body: phase angle (> 0 = target ahead), relative inclination, time to the ascending/
    descending nodes, and closest approach over the next orbit both from kRPC (single conic) and
    from sampling both orbits. For a loaded target vessel, its docking ports (with idx for
    target_set/mj_dock).
    """
    k = ksp()
    v = k.vessel()
    sc = k.sc
    obj, kind = current_target(sc)
    if obj is None:
        raise AstraError("no target is set", "target_set(name) first")
    tv = obj.part.vessel if kind == "docking_port" else (obj if kind == "vessel" else None)
    body = v.orbit.body
    nr = body.non_rotating_reference_frame
    ut = sc.ut
    r_v, v_v = v.position(nr), v.velocity(nr)
    if kind == "docking_port":
        r_t, v_t = obj.position(nr), tv.velocity(nr)
    else:
        r_t, v_t = obj.position(nr), obj.velocity(nr)
    rel_r, rel_v = _sub(r_t, r_v), _sub(v_t, v_v)
    dist = _norm3(rel_r)

    orf = v.orbital_reference_frame
    ax_x, ax_pro, ax_nrm = (sc.transform_direction(a, orf, nr) for a in ((1, 0, 0), (0, 1, 0), (0, 0, 1)))
    radial_out = ax_x if _dot(ax_x, r_v) > 0 else (-ax_x[0], -ax_x[1], -ax_x[2])

    def along(vec):
        return {"prograde": _dot(vec, ax_pro), "normal": _dot(vec, ax_nrm), "radial_out": _dot(vec, radial_out)}

    out: dict[str, Any] = {
        "target": f"{obj.part.name} on {tv.name}" if kind == "docking_port" else obj.name, "kind": kind,
        "target_vessel": tv.name if tv is not None else None, "ut": ut,
        "distance_m": dist, "relative_speed_mps": _norm3(rel_v),
        "closing_speed_mps": -_dot(rel_r, rel_v) / dist if dist > 0 else 0.0,
        "relative": {"position_m": along(rel_r), "velocity_mps": along(rel_v)},
    }
    t_orbit = _target_orbit(obj, kind)
    same_body = t_orbit is not None and t_orbit.body == body
    out["same_soi"] = same_body
    if same_body:
        o = v.orbit
        period = finite(o.period)
        out["phase_angle_deg"] = phase_angle_deg(r_v, v_v, r_t)
        out["relative_inclination_deg"] = math.degrees(o.relative_inclination(t_orbit))
        for label, fn in (("ascending_node", o.true_anomaly_at_an), ("descending_node", o.true_anomaly_at_dn)):
            node_ut = _safe(lambda fn=fn: _future_ut(o.ut_at_true_anomaly(fn(t_orbit)), ut, period))
            out[label] = {"ut": node_ut, "time_to_s": node_ut - ut if node_ut is not None else None}
        out["closest_approach_krpc"] = _safe(lambda: {
            "ut": o.time_of_closest_approach(t_orbit),
            "distance_m": o.distance_at_closest_approach(t_orbit),
            "note": "single-conic estimate"})
        spans = [x for x in (period, finite(o.time_to_soi_change)) if x]
        window = min(spans) if spans else None
        if window:
            def dist_at(t: float) -> float:
                return _norm3(_sub(o.position_at(t, nr), t_orbit.position_at(t, nr)))
            t_ca, d_ca = closest_approach_sampled(dist_at, ut, ut + window)
            out["closest_approach_sampled"] = {"ut": t_ca, "time_to_s": t_ca - ut, "distance_m": d_ca,
                                               "window_s": window}
    if tv is not None:
        ports = _safe(lambda: _docking_ports(tv), [])
        if ports:
            out["target_docking_ports"] = ports
    return out


def _docking_ports(vessel: Any) -> list[dict]:
    parts = vessel.parts.all
    idx_of = part_index(parts)
    return [{"idx": idx_of.get(d.part._object_id), "name": d.part.name, "tag": d.part.tag,
             "state": enum_name(d.state)} for d in vessel.parts.docking_ports]


# ---------------------------------------------------------------------------------------------
# Camera


def _wait_for_file(path: Path, timeout_s: float) -> bool:
    """True once `path` exists with a size that stopped changing (Unity writes asynchronously)."""
    deadline = time.monotonic() + timeout_s
    last = -1
    while time.monotonic() < deadline:
        if path.exists():
            size = path.stat().st_size
            if size > 0 and size == last:
                return True
            last = size
        time.sleep(0.2)
    return path.exists() and path.stat().st_size > 0


@tool("observe")
def camera_look(
    width: Annotated[int, Field(ge=160, le=3840,
                                description="Width in pixels of the returned image (aspect kept). "
                                            "About 1024 to inspect details, 640 for a quick look.")] = 1024,
    clean_frame: Annotated[bool, Field(
        description="kRPC fallback only: while the game is paused KSP draws its pause menu over the "
                    "view; true lets about half a second of game time run to capture a clean frame, "
                    "then pauses again.")] = False,
) -> Picture:
    """Screenshot of what the game shows right now, returned as an image you can look at.

    Uses the bridge's render-to-texture capture (any scene); falls back to kRPC's screenshot,
    which works only in the flight scene and shows the pause menu while paused (see clean_frame).
    The caption gives scene, UT, vessel and source. Use it to verify what telemetry cannot show:
    the rocket on the pad, separation, chute canopies, attitude.
    """
    k = ksp()
    shots = CONFIG.cache_dir / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    raw = (shots / f"{stamp}-raw.png").resolve()
    source, notes = None, []
    if k.bridge.up():
        try:
            reply = k.bridge.post("/screenshot", {"path": str(raw), "width": width}, timeout=30)
            if reply.get("image_b64"):
                raw.write_bytes(base64.b64decode(reply["image_b64"]))
            elif reply.get("path") and Path(reply["path"]).resolve() != raw:
                raw = Path(reply["path"])  # written elsewhere: read it, but never delete it
            source = "bridge"
        except (BridgeError, NotConnected) as exc:
            notes.append(f"bridge capture failed: {exc.message}")
    scene = _safe(k.scene, "unknown")
    if source is None:
        if scene != "flight":
            raise WrongScene(f"no screenshot source: the kRPC fallback only works in flight (scene: {scene})",
                             "; ".join(notes) or "the bridge is not running")
        source = "krpc"
        if clean_frame and k.paused:
            k.set_paused(False)
            try:
                time.sleep(0.5)  # let the pause menu close before the frame is grabbed
                k.sc.screenshot(str(raw), 1)
                _wait_for_file(raw, 15.0)
            finally:
                k.hold_for_deliberation()
        else:
            k.sc.screenshot(str(raw), 1)
            if k.paused:
                notes.append("paused: the pause menu covers part of the view (clean_frame=true avoids it)")
    if not _wait_for_file(raw, 15.0):
        raise AstraError(f"the screenshot never appeared at {raw}",
                         "the KSP window must be rendering (not minimized); try again")
    from PIL import Image

    with Image.open(raw) as img:
        img = img.convert("RGB")
        if img.width > width:
            img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
        out = raw.with_name(f"{stamp}.jpg")
        img.save(out, "JPEG", quality=85)
        size = img.size
    if raw.parent == shots.resolve():
        raw.unlink(missing_ok=True)
    ut = _safe(lambda: k.sc.ut)
    vessel = _safe(lambda: k.sc.active_vessel.name) if scene == "flight" else None
    caption = (f"scene={scene} ut={ut:.1f} vessel={vessel or '-'} {size[0]}x{size[1]} via {source}"
               if ut is not None else f"scene={scene} {size[0]}x{size[1]} via {source}")
    if notes:
        caption += " (" + "; ".join(notes) + ")"
    return Picture(path=out, fmt="jpeg", caption=caption)
