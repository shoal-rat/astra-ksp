"""Locations and endpoints. Everything can be overridden with ASTRA_* environment variables."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KSP_DIR = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Kerbal Space Program")


def _running_ksp_dir() -> Path | None:
    """Return the install dir of a running KSP_x64.exe, if any (Windows only)."""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process KSP_x64 -ErrorAction SilentlyContinue | Select-Object -First 1).Path"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out).parent if out else None


def _ksp_dir() -> Path:
    env = os.environ.get("ASTRA_KSP_DIR")
    if env:
        return Path(env)
    if DEFAULT_KSP_DIR.exists():
        return DEFAULT_KSP_DIR
    return _running_ksp_dir() or DEFAULT_KSP_DIR


@dataclass(frozen=True)
class Config:
    ksp_dir: Path = field(default_factory=_ksp_dir)
    krpc_host: str = field(default_factory=lambda: os.environ.get("ASTRA_KRPC_HOST", "127.0.0.1"))
    krpc_rpc_port: int = field(default_factory=lambda: int(os.environ.get("ASTRA_KRPC_RPC_PORT", "50000")))
    krpc_stream_port: int = field(default_factory=lambda: int(os.environ.get("ASTRA_KRPC_STREAM_PORT", "50001")))
    bridge_url: str = field(default_factory=lambda: os.environ.get("ASTRA_BRIDGE_URL", "http://127.0.0.1:48500"))
    missions_dir: Path = field(default_factory=lambda: Path(os.environ.get("ASTRA_MISSIONS_DIR", REPO_ROOT / "missions")))
    knowledge_dir: Path = field(default_factory=lambda: Path(os.environ.get("ASTRA_KNOWLEDGE_DIR", REPO_ROOT / "knowledge")))
    cache_dir: Path = field(default_factory=lambda: Path(os.environ.get("ASTRA_CACHE_DIR", REPO_ROOT / ".cache")))
    # Pause the game whenever control returns to the AI, so deliberation costs zero game time.
    pause_between_commands: bool = field(
        default_factory=lambda: os.environ.get("ASTRA_PAUSE_BETWEEN_COMMANDS", "1") not in ("0", "false", "no"))

    @property
    def saves_dir(self) -> Path:
        return self.ksp_dir / "saves"

    @property
    def gamedata_dir(self) -> Path:
        return self.ksp_dir / "GameData"


CONFIG = Config()
