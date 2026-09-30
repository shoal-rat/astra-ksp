"""Render the README art: the hero banners and the loop diagrams (English and Chinese).

    .venv/Scripts/python.exe scripts/showcase/readme_art.py

Writes docs/media/banner.jpg, banner_zh.jpg (from duna_flag.jpg), loop.png, loop_zh.png, and the
video thumbnails video.jpg, video_zh.jpg (from mun_flag.jpg).
Windows fonts: Segoe UI and Bahnschrift for English, Microsoft YaHei for Chinese, Consolas for code.
"""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "docs" / "media"
FONTS = Path("C:/Windows/Fonts")
ORANGE, CYAN, WHITE, MUTED = (255, 138, 61), (79, 195, 247), (240, 244, 248), (160, 178, 196)
BOX = (14, 26, 44)


def font(name: str, size: int, index: int = 0) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size, index=index)


EN = {
    "title": lambda s: font("bahnschrift.ttf", s),
    "bold": lambda s: font("segoeuib.ttf", s),
    "text": lambda s: font("segoeui.ttf", s),
}
ZH = {
    "title": lambda s: font("bahnschrift.ttf", s),
    "bold": lambda s: font("msyhbd.ttc", s),
    "text": lambda s: font("msyh.ttc", s),
}
MONO = lambda s: font("consola.ttf", s)  # noqa: E731


def shadowed(img: Image.Image, xy: tuple[float, float], text: str, fnt, fill, anchor: str = "ra",
             blur: int = 10, alpha: int = 170) -> None:
    """Text with a soft drop shadow, so it reads over a bright sky."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((xy[0] + 3, xy[1] + 4), text, font=fnt, fill=(20, 8, 4, alpha), anchor=anchor)
    img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))
    ImageDraw.Draw(img).text(xy, text, font=fnt, fill=fill, anchor=anchor)


def banner(lang: str) -> Path:
    W, H = 1920, 760
    photo = Image.open(MEDIA / "duna_flag.jpg").convert("RGBA")
    img = photo.crop((0, 30, W, 30 + H))
    # darken the sky behind the title (top right), fading out toward the kerbal and the ground
    x = np.clip((np.arange(W) - 700) / 1220, 0, 1) ** 1.2
    y = np.clip(1 - np.arange(H) / 470, 0, 1) ** 1.1
    shade = np.zeros((H, W, 4), np.uint8)
    shade[..., 3] = (np.outer(y, x) * 150).astype(np.uint8)
    img.alpha_composite(Image.fromarray(shade, "RGBA"))
    # a thin fade to near black along the bottom edge
    fade = np.zeros((H, W, 4), np.uint8)
    fade[..., 3] = (np.clip((np.arange(H) - (H - 140)) / 140, 0, 1)[:, None] ** 2 * 110).astype(np.uint8)
    img.alpha_composite(Image.fromarray(fade, "RGBA"))
    f = EN if lang == "en" else ZH
    right = W - 90
    # the text stays in the sky, right of the lander and above the kerbal (x 900-1110, y > 400)
    shadowed(img, (right, 36), "ASTRA", f["title"](196), WHITE)
    if lang == "en":
        shadowed(img, (right, 262), "An AI flight crew for Kerbal Space Program", f["bold"](50), WHITE, blur=8)
        shadowed(img, (right, 334), "designs the rocket · flies it · lands on the Mun and Duna · plants the flag",
                 f["text"](33), (255, 178, 112), blur=4, alpha=255)
    else:
        shadowed(img, (right, 258), "让 AI 当宇航员和地面飞控", f["bold"](60), WHITE, blur=8)
        shadowed(img, (right, 344), "设计火箭 · 发射 · 登月 · 登陆火星 · 插旗，全程 AI 操作", f["text"](36), (255, 178, 112),
                 blur=4, alpha=255)
    out = MEDIA / ("banner.jpg" if lang == "en" else "banner_zh.jpg")
    img.convert("RGB").save(out, quality=90, optimize=True, progressive=True)
    return out


def backdrop(W: int, H: int, seed: int) -> Image.Image:
    """Deep-space gradient with stars and a warm glow, as on the video's title cards."""
    rng = np.random.default_rng(seed)
    y = np.linspace(0, 1, H)[:, None]
    base = np.zeros((H, W, 3))
    base[..., 0] = 8 + 20 * y
    base[..., 1] = 12 + 14 * y
    base[..., 2] = 26 + 30 * (1 - y)
    img = Image.fromarray(base.astype(np.uint8))
    d = ImageDraw.Draw(img)
    for _ in range(int(W * H / 5000)):
        x, yy = rng.integers(0, W), rng.integers(0, H)
        b = int(rng.integers(90, 255))
        r = 1 if rng.random() < 0.9 else 2
        d.ellipse([x, yy, x + r, yy + r], fill=(b, b, min(255, b + 20)))
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W * 0.6, H * 0.3, W * 1.4, H * 1.8], fill=(120, 50, 25))
    return Image.blend(img, glow.filter(ImageFilter.GaussianBlur(160)), 0.35).convert("RGBA")


