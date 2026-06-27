"""General, BODY-AGNOSTIC mission decomposer — ASTRA's autonomous planner with NO per-mission scripts.

The owner's directive: the agent must make its OWN decisions and iterate, not run rigid pre-programmed
per-mission logic (the old ``tools/fly_mun_roundtrip.py`` 7-step Mun PLAN is exactly what is forbidden).
This module turns ANY one-line goal — "land a crew on Duna, plant a flag, and bring them home", "put a
relay in synchronous orbit around Eve", "land on the Mun and return" — into an ORDERED list of atomic
primitives, with every parameter (capture mode, altitudes, crew, heat-shield, legs, post-LKO Δv budget)
COMPUTED from the bodies table + physics for the named destination. It is a single GENERAL algorithm, not
a per-body template.

It is used as the planner when ``ANTHROPIC_API_KEY`` is unset (no LLM mission-architect available); when a
key IS present the LLM interpreter is used instead. Either way the agent decomposes autonomously — this
module is the deterministic, key-free path so the agent is never blocked on an external service.
"""
from __future__ import annotations

from ..bodies import body as lookup_body
from ..bodies import synchronous_altitude_m

# Margin folded on top of the mission graph's summed post-LKO Δv when sizing the launch vehicle (the graph
# uses nominal Hohmann/vis-viva; a live grid-search capture + a sloppier node run a few hundred m/s over).
_POST_LKO_MARGIN_FRAC = 0.08


def _has_atmosphere(body) -> bool:
    try:
        return float(getattr(body, "atmosphere_top_m", 0.0)) > 0.0
    except Exception:
        return False


def _is_moon(body) -> bool:
    """A moon orbits a planet (its parent is NOT the Sun); a planet orbits the Sun. Moon transfers need
    only phasing; planet transfers need a heliocentric launch window."""
    return str(getattr(body, "parent", "Sun")) not in ("Sun", "")


def _low_orbit_alt_km(body) -> float:
    """A safe low parking/return orbit altitude (km) for a body: above its atmosphere if it has one, else a
    low circular orbit clear of terrain."""
    try:
        return max(0.0, body.low_orbit_radius_m() - body.radius_m) / 1000.0
    except Exception:
        top = float(getattr(body, "atmosphere_top_m", 0.0))
        return (top * 1.15 / 1000.0) if top > 0 else 30.0


def _capture_alt_km(body) -> float:
    """Where to capture: a low circular orbit to land from (airless), or a low atmospheric periapsis to
    aerocapture into (has air)."""
    if _has_atmosphere(body):
        # Aim a periapsis well inside the atmosphere so drag captures cheaply but not so deep it burns up.
        return max(8.0, float(body.atmosphere_top_m) * 0.55 / 1000.0)
    sync = synchronous_altitude_m(body)
    if sync and sync > 0:
        return min(_low_orbit_alt_km(body) * 3.0, sync / 1000.0)
    return max(_low_orbit_alt_km(body) + 10.0, body.radius_m * 0.25 / 1000.0)


def parse_intent(command: str) -> dict:
    """Read the GOAL TYPE from plain English: does it land? plant a flag? return home? carry crew? want a
    relay/orbit-only? This is keyword-robust, not a rigid template — the step list is built from these
    booleans + the destination body's physics."""
    t = (command or "").lower()
    return {
        "land": any(w in t for w in ("land", "touch down", "touchdown", "surface", "set down", "flag")),
        "flag": "flag" in t,
        "return": any(w in t for w in ("return", "home", "back", "round trip", "round-trip", "recover",
                                       "bring", "come back", "and back")),
        "crewed": any(w in t for w in ("crew", "kerbal", "astronaut", "people", "manned", "man ",
                                       "person", "passenger", "pilot")),
        "relay": any(w in t for w in ("relay", "comsat", "communication", "antenna network", "constellation")),
        "orbit_only": any(w in t for w in ("orbit around", "into orbit", "synchronous", "stationary",
                                           "keostationary", "parking orbit")) and "land" not in t,
    }


