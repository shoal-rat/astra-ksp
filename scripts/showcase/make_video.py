"""Build the ASTRA showcase video from the flight recordings (video production, not flight code).

    .venv/Scripts/python.exe scripts/showcase/make_video.py [--lang zh|en] [--text PATH] [--only SEG ...] [--out PATH]

Inputs: scripts/showcase/storyboard.json (segments: narration + visuals), recordings/<name>/video.mkv
with frames.csv and marks.csv from `astra record`, and photos in docs/media. Output: an H.264/AAC MP4
at 1920x1080 30 fps with narration (edge-tts), subtitles, the tool calls the AI made (from the
recorder's marks), a HUD from the recorded telemetry, and a synthesized music bed (no third-party
music). Intermediate files go to recordings/showcase/ (git-ignored); TTS is cached in its tts/ folder,
shared by both languages (the cache key includes the voice).

--lang zh (the default) builds the Chinese cut for Bilibili from storyboard.json as written: work files
in recordings/showcase/, output astra_showcase.mp4 there, plus cover.jpg and chapters.txt.

--lang en builds the English cut for YouTube. --text PATH (default scripts/showcase/storyboard.en.json)
is an overlay merged over storyboard.json, which keeps the visuals, the Chinese text and the segment
ids. The overlay gives "title", "voice", "rate" and "badge" at the top and, under "segments", per
segment id: "narration" (required for every narrated segment), "chapter" (for segments that have one)
and "title" (the journal card's title). Work files go to recordings/showcase/en/; outputs are
astra_showcase_en.mp4, astra_cover_en.jpg (the thumbnail) and chapters_en.txt ("00:00 Name" per line)
in recordings/showcase/. On-screen text comes from the per-language table TEXT, and English subtitles
are timed word by word from the TTS word boundaries. --text also works with --lang zh (re-voicing).
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import wave
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
STORYBOARD = ROOT / "scripts" / "showcase" / "storyboard.json"
WORK = ROOT / "recordings" / "showcase"  # the zh work directory; other languages work in WORK/<lang>
FF = imageio_ffmpeg.get_ffmpeg_exe()
W, H, FPS = 1920, 1080, 30
REC_FPS = 15.0
SR = 48000
LEAD, TAIL = 0.5, 1.2  # seconds of picture before and after each segment's narration
FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_EN = "C:/Windows/Fonts/segoeui.ttf"
FONT_EN_BOLD = "C:/Windows/Fonts/segoeuib.ttf"
MONO = "C:/Windows/Fonts/consola.ttf"
ORANGE, CYAN, WHITE = (255, 138, 61), (79, 195, 247), (240, 244, 248)
MARGIN = 80  # a centred card line is shrunk until it fits W - 2 * MARGIN

# Everything the video shows on screen, per language. Numbers must stay identical across languages.
# Templates take the live stats: {tools} and {tests}.
TEXT: dict[str, dict] = {
    "zh": {
        "fonts": (FONT, FONT_BOLD),
        "ass_font": "Microsoft YaHei",
        "sub_size": 54,
        "boundary": "SentenceBoundary",  # captions: TTS sentences split at commas
        "title": ("让 AI 当宇航员：从设计火箭到登陆火星", "Claude × Kerbal Space Program · 全程 AI 自主操作"),
        "loop_title": "AI 的工作方式：没有任务脚本，只有工具",
        "loop_steps": ("观察", "计算", "决策", "执行", "验证"),
        "loop_lines": ("{tools} 个工具：遥测 · 轨道计算 · 火箭设计 · 控制 · 反射动作 · MechJeb · 出舱 · 任务日志",
                       "AI 思考时游戏自动暂停；只有在 AI 设定的触发条件下，游戏时间才会前进",
                       "每一个数字：远地点、点火时间、俯仰程序、着陆时机，都由 AI 当场计算"),
        "design_title": "AI 设计的登月火箭：ASTRA Mun Lander",
        "design_rows": (("级", "发动机", "Δv（真空）", "推重比", "任务"),
                        ("1", "Mainsail ×1", "3644 m/s", "1.89", "上升入轨"),
                        ("2", "Terrier（转移级）", "1773 m/s", "0.64*", "奔月、捕获、离轨"),
                        ("3", "Terrier（着陆器）", "1841 m/s", "7.7†", "着陆、起飞、返航"),
                        ("4", "Mk1 返回舱", "—", "—", "隔热罩 + 降落伞")),
        "design_cols": (140, 330, 830, 1150, 1400),
        "design_notes": ("30 个零件 · 74.5 吨 · 零件树由 AI 根据游戏实时零件数据写出，工具生成 .craft 文件",
                         "* 真空推重比（坎星重力）  † 月球表面推重比"),
        "lessons_title": "失败 → 教训 → 新代码",
        "lessons": (("一个零件报告了 ±6×10¹⁷ 米的尺寸", "着陆器在 13 公里高空悬停 → 改为逐零件测量高度"),
                    ("前方山坡比平地模型高 450 米", "以 35 m/s 撞地 → 扫描前方地形，提前制动"),
                    ("低推重比时减速模型失准", "以 118 m/s 撞地 → 数值积分预测 + 高度优先制动"),
                    ("宇航员出舱时着陆器翻倒", "→ 背包喷气跳离 + 探测核心保持 SAS")),
        "journal_title": "AI 的飞行日志（原文）",
        "outro_title": "ASTRA 已开源",
        "outro_chips": ("{tools} 个工具", "0 行任务脚本", "{tests} 个测试", "月球 + 杜娜 两面旗帜"),
        "outro_cta": "点赞 · 投币 · 关注，我们下次任务见",
        "cover": (("AI 全自动登陆火星", 120, 50), ("设计火箭 · 发射 · 着陆 · 插旗，全程无人工操作", 54, 215)),
        "bodies": {"Kerbin": "坎星", "Mun": "月球", "Duna": "杜娜（火星）", "Sun": "日心轨道", "Ike": "艾克"},
        "hud": {"alt": "高度 {:,.0f} m", "orbital": "轨道 {:,.1f} m/s", "ground": "地速 {:,.1f} m/s",
                "vertical": "垂直 {:+,.1f} m/s", "warp": "时间加速 ×{:g}", "video": "视频 ×{:.0f}"},
    },
    "en": {
        "fonts": (FONT_EN, FONT_EN_BOLD),
        "ass_font": "Segoe UI",
        "sub_size": 52,
        "boundary": "WordBoundary",  # captions: chunks of the narration timed word by word
        "title": ("An AI astronaut: from rocket design to landing on Mars",
                  "Claude × Kerbal Space Program · every action by the AI"),
        "loop_title": "How the AI works: no mission scripts, only tools",
        "loop_steps": ("OBSERVE", "COMPUTE", "DECIDE", "ACT", "VERIFY"),
        "loop_lines": ("{tools} tools: telemetry · orbital math · rocket design · control · reflexes · MechJeb · EVA"
                       " · mission log",
                       "The game pauses while the AI thinks; time runs only until a trigger the AI chose fires",
                       "Every number (apoapsis, burn time, pitch program, landing timing) is computed by the AI"
                       " on the spot"),
        "design_title": "The AI's Mun rocket: ASTRA Mun Lander",
        "design_rows": (("Stage", "Engine", "Δv (vacuum)", "TWR", "Job"),
                        ("1", "Mainsail ×1", "3644 m/s", "1.89", "Ascent to orbit"),
                        ("2", "Terrier (transfer)", "1773 m/s", "0.64*", "To the Mun, capture, deorbit"),
                        ("3", "Terrier (lander)", "1841 m/s", "7.7†", "Land, lift off, fly home"),
                        ("4", "Mk1 return capsule", "—", "—", "Heat shield + parachute")),
        "design_cols": (140, 305, 745, 1065, 1225),
        "design_notes": ("30 parts · 74.5 t · the AI wrote the part tree from live part data; a tool generated the"
                         " .craft file",
                         "* vacuum TWR (Kerbin gravity)   † TWR on the Mun's surface"),
        "lessons_title": "Failure → lesson → new code",
        "lessons": (("A part reported a size of ±6×10¹⁷ m",
                     "The lander hovered 13 km up → measure height part by part"),
                    ("The slope ahead was 450 m higher than the flat-ground model",
                     "Hit the ground at 35 m/s → scan the terrain ahead, brake early"),
                    ("The braking model failed at low thrust-to-weight",
                     "Hit the ground at 118 m/s → numerical braking prediction + height-first braking"),
                    ("The lander tipped over when the kerbal climbed out",
                     "→ jetpack hop clear of the lander + a probe core keeps SAS on")),
        "journal_title": "The AI's flight log (verbatim)",
        "outro_title": "ASTRA is open source",
        "outro_chips": ("{tools} tools", "0 mission scripts", "{tests} tests", "Flags on the Mun and Duna"),
        "outro_cta": "Like · Subscribe · See you on the next mission",
        "cover": (("AI Lands on Mars", 165, 22),
                  ("Designs the rocket · launches · lands · plants the flag — no human at the controls", 54, 240)),
        "bodies": {"Sun": "Sun orbit"},  # other bodies keep their in-game names
        "hud": {"alt": "altitude {:,.0f} m", "orbital": "orbital speed {:,.1f} m/s",
                "ground": "ground speed {:,.1f} m/s", "vertical": "vertical {:+,.1f} m/s",
                "warp": "time warp ×{:g}", "video": "video ×{:.0f}"},
    },
}


def workdir(lang: str) -> Path:
    """Intermediate files of one language's build (the zh build keeps the original layout)."""
    return WORK if lang == "zh" else WORK / lang


