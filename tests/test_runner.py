"""The autonomous crew runner (astra.agent.runner / prompts), tested without starting a Claude session."""

import asyncio
import io
import json
import re
import sys

import pytest
from claude_agent_sdk import (AgentDefinition, AssistantMessage, ResultMessage, SystemMessage, TextBlock,
                              ThinkingBlock, ToolResultBlock, ToolUseBlock, UserMessage)

from astra import journal, registry
from astra.agent import prompts, runner
from astra.config import CONFIG, REPO_ROOT

GOAL = "Put a probe into a stable orbit around Kerbin"
CODE_TOOLS = ("Bash", "PowerShell", "Read", "Write", "Edit", "NotebookEdit", "WebFetch", "Glob", "Grep")


def _catalog() -> set[str]:
    """Tool names from the binding catalog in docs/ARCHITECTURE.md."""
    text = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    section = text.split("## Tool catalog", 1)[1].split("## Craft spec", 1)[0]
    return set(re.findall(r"`([a-z][a-z0-9_]*)\(", section))


@pytest.fixture
def missions(tmp_path):
    old = CONFIG.missions_dir
    object.__setattr__(CONFIG, "missions_dir", tmp_path / "missions")
    yield tmp_path / "missions"
    object.__setattr__(CONFIG, "missions_dir", old)


@pytest.fixture
def options(monkeypatch):
    monkeypatch.setenv("ASTRA_SAVE", "unit-test-save")
    monkeypatch.delenv("MCP_TOOL_TIMEOUT", raising=False)
    return runner.build_options(GOAL, model="claude-opus-5-5", max_turns=50, effort="high",
                                resume=None, max_budget_usd=5.0)


# ---------------------------------------------------------------------------------------------
# options


def test_crew_has_no_builtin_tools_that_run_or_write_code(options):
    assert options.tools == ["Agent"]
    assert not set(CODE_TOOLS) & set(options.tools)
    assert set(CODE_TOOLS) <= set(options.disallowed_tools)
    denied = set(options.disallowed_tools)
    assert {"Agent(general-purpose)", "Agent(Explore)", "Agent(Plan)", "Task(general-purpose)"} <= denied
    assert options.allowed_tools == ["mcp__astra", "Agent", "Task"]
    assert options.permission_mode == "dontAsk"
    assert options.setting_sources == []


def test_astra_mcp_server_is_the_only_server(options):
    assert options.strict_mcp_config is True
    assert list(options.mcp_servers) == ["astra"]
    server = options.mcp_servers["astra"]
    assert server["type"] == "stdio" and server["command"] == sys.executable
    assert server["args"] == ["-m", "astra", "serve"]
    assert server["env"]["PYTHONIOENCODING"] == "utf-8"
    assert server["env"]["ASTRA_SAVE"] == "unit-test-save"
    assert str(REPO_ROOT / "src") in server["env"]["PYTHONPATH"]


def test_session_settings_pass_through(options):
    assert (options.model, options.effort, options.max_turns, options.max_budget_usd, options.resume) == \
        ("claude-opus-5-5", "high", 50, 5.0, None)
    assert options.env["MCP_TOOL_TIMEOUT"] == runner.MCP_TOOL_TIMEOUT_MS
    assert options.cwd == str(REPO_ROOT)


def test_astra_tools_are_not_deferred_behind_a_tool_the_crew_lacks(options, monkeypatch):
    # tools=["Agent"] removes ToolSearch, so the CLI must load every astra schema up front.
    assert "ToolSearch" not in options.tools
    assert options.env["ENABLE_TOOL_SEARCH"] == "false"
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "auto")
    assert runner.build_options(GOAL).env["ENABLE_TOOL_SEARCH"] == "auto"  # an explicit choice wins


def test_system_prompt_carries_identity_doctrine_and_goal(options):
    doctrine = (REPO_ROOT / "knowledge" / "doctrine.md").read_text(encoding="utf-8").strip()
    sp = options.system_prompt
    assert isinstance(sp, str)
    assert doctrine in sp
    assert GOAL in sp
    assert "no shell" in sp and "mission_end" in sp and "capcom_inbox" in sp


