"""``astra mission "<goal>"``: fly a mission unattended with a Claude Agent SDK crew.

The crew is a Claude Code session (the locally logged-in CLI, no API key) whose only instruments are
the astra MCP tools, plus two specialist subagents that share them. Every built-in tool except the
subagent launcher is disabled, so the crew cannot write, read, or run programs: each action is a tool
call it chose after looking at live state. The runner streams a compact log to the console, keeps
the full transcript (filed into the mission folder at the end), nudges the crew back to work if it
stops before mission_end, and leaves the game paused when it is interrupted.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import sys
import time
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from astra import __version__, journal
from astra.agent import prompts
from astra.config import CONFIG, REPO_ROOT

SERVER = "astra"
MCP_PREFIX = f"mcp__{SERVER}__"
# The only built-in tool the crew keeps is the subagent launcher. No Bash/Read/Write/Edit/Web*:
# the crew flies with instruments and controls, never with scripts.
BUILTIN_TOOLS = ["Agent"]
# The CLI has called the subagent launcher both "Agent" and "Task" (the init message of 2.1.x still
# lists "Task"); permission rules name both so neither spelling slips through or gets denied.
LAUNCHER_NAMES = ("Agent", "Task")
BUILTIN_SUBAGENTS = ("general-purpose", "Explore", "Plan", "statusline-setup", "claude")
DISALLOWED_TOOLS = ["Bash", "PowerShell", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob",
                    "Grep", "WebFetch", "WebSearch", "Skill", "Monitor", "CronCreate", "RemoteTrigger",
                    # Claude Code's built-in subagents: only the ASTRA specialists may be spawned, so no
                    # general-purpose helper ends up with the stick.
                    *(f"{tool}({kind})" for tool in LAUNCHER_NAMES for kind in BUILTIN_SUBAGENTS)]
# Long reflexes (a coast, a slow burn, a descent) must not be cut off by the MCP client's timeout.
MCP_TOOL_TIMEOUT_MS = "1800000"
# The astra schemas are ~30k tokens, above the CLI's threshold for deferring MCP tools behind its
# ToolSearch tool, which this crew does not have (tools=["Agent"]). Load every schema up front.
TOOL_SEARCH = "false"
# MCP server states in the session's init message that mean the crew has no instruments.
DEAD_SERVER = ("failed", "needs-auth", "disabled")
MAX_NUDGES = 3  # times the crew is sent back to work after stopping with the mission still open
# Set when `astra mission` runs inside another Claude Code session (e.g. from its terminal). They bind
# a CLI to that host session (its messaging socket, its token refresh); the crew's CLI must use its
# own login instead.
PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID",
                       "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_ENTRYPOINT",
                       "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                       "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "CLAUDE_PID")


def detach_from_parent_session() -> list[str]:
    """Drop variables that tie the crew's CLI to an enclosing Claude Code session."""
    dropped = [k for k in PARENT_SESSION_VARS if k in os.environ]
    for k in dropped:
        os.environ.pop(k, None)
    return dropped


def mcp_server_config() -> dict[str, Any]:
    """The astra MCP server as a stdio child of the Claude CLI, running in this interpreter."""
    env = {k: v for k, v in os.environ.items() if k.startswith("ASTRA_")}
    env["PYTHONIOENCODING"] = "utf-8"
    src = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = src + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    return {"type": "stdio", "command": sys.executable, "args": ["-m", "astra", "serve"], "env": env}


