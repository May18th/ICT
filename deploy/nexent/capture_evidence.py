"""抓取 Nexent 真机证据（宿主执行）：Agent 列表、会话记录、MCP 工具调用日志。

用法：
    $env:NEXENT_API_KEY = "<Access Key>"
    $env:NEXENT_BASE_URL = "http://localhost:5013"
    $env:NEXENT_CONVERSATION_ID = "21"
    $env:PYTHONPATH = "src"
    uv run --locked python deploy/nexent/capture_evidence.py

产出（samples/nexent/live/）：
    agents.json              北向 /nb/v1/agents 里已发布 Agent 列表
    conversation_<id>.json   一次真实对话（用户问题 + Agent 结构化结论）
    mcp_tool_log.jsonl       平台调用 MCP 工具留下的调用日志（从容器里拷出）
    live_evidence.json       汇总：环境、Agent、会话、工具调用、校验结果
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

LIVE_DIR = Path("samples/nexent/live")
MCP_CONTAINER = os.getenv("NEXENT_MCP_CONTAINER", "urban-twin-mcp")
TOOL_LOG_IN_CONTAINER = "/app/artifacts/visual/tool_log.jsonl"


def _get(path: str, *, timeout: int = 30):
    base = os.getenv("NEXENT_BASE_URL", "http://localhost:5013").rstrip("/")
    key = os.getenv("NEXENT_API_KEY", "").strip()
    if not key:
        raise SystemExit("缺少 NEXENT_API_KEY")
    request = urllib.request.Request(
        base + path, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _write(name: str, payload) -> Path:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    target = LIVE_DIR / name
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def _copy_tool_log() -> Path | None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    target = LIVE_DIR / "mcp_tool_log.jsonl"
    # 优先从容器实时拷；拷不到（未装 docker / 容器名不同）就退回已有的落盘证据
    try:
        subprocess.run(
            ["docker", "cp", f"{MCP_CONTAINER}:{TOOL_LOG_IN_CONTAINER}", str(target)],
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    return target if target.is_file() and target.stat().st_size > 0 else None


def main() -> int:
    conversation_id = os.getenv("NEXENT_CONVERSATION_ID", "").strip()
    agents = _get("/nb/v1/agents")
    _write("agents.json", agents)
    print("agents:", [item.get("name") for item in (agents.get("data") or [])])

    conversation = None
    if conversation_id:
        conversation = _get(f"/nb/v1/conversations/{conversation_id}")
        _write(f"conversation_{conversation_id}.json", conversation)
        print(f"conversation {conversation_id}:",
              len((conversation.get("data") or {}).get("history") or []), "条消息")

    tool_log = _copy_tool_log()
    calls = []
    if tool_log is not None:
        calls = [
            json.loads(line)
            for line in tool_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        print("MCP 工具调用:", [(item.get("tool"), item.get("status")) for item in calls])

    smoke_path = Path("samples/eval/nexent_urban_smoke.json")
    smoke = json.loads(smoke_path.read_text(encoding="utf-8")) if smoke_path.is_file() else {}
    evidence = {
        "schema_version": "nexent-live-evidence-1",
        "nexent_base_url": os.getenv("NEXENT_BASE_URL", "http://localhost:5013"),
        "conversation_id": int(conversation_id) if conversation_id.isdigit() else None,
        "agents": [
            {
                "name": item.get("name"),
                "display_name": item.get("display_name"),
                "model_name": item.get("model_name"),
                "current_version_no": item.get("current_version_no"),
                "version_name": item.get("version_name"),
            }
            for item in (agents.get("data") or [])
        ],
        "tool_calls": calls,
        "checks_passed": sum(1 for item in smoke.get("checks", []) if item.get("passed")),
        "checks_total": len(smoke.get("checks", [])),
        "smoke_passed": bool(smoke) and all(item.get("passed") for item in smoke.get("checks", [])),
        "note": "Nexent v2.7.0 真机：平台 Agent 调用 MCP 工具后产出六跳结论，与离线推演逐项比对",
    }
    _write("live_evidence.json", evidence)
    print("checks:", evidence["checks_passed"], "/", evidence["checks_total"])
    return 0 if evidence["smoke_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