def decompose(command: str, target_body: str, *, launch_body: str = "Kerbin") -> tuple[list[dict], str]:
    """Decompose a goal into ordered primitive steps for ANY destination body, with parameters computed
    from physics. Returns (steps, rationale). Mirrors the old hand-built Mun PLAN but for any body, and
    folds in the MISSION-AWARE launch sizing (post-LKO Δv + legs + heat-shield/chutes) so ONE vehicle is
    built for the whole round trip — generalised out of the deleted hardcoded fly_mun_roundtrip script."""
    intent = parse_intent(command)
    tb = lookup_body(target_body)
    lb = lookup_body(launch_body)
    crew = 1 if intent["crewed"] else 0
    park_km = round(_low_orbit_alt_km(lb), 0) or 100.0
    name = f"AI-{target_body}-1"

    steps: list[dict] = []

    # 1. LAUNCH to a parking orbit of the launch body. heat-shield + chutes only matter for a body that we
    #    RECOVER on (Kerbin, has air). mission_dv + needs_legs are filled in below from the mission graph.
    needs_recovery_aero = intent["return"] and _has_atmosphere(lb)
    launch_args: dict = {
        "crew": crew, "target_alt_km": park_km, "name": name,
        "heatshield": bool(needs_recovery_aero), "chutes": bool(needs_recovery_aero),
        "radial_boosters": 0,
    }
    steps.append({"primitive": "launch", "args": launch_args})

    if intent["relay"] or intent["orbit_only"]:
        # Orbit-only / relay: transfer + (circular capture) and commission; no landing or return.
        cap = _capture_alt_km(tb)
        steps.append({"primitive": "transfer",
                      "args": {"target_body": target_body, "capture_mode": "circular",
                               "capture_alt_km": round(cap, 0)}})
        if intent["relay"]:
            steps.append({"primitive": "commission_relay", "args": {}})
        rationale = (f"Orbit-{target_body} mission: launch -> circular capture at {round(cap)} km"
                     + (" -> commission relay" if intent["relay"] else "") + ".")
        return steps, rationale

    # 2. TRANSFER + capture at the destination. A LANDER captures PROPULSIVELY to a STABLE orbit (circular),
    #    NOT by aerocapture into a FOREIGN atmosphere: the craft would enter engine-first, UNPROTECTED (its
    #    heat shield faces the home-return reentry, not this arrival), and break up at orbital speed — the
    #    crewed Duna craft did exactly that. Capture above the air, then deorbit gently. (Only the home-body
    #    RETURN aerocaptures — there the heat shield protects the reentry.) On an airless body, capture to a
    #    low circular orbit to deorbit from.
    capture_mode = "circular"
    if _has_atmosphere(tb):
        cap = round(float(tb.atmosphere_top_m) * 1.25 / 1000.0, 0)   # stable parking orbit ABOVE the air
    else:
        cap = round(_capture_alt_km(tb), 0)
    steps.append({"primitive": "transfer",
                  "args": {"target_body": target_body, "capture_mode": capture_mode,
                           "capture_alt_km": cap}})

    if intent["land"]:
        steps.append({"primitive": "land", "args": {}})
    if intent["flag"]:
        steps.append({"primitive": "plant_flag", "args": {}})

    if intent["return"]:
        # 5. ASCEND back to a low orbit of the destination, 6. transfer home, 7. recover.
        steps.append({"primitive": "ascend", "args": {"target_alt_km": round(_low_orbit_alt_km(tb), 0)}})
        steps.append({"primitive": "transfer", "args": {"target_body": launch_body, "capture_mode": "aerocapture"
                                                        if _has_atmosphere(lb) else "loose"}})
        steps.append({"primitive": "recover", "args": {}})

    # MISSION-AWARE launch sizing: one vehicle for the whole trip (post-LKO Δv + legs + heat-shield/chutes).
    _apply_mission_aware_launch(steps, launch_body=launch_body)

    verb = "land + return" if intent["return"] else ("land" if intent["land"] else "fly to")
    rationale = (f"{verb} {target_body} ({'crewed' if crew else 'uncrewed'}): "
                 f"{capture_mode} capture; "
                 + ("aerobraked chute-assisted descent" if _has_atmosphere(tb) else "propulsive hoverslam on legs")
                 + ("; ascend + heliocentric/phasing return + Kerbin reentry recover" if intent["return"] else ""))
    return steps, rationale


def _apply_mission_aware_launch(steps: list[dict], *, launch_body: str = "Kerbin") -> dict:
    """Size ONE launch vehicle for the WHOLE mission: read the post-LKO Δv from the mission graph, decide
    landing legs (any land/ascend on an AIRLESS body needs propulsive legs) + heat-shield/chutes (any
    recover on a body WITH air), and MERGE them into the launch step's args. Generalised out of the deleted
    fly_mun_roundtrip._mission_aware_launch_args so it works for any body, not just the Mun."""
    from .mission_graph import build_mission_graph

    try:
        g = build_mission_graph(steps, launch_body=launch_body)
    except Exception:
        return {}
    post_lko_dv = sum(getattr(n, "dv_mps", 0.0) for n in g.nodes if n.primitive != "launch")
    if post_lko_dv <= 0.0:
        return {}

    needs_legs = False
    needs_heatshield = False
    needs_chutes = False
    for n in g.nodes:
        if n.primitive in ("land", "ascend"):
            try:
                if float(lookup_body(n.target_body).atmosphere_top_m) <= 0:
                    needs_legs = True  # airless touchdown/ascent needs legs (chutes do nothing without air)
            except Exception:
                pass
        if n.primitive == "recover":
            try:
                if float(lookup_body(n.target_body).atmosphere_top_m) > 0:
                    needs_heatshield = True
                    needs_chutes = True
            except Exception:
                pass
    # A propulsive (airless) lander always needs legs; an atmospheric (chute) lander on a body with thin
    # air still wants legs for the final touchdown, so legs whenever the mission lands at all.
    if any(n.primitive == "land" for n in g.nodes):
        needs_legs = True

    merged = {"mission_dv": round(post_lko_dv * (1.0 + _POST_LKO_MARGIN_FRAC), 1), "needs_legs": needs_legs}
    if needs_heatshield:
        merged["heatshield"] = True
    if needs_chutes:
        merged["chutes"] = True
    for step in steps:
        if step.get("primitive") == "launch":
            step.setdefault("args", {})
            for k, v in merged.items():
                step["args"].setdefault(k, v)
            # A HEAVY interplanetary craft (large post-LKO budget — a propulsive capture at a far planet)
            # makes a several-hundred-tonne rocket a single core engine cannot lift (liftoff TWR < 1.2). Give
            # the booster a bigger CORE ENGINE CLUSTER (max_core_engines) for the liftoff thrust — a clustered
            # core has NO radial protrusions, so it stays a clean rocket and passes the ascent-envelope shape
            # gate (radial strap-on boosters FAIL that gate: "radial protrusions within ascent envelope").
            # design.py sizes the cluster within a 1.5x mounting plate.
            if post_lko_dv > 3200.0:
                step["args"]["max_core_engines"] = max(int(step["args"].get("max_core_engines", 1)), 4)
            break
    return merged
