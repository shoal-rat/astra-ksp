"""MechJeb autopilots through the bridge, with every setting chosen by the AI.

Nothing here has a hidden default: target orbits, turn shapes, speeds and staging choices are all
arguments, and every reply echoes MechJeb's effective settings. Enabling tools can `watch`: the
reflex engine then runs game time hands-off (MechJeb flies, ASTRA's interlocks still apply) until
MechJeb reports the module idle, a `stop_when` trigger fires, or `max_game_s` passes. Without
watching, the game stays paused and MechJeb acts only while time runs (fly_until with
throttle='keep' and attitude='keep', fly_warp, or game_pause(false)).
"""

import time
from typing import Annotated, Any, Literal

from pydantic import Field

from astra.errors import AstraError, BridgeError, NotConnected
from astra.ksp import ksp
from astra.registry import tool
from astra.tools.control import node_info, run_separation
from astra.tools.game import reset_links
from astra.tools.observe import (
    bridge_part_id, bridge_parts, bridge_vessel_id, enum_name, finite, resolve_part, resolve_vessel,
)

MJ_MODULES = ("ascent", "node", "landing", "rendezvous", "dock", "staging", "smartass", "predictor")
# /mj-status flag names of the original bridge, used when the reply has no "modules" object.
_LEGACY_FLAGS = {"ascent": "ascentEnabled", "node": "nodeExecEnabled", "landing": "landingEnabled",
                 "rendezvous": "rvEnabled", "dock": "dockEnabled", "staging": "stagingEnabled"}
# Reply field that says whether the module engaged, per enabling route.
_ENGAGED_FLAG = {"ascent": "engaged", "node": "executing", "landing": "landing", "rendezvous": "enabled",
                 "dock": "enabled"}

_WATCH = Annotated[bool, Field(
    description="true: run game time now (hands-off, interlocks active) until MechJeb reports this "
                "autopilot idle, a stop_when trigger fires, an interlock trips, or max_game_s passes. "
                "false: return right away with the game paused.")]
_MAX_GAME_S = Annotated[float | None, Field(
    description="Required when watching: the most game seconds to watch, from your own estimate of how "
                "long the maneuver takes plus margin.")]
_STOP_WHEN = Annotated[list[dict] | None, Field(
    description="Extra triggers that end the watch early (fly_until's trigger language, e.g. "
                "[{'metric': 'apoapsis_altitude', 'op': '>=', 'value': 75000}]). MechJeb keeps flying; "
                "you just get control back to look.")]
_INTERLOCKS = Annotated[dict[str, Any] | None, Field(
    description="Overrides for the reflex interlocks while watching (same keys and defaults as "
                "fly_until's interlocks). The defaults stop the watch on any engine flameout and on an "
                "imminent-impact descent rate, both normal under MechJeb's own staging or landing burn: "
                "decide which to relax (false/null disables one). None = defaults.")]


def _safe(fn, default: Any = None) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def module_enabled(status: dict, module: str) -> bool | None:
    """Whether MechJeb `module` is active per a /mj-status reply (None when the reply is silent)."""
    mods = status.get("modules")
    if isinstance(mods, dict) and module in mods:
        entry = mods[module]
        if not isinstance(entry, dict):
            return bool(entry)
        if "enabled" in entry:
            return bool(entry["enabled"])
        if "target" in entry:  # SmartASS reports its target mode instead of a flag
            return entry["target"] not in (None, "OFF")
        return None
    flag = _LEGACY_FLAGS.get(module)
    if flag and flag in status:
        return bool(status[flag])
    return None


def _status() -> dict:
    return ksp().bridge.get("/mj-status", timeout=15.0)


def _require_core(status: dict) -> None:
    if status.get("hasCore") is False:
        raise AstraError("the active vessel has no MechJeb core",
                         "MechJebForAll.cfg adds one to every command part at game load; a vessel "
                         "saved before the patch needs a reload (game_restore / game_load_save)")


def _check_watch(watch: bool, max_game_s: float | None) -> None:
    if watch and (max_game_s is None or max_game_s <= 0):
        raise AstraError("watch=true needs a positive max_game_s",
                         "estimate the maneuver duration (burn times from mj_stage_stats or vessel_stages) "
                         "and add margin")


