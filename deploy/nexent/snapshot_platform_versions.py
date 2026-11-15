#!/usr/bin/env python3
"""平台版本快照：把 Nexent 上各 Agent 的当前版本与本地生产策略版本对齐成一张表。

**取数走两条路，北向优先**：
1. `GET /nb/v1/agents`（北向，Bearer key）—— 平台对外接口，直接给 `current_version_no`
   与 `version_name`（如 `urban-mcp-v3`）。要说明"平台对外可回读版本"，就得走这条。
2. 回退读库 `nexent.ag_tenant_agent_t` —— 北向没配 key 或服务没起时用，只读 select。

北向**没有版本历史**（只有当前版本），要看逐版本轨迹得走 config 服务的内部路由，
那不在对外契约里，本脚本不碰。

**只读**：本脚本不写平台任何状态。

用法：
    python deploy/nexent/snapshot_platform_versions.py                 # 北向优先，失败回退读库
    python deploy/nexent/snapshot_platform_versions.py --source db     # 强制读库
    python deploy/nexent/snapshot_platform_versions.py --from-stdin    # 从 stdin 吃 psql TSV
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts" / "ledger" / "platform_versions.json"
HISTORY = ROOT / "artifacts" / "feedback" / "history.jsonl"

NB_BASE = os.getenv("NEXENT_NORTHBOUND", "http://127.0.0.1:5013")
PG_CONTAINER = "nexent-postgresql"
PG_USER = "root"
PG_DB = "nexent"

AGENT_SQL = """
select a.agent_id, a.name,
       max(a.version_no)                          as latest_version,
       max(a.current_version_no)                  as current_version,
       count(*)                                   as version_rows,
       max(a.update_time)::date                   as updated,
       bool_or(a.enabled)                          as enabled
from nexent.ag_tenant_agent_t a
where a.delete_flag = 'N'
group by a.agent_id, a.name
order by a.agent_id;
"""


SEP = "\x1f"


def _psql(sql: str) -> list[list[str]]:
    """执行只读查询，返回按行切好的字段。"""
    completed = subprocess.run(
        [
            "docker", "exec", PG_CONTAINER,
            "psql", "-U", PG_USER, "-d", PG_DB,
            "-t", "-A", "-F", SEP, "-c", sql,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"查询失败（平台容器是否在跑？）：{completed.stderr.strip()[:300]}"
        )
    return _split(completed.stdout)


def _split(raw: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        rows.append([cell.strip() for cell in line.split(SEP)])
    return rows


def _nb_agents() -> list[dict]:
    """北向 `GET /nb/v1/agents`：平台对外契约里唯一带当前版本号的端点。"""
    key = os.getenv("NEXENT_NB_API_KEY", "").strip()
    if not key:
        raise RuntimeError("未设置 NEXENT_NB_API_KEY")
    request = urllib.request.Request(
        f"{NB_BASE}/nb/v1/agents",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))
    agents: list[dict] = []
    for item in payload.get("data") or []:
        agents.append(
            {
                "name": item.get("name"),
                "current_version": item.get("current_version_no"),
                "version_name": item.get("version_name"),
                "model": item.get("model_name"),
                "available": bool(item.get("is_available")),
                "enabled": True,
                "updated": None,
                "version_rows": None,
            }
        )
    return sorted(agents, key=lambda agent: agent["name"] or "")


def _agents_from_rows(rows: list[list[str]]) -> list[dict]:
    agents: list[dict] = []
    for agent_id, name, latest, current, count, updated, enabled in rows:
        agents.append(
            {
                "agent_id": int(agent_id),
                "name": name,
                "current_version": int(current) if current else None,
                "latest_version": int(latest) if latest else None,
                "version_rows": int(count),
                "version_name": None,
                "model": None,
                "available": enabled == "t",
                "enabled": enabled == "t",
                "updated": updated or None,
            }
        )
    return agents


def platform_agents(
    *, from_stdin: bool = False, source: str = "auto"
) -> tuple[list[dict], str]:
    """返回 (agents, 实际取数来源)。source: auto / northbound / db。"""
    if from_stdin:
        return _agents_from_rows(_split(sys.stdin.read())), "stdin"
    if source in {"auto", "northbound"}:
        try:
            return _nb_agents(), "northbound"
        except Exception as exc:  # noqa: BLE001 - 北向不可用就回退，不判死
            if source == "northbound":
                raise
            print(f"（北向取数失败：{type(exc).__name__}: {exc}；回退读库）", file=sys.stderr)
    return _agents_from_rows(_psql(AGENT_SQL)), "db"


def local_policy() -> dict:
    """本地生产策略口径：唯一常量 + 反馈流水里最后一次演化结果。"""
    sys.path.insert(0, str(ROOT / "src"))
    from decision_poc.policy_evolution import PRODUCTION_LABEL  # noqa: PLC0415

    runs: list[dict] = []
    if HISTORY.is_file():
        for line in HISTORY.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    triggered = [run for run in runs if run.get("triggered")]
    return {
        "production_label": PRODUCTION_LABEL,
        "feedback_runs": len(runs),
        "triggered_runs": len(triggered),
        "latest_run_label": runs[-1].get("final_label") if runs else None,
        "latest_triggered_label": triggered[-1].get("final_label") if triggered else None,
    }


def build_snapshot(*, from_stdin: bool = False, source: str = "auto") -> dict:
    agents, used = platform_agents(from_stdin=from_stdin, source=source)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": used,
        "source_detail": {
            "northbound": f"{NB_BASE}/nb/v1/agents",
            "db": f"{PG_CONTAINER}:{PG_DB}.nexent.ag_tenant_agent_t",
            "stdin": "psql TSV via stdin",
        }[used],
        "local_policy": local_policy(),
        "platform_agents": agents,
    }


def render(snapshot: dict) -> str:
    policy = snapshot["local_policy"]
    lines = [
        f"本地生产策略：{policy['production_label']}",
        f"反馈流水：{policy['feedback_runs']} 条，其中触发演化 {policy['triggered_runs']} 条"
        f"（最近演化结果 {policy['latest_triggered_label'] or '—'}）",
        f"平台取数：{snapshot['source']}（{snapshot['source_detail']}）",
        "",
        f"{'Agent':<26}{'当前版本':>9}  {'版本名':<20}{'模型':<16}状态",
        "-" * 88,
    ]
    for agent in snapshot["platform_agents"]:
        current = agent.get("current_version")
        lines.append(
            f"{agent.get('name') or '—':<26}"
            f"{('v' + str(current)) if current is not None else '—':>9}  "
            f"{(agent.get('version_name') or '—'):<20}"
            f"{(agent.get('model') or '—'):<16}"
            f"{'可用' if agent.get('available') else '不可用'}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="平台 Agent 版本快照（只读）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON，不写文件")
    parser.add_argument("--out", default=str(OUT), help="快照落点，默认 artifacts/ledger/platform_versions.json")
    parser.add_argument(
        "--from-stdin",
        action="store_true",
        help="从标准输入读 psql 的 TSV（受限环境下无法直接调 docker 时用）",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "northbound", "db"),
        default="auto",
        help="取数来源：auto=北向优先、失败回退读库（默认）",
    )
    args = parser.parse_args(argv)

    try:
        snapshot = build_snapshot(from_stdin=args.from_stdin, source=args.source)
    except RuntimeError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(render(snapshot))
    print(f"\n快照已写入 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
