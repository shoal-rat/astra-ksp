"""Slide rule: orbital mechanics, rocket equation, transfers, maneuver planning, terrain.

The pure calculators (``needs_game=False``) read body constants live from kRPC whenever the game
answers and otherwise from the cache at ``CONFIG.cache_dir/bodies.json``, which every live read
refreshes; each result says which it used. Per-body maximum terrain lives in
``CONFIG.cache_dir/terrain.json``, shared with observe.body_info. The maneuver planners and the node search read the active
vessel live and ask KSP's own patched-conic solver what a burn would do through ONE temporary
maneuver node that is always removed again, with the game paused while they work. Nothing here leaves
a node behind: the crew places real nodes with node_create.

Every mission-level number (altitudes, tolerances, margins, what to minimize) is an argument.
"""

import contextlib
import json
import math
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal

from pydantic import ConfigDict, Field, with_config
from typing_extensions import TypedDict

from astra.config import CONFIG
from astra.errors import AstraError, NotConnected
from astra.ksp import KSP, ksp
from astra.physics import calc, lambert, orbits, rocket
from astra.physics.orbits import KeplerOrbit, Vec3, vdot, vnorm, vscale, vsub, vunit
from astra.registry import tool

KERBIN_DAY_S = 21600.0  # the unit of every *_days argument (a 6-hour Kerbin day)
_DEG = math.degrees

# ---------------------------------------------------------------------------------------------
# body constants: live kRPC or the local cache


@dataclass
class Body:
    name: str
    mu: float
    radius: float
    soi: float
    rotational_period: float
    parent: str | None
    satellites: list[str]
    has_atmosphere: bool
    atmosphere_depth: float
    surface_gravity: float
    has_solid_surface: bool
    atmosphere: list[list[float]] = field(default_factory=list)  # [alt_m, pressure_pa, density_kgpm3]
    orbit: dict | None = None
    max_terrain_m: float | None = None
    max_terrain_note: str | None = None
    read_utc: str | None = None

    @property
    def equator_speed(self) -> float:
        return math.tau * self.radius / abs(self.rotational_period) if self.rotational_period else 0.0

    def kepler(self, parent_mu: float) -> KeplerOrbit:
        if not self.orbit:
            raise AstraError(f"{self.name} has no orbit (it is the star)")
        o = self.orbit
        return KeplerOrbit(parent_mu, o["sma_m"], o["eccentricity"], o["inclination_rad"], o["lan_rad"],
                           o["argpe_rad"], o["mean_anomaly_at_epoch_rad"], o["epoch_s"])

    @classmethod
    def from_record(cls, rec: dict, terrain: dict | None = None) -> "Body":
        return cls(
            name=rec["name"], mu=rec["mu_m3ps2"], radius=rec["radius_m"],
            soi=math.inf if rec.get("soi_m") is None else rec["soi_m"],
            rotational_period=rec["rotational_period_s"], parent=rec.get("parent"),
            satellites=list(rec.get("satellites", [])), has_atmosphere=rec["has_atmosphere"],
            atmosphere_depth=rec.get("atmosphere_depth_m", 0.0), surface_gravity=rec["surface_gravity_mps2"],
            has_solid_surface=rec.get("has_solid_surface", True), atmosphere=rec.get("atmosphere_profile", []),
            orbit=rec.get("orbit"), max_terrain_m=terrain["max_m"] if terrain else None,
            max_terrain_note=terrain.get("method") if terrain else None, read_utc=rec.get("read_utc"))