def build_options(goal: str, *, model: str = "claude-opus-5-5", max_turns: int | None = None,
                  effort: str | None = None, resume: str | None = None,
                  max_budget_usd: float | None = None):
    """The Agent SDK configuration for a crew flying `goal` (no query is started here).

    Permission mode is ``dontAsk``: anything not pre-approved is denied instead of prompting, which
    is what an unattended run needs, and the allow-list is exactly the astra MCP server plus the
    subagent launcher. ``setting_sources=[]`` keeps user/project settings, hooks, and plugins out of
    the crew's session; the doctrine arrives through the system prompt instead of CLAUDE.md.
    """
    from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

    agents = {
        name: AgentDefinition(description=spec.description, prompt=spec.prompt, tools=list(spec.tools),
                              model=spec.model)
        for name, spec in prompts.subagents().items()
    }
    return ClaudeAgentOptions(
        tools=list(BUILTIN_TOOLS),
        allowed_tools=[f"mcp__{SERVER}", *LAUNCHER_NAMES],
        disallowed_tools=list(DISALLOWED_TOOLS),
        system_prompt=prompts.system_prompt(goal),
        mcp_servers={SERVER: mcp_server_config()},
        strict_mcp_config=True,
        setting_sources=[],
        permission_mode="dontAsk",
        agents=agents,
        model=model,
        effort=effort,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        resume=resume,
        cwd=str(REPO_ROOT),
        env={"MCP_TOOL_TIMEOUT": os.environ.get("MCP_TOOL_TIMEOUT", MCP_TOOL_TIMEOUT_MS),
             "ENABLE_TOOL_SEARCH": os.environ.get("ENABLE_TOOL_SEARCH", TOOL_SEARCH),
             "PYTHONIOENCODING": "utf-8",
             "CLAUDE_AGENT_SDK_CLIENT_APP": f"astra/{__version__}"},
    )


# ---------------------------------------------------------------------------------------------
# console + transcript


