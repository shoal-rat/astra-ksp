"""Flight instruments: stream-backed metrics for the active vessel.

`Probe` owns kRPC streams for the active vessel and its current body. It rebuilds them when the
vessel changes or the vessel crosses into another sphere of influence (surface frames depend on
the body). Both the `telemetry` tool and the reflex engine read from the same probe, so a reflex
tick costs no RPC round trips beyond stream updates.

Speeds are always measured in the body's rotating reference frame (`vessel.flight(body.reference_frame)`);
the default `vessel.flight()` frame co-moves with the vessel and reads ~0 speed.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable

from astra.ksp import KSP, ksp

G0 = 9.80665
PROPELLANTS = ("LiquidFuel", "Oxidizer", "SolidFuel", "MonoPropellant", "XenonGas")

# name -> (unit, description). These are also the metric names usable in reflex triggers.
METRICS: dict[str, tuple[str, str]] = {
    "ut": ("s", "universal time"),
    "met": ("s", "mission elapsed time"),
    "altitude": ("m", "altitude above sea level (mean)"),
    "surface_altitude": ("m", "height above the terrain (or sea) directly below"),
    "apoapsis_altitude": ("m", "apoapsis above sea level of the current orbit patch (+inf while unbound, e >= 1)"),
    "periapsis_altitude": ("m", "periapsis above sea level (negative = intersects the body)"),
    "time_to_apoapsis": ("s", "time until apoapsis"),
    "time_to_periapsis": ("s", "time until periapsis"),
    "eccentricity": ("", "orbital eccentricity"),
    "inclination": ("deg", "orbital inclination relative to the body's equator"),
    "semi_major_axis": ("m", "semi-major axis"),
    "period": ("s", "orbital period (NaN/inf when not bound)"),
    "time_to_soi_change": ("s", "time until leaving/entering an SOI (NaN when none)"),
    "next_periapsis_altitude": ("m", "periapsis altitude in the NEXT patch (encounter/escape), NaN if none"),
    "orbital_speed": ("m/s", "speed in the body's inertial frame"),
    "surface_speed": ("m/s", "speed relative to the rotating surface"),
    "vertical_speed": ("m/s", "rate of climb relative to the surface (negative = descending)"),
    "horizontal_speed": ("m/s", "horizontal speed relative to the surface"),
    "latitude": ("deg", "latitude"),
    "longitude": ("deg", "longitude"),
    "dynamic_pressure": ("Pa", "dynamic pressure q"),
    "mach": ("", "Mach number"),
    "atmosphere_density": ("kg/m3", "local air density"),
    "static_pressure": ("Pa", "local static pressure"),
    "g_force": ("g", "sensed acceleration in g (0 in free fall)"),
    "pitch": ("deg", "pitch above the horizon"),
    "heading": ("deg", "compass heading"),
    "roll": ("deg", "roll"),
    "angle_of_attack": ("deg", "angle between nose and airflow"),
    "sideslip": ("deg", "sideslip angle"),
    "mass": ("kg", "total vessel mass"),
    "thrust": ("N", "current total thrust"),
    "available_thrust": ("N", "thrust at full throttle with the active engines here"),
    "isp": ("s", "combined specific impulse of the active engines here"),
    "twr": ("", "current thrust-to-weight in local gravity"),
    "max_twr": ("", "full-throttle thrust-to-weight in local gravity"),
    "local_g": ("m/s2", "gravitational acceleration at the vessel's altitude"),
    "throttle": ("", "commanded throttle 0..1"),
    "stage": ("", "current KSP stage number (counts down)"),
    "stage_propellant": ("kg", "propellant mass in parts dropped by the next staging event"),
    "stage_dv": ("m/s", "rough Δv of the propellant in stage_propellant at the current Isp"),
    "electric_charge": ("", "fraction of ElectricCharge capacity remaining"),
    "part_count": ("", "number of parts in the vessel"),
    "situation": ("", "pre_launch | landed | splashed | flying | sub_orbital | orbiting | escaping | docked"),
    "body": ("", "body whose SOI the vessel is in"),
    "node_time_to": ("s", "time until the next maneuver node (NaN if none)"),
    "node_remaining_dv": ("m/s", "remaining Δv of the next maneuver node (NaN if none)"),
    "target_distance": ("m", "distance to the current target (NaN if none)"),
    "target_speed": ("m/s", "speed relative to the current target (NaN if none)"),
    "max_temp_fraction": ("", "highest part temperature / its max temperature"),
}

_NAN = float("nan")


def _apoapsis(ap: float, ecc: float) -> float:
    """+inf while the orbit is unbound, so 'apoapsis <= x' cannot fire and 'apoapsis >= x' can."""
    if isinstance(ecc, float) and ecc == ecc and ecc >= 1.0:
        return float("inf")
    return ap


def vessel_box(vessel: Any, frame: Any, limit_m: float = 500.0) -> tuple[list[float], list[float]] | None:
    """Axis-aligned box around the vessel in `frame`, built from the parts' own boxes.

    kRPC's Vessel.bounding_box can come back as +-1e17 when one part reports a broken bound (the
    stock Mk1 pod does), so parts whose box reaches beyond `limit_m` from the frame origin are
    replaced by their position. None if nothing sane was found.
    """
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for part in vessel.parts.all:
        try:
            a, b = part.bounding_box(frame)
            if max(abs(x) for x in (*a, *b)) > limit_m:
                a = b = part.position(frame)
        except Exception:  # noqa: BLE001 — a part may vanish mid-query
            continue
        if max(abs(x) for x in (*a, *b)) > limit_m:
            continue
        lo = [min(u, x) for u, x in zip(lo, a)]
        hi = [max(u, x) for u, x in zip(hi, b)]
    return (lo, hi) if lo[0] <= hi[0] else None


def _enum_name(x: Any) -> str:
    return str(x).split(".")[-1]


@dataclass
class _Const:
    body: str
    mu: float
    radius: float
    atmosphere_depth: float
    has_atmosphere: bool


class Probe:
    """Stream-backed reader for the active vessel. Call `read()` as often as you like."""

    def __init__(self, k: KSP | None = None):
        self.k = k or ksp()
        self._key: tuple | None = None
        self._s: dict[str, Any] = {}
        self._const: _Const | None = None
        self._engines: list[dict] = []
        self._engine_key: tuple | None = None
        self._temps: list[tuple[Any, Any, tuple[float, float], str]] = []
        self._temp_key: tuple | None = None
        self._slow_cache: dict[str, tuple[float, Any]] = {}

    # -- stream management --------------------------------------------------------------------

    def _add(self, name: str, fn: Callable, *args: Any) -> None:
        try:
            self._s[name] = self.k.conn.add_stream(fn, *args)
        except Exception:  # noqa: BLE001 — a metric that cannot stream is read on demand
            self._s[name] = None

    def _rebuild(self) -> None:
        for s in self._s.values():
            try:
                if s is not None:
                    s.remove()
            except Exception:  # noqa: BLE001
                pass
        self._s.clear()
        self._engines, self._engine_key = [], None
        self._temps, self._temp_key = [], None
        v = self.vessel
        body = v.orbit.body
        self._const = _Const(body.name, body.gravitational_parameter, body.equatorial_radius,
                             body.atmosphere_depth if body.has_atmosphere else 0.0, body.has_atmosphere)
        sc, orbit, control = self.k.sc, v.orbit, v.control
        fl = v.flight(body.reference_frame)
        self._flight = fl
        add = self._add
        add("ut", getattr, sc, "ut")
        add("met", getattr, v, "met")
        add("situation", getattr, v, "situation")
        add("mass", getattr, v, "mass")
        add("thrust", getattr, v, "thrust")
        add("available_thrust", getattr, v, "available_thrust")
        add("isp", getattr, v, "specific_impulse")
        for a in ("apoapsis_altitude", "periapsis_altitude", "time_to_apoapsis", "time_to_periapsis",
                  "eccentricity", "inclination", "semi_major_axis", "period", "time_to_soi_change",
                  "speed", "radius"):
            add("o_" + a, getattr, orbit, a)
        for a in ("mean_altitude", "surface_altitude", "speed", "vertical_speed", "horizontal_speed",
                  "latitude", "longitude", "dynamic_pressure", "mach", "atmosphere_density",
                  "static_pressure", "g_force", "angle_of_attack", "sideslip_angle"):
            add("f_" + a, getattr, fl, a)
        # Attitude angles are relative to the flight object's frame: use the local horizon frame.
        fl_srf = v.flight(v.surface_reference_frame)
        for a in ("pitch", "heading", "roll"):
            add("f_" + a, getattr, fl_srf, a)
        add("throttle", getattr, control, "throttle")
        add("stage", getattr, control, "current_stage")

    @property
    def vessel(self):
        return self.k.vessel()

    def _ensure(self) -> None:
        v = self.vessel
        key = (self.k.scene(), v._object_id, v.orbit.body.name)
        if key != self._key:
            self._rebuild()
            self._key = key

    def _get(self, name: str) -> Any:
        s = self._s.get(name)
        if s is None:
            return _NAN
        try:
            return s()
        except Exception:  # noqa: BLE001 — e.g. NaN orbit fields on escape
            return _NAN

    def _slow(self, name: str, ttl: float, fn: Callable[[], Any]) -> Any:
        """Cache values that need RPCs (not streams) for `ttl` seconds of wall time."""
        now = time.monotonic()
        hit = self._slow_cache.get(name)
        if hit and now - hit[0] < ttl:
            return hit[1]
        try:
            val = fn()
        except Exception:  # noqa: BLE001
            val = _NAN
        self._slow_cache[name] = (now, val)
        return val

    def invalidate(self) -> None:
        """Force a rebuild on next read (after staging, docking, vessel switch...)."""
        self._key = None
        self._slow_cache.clear()

    # -- reading ------------------------------------------------------------------------------

    def read(self) -> dict[str, Any]:
        """All core metrics (streams only; cheap)."""
        self._ensure()
        c = self._const
        assert c is not None
        g = self._get
        alt = g("f_mean_altitude")
        r = c.radius + (alt if isinstance(alt, float) and not math.isnan(alt) else 0.0)
        local_g = c.mu / (r * r) if r > 0 else _NAN
        mass = g("mass")
        thrust, avail = g("thrust"), g("available_thrust")
        weight = mass * local_g if mass and local_g == local_g else _NAN
        m = {
            "ut": g("ut"), "met": g("met"),
            "altitude": alt, "surface_altitude": g("f_surface_altitude"),
            # kRPC reports a negative apoapsis on hyperbolic orbits; an unbound orbit has none.
            "apoapsis_altitude": _apoapsis(g("o_apoapsis_altitude"), g("o_eccentricity")),
            "periapsis_altitude": g("o_periapsis_altitude"),
            "time_to_apoapsis": g("o_time_to_apoapsis"), "time_to_periapsis": g("o_time_to_periapsis"),
            "eccentricity": g("o_eccentricity"), "inclination": math.degrees(g("o_inclination")),
            "semi_major_axis": g("o_semi_major_axis"), "period": g("o_period"),
            "time_to_soi_change": g("o_time_to_soi_change"),
            "orbital_speed": g("o_speed"), "surface_speed": g("f_speed"),
            "vertical_speed": g("f_vertical_speed"), "horizontal_speed": g("f_horizontal_speed"),
            "latitude": g("f_latitude"), "longitude": g("f_longitude"),
            "dynamic_pressure": g("f_dynamic_pressure"), "mach": g("f_mach"),
            "atmosphere_density": g("f_atmosphere_density"), "static_pressure": g("f_static_pressure"),
            "g_force": g("f_g_force"), "pitch": g("f_pitch"), "heading": g("f_heading"), "roll": g("f_roll"),
            "angle_of_attack": g("f_angle_of_attack"), "sideslip": g("f_sideslip_angle"),
            "mass": mass, "thrust": thrust, "available_thrust": avail, "isp": g("isp"),
            "local_g": local_g,
            "twr": thrust / weight if weight and weight == weight else _NAN,
            "max_twr": avail / weight if weight and weight == weight else _NAN,
            "throttle": g("throttle"), "stage": g("stage"),
            "situation": _enum_name(g("situation")), "body": c.body,
        }
        return m

    def read_extended(self, include_temp: bool = True) -> dict[str, Any]:
        """Core metrics plus slower derived ones (node, target, next patch, stage propellant...).
        Part temperatures need a stream per part, so callers skip them when heating is implausible."""
        m = self.read()
        v = self.vessel
        m["next_periapsis_altitude"] = self._slow("next_pe", 1.0, lambda: self._next_pe(v, m))
        m["node_time_to"], m["node_remaining_dv"] = self._slow("node", 0.5, lambda: self._node(v))
        m["target_distance"], m["target_speed"] = self._slow("target", 0.5, lambda: self._target(v))
        m["part_count"] = self._slow("parts", 0.5, lambda: len(v.parts.all))
        m["electric_charge"] = self._slow("ec", 2.0, lambda: self._ec(v))
        prop = self._slow("stage_prop", 1.0, lambda: self.stage_propellant_kg(v, int(m["stage"]), self.density))
        m["stage_propellant"] = prop
        m["stage_dv"] = self._rough_dv(m["isp"], m["mass"], prop)
        m["max_temp_fraction"] = self._slow("temp", 1.0, self.max_temp_fraction) if include_temp else _NAN
        return m

    # -- slow helpers ---------------------------------------------------------------------------

    @staticmethod
    def _next_pe(v, m: dict) -> float:
        t = m.get("time_to_soi_change", _NAN)
        if not isinstance(t, float) or math.isnan(t) or math.isinf(t):
            return _NAN
        nxt = v.orbit.next_orbit
        return nxt.periapsis_altitude if nxt is not None else _NAN

    def _node(self, v) -> tuple[float, float]:
        nodes = v.control.nodes
        if not nodes:
            return (_NAN, _NAN)
        n = nodes[0]
        return (n.time_to, n.remaining_delta_v)

    def _target(self, v) -> tuple[float, float]:
        sc = self.k.sc
        tgt = sc.target_vessel or sc.target_docking_port or sc.target_body
        if tgt is None:
            return (_NAN, _NAN)
        ref = v.orbit.body.non_rotating_reference_frame
        p1, p2 = v.position(ref), tgt.position(ref)
        v1, v2 = v.velocity(ref), tgt.velocity(ref)
        d = math.dist(p1, p2)
        s = math.dist(v1, v2)
        return (d, s)

    @staticmethod
    def _ec(v) -> float:
        res = v.resources
        if not res.has_resource("ElectricCharge"):
            return _NAN
        cap = res.max("ElectricCharge")
        return res.amount("ElectricCharge") / cap if cap > 0 else _NAN

    def density(self, name: str) -> float:
        """Resource density in kg/unit (kRPC: a static method on the Resources class)."""
        cache = self.__dict__.setdefault("_density", {})
        if name not in cache:
            cache[name] = self.k.sc.Resources.density(name)
        return cache[name]

    @staticmethod
    def stage_propellant_kg(v, current_stage: int, density: Callable[[str], float]) -> float:
        """Propellant mass in parts dropped at the next staging event (current_stage - 1)."""
        res = v.resources_in_decouple_stage(current_stage - 1, cumulative=False)
        total = 0.0
        for name in PROPELLANTS:
            if res.has_resource(name):
                total += res.amount(name) * density(name)
        return total

    @staticmethod
    def _rough_dv(isp: float, mass: float, prop: float) -> float:
        try:
            if prop == 0:
                return 0.0
            if isp > 0 and mass > prop > 0:
                return isp * G0 * math.log(mass / (mass - prop))
        except (TypeError, ValueError):
            pass
        return _NAN

    def engines(self) -> list[dict]:
        """Status of every engine that is active (ignited) right now, via streams."""
        v = self.vessel
        stage = int(self._get("stage")) if self._s else None
        count = self._slow("parts", 0.5, lambda: len(v.parts.all))
        key = (self._key, stage, count)
        if key != self._engine_key:
            for e in self._engines:
                for s in e["_s"]:
                    try:
                        s.remove()
                    except Exception:  # noqa: BLE001
                        pass
            self._engines = []
            conn = self.k.conn
            for eng in v.parts.engines:
                try:
                    if not eng.active:
                        continue
                    streams = [conn.add_stream(getattr, eng, "has_fuel"),
                               conn.add_stream(getattr, eng, "thrust"),
                               conn.add_stream(getattr, eng, "available_thrust")]
                    self._engines.append({"part": eng.part.name, "title": eng.part.title,
                                          "throttle_locked": eng.throttle_locked, "_s": streams})
                except Exception:  # noqa: BLE001
                    continue
            self._engine_key = key
        out = []
        for e in self._engines:
            try:
                hf, th, av = (s() for s in e["_s"])
            except Exception:  # noqa: BLE001 — engine gone (decoupled)
                self._engine_key = None
                continue
            out.append({"part": e["part"], "has_fuel": hf, "thrust": th, "available_thrust": av,
                        "solid": e["throttle_locked"]})
        return out

    def max_temp_fraction(self) -> float:
        v = self.vessel
        count = len(v.parts.all)
        key = (self._key, count)
        if key != self._temp_key:
            for t in self._temps:
                for s in t[:2]:
                    try:
                        s.remove()
                    except Exception:  # noqa: BLE001
                        pass
            self._temps = []
            conn = self.k.conn
            for p in v.parts.all:
                try:
                    self._temps.append((conn.add_stream(getattr, p, "temperature"),
                                        conn.add_stream(getattr, p, "skin_temperature"),
                                        (p.max_temperature or 1.0, p.max_skin_temperature or 1.0), p.name))
                except Exception:  # noqa: BLE001
                    continue
            self._temp_key = key
        best = 0.0
        for t_int, t_skin, (max_int, max_skin), _ in self._temps:
            try:
                # Each against its own limit: a pod's skin runs far hotter than its interior may.
                best = max(best, t_int() / max_int, t_skin() / max_skin)
            except Exception:  # noqa: BLE001
                self._temp_key = None
        return best

    def hottest_parts(self, n: int = 3) -> list[dict]:
        v = self.vessel
        rows = []
        for p in v.parts.all:
            try:
                frac = max(p.temperature / p.max_temperature, p.skin_temperature / p.max_skin_temperature)
                rows.append({"part": p.name, "temp_fraction": frac})
            except Exception:  # noqa: BLE001
                continue
        rows.sort(key=lambda r: -r["temp_fraction"])
        return rows[:n]


_PROBE: Probe | None = None


def probe() -> Probe:
    global _PROBE
    if _PROBE is None:
        _PROBE = Probe()
    return _PROBE


# -- snapshots for the telemetry tool ---------------------------------------------------------


def _fmt_time(seconds: float) -> str | None:
    if not isinstance(seconds, (int, float)) or math.isnan(seconds) or math.isinf(seconds):
        return None
    seconds = int(seconds)
    d, rem = divmod(seconds, 6 * 3600)  # Kerbin days are 6 h
    h, rem = divmod(rem, 3600)
    mnt, s = divmod(rem, 60)
    return (f"{d}d " if d else "") + f"{h:02d}:{mnt:02d}:{s:02d}"


def snapshot(detail: str = "brief") -> dict[str, Any]:
    """Structured flight-state snapshot for the AI."""
    p = probe()
    m = p.read_extended()
    k = p.k
    v = p.vessel
    ctl = v.control
    snap: dict[str, Any] = {
        "vessel": v.name, "situation": m["situation"], "body": m["body"],
        "ut": m["ut"], "met": _fmt_time(m["met"]),
        "position": {"altitude_m": m["altitude"], "surface_altitude_m": m["surface_altitude"],
                     "lat_deg": m["latitude"], "lon_deg": m["longitude"]},
        "velocity": {"orbital_mps": m["orbital_speed"], "surface_mps": m["surface_speed"],
                     "vertical_mps": m["vertical_speed"], "horizontal_mps": m["horizontal_speed"]},
        "orbit": {"apoapsis_m": m["apoapsis_altitude"], "periapsis_m": m["periapsis_altitude"],
                  "time_to_apoapsis_s": m["time_to_apoapsis"], "time_to_periapsis_s": m["time_to_periapsis"],
                  "eccentricity": m["eccentricity"], "inclination_deg": m["inclination"],
                  "period_s": m["period"], "time_to_soi_change_s": m["time_to_soi_change"],
                  "next_patch_periapsis_m": m["next_periapsis_altitude"]},
        "attitude": {"pitch_deg": m["pitch"], "heading_deg": m["heading"], "roll_deg": m["roll"],
                     "aoa_deg": m["angle_of_attack"]},
        "aero": {"dynamic_pressure_pa": m["dynamic_pressure"], "mach": m["mach"],
                 "density_kgpm3": m["atmosphere_density"]},
        "propulsion": {"mass_t": m["mass"] / 1000.0, "thrust_kn": m["thrust"] / 1000.0,
                       "available_thrust_kn": m["available_thrust"] / 1000.0, "isp_s": m["isp"],
                       "twr": m["twr"], "max_twr": m["max_twr"], "local_g": m["local_g"],
                       "throttle": m["throttle"], "stage": m["stage"],
                       "stage_propellant_t": m["stage_propellant"] / 1000.0 if m["stage_propellant"] == m["stage_propellant"] else None,
                       "stage_dv_rough_mps": m["stage_dv"]},
        "control": {"sas": ctl.sas, "rcs": ctl.rcs, "gear": ctl.gear, "legs": ctl.legs,
                    "autopilot_error_deg": _safe(lambda: v.auto_pilot.error)},
        "power": {"electric_charge_fraction": m["electric_charge"]},
        "g_force": m["g_force"], "part_count": m["part_count"],
        "max_temp_fraction": m["max_temp_fraction"],
        "paused": k.paused,
    }
    if m["node_time_to"] == m["node_time_to"]:
        snap["next_node"] = {"time_to_s": m["node_time_to"], "remaining_dv_mps": m["node_remaining_dv"]}
    if m["target_distance"] == m["target_distance"]:
        snap["target"] = {"distance_m": m["target_distance"], "relative_speed_mps": m["target_speed"]}
    if detail == "full":
        snap["engines"] = p.engines()
        snap["hottest_parts"] = p.hottest_parts()
        res = v.resources
        snap["resources"] = {n: {"amount": res.amount(n), "max": res.max(n)} for n in res.names}
        snap["crew"] = [c.name for c in v.crew]
        snap["control"].update({"sas_mode": _safe(lambda: _enum_name(ctl.sas_mode)),
                                "speed_mode": _safe(lambda: _enum_name(ctl.speed_mode)),
                                "lights": ctl.lights, "brakes": ctl.brakes, "abort": ctl.abort})
        snap["comms"] = _safe(lambda: {"can_communicate": v.comms.can_communicate,
                                       "signal_strength": v.comms.signal_strength})
        snap["biome"] = _safe(lambda: v.biome)
    return snap


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None