def _require_engaged(reply: dict, module: str) -> None:
    flag = _ENGAGED_FLAG.get(module)
    if flag and reply.get(flag) is False:
        raise AstraError(f"MechJeb did not engage its {module} autopilot (status: {reply.get('status') or reply.get('state')})",
                         "read mj_status; the reply's 'effective' settings show what MechJeb accepted")


def _watch(module: str, max_game_s: float, stop_when: list[dict] | None, interlocks: dict | None,
           label: str) -> dict:
    """Run game time hands-off until MechJeb's `module` goes idle; returns the reflex report."""
    from astra.reflex.engine import fly  # lazy: the reflex engine is optional at import time

    bridge = ksp().bridge
    poll = {"t": 0.0, "done": None, "last": None, "seen_on": False, "polls": 0}

    def mechjeb_done(_metrics: dict) -> str | None:
        now = time.monotonic()
        if poll["done"] or now - poll["t"] < 0.5:  # at most ~2 Hz of bridge traffic
            return poll["done"]
        poll["t"] = now
        try:
            status = bridge.get("/mj-status", timeout=5.0)
        except (BridgeError, NotConnected):
            return None  # transient: keep watching; max_game_s still bounds the watch
        poll["last"] = status
        poll["polls"] += 1
        enabled = module_enabled(status, module)
        if enabled:
            poll["seen_on"] = True
        elif enabled is False and poll["seen_on"]:
            poll["done"] = "mechjeb_done"
        elif enabled is False and poll["polls"] >= 6:  # ~3 s and MechJeb never showed it engaged
            poll["done"] = "mechjeb_never_engaged"
        return poll["done"]

    report = fly(until=stop_when or [], throttle="keep", attitude="keep", hands_off=True, interlocks=interlocks,
                 max_game_s=max_game_s, max_real_s=max(600.0, 4.0 * max_game_s),
                 extra_stop=mechjeb_done, label=label)
    report["mechjeb_finished"] = poll["done"] == "mechjeb_done"
    if poll["last"] is not None:
        report["mechjeb_status"] = poll["last"]
        still = module_enabled(poll["last"], module)
        if still and not report["mechjeb_finished"]:
            report["note"] = (f"MechJeb's {module} autopilot is still engaged: fly_until(until=[...], "
                              "throttle='keep', attitude='keep', max_game_s=...) lets it keep flying; "
                              f"mj_abort(['{module}']) releases it")
    return report


def _finish(out: dict, module: str, watch: bool, max_game_s: float | None, stop_when: list[dict] | None,
            interlocks: dict | None, label: str) -> dict:
    k = ksp()
    if watch:
        out["watch"] = _watch(module, float(max_game_s), stop_when, interlocks, label)
        reset_links()
    else:
        out["note"] = "MechJeb is engaged but acts only while game time runs (the game is paused now)"
    out["paused"] = k.hold_for_deliberation()
    return out


def _orbit_brief(v: Any) -> dict:
    o = v.orbit
    return {"body": o.body.name, "situation": enum_name(v.situation), "apoapsis_alt_m": finite(o.apoapsis_altitude),
            "periapsis_alt_m": o.periapsis_altitude, "inclination_deg": o.inclination * 57.29577951308232}


def _target_fields(tv: Any) -> dict:
    """Bridge fields naming a target vessel: persistentId when determinable (names repeat)."""
    pid = bridge_vessel_id(tv)
    return {"targetPersistentId": pid} if pid is not None else {"target": tv.name}


# ---------------------------------------------------------------------------------------------


