"""Journal tools: mission log, flight doctrine, playbooks, lessons learned, and CAPCOM.

Mission folders, the automatic black-box log, and the lessons store live in :mod:`astra.journal`.
These tools are the crew's pen: they open and close missions, record intent and outcomes, look up
physics notes, and talk to the human player through the in-game CAPCOM panel. CAPCOM is best
effort everywhere: a closed game or a bridge without the panel never fails a journal call.
"""

import difflib
import json
import re
import socket
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import Field

from astra import journal
from astra.bridge import Bridge
from astra.config import CONFIG
from astra.errors import AstraError, BridgeError, NotConnected
from astra.registry import GAME_LOCK, tool

CAPCOM_TIMEOUT_S = 3.0  # the panel is a courtesy; never hold a journal call hostage to it
CAPCOM_MAX_CHARS = 600
NOTE_LEVEL = {"plan": "info", "decision": "info", "observation": "info", "anomaly": "warn",
              "result": "info"}

# Common words that would otherwise match every lesson in journal.search_lessons.
STOP_WORDS = {"the", "and", "for", "with", "into", "onto", "from", "then", "than", "that", "this", "these",
              "them", "their", "its", "our", "you", "your", "are", "was", "were", "has", "have", "had", "not",
              "but", "all", "any", "can", "will", "would", "should", "what", "when", "how", "why", "who"}

Level = Literal["info", "warn", "alert"]
NoteKind = Literal["plan", "decision", "observation", "anomaly", "result"]
Outcome = Literal["success", "partial", "failure", "aborted"]


# ---------------------------------------------------------------------------------------------
# mission


@tool("journal", needs_game=False)
def mission_start(
    goal: Annotated[str, Field(description="The mission goal in plain language, as the flight director "
                                           "gave it (e.g. 'land a kerbal on the Mun and bring them home').")],
) -> dict:
    """Open a new mission folder, make it the active mission, and recall lessons relevant to the goal.

    Every tool call from now on is recorded to the mission's log.jsonl automatically, and
    journal_note writes to its journal.md. Refuses if another mission is still active (an earlier
    run that never called mission_end): continue that one instead, or close it with mission_end
    first. The result lists past lessons whose wording matches the goal; read them before planning.
    """
    goal = " ".join(goal.split())
    if not goal:
        raise AstraError("the goal is empty", "state what the mission must achieve in one sentence.")
    prev = journal.active_mission()
    if prev is not None:
        raise AstraError(
            f"mission '{prev.name}' is still active",
            "continue it (skip mission_start and keep journaling), or close it first with "
            "mission_end(outcome=..., summary=...) and then start the new one.")
    try:
        d = journal.start_mission(goal)
    except FileExistsError as exc:
        raise AstraError(f"mission folder already exists ({exc})",
                         "wait a second and call mission_start again.") from exc
    return {
        "mission": d.name,
        "folder": str(d),
        "goal": goal,
        "relevant_lessons": journal.search_lessons(_query(goal), limit=8),
        "capcom": _capcom_send(f"MISSION START: {goal}", "info"),
    }


@tool("journal", needs_game=False)
def mission_end(
    outcome: Annotated[Outcome, Field(description="Honest outcome judged from telemetry: 'success' (every "
                                                  "goal criterion met), 'partial' (some met), 'failure' "
                                                  "(attempted, not met), 'aborted' (stopped deliberately).")],
    summary: Annotated[str, Field(description="What was achieved and what was not, with the key numbers "
                                              "(final orbit, landing speed, Δv used vs budget, crew status).")],
) -> dict:
    """Close the active mission with an outcome and a summary; returns the flight-recorder statistics.

    Writes the outcome to mission.json and journal.md and clears the active-mission pointer. Call it
    once, at the very end. Say 'success' only if telemetry shows the goal met (in orbit = periapsis
    above the atmosphere or terrain; landed = situation landed or splashed).
    """
    summary = summary.strip()
    if not summary:
        raise AstraError("the summary is empty", "summarize what happened, with numbers, in a few sentences.")
    d = journal.end_mission(outcome, summary)
    if d is None:
        raise AstraError("there is no active mission to close", "mission_start opens one; nothing to do here.")
    calls, errors = _log_stats(d / "log.jsonl")
    return {
        "mission": d.name,
        "outcome": outcome,
        "folder": str(d),
        "tool_calls": calls,
        "tool_errors": errors,
        "capcom": _capcom_send(f"MISSION END ({outcome.upper()}): {summary}",
                               "info" if outcome in ("success", "partial") else "alert"),
    }


