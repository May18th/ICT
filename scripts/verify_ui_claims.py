"""全量核对：把文档里写过的界面断言，逐条拿真实页面验证。

为什么要有这个脚本：本轮已经发现 5 处错误全是「凭印象写、没核对」造成的
（图例颜色、六跳、原文定位按钮、2/8、KEY 报错）。凡是没被追问的地方，
都可能同样是错的。这个脚本把「我写过的断言」和「页面真实文字」直接对撞，
不靠记忆，也不靠人肉看截图。
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8803"
OUT = Path(r"D:\华为ICT\agent_poc\out")

# 我写进文档的断言，逐条核对
CLAIMS = {
    "portal_entry_count": "门户首页应有 5 个入口方块",
    "portal_titles": ["这是什么", "它怎么一步步推的", "凭什么信", "它会错吗", "换个地方能用吗"],
    "workbench_tabs": ["事件", "证据", "工具轨迹", "自检", "重跑"],
    "workbench_buttons": ["这地方积水了，该谁去处理？", "先封路还是先开泵？", "兴华路泵站设计流量多少？"],
    "workbench_big": ["A-03", "橙色", "30.0"],
    "workbench_blocks": ["处置顺序", "跨部门冲突", "资源缺口"],
    "map2d_metrics": ["推演积水深度", "封路路段", "堵塞雨水口", "泵站运行", "推演进度"],
    "map2d_legend": ["积水", "泵站", "道路"],
    "map2d_buttons": ["暂停", "重置"],
    "map2d_step_card": ["第 1/7 步"],
    # 数据分层与来源标签：每条 KPI 都要能回答「这是台账、模型还是推演」
    "map2d_provenance": ["模型估算", "封路通告", "巡检台账", "设施台账", "回放时钟",
                         "OpenStreetMap 贡献者", "数据来源与边界"],
}


async def main() -> None:
    report: dict[str, object] = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="msedge", headless=True)
        pg = await b.new_page(viewport={"width": 1680, "height": 1050})

        # ---- 门户 ----
        await pg.goto(BASE + "/", wait_until="domcontentloaded", timeout=40000)
        await pg.wait_for_timeout(2500)
        portal = await pg.inner_text("body")
        (OUT / "chk_portal.txt").write_text(portal, encoding="utf-8")
        report["portal_has_entries"] = {
            t: (t in portal) for t in CLAIMS["portal_titles"]
        }
        # 入口方块的实际标题（抓 ask-grid 里的元素）
        entries = await pg.locator(".ask, .askcard, a.ask").all_inner_texts()
        report["portal_entry_texts"] = entries[:12]

        # ---- 工作台 ----
        await pg.goto(BASE + "/workbench", wait_until="domcontentloaded", timeout=40000)
        await pg.wait_for_timeout(2500)
        wb = await pg.inner_text("body")
        (OUT / "chk_workbench.txt").write_text(wb, encoding="utf-8")
        report["workbench_tabs"] = {t: (t in wb) for t in CLAIMS["workbench_tabs"]}
        report["workbench_buttons"] = {t: (t in wb) for t in CLAIMS["workbench_buttons"]}
        report["workbench_big"] = {t: (t in wb) for t in CLAIMS["workbench_big"]}
        report["workbench_blocks"] = {t: (t in wb) for t in CLAIMS["workbench_blocks"]}

        # ---- 大屏 ----
        await pg.goto(BASE + "/map2d", wait_until="domcontentloaded", timeout=60000)
        await pg.wait_for_timeout(4000)
        mp = await pg.inner_text("body")
        (OUT / "chk_map2d.txt").write_text(mp, encoding="utf-8")
        report["map2d_metrics"] = {t: (t in mp) for t in CLAIMS["map2d_metrics"]}
        report["map2d_buttons"] = {t: (t in mp) for t in CLAIMS["map2d_buttons"]}
        report["map2d_step_card"] = {t: (t in mp) for t in CLAIMS["map2d_step_card"]}
        report["map2d_provenance"] = {t: (t in mp) for t in CLAIMS["map2d_provenance"]}

        # ---- 其余页面是否能开、有什么标题 ----
        others: dict[str, str] = {}
        for path in ["/limits", "/status", "/data", "/evolution"]:
            try:
                resp = await pg.goto(BASE + path, wait_until="domcontentloaded", timeout=40000)
                await pg.wait_for_timeout(1200)
                title = await pg.title()
                first = (await pg.inner_text("body"))[:120].replace("\n", " ")
                others[path] = f"{resp.status if resp else '?'} | {title} | {first}"
            except Exception as exc:  # noqa: BLE001
                others[path] = f"ERROR {type(exc).__name__}: {exc}"[:160]
        report["other_pages"] = others

        await b.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))


asyncio.run(main())