@tool("autopilot")
def mj_ascent(
    altitude_m: Annotated[float, Field(description="Target circular orbit altitude above sea level, m.")],
    inclination_deg: Annotated[float, Field(description="Target inclination, degrees (negative = launch southward).")],
    auto_path: Annotated[bool, Field(
        description="true: MechJeb derives the turn from auto_turn_percent / auto_turn_speed_factor; false: "
                    "you give the turn shape with the turn_* arguments.")],
    autostage: Annotated[bool, Field(
        description="Let MechJeb stage on burnout. Its stager stays armed after the ascent and will "
                    "also stage in later burns until released with mj_abort(['staging']).")],
    skip_circularization: Annotated[bool, Field(
        description="true: stop after raising apoapsis and coasting out of the atmosphere (you circularize).")],
    limit_aoa: Annotated[bool, Field(description="Limit angle of attack during the turn.")],
    watch: _WATCH,
    max_game_s: _MAX_GAME_S = None,
    auto_turn_percent: Annotated[float | None, Field(
        description="auto_path=true: turn-start altitude as a fraction (0..1) of the atmosphere height "
                    "(MechJeb's autoTurnPercent).")] = None,
    auto_turn_speed_factor: Annotated[float | None, Field(
        description="auto_path=true: MechJeb's turn-start speed factor (larger = the turn starts at a higher "
                    "speed). The reply's mechjeb.effective shows the resulting autoTurnStartAltitude/"
                    "autoTurnStartVelocity/autoTurnEndAltitude.")] = None,
    turn_start_altitude_m: Annotated[float | None, Field(description="auto_path=false: altitude where the turn starts, m.")] = None,
    turn_start_velocity_mps: Annotated[float | None, Field(description="auto_path=false: speed where the turn starts, m/s.")] = None,
    turn_end_altitude_m: Annotated[float | None, Field(description="auto_path=false: altitude where the turn ends, m.")] = None,
    turn_end_angle_deg: Annotated[float | None, Field(description="auto_path=false: final flight-path angle above the horizon, degrees.")] = None,
    turn_shape_exponent: Annotated[float | None, Field(description="auto_path=false: turn shape 0..1 (higher = flatter, earlier turn).")] = None,
    max_aoa_deg: Annotated[float | None, Field(description="limit_aoa=true: the angle-of-attack limit, degrees.")] = None,
    autostage_pre_delay_s: Annotated[float | None, Field(description="autostage=true: delay before staging, s.")] = None,
    autostage_post_delay_s: Annotated[float | None, Field(description="autostage=true: delay after staging, s.")] = None,
    autostage_limit: Annotated[int | None, Field(description="autostage=true: MechJeb never stages below this stage number.")] = None,
    stop_when: _STOP_WHEN = None,
    interlocks: _INTERLOCKS = None,
) -> dict:
    """Fly an ascent to orbit with MechJeb's (classic) ascent autopilot, using the settings you chose.

    MechJeb does not ignite from the pad: if the vessel is pre-launch with no engine running, this
    tool activates the next stage once after MechJeb is engaged, running a moment of game time for
    it (the `ignition` entry says what lit); MechJeb then commands the throttle. The result echoes
    the settings MechJeb accepted (`mechjeb.effective`), and the watch report if watching.
    """
    _check_watch(watch, max_game_s)
    turn = {"turnStartAltitude": turn_start_altitude_m, "turnStartVelocity": turn_start_velocity_mps,
            "turnEndAltitude": turn_end_altitude_m, "turnEndAngle": turn_end_angle_deg,
            "turnShapeExponent": turn_shape_exponent}
    auto = {"autoTurnPercent": auto_turn_percent, "autoTurnSpeedFactor": auto_turn_speed_factor}
    if not auto_path:
        missing = [key for key, val in turn.items() if val is None]
        if missing:
            raise AstraError(f"auto_path=false needs the turn shape: missing {missing}",
                             "choose them from the vehicle's TWR and the atmosphere, or set auto_path=true")
        if not 0.0 <= turn_shape_exponent <= 1.0:
            raise AstraError("turn_shape_exponent must be within 0..1")
        if any(val is not None for val in auto.values()):
            raise AstraError("auto_path=false uses the turn_* arguments: leave auto_turn_* out")
    else:
        if any(val is not None for val in turn.values()):
            raise AstraError("auto_path=true computes the turn itself: leave the turn_* arguments out")
        if auto_turn_percent is None or auto_turn_speed_factor is None:
            raise AstraError("auto_path=true needs auto_turn_percent and auto_turn_speed_factor",
                             "MechJeb would otherwise reuse whatever values it stored last")
        if not 0.0 < auto_turn_percent <= 1.0 or auto_turn_speed_factor <= 0:
            raise AstraError("auto_turn_percent must be within (0, 1] and auto_turn_speed_factor positive")
    if limit_aoa and max_aoa_deg is None:
        raise AstraError("limit_aoa=true needs max_aoa_deg")
    stage_args = {"autostagePreDelay": autostage_pre_delay_s, "autostagePostDelay": autostage_post_delay_s,
                  "autostageLimit": autostage_limit}
    if autostage and any(val is None for val in stage_args.values()):
        raise AstraError("autostage=true needs autostage_pre_delay_s, autostage_post_delay_s and autostage_limit")
    k = ksp()
    v = k.vessel()
    _require_core(_status())
    body = {"ascentType": "classic", "altitude": altitude_m, "inclination": inclination_deg, "autoPath": auto_path,
            "autostage": autostage, "skipCircularization": skip_circularization, "limitAoA": limit_aoa,
            "maxAoA": max_aoa_deg if limit_aoa else None}
    body.update(auto if auto_path else turn)
    if autostage:
        body.update(stage_args)
    reply = k.bridge.post("/mj-ascent", body, timeout=30.0)
    _require_engaged(reply, "ascent")
    out: dict[str, Any] = {"mechjeb": reply}
    situation = enum_name(v.situation)
    engines_on = any(_safe(lambda e=e: e.active, False) for e in v.parts.engines)
    if situation == "pre_launch" and not engines_on:
        stage_before = v.control.current_stage
        _, game_s = run_separation(k, v.control.activate_next_stage, None)
        reset_links()
        v = k.vessel()
        out["ignition"] = {"staged": True, "stage_before": stage_before, "stage_after": v.control.current_stage,
                           "engines_lit": [e.part.name for e in v.parts.engines if _safe(lambda e=e: e.active, False)],
                           "game_s_elapsed": game_s,
                           "why": "MechJeb's ascent autopilot does not ignite from the pad"}
    else:
        out["ignition"] = {"staged": False, "situation": situation, "engines_running": engines_on}
        if not engines_on:
            out["ignition"]["note"] = ("no engine is running and MechJeb may not ignite one: "
                                       "control_stage (or control_part activate) to light the ascent engine")
    if autostage:
        out["reminder"] = "MechJeb's stager stays armed after the ascent: mj_abort(['staging']) before later burns"
    result = _finish(out, "ascent", watch, max_game_s, stop_when, interlocks, "mj_ascent")
    result["orbit"] = _safe(lambda: _orbit_brief(k.vessel()))
    return result


