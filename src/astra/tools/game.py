"""Game director: scenes, saves and checkpoints, launch, vessel switching, recovery, pause, log.

Scene changes invalidate every kRPC object and stream, so each tool here re-acquires what it
reports after the change and resets the telemetry probe. Tools that leave the game running in
flight end with `hold_for_deliberation()`.
"""

import json
import math
import re
import socket
import time
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from pydantic import Field

from astra import journal
from astra import telemetry as tm
from astra.config import CONFIG
from astra.errors import AstraError, BridgeError, NotConnected, WrongScene
from astra.ksp import ksp
from astra.registry import tool
from astra.tools.observe import bridge_vessel_ref, enum_name, pick_name, resolve_vessel, vessel_id

_CHECKPOINT_PREFIX = "astra_"


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 — status probes never throw
        return default


def wait_for(pred: Callable[[], Any], timeout_s: float, interval_s: float = 0.5) -> Any:
    """Poll `pred` until it returns something truthy (exceptions count as 'not yet')."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            value = pred()
            if value:
                return value
        except Exception:  # noqa: BLE001 — scene transitions make RPCs fail for a while
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval_s)


def reset_links() -> None:
    """Forget streams bound to the previous scene/vessel after a scene change or vessel switch."""
    tm.probe().invalidate()
    drop = getattr(ksp(), "drop_streams", None)
    if drop is not None:
        drop()


def stop_warp(sc: Any) -> bool:
    """Drop out of time warp. Returns True if warp was active."""
    active = bool(_safe(lambda: sc.rails_warp_factor, 0) or _safe(lambda: sc.physics_warp_factor, 0))
    if active:
        sc.rails_warp_factor = 0
        sc.physics_warp_factor = 0
    return active


def vessel_brief(v: Any) -> dict:
    return {"name": v.name, "id": vessel_id(v), "type": enum_name(v.type),
            "situation": enum_name(v.situation), "body": v.orbit.body.name,
            "parts": len(v.parts.all), "mass_t": v.mass / 1000.0,
            "crew": [c.name for c in v.crew], "stage": v.control.current_stage}


def _port_open(host: str, port: int, timeout_s: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _krpc_up() -> bool:
    return _port_open(CONFIG.krpc_host, CONFIG.krpc_rpc_port)


def _await_scene(scenes: tuple[str, ...], timeout_s: float, need_vessel: bool = False) -> str | None:
    """Wait until kRPC answers from one of `scenes` (and, if asked, has an active vessel)."""
    def ready():
        if not _krpc_up():
            return None
        k = ksp()
        s = k.scene()
        if s not in scenes:
            return None
        if need_vessel and s == "flight" and k.sc.active_vessel is None:
            return None
        return s
    return wait_for(ready, timeout_s)


def _mission_info() -> dict | None:
    d = journal.active_mission()
    if d is None:
        return None
    meta = _safe(lambda: json.loads((d / "mission.json").read_text(encoding="utf-8")), {})
    return {"folder": d.name, "goal": meta.get("goal"), "started": meta.get("started")}


# ---------------------------------------------------------------------------------------------


@tool("game", needs_game=False)
def game_status() -> dict:
    """Is the game up, and where is it? Scene, pause, UT, save, active vessel, active mission.

    Never fails: when KSP, kRPC or the bridge are down it says so and suggests the next step.
    `krpc_up` means the kRPC server answers (only once a save is loaded); `bridge.scene` is KSP's
    own scene name (MAINMENU, SPACECENTER, FLIGHT, EDITOR...) and works from the main menu.
    """
    out: dict[str, Any] = {"krpc_up": False, "bridge_up": False, "mission": _mission_info()}
    k = ksp()
    if k.bridge.up():
        state = _safe(lambda: k.bridge.get("/state", timeout=5.0))
        out["bridge_up"] = state is not None
        if state:
            out["bridge"] = {key: state.get(key) for key in
                             ("scene", "saveFolder", "activeVessel", "activeVesselSituation", "queueDepth",
                              "lastError") if key in state}
            out["save"] = state.get("saveFolder")
    if _krpc_up():
        try:
            sc = k.sc
            out["krpc_up"] = True
            scene = k.scene()
            out.update(scene=scene, paused=_safe(lambda: k.paused), ut=_safe(lambda: sc.ut),
                       game_mode=_safe(lambda: enum_name(sc.game_mode)))
            out["launchable_craft"] = {d: _safe(lambda d=d: len(sc.launchable_vessels(d))) for d in ("VAB", "SPH")}
            if scene == "flight":
                v = _safe(lambda: sc.active_vessel)
                out["active_vessel"] = _safe(lambda: vessel_brief(v)) if v is not None else None
                out["warp"] = {"rails_factor": _safe(lambda: sc.rails_warp_factor),
                               "physics_factor": _safe(lambda: sc.physics_warp_factor)}
        except Exception as exc:  # noqa: BLE001 — report, never raise
            out["krpc_error"] = str(exc)
    bridge_scene = (out.get("bridge") or {}).get("scene")
    if not out["krpc_up"] and not out["bridge_up"]:
        out["next_step"] = "KSP is not running (or not loaded yet): start it with `astra up`."
    elif not out["krpc_up"] and bridge_scene == "MAINMENU":
        out["next_step"] = "At the main menu: game_load_save(save, scene) loads a save."
    elif not out["krpc_up"]:
        out["next_step"] = "kRPC does not answer yet: KSP may still be loading; call game_status again."
    return out


@tool("game")
def game_load_save(
    save: Annotated[str, Field(description="Save folder name under KSP's saves/ directory (e.g. 'astra').")],
    scene: Annotated[Literal["space_center", "flight"], Field(
        description="Where to enter: 'space_center', or 'flight' to resume the save's active vessel.")],
) -> dict:
    """Load a save game (works from the main menu) and wait until kRPC answers in the new scene.

    Goes through the bridge (kRPC cannot switch save folders). Loading takes tens of seconds.
    Returns the scene, UT and active vessel. In flight the game is then paused for deliberation.
    """
    if not save or save.strip() != save or any(c in save for c in '/\\:') or ".." in save:
        raise AstraError(f"bad save folder name {save!r}", "give the folder name only, as game_status 'save' shows it")
    folder = CONFIG.saves_dir / save
    if not (folder / "persistent.sfs").exists():
        known = sorted(p.parent.name for p in CONFIG.saves_dir.glob("*/persistent.sfs"))
        raise AstraError(f"no save folder {save!r} with a persistent.sfs", f"saves: {', '.join(known) or 'none'}")
    k = ksp()
    k.bridge.post("/load-save", {"saveName": save, "scene": "flight" if scene == "flight" else "spacecenter"},
                  timeout=120.0)
    got = _await_scene((scene,), 240.0, need_vessel=scene == "flight")
    if got is None:
        raise AstraError(f"the game did not reach {scene} after loading {save!r}",
                         "call game_status; check game_log for load errors")
    reset_links()
    out: dict[str, Any] = {"save": save, "scene": got, "ut": k.sc.ut}
    if got == "flight":
        out["active_vessel"] = vessel_brief(k.vessel())
        out["paused"] = k.hold_for_deliberation()
    return out


def _checkpoint_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,48}", name or ""):
        raise AstraError(f"bad checkpoint name {name!r}",
                         "use letters, digits, '-' and '_' (e.g. 'before_capture')")
    return _CHECKPOINT_PREFIX + name


def _checkpoint_file(fname: str) -> Path | None:
    """Where checkpoint `fname` lives in the save being played. The bridge names the save folder;
    without it, a file found in exactly one save folder is accepted."""
    k = ksp()
    folder = _safe(lambda: k.bridge.get("/state", timeout=5.0).get("saveFolder")) if k.bridge.up() else None
    if folder:
        return CONFIG.saves_dir / folder / f"{fname}.sfs"
    found = list(CONFIG.saves_dir.glob(f"*/{fname}.sfs"))
    if len(found) > 1:
        raise AstraError(f"{fname}.sfs exists in several save folders and the bridge is down",
                         "start the bridge (game_status) so the active save is known")
    return found[0] if found else None


def list_checkpoints(folder: Path | None) -> list[dict]:
    if folder is None or not folder.is_dir():
        return []
    rows = []
    for p in sorted(folder.glob(f"{_CHECKPOINT_PREFIX}*.sfs"), key=lambda p: p.stat().st_mtime):
        rows.append({"name": p.stem[len(_CHECKPOINT_PREFIX):], "ut": saved_ut(p),
                     "saved": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))})
    return rows


def saved_ut(path: Path) -> float | None:
    """The universal time stored in a .sfs file (FLIGHTSTATE { UT = ... })."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    i = text.find("FLIGHTSTATE")
    m = re.search(r"^\s*UT\s*=\s*([-+0-9.eE]+)", text[i:] if i >= 0 else text, re.MULTILINE)
    return float(m.group(1)) if m else None


