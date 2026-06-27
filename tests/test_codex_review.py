"""Tests for the FORCED Codex three-view review.

These NEVER spawn the real ``codex`` CLI or touch the network — every call to ``subprocess.run`` is
mocked. We verify the argv contract (multimodal ``-i`` per image, prompt on stdin), the APPROVED vs
flaw-list parsing, and graceful degradation when the CLI is missing / times out / errors.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ksp_lab.astra import codex_review  # noqa: E402
from ksp_lab.astra.codex_review import CodexVerdict, codex_review_three_view  # noqa: E402


def _fake_run(stdout="", stderr="", returncode=0, *, capture=None):
    """Build a fake subprocess.run that records the call and returns a CompletedProcess."""
    def runner(cmd, **kwargs):
        if capture is not None:
            capture["cmd"] = cmd
            capture["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=stdout, stderr=stderr)
    return runner


def test_invokes_cli_multimodally_with_prompt_on_stdin():
    cap: dict = {}
    runner = _fake_run(stdout="APPROVED — no objections", capture=cap)
    codex_review_three_view(["a.png", "b.png"], runner=runner, codex_bin="codex")

    cmd = cap["cmd"]
    # cmd[0] is the RESOLVED codex executable (on Windows the vendored codex.exe, since the bare name + a
    # .cmd shim can't be exec'd by subprocess); the exec sub-args follow unchanged.
    assert "codex" in cmd[0].lower()
    assert cmd[1:5] == ["exec", "--sandbox", "read-only", "--skip-git-repo-check"]
    # one -i per image, multimodal
    assert cmd.count("-i") == 2
    assert "a.png" in cmd and "b.png" in cmd
    # the prompt goes on STDIN (so variadic -i doesn't eat it), not as a positional arg
    assert "input" in cap["kwargs"] and "three-view" in cap["kwargs"]["input"].lower()
    assert cap["kwargs"].get("encoding") == "utf-8"


def test_approved_response_single_image():
    runner = _fake_run(stdout="APPROVED — no objections")
    v = codex_review_three_view(["x.png"], runner=runner)
    assert isinstance(v, CodexVerdict)
    assert v.approved is True
    assert v.flaws == []


def test_approved_requires_one_approval_per_image():
    # two images but only one APPROVED line -> NOT approved
    runner = _fake_run(stdout="APPROVED — no objections")
    v = codex_review_three_view(["x.png", "y.png"], runner=runner)
    assert v.approved is False


def test_flaw_list_response_is_not_approved():
    reply = (
        "1. The payload antenna protrudes past the fairing and will shear off.\n"
        "2. Radial boosters are taller than the core stage.\n"
    )
    runner = _fake_run(stdout=reply)
    v = codex_review_three_view(["x.png"], runner=runner)
    assert v.approved is False
    assert len(v.flaws) == 2
    assert any("antenna" in f for f in v.flaws)


def test_mixed_approval_and_flaw_is_not_approved():
    reply = "APPROVED — no objections\n1. Upper-stage engine bell is exposed, add a shroud."
    runner = _fake_run(stdout=reply)
    v = codex_review_three_view(["x.png"], runner=runner)
    assert v.approved is False
    assert any("shroud" in f for f in v.flaws)


def test_unparseable_reply_is_conservatively_not_approved():
    runner = _fake_run(stdout="hmm, looks fine I guess")
    v = codex_review_three_view(["x.png"], runner=runner)
    assert v.approved is False
    assert v.flaws  # surfaced as a flaw rather than silently passing


def test_codex_missing_degrades_gracefully():
    def runner(cmd, **kwargs):
        raise FileNotFoundError("codex not found")
    v = codex_review_three_view(["x.png"], runner=runner)
    assert v.approved is False
    assert any("codex unavailable" in f for f in v.flaws)


def test_codex_timeout_degrades_gracefully():
    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)
    v = codex_review_three_view(["x.png"], runner=runner, timeout_s=1)
    assert v.approved is False
    assert any("timed out" in f for f in v.flaws)


def test_codex_nonzero_exit_degrades_gracefully():
    runner = _fake_run(stdout="", stderr="boom: sandbox denied", returncode=3)
    v = codex_review_three_view(["x.png"], runner=runner)
    assert v.approved is False
    assert any("codex unavailable" in f for f in v.flaws)


def test_no_pngs_is_unavailable():
    v = codex_review_three_view([])
    assert v.approved is False
    assert any("no PNG" in f for f in v.flaws)


def test_context_is_appended_to_prompt():
    cap: dict = {}
    runner = _fake_run(stdout="APPROVED — no objections", capture=cap)
    codex_review_three_view(["x.png"], context="crew=3 to Mun", runner=runner)
    assert "crew=3 to Mun" in cap["kwargs"]["input"]


def test_iterate_stops_on_approval():
    calls = {"render": 0, "fixes": 0}

    def render_fn():
        calls["render"] += 1
        return ["x.png"]

    def fix_sink(flaws):
        calls["fixes"] += 1

    # First review flaws, second approves.
    replies = iter([
        CodexVerdict(approved=False, flaws=["1. exposed engine"]),
        CodexVerdict(approved=True, flaws=[]),
    ])

    def review_fn(pngs, **kwargs):
        return next(replies)

    rounds = list(codex_review.iterate_design_with_codex(
        render_fn, fix_sink, max_rounds=4, review_fn=review_fn))
    assert len(rounds) == 2
    assert rounds[-1][1].approved is True
    assert calls["render"] == 2          # re-rendered after the fix
    assert calls["fixes"] == 1           # one fix handoff


def test_iterate_exhausts_max_rounds_without_approval():
    def render_fn():
        return ["x.png"]

    def review_fn(pngs, **kwargs):
        return CodexVerdict(approved=False, flaws=["1. still bad"])

    rounds = list(codex_review.iterate_design_with_codex(
        render_fn, None, max_rounds=3, review_fn=review_fn))
    assert len(rounds) == 3
    assert all(not v.approved for _, v in rounds)
