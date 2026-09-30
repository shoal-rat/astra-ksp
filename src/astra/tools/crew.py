"""Kerbals: roster, EVA, walking, flags, boarding, and crew transfer inside a vessel.

EVA, walking, flags and boarding go through the bridge (kRPC cannot do them); moving a kerbal
between parts of one vessel uses kRPC. Bridge replies are claims, not proof: every tool checks the
outcome through kRPC where it can and reports both.
"""

import math
import re
from typing import Annotated, Any, Callable

from pydantic import Field

from astra.errors import AstraError, BridgeError, NotConnected
from astra.ksp import ksp
from astra.registry import tool
from astra.tools.game import reset_links, stop_warp, wait_for
from astra.tools.observe import bridge_parts, enum_name, pick_name, resolve_part


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 — optional readouts only
        return default


def eva_kerbals(k: Any) -> list[dict]:
    """EVA kerbals in physics range as the bridge sees them. kRPC's vessel list leaves out EVA
    kerbals and flags, so every EVA and flag check goes through the bridge."""
    return k.bridge.get("/eva-status", timeout=15.0).get("kerbals") or []


def find_eva(k: Any, kerbal: str | None) -> dict:
    """The bridge record of `kerbal` on EVA; None = the active vessel if it is a kerbal on EVA."""
    evas = eva_kerbals(k)
    if kerbal is None:
        active = [e for e in evas if e.get("isActiveVessel")]
        if not active:
            raise AstraError("the active vessel is not a kerbal on EVA", "name the kerbal")
        return active[0]
    if not evas:
        raise AstraError(f"{kerbal} is not on EVA (no kerbal is)", "crew_eva first")
    return evas[pick_name([e.get("name", "") for e in evas], kerbal, "kerbal on EVA")]


def _situation(text: Any) -> str:
    """Bridge situations (LANDED, PRELAUNCH, SUB_ORBITAL) in the kRPC spelling (landed, pre_launch...)."""
    t = str(text or "").lower()
    return "pre_launch" if t == "prelaunch" else t


def _on_ground(rec: dict | None) -> bool:
    """On the ground or in the water by the bridge's own test (KSP's Landed/Splashed flags). A kerbal
    that stepped out at the launch site keeps situation PRELAUNCH while it stands, so the situation
    string is only the fallback for a record without the flags."""
    if not rec:
        return False
    if rec.get("landed") is not None or rec.get("splashed") is not None:
        return bool(rec.get("landed") or rec.get("splashed"))
    return _situation((rec.get("vessel") or {}).get("situation")) in ("landed", "splashed", "pre_launch")


def eva_state(e: dict) -> dict:
    """What the crew needs to know about a kerbal on EVA, from its bridge record."""
    v = e.get("vessel") or {}
    near = e.get("nearestHatch") or {}
    out = {"name": e.get("name"), "situation": _situation(v.get("situation")), "on_ground": _on_ground(e),
           "body": e.get("body"), "biome": e.get("biome"), "lat_deg": e.get("latitude"), "lon_deg": e.get("longitude"),
           "altitude_m": e.get("altitude_m"), "surface_altitude_m": e.get("radarAltitude_m"),
           "surface_speed_mps": e.get("surfaceSpeed_mps"), "state": e.get("fsmState"),
           "active": e.get("isActiveVessel"), "on_ladder": e.get("onLadder"),
           "jetpack_fuel": e.get("jetpackFuel"), "flags_carried": e.get("flagItems"),
           "can_plant_flag": e.get("canPlantFlag"), "plant_blocker": e.get("plantBlocker"), "walk": e.get("walk")}
    if near:
        out["nearest_hatch"] = {"part": near.get("title") or near.get("name"), "index": near.get("index"),
                                "vessel": (near.get("vessel") or {}).get("name"), "distance_m": near.get("distance_m"),
                                "within_boarding_reach": near.get("withinBoardingReach")}
    return out


def _flags(k: Any) -> dict[Any, dict]:
    """Flag vessels by persistentId (bridge /vessels)."""
    rows = k.bridge.get("/vessels", timeout=15.0).get("vessels") or []
    return {r.get("persistentId"): r for r in rows if str(r.get("type", "")).lower() == "flag"}


