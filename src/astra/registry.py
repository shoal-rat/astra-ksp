"""The tool registry.

A tool is a plain synchronous Python function with typed (``Annotated``) parameters and a docstring.
Registering it here makes it available three ways, with the same schema and behavior:

* over MCP (``astra serve``) to Claude Code, Claude Desktop, the Agent SDK runner, or any MCP client;
* on the command line (``astra call <tool> key=value ...``) for humans and non-MCP agents;
* in-process (``registry.invoke``) for tests.

Tools return JSON-able data (dicts/lists/numbers/strings) or a :class:`Picture`. Floats are rounded
to readable precision before they reach the model. Tools report problems by raising
:class:`astra.errors.AstraError` with a hint.
"""

from __future__ import annotations

import enum
import inspect
import json
import math
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

GROUPS: dict[str, str] = {
    "observe": "Instruments: telemetry, vessel structure, stages, bodies, targets, and a camera.",
    "compute": "Slide rule: orbital mechanics, rocket equation, transfer windows, free-form math.",
    "design": "Engineering desk: part catalog, stack analysis, craft building.",
    "game": "Game director: scenes, saves/checkpoints, launch, recover, switch vessel, pause.",
    "control": "Direct controls: throttle, staging, SAS/RCS, attitude, action groups, parts, maneuver nodes.",
    "fly": "Reflexes: run the sim under commanded control until a trigger fires; burns; warp; descent.",
    "autopilot": "Delegate to MechJeb autopilots with parameters you choose.",
    "crew": "Kerbals: roster, EVA, flags, boarding, crew transfer.",
    "journal": "Mission log, decisions, lessons learned, and flight-doctrine playbooks.",
}


@dataclass
class Picture:
    """An image result (e.g. a camera frame). ``caption`` is returned as text alongside it."""

    path: Path | None = None
    data: bytes | None = None
    fmt: str = "png"
    caption: str = ""


@dataclass
class ToolSpec:
    name: str
    group: str
    fn: Callable[..., Any]
    summary: str
    needs_game: bool
    signature: inspect.Signature = field(repr=False)


TOOLS: dict[str, ToolSpec] = {}

# Serializes every tool that touches the game: one command at a time, like one pair of hands.
GAME_LOCK = threading.RLock()

# Observers of every invocation: fn(name, args, ok, result_or_error, seconds). Used by the flight recorder.
_HOOKS: list[Callable[[str, dict, bool, Any, float], None]] = []
_START_HOOKS: list[Callable[[str, dict], None]] = []


def tool(group: str, *, name: str | None = None, needs_game: bool = True):
    """Register a function as a tool in ``group``."""
    if group not in GROUPS:
        raise ValueError(f"unknown tool group {group!r}")

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or fn.__name__
        if tool_name in TOOLS:
            raise ValueError(f"duplicate tool name {tool_name!r}")
        doc = inspect.getdoc(fn) or ""
        TOOLS[tool_name] = ToolSpec(
            name=tool_name,
            group=group,
            fn=fn,
            summary=doc.split("\n\n", 1)[0].replace("\n", " ").strip(),
            needs_game=needs_game,
            signature=inspect.signature(fn, eval_str=True),
        )
        return fn

    return deco


def add_hook(hook: Callable[[str, dict, bool, Any, float], None]) -> None:
    """Call hook(name, args, ok, result_or_exception, seconds) after every tool call."""
    _HOOKS.append(hook)


def add_start_hook(hook: Callable[[str, dict], None]) -> None:
    """Call hook(name, args) before every tool call."""
    _START_HOOKS.append(hook)


def load_all() -> dict[str, ToolSpec]:
    """Import every tool module so its @tool decorators run."""
    from astra import tools  # noqa: F401  (the package imports its modules)

    return TOOLS


# ---------------------------------------------------------------------------------------------
# Result shaping


def _round(x: float) -> float | int | None:
    if math.isnan(x) or math.isinf(x):
        return None  # JSON has no NaN/inf; tools explain what an absent value means
    if x == 0:
        return 0
    mag = int(math.floor(math.log10(abs(x))))
    return round(x, max(1, 5 - mag))  # ~6 significant digits, never coarser than 0.1


def to_jsonable(obj: Any) -> Any:
    """Convert a tool result into compact JSON-able data with readable float precision."""
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, float):
        return _round(obj)
    if isinstance(obj, enum.Enum):
        return obj.name
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(getattr(obj, k)) for k in obj.__dataclass_fields__}
    # kRPC enums look like "VesselSituation.orbiting"
    text = str(obj)
    return text.split(".", 1)[1] if text.count(".") == 1 and " " not in text else text


def render_text(result: Any) -> str:
    return json.dumps(to_jsonable(result), ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------------------------
# Invocation


def call(name: str, args: dict[str, Any]) -> Any:
    """Run a tool by name with already-validated keyword arguments. Raises on failure."""
    spec = TOOLS.get(name)
    if spec is None:
        raise KeyError(f"no tool named {name!r}")
    for hook in _START_HOOKS:
        try:
            hook(name, args)
        except Exception:  # noqa: BLE001 — a broken observer must never break flight
            traceback.print_exc()
    t0 = time.monotonic()
    try:
        if spec.needs_game:
            with GAME_LOCK:
                result = spec.fn(**args)
        else:
            result = spec.fn(**args)
    except Exception as exc:  # noqa: BLE001 — reported to hooks, then re-raised
        _notify(name, args, False, exc, time.monotonic() - t0)
        raise
    _notify(name, args, True, result, time.monotonic() - t0)
    return result


def _notify(name: str, args: dict, ok: bool, payload: Any, seconds: float) -> None:
    for hook in _HOOKS:
        try:
            hook(name, args, ok, payload, seconds)
        except Exception:  # noqa: BLE001 — a broken recorder must never break flight
            traceback.print_exc()


def arg_model(spec: ToolSpec):
    """A pydantic model of the tool's parameters (the same validation MCP applies)."""
    from pydantic import create_model

    fields: dict[str, Any] = {}
    for p in spec.signature.parameters.values():
        ann = Any if p.annotation is inspect.Parameter.empty else p.annotation
        fields[p.name] = (ann, ... if p.default is inspect.Parameter.empty else p.default)
    return create_model(f"{spec.name}_args", **fields)


def validate_and_call(name: str, raw_args: dict[str, Any]) -> Any:
    """Coerce loosely-typed args (e.g. from the CLI) against the tool signature, then call it."""
    spec = TOOLS.get(name)
    if spec is None:
        raise KeyError(f"no tool named {name!r}; run `astra tools` to list them")
    unknown = set(raw_args) - set(spec.signature.parameters)
    if unknown:
        raise TypeError(f"{name}: unknown argument(s) {sorted(unknown)}; "
                        f"expected {list(spec.signature.parameters)}")
    model = arg_model(spec).model_validate(raw_args)
    return call(name, {k: getattr(model, k) for k in spec.signature.parameters})
