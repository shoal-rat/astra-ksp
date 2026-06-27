"""ASTRA command-line entry point.

    PYTHONPATH=src python tools/astra.py "land a relay in high Mun orbit and bring a crew home"

ASTRA is a GENERAL KSP1 agent: it DECOMPOSES the command into an ordered list of atomic, body-agnostic
PRIMITIVES (launch / transfer / land / plant_flag / dock / recover / ...) and executes them against one
live kRPC + bridge connection. It is no longer a fixed Mun-mission selector.

ASTRA decomposes AUTONOMOUSLY. When ANTHROPIC_API_KEY is set the Claude mission-architect decomposes
(richest reasoning); when it is NOT set ASTRA uses its GENERAL body-agnostic planner (a single algorithm
that computes every step's parameters from the bodies table + physics for ANY destination — NOT a
per-mission script). Either way the agent plans, validates, flies, diagnoses and retries on its own.

Options:
    --config PATH     kRPC/runner config (default: configs/local-ksp.yaml)
    --dry-run         decompose the command and print the primitive plan; do NOT fly
    --max-attempts N  retries per primitive step to absorb run-to-run variance (default 2)
    --from-step N     RESUME: skip the first N-1 steps (assumed already flown) and fly from step N against
                      the live active vessel — re-attempt one leg without re-flying launch/transfer
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
    parser.add_argument("--dry-run", action="store_true", help="interpret only; do not fly")
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--from-step", type=int, default=1,
                        help="resume: fly from this 1-based step against the live vessel")
    args = parser.parse_args(argv)

    agent = AstraAgent(
        args.config,
        interpreter=Interpreter(),
        max_attempts=args.max_attempts,
        dry_run=args.dry_run,
    )
    try:
        result = agent.run(args.command, from_step=max(1, args.from_step))
    except LLMUnavailableError as exc:
        print(f"ASTRA: {exc}", file=sys.stderr)
        return 3
    print("\n" + result.summary_text())
    return 0 if result.success else 2


if __name__ == "__main__":
    raise SystemExit(main())