def ffmpeg(*args: str, cwd: Path | None = None) -> None:
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", *args], check=True, cwd=cwd)


def decode_audio(path: Path) -> np.ndarray:
    out = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "f32le", "-ac", "1",
                          "-ar", str(SR), "-"], check=True, capture_output=True).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


# -- narration -------------------------------------------------------------------------------------


async def _tts(text: str, voice: str, rate: str, mp3: Path, boundary: str) -> list[list]:
    import edge_tts

    comm = edge_tts.Communicate(text, voice, rate=rate, boundary=boundary)
    subs: list[list] = []
    with mp3.open("wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] in ("SentenceBoundary", "WordBoundary"):
                subs.append([chunk["offset"] / 1e7, (chunk["offset"] + chunk["duration"]) / 1e7, chunk["text"]])
    return subs


def narration(seg: dict, voice: str, rate: str, boundary: str = "SentenceBoundary") -> tuple[np.ndarray, list[list]]:
    """Speech for a segment and its boundary events [start_s, end_s, text] (sentences or words)."""
    # the boundary kind is part of the key; sentence keys keep their original form so the zh cache stays valid
    kind = "" if boundary == "SentenceBoundary" else f"{boundary}|"
    key = hashlib.sha1(f"{voice}|{rate}|{kind}{seg['narration']}".encode("utf-8")).hexdigest()[:12]
    mp3, meta = WORK / "tts" / f"{seg['id']}_{key}.mp3", WORK / "tts" / f"{seg['id']}_{key}.json"
    mp3.parent.mkdir(parents=True, exist_ok=True)
    if not (mp3.exists() and meta.exists()):
        for attempt in range(4):  # the TTS service occasionally stalls mid-stream
            try:
                subs = asyncio.run(asyncio.wait_for(_tts(seg["narration"], voice, rate, mp3, boundary), 90))
                break
            except (asyncio.TimeoutError, OSError, RuntimeError) as exc:
                print(f"  tts {seg['id']}: attempt {attempt + 1} failed ({type(exc).__name__}); retrying", flush=True)
                mp3.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"text-to-speech failed for segment {seg['id']}")
        meta.write_text(json.dumps(subs, ensure_ascii=False), encoding="utf-8")
    return decode_audio(mp3), json.loads(meta.read_text(encoding="utf-8"))


# -- recordings ------------------------------------------------------------------------------------


@dataclass
class Recording:
    name: str
    frames: dict[int, dict] = field(default_factory=dict)
    marks: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, name: str) -> "Recording":
        d = ROOT / "recordings" / name
        rec = cls(name)
        with (d / "frames.csv").open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rec.frames[int(row["frame"])] = row
        with (d / "marks.csv").open(encoding="utf-8") as f:
            rec.marks = [dict(r, frame=int(r["frame"])) for r in csv.DictReader(f)]
        return rec

    @property
    def video(self) -> Path:
        return ROOT / "recordings" / self.name / "video.mkv"