@tool("game")
def game_checkpoint(
    name: Annotated[str, Field(description="Checkpoint label: letters, digits, '-', '_' (e.g. 'lko_before_tmi'). "
                                           "Reusing a name overwrites that checkpoint.")],
) -> dict:
    """Save a named checkpoint of the whole game state (file saves/<save>/astra_<name>.sfs).

    Works in flight and at the space center (not in the editor). Make one before anything risky;
    game_restore(name) returns to it. The result lists every checkpoint in this save.
    """
    fname = _checkpoint_name(name)
    k = ksp()
    scene = k.scene()
    if scene.startswith("editor"):
        raise WrongScene("the editor state cannot be checkpointed", "go to the space center or fly first")
    if scene == "flight" and k.paused:
        # Just after a vessel switch (EVA, boarding, undocking) KSP has not finished making the new
        # vessel active until a physics frame runs; a save taken then can restore the wrong vessel.
        ut0 = k.sc.ut
        try:
            with k.running():
                wait_for(lambda: k.sc.ut - ut0 >= 0.2, 5.0, 0.05)
        finally:
            k.hold_for_deliberation()
    t0 = time.time()
    k.sc.save(fname)

    def written():
        fresh = [p for p in CONFIG.saves_dir.glob(f"*/{fname}.sfs") if p.stat().st_mtime >= t0 - 2]
        return max(fresh, key=lambda p: p.stat().st_mtime) if fresh else None
    path = wait_for(written, 10.0, 0.25)
    if path is None:
        raise AstraError(f"KSP did not write {fname}.sfs", "check game_log; try again from a stable situation")
    out = {"checkpoint": name, "file": str(path), "scene": scene, "ut": k.sc.ut,
           "checkpoints": list_checkpoints(path.parent)}
    if scene == "flight":
        out["active_vessel"] = _safe(lambda: k.sc.active_vessel.name)
    # The .sfs does not record the scene, and kRPC's load always enters flight; remember where we were.
    _safe(lambda: path.with_suffix(".astra.json").write_text(
        json.dumps({"scene": scene, "ut": out["ut"], "active_vessel": out.get("active_vessel")}), encoding="utf-8"))
    return out


