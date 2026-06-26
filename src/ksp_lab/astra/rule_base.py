"""ASTRA's structured, QUERYABLE failure-pattern rule base.

The experience ledger (``ledger.py``) is an append-only NARRATIVE of attempts; this module turns the
project's hard-won failure patterns into a STRUCTURED, queryable knowledge object. Each rule carries a
``marker`` (the log/phase signature that identifies the failure), a ``symptom`` (observable behaviour),
a ``cause`` (root cause), a ``fix`` (the remedy), the ``applicable_primitives`` it bears on, a
``confidence`` in [0, 1], and ``tags``.

The rules live in ``src/ksp_lab/data/failure_rules.json`` so they are data, not code, and can be grown
without touching the agent. ``RuleBase.query(...)`` returns matching rules RANKED by confidence (marker
substring/regex match, primitive membership, symptom keyword/fuzzy match); ``RuleBase.diagnose(marker,
log_tail)`` returns the single best match. ``knowledge.py`` consults this FIRST and falls back to its
hardcoded seed rules.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

# The packaged rule-base JSON, resolved relative to this file: src/ksp_lab/data/failure_rules.json.
_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "failure_rules.json"

_REQUIRED_FIELDS = (
    "id",
    "marker",
    "symptom",
    "cause",
    "fix",
    "applicable_primitives",
    "confidence",
    "tags",
)

# Tiny stop-word list so a symptom keyword match isn't swamped by common words.
_STOPWORDS = frozenset(
    "the a an of to in on at by for and or not is are was were it its as with from into "
    "that this these those during any all one no off out up so be been has had its".split()
)


@dataclass(slots=True)
class Rule:
    """One structured failure->fix rule."""

    id: str
    marker: str
    symptom: str
    cause: str
    fix: str
    applicable_primitives: list[str] = field(default_factory=list)
    confidence: float = 0.0
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rule":
        return cls(
            id=str(d["id"]),
            marker=str(d["marker"]),
            symptom=str(d["symptom"]),
            cause=str(d["cause"]),
            fix=str(d["fix"]),
            applicable_primitives=[str(p) for p in d.get("applicable_primitives", [])],
            confidence=float(d["confidence"]),
            tags=[str(t) for t in d.get("tags", [])],
        )

    def markers(self) -> list[str]:
        """The marker field is a '|'-separated set of alternative signatures."""
        return [m.strip() for m in self.marker.split("|") if m.strip()]


def _norm(s: str) -> str:
    return (s or "").lower()


def _keywords(text: str) -> set[str]:
    toks = re.findall(r"[a-z0-9_]+", _norm(text))
    return {t for t in toks if len(t) > 2 and t not in _STOPWORDS}


def _marker_score(rule: Rule, needle: str) -> float:
    """How well ``needle`` (a marker / log text) matches this rule's marker signatures.

    A marker alternative matches if it appears as a SUBSTRING of the needle (or vice-versa), or if it
    is a valid regex that searches the needle. Returns the best alternative's score in [0, 1]."""
    if not needle:
        return 0.0
    hay = _norm(needle)
    best = 0.0
    for alt in rule.markers():
        a = _norm(alt)
        if not a:
            continue
        # Plain substring either way (markers are often short signatures inside a longer log line).
        if a in hay or (len(hay) >= 3 and hay in a):
            # A longer, more specific alternative is a stronger signal than a 3-letter token.
            best = max(best, min(1.0, 0.7 + 0.3 * min(len(a), 40) / 40.0))
            continue
        # Treat the alternative as a simple regex (the JSON uses .* / \\? etc.).
        try:
            if re.search(a, hay):
                best = max(best, 0.85)
        except re.error:
            pass
    return best