def fnum(x: str | None, default: float = float("nan")) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# -- cards -----------------------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _font(size: int, bold: bool = False, mono: bool = False, lang: str = "zh") -> ImageFont.FreeTypeFont:
    regular, heavy = TEXT[lang]["fonts"]
    return ImageFont.truetype(MONO if mono else heavy if bold else regular, size)


def _fit(d: ImageDraw.ImageDraw, texts, size: int, max_w: float, bold: bool = False, mono: bool = False,
         lang: str = "zh") -> ImageFont.FreeTypeFont:
    """The largest font of at most `size` px in which every one of `texts` is at most max_w px wide."""
    while size > 12 and max(d.textlength(t, font=_font(size, bold, mono, lang)) for t in texts) > max_w:
        size -= 1
    return _font(size, bold, mono, lang)


def _backdrop(seed: int) -> Image.Image:
    rng = np.random.default_rng(seed)
    y = np.linspace(0, 1, H)[:, None]
    base = np.zeros((H, W, 3))
    base[..., 0] = 8 + 20 * y
    base[..., 1] = 12 + 14 * y
    base[..., 2] = 26 + 30 * (1 - y)
    img = Image.fromarray(base.astype(np.uint8))
    d = ImageDraw.Draw(img)
    for _ in range(420):
        x, yy = rng.integers(0, W), rng.integers(0, H)
        b = int(rng.integers(90, 255))
        r = 1 if rng.random() < 0.9 else 2
        d.ellipse([x, yy, x + r, yy + r], fill=(b, b, min(255, b + 20)))
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W * 0.55, H * 0.35, W * 1.4, H * 1.6], fill=(120, 50, 25))
    return Image.blend(img, glow.filter(ImageFilter.GaussianBlur(160)), 0.35)


def _center(d: ImageDraw.ImageDraw, y: int, text: str, font, fill) -> None:
    w = d.textlength(text, font=font)
    d.text(((W - w) / 2, y), text, font=font, fill=fill)


def card(item: dict, stats: dict, lang: str = "zh", out_dir: Path | None = None) -> Path:
    """Render one storyboard card in the given language to <out_dir or the language's work dir/cards>."""
    T = TEXT[lang]
    kind = item["card"]
    img = _backdrop({"title": 1, "loop": 2, "design": 3, "lessons": 4, "outro": 5}.get(kind, 9))
    d = ImageDraw.Draw(img)

    def font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
        return _font(size, bold, mono, lang)

    def fit(texts, size: int, max_w: float, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
        return _fit(d, texts, size, max_w, bold, mono, lang)

    def center(y: int, text: str, size: int, fill, bold: bool = False, mono: bool = False) -> None:
        _center(d, y, text, fit([text], size, W - 2 * MARGIN, bold, mono), fill)

    if kind == "title":
        center(300, "ASTRA", 190, WHITE, bold=True)
        center(540, T["title"][0], 64, ORANGE)
        center(650, T["title"][1], 40, CYAN)
    elif kind == "loop":
        center(110, T["loop_title"], 62, WHITE, bold=True)
        steps = T["loop_steps"]
        x0, bw, gap, y = 170, 250, 70, 330
        sf = fit(steps, 64, bw - 56, bold=True)  # one size for all five boxes
        for i, s in enumerate(steps):
            x = x0 + i * (bw + gap)
            d.rounded_rectangle([x, y, x + bw, y + 150], 24, outline=CYAN, width=5, fill=(18, 30, 52))
            tw = d.textlength(s, font=sf)
            d.text((x + (bw - tw) / 2, y + 36 + (64 - sf.size) / 2), s, font=sf, fill=WHITE)
            if i < len(steps) - 1:
                ax = x + bw + 10
                d.polygon([(ax, y + 60), (ax + gap - 20, y + 75), (ax, y + 90)], fill=ORANGE)
        lines = [t.format(**stats) for t in T["loop_lines"]]
        lf = fit(lines, 40, W - 2 * MARGIN)
        for i, t in enumerate(lines):
            _center(d, 590 + i * 80, t, lf, WHITE if i else ORANGE)
    elif kind == "design":
        center(90, T["design_title"], 60, WHITE, bold=True)
        rows, cols = T["design_rows"], T["design_cols"]
        ends = [c - 24 for c in cols[1:]] + [W - 130]  # each column's text must end before the next one
        size = 46
        while size > 12 and any(d.textlength(text, font=font(size, bold=r == 0)) > ends[c] - cols[c]
                                for r, row in enumerate(rows) for c, text in enumerate(row)):
            size -= 1
        for r, row in enumerate(rows):
            y = 250 + r * 110
            if r == 0:
                d.rectangle([110, y - 15, W - 110, y + 75], fill=(40, 60, 90))
            for c, text in enumerate(row):
                d.text((cols[c], y + (46 - size) / 2), text, font=font(size, bold=r == 0),
                       fill=ORANGE if (r and c == 2) else WHITE)
        d.text((140, 830), T["design_notes"][0], font=fit(T["design_notes"][:1], 36, W - 280), fill=CYAN)
        d.text((140, 890), T["design_notes"][1], font=fit(T["design_notes"][1:], 30, W - 280), fill=(170, 180, 190))
    elif kind == "lessons":
        center(90, T["lessons_title"], 66, WHITE, bold=True)
        items = T["lessons"]
        fa = fit([a for a, _ in items], 42, W - 360, bold=True)  # text runs from x=180 to 40 px inside the box
        fb = fit([b for _, b in items], 38, W - 360)
        for i, (a, b) in enumerate(items):
            y = 250 + i * 170
            d.rounded_rectangle([140, y, W - 140, y + 140], 20, fill=(22, 30, 48), outline=(70, 90, 120), width=2)
            d.text((180, y + 18), a, font=fa, fill=ORANGE)
            d.text((180, y + 76), b, font=fb, fill=WHITE)
    elif kind == "journal":
        center(90, item.get("title", T["journal_title"]), 58, WHITE, bold=True)
        y = 220
        for kind_word, text in item["lines"]:
            d.text((140, y), kind_word, font=font(34, bold=True, mono=True), fill=ORANGE)
            words, line = text.split(), ""
            yy = y
            for wd in words:
                trial = (line + " " + wd).strip()
                if d.textlength(trial, font=font(34, mono=True)) > W - 480:
                    d.text((380, yy), line, font=font(34, mono=True), fill=WHITE)
                    yy += 46
                    line = wd
                else:
                    line = trial
            d.text((380, yy), line, font=font(34, mono=True), fill=WHITE)
            y = yy + 78
    elif kind == "terminal":
        d.rounded_rectangle([110, 110, W - 110, H - 110], 24, fill=(12, 14, 20), outline=(70, 80, 100), width=3)
        for k, col in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([150 + k * 44, 140, 176 + k * 44, 166], fill=col)
        y = 210
        for line in item["lines"]:
            col = CYAN if line.startswith("$") else ORANGE if line.startswith("mission") else (205, 214, 224)
            d.text((160, y), line, font=font(33, mono=True), fill=col)
            y += 52
    elif kind == "outro":
        center(170, T["outro_title"], 96, WHITE, bold=True)
        center(320, "github.com/shoal-rat/astra-ksp", 58, CYAN, mono=True)
        facts = [t.format(**stats) for t in T["outro_chips"]]
        size = 44  # the row of chips must fit between x=180 and W-180
        while size > 12 and sum(d.textlength(f, font=font(size, bold=True)) + 60 for f in facts) \
                + 40 * (len(facts) - 1) > W - 360:
            size -= 1
        x = 180
        for fct in facts:
            w = d.textlength(fct, font=font(size, bold=True)) + 60
            d.rounded_rectangle([x, 470, x + w, 560], 20, outline=ORANGE, width=4)
            d.text((x + 30, 485 + (44 - size) / 2), fct, font=font(size, bold=True), fill=WHITE)
            x += w + 40
        center(700, T["outro_cta"], 56, ORANGE)
    path = (out_dir or workdir(lang) / "cards") / f"{kind}_{item.get('name', '')}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def cover(photo: Path, lang: str = "zh", dst: Path | None = None) -> Path:
    """Video cover (Bilibili, README) or YouTube thumbnail: the Duna flag photo with the title."""
    img = Image.open(photo).convert("RGB")
    img = img.resize((W, round(img.height * W / img.width)))
    top = (img.height - H) // 2
    img = img.crop((0, top, W, top + H))
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shade).rectangle([0, 0, W, 330], fill=(0, 0, 0, 150))
    img = Image.alpha_composite(img.convert("RGBA"), shade).convert("RGB")
    d = ImageDraw.Draw(img)
    (title, t_size, t_y), (sub, s_size, s_y) = TEXT[lang]["cover"]
    _center(d, t_y, title, _fit(d, [title], t_size, W - 2 * MARGIN, bold=True, lang=lang), WHITE)
    _center(d, s_y, sub, _fit(d, [sub], s_size, W - 2 * MARGIN, lang=lang), ORANGE)
    path = dst or WORK / "cover.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    quality = 92
    img.save(path, quality=quality)
    while path.stat().st_size >= 2_000_000 and quality > 60:  # YouTube thumbnails must be under 2 MB
        quality -= 8
        img.save(path, quality=quality)
    return path