@tool("autopilot")
def mj_execute_node(
    all_nodes: Annotated[bool, Field(description="true: execute every planned node in order; false: only the next.")],
    autowarp: Annotated[bool, Field(description="Let MechJeb time-warp to the burn.")],
    lead_time_s: Annotated[float, Field(description="Seconds before the burn start at which MechJeb stops warping and orients.")],
    autostage: Annotated[bool, Field(
        description="Let MechJeb's stager stage during the burn(s) when a stage burns out. false releases "
                    "a stager left armed by an earlier ascent or landing.")],
    watch: _WATCH,
    max_game_s: _MAX_GAME_S = None,
    stop_when: _STOP_WHEN = None,
    interlocks: _INTERLOCKS = None,
) -> dict:
    """Execute maneuver node(s) with MechJeb's node executor.

    MechJeb (2.15) ends each burn by its own cutoff logic; there is no tolerance setting. It may not
    auto-warp to a distant node while still turning toward it; for far nodes warp close first with
    fly_warp. Completion is judged by the executor going idle; the result lists the nodes left and
    the orbit afterwards so you can verify the burn.
    """
    _check_watch(watch, max_game_s)
    if lead_time_s < 0:
        raise AstraError("lead_time_s must be >= 0")
    k = ksp()
    v = k.vessel()
    nodes = v.control.nodes
    if not nodes:
        raise AstraError("there is no maneuver node to execute", "node_create or mj_plan(place=true) first")
    now = k.sc.ut
    before = [node_info(n, i, now) for i, n in enumerate(nodes)]
    _require_core(_status())
    reply = k.bridge.post("/mj-execute-node", {"all": all_nodes, "autowarp": autowarp, "leadTime": lead_time_s,
                                                "autostage": autostage}, timeout=30.0)
    _require_engaged(reply, "node")
    out: dict[str, Any] = {"mechjeb": reply, "nodes_before": before}
    result = _finish(out, "node", watch, max_game_s, stop_when, interlocks, "mj_execute_node")
    v = k.vessel()
    now = k.sc.ut
    result["nodes_after"] = [node_info(n, i, now) for i, n in enumerate(v.control.nodes)]
    result["orbit"] = _orbit_brief(v)
    return result