def test_subagents_are_defined_with_astra_tools_only(options):
    assert set(options.agents) == {"booster-engineer", "fido"}
    for name, agent in options.agents.items():
        assert isinstance(agent, AgentDefinition)
        assert agent.description and len(agent.prompt) > 500
        assert agent.tools and all(t.startswith("mcp__astra__") for t in agent.tools), name
        assert len(agent.tools) == len(set(agent.tools)), name
        bare = {t.removeprefix("mcp__astra__") for t in agent.tools}
        assert not {t for t in bare if t.startswith(("fly_", "control_", "mj_", "game_", "crew_"))}, name
    engineer = {t.removeprefix("mcp__astra__") for t in options.agents["booster-engineer"].tools}
    fido = {t.removeprefix("mcp__astra__") for t in options.agents["fido"].tools}
    assert {"parts_search", "part_info", "design_check", "design_build", "design_from_craft", "craft_list",
            "compute_rocket"} <= engineer
    assert {"compute_maneuver", "compute_node_search", "compute_transfer_window", "orbit_info", "node_create",
            "node_list", "node_delete", "telemetry"} <= fido


def test_every_tool_named_in_prompts_is_in_the_catalog():
    catalog = _catalog()
    assert len(catalog) > 60
    named = set(re.findall(r"\b([a-z]+_[a-z_]+)\b", prompts.IDENTITY)) - {"mcp__astra__"}
    for spec in prompts.subagents().values():
        named |= {t.removeprefix("mcp__astra__") for t in spec.tools}
    assert named - catalog == set()


def test_every_tool_named_in_prompts_is_registered():
    registry.load_all()
    catalog = _catalog()
    if catalog - set(registry.TOOLS):
        pytest.skip(f"tool modules still missing: {sorted(catalog - set(registry.TOOLS))[:5]}...")
    for spec in prompts.subagents().values():
        assert {t.removeprefix("mcp__astra__") for t in spec.tools} <= set(registry.TOOLS)
    named = set(re.findall(r"\b([a-z]+_[a-z_]+)\b", prompts.IDENTITY)) - {"mcp__astra__"}
    assert named <= set(registry.TOOLS)
    # and the crew is told about every tool it has
    assert set(registry.TOOLS) <= set(re.findall(r"\b[a-z][a-z0-9_]*\b", prompts.IDENTITY))


# ---------------------------------------------------------------------------------------------
# prompts


def test_messages_steer_the_crew():
    first = prompts.first_message(GOAL)
    assert GOAL in first
    assert first.index("game_status") < first.index("journal_read_doctrine") < first.index("mission_start")
    resume = prompts.resume_message(GOAL)
    assert GOAL in resume and "telemetry" in resume and "mission_start" not in resume
    nudge = prompts.continue_message(GOAL)
    assert GOAL in nudge and "mission_end" in nudge