def _checkpoint_scene(path: Path) -> str:
    """'flight' or 'space_center': from game_checkpoint's sidecar, else inferred from the save
    (no vessels means there was nothing to fly)."""
    meta = _safe(lambda: json.loads(path.with_suffix(".astra.json").read_text(encoding="utf-8")), None)
    if isinstance(meta, dict) and meta.get("scene"):
        return "flight" if meta["scene"] == "flight" else "space_center"
    text = _safe(lambda: path.read_text(encoding="utf-8", errors="replace"), "")
    return "flight" if "\n\t\tVESSEL\n" in text else "space_center"


@tool("game")
def game_restore(
    name: Annotated[str, Field(description="Checkpoint label given to game_checkpoint.")],
) -> dict:
    """Load a checkpoint made with game_checkpoint, replacing the current game state.

    Waits for the scene to load, then reports scene, UT and the active vessel; in flight the game
    is paused for deliberation. Everything since the checkpoint is lost.
    """
    fname = _checkpoint_name(name)
    path = _checkpoint_file(fname)
    if path is None or not path.exists():
        names = [c["name"] for c in list_checkpoints(path.parent if path else None)]
        raise AstraError(f"no checkpoint {name!r} in this save", f"checkpoints: {', '.join(names) or 'none'}")
    target_ut = saved_ut(path)
    k = ksp()
    ut_before = _safe(lambda: k.sc.ut)
    stop_warp(k.sc)
    t0 = time.monotonic()
    if _checkpoint_scene(path) == "flight" or not k.bridge.up():
        k.sc.load(fname)
    else:
        # kRPC's load would enter flight on an arbitrary vessel; the bridge loads into the space center.
        k.bridge.post("/load-save", {"saveFolder": path.parent.name, "saveFile": fname, "scene": "spacecenter"},
                      timeout=60)
        k.drop_streams()

    def loaded():
        scene = _await_scene(("flight", "space_center", "tracking_station"), 2.0)
        if scene is None:
            return None
        ut = k.sc.ut
        if target_ut is not None:
            elapsed = time.monotonic() - t0
            if abs(ut - target_ut) > 5.0 + 2.0 * elapsed:
                return None
            if ut_before is not None and abs(ut_before - target_ut) <= 5.0 + 2.0 * elapsed and elapsed < 3.0:
                return None  # cannot tell old from new state by UT yet: give the load time
        if scene == "flight" and k.sc.active_vessel is None:
            return None
        return scene
    scene = wait_for(loaded, 180.0)
    if scene is None:
        raise AstraError(f"checkpoint {name!r} did not finish loading", "call game_status and game_log")
    reset_links()
    out: dict[str, Any] = {"restored": name, "scene": scene, "ut": k.sc.ut, "checkpoint_ut": target_ut}
    if scene == "flight":
        # A just-loaded vessel is packed: engines have no thrust transforms and parts no physics
        # until a few frames have run. Let them run (no controls change) before handing over.
        ut0 = k.sc.ut
        try:
            with k.running():
                wait_for(lambda: k.sc.ut - ut0 >= 1.0, 10.0, 0.1)
        finally:
            k.hold_for_deliberation()
        out["settle_game_s"] = k.sc.ut - ut0
        out["active_vessel"] = vessel_brief(k.vessel())
        out["paused"] = k.hold_for_deliberation()
    return out


