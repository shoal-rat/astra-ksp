from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class KnowledgeBase:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def source_notes(self) -> list[dict[str, Any]]:
        path = self.root / "sources.yaml"
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or []

    def principles_text(self) -> str:
        path = self.root / "principles.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def context_summary(self) -> str:
        sources = self.source_notes()
        principles = self.principles_text().strip()
        source_lines = [f"- {item.get('title')}: {item.get('url')}" for item in sources]
        return "\n".join(["# Principles", principles, "", "# Sources", *source_lines]).strip()

