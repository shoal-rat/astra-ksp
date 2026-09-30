"""Errors that tools raise. Every error carries a hint so the AI knows what to try next."""

from __future__ import annotations


class AstraError(Exception):
    """A tool-level failure the AI should read and react to."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message}\nHINT: {self.hint}" if self.hint else self.message


class NotConnected(AstraError):
    """kRPC or the bridge is unreachable."""


class WrongScene(AstraError):
    """The game is in a scene where this tool cannot act."""


class NoVessel(AstraError):
    """There is no active vessel to act on."""


class BridgeError(AstraError):
    """The KSP bridge plugin returned an error."""
