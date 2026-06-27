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
# 0.13: a crewed interplanetary round-trip's capture (lowered-periapsis Oberth, still ~2x the nominal model)
# + ascent + return runs the budget razor-thin; the extra reserve buys the margin to actually get home.
_POST_LKO_MARGIN_FRAC = 0.13
# The DROPPABLE TRANSFER stage absorbs the ejection + mid-course corrections + capture variance; over-sizing
# it only grows the dropped stage, not the lander's budget. The live worst case is ~3138 m/s (eject 1039 +
# imprecise-ejection corrections ~890 + capture+Hohmann ~1176) — the cost that ran the 0.6-margin transfer
# (2701 m/s) DRY mid-capture and stranded the craft. The old reason 0.6 was the ceiling: at 0.85 an
# ALL-CORE upper became a 574 t / 65 m needle whose launch TWR dipped under 1. SIDE BOOSTERS remove that
# ceiling — they lift a heavier transfer stage off the pad and drop their mass early, so at margin 1.0 the
# transfer stage carries 3376 m/s (covering the ~3138 worst case with ~240 m/s slack) on a launchable
# ~487 t / TWR-1.76 asparagus rocket that still passes the shape gate. 1.0 is the real fix for the capture
# running dry; the split keeps the lander's get-home budget independent of it. See project_ksp_mars_duna.
_TRANSFER_MARGIN_FRAC = 1.0


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