@tool("autopilot")
def mj_land(
    targeted: Annotated[bool, Field(description="true: land at lat_deg/lon_deg; false: land wherever the trajectory leads.")],
    touchdown_speed_mps: Annotated[float, Field(description="Vertical speed at touchdown, m/s.")],
    deploy_gears: Annotated[bool, Field(description="Let MechJeb deploy landing gear/legs before touchdown.")],
    deploy_chutes: Annotated[bool, Field(description="Let MechJeb deploy parachutes (atmospheric bodies).")],
    watch: _WATCH,
    max_game_s: _MAX_GAME_S = None,
    lat_deg: Annotated[float | None, Field(description="targeted=true: landing latitude, degrees.")] = None,
    lon_deg: Annotated[float | None, Field(description="targeted=true: landing longitude, degrees.")] = None,
    stop_when: _STOP_WHEN = None,
    interlocks: _INTERLOCKS = None,
) -> dict:
    """Land with MechJeb's landing autopilot (deorbit or descent, braking, touchdown).

    It has stalled from orbits whose periapsis is above the atmosphere and on grazing entries, so
    `facts` reports the periapsis against the atmosphere for you to judge. MechJeb's landing may
    leave its stager armed: release it with mj_abort(['staging']) before later burns. When
    watching, the default impact interlock trips during MechJeb's braking burn unless you relax it.
    """
    _check_watch(watch, max_game_s)
    if targeted and (lat_deg is None or lon_deg is None):
        raise AstraError("targeted=true needs lat_deg and lon_deg")
    if touchdown_speed_mps <= 0:
        raise AstraError("touchdown_speed_mps must be positive")
    k = ksp()
    v = k.vessel()
    body = v.orbit.body
    facts = {"body": body.name, "situation": enum_name(v.situation), "periapsis_alt_m": v.orbit.periapsis_altitude,
             "atmosphere_depth_m": body.atmosphere_depth if body.has_atmosphere else None}
    if facts["atmosphere_depth_m"] and facts["periapsis_alt_m"] > facts["atmosphere_depth_m"]:
        facts["note"] = "periapsis is above the atmosphere"
    _require_core(_status())
    reply = k.bridge.post("/mj-land", {"targeted": targeted, "lat": lat_deg if targeted else None,
                                       "lon": lon_deg if targeted else None, "touchdownSpeed": touchdown_speed_mps,
                                       "deployGears": deploy_gears, "deployChutes": deploy_chutes}, timeout=30.0)
    _require_engaged(reply, "landing")
    out: dict[str, Any] = {"mechjeb": reply, "facts": facts}
    result = _finish(out, "landing", watch, max_game_s, stop_when, interlocks, "mj_land")
    result["situation"] = _safe(lambda: enum_name(k.vessel().situation))
    return result


@tool("autopilot")
def mj_rendezvous(
    target: Annotated[str, Field(description="Target vessel name (tolerant) or '#id'. It also becomes the KSP target.")],
    desired_distance_m: Annotated[float, Field(description="Distance to stop at from the target, m.")],
    max_phasing_orbits: Annotated[float, Field(description="Most phasing orbits MechJeb may wait for the intercept.")],
    max_closing_speed_mps: Annotated[float, Field(description="Highest approach speed MechJeb may use, m/s.")],
    watch: _WATCH,
    max_game_s: _MAX_GAME_S = None,
    stop_when: _STOP_WHEN = None,
    interlocks: _INTERLOCKS = None,
) -> dict:
    """Rendezvous with a vessel using MechJeb's rendezvous autopilot (main engine).

    Sets the KSP target first. Good for closing kilometres down to the distance you choose; finish
    with mj_dock. The result reports distance and relative speed at the end.
    """
    _check_watch(watch, max_game_s)
    if desired_distance_m <= 0 or max_closing_speed_mps <= 0 or max_phasing_orbits < 0:
        raise AstraError("desired_distance_m and max_closing_speed_mps must be positive, max_phasing_orbits >= 0")
    k = ksp()
    v = k.vessel()
    tv = resolve_vessel(target, exclude=v)
    k.sc.target_vessel = tv
    _require_core(_status())
    reply = k.bridge.post("/mj-rendezvous", {**_target_fields(tv), "desiredDistance": desired_distance_m,
                                             "maxPhasingOrbits": max_phasing_orbits,
                                             "maxClosingSpeed": max_closing_speed_mps}, timeout=30.0)
    _require_engaged(reply, "rendezvous")
    out: dict[str, Any] = {"target": tv.name, "mechjeb": reply}
    result = _finish(out, "rendezvous", watch, max_game_s, stop_when, interlocks, "mj_rendezvous")
    result["relative"] = _safe(lambda: _relative(k.vessel(), tv))
    return result