def _short(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_label(name: str) -> str:
    return name[len(MCP_PREFIX):] if name.startswith(MCP_PREFIX) else name


def format_call(name: str, args: dict[str, Any], limit: int = 200) -> str:
    """``fly_until(until=[...], throttle=1)``: a one-line rendering of a tool call."""
    inner = ", ".join(f"{k}={_short(v, 70)}" for k, v in (args or {}).items())
    return f"{tool_label(name)}({_short(inner, limit)})"


def _result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
        elif isinstance(item, dict) and item.get("type") == "image":
            parts.append("[image]")
    return " ".join(parts)


class Console:
    """Compact live log: crew text, tool calls with short args, errors, subagent activity."""

    def __init__(self, out: TextIO | None = None):
        self.out = out or sys.stdout
        self.calls: dict[str, str] = {}      # tool_use_id -> tool name
        self.subagents: dict[str, str] = {}  # Agent tool_use_id -> subagent type
        self.paused_on_exit = False          # the game was already paused while the session was stopping

    def line(self, text: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {text}", file=self.out, flush=True)

    def show(self, msg: Any) -> None:
        from claude_agent_sdk import (AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolResultBlock,
                                      ToolUseBlock, UserMessage)

        if isinstance(msg, AssistantMessage):
            who = f"[{self.subagents.get(msg.parent_tool_use_id, 'subagent')}] " if msg.parent_tool_use_id else ""
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    self.line(f"{who}CREW  " + block.text.strip().replace("\n", "\n" + " " * 17))
                elif isinstance(block, ToolUseBlock):
                    self.calls[block.id] = block.name
                    if block.name in ("Agent", "Task"):
                        kind = str(block.input.get("subagent_type", "subagent"))
                        self.subagents[block.id] = kind
                        self.line(f"{who}  => {kind}: {_short(block.input.get('description', ''), 120)}")
                    else:
                        self.line(f"{who}  -> {format_call(block.name, block.input)}")
            if msg.error:
                self.line(f"{who}  !! assistant error: {msg.error}")
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            who = f"[{self.subagents.get(msg.parent_tool_use_id, 'subagent')}] " if msg.parent_tool_use_id else ""
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    name = tool_label(self.calls.get(block.tool_use_id, "tool"))
                    text = _result_text(block.content)
                    if block.is_error:
                        self.line(f"{who}  !! {name}: {_short(text, 300)}")
                    elif block.tool_use_id in self.subagents:
                        self.line(f"{who}  <= {self.subagents[block.tool_use_id]} reported: {_short(text, 200)}")
                    else:
                        self.line(f"{who}  <- {name}: {_short(text, 160)}")
        elif isinstance(msg, SystemMessage) and msg.subtype == "init":
            n_tools = len(_astra_tools(msg.data))
            self.line(f"session {msg.data.get('session_id')} · model {msg.data.get('model')} · "
                      f"astra server {_server_status(msg.data) or 'missing'} · {n_tools} astra tools")
        elif isinstance(msg, ResultMessage):
            cost = f"${msg.total_cost_usd:.2f}" if msg.total_cost_usd is not None else "n/a"
            self.line(f"crew stopped: {msg.subtype} · {msg.num_turns} turns · {cost} · "
                      f"{msg.duration_ms / 60000:.1f} min")


def _server_status(init: dict) -> str | None:
    for server in init.get("mcp_servers", []):
        if isinstance(server, dict) and server.get("name") == SERVER:
            return server.get("status")
    return None


def _astra_tools(init: dict) -> list[str]:
    return [t for t in init.get("tools", []) if str(t).startswith(MCP_PREFIX)]


def _jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        out = {"_type": type(obj).__name__}
        for f in fields(obj):
            if f.name == "signature":  # thinking signatures are opaque blobs
                continue
            out[f.name] = _jsonable(getattr(obj, f.name))
        return out
    if isinstance(obj, dict):
        if obj.get("type") == "image" and isinstance(obj.get("source"), dict):
            return {"type": "image", "bytes_base64": len(str(obj["source"].get("data", "")))}
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


class Transcript:
    """Every SDK message as one JSON line; filed into the mission folder when the run ends."""

    def __init__(self, goal: str):
        folder = journal.missions_dir() / "_transcripts"
        folder.mkdir(parents=True, exist_ok=True)
        self.stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = (re.sub(r"[^A-Za-z0-9]+", "-", goal).strip("-").lower() or "mission")[:40]
        self.path = folder / f"{self.stamp}-{slug}.jsonl"
        self._f = self.path.open("a", encoding="utf-8")
        self.mission: Path | None = None
        self.session_id: str | None = None

    def write(self, msg: Any) -> None:
        rec = {"t": round(time.time(), 2), **_jsonable(msg)}
        self._f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._f.flush()
        sid = getattr(msg, "session_id", None) or (getattr(msg, "data", None) or {}).get("session_id")
        if sid:
            self.session_id = sid

    def note_mission(self) -> None:
        current = journal.active_mission()
        if current is not None:
            self.mission = current

    def close(self) -> Path | None:
        """Close the file and copy it into the mission folder the crew flew (if any)."""
        self._f.close()
        if self.mission is None or not self.mission.is_dir():
            return None
        dest = self.mission / f"transcript-{self.stamp}.jsonl"
        shutil.copyfile(self.path, dest)
        if self.session_id:
            with (self.mission / "sessions.txt").open("a", encoding="utf-8") as f:
                f.write(f"{self.stamp} {self.session_id}\n")
        return dest


# ---------------------------------------------------------------------------------------------
# the run


class CrewUnavailable(RuntimeError):
    """The crew session started without its instruments (astra MCP server not connected)."""


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _bridge_port() -> int:
    from urllib.parse import urlparse

    return urlparse(CONFIG.bridge_url).port or 80


def game_reachable() -> tuple[bool, bool]:
    """(kRPC serving, bridge listening) — checked with plain sockets, no game calls."""
    return _port_open(CONFIG.krpc_host, CONFIG.krpc_rpc_port), _port_open("127.0.0.1", _bridge_port())


def leave_game_paused() -> str:
    """Pause the game if it is in flight; report what happened. Never raises."""
    if not _port_open(CONFIG.krpc_host, CONFIG.krpc_rpc_port):
        return "game not reachable (nothing to pause)"
    try:
        from astra.ksp import ksp

        k = ksp()
        scene = k.scene()
        if scene != "flight":
            return f"game in {scene}: nothing to pause"
        k.set_paused(True)
        return "game paused"
    except Exception as exc:  # noqa: BLE001
        return f"could not pause the game ({exc})"


def _check_init(msg: Any) -> None:
    """Refuse to fly when the session reports the astra server dead or connected without tools.

    A server still 'pending' is not fatal: its tools may arrive after the init message.
    """
    from claude_agent_sdk import SystemMessage

    if not (isinstance(msg, SystemMessage) and msg.subtype == "init"):
        return
    status = _server_status(msg.data)
    if status in DEAD_SERVER:
        raise CrewUnavailable(f"the astra MCP server is '{status}'; run `astra tools` to see why it fails to start")
    if status in (None, "connected") and "tools" in msg.data and not _astra_tools(msg.data):
        raise CrewUnavailable("the crew session has no astra tools; run `astra tools` to check the registry")


async def _fly(goal: str, options: Any, first_prompt: str, max_turns: int | None,
               max_budget_usd: float | None, console: Console, transcript: Transcript) -> tuple[int, Any]:
    """Run the crew session; send it back to work while the mission is open. Returns (code, last result).

    On Ctrl+C (asyncio cancels this task) the game is paused *before* the session closes: closing it
    ends the MCP server, whose kRPC connection holds the throttle and autopilot mid-reflex.
    """
    from claude_agent_sdk import ClaudeSDKClient

    async with ClaudeSDKClient(options=options) as client:
        try:
            return await _converse(client, goal, first_prompt, max_turns, max_budget_usd, console, transcript)
        except (asyncio.CancelledError, KeyboardInterrupt):
            console.line(await asyncio.to_thread(leave_game_paused))
            console.paused_on_exit = True
            raise


async def _converse(client: Any, goal: str, first_prompt: str, max_turns: int | None,
                    max_budget_usd: float | None, console: Console, transcript: Transcript) -> tuple[int, Any]:
    from claude_agent_sdk import AssistantMessage, ResultMessage, UserMessage

    last: ResultMessage | None = None
    turns = 0
    prompt = first_prompt
    for nudge in range(MAX_NUDGES + 1):
        await client.query(prompt)
        result, session_error = None, None
        async for msg in client.receive_response():
            transcript.write(msg)
            console.show(msg)
            _check_init(msg)
            if isinstance(msg, UserMessage):
                transcript.note_mission()
            elif isinstance(msg, AssistantMessage) and msg.error and msg.parent_tool_use_id is None:
                session_error = msg.error
            elif isinstance(msg, ResultMessage):
                result = last = msg
        transcript.note_mission()
        if result is None:
            return 1, last
        if session_error:
            login = session_error == "authentication_failed"
            console.line(f"the crew's session failed: {session_error}"
                         + (" (log in again by running `claude` in a terminal)" if login else ""))
            return 1, result
        turns += result.num_turns or 0
        if result.is_error or result.subtype != "success":
            return 1, result
        opened = transcript.mission is not None
        if opened and journal.active_mission() is None:
            return 0, result  # the crew closed its mission; the caller reads the outcome
        state = "still open" if opened else "not opened yet"
        over_budget = bool(max_budget_usd) and (result.total_cost_usd or 0.0) >= max_budget_usd
        if (max_turns and turns >= max_turns) or over_budget:
            console.line(f"mission {state}, but the turn or budget limit is reached")
            return 1, result
        if nudge < MAX_NUDGES:
            console.line(f"mission {state}; sending the crew back to work ({nudge + 1}/{MAX_NUDGES})")
            prompt = prompts.continue_message(goal) if opened else prompts.start_message(goal)
    console.line("the crew stopped repeatedly without closing the mission")
    return 1, last


def mission_outcome(mission: Path | None) -> str | None:
    """The status recorded in mission.json ('active', 'success', 'partial', 'failure', 'aborted')."""
    try:
        return json.loads((mission / "mission.json").read_text(encoding="utf-8")).get("status")
    except (TypeError, OSError, ValueError):
        return None


def run_mission(goal: str, model: str = "claude-opus-5-5", max_turns: int | None = None,
                effort: str | None = None, resume: str | None = None,
                max_budget_usd: float | None = None, record: str | None = None) -> int:
    """Fly `goal` unattended and return a process exit code.

    record: a folder to film the flight into (video, tool-call marks, camera director); "" for
    recordings/<timestamp>; None to not record.

    0: the crew closed the mission with outcome 'success'. 1: any other outcome, a mission left
    open, or a runner/session error. 2: setup problem (no goal, SDK or CLI missing). 3: KSP not
    running. 4: the crew's instruments (astra MCP server) failed to start. 130: interrupted.
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    console = Console()
    goal = goal.strip()
    if not goal:
        console.line("no goal given")
        return 2
    try:
        from claude_agent_sdk import CLINotFoundError
    except ImportError:
        console.line("claude-agent-sdk is not installed: pip install -e .[agent]")
        return 2

    detached = detach_from_parent_session()
    krpc_up, bridge_up = game_reachable()
    if not (krpc_up or bridge_up):
        console.line("KSP is not running (no kRPC, no bridge). Start it with `astra up`, then retry.")
        return 3
    console.line(f"ASTRA mission control · goal: {goal}")
    if detached:
        console.line(f"(running inside another Claude Code session: detached {len(detached)} session variables)")
    console.line(f"game: kRPC {'up' if krpc_up else 'down (no save loaded?)'} · "
                 f"bridge {'up' if bridge_up else 'down'} · model {model} · effort {effort or 'default'}"
                 + (f" · resuming {resume}" if resume else ""))
    active = journal.active_mission()
    if active is not None and not resume:
        console.line(f"note: mission '{active.name}' is still marked active from an earlier run")

    try:
        options = build_options(goal, model=model, max_turns=max_turns, effort=effort, resume=resume,
                                max_budget_usd=max_budget_usd)
    except (OSError, ValueError) as exc:  # doctrine or a subagent brief missing or malformed
        console.line(f"cannot brief the crew: {exc}")
        return 2
    transcript = Transcript(goal)
    first = prompts.resume_message(goal) if resume else prompts.first_message(goal)
    code, last = 1, None
    filming = None
    try:
        if record is not None:  # inside the try, so an interrupt while it starts still stops the recorder
            from astra import media

            folder = Path(record) if record else media.recordings_dir() / time.strftime("%Y%m%d-%H%M%S")
            filming = media.Session(folder)
            try:
                filming.__enter__()
                console.line(f"recording to {folder} ({filming.started.get('mode', '?')}, "
                             f"{filming.started.get('width')}x{filming.started.get('height')})")
                if filming.started.get("warning"):
                    console.line(f"recording: {filming.started['warning']}")
            except Exception as exc:  # noqa: BLE001 — a camera problem must not ground the crew
                console.line(f"recording unavailable: {exc}")
                filming = None
        code, last = asyncio.run(_fly(goal, options, first, max_turns, max_budget_usd, console, transcript))
    except KeyboardInterrupt:
        console.line("interrupted: crew stood down")
        code = 130
    except CrewUnavailable as exc:
        console.line(f"cannot fly: {exc}")
        code = 4
    except CLINotFoundError as exc:
        console.line(f"Claude Code CLI not found ({exc}); install it and log in with `claude`")
        code = 2
    except Exception as exc:  # noqa: BLE001 — report and still leave the game safe
        console.line(f"runner error: {type(exc).__name__}: {exc}")
        code = 1
    finally:
        try:
            if not console.paused_on_exit and (code != 0 or CONFIG.pause_between_commands):
                console.line(leave_game_paused())
            if filming is not None:
                try:
                    rec = filming.close()  # can take a minute while ffmpeg finishes the video
                except KeyboardInterrupt:
                    rec = {"error": "interrupted while the recorder stopped; check `astra record --status`"}
                if rec.get("error"):
                    console.line(f"recording: stop failed ({rec['error']})")
                else:
                    console.line(f"recording: {rec.get('frames')} frames -> {rec.get('output')}"
                                 + (f" ({rec['lastError']})" if rec.get("lastError") else ""))
        finally:  # the transcript is filed whatever happened to the pause or the recording
            mission = transcript.mission
            filed = transcript.close()
            console.line(f"transcript: {filed or transcript.path}")
            if transcript.session_id:
                console.line(f"session: {transcript.session_id} (continue with --resume {transcript.session_id})")
    if last is not None and last.result:
        console.line("final report:\n" + last.result.strip())
    if code == 0:
        outcome = mission_outcome(mission)
        console.line(f"mission {mission.name if mission else '(none opened)'}: outcome {outcome or 'unknown'}")
        code = 0 if outcome == "success" else 1
    return code
