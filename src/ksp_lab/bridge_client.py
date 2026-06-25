from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlparse

import requests


class BridgeError(RuntimeError):
    pass


@dataclass(slots=True)
class BridgeClient:
    base_url: str = "http://127.0.0.1:48500"
    timeout_s: int = 30

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise BridgeError("KSP bridge client refuses to connect to non-localhost hosts.")

    def state(self) -> dict:
        return self._request("GET", "/state")

    def load_craft(self, craft_name: str, craft_path: str | None = None, building: str = "VAB") -> dict:
        payload = {"craftName": craft_name, "building": building}
        if craft_path:
            payload["craftPath"] = craft_path
        return self._request("POST", "/craft/load", json=payload)

    def launch(self) -> dict:
        return self._request("POST", "/launch", json={})

    def revert(self) -> dict:
        return self._request("POST", "/revert", json={})

    def reset(self) -> dict:
        return self._request("POST", "/reset", json={})

    def save(self) -> dict:
        return self._request("POST", "/save", json={})

    def load_save(self, save_folder: str, scene: str = "spacecenter") -> dict:
        """Load a save. scene='spacecenter' (default) or 'flight' to resume the saved active vessel
        directly in flight (lets an in-space vessel be re-controlled after a bridge rebuild)."""
        return self._request("POST", "/save/load", json={"saveFolder": save_folder, "scene": scene})

    def space_center(self) -> dict:
        return self._request("POST", "/space-center", json={})

    def refuel_vessel(
        self,
        vessel_name: str = "",
        fraction: float = 1.0,
        resources: str = "",
    ) -> dict:
        payload: dict[str, str | float] = {"fraction": float(fraction)}
        if vessel_name:
            payload["vesselName"] = vessel_name
        if resources:
            payload["resources"] = resources
        return self._request("POST", "/vessel/refuel", json=payload)

    # ---- MechJeb autopilots (delegate rendezvous/docking to MechJeb instead of hand-rolling) ----
    # The bridge's JSON parser only reads string values, so every field is passed as a string.

    def mj_rendezvous(
        self,
        target: str,
        desired_distance: float = 100.0,
        max_phasing_orbits: float = 5.0,
        max_closing_speed: float = 100.0,
    ) -> dict:
        """Enable MechJeb's rendezvous autopilot on the ACTIVE vessel to close on ``target``."""
        return self._request("POST", "/mj-rendezvous", json={
            "target": target,
            "desiredDistance": str(desired_distance),
            "maxPhasingOrbits": str(max_phasing_orbits),
            "maxClosingSpeed": str(max_closing_speed),
        })

    def mj_dock(self, target: str, speed_limit: float = 1.0, force_rol: bool = False) -> dict:
        """Enable MechJeb's docking autopilot on the ACTIVE vessel to dock with ``target``'s port."""
        return self._request("POST", "/mj-dock", json={
            "target": target,
            "speedLimit": str(speed_limit),
            "forceRol": "true" if force_rol else "false",
        })

    def mj_ascent(self, altitude: float = 90000.0, inclination: float = 0.0,
                  autostage: bool = True) -> dict:
        """Enable MechJeb's ascent autopilot on the ACTIVE vessel to a parking orbit (Classic path).

        autostage=False disables MechJeb's own ascent autostaging (sets MechJebModuleAscentSettings.Autostage
        false before enabling the AP), so a caller that fires its own decouplers explicitly is the SOLE
        stager — no two-stagers race. Leave True for hand-off flights that want MechJeb to autostage."""
        return self._request("POST", "/mj-ascent", json={
            "altitude": str(altitude),
            "inclination": str(inclination),
            "autostage": "true" if autostage else "false",
        })

    def mj_execute_node(self, autowarp: bool = True, all_nodes: bool = False) -> dict:
        """Have MechJeb's node executor burn the next (or all) maneuver node(s) precisely."""
        return self._request("POST", "/mj-execute-node", json={
            "autowarp": "true" if autowarp else "false",
            "all": "true" if all_nodes else "false",
        })

    def mj_land(self, targeted: bool = False, lat: float = 0.0, lon: float = 0.0,
                touchdown_speed: float = 0.5) -> dict:
        """Enable MechJeb's landing autopilot on the ACTIVE vessel (targeted site or straight down)."""
        return self._request("POST", "/mj-land", json={
            "targeted": "true" if targeted else "false",
            "lat": str(lat),
            "lon": str(lon),
            "touchdownSpeed": str(touchdown_speed),
        })

    def mj_disable(self, which: str = "all") -> dict:
        """Disable a MechJeb autopilot module on the active vessel. which: dock | rendezvous | staging | all.

        "staging" turns OFF MechJeb's autostager (MechJebModuleStagingController) — distinct from the ascent
        AP's autostage flag. The autostager otherwise fires decouplers during ANY burn, including the in-space
        capture burn, which on a crewed/heat-shield craft jettisons the payload/heat-shield decoupler and
        strands the crew pod. Call mj_disable("staging") before in-space burns on such craft so the explicit
        guarded staging loop is the SOLE stager. "all" includes staging."""
        return self._request("POST", "/mj-disable", json={"which": which})

    def mj_status(self) -> dict:
        return self._request("GET", "/mj-status")

    def mj_stage_stats(self) -> dict:
        """Return MechJeb's latest fuel-flow stage simulation for the active vessel."""
        return self._request("GET", "/mj-stage-stats")

    def mj_plan(self, target: str = "Duna", operation: str = "interplanetary") -> dict:
        """Plan a maneuver node with MechJeb's maneuver planner on the ACTIVE vessel — the
        interplanetary transfer computes the precise ejection ANGLE + timing (the thing a hand-rolled
        prograde burn can't, which is why comsats missed Duna). Sets the target body and places the
        node; then call mj_execute_node to fly it. operation: interplanetary | circularize | plane."""
        return self._request("POST", "/mj-plan", json={"target": target, "operation": operation})

    def transfer_crew(self, to_vessel: str = "") -> dict:
        payload = {"toVessel": to_vessel} if to_vessel else {}
        return self._request("POST", "/transfer-crew", json=payload)

    def spawn_crew(self, vessel: str = "") -> dict:
        """Seat a roster kerbal into the first empty crewable seat (a headless launch leaves crewed
        pods empty). ``vessel`` (optional) targets a craft by a substring of its name; empty = the
        active vessel. The bridge returns the spawned kerbal's name on success."""
        payload = {"vessel": vessel} if vessel else {}
        return self._request("POST", "/spawn-crew", json=payload)

    def eva_flag(self, crew: str = "") -> dict:
        """Put a seated kerbal on EVA on the LANDED/SPLASHED active vessel and plant the stock flag
        headlessly. ``crew`` (optional) names the kerbal by exact name; empty = the first seated crew
        member found. The bridge tries ``KerbalEVA.PlantFlag()`` first, then falls back to invoking the
        part's "flag" BaseEvent, and reports which path fired (``flagMethod``/``flagDetail``) plus the
        landed body/biome/lat/lon. The flag actually appearing must still be verified in-game."""
        payload = {"crew": crew} if crew else {}
        return self._request("POST", "/eva-flag", json=payload)

    def eva_go(self, crew: str = "", vessel: str = "") -> dict:
        """Put a seated kerbal on EVA on a LANDED/SPLASHED vessel WITHOUT planting a flag (walk to a
        ladder, take surface science, set up a board). ``crew`` (optional) names the kerbal by exact
        name; empty = the first seated crew member found. ``vessel`` (optional) targets a craft by a
        substring of its name; empty = the active vessel. The bridge returns the new EVA vessel name
        plus the landed body/biome/lat/lon. The kerbal stays on EVA; call ``eva_board`` to re-board."""
        payload: dict = {}
        if crew:
            payload["crew"] = crew
        if vessel:
            payload["vessel"] = vessel
        return self._request("POST", "/eva-go", json=payload)

    def eva_walk_to(
        self,
        lat: float | None = None,
        lon: float | None = None,
        bearing: float | None = None,
        distance: float | None = None,
        crew: str = "",
    ) -> dict:
        """Drive the active (or named) EVA kerbal toward a surface target with PRECISE, calculated
        movement. Pass either an absolute target ``lat``/``lon`` OR a ``bearing`` (deg, clockwise from
        north) + ``distance`` (m) relative to the kerbal's current position. The bridge computes the
        world-space point from the body's own geodesy (``CelestialBody.GetWorldSurfacePosition`` at the
        terrain altitude there) and hands it to ``KerbalEVA.SetWaypoint`` — the stock engine does the
        pathing, so the move is error-free by construction. Returns the resolved target lat/lon, the
        great-circle distance/bearing, and the body radius used. Arrival must still be VERIFIED in-game.

        Exactly one of {lat & lon} or {bearing & distance} must be supplied."""
        has_latlon = lat is not None and lon is not None
        has_vector = bearing is not None and distance is not None
        if has_latlon == has_vector:
            raise BridgeError("eva_walk_to: pass exactly one of {lat,lon} or {bearing,distance}.")
        payload: dict[str, str] = {}
        if has_latlon:
            payload["lat"] = str(lat)
            payload["lon"] = str(lon)
        else:
            payload["bearing"] = str(bearing)
            payload["distance"] = str(distance)
        if crew:
            payload["crew"] = crew
        return self._request("POST", "/eva-walk-to", json=payload)

    def eva_status(self) -> dict:
        """Read the active (or first) EVA kerbal's state: body, lat/lon/alt, surface velocity,
        landed/splashed, on-ladder, jetpack, EVA propellant fuel/capacity, and the FSM state name."""
        return self._request("GET", "/eva-status")

    def crew_list(self) -> dict:
        """Every kerbal currently in the loaded flight scene: name, type, trait, level, the vessel +
        part they occupy, seat index, and whether they are on EVA."""
        return self._request("GET", "/crew-list")

    def crew_roster(self) -> dict:
        """The whole game roster from ``HighLogic.CurrentGame.CrewRoster`` (Available/Assigned/Dead/…)
        plus Available/Assigned/KIA/Missing counts."""
        return self._request("GET", "/crew-roster")

    def vessel_info(self, vessel: str = "") -> dict:
        """Mass (total/dry/resource, tonnes), part/stage counts, current stage, crew count/capacity, and
        aggregated per-resource amount/maxAmount/density for ``vessel`` (substring; empty = active). If the
        ACTIVE vessel has a MechJeb core, the vacuum total Δv is included. Lets the planner reason over the
        SAME numbers the game uses."""
        payload = {"vessel": vessel} if vessel else {}
        return self._request("POST", "/vessel-info", json=payload)

    def parts_list(self, vessel: str = "") -> dict:
        """Every part on ``vessel`` (substring; empty = active): name, title, dry/resource mass (tonnes),
        inverse stage, crew/capacity, and the module class names on it."""
        payload = {"vessel": vessel} if vessel else {}
        return self._request("POST", "/parts-list", json=payload)

    def resources(self, vessel: str = "") -> dict:
        """Aggregated per-resource amount/maxAmount/density/mass across ``vessel`` (substring; empty =
        active). A focused, lighter alternative to ``vessel_info`` for fuel/EC budgeting."""
        payload = {"vessel": vessel} if vessel else {}
        return self._request("POST", "/resources", json=payload)

    def part_database(self) -> dict:
        """The GAME's authoritative loaded part catalog from ``PartLoader.LoadedPartsList`` — post-load,
        module-PROCESSED truth (what KSP actually instantiated after cfg parse + patches + variant
        resolution), so it catches anything the lab's raw cfg parse (``data/stock_parts.json``) missed.

        Returns ``{"ok": True, "count": N, "parts": [ ... ]}`` where each part is::

            {"name", "title", "category", "bulkhead", "crewCapacity", "dryMassT",
             "maxThrustKn"?, "ispVacS"?, "ispAslS"?, "resources": {"ResourceName": maxAmount, ...}}

        ``maxThrustKn``/``ispVacS``/``ispAslS`` are present only for parts with a ModuleEngines(FX).
        Used by ``tools/validate_parts_live.py`` to cross-check the materialized catalog against the
        live game. Available from any scene (the part DB loads at game start). Read-only.

        NOTE: raises ``BridgeError`` (wrapping HTTP 404) if the running bridge DLL predates this endpoint —
        the consolidated DLL must be installed and KSP restarted for it to answer."""
        return self._request("GET", "/part-database")

    def eva_board(self, crew: str = "") -> dict:
        """Re-board the active (or named) EVA kerbal into the nearest crewable part with a free seat —
        closes the loop after ``eva_go``/``eva_flag``. ``crew`` (optional) selects the EVA kerbal by
        name (matched against the EVA vessel/kerbal name); empty = the first EVA kerbal found. "Nearest"
        is the smallest world-space distance to a candidate part across the loaded vessels. The bridge
        returns the boarded part/vessel and the distance travelled (``distanceM``)."""
        payload = {"crew": crew} if crew else {}
        return self._request("POST", "/eva-board", json=payload)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = self.base_url.rstrip("/") + path
        if "json" in kwargs:
            payload = kwargs.pop("json")
            kwargs["data"] = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("Content-Type", "application/json; charset=utf-8")
            kwargs["headers"] = headers
        try:
            response = requests.request(method, url, timeout=self.timeout_s, **kwargs)
        except requests.RequestException as exc:
            raise BridgeError(f"KSP bridge request failed: {method} {url}: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise BridgeError(f"KSP bridge returned non-JSON response from {url}") from exc
        if response.status_code >= 400:
            error = data.get("error", response.text)
            raise BridgeError(f"KSP bridge request failed: {method} {url}: HTTP {response.status_code}: {error}")
        if not data.get("ok", False):
            raise BridgeError(data.get("error", f"Bridge command failed: {data}"))
        return data
