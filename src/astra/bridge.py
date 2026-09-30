"""HTTP client for the KspAutomationBridge plugin (csharp/KspAutomationBridge).

The bridge covers what kRPC cannot: MechJeb autopilots, EVA and flags, crew seating, the loaded part
database, scene changes from the main menu, pausing without the menu, and camera frames. Request
bodies are JSON objects; parameters may be native values or strings.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from astra.config import CONFIG
from astra.errors import BridgeError, NotConnected


class Bridge:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        self.base_url = (base_url or CONFIG.bridge_url).rstrip("/")
        host = urlparse(self.base_url).hostname
        if host not in {"127.0.0.1", "localhost"}:
            raise BridgeError("refusing to talk to a non-local bridge", "set ASTRA_BRIDGE_URL to a localhost URL")
        self.timeout = timeout

    def up(self) -> bool:
        u = urlparse(self.base_url)
        try:
            with socket.create_connection((u.hostname, u.port or 80), timeout=1.5):
                return True
        except OSError:
            return False

    def get(self, path: str, timeout: float | None = None) -> dict:
        return self._request("GET", path, None, timeout)

    def post(self, path: str, body: dict[str, Any] | None = None, timeout: float | None = None) -> dict:
        payload = {k: v for k, v in (body or {}).items() if v is not None}
        return self._request("POST", path, payload, timeout)

    def _request(self, method: str, path: str, payload: dict | None, timeout: float | None) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as exc:
            raise NotConnected(
                f"KSP bridge unreachable at {self.base_url} ({exc})",
                "Is KSP running with GameData/KspAutomationBridge installed? Use game_status / "
                "`astra up` to start the game.") from exc
        try:
            result = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise BridgeError(f"bridge returned non-JSON for {path}: {body[:200]}") from exc
        if not result.get("ok", False):
            raise BridgeError(f"bridge {path} failed: {result.get('error', 'unknown error')}", result.get("hint"))
        result.pop("ok", None)
        return result