@tool("journal", needs_game=False)
def journal_note(
    kind: Annotated[NoteKind, Field(description="plan (intent for the coming phase and its success criteria), "
                                                "decision (what you will do, the expected result, the abort "
                                                "condition), observation (what telemetry shows), anomaly "
                                                "(something off-nominal and its suspected cause), result "
                                                "(outcome of a step compared with the prediction).")],
    text: Annotated[str, Field(description="One or two sentences with the numbers that matter.")],
) -> dict:
    """Write a timestamped entry to the mission journal and mirror it to the in-game CAPCOM panel.

    The entry carries wall-clock time and, when the game answers immediately, game UT and mission
    elapsed time. Without an active mission the note is filed under missions/_unassigned. The CAPCOM
    mirror is best effort; its outcome is reported under 'capcom'.
    """
    text = text.strip()
    if not text:
        raise AstraError("the note is empty", "write what you decided or observed, with numbers.")
    clock = _game_clock()
    path = journal.append_journal(kind, text, clock)
    mission = journal.active_mission()
    out: dict[str, Any] = {
        "logged": kind,
        "mission": mission.name if mission else None,
        "journal": str(path),
        "game_time": clock,
        "capcom": _capcom_send(f"[{kind}] {text}", NOTE_LEVEL[kind]),
    }
    if mission is None:
        out["note"] = "no active mission: filed under _unassigned (mission_start opens a mission)"
    return out


# ---------------------------------------------------------------------------------------------
# knowledge


@tool("journal", needs_game=False)
def journal_read_doctrine() -> dict:
    """Return the flight doctrine (the crew's operating contract) and the list of playbook topics.

    Read it at the start of every session. Playbooks (physics and decision criteria per phase) are
    fetched with playbook(topic); past lessons with lessons_search(query).
    """
    path = CONFIG.knowledge_dir / "doctrine.md"
    if not path.exists():
        raise AstraError(f"doctrine not found at {path}",
                         "check ASTRA_KNOWLEDGE_DIR; the repo ships knowledge/doctrine.md.")
    mission = journal.active_mission()
    return {
        "doctrine": path.read_text(encoding="utf-8"),
        "playbooks": [{"topic": p["topic"], "title": p["title"]} for p in _playbooks()],
        "lessons_on_file": len(_lesson_lines()),
        "active_mission": mission.name if mission else None,
    }


@tool("journal", needs_game=False)
def playbook(
    topic: Annotated[str, Field(description="Phase or subject, loosely worded: e.g. 'ascent', 'hoverslam', "
                                            "'reentry', 'Duna transfer', 'docking', 'fly_until triggers'.")],
) -> dict:
    """Return the playbook that best matches a topic: physics, what to compute, what to read, pitfalls.

    Matching is fuzzy over file names, titles, and each playbook's keyword line. The result has the
    full markdown text plus other topics that also matched. Playbooks explain how to derive numbers
    for the current vehicle and body; they never give the numbers to type.
    """
    books = _playbooks()
    if not books:
        raise AstraError(f"no playbooks in {CONFIG.knowledge_dir / 'playbooks'}", "check ASTRA_KNOWLEDGE_DIR.")
    scored = sorted(((_match_score(topic, b), b["topic"], b) for b in books), key=lambda s: (-s[0], s[1]))
    best_score, _, best = scored[0]
    if best_score < 1.5:
        raise AstraError(f"no playbook matches {topic!r}",
                         "topics: " + ", ".join(f"{b['topic']} ({b['title']})" for b in books))
    return {
        "topic": best["topic"],
        "title": best["title"],
        "text": best["text"],
        "also_relevant": [t for s, t, _ in scored[1:4] if s >= max(1.5, 0.5 * best_score)],
    }


