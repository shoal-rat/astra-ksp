"""Rich PLANNING CONTEXT for ASTRA's LLM mission architect.

The interpreter no longer treats Claude as a word-guesser that picks one of three canned bundles.
It hands Claude a compact-but-complete briefing of everything a real flight planner would need to
DECOMPOSE a goal and CALCULATE its parameters:

  * the full primitive / action catalog (what the agent can actually do),
  * the bodies catalog with REAL stock-KSP constants (mu, radius, SOI, sidereal period, atmosphere,
    rotational speed, synchronous-orbit altitude, low-orbit altitude, SMA + parent),
  * the CURRENT universe time and every existing vessel's body + orbit (pe / ap / inclination), so the
    plan can reason about phasing, rendezvous targets and ground-wait windows, and
  * the CALCULATION HELPERS the plan may reference (find_transfer_window for launch/transfer windows,
    plan_ejection_node for the ejection burn, the astro.* closed-form Δv helpers, and
    bodies.synchronous_altitude_m for stationary altitudes).

Two builders:
  * ``build_planning_context(conn, sc, command)`` — the LIVE variant (kRPC space-center handle ``sc``);
    adds universe time + existing vessels. Degrades gracefully if any live read fails.
  * ``build_planning_context_static(command)`` — the OFFLINE variant (no kRPC): bodies + catalog +
    helper descriptions only. Used for ``--dry-run`` and the test-suite.

Both return a structured dict; ``render_context_text(ctx)`` collapses it to a token-efficient text
block for the system prompt. Everything here is read-only — it never flies anything.
"""
from __future__ import annotations

import math
from typing import Any

from .. import bodies as _bodies
from ..bodies import _REGISTRY
from . import primitives


# --------------------------------------------------------------------------- #
# CALCULATION HELPERS the plan may invoke (names + what they compute). These   #
# are advertised to the LLM so it can REASON about which calculation a step    #
# needs ("a Duna transfer => transfer_planner.find_transfer_window('Kerbin',   #
# 'Duna')") and roughly how long a ground-wait + warp would be.                #
# --------------------------------------------------------------------------- #
CALC_HELPERS: list[dict[str, str]] = [
    {
        "helper": "transfer_planner.find_transfer_window(sc, dep, tgt)",
        "computes": "Next optimal heliocentric DEPARTURE window between two bodies that share a "
                    "parent (both orbit the Sun). Returns ut_dep (departure universe-time), tof "
                    "(time-of-flight s), vinf_dep (departure v_infinity), vinf_mag, c3, synodic "
                    "period. Use this to decide HOW LONG to wait on the ground/in-orbit before the "
                    "ejection burn, and to warp (e.g. 1000x) to ut_dep.",
    },
    {
        "helper": "transfer_planner.plan_ejection_node(sc, vessel, vinf_vec, ut_min)",
        "computes": "The ejection MANEUVER NODE (UT + prograde/normal/radial components) that throws "
                    "the vessel onto the interplanetary transfer with the required v_infinity.",
    },
    {
        "helper": "bodies.synchronous_altitude_m(body)",
        "computes": "Stationary/synchronous-orbit ALTITUDE for a body: a=(mu*T_sid^2/4pi^2)^(1/3)-R. "
                    "Kerbin -> ~2863.33 km (keostationary); returns -1 if the radius is outside the "
                    "SOI (e.g. the Mun) so the plan must fall back to a sub-synchronous ring.",
    },
    {
        "helper": "astro.hohmann(mu, r1, r2)",
        "computes": "Two-burn Hohmann transfer Δv (dv1, dv2, total) between circular radii.",
    },
    {
        "helper": "astro.circular_speed(mu, r) / astro.vis_viva_speed(mu, r, a)",
        "computes": "Circular and vis-viva orbital speeds (m/s) at radius r.",
    },
    {
        "helper": "astro.ascent_dv(mu, r_surface, r_low_orbit, atmosphere_top_m, ...)",
        "computes": "Surface->low-orbit ascent Δv including gravity + (if atmospheric) drag loss.",
    },
    {
        "helper": "astro.oberth_ejection_dv(mu_body, r_park, v_infinity)",
        "computes": "Δv for an Oberth ejection burn from a parking orbit to reach a hyperbolic v_inf.",
    },
    {
        "helper": "astro.capture_dv(mu, r_periapsis, sma_arrival, r_target_apoapsis)",
        "computes": "Δv to capture from an arrival hyperbola into a bound orbit at the target.",
    },
    {
        "helper": "astro.orbital_period(mu, a)",
        "computes": "Orbital period (s) for a semi-major axis a — for phasing / ground-wait math.",
    },
]


