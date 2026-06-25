"""ASTRA command-line entry point.

    PYTHONPATH=src python tools/astra.py "land a relay in high Mun orbit and bring a crew home"

ASTRA is a GENERAL KSP1 agent: it DECOMPOSES the command into an ordered list of atomic, body-agnostic
PRIMITIVES (launch / transfer / land / plant_flag / dock / recover / ...) and executes them against one
live kRPC + bridge connection. It is no longer a fixed Mun-mission selector.

The decomposition is done by the Claude mission-architect — there is NO offline/heuristic fallback.
ANTHROPIC_API_KEY MUST be set; without it (or if the Claude call fails) ASTRA raises rather than
silently degrading to a keyword guesser. Set ASTRA_MODEL to choose the model (default claude-opus-4-8).

Options:
    --config PATH     kRPC/runner config (default: configs/local-ksp.yaml)
    --dry-run         decompose the command (still via the LLM) and print the primitive plan; do NOT fly
    --max-attempts N  retries per primitive step to absorb run-to-run variance (default 2)
"""
from __future__ import annotations

import argparse
import sys

from ksp_lab.astra import AstraAgent
from ksp_lab.astra.interpreter import Interpreter, LLMUnavailableError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra", description="Autonomous KSP1 mission agent.")
    parser.add_argument("command", help="one line of natural language describing the mission")
    parser.add_argument("--config", default="configs/local-ksp.yaml")
    parser.add_argument("--dry-run", action="store_true", help="interpret only (still via the LLM); do not fly")
    parser.add_argument("--max-attempts", type=int, default=2)
    args = parser.parse_args(argv)

    agent = AstraAgent(
        args.config,
        interpreter=Interpreter(),
        max_attempts=args.max_attempts,
        dry_run=args.dry_run,
    )
    try:
        result = agent.run(args.command)
    except LLMUnavailableError as exc:
        print(f"ASTRA: {exc}", file=sys.stderr)
        return 3
    print("\n" + result.summary_text())
    return 0 if result.success else 2


if __name__ == "__main__":
    raise SystemExit(main())
