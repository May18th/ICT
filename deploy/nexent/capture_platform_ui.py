"""抓取 Nexent 平台界面截图 / 录屏，产出可核对的答辩材料证据。

为什么要单独一个脚本
-------------------
平台界面截图以前是"人工开浏览器、截一张图、放进 PPT"：既不能复现，也没法核对页面里到底
有没有出现我们宣称的东西。这个脚本把"截哪几页、必须出现哪些文字、图存哪、图多大/哈希多少"
写成可执行的断言：任一期望文字没出现就退出码 1，图纸不会冒充通过。

产物
----
    <out>/skill-space.png      技能仓库页（必须能看到两个 SKILL.md 技能名）
    <out>/agents.png           智能体页（必须能看到城市内涝跨部门决策智能体）
    <out>/mcp-space.png        MCP 页（必须能看到 urban-twin-mcp）
    <out>/platform_ui.json     每页 URL / 命中文字 / PNG 大小与 sha256 / 期望项通过情况
    <out>/platform_tour.webm   加 `--video` 时的页面浏览录屏

用法（宿主机，需要已启动 Nexent 前端与 Playwright）：

    $env:NEXENT_WEB_URL        = "http://127.0.0.1:3000"
    $env:NEXENT_ADMIN_EMAIL    = "admin@nexent.com"        # Agent 所有者所在租户的用户
    $env:NEXENT_ADMIN_PASSWORD = "<平台密码>"               # 只从环境变量读，不写进仓库
    uv run --with playwright python deploy/nexent/capture_platform_ui.py \
        --out docs/reports/captures/nexent --video

边界（必须如实说明）：
    - 脚本只截图、不改平台数据；录屏是页面浏览过程，不是"问答全过程"。
    - 使用本机已安装的 Microsoft Edge（`channel="msedge"`），不额外下载浏览器。
    - 密码只从环境变量读；脚本不打印密码，也不把它写进证据文件。

退出码：0 全部期望命中；1 有期望未命中或页面打不开；2 缺环境变量 / 缺 Playwright。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_WEB_URL = "http://127.0.0.1:3000"
DEFAULT_LOCALE = "zh"
DEFAULT_OUT = "docs/reports/captures/nexent"

# 每页：页面名 → (路径, 必须出现的文字, 说明)。文字按"页面上真能看到"来写，
# 不写只有 API 才查得到的东西，避免截图证据与断言脱节。
PAGES: List[Dict[str, Any]] = [
    {
        "name": "skill-space",
        "path": "/zh/skill-space",
        "click": "我的 Skill",
        "expect": ["urban-six-hop-deduction", "scarcity-allocation-evolution"],
        "purpose": "平台技能仓库「我的 Skill」：两个 SKILL.md 技能已上传到平台（非本地文件）",
    },
    {
        "name": "agents",
        "path": "/zh/agents",
        "expect": ["城市内涝跨部门决策智能体"],
        "purpose": "平台智能体列表：已发布的城市内涝决策 Agent（版本号以截图为准）",
    },
    {
        "name": "mcp-space",
        "path": "/zh/mcp-space",
        "click": "我的MCP",
        # 卡片右上角的"连通性校验"按钮：先让平台真的去连一次，截图里才是"连接成功"而不是陈旧状态。
        # 现在「我的MCP」里只剩 urban-twin-mcp 一个（无关的 kg-mcp-server 已清理），点第一个即可。
        "click_selector": 'button[aria-label="连通性校验"]',
        # 卡片角标现在实时显示 15 个工具（2026-11-05 删掉旧注册、重加 urban-twin-mcp 并重扫工具后刷新）。
        "expect": ["urban-twin-mcp", "15 个工具", "连接成功"],
        "purpose": "平台 MCP「我的MCP」：urban-twin-mcp 连通性校验通过，15 个工具可用",
    },
]


def sha256_file(path: Path) -> str:
    """截图哈希：写进证据，改图即对不上。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_summary(pages: List[Dict[str, Any]], captured_at: str, web_url: str) -> Dict[str, Any]:
    """汇总证据，并给出"是否可以对外宣称"的唯一判据 `all_expectations_met`。"""
    return {
        "schema_version": "platform-ui-capture-1",
        "captured_at": captured_at,
        "web_url": web_url,
        "pages": pages,
        "all_expectations_met": all(page.get("ok") for page in pages) and bool(pages),
        "boundary": [
            "截图来自本机 Nexent v2.7.0 前端，不是公网部署；换环境需重跑本脚本。",
            "录屏只覆盖页面浏览，不包含问答全过程；问答一致性证据见 samples/nexent/live/。",
        ],
    }


