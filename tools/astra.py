"""ASTRA command-line entry point.

    PYTHONPATH=src python tools/astra.py "land a relay in high Mun orbit and bring a crew home"

ASTRA is a GENERAL KSP1 agent: it DECOMPOSES the command into an ordered list of atomic, body-agnostic
PRIMITIVES (launch / transfer / land / plant_flag / dock / recover / ...) and executes them against one
live kRPC + bridge connection. It is no longer a fixed Mun-mission selector.

Options:
    --config PATH     kRPC/runner config (default: configs/local-ksp.yaml)
    --dry-run         decompose the command and print the primitive plan; do NOT fly
    --max-attempts N  retries per primitive step to absorb run-to-run variance (default 2)
    --no-llm          force the heuristic decomposer even if ANTHROPIC_API_KEY is set

ASTRA runs with zero configuration (heuristic decomposer). Set ANTHROPIC_API_KEY to let Claude do the
natural-language decomposition; set ASTRA_MODEL to choose the model (default claude-opus-4-8).
"""
from __future__ import annotations

import argparse
import sys

from ksp_lab.astra import AstraAgent
from ksp_lab.astra.interpreter import Interpreter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra", description="Autonomous KSP1 mission agent.")
    parser.add_argument("command", help="one line of natural language describing the mission")
    parser.add_argument("--config", default="configs/local-ksp.yaml")
    parser.add_argument("--dry-run", action="store_true", help="interpret only; do not fly")
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--no-llm", action="store_true", help="force heuristic interpreter")
    args = parser.parse_args(argv)

    agent = AstraAgent(
        args.config,
        interpreter=Interpreter(allow_llm=not args.no_llm),
        max_attempts=args.max_attempts,
        dry_run=args.dry_run,
    )
    result = agent.run(args.command)
    print("\n" + result.summary_text())
    return 0 if result.success else 2


if __name__ == "__main__":
    raise SystemExit(main())
