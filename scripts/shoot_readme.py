# -*- coding: utf-8 -*-
"""把门户各页面截成 README 用图。

只做一次性截图，不参与 CI。页面内容全部服务端渲染或本地 fetch，
所以除 map2d 需要等底图加载外，其余等待时间很短。
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8803"
OUT = Path(__file__).resolve().parents[1] / "docs" / "reports" / "shots"

PAGES = [
    # 含底图的两页用 JPEG：照片类内容 PNG 压不下去，1920 宽要 1.2 MB
    ("portal", "/", 1200, "png"),
    ("map2d", "/map2d", 4500, "jpeg"),
    ("data", "/data", 1800, "jpeg"),
    ("limits", "/limits", 1500, "png"),
    ("evolution", "/evolution", 1500, "png"),
]

EXT = {"png": "png", "jpeg": "jpg", "webp": "webp"}

# 4/3 的像素比让 1440 视口正好出 1920 宽，免去二次缩放
SCALE = 4 / 3


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception:
            # 未下载 playwright 自带内核时退到本机 Edge，避免额外下载
            browser = pw.chromium.launch(channel="msedge")
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=SCALE)
        for name, path, wait, fmt in PAGES:
            page.goto(f"{BASE}{path}", wait_until="load", timeout=90000)
            page.wait_for_timeout(wait)
            target = OUT / f"{name}.{EXT[fmt]}"
            opts = {"path": str(target), "type": fmt, "animations": "disabled", "timeout": 120000}
            if fmt == "jpeg":
                opts["quality"] = 90
            # 大屏有无限动画，不禁用会让截图等不到稳定帧
            page.screenshot(**opts)
            print(f"{name}.{fmt}: {target.stat().st_size // 1024} KB")
        browser.close()


if __name__ == "__main__":
    main()