# -- visual items ----------------------------------------------------------------------------------


ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(FPS), "-an"]


def render_clip(rec: Recording, start: int, end: int, speed: float, out_dur: float, dst: Path) -> None:
    src_dur = (end - start) / REC_FPS
    natural = src_dur / speed
    pad = max(0.0, out_dur - natural)
    vf = (f"setpts=(PTS-STARTPTS)/{speed:.5f},fps={FPS},split[a][b];"
          f"[a]scale={W}:{H},boxblur=30:3,eq=brightness=-0.12:saturation=0.8[bg];"
          f"[b]scale=-2:{H}[fg];[bg][fg]overlay=(W-w)/2:0"
          + (f",tpad=stop_mode=clone:stop_duration={pad:.3f}" if pad > 0.01 else "")
          + f",trim=duration={out_dur:.3f},setpts=PTS-STARTPTS,format=yuv420p")
    ffmpeg("-ss", f"{start / REC_FPS:.3f}", "-t", f"{src_dur + 1:.3f}", "-i", str(rec.video), "-vf", vf, *ENC,
           "-t", f"{out_dur:.3f}", str(dst))


def render_still(image: Path, zoom: tuple[float, float], out_dur: float, dst: Path, work: Path = WORK) -> None:
    img = Image.open(image).convert("RGB")
    img = img.resize((W, round(img.height * W / img.width))) if img.width != W else img
    if img.height > H:
        top = (img.height - H) // 2
        img = img.crop((0, top, W, top + H))
    big = work / "cards" / f"_{dst.stem}.png"
    big.parent.mkdir(parents=True, exist_ok=True)
    img.resize((W * 2, H * 2), Image.LANCZOS).save(big)
    n = max(1, round(out_dur * FPS))
    z0, z1 = zoom
    vf = (f"zoompan=z='{z0}+({z1}-{z0})*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={n}:s={W}x{H}:fps={FPS},"
          f"trim=duration={out_dur:.3f},format=yuv420p")
    ffmpeg("-i", str(big), "-vf", vf, *ENC, "-frames:v", str(n), str(dst))


# -- overlays (ASS) --------------------------------------------------------------------------------


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},{sub_size},&H00FFFFFF,&H00FFFFFF,&H78000000,&H78000000,1,0,0,0,100,100,1,0,3,14,0,2,120,120,40,1
Style: Badge,{font},30,&H00FFFFFF,&H00FFFFFF,&H00000000,&H9A1A1A1A,1,0,0,0,100,100,0,0,3,10,0,7,40,40,34,1
Style: Tool,Consolas,30,&H0080FFB8,&H0080FFB8,&H00000000,&HA0101418,0,0,0,0,100,100,0,0,3,10,0,9,40,40,34,1
Style: Hud,{font},32,&H00F7C34F,&H00F7C34F,&H00000000,&HA0101418,1,0,0,0,100,100,0,0,3,10,0,1,40,40,170,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_header(lang: str) -> str:
    return ASS_HEADER.format(font=TEXT[lang]["ass_font"], sub_size=TEXT[lang]["sub_size"])