@tool("journal", needs_game=False)
def lessons_search(
    query: Annotated[str, Field(description="Words describing the phase or problem, e.g. 'capture overshoot "
                                            "periapsis', 'mechjeb staging heat shield', 'chute duna'.")],
    limit: Annotated[int, Field(ge=1, le=60, description="Maximum number of lessons to return.")] = 12,
) -> dict:
    """Search the lessons learned in earlier flights, best matches first.

    Lessons are one line each: '[date] (tags) lesson'. Many carry the numbers observed on a specific
    craft; treat those as evidence about the game's behavior, not as values to reuse.
    """
    return {"query": query, "lessons": journal.search_lessons(_query(query), limit=limit),
            "lessons_on_file": len(_lesson_lines())}


@tool("journal", needs_game=False)
def lesson_add(
    text: Annotated[str, Field(description="One self-contained, specific lesson: symptom, cause, and what to do "
                                           "differently, with the telemetry numbers that showed it.")],
    tags: Annotated[list[str], Field(description="A few lowercase topic tags, e.g. ['capture', 'throttle'] or "
                                                 "['krpc', 'frames']. Empty means 'general'.")],
) -> dict:
    """Append a lesson to knowledge/lessons.md so future missions can find it with lessons_search.

    Add a lesson when you learned something non-obvious that would change a future decision. Exact
    duplicates are not added again; near-duplicates are reported so you can judge.
    """
    body = " ".join(text.split())
    if len(body) < 20:
        raise AstraError("the lesson is too short to be useful",
                         "state the symptom, the cause, and what to do differently.")
    clean_tags = [t for t in (re.sub(r"[^a-z0-9_-]+", "-", tag.strip().lower()).strip("-") for tag in tags) if t]
    existing = {_lesson_body(line): line for line in _lesson_lines()}
    key = body.lower()
    for known, line in existing.items():
        if known.lower() == key:
            return {"added": False, "duplicate_of": line}
    similar = [line for known, line in existing.items() if _similar(known.lower(), key)]
    path = journal.lessons_path()
    with path.open("rb+") as f:  # a hand-edited file without a final newline would swallow the entry
        if f.seek(0, 2) > 0:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                f.write(b"\n")
    entry = journal.add_lesson(body, clean_tags)
    out: dict[str, Any] = {"added": True, "entry": entry, "file": str(path)}
    if similar:
        out["similar_existing"] = similar[:3]
    return out


# ---------------------------------------------------------------------------------------------
# CAPCOM


@tool("journal")
def capcom_say(
    text: Annotated[str, Field(description="Message for the human player, shown on the in-game CAPCOM panel.")],
    level: Annotated[Level, Field(description="info (logged on the panel), warn (off-nominal, also shown "
                                              "on screen), or alert (needs the player's attention; opens "
                                              "the panel).")] = "info",
) -> dict:
    """Show a message to the human player on the in-game CAPCOM panel (best effort).

    Never fails: when the game or the panel is unavailable the result says delivered=false and why.
    Use it for milestones, decisions the player should know about, and replies to capcom_inbox.
    """
    text = text.strip()
    if not text:
        raise AstraError("the message is empty", "say something to the player.")
    return _capcom_send(text, level)


@tool("journal")
def capcom_inbox(
    since_seq: Annotated[int, Field(ge=0, description="Only return messages with a sequence number greater than "
                                                      "this; pass the last_seq of your previous call (0 = all).")] = 0,
) -> dict:
    """Read messages the human player typed into the in-game CAPCOM panel (best effort).

    Treat them as direction from the flight director: follow them when they are safe, and answer
    with capcom_say. Never fails: when the game or the panel is unavailable the result says
    available=false and why. Check it at phase boundaries and after long reflexes.
    """
    bridge, reason = _bridge()
    if bridge is None:
        return {"available": False, "reason": reason, "messages": []}
    try:
        res = bridge.get(f"/capcom/inbox?since={since_seq}")
    except BridgeError as exc:
        if _unknown_route(exc):
            return _legacy_inbox(bridge)
        return {"available": False, "reason": exc.message, "messages": []}
    except NotConnected as exc:
        return {"available": False, "reason": exc.message, "messages": []}
    messages = [_normalize_message(m) for m in _message_list(res)]
    messages = [m for m in messages if m["from"] == "player"
                and (not isinstance(m["seq"], int) or m["seq"] > since_seq)]
    seqs = [m["seq"] for m in messages if isinstance(m["seq"], int)]
    last = res.get("lastSeq") if isinstance(res, dict) else None
    return {"available": True, "via": "/capcom/inbox", "messages": messages,
            "last_seq": int(last) if isinstance(last, (int, float)) else max(seqs, default=since_seq)}


