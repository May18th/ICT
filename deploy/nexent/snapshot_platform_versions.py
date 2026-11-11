#!/usr/bin/env python3
"""平台版本快照：把 Nexent 上各 Agent 的当前版本与本地生产策略版本对齐成一张表。

**为什么读库而不是调接口**：北向（5013）只暴露 chat / agents / knowledge-bases 三类端点，
没有版本历史；`/agent/{id}/versions` 挂在 config 服务的内部路由上，宿主侧经网关访问当前
返回 502（upstream connect failed），而版本快照本来就是只读事实——直接从 `nexent` schema
的 `ag_tenant_agent_t` 取，既不绕过业务写入，也不依赖网关是否健康。

**只读**：本脚本只执行 select。平台侧任何写操作都不在这里。

用法：
    python deploy/nexent/snapshot_platform_versions.py
    python deploy/nexent/snapshot_platform_versions.py --json   # 只打印 JSON
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts" / "ledger" / "platform_versions.json"
HISTORY = ROOT / "artifacts" / "feedback" / "history.jsonl"

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


def platform_agents(*, from_stdin: bool = False) -> list[dict]:
    rows = _split(sys.stdin.read()) if from_stdin else _psql(AGENT_SQL)
    agents: list[dict] = []
    for agent_id, name, latest, current, count, updated, enabled in rows:
        agents.append(
            {
                "agent_id": int(agent_id),
                "name": name,
                "current_version": int(current) if current else None,
                "latest_version": int(latest) if latest else None,
                "version_rows": int(count),
                "updated": updated or None,
                "enabled": enabled == "t",
            }
        )
    return agents


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


def build_snapshot(*, from_stdin: bool = False) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": f"{PG_CONTAINER}:{PG_DB}.nexent.ag_tenant_agent_t (只读)",
        "local_policy": local_policy(),
        "platform_agents": platform_agents(from_stdin=from_stdin),
    }


def render(snapshot: dict) -> str:
    policy = snapshot["local_policy"]
    lines = [
        f"本地生产策略：{policy['production_label']}",
        f"反馈流水：{policy['feedback_runs']} 条，其中触发演化 {policy['triggered_runs']} 条"
        f"（最近演化结果 {policy['latest_triggered_label'] or '—'}）",
        "",
        f"{'agent_id':>8}  {'name':<26}{'当前版本':>8}{'最新草稿':>8}{'版本行':>6}  {'更新时间':<12}状态",
        "-" * 86,
    ]
    for agent in snapshot["platform_agents"]:
        lines.append(
            f"{agent['agent_id']:>8}  {agent['name']:<26}"
            f"{(agent['current_version'] if agent['current_version'] is not None else '—'):>8}"
            f"{(agent['latest_version'] if agent['latest_version'] is not None else '—'):>8}"
            f"{agent['version_rows']:>6}  {agent['updated'] or '—':<12}"
            f"{'启用' if agent['enabled'] else '停用'}"
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
    args = parser.parse_args(argv)

    try:
        snapshot = build_snapshot(from_stdin=args.from_stdin)
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
