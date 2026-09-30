"""Journal tools (astra.tools.logbook) and the shipped knowledge files, tested offline.

Missions and lessons go to a temporary folder; the bridge and kRPC endpoints point at closed ports
or at a fake bridge served from this process, so no test touches the running game.
"""

import inspect
import json
import re
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError

from astra import journal, registry
from astra.config import CONFIG, REPO_ROOT
from astra.errors import AstraError
from astra.tools import logbook

JOURNAL_TOOLS = {"mission_start": False, "mission_end": False, "journal_note": False,
                 "journal_read_doctrine": False, "playbook": False, "lessons_search": False,
                 "lesson_add": False, "capcom_say": True, "capcom_inbox": True}
PLAYBOOKS = {"rocket-design", "ascent", "orbital-maneuvers", "transfers", "capture", "landing-airless",
             "atmospheric-entry", "rendezvous-docking", "eva-surface-ops", "recovery-anomalies",
             "tools-cheatsheet"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _set(**values) -> dict:
    old = {k: getattr(CONFIG, k) for k in values}
    for k, v in values.items():
        object.__setattr__(CONFIG, k, v)  # CONFIG is a frozen dataclass
    return old


@pytest.fixture
def ws(tmp_path):
    """A private missions dir and a copy of the repo's knowledge; game endpoints on closed ports."""
    knowledge = tmp_path / "knowledge"
    shutil.copytree(REPO_ROOT / "knowledge", knowledge)
    old = _set(missions_dir=tmp_path / "missions", knowledge_dir=knowledge,
               bridge_url=f"http://127.0.0.1:{_free_port()}", krpc_rpc_port=_free_port())
    logbook._panel = None  # forget which bridge flavour an earlier test served
    yield tmp_path
    _set(**old)
    logbook._panel = None


class FakeBridge:
    """Serves the bridge's JSON protocol on 127.0.0.1 from a thread, recording requests."""

    def __init__(self, routes: dict):
        self.routes = routes  # (method, path) -> dict | callable(body) -> dict ; missing -> Unknown route
        self.requests: list[tuple[str, str, dict | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else None
                outer.requests.append((method, self.path, body))
                route = outer.routes.get((method, self.path))
                if route is None:
                    status, payload = 404, {"ok": False, "error": f"Unknown route: {method} {self.path}"}
                else:
                    status, payload = 200, {"ok": True, **(route(body) if callable(route) else route)}
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._reply("GET")

            def do_POST(self):  # noqa: N802
                self._reply("POST")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def posts(self, path: str) -> list[dict]:
        return [b for m, p, b in self.requests if m == "POST" and p == path]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def bridge(ws):
    fakes: list[FakeBridge] = []

    def make(routes: dict) -> FakeBridge:
        fake = FakeBridge(routes)
        _set(bridge_url=fake.url)
        fakes.append(fake)
        return fake

    yield make
    for fake in fakes:
        fake.close()


# ---------------------------------------------------------------------------------------------
# registration and conventions


def test_journal_tools_registered_with_described_parameters():
    for name, needs_game in JOURNAL_TOOLS.items():
        spec = registry.TOOLS[name]
        assert spec.group == "journal"
        assert spec.needs_game is needs_game, name
        assert spec.summary and len(spec.summary) < 200, name
        for p in spec.signature.parameters.values():
            meta = getattr(p.annotation, "__metadata__", ())
            assert any(getattr(m, "description", None) for m in meta), f"{name}.{p.name} lacks a description"
    assert "from __future__" not in inspect.getsource(logbook)


# ---------------------------------------------------------------------------------------------
# missions and the journal


def test_mission_lifecycle(ws):
    start = registry.validate_and_call("mission_start", {"goal": "  Land   a kerbal on the Mun  "})
    folder = Path(start["folder"])
    assert start["goal"] == "Land a kerbal on the Mun"
    assert folder.is_dir() and folder.parent == ws / "missions"
    assert journal.active_mission() == folder
    lessons = start["relevant_lessons"]
    assert 1 <= len(lessons) <= 8 and all("mun" in ln.lower() for ln in lessons[:3])
    assert start["capcom"]["delivered"] is False  # no bridge on that port

    with pytest.raises(AstraError, match="still active"):
        logbook.mission_start("another goal")

    note = registry.validate_and_call("journal_note", {"kind": "decision", "text": "Burn 856 m/s at UT 1000."})
    assert note["mission"] == folder.name and note["game_time"] is None
    assert "**decision**: Burn 856 m/s at UT 1000." in (folder / "journal.md").read_text(encoding="utf-8")

    (folder / "log.jsonl").write_text(
        '{"tool":"telemetry","ok":true}\n{"tool":"fly_burn","ok":false}\nnot json\n', encoding="utf-8")
    end = registry.validate_and_call("mission_end", {"outcome": "success", "summary": "Landed at 0.4 m/s."})
    assert (end["mission"], end["outcome"], end["tool_calls"], end["tool_errors"]) == (folder.name, "success", 2, 1)
    meta = json.loads((folder / "mission.json").read_text(encoding="utf-8"))
    assert meta["status"] == "success" and meta["summary"] == "Landed at 0.4 m/s."
    assert journal.active_mission() is None

    with pytest.raises(AstraError, match="no active mission"):
        logbook.mission_end("failure", "nothing")


def test_mission_tools_validate_arguments(ws):
    with pytest.raises(ValidationError):
        registry.validate_and_call("mission_end", {"outcome": "great", "summary": "x"})
    with pytest.raises(ValidationError):
        registry.validate_and_call("journal_note", {"kind": "musing", "text": "x"})
    with pytest.raises(AstraError):
        logbook.mission_start("   ")
    with pytest.raises(AstraError):
        logbook.journal_note("plan", "  ")


def test_note_without_mission_goes_to_unassigned(ws):
    out = logbook.journal_note("observation", "Apoapsis 81.2 km, periapsis -540 km.")
    assert out["mission"] is None and "note" in out
    assert Path(out["journal"]).parent.name == "_unassigned"


# ---------------------------------------------------------------------------------------------
# knowledge


def test_read_doctrine_lists_playbooks_and_lessons(ws):
    out = logbook.journal_read_doctrine()
    assert out["doctrine"] == (ws / "knowledge" / "doctrine.md").read_text(encoding="utf-8")
    assert {p["topic"] for p in out["playbooks"]} == PLAYBOOKS
    assert out["lessons_on_file"] >= 100
    assert out["active_mission"] is None


def test_read_doctrine_missing_file(ws):
    (ws / "knowledge" / "doctrine.md").unlink()
    with pytest.raises(AstraError, match="doctrine not found"):
        logbook.journal_read_doctrine()


@pytest.mark.parametrize("query, topic", [
    ("ascent", "ascent"),
    ("landing-airless", "landing-airless"),
    ("hoverslam", "landing-airless"),
    ("suicide burn on the Mun", "landing-airless"),
    ("reentry", "atmospheric-entry"),
    ("parachutes", "atmospheric-entry"),
    ("aerobraking", "atmospheric-entry"),
    ("Duna transfer window", "transfers"),
    ("docking", "rendezvous-docking"),
    ("rendevous", "rendezvous-docking"),
    ("fly_until triggers", "tools-cheatsheet"),
    ("rocket design", "rocket-design"),
    ("staging", "rocket-design"),
    ("capture", "capture"),
    ("circularize", "orbital-maneuvers"),
    ("plant a flag", "eva-surface-ops"),
    ("anomaly", "recovery-anomalies"),
    ("Tumbling", "recovery-anomalies"),
    # the phase names the doctrine uses
    ("design", "rocket-design"),
    ("orbit", "orbital-maneuvers"),
    ("transfer", "transfers"),
    ("landing", "landing-airless"),
    ("EVA", "eva-surface-ops"),
    ("rendezvous", "rendezvous-docking"),
    # subjects the crew asks about that have no playbook of their own
    ("MechJeb", "tools-cheatsheet"),
    ("hover", "landing-airless"),
    ("dv", "rocket-design"),
    ("relay", "rocket-design"),
])
def test_playbook_fuzzy_lookup(ws, query, topic):
    out = registry.validate_and_call("playbook", {"topic": query})
    assert out["topic"] == topic
    assert out["text"].startswith("# ") and out["title"]
    assert topic not in out["also_relevant"]


def test_playbook_miss_lists_topics(ws):
    with pytest.raises(AstraError) as err:
        logbook.playbook("quantum chromodynamics")
    assert "no playbook matches" in err.value.message
    assert all(t in err.value.hint for t in PLAYBOOKS)


def test_shipped_playbooks_are_well_formed():
    folder = REPO_ROOT / "knowledge" / "playbooks"
    assert {p.stem for p in folder.glob("*.md")} == PLAYBOOKS
    for p in folder.glob("*.md"):
        lines = p.read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("# "), p.name
        assert lines[2].startswith("Keywords: ") and len(lines[2].split(",")) >= 5, p.name
        assert "TODO" not in "\n".join(lines), p.name


def test_shipped_lessons_are_well_formed():
    text = (REPO_ROOT / "knowledge" / "lessons.md").read_text(encoding="utf-8")
    assert text.startswith("# Lessons learned in flight\n")
    entries = [ln for ln in text.splitlines() if ln.startswith("- ")]
    assert len(entries) >= 100
    pattern = re.compile(r"^- \[\d{4}-\d{2}-\d{2}\] \([a-z0-9-]+(, [a-z0-9-]+)*\) \S.{40,}$")
    bad = [ln for ln in entries if not pattern.match(ln)]
    assert not bad, bad[:3]
    bodies = [logbook._lesson_body(ln).lower() for ln in entries]
    assert len(bodies) == len(set(bodies)), "duplicate lessons"


def test_lessons_search_ranks_matching_lessons(ws):
    out = registry.validate_and_call("lessons_search", {"query": "mechjeb staging controller heat shield", "limit": 5})
    assert 1 <= len(out["lessons"]) <= 5
    assert "StagingController" in out["lessons"][0]
    assert out["lessons_on_file"] >= 100


def test_lesson_add_dedupes_and_normalizes(ws):
    text = "Capture burn at   Eve overshot\nbecause the throttle feathered too late near the apoapsis target."
    out = registry.validate_and_call("lesson_add", {"text": text, "tags": ["Capture", " Throttle ", "(x)"]})
    assert out["added"] is True
    assert out["entry"].endswith("(capture, throttle, x) Capture burn at Eve overshot because the throttle "
                                 "feathered too late near the apoapsis target.")
    again = logbook.lesson_add(text.upper(), [])
    assert again["added"] is False and again["duplicate_of"] == out["entry"]
    near = logbook.lesson_add("Capture burn at Eve overshot because the throttle feathered too late near the "
                              "apoapsis targets.", ["capture"])
    assert near["added"] is True and near["similar_existing"] == [out["entry"]]
    assert out["entry"] in logbook.lessons_search("eve overshot feathered", 3)["lessons"]
    with pytest.raises(AstraError, match="too short"):
        logbook.lesson_add("be careful", ["x"])


# ---------------------------------------------------------------------------------------------
# CAPCOM


def test_capcom_never_fails_when_the_game_is_down(ws):
    said = registry.validate_and_call("capcom_say", {"text": "Go for launch.", "level": "info"})
    assert said["delivered"] is False and "not running" in said["reason"]
    inbox = registry.validate_and_call("capcom_inbox", {"since_seq": 0})
    assert inbox["available"] is False and inbox["messages"] == []
    with pytest.raises(ValidationError):
        registry.validate_and_call("capcom_say", {"text": "x", "level": "panic"})


def test_capcom_panel(bridge):
    # Shapes as served by csharp/KspAutomationBridge/Capcom.cs.
    fake = bridge({
        ("GET", "/capcom?limit=1"): {"messages": [], "lastSeq": 7},
        ("GET", "/capcom/inbox?since=1"): {"messages": [
            {"seq": 2, "from": "player", "level": "info", "text": "go", "utc": "2026-09-29T10:00:00Z", "ut": 1.5e6},
            {"seq": 1, "from": "player", "text": "stale"}], "count": 2, "lastSeq": 7},
        ("POST", "/capcom"): {"seq": 8, "level": "warn", "shownOnScreen": True},
    })
    said = logbook.capcom_say("Δv margin 120 m/s", "warn")
    assert said == {"delivered": True, "via": "/capcom"}
    assert fake.posts("/capcom") == [{"text": "Δv margin 120 m/s", "level": "warn"}]

    inbox = registry.validate_and_call("capcom_inbox", {"since_seq": 1})
    assert inbox["available"] is True and inbox["last_seq"] == 7
    assert inbox["messages"] == [{"seq": 2, "text": "go", "from": "player", "time": "2026-09-29T10:00:00Z",
                                  "ut": 1.5e6}]

    logbook.journal_note("anomaly", "Thrust 0 after warp; SMA rising, so the readout lags.")
    assert fake.posts("/capcom")[-1] == {"text": "[anomaly] Thrust 0 after warp; SMA rising, so the readout lags.",
                                         "level": "warn"}
    assert not [r for r in fake.requests if r[1].startswith("/capcom/inbox") and r[1] != "/capcom/inbox?since=1"]


def test_capcom_falls_back_to_the_legacy_bridge(bridge):
    queue = ["abort the landing", None]
    fake = bridge({
        ("POST", "/status"): {"count": 1},
        ("GET", "/command/pending"): lambda body: {"command": queue.pop(0)},
    })
    said = logbook.capcom_say("Δv left: 340 m/s — landing at 2° tilt", "info")
    assert said["delivered"] is True and said["via"].startswith("/status")
    posted = fake.posts("/status")[0]
    assert posted["phase"] == "CAPCOM info"
    assert posted["message"] == "dv left: 340 m/s - landing at 2 deg tilt"  # ASCII only for the legacy parser

    inbox = logbook.capcom_inbox(0)
    assert inbox["available"] is True
    assert [m["text"] for m in inbox["messages"]] == ["abort the landing"]


def test_capcom_reprobes_when_the_bridge_changes(bridge):
    legacy = bridge({("POST", "/status"): {"count": 1}})
    assert logbook.capcom_say("first", "info")["via"].startswith("/status")
    assert logbook._panel is False
    legacy.close()
    modern = bridge({("GET", "/capcom?limit=1"): {"messages": []}, ("POST", "/capcom"): {}})
    logbook._panel = False  # stale knowledge from the old bridge; /status is gone on the new one
    said = logbook.capcom_say("second", "info")
    assert said == {"delivered": True, "via": "/capcom"}
    assert modern.posts("/capcom") == [{"text": "second", "level": "info"}]


def test_lesson_add_survives_a_file_without_final_newline(ws):
    path = journal.lessons_path()
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
    last = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")][-1]
    out = logbook.lesson_add("Parachutes armed while still supersonic opened late but intact at Duna.", ["chutes"])
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[-2:] == [last, out["entry"]]


def test_capture_playbooks_never_stop_on_a_hyperbolic_apoapsis():
    # On a hyperbola kRPC reports a negative apoapsis: "apoapsis <= target" fires before the burn starts.
    for name in ("capture.md", "tools-cheatsheet.md"):
        text = (REPO_ROOT / "knowledge" / "playbooks" / name).read_text(encoding="utf-8")
        assert '"metric": "apoapsis_altitude", "op": "<="' not in text, name
        assert '"metric": "eccentricity"' in text, name
    capture = (REPO_ROOT / "knowledge" / "playbooks" / "capture.md").read_text(encoding="utf-8")
    assert re.search(r"stop_when` on\s+`eccentricity <= e_target`", capture)


def test_capcom_say_defaults_to_info_and_failure_ends_as_alert(bridge):
    fake = bridge({("GET", "/capcom?limit=1"): {"messages": []}, ("POST", "/capcom"): {"seq": 1}})
    assert registry.validate_and_call("capcom_say", {"text": "Pad clear."})["delivered"] is True
    logbook.mission_start("Orbit Kerbin")
    logbook.mission_end("failure", "Fell back from 40 km.")
    posts = fake.posts("/capcom")
    assert posts[0] == {"text": "Pad clear.", "level": "info"}
    assert posts[-1]["level"] == "alert" and posts[-1]["text"].startswith("MISSION END (FAILURE)")


def test_game_clock_never_waits_for_a_busy_game(ws):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    _set(krpc_rpc_port=listener.getsockname()[1])  # "kRPC" answers; a reflex holds the controls
    held, release = threading.Event(), threading.Event()

    def reflex():
        with registry.GAME_LOCK:
            held.set()
            release.wait(5)

    worker = threading.Thread(target=reflex)
    worker.start()
    try:
        held.wait(5)
        note = logbook.journal_note("observation", "Coasting to apoapsis; nothing to do.")
        assert note["game_time"] is None
    finally:
        release.set()
        worker.join()
        listener.close()