def split_caption(text: str, limit: int = 26) -> list[str]:
    """Split a sentence at Chinese commas into chunks of at most ~limit characters."""
    parts, cur = [], ""
    for ch in text:
        cur += ch
        if ch in "，；、：,;" and len(cur) >= limit * 0.55:
            parts.append(cur)
            cur = ""
    if cur:
        parts.append(cur)
    out: list[str] = []
    for p in parts:
        k = max(1, math.ceil(len(p) / limit))  # split a long clause into k even pieces, not a fixed prefix
        size = math.ceil(len(p) / k)
        out.extend(p[i:i + size] for i in range(0, len(p), size))
    merged: list[str] = []
    for p in out:
        if merged and len(merged[-1]) + len(p) <= limit:
            merged[-1] += p
        else:
            merged.append(p)
    return [m.strip("，；、,; ") or m for m in merged]


# English captions: whole words of the narration, at most CAPTION_LIMIT characters, broken where a reader
# expects a break (sentence end, then clause punctuation, then before a conjunction or preposition).
CAPTION_LIMIT = 52
CONJUNCTIONS = frozenset("and but or nor so then while which that who whose because where when until after "
                         "before though although yet if unless since whereas".split())
PREPOSITIONS = frozenset("in on at from into onto with without for of by to through over under across toward "
                         "towards around past".split())
WEAK_ENDS = frozenset("a an the to of in on at by for from with into onto and or but nor it its his her their "
                      "our we they he she".split())  # words that should not end a caption line
_CLOSERS = "\"'”’)]"


def _sentence_end(tok: str) -> bool:
    return tok.rstrip(_CLOSERS).endswith((".", "!", "?", "…"))


def _break_cost(tok: str, nxt: str) -> float:
    """How bad it is to end a caption after token `tok` when `nxt` starts the next one (0 is ideal)."""
    if _sentence_end(tok):
        return 0.0
    if tok.rstrip(_CLOSERS).endswith((",", ";", ":", "—", "–")) or tok.endswith(")") or nxt.startswith(("—", "–", "(")):
        return 1.0
    if tok.lower() in WEAK_ENDS:
        return 12.0
    word = nxt.strip("\"'“‘(").lower()
    if word in CONJUNCTIONS:
        return 3.0
    if word in PREPOSITIONS:
        return 4.5
    return 7.0


def _chunk_tokens(tokens: list[str], limit: int) -> list[tuple[int, int]]:
    """Split whitespace tokens into caption chunks [i, j) by dynamic programming over the break points.

    A chunk may exceed `limit` characters only when it is a single token. Short chunks, weak break
    points and a sentence end inside a chunk cost extra (little when the chunk is whole sentences);
    the cheapest split wins."""
    n = len(tokens)
    ends = [_sentence_end(t) for t in tokens]
    best = [0.0] + [math.inf] * n
    back = [0] * (n + 1)
    for j in range(1, n + 1):
        brk = 0.0 if j == n else _break_cost(tokens[j - 1], tokens[j])
        length, inner = -1, 0
        for i in range(j - 1, -1, -1):
            length += len(tokens[i]) + 1
            if i < j - 1 and ends[i]:
                inner += 1
            if length > limit and i < j - 1:
                break
            whole = (i == 0 or ends[i - 1]) and ends[j - 1]  # the chunk is one or more complete sentences
            short = 10.0 * (max(0, limit - length) / limit) ** 2  # a short complete sentence is fine on its own
            cost = best[i] + (0.5 * short if whole else short) + brk + inner * (1.5 if whole else 12.0)
            if cost < best[j]:
                best[j], back[j] = cost, i
    spans, j = [], n
    while j > 0:
        spans.append((back[j], j))
        j = back[j]
    return spans[::-1]


def caption_chunks(text: str, limit: int = CAPTION_LIMIT) -> list[str]:
    """The narration split into caption lines (whole words, punctuation kept, whitespace normalised)."""
    toks = text.split()
    return [" ".join(toks[i:j]) for i, j in _chunk_tokens(toks, limit)]


