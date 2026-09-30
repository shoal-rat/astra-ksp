"""Direct controls: throttle and switches, staging, attitude hold, part actions, action groups,
maneuver nodes, and targets.

These commands act instantly and do not advance game time, except `control_attitude` when asked to
wait for alignment. Every tool reads back the state it changed and reports it.
"""

import math
import time
from typing import Annotated, Any, Iterable, Literal

from pydantic import Field

from astra.errors import AstraError
from astra.ksp import ksp
from astra.registry import tool
from astra.tools.game import reset_links, stop_warp, wait_for
from astra.tools.observe import (
    PART_KINDS, current_target, enum_name, finite, module_state, part_index, part_module, patch_chain,
    resolve_part, resolve_vessel,
)

G0 = 9.80665
_NAN = float("nan")

# SAS modes by kRPC SASMode member name, with the navball words people also use.
SAS_MODES = {
    "stability_assist": "stability_assist", "hold": "stability_assist",
    "maneuver": "maneuver", "node": "maneuver",
    "prograde": "prograde", "retrograde": "retrograde",
    "normal": "normal", "anti_normal": "anti_normal", "antinormal": "anti_normal",
    "radial": "radial", "radial_out": "radial", "anti_radial": "anti_radial", "radial_in": "anti_radial",
    "target": "target", "anti_target": "anti_target",
}


def _safe(fn, default: Any = None) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 — optional readouts only
        return default


def _sas_member(sc: Any, mode: str) -> Any:
    key = SAS_MODES.get(mode.strip().lower())
    if key is None:
        raise AstraError(f"unknown SAS mode {mode!r}", "one of: " + ", ".join(sorted(set(SAS_MODES.values()))))
    return getattr(sc.SASMode, key)


def _controls(v: Any) -> dict:
    c = v.control
    return {"throttle": c.throttle, "sas": c.sas, "sas_mode": _safe(lambda: enum_name(c.sas_mode)),
            "speed_mode": _safe(lambda: enum_name(c.speed_mode)), "rcs": c.rcs, "gear": c.gear,
            "legs": c.legs, "lights": c.lights, "brakes": c.brakes}


def _control_warnings(v: Any, sc: Any) -> list[str]:
    out = []
    thrust = _safe(lambda: v.thrust, 0.0) or 0.0
    throttle = _safe(lambda: v.control.throttle, 0.0) or 0.0
    if throttle == 0 and thrust > 500.0:
        out.append(f"throttle is 0 but thrust is {thrust / 1e3:.1f} kN: solid or unthrottleable engines are burning")
    if throttle > 0 and (_safe(lambda: sc.rails_warp_factor, 0) or 0) > 0:
        out.append("rails time warp is active: engines cannot fire until warp stops")
    if _safe(lambda: enum_name(v.control.state)) == "none":
        out.append("control state is 'none': no pilot, probe signal or power; commands may have no effect")
    return out


@tool("control")
def control_set(
    throttle: Annotated[float | None, Field(description="Main throttle 0..1 (does not affect solid boosters).")] = None,
    sas: Annotated[bool | None, Field(description="SAS on/off. Turning SAS on releases the kRPC autopilot.")] = None,
    sas_mode: Annotated[str | None, Field(
        description="SAS hold mode: stability_assist, prograde, retrograde, normal, anti_normal, radial "
                    "(out), anti_radial (in), maneuver, target, anti_target. Needs SAS on; basic probe "
                    "cores and low-level pilots lack some modes.")] = None,
    speed_mode: Annotated[Literal["orbit", "surface", "target"] | None, Field(
        description="Navball speed mode; decides what SAS prograde/retrograde follow.")] = None,
    rcs: Annotated[bool | None, Field(description="RCS on/off.")] = None,
    gear: Annotated[bool | None, Field(description="Landing gear/wheels deployed.")] = None,
    legs: Annotated[bool | None, Field(description="Landing legs deployed.")] = None,
    lights: Annotated[bool | None, Field(description="Lights on/off.")] = None,
    brakes: Annotated[bool | None, Field(description="Brakes on/off.")] = None,
) -> dict:
    """Set throttle and vessel switches; omitted arguments stay as they are.

    Returns the control state read back afterwards plus warnings (e.g. thrust with throttle 0
    means solid boosters are burning; rails warp prevents engines from firing). A setting KSP
    refuses (typically a SAS mode the control point lacks) raises an error that lists what was
    applied and what was refused.
    """
    if throttle is not None and not 0.0 <= throttle <= 1.0:
        raise AstraError(f"throttle must be within 0..1, got {throttle}")
    k = ksp()
    v = k.vessel()
    sc, c = k.sc, v.control
    applied: dict[str, Any] = {}
    refused: dict[str, str] = {}

    def attempt(key: str, value: Any, fn) -> None:
        try:
            fn()
            applied[key] = value
        except Exception as exc:  # noqa: BLE001 — collected and reported below
            refused[key] = str(exc).splitlines()[0]

    if sas:
        attempt("autopilot", "released", v.auto_pilot.disengage)
    if sas is not None:
        attempt("sas", sas, lambda: setattr(c, "sas", sas))
    if speed_mode is not None:
        attempt("speed_mode", speed_mode, lambda: setattr(c, "speed_mode", getattr(sc.SpeedMode, speed_mode)))
    if sas_mode is not None:
        member = _sas_member(sc, sas_mode)
        attempt("sas_mode", enum_name(member), lambda: setattr(c, "sas_mode", member))
    for key, value in (("throttle", throttle), ("rcs", rcs), ("gear", gear), ("legs", legs),
                       ("lights", lights), ("brakes", brakes)):
        if value is not None:
            attempt(key, value, lambda key=key, value=value: setattr(c, key, value))
    state = _controls(v)
    if refused:
        hint = "applied: " + (", ".join(f"{a}={b}" for a, b in applied.items()) or "nothing")
        if "sas_mode" in refused:
            hint += ("; this control point lacks that SAS mode: use control_attitude with the same "
                     "direction (kRPC autopilot) instead")
        raise AstraError("refused: " + "; ".join(f"{a} ({b})" for a, b in refused.items()), hint)
    return {"applied": applied, "state": state, "warnings": _control_warnings(v, sc)}


