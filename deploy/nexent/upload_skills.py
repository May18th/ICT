"""把 `samples/nexent/skill/*/SKILL.md` 上传到 Nexent 平台技能仓库（幂等，可重复执行）。

为什么在容器里跑
-----------------
平台管理 API（`/api/...`）用 JWT（HS256，`SUPABASE_JWT_SECRET`）鉴权，密钥只存在于
`nexent-config` 容器环境。上传端点 `POST /api/skills/upload` 只接受 `multipart/form-data`，
且技能内容由平台自己的 `SkillLoader` 解析，所以"解析口径"以平台为准：本脚本解析失败即报错，
不做容错兜底，避免上传出平台读不懂的技能。

用法（宿主机执行）：

    docker cp deploy/nexent/platform_client.py nexent-config:/tmp/platform_client.py
    docker cp deploy/nexent/upload_skills.py nexent-config:/tmp/upload_skills.py
    docker cp samples/nexent/skill nexent-config:/tmp/nexent_skills
    docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/upload_skills.py all --json /tmp/skills_evidence.json"
    docker cp nexent-config:/tmp/skills_evidence.json samples/nexent/live/skills.json

子命令：
    status   只读：打印平台技能仓库现状（技能名 / skill_id / tags / source）
    upload   上传或更新 `NEXENT_SKILL_DIR` 下（默认 `/tmp/nexent_skills`）的每个 SKILL.md
    all      依次 status → upload → status，并可用 `--json` 落盘可核对证据

幂等策略：
    平台已有同名技能且内容/描述/tags 完全一致 → `unchanged`，不发写请求；
    平台已有同名技能但内容漂移 → `PUT /api/skills/<name>/upload` 更新，动作为 `updated`；
    平台没有 → `POST /api/skills/upload` 新建，动作为 `created`。

退出码：0 全部成功；1 有技能上传/更新失败；2 技能目录缺失或配置不全。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from platform_client import PlatformClient, config_api  # noqa: E402

DEFAULT_SKILLS_DIR = "/tmp/nexent_skills"


def skills_dir_from_env() -> Path:
    """技能源目录（容器内路径，由 `docker cp` 准备）。"""
    return Path(os.getenv("NEXENT_SKILL_DIR", DEFAULT_SKILLS_DIR))


def sha256_hex(data: bytes | str) -> str:
    """内容哈希：文件字节与平台存储的正文都按 UTF-8 归一，便于逐位比对。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def iter_skill_files(root: Path) -> list[Path]:
    """列出 `<root>/*/SKILL.md`，按目录名排序，保证证据里顺序稳定。"""
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("*/SKILL.md") if path.is_file())


def parse_skill_file(path: Path) -> Dict[str, Any]:
    """用平台自己的解析器解析 SKILL.md；平台解析器不可用时直接失败。"""
    from nexent.skills.skill_loader import SkillLoader

    raw = path.read_bytes()
    parsed = SkillLoader.load(str(path))
    if not parsed.get("name"):
        raise ValueError(f"{path.name}: 平台解析器没有读到 name 字段")
    parsed["file"] = str(path)
    parsed["file_sha256"] = sha256_hex(raw)
    parsed["content_sha256"] = sha256_hex(parsed.get("content", ""))
    return parsed


def platform_skills(client: PlatformClient) -> Dict[str, Dict[str, Any]]:
    """平台技能仓库现状：`{skill_name: skill}`。

    注意 `/api/skills` 返回的是"当前用户可见技能"列表，包含自己上传的 custom 技能；
    官方技能是否已安装到本租户另由 `/api/skills/official` 表达，本脚本不处理官方技能。
    """
    status, body = client.call("/api/skills")
    if status != 200:
        raise RuntimeError(f"读取平台技能列表失败：{status} {str(body)[:200]}")
    skills = body.get("skills") if isinstance(body, dict) else body
    if not isinstance(skills, list):
        raise RuntimeError(f"平台技能列表结构异常：{str(body)[:200]}")
    return {item.get("name"): item for item in skills if isinstance(item, dict) and item.get("name")}


def _same_content(parsed: Dict[str, Any], existing: Dict[str, Any]) -> bool:
    """平台已有技能与本文件的正文/描述/tags 是否一致（一致则无需写请求）。"""
    if (existing.get("content") or "").strip() != (parsed.get("content") or "").strip():
        return False
    if (existing.get("description") or "").strip() != (parsed.get("description") or "").strip():
        return False
    return list(existing.get("tags") or []) == list(parsed.get("tags") or [])


def _upload(client: PlatformClient, parsed: Dict[str, Any], name: str):
    """新建技能：`POST /api/skills/upload`（multipart，正文由平台解析）。"""
    raw = Path(parsed["file"]).read_bytes()
    return client.call_multipart(
        "/api/skills/upload",
        fields={"skill_name": name, "source": "custom"},
        file_field="file",
        filename="SKILL.md",
        content=raw,
    )


def _update(client: PlatformClient, parsed: Dict[str, Any], name: str):
    """更新已有技能：`PUT /api/skills/<name>/upload`（同一套解析与落盘路径）。"""
    raw = Path(parsed["file"]).read_bytes()
    return client.call_multipart(
        f"/api/skills/{name}/upload",
        fields={},
        file_field="file",
        filename="SKILL.md",
        content=raw,
        method="PUT",
    )