def _relative(v: Any, tv: Any) -> dict:
    nr = v.orbit.body.non_rotating_reference_frame
    d = [a - b for a, b in zip(tv.position(nr), v.position(nr))]
    s = [a - b for a, b in zip(tv.velocity(nr), v.velocity(nr))]
    return {"distance_m": sum(x * x for x in d) ** 0.5, "relative_speed_mps": sum(x * x for x in s) ** 0.5}


@tool("autopilot")
def mj_dock(
    own_port: Annotated[int | str, Field(description="Docking port on the active vessel: idx from vessel_parts(kind='docking_port') or a unique name/tag.")],
    target_port: Annotated[int | str, Field(description="Docking port on the target vessel: idx from target_info's target_docking_ports.")],
    target: Annotated[str, Field(description="Target vessel name (tolerant) or '#id'; it must be loaded (within ~2 km).")],
    speed_limit_mps: Annotated[float, Field(description="Highest approach speed MechJeb may use, m/s.")],
    force_roll: Annotated[bool, Field(description="Hold a specific roll angle while docking.")],
    override_safe_distance: Annotated[bool, Field(
        description="Replace MechJeb's computed keep-out distance around the target with safe_distance_m.")],
    watch: _WATCH,
    max_game_s: _MAX_GAME_S = None,
    roll_deg: Annotated[float | None, Field(description="force_roll=true: the roll angle, degrees.")] = None,
    safe_distance_m: Annotated[float | None, Field(
        description="override_safe_distance=true: keep-out distance from the target while maneuvering around it, m.")] = None,
    stop_when: _STOP_WHEN = None,
    interlocks: _INTERLOCKS = None,
) -> dict:
    """Dock with MechJeb's docking autopilot (RCS), between the two ports you chose.

    The bridge makes `own_port` the control point and `target_port` the target, then engages
    MechJeb. MechJeb also switches itself off on target loss, so success is judged here by the
    ports: `docked` is true only if the own port reports docked or the part count jumped.
    """
    _check_watch(watch, max_game_s)
    if force_roll and roll_deg is None:
        raise AstraError("force_roll=true needs roll_deg")
    if override_safe_distance and (safe_distance_m is None or safe_distance_m <= 0):
        raise AstraError("override_safe_distance=true needs a positive safe_distance_m")
    if speed_limit_mps <= 0:
        raise AstraError("speed_limit_mps must be positive")
    k = ksp()
    v = k.vessel()
    own, own_idx = resolve_part(v, own_port)
    if own.docking_port is None:
        raise AstraError(f"own_port idx {own_idx} ({own.name}) is not a docking port",
                         "vessel_parts(kind='docking_port') lists them")
    tv = resolve_vessel(target, exclude=v)
    tpart, t_idx = resolve_part(tv, target_port)
    if tpart.docking_port is None:
        raise AstraError(f"target_port idx {t_idx} ({tpart.name}) on {tv.name} is not a docking port",
                         "target_info lists target_docking_ports")
    # The bridge addresses parts by persistentId; its part lists share kRPC's idx order.
    tv_pid = bridge_vessel_id(tv)
    own_pid = bridge_part_id(bridge_parts(), own_idx, own.name)
    t_pid = bridge_part_id(bridge_parts({"vesselPersistentId": tv_pid} if tv_pid is not None else {"vessel": tv.name}),
                           t_idx, tpart.name)
    parts_before = len(v.parts.all)
    _require_core(_status())
    reply = k.bridge.post("/mj-dock", {"ownPortPartId": own_pid, "targetPortPartId": t_pid,
                                       "speedLimit": speed_limit_mps, "forceRoll": force_roll,
                                       "roll": roll_deg if force_roll else None,
                                       "overrideSafeDistance": override_safe_distance,
                                       "safeDistance": safe_distance_m if override_safe_distance else None},
                          timeout=30.0)
    _require_engaged(reply, "dock")
    out: dict[str, Any] = {"own_port": {"idx": own_idx, "name": own.name},
                           "target_port": {"idx": t_idx, "name": tpart.name, "vessel": tv.name}, "mechjeb": reply}
    result = _finish(out, "dock", watch, max_game_s, stop_when, interlocks, "mj_dock")
    now = k.vessel()
    parts_after = len(now.parts.all)
    own_state = _safe(lambda: enum_name(own.docking_port.state))
    result.update(parts_before=parts_before, parts_after=parts_after, own_port_state=own_state,
                  docked=own_state == "docked" or parts_after > parts_before, active_vessel=now.name)
    return result