def word_captions(text: str, words: list[list], limit: int = CAPTION_LIMIT, duration: float | None = None,
                  bridge: float = 1.0, linger: float = 0.3) -> list[tuple[float, float, str]]:
    """Timed caption chunks (start_s, end_s, text) from the narration and its TTS word boundaries.

    `words` are [start_s, end_s, word] events in speaking order. Each word is located in the narration
    (case-insensitive, never inside a longer word); a whitespace token takes the times of the words
    found in it, and tokens with no word found are interpolated by character position. A chunk runs
    from its first token's start to its last token's end; it is held until the next chunk when the gap
    is at most `bridge` seconds, otherwise (and for the last chunk) `linger` seconds longer.
    `duration` (the audio length) caps the last chunk and times the text when no word was found."""
    spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    n = len(spans)
    if n == 0:
        return []
    starts = [a for a, _ in spans]
    t0, t1 = [math.nan] * n, [math.nan] * n
    norm = text.replace("’", "'").replace("‘", "'")  # same length as text
    pos = 0
    for w_start, w_end, word in words:
        word = str(word).strip().replace("’", "'").replace("‘", "'")
        if not word:
            continue
        # a boundary can span several tokens ("0.03 m/s"); match it across any whitespace
        body = r"\s+".join(re.escape(part) for part in word.split())
        pat = ("(?<!\\w)" if word[0].isalnum() else "") + body + ("(?!\\w)" if word[-1].isalnum() else "")
        m = re.compile(pat, re.IGNORECASE).search(norm, pos, pos + len(word) + 40)
        if m is None:  # the TTS spelled it differently; its token gets interpolated
            continue
        pos = m.end()
        k = max(0, bisect.bisect_right(starts, m.start()) - 1)
        while k < n and spans[k][0] < m.end():
            t0[k] = w_start if math.isnan(t0[k]) else min(t0[k], w_start)
            t1[k] = w_end if math.isnan(t1[k]) else max(t1[k], w_end)
            k += 1
    timed = [k for k in range(n) if not math.isnan(t0[k])]
    if not timed:
        per_char = (duration or len(text) / 15.0) / max(len(text), 1)
        t0 = [a * per_char for a, _ in spans]
        t1 = [b * per_char for _, b in spans]
    else:
        f, l = timed[0], timed[-1]
        per_char = (t1[l] - t0[f]) / max(spans[l][1] - spans[f][0], 1)
        for k in range(n):
            if k in timed:
                continue
            p = max((i for i in timed if i < k), default=None)
            q = min((i for i in timed if i > k), default=None)
            ca, ta = (spans[p][1], t1[p]) if p is not None else (0, max(0.0, t0[q] - per_char * spans[q][0]))
            if q is not None:
                cb, tb = spans[q][0], t0[q]
            else:
                cb, tb = len(text), t1[p] + per_char * (len(text) - spans[p][1])
                if duration is not None:
                    tb = min(tb, duration)
            t0[k] = ta + (tb - ta) * (spans[k][0] - ca) / max(cb - ca, 1)
            t1[k] = max(t0[k], ta + (tb - ta) * (spans[k][1] - ca) / max(cb - ca, 1))
    timing: list[list] = []
    for i, j in _chunk_tokens([text[a:b] for a, b in spans], limit):
        start = max(t0[i], timing[-1][1] if timing else 0.0)  # never before the previous chunk ends
        timing.append([start, max(start, *t1[i:j]), " ".join(text[spans[i][0]:spans[j - 1][1]].split())])
    for c, cap in enumerate(timing):
        if c + 1 < len(timing):
            nxt = timing[c + 1][0]
            cap[1] = nxt if nxt - cap[1] <= bridge else min(nxt, cap[1] + linger)
        else:
            cap[1] = cap[1] + linger if duration is None else max(cap[1], min(cap[1] + linger, duration))
    return [(s, e, c) for s, e, c in timing]


def tool_text(label: str, detail: str) -> str:
    name = label.split(":", 1)[1]
    args = ""
    try:
        d = json.loads(detail)
        kv = [f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in d.items()
              if v is not None and k not in ("text", "summary", "plaque")][:3]
        args = ", ".join(kv)
    except (ValueError, AttributeError):
        args = detail[:60]
    if len(args) > 70:
        args = args[:67] + "..."
    return f"AI ▶ {name}({args})"


def hud_text(row: dict, speed: float, lang: str = "zh") -> str | None:
    body = row.get("body") or ""
    if not body:
        return None
    radar, alt = fnum(row.get("radar_altitude")), fnum(row.get("altitude"))
    h = radar if (not math.isnan(radar) and radar < 30000) else alt
    vs, srf, orb = fnum(row.get("vertical_speed")), fnum(row.get("surface_speed")), fnum(row.get("orbital_speed"))
    sit = (row.get("situation") or "").upper()
    fast = sit in ("ORBITING", "ESCAPING", "SUB_ORBITAL") and not math.isnan(h) and h > 30000
    v = orb if fast else srf
    T = TEXT[lang]["hud"]
    parts = [TEXT[lang]["bodies"].get(body, body)]
    if not math.isnan(h):
        parts.append(T["alt"].format(h))
    if not math.isnan(v):
        parts.append(T["orbital" if fast else "ground"].format(v))
    if not math.isnan(vs) and abs(vs) < 5000:
        parts.append(T["vertical"].format(vs))
    warp = fnum(row.get("warp"), 1.0)
    if warp > 1.5:
        parts.append(T["warp"].format(warp))
    if speed > 1.5:
        parts.append(T["video"].format(speed))
    return "  ·  ".join(parts)


# -- music ---------------------------------------------------------------------------------------------


