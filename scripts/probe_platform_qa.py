#!/usr/bin/env python3
"""平台问答质量探针：把固定几问打到北向 chat/run，把工具链、命中来源、最终答案摊开看。

**为什么固定问题**：改 duty_prompt、换知识库、同步台账之后，"有没有变差"要能一键复现，
不能每次手写 curl 再肉眼比对。问题本身要覆盖三条不同的取数路径——
运行版本（台账文档）、业务资产（资产索引）、跨部门协同（协同 agent）。

**只读**：本脚本只发起对话，不写平台任何状态；产物默认只打印，`--out` 才落盘。

用法：
    export NEXENT_NB_API_KEY=...
    python scripts/probe_platform_qa.py
    python scripts/probe_platform_qa.py --agent urban_asset_qa --out artifacts/qa/probe.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NB_BASE = os.getenv("NEXENT_NORTHBOUND", "http://127.0.0.1:5013")

# 三问对应三条独立取数路径，任何一条断了都能从结果里一眼看出来
QUESTIONS: tuple[dict[str, str], ...] = (
    {
        "id": "policy-version",
        "agent": "urban_asset_qa",
        "query": "当前生产策略是哪个版本？训练集和留出集的偏差降幅分别是多少？",
        "expect": "policy-ledger",
    },
    {
        "id": "platform-version",
        "agent": "urban_asset_qa",
        "query": "平台上的 urban_deduction_agent 现在跑的是哪个版本？版本名是什么？",
        "expect": "platform-versions",
    },
    {
        "id": "asset-fact",
        "agent": "urban_asset_qa",
        "query": "A-03 片区有哪些泵站？各自的设计流量是多少？",
        "expect": "urban-assets",
    },
)

_URL_RE = re.compile(r'"(?:url|path_or_url)"\s*:\s*"([^"]+)"')


def run_query(agent: str, query: str, *, timeout: int = 200) -> dict:
    key = os.environ.get("NEXENT_NB_API_KEY", "").strip()
    if not key:
        raise SystemExit("缺少 NEXENT_NB_API_KEY（从平台 user_token_info_t.access_key 取）")
    body = json.dumps({"agent_name": agent, "query": query}).encode("utf-8")
    request = urllib.request.Request(
        f"{NB_BASE}/nb/v1/chat/run",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    tools: list[str] = []
    sources: list[str] = []
    final: str | None = None
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "tool":
                name = event.get("tool_name")
                if name and name not in tools:
                    tools.append(name)
            elif kind == "search_content":
                content = event.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                for url in _URL_RE.findall(text):
                    if url not in sources:
                        sources.append(url)
            elif kind == "final_answer":
                final = event.get("content")
    return {"agent": agent, "query": query, "tools": tools, "sources": sources, "answer": final}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="平台问答质量探针（只读）")
    parser.add_argument("--agent", default="", help="覆盖问题里的默认 agent")
    parser.add_argument("--only", default="", help="只跑某个问题 id")
    parser.add_argument("--out", default="", help="结果落盘路径（JSON），默认只打印")
    args = parser.parse_args(argv)

    picked = [
        q for q in QUESTIONS
        if (not args.only or q["id"] == args.only)
    ]
    if not picked:
        print(f"没有问题匹配 --only={args.only}", file=sys.stderr)
        return 1

    results: list[dict] = []
    for item in picked:
        agent = args.agent or item["agent"]
        print(f"\n=== [{item['id']}] {agent} ===")
        print(f"问：{item['query']}")
        try:
            result = run_query(agent, item["query"])
        except Exception as exc:  # noqa: BLE001 - 探针要能跑完剩下几问
            print(f"  失败：{type(exc).__name__}: {exc}")
            results.append({**item, "error": f"{type(exc).__name__}: {exc}"})
            continue
        hit = any(item["expect"] in url for url in result["sources"])
        result["expect_hit"] = hit
        print(f"工具链：{' → '.join(result['tools']) or '（无工具调用）'}")
        print(f"命中来源：{', '.join(result['sources'][:6]) or '（无）'}")
        print(f"期望命中 {item['expect']}：{'是' if hit else '否'}")
        print("回答：")
        print(result["answer"] or "（没有 final_answer 事件）")
        results.append(result)

    ok = sum(1 for r in results if r.get("expect_hit"))
    print(f"\n--- 汇总：{ok}/{len(results)} 问命中期望来源 ---")

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "northbound": NB_BASE,
            "results": results,
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"结果已写入 {target}")
    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