# ---------------------------------------------------------------------------------------------
# helpers


def _playbooks() -> list[dict]:
    folder = CONFIG.knowledge_dir / "playbooks"
    books = []
    for p in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        title = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), p.stem)
        kw_line = next((ln.split(":", 1)[1] for ln in lines if ln.lower().startswith("keywords:")), "")
        books.append({"topic": p.stem, "title": title, "text": text,
                      "keywords": [k.strip().lower() for k in kw_line.split(",") if k.strip()]})
    return books


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _match_score(query: str, book: dict) -> float:
    """How well a loosely worded topic matches a playbook (0 = not at all)."""
    q = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
    if not q:
        return 0.0
    if q == book["topic"].replace("-", " "):
        return 100.0
    stem_w = _words(book["topic"])
    title_w = _words(book["title"])
    kw_w = _words(" ".join(book["keywords"]))
    score = 0.0
    for kw in book["keywords"]:
        phrase = " ".join(re.findall(r"[a-z0-9]+", kw))
        if phrase == q:
            score += 10.0
        elif len(phrase) > 3 and " " in phrase and phrase in q:
            score += 3.0
    for w in _words(q):
        if len(w) < 3:
            continue
        if w in stem_w:
            score += 3.0
        elif w in title_w or w in kw_w:
            score += 2.0
        else:
            vocab = stem_w | title_w | kw_w
            if len(w) >= 4 and any(c.startswith(w) or (len(c) >= 4 and w.startswith(c)) for c in vocab):
                score += 1.5
            else:
                ratio = max((difflib.SequenceMatcher(None, w, c).ratio() for c in vocab), default=0.0)
                if ratio >= 0.8:
                    score += 2.0 * ratio
    return score


def _query(text: str) -> str:
    words = [w for w in re.findall(r"\w+", text.lower()) if w not in STOP_WORDS]
    return " ".join(words) or text


def _lesson_lines() -> list[str]:
    return [ln.strip() for ln in journal.lessons_path().read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("- ")]


def _similar(a: str, b: str, threshold: float = 0.85) -> bool:
    m = difflib.SequenceMatcher(None, a, b)
    return m.quick_ratio() > threshold and m.ratio() > threshold


def _lesson_body(line: str) -> str:
    m = re.match(r"^- \[[^\]]*\]\s*\([^)]*\)\s*(.*)$", line)
    return m.group(1).strip() if m else line[2:].strip()