# ---------------------------------------------------------------------------------------------
# Staging


def separation_plan(parent: dict[int, int | None], cut: Iterable[int], engines: set[int],
                    keep: int) -> dict[str, set[int]]:
    """Which parts stay with part `keep` when each part in `cut` detaches from its parent.

    Decouplers leave with their own subtree (the part tree is rooted at the command part). Returns
    {"kept": part idx still attached to `keep`, "dropped": the rest, "engines_kept": engines kept}.
    """
    cut = set(cut)
    links: dict[int, list[int]] = {i: [] for i in parent}
    for child, par in parent.items():
        if par is not None and child not in cut:
            links[child].append(par)
            links.setdefault(par, []).append(child)
    kept, stack = set(), [keep]
    while stack:
        n = stack.pop()
        if n in kept:
            continue
        kept.add(n)
        stack.extend(links.get(n, ()))
    return {"kept": kept, "dropped": set(parent) - kept, "engines_kept": engines & kept}


def _structure(v: Any) -> tuple[list[Any], dict[int, int], dict[int, int | None], int]:
    parts = v.parts.all
    idx_of = part_index(parts)
    parent = {}
    for i, p in enumerate(parts):
        par = p.parent
        parent[i] = idx_of.get(par._object_id) if par is not None else None
    ctl = _safe(lambda: v.parts.controlling)
    keep = idx_of.get(ctl._object_id) if ctl is not None else None
    if keep is None:
        keep = next(i for i, par in parent.items() if par is None)
    return parts, idx_of, parent, keep


def separation_warnings(v: Any, cut_idx: list[int]) -> tuple[list[str], dict]:
    """Warnings about separating the parts in `cut_idx`, and the plan behind them."""
    parts, idx_of, parent, keep = _structure(v)
    engines = {idx_of[e.part._object_id] for e in v.parts.engines if e.part._object_id in idx_of}
    plan = separation_plan(parent, cut_idx, engines, keep)
    warnings = []
    if cut_idx and not plan["engines_kept"]:
        warnings.append("after this separation no engine remains on the controlling part's side "
                        "(payload, capsule or heat-shield release?). Fire it only if that is intended.")
    summary = {"parts_leaving": len(plan["dropped"]), "engines_kept": sorted(plan["engines_kept"]),
               "controlling_idx": keep}
    return warnings, summary


def _active_engines(v: Any) -> dict[int, Any]:
    return {e.part._object_id: e for e in v.parts.engines if _safe(lambda e=e: e.active, False)}


def run_separation(k: Any, fire, count_before: int | None, wait_s: float = 3.0) -> tuple[Any, float]:
    """Call `fire()` with game time running and, if `count_before` is given, keep it running until
    the active vessel has fewer parts (KSP splits a vessel over several physics frames).

    kRPC's staging, decouple and undock calls wait for physics frames: made while the game is
    paused they never return (see KSP.running). Returns (fire's result, game seconds that ran);
    the game is paused for deliberation afterwards."""
    stop_warp(k.sc)
    ut0 = _safe(lambda: k.sc.ut)
    try:
        with k.running():
            result = fire()
            if count_before is not None:
                wait_for(lambda: len(k.vessel().parts.all) < count_before, wait_s, 0.05)
    finally:
        k.hold_for_deliberation()
    ut1 = _safe(lambda: k.sc.ut)
    return result, (ut1 - ut0 if ut0 is not None and ut1 is not None else 0.0)