def finalize_plan(steps: list[dict], *, launch_body: str = "Kerbin") -> list[dict]:
    """ENGINEERING finalization of an LLM-DECOMPOSED plan. The LLM does the DECOMPOSITION (which steps, in
    what order, for which bodies); this fills the deterministic PHYSICS the model should not have to nail by
    hand, and is the ONLY non-LLM step left in the planning path:

      * fill any missing capture_alt_km / target_alt_km / parking altitude / craft name from the body's
        atmosphere + low-orbit constants, and the RETURN-leg capture mode (aerocapture at an air home);
      * heat shield + chutes on the launch when the mission recovers on a body WITH air;
      * for a CREWED land-AND-RETURN on a PLANET (interplanetary), insert a ``jettison_transfer_stage`` step
        after the outbound capture so the upper is sized as a DROPPABLE transfer stage + a SHORT lander
        stage (the split that lets the lander touch down UPRIGHT and keep its OWN get-home budget) — unless
        the LLM already emitted that step;
      * SIZE the launch vehicle for the whole mission from the mission graph (mission_dv / the split
        transfer+lander budget, legs, side boosters) via ``_apply_mission_aware_launch``.

    Mutates and returns ``steps``. A relay / one-way / moon plan is simply sized; only a crewed planetary
    round-trip gets the split jettison inserted."""
    lb = lookup_body(launch_body)
    launch_step = next((s for s in steps if s.get("primitive") == "launch"), None)
    crew = int((launch_step or {}).get("args", {}).get("crew", 0) or 0)

    transfers = [s for s in steps if s.get("primitive") == "transfer"]
    outbound = next((s for s in transfers
                     if str(s.get("args", {}).get("target_body") or "") not in ("", launch_body)), None)
    target_body = str((outbound or {}).get("args", {}).get("target_body") or "")
    has_land = any(s.get("primitive") == "land" for s in steps)
    has_return = any(str(s.get("args", {}).get("target_body") or "") == launch_body for s in transfers)

    # 1) fill the precise numeric / mode args the LLM may have left blank (physics, not decomposition).
    if launch_step is not None:
        la = launch_step.setdefault("args", {})
        la.setdefault("name", f"AI-{target_body or launch_body}-1")
        la.setdefault("target_alt_km", round(_low_orbit_alt_km(lb), 0) or 100.0)
    for s in transfers:
        a = s.setdefault("args", {})
        tname = str(a.get("target_body") or "")
        if not tname:
            continue
        if tname == launch_body:                          # RETURN leg
            a.setdefault("capture_mode", "aerocapture" if _has_atmosphere(lb) else "loose")
            continue
        tb = lookup_body(tname)
        a.setdefault("capture_mode", "circular")
        if "capture_alt_km" not in a:
            a["capture_alt_km"] = (round(float(tb.atmosphere_top_m) * 1.25 / 1000.0, 0)
                                   if _has_atmosphere(tb) else round(_capture_alt_km(tb), 0))
    if target_body:
        tb = lookup_body(target_body)
        for s in steps:
            if s.get("primitive") == "ascend":
                s.setdefault("args", {}).setdefault("target_alt_km", round(_low_orbit_alt_km(tb), 0))
    if launch_step is not None and has_return and _has_atmosphere(lb):
        la = launch_step.setdefault("args", {})
        la.setdefault("heatshield", True)
        la.setdefault("chutes", True)

    # 2) SPLIT-STAGE: a crewed land-AND-return on a PLANET gets the droppable transfer stage + short lander.
    #    The capture is expensive + variable there (8k-40k km, 1100-2400 m/s) and a tall single stack topples;
    #    a moon round-trip keeps the proven single stack. Insert the in-orbit jettison if the LLM omitted it.
    if outbound is not None:
        is_planet = bool(target_body) and not _is_moon(lookup_body(target_body))
        if has_land and has_return and crew > 0 and is_planet:
            outbound["args"]["capture_mode"] = "circular"      # the split lander needs a stable orbit
            # FEASIBILITY CAP (physics, not decomposition): a SINGLE-LAUNCH crewed planetary round-trip is
            # sized for ONE kerbal + a light payload. The split transfer+lander already makes a ~490 t /
            # 4-booster rocket at crew=1; each extra seat or payload tonne cascades through ~6 km/s of Δv
            # (the LLM's crew=2 / payload_t=2 blew it to ~1000+ t — infeasible, and the design gate rejected
            # it on fineness). Pin the lander to crew=1 + a light payload so the gated design IS the
            # launchable flown craft (launch_to_lko itself flies payload_t=0.3). "A crew" = one kerbal.
            if launch_step is not None:
                la = launch_step.setdefault("args", {})
                if int(la.get("crew", 1) or 1) > 1:
                    la["crew"] = 1
                if float(la.get("payload_t", 0.0) or 0.0) > 0.5:
                    la["payload_t"] = 0.3
            if not any(s.get("primitive") == "jettison_transfer_stage" for s in steps):
                steps.insert(steps.index(outbound) + 1,
                             {"primitive": "jettison_transfer_stage", "args": {"target_body": target_body}})

    # 3) size ONE launch vehicle for the whole trip (mission_dv / split budget, legs, side boosters).
    #    The Δv budget + staging are PHYSICS, not the LLM's job: strip any value the model hallucinated for
    #    these so _apply_mission_aware_launch recomputes them from the mission graph (the model keeps the
    #    high-level args it owns — crew, capture mode, altitudes, name).
    if launch_step is not None:
        for k in ("mission_dv", "transfer_dv", "lander_dv", "lander_body_g", "needs_legs",
                  "max_core_engines", "radial_boosters"):
            launch_step.get("args", {}).pop(k, None)
    _apply_mission_aware_launch(steps, launch_body=launch_body)
    return steps


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

    # SPLIT-STAGE budget partition (a jettison_transfer_stage step present): the OUTBOUND transfer
    # (eject + capture) sizes the droppable TRANSFER stage; land + ascend + the RETURN transfer + recover
    # size the short LANDER stage. TWO independent budgets (each +margin ONCE) so the lander is immune to
    # capture overspend. Otherwise the whole post-LKO budget is ONE 'mission' stage (Mun/legacy/one-way).
    m = 1.0 + _POST_LKO_MARGIN_FRAC
    is_split = any(n.primitive == "jettison_transfer_stage" for n in g.nodes)
    transfer_dv = sum(getattr(n, "dv_mps", 0.0) for n in g.nodes
                      if n.primitive == "transfer" and str(n.target_body) != str(launch_body))
    lander_dv = sum(getattr(n, "dv_mps", 0.0) for n in g.nodes
                    if n.primitive in ("land", "ascend", "recover")
                    or (n.primitive == "transfer" and str(n.target_body) == str(launch_body)))
    merged = {"needs_legs": needs_legs}
    if is_split and transfer_dv > 0.0 and lander_dv > 0.0:
        land_body = next((str(n.target_body) for n in g.nodes
                          if n.primitive == "transfer" and str(n.target_body) != str(launch_body)), None)
        try:
            lander_body_g = float(getattr(lookup_body(land_body), "surface_g", 0.0))
        except Exception:
            lander_body_g = 0.0
        merged["transfer_dv"] = round(transfer_dv * (1.0 + _TRANSFER_MARGIN_FRAC), 1)
        merged["lander_dv"] = round(lander_dv * m, 1)
        merged["lander_body_g"] = round(lander_body_g, 3)
    else:
        merged["mission_dv"] = round(post_lko_dv * m, 1)
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
            # makes a several-hundred-tonne rocket a single core engine cannot lift off the pad (liftoff
            # TWR < 1.2), and stacking all that propellant into the core alone makes an un-launchable tall
            # needle. The fix is SIDE BOOSTERS.
            if is_split:
                # SIDE BOOSTERS (asparagus): radial pods carrying their OWN fuel tanks + engines that
                # crossfeed the core and DROP FIRST, shedding their mass early. They lift the heavy split
                # stack off the pad and let the CORE be far lighter and shorter than an all-core needle — in
                # sizing the wet mass falls ~504 t -> ~343 t and the liftoff TWR clears the floor. The
                # geometry gate ACCEPTS a SYMMETRIC strap-on ring (it judges the core envelope separately and
                # the even pod cluster on its own), so this is still a clean, gate-passing rocket. design.py
                # asparagus-sizes the pods; 4 even pods on a 6-engine core. (radial_boosters was stripped
                # above so the model can't override this physics choice — set it fresh here.)
                step["args"]["radial_boosters"] = 4
                step["args"]["max_core_engines"] = max(int(step["args"].get("max_core_engines", 1)), 6)
            elif post_lko_dv > 3200.0:
                # A heavy ONE-WAY interplanetary craft (far relay / lander, no return mass): a 2-pod assist +
                # a 4-engine core clears the pad without the full asparagus of a round-trip.
                step["args"]["radial_boosters"] = 2
                step["args"]["max_core_engines"] = max(int(step["args"].get("max_core_engines", 1)), 4)
            break
    return merged
