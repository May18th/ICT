"""把平台技能仓库里的 SKILL.md 绑定到 Agent，并在重新发布后复核版本快照（幂等）。

为什么不能只上传、必须再绑定
---------------------------
上传（`upload_skills.py`）只让技能在平台"技能仓库"里可浏览；**Agent 运行时看不到它**。
平台把"哪个技能属于哪个 Agent"放在 `ag_skill_instance_t` 表里，分两份：
草稿（`version_no=0`）与已发布版本快照（`version_no>=1`）。运行期（非 debug）读的是
`current_version_no` 那份快照，所以正确顺序是：

    绑草稿（本脚本 all） → 重新发布（`setup_nexent.py`） → 复核已发布版本（本脚本 verify）

平台约束（源码 `apps/skill_app.py::update_skill_instance`）：
`POST /api/skills/instance/update` 只接受 `version_no=0`，且要求执行人对该 Agent 有草稿编辑权
（`services/agent_draft_permission_service.require_agent_draft_edit`）。

用法（宿主机执行）：

    docker cp deploy/nexent/platform_client.py nexent-config:/tmp/platform_client.py
    docker cp deploy/nexent/bind_skills.py      nexent-config:/tmp/bind_skills.py
    docker cp samples/nexent/skill              nexent-config:/tmp/nexent_skills
    docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/bind_skills.py all --json /tmp/skill_bindings.json"

子命令：
    all      绑定到 Agent 草稿 → 复核草稿；退出码 0 表示两个技能都已在草稿实例表里
    verify   只读：复核草稿与"当前已发布版本"是否都带这两个技能（发布后才能过）
    status   只读：打印指定 Agent 在草稿/各发布版本上的技能实例

退出码：0 全部就位；1 绑定或复核失败；2 技能目录/参数缺失。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from platform_client import PlatformClient, config_api  # noqa: E402
from upload_skills import iter_skill_files, parse_skill_file, sha256_hex, skills_dir_from_env  # noqa: E402

DRAFT_VERSION = 0


# --- 平台读取 -------------------------------------------------------------


def find_agent(client: PlatformClient, name: str) -> Optional[Dict[str, Any]]:
    """按名字找 Agent；返回的字典里含 `agent_id` 与 `current_version_no`。"""
    status, agents = client.call("/api/agent/list")
    if status != 200 or not isinstance(agents, list):
        print(f"[agent] 读取失败：{status} {str(agents)[:200]}", file=sys.stderr)
        return None
    for item in agents:
        if item.get("name") == name:
            return item
    return None


def platform_skills(client: PlatformClient) -> Dict[str, Dict[str, Any]]:
    """平台技能仓库：`{skill_name: skill}`（与 upload_skills.py 同一口径）。"""
    status, body = client.call("/api/skills")
    if status != 200:
        raise RuntimeError(f"读取平台技能列表失败：{status} {str(body)[:200]}")
    skills = body.get("skills") if isinstance(body, dict) else body
    if not isinstance(skills, list):
        raise RuntimeError(f"平台技能列表结构异常：{str(body)[:200]}")
    return {item.get("name"): item for item in skills if isinstance(item, dict) and item.get("name")}


def list_instances(client: PlatformClient, agent_id: int, version_no: int) -> list[Dict[str, Any]]:
    """某 Agent 某版本上的技能实例（含 `enabled` 标记）。"""
    status, body = client.call(f"/api/skills/instance/list?agent_id={agent_id}&version_no={version_no}")
    if status not in (200, 404):
        print(f"[instance] 读取失败 version={version_no}：{status} {str(body)[:200]}", file=sys.stderr)
        return []
    instances = (body or {}).get("instances") if isinstance(body, dict) else body
    return [item for item in (instances or []) if isinstance(item, dict)]


# --- 绑定与复核 -----------------------------------------------------------


def bind_one(
    client: PlatformClient,
    *,
    agent_id: int,
    skill_id: int,
    skill_name: str,
    parsed: Dict[str, Any],
) -> Dict[str, Any]:
    """把单个技能绑到 Agent 草稿；返回一条可落盘的证据。"""
    status, body = client.call(
        "/api/skills/instance/update",
        method="POST",
        payload={
            "skill_id": skill_id,
            "agent_id": agent_id,
            "enabled": True,
            "version_no": DRAFT_VERSION,
            "config_values": None,
        },
    )
    ok = status in (200, 201)
    print(f"[bind] {skill_name}(skill_id={skill_id}) -> agent {agent_id} 草稿: {status}"
          + ("" if ok else f"  error={str(body)[:300]}"))
    return {
        "skill": skill_name,
        "skill_id": skill_id,
        "agent_id": agent_id,
        "version_no": DRAFT_VERSION,
        "enabled": True,
        "http_status": status,
        "ok": ok,
        "file_sha256": parsed["file_sha256"],
        "content_sha256": parsed["content_sha256"],
        "error": None if ok else str(body)[:500],
    }


def summarize_instances(instances: list[Dict[str, Any]], expected: Dict[str, int]) -> Dict[str, Any]:
    """把平台实例列表折算成"每个期望技能是否 enabled"的可核对结构。"""
    by_skill_id = {item.get("skill_id"): item for item in instances if isinstance(item, dict)}
    rows = []
    for name, skill_id in sorted(expected.items()):
        item = by_skill_id.get(skill_id)
        rows.append(
            {
                "skill": name,
                "skill_id": skill_id,
                "present": item is not None,
                "enabled": bool(item.get("enabled")) if item else False,
            }
        )
    return {"all_present_enabled": all(row["present"] and row["enabled"] for row in rows), "rows": rows}


def verify(
    client: PlatformClient,
    *,
    agent_name: str,
    expected: Dict[str, int],
    require_published: bool,
) -> Dict[str, Any]:
    """只读复核：草稿 + 当前已发布版本。返回证据，不写文件。"""
    agent = find_agent(client, agent_name)
    if agent is None:
        raise RuntimeError(f"平台智能体列表里找不到 {agent_name}")
    agent_id = int(agent["agent_id"])
    current_version_no = agent.get("current_version_no")

    draft = summarize_instances(list_instances(client, agent_id, DRAFT_VERSION), expected)
    published: Optional[Dict[str, Any]] = None
    if current_version_no:
        published = summarize_instances(list_instances(client, agent_id, int(current_version_no)), expected)
        published["version_no"] = int(current_version_no)

    payload = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config_api": config_api(),
        "agent": {"name": agent_name, "agent_id": agent_id, "current_version_no": current_version_no},
        "expected_skills": expected,
        "draft": draft,
        "published": published,
        "draft_ok": draft["all_present_enabled"],
        "published_ok": bool(published and published["all_present_enabled"]),
    }
    print(f"[verify] agent {agent_name}(id={agent_id}) 草稿 {DRAFT_VERSION}: "
          f"{'OK' if draft['all_present_enabled'] else '缺失/未启用'}"
          f"；已发布版本 {current_version_no}: "
          f"{'OK' if published and published['all_present_enabled'] else '缺失/未发布'}")
    for bucket, label in ((draft, "draft"), (published or {}, "published")):
        for row in bucket.get("rows") or []:
            print(f"  {label:9s} {row['skill']}: present={row['present']} enabled={row['enabled']}")
    ok = payload["draft_ok"] and (payload["published_ok"] or not require_published)
    payload["checked"] = {"require_published": require_published, "ok": ok}
    return payload


# --- 主流程 ---------------------------------------------------------------


def _write_json(path: Optional[str], payload: Dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[evidence] 已写入 {target}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="把平台技能绑定到 Agent 草稿并复核发布版本（幂等）")
    parser.add_argument("command", nargs="?", default="all", choices=["all", "verify", "status"])
    parser.add_argument("--agent", default=os.getenv("NEXENT_URBAN_AGENT", "urban_deduction_agent"),
                        help="目标 Agent 名（默认 $NEXENT_URBAN_AGENT）")
    parser.add_argument("--skills-dir", default=None, help="本地 SKILL.md 目录（默认 $NEXENT_SKILL_DIR 或 /tmp/nexent_skills）")
    parser.add_argument("--json", dest="json_path", default=None, help="证据写入路径（容器内路径）")
    parser.add_argument("--require-published", action="store_true",
                        help="verify 时要求已发布版本也带技能（发布后复核用）")
    args = parser.parse_args(argv[1:])

    root = Path(args.skills_dir) if args.skills_dir else skills_dir_from_env()
    files = iter_skill_files(root)
    if not files:
        print(f"[skills] 在 {root} 下没有找到 */SKILL.md，请先 docker cp 技能目录进容器", file=sys.stderr)
        return 2

    parsed = [parse_skill_file(path) for path in files]
    client = PlatformClient()
    existing = platform_skills(client)
    missing = [item["name"] for item in parsed if item["name"] not in existing]
    if missing:
        print(f"[skills] 平台上还没有这些技能：{missing}；先跑 upload_skills.py", file=sys.stderr)
        return 2
    expected = {item["name"]: int(existing[item["name"]]["skill_id"]) for item in parsed}
    print(f"[skills] 待绑定 {len(expected)} 个：{expected}")

    if args.command == "status":
        payload = verify(client, agent_name=args.agent, expected=expected, require_published=False)
        _write_json(args.json_path, payload)
        return 0

    if args.command == "verify":
        payload = verify(client, agent_name=args.agent, expected=expected, require_published=args.require_published)
        _write_json(args.json_path, payload)
        return 0 if payload["checked"]["ok"] else 1

    agent = find_agent(client, args.agent)
    if agent is None:
        print(f"[agent] 平台里找不到 {args.agent}", file=sys.stderr)
        return 2
    agent_id = int(agent["agent_id"])

    records = [
        bind_one(client, agent_id=agent_id, skill_id=expected[item["name"]], skill_name=item["name"], parsed=item)
        for item in parsed
    ]
    payload: Dict[str, Any] = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config_api": config_api(),
        "agent": {"name": args.agent, "agent_id": agent_id, "current_version_no": agent.get("current_version_no")},
        "expected_skills": expected,
        "bindings": records,
        "draft": summarize_instances(list_instances(client, agent_id, DRAFT_VERSION), expected),
    }
    payload["draft_ok"] = all(record["ok"] for record in records) and payload["draft"]["all_present_enabled"]
    if not payload["draft_ok"]:
        print("[bind] 绑定未全部成功，先修平台侧错误再重新发布", file=sys.stderr)
        _write_json(args.json_path, payload)
        return 1

    # 绑定成功后再复核一次已发布版本：没重发布时应为 False，用来证明"绑了但没发布"确实不可宣称
    payload["published_before"] = verify(
        client, agent_name=args.agent, expected=expected, require_published=False
    )["published"]
    print("[next] 绑定在草稿上，运行期读的是已发布版本：请执行 setup_nexent.py 重新发布，"
          "再用 `bind_skills.py verify --require-published` 复核")
    _write_json(args.json_path, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