@tool("control")
def control_stage() -> dict:
    """Activate the next stage (the space bar) and report exactly what happened.

    Before firing it checks what the stage detaches and warns (does not block) when no engine
    would remain on the controlling part's side. Staging needs game time: it runs for the few
    physics frames KSP takes to split the vessel (`game_s_elapsed`, typically a fraction of a
    second, with whatever throttle is set), then pauses again. It reports the stage number change,
    parts dropped, engines newly lit (with available thrust), jettisoned vessels, and whether the
    active vessel changed. Part idx numbers change when parts leave: list the parts again before
    addressing one.
    """
    k = ksp()
    v = k.vessel()
    c = v.control
    if c.stage_lock:
        raise AstraError("staging is locked", "unlock staging in the game UI (the lock icon by the stage stack)")
    stage_before = c.current_stage
    if stage_before <= 0:
        raise AstraError("there is no stage left to activate", "use control_part for individual parts")
    nxt = stage_before - 1
    firing_parts = v.parts.in_stage(nxt)
    parts = v.parts.all
    idx_of = part_index(parts)
    firing = []
    for p in firing_parts:
        is_sep = _safe(lambda p=p: (p.decoupler is not None and not p.decoupler.decoupled)
                       or p.launch_clamp is not None, False)
        if is_sep:
            firing.append(idx_of[p._object_id])
    warnings, plan = separation_warnings(v, firing) if firing else ([], {"parts_leaving": 0})
    activated = [{"idx": idx_of.get(p._object_id), "name": p.name} for p in firing_parts]
    lit_before = _active_engines(v)
    count_before = len(parts)
    vid = v._object_id

    jettisoned, game_s = run_separation(k, c.activate_next_stage, count_before if plan["parts_leaving"] else None)
    names = [_safe(lambda j=j: j.name, "?") for j in jettisoned or []]
    reset_links()
    v2 = k.vessel()
    lit_after = _active_engines(v2)
    new_engines = [{"name": e.part.name, "available_thrust_kn": e.available_thrust / 1e3,
                    "solid": e.throttle_locked} for oid, e in lit_after.items() if oid not in lit_before]
    count_after = len(v2.parts.all)
    out: dict[str, Any] = {
        "stage_before": stage_before, "stage_after": v2.control.current_stage,
        "activated_parts": activated, "engines_lit": new_engines,
        "parts_before": count_before, "parts_after": count_after,
        "jettisoned_vessels": names, "active_vessel": v2.name, "active_vessel_changed": v2._object_id != vid,
        "thrust_kn": v2.thrust / 1e3, "available_thrust_kn": v2.available_thrust / 1e3,
        "warnings": warnings, "game_s_elapsed": game_s, "paused": k.paused,
    }
    if plan["parts_leaving"] and count_after >= count_before:
        out["separation_confirmed"] = False
        out["note"] = "no parts left within the wait; check vessel_parts and camera_look"
    elif plan["parts_leaving"]:
        out["separation_confirmed"] = True
    return out


# ---------------------------------------------------------------------------------------------
# Attitude

_VECTOR_FRAMES = ("surface", "orbital", "body", "inertial")
_AP_MODES = ("hold", "prograde", "retrograde", "normal", "antinormal", "radial_out", "radial_in", "node",
             "target", "anti_target", "up", "vector")


def _unit(a):
    n = math.sqrt(sum(x * x for x in a))
    if n == 0:
        raise AstraError("direction vector has zero length")
    return tuple(x / n for x in a)


def _angle_deg(a, b) -> float:
    a, b = _unit(a), _unit(b)
    return math.degrees(math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))))


def _radial_out_sign(k: Any, v: Any) -> float:
    """+1 if the x axis of vessel.orbital_reference_frame points away from the body, else -1."""
    nr = v.orbit.body.non_rotating_reference_frame
    x = k.sc.transform_direction((1.0, 0.0, 0.0), v.orbital_reference_frame, nr)
    return 1.0 if sum(a * b for a, b in zip(x, v.position(nr))) > 0 else -1.0


def _aim(k: Any, v: Any, mode: str, frame: str | None, pitch: float | None, heading: float | None,
         vector: list[float] | None) -> dict:
    """Autopilot target for `mode`: a reference frame in which the target is constant (so the
    autopilot holds it without re-aiming), plus a direction or pitch/heading."""
    body = v.orbit.body
    if mode == "hold":
        if pitch is None or heading is None:
            raise AstraError("mode 'hold' needs pitch_deg and heading_deg")
        return {"frame": v.surface_reference_frame, "pitch_heading": (pitch, heading), "tracks": True,
                "label": f"pitch {pitch:g} deg, heading {heading:g} deg"}
    if mode in ("prograde", "retrograde"):
        if frame not in ("orbital", "surface"):
            raise AstraError(f"mode '{mode}' needs frame='orbital' or frame='surface'",
                             "orbital: relative to the inertial orbit (burns in space); surface: relative "
                             "to the ground (ascent, reentry, landing)")
        sign = 1.0 if mode == "prograde" else -1.0
        rf = v.orbital_reference_frame if frame == "orbital" else v.surface_velocity_reference_frame
        return {"frame": rf, "direction": (0.0, sign, 0.0), "tracks": True, "label": f"{frame} {mode}"}
    if mode in ("normal", "antinormal"):
        return {"frame": v.orbital_reference_frame, "direction": (0.0, 0.0, 1.0 if mode == "normal" else -1.0),
                "tracks": True, "label": mode}
    if mode in ("radial_out", "radial_in"):
        s = _radial_out_sign(k, v) * (1.0 if mode == "radial_out" else -1.0)
        return {"frame": v.orbital_reference_frame, "direction": (s, 0.0, 0.0), "tracks": True, "label": mode}
    if mode == "up":
        return {"frame": v.surface_reference_frame, "direction": (1.0, 0.0, 0.0), "tracks": True, "label": "local up"}
    if mode == "node":
        nodes = v.control.nodes
        if not nodes:
            raise AstraError("there is no maneuver node", "node_create first")
        return {"frame": nodes[0].reference_frame, "direction": (0.0, 1.0, 0.0), "tracks": True,
                "label": "maneuver node 0 remaining burn"}
    if mode in ("target", "anti_target"):
        obj, _ = current_target(k.sc)
        if obj is None:
            raise AstraError("no target is set", "target_set first")
        nr = body.non_rotating_reference_frame
        d = _unit([a - b for a, b in zip(obj.position(nr), v.position(nr))])
        if mode == "anti_target":
            d = tuple(-x for x in d)
        return {"frame": nr, "direction": d, "tracks": False,
                "label": f"{mode} (aimed at the target's current direction; it does not follow the target)"}
    if mode == "vector":
        if frame not in _VECTOR_FRAMES:
            raise AstraError("mode 'vector' needs frame = " + " | ".join(_VECTOR_FRAMES))
        if not vector or len(vector) != 3:
            raise AstraError("mode 'vector' needs vector=[x, y, z]")
        rf = {"surface": v.surface_reference_frame, "orbital": v.orbital_reference_frame,
              "body": body.reference_frame, "inertial": body.non_rotating_reference_frame}[frame]
        return {"frame": rf, "direction": _unit(vector), "tracks": True, "label": f"vector {list(vector)} in {frame}"}
    raise AstraError(f"unknown attitude mode {mode!r}",
                     "one of: " + ", ".join(_AP_MODES) + ", sas:<mode>, off")