@tool("autopilot")
def mj_plan(
    operation: Annotated[str, Field(
        description="MechJeb maneuver-planner operation: circularize, apoapsis, periapsis, ellipticize, "
                    "eccentricity, semi_major, inclination, lan, longitude, plane, kill_rel_vel, hohmann, "
                    "lambert, interplanetary, course_correction, moon_return, resonant_orbit.")],
    params: Annotated[dict[str, float | int | str | bool], Field(
        description="Flat operation parameters as the bridge's /mj-plan takes them (e.g. {'timeRef': "
                    "'apoapsis', 'newApoapsis': 250000}); values must be scalars. Also: planFrom "
                    "('current' | 'last_node'), replaceExisting (true deletes existing nodes when placing), "
                    "target / targetBody for target-relative operations.")],
    place: Annotated[bool, Field(
        description="true: place the node(s) (refused while nodes exist unless params has replaceExisting "
                    "or planFrom='last_node'); false: dry run, only report them.")],
) -> dict:
    """Ask MechJeb's maneuver planner for node(s) for an operation, as a dry run or placed.

    Compare MechJeb's answer with your own compute_* result before trusting either. When placed,
    `nodes` are read back from KSP (Δv, time, orbit after, encounter patches).
    """
    if not operation.strip():
        raise AstraError("operation is required")
    if {"operation", "place"} & set(params):
        raise AstraError("params must not repeat 'operation' or 'place'")
    k = ksp()
    v = k.vessel()
    count_before = len(v.control.nodes)
    _require_core(_status())
    reply = k.bridge.post("/mj-plan", {"operation": operation.strip(), "place": place, **params}, timeout=60.0)
    out: dict[str, Any] = {"operation": operation, "placed": place, "mechjeb": reply}
    if place:
        now = k.sc.ut
        nodes = k.vessel().control.nodes
        out["nodes"] = [node_info(n, i, now) for i, n in enumerate(nodes)]
        if count_before and len(nodes) <= count_before:
            out["note"] = f"{count_before} node(s) existed before; MechJeb replaced them"
    return out


@tool("autopilot")
def mj_status() -> dict:
    """What MechJeb is doing: which autopilots are engaged and their status text, target, nodes.

    `modules_enabled` summarizes ascent/node/landing/rendezvous/dock/staging/smartass/predictor
    (null = not reported). The rest is the bridge's reply as-is.
    """
    status = _status()
    status["modules_enabled"] = {m: module_enabled(status, m) for m in MJ_MODULES}
    return status


@tool("autopilot")
def mj_abort(
    modules: Annotated[list[Literal[MJ_MODULES + ("all",)]], Field(  # type: ignore[valid-type]
        description="Which MechJeb modules to release: " + ", ".join(MJ_MODULES) + ", or 'all'.")],
) -> dict:
    """Disengage MechJeb autopilots (and its auto-stager) and verify they are off.

    Throttle and attitude are left as MechJeb left them: set them yourself afterwards
    (control_set, control_attitude).
    """
    if not modules:
        raise AstraError("name at least one module", "e.g. ['staging'] or ['all']")
    k = ksp()
    reply = k.bridge.post("/mj-abort", {"modules": ",".join(modules)}, timeout=30.0)
    status = _status()
    check = MJ_MODULES if "all" in modules else tuple(modules)
    still = [m for m in check if module_enabled(status, m)]
    out = {"released": list(modules), "mechjeb": reply,
           "modules_enabled": {m: module_enabled(status, m) for m in MJ_MODULES}}
    if still:
        out["still_enabled"] = still
    return out


@tool("autopilot")
def mj_stage_stats() -> dict:
    """MechJeb's per-stage Δv, burn time, TWR and masses for the active vessel (vacuum and atmospheric).

    A cross-check for vessel_stages. MechJeb computes asynchronously: `pending` true means ask
    again (it updates while game time runs).
    """
    bridge = ksp().bridge
    reply = bridge.get("/mj-stage-stats", timeout=15.0)
    deadline = time.monotonic() + 3.0
    while reply.get("pending") and time.monotonic() < deadline:
        time.sleep(0.5)
        reply = bridge.get("/mj-stage-stats", timeout=15.0)
    _require_core(reply)
    return reply
