"""MCP server: exposes every registered tool to an AI flight crew over stdio.

Run with ``astra serve`` (Claude Code picks it up from .mcp.json). Each tool runs in a worker
thread under the registry's game lock, so long reflexes never block the protocol loop and two
commands never fight over the controls.
"""

from __future__ import annotations

import functools
import inspect
import os
from typing import Any

import anyio.to_thread
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from astra import registry
from astra.errors import AstraError
from astra.registry import Picture, ToolSpec

INSTRUCTIONS = """\
You are the flight crew and mission control for a live Kerbal Space Program game. These tools are
your instruments, slide rule, engineering desk, and controls. Nothing here flies a mission for you:
you observe, compute, decide, command, and verify, one step at a time.

Start every session with game_status and journal_read_doctrine. Compute numbers from live data with
the compute_* tools instead of recalling them. While you deliberate the game stays paused; fly_*
tools advance time under the control you command and stop when a trigger you chose fires.
"""


def _to_content(result: Any) -> Any:
    if isinstance(result, Picture):
        img = Image(path=result.path) if result.path else Image(data=result.data, format=result.fmt)
        return [img, result.caption] if result.caption else img
    if isinstance(result, list) and any(isinstance(r, Picture) for r in result):
        out: list[Any] = []
        for r in result:
            out.extend(_to_content(r) if isinstance(r, Picture) else [registry.render_text(r)])
        return out
    return registry.render_text(result)


def _wrap(spec: ToolSpec):
    """An async function with the tool's exact signature that runs it in a thread."""

    async def runner(**kwargs: Any) -> Any:
        def work() -> Any:
            try:
                return _to_content(registry.call(spec.name, kwargs))
            except AstraError as exc:
                raise ToolError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 — every failure must reach the model readable
                raise ToolError(f"{type(exc).__name__}: {exc}\nHINT: check the arguments against the "
                                "tool description; call game_status if the game state may have changed."
                                ) from exc

        return await anyio.to_thread.run_sync(work)

    functools.update_wrapper(runner, spec.fn)
    del runner.__wrapped__  # keep our signature below, not the sync one via __wrapped__
    params = list(spec.signature.parameters.values())
    runner.__signature__ = spec.signature.replace(parameters=params, return_annotation=Any)
    runner.__annotations__ = {p.name: p.annotation for p in params
                              if p.annotation is not inspect.Parameter.empty} | {"return": Any}
    return runner


def build_server() -> MCPServer:
    from astra import journal, media  # the flight recorder; video marks when a recording runs

    journal.install_recorder()
    media.install_marks()
    server = MCPServer("astra", instructions=INSTRUCTIONS)
    for spec in registry.load_all().values():
        doc = inspect.getdoc(spec.fn) or spec.summary
        server.add_tool(_wrap(spec), name=spec.name, description=f"[{spec.group}] {doc}",
                        structured_output=False)
    return server


DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = int(os.environ.get("ASTRA_DAEMON_PORT", "48600"))
DAEMON_URL = os.environ.get("ASTRA_DAEMON_URL", f"http://{DAEMON_HOST}:{DAEMON_PORT}/mcp")


def main(http: bool = False, port: int | None = None) -> None:
    """stdio for an MCP client that spawns us; http for a long-lived daemon that owns the kRPC
    connection (kRPC ties autopilot and control inputs to the client connection that set them)."""
    server = build_server()
    if http:
        server.run("streamable-http", host=DAEMON_HOST, port=port or DAEMON_PORT)
    else:
        server.run("stdio")