@tool("game")
def game_revert(
    to: Annotated[Literal["launch", "editor"], Field(
        description="'launch' puts the same craft back on the pad/runway as it was at launch. "
                    "'editor' discards the flight and opens the craft in the VAB/SPH it was built in.")],
) -> dict:
    """Revert the current flight to its launch or to the editor (the flight is discarded).

    Only possible while KSP still allows a revert (not after scene changes or loading a save);
    otherwise restore a checkpoint. After a revert to launch the game is paused for deliberation.
    Reverting to the editor goes through the bridge (kRPC cannot).
    """
    k = ksp()
    k.require_flight()
    if to == "editor":
        try:
            reply = k.bridge.post("/revert", {"to": "editor"}, timeout=60.0)
        except BridgeError as exc:
            if "Unknown route" in exc.message:
                raise AstraError("this bridge build cannot revert to the editor",
                                 "game_revert('launch') retries the same craft; to change the craft: "
                                 "game_space_center, design_build the revision, then game_launch") from exc
            raise
        got = _await_scene(("editor_vab", "editor_sph"), 120.0)
        if got is None:
            raise AstraError("the revert to the editor did not complete", "call game_status and game_log")
        reset_links()
        return {"reverted_to": "editor", "scene": got, "facility": reply.get("facility")}
    sc = k.sc
    if not sc.can_revert_to_launch():
        raise AstraError("KSP does not allow reverting this flight",
                         "game_restore a checkpoint, or game_space_center and launch again")
    stop_warp(sc)
    ut_before = sc.ut
    t0 = time.monotonic()
    sc.revert_to_launch()

    def reverted():
        if k.scene() != "flight" or k.sc.active_vessel is None:
            return False
        if k.sc.ut < ut_before - 0.5:  # the clock went back to launch time
            return True
        # reverting a flight that never ran (paused on the pad) leaves the clock where it was
        return time.monotonic() - t0 > 5.0 and enum_name(k.sc.active_vessel.situation) == "pre_launch"
    if wait_for(reverted, 120.0) is None:
        raise AstraError("the revert did not complete", "call game_status")
    reset_links()
    return {"reverted_to": "launch", "ut": sc.ut, "active_vessel": vessel_brief(k.vessel()),
            "paused": k.hold_for_deliberation()}