def upload_one(
    client: PlatformClient,
    path: Path,
    existing: Dict[str, Dict[str, Any]],
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """上传/更新单个技能，返回可落盘的一条证据。"""
    parsed = parse_skill_file(path)
    name = str(parsed["name"])
    current = existing.get(name)

    record: Dict[str, Any] = {
        "name": name,
        "file": str(path),
        "file_sha256": parsed["file_sha256"],
        "content_sha256": parsed["content_sha256"],
        "description": parsed.get("description", ""),
        "tags": list(parsed.get("tags") or []),
        "platform_skill_id": (current or {}).get("skill_id"),
        "platform_source": (current or {}).get("source"),
        "action": "skipped",
        "http_status": None,
        "error": None,
    }

    if current is not None and _same_content(parsed, current):
        record["action"] = "unchanged"
        return record
    if dry_run:
        record["action"] = "would-update" if current is not None else "would-create"
        return record

    if current is None:
        status, body = _upload(client, parsed, name)
        record["action"] = "created"
        if status == 409:  # 并发下已被别处创建：退回更新路径
            status, body = _update(client, parsed, name)
            record["action"] = "updated"
    else:
        status, body = _update(client, parsed, name)
        record["action"] = "updated"

    record["http_status"] = status
    if status not in (200, 201):
        record["error"] = str(body)[:300]
        record["action"] = f"failed:{record['action']}"
    elif isinstance(body, dict):
        record["platform_skill_id"] = body.get("skill_id") or record["platform_skill_id"]
        record["platform_source"] = body.get("source") or record["platform_source"]
    return record


def build_evidence(records: list[Dict[str, Any]], skills: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """把本次上传结果与平台复核结果合成一份可核对证据。"""
    verified = []
    for record in records:
        current = skills.get(record["name"])
        stored = (current or {}).get("content") or ""
        verified.append(
            {
                "name": record["name"],
                "file_sha256": record["file_sha256"],
                "content_sha256": record["content_sha256"],
                "stored_content_sha256": sha256_hex(stored.strip()) if stored else None,
                "content_matches": bool(stored) and sha256_hex(stored.strip()) == record["content_sha256"],
            }
        )
    return {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config_api": config_api(),
        "skills": records,
        "platform_verification": verified,
        "platform_skill_names": sorted(skills),
        "all_verified": all(item["content_matches"] for item in verified) and bool(verified),
    }


def _print_records(records: list[Dict[str, Any]]) -> None:
    for record in records:
        error = f"  error={record['error']}" if record.get("error") else ""
        print(
            f"[skill] {record['name']}: {record['action']}"
            f" (http={record['http_status']} skill_id={record['platform_skill_id']}){error}"
        )


def _write_json(path: Optional[str], payload: Dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[evidence] 已写入 {target}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="上传 SKILL.md 到 Nexent 平台技能仓库（幂等）")
    parser.add_argument("command", nargs="?", default="all", choices=["status", "upload", "all"])
    parser.add_argument("--skills-dir", default=None, help="SKILL.md 所在目录（默认 $NEXENT_SKILL_DIR 或 /tmp/nexent_skills）")
    parser.add_argument("--json", dest="json_path", default=None, help="把可核对证据写入该路径（容器内路径）")
    parser.add_argument("--dry-run", action="store_true", help="只判断会做什么，不发写请求")
    args = parser.parse_args(argv[1:])

    root = Path(args.skills_dir) if args.skills_dir else skills_dir_from_env()
    files = iter_skill_files(root)
    if not files:
        print(f"[skills] 在 {root} 下没有找到 */SKILL.md，请先 docker cp 技能目录进容器", file=sys.stderr)
        return 2

    client = PlatformClient()
    if args.command == "status":
        skills = platform_skills(client)
        print(f"[skills] 平台技能仓库共 {len(skills)} 个：{sorted(skills)}")
        for name in sorted(skills):
            item = skills[name]
            print(
                f"[skills] {name}: skill_id={item.get('skill_id')} source={item.get('source')} "
                f"tags={item.get('tags')} desc={(item.get('description') or '')[:40]}"
            )
        _write_json(args.json_path, {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                     "config_api": config_api(),
                                     "platform_skill_names": sorted(skills),
                                     "skills": [skills[name] for name in sorted(skills)]})
        return 0

    existing = platform_skills(client)
    print(f"[skills] 目标目录 {root}，待处理 {len(files)} 个技能；平台已有 {sorted(existing)}")
    records = [upload_one(client, path, existing, dry_run=args.dry_run) for path in files]
    _print_records(records)

    if any(record["action"].startswith("failed") for record in records):
        _write_json(args.json_path, {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                     "config_api": config_api(), "skills": records})
        return 1

    if args.dry_run:
        _write_json(args.json_path, {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                     "config_api": config_api(), "skills": records})
        return 0

    after = platform_skills(client)
    evidence = build_evidence(records, after)
    missing = [record["name"] for record in records if record["name"] not in after]
    print(f"[skills] 平台复核：{sorted(after)}；缺失 {missing}；正文逐位一致={evidence['all_verified']}")
    _write_json(args.json_path, evidence)
    return 0 if not missing and evidence["all_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