def _log_stats(log: Path) -> tuple[int, int]:
    if not log.exists():
        return 0, 0
    calls = errors = 0
    with log.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            calls += 1
            errors += 0 if rec.get("ok", True) else 1
    return calls, errors


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _fmt_met(seconds: float) -> str:
    s = int(max(seconds, 0))
    d, s = divmod(s, 6 * 3600)  # Kerbin days are 6 h
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return (f"{d}d " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"


def _game_clock() -> str | None:
    """'UT 1234567 · MET 00:12:34' when the game answers right away; None otherwise (never waits)."""
    if not _port_open(CONFIG.krpc_host, CONFIG.krpc_rpc_port):
        return None
    if not GAME_LOCK.acquire(blocking=False):
        return None  # a reflex or command holds the game; the note must not wait for it
    try:
        from astra.ksp import ksp

        k = ksp()
        text = f"UT {k.sc.ut:.0f}"
        if k.scene() == "flight":
            text += f" · MET {_fmt_met(k.vessel().met)}"
        return text
    except Exception:  # noqa: BLE001 — the clock is decoration, never a failure
        return None
    finally:
        GAME_LOCK.release()


def _bridge() -> tuple[Bridge | None, str]:
    """A short-timeout bridge client, or None and why (probing the port first keeps a closed game cheap)."""
    try:
        bridge = Bridge(timeout=CAPCOM_TIMEOUT_S)
    except BridgeError as exc:
        return None, exc.message
    url = urlparse(bridge.base_url)
    if not _port_open(url.hostname or "127.0.0.1", url.port or 80):
        return None, "the KSP bridge is not running (game closed or still loading)"
    return bridge, ""


def _unknown_route(exc: BridgeError) -> bool:
    return "unknown route" in str(exc).lower()


def _ascii(text: str) -> str:
    # The legacy bridge reads bodies as chars against a byte Content-Length and hangs on UTF-8.
    swaps = {"Δ": "d", "·": "-", "—": "-", "–": "-", "°": " deg", "≈": "~", "≤": "<=", "≥": ">=", "→": "->"}
    return "".join(swaps.get(c, c) for c in text).encode("ascii", "replace").decode("ascii")


_panel: bool | None = None  # does the running bridge have the CAPCOM panel? probed once, re-probed on change


def _has_panel(bridge: Bridge) -> bool:
    global _panel
    if _panel is None:
        try:
            bridge.get("/capcom?limit=1")  # reads the thread's tail; does not count as an inbox poll
            _panel = True
        except BridgeError as exc:
            if not _unknown_route(exc):
                raise
            _panel = False
    return _panel


def _capcom_send(text: str, level: str) -> dict:
    global _panel
    text = text if len(text) <= CAPCOM_MAX_CHARS else text[:CAPCOM_MAX_CHARS - 1] + "…"
    bridge, reason = _bridge()
    if bridge is None:
        return {"delivered": False, "reason": reason}
    for attempt in range(2):
        try:
            if _has_panel(bridge):
                bridge.post("/capcom", {"text": text, "level": level})
                return {"delivered": True, "via": "/capcom"}
            bridge.post("/status", {"phase": f"CAPCOM {level}", "message": _ascii(text)})
            return {"delivered": True, "via": "/status (legacy status panel)"}
        except BridgeError as exc:
            if _unknown_route(exc) and attempt == 0:
                _panel = None  # the bridge was swapped under us: probe again
                continue
            return {"delivered": False, "reason": exc.message}
        except NotConnected as exc:
            _panel = None
            return {"delivered": False, "reason": exc.message}
    return {"delivered": False, "reason": "the bridge rejected both CAPCOM routes"}


def _message_list(res: Any) -> list:
    if isinstance(res, list):
        return res
    for key in ("messages", "inbox", "items"):
        if isinstance(res.get(key), list):
            return res[key]
    return []


def _normalize_message(m: Any) -> dict:
    if not isinstance(m, dict):
        return {"seq": None, "text": str(m), "from": "player", "time": None, "ut": None}
    seq = m.get("seq", m.get("id"))
    try:
        seq = int(seq) if seq is not None else None
    except (TypeError, ValueError):
        seq = None
    return {"seq": seq, "text": str(m.get("text", m.get("message", ""))), "from": m.get("from", "player"),
            "time": m.get("utc", m.get("time", m.get("ts"))), "ut": m.get("ut")}


def _legacy_inbox(bridge: Bridge) -> dict:
    """Older bridges have a one-shot player command queue instead of the CAPCOM inbox."""
    messages = []
    try:
        for _ in range(20):
            cmd = bridge.get("/command/pending").get("command")
            if not cmd:
                break
            messages.append({"seq": None, "text": str(cmd), "from": "player", "time": None, "ut": None})
    except (BridgeError, NotConnected) as exc:
        if not messages:
            return {"available": False, "reason": exc.message, "messages": []}
    return {"available": True, "via": "/command/pending (legacy queue; messages are consumed)",
            "messages": messages, "last_seq": None}