class RuleBase:
    """A queryable collection of structured failure rules."""

    def __init__(self, rules: list[Rule]):
        self.rules = list(rules)

    # ---- loading -------------------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "RuleBase":
        p = Path(path) if path else _DATA_PATH
        raw = json.loads(p.read_text(encoding="utf-8"))
        rule_dicts = raw["rules"] if isinstance(raw, dict) else raw
        return cls([Rule.from_dict(d) for d in rule_dicts])

    def __len__(self) -> int:
        return len(self.rules)

    def by_id(self, rule_id: str) -> Rule | None:
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None

    # ---- querying ------------------------------------------------------------------------------
    def query(
        self,
        *,
        marker: str | None = None,
        primitive: str | None = None,
        symptom_text: str | None = None,
        min_confidence: float = 0.0,
    ) -> list[Rule]:
        """Return rules matching the given filters, RANKED best-first.

        - ``marker``: substring/regex match against each rule's marker signatures.
        - ``primitive``: keep only rules whose ``applicable_primitives`` contains it.
        - ``symptom_text``: keyword-overlap + fuzzy match against symptom/cause/marker/tags.
        - ``min_confidence``: drop rules below this confidence.

        Ranking key: (relevance score, confidence) descending. With no marker/symptom filter the
        result is just every (primitive-filtered, confidence-gated) rule sorted by confidence.
        """
        prim = _norm(primitive) if primitive else None
        sym_kw = _keywords(symptom_text) if symptom_text else set()

        scored: list[tuple[float, float, Rule]] = []
        for r in self.rules:
            if r.confidence < min_confidence:
                continue
            if prim is not None and prim not in {_norm(p) for p in r.applicable_primitives}:
                continue

            relevance = 0.0
            matched_a_filter = marker is None and symptom_text is None  # no relevance filter => keep

            if marker is not None:
                m = _marker_score(r, marker)
                if m > 0.0:
                    relevance += m
                    matched_a_filter = True

            if symptom_text:
                relevance += self._symptom_score(r, symptom_text, sym_kw)
                if relevance > 0.0:
                    matched_a_filter = True

            if not matched_a_filter:
                continue
            scored.append((relevance, r.confidence, r))

        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [r for _, _, r in scored]

    def query_scored(
        self,
        *,
        marker: str | None = None,
        primitive: str | None = None,
        symptom_text: str | None = None,
        min_confidence: float = 0.0,
    ) -> list[tuple[Rule, float]]:
        """Like :meth:`query` but also returns each rule's relevance score (best-first)."""
        prim = _norm(primitive) if primitive else None
        sym_kw = _keywords(symptom_text) if symptom_text else set()
        out: list[tuple[float, float, Rule]] = []
        for r in self.rules:
            if r.confidence < min_confidence:
                continue
            if prim is not None and prim not in {_norm(p) for p in r.applicable_primitives}:
                continue
            relevance = 0.0
            matched = marker is None and symptom_text is None
            if marker is not None:
                m = _marker_score(r, marker)
                if m > 0.0:
                    relevance += m
                    matched = True
            if symptom_text:
                relevance += self._symptom_score(r, symptom_text, sym_kw)
                if relevance > 0.0:
                    matched = True
            if not matched:
                continue
            out.append((relevance, r.confidence, r))
        out.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [(r, rel) for rel, _, r in out]

    @staticmethod
    def _symptom_score(rule: Rule, symptom_text: str, sym_kw: set[str] | None = None) -> float:
        """Keyword overlap (primary) + a fuzzy ratio (tie-break) of free text vs the rule's text."""
        kw = sym_kw if sym_kw is not None else _keywords(symptom_text)
        if not kw:
            return 0.0
        rule_blob = " ".join(
            [rule.symptom, rule.cause, rule.marker, " ".join(rule.tags)]
        )
        rule_kw = _keywords(rule_blob)
        overlap = len(kw & rule_kw)
        kw_score = overlap / max(1, len(kw))  # fraction of the query's keywords the rule covers
        fuzzy = SequenceMatcher(None, _norm(symptom_text), _norm(rule.symptom)).ratio()
        return kw_score + 0.25 * fuzzy

    # A symptom-only fallback must clear this relevance floor, so a stray single keyword (e.g. the word
    # "marker" inside an unseen marker string) does NOT spuriously diagnose. Marker-signature hits, being
    # far more reliable, have no such floor.
    _SYMPTOM_FALLBACK_FLOOR = 0.34

    def diagnose(self, marker: str, log_tail: str = "") -> Rule | None:
        """Return the single best-matching rule for a flight's ending marker + optional log tail.

        Matches on the marker signature first (most reliable); if nothing matches, falls back to a
        symptom keyword match that must clear a relevance floor. Returns ``None`` if nothing matches —
        an UNKNOWN marker correctly yields no diagnosis."""
        needle = f"{marker} {log_tail}".strip()
        # First: marker-signature match (ranked by marker score, then confidence).
        by_marker = self.query(marker=needle)
        if by_marker:
            return by_marker[0]
        # Fallback: treat the whole text as a symptom and keyword-match, but only trust a strong hit.
        by_symptom = self.query_scored(symptom_text=needle)
        if by_symptom and by_symptom[0][1] >= self._SYMPTOM_FALLBACK_FLOOR:
            return by_symptom[0][0]
        return None
