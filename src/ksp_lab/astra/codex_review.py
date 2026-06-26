"""FORCED Codex (ChatGPT) review of every flown rocket three-view.

The USER DIRECTIVE for this module: *Claude designs and codes; Codex is FORCED to look at the
three-view drawing and keep handing back objections until the design passes.* So the design pipeline is
a two-model gate:

  * Claude's geometry gate (``design_chart.design_and_verify``) sizes the rocket, renders the
    orthographic side/front/top chart, rasterizes it to a real PNG, and runs ``looks_like_a_rocket``.
  * **Codex** (the ChatGPT model, run via the local ``codex`` CLI) then *looks at that PNG with its own
    eyes* and critiques the SHAPE — protruding mass, separation/staging sequence, booster height, exposed
    engines, payload housing — replying ``APPROVED — no objections`` per image or a numbered flaw list.

Only a design both gates approve is flown (see ``primitives.launch``). This is the "use Codex to see the
three-view, then iterate until it passes" half of the design loop; Claude does the fixing.

CLI contract (local Codex CLI, multimodal, OFFLINE-capable but network is the CLI's business not ours)::

    codex exec --sandbox read-only --skip-git-repo-check -i <png> [-i <png> ...]

The variadic ``-i/--image`` flag *eats* a trailing positional argument, so the critique PROMPT is **piped
on STDIN** rather than passed positionally (otherwise ``-i`` would swallow it as another image path). The
CLI prints the model's reply to stdout, which we parse.

Everything degrades gracefully: a missing CLI, a timeout, or a non-zero exit returns
``CodexVerdict(approved=False, flaws=["codex unavailable: ..."], raw=...)`` so the caller can decide to
fall back to Claude's gate rather than blocking a flight when Codex simply is not installed.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Callable, Iterator


# The default Codex CLI executable name. Overridable per-call for tests / alternate installs.
CODEX_BIN = "codex"

# The marker Codex is instructed to emit for an image it has no objections to. Matched case-insensitively
# and tolerant of the em-dash vs hyphen the model might use ("APPROVED — no objections").
_APPROVAL_TOKEN = "approved"

_PROMPT = """\
You are Codex, acting as an independent aerospace design reviewer for a Kerbal Space Program rocket.
You are being shown one or more THREE-VIEW engineering drawings (orthographic SIDE / FRONT / TOP views)
of a launch vehicle that another AI (Claude) designed and intends to FLY. Claude has already passed its
own geometry gate; your job is to catch what that gate missed by LOOKING at the picture.

Critique each image for these failure modes, in order:
  1. PROTRUDING MASS  — any payload/appendage sticking out past the fairing or body line that would rip
     off or wreck aerodynamics.
  2. SEPARATION / STAGING SEQUENCE — decouplers in the wrong place, stages that cannot cleanly separate,
     boosters that would collide with the core on jettison.
  3. BOOSTER HEIGHT — radial boosters taller than the core stage they are strapped to, or mounted so the
     nose/engine overhangs the core.
  4. EXPOSED ENGINES — upper-stage or interstage engine bells left naked in the airstream with no shroud.
  5. PAYLOAD HOUSING — payload not enclosed by a fairing that actually covers it.

For EACH image, respond on its own line:
  * If you have NO objections to that image, write exactly:  APPROVED — no objections
  * Otherwise, write a NUMBERED list of the specific flaws you see (one flaw per number).