def _sas_direction(k: Any, v: Any, member: str):
    """(frame, desired direction) for a SAS hold mode, or None when it has no fixed target."""
    sc = k.sc
    body = v.orbit.body
    if member == "stability_assist":
        return None
    if member == "maneuver":
        nodes = v.control.nodes
        if not nodes:
            return None
        nr = body.non_rotating_reference_frame
        return nr, nodes[0].remaining_burn_vector(nr)
    if member in ("target", "anti_target"):
        obj, _ = current_target(sc)
        if obj is None:
            return None
        nr = body.non_rotating_reference_frame
        d = [a - b for a, b in zip(obj.position(nr), v.position(nr))]
        return nr, d if member == "target" else [-x for x in d]
    speed = enum_name(v.control.speed_mode)
    if speed == "target":
        return None
    rf = body.reference_frame if speed == "surface" else body.non_rotating_reference_frame
    fl = v.flight(rf)
    if member in ("radial", "anti_radial"):
        radial = fl.radial
        out = radial if sum(a * b for a, b in zip(radial, v.position(rf))) > 0 else [-x for x in radial]
        return rf, out if member == "radial" else [-x for x in out]
    return rf, getattr(fl, member)


def _authority(v: Any) -> dict:
    """Maximum angular acceleration per axis from available torque and moment of inertia."""
    try:
        pos, neg = v.available_torque
        inertia = v.moment_of_inertia
    except Exception:  # noqa: BLE001
        return {}
    acc = [min(abs(p), abs(n)) / i if i > 0 else 0.0 for p, n, i in zip(pos, neg, inertia)]
    pitch, roll, yaw = (math.degrees(a) for a in acc)
    steer = min(pitch, yaw)
    return {"pitch_degps2": pitch, "yaw_degps2": yaw, "roll_degps2": roll,
            "turn_90deg_min_s": 2.0 * math.sqrt(90.0 / steer) if steer > 0 else None}


def _wait_aligned(k: Any, error_fn, threshold_deg: float, timeout_s: float) -> dict:
    """Run game time until error_fn() stays within the threshold (3 readings) or timeout_s of game
    time passes; then pause for deliberation."""
    sc = k.sc
    warp_stopped = stop_warp(sc)
    ut0, wall0 = sc.ut, time.monotonic()
    wall_cap = 4.0 * timeout_s + 10.0  # guards against a game that stops advancing time
    streak, err, aligned = 0, None, False
    k.set_paused(False)
    try:
        while True:
            err = _safe(error_fn)
            streak = streak + 1 if err is not None and err <= threshold_deg else 0
            if streak >= 3:
                aligned = True
                break
            if sc.ut - ut0 >= timeout_s or time.monotonic() - wall0 > wall_cap:
                break
            time.sleep(0.1)
    finally:
        paused = k.hold_for_deliberation()
    out = {"aligned": aligned, "error_deg": err, "waited_game_s": sc.ut - ut0, "paused": paused}
    if warp_stopped:
        out["warp_stopped"] = True
    return out


