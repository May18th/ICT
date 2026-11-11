"""补齐 Nexent 平台上内涝系 Agent 的展示内容（开场白 + 示例问题），并发布新版本。

背景
----
`urban_deduction_agent` / `urban_collab_agent` 创建时只写了 description，`greeting_message`
为空、`example_questions` 为 null。评委打开对话窗口看到的是空白输入框，不知道能问什么，
初赛「Agent 构建说明 + 录屏演示」两项都吃亏。

两个坑（2026-11-11 实测，别再踩）
--------------------------------
1. `POST /agent/update` **只写草稿（version_no=0）**，对话读的是已发布版本。改完必须 `publish`，
   否则前端毫无变化。
2. `POST /agent/update` 的请求体**不能带 `version_no`**——它会被当作字段写进
   `UPDATE ... SET version_no=...`，撞 `ag_tenant_agent_t_pkey` 直接 500。

用法
----
    PYTHONPATH=src python deploy/nexent/fill_agent_content.py --dry-run   # 只看将要写入什么
    PYTHONPATH=src python deploy/nexent/fill_agent_content.py --apply     # 写入并发布

凭据只从环境变量读取：`NEXENT_SUPER_ADMIN_PASSWORD`（与 deploy/env/.env 保持一致），
账号默认 `suadmin@nexent.com`，可用 `--email` 覆盖。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:3000/api"

CONTENT: dict[str, dict[str, object]] = {
    "urban_deduction_agent": {
        "greeting_message": (
            "我是城市内涝跨部门决策智能体。给我一个工单号（如 WO-20261029-007），"
            "我按「事件现象 → 受影响设施 → 责任部门 → 资源需求 → 处置动作 → 后续风险」"
            "六跳推演，每个结论都标注依据来源与版本。"
        ),
        "example_questions": [
            "WO-20261029-007 应该怎么处置？先给结论，再给六跳推演链路。",
            "WO-20261029-007 涉及哪些部门职责冲突？",
            "封路对地下管线施工有什么二次风险？",
            "哪些结论的依据来自应急预案？",
        ],
    },
    "urban_collab_agent": {
        "greeting_message": (
            "我是城市内涝应急协同总控。负责跨部门分工、抽排资源调拨与处置顺序裁决，"
            "输出各部门动作卡与联合发布对。"
        ),
        "example_questions": [
            "WO-20261029-007 的跨部门处置顺序怎么排？",
            "只有 6 台抽排设备，A-03 和 B-01 怎么分？",
            "哪些动作卡需要会签才能关闭？",
            "先封路还是先隔离作业面？两种顺序的通行代价差多少？",
        ],
    },
}


def _request(path: str, payload: dict | None, token: str | None = None) -> tuple[int, str, list[str]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode(), resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300], []


def signin(email: str, password: str) -> str:
    status, body, cookies = _request("/user/signin", {"email": email, "password": password})
    if status != 200:
        raise SystemExit(f"登录失败 {status}: {body}")
    for cookie in cookies:
        match = re.match(r"nexent_access_token=([^;]+)", cookie)
        if match:
            return match.group(1)
    raise SystemExit("登录成功但没拿到 nexent_access_token cookie")


def agent_ids(token: str) -> dict[str, int]:
    status, body, _ = _request("/agent/list", None, token)
    if status != 200:
        raise SystemExit(f"读取 Agent 列表失败 {status}: {body}")
    return {item["name"]: item["agent_id"] for item in json.loads(body)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default="suadmin@nexent.com")
    parser.add_argument("--apply", action="store_true", help="真正写入并发布")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写入的内容")
    args = parser.parse_args()

    if not (args.apply ^ args.dry_run):
        raise SystemExit("必须且只能指定 --apply 或 --dry-run 之一")

    if args.dry_run:
        for name, content in CONTENT.items():
            print(f"== {name}")
            print("  greeting:", content["greeting_message"])
            for q in content["example_questions"]:
                print("  -", q)
        return

    password = os.environ.get("NEXENT_SUPER_ADMIN_PASSWORD", "")
    if not password:
        raise SystemExit("缺少 NEXENT_SUPER_ADMIN_PASSWORD 环境变量")

    token = signin(args.email, password)
    ids = agent_ids(token)

    for name, content in CONTENT.items():
        if name not in ids:
            print(f"跳过 {name}：平台上不存在这个 Agent")
            continue
        agent_id = ids[name]

        # 注意：请求体不带 version_no，否则会撞主键（见模块 docstring 第 2 条）
        status, body, _ = _request(
            "/agent/update", {"agent_id": agent_id, **content}, token
        )
        print(f"{name}(id={agent_id}) 草稿更新 -> {status} {body[:80]}")
        if status != 200:
            continue

        status, body, _ = _request(
            f"/agent/{agent_id}/publish",
            {"version_name": "content-polish", "release_note": "补齐开场白与示例问题"},
            token,
        )
        print(f"{name} 发布 -> {status} {body[:120]}")


if __name__ == "__main__":
    main()
