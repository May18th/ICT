"""导出**已发布版本**的 Agent 配置（不是草稿）。

为什么需要它
------------
平台 HTTP 路由 `/agent/export` 调实现时没传版本号（`apps/agent_app.py:662`），实现默认
`version_no=0`，导出来的是**草稿**：主 Agent 草稿只挂了 2 个工具，协同草稿 0 个，
与已发布的 4 / 6 对不上。实现本身支持 `version_no`（`management.py:1410`），
只是路由没透出来——所以只能在容器里直调。

用法（必须在 `nexent-config` 容器内）
------------------------------------
    docker cp deploy/nexent/platform_client.py   nexent-config:/tmp/platform_client.py
    docker cp deploy/nexent/export_published.py  nexent-config:/tmp/export_published.py
    docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/export_published.py \
        --json /tmp/published_exports.json --out-dir /tmp/published"
    docker cp nexent-config:/tmp/published_exports.json samples/nexent/live/published_exports.json

产物
----
- 挂了技能 → zip（`agent.json` + `skills/*.zip`）；没挂 → JSON
- 清单里逐包记：字节数、整包 sha256（不可复算，zip 头带时间戳）、`agent.json` 的内容 sha256
  与工具名清单——**工具名清单才是用来和发布版本对账的那一项**。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 脚本从 /tmp 执行时 sys.path[0] 是 /tmp，后端包不在里面，显式补上（容器内固定路径）。
if os.path.isdir("/opt/backend"):
    sys.path.insert(0, "/opt/backend")

from platform_client import mint_token  # noqa: E402

#: (agent_id, 期望发布版本号, 名字)。版本号取自北向快照，改版本时同步改这里。
TARGETS = (
    (3, 34, "urban_deduction_agent"),
    (4, 12, "urban_collab_agent"),
    (40, 11, "urban_asset_qa"),
)


def _summarize_zip(blob: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        entries = []
        for info in archive.infolist():
            payload = archive.read(info.filename)
            entries.append({"name": info.filename, "bytes": info.file_size,
                            "sha256": hashlib.sha256(payload).hexdigest()})
        agent_json = archive.read("agent.json") if "agent.json" in archive.namelist() else b""
    data = json.loads(agent_json.decode("utf-8")) if agent_json else {}
    info = data.get("agent_info") or {}
    flat = info.get("name") if "name" in info else next(iter(info.values()), {})
    tools = [t.get("name") or t.get("class_name") for t in flat.get("tools") or []]
    return {
        "entries": entries,
        "agent_json_sha256": hashlib.sha256(agent_json).hexdigest(),
        "tools": tools,
        "tools_count": len(tools),
        "skill_names": flat.get("skill_names") or [],
        "max_steps": flat.get("max_steps"),
        "requested_output_tokens": flat.get("requested_output_tokens"),
        "mcp_info": data.get("mcp_info") or [],
    }


def _summarize_json(data: dict) -> dict:
    info = data.get("agent_info") or {}
    flat = info.get("name") if "name" in info else next(iter(info.values()), {})
    tools = [t.get("name") or t.get("class_name") for t in flat.get("tools") or []]
    return {
        "agent_json_sha256": hashlib.sha256(
            json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "tools": tools,
        "tools_count": len(tools),
        "skill_names": flat.get("skill_names") or [],
        "max_steps": flat.get("max_steps"),
        "requested_output_tokens": flat.get("requested_output_tokens"),
        "mcp_info": data.get("mcp_info") or [],
    }


async def main(argv: list[str] | None = None) -> int:
    from management.services.agent.management import export_agent_with_skills_impl

    ap = argparse.ArgumentParser(description="导出已发布版本的 Agent 配置")
    ap.add_argument("--json", default="", help="清单落盘路径")
    ap.add_argument("--out-dir", default="/tmp/published")
    args = ap.parse_args(argv)

    auth = "Bearer " + mint_token()
    os.makedirs(args.out_dir, exist_ok=True)
    records = []
    for agent_id, version_no, name in TARGETS:
        entry = {"agent_id": agent_id, "name": name, "version_no": version_no}
        try:
            result = await export_agent_with_skills_impl(agent_id, auth, version_no=version_no)
        except Exception as exc:  # noqa: BLE001 - 平台侧异常要可读回传
            entry.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            records.append(entry)
            print(f"[{name}] 失败：{entry['error']}")
            continue

        if isinstance(result, dict) and result.get("_zip"):
            blob = result["data"]
            path = os.path.join(args.out_dir, f"{name}-v{version_no}.zip")
            with open(path, "wb") as handle:
                handle.write(blob)
            entry.update({"ok": True, "kind": "zip", "file": path,
                          "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})
            entry.update(_summarize_zip(blob))
        else:
            path = os.path.join(args.out_dir, f"{name}-v{version_no}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
            entry.update({"ok": True, "kind": "json", "file": path})
            entry.update(_summarize_json(result))
        records.append(entry)
        print(f"[{name}] v{version_no} -> {entry['kind']} 工具 {entry['tools_count']} 个：{entry['tools']}")

    manifest = {
        "schema_version": "nexent-published-exports-1",
        "method": "容器内直调 export_agent_with_skills_impl(version_no=<已发布版本号>)，绕开路由固定导草稿的限制",
        "agents": records,
        "all_ok": all(r.get("ok") for r in records),
    }
    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print("\n" + json.dumps({"all_ok": manifest["all_ok"]}, ensure_ascii=False))
    return 0 if manifest["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
