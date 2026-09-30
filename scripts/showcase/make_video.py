"""Build the ASTRA showcase video from the flight recordings (video production, not flight code).

    .venv/Scripts/python.exe scripts/showcase/make_video.py [--only SEG ...] [--out PATH]

Inputs: scripts/showcase/storyboard.json (segments: narration + visuals), recordings/<name>/video.mkv
with frames.csv and marks.csv from `astra record`, and photos in docs/media. Output: an H.264/AAC MP4
at 1920x1080 30 fps with Chinese narration (edge-tts), subtitles, the tool calls the AI made (from the
recorder's marks), a HUD from the recorded telemetry, and a synthesized music bed (no third-party
music). Intermediate files go to recordings/showcase/ (git-ignored); TTS is cached there.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import shutil
import subprocess
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "recordings" / "showcase"
FF = imageio_ffmpeg.get_ffmpeg_exe()
W, H, FPS = 1920, 1080, 30
REC_FPS = 15.0
SR = 48000
LEAD, TAIL = 0.5, 1.2  # seconds of picture before and after each segment's narration
FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
MONO = "C:/Windows/Fonts/consola.ttf"
ORANGE, CYAN, WHITE = (255, 138, 61), (79, 195, 247), (240, 244, 248)

BODY_ZH = {"Kerbin": "坎星", "Mun": "月球", "Duna": "杜娜（火星）", "Sun": "日心轨道", "Ike": "艾克"}


def ffmpeg(*args: str, cwd: Path | None = None) -> None:
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", *args], check=True, cwd=cwd)


def decode_audio(path: Path) -> np.ndarray:
    out = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "f32le", "-ac", "1",
                          "-ar", str(SR), "-"], check=True, capture_output=True).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


# -- narration -------------------------------------------------------------------------------------


async def _tts(text: str, voice: str, rate: str, mp3: Path) -> list[list]:
    import edge_tts

    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="SentenceBoundary")
    subs: list[list] = []
    with mp3.open("wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] in ("SentenceBoundary", "WordBoundary"):
                subs.append([chunk["offset"] / 1e7, (chunk["offset"] + chunk["duration"]) / 1e7, chunk["text"]])
    return subs


def narration(seg: dict, voice: str, rate: str) -> tuple[np.ndarray, list[list]]:
    key = hashlib.sha1(f"{voice}|{rate}|{seg['narration']}".encode("utf-8")).hexdigest()[:12]
    mp3, meta = WORK / "tts" / f"{seg['id']}_{key}.mp3", WORK / "tts" / f"{seg['id']}_{key}.json"
    mp3.parent.mkdir(parents=True, exist_ok=True)
    if not (mp3.exists() and meta.exists()):
        for attempt in range(4):  # the TTS service occasionally stalls mid-stream
            try:
                subs = asyncio.run(asyncio.wait_for(_tts(seg["narration"], voice, rate, mp3), 90))
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


def _font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(MONO if mono else FONT_BOLD if bold else FONT, size)


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


def card(item: dict, stats: dict) -> Path:
    kind = item["card"]
    img = _backdrop({"title": 1, "loop": 2, "design": 3, "lessons": 4, "outro": 5}.get(kind, 9))
    d = ImageDraw.Draw(img)
    if kind == "title":
        _center(d, 300, "ASTRA", _font(190, bold=True), WHITE)
        _center(d, 540, "让 AI 当宇航员：从设计火箭到登陆火星", _font(64), ORANGE)
        _center(d, 650, "Claude × Kerbal Space Program · 全程 AI 自主操作", _font(40), CYAN)
    elif kind == "loop":
        _center(d, 110, "AI 的工作方式：没有任务脚本，只有工具", _font(62, bold=True), WHITE)
        steps = ["观察", "计算", "决策", "执行", "验证"]
        x0, bw, gap, y = 170, 250, 70, 330
        for i, s in enumerate(steps):
            x = x0 + i * (bw + gap)
            d.rounded_rectangle([x, y, x + bw, y + 150], 24, outline=CYAN, width=5, fill=(18, 30, 52))
            tw = d.textlength(s, font=_font(64, bold=True))
            d.text((x + (bw - tw) / 2, y + 36), s, font=_font(64, bold=True), fill=WHITE)
            if i < len(steps) - 1:
                ax = x + bw + 10
                d.polygon([(ax, y + 60), (ax + gap - 20, y + 75), (ax, y + 90)], fill=ORANGE)
        lines = [
            f"{stats['tools']} 个工具：遥测 · 轨道计算 · 火箭设计 · 控制 · 反射动作 · MechJeb · 出舱 · 任务日志",
            "AI 思考时游戏自动暂停；只有在 AI 设定的触发条件下，游戏时间才会前进",
            "每一个数字：远地点、点火时间、俯仰程序、着陆时机，都由 AI 当场计算",
        ]
        for i, t in enumerate(lines):
            _center(d, 590 + i * 80, t, _font(40), WHITE if i else ORANGE)
    elif kind == "design":
        _center(d, 90, "AI 设计的登月火箭：ASTRA Mun Lander", _font(60, bold=True), WHITE)
        rows = [("级", "发动机", "Δv（真空）", "推重比", "任务"),
                ("1", "Mainsail ×1", "3644 m/s", "1.89", "上升入轨"),
                ("2", "Terrier（转移级）", "1773 m/s", "0.64*", "奔月、捕获、离轨"),
                ("3", "Terrier（着陆器）", "1841 m/s", "7.7†", "着陆、起飞、返航"),
                ("4", "Mk1 返回舱", "—", "—", "隔热罩 + 降落伞")]
        cols = [140, 330, 830, 1150, 1400]
        for r, row in enumerate(rows):
            y = 250 + r * 110
            if r == 0:
                d.rectangle([110, y - 15, W - 110, y + 75], fill=(40, 60, 90))
            for c, text in enumerate(row):
                d.text((cols[c], y), text, font=_font(46, bold=r == 0), fill=ORANGE if (r and c == 2) else WHITE)
        d.text((140, 830), "30 个零件 · 74.5 吨 · 零件树由 AI 根据游戏实时零件数据写出，工具生成 .craft 文件",
               font=_font(36), fill=CYAN)
        d.text((140, 890), "* 真空推重比（坎星重力）  † 月球表面推重比", font=_font(30), fill=(170, 180, 190))
    elif kind == "lessons":
        _center(d, 90, "失败 → 教训 → 新代码", _font(66, bold=True), WHITE)
        items = [
            ("一个零件报告了 ±6×10¹⁷ 米的尺寸", "着陆器在 13 公里高空悬停 → 改为逐零件测量高度"),
            ("前方山坡比平地模型高 450 米", "以 35 m/s 撞地 → 扫描前方地形，提前制动"),
            ("低推重比时减速模型失准", "以 118 m/s 撞地 → 数值积分预测 + 高度优先制动"),
            ("宇航员出舱时着陆器翻倒", "→ 背包喷气跳离 + 探测核心保持 SAS"),
        ]
        for i, (a, b) in enumerate(items):
            y = 250 + i * 170
            d.rounded_rectangle([140, y, W - 140, y + 140], 20, fill=(22, 30, 48), outline=(70, 90, 120), width=2)
            d.text((180, y + 18), a, font=_font(42, bold=True), fill=ORANGE)
            d.text((180, y + 76), b, font=_font(38), fill=WHITE)
    elif kind == "journal":
        _center(d, 90, item.get("title", "AI 的飞行日志（原文）"), _font(58, bold=True), WHITE)
        y = 220
        for kind_word, text in item["lines"]:
            d.text((140, y), kind_word, font=_font(34, bold=True, mono=True), fill=ORANGE)
            words, line = text.split(), ""
            yy = y
            for wd in words:
                trial = (line + " " + wd).strip()
                if d.textlength(trial, font=_font(34, mono=True)) > W - 480:
                    d.text((380, yy), line, font=_font(34, mono=True), fill=WHITE)
                    yy += 46
                    line = wd
                else:
                    line = trial
            d.text((380, yy), line, font=_font(34, mono=True), fill=WHITE)
            y = yy + 78
    elif kind == "terminal":
        d.rounded_rectangle([110, 110, W - 110, H - 110], 24, fill=(12, 14, 20), outline=(70, 80, 100), width=3)
        for k, col in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([150 + k * 44, 140, 176 + k * 44, 166], fill=col)
        y = 210
        for line in item["lines"]:
            col = CYAN if line.startswith("$") else ORANGE if line.startswith("mission") else (205, 214, 224)
            d.text((160, y), line, font=_font(33, mono=True), fill=col)
            y += 52
    elif kind == "outro":
        _center(d, 170, "ASTRA 已开源", _font(96, bold=True), WHITE)
        _center(d, 320, "github.com/shoal-rat/astra-ksp", _font(58, mono=True), CYAN)
        facts = [f"{stats['tools']} 个工具", "0 行任务脚本", f"{stats['tests']} 个测试", "月球 + 杜娜 两面旗帜"]
        x = 180
        for fct in facts:
            w = d.textlength(fct, font=_font(44, bold=True)) + 60
            d.rounded_rectangle([x, 470, x + w, 560], 20, outline=ORANGE, width=4)
            d.text((x + 30, 485), fct, font=_font(44, bold=True), fill=WHITE)
            x += w + 40
        _center(d, 700, "点赞 · 投币 · 关注，我们下次任务见", _font(56), ORANGE)
    path = WORK / "cards" / f"{kind}_{item.get('name', '')}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def cover(photo: Path) -> Path:
    """Bilibili/README cover: the Duna flag photo with the title."""
    img = Image.open(photo).convert("RGB")
    img = img.resize((W, round(img.height * W / img.width)))
    top = (img.height - H) // 2
    img = img.crop((0, top, W, top + H))
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shade).rectangle([0, 0, W, 330], fill=(0, 0, 0, 150))
    img = Image.alpha_composite(img.convert("RGBA"), shade).convert("RGB")
    d = ImageDraw.Draw(img)
    _center(d, 50, "AI 全自动登陆火星", _font(120, bold=True), WHITE)
    _center(d, 215, "设计火箭 · 发射 · 着陆 · 插旗，全程无人工操作", _font(54), ORANGE)
    path = WORK / "cover.jpg"
    img.save(path, quality=92)
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


def render_still(image: Path, zoom: tuple[float, float], out_dur: float, dst: Path) -> None:
    img = Image.open(image).convert("RGB")
    img = img.resize((W, round(img.height * W / img.width))) if img.width != W else img
    if img.height > H:
        top = (img.height - H) // 2
        img = img.crop((0, top, W, top + H))
    big = WORK / "cards" / f"_{dst.stem}.png"
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
Style: Sub,Microsoft YaHei,54,&H00FFFFFF,&H00FFFFFF,&H78000000,&H78000000,1,0,0,0,100,100,1,0,3,14,0,2,120,120,40,1
Style: Badge,Microsoft YaHei,30,&H00FFFFFF,&H00FFFFFF,&H00000000,&H9A1A1A1A,1,0,0,0,100,100,0,0,3,10,0,7,40,40,34,1
Style: Tool,Consolas,30,&H0080FFB8,&H0080FFB8,&H00000000,&HA0101418,0,0,0,0,100,100,0,0,3,10,0,9,40,40,34,1
Style: Hud,Microsoft YaHei,32,&H00F7C34F,&H00F7C34F,&H00000000,&HA0101418,1,0,0,0,100,100,0,0,3,10,0,1,40,40,170,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


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


def hud_text(row: dict, speed: float) -> str | None:
    body = row.get("body") or ""
    if not body:
        return None
    radar, alt = fnum(row.get("radar_altitude")), fnum(row.get("altitude"))
    h = radar if (not math.isnan(radar) and radar < 30000) else alt
    vs, srf, orb = fnum(row.get("vertical_speed")), fnum(row.get("surface_speed")), fnum(row.get("orbital_speed"))
    sit = (row.get("situation") or "").upper()
    fast = sit in ("ORBITING", "ESCAPING", "SUB_ORBITAL") and not math.isnan(h) and h > 30000
    v = orb if fast else srf
    parts = [BODY_ZH.get(body, body)]
    if not math.isnan(h):
        parts.append(f"高度 {h:,.0f} m")
    if not math.isnan(v):
        parts.append(f"{'轨道' if fast else '地速'} {v:,.1f} m/s")
    if not math.isnan(vs) and abs(vs) < 5000:
        parts.append(f"垂直 {vs:+,.1f} m/s")
    warp = fnum(row.get("warp"), 1.0)
    if warp > 1.5:
        parts.append(f"时间加速 ×{warp:g}")
    if speed > 1.5:
        parts.append(f"视频 ×{speed:.0f}")
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="render only these segment ids (preview)")
    ap.add_argument("--out", default=str(ROOT / "recordings" / "showcase" / "astra_showcase.mp4"))
    ns = ap.parse_args()
    sb = json.loads((ROOT / "scripts" / "showcase" / "storyboard.json").read_text(encoding="utf-8"))
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "items").mkdir(exist_ok=True)
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
            audio, subs = narration(seg, sb["voice"], sb["rate"])
            vdur = len(audio) / SR
            dur = LEAD + vdur + TAIL
            voice_parts.append((t0 + LEAD, audio))
        else:  # a music-only beat of the given length
            subs, dur = [], float(seg["duration"])
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
            dst = WORK / "items" / f"{seg['id']}_{i}.mp4"
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
                        txt = hud_text(row, speed) if row else None
                        if txt:
                            events.append(f"Dialogue: 0,{ass_time(cursor + k)},{ass_time(cursor + min(k + step, out_dur))},"
                                          f"Hud,,0,0,0,,{ass_escape(txt)}")
                        k += step
            else:
                out_dur = flex_each
                if i == len(vis) - 1:
                    out_dur = max(0.5, t0 + dur - cursor)
                image = card(v, st) if "card" in v else ROOT / v["photo"]
                print(f"  {seg['id']}[{i}] {'card ' + v['card'] if 'card' in v else v['photo']} -> {out_dur:.2f}s", flush=True)
                render_still(image, tuple(v.get("zoom", [1.0, 1.04])), out_dur, dst)
            items.append(dst)
            cursor += out_dur
        t0 = cursor
    total = t0
    events.insert(0, f"Dialogue: 3,{ass_time(0)},{ass_time(total)},Badge,,0,0,0,,{ass_escape(sb['badge'])}")
    (WORK / "overlay.ass").write_text(ASS_HEADER + "\n".join(events) + "\n", encoding="utf-8-sig")

    # picture
    lst = WORK / "items.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in items), encoding="utf-8")
    silent = WORK / "silent.mp4"
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
    wav = WORK / "mix.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((mix.T * 32767).astype(np.int16).tobytes())

    # fonts for libass, then burn in the overlays and mux
    fonts = WORK / "fonts"
    fonts.mkdir(exist_ok=True)
    for fnt in (FONT, FONT_BOLD, MONO):
        if Path(fnt).exists():
            shutil.copy(fnt, fonts / Path(fnt).name)
    out = Path(ns.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-i", "silent.mp4", "-i", "mix.wav", "-vf", "subtitles=overlay.ass:fontsdir=fonts",
           "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",  # streaming loudness (Bilibili/YouTube ~ -14..-16 LUFS)
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", "-shortest", str(out.resolve()), cwd=WORK)
    cover(ROOT / "docs" / "media" / "duna_flag.jpg")
    (WORK / "chapters.txt").write_text(
        "".join(f"{int(t // 60):02d}:{int(t % 60):02d} {name}\n" for t, name in chapters), encoding="utf-8")
    print(f"done: {out}  ({total:.1f} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
