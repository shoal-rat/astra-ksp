"""Per-reflex context: the live vessel, frames, and streams scoped to one reflex run."""

from __future__ import annotations

from typing import Any, Callable

from astra.ksp import KSP
from astra.telemetry import Probe


class Ctx:
    """Holds what control laws need during one reflex run. Streams created through `stream()`
    are removed when the run ends or when the vessel/body changes."""

    def __init__(self, k: KSP, probe: Probe):
        self.k = k
        self.probe = probe
        self.conn = k.conn
        self.sc = k.sc
        self._streams: dict[str, Any] = {}
        self._owner: tuple | None = None
        self.vessel = k.vessel()
        self.notes: list[str] = []
        self._sync()

    def _sync(self) -> None:
        body = self.vessel.orbit.body
        owner = (self.vessel._object_id, body.name)
        if owner != self._owner:
            self.drop_streams()
            self._owner = owner
            self.body = body
            self.nrf = body.non_rotating_reference_frame
            self.brf = body.reference_frame
            self.srf = self.vessel.surface_reference_frame

    def refresh(self) -> None:
        """Re-acquire the active vessel (after staging/decoupling) and rebuild if it changed."""
        self.vessel = self.k.vessel()
        self._sync()

    def check_body(self, body_name: str) -> None:
        """Cheap per-tick check (the name comes from a stream): rebuild frames after an SOI change."""
        if self._owner is not None and body_name != self._owner[1]:
            self._sync()

    def stream(self, key: str, fn: Callable, *args: Any) -> Any:
        s = self._streams.get(key)
        if s is None:
            s = self.conn.add_stream(fn, *args)
            self._streams[key] = s
        return s()

    def drop_streams(self) -> None:
        for s in self._streams.values():
            try:
                s.remove()
            except Exception:  # noqa: BLE001
                pass
        self._streams.clear()

    def close(self) -> None:
        self.drop_streams()
