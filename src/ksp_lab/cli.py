from __future__ import annotations

import argparse
import json
from pathlib import Path

from .bridge_client import BridgeClient
from .config import load_config
from .craft_writer import CraftWriter
from .mission import MissionPlanner
from .optimizer import HistoryOptimizer
from .parts import estimate_design
from .research import KnowledgeBase
from .runner import AutomationRunner
from .storage import TrialDatabase


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ksp-lab")
    parser.add_argument("--config", default="configs/default.yaml", help="Path to lab config YAML.")
    config_parent = argparse.ArgumentParser(add_help=False)
    config_parent.add_argument("--config", default=argparse.SUPPRESS, help="Path to lab config YAML.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", parents=[config_parent], help="Run closed-loop trials for a mission goal.")
    run_p.add_argument("--mission", required=True, help="Natural-language mission goal.")
    run_p.add_argument("--max-trials", type=int, default=None)
    run_p.add_argument("--offline", action="store_true", help="Use local surrogate instead of KSP/kRPC.")

    plan_p = sub.add_parser("plan", parents=[config_parent], help="Parse a mission and generate the first design.")
    plan_p.add_argument("--mission", required=True)

    craft_p = sub.add_parser("write-craft", parents=[config_parent], help="Write the first generated craft to a folder.")
    craft_p.add_argument("--mission", required=True)
    craft_p.add_argument("--output-dir", required=True)

    hist_p = sub.add_parser("history", parents=[config_parent], help="Show recent trial records.")
    hist_p.add_argument("--limit", type=int, default=20)

    kb_p = sub.add_parser("research", parents=[config_parent], help="Print the local research knowledge-base summary.")
    kb_p.add_argument("--format", choices=["text", "json"], default="text")

    bridge_p = sub.add_parser("bridge", parents=[config_parent], help="Call the KSP bridge.")
    bridge_p.add_argument("action", choices=["state", "launch", "revert", "reset", "save", "load-save", "space-center"])
    bridge_p.add_argument("--save-folder", default="默认", help="Save folder for the load-save bridge action.")

    args = parser.parse_args(argv)

    if args.command == "run":
        result = AutomationRunner(args.config, offline=args.offline).run(args.mission, args.max_trials)
        print(json.dumps(result, indent=2))
        return 0 if result["success"] else 2

    if args.command == "plan":
        mission = MissionPlanner().interpret(args.mission)
        design = HistoryOptimizer(mission).first_design()
        design.estimates = estimate_design(design)
        print(json.dumps({"mission": mission.to_dict(), "design": design.to_dict()}, indent=2))
        return 0

    if args.command == "write-craft":
        mission = MissionPlanner().interpret(args.mission)
        design = HistoryOptimizer(mission).first_design()
        design.estimates = estimate_design(design)
        path = CraftWriter().write(design, Path(args.output_dir))
        print(path)
        return 0

    if args.command == "history":
        cfg = load_config(args.config)
        root = Path(args.config).resolve().parent.parent
        db_path = Path(cfg["paths"]["database"])
        if not db_path.is_absolute():
            db_path = root / db_path
        print(json.dumps(TrialDatabase(db_path).list_trials(args.limit), indent=2))
        return 0

    if args.command == "research":
        cfg = load_config(args.config)
        root = Path(args.config).resolve().parent.parent
        kb_path = Path(cfg["paths"]["knowledge_base"])
        if not kb_path.is_absolute():
            kb_path = root / kb_path
        kb = KnowledgeBase(kb_path)
        if args.format == "json":
            print(json.dumps({"sources": kb.source_notes(), "principles": kb.principles_text()}, indent=2))
        else:
            print(kb.context_summary())
        return 0

    if args.command == "bridge":
        cfg = load_config(args.config)
        client = BridgeClient(**cfg["bridge"])
        action = args.action.replace("-", "_")
        result = client.load_save(args.save_folder) if action == "load_save" else getattr(client, action)()
        print(json.dumps(result, indent=2))
        return 0

    return 1
