"""全量核对：把文档里写过的界面断言，逐条拿真实页面验证。

为什么要有这个脚本：本轮已经发现 5 处错误全是「凭印象写、没核对」造成的
（图例颜色、六跳、原文定位按钮、2/8、KEY 报错）。凡是没被追问的地方，
都可能同样是错的。这个脚本把「我写过的断言」和「页面真实文字」直接对撞，
不靠记忆，也不靠人肉看截图。
"""
import asyncio
import csv
import json
import os
from pathlib import Path

# 端口可被 WB_BASE 覆盖：两个核对脚本经常要连同一个服务跑，改代码容易漏掉另一个
BASE = os.environ.get("WB_BASE", "http://127.0.0.1:8803")
OUT = Path(r"D:\华为ICT\agent_poc\out")
SCENE_DIR = Path(r"D:\华为ICT\agent_poc\samples\assets\urban")
EXPECTED_SCENARIO = "default"

# 我写进文档的断言，逐条核对
CLAIMS = {
    "portal_entry_count": "门户首页应有 5 个入口方块",
    "portal_titles": ["系统概览", "推演流程", "证据依据", "能力边界"],
    "portal_secondary": ["证据查询与台账定位", "策略调整与版本回滚", "反馈注入", "方案评审", "内部自检"],
    "workbench_tabs": ["事件", "证据", "取证过程", "自检", "重跑"],
    "workbench_buttons": ["查询某路段积水 30 cm 时的首要处置责任部门",
                          "查询封路与开泵的处置顺序及规则依据",
                          "查询兴华路泵站的设计流量"],
    "workbench_big": ["A-03", "橙色", "30.0"],
    "workbench_blocks": ["处置顺序", "跨部门冲突", "资源缺口"],
    "map2d_metrics": ["推演积水深度", "封路路段", "堵塞雨水口", "泵站运行", "推演进度"],
    "map2d_legend": ["积水", "泵站", "道路"],
    "map2d_buttons": ["暂停", "重置"],
    "map2d_step_card": ["第 1/7 步"],
    # 数据分层与来源标签：每条 KPI 都要能回答「这是台账、模型还是推演」
    "map2d_provenance": ["模型估算", "封路通告", "巡检台账", "设施台账", "回放时钟",
                         "OpenStreetMap 贡献者", "数据来源与边界", "台账", "推演"],
}


def report_exit_code(value: object) -> int:
    if isinstance(value, bool):
        return 0 if value else 1
    if isinstance(value, dict):
        return max((report_exit_code(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return max((report_exit_code(item) for item in value), default=0)
    return 0


def verify_timeline_tags() -> None:
    timeline_path = OUT / "map2d" / "event_timeline.json"
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    assert timeline.get("scenario") == EXPECTED_SCENARIO
    jumps = timeline.get("jumps") or []
    assert len(jumps) == 7

    non_empty = {j["idx"] for j in jumps if j.get("cause_tags")}
    empty = {j["idx"] for j in jumps if not j.get("cause_tags")}
    assert non_empty == {0, 2, 3, 4}
    assert empty == {1, 5, 6}

    tags = [tag for jump in jumps for tag in jump.get("cause_tags") or []]
    assert len(jumps[0]["cause_tags"]) == 2
    assert all(len(jump.get("cause_tags") or []) == 1 for jump in jumps if jump["idx"] in {2, 3, 4})
    for tag in tags:
        assert {"key", "tag", "truth_type", "scenario", "evidence_ref"} <= set(tag)
        assert tag["truth_type"] in {"real", "derived"}
        assert tag["scenario"] == EXPECTED_SCENARIO

    with (SCENE_DIR / "weather_alerts.csv").open(encoding="utf-8-sig", newline="") as fh:
        alerts = list(csv.DictReader(fh))
    alert = next(row for row in alerts if row["alert_id"] == timeline["alert_id"])
    rain = next(tag for tag in jumps[0]["cause_tags"] if tag["key"] == "rain.alert")
    level_cn = {"blue": "蓝色", "yellow": "黄色", "orange": "橙色", "red": "红色"}
    expected_value = f"{level_cn.get(alert['level'].lower(), alert['level'])} {alert['rainfall_mm_3h']} mm/3h"
    assert rain["value"] == expected_value
    assert alert["status"] == "active"
    assert alert["valid_from"].startswith(timeline["event_date"])

    for tag in tags:
        filename, key = tag["evidence_ref"].split("#", 1)
        with (SCENE_DIR / filename).open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert any(
            value == key
            for row in rows
            for column, value in row.items()
            if column.endswith("_id")
        )


async def main() -> int:
    verify_timeline_tags()
    report: dict[str, object] = {}
    from playwright.async_api import async_playwright

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
        report["portal_secondary"] = {
            t: (t in portal) for t in CLAIMS["portal_secondary"]
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
    return report_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