def _read_body(b: Any) -> dict:
    """All constants of one kRPC CelestialBody as a cache record (about 20-50 RPCs)."""
    o = b.orbit
    soi = b.sphere_of_influence
    rec: dict[str, Any] = {
        "name": b.name, "mu_m3ps2": b.gravitational_parameter, "radius_m": b.equatorial_radius,
        "soi_m": soi if math.isfinite(soi) else None, "rotational_period_s": b.rotational_period,
        "parent": o.body.name if o is not None else None, "satellites": [s.name for s in b.satellites],
        "has_atmosphere": b.has_atmosphere, "atmosphere_depth_m": b.atmosphere_depth if b.has_atmosphere else 0.0,
        "surface_gravity_mps2": b.surface_gravity, "has_solid_surface": b.has_solid_surface,
        "read_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if b.has_atmosphere:
        depth, n = b.atmosphere_depth, 16
        rec["atmosphere_profile"] = [[h, b.pressure_at(h), b.density_at(h)]
                                     for h in (depth * i / (n - 1) for i in range(n))]
    if o is not None:
        rec["orbit"] = {"sma_m": o.semi_major_axis, "eccentricity": o.eccentricity,
                        "inclination_rad": o.inclination, "lan_rad": o.longitude_of_ascending_node,
                        "argpe_rad": o.argument_of_periapsis, "mean_anomaly_at_epoch_rad": o.mean_anomaly_at_epoch,
                        "epoch_s": o.epoch}
    return rec


def _cache_file() -> Path:
    return Path(CONFIG.cache_dir) / "bodies.json"


def _load_cache() -> dict[str, dict]:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8")).get("bodies", {})
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # caches are a convenience; never fail a calculation over one


def _merge_cache(records: dict[str, dict]) -> None:
    """Merge freshly read body records into the cache file."""
    _write_json(_cache_file(), {"format": 1, "bodies": {**_load_cache(), **records}})


def _terrain_file() -> Path:
    """Per-body maximum terrain, shared with observe.body_info: {body: {max_m, lat_deg, lon_deg, samples, method}}."""
    return Path(CONFIG.cache_dir) / "terrain.json"


def _load_terrain() -> dict[str, dict]:
    try:
        return json.loads(_terrain_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _record_max_terrain(name: str, entry: dict) -> tuple[dict, bool]:
    """Store a sampled body maximum if it beats the cached one (every sampled maximum is a lower
    bound of the true peak, so the higher value is strictly better). Returns (kept entry, updated)."""
    cache = _load_terrain()
    old = cache.get(name)
    if old is not None and old.get("max_m", -math.inf) >= entry["max_m"]:
        return old, False
    cache[name] = entry
    _write_json(_terrain_file(), cache)
    return entry, True


def _live_link() -> KSP | None:
    """The game link if kRPC answers right now, else None. One quick probe (``KSP.try_conn``): never
    retries, never waits for the game to start."""
    k = ksp()
    try:
        return k if k.try_conn(timeout=0.5) is not None else None
    except NotConnected:  # try_conn swallows this today; stay offline-safe if that ever changes
        return None


def _match(name: str, known: list[str]) -> str:
    for k in known:
        if k.lower() == name.strip().lower():
            return k
    raise AstraError(f"unknown body {name!r}", f"known bodies: {', '.join(sorted(known))}")


class BodyBook:
    """Body constants for one tool call: live from kRPC when reachable, else the local cache."""

    def __init__(self, link: KSP | None):
        self.link = link
        self.cache = _load_cache()
        self.terrain = _load_terrain()
        self.fresh: dict[str, dict] = {}
        self._krpc_bodies: dict | None = None
        self.live_error: str | None = None

    @property
    def live(self) -> bool:
        return self.link is not None and self.live_error is None

    def get(self, name: str) -> Body:
        if self.live:
            try:
                if self._krpc_bodies is None:
                    self._krpc_bodies = self.link.sc.bodies
                key = _match(name, list(self._krpc_bodies))
                if key not in self.fresh:
                    self.fresh[key] = _read_body(self._krpc_bodies[key])
                return Body.from_record(self.fresh[key], self.terrain.get(key))
            except AstraError:
                raise
            except Exception as exc:  # noqa: BLE001 — link dropped mid-call: use the cache instead
                self.live_error = f"{type(exc).__name__}: {exc}"
        if not self.cache:
            raise AstraError("no body constants available: the game is not reachable and nothing is cached",
                             "load a save in KSP once (any scene); compute tools then cache every body they read")
        key = _match(name, list(self.cache))
        return Body.from_record(self.cache[key], self.terrain.get(key))

    def source(self) -> str:
        if self.live:
            return "live game (kRPC)"
        why = f" (live read failed: {self.live_error})" if self.live_error else " (game not reachable)"
        return f"cache {_cache_file()}{why}"

    def save(self) -> None:
        if self.fresh:
            _merge_cache(self.fresh)


@contextlib.contextmanager
def _bodies() -> Iterator[BodyBook]:
    book = BodyBook(_live_link())
    yield book
    book.save()


def _terrain_warning(b: Body, alt: float, what: str) -> str | None:
    if alt < 0:
        return f"{what} {alt:.0f} m is below {b.name}'s datum (sea level): the trajectory intersects the body"
    if b.has_atmosphere and alt < b.atmosphere_depth:
        return f"{what} {alt:.0f} m is inside the atmosphere (top at {b.atmosphere_depth:.0f} m): drag will change the orbit"
    if b.max_terrain_m is not None and alt < b.max_terrain_m:
        return f"{what} {alt:.0f} m is below the highest sampled terrain ({b.max_terrain_m:.0f} m): check the ground track with compute_terrain"
    return None


def _need(cond: bool, message: str, hint: str | None = None) -> None:
    if not cond:
        raise AstraError(message, hint)


def _finite(**values: Any) -> None:
    """Reject NaN/inf arguments up front (pydantic accepts them): they would otherwise surface as a
    bare ValueError deep in the physics, or as nonsense such as a node at UT = inf."""
    for name, v in values.items():
        items = v if isinstance(v, (tuple, list)) else (v,)
        _need(all(x is None or math.isfinite(x) for x in items), f"{name} must be finite, got {v!r}",
              "pass ordinary numbers (no NaN or infinity)")


def _physics(fn, *args, hint: str | None = None, **kwargs):
    """Call a physics function, turning its ValueError into an AstraError the crew can act on."""
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise AstraError(str(exc), hint or "check the inputs against the tool description") from exc


# ---------------------------------------------------------------------------------------------
# free-form math


@tool("compute", needs_game=False)
def compute_calc(
    expression: Annotated[str, Field(description=(
        "Math to evaluate. One expression, or statements separated by ';' or newlines where "
        "'name = expr' stores a value for later statements; the last statement is the result. "
        "Functions: sqrt cbrt sin cos tan asin acos atan atan2 sinh cosh tanh asinh acosh atanh exp "
        "log(x[, base]) log10 log2 radians degrees hypot floor ceil abs min max copysign. Constants: "
        "pi e tau inf g0 (=9.80665). Trig works in radians; '^' means power."))],
    variables: Annotated[dict[str, float] | None, Field(description=(
        "Named numbers used by the expression, e.g. {\"m0\": 12.5, \"isp\": 320}. Keep units consistent."))] = None,
) -> dict:
    """Evaluate free-form math safely: a calculator for anything the other compute tools do not cover.

    Returns {value, assigned}: the last statement's value and every variable assigned along the way.
    Only numbers, names, arithmetic (+ - * / // % **), comparisons, and/or/not, 'a if cond else b'
    and the listed functions are allowed; attribute access, strings, imports and lambdas are rejected.
    """
    try:
        return calc.evaluate(expression, variables)
    except calc.CalcError as exc:
        raise AstraError(f"calc: {exc}", "fix the expression; only the listed functions and constants exist") from exc


# ---------------------------------------------------------------------------------------------
# orbit / Hohmann


BodyArg = Annotated[str, Field(description="Celestial body name as KSP spells it, e.g. 'Kerbin', 'Mun', 'Duna' (case-insensitive).")]


@tool("compute", needs_game=False)
def compute_orbit(
    body: BodyArg,
    periapsis_alt_m: Annotated[float, Field(description="Periapsis altitude above the body's sea level (datum), m.")],
    apoapsis_alt_m: Annotated[float, Field(description="Apoapsis altitude above sea level, m (equal to periapsis for a circular orbit).")],
    at_alt_m: Annotated[float | None, Field(description="Optional altitude (between the apsides) at which to also report speed, flight-path angle and timing.")] = None,
) -> dict:
    """Vis-viva calculator for an orbit given by its apsis altitudes: speeds, period, energy.

    Returns semi-major axis, eccentricity, period, and at each apsis the orbital speed plus the
    circular and escape speeds there. With ``at_alt_m`` also the speed, flight-path angle (+ climbing
    on the outbound leg) and time after periapsis where the orbit crosses that altitude. Warns about an
    apoapsis beyond the SOI and a periapsis inside the atmosphere, below the datum, or below the highest
    sampled terrain.
    """
    _finite(periapsis_alt_m=periapsis_alt_m, apoapsis_alt_m=apoapsis_alt_m, at_alt_m=at_alt_m)
    with _bodies() as book:
        b = book.get(body)
    _need(apoapsis_alt_m >= periapsis_alt_m, "apoapsis_alt_m is below periapsis_alt_m", "swap the two values")
    r_pe, r_ap = b.radius + periapsis_alt_m, b.radius + apoapsis_alt_m
    _need(r_pe > 0, "periapsis radius must be positive (altitude above -radius)")
    a = orbits.semi_major_axis(r_pe, r_ap)
    e = orbits.eccentricity(r_pe, r_ap)

    def apsis(r: float) -> dict:
        return {"altitude_m": r - b.radius, "radius_m": r, "speed_mps": orbits.vis_viva_speed(b.mu, r, a),
                "circular_speed_mps": orbits.circular_speed(b.mu, r), "escape_speed_mps": orbits.escape_speed(b.mu, r)}

    out: dict[str, Any] = {
        "body": b.name, "semi_major_axis_m": a, "eccentricity": e, "period_s": orbits.orbital_period(b.mu, a),
        "specific_energy_jpkg": orbits.specific_energy(b.mu, a), "periapsis": apsis(r_pe), "apoapsis": apsis(r_ap),
    }
    if at_alt_m is not None:
        r = b.radius + at_alt_m
        _need(r_pe - 1e-6 <= r <= r_ap + 1e-6, f"at_alt_m {at_alt_m} m is outside the orbit's altitude range "
              f"[{periapsis_alt_m}, {apoapsis_alt_m}]")
        nu = orbits.true_anomaly_at_radius(a, e, min(max(r, r_pe), r_ap)) if e > 1e-12 else 0.0
        t = orbits.time_between_anomalies(b.mu, a, e, 0.0, nu) if e > 1e-12 else 0.0
        out["at_altitude"] = {
            "altitude_m": at_alt_m, "speed_mps": orbits.vis_viva_speed(b.mu, r, a),
            "flight_path_angle_deg": _DEG(orbits.flight_path_angle(e, nu)), "true_anomaly_deg": _DEG(nu),
            "time_after_periapsis_s": t, "time_before_periapsis_s": out["period_s"] - t,
            "note": "outbound crossing; the inbound crossing mirrors it (negative flight-path angle)"}
    warnings = [w for w in (_terrain_warning(b, periapsis_alt_m, "periapsis"),) if w]
    if r_ap > b.soi:
        warnings.append(f"apoapsis radius {r_ap:.0f} m is beyond the SOI ({b.soi:.0f} m): the craft escapes {b.name}")
    out["warnings"] = warnings
    out["source"] = book.source()
    return out


@tool("compute", needs_game=False)
def compute_hohmann(
    body: Annotated[str, Field(description="The body both orbits go around (e.g. 'Kerbin' for LKO -> Mun).")],
    from_alt_m: Annotated[float, Field(description="Altitude of the starting circular orbit above sea level, m.")],
    to_alt_m: Annotated[float | None, Field(description="Altitude of the destination circular orbit, m. Omit when target_body is given.")] = None,
    target_body: Annotated[str | None, Field(description="A moon/planet orbiting `body` to transfer to; its orbit radius becomes the destination and arrival v_inf is reported.")] = None,
    capture_alt_m: Annotated[float | None, Field(description="With target_body: periapsis altitude above the target at which you would capture into a circular orbit; adds the capture burn.")] = None,
) -> dict:
    """Two-burn Hohmann transfer between circular orbits: Δv of both burns, transfer time, phase angle.

    Burns are signed (+ prograde, - retrograde). ``phase_angle_deg`` is how far the destination must
    LEAD you at the first burn (negative: trail). With ``target_body`` the second burn is replaced by
    the arrival excess speed relative to the target and, with ``capture_alt_m``, the Oberth capture burn
    into a circular orbit there. Circular, coplanar model: use compute_maneuver for the live burn.
    """
    _finite(from_alt_m=from_alt_m, to_alt_m=to_alt_m, capture_alt_m=capture_alt_m)
    with _bodies() as book:
        b = book.get(body)
        tgt = book.get(target_body) if target_body else None
    r1 = b.radius + from_alt_m
    if tgt is not None:
        _need(to_alt_m is None, "give either to_alt_m or target_body, not both")
        _need(tgt.parent == b.name, f"{tgt.name} orbits {tgt.parent}, not {b.name}",
              f"use body='{tgt.parent}' or compute_transfer_window for bodies with different parents")
        r2 = tgt.orbit["sma_m"]
    else:
        _need(to_alt_m is not None, "to_alt_m is required without target_body")
        _need(capture_alt_m is None, "capture_alt_m needs target_body")
        r2 = b.radius + to_alt_m
    _need(r1 > 0 and r2 > 0, "orbit radii must be positive")
    h = orbits.hohmann(b.mu, r1, r2)
    out: dict[str, Any] = {
        "body": b.name, "from_radius_m": r1, "to_radius_m": r2,
        "departure_burn_mps": h.dv1, "transfer_time_s": h.tof,
        "transfer_orbit": {"periapsis_alt_m": min(r1, r2) - b.radius, "apoapsis_alt_m": max(r1, r2) - b.radius,
                           "semi_major_axis_m": h.a_transfer},
        "phase_angle_deg": _DEG(orbits.phase_angle_for_transfer(b.mu, r2, h.tof)),
        "synodic_period_s": orbits.synodic_period(orbits.orbital_period(b.mu, r1), orbits.orbital_period(b.mu, r2)),
    }
    if tgt is None:
        out.update(arrival_burn_mps=h.dv2, total_dv_mps=h.total)
        out["math"] = "a_t=(r1+r2)/2; dv1=v_vis(r1,a_t)-sqrt(mu/r1); dv2=sqrt(mu/r2)-v_vis(r2,a_t); t=pi*sqrt(a_t^3/mu)"
    else:
        v_inf = abs(h.dv2)
        arrival: dict[str, Any] = {"target": tgt.name, "v_inf_mps": v_inf,
                                   "note": "target orbit taken as circular at its semi-major axis; v_inf = |v_target - v_transfer| at r2"}
        if capture_alt_m is not None:
            rc = tgt.radius + capture_alt_m
            cap = _physics(orbits.capture_dv, tgt.mu, rc, v_inf, rc)
            arrival.update(capture_alt_m=capture_alt_m, capture_dv_mps=cap,
                           periapsis_speed_mps=math.sqrt(v_inf ** 2 + 2 * tgt.mu / rc),
                           total_with_capture_mps=abs(h.dv1) + cap)
            if rc > tgt.soi:
                arrival["warning"] = "capture altitude is outside the target's SOI"
        out["arrival"] = arrival
        out["math"] = ("Hohmann to the target's orbit radius; capture dv = sqrt(v_inf^2 + 2 mu_t/r) - sqrt(mu_t/r) at "
                       "the capture periapsis r (patched conics)")
    out["source"] = book.source()
    return out


# ---------------------------------------------------------------------------------------------
# rocket equation, braking, ascent


@tool("compute", needs_game=False)
def compute_rocket(
    isp_s: Annotated[float | None, Field(description="Specific impulse, s, at the ambient pressure where the burn happens (vacuum Isp in space).")] = None,
    mass_start_t: Annotated[float | None, Field(description="Mass before the burn, t.")] = None,
    mass_end_t: Annotated[float | None, Field(description="Mass after the burn (e.g. stage dry mass + everything above), t.")] = None,
    dv_mps: Annotated[float | None, Field(description="Δv of the burn, m/s.")] = None,
    thrust_kn: Annotated[float | None, Field(description="Thrust at that ambient pressure, kN (sum of the engines that burn).")] = None,
    body: Annotated[str | None, Field(description="Body whose gravity sets TWR/hover (with altitude_m, else at the surface).")] = None,
    altitude_m: Annotated[float | None, Field(description="Altitude above sea level for the gravity used in TWR, m.")] = None,
    g_mps2: Annotated[float | None, Field(description="Explicit local gravity for TWR, m/s² (overrides body).")] = None,
) -> dict:
    """Rocket equation both ways, burn duration, and thrust-to-weight: give what you know, get the rest.

    Computes everything derivable from the inputs:
    isp + start + end mass -> Δv and propellant; isp + Δv + one mass -> the other mass and propellant;
    Δv + both masses -> required Isp; thrust + start mass + Δv -> burn time (mass loss included when
    isp is known) and the time at which half the Δv is delivered (start that long before a node);
    thrust + mass + gravity -> TWR and hover throttle at start and end mass. Masses in t, thrust in kN.
    """
    _finite(isp_s=isp_s, mass_start_t=mass_start_t, mass_end_t=mass_end_t, dv_mps=dv_mps, thrust_kn=thrust_kn,
            altitude_m=altitude_m, g_mps2=g_mps2)
    m0 = mass_start_t * 1000 if mass_start_t is not None else None
    m1 = mass_end_t * 1000 if mass_end_t is not None else None
    thrust = thrust_kn * 1000 if thrust_kn is not None else None
    out: dict[str, Any] = {}
    notes: list[str] = []
    try:
        if isp_s is not None:
            out["exhaust_velocity_mps"] = rocket.exhaust_velocity(isp_s)
            if m0 is not None and m1 is not None:
                out["dv_mps"] = rocket.delta_v(isp_s, m0, m1)
                if dv_mps is not None and abs(out["dv_mps"] - dv_mps) > 1e-6 * max(1.0, dv_mps):
                    notes.append(f"the masses give {out['dv_mps']:.1f} m/s, not the {dv_mps} m/s you passed")
            elif dv_mps is not None and m0 is not None:
                m1 = rocket.final_mass(isp_s, dv_mps, m0)
                out["mass_end_t"] = m1 / 1000
            elif dv_mps is not None and m1 is not None:
                m0 = rocket.initial_mass(isp_s, dv_mps, m1)
                out["mass_start_t"] = m0 / 1000
            if m0 is not None and m1 is not None:
                out["propellant_t"] = (m0 - m1) / 1000
                out["mass_ratio"] = m0 / m1
            if thrust is not None:
                out["mass_flow_kgps"] = rocket.mass_flow(thrust, isp_s)
        elif dv_mps is not None and m0 is not None and m1 is not None:
            _need(m0 > m1, "mass_start_t must exceed mass_end_t")
            out["isp_required_s"] = dv_mps / (rocket.G0 * math.log(m0 / m1))
        dv = out.get("dv_mps", dv_mps)
        if thrust is not None and m0 is not None and dv is not None:
            out["burn_time_s"] = rocket.burn_time(dv, m0, thrust, isp_s)
            out["half_dv_time_s"] = rocket.half_dv_time(dv, m0, thrust, isp_s)
            if isp_s is None:
                notes.append("no isp: burn time uses constant mass (an overestimate)")
        if thrust is not None and (m0 is not None or m1 is not None):
            if g_mps2 is not None:
                g, g_src = g_mps2, "given"
                if body is not None or altitude_m is not None:
                    notes.append("g_mps2 given: body/altitude_m were not used for gravity")
            elif body is not None:
                with _bodies() as book:
                    b = book.get(body)
                g = rocket.gravity_at(b.mu, b.radius + (altitude_m or 0.0))
                g_src = f"{b.name} at {altitude_m or 0.0:.0f} m ({book.source()})"
            else:
                g = None
                notes.append("pass body or g_mps2 to get TWR and hover throttle")
            if g is not None:
                out["gravity_mps2"] = g
                out["gravity_source"] = g_src
                for label, m in (("start", m0), ("end", m1)):
                    if m is not None:
                        out[f"twr_{label}"] = rocket.twr(thrust, m, g)
                        out[f"hover_throttle_{label}"] = rocket.hover_throttle(thrust, m, g)
    except ValueError as exc:
        raise AstraError(str(exc), "check masses (t), thrust (kN), isp (s) and dv (m/s)") from exc
    if not out:
        raise AstraError("nothing to compute from these inputs",
                         "give isp_s with two of (mass_start_t, mass_end_t, dv_mps); and/or thrust_kn with a mass "
                         "and Δv (burn time) or a mass and body/g_mps2 (TWR)")
    out["notes"] = notes
    return out


@tool("compute", needs_game=False)
def compute_descent(
    body: BodyArg,
    altitude_m: Annotated[float, Field(description="Height above the terrain directly below, m (telemetry surface_altitude minus your CoM-to-landing-legs offset).")],
    vertical_speed_mps: Annotated[float, Field(description="Vertical speed, m/s, positive UP (descending = negative), as telemetry reports it.")],
    horizontal_speed_mps: Annotated[float, Field(description="Surface-relative horizontal speed, m/s.")],
    mass_t: Annotated[float, Field(description="Current mass, t.")],
    thrust_kn: Annotated[float, Field(description="Full thrust of the engines you will brake with, kN, at the local pressure.")],
    isp_s: Annotated[float | None, Field(description="Isp of those engines, s; include to account for mass loss (else constant mass, conservative).")] = None,
    throttle: Annotated[float | None, Field(description="Fraction of full thrust you plan to brake with (your control margin), 0-1. Omit for full thrust: the physical limit.")] = None,
    reaction_time_s: Annotated[float | None, Field(description="Seconds of lag you allow between deciding and full thrust (added as fall distance). Omit for zero.")] = None,
    terrain_height_m: Annotated[float | None, Field(description="Height of the terrain below you above sea level, m (telemetry altitude minus surface altitude); sets the local gravity. Omit to take gravity at sea level + altitude_m (slightly high over high ground, i.e. conservative).")] = None,
) -> dict:
    """Braking calculator for a powered landing: suicide-burn height and timing, hover throttle, feasibility.

    Returns local gravity, TWR and hover throttle; ``vertical_only`` (1-D constant-mass stop of the
    vertical speed alone); ``retrograde_burn`` (a simulated burn held retrograde to the surface
    velocity until at rest: altitude and ground distance consumed, time, Δv); ``burn_start_altitude_m``
    (altitude consumed plus reaction allowance); ``margin_m`` (altitude_m minus that; negative = too
    late); ``time_to_burn_start_s`` (free-fall coast until the burn must begin); and free-fall impact
    time. Flat ground, no drag, uniform gravity: add your own terrain and control margins.
    """
    with _bodies() as book:
        b = book.get(body)
    for name, val in (("altitude_m", altitude_m), ("vertical_speed_mps", vertical_speed_mps),
                      ("horizontal_speed_mps", horizontal_speed_mps), ("mass_t", mass_t), ("thrust_kn", thrust_kn),
                      ("isp_s", isp_s), ("reaction_time_s", reaction_time_s), ("terrain_height_m", terrain_height_m)):
        _need(val is None or math.isfinite(val), f"{name} must be a finite number")
    _need(0 < (throttle if throttle is not None else 1.0) <= 1.0, "throttle must be in (0, 1]")
    _need(altitude_m >= 0, "altitude_m must be >= 0 (height above terrain)")
    _need(mass_t > 0 and thrust_kn > 0, "mass_t and thrust_kn must be positive",
          "thrust_kn is the full thrust of the engines you will brake with (vessel_stages shows it per stage)")
    _need(isp_s is None or isp_s > 0, "isp_s must be positive")
    _need(reaction_time_s is None or reaction_time_s >= 0, "reaction_time_s must be >= 0")
    frac = throttle if throttle is not None else 1.0
    lag = reaction_time_s or 0.0
    m = mass_t * 1000.0
    f_full = thrust_kn * 1000.0
    f = f_full * frac
    r_craft = b.radius + (terrain_height_m or 0.0) + altitude_m
    _need(r_craft > 0, "terrain_height_m + altitude_m lies below the body's centre")
    g = rocket.gravity_at(b.mu, r_craft)
    twr_full = rocket.twr(f_full, m, g)
    out: dict[str, Any] = {"body": b.name, "gravity_mps2": g, "twr_full": twr_full, "twr_planned": twr_full * frac,
                           "hover_throttle": rocket.hover_throttle(f_full, m, g)}
    descending = -min(0.0, vertical_speed_mps)
    if descending > 0:
        try:
            vs = rocket.vertical_stop(descending, f / m, g)
            out["vertical_only"] = {"stop_distance_m": vs.distance_m, "stop_time_s": vs.time_s,
                                    "net_decel_mps2": vs.net_decel_mps2}
        except ValueError as exc:
            out["vertical_only"] = {"feasible": False, "reason": str(exc)}

    def need_alt(vz: float, steps: int) -> float:
        rb = rocket.retro_burn(horizontal_speed_mps, vz, m, f, g, isp_s, steps=steps)
        return rb.altitude_lost_m + lag * max(0.0, -vz)

    out["assumptions"] = [
        "flat ground at the height given, no drag, uniform gravity at this altitude"
        + ("" if terrain_height_m is not None else " (taken above sea level: no terrain_height_m given)"),
        "thrust held exactly retrograde to the surface velocity" + ("" if isp_s else "; constant mass (conservative)"),
        f"braking thrust {frac:.0%} of full; reaction allowance {lag:.1f} s",
    ]
    relief = horizontal_speed_mps ** 2 / r_craft
    if relief > 0.05 * g:  # flat-ground model: say how far from the truth it is at orbital-ish speeds
        out["assumptions"].append(
            f"centrifugal relief v_h²/r = {relief:.2f} m/s² ({relief / g:.0%} of gravity) and the ground curving away "
            "are ignored: conservative, so the burn start and free-fall impact come earlier than in reality")
    out["source"] = book.source()
    try:
        rb = rocket.retro_burn(horizontal_speed_mps, vertical_speed_mps, m, f, g, isp_s)
    except ValueError as exc:
        out.update(feasible=False, reason=str(exc))
        return out
    start_alt = rb.altitude_lost_m + lag * descending
    out["retrograde_burn"] = {"altitude_consumed_m": rb.altitude_lost_m, "ground_distance_m": rb.downrange_m,
                              "burn_time_s": rb.time_s, "dv_used_mps": rb.dv_used_mps,
                              "mass_after_t": rb.final_mass_kg / 1000, "stops": rb.stopped}
    out["burn_start_altitude_m"] = start_alt
    out["margin_m"] = altitude_m - start_alt
    out["feasible"] = rb.stopped and out["margin_m"] >= 0
    if not rb.stopped:
        out["reason"] = "the braking thrust cannot hold the craft against gravity at the end of the burn"
    elif out["margin_m"] < 0:
        out["reason"] = "too late at this throttle: the burn needs more altitude than there is"
    impact = (vertical_speed_mps + math.sqrt(vertical_speed_mps ** 2 + 2 * g * altitude_m)) / g
    out["free_fall_impact_s"] = impact
    if out["margin_m"] > 0 and rb.stopped:
        lo, hi = 0.0, impact
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            vz = vertical_speed_mps - g * mid
            h = altitude_m + vertical_speed_mps * mid - 0.5 * g * mid * mid
            if h - need_alt(vz, 800) > 0:
                lo = mid
            else:
                hi = mid
            if hi - lo < 0.02:
                break
        out["time_to_burn_start_s"] = lo
    return out


@tool("compute", needs_game=False)
def compute_ascent_estimate(
    body: BodyArg,
    orbit_alt_m: Annotated[float, Field(description="Altitude of the circular orbit to reach, m above sea level.")],
    launch_latitude_deg: Annotated[float | None, Field(description="Launch site latitude, deg (omit for the equator).")] = None,
    inclination_deg: Annotated[float | None, Field(description="Target inclination, deg (omit to launch due east into the lowest inclination the latitude allows).")] = None,
    liftoff_twr: Annotated[float | None, Field(description="Your vehicle's liftoff thrust-to-weight on this body; narrows the loss band. Omit to see a spread of TWRs.")] = None,
) -> dict:
    """ESTIMATE of the Δv to reach orbit from the surface: exact ideal part plus a labeled loss RANGE.

    ``ideal_dv_mps`` is exact and a hard lower bound: an impulsive burn at the surface into an ellipse
    reaching the orbit, credited with the surface rotation, plus circularization. Losses are MODELS
    with stated assumptions (see ``assumptions``): gravity loss of a level acceleration burn at the
    given TWR, plus, on bodies with air, a vertical climb through 1-3 pressure scale heights (or, on
    airless bodies, up to the highest sampled terrain). Drag is NOT included. Use the band for sizing
    and read the real cost from telemetry in flight.
    """
    _finite(orbit_alt_m=orbit_alt_m, launch_latitude_deg=launch_latitude_deg, inclination_deg=inclination_deg,
            liftoff_twr=liftoff_twr)
    with _bodies() as book:
        b = book.get(body)
    _need(b.has_solid_surface, f"{b.name} has no solid surface to launch from")
    _need(orbit_alt_m > 0, "orbit_alt_m must be positive")
    _need(launch_latitude_deg is None or -90 < launch_latitude_deg < 90, "launch_latitude_deg must be within (-90, 90)")
    _need(inclination_deg is None or 0 <= inclination_deg <= 180, "inclination_deg must be within 0-180")
    warnings = [w for w in (_terrain_warning(b, orbit_alt_m, "orbit altitude"),) if w]
    if b.radius + orbit_alt_m > b.soi:
        warnings.append(f"orbit radius is beyond {b.name}'s SOI ({b.soi:.0f} m): no such orbit exists")
    lat = math.radians(launch_latitude_deg or 0.0)
    inc = math.radians(inclination_deg) if inclination_deg is not None else abs(lat)
    r_s, r_o = b.radius, b.radius + orbit_alt_m
    az = _physics(rocket.launch_azimuth, lat, inc, hint="choose an inclination >= |launch latitude|")
    v_rot = b.equator_speed * math.cos(lat) * (1 if b.rotational_period > 0 else -1)
    a_t = orbits.semi_major_axis(r_s, r_o)
    v_pe = orbits.vis_viva_speed(b.mu, r_s, a_t)
    circ = orbits.circular_speed(b.mu, r_o) - orbits.vis_viva_speed(b.mu, r_o, a_t)
    ideal = rocket.rotation_assisted_dv(v_pe, v_rot, az) + circ
    v_c_surface = orbits.circular_speed(b.mu, r_s)
    g = b.mu / r_s ** 2
    assumptions = [
        "ideal: impulsive burn at the surface into a transfer ellipse to the orbit altitude, then circularize",
        "gravity loss: level burn with constant thrust acceleration TWR*g tilted to cancel gravity minus centrifugal "
        "relief (mass loss ignored: conservative)",
        "drag not included (depends on the craft's drag cubes and your trajectory)",
    ]
    if b.has_atmosphere and len(b.atmosphere) >= 2:
        prof = [(h, p) for h, p, _ in b.atmosphere]
        climb = (rocket.altitude_at_pressure_fraction(prof, math.exp(-1)),
                 rocket.altitude_at_pressure_fraction(prof, math.exp(-3)))
        assumptions.append(f"vertical climb before pitching over: {climb[0]:.0f}-{climb[1]:.0f} m "
                           "(pressure down to e^-1..e^-3 of the surface value)")
    elif b.has_atmosphere:
        climb = (0.0, 0.0)
        assumptions.append("no cached pressure profile for this atmosphere: vertical climb not modelled")
    else:
        climb = (0.0, b.max_terrain_m or 0.0)
        assumptions.append(f"vertical climb to clear terrain: 0-{climb[1]:.0f} m"
                           + ("" if b.max_terrain_m is not None else " (max terrain not sampled yet: body_info or a whole-body compute_terrain samples it)"))
    if climb[1] <= climb[0]:
        assumptions.append("no climb range known: each band runs from the loss-free ideal up to the model")
    twrs = [liftoff_twr] if liftoff_twr is not None else [1.3, 1.5, 2.0, 3.0, 5.0]
    table = []
    for n in twrs:
        _need(n > 1, "liftoff_twr must exceed 1 to leave the ground")
        level = rocket.level_burn_dv(0.0, v_c_surface, n) - v_c_surface
        lo = ideal + level + rocket.vertical_climb_gravity_loss(g, n, climb[0])
        hi = ideal + level + rocket.vertical_climb_gravity_loss(g, n, climb[1])
        table.append({"twr": n, "band_mps": [lo if climb[1] > climb[0] else ideal, hi]})
    return {
        "label": "ESTIMATE (models with the assumptions below; drag excluded)",
        "body": b.name, "orbit_alt_m": orbit_alt_m, "orbital_speed_mps": orbits.circular_speed(b.mu, r_o),
        "surface_rotation_speed_mps": v_rot, "launch_azimuth_deg": _DEG(az),
        "rotation_assist_mps": v_pe + circ - ideal, "ideal_dv_mps": ideal,
        "estimate_band_mps": [min(t["band_mps"][0] for t in table), max(t["band_mps"][1] for t in table)],
        "by_twr": table, "assumptions": assumptions, "warnings": warnings, "source": book.source(),
    }


# ---------------------------------------------------------------------------------------------
# live orbit helpers


def _kepler_of(o: Any) -> KeplerOrbit:
    return KeplerOrbit(o.body.gravitational_parameter, o.semi_major_axis, o.eccentricity, o.inclination,
                       o.longitude_of_ascending_node, o.argument_of_periapsis, o.mean_anomaly_at_epoch, o.epoch)


def _krpc_axes(kep: KeplerOrbit):
    """State function in kRPC axes (the parent's non-rotating frame): KSP elements map (x, y, z) -> (x, z, y)."""
    def state(ut: float) -> tuple[Vec3, Vec3]:
        r, v = kep.state_at(ut)
        return (r[0], r[2], r[1]), (v[0], v[2], v[1])
    return state


def _patches(orbit: Any, now: float, limit: int = 5) -> list[dict]:
    out, o = [], orbit
    while o is not None and len(out) < limit:
        e = o.eccentricity
        t_soi = o.time_to_soi_change
        out.append({"body": o.body.name, "periapsis_alt_m": o.periapsis_altitude,
                    "apoapsis_alt_m": o.apoapsis_altitude if e < 1 else None, "eccentricity": e,
                    "inclination_deg": _DEG(o.inclination),
                    "soi_change_ut": now + t_soi if math.isfinite(t_soi) else None})
        if not math.isfinite(t_soi):
            break
        o = o.next_orbit
    return out


def _burn(vessel: Any, dv: float) -> dict:
    thrust, isp, mass = vessel.available_thrust, vessel.specific_impulse, vessel.mass
    if thrust > 0 and isp > 0:
        return {"burn_time_s": rocket.burn_time(dv, mass, thrust, isp),
                "half_dv_time_s": rocket.half_dv_time(dv, mass, thrust, isp),
                "burn_basis": f"engines active now: {thrust / 1000:.1f} kN, Isp {isp:.0f} s, mass {mass / 1000:.3f} t "
                              "(wrong if the burn spans a staging event)"}
    return {"burn_time_s": None, "half_dv_time_s": None,
            "burn_basis": "no active engine can thrust now (available_thrust = 0): activate/stage engines, or use "
                          "compute_rocket with the stage's thrust and Isp"}


class _Trial:
    """One temporary maneuver node: asks KSP's patched-conic solver 'what if', then is removed.

    Editing the node while the game is paused is enough to update ``node.orbit``: kRPC's
    ``Control.add_node`` and every ``Node`` setter end in ``PatchedConicSolver.UpdateFlightPlan()``,
    whose ``CheckNextManeuver`` rebuilds the node's ``nextPatch`` from the burn
    (``Orbit.UpdateFromFixedVectors``) and its SOI transitions (``PatchedConics.CalculatePatch``)
    inside the RPC itself. Only the solver's per-frame ``Update()`` stops while paused (it returns on
    ``Planetarium.Pause`` / ``timeScale == 0``), and that pass re-solves the vessel's own patches, which
    cannot change while paused anyway (node edits never touch them). Each update assigns the node a NEW
    ``nextPatch`` object, so an Orbit proxy read before an edit is stale: always read ``node.orbit``
    after the latest ``set``. (Read from the IL of KRPC.SpaceCenter.dll and KSP 1.12.5's
    Assembly-CSharp.dll; not yet exercised live.)

    Edits before ``check_before_ut`` (the current patch's end or the first existing node, whichever is
    earlier) are still cross-checked: KSP's post-burn orbital energy must equal the model's. Should a
    future KSP/kRPC defer that update to a frame, the check fails; the sim is then allowed to tick
    briefly (``ksp().running()``, which restores the pause) and the check repeats before giving up.
    ``ticked_s`` reports how long the game ran for that (normally 0)."""

    REFRESH_S = 0.5  # real seconds of sim time allowed for a deferred solver update

    def __init__(self, k: Any, vessel: Any, craft: KeplerOrbit, check_before_ut: float):
        self.k, self.vessel, self.craft, self.check_before = k, vessel, craft, check_before_ut
        self.node = None
        self._sent: tuple[float, float, float, float] | None = None  # values last sent to the node
        self.ticked_s = 0.0

    def set(self, ut: float, pro: float, nor: float, rad: float) -> Any:
        want = (ut, pro, nor, rad)
        if self.node is None:
            self.node = self.vessel.control.add_node(ut, prograde=pro, normal=nor, radial=rad)
        else:
            # Each setter is one RPC plus one full flight-plan update: send only what changed (a
            # pattern search moves one variable at a time).
            n, sent = self.node, self._sent or (None,) * 4
            for name, value, old in zip(("ut", "prograde", "normal", "radial"), want, sent):
                if value != old:
                    setattr(n, name, value)
        self._sent = want
        self._confirm(ut, pro, nor, rad)
        return self.node

    def _stale(self, ut: float, pro: float, nor: float, rad: float) -> bool:
        """True if KSP's post-burn energy disagrees with the model (a prediction not yet updated).
        Burns beyond ``check_before`` cannot be modelled here and count as fresh."""
        if ut >= self.check_before:
            return False
        mu = self.craft.mu
        r, v = self.craft.state_at(ut)
        rn, vn = vnorm(r), vnorm(v)
        energy = ((vn + pro) ** 2 + nor ** 2 + rad ** 2) / 2 - mu / rn
        # Control.add_node takes the Δv components as 32-bit floats (the Node setters take doubles), so
        # a component sent only at creation is rounded by up to 2^-24 of itself: allow for that.
        v_after = math.sqrt(max(0.0, 2.0 * (energy + mu / rn)))
        f32 = 1.2e-7 * (abs(pro) + abs(nor) + abs(rad)) * (v_after + 1.0)
        sma = self.node.orbit.semi_major_axis
        got = -mu / (2 * sma) if sma else 0.0
        return not abs(got - energy) <= 1e-6 * mu / rn + f32

    def _confirm(self, ut: float, pro: float, nor: float, rad: float) -> None:
        if not self._stale(ut, pro, nor, rad):
            return
        t0 = time.monotonic()
        try:
            with self.k.running():
                while time.monotonic() - t0 < self.REFRESH_S:
                    time.sleep(0.05)
                    if not self._stale(ut, pro, nor, rad):
                        break
        finally:
            self.ticked_s += time.monotonic() - t0
        if self._stale(ut, pro, nor, rad):
            raise AstraError("KSP's predicted orbit did not match the trial node, even after letting the game "
                             f"run {self.REFRESH_S:.1f} s to refresh it",
                             "call game_status (the flight scene may be changing, or the craft is under thrust or "
                             "drag); retry once it is stable, or plan without the KSP prediction")

    def remove(self) -> None:
        if self.node is None:
            return
        node, self.node = self.node, None
        try:
            node.remove()
        except Exception as exc:  # noqa: BLE001 — a node left behind must be reported, not hidden
            raise AstraError(f"the temporary planning node could not be removed ({type(exc).__name__}: {exc})",
                             "list nodes with node_list and delete the extra one with node_delete before flying") from exc


@contextlib.contextmanager
def _paused(k: KSP) -> Iterator[None]:
    """Hold the game paused for the block and restore the previous pause state however it exits."""
    was = k.paused
    if not was:
        k.set_paused(True)
    try:
        yield
    finally:
        if not was:
            k.set_paused(False)


@dataclass
class _Live:
    k: KSP
    vessel: Any
    orbit: Any
    body: Any
    mu: float
    radius: float
    now: float
    patch_end: float  # UT of the current patch's SOI change (inf if none)
    existing_node_uts: list[float]
    craft: KeplerOrbit
    trial: _Trial

    def state(self, ut: float) -> tuple[Vec3, Vec3]:
        return _krpc_axes(self.craft)(ut)


@contextlib.contextmanager
def _planning() -> Iterator[_Live]:
    """Pause, snapshot the active vessel's orbit, and guarantee the trial node is removed."""
    k = ksp()
    vessel = k.vessel()
    with _paused(k):
        o = vessel.orbit
        now = k.sc.ut
        t_soi = o.time_to_soi_change
        patch_end = now + t_soi if math.isfinite(t_soi) else math.inf
        existing = sorted(n.ut for n in vessel.control.nodes)
        craft = _kepler_of(o)
        _need(all(math.isfinite(x) for x in (craft.mu, craft.a, craft.e, craft.inc, craft.lan, craft.argpe,
                                             craft.mean_anomaly_at_epoch, craft.epoch)) and craft.a != 0,
              "KSP reports no defined orbit for the active vessel (non-finite orbital elements)",
              "this happens landed, on the pad or mid-scene-change: check telemetry; plan once the craft is flying")
        trial = _Trial(k, vessel, craft, min([patch_end] + existing))
        live = _Live(k, vessel, o, o.body, o.body.gravitational_parameter, o.body.equatorial_radius, now,
                     patch_end, existing, craft, trial)
        try:
            yield live
        except BaseException as exc:
            try:
                trial.remove()
            except AstraError as cleanup:  # keep the original failure and add the leftover node to it
                if isinstance(exc, Exception):
                    raise AstraError(f"{exc}\nALSO: {cleanup.message}", cleanup.hint) from exc
            raise
        trial.remove()


def _resolve_target(k: KSP, name: str) -> tuple[str, Any]:
    """('body'|'vessel', object) for a body or vessel name (case-insensitive; vessel prefix match)."""
    for n, b in k.sc.bodies.items():
        if n.lower() == name.strip().lower():
            return "body", b
    vessels = k.sc.vessels
    want = name.strip().lower()
    for pred in (lambda s: s == want, lambda s: s.startswith(want)):
        hits = [v for v in vessels if pred(v.name.strip().lower())]
        if len(hits) == 1:
            return "vessel", hits[0]
    raise AstraError(f"no body or unique vessel named {name!r}", "check names with game_list_vessels / body_info")


# ---------------------------------------------------------------------------------------------
# maneuver planner

_KINDS = Literal["circularize", "set_apsis", "deorbit_to_periapsis", "plane_change", "hohmann_to_body",
                 "return_from_moon", "capture_at_periapsis"]
_AT = Literal["apoapsis", "periapsis", "ut", "ascending_node", "descending_node", "nearest_node", "cheapest_node"]
_KIND_RULES: dict[str, tuple[set[str], set[str], set[str]]] = {
    # kind: (required args, optional args, allowed `at` values)
    "circularize": ({"at"}, {"ut", "earliest_ut"}, {"apoapsis", "periapsis", "ut"}),
    "set_apsis": ({"at", "target_alt_m"}, {"ut", "earliest_ut"}, {"apoapsis", "periapsis", "ut"}),
    "deorbit_to_periapsis": ({"at", "target_alt_m"}, {"ut", "earliest_ut"}, {"apoapsis", "periapsis", "ut"}),
    "plane_change": ({"at"}, {"target", "target_inclination_deg", "earliest_ut"},
                     {"ascending_node", "descending_node", "nearest_node", "cheapest_node"}),
    "hohmann_to_body": ({"target"}, {"earliest_ut"}, set()),
    "return_from_moon": ({"target_alt_m"}, {"earliest_ut"}, set()),
    "capture_at_periapsis": ({"target_alt_m"}, {"target"}, set()),
}


def _node_components(r: Vec3, v: Vec3, dv_vec_rt: tuple[float, float, float]) -> tuple[float, float, float]:
    """Map a Δv given as (radial-out, horizontal-along-track, normal) into node (prograde, normal, radial)."""
    fpa = orbits.fpa_of(r, v)
    d_r, d_t, d_n = dv_vec_rt
    return (d_r * math.sin(fpa) + d_t * math.cos(fpa), d_n, d_r * math.cos(fpa) - d_t * math.sin(fpa))


@tool("compute")
def compute_maneuver(
    kind: Annotated[_KINDS, Field(description=(
        "circularize (at apoapsis|periapsis|ut); set_apsis (burn at apoapsis|periapsis|ut, move the OPPOSITE "
        "side of the orbit to target_alt_m); deorbit_to_periapsis (retrograde at apoapsis|periapsis|ut to lower "
        "periapsis to target_alt_m, may be negative = impact); plane_change (at a node: match `target`'s orbit "
        "plane, or reach target_inclination_deg); hohmann_to_body (moon/planet `target` in your SOI: phasing + "
        "prograde transfer burn); return_from_moon (escape your moon onto a parent orbit with periapsis "
        "target_alt_m); capture_at_periapsis (retrograde at the periapsis of the patch around `target`, or the "
        "current one, to an apoapsis of target_alt_m; equal to the periapsis altitude = circular)."))],
    at: Annotated[_AT | None, Field(description=(
        "Where to burn: apoapsis|periapsis|ut (with `ut`) for circularize/set_apsis/deorbit; "
        "ascending_node|descending_node|nearest_node|cheapest_node (slowest point) for plane_change."))] = None,
    target_alt_m: Annotated[float | None, Field(description="Target altitude above the relevant body's sea level, m (meaning depends on kind).")] = None,
    target: Annotated[str | None, Field(description="Body or vessel name: plane_change (match its plane), hohmann_to_body (the body), capture_at_periapsis (the body whose SOI patch to use).")] = None,
    target_inclination_deg: Annotated[float | None, Field(description="plane_change: wanted inclination relative to the body's equator, deg (0-180).")] = None,
    ut: Annotated[float | None, Field(description="Burn UT when at='ut' (game seconds).")] = None,
    earliest_ut: Annotated[float | None, Field(description="Plan the first opportunity at or after this UT (omit for now).")] = None,
) -> dict:
    """Plan one burn from the active vessel's live orbit and return the node parameters (no node is created).

    Returns {ut, time_to_s, prograde_mps, normal_mps, radial_mps, dv_mps, burn_time_s, half_dv_time_s,
    predicted: {model, ksp}, math, notes}. ``predicted.ksp`` is KSP's own patched-conic result for the
    burn (a temporary node is placed, read, and removed; the game is paused meanwhile); plane_change
    adds ``predicted.ksp_check`` (KSP's inclination, or relative inclination to `target`, after the
    burn). Burn time uses
    the engines active now; null with a reason if none can thrust. Create the node yourself with
    node_create, then verify with orbit_info. Existing nodes before this UT are included in KSP's
    prediction (see notes).
    """
    required, optional, at_values = _KIND_RULES[kind]
    given = {n for n, v in (("at", at), ("target_alt_m", target_alt_m), ("target", target),
                            ("target_inclination_deg", target_inclination_deg), ("ut", ut),
                            ("earliest_ut", earliest_ut)) if v is not None}
    missing, extra = required - given, given - required - optional
    _need(not missing, f"{kind} needs {sorted(missing)}", f"{kind} takes {sorted(required)} and optionally {sorted(optional)}")
    _need(not extra, f"{kind} does not use {sorted(extra)}", f"{kind} takes {sorted(required)} and optionally {sorted(optional)}")
    if at is not None:
        _need(at in at_values, f"{kind} cannot burn at '{at}'", f"choose one of {sorted(at_values)}")
    _need((at == "ut") == (ut is not None), "pass `ut` exactly when at='ut'")
    if kind == "plane_change":
        _need((target is None) != (target_inclination_deg is None),
              "plane_change needs exactly one of target or target_inclination_deg")
    _finite(target_alt_m=target_alt_m, target_inclination_deg=target_inclination_deg, ut=ut, earliest_ut=earliest_ut)

    with _planning() as lv:
        if ut is not None:  # before any trial node exists: KSP silently deletes nodes placed in the past
            _need(ut >= lv.now, f"ut {ut:.1f} is in the past (now {lv.now:.1f})", "choose a future burn time")
            _need(ut < lv.patch_end, f"ut {ut:.1f} falls after the SOI change at UT {lv.patch_end:.0f}",
                  "plan it from the next patch, or burn earlier")
        t0 = max(lv.now, earliest_ut if earliest_ut is not None else lv.now)
        planner = {"circularize": _plan_circularize, "set_apsis": _plan_set_apsis,
                   "deorbit_to_periapsis": _plan_set_apsis, "plane_change": _plan_plane_change,
                   "hohmann_to_body": _plan_hohmann_to_body, "return_from_moon": _plan_return_from_moon,
                   "capture_at_periapsis": _plan_capture}[kind]
        plan = planner(lv, kind=kind, at=at, t0=t0, ut=ut, target_alt_m=target_alt_m, target=target,
                       target_inclination_deg=target_inclination_deg)
        p_ut, pro, nor, rad = plan.pop("ut"), plan.pop("prograde"), plan.pop("normal", 0.0), plan.pop("radial", 0.0)
        _need(p_ut >= lv.now, "the planned burn time is in the past")
        if kind != "capture_at_periapsis":
            _need(p_ut < lv.patch_end, f"the burn would fall after the SOI change at UT {lv.patch_end:.0f}",
                  "plan it from the next patch, or burn earlier")
        notes = plan.pop("notes", [])
        encounter_target = plan.pop("encounter_target", None)
        verify = plan.pop("verify", None)
        notes += _existing_node_notes(lv, p_ut, p_ut)
        ksp_pred: list[dict] | None = None
        ksp_check: dict | None = None
        try:
            node = lv.trial.set(p_ut, pro, nor, rad)
            ksp_pred = _patches(node.orbit, lv.now)
            if encounter_target is not None:
                plan["encounter"] = _encounter(node.orbit, ksp_pred, encounter_target)
            if verify is not None:
                ksp_check = verify(node)
        except AstraError:
            raise
        except Exception as exc:  # noqa: BLE001 — e.g. node editing unavailable in career mode
            notes.append(f"could not read KSP's prediction ({type(exc).__name__}: {exc}); model values only")
        if lv.trial.ticked_s > 0:
            notes.append(f"the game ran {lv.trial.ticked_s:.2f} s (real time) to refresh KSP's prediction; "
                         "the pause state was restored")
        dv = math.sqrt(pro * pro + nor * nor + rad * rad)
        return {"kind": kind, "ut": p_ut, "time_to_s": p_ut - lv.now, "prograde_mps": pro, "normal_mps": nor,
                "radial_mps": rad, "dv_mps": dv, **_burn(lv.vessel, dv),
                "predicted": {"model": plan.pop("model", {}), "ksp": ksp_pred,
                              **({"ksp_check": ksp_check} if ksp_check is not None else {})},
                "math": plan.pop("math", ""), **plan, "notes": notes,
                "current_orbit": _patches(lv.orbit, lv.now, limit=1)[0]}


def _existing_node_notes(lv: _Live, ut_first: float, ut_last: float) -> list[str]:
    """How the crew's own nodes interact with the trial node placed in [ut_first, ut_last]."""
    notes = []
    before = [u for u in lv.existing_node_uts if u < ut_first]
    after = [u for u in lv.existing_node_uts if u > ut_last]
    inside = [u for u in lv.existing_node_uts if ut_first <= u <= ut_last]
    if before:
        notes.append(f"{len(before)} existing node(s) before this burn are included in KSP's prediction")
    if inside:
        notes.append(f"{len(inside)} existing node(s) inside the burn-time window: KSP's prediction changes with "
                     "which side of them the trial burn falls")
    if after:
        notes.append(f"{len(after)} existing node(s) after this burn (first at UT {after[0]:.0f}) cut or alter KSP's "
                     "predicted trajectory beyond them: delete them (node_delete) for a clean prediction")
    return notes


def _encounter(post_burn: Any, patches: list[dict], tgt: Any) -> dict:
    """Did KSP's prediction enter ``tgt``'s SOI? Else its closest-approach estimate."""
    hit = next((p for p in patches if p["body"] == tgt.name), None)
    if hit is not None:
        return {"found": True, "periapsis_alt_m": hit["periapsis_alt_m"], "inclination_deg": hit["inclination_deg"]}
    o = post_burn
    while o is not None and o.body != tgt.orbit.body:
        o = o.next_orbit if math.isfinite(o.time_to_soi_change) else None
    if o is None:
        return {"found": False}
    return {"found": False, "closest_approach_m": o.distance_at_closest_approach(tgt.orbit),
            "note": "KSP single-conic closest-approach estimate; refine with compute_node_search"}


def _apsis_ut(lv: _Live, at: str, t0: float) -> float:
    if at == "apoapsis":
        _need(lv.craft.e < 1, "a hyperbolic orbit has no apoapsis", "burn at periapsis or at a chosen ut")
    return _physics(lv.craft.next_ut_at, math.pi if at == "apoapsis" else 0.0, t0,
                    hint="the periapsis of this escape trajectory is behind you; plan at a chosen ut")


def _ksp_pick_sign(lv: _Live, t: float, pro: float, nor: float, rad: float, which: str,
                   score) -> tuple[float, float, str | None]:
    """Try the ``which`` ('normal' | 'radial') component with both signs on the trial node and keep
    the one KSP's own prediction scores lower, instead of trusting a node-frame sign convention.
    Returns (normal, radial, note)."""
    def comps(sign: float) -> tuple[float, float]:
        return (sign * nor, rad) if which == "normal" else (nor, sign * rad)

    try:
        first = score(lv.trial.set(t, pro, *comps(1.0)))
        flipped = score(lv.trial.set(t, pro, *comps(-1.0)))
    except AstraError:
        raise
    except Exception as exc:  # noqa: BLE001 — e.g. no maneuver nodes in this career save
        return nor, rad, f"could not cross-check the {which} sign with KSP ({type(exc).__name__}: {exc})"
    if flipped < first:
        return *comps(-1.0), f"{which} sign chosen by KSP's prediction (the geometric convention guessed the other way)"
    if flipped == first:
        return nor, rad, (f"KSP's prediction did not distinguish the two {which} signs: check predicted.ksp "
                          "before burning")
    return nor, rad, None


def _plan_circularize(lv: _Live, *, at: str, t0: float, ut: float | None, **_: Any) -> dict:
    t = ut if at == "ut" else _apsis_ut(lv, at, t0)
    r, v = lv.state(t)
    rn, vn = vnorm(r), vnorm(v)
    v_c = orbits.circular_speed(lv.mu, rn)
    v_r = vdot(r, v) / rn
    v_h = math.sqrt(max(0.0, vn * vn - v_r * v_r))
    pro, _, rad = _node_components(r, v, (-v_r, v_c - v_h, 0.0))
    notes: list[str] = []
    if abs(rad) > 1e-6 * vn:  # off an apsis the radial part matters: confirm its sign with KSP
        _, rad, note = _ksp_pick_sign(lv, t, pro, 0.0, rad, "radial", lambda node: node.orbit.eccentricity)
        notes += [note] if note else []
    alt = rn - lv.radius
    return {"ut": t, "prograde": pro, "radial": rad, "notes": notes,
            "model": {"periapsis_alt_m": alt, "apoapsis_alt_m": alt},
            "math": "target velocity = horizontal sqrt(mu/r); Δv = target - current, split into prograde/radial "
                    "by the flight-path angle (pure prograde at an apsis)"}


def _plan_set_apsis(lv: _Live, *, kind: str, at: str, t0: float, ut: float | None, target_alt_m: float, **_: Any) -> dict:
    t = ut if at == "ut" else _apsis_ut(lv, at, t0)
    r, v = lv.state(t)
    rn, vn = vnorm(r), vnorm(v)
    rho = lv.radius + target_alt_m
    _need(rho > 0, "target radius must be positive (target_alt_m above -radius)")
    fpa = orbits.fpa_of(r, v)
    v_new = _physics(orbits.speed_for_apsis, lv.mu, rn, fpa, rho,
                     hint="a prograde/retrograde burn here cannot put an apsis at that altitude; try another point")
    dv = v_new - vn
    el = orbits.elements_from_state(lv.mu, r, vscale(vunit(v), v_new))
    notes: list[str] = []
    model = {"periapsis_alt_m": el["r_pe"] - lv.radius,
             "apoapsis_alt_m": el["r_ap"] - lv.radius if math.isfinite(el["r_ap"]) else None}
    if kind == "deorbit_to_periapsis":
        _need(dv < 0, "that periapsis is not below the current trajectory's", "use set_apsis to raise an apsis")
        atm = lv.body.atmosphere_depth if lv.body.has_atmosphere else 0.0
        label = "atmosphere_interface_ut" if atm > 0 else "datum_crossing_ut"
        r_x = lv.radius + atm
        if el["r_pe"] < r_x < el["r_ap"] and el["e"] > 1e-9:
            nu_x = -orbits.true_anomaly_at_radius(el["a"], el["e"], r_x)  # inbound crossing
            dt = orbits.time_between_anomalies(lv.mu, el["a"], el["e"], el["nu"], nu_x)
            if dt >= 0:
                model[label] = t + dt
        if rho > lv.radius and not lv.body.has_atmosphere:
            notes.append("periapsis above the datum: this lowers the orbit but does not reach the ground")
    return {"ut": t, "prograde": dv, "model": model, "notes": notes,
            "math": "burn along the velocity keeps the flight-path angle γ; new speed v with an apsis at ρ: "
                    "v² = 2μ(1/ρ - 1/r) / ((r cosγ/ρ)² - 1) (vis-viva at an apsis)"}


def _plan_plane_change(lv: _Live, *, at: str, t0: float, target: str | None,
                       target_inclination_deg: float | None, **_: Any) -> dict:
    o, craft = lv.orbit, lv.craft
    notes: list[str] = []
    if target is not None:
        _, obj = _resolve_target(lv.k, target)
        t_orbit = obj.orbit
        _need(t_orbit is not None and t_orbit.body == lv.body,
              f"{target} does not orbit {lv.body.name}", "a plane match needs both orbits around the same body")
        theta = o.relative_inclination(t_orbit)
        nu_an, nu_dn = o.true_anomaly_at_an(t_orbit), o.true_anomaly_at_dn(t_orbit)
        sign_an = -1.0

        def score(node: Any) -> float:
            return abs(node.orbit.relative_inclination(t_orbit))

        def verify(node: Any) -> dict:
            return {"relative_inclination_after_deg": _DEG(node.orbit.relative_inclination(t_orbit))}
        goal = f"relative inclination to {target}: {_DEG(theta):.3f} deg -> 0"
    else:
        i_goal = math.radians(target_inclination_deg)
        _need(0 <= target_inclination_deg <= 180, "target_inclination_deg must be within 0-180")
        delta = i_goal - craft.inc
        theta = abs(delta)
        nu_an, nu_dn = -craft.argpe, math.pi - craft.argpe
        sign_an = 1.0 if delta > 0 else -1.0

        def score(node: Any) -> float:
            return abs(node.orbit.inclination - i_goal)

        def verify(node: Any) -> dict:
            return {"inclination_after_deg": _DEG(node.orbit.inclination)}
        goal = f"inclination {_DEG(craft.inc):.3f} -> {target_inclination_deg:.3f} deg"
    options = []
    for label, nu, sign in (("ascending_node", nu_an, sign_an), ("descending_node", nu_dn, -sign_an)):
        try:
            t = craft.next_ut_at(nu, t0)
        except ValueError:
            continue
        r, v = lv.state(t)
        options.append((label, t, sign, r, v))
    _need(bool(options), "no node ahead on this trajectory")
    if at in ("ascending_node", "descending_node"):
        options = [x for x in options if x[0] == at]
        _need(bool(options), f"the {at.replace('_', ' ')} is not ahead on this trajectory")
    elif at == "nearest_node":
        options = [min(options, key=lambda x: x[1])]
    else:  # cheapest: slowest horizontal speed
        options = [min(options, key=lambda x: vnorm(x[4]) * math.cos(orbits.fpa_of(x[3], x[4])))]
    label, t, sign, r, v = options[0]
    fpa = orbits.fpa_of(r, v)
    v_h = vnorm(v) * math.cos(fpa)
    pro = v_h * (math.cos(theta) - 1.0) * math.cos(fpa)
    rad = v_h * (1.0 - math.cos(theta)) * math.sin(fpa)
    nor = sign * v_h * math.sin(theta)
    # Confirm the normal sign with KSP's own prediction rather than trusting a convention.
    if nor != 0.0:
        nor, rad, note = _ksp_pick_sign(lv, t, pro, nor, rad, "normal", score)
        notes += [note] if note else []
    return {"ut": t, "prograde": pro, "normal": nor, "radial": rad, "burn_point": label, "notes": notes,
            "verify": verify, "model": {"plane_rotation_deg": _DEG(theta), "goal": goal},
            "math": "rotate the horizontal velocity v_h about the radius by θ at the node: |Δv| = 2 v_h sin(θ/2); "
                    "normal = ±v_h sinθ, prograde/radial = v_h(cosθ-1) split by the flight-path angle"}


def _plan_hohmann_to_body(lv: _Live, *, t0: float, target: str, **_: Any) -> dict:
    kind, tgt = _resolve_target(lv.k, target)
    _need(kind == "body", f"{target} is a vessel", "hohmann_to_body targets a moon or planet; use a node search for vessels")
    _need(tgt.orbit is not None and tgt.orbit.body == lv.body, f"{tgt.name} does not orbit {lv.body.name}",
          "use compute_transfer_window for a body around a different parent")
    moon = _krpc_axes(_kepler_of(tgt.orbit))
    plan = _physics(orbits.plan_transfer_to_satellite, lv.mu, lv.state, moon, t0)
    notes = []
    if lv.craft.e > 0.01:
        notes.append(f"parking orbit eccentricity {lv.craft.e:.3f}: phasing assumes near-circular; trust KSP's prediction")
    rel_inc = orbits.vangle(orbits.vcross(*lv.state(t0)), orbits.vcross(*moon(t0)))
    return {"ut": plan["ut"], "prograde": plan["prograde_dv"], "notes": notes, "encounter_target": tgt,
            "model": {"relative_inclination_deg": _DEG(rel_inc),  # coplanar model: a tilted target can be missed
                      "transfer_time_s": plan["tof"], "phase_angle_now_deg": _DEG(plan["lead_angle_at_start"]),
                      "phase_angle_at_burn_deg": _DEG(plan["lead_angle"]),
                      "required_phase_angle_deg": _DEG(plan["required_lead"]),
                      "arrival_radius_m": plan["arrival_radius"], "synodic_period_s": plan["synodic_period"]},
            "math": "tof = pi*sqrt(((r1+r2)/2)^3/mu); burn when the target leads by pi minus its sweep during tof; "
                    "the prograde burn puts the apoapsis at the target's radius at arrival. This aims at the target's "
                    "centre: set the encounter periapsis with compute_node_search"}


def _plan_return_from_moon(lv: _Live, *, t0: float, target_alt_m: float, **_: Any) -> dict:
    moon = lv.body
    _need(moon.orbit is not None, f"{moon.name} does not orbit anything", "return_from_moon works from a moon's SOI")
    parent = moon.orbit.body
    _need(lv.craft.e < 1, "the craft must be on a bound orbit around the moon")
    r_pe = parent.equatorial_radius + target_alt_m
    plan = _physics(orbits.plan_escape_from_satellite, parent.gravitational_parameter, lv.mu, lv.state,
                    _krpc_axes(_kepler_of(moon.orbit)), r_pe, t0, moon.sphere_of_influence,
                    hint="check target_alt_m (parent periapsis altitude) and that the orbit around the moon is bound")
    notes = []
    if abs(plan["out_of_plane"]) > math.radians(5):
        notes.append(f"your orbit is tilted {abs(_DEG(plan['out_of_plane'])):.1f} deg from the moon's motion: an "
                     "in-plane burn cannot aim straight back; refine with compute_node_search (normal component)")
    return {"ut": plan["ut"], "prograde": plan["prograde_dv"], "notes": notes,
            "model": {"parent": parent.name, "parent_periapsis_alt_m": plan["parent_periapsis"] - parent.equatorial_radius,
                      "v_inf_mps": plan["v_inf"], "soi_exit_after_s": plan["escape_time"],
                      "out_of_plane_deg": _DEG(plan["out_of_plane"])},
            "math": "patched conics solved exactly: for each Δv the burn time minimizing the parent periapsis is found "
                    "(exit opposite the moon's motion), then Δv is bisected until that periapsis equals the target"}


def _plan_capture(lv: _Live, *, target: str | None, target_alt_m: float, **_: Any) -> dict:
    o, start = lv.orbit, lv.now
    while o is not None:
        if target is None or o.body.name.lower() == target.strip().lower():
            break
        t_soi = o.time_to_soi_change
        _need(math.isfinite(t_soi), f"no patch around {target} on the current trajectory",
              "make an encounter first (hohmann_to_body / compute_node_search)")
        start = lv.now + t_soi
        o = o.next_orbit
    _need(o is not None, f"no patch around {target} on the current trajectory")
    body = o.body
    mu, radius = body.gravitational_parameter, body.equatorial_radius
    kep = _kepler_of(o)
    t_pe = _physics(kep.next_ut_at, 0.0, max(start, lv.now), hint="the periapsis of that patch has passed")
    t_soi = o.time_to_soi_change
    if math.isfinite(t_soi):
        _need(t_pe < lv.now + t_soi, "that patch leaves the SOI before reaching periapsis")
    r_pe = o.periapsis
    rho = radius + target_alt_m
    _need(rho >= r_pe - 1e-6, "target apoapsis is below the periapsis",
          f"target_alt_m must be >= the periapsis altitude {r_pe - radius:.0f} m")
    v_pe = orbits.vis_viva_speed(mu, r_pe, kep.a)
    dv = orbits.vis_viva_speed(mu, r_pe, orbits.semi_major_axis(r_pe, rho)) - v_pe
    book = BodyBook(lv.k)
    notes = [w for w in (_terrain_warning(book.get(body.name), r_pe - radius, "arrival periapsis"),) if w]
    book.save()
    if rho > body.sphere_of_influence:
        notes.append("target apoapsis lies outside the SOI: the craft would not stay captured")
    return {"ut": t_pe, "prograde": dv, "notes": notes,
            "model": {"body": body.name, "periapsis_alt_m": r_pe - radius, "apoapsis_alt_m": target_alt_m,
                      "arrival_speed_mps": v_pe},
            "math": "Δv = v_vis(r_pe, (r_pe+r_ap)/2) - v_vis(r_pe, a_arrival) at the periapsis of the arrival patch"}


# ---------------------------------------------------------------------------------------------
# node search against KSP's patched conics


# extra="forbid": a misspelled key (say `target_periapsis`) must be an error, never a silently dropped
# target that lets the search report "objective met" for a trajectory into the ground.
@with_config(ConfigDict(extra="forbid"))
class NodeObjective(TypedDict, total=False):
    encounter_body: Annotated[str, Field(description="Require the trajectory after the burn to enter this body's SOI; the first patch around it is scored.")]
    target_periapsis_m: Annotated[float, Field(description="Wanted periapsis altitude (m above that body's datum) of the scored patch.")]
    target_apoapsis_m: Annotated[float, Field(description="Wanted apoapsis altitude of the scored patch, m.")]
    target_inclination_deg: Annotated[float, Field(description="Wanted inclination of the scored patch, deg.")]
    periapsis_floor_m: Annotated[float, Field(description="Hard minimum periapsis altitude of the scored patch, m (your terrain/atmosphere clearance).")]
    tolerance_m: Annotated[float, Field(description="Altitude error that counts as met, m. Required with an altitude target.")]
    tolerance_deg: Annotated[float, Field(description="Inclination error that counts as met, deg. Required with target_inclination_deg.")]
    minimize: Annotated[Literal["dv", "error"], Field(description="'dv': cheapest node meeting every target within tolerance; 'error': closest to the targets whatever it costs.")]


@with_config(ConfigDict(extra="forbid"))
class NodeBounds(TypedDict, total=False):
    prograde: Annotated[tuple[float, float], Field(description="[min, max] prograde Δv, m/s.")]
    normal: Annotated[tuple[float, float], Field(description="[min, max] normal Δv, m/s.")]
    radial: Annotated[tuple[float, float], Field(description="[min, max] radial Δv, m/s.")]


@with_config(ConfigDict(extra="forbid"))
class NodeSeed(TypedDict, total=False):
    ut: Annotated[float, Field(description="Seed burn UT, s (clamped into [ut_min, ut_max]).")]
    prograde: Annotated[float, Field(description="Seed prograde Δv, m/s (clamped into bounds.prograde).")]
    normal: Annotated[float, Field(description="Seed normal Δv, m/s (clamped into bounds.normal).")]
    radial: Annotated[float, Field(description="Seed radial Δv, m/s (clamped into bounds.radial).")]


_COMPONENTS = ("prograde", "normal", "radial")


@tool("compute")
def compute_node_search(
    objective: Annotated[NodeObjective, Field(description="What the burn must achieve and what to minimize (see fields).")],
    ut_min: Annotated[float, Field(description="Earliest burn UT to consider, s.")],
    ut_max: Annotated[float, Field(description="Latest burn UT to consider, s (equal to ut_min to fix the time).")],
    bounds: Annotated[NodeBounds, Field(description="Search range per component; a component left out stays at the seed value (or 0).")],
    max_evals: Annotated[int, Field(description="Budget of trial evaluations (each ~5-20 ms of game-side work while paused).", ge=1, le=5000)],
    seed: Annotated[NodeSeed | None, Field(description="Starting node {ut, prograde, normal, radial}, e.g. from compute_maneuver. Omit to seed analytically (Hohmann phasing for a moon encounter) or at the range centres.")] = None,
) -> dict:
    """Search node parameters against KSP's own patched conics until the objective is met.

    Each evaluation edits one temporary node and reads KSP's predicted patch chain (encounter body,
    periapsis, apoapsis, inclination); a missed encounter is scored by KSP's closest-approach estimate
    so the search can find its way in. Coarse sampling (only when the seed misses) is followed by a
    shrinking compass search. The game is paused for the duration and the previous pause state
    restored; the temporary node is always removed. Returns the best node evaluated, whether every
    target is met, the scored patch, the patch chain, the evaluation count, and notes (e.g. how your
    existing nodes affect the prediction). Create the node with node_create.
    """
    obj = dict(objective)
    minimize = obj.get("minimize")
    _need(minimize in ("dv", "error"), "objective.minimize must be 'dv' or 'error'")
    alt_targets = [k for k in ("target_periapsis_m", "target_apoapsis_m", "periapsis_floor_m") if k in obj]
    _need(not alt_targets or obj.get("tolerance_m", 0) > 0, "objective.tolerance_m (> 0) is required with altitude targets")
    _need("target_inclination_deg" not in obj or obj.get("tolerance_deg", 0) > 0,
          "objective.tolerance_deg (> 0) is required with target_inclination_deg")
    _need(minimize == "dv" or any(k in obj for k in ("target_periapsis_m", "target_apoapsis_m", "target_inclination_deg")),
          "minimize='error' needs at least one target_* value")
    _need(any(k in obj for k in ("encounter_body", "target_periapsis_m", "target_apoapsis_m", "target_inclination_deg",
                                 "periapsis_floor_m")),
          "the objective has nothing to achieve, so the cheapest answer is no burn at all",
          "give encounter_body, a target_* value and/or periapsis_floor_m")
    _finite(ut_min=ut_min, ut_max=ut_max, **{f"objective.{k}": v for k, v in obj.items() if not isinstance(v, str)},
            **{f"bounds.{k}": v for k, v in bounds.items()}, **{f"seed.{k}": v for k, v in (seed or {}).items()})
    _need(ut_max >= ut_min, "ut_max must be >= ut_min")
    for name, rng in bounds.items():
        _need(len(rng) == 2 and rng[1] >= rng[0], f"bounds.{name} must be [min, max]")

    with _planning() as lv:
        _need(ut_min >= lv.now, "ut_min is in the past", f"current UT is {lv.now:.1f}")
        enc = None
        if obj.get("encounter_body"):
            kind, enc = _resolve_target(lv.k, obj["encounter_body"])
            _need(kind == "body", "encounter_body must be a celestial body")
        free = [("ut", ut_min, ut_max)] if ut_max > ut_min else []
        free += [(c, *bounds[c]) for c in _COMPONENTS if c in bounds and bounds[c][1] > bounds[c][0]]
        start, seed_source = _search_seed(lv, seed, enc, ut_min, ut_max, bounds)
        dv_scale = sum(hi - lo for name, lo, hi in free if name != "ut") or 1.0
        search = _NodeSearch(lv, obj, enc, start, free, dv_scale, max_evals)
        try:
            x, result = search.run()
        except AstraError:
            raise
        except Exception as exc:  # noqa: BLE001 — kRPC refusing the node (e.g. a career save without nodes)
            raise AstraError(f"placing or reading the trial maneuver node failed ({type(exc).__name__}: {exc})",
                             "call game_status; career saves need the upgraded Tracking Station / Mission Control "
                             "for maneuver nodes. Without nodes, plan with compute_maneuver's model values") from exc
        vals = search.values(x)
        dv = math.sqrt(sum(vals[c] ** 2 for c in _COMPONENTS))
        notes = _existing_node_notes(lv, ut_min, ut_max)
        if lv.trial.ticked_s > 0:
            notes.append(f"the game ran {lv.trial.ticked_s:.2f} s (real time) to refresh KSP's predictions; "
                         "the pause state was restored")
        return {
            "best": {"ut": vals["ut"], "time_to_s": vals["ut"] - lv.now,
                     **{f"{c}_mps": vals[c] for c in _COMPONENTS}, "dv_mps": dv, **_burn(lv.vessel, dv)},
            "objective_met": result["met"], "scored_patch": result.get("scored"), "encounter": result.get("encounter"),
            "closest_approach_m": result.get("closest_approach_m"), "errors_in_tolerances": result.get("errors"),
            "patches": result.get("patches"), "evaluations": search.evals, "stopped_by": search.stopped_by,
            "seed": {"source": seed_source, **start}, "notes": notes,
            "note": "the temporary node was removed; create this node with node_create",
        }


def _search_seed(lv: _Live, seed: NodeSeed | None, enc: Any, ut_min: float, ut_max: float,
                 bounds: NodeBounds) -> tuple[dict, str]:
    """Starting node: the given seed, else Hohmann phasing toward a moon, else the range centres.
    Components outside ``bounds`` keep the seed value (or 0); every value is clamped into range."""
    def clamp(name: str, x: float) -> float:
        lo, hi = (ut_min, ut_max) if name == "ut" else bounds.get(name, (x, x))
        return min(max(x, lo), hi)

    given = dict(seed or {})
    base = {"ut": given.get("ut", 0.5 * (ut_min + ut_max)),
            **{c: given.get(c, 0.5 * sum(bounds[c]) if c in bounds else 0.0) for c in _COMPONENTS}}
    if seed:
        return {k: clamp(k, v) for k, v in base.items()}, "given"
    if (enc is not None and enc.orbit is not None and enc.orbit.body == lv.body and "prograde" in bounds
            and ut_min < lv.patch_end):  # the phasing model only knows the current patch
        try:
            plan = orbits.plan_transfer_to_satellite(lv.mu, lv.state, _krpc_axes(_kepler_of(enc.orbit)), ut_min)
            if plan["ut"] <= ut_max:
                base.update(ut=plan["ut"], prograde=plan["prograde_dv"])
                return {k: clamp(k, v) for k, v in base.items()}, "Hohmann phasing"
        except ValueError:
            pass
    return {k: clamp(k, v) for k, v in base.items()}, "range centres"


class _NodeSearch:
    """Scores trial nodes with KSP's patched conics and runs a bounded Hooke-Jeeves pattern search
    over the free variables, normalized to [0, 1] across their ranges."""

    MISS = 1e6  # every encounter scores below this; misses score above it

    def __init__(self, lv: _Live, obj: dict, enc: Any, base: dict, free: list[tuple[str, float, float]],
                 dv_scale: float, max_evals: int):
        self.lv, self.obj, self.enc, self.base, self.free = lv, obj, enc, base, free
        self.dv_scale, self.max_evals = dv_scale, max_evals
        self.evals, self.stopped_by = 0, "converged"
        self._memo: dict[tuple, dict] = {}
        self.best: tuple[tuple[float, ...], float] | None = None  # lowest-cost point evaluated so far

    def values(self, x: tuple[float, ...]) -> dict:
        vals = dict(self.base)
        for (name, lo, hi), u in zip(self.free, x):
            vals[name] = lo + (hi - lo) * u
        return vals

    def cost(self, x: tuple[float, ...]) -> float:
        return self.evaluate(x)["cost"]

    def evaluate(self, x: tuple[float, ...]) -> dict:
        key = tuple(round(u, 12) for u in x)
        if key in self._memo:
            return self._memo[key]
        self.evals += 1
        v = self.values(x)
        node = self.lv.trial.set(v["ut"], v["prograde"], v["normal"], v["radial"])
        chain, o = [], node.orbit
        while o is not None and len(chain) < 6:
            chain.append(o)
            if not math.isfinite(o.time_to_soi_change):
                break
            o = o.next_orbit
        res: dict[str, Any] = {"dv": math.sqrt(sum(v[c] ** 2 for c in _COMPONENTS))}
        scored = chain[0]
        if self.enc is not None:
            scored = next((p for p in chain if p.body == self.enc), None)
            res["encounter"] = scored is not None
            if scored is None:
                res.update(self._miss(chain), met=False)
        if scored is not None:
            res.update(self._score(scored, res["dv"]))
        self._memo[key] = res
        if self.best is None or res["cost"] < self.best[1]:
            self.best = (tuple(x), res["cost"])
        return res

    def _miss(self, chain: list) -> dict:
        """Guide toward an encounter: KSP's closest approach in the target's parent SOI, or, before
        reaching that SOI, how far the apoapsis is from leaving the current one."""
        parent = self.enc.orbit.body if self.enc.orbit is not None else None  # None: the star itself
        around = next((p for p in chain if p.body == parent), None) if parent is not None else None
        if around is not None:
            miss = around.distance_at_closest_approach(self.enc.orbit)
            return {"cost": self.MISS + miss / self.enc.sphere_of_influence, "closest_approach_m": miss}
        last = chain[-1]
        ap = last.apoapsis if last.eccentricity < 1 else math.inf
        shortfall = max(0.0, 1.0 - ap / last.body.sphere_of_influence)
        return {"cost": self.MISS * 2 + 1e3 * shortfall}

    def _score(self, p: Any, dv: float) -> dict:
        e = p.eccentricity
        pe, inc = p.periapsis_altitude, _DEG(p.inclination)
        ap = p.apoapsis_altitude if e < 1 else math.inf
        tol_m, tol_deg = self.obj.get("tolerance_m", 1.0), self.obj.get("tolerance_deg", 1.0)
        errors: dict[str, float] = {}
        if "target_periapsis_m" in self.obj:
            errors["periapsis"] = (pe - self.obj["target_periapsis_m"]) / tol_m
        if "target_apoapsis_m" in self.obj:
            errors["apoapsis"] = (ap - self.obj["target_apoapsis_m"]) / tol_m if math.isfinite(ap) else 1e5
        if "target_inclination_deg" in self.obj:
            errors["inclination"] = (inc - self.obj["target_inclination_deg"]) / tol_deg
        err = math.sqrt(sum(min(abs(x), 1e5) ** 2 for x in errors.values()))
        floor = max(0.0, (self.obj["periapsis_floor_m"] - pe) / tol_m) if "periapsis_floor_m" in self.obj else 0.0
        if self.obj["minimize"] == "error":
            cost = err + 100.0 * floor
        else:  # lexicographic: meet every target first, then spend as little as possible
            cost = 1000.0 * (max(0.0, err - 1.0) + floor) + dv / self.dv_scale
        return {"cost": min(cost, 0.9 * self.MISS), "met": err <= 1.0 and floor == 0.0, "errors": errors,
                "scored": {"body": p.body.name, "periapsis_alt_m": pe, "apoapsis_alt_m": ap if math.isfinite(ap) else None,
                           "inclination_deg": inc, "eccentricity": e}}

    def _budget_left(self) -> bool:
        if self.evals >= self.max_evals:
            self.stopped_by = "max_evals"
            return False
        return True

    def run(self) -> tuple[tuple[float, ...], dict]:
        x = tuple((self.base[name] - lo) / (hi - lo) for name, lo, hi in self.free)
        fx = self.cost(x)
        if not self.free:
            self.stopped_by = "nothing free to vary"
            return self._final(x)
        if fx >= self.MISS:  # the seed misses: sample the box to find a way in
            rng = random.Random(0)
            for _ in range(max(1, self.max_evals // 3)):
                if not self._budget_left():
                    break
                y = tuple(rng.random() for _ in self.free)
                fy = self.cost(y)
                if fy < fx:
                    x, fx = y, fy
        step = 0.05
        while step > 1e-6 and self._budget_left():
            y, fy = self._explore(x, fx, step)
            if fy < fx:
                while self._budget_left():  # pattern move: keep going the way that worked
                    z = tuple(min(1.0, max(0.0, 2 * b - a)) for a, b in zip(x, y))
                    x, fx = y, fy
                    z, fz = self._explore(z, self.cost(z), step)
                    if fz >= fx:
                        break
                    y, fy = z, fz
            else:
                step *= 0.5
        # The pattern loop can stop on the budget right after an improvement it has not adopted yet:
        # report the best point actually evaluated, never a worse one.
        return self._final(self.best[0] if self.best is not None else x)

    def _explore(self, x: tuple[float, ...], fx: float, step: float) -> tuple[tuple[float, ...], float]:
        x = list(x)
        for i in range(len(x)):
            for sgn in (1.0, -1.0):
                if not self._budget_left():
                    return tuple(x), fx
                y = list(x)
                y[i] = min(1.0, max(0.0, y[i] + sgn * step))
                if y[i] == x[i]:
                    continue
                fy = self.cost(tuple(y))
                if fy < fx:
                    x, fx = y, fy
                    break
        return tuple(x), fx

    def _final(self, x: tuple[float, ...]) -> tuple[tuple[float, ...], dict]:
        v = self.values(x)
        node = self.lv.trial.set(v["ut"], v["prograde"], v["normal"], v["radial"])
        return x, {**self.evaluate(x), "patches": _patches(node.orbit, self.lv.now)}


# ---------------------------------------------------------------------------------------------
# interplanetary (or inter-moon) transfer window


@tool("compute", needs_game=False)
def compute_transfer_window(
    origin: Annotated[str, Field(description="Body you depart from (the craft parks around it), e.g. 'Kerbin'.")],
    target: Annotated[str, Field(description="Destination body; must share origin's parent (e.g. 'Duna' from 'Kerbin', 'Minmus' from 'Mun').")],
    parking_alt_m: Annotated[float, Field(description="Altitude of your circular parking orbit around origin, m (sets the ejection Δv).")],
    minimize: Annotated[Literal["departure", "arrival", "total"], Field(description="departure = ejection Δv; arrival = capture Δv (or arrival v_inf without arrival_alt_m); total = both.")],
    earliest_ut: Annotated[float | None, Field(description="Earliest departure UT, s. Omit to start now (needs the game).")] = None,
    search_days: Annotated[float | None, Field(description="Length of the departure search, in 6-hour Kerbin days (21,600 s). Omit for one synodic period (every geometry once).")] = None,
    tof_range_days: Annotated[tuple[float, float] | None, Field(description="[min, max] time of flight in Kerbin days. Omit for 0.5-1.5x the Hohmann transfer time.")] = None,
    arrival_alt_m: Annotated[float | None, Field(description="Periapsis altitude at the target for a circular capture, m; adds capture Δv. Omit for arrival v_inf only (flyby/aerocapture).")] = None,
    include_porkchop: Annotated[bool, Field(description="Also return a coarse departure x time-of-flight cost grid.")] = False,
) -> dict:
    """Find the cheapest departure window with a Lambert porkchop over the bodies' exact Kepler orbits.

    Ephemerides come from each body's orbital elements (live from kRPC when reachable, else cached;
    exact for KSP's on-rails bodies). Every Lambert arc is the prograde branch judged against the
    origin's own orbital angular momentum, which is correct in kRPC's y-up, left-handed frames.
    Returns departure/arrival UT, time of flight, v_inf at both ends, C3, ejection Δv and ejection
    angle for the parking orbit, capture Δv, phase angles, the Hohmann reference, and, when the active
    vessel orbits ``origin``, an ejection-node seed (UT + prograde) to refine with compute_node_search.
    """
    _finite(parking_alt_m=parking_alt_m, earliest_ut=earliest_ut, search_days=search_days,
            tof_range_days=tof_range_days, arrival_alt_m=arrival_alt_m)
    with _bodies() as book:
        o_b, t_b = book.get(origin), book.get(target)
        _need(o_b.name != t_b.name, "origin and target are the same body", "name the destination body as target")
        _need(o_b.parent is not None and o_b.parent == t_b.parent,
              f"{o_b.name} orbits {o_b.parent} but {t_b.name} orbits {t_b.parent}",
              "a transfer window needs two bodies with the same parent; plan legs through the common parent")
        par = book.get(o_b.parent)
        link, live = book.link, book.live
        source = book.source()
    now = link.sc.ut if live else None
    t_start = earliest_ut if earliest_ut is not None else now
    _need(t_start is not None, "earliest_ut is required when the game is not reachable (no clock offline)")
    if now is not None:
        _need(t_start >= now, f"earliest_ut {t_start:.0f} is in the past (now {now:.0f})",
              "omit earliest_ut to search from now")
    ko, kt = o_b.kepler(par.mu), t_b.kepler(par.mu)
    eph_o, eph_t = _krpc_axes(ko), _krpc_axes(kt)
    t_h = orbits.hohmann(par.mu, ko.a, kt.a).tof
    synodic = orbits.synodic_period(ko.period, kt.period)
    _need(search_days is None or 0 <= search_days < math.inf, "search_days must be a finite number >= 0")
    span = search_days * KERBIN_DAY_S if search_days is not None else synodic
    _need(math.isfinite(span), "the two orbits have the same period (no synodic period): pass search_days")
    tof_lo, tof_hi = ((tof_range_days[0] * KERBIN_DAY_S, tof_range_days[1] * KERBIN_DAY_S)
                      if tof_range_days is not None else (0.5 * t_h, 1.5 * t_h))
    r_park = o_b.radius + parking_alt_m
    _need(r_park < o_b.soi, "the parking orbit is outside the origin's SOI")
    r_cap = t_b.radius + arrival_alt_m if arrival_alt_m is not None else None

    def dep_cost(v_inf: float) -> float:
        return orbits.ejection_dv(o_b.mu, r_park, v_inf)

    def arr_cost(v_inf: float) -> float:
        return orbits.capture_dv(t_b.mu, r_cap, v_inf, r_cap) if r_cap is not None else v_inf

    cost = {"departure": lambda d, a: dep_cost(d), "arrival": lambda d, a: arr_cost(a),
            "total": lambda d, a: dep_cost(d) + arr_cost(a)}[minimize]
    ws = _physics(lambert.search_transfer_window, par.mu, eph_o, eph_t, t_start=t_start, t_end=t_start + span,
                  tof_min=tof_lo, tof_max=tof_hi, cost=cost, hint="widen search_days or tof_range_days")
    best = ws.best
    vd, va = best.v_inf_departure_mag, best.v_inf_arrival_mag
    h_hat = vunit(best.departure_normal)
    prograde_hat = vunit(best.origin_velocity)
    in_plane = vsub(best.v_inf_departure, vscale(h_hat, vdot(best.v_inf_departure, h_hat)))
    nu_inf = orbits.asymptote_true_anomaly(o_b.mu, r_park, vd)
    ejection_angle = (orbits.signed_angle(prograde_hat, in_plane, best.departure_normal) - nu_inf) % math.tau
    out: dict[str, Any] = {
        "origin": o_b.name, "target": t_b.name, "parent": par.name,
        "departure_ut": best.departure_ut, "arrival_ut": best.arrival_ut, "tof_s": best.tof,
        "tof_days": best.tof / KERBIN_DAY_S, "v_inf_departure_mps": vd, "c3_m2ps2": vd * vd,
        "ejection_dv_mps": dep_cost(vd), "v_inf_arrival_mps": va,
        "ejection": {
            "burn_angle_from_origin_prograde_deg": _DEG(orbits.wrap_pi(ejection_angle)),
            "asymptote_true_anomaly_deg": _DEG(nu_inf),
            "v_inf_declination_deg": _DEG(math.asin(max(-1.0, min(1.0, vdot(best.v_inf_departure, h_hat) / vd)))),
            "note": "where to burn in a prograde parking orbit lying in the origin's orbital plane: the angle from the "
                    "origin's prograde direction to the burn point, + ahead / - behind along your motion; the "
                    "declination is how far v_inf leaves that plane (an in-plane burn cannot supply it)"},
        "phase_angle_at_departure_deg": _DEG(orbits.wrap_pi(orbits.signed_angle(best.origin_position, eph_t(best.departure_ut)[0], best.departure_normal))),
        "phase_angle_at_start_deg": _DEG(orbits.wrap_pi(orbits.signed_angle(eph_o(t_start)[0], eph_t(t_start)[0], best.departure_normal))),
        "hohmann_reference": {"tof_s": t_h, "phase_angle_deg": _DEG(orbits.phase_angle_for_transfer(par.mu, kt.a, t_h))},
        "synodic_period_s": synodic, "search": {"start_ut": t_start, "span_s": span, "tof_range_s": [tof_lo, tof_hi],
                                                "evaluations": ws.evaluations, "minimize": minimize},
        "source": source,
    }
    if r_cap is not None:
        out["capture_dv_mps"] = arr_cost(va)
        out["capture_alt_m"] = arrival_alt_m
    if best.departure_ut - t_start < 1.0 or span - (best.departure_ut - t_start) < 1.0:
        out["warning"] = "the optimum lies on the edge of the searched span: widen search_days to be sure"
    alt_warnings = [w for w in (_terrain_warning(o_b, parking_alt_m, "parking orbit altitude"),
                                _terrain_warning(t_b, arrival_alt_m, "arrival periapsis") if arrival_alt_m is not None
                                else None) if w]
    if alt_warnings:
        out["altitude_warnings"] = alt_warnings
    if include_porkchop:
        out["porkchop"] = _porkchop(ws, t_start)
    if live:
        out.update(_live_ejection_extras(link, o_b, t_b, eph_o, eph_t, best))
    return out


def _porkchop(ws: lambert.WindowSearch, t_start: float, max_rows: int = 24, max_cols: int = 12) -> dict:
    ri = sorted({round(i * (len(ws.grid_departure_ut) - 1) / (max_rows - 1)) for i in range(max_rows)})
    ci = sorted({round(j * (len(ws.grid_tof) - 1) / (max_cols - 1)) for j in range(max_cols)})
    return {"departure_days_from_start": [(ws.grid_departure_ut[i] - t_start) / KERBIN_DAY_S for i in ri],
            "tof_days": [ws.grid_tof[j] / KERBIN_DAY_S for j in ci],
            "cost_mps": [[ws.grid_cost[i][j] for j in ci] for i in ri],
            "note": "rows = departure, columns = time of flight; null = no valid arc"}


def _live_ejection_extras(k: KSP, o_b: Body, t_b: Body, eph_o, eph_t, best: lambert.Transfer) -> dict:
    extras: dict[str, Any] = {}
    try:
        par = k.sc.bodies[o_b.parent]
        frame = par.non_rotating_reference_frame
        err = max(vnorm(vsub(eph_o(best.departure_ut)[0], k.sc.bodies[o_b.name].orbit.position_at(best.departure_ut, frame))),
                  vnorm(vsub(eph_t(best.arrival_ut)[0], k.sc.bodies[t_b.name].orbit.position_at(best.arrival_ut, frame))))
        extras["ephemeris_check_m"] = err
        v = k.sc.active_vessel if k.scene() == "flight" else None
        if v is not None and v.orbit.body.name == o_b.name:
            floor = o_b.atmosphere_depth if o_b.has_atmosphere else 0.0
            if v.orbit.eccentricity >= 1 or v.orbit.periapsis_altitude <= floor:
                extras["ejection_note"] = f"the active vessel is not in a stable orbit around {o_b.name}: no ejection seed"
            else:
                craft = _krpc_axes(_kepler_of(v.orbit))
                plan = orbits.plan_ejection(o_b.mu, craft, best.v_inf_departure, best.departure_ut, o_b.soi)
                now = k.sc.ut
                if plan["ut"] < now:
                    extras["ejection_note"] = (
                        f"the ejection burn for this window would have been at UT {plan['ut']:.0f}, before now "
                        f"({plan['escape_time']:.0f} s of escape precede the departure): pass a later earliest_ut")
                    return extras
                extras["ejection_node_seed"] = {
                    "ut": plan["ut"], "prograde_mps": plan["prograde_dv"], "normal_mps": 0.0, "radial_mps": 0.0,
                    "escape_time_s": plan["escape_time"], "out_of_plane_deg": _DEG(plan["out_of_plane"]),
                    "note": "in-plane seed for the active vessel; refine with compute_node_search "
                            f"(objective encounter_body='{t_b.name}') and correct mid-course"}
    except Exception as exc:  # noqa: BLE001 — extras are best effort; the window itself stands
        extras["live_extras_error"] = f"{type(exc).__name__}: {exc}"
    return extras


# ---------------------------------------------------------------------------------------------
# terrain


@tool("compute")
def compute_terrain(
    mode: Annotated[Literal["region", "ground_track"], Field(description="region: a lat/lon box (whole body when the ranges are omitted); ground_track: under the active vessel's predicted path between two UTs.")],
    body: Annotated[str | None, Field(description="Body to sample (region mode). Omit for the active vessel's body.")] = None,
    lat_range_deg: Annotated[tuple[float, float] | None, Field(description="[south, north] latitude, deg (region). Omit for -90..90.")] = None,
    lon_range_deg: Annotated[tuple[float, float] | None, Field(description="[west, east] longitude, deg (region). Omit for -180..180.")] = None,
    step_deg: Annotated[float | None, Field(description="Grid spacing, deg (region); 1 deg = 10.5 km on Kerbin, 3.5 km on the Mun. Omit for ~10,000 samples. Each sample costs ~0.2 ms (Space Center) to ~0.6 ms (flight).", gt=0)] = None,
    ut_start: Annotated[float | None, Field(description="Track start UT (ground_track). Omit for now.")] = None,
    ut_end: Annotated[float | None, Field(description="Track end UT (ground_track); clipped at the next SOI change.")] = None,
    samples: Annotated[int | None, Field(description="Points along the track (ground_track). Omit for one per ~5 s (100-2000).", ge=2, le=20000)] = None,
) -> dict:
    """Sample terrain height (body.surface_height) over a region or under the predicted ground track.

    region: max/min terrain and where, with the sample spacing (true peaks can be higher than the
    sampled maximum). A whole-body scan updates the shared per-body maximum (also used by body_info
    and the other compute tools) when it finds a higher peak; offline, a whole-body request returns
    that cached value. ground_track: the vessel's
    altitude above the terrain along its current conic (body rotation included, no drag), the
    minimum clearance and where, and the first predicted ground contact if the path hits the surface.
    """
    _finite(lat_range_deg=lat_range_deg, lon_range_deg=lon_range_deg, step_deg=step_deg, ut_start=ut_start,
            ut_end=ut_end)
    if mode == "region":
        _need(ut_start is None and ut_end is None and samples is None, "ut_start/ut_end/samples belong to ground_track")
        return _terrain_region(body, lat_range_deg or (-90.0, 90.0), lon_range_deg or (-180.0, 180.0), step_deg,
                               whole=lat_range_deg is None and lon_range_deg is None)
    _need(body is None and lat_range_deg is None and lon_range_deg is None and step_deg is None,
          "body/lat_range_deg/lon_range_deg/step_deg belong to region mode")
    _need(ut_end is not None, "ground_track needs ut_end")
    return _terrain_track(ut_start, ut_end, samples)


def _terrain_region(body: str | None, lat_rng: tuple[float, float], lon_rng: tuple[float, float],
                    step: float | None, whole: bool) -> dict:
    _need(-90 <= lat_rng[0] <= lat_rng[1] <= 90, "lat_range_deg must be [south, north] within -90..90")
    _need(lon_rng[1] >= lon_rng[0] and lon_rng[1] - lon_rng[0] <= 360, "lon_range_deg must be [west, east], at most 360 wide")
    link = _live_link()
    if link is None:
        terrain = {k.lower(): (k, v) for k, v in _load_terrain().items()}
        if whole and body and body.strip().lower() in terrain:
            name, entry = terrain[body.strip().lower()]
            return {"body": name, "max_terrain_m": entry["max_m"],
                    "max_at": {"lat_deg": entry.get("lat_deg"), "lon_deg": entry.get("lon_deg")},
                    "note": entry.get("method"), "source": f"cache {_terrain_file()} (game not reachable)"}
        raise NotConnected("terrain sampling needs the game", "start KSP with a save loaded (game_status)")
    try:
        b = link.body(body) if body else link.body()
    except KeyError as exc:
        raise AstraError(str(exc).strip("'\""), "use a body name as KSP spells it") from exc
    lat_span, lon_span = lat_rng[1] - lat_rng[0], lon_rng[1] - lon_rng[0]
    if step is None:
        step = max(0.01, math.sqrt(max(lat_span, 1e-9) * max(lon_span, 1e-9) / 10_000.0))
    n_lat, n_lon = int(lat_span / step) + 1, int(lon_span / step) + (0 if lon_span >= 360 else 1)
    _need(n_lat * n_lon <= 200_000, f"{n_lat * n_lon} samples is too many", "use a coarser step_deg or a smaller box")
    hi, lo = (-math.inf, 0.0, 0.0), (math.inf, 0.0, 0.0)
    for i in range(n_lat):
        lat = lat_rng[0] + i * step
        for j in range(n_lon):
            lon = lon_rng[0] + j * step
            h = b.surface_height(lat, lon)
            if h > hi[0]:
                hi = (h, lat, lon)
            if h < lo[0]:
                lo = (h, lat, lon)
    spacing = math.radians(step) * b.equatorial_radius
    out = {"body": b.name, "max_terrain_m": hi[0], "max_at": {"lat_deg": hi[1], "lon_deg": hi[2]},
           "min_terrain_m": lo[0], "min_at": {"lat_deg": lo[1], "lon_deg": lo[2]},
           "samples": n_lat * n_lon, "step_deg": step, "spacing_m": spacing,
           "note": "sampled grid: true peaks between samples can be higher; below 0 = below sea level/datum"}
    if whole:
        kept, updated = _record_max_terrain(b.name, {
            "max_m": hi[0], "lat_deg": hi[1], "lon_deg": hi[2], "samples": n_lat * n_lon,
            "method": f"{step:.3g} deg whole-body grid ({spacing:.0f} m spacing, compute_terrain); a lower bound"})
        out["body_max_terrain"] = {**kept, "updated_cache": updated}
    return out


def _terrain_track(ut_start: float | None, ut_end: float, samples: int | None) -> dict:
    k = ksp()
    v = k.vessel()
    # Paused, so the body cannot turn between the clock read and the samples: the longitude
    # correction below assumes `now` is the orientation position_at uses.
    with _paused(k):
        o = v.orbit
        b = o.body
        now = k.sc.ut
        t0 = now if ut_start is None else ut_start
        _need(ut_end > t0, "ut_end must be after ut_start")
        t_soi = o.time_to_soi_change
        t1 = min(ut_end, now + t_soi) if math.isfinite(t_soi) else ut_end
        _need(t1 > t0, f"the trajectory leaves {b.name}'s SOI at UT {now + t_soi:.0f}, before ut_start",
              "sample a window inside the current SOI")
        n = samples or int(min(2000, max(100, (t1 - t0) / 5.0)))
        frame = b.reference_frame
        period = b.rotational_period
        radius = b.equatorial_radius
        pts: list[dict] = []
        for i in range(n):
            ut = t0 + (t1 - t0) * i / (n - 1)
            x, y, z = o.position_at(ut, frame)
            rn = math.sqrt(x * x + y * y + z * z)
            lat = math.degrees(math.asin(y / rn))
            # the frame is the body's CURRENT orientation; the ground moves east by the rotation since now
            lon = (math.degrees(math.atan2(z, x)) - 360.0 * (ut - now) / period + 180.0) % 360.0 - 180.0
            ground = b.surface_height(lat, lon)
            pts.append({"ut": ut, "lat_deg": lat, "lon_deg": lon, "terrain_m": ground, "altitude_m": rn - radius,
                        "clearance_m": rn - radius - ground})
    worst = min(pts, key=lambda p: p["clearance_m"])
    highest = max(pts, key=lambda p: p["terrain_m"])
    out: dict[str, Any] = {"body": b.name, "ut_start": t0, "ut_end": t1, "samples": n,
                           "max_terrain_m": highest["terrain_m"], "max_terrain_at": highest,
                           "min_clearance_m": worst["clearance_m"], "min_clearance_at": worst}
    if t1 < ut_end:
        out["note"] = "clipped at the SOI change"
    hit = next((i for i, p in enumerate(pts) if p["clearance_m"] < 0), None)
    if hit is not None:
        a = pts[hit - 1] if hit > 0 else pts[hit]
        c = pts[hit]
        f = a["clearance_m"] / (a["clearance_m"] - c["clearance_m"]) if a is not c else 0.0
        out["first_ground_contact"] = {k2: a[k2] + f * (c[k2] - a[k2]) for k2 in ("ut", "lat_deg", "lon_deg", "terrain_m")}
        out["first_ground_contact"]["note"] = "vacuum conic, no drag; interpolated between samples"
    stride = max(1, n // 40)
    out["track"] = [{k2: p[k2] for k2 in ("ut", "lat_deg", "lon_deg", "terrain_m", "clearance_m")} for p in pts[::stride]]
    return out