@tool("game")
def game_launch(
    craft: Annotated[str, Field(description="Craft name as saved in this save's Ships/VAB or Ships/SPH "
                                            "(craft_list shows them; tolerant matching).")],
    site: Annotated[str, Field(description="Launch site: 'LaunchPad' or 'Runway', or another site KSP "
                                           "offers (an unknown name lists them).")],
    crew: Annotated[list[str], Field(description="Kerbal names to seat, e.g. ['Jebediah Kerman']. [] lets KSP "
                                                 "assign its default crew (probes need none). Use "
                                                 "crew_roster to see who is available.")],
) -> dict:
    """Launch a craft file onto a launch site with the crew you choose, and verify it on the pad.

    Uses KSP's own pre-flight checks and recovers any vessel sitting on the site first. Launch from
    the space center (or editor); from flight, leave with game_space_center first. The result gives
    the vessel's name (KSP may append a localized suffix), situation, parts, mass, crew aboard,
    and current stage; `crew_missing` lists requested kerbals who are not aboard. The game is
    paused on the pad for deliberation.
    """
    k = ksp()
    scene = k.scene()
    if scene == "flight":
        raise WrongScene("cannot launch from the flight scene",
                         "game_space_center first (it keeps this flight), or game_revert('launch') to retry it")
    sc = k.sc
    listed = [(d, n) for d in ("VAB", "SPH") for n in sc.launchable_vessels(d)]
    if not listed:
        raise AstraError("this save has no craft files to launch", "design_build a craft first")
    directory, name = listed[pick_name([n for _, n in listed], craft, "craft",
                                       lambda i: f"{listed[i][1]} ({listed[i][0]})")]
    sites = [s.name for s in sc.launch_sites]
    site_name = sites[pick_name(sites, site, "launch site")]
    for kerbal in crew:
        member = sc.get_kerbal(kerbal)
        if member is None:
            raise AstraError(f"no kerbal named {kerbal!r}", "crew_roster lists the roster")
        status = enum_name(member.roster_status)
        if status != "available":
            raise AstraError(f"{kerbal} is {status}, not available", "choose an available kerbal (crew_roster)")
    try:
        sc.launch_vessel(directory, name, site_name, True, list(crew), "")
    except Exception as exc:  # noqa: BLE001 — KSP's pre-flight refusals arrive as RPC errors
        raise AstraError(f"KSP refused the launch: {exc}",
                         "pre-flight checks need a control source (crew or probe core), existing "
                         "part names, and crew that fit; see game_log for load errors") from exc
    got = _await_scene(("flight",), 180.0, need_vessel=True)
    if got is None:
        raise AstraError("the launch did not reach the flight scene", "call game_status and game_log")
    reset_links()
    v = k.vessel()
    info = vessel_brief(v)
    aboard = set(info["crew"])
    out: dict[str, Any] = {"craft": name, "directory": directory, "site": site_name, "vessel": info,
                           "crew_capacity": v.crew_capacity}
    missing = [c for c in crew if c not in aboard]
    if missing:
        out["crew_missing"] = missing
    if info["situation"] not in ("pre_launch", "landed"):
        out["warning"] = f"vessel situation is {info['situation']} right after launch"
    out["paused"] = k.hold_for_deliberation()
    return out


@tool("game")
def game_list_vessels(
    kinds: Annotated[list[str] | None, Field(
        description="Vessel types to include: ship, probe, lander, relay, station, base, rover, plane, "
                    "eva, flag, debris, space_object, ... None = everything except debris and "
                    "space_object (their counts are still reported).")] = None,
) -> dict:
    """Vessels in the game with id, name, type, situation, body, and distance from the active vessel.

    `id` ('#123') is unambiguous for game_switch_vessel / target_set / orbit_info even when names
    repeat (debris inherits its parent's name). Sorted by distance in flight, else by name.
    """
    k = ksp()
    sc = k.sc
    if kinds is not None:
        known = _safe(lambda: set(sc.VesselType.__members__), None)
        unknown = sorted(set(kinds) - known) if known else []
        if unknown:
            raise AstraError(f"unknown vessel type(s) {unknown}", "one of: " + ", ".join(sorted(known)))
    active = _safe(lambda: sc.active_vessel) if k.scene() == "flight" else None
    hidden: dict[str, int] = {}
    rows = []
    for v in sc.vessels:
        try:
            vtype = enum_name(v.type)
            if (kinds is None and vtype in ("debris", "space_object")) or (kinds is not None and vtype not in kinds):
                hidden[vtype] = hidden.get(vtype, 0) + 1
                continue
            row: dict[str, Any] = {"id": vessel_id(v), "name": v.name, "type": vtype,
                                   "situation": enum_name(v.situation), "body": v.orbit.body.name}
            if active is not None:
                row["active"] = v == active
                row["distance_m"] = math.dist((0.0, 0.0, 0.0), v.position(active.reference_frame))
            rows.append(row)
        except Exception:  # noqa: BLE001 — a vessel destroyed mid-listing
            continue
    rows.sort(key=lambda r: (r.get("distance_m", 0.0), r["name"]))
    return {"count": len(rows), "hidden": hidden, "vessels": rows}


