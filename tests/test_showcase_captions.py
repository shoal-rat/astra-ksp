"""Offline tests for the English caption chunker and the storyboard overlay in scripts/showcase/make_video.py."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("showcase_make_video", ROOT / "scripts" / "showcase" / "make_video.py")
mv = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mv  # dataclasses look the module up while it executes
_SPEC.loader.exec_module(mv)

LIMIT = mv.CAPTION_LIMIT

NARRATIONS = [
    "This is Mars. Well, to be precise, it's Duna, in the Kerbal universe. The astronaut who planted this flag "
    "was flown by an AI, from designing the rocket to launch, flight and landing.",
    "The hard part is landing. ASTRA's landing guidance was rebuilt by the AI flight after flight: at every moment "
    "it numerically integrates a braking burn to predict where it would stop; it scans the terrain ahead and lights "
    "the engine early for rising ground; near the surface it hovers, kills the sideways drift, then drops straight "
    "down. Touchdown speed: 0.03 m/s.",
    "The AI used a Lambert solver to scan for a launch window, launched at dawn, and left Kerbin at 1,090 meters "
    "per second — then, 300-odd days later (after two corrections), Duna captured it.",
    "Short.",
    "It's open source: github.com/shoal-rat/astra-ksp/tree/main/scripts/showcase/make_video.py/and-some-more — "
    "link below.",
]


def fake_words(text: str, drop_every: int = 0) -> list[list]:
    """Word boundaries shaped like edge-tts WordBoundary events: punctuation stripped, times in seconds."""
    return [w for _, w in _timed_tokens(text, drop_every)]


def _timed_tokens(text: str, drop_every: int = 0) -> list[tuple[int, list]]:
    """(token index, [start_s, end_s, word]) for every whitespace token that is spoken and not dropped."""
    out, t = [], 0.1
    for k, tok in enumerate(text.split()):
        word = tok.strip(".,;:!?()\"“”—–")
        if not word:
            t += 0.15
            continue
        dur = 0.06 * len(word) + 0.1
        if not (drop_every and k % drop_every == drop_every - 1):
            out.append((k, [round(t, 4), round(t + dur, 4), word]))
        t += dur + (0.5 if tok.endswith((".", "?", "!")) else 0.05)
    return out


def check_captions(text: str, caps: list[tuple[float, float, str]]) -> None:
    tokens = text.split()
    # chunks are whole words of the narration, in order, with their punctuation: they join back to it
    assert " ".join(c for _, _, c in caps) == " ".join(tokens)
    assert [w for _, _, c in caps for w in c.split()] == tokens  # no word was split or merged
    for _, _, c in caps:
        assert len(c) <= LIMIT or len(c.split()) == 1, c
    # timings: each chunk starts no earlier than the previous one ends, and ends no earlier than it starts
    for k, (start, end, _) in enumerate(caps):
        assert 0.0 <= start <= end
        if k:
            assert caps[k - 1][1] <= start
            assert caps[k - 1][0] <= start


@pytest.mark.parametrize("text", NARRATIONS)
def test_word_captions_cover_the_narration(text):
    words = fake_words(text)
    caps = mv.word_captions(text, words, duration=words[-1][1] + 0.8)
    check_captions(text, caps)
    assert caps[0][0] == pytest.approx(words[0][0])  # the first caption starts with the first spoken word
    assert caps[-1][1] <= words[-1][1] + 0.8


@pytest.mark.parametrize("text", NARRATIONS)
def test_chunk_starts_at_its_first_words_start(text):
    timed = _timed_tokens(text)
    starts = {k: w[0] for k, w in timed}
    caps = mv.word_captions(text, [w for _, w in timed])
    first = 0  # token index of each caption's first word
    for start, _, c in caps:
        if first in starts:
            assert start == pytest.approx(starts[first])
        first += len(c.split())


def test_long_word_is_its_own_chunk_and_never_split():
    text = NARRATIONS[-1]
    long_word = next(w for w in text.split() if len(w) > LIMIT)
    chunks = mv.caption_chunks(text)
    assert long_word in chunks
    assert all(len(c) <= LIMIT for c in chunks if c != long_word)


def test_missing_words_are_interpolated():
    text = NARRATIONS[1]
    caps = mv.word_captions(text, fake_words(text, drop_every=3), duration=40.0)
    check_captions(text, caps)


def test_no_words_spreads_over_duration():
    text = NARRATIONS[0]
    caps = mv.word_captions(text, [], duration=12.0)
    check_captions(text, caps)
    assert caps[-1][1] <= 12.0


def test_multi_token_boundaries_align():
    # edge-tts reports "0.03 m/s" and "13 km" as single boundary words spanning a space
    text = "Touchdown at 0.03 m/s. It hovered 13 km up."
    words = [[0.1, 0.5, "Touchdown"], [0.5, 0.6, "at"], [0.6, 2.0, "0.03 m/s"], [2.4, 2.5, "It"],
             [2.5, 2.9, "hovered"], [2.9, 3.6, "13 km"], [3.6, 3.8, "up"]]
    caps = mv.word_captions(text, words, limit=24)
    check_captions(text, caps)
    assert [c for _, _, c in caps] == ["Touchdown at 0.03 m/s.", "It hovered 13 km up."]
    assert caps[0][0] == pytest.approx(0.1)
    assert caps[1][0] == pytest.approx(2.4)
    assert caps[0][1] == pytest.approx(2.4)  # a gap under a second is bridged


def test_breaks_prefer_sentence_ends_and_punctuation():
    chunks = mv.caption_chunks(NARRATIONS[0])
    assert chunks[0] == "This is Mars."
    assert all(not c.split()[-1].lower() in ("the", "a", "an", "to", "of") for c in chunks)
    for c in chunks:  # no chunk runs from the end of one sentence into the middle of the next
        inner = [w for w in c.split()[:-1] if w.endswith(".")]
        assert not inner or c.endswith("."), c


def test_overlay_requires_english_for_every_narrated_segment(tmp_path):
    sb = json.loads(mv.STORYBOARD.read_text(encoding="utf-8"))
    narrated = [s["id"] for s in sb["segments"] if s.get("narration")]
    segs = {s["id"]: {"narration": "Hello.", "chapter": "Chapter"} for s in sb["segments"] if s.get("narration")}
    overlay = {"voice": "en-US-AndrewNeural", "rate": "+0%", "badge": "Badge", "title": "T", "segments": segs}
    path = tmp_path / "ok.en.json"
    path.write_text(json.dumps(overlay), encoding="utf-8")
    merged = mv.load_storyboard("en", path)
    assert all(s["narration"] == "Hello." for s in merged["segments"] if s["id"] in narrated)
    assert merged["voice"] == "en-US-AndrewNeural" and merged["badge"] == "Badge"
    journal = [v for s in merged["segments"] for v in s["visual"] if v.get("card") == "journal"]
    assert journal and all("title" not in v for v in journal)  # the zh card title never leaks into en

    del segs[narrated[0]]
    path.write_text(json.dumps(overlay), encoding="utf-8")
    with pytest.raises(SystemExit, match=narrated[0]):
        mv.load_storyboard("en", path)