def synth_music(dur: float) -> np.ndarray:
    """Original ambient bed: Am-F-C-G pads, sub bass, soft arpeggio, simple stereo echoes."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = np.zeros((2, n), dtype=np.float64)
    prog = [(45, [57, 60, 64]), (41, [53, 57, 60]), (48, [52, 55, 60]), (43, [55, 59, 62])]
    bar = 4.0

    def f(m: float) -> float:
        return 440.0 * 2 ** ((m - 69) / 12)

    for i, start in enumerate(np.arange(0.0, dur, bar)):
        root, notes = prog[i % 4]
        a, b = int(start * SR), min(n, int((start + bar + 2.0) * SR))
        tt = t[a:b] - start
        env = np.minimum(1.0, tt / 1.4) * np.clip((bar + 2.0 - tt) / 2.0, 0.0, 1.0)
        for ch, det in ((0, -0.06), (1, 0.06)):
            pad = np.zeros_like(tt)
            for m in notes:
                for dd in (det, 0.0):
                    fr = f(m + dd)
                    pad += np.sin(2 * np.pi * fr * tt) + 0.25 * np.sin(4 * np.pi * fr * tt)
            out[ch, a:b] += 0.05 * env * pad
            out[ch, a:b] += 0.10 * env * np.sin(2 * np.pi * f(root) * tt)
        arp = notes + [notes[1] + 12]
        for k in range(8):
            ts = start + k * bar / 8
            a2, b2 = int(ts * SR), min(n, int((ts + 1.2) * SR))
            if a2 >= n:
                break
            tk = t[a2:b2] - ts
            m = arp[k % len(arp)] + 12
            pl = np.sin(2 * np.pi * f(m) * tk) * np.exp(-tk * 5.0) * 0.05
            out[0, a2:b2] += pl * (0.7 if k % 2 else 1.0)
            out[1, a2:b2] += pl * (1.0 if k % 2 else 0.7)
    for delay, gain in ((0.19, 0.30), (0.37, 0.18), (0.61, 0.10)):
        d = int(delay * SR)
        out[0, d:] += gain * out[1, :-d]
        out[1, d:] += gain * out[0, :-d]
    kernel = np.ones(6) / 6
    out = np.stack([np.convolve(c, kernel, mode="same") for c in out])
    fade = np.minimum(1.0, t / 3.0) * np.clip((dur - t) / 4.0, 0.0, 1.0)
    out *= fade
    return (out / (np.abs(out).max() + 1e-9) * 0.5).astype(np.float32)


# -- build -----------------------------------------------------------------------------------------


def stats() -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from astra import registry

    tools = len(registry.load_all())
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--collect-only", "tests"], cwd=ROOT,
                       capture_output=True, text=True)
    tests = next((int(w) for line in r.stdout.splitlines() if "tests collected" in line or "collected" in line
                  for w in line.split() if w.isdigit()), 0)
    return {"tools": tools, "tests": tests}


def load_storyboard(lang: str, text: Path | None) -> dict:
    """storyboard.json with a language overlay merged in (no overlay: the storyboard as written).

    From the overlay: title, voice, rate and badge; per segment id its narration, chapter name and the
    title of its journal card. The visuals, timing hints and segment order stay the storyboard's.
    Raises SystemExit naming every narrated segment without narration (and every other gap) so a
    build never mixes languages."""
    sb = json.loads(STORYBOARD.read_text(encoding="utf-8"))
    if text is None:
        return sb
    ov = json.loads(text.read_text(encoding="utf-8"))
    problems = []
    for k in ("title", "voice", "rate", "badge"):
        if ov.get(k):
            sb[k] = ov[k]
        elif k in ("voice", "badge"):
            problems.append(f'no top-level "{k}"')
    tr = ov.get("segments", {})
    ids = [s["id"] for s in sb["segments"]]
    problems += [f"unknown segment id {k!r} (storyboard ids: {', '.join(ids)})" for k in tr if k not in ids]
    for seg in sb["segments"]:
        t = tr.get(seg["id"], {})
        if seg.get("narration"):
            if str(t.get("narration", "")).strip():
                seg["narration"] = " ".join(t["narration"].split())
            else:
                problems.append(f"segment {seg['id']!r} has no {lang} narration")
        elif str(t.get("narration", "")).strip():
            problems.append(f"segment {seg['id']!r} is music-only; drop its narration")
        if seg.get("chapter"):
            if str(t.get("chapter", "")).strip():
                seg["chapter"] = t["chapter"].strip()
            else:
                problems.append(f"segment {seg['id']!r} has no {lang} chapter name")
        for v in seg["visual"]:
            if v.get("card") == "journal":
                if t.get("title"):
                    v["title"] = t["title"]
                else:
                    v.pop("title", None)  # the card falls back to the language's default title
    if problems:
        raise SystemExit(f"{text}: overlay incomplete for --lang {lang}:\n  " + "\n  ".join(problems))
    return sb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", choices=sorted(TEXT), default="zh",
                    help="zh: Chinese cut (Bilibili, the default); en: English cut (YouTube)")
    ap.add_argument("--text", help="language overlay merged over storyboard.json "
                                   "(default: scripts/showcase/storyboard.<lang>.json for en, none for zh)")
    ap.add_argument("--only", nargs="*", help="render only these segment ids (preview)")
    ap.add_argument("--out", help="output MP4 (default recordings/showcase/astra_showcase.mp4, "
                                  "astra_showcase_<lang>.mp4 for other languages)")
    ns = ap.parse_args()
    lang = ns.lang
    tag = "" if lang == "zh" else f"_{lang}"
    overlay = Path(ns.text) if ns.text else None if lang == "zh" else STORYBOARD.with_name(f"storyboard.{lang}.json")
    if overlay is not None and not overlay.exists():
        raise SystemExit(f"no {lang} text overlay at {overlay}; write it or pass --text PATH")
    sb = load_storyboard(lang, overlay)
    work = workdir(lang)
    work.mkdir(parents=True, exist_ok=True)
    (work / "items").mkdir(exist_ok=True)
    st = stats()
    print("stats", st, flush=True)
    recs: dict[str, Recording] = {}
    segments = [s for s in sb["segments"] if not ns.only or s["id"] in ns.only]

    items: list[Path] = []
    events: list[str] = []
    voice_parts: list[tuple[float, np.ndarray]] = []
    t0 = 0.0
    chapters: list[tuple[float, str]] = []
    for seg in segments:
        if seg.get("chapter"):
            chapters.append((t0, seg["chapter"]))
        if seg.get("narration"):
            audio, subs = narration(seg, sb["voice"], sb["rate"], TEXT[lang]["boundary"])
            vdur = len(audio) / SR
            dur = LEAD + vdur + TAIL
            voice_parts.append((t0 + LEAD, audio))
        else:  # a music-only beat of the given length
            subs, dur = [], float(seg["duration"])
        if TEXT[lang]["boundary"] == "WordBoundary":
            # narration subtitles: whole-word chunks of the narration, timed by the spoken words
            if seg.get("narration"):
                for c_start, c_end, c in word_captions(seg["narration"], subs, duration=vdur):
                    events.append(f"Dialogue: 2,{ass_time(t0 + LEAD + c_start)},{ass_time(t0 + LEAD + c_end)},Sub,,0,0,0,,"
                                  f"{ass_escape(c)}")
        else:
            # narration subtitles, sentence by sentence, split into readable chunks
            for s_start, s_end, text in subs:
                chunks = split_caption(text)
                total = sum(len(c) for c in chunks) or 1
                cur = s_start
                for c in chunks:
                    span = (s_end - s_start) * len(c) / total
                    events.append(f"Dialogue: 2,{ass_time(t0 + LEAD + cur)},{ass_time(t0 + LEAD + cur + span)},Sub,,0,0,0,,"
                                  f"{ass_escape(c)}")
                    cur += span
        # timing of the visual items
        vis = seg["visual"]
        clips = [v for v in vis if "clip" in v]
        flex = [v for v in vis if "clip" not in v]
        natural = {id(v): (v["to"] - v["from"]) / REC_FPS / v.get("speed", 1.0) for v in clips}
        kept = sum(natural[id(v)] for v in clips if v.get("keep"))  # pinned clips play at their own speed
        sum_clip = sum(natural[id(v)] for v in clips if not v.get("keep"))
        min_flex = 3.2
        scale = 1.0
        if flex:
            room = dur - kept - min_flex * len(flex)
            if sum_clip > room:
                scale = sum_clip / max(room, 1.0)
            flex_each = max(min_flex, (dur - kept - sum_clip / scale) / len(flex))
        else:
            scale = sum_clip / max(dur - kept, 1.0) if sum_clip > 0 else 1.0
            flex_each = 0.0
            if scale < 0.6:  # too little footage: play slower down to 0.6x and hold the last frame
                scale = 0.6
        cursor = t0
        for i, v in enumerate(vis):
            dst = work / "items" / f"{seg['id']}_{i}.mp4"
            if "clip" in v:
                rec = recs.setdefault(v["clip"], Recording.load(v["clip"]))
                k = 1.0 if v.get("keep") else scale
                speed = v.get("speed", 1.0) * k
                out_dur = natural[id(v)] / k
                if not flex and i == len(vis) - 1:
                    out_dur = max(out_dur, t0 + dur - cursor)
                print(f"  {seg['id']}[{i}] {v['clip']} {v['from']}-{v['to']} x{speed:.2f} -> {out_dur:.2f}s", flush=True)
                render_clip(rec, v["from"], v["to"], speed, out_dur, dst)
                # tool calls the AI made during this footage
                last = -9.0
                for mk in rec.marks:
                    if v["from"] <= mk["frame"] <= v["to"] and mk["label"].startswith("start:"):
                        tt = cursor + (mk["frame"] - v["from"]) / REC_FPS / speed
                        if tt - last < 1.4 or tt > cursor + out_dur - 0.8:
                            continue
                        last = tt
                        events.append(f"Dialogue: 1,{ass_time(tt)},{ass_time(min(tt + 2.4, cursor + out_dur))},Tool,,0,0,0,,"
                                      f"{ass_escape(tool_text(mk['label'], mk['detail']))}")
                if seg.get("hud"):
                    step = 0.5
                    k = 0.0
                    while k < out_dur - 0.05:
                        frame = int(v["from"] + k * speed * REC_FPS)
                        row = rec.frames.get(frame) or rec.frames.get(frame + 1)
                        txt = hud_text(row, speed, lang) if row else None
                        if txt:
                            events.append(f"Dialogue: 0,{ass_time(cursor + k)},{ass_time(cursor + min(k + step, out_dur))},"
                                          f"Hud,,0,0,0,,{ass_escape(txt)}")
                        k += step
            else:
                out_dur = flex_each
                if i == len(vis) - 1:
                    out_dur = max(0.5, t0 + dur - cursor)
                image = card(v, st, lang) if "card" in v else ROOT / v["photo"]
                print(f"  {seg['id']}[{i}] {'card ' + v['card'] if 'card' in v else v['photo']} -> {out_dur:.2f}s", flush=True)
                render_still(image, tuple(v.get("zoom", [1.0, 1.04])), out_dur, dst, work)
            items.append(dst)
            cursor += out_dur
        t0 = cursor
    total = t0
    events.insert(0, f"Dialogue: 3,{ass_time(0)},{ass_time(total)},Badge,,0,0,0,,{ass_escape(sb['badge'])}")
    (work / "overlay.ass").write_text(ass_header(lang) + "\n".join(events) + "\n", encoding="utf-8-sig")

    # picture
    lst = work / "items.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in items), encoding="utf-8")
    silent = work / "silent.mp4"
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(silent))

    # sound: narration + ducked music
    n = int(total * SR) + SR
    voice = np.zeros(n, dtype=np.float32)
    for start, a in voice_parts:
        i0 = int(start * SR)
        voice[i0:i0 + len(a)] += a[: max(0, n - i0)]
    music = synth_music(n / SR)
    win = int(0.25 * SR)
    csum = np.concatenate([[0.0], np.cumsum(np.abs(voice), dtype=np.float64)])
    idx = np.arange(n)
    lo, hi = np.clip(idx - win // 2, 0, n), np.clip(idx + win // 2, 0, n)
    env = ((csum[hi] - csum[lo]) / np.maximum(hi - lo, 1)).astype(np.float32)
    duck = 0.30 - 0.18 * np.clip(env / 0.02, 0.0, 1.0)
    mix = np.stack([voice * 0.95 + music[0] * duck, voice * 0.95 + music[1] * duck])
    mix /= max(1.0, np.abs(mix).max() / 0.95)
    wav = work / "mix.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((mix.T * 32767).astype(np.int16).tobytes())

    # fonts for libass, then burn in the overlays and mux
    fonts = work / "fonts"
    fonts.mkdir(exist_ok=True)
    for fnt in (*TEXT[lang]["fonts"], MONO):
        if Path(fnt).exists():
            shutil.copy(fnt, fonts / Path(fnt).name)
    out = Path(ns.out) if ns.out else WORK / f"astra_showcase{tag}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-i", "silent.mp4", "-i", "mix.wav", "-vf", "subtitles=overlay.ass:fontsdir=fonts",
           "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",  # streaming loudness (Bilibili/YouTube ~ -14..-16 LUFS)
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", str(SR),  # loudnorm upsamples; deliver 48 kHz
           "-movflags", "+faststart", "-shortest", str(out.resolve()), cwd=work)
    thumb = cover(ROOT / "docs" / "media" / "duna_flag.jpg", lang,
                  WORK / "cover.jpg" if lang == "zh" else WORK / f"astra_cover{tag}.jpg")
    if lang != "zh" and chapters:  # YouTube wants the first chapter at 00:00 and each at least 10 s long
        chapters[0] = (0.0, chapters[0][1])
        for (ta, name), tb in zip(chapters, [t for t, _ in chapters[1:]] + [total]):
            if tb - ta < 10.0:
                print(f"  warning: chapter {name!r} is {tb - ta:.1f} s; YouTube ignores chapters under 10 s", flush=True)
    chap = WORK / f"chapters{tag}.txt"
    chap.write_text(
        "".join(f"{int(t // 60):02d}:{int(t % 60):02d} {name}\n" for t, name in chapters), encoding="utf-8")
    print(f"done: {out}  ({total:.1f} s)")
    if lang != "zh":
        print(f"  title: {sb.get('title', '')}\n  thumbnail: {thumb}\n  chapters: {chap}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