@tool("game")
def game_switch_vessel(
    name: Annotated[str, Field(description="Vessel name (tolerant) or '#id' from game_list_vessels.")],
) -> dict:
    """Make another vessel the active one (from flight, or fly it from the space center / tracking station).

    Stops time warp first (switching mid-warp can hang KSP), waits for the switch to complete and
    reports the new active vessel. Switching to a vessel outside physics range reloads the flight
    scene, which KSP refuses while the current vessel is not in a savable state (thrusting, in the
    atmosphere). A short stretch of game time passes during the switch; the game is paused afterwards.
    """
    k = ksp()
    scene = k.scene()
    target = resolve_vessel(name)
    exact = target.name
    active = k.sc.active_vessel if scene == "flight" else None

    def arrived():  # by name too: a scene reload may hand out new proxies (never the vessel we left)
        now = k.sc.active_vessel
        return now == target or (now.name == exact and now != active)
    if scene == "flight":
        sc = k.sc
        if active == target:
            return {"switched": False, "note": "already the active vessel", "active_vessel": vessel_brief(target)}
        stop_warp(sc)
        if k.bridge.up():  # the bridge reports KSP's refusal instead of waiting on it
            request = bridge_vessel_ref(target)
            k.set_paused(False)
            try:
                k.bridge.post("/fly-vessel", request, timeout=90.0)
            except (BridgeError, NotConnected):
                k.hold_for_deliberation()
                raise
        else:
            # kRPC's setter waits until KSP has switched; if KSP refuses (unloaded target, vessel not
            # savable) it would wait forever, so only take it when the target is in physics range.
            distance = math.dist((0.0, 0.0, 0.0), target.position(active.reference_frame))
            if distance > 2000.0:
                raise AstraError(f"{exact} is {distance / 1000.0:.1f} km away and the bridge is down",
                                 "start the bridge (game_status) to switch to vessels outside physics range")
            k.set_paused(False)
            sc.active_vessel = target
        if wait_for(arrived, 60.0, 0.25) is None:
            k.hold_for_deliberation()
            raise AstraError("KSP did not switch to the vessel", "call game_status; try again")
    elif scene in ("space_center", "tracking_station"):
        request = bridge_vessel_ref(target)
        k.bridge.post("/fly-vessel", request, timeout=90.0)
        if _await_scene(("flight",), 180.0, need_vessel=True) is None or wait_for(arrived, 30.0) is None:
            raise AstraError(f"did not enter flight on {exact!r}", "call game_status")
    else:
        raise WrongScene(f"cannot switch vessels from '{scene}'", "game_space_center first")
    reset_links()
    return {"switched": True, "active_vessel": vessel_brief(k.vessel()), "paused": k.hold_for_deliberation()}


@tool("game")
def game_recover(
    vessel: Annotated[str | None, Field(description="Vessel to recover (name or '#id'); None = the active vessel.")] = None,
) -> dict:
    """Recover a landed or splashed vessel (crew back to the roster; funds and science in career).

    Recovering the active vessel ends the flight and returns to the space center.
    """
    k = ksp()
    sc = k.sc
    v = resolve_vessel(vessel) if vessel else k.vessel()
    if not v.recoverable:
        raise AstraError(f"{v.name} is not recoverable ({enum_name(v.situation)})",
                         "only vessels landed or splashed where KSP allows recovery can be recovered")
    name, oid, crew = v.name, v._object_id, [c.name for c in v.crew]
    funds0, science0 = _safe(lambda: sc.funds), _safe(lambda: sc.science)
    v.recover()

    def gone():
        if _krpc_up() and ksp().scene() != "flight":
            return True
        return all(x._object_id != oid for x in ksp().sc.vessels)
    if wait_for(gone, 60.0) is None:
        raise AstraError(f"{name} is still there after the recovery request", "call game_list_vessels")
    reset_links()
    out = {"recovered": name, "crew": crew, "scene": _safe(ksp().scene)}
    funds1, science1 = _safe(lambda: ksp().sc.funds), _safe(lambda: ksp().sc.science)
    if isinstance(funds0, float) and isinstance(funds1, float) and math.isfinite(funds1 - funds0):
        out["funds_gained"] = funds1 - funds0
    if isinstance(science0, float) and isinstance(science1, float) and math.isfinite(science1 - science0):
        out["science_gained"] = science1 - science0
    return out


