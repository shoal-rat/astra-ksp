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

    def load_save(self, save_folder: str) -> dict:
        return self._request("POST", "/save/load", json={"saveFolder": save_folder})

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

    def mj_disable(self, which: str = "all") -> dict:
        return self._request("POST", "/mj-disable", json={"which": which})

    def mj_status(self) -> dict:
        return self._request("GET", "/mj-status")

    def transfer_crew(self, to_vessel: str = "") -> dict:
        payload = {"toVessel": to_vessel} if to_vessel else {}
        return self._request("POST", "/transfer-crew", json=payload)

    def spawn_crew(self) -> dict:
        return self._request("POST", "/spawn-crew", json={})

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