Do not approve a design you have any concern about. Be terse and concrete.
"""


@dataclass(slots=True)
class CodexVerdict:
    """The parsed outcome of one Codex three-view review.

    ``approved`` is True ONLY when every reviewed image was approved and Codex listed no flaws.
    ``flaws`` carries the numbered objections (or the unavailability reason). ``raw`` is the unparsed
    CLI stdout, kept for logging / audit."""

    approved: bool
    flaws: list[str] = field(default_factory=list)
    raw: str = ""


def _build_command(png_paths: list[str], *, codex_bin: str) -> list[str]:
    """Assemble the ``codex exec`` argv with one ``-i <png>`` per image. The prompt is NOT here — it goes
    on stdin so the variadic ``-i`` does not eat it."""
    cmd = [codex_bin, "exec", "--sandbox", "read-only", "--skip-git-repo-check"]
    for p in png_paths:
        cmd += ["-i", str(p)]
    return cmd


def parse_codex_reply(stdout: str, *, image_count: int) -> CodexVerdict:
    """Parse Codex's stdout into a verdict.

    Rule (matches the prompt's instructions): the design is APPROVED iff
      * the reply mentions the approval token at least ``image_count`` times (one per image), AND
      * there are no numbered flaw lines.
    Any numbered ``N.`` / ``N)`` line is treated as a flaw. If nothing parses as either an approval or a
    flaw, we conservatively treat it as NOT approved with the raw text surfaced as a single flaw."""
    text = stdout or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    flaws: list[str] = []
    approvals = 0
    for ln in lines:
        low = ln.lower()
        # A numbered objection line: "1. foo", "2) bar". Approval lines never start with a bare number.
        stripped = low.lstrip("*-• ").lstrip()
        head = stripped.split(".", 1)[0].split(")", 1)[0]
        is_numbered = head.isdigit() and (stripped[len(head):].startswith(".") or stripped[len(head):].startswith(")"))
        if _APPROVAL_TOKEN in low and not is_numbered:
            approvals += 1
            continue
        if is_numbered:
            flaws.append(ln)

    approved = (approvals >= max(1, image_count)) and not flaws
    if not approved and not flaws:
        # Codex said something we could not classify as approval-per-image; do not silently pass it.
        snippet = text.strip()
        flaws = [f"codex reply not understood / not approved: {snippet[:300]}"] if snippet else \
                ["codex returned no parseable verdict"]
    return CodexVerdict(approved=approved, flaws=flaws, raw=text)


def codex_review_three_view(
    png_paths: list[str],
    *,
    context: str = "",
    timeout_s: int = 540,
    codex_bin: str = CODEX_BIN,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> CodexVerdict:
    """FORCE a Codex (ChatGPT) multimodal review of one or more rocket three-view PNGs.

    Invokes ``codex exec --sandbox read-only --skip-git-repo-check -i <png> ...`` with the critique
    prompt piped on STDIN, parses the reply, and returns a :class:`CodexVerdict`.

    Args:
        png_paths: paths to the rasterized three-view PNG(s) Codex must look at.
        context:   optional extra context (mission/body/payload) appended to the prompt.
        timeout_s: hard wall-clock budget for the CLI call (default 9 min).
        codex_bin: the CLI executable name (override for tests).
        runner:    the subprocess entry point (injectable so tests never spawn a real process).

    Never raises for an environment problem: a missing CLI, timeout, or non-zero exit yields
    ``approved=False`` with a clear ``codex unavailable`` flaw so the caller can fall back to Claude's
    gate instead of being blocked."""
    paths = [str(p) for p in (png_paths or [])]
    if not paths:
        return CodexVerdict(approved=False, flaws=["codex unavailable: no PNG to review"], raw="")

    prompt = _PROMPT
    if context:
        prompt = f"{prompt}\nADDITIONAL CONTEXT: {context}\n"

    cmd = _build_command(paths, codex_bin=codex_bin)
    try:
        proc = runner(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
        )
    except FileNotFoundError:
        return CodexVerdict(approved=False, flaws=[f"codex unavailable: '{codex_bin}' CLI not installed"], raw="")
    except subprocess.TimeoutExpired:
        return CodexVerdict(
            approved=False,
            flaws=[f"codex unavailable: review timed out after {timeout_s}s"],
            raw="",
        )
    except OSError as exc:  # permission, exec format, etc.
        return CodexVerdict(approved=False, flaws=[f"codex unavailable: {type(exc).__name__}: {exc}"], raw="")

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if proc.returncode != 0:
        reason = (stderr.strip() or stdout.strip() or f"exit code {proc.returncode}")
        return CodexVerdict(approved=False, flaws=[f"codex unavailable: CLI error: {reason[:300]}"], raw=stdout)

    return parse_codex_reply(stdout, image_count=len(paths))


def iterate_design_with_codex(
    render_fn: Callable[[], list[str]],
    fix_hint_sink: Callable[[list[str]], None] | None = None,
    *,
    max_rounds: int = 4,
    context: str = "",
    review_fn: Callable[..., CodexVerdict] = codex_review_three_view,
) -> Iterator[tuple[int, CodexVerdict]]:
    """Render -> Codex-review -> (if flaws) hand the flaws to Claude to fix -> re-render, until APPROVED
    or ``max_rounds`` is hit.

    This is the SCAFFOLD for the "iterate until it passes" loop. Claude owns the actual fixing: each round
    this generator

      1. calls ``render_fn()`` to (re)produce the three-view PNG path(s),
      2. runs Codex's review,
      3. YIELDS ``(round_index, verdict)`` so the caller (Claude's design step) can act,
      4. if not approved, forwards ``verdict.flaws`` to ``fix_hint_sink`` (where Claude applies fixes),
      5. stops early once a verdict is approved.

    The generator does not itself edit any design — it only sequences render/review/hand-off, keeping the
    fix authored by Claude. Yielding (rather than returning) lets the caller log every round."""
    rounds = max(1, int(max_rounds))
    for i in range(rounds):
        pngs = render_fn() or []
        verdict = review_fn(pngs, context=context)
        yield i, verdict
        if verdict.approved:
            return
        if fix_hint_sink is not None and verdict.flaws:
            fix_hint_sink(verdict.flaws)
    # Exhausted max_rounds without approval; the caller's last yielded verdict carries the standing flaws.