@tool("game")
def game_space_center() -> dict:
    """Leave flight (or the editor) for the space center, keeping the flight's vessels in the save.

    From flight the game is saved to persistent.sfs first (what KSP's own "Space Center" button
    does), so vessels in flight stay where they are. Refused while the active vessel is moving
    through an atmosphere, because KSP deletes vessels put on rails there.
    """
    k = ksp()
    sc = k.sc
    scene = k.scene()
    if scene == "space_center":
        return {"scene": scene, "note": "already at the space center"}
    persisted = False
    if scene == "flight":
        v = k.vessel()
        situation = enum_name(v.situation)
        body = v.orbit.body
        if situation in ("flying", "sub_orbital") and body.has_atmosphere and \
                v.flight(body.reference_frame).mean_altitude < body.atmosphere_depth:
            raise AstraError(f"{v.name} is {situation} inside {body.name}'s atmosphere",
                             "finish the ascent or landing first, or game_revert / game_restore to abandon it")
        stop_warp(sc)
        sc.save("persistent")
        persisted = True
    sc.load_space_center()
    if _await_scene(("space_center",), 120.0) is None:
        raise AstraError("the game did not reach the space center", "call game_status")
    reset_links()
    return {"scene": "space_center", "flight_persisted": persisted, "ut": sc.ut}


@tool("game")
def game_pause(
    paused: Annotated[bool, Field(description="true freezes game time; false lets it run in real time.")],
) -> dict:
    """Pause or unpause the game clock.

    ASTRA already pauses whenever control returns to you (deliberation costs no game time) and
    reflex/warp tools run time as needed, so you rarely need this. Unpausing lets physics, MechJeb
    and walking kerbals act in real time until something pauses again.
    """
    k = ksp()
    k.set_paused(paused)
    return {"paused": k.paused, "scene": k.scene(), "ut": k.sc.ut}


def _log_path() -> Path:
    return CONFIG.ksp_dir / "KSP.log"


def tail_lines(path: Path, count: int) -> list[str]:
    """The last `count` lines of a (possibly large) text file."""
    with path.open("rb") as f:
        f.seek(0, 2)
        end = f.tell()
        block, data = 64 * 1024, b""
        pos = end
        while pos > 0 and data.count(b"\n") <= count:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    return data.decode("utf-8", errors="replace").splitlines()[-count:]


DEFAULT_LOG_PATTERN = r"^\[(EXC|ERR)\b|Exception|NullReference"


@tool("game", needs_game=False)
def game_log(
    lines: Annotated[int, Field(ge=1, le=400, description="How many matching entries to return (the most recent).")] = 40,
    pattern: Annotated[str | None, Field(
        description="Regex to filter lines (case-insensitive). None = exceptions and errors "
                    f"({DEFAULT_LOG_PATTERN!r}). Use '.' to see the raw tail.")] = None,
    scan_lines: Annotated[int, Field(ge=100, le=200_000, description="How far back in KSP.log to look, in lines.")] = 5000,
) -> dict:
    """Recent KSP.log entries matching a pattern (default: exceptions and errors) to diagnose failures.

    Exception entries include their stack-trace continuation lines. Timestamps are the game's
    wall-clock times. Read it after a refused launch, a craft that loads broken, or odd behavior.
    """
    path = _log_path()
    if not path.exists():
        raise AstraError(f"no KSP.log at {path}", "set ASTRA_KSP_DIR to the KSP install folder")
    try:
        regex = re.compile(pattern or DEFAULT_LOG_PATTERN, re.IGNORECASE)
    except re.error as exc:
        raise AstraError(f"bad regex {pattern!r}: {exc}") from exc
    tail = tail_lines(path, scan_lines)
    entries: list[str] = []
    i = 0
    while i < len(tail):
        line = tail[i]
        i += 1
        if not regex.search(line):
            continue
        extra = []
        while i < len(tail) and not tail[i].startswith("[") and len(extra) < 8:
            if tail[i].strip():
                extra.append(tail[i].rstrip())
            i += 1
        entries.append("\n".join([line.rstrip(), *extra]))
    return {"log": str(path), "scanned_lines": len(tail), "matched": len(entries), "entries": entries[-lines:]}