def test_subagent_files_parse(tmp_path):
    specs = prompts.subagents()
    for name, spec in specs.items():
        assert spec.name == name and spec.model == "inherit"
        assert spec.prompt and not spec.prompt.startswith("---")
    (tmp_path / "broken.md").write_text("no frontmatter here", encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        prompts.load_subagent("broken", tmp_path)
    (tmp_path / "quoted.md").write_text("---\nname: \"quoted\"\ndescription: 'Plans: burns'\ntools: a\n---\nB\n",
                                        encoding="utf-8")
    assert prompts.load_subagent("quoted", tmp_path).description == "Plans: burns"
    (tmp_path / "crlf.md").write_text("---\r\nname: crlf\r\ndescription: d\r\ntools: a, b\r\n---\r\nBody\r\n",
                                      encoding="utf-8")
    spec = prompts.load_subagent("crlf", tmp_path)
    assert (spec.name, spec.description, spec.tools, spec.prompt) == ("crlf", "d", ("a", "b"), "Body")


# ---------------------------------------------------------------------------------------------
# console and transcript


def _result(subtype="success", turns=3, cost=0.5, text="done"):
    return ResultMessage(subtype=subtype, duration_ms=60_000, duration_api_ms=50_000, is_error=subtype != "success",
                         num_turns=turns, session_id="sess-1", total_cost_usd=cost, result=text)


def test_console_shows_calls_errors_and_subagents():
    out = io.StringIO()
    con = runner.Console(out)
    con.show(SystemMessage("init", {"session_id": "sess-1", "model": "m", "tools": ["mcp__astra__telemetry", "Agent"],
                                    "mcp_servers": [{"name": "astra", "status": "connected"}]}))
    con.show(AssistantMessage(content=[TextBlock("Checking the pad."),
                                       ToolUseBlock("t1", "mcp__astra__telemetry", {"detail": "brief"}),
                                       ToolUseBlock("a1", "Agent", {"subagent_type": "fido", "description": "plan TMI"})],
                              model="m"))
    con.show(AssistantMessage(content=[ToolUseBlock("t2", "mcp__astra__orbit_info", {"of": "vessel"})], model="m",
                              parent_tool_use_id="a1"))
    con.show(UserMessage(content=[ToolResultBlock("t1", "boom\nHINT: fix it", is_error=True)]))
    con.show(UserMessage(content=[ToolResultBlock("a1", [{"type": "text", "text": "Burn 856 m/s"}])]))
    con.show(_result())
    text = out.getvalue()
    assert "astra server connected · 1 astra tools" in text
    assert "CREW  Checking the pad." in text
    assert "-> telemetry(detail=brief)" in text
    assert "=> fido: plan TMI" in text
    assert "[fido]   -> orbit_info(of=vessel)" in text
    assert "!! telemetry: boom HINT: fix it" in text
    assert "<= fido reported: Burn 856 m/s" in text
    assert "crew stopped: success · 3 turns · $0.50 · 1.0 min" in text


def test_format_call_is_one_short_line():
    line = runner.format_call("mcp__astra__fly_until", {"until": [{"metric": "apoapsis_altitude", "op": ">=",
                                                                   "value": 80000}] * 20, "throttle": 1})
    assert line.startswith("fly_until(until=") and "\n" not in line and len(line) < 230


def test_transcript_is_filed_into_the_mission(missions):
    t = runner.Transcript(GOAL)
    t.write(SystemMessage("init", {"session_id": "sess-9"}))
    mission = journal.start_mission(GOAL)
    t.write(AssistantMessage(content=[ThinkingBlock("hmm", "SIGNATURE"), TextBlock("hi")], model="m"))
    t.write(UserMessage(content=[ToolResultBlock("x", [{"type": "image", "source": {"data": "A" * 1000}}])]))
    t.note_mission()
    journal.end_mission("success", "ok")
    t.note_mission()  # mission closed: the transcript still belongs to it
    filed = t.close()
    assert filed is not None and filed.parent == mission
    rows = [json.loads(line) for line in filed.read_text(encoding="utf-8").splitlines()]
    assert [r["_type"] for r in rows] == ["SystemMessage", "AssistantMessage", "UserMessage"]
    assert "signature" not in rows[1]["content"][0] and rows[1]["content"][0]["thinking"] == "hmm"
    assert rows[2]["content"][0]["content"][0] == {"type": "image", "bytes_base64": 1000}
    assert (mission / "sessions.txt").read_text(encoding="utf-8").split() == [t.stamp, "sess-9"]
    assert runner.mission_outcome(mission) == "success"
    assert runner.mission_outcome(None) is None


def test_init_check_refuses_a_crew_without_instruments():
    ok = SystemMessage("init", {"tools": ["mcp__astra__telemetry"], "mcp_servers": [{"name": "astra", "status": "connected"}]})
    runner._check_init(ok)
    with pytest.raises(runner.CrewUnavailable, match="failed"):
        runner._check_init(SystemMessage("init", {"tools": [], "mcp_servers": [{"name": "astra", "status": "failed"}]}))
    with pytest.raises(runner.CrewUnavailable, match="no astra tools"):
        runner._check_init(SystemMessage("init", {"tools": ["Agent"], "mcp_servers": []}))
    with pytest.raises(runner.CrewUnavailable, match="needs-auth"):
        runner._check_init(SystemMessage("init", {"tools": [], "mcp_servers": [{"name": "astra", "status": "needs-auth"}]}))
    # still starting: its tools may arrive after the init message
    runner._check_init(SystemMessage("init", {"tools": ["Agent"], "mcp_servers": [{"name": "astra", "status": "pending"}]}))


# ---------------------------------------------------------------------------------------------
# the run loop, with a scripted stand-in for the Claude session


class ScriptedClient:
    """Replays one scripted response per query(); `on_query` hooks let a script change the world."""

    prompts: list[str] = []
    script: list = []

    def __init__(self, options=None):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        ScriptedClient.prompts.append(prompt)

    async def receive_response(self):
        step = ScriptedClient.script.pop(0)
        for msg in step():
            yield msg


def test_crew_is_sent_back_until_it_closes_the_mission(missions, monkeypatch):
    import claude_agent_sdk

    def first():
        journal.start_mission(GOAL)
        return [SystemMessage("init", {"tools": ["mcp__astra__game_status"],
                                       "mcp_servers": [{"name": "astra", "status": "connected"}]}),
                UserMessage(content=[ToolResultBlock("t", "ok")]), _result(text="Waiting for approval.")]

    def second():
        journal.end_mission("success", "orbit 80x81 km")
        return [UserMessage(content=[ToolResultBlock("t", "ok")]), _result(text="Mission complete.")]

    ScriptedClient.prompts, ScriptedClient.script = [], [first, second]
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", ScriptedClient)
    out = io.StringIO()
    transcript = runner.Transcript(GOAL)
    code, last = asyncio.run(runner._fly(GOAL, None, "go", None, None, runner.Console(out), transcript))
    transcript.close()
    assert (code, last.result) == (0, "Mission complete.")
    assert ScriptedClient.prompts == ["go", prompts.continue_message(GOAL)]
    assert transcript.mission is not None and runner.mission_outcome(transcript.mission) == "success"
    assert "sending the crew back to work (1/3)" in out.getvalue()


def test_run_stops_on_session_errors_and_limits(missions, monkeypatch):
    import claude_agent_sdk

    def open_mission():
        journal.start_mission(GOAL)
        return [_result(turns=40)]

    ScriptedClient.prompts, ScriptedClient.script = [], [open_mission]
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", ScriptedClient)
    out = io.StringIO()
    code, _ = asyncio.run(runner._fly(GOAL, None, "go", 30, None, runner.Console(out), runner.Transcript(GOAL)))
    assert code == 1 and "limit is reached" in out.getvalue()

    ScriptedClient.prompts, ScriptedClient.script = [], [lambda: [_result(subtype="error_max_turns")]]
    code, last = asyncio.run(runner._fly(GOAL, None, "go", None, None, runner.Console(io.StringIO()),
                                         runner.Transcript(GOAL)))
    assert code == 1 and last.subtype == "error_max_turns" and ScriptedClient.prompts == ["go"]


def test_run_mission_refuses_without_goal_or_game(missions, monkeypatch, capsys):
    assert runner.run_mission("   ") == 2
    monkeypatch.setattr(runner, "game_reachable", lambda: (False, False))
    assert runner.run_mission(GOAL) == 3
    assert "astra up" in capsys.readouterr().out


def test_session_errors_are_not_nudged(missions, monkeypatch):
    import claude_agent_sdk

    def failed_login():
        journal.start_mission(GOAL)
        return [AssistantMessage(content=[TextBlock("Failed to authenticate.")], model="m",
                                 error="authentication_failed"), _result(turns=1)]

    ScriptedClient.prompts, ScriptedClient.script = [], [failed_login]
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", ScriptedClient)
    out = io.StringIO()
    code, _ = asyncio.run(runner._fly(GOAL, None, "go", None, None, runner.Console(out), runner.Transcript(GOAL)))
    assert code == 1 and ScriptedClient.prompts == ["go"]
    assert "authentication_failed" in out.getvalue() and "log in again" in out.getvalue()


def test_interrupt_pauses_the_game_before_the_session_closes(missions, monkeypatch):
    import claude_agent_sdk

    order: list[str] = []

    class Session(ScriptedClient):
        async def __aexit__(self, *exc):
            order.append("session closed")
            return False

    def interrupted():
        raise asyncio.CancelledError  # what asyncio.run does to the task on Ctrl+C

    ScriptedClient.prompts, ScriptedClient.script = [], [interrupted]
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", Session)
    monkeypatch.setattr(runner, "leave_game_paused", lambda: order.append("paused") or "game paused")
    console = runner.Console(io.StringIO())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner._fly(GOAL, None, "go", None, None, console, runner.Transcript(GOAL)))
    assert order == ["paused", "session closed"]
    assert console.paused_on_exit is True


