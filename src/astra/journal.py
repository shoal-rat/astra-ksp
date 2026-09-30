"""Flight recorder and mission logbook.

Every tool call is appended to the active mission's ``log.jsonl`` automatically (the black box).
The crew writes decisions and observations to ``journal.md`` through the journal tools. Lessons
that should outlive a mission go to ``knowledge/lessons.md``. The active mission is a pointer
file so the MCP server and one-off ``astra call`` processes share it.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astra import registry
from astra.config import CONFIG

_ACTIVE = "ACTIVE"
_installed = False
_just_closed: Path | None = None  # so the mission_end call itself is logged into the mission it closed
_STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "when", "what", "how", "are", "was",
         "not", "but", "you", "its", "can", "all", "any", "use", "one"}


def missions_dir() -> Path:
    CONFIG.missions_dir.mkdir(parents=True, exist_ok=True)
    return CONFIG.missions_dir


def active_mission() -> Path | None:
    ptr = missions_dir() / _ACTIVE
    if not ptr.exists():
        return None
    d = missions_dir() / ptr.read_text(encoding="utf-8").strip()
    return d if d.is_dir() else None


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (s or "mission")[:40]


def start_mission(goal: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = missions_dir() / f"{stamp}-{_slug(goal)}"
    d.mkdir(parents=True, exist_ok=False)
    (d / "mission.json").write_text(json.dumps(
        {"goal": goal, "started": datetime.now().isoformat(timespec="seconds"), "status": "active"},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "journal.md").write_text(f"# Mission: {goal}\n\nStarted {datetime.now():%Y-%m-%d %H:%M}\n\n",
                                  encoding="utf-8")
    (missions_dir() / _ACTIVE).write_text(d.name, encoding="utf-8")
    return d


def end_mission(outcome: str, summary: str) -> Path | None:
    global _just_closed
    d = active_mission()
    if d is None:
        return None
    meta = json.loads((d / "mission.json").read_text(encoding="utf-8"))
    meta.update(status=outcome, ended=datetime.now().isoformat(timespec="seconds"), summary=summary)
    (d / "mission.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    append_journal("outcome", f"**{outcome.upper()}** — {summary}")
    _just_closed = d
    (missions_dir() / _ACTIVE).unlink(missing_ok=True)
    return d


def append_journal(kind: str, text: str, game_time: str | None = None) -> Path:
    d = active_mission() or _scratch_dir()
    stamp = datetime.now().strftime("%H:%M:%S")
    gt = f" · {game_time}" if game_time else ""
    with (d / "journal.md").open("a", encoding="utf-8") as f:
        f.write(f"- `{stamp}{gt}` **{kind}**: {text.strip()}\n")
    return d / "journal.md"


def _scratch_dir() -> Path:
    d = missions_dir() / "_unassigned"
    d.mkdir(exist_ok=True)
    if not (d / "journal.md").exists():
        (d / "journal.md").write_text("# Unassigned activity\n\n", encoding="utf-8")
    return d


def _record(name: str, args: dict, ok: bool, payload: Any, seconds: float) -> None:
    d = active_mission() or (_just_closed if name == "mission_end" else None) or _scratch_dir()
    if isinstance(payload, registry.Picture):
        out: Any = {"picture": str(payload.path), "caption": payload.caption}
    elif ok:
        out = registry.to_jsonable(payload)
    else:
        out = {"error": str(payload)}
    text = json.dumps(out, ensure_ascii=False)
    if len(text) > 6000:
        text = json.dumps({"truncated": text[:6000]}, ensure_ascii=False)
    line = json.dumps({"t": round(time.time(), 2), "tool": name, "ok": ok, "s": round(seconds, 2),
                       "args": registry.to_jsonable(args)}, ensure_ascii=False)
    with (d / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write(line[:-1] + ',"result":' + text + "}\n")


def install_recorder() -> None:
    global _installed
    if not _installed:
        registry.add_hook(_record)
        _installed = True


# -- lessons -----------------------------------------------------------------------------------


def lessons_path() -> Path:
    p = CONFIG.knowledge_dir / "lessons.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("# Lessons learned in flight\n\n", encoding="utf-8")
    return p


def add_lesson(text: str, tags: list[str]) -> str:
    entry = f"- [{datetime.now():%Y-%m-%d}] ({', '.join(tags) or 'general'}) {text.strip()}\n"
    with lessons_path().open("a", encoding="utf-8") as f:
        f.write(entry)
    return entry.strip()


def search_lessons(query: str, limit: int = 12) -> list[str]:
    lines = [ln.strip() for ln in lessons_path().read_text(encoding="utf-8").splitlines()
             if ln.strip().startswith("- ")]
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2 and w not in _STOP]
    if not words:
        return lines[-limit:]
    scored = sorted(((sum(w in ln.lower() for w in words), i, ln) for i, ln in enumerate(lines)),
                    reverse=True)
    return [ln for score, _, ln in scored if score > 0][:limit]