# --------------------------------------------------------------------------- #
# Bodies catalog                                                              #
# --------------------------------------------------------------------------- #
def _body_record(b: Any) -> dict[str, Any]:
    """A compact, calculation-ready record for one body. All values are the same quantities kRPC
    returns live; synchronous altitude + low-orbit altitude are derived so the plan need not."""
    sync_alt = _bodies.synchronous_altitude_m(b)
    rec: dict[str, Any] = {
        "name": b.name,
        "parent": b.parent or "Sun",
        "mu": b.mu,
        "radius_km": round(b.radius_m / 1000.0, 1),
        "surface_g": round(b.surface_g, 3),
        "soi_km": round(b.soi_m / 1000.0, 1) if b.soi_m else None,
        "sidereal_period_s": round(b.sidereal_period_s, 1) if b.sidereal_period_s else None,
        "rotational_speed_mps": round(b.rotational_speed_mps, 1),
        "atmosphere": (
            {"top_km": round(b.atmosphere_top_m / 1000.0, 1),
             "surface_density_kgm3": round(b.surface_rho, 4)}
            if b.atmosphere_top_m > 0 else None
        ),
        "low_orbit_alt_km": round((b.low_orbit_radius_m() - b.radius_m) / 1000.0, 1),
        "sma_about_parent_km": round(b.orbit_radius_m / 1000.0, 1) if b.orbit_radius_m else None,
    }
    if sync_alt > 0:
        rec["synchronous_alt_km"] = round(sync_alt / 1000.0, 1)
    elif b.sidereal_period_s > 0:
        rec["synchronous_alt_km"] = "outside_SOI (use sub-synchronous ring)"
    return rec


def _bodies_catalog() -> list[dict[str, Any]]:
    """Every body except the Sun itself, parent first then children, with real constants."""
    out = []
    for b in _REGISTRY.values():
        if b.name == "Sun":
            continue
        out.append(_body_record(b))
    return out


# --------------------------------------------------------------------------- #
# Live universe + vessels (best-effort; never raises)                          #
# --------------------------------------------------------------------------- #
def _orbit_record(vessel: Any) -> dict[str, Any]:
    try:
        orb = vessel.orbit
        body = orb.body
        R = float(body.equatorial_radius)
        return {
            "name": getattr(vessel, "name", "?"),
            "body": getattr(body, "name", "?"),
            "periapsis_km": round((float(orb.periapsis) - R) / 1000.0, 1),
            "apoapsis_km": round((float(orb.apoapsis) - R) / 1000.0, 1),
            "inclination_deg": round(math.degrees(float(orb.inclination)), 2),
        }
    except Exception:
        return {"name": getattr(vessel, "name", "?"), "body": "?", "note": "orbit unreadable"}


def _live_universe(sc: Any) -> dict[str, Any]:
    """Universe time + every existing vessel's body/orbit. Returns {} on any failure (offline-safe)."""
    info: dict[str, Any] = {}
    try:
        info["universe_time_s"] = round(float(sc.ut), 1)
    except Exception:
        pass
    vessels: list[dict[str, Any]] = []
    try:
        for v in list(sc.vessels):
            vessels.append(_orbit_record(v))
    except Exception:
        pass
    if vessels:
        info["vessels"] = vessels
    return info


# --------------------------------------------------------------------------- #
# Public builders                                                              #
# --------------------------------------------------------------------------- #
def build_planning_context_static(command: str) -> dict[str, Any]:
    """OFFLINE planning context: catalog + bodies + helper descriptions (no kRPC). For dry-run/tests."""
    return {
        "command": command,
        "launch_body": "Kerbin",
        "primitive_catalog": primitives.catalog_for_prompt(),
        "bodies": _bodies_catalog(),
        "calculation_helpers": CALC_HELPERS,
        "live": {},  # no live universe offline
    }


def build_planning_context(conn: Any, sc: Any, command: str) -> dict[str, Any]:
    """LIVE planning context: the static briefing PLUS the current universe time and every existing
    vessel's body/orbit. ``conn`` is the kRPC connection (kept for future live calc), ``sc`` the
    space-center handle. Degrades to the static context if the live reads fail."""
    ctx = build_planning_context_static(command)
    try:
        ctx["live"] = _live_universe(sc)
    except Exception:
        ctx["live"] = {}
    return ctx


# --------------------------------------------------------------------------- #
# Token-efficient text rendering for the system prompt                         #
# --------------------------------------------------------------------------- #
def render_context_text(ctx: dict[str, Any]) -> str:
    """Collapse the context dict into a compact text block for the system prompt. JSON for the
    structured catalogs (the model parses them well), prose for the helper menu."""
    import json

    lines: list[str] = []
    lines.append(f"LAUNCH BODY: {ctx.get('launch_body', 'Kerbin')}")
    lines.append("")
    lines.append("PRIMITIVE / ACTION CATALOG (the ONLY actions you may emit):")
    lines.append(json.dumps(ctx["primitive_catalog"], ensure_ascii=False))
    lines.append("")
    lines.append("BODIES (real stock-KSP constants; altitudes are ABOVE the surface in km):")
    lines.append(json.dumps(ctx["bodies"], ensure_ascii=False))
    lines.append("")
    lines.append("CALCULATION HELPERS you may reference in your reasoning to pick parameters/windows:")
    for h in ctx["calculation_helpers"]:
        lines.append(f"  - {h['helper']}: {h['computes']}")
    live = ctx.get("live") or {}
    if live:
        lines.append("")
        lines.append("LIVE UNIVERSE STATE:")
        lines.append(json.dumps(live, ensure_ascii=False))
    return "\n".join(lines)