STEPS = {
    "en": [("OBSERVE", ["telemetry", "orbit_info", "vessel_stages"]),
           ("COMPUTE", ["compute_hohmann", "compute_descent", "compute_calc"]),
           ("DECIDE", ["journal_note", "game_checkpoint"]),
           ("ACT", ["fly_burn", "mj_ascent", "fly_descent", "crew_hop"]),
           ("VERIFY", ["orbit_info", "camera_look", "crew_status"])],
    "zh": [("观察", ["telemetry", "orbit_info", "vessel_stages"]),
           ("计算", ["compute_hohmann", "compute_descent", "compute_calc"]),
           ("决策", ["journal_note", "game_checkpoint"]),
           ("执行", ["fly_burn", "mj_ascent", "fly_descent", "crew_hop"]),
           ("验证", ["orbit_info", "camera_look", "crew_status"])],
}
LOOP_TEXT = {
    "en": ("No mission scripts. Just tools, and a loop.",
           "The game pauses whenever a tool returns, so the AI can think as long as it needs.",
           "Game time runs only inside a reflex, until a trigger the AI chose fires or a safety interlock trips."),
    "zh": ("没有任务脚本，只有工具和一个循环",
           "每次工具返回，游戏就自动暂停：AI 想多久都不耗费游戏时间。",
           "游戏时间只在反射动作里前进，直到 AI 自己设定的触发条件或安全联锁生效。"),
}


def arrow(d: ImageDraw.ImageDraw, x0: float, x1: float, y: float, color) -> None:
    d.line([(x0, y), (x1 - 18, y)], fill=color, width=6)
    d.polygon([(x1, y), (x1 - 24, y - 14), (x1 - 24, y + 14)], fill=color)


def loop(lang: str) -> Path:
    W, H = 1920, 880
    img = backdrop(W, H, 2 if lang == "en" else 3)
    d = ImageDraw.Draw(img)
    f = EN if lang == "en" else ZH
    title, line1, line2 = LOOP_TEXT[lang]
    d.text((W / 2, 70), title, font=f["bold"](70 if lang == "en" else 72), fill=WHITE, anchor="ma")
    bw, bh, gap, top = 290, 130, 62, 250
    x0 = (W - (5 * bw + 4 * gap)) / 2
    centers = []
    for i, (name, tools) in enumerate(STEPS[lang]):
        x = x0 + i * (bw + gap)
        d.rounded_rectangle([x, top, x + bw, top + bh], radius=24, fill=BOX, outline=CYAN, width=5)
        d.text((x + bw / 2, top + bh / 2), name,
               font=f["title"](58) if lang == "en" else f["bold"](62), fill=WHITE, anchor="mm")
        for j, t in enumerate(tools):
            d.text((x + bw / 2, top + bh + 34 + j * 40), t, font=MONO(30), fill=MUTED, anchor="ma")
        if i:
            arrow(d, x - gap + 10, x - 8, top + bh / 2, ORANGE)
        centers.append(x + bw / 2)
    # the loop closes: verify feeds the next observation
    yb = top - 48
    d.line([(centers[-1], top - 4), (centers[-1], yb), (centers[0], yb)], fill=ORANGE, width=5, joint="curve")
    d.line([(centers[0], yb), (centers[0], top - 20)], fill=ORANGE, width=5)
    d.polygon([(centers[0], top - 2), (centers[0] - 14, top - 26), (centers[0] + 14, top - 26)], fill=ORANGE)
    d.text((W / 2, yb - 44), "repeat, one decision at a time" if lang == "en" else "每一步都重复这个循环",
           font=f["text"](30), fill=ORANGE, anchor="ma")
    d.text((W / 2, 700), line1, font=f["text"](38), fill=WHITE, anchor="ma")
    d.text((W / 2, 760), line2, font=f["text"](38), fill=WHITE, anchor="ma")
    out = MEDIA / ("loop.png" if lang == "en" else "loop_zh.png")
    img.convert("RGB").save(out, optimize=True)
    return out


def video_thumb(lang: str) -> Path:
    """A clickable thumbnail for the showcase video: the Mun flag shot, a play button and a caption."""
    W, H = 1600, 900
    img = Image.open(MEDIA / "mun_flag.jpg").convert("RGBA")
    img = img.resize((W, round(img.height * W / img.width)), Image.LANCZOS)
    top = (img.height - H) // 2
    img = img.crop((0, top, W, top + H))
    band = np.zeros((H, W, 4), np.uint8)
    band[..., 3] = (np.clip((np.arange(H) - H * 0.55) / (H * 0.45), 0, 1)[:, None] ** 1.3 * 215).astype(np.uint8)
    img.alpha_composite(Image.fromarray(band, "RGBA"))
    ring = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ring)
    cx, cy, r = int(W * 0.24), int(H * 0.42), 92  # over the empty ground, clear of the kerbal
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(10, 16, 28, 190), outline=WHITE + (255,), width=6)
    d.polygon([(cx - 30, cy - 46), (cx - 30, cy + 46), (cx + 52, cy)], fill=WHITE + (255,))
    img.alpha_composite(ring)
    f = EN if lang == "en" else ZH
    d = ImageDraw.Draw(img)
    if lang == "en":
        d.text((W / 2, H - 190), "Watch the flight reel", font=f["bold"](64), fill=WHITE, anchor="ma")
        d.text((W / 2, H - 100), "5 minutes of in-game footage: rocket design, the Mun, Duna and the flags, all flown by the AI",
               font=f["text"](32), fill=(255, 178, 112), anchor="ma")
    else:
        d.text((W / 2, H - 190), "观看 5 分钟演示视频", font=f["bold"](68), fill=WHITE, anchor="ma")
        d.text((W / 2, H - 100), "全程游戏实录：设计火箭、登月、登陆火星、插旗，全部由 AI 操作",
               font=f["text"](36), fill=(255, 178, 112), anchor="ma")
    out = MEDIA / ("video.jpg" if lang == "en" else "video_zh.jpg")
    img.convert("RGB").save(out, quality=88, optimize=True, progressive=True)
    return out


if __name__ == "__main__":
    for lang in ("en", "zh"):
        for path in (banner(lang), loop(lang), video_thumb(lang)):
            print(path.relative_to(ROOT), f"{path.stat().st_size / 1024:.0f} KB")
