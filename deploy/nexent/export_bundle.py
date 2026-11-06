"""导出平台作品包（初赛「配置能力」项）。

为什么需要它
------------
官方赋能直播（BV18Kxv6jEuF，17:05）明确：初赛要交「配置能力」= 用平台导出功能导出作品。
平台导出实现（`Nexent/backend/management/services/agent/management.py:1410`）：

- Agent 挂了 Skill → 返回 zip：`<agent_name>.zip`，内含 `agent.json` + `skills/<name>.zip`
- 未挂 Skill → 返回 `agent.json` 的 JSON

注意：讲师口语说的「DSR / DCR 文件」在平台上**不存在同名格式**，别按这个名字找。

运行前提
--------
平台管理 API 需要 JWT，密钥只在 `nexent-config` 容器里，所以拷进容器用后端 venv 跑：

    docker cp deploy/nexent/platform_client.py nexent-config:/tmp/platform_client.py
    docker cp deploy/nexent/export_bundle.py    nexent-config:/tmp/export_bundle.py
    docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/export_bundle.py \\
        --agent-id 3 --out /tmp/urban_deduction_agent.zip --json /tmp/agent_export.json"
    docker cp nexent-config:/tmp/urban_deduction_agent.zip "samples/nexent/live/urban_deduction_agent.zip"
    docker cp nexent-config:/tmp/agent_export.json       "samples/nexent/live/agent_export.json"

产物用途
--------
`agent_export.json` 里的 sha256 是导出包指纹，写进初赛材料后可被评委复算核对。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from platform_client import config_api, mint_token  # noqa: E402


def _post_binary(path: str, payload: dict, token: str, timeout: int = 180):
    """POST 并返回 (status, content_type, bytes)。导出端点可能回 zip 或 JSON，不能假定 JSON。"""
    request = urllib.request.Request(
        config_api() + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", "") if exc.headers else "", exc.read()
    except Exception as exc:  # noqa: BLE001 - 平台侧异常要可读回传
        return -1, "", f"{type(exc).__name__}: {exc}".encode("utf-8")


def _bundle_summary(blob: bytes) -> dict:
    """解开 zip，核对 agent.json + skills/ 是否齐全，并给出内容级指纹。

    为什么除整包 sha256 还要算内容指纹：平台用 `zipfile.writestr` 打包且未指定时间戳，
    zip 条目头会带**导出当刻的本地时间**，所以同一个 Agent 两次导出的整包 sha256 必然不同。
    要一个可复算的稳定指纹，必须落到**解压后的内容**（agent.json 原文 + 每个 skill zip 原始字节）。
    """
    summary: dict = {"is_zip": zipfile.is_zipfile(io.BytesIO(blob)), "entries": []}
    if not summary["is_zip"]:
        return summary
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for info in archive.infolist():
            payload = archive.read(info.filename)
            summary["entries"].append({
                "name": info.filename,
                "bytes": info.file_size,
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
        names = [item["name"] for item in summary["entries"]]
        # 内容指纹：只覆盖业务语义（agent.json + skills/*.zip），不含 zip 头时间戳
        stable = [(item["name"], item["sha256"]) for item in summary["entries"]
                  if item["name"] == "agent.json" or item["name"].startswith("skills/")]
        summary["content_sha256"] = hashlib.sha256(
            "\n".join(f"{name}:{digest}" for name, digest in sorted(stable)).encode("utf-8")
        ).hexdigest()
        summary["has_agent_json"] = "agent.json" in names
        summary["skill_zips"] = [n for n in names if n.startswith("skills/") and n.endswith(".zip")]
        if summary["has_agent_json"]:
            try:
                agent = json.loads(archive.read("agent.json").decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                summary["agent_json_error"] = f"{type(exc).__name__}: {exc}"
            else:
                # agent.json 实测结构：{"agent_id": 3, "agent_info": {"3": {...}}, "mcp_info": [...]}
                agents = list((agent.get("agent_info") or {}).values()) if isinstance(agent, dict) else []
                first = agents[0] if agents else {}
                summary["agent_json_top_keys"] = sorted(agent.keys()) if isinstance(agent, dict) else []
                summary["agent_count"] = len(agents)
                summary["agent_name"] = first.get("name")
                summary["agent_display_name"] = first.get("display_name")
                summary["agent_enabled"] = first.get("enabled")
                summary["agent_models"] = first.get("model_names")
                summary["agent_tool_count"] = len(first.get("tools") or [])
                summary["agent_tools"] = [t.get("class_name") for t in (first.get("tools") or [])
                                          if isinstance(t, dict)]
                summary["agent_skill_names"] = first.get("skill_names") or []
                summary["mcp_servers"] = [
                    {"name": item.get("mcp_server_name"), "url": item.get("mcp_url")}
                    for item in (agent.get("mcp_info") or []) if isinstance(item, dict)
                ]
                summary["has_duty_prompt"] = bool(first.get("duty_prompt"))
                summary["has_constraint_prompt"] = bool(first.get("constraint_prompt"))
                summary["requested_output_tokens"] = first.get("requested_output_tokens")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 Nexent 平台作品包并核验结构")
    parser.add_argument("--agent-id", type=int, default=3, help="要导出的 agent_id（urban_deduction_agent=3）")
    parser.add_argument("--out", default="/tmp/agent_export.zip", help="导出包落盘路径")
    parser.add_argument("--json", default="", help="证据 JSON 落盘路径（可选）")
    args = parser.parse_args(argv)

    status, content_type, blob = _post_binary("/agent/export", {"agent_id": args.agent_id}, mint_token())
    result: dict = {
        "agent_id": args.agent_id,
        "endpoint": config_api() + "/agent/export",
        "http_status": status,
        "content_type": content_type,
        "bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest() if status == 200 else "",
    }

    if status != 200:
        result["error"] = blob.decode("utf-8", errors="replace")[:800]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    with open(args.out, "wb") as handle:
        handle.write(blob)
    result["out"] = args.out
    result["bundle"] = _bundle_summary(blob)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
