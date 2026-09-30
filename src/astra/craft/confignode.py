"""KSP ConfigNode text format: parse and serialize .craft, .cfg and ModuleManager cache files.

A node is an ordered list of items, each either a ``key = value`` pair or a named child node, so
duplicate keys and the interleaving of values and nodes survive a round trip. KSP semantics:

* ``//`` starts a comment. A trailing comment is kept on its value (part titles carry the English
  text there, e.g. ``title = #autoLOC_500820 //#autoLOC_500820 = TT-70 Radial Decoupler``); full-line
  comments are dropped.
* ``{`` and ``}`` are always structural, wherever they appear on a line.
* A value splits at the first ``=``; key and value are trimmed.
* A node name is the last bare word before ``{`` (``NAME`` on its own line or ``NAME {``).

Serialization writes KSP's own layout (tab indentation, ``NAME`` / ``{`` / ``}`` on separate lines),
so a craft file KSP saved parses and re-serializes to the same text.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(slots=True)
class Value:
    key: str
    value: str
    comment: str | None = None  # text after '//' on the same line
    gap: str = " "  # whitespace between the value and '//', kept for faithful round trips


class ConfigNode:
    __slots__ = ("name", "items")

    def __init__(self, name: str = "", items: list[Value | ConfigNode] | None = None):
        self.name = name
        self.items: list[Value | ConfigNode] = items if items is not None else []

    # -- queries ------------------------------------------------------------------------------

    def values(self) -> Iterator[Value]:
        return (it for it in self.items if isinstance(it, Value))

    def get(self, key: str, default: str | None = None) -> str | None:
        for v in self.values():
            if v.key == key:
                return v.value
        return default

    def get_value(self, key: str) -> Value | None:
        return next((v for v in self.values() if v.key == key), None)

    def get_all(self, key: str) -> list[str]:
        return [v.value for v in self.values() if v.key == key]

    def has(self, key: str) -> bool:
        return any(v.key == key for v in self.values())

    def nodes(self, name: str | None = None) -> list[ConfigNode]:
        return [it for it in self.items if isinstance(it, ConfigNode) and (name is None or it.name == name)]

    def node(self, name: str) -> ConfigNode | None:
        return next((it for it in self.items if isinstance(it, ConfigNode) and it.name == name), None)

    def find_nodes(self, node_name: str, /, **match: str) -> list[ConfigNode]:
        """Child nodes called ``node_name`` whose values equal every ``key=value`` in ``match``."""
        return [n for n in self.nodes(node_name) if all(n.get(k) == v for k, v in match.items())]

    def __repr__(self) -> str:
        return f"ConfigNode({self.name!r}, {sum(1 for _ in self.values())} values, {len(self.nodes())} nodes)"

    # -- edits --------------------------------------------------------------------------------

    def add(self, key: str, value: object, comment: str | None = None) -> ConfigNode:
        self.items.append(Value(key, _text(value), comment))
        return self

    def set(self, key: str, value: object) -> ConfigNode:
        """Replace the first ``key`` (keeping its position) or append it."""
        for v in self.values():
            if v.key == key:
                v.value = _text(value)
                return self
        return self.add(key, value)

    def remove(self, key: str) -> int:
        before = len(self.items)
        self.items = [it for it in self.items if not (isinstance(it, Value) and it.key == key)]
        return before - len(self.items)

    def add_node(self, node: ConfigNode | str) -> ConfigNode:
        child = ConfigNode(node) if isinstance(node, str) else node
        self.items.append(child)
        return child

    def remove_nodes(self, name: str) -> int:
        before = len(self.items)
        self.items = [it for it in self.items if not (isinstance(it, ConfigNode) and it.name == name)]
        return before - len(self.items)

    # -- text ---------------------------------------------------------------------------------

    @classmethod
    def parse(cls, text: str) -> ConfigNode:
        """Parse ConfigNode text into an unnamed root node holding the top-level items."""
        root = cls("")
        stack = [root]
        pending: str | None = None  # a bare word waiting for its '{'
        text = text.removeprefix("﻿")
        for raw in text.splitlines():
            line, comment, gap = raw, None, " "
            cut = raw.find("//")
            if cut >= 0:
                line, comment = raw[:cut], raw[cut + 2:]
                gap = line[len(line.rstrip()):]
            start = 0
            for i, ch in enumerate(line + "\n"):
                if ch not in "{}\n":
                    continue
                segment = line[start:i].strip()
                start = i + 1
                if segment:
                    if "=" in segment:
                        key, _, value = segment.partition("=")
                        # a comment belongs to the value that ends the line
                        ends = ch == "\n"
                        stack[-1].items.append(Value(key.strip(), value.strip(), comment if ends else None,
                                                     gap if ends else " "))
                        pending = None
                    else:
                        pending = segment
                if ch == "{":
                    child = cls(pending or "")
                    stack[-1].items.append(child)
                    stack.append(child)
                    pending = None
                elif ch == "}":
                    if len(stack) > 1:
                        stack.pop()
                    pending = None
        return root

    @classmethod
    def load(cls, path: str | Path) -> ConfigNode:
        return cls.parse(read_text(path))

    def to_text(self, newline: str = "\n") -> str:
        """Serialize. The root node (empty name) writes its items without an enclosing block."""
        lines: list[str] = []
        if self.name:
            self._emit(lines, 0)
        else:
            for it in self.items:
                _emit_item(it, lines, 0)
        return newline.join(lines) + newline

    def _emit(self, lines: list[str], depth: int) -> None:
        pad = "\t" * depth
        lines.append(pad + self.name)
        lines.append(pad + "{")
        for it in self.items:
            _emit_item(it, lines, depth + 1)
        lines.append(pad + "}")

    def save(self, path: str | Path, newline: str = "\n") -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8", newline="") as f:
            f.write(self.to_text(newline))
        return p


def _emit_item(it: Value | ConfigNode, lines: list[str], depth: int) -> None:
    if isinstance(it, ConfigNode):
        it._emit(lines, depth)
        return
    text = "\t" * depth + f"{it.key} = {it.value}"
    if it.comment is not None:
        gap = it.gap[1:] if not it.value and it.gap.startswith(" ") else it.gap  # "k = " ends in a space
        text += gap + "//" + it.comment
    lines.append(text)


def read_text(path: str | Path) -> str:
    """Read a KSP text file: UTF-8 (with or without BOM); some stock files are Windows-1252."""
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _text(value: object) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        return fmt_float(value)
    return str(value)


def fmt_float(x: float, digits: int = 7) -> str:
    """Compact decimal text KSP parses: no exponent, no trailing zeros, tiny values as 0."""
    if abs(x) < 0.5 * 10 ** -digits:
        return "0"
    text = f"{x:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def parse_floats(text: str | None) -> list[float]:
    """'0, 1.5,2' or '0 1.5 2' -> [0.0, 1.5, 2.0]; unparsable tokens are skipped."""
    out: list[float] = []
    for tok in (text or "").replace(",", " ").split():
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out


def parse_bool(text: str | None, default: bool = False) -> bool:
    if text is None:
        return default
    t = text.strip().lower()
    if t in ("true", "1", "yes", "on"):
        return True
    if t in ("false", "0", "no", "off"):
        return False
    return default