@tool("control")
def control_attitude(
    mode: Annotated[str, Field(
        description="hold | prograde | retrograde | normal | antinormal | radial_out | radial_in | node | "
                    "target | anti_target | up | vector | sas:<mode> | off. Autopilot modes use kRPC's "
                    "autopilot; 'sas:<mode>' uses the vessel's SAS (e.g. 'sas:retrograde', "
                    "'sas:stability_assist'); 'off' releases both (the vessel drifts).")],
    frame: Annotated[str | None, Field(
        description="prograde/retrograde: 'orbital' or 'surface' (required). vector: 'surface' (x up, y "
                    "north, z east), 'orbital' (kRPC orbital frame: x anti-radial, y prograde, z normal), "
                    "'body' (rotates with the body) or 'inertial' (body non-rotating).")] = None,
    pitch_deg: Annotated[float | None, Field(description="hold: pitch above the horizon, degrees.")] = None,
    heading_deg: Annotated[float | None, Field(description="hold: compass heading, degrees (90 = east).")] = None,
    roll_deg: Annotated[float | None, Field(
        description="Autopilot modes: roll angle to hold, degrees; None leaves roll free.")] = None,
    vector: Annotated[list[float] | None, Field(description="vector: [x, y, z] direction in `frame`.")] = None,
    wait_aligned_deg: Annotated[float | None, Field(
        description="If given, let game time run until the pointing error stays below this many "
                    "degrees (needs timeout_s). Choose from how precise the next action must be.")] = None,
    timeout_s: Annotated[float | None, Field(
        description="Game seconds to wait for alignment at most. The result's authority.turn_90deg_min_s "
                    "estimates the fastest possible 90 degree turn for this vessel.")] = None,
) -> dict:
    """Point the vessel and keep pointing until told otherwise (kRPC autopilot or SAS).

    Autopilot modes pick a reference frame in which the direction is constant, so the hold persists
    between calls (it is released if the ASTRA server's connection closes). `target`/`anti_target`
    aim at the target's current direction and do not follow it; use 'sas:target' or a fly_* reflex
    for tracking. Without wait_aligned_deg no game time passes: the result shows the current error
    and the vessel's turning authority. With it, time runs until aligned or timeout_s, then pauses.
    """
    k = ksp()
    v = k.vessel()
    sc, ap, c = k.sc, v.auto_pilot, v.control
    m = mode.strip().lower()
    if wait_aligned_deg is not None and (timeout_s is None or timeout_s <= 0):
        raise AstraError("wait_aligned_deg needs a positive timeout_s (game seconds)",
                         "estimate it from authority.turn_90deg_min_s of a call without waiting")
    out: dict[str, Any] = {"mode": m}
    error_fn = None
    no_target = AstraError(f"mode {m!r} has no pointing target to wait for", "omit wait_aligned_deg")
    if m == "off":
        if wait_aligned_deg is not None:
            raise no_target
        ap.disengage()
        c.sas = False
        out["state"] = "autopilot and SAS released"
    elif m.startswith("sas:"):
        member = _sas_member(sc, m[4:])
        name = enum_name(member)
        if _safe(lambda: _sas_direction(k, v, name)) is not None:
            def error_fn():  # the SAS target moves (prograde turns with the orbit): re-read it
                rf, want = _sas_direction(k, v, name)
                return _angle_deg(v.direction(rf), want)
        elif wait_aligned_deg is not None:
            raise no_target
        ap.disengage()
        c.sas = True
        try:  # always set: SAS turned on keeps whatever mode it held before
            c.sas_mode = member
        except Exception as exc:  # noqa: BLE001
            raise AstraError(f"SAS refused mode {name}: {str(exc).splitlines()[0]}",
                             "this control point lacks that SAS mode; use the autopilot mode with the "
                             "same direction (e.g. mode='retrograde', frame='orbital')") from exc
        out["state"] = f"SAS on, mode {enum_name(c.sas_mode)}"
    else:
        aim = _aim(k, v, m, frame, pitch_deg, heading_deg, vector)
        c.sas = False
        ap.reference_frame = aim["frame"]
        if "pitch_heading" in aim:
            ap.target_pitch_and_heading(*aim["pitch_heading"])
        else:
            ap.target_direction = aim["direction"]
        ap.target_roll = float(roll_deg) if roll_deg is not None else _NAN
        ap.engage()
        out.update(state=f"autopilot: {aim['label']}", tracks=aim["tracks"])
        error_fn = lambda: ap.error  # noqa: E731
        if m in ("prograde", "retrograde") and frame == "surface":
            speed = _safe(lambda: v.flight(v.orbit.body.reference_frame).speed, 0.0)
            if speed is not None and speed < 1.0:
                out["warning"] = "surface speed is ~0: surface prograde/retrograde is undefined here"
    out["authority"] = _authority(v)
    if wait_aligned_deg is not None:
        out.update(_wait_aligned(k, error_fn, wait_aligned_deg, float(timeout_s)))
    elif error_fn is not None:
        out["error_deg"] = _safe(error_fn)
    warns = _control_warnings(v, sc)
    if warns:
        out.setdefault("warnings", []).extend(warns)
    return out


# ---------------------------------------------------------------------------------------------
# Parts

PART_ACTIONS = ("decouple", "jettison", "deploy", "retract", "arm", "activate", "shutdown", "thrust_limit",
                "crossfeed", "control_from", "deploy_chute", "cut_chute", "chute_altitude", "chute_min_pressure",
                "release", "undock")
_DEPLOYABLE = ("leg", "wheel", "solar_panel", "antenna", "radiator")
_SWITCHABLE = (("engine", "active"), ("rcs", "enabled"), ("reaction_wheel", "active"), ("light", "active"))