def _settle(k: Any, done: Callable[[], Any], real_s: float) -> tuple[Any, float]:
    """Wait for `done()`; if the game is paused and it has not happened within a second, let game
    time run (some actions only complete over frames) and pause again. Returns (result, game_s)."""
    result = wait_for(done, 1.0, 0.2)
    if result or not k.paused:
        return result or wait_for(done, real_s, 0.2), 0.0
    ut0 = k.sc.ut
    k.set_paused(False)
    try:
        result = wait_for(done, real_s, 0.2)
    finally:
        k.hold_for_deliberation()
    return result, k.sc.ut - ut0


def _crew_of(v: Any) -> list[str]:
    return [c.name for c in v.crew]


@tool("crew")
def crew_roster() -> dict:
    """Every kerbal in the game with trait, level and status, plus who is aboard the active vessel.

    `roster` comes from the bridge (works at the space center too). In flight, `aboard` lists the
    active vessel's crew (kRPC) and `seats` where each kerbal of the loaded vessels sits.
    """
    k = ksp()
    out: dict[str, Any] = {}
    try:
        reply = k.bridge.get("/crew-roster", timeout=15.0)
        out["roster"] = reply.get("roster", [])
        out["counts"] = {key: reply[key] for key in ("available", "assigned", "kia", "dead", "missing") if key in reply}
    except (BridgeError, NotConnected) as exc:
        out["roster_error"] = f"{exc.message} (the full roster needs the bridge)"
    if k.scene() == "flight":
        v = k.vessel()
        out["aboard"] = {"vessel": v.name, "capacity": v.crew_capacity,
                         "crew": [{"name": c.name, "trait": c.trait, "experience": c.experience} for c in v.crew]}
        seats = _safe(lambda: k.bridge.get("/crew-list", timeout=15.0).get("crew"))
        if seats is not None:
            out["seats"] = seats
    if "roster" not in out and "aboard" not in out:
        raise AstraError(out.get("roster_error", "no crew information available"),
                         "start the bridge (game_status), or fly a vessel to see its crew")
    return out


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def _ground_distance_m(lat1: float, lon1: float, lat2: float, lon2: float, radius_m: float) -> float:
    """Great-circle distance between two points on a sphere of radius_m (haversine), m."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2.0 * radius_m * math.asin(min(1.0, math.sqrt(a)))


def _part_latlon(v: Any, ref: dict) -> tuple[float, float]:
    """Latitude and longitude of the centre of the part a bridge part reference names (its index is
    the position in kRPC's parts.all; the names must agree)."""
    p = v.parts.all[int(ref["index"])]
    if p.name != ref.get("name"):
        raise AstraError(f"the bridge's part {ref.get('name')} is {p.name} in kRPC's list")
    body = v.orbit.body
    pos = p.position(body.reference_frame)
    return body.latitude_at_position(pos, body.reference_frame), body.longitude_at_position(pos, body.reference_frame)


def _standing(rec: dict | None) -> bool:
    """Settled: on the ground in a grounded idle state ("Idle (Grounded)"; not "Idle (Floating)" or a
    low-gravity bound), or in the water ("Swim (Idle)", which never says grounded)."""
    if not _on_ground(rec):
        return False
    state = str(rec.get("fsmState", "")).lower()
    if rec.get("splashed") or _situation((rec.get("vessel") or {}).get("situation")) == "splashed":
        return state.startswith("swim") or "grounded" in state
    return state.startswith("idle") and "grounded" in state


def _run_until(k: Any, done: Callable[[], Any], game_s: float) -> Any:
    """Let game time run until done() is truthy or game_s passes; pause again. Returns done()'s value,
    or "timeout" (also when done() keeps failing: the game-time bound does not depend on it)."""
    ut0 = k.sc.ut

    def check():
        over = k.sc.ut - ut0 >= game_s
        return _safe(done) or (over and "timeout")
    try:
        with k.running():
            return wait_for(check, game_s * 3 + 20.0, 0.2)
    finally:
        k.hold_for_deliberation()


_HOP_SPEED_MPS = 1.5  # the bridge's hop cruise speed (EvaMath.HopMaxSpeed); 0.8/s x distance on the final approach
_HOP_MAX_START_SPEED = 5.0  # m/s over the ground: faster, the kerbal is not beside a point on the ground
_HOP_MIN_OFFSET_M = 0.3  # crew_eva: nearer the crew part's centre than this, "out from the hatch" has no direction


def _hop_blocker(rec: dict) -> tuple[str, str] | None:
    """(why, hint) when a jetpack hop cannot start for this EVA kerbal, else None. The bridge's hop
    steers toward a point on the ground: in orbit or at speed it would fire the pack at full thrust
    for its whole time bound and carry the kerbal away."""
    who = rec.get("name")
    situation = _situation((rec.get("vessel") or {}).get("situation"))
    speed = float(rec.get("surfaceSpeed_mps") or 0.0)
    if situation in ("orbiting", "escaping", "docked") or speed > _HOP_MAX_START_SPEED:
        return (f"{who} is {situation} at {speed:.0f} m/s over the surface; a hop starts on or just above the ground",
                "crew_hop flies over the ground next to the start; in space ASTRA does not fly the jetpack")
    if rec.get("hasJetpack") is False:
        return f"{who} carries no jetpack", "walk instead (crew_walk)"
    if rec.get("jetpackFuel") is not None and rec["jetpackFuel"] <= 0:
        return f"{who}'s jetpack is out of EVA propellant", "walk instead (crew_walk)"
    return None


def _hop(k: Any, name: str, bearing: float, distance_m: float, rise_m: float) -> dict:
    """Post a jetpack hop and fly it until the pack is stowed and the kerbal stands (or time runs out).
    The bridge's time bound covers the cruise (with its slow final approach), the climb and a start."""
    max_game_s = 10.0 + distance_m / (0.8 * _HOP_SPEED_MPS) + 2.0 * rise_m
    reply = k.bridge.post("/eva-hop", {"crew": name, "bearing": bearing % 360.0, "distance": distance_m,
                                       "rise": rise_m, "maxS": max_game_s}, timeout=15.0)
    ut0 = k.sc.ut

    def landed_after_hop():
        rec = next((e for e in eva_kerbals(k) if e.get("name") == name), None)
        hop = (rec or {}).get("hop") or {}
        return rec if hop.get("state") not in (None, "flying") and _standing(rec) else None
    _run_until(k, landed_after_hop, max_game_s + 15.0)
    rec = find_eva(k, name)
    return {"hop": rec.get("hop"), "fuel_before": reply.get("jetpackFuel"), "game_s": k.sc.ut - ut0,
            "now": eva_state(rec)}


@tool("crew")
def crew_eva(
    kerbal: Annotated[str, Field(description="Name of a kerbal aboard the active vessel (tolerant matching).")],
    hop_clear_m: Annotated[float | None, Field(
        gt=0, le=50,
        description="Landed or splashed only: fly the EVA jetpack straight out from the hatch this far (m), "
                    "about 1 m up, before touching anything, then stow it and drop to the ground. Use it when "
                    "the hatch is above parts the kerbal would land on (tanks, panels, a heat shield lip): "
                    "in low gravity a kerbal standing or walking on a light lander can push it over. "
                    "None: step out and fall where the hatch puts you.")] = None,
) -> dict:
    """Send a kerbal out on EVA from the active vessel; the kerbal becomes the active vessel.

    Works landed, splashed or in space (in space the kerbal floats free next to the hatch: have a
    plan to return). Fails if the hatch is blocked. Verifies that an EVA kerbal now exists. From a
    vessel on the ground, game time then runs until the kerbal stands on the ground (a few seconds;
    `game_s_elapsed`), so it can walk or plant a flag next; with hop_clear_m the jetpack first carries
    it clear of the vessel (`hop`), or `hop_skipped` / `hop_error` says why it did not (the kerbal is
    outside either way). The game is paused again afterwards (`paused`).
    """
    k = ksp()
    v = k.vessel()
    aboard = _crew_of(v)
    if not aboard:
        raise AstraError(f"{v.name} has no crew aboard", None)
    name = aboard[pick_name(aboard, kerbal, "crew member aboard")]
    grounded = _safe(lambda: enum_name(v.situation)) in ("landed", "splashed", "pre_launch")
    if hop_clear_m is not None and not grounded:
        raise AstraError("hop_clear_m is for vessels on the ground", "in space the kerbal floats at the hatch")
    body = _safe(lambda: v.orbit.body)
    fl = _safe(lambda: v.flight(body.reference_frame))
    origin = _safe(lambda: (fl.latitude, fl.longitude, body.equatorial_radius))  # lat, lon, body radius
    if hop_clear_m is not None and origin is None:
        raise AstraError(f"could not read {v.name}'s position to aim the hop; {name} is still aboard",
                         "retry crew_eva, or go out without hop_clear_m and then crew_hop away_from the vessel")
    stop_warp(k.sc)
    ut0 = k.sc.ut
    reply = k.bridge.post("/eva-go", {"crew": name}, timeout=30.0)
    eva, _ = _settle(k, lambda: next((e for e in eva_kerbals(k) if e.get("name") == name), None), 5.0)
    out: dict[str, Any] = {"kerbal": name, "from_vessel": v.name, "verified": eva is not None}
    hopped = False
    if eva is not None and hop_clear_m is not None:
        # Straight out through the hatch: from the centre of the crew part the kerbal left (else the
        # vessel's position) to the kerbal at the hatch. From here on the kerbal is outside, so a hop
        # that cannot fly is reported, not raised.
        start = _safe(lambda: _part_latlon(v, reply["fromPart"])) or origin[:2]
        blocker = _hop_blocker(eva)
        offset_m = _ground_distance_m(*start, eva["latitude"], eva["longitude"], origin[2])
        if blocker is None and offset_m < _HOP_MIN_OFFSET_M:
            blocker = (f"{name} came out within {_HOP_MIN_OFFSET_M} m (horizontally) of the crew part's centre, "
                       "so 'straight out from the hatch' has no direction (a hatch facing up?)",
                       "choose a bearing clear of the vessel and crew_hop")
        if blocker is not None:
            out["hop_skipped"] = f"{blocker[0]}; {blocker[1]}"
        else:
            try:
                out["hop"] = _hop(k, name, _bearing_deg(*start, eva["latitude"], eva["longitude"]), hop_clear_m, 1.0)
                hopped = True
            except AstraError as exc:
                out["hop_error"] = exc.message
        eva = _safe(lambda: find_eva(k, name), eva)
    if eva is not None and grounded and not hopped:
        found = _run_until(k, lambda: next((e for e in eva_kerbals(k) if e.get("name") == name and _standing(e)), None),
                           _SETTLE_S * 2)
        eva = found if isinstance(found, dict) else next((e for e in eva_kerbals(k) if e.get("name") == name), eva)
    game_s = k.sc.ut - ut0
    reset_links()
    if eva is not None:
        out["eva"] = eva_state(eva)
    for key in ("biome", "warnings"):
        if reply.get(key):
            out[key] = reply[key]
    out["active_vessel"] = _safe(lambda: k.sc.active_vessel.name)
    if game_s > 0.05:
        out["game_s_elapsed"] = game_s
    if eva is None:
        out["note"] = "no EVA kerbal appeared: the hatch may be blocked; check camera_look and game_log"
    out["paused"] = k.hold_for_deliberation()
    return out


@tool("crew")
def crew_hop(
    kerbal: Annotated[str, Field(description="Kerbal on EVA with a jetpack, on or just above the ground.")],
    distance_m: Annotated[float, Field(
        gt=0, le=50, description="How far to fly over the ground, m (the pack cruises at up to 1.5 m/s).")],
    rise_m: Annotated[float, Field(
        ge=0, le=10,
        description="Cruise height above the kerbal's start altitude, m (absolute: not above the ground under "
                    "the track). Choose it above the tallest obstacle on the track plus any rise of the ground "
                    "along it, plus a margin; about 1 m is enough only to step off a shelf or a drop-off.")],
    bearing_deg: Annotated[float | None, Field(
        description="Direction, degrees clockwise from north (or give away_from).")] = None,
    away_from: Annotated[str | None, Field(
        description="Instead of a bearing: a vessel name; fly straight away from it (e.g. off a lander).")] = None,
) -> dict:
    """Short EVA jetpack flight over the ground: fly beside the start at a low height, stow the pack,
    drop and stand.

    For getting off a vessel (standing on a tank or panel) or over an obstacle without walking on
    anything. Surface only: refused in orbit or when the kerbal moves fast over the ground. Game time
    runs until the kerbal stands again (bounded by about 25 s + distance / 1.2 m/s + 2 s per m of
    rise), then the game is paused. Uses a little EVA propellant; walk for longer distances.
    """
    if (bearing_deg is None) == (away_from is None):
        raise AstraError("give either bearing_deg or away_from")
    k = ksp()
    k.require_flight()
    rec = find_eva(k, kerbal)
    name = rec["name"]
    blocker = _hop_blocker(rec)
    if blocker is not None:
        raise AstraError(*blocker)
    if bearing_deg is None:
        vessels = list(k.sc.vessels)
        target = vessels[pick_name([x.name for x in vessels], away_from, "vessel")]
        fl = target.flight(target.orbit.body.reference_frame)
        bearing_deg = _bearing_deg(fl.latitude, fl.longitude, rec["latitude"], rec["longitude"])
    out = _hop(k, name, bearing_deg, distance_m, rise_m)
    reset_links()
    return {"kerbal": name, "bearing_deg": bearing_deg % 360.0, **out, "paused": k.hold_for_deliberation()}


@tool("crew")
def crew_status(
    kerbal: Annotated[str | None, Field(description="Kerbal on EVA; None = the active vessel if it is one.")] = None,
) -> dict:
    """State of a kerbal on EVA: position, situation, speed, and the EVA controller state.

    `state` is KSP's EVA state (e.g. "Idle (Grounded)", walking, ladder); also jetpack fuel, flags
    carried, whether a flag can be planted here (and what blocks it), an active walk-to, and the
    nearest free hatch with its distance and whether it is within boarding reach. `on_ground` is
    KSP's own landed/splashed test (at the launch site the situation stays pre_launch while standing).
    A settled kerbal (on the ground, speed ~0, idle) can walk, plant a flag or board.
    """
    k = ksp()
    k.require_flight()
    return eva_state(find_eva(k, kerbal))


@tool("crew")
def crew_walk(
    kerbal: Annotated[str, Field(description="Kerbal on EVA, standing on the ground.")],
    lat_deg: Annotated[float | None, Field(description="Destination latitude, degrees (with lon_deg).")] = None,
    lon_deg: Annotated[float | None, Field(description="Destination longitude, degrees (with lat_deg).")] = None,
    bearing_deg: Annotated[float | None, Field(
        description="Instead of lat/lon: direction to walk, degrees clockwise from north (with distance_m).")] = None,
    distance_m: Annotated[float | None, Field(description="Instead of lat/lon: how far to walk, m (with bearing_deg).")] = None,
    wait: Annotated[bool, Field(
        description="true: let game time run until the kerbal arrives, stalls, or max_game_s passes, then "
                    "pause; false: only give the order (it walks whenever time runs).")] = True,
    max_game_s: Annotated[float | None, Field(
        gt=0, description="With wait: bound on game time for the walk. None = 30 s + distance / 0.4 m/s.")] = None,
) -> dict:
    """Walk a kerbal on the surface to a point (KSP's own EVA walking, steered toward the target).

    Give either lat_deg + lon_deg or bearing_deg + distance_m. With wait (default) game time runs
    until the walk ends: `walk.state` is arrived, stalled (no progress; an obstacle or steep slope),
    stopped with `timed_out` (max_game_s ran out and the order was cancelled; the kerbal stands where
    it is), or the bridge's "left <body>" / "error: ...". `lost` means the kerbal is no longer on EVA
    in physics range. Then the game is paused again (`paused`). Walking speed is about 0.5-1 m/s on
    Kerbin; in low gravity kerbals bound. Stay within ~2 km of vessels you need loaded.
    """
    by_point = lat_deg is not None or lon_deg is not None
    by_bearing = bearing_deg is not None or distance_m is not None
    if by_point == by_bearing or (by_point and (lat_deg is None or lon_deg is None)) or \
            (by_bearing and (bearing_deg is None or distance_m is None)):
        raise AstraError("give either lat_deg and lon_deg, or bearing_deg and distance_m")
    if by_point and not (-90.0 <= lat_deg <= 90.0):
        raise AstraError(f"latitude {lat_deg} is outside -90..90")
    if by_bearing and distance_m <= 0:
        raise AstraError("distance_m must be positive")
    k = ksp()
    k.require_flight()
    rec = find_eva(k, kerbal)
    state = eva_state(rec)
    name = state["name"]
    if not _on_ground(rec):
        raise AstraError(f"{name} is {state['situation']}, not standing on the ground",
                         "walking needs a kerbal on the ground; wait until crew_status shows on_ground")
    body = {"crew": name}
    if by_point:
        body.update(lat=lat_deg, lon=lon_deg)
    else:
        body.update(bearing=bearing_deg % 360.0, distance=distance_m)
    reply = k.bridge.post("/eva-walk-to", body, timeout=30.0)
    out: dict[str, Any] = {"kerbal": name, "from": {key: state[key] for key in ("lat_deg", "lon_deg", "altitude_m")},
                           "target": {"lat_deg": reply.get("targetLatitude"), "lon_deg": reply.get("targetLongitude"),
                                      "distance_m": reply.get("distance_m")}}
    if reply.get("warnings"):
        out["warnings"] = reply["warnings"]
    if not wait:
        out["note"] = "the kerbal walks while game time runs; follow it with crew_status"
        return out
    limit = max_game_s or 30.0 + float(reply.get("distance_m") or 0.0) / 0.4
    ut0 = k.sc.ut
    last: dict = {}
    ended: Any = None

    def walk_over():
        nonlocal last
        if k.sc.ut - ut0 >= limit:  # first: the game-time bound must not depend on the bridge read
            return "timeout"
        rec = next((e for e in eva_kerbals(k) if e.get("name") == name), None)
        if rec is None:
            return "lost"
        last = rec
        return (rec.get("walk") or {}).get("state") != "walking"
    try:
        with k.running():
            ended = wait_for(walk_over, limit * 2 + 30.0, 0.25)  # real-time bound: the game may run slower than 1x
            if ended != "lost" and (last.get("walk") or {}).get("state") == "walking":
                k.bridge.post("/eva-walk-to", {"crew": name, "stop": True}, timeout=15.0)
                out["timed_out"] = True
                last = find_eva(k, name)
    finally:
        paused = k.hold_for_deliberation()
    if ended == "lost":
        out.update(walk=None, lost=True, game_s_elapsed=k.sc.ut - ut0, paused=paused,
                   note=f"{name} is no longer on EVA in physics range (killed, or out of the loaded area); "
                        "game_log and game_list_vessels show what happened")
        return out
    end = eva_state(last) if last else eva_state(find_eva(k, name))
    out.update(walk=end.pop("walk"), game_s_elapsed=k.sc.ut - ut0, now=end, paused=paused)
    return out


_FLAG_WAIT_S = 20.0  # the planting animation takes a few seconds of game time
_SETTLE_S = 6.0  # real seconds for a kerbal to come to an idle stand


@tool("crew")
def crew_plant_flag(
    kerbal: Annotated[str, Field(description="Kerbal on EVA, standing still on the ground.")],
    name: Annotated[str, Field(description="Flag name (shown on the map and as the flag vessel's name).")],
    plaque: Annotated[str, Field(description="Plaque text on the flag.")] = "",
) -> dict:
    """Plant a flag where the kerbal stands, and verify that a flag now exists there and got its name.

    The planting animation needs a few seconds of game time, which this tool runs; KSP names the
    flag at its end (`named`). The game is paused again afterwards (`game_s_elapsed`, `paused`).
    """
    k = ksp()
    k.require_flight()
    rec = find_eva(k, kerbal)
    who = rec["name"]
    if not _on_ground(rec) and (rec.get("radarAltitude_m") or 99.0) < 5.0:
        # Mid-bound in low gravity (or just dropped): let the kerbal come to a stand first.
        _run_until(k, lambda: next((e for e in eva_kerbals(k) if e.get("name") == who and _standing(e)), None), 8.0)
        rec = find_eva(k, who)
    state = eva_state(rec)
    if not _on_ground(rec):
        raise AstraError(f"{who} is {state['situation']}; flags are planted standing on the ground",
                         "wait until crew_status shows on_ground and nearly zero speed")
    flags_before = set(_flags(k))
    stop_warp(k.sc)
    ut0 = k.sc.ut
    try:
        # Game time runs: the kerbal must settle until KSP allows planting, and the animation, the flag
        # vessel and its naming only progress while time runs (a plant posted while paused is only
        # queued; the bridge then replies pending).
        with k.running():
            # A kerbal that just stopped walking or landed needs a few physics ticks to stand idle.
            ready = wait_for(lambda: find_eva(k, who).get("canPlantFlag"), _SETTLE_S, 0.2)
            if not ready:
                blocker = find_eva(k, who).get("plantBlocker")
                raise AstraError(f"{who} cannot plant a flag here: {blocker}",
                                 "stand still on open ground (crew_status: state idle, can_plant_flag true)")
            reply = k.bridge.post("/eva-plant-flag", {"crew": who, "siteName": name, "plaque": plaque,
                                                      "waitS": _FLAG_WAIT_S}, timeout=_FLAG_WAIT_S + 25.0)
    finally:
        k.hold_for_deliberation()
    if not reply.get("verified") and reply.get("watchId") is not None:
        # A slow game: the flag vessel may stand under KSP's default name until the animation ends and
        # the bridge names it. Run game time until the bridge's watch has an outcome.
        def watch_outcome():
            rows = k.bridge.get("/eva-status", timeout=15.0).get("flagPlanting") or []
            w = next((r for r in rows if r.get("watchId") == reply["watchId"]), None)
            return w if w and w.get("state") in ("named", "lost", "expired") else None
        outcome = _run_until(k, watch_outcome, _FLAG_WAIT_S)
        if isinstance(outcome, dict):
            reply = outcome

    def new_flag():
        return next((f for pid, f in _flags(k).items() if pid not in flags_before), None)
    flag, _ = _settle(k, new_flag, 5.0)
    named = bool(reply.get("verified"))
    out: dict[str, Any] = {"kerbal": who, "planted": flag is not None, "named": named,
                           "plant_state": reply.get("state"), "game_s_elapsed": k.sc.ut - ut0}
    if flag is not None:
        out["flag"] = {"name": flag.get("name"), "situation": _situation(flag.get("situation")),
                       "body": flag.get("body"), "lat_deg": state["lat_deg"], "lon_deg": state["lon_deg"],
                       "biome": state["biome"]}
        if named and (reply.get("flag") or {}).get("plaque") is not None:
            out["flag"]["plaque"] = reply["flag"]["plaque"]
        if not named and reply.get("state") in ("planting", "placing", None):
            out["note"] = ("the flag stands but KSP has not named it yet: the name and plaque are applied when "
                           "game time next runs, and are lost if the game is restored or the scene changes first")
        elif not named:
            out["note"] = f"the bridge could not name the flag ({reply.get('state')}: {reply.get('detail')})"
    else:
        out["bridge"] = reply
        out["note"] = "no new flag vessel appeared; check crew_status (state, plant_blocker) and camera_look"
    out["paused"] = k.hold_for_deliberation()
    return out


def _eva_status(k: Any, name: str) -> dict:
    """The bridge's /eva-status entry for one kerbal (hatch in reach, nearest free hatch...)."""
    return k.bridge.post("/eva-status", {"crew": name}, timeout=15.0).get("kerbal") or {}


def _boarding_part_id(k: Any, name: str, part: str) -> tuple[int, dict]:
    """(persistentId, description) of the crew part `name` should board, as the bridge needs it."""
    status = _eva_status(k, name)
    hatch, nearest = status.get("hatchPart"), status.get("nearestHatch")
    want = part.strip()
    if want.lower() == "nearest":
        if hatch:
            return int(hatch["persistentId"]), {**hatch, "vessel": status.get("hatchVessel")}
        if nearest and nearest.get("withinBoardingReach"):
            return int(nearest["persistentId"]), nearest
        where = (f"the nearest free hatch is {nearest.get('title') or nearest.get('name')} on "
                 f"{(nearest.get('vessel') or {}).get('name')}, {nearest.get('distance_m') or float('nan'):.1f} m away"
                 if nearest else "no vessel with a free hatch is loaded")
        raise AstraError(f"{name} is not within boarding reach of a free hatch: {where}",
                         "crew_walk closer (crew_status shows the distance), then crew_board again")
    vessel = status.get("hatchVessel") or (nearest or {}).get("vessel")
    if not vessel or vessel.get("persistentId") is None:
        raise AstraError(f"no vessel with a hatch is near {name}", "crew_walk to the vessel first")
    rows = [r for r in bridge_parts({"vesselPersistentId": vessel["persistentId"]}) if (r.get("crewCapacity") or 0) > 0]
    if re.fullmatch(r"\s*\d+\s*", want):
        hits = [r for r in rows if r.get("index") == int(want)]
    else:
        hits = [r for r in rows if str(r.get("name", "")).casefold() == want.casefold()]
        free = [r for r in hits if len(r.get("crew") or []) < r["crewCapacity"]]
        hits = free or hits
    if len(hits) != 1:
        listing = "; ".join(f"idx {r.get('index')}: {r.get('name')} ({len(r.get('crew') or [])}/{r.get('crewCapacity')} seats)"
                            for r in rows[:20])
        raise AstraError(f"{'no' if not hits else len(hits)} crew parts on {vessel.get('name')} match {part!r}",
                         f"crew parts there: {listing or 'none'}; or pass part='nearest'")
    return int(hits[0]["persistentId"]), {**hits[0], "vessel": vessel}


@tool("crew")
def crew_board(
    kerbal: Annotated[str, Field(description="Kerbal on EVA, next to the vessel to board.")],
    part: Annotated[str, Field(
        description="'nearest' (the hatch the kerbal touches, else the nearest free hatch within boarding "
                    "reach), or a crew part of the vessel next to the kerbal by its idx (vessel_parts of "
                    "that vessel) or internal part name (e.g. 'mk1pod.v2').")],
) -> dict:
    """Board a vessel from EVA (the kerbal must be within reach of the hatch) and verify the seat.

    Afterwards the boarded vessel is active and the kerbal appears in its crew; a few frames of game
    time may run while KSP completes the boarding (the game is paused again: `paused`).
    """
    k = ksp()
    k.require_flight()
    sc = k.sc
    name = find_eva(k, kerbal).get("name")
    part_id, chosen = _boarding_part_id(k, name, part)
    reply = k.bridge.post("/eva-board", {"crew": name, "partId": part_id}, timeout=30.0)
    # boarded:false comes only from within reach: KSP stopped at a carried-science dialog or refused
    # the kerbal's inventory. Running game time cannot change that, so none runs.
    refused = reply.get("boarded") is False

    def boarded():
        if any(e.get("name") == name for e in eva_kerbals(k)):
            return None  # still on EVA
        active = k.sc.active_vessel  # KSP focuses the vessel that was boarded
        return active if name in _crew_of(active) else None
    vessel, game_s = (wait_for(boarded, 1.0, 0.2), 0.0) if refused else _settle(k, boarded, 5.0)
    if vessel is None and not any(e.get("name") == name for e in eva_kerbals(k)):
        vessel = next((v for v in sc.vessels if name in _safe(lambda v=v: _crew_of(v), [])), None)
    reset_links()
    out: dict[str, Any] = {"kerbal": name, "part": {key: chosen.get(key) for key in ("index", "name", "title", "vessel")},
                           "bridge": reply, "boarded": vessel is not None}
    if vessel is not None:
        out.update(vessel=vessel.name, crew=_crew_of(vessel))
    elif refused:
        out["note"] = (f"KSP did not seat the kerbal: {reply.get('note') or 'no reason given'} Walking closer "
                       "will not help; look at the screen with camera_look")
    else:
        out["note"] = "the kerbal is still on EVA: move closer to the hatch (crew_walk) and try again"
    out["active_vessel"] = _safe(lambda: k.sc.active_vessel.name)
    if game_s:
        out["game_s_elapsed"] = game_s
    out["paused"] = k.hold_for_deliberation()
    return out


@tool("crew")
def crew_transfer(
    kerbal: Annotated[str, Field(description="Kerbal aboard the active vessel.")],
    to_part: Annotated[int | str, Field(description="Destination crewable part: idx from vessel_parts (kind='command' "
                                                    "shows free_seats) or a unique part name/tag.")],
) -> dict:
    """Move a kerbal to another crewable part of the same vessel (e.g. across a docking port).

    Only within one vessel (docked vessels count as one); between separate vessels use crew_eva and
    crew_board. Verifies that the destination part lost a free seat (a moment of game time may run
    for KSP to complete the move; the game is paused again).
    """
    k = ksp()
    v = k.vessel()
    crew = list(v.crew)
    if not crew:
        raise AstraError(f"{v.name} has no crew aboard", None)
    names = [c.name for c in crew]
    member = crew[pick_name(names, kerbal, "crew member aboard")]
    p, idx = resolve_part(v, to_part)
    seats = p.available_seats
    if seats <= 0:
        raise AstraError(f"{p.name} (idx {idx}) has no free seat",
                         "vessel_parts(kind='command') shows free_seats per crewable part")
    ut0 = k.sc.ut
    try:
        with k.running():  # never make a kRPC call that may wait on frames while paused: it cannot return
            k.sc.transfer_crew(member, p)
    finally:
        k.hold_for_deliberation()
    moved, _ = _settle(k, lambda: p.available_seats < seats, 3.0)
    game_s = k.sc.ut - ut0
    return {"kerbal": member.name, "to_part": p.name, "idx": idx, "moved": bool(moved),
            "free_seats_before": seats, "free_seats_after": p.available_seats, "game_s_elapsed": game_s,
            "paused": k.hold_for_deliberation()}
