"""Bring the game up (``astra up``) and build/install the bridge plugin (``astra bridge``)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from astra.bridge import Bridge
from astra.config import CONFIG, REPO_ROOT

STEAM_APP_ID = "220200"
BRIDGE_SRC = REPO_ROOT / "csharp" / "KspAutomationBridge"
BRIDGE_OUT = REPO_ROOT / "csharp" / "build" / "KspAutomationBridge.dll"


def _log(msg: str) -> None:
    print(f"[astra {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def ksp_running() -> bool:
    if os.name != "nt":
        return False
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq KSP_x64.exe"], capture_output=True, text=True).stdout
    return "KSP_x64.exe" in out


def start_ksp() -> None:
    exe = CONFIG.ksp_dir / "KSP_x64.exe"
    if not exe.exists():
        raise SystemExit(f"KSP not found at {exe}; set ASTRA_KSP_DIR")
    _log(f"starting {exe}")
    # Steam games must start with their install dir as the working directory.
    subprocess.Popen([str(exe)], cwd=str(CONFIG.ksp_dir), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def bring_up(save: str | None = None, scene: str = "spacecenter", timeout_s: int = 600) -> int:
    save = save or os.environ.get("ASTRA_SAVE", "astra")
    deadline = time.monotonic() + timeout_s
    if not ksp_running():
        start_ksp()
    bridge = Bridge()
    _log("waiting for the bridge (KSP loading takes a few minutes)...")
    while not bridge.up():
        if time.monotonic() > deadline:
            _log("bridge never came up; is GameData/KspAutomationBridge installed? (astra bridge install)")
            return 1
        time.sleep(5)
    if _port_open(CONFIG.krpc_rpc_port):
        _log("kRPC already serving; a save is loaded")
        return 0
    # The bridge accepts connections during loading but load-save needs the main menu to be ready.
    while time.monotonic() < deadline:
        try:
            res = bridge.post("/load-save", {"saveFolder": save, "scene": scene}, timeout=180)
            _log(f"loaded save '{save}': {res}")
            break
        except Exception as exc:  # noqa: BLE001 — main menu may not be ready yet
            _log(f"load-save not ready yet ({exc}); retrying")
            time.sleep(8)
    while not _port_open(CONFIG.krpc_rpc_port):
        if time.monotonic() > deadline:
            _log("kRPC did not start; check the kRPC mod settings (autoStartServers)")
            return 1
        time.sleep(3)
    _log("ready: kRPC and bridge are up")
    return 0


# -- bridge build ------------------------------------------------------------------------------


def _csc() -> Path:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for cand in (windir / r"Microsoft.NET\Framework64\v4.0.30319\csc.exe",
                 windir / r"Microsoft.NET\Framework\v4.0.30319\csc.exe"):
        if cand.exists():
            return cand
    raise SystemExit("no .NET Framework csc.exe found (the bridge targets C# 5 / .NET 4.x)")


def build_bridge(install: bool = False) -> int:
    managed = CONFIG.ksp_dir / "KSP_x64_Data" / "Managed"
    mj = CONFIG.gamedata_dir / "MechJeb2" / "Plugins"
    refs = [managed / "Assembly-CSharp.dll", managed / "Assembly-CSharp-firstpass.dll"]
    refs += sorted(managed.glob("UnityEngine*.dll"))
    refs += [p for p in (mj / "MechJeb2.dll", mj / "MechJebLib.dll") if p.exists()]
    sources = sorted(BRIDGE_SRC.glob("*.cs")) + sorted((BRIDGE_SRC / "Properties").glob("*.cs"))
    BRIDGE_OUT.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(_csc()), "-target:library", "-nologo", "-optimize+", "-nowarn:1701,1702",
           f"-out:{BRIDGE_OUT}"] + [f"-reference:{r}" for r in refs] + [str(s) for s in sources]
    _log(f"compiling {len(sources)} source file(s) with {len(refs)} references")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        print(out, file=sys.stderr)
        return proc.returncode
    if out:
        print(out)
    _log(f"built {BRIDGE_OUT}")
    if not install:
        return 0
    target = CONFIG.gamedata_dir / "KspAutomationBridge" / "Plugins"
    target.mkdir(parents=True, exist_ok=True)
    dst = target / "KspAutomationBridge.dll"
    if dst.exists():
        shutil.copy2(dst, target / "KspAutomationBridge.dll.prev")
    try:
        shutil.copy2(BRIDGE_OUT, dst)
    except PermissionError:
        _log("the installed DLL is locked because KSP is running; quit KSP, then run `astra bridge install`")
        return 3
    cfg = BRIDGE_SRC / "MechJebForAll.cfg"
    if cfg.exists():
        shutil.copy2(cfg, target.parent / "MechJebForAll.cfg")
    _log(f"installed to {dst} (restart KSP to load it)")
    return 0


# -- clean saves ---------------------------------------------------------------------------------


def _strip_vessels(sfs_text: str) -> tuple[str, int]:
    """Remove every VESSEL node from FLIGHTSTATE, free assigned kerbals, and clear the active vessel."""
    lines = sfs_text.splitlines()
    out: list[str] = []
    removed = 0
    in_flightstate = False
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln == "\tFLIGHTSTATE":
            in_flightstate = True
        elif ln.startswith("\t") and not ln.startswith("\t\t") and ln.strip() not in ("{", "}"):
            in_flightstate = ln == "\tFLIGHTSTATE"
        if in_flightstate and ln == "\t\tVESSEL":
            j = i + 1
            while j < len(lines) and lines[j] != "\t\t}":
                j += 1
            i = j + 1
            removed += 1
            continue
        out.append(ln)
        i += 1
    text = "\n".join(out) + "\n"
    text = text.replace("\t\t\tstate = Assigned", "\t\t\tstate = Available")
    import re

    text = re.sub(r"(\n\t\tactiveVessel = )-?\d+", r"\g<1>-1", text)
    return text, removed


def new_save(name: str, source: str) -> int:
    """Create saves/<name> from saves/<source>: same game settings and roster, no vessels."""
    import re

    if not re.fullmatch(r"[^\/:*?\"<>|.][^\/:*?\"<>|]{0,79}", name):
        _log(f"invalid save name {name!r}")
        return 2
    src = CONFIG.saves_dir / source / "persistent.sfs"
    dst = CONFIG.saves_dir / name
    if not src.exists():
        _log(f"no save at {src}")
        return 2
    if dst.exists():
        _log(f"{dst} already exists; choose another name")
        return 2
    text, removed = _strip_vessels(src.read_text(encoding="utf-8"))
    (dst / "Ships" / "VAB").mkdir(parents=True)
    (dst / "Ships" / "SPH").mkdir(parents=True)
    (dst / "persistent.sfs").write_text(text, encoding="utf-8")
    _log(f"created save '{name}' from '{source}' ({removed} vessels removed, crew freed); "
         f"load it with `astra up --save {name}`")
    return 0
