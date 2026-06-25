"""Tolerant vessel-name matching.

KSP localizes vessel names in some installs by appending a suffix to the craft name a tool launched
under — e.g. a craft written as ``AI-Eve-Crew`` shows up in the kRPC vessel list as ``AI-Eve-Crew 飞船``
(the Chinese word for "spacecraft") or ``AI-Eve-Crew Probe``. The old selector compared with ``==``,
so it could never find the vessel it had just launched and STRANDED it (a real bug). This module does
the matching the whole agent should use: case-insensitive, suffix-tolerant, prefix/normalized.

Pure (no kRPC), so it is unit-testable offline and importable from both the flight controller and the
ASTRA primitive layer with no circular import.
"""
from __future__ import annotations

import re

# Localization/auto suffixes KSP (or a mod) may append after the craft name. Matched case-insensitively
# as a trailing token; this is NOT an exhaustive list — the prefix/normalized fallbacks below catch the
# rest. 飞船 = Chinese "spacecraft"; the English words are the stock vessel-type nouns.
_KNOWN_SUFFIXES = (
    "飞船", "宇宙飞船", "探测器", "卫星", "空间站", "着陆器", "中继",
    "spacecraft", "probe", "ship", "vessel", "relay", "lander", "station", "rover", "debris",
)


def _normalize(name: str) -> str:
    """Lowercase, collapse whitespace, drop punctuation that KSP/locale insert around an appended suffix."""
    s = (name or "").strip().lower()
    s = re.sub(r"[\s　]+", " ", s)          # ASCII + ideographic spaces -> one space
    return s


def _strip_known_suffix(name: str) -> str:
    """Remove ONE trailing known localization suffix (with any separating space/punctuation), if present."""
    s = _normalize(name)
    for suf in _KNOWN_SUFFIXES:
        suf_n = _normalize(suf)
        # trailing "<sep><suffix>" where sep is optional space/dash/paren/colon
        m = re.search(r"[\s\-_:()\[\]]*" + re.escape(suf_n) + r"$", s)
        if m and m.start() > 0:
            return s[: m.start()].strip()
    return s


def vessel_names_match(actual: str, wanted: str) -> bool:
    """True if the live vessel name ``actual`` refers to the craft the caller asked for as ``wanted``.

    Tolerant of KSP's localized vessel-name suffixes (the Chinese "飞船", English "Probe", etc.): an exact,
    normalized, suffix-stripped, or prefix match all count. Empty ``wanted`` matches nothing (callers that
    want "any vessel" should not route through here). This is the fix for the stranded-vessel bug where
    ``str(vessel.name) == name`` failed because the live name carried a localization suffix."""
    if not wanted:
        return False
    a_raw, w_raw = _normalize(actual), _normalize(wanted)
    if a_raw == w_raw:
        return True
    a = _strip_known_suffix(actual)
    w = _strip_known_suffix(wanted)
    if a == w and a:
        return True
    # Prefix match (the live name STARTS with the requested name, then a separator + suffix). Guard with a
    # separator so "AI-Relay" does not match "AI-Relay-2"; a suffix must be set off by space/punctuation.
    longer, shorter = (a_raw, w_raw) if len(a_raw) >= len(w_raw) else (w_raw, a_raw)
    if shorter and longer.startswith(shorter):
        rest = longer[len(shorter):]
        if rest == "" or rest[0] in " -_:()[]　":
            return True
    return False
