"""Rasterize an SVG (e.g. a design orthographic chart) to PNG via headless Chrome/Edge.

WHY THIS EXISTS: an orthographic SVG must be inspected VISUALLY, not by reading the XML. Parts
clipping into each other, an engine ring wider than the tank, or legs floating off the hull are
obvious at a glance in a raster image but invisible in the path/rect coordinates. The design loop
(and any reviewer) must render the chart to PNG and LOOK at it before trusting a "looks like a
rocket" verdict. Pure-stdlib so it runs anywhere a browser is installed.

    python tools/render_chart_png.py docs/design_chart_AI-Duna-Ring-Y.svg [out.png]
    python tools/render_chart_png.py docs/design_chart_AI-*.svg            # glob, batch
"""
from __future__ import annotations

import glob
import os
import pathlib
import re
import shutil
import subprocess
import sys

_BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser() -> str:
    for p in _BROWSERS:
        if os.path.exists(p):
            return p
    for n in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        w = shutil.which(n)
        if w:
            return w
    raise SystemExit("no Chrome/Edge found for SVG rasterization")


def svg_size(svg_path: str) -> tuple[int, int]:
    txt = pathlib.Path(svg_path).read_text(encoding="utf-8", errors="ignore")
    w = re.search(r'\bwidth="(\d+(?:\.\d+)?)"', txt)
    h = re.search(r'\bheight="(\d+(?:\.\d+)?)"', txt)
    if w and h:
        return int(float(w.group(1))), int(float(h.group(1)))
    vb = re.search(r'viewBox="[\d.\-]+ [\d.\-]+ ([\d.]+) ([\d.]+)"', txt)
    if vb:
        return int(float(vb.group(1))), int(float(vb.group(2)))
    return 900, 600


def render(svg_path: str, png_path: str | None = None, scale: int = 2) -> str:
    svg_path = os.path.abspath(svg_path)
    if png_path is None:
        png_path = os.path.splitext(svg_path)[0] + ".png"
    png_path = os.path.abspath(png_path)
    w, h = svg_size(svg_path)
    url = "file:///" + svg_path.replace("\\", "/")
    cmd = [
        find_browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
        f"--force-device-scale-factor={scale}", "--default-background-color=FFFFFFFF",
        f"--screenshot={png_path}", f"--window-size={w},{h}", url,
    ]
    subprocess.run(cmd, check=True, timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(png_path):
        raise SystemExit(f"render produced no file: {png_path}")
    return png_path


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    out = argv[1] if len(argv) > 1 and not any(c in argv[1] for c in "*?") else None
    paths: list[str] = []
    for a in argv:
        paths.extend(glob.glob(a))
    paths = [p for p in paths if p.lower().endswith(".svg")]
    if not paths:
        print("no .svg matched")
        return 2
    for p in paths:
        png = render(p, out if len(paths) == 1 else None)
        print(f"rendered {p} -> {png} ({os.path.getsize(png)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