def _first_module(part: Any, kinds: Iterable[str]) -> tuple[str, Any] | tuple[None, None]:
    for kind in kinds:
        m = _safe(lambda kind=kind: part_module(part, kind))
        if m is not None:
            return kind, m
    return None, None


def _modules_on(part: Any) -> list[str]:
    return [kind for kind in PART_KINDS[:-2] if _safe(lambda kind=kind: part_module(part, kind)) is not None]


def _deploy(p: Any, idx: int, on: bool) -> str:
    """Deploy or retract whatever deployable module the part has; returns the module kind."""
    kind, m = _first_module(p, _DEPLOYABLE)
    if m is not None:
        if not m.deployable:
            raise AstraError(f"{p.name} is fixed and cannot {'deploy' if on else 'retract'}")
        m.deployed = on
        return kind
    kind, m = _first_module(p, ("cargo_bay", "intake"))
    if m is not None:
        m.open = on
        return kind
    port = _safe(lambda: p.docking_port)
    if port is not None and port.has_shield:
        port.shielded = not on
        return "docking_port"
    chute = _safe(lambda: p.parachute)
    if on and chute is not None:
        chute.deploy()
        return "parachute"
    raise AstraError(f"{p.name} (idx {idx}) has nothing to {'deploy' if on else 'retract'}",
                     f"its modules: {', '.join(_modules_on(p)) or 'none'}")


def _set_crossfeed(part: Any, on: bool) -> None:
    """Toggle crossfeed through the part's ModuleToggleCrossfeed (kRPC's Part.crossfeed is read-only)."""
    modules = [m for m in part.modules if m.name == "ModuleToggleCrossfeed"]
    if not modules:
        raise AstraError(f"{part.name} has no crossfeed toggle", "crossfeed can be toggled on decouplers "
                         "and parts that carry ModuleToggleCrossfeed")
    m = modules[0]
    event, action = ("EnableXFeed", "EnableAction") if on else ("DisableXFeed", "DisableAction")
    if m.has_event_with_id(event):
        m.trigger_event_by_id(event)
    elif m.has_action_with_id(action):
        m.set_action_by_id(action, True)
    else:
        raise AstraError(f"{part.name} does not allow changing crossfeed now",
                         f"available events: {m.events_by_id}; actions: {m.actions_by_id}")


@tool("control")
def control_part(
    action: Annotated[Literal[PART_ACTIONS], Field(  # type: ignore[valid-type]
        description="decouple (decoupler/separator) | jettison (fairing) | deploy / retract (legs, wheels, "
                    "solar panels, antennas, radiators, cargo bays, intakes, docking-port shields; deploy "
                    "also opens a parachute) | arm (parachute: opens by itself when safe) | activate / "
                    "shutdown (engine, RCS, reaction wheel, light) | thrust_limit (engine, value = percent "
                    "0-100) | crossfeed (value true/false) | control_from (make this part the control "
                    "point) | deploy_chute | cut_chute | chute_altitude (value = full-deploy altitude, m) | "
                    "chute_min_pressure (value = semi-deploy pressure, atm) | release (launch clamp) | "
                    "undock (docking port).")],
    part: Annotated[int | str, Field(description="Part idx from vessel_parts, or a unique internal part name / tag.")],
    value: Annotated[float | bool | None, Field(
        description="Needed by thrust_limit, crossfeed, chute_altitude and chute_min_pressure.")] = None,
) -> dict:
    """Operate one specific part: separate, deploy, arm, switch, limit thrust, set the control point.

    Reads the part's state back afterwards. Separations (decouple, undock, release) warn when no
    engine would remain on the controlling part's side, run game time for the few physics frames
    the split takes (`game_s_elapsed`), pause again and report part counts; re-list parts
    afterwards because idx numbering changes. Lighting an engine directly does not advance KSP's
    stage counter: prefer control_stage for normal staging.
    """
    k = ksp()
    v = k.vessel()
    parts = v.parts.all
    p, idx = resolve_part(v, part, parts)
    out: dict[str, Any] = {"action": action, "idx": idx, "part": p.name, "warnings": []}
    needs_value = {"thrust_limit", "crossfeed", "chute_altitude", "chute_min_pressure"}
    if action in needs_value and value is None:
        raise AstraError(f"action {action!r} needs a value")
    count_before = len(parts)
    separate = None  # a separation runs with game time (see run_separation)

    def need(kind: str) -> Any:
        m = part_module(p, kind)
        if m is None:
            raise AstraError(f"{p.name} (idx {idx}) has no {kind} module",
                             f"its modules: {', '.join(_modules_on(p)) or 'none'}")
        return m

    if action == "decouple":
        d = need("decoupler")
        if d.decoupled:
            raise AstraError(f"decoupler idx {idx} has already fired")
        warns, plan = separation_warnings(v, [idx])
        out["warnings"] += warns
        out["plan"] = plan
        separate = d.decouple
    elif action == "undock":
        port = need("docking_port")
        if enum_name(port.state) != "docked":
            raise AstraError(f"docking port idx {idx} is not docked ({enum_name(port.state)})")
        separate = port.undock
    elif action == "release":
        separate = need("launch_clamp").release
    elif action == "jettison":
        f = need("fairing")
        if f.jettisoned:
            raise AstraError(f"fairing idx {idx} is already jettisoned")
        f.jettison()
    elif action in ("deploy", "retract"):
        out["module"] = _deploy(p, idx, action == "deploy")
    elif action == "arm":
        need("parachute").arm()
    elif action == "deploy_chute":
        need("parachute").deploy()
    elif action == "cut_chute":
        need("parachute").cut()
    elif action == "chute_altitude":
        need("parachute").deploy_altitude = float(value)
    elif action == "chute_min_pressure":
        need("parachute").deploy_min_pressure = float(value)
    elif action in ("activate", "shutdown"):
        on = action == "activate"
        for kind, attr in _SWITCHABLE:
            m = _safe(lambda kind=kind: part_module(p, kind))
            if m is None:
                continue
            if kind == "engine" and not on and not m.can_shutdown:
                raise AstraError(f"{p.name} cannot be shut down (solid motors burn out)")
            setattr(m, attr, on)
            out["module"] = kind
            if kind == "engine" and on:
                out["warnings"].append("engine lit directly: KSP's stage counter did not move; a later "
                                       "control_stage fires the stage as numbered, not what is burning")
            break
        else:
            raise AstraError(f"{p.name} (idx {idx}) has nothing to {action}",
                             f"its modules: {', '.join(_modules_on(p)) or 'none'}")
    elif action == "thrust_limit":
        pct = float(value)
        if not 0.0 <= pct <= 100.0:
            raise AstraError("thrust_limit value is a percentage 0-100")
        need("engine").thrust_limit = pct / 100.0
        if 0.0 < pct < 1.0:
            out["warnings"].append(f"value is a percentage: the limit is now {pct}% of full thrust")
    elif action == "crossfeed":
        _set_crossfeed(p, bool(value))
        out["crossfeed"] = p.crossfeed
    elif action == "control_from":
        v.parts.controlling = p
        now = v.parts.controlling
        out["controlling"] = now is not None and now == p
    if separate is not None:
        _, game_s = run_separation(k, separate, count_before)
        reset_links()
        v2 = k.vessel()
        out.update(parts_before=count_before, parts_after=len(v2.parts.all), active_vessel=v2.name,
                   game_s_elapsed=game_s, paused=k.paused)
        if out["parts_after"] >= count_before:
            out["note"] = "no parts left within the wait; check vessel_parts and camera_look"
        return out
    kind = out.get("module") or {"decouple": "decoupler", "jettison": "fairing", "arm": "parachute",
                                 "deploy_chute": "parachute", "cut_chute": "parachute",
                                 "chute_altitude": "parachute", "chute_min_pressure": "parachute",
                                 "thrust_limit": "engine"}.get(action)
    if kind:
        out["state"] = _safe(lambda: module_state(p, kind))
    return out


