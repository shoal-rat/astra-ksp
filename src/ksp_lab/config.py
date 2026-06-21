from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "bridge": {
        "base_url": "http://127.0.0.1:48500",
        "timeout_s": 30,
    },
    "krpc": {
        "name": "ksp1-automation-lab",
        "host": "127.0.0.1",
        "rpc_port": 50000,
        "stream_port": 50001,
        "physics_warp_factor": 3,
    },
    "paths": {
        "run_dir": "runs",
        "database": "runs/trials.sqlite3",
        "knowledge_base": "data/knowledge_base",
        "ksp_save_ships_vab": "",
    },
    "runner": {
        "max_trials": 25,
        "success_streak_required": 1,
        "revert_after_trial": True,
        "flight_timeout_s": 900,
        "post_load_settle_s": 6,
        "scene_transition_timeout_s": 60,
    },
    "optimizer": {
        "external_command": "",
        "external_timeout_s": 120,
    },
    "craft_writer": {
        "template_path": "",
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return DEFAULT_CONFIG
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    return deep_merge(DEFAULT_CONFIG, loaded)


def project_root_from_config(config_path: str | Path | None) -> Path:
    if config_path:
        return Path(config_path).resolve().parent.parent
    return Path.cwd()
