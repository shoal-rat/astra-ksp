"""Call a LOCAL coding-agent CLI (Claude Code or Codex) as ASTRA's mission-architect LLM — NO API key.

The directive: the task DECOMPOSITION must be done by an LLM, but ASTRA must not need a separate
``ANTHROPIC_API_KEY``. Both Claude Code (``claude``) and Codex (``codex``) are already signed in for the
human running the lab over the machine's own session, so ASTRA shells out to whichever is installed and
reads the model's strict-JSON plan off stdout. The system prompt + the mission goal go in on stdin.

``ASTRA_LLM_CLI=claude|codex`` forces a backend; otherwise both are tried (codex first — its binary
resolver is shared with the three-view review). A missing/failed CLI raises so the interpreter surfaces
``LLMUnavailableError`` rather than silently degrading (there is NO heuristic fallback by design).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable

from .codex_review import _resolve_codex_bin


def _resolve_claude_bin() -> str | None:
    """Find a directly-executable Claude Code CLI. On Windows ``claude`` is a .cmd/.ps1 shim subprocess
    cannot launch from the bare name, so probe the real .exe / explicit extensions too."""
    found = shutil.which("claude")
    if found and (os.name != "nt" or found.lower().endswith(".exe")):
        return found
    if os.name == "nt":
        for ext in (".exe", ".cmd", ".bat"):
            hit = shutil.which("claude" + ext)
            if hit:
                return hit
    return found


def _build_invocation(backend: str) -> tuple[str, list[str]] | None:
    """(executable, argv-tail) for a backend, or None if that CLI is not installed. Both read the prompt
    from STDIN and print the model's reply to stdout."""
    if backend == "codex":
        codex = _resolve_codex_bin("codex")
        # 'exec' = non-interactive one-shot; read-only sandbox; don't require a git repo.
        return codex, ["exec", "--sandbox", "read-only", "--skip-git-repo-check"]
    if backend == "claude":
        claude = _resolve_claude_bin()
        if not claude:
            return None
        # -p = print mode (one response, non-interactive); plain text out (the model emits the JSON).
        return claude, ["-p", "--output-format", "text"]
    return None


def call_llm_cli(
    system_prompt: str,
    user_command: str,
    *,
    backend: str | None = None,
    timeout_s: int = 300,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """Run the local LLM CLI on (system_prompt + goal) and return its raw stdout (the model's reply, which
    contains the strict-JSON plan — the caller extracts the JSON). Raises ``RuntimeError`` if no local CLI
    is available or every attempt fails."""
    chosen = (backend or os.environ.get("ASTRA_LLM_CLI") or "").strip().lower()
    order = [chosen] if chosen in ("codex", "claude") else ["codex", "claude"]

    prompt = f"{system_prompt}\n\n==================== MISSION GOAL ====================\n{user_command}\n"
    last_err = "no local LLM CLI found"
    for be in order:
        inv = _build_invocation(be)
        if inv is None:
            last_err = f"{be} CLI not installed"
            continue
        exe, tail = inv
        try:
            proc = runner(
                [exe, *tail],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                encoding="utf-8",
            )
        except FileNotFoundError:
            last_err = f"{be}: '{exe}' not launchable"
            continue
        except subprocess.TimeoutExpired:
            last_err = f"{be}: timed out after {timeout_s}s"
            continue
        except OSError as exc:
            last_err = f"{be}: {type(exc).__name__}: {exc}"
            continue
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out:
            return proc.stdout
        last_err = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()[:300]

    raise RuntimeError(f"local LLM CLI failed (tried {order}): {last_err}")