async def _login(page, email: str, password: str, timeout: int) -> None:
    """登录（中文界面）：先点欢迎弹窗的"登录账户"，再填邮箱口令。"""
    await page.click("button:has-text('登录账户')", timeout=timeout)
    await page.wait_for_timeout(1200)
    await page.fill("#email", email)
    await page.fill("#password", password)
    await page.click("button[type=submit]")
    await page.wait_for_timeout(6000)


async def _capture(cfg: Dict[str, Any], out_dir: Path) -> None:
    from playwright.async_api import async_playwright

    web_url = os.getenv("NEXENT_WEB_URL", DEFAULT_WEB_URL).rstrip("/")
    email = os.getenv("NEXENT_ADMIN_EMAIL", "")
    password = os.getenv("NEXENT_ADMIN_PASSWORD", "")
    if not email or not password:
        raise SystemExit("缺少 NEXENT_ADMIN_EMAIL / NEXENT_ADMIN_PASSWORD（不要写进仓库）")

    timeout = int(os.getenv("NEXENT_UI_TIMEOUT_MS", "60000"))
    out_dir.mkdir(parents=True, exist_ok=True)
    pages: List[Dict[str, Any]] = []
    captured_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel=cfg["channel"], headless=not cfg["headed"])
        context_kwargs: Dict[str, Any] = {"viewport": {"width": 1440, "height": 900}, "locale": "zh-CN"}
        if cfg["video"]:
            context_kwargs["record_video_dir"] = str(out_dir)
            context_kwargs["record_video_size"] = {"width": 1440, "height": 900}
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        await page.goto(web_url, wait_until="domcontentloaded", timeout=timeout)
        await page.wait_for_timeout(4000)
        await _login(page, email, password, timeout)
        print(f"[ui] 已登录：{page.url}", flush=True)

        for spec in PAGES:
            record: Dict[str, Any] = {
                "name": spec["name"],
                "path": spec["path"],
                "purpose": spec["purpose"],
                "expect": list(spec["expect"]),
                "hit": [],
                "missing": [],
                "ok": False,
            }
            try:
                await page.goto(web_url + spec["path"], wait_until="domcontentloaded", timeout=timeout)
                await page.wait_for_timeout(5000)
                if spec.get("click"):
                    # 有些页面的目标内容在标签页后面（如「我的 Skill」/「我的MCP」），先切过去再取证。
                    await page.click(f"text={spec['click']}", timeout=15000)
                    await page.wait_for_timeout(4000)
                if spec.get("click_selector"):
                    await page.locator(spec["click_selector"]).first.click(timeout=15000)
                    await page.wait_for_timeout(5000)
                body = await page.inner_text("body")
                record["url"] = page.url
                record["title"] = await page.title()
                record["hit"] = [text for text in spec["expect"] if text in body]
                record["missing"] = [text for text in spec["expect"] if text not in body]
                shot = out_dir / f"{spec['name']}.png"
                await page.screenshot(path=str(shot), full_page=False)
                record["screenshot"] = str(shot).replace("\\", "/")
                record["screenshot_bytes"] = shot.stat().st_size
                record["screenshot_sha256"] = sha256_file(shot)
                record["ok"] = not record["missing"]
            except Exception as exc:  # noqa: BLE001 - 页面打不开要写进证据而不是崩掉
                record["error"] = f"{type(exc).__name__}: {exc}"[:300]
            pages.append(record)
            state = "OK" if record["ok"] else f"MISSING {record['missing']}"
            print(f"[ui] {record['name']}: {state}", flush=True)

        video_path = None
        video = page.video
        await context.close()  # 关闭上下文才会把录屏写完
        if video is not None:
            video_path = await video.path()
        await browser.close()

    if cfg["video"] and video_path:
        target = out_dir / "platform_tour.webm"
        Path(video_path).replace(target)
        print(f"[ui] 录屏：{target}", flush=True)

    summary = build_summary(pages, captured_at, web_url)
    if cfg["video"] and video_path:
        summary["video"] = str(out_dir / "platform_tour.webm").replace("\\", "/")
    evidence_path = out_dir / "platform_ui.json"
    evidence_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ui] 证据：{evidence_path}", flush=True)
    raise SystemExit(0 if summary["all_expectations_met"] else 1)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="抓取 Nexent 平台界面截图/录屏并核对页面文字")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"产物目录（默认 {DEFAULT_OUT}）")
    parser.add_argument("--channel", default="msedge", help="Playwright 浏览器通道（默认本机 Edge）")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认无头）")
    parser.add_argument("--video", action="store_true", help="额外录制页面浏览过程为 webm")
    args = parser.parse_args(argv[1:])

    cfg = {
        "channel": args.channel,
        "headed": args.headed,
        "video": args.video,
    }
    try:
        asyncio.run(_capture(cfg, Path(args.out)))
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
