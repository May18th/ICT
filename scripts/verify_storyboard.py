"""核对门户 5 个入口的实际跳转目标，以及分镜字幕念不念得完。

两个未验证的假设：
  1. 文档写「① → /map2d、②③ → /workbench、④ → /status、⑤ → /data」，实际是不是？
  2. 7 个镜头的字幕在分配时长内念不念得完？
"""
import asyncio
import re
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8804"
ROOT = Path(r"D:\华为ICT\agent_poc")

# 文档里声称的目标
CLAIMED = {
    "这是什么": "/map2d",
    "它怎么一步步推的": "/workbench",
    "凭什么信": "/workbench",
    "它会错吗": "/status",
    "换个地方能用吗": "/data",
}


async def check_links() -> None:
    print("=" * 62)
    print("一、门户 5 个入口的实际去向")
    print("=" * 62)
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="msedge", headless=True)
        pg = await b.new_page(viewport={"width": 1680, "height": 1050})
        await pg.goto(BASE + "/", wait_until="domcontentloaded", timeout=40000)
        await pg.wait_for_timeout(2000)

        anchors = await pg.locator("a[href]").all()
        rows = []
        for a in anchors:
            txt = (await a.inner_text()).strip().replace("\n", " ")
            href = await a.get_attribute("href")
            if not txt or not href:
                continue
            # 只取 5 个问答入口
            for key in CLAIMED:
                if key in txt:
                    rows.append((key, href, txt[:38]))
                    break

        for key, href, txt in rows:
            claim = CLAIMED[key]
            hit = "OK  " if href.rstrip("/").endswith(claim.rstrip("/")) else "DIFF"
            print(f"{hit} {key:<10} href={href:<16} 文档写={claim}")
            print(f"     （原文：{txt}）")

        # 实际点一次第 ⑤ 个，看落地页标题
        print()
        print("-- 实际点击第 ⑤ 个入口，看落地 --")
        try:
            await pg.click("text=换个地方能用吗", timeout=8000)
            await pg.wait_for_timeout(2500)
            print("   落地 URL:", pg.url)
            print("   页面标题:", await pg.title())
            first = (await pg.inner_text("body"))[:90].replace("\n", " ")
            print("   首屏文字:", first)
        except Exception as exc:  # noqa: BLE001
            print("   点击失败:", type(exc).__name__, str(exc)[:120])

        await b.close()


def check_subtitles() -> None:
    print()
    print("=" * 62)
    print("二、分镜字幕念不念得完（按 4.5 字/秒，含停顿）")
    print("=" * 62)
    text = (ROOT / "docs/competition/录屏分镜.md").read_text(encoding="utf-8")

    # 按镜头切段
    chunks = re.split(r"^### (镜头 \d｜[^\n]*)$", text, flags=re.M)
    cps = 4.5  # 中文口播约 4.5 字/秒（含自然停顿）
    total_need = 0.0
    total_have = 0.0

    for i in range(1, len(chunks), 2):
        title = chunks[i]
        body = chunks[i + 1]
        # 只取字幕行（以 > 「 开头）
        lines = [ln for ln in body.splitlines() if ln.strip().startswith("> 「")]
        chars = sum(len(re.sub(r"[^\u4e00-\u9fff0-9A-Za-z]", "", ln)) for ln in lines)
        # 时长从标题里取 0:20–0:50
        m = re.search(r"(\d):(\d\d)[–-](\d):(\d\d)", title)
        have = 0.0
        if m:
            a = int(m.group(1)) * 60 + int(m.group(2))
            c = int(m.group(3)) * 60 + int(m.group(4))
            have = c - a
        need = chars / cps if chars else 0
        total_need += need
        total_have += have
        flag = "OK  " if have >= need else "太赶"
        print(f"{flag} {title[:22]:<24} 字幕 {chars:>3} 字  需 {need:>4.1f}s  给 {have:>4.1f}s")

    print()
    verdict = "够" if total_have >= total_need else "不够"
    print(f"合计：需 {total_need:.1f}s / 给 {total_have:.1f}s → {verdict}")


async def main() -> None:
    await check_links()
    check_subtitles()


asyncio.run(main())