# ---------------------------------------------------------------------------------------------
# Action groups

_NAMED_GROUPS = ("gear", "lights", "brakes", "rcs", "sas", "abort")


@tool("control")
def control_action_group(
    group: Annotated[int | str, Field(
        description="Custom action group 1-10 as numbered in KSP (0 also means 10), or one of: "
                    + ", ".join(_NAMED_GROUPS) + ".")],
    state: Annotated[Literal["on", "off", "toggle"], Field(description="Set the group on or off, or toggle it.")],
) -> dict:
    """Fire an action group (custom 1-10 or gear/lights/brakes/rcs/sas/abort) and report its state.

    Custom groups do whatever the craft designer bound to them; the result reports the group state
    before and after, not what the actions did (check telemetry or vessel_parts).
    """
    k = ksp()
    v = k.vessel()
    c = v.control
    if isinstance(group, str) and group.strip().lower() in _NAMED_GROUPS:
        name = group.strip().lower()
        before = getattr(c, name)
        setattr(c, name, (not before) if state == "toggle" else state == "on")
        return {"group": name, "before": before, "after": getattr(c, name)}
    try:
        n = int(group)
    except (TypeError, ValueError):
        raise AstraError(f"unknown action group {group!r}", "1-10 or one of " + ", ".join(_NAMED_GROUPS)) from None
    if not 0 <= n <= 10:
        raise AstraError(f"action group {n} is out of range 1-10")
    g = n % 10  # kRPC numbers custom group 10 as 0
    before = c.get_action_group(g)
    if state == "toggle":
        c.toggle_action_group(g)
    else:
        c.set_action_group(g, state == "on")
    return {"group": n or 10, "before": before, "after": c.get_action_group(g)}


# ---------------------------------------------------------------------------------------------
# Maneuver nodes


def burn_estimate(v: Any, dv: float) -> dict:
    """Burn time for `dv` with the engines active right now (rocket equation, constant thrust)."""
    thrust, isp, mass = v.available_thrust, v.specific_impulse, v.mass
    if thrust <= 0 or isp <= 0:
        return {"burn_time_s": None, "note": "no active engine: burn time depends on which stage burns "
                                             "(see vessel_stages)"}
    ve = isp * G0
    return {"burn_time_s": mass * ve / thrust * (1.0 - math.exp(-dv / ve)), "thrust_kn": thrust / 1e3,
            "isp_s": isp, "mass_t": mass / 1e3}