def test_run_mission_interrupted_returns_130_and_pauses_once(missions, monkeypatch, capsys):
    pauses: list[int] = []

    async def fly(goal, options, first, max_turns, budget, console, transcript):
        console.paused_on_exit = False  # interrupted before the session could pause the game
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "game_reachable", lambda: (True, True))
    monkeypatch.setattr(runner, "_fly", fly)
    monkeypatch.setattr(runner, "leave_game_paused", lambda: pauses.append(1) or "game paused")
    assert runner.run_mission(GOAL) == 130
    out = capsys.readouterr().out
    assert pauses == [1] and "interrupted" in out and "transcript:" in out


def test_crew_that_stops_before_opening_a_mission_is_sent_back(missions, monkeypatch):
    import claude_agent_sdk

    def asks_a_question():
        return [UserMessage(content=[ToolResultBlock("t", "ok")]), _result(text="Shall I proceed?")]

    def flies():  # a generator, so the world changes between the messages the runner sees
        journal.start_mission(GOAL)
        yield UserMessage(content=[ToolResultBlock("t", "mission started")])
        journal.end_mission("partial", "orbit reached, periapsis 65 km")
        yield UserMessage(content=[ToolResultBlock("t", "mission closed")])
        yield _result(text="Done.")

    ScriptedClient.prompts, ScriptedClient.script = [], [asks_a_question, flies]
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", ScriptedClient)
    out = io.StringIO()
    transcript = runner.Transcript(GOAL)
    code, last = asyncio.run(runner._fly(GOAL, None, "go", None, None, runner.Console(out), transcript))
    transcript.close()
    assert ScriptedClient.prompts == ["go", prompts.start_message(GOAL)]
    assert "not opened yet" in out.getvalue()
    # the session ended cleanly; the mission's own outcome decides the exit code in run_mission
    assert code == 0 and runner.mission_outcome(transcript.mission) == "partial"


def test_run_mission_without_doctrine_is_a_setup_error(missions, monkeypatch, tmp_path, capsys):
    old = CONFIG.knowledge_dir
    object.__setattr__(CONFIG, "knowledge_dir", tmp_path / "no-knowledge")
    monkeypatch.setattr(runner, "game_reachable", lambda: (True, True))
    try:
        assert runner.run_mission(GOAL) == 2
    finally:
        object.__setattr__(CONFIG, "knowledge_dir", old)
    assert "doctrine not found" in capsys.readouterr().out


def test_detach_from_parent_session(monkeypatch):
    from astra.agent import runner

    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_KEEP_ME", "x")
    dropped = runner.detach_from_parent_session()
    assert {"CLAUDECODE", "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH"} <= set(dropped)
    import os
    assert "CLAUDECODE" not in os.environ
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN_KEEP_ME"] == "x"  # user configuration is left alone