def node_info(node: Any, index: int, ut: float) -> dict:
    o = node.orbit
    return {
        "index": index, "ut": node.ut, "time_to_s": node.time_to,
        "prograde_mps": node.prograde, "normal_mps": node.normal, "radial_mps": node.radial,
        "delta_v_mps": node.delta_v, "remaining_delta_v_mps": node.remaining_delta_v,
        "orbit_after": {"body": o.body.name, "periapsis_alt_m": o.periapsis_altitude,
                        "apoapsis_alt_m": finite(o.apoapsis_altitude), "eccentricity": o.eccentricity,
                        "inclination_deg": math.degrees(o.inclination), "period_s": finite(o.period)},
        "patches": patch_chain(o, ut),
    }


@tool("control")
def node_create(
    ut: Annotated[float, Field(description="Universal time of the burn, s (e.g. telemetry ut + time_to_apoapsis_s).")],
    prograde_mps: Annotated[float, Field(description="Δv along prograde, m/s (negative = retrograde).")],
    normal_mps: Annotated[float, Field(description="Δv along the orbit normal, m/s (negative = antinormal).")],
    radial_mps: Annotated[float, Field(description="Δv radial-out, m/s (negative = radial-in).")],
) -> dict:
    """Place a maneuver node and report KSP's predicted result (orbit after, encounters/escapes).

    The node is kept (node_delete removes it). `patches` walks KSP's patched conics after the burn,
    which is how to check an encounter. `burn` estimates the burn time with the currently active
    engines. Use compute_* tools to choose the numbers; iterate by deleting and re-creating.
    """
    k = ksp()
    v = k.vessel()
    now = k.sc.ut
    if ut <= now:
        raise AstraError(f"ut {ut:.1f} is not in the future (now {now:.1f})", "add the time until the burn point to now")
    node = v.control.add_node(ut, prograde=prograde_mps, normal=normal_mps, radial=radial_mps)
    nodes = v.control.nodes
    index = next((i for i, n in enumerate(nodes) if n == node), len(nodes) - 1)
    out = node_info(node, index, now)
    out["burn"] = burn_estimate(v, node.delta_v)
    out["node_count"] = len(nodes)
    return out


@tool("control")
def node_list() -> dict:
    """All maneuver nodes in time order, with Δv, time to go, and the predicted orbit after each."""
    k = ksp()
    v = k.vessel()
    now = k.sc.ut
    nodes = v.control.nodes
    return {"ut": now, "nodes": [node_info(n, i, now) for i, n in enumerate(nodes)]}


@tool("control")
def node_delete(
    index: Annotated[int | Literal["all"], Field(description="Node index from node_list (0 = next), or 'all'.")],
) -> dict:
    """Delete one maneuver node, or all of them."""
    v = ksp().vessel()
    c = v.control
    nodes = c.nodes
    if index == "all":
        c.remove_nodes()
        return {"removed": len(nodes), "remaining": len(c.nodes)}
    if not 0 <= int(index) < len(nodes):
        raise AstraError(f"no node {index} (there are {len(nodes)})", "node_list shows the indices")
    nodes[int(index)].remove()
    return {"removed": 1, "remaining": len(c.nodes)}


# ---------------------------------------------------------------------------------------------
# Targets


@tool("control")
def target_set(
    name: Annotated[str | None, Field(description="Body name, vessel name (tolerant) or '#id'; None clears the target.")],
    port: Annotated[int | str | None, Field(
        description="Target a docking port on that vessel: its idx from target_info's target_docking_ports "
                    "(or a unique part name/tag). The vessel must be loaded (within ~2 km).")] = None,
) -> dict:
    """Set (or clear) the navigation target: a body, a vessel, or a docking port on a vessel.

    MechJeb and SAS target modes use this target. Bodies match exactly (case-insensitive) before
    vessels are considered.
    """
    k = ksp()
    v = k.vessel()
    sc = k.sc
    if name is None:
        sc.clear_target()
        return {"target": None}
    bodies = sc.bodies
    body_name = next((b for b in bodies if b.casefold() == name.strip().casefold()), None)
    if body_name is not None and port is None:
        sc.target_body = bodies[body_name]
        kind, label = "body", body_name
    else:
        tv = resolve_vessel(name, exclude=v)
        if port is None:
            sc.target_vessel = tv
            kind, label = "vessel", tv.name
        else:
            docking = tv.parts.docking_ports
            if not docking:
                raise AstraError(f"{tv.name} has no docking ports loaded",
                                 "it must be within physics range (~2.3 km) and carry docking ports")
            p, idx = resolve_part(tv, port)
            match = next((d for d in docking if d.part == p), None)
            if match is None:
                raise AstraError(f"part idx {idx} ({p.name}) on {tv.name} is not a docking port",
                                 "target_info lists target_docking_ports with idx")
            sc.target_docking_port = match
            kind, label = "docking_port", f"{p.name} (idx {idx}) on {tv.name}"
    obj, got = current_target(sc)
    if obj is None:
        raise AstraError("KSP did not accept the target", "a vessel cannot target itself; try again")
    nr = v.orbit.body.non_rotating_reference_frame
    distance = math.dist(obj.position(nr), v.position(nr))
    return {"target": label, "kind": got, "distance_m": distance}
