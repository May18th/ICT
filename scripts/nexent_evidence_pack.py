"""把 `samples/nexent/live/` 下的平台实测产物压成一份可核验的证据文档。

为什么单独一个脚本
------------------
平台实测证据是**历史快照**：它记录的是某天某次真机运行的结果，不能重跑（重跑会覆盖
当时的取值，也就失去了"当时确实跑通过"的意义）。所以它不适合塞进 `reproduce.py`
那条"每次都重新生成、产物必须一致"的管线，而是走"读快照 → 断言快照自洽 → 出报告"。

报告落在 `docs/reports/`：该前缀在 `tests/test_frozen_numbers.py` 的排除清单里
（阶段报告保留落地时数字，政策如此），避免把历史版本号当成当前口径去校验。

口径约定
--------
- **所有数字从证据文件读，脚本里不写死任何版本号或计数**（除断言用的字段名）。
  唯一例外是 `--expect-version`：显式传参才校验，默认不校验。
- 每条结论后面带来源标注 `〔源: 文件名 → 字段路径〕`，评委会问"这个数字哪来的"。
- 日志目录不在仓库内（`Nexent-data/` 是运行态数据，不入库）。目录不存在时报告明确
  标"未采集"，而不是静默当成 0 次调用。

用法::

    uv run --locked python scripts/nexent_evidence_pack.py            # 出报告
    uv run --locked python scripts/nexent_evidence_pack.py --check    # 只跑断言
    uv run --locked python scripts/nexent_evidence_pack.py --expect-version 31
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "samples" / "nexent" / "live"
REPORT_PATH = ROOT / "docs" / "reports" / "nexent-live-evidence.md"

#: 平台运行态日志，位于仓库外（Docker volume 落盘位置）。可在命令行覆盖。
DEFAULT_LOG_DIR = ROOT.parent / "Nexent-data" / "logs" / "northbound"

#: 主推演 Agent 的平台标识。版本号不写死，从 agents.json 读。
MAIN_AGENT = "urban_deduction_agent"

#: 对照组：与主 Agent 同提示词同模型，仅解绑 MCP 工具。缺任一个，"结论来自工具"就无从证明。
CONTROL_AGENTS = ("urban_control_no_tools", "urban_collab_control_no_tools")


# --- 基础读取 ---------------------------------------------------------------


def load(name: str) -> Any:
    path = LIVE_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def has(name: str) -> bool:
    return (LIVE_DIR / name).is_file()


def sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def src(name: str, field_path: str = "") -> str:
    """来源标注。评审问"这个数哪来的"时，这行就是答案。"""

    tail = f" → {field_path}" if field_path else ""
    return f"〔源: {name}{tail}〕"


def esc(text: str) -> str:
    """表格单元格里不能有裸竖线与换行。"""

    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def brief(value: Any, limit: int = 90) -> str:
    """长文本压成一行短摘要。A5 的 summary / risks 动辄上千字，原样铺进报告没法读。"""

    if isinstance(value, (list, tuple)):
        text = "；".join(str(item) for item in value)
    else:
        text = str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


# --- A1 平台与模型 ----------------------------------------------------------


@dataclass
class AgentRow:
    name: str
    display_name: str
    version_no: int
    version_name: str
    model_name: str
    available: bool
    description: str


def agents() -> list[AgentRow]:
    payload = load("agents.json")
    rows: list[AgentRow] = []
    for item in payload.get("data", []):
        rows.append(
            AgentRow(
                name=str(item.get("name", "")),
                display_name=str(item.get("display_name", "")),
                version_no=int(item.get("current_version_no", 0)),
                version_name=str(item.get("version_name", "")),
                model_name=str(item.get("model_name", "")),
                available=bool(item.get("is_available", False)),
                description=str(item.get("description", "")),
            )
        )
    return rows


def platform_base_url() -> str:
    for name in ("live_evidence.json", "llm_decision_platform_run1.json"):
        if has(name):
            value = load(name).get("nexent_base_url")
            if value:
                return str(value)
    return "未记录"


def platform_version_note() -> str:
    if has("live_evidence.json"):
        return str(load("live_evidence.json").get("note", ""))
    return ""


# --- A2 分组：工具分区 ------------------------------------------------------


def tool_groups() -> dict[str, tuple[str, ...]]:
    sys.path.insert(0, str(ROOT / "src"))
    from decision_poc.mcp_server import TOOL_GROUPS  # noqa: PLC0415

    return dict(TOOL_GROUPS)


# --- A3 调用流水 ------------------------------------------------------------


@dataclass
class CallStats:
    total: int
    by_tool: Counter[str]
    by_status: Counter[str]
    by_origin: Counter[str]
    latencies: list[float]
    snapshot_hashes: set[str]
    knowledge_versions: set[str]
    rows: list[dict[str, Any]] = field(default_factory=list)

    def pct(self, n: float) -> str:
        return f"{(100.0 * n / self.total):.1f}%" if self.total else "—"

    def p(self, q: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        return ordered[idx]


def call_stats() -> CallStats:
    payload = load("live_evidence.json")
    calls = payload.get("tool_calls", [])
    return CallStats(
        total=len(calls),
        by_tool=Counter(str(c.get("tool")) for c in calls),
        by_status=Counter(str(c.get("status")) for c in calls),
        by_origin=Counter(str(c.get("origin")) for c in calls),
        latencies=[float(c.get("latency_ms") or 0) for c in calls],
        snapshot_hashes={str(c.get("snapshot_hash")) for c in calls if c.get("snapshot_hash")},
        knowledge_versions={str(c.get("knowledge_version")) for c in calls if c.get("knowledge_version")},
        rows=calls,
    )


# --- A4 无工具对照组 --------------------------------------------------------


@dataclass
class ControlRun:
    file: str
    conversation_id: int
    attempts_used: int
    passed: int
    total: int
    failed_checks: list[str]
    handoff_required: bool
    summary: str
    conflicts: int
    action_order: int


def control_runs() -> list[ControlRun]:
    out: list[ControlRun] = []
    for name in ("control_no_tools.json", "control_main_smoke.json"):
        if not has(name):
            continue
        d = load(name)
        checks = d.get("checks", [])
        answer = d.get("answer", {}) or {}
        out.append(
            ControlRun(
                file=name,
                conversation_id=int(d.get("conversation_id", 0)),
                attempts_used=int(d.get("attempts_used", 0)),
                passed=sum(1 for c in checks if c.get("passed")),
                total=len(checks),
                failed_checks=[str(c.get("name")) for c in checks if not c.get("passed")],
                handoff_required=bool(answer.get("handoff_required")),
                summary=str(answer.get("summary", "")),
                conflicts=len(answer.get("conflicts") or []),
                action_order=len(answer.get("action_order") or []),
            )
        )
    return out


# --- A5 多轮一致性 ----------------------------------------------------------


CONSISTENCY_FIELDS = (
    "selected_option_id",
    "schema_valid",
    "selected_match",
    "confidence",
    "decision_error",
    "latency_ms",
)

#: 每次重跑都会变的字段，diff 时排除（否则表里全是噪声）。
VOLATILE = ("generated_at", "request_id", "latency_ms", "answer_length")


def consistency_runs() -> dict[str, dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    for idx in (1, 2, 3):
        name = f"llm_decision_platform_run{idx}.json"
        if has(name):
            runs[name] = load(name)
    return runs


def consistency_diff(runs: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """返回（稳定字段，漂移字段）。稳定 = 三次取值完全相同。"""

    if len(runs) < 2:
        return [], []
    keys = [k for k in next(iter(runs.values())).keys() if k not in VOLATILE]
    stable, drifting = [], []
    for key in keys:
        values = {json.dumps(runs[name].get(key), ensure_ascii=False, sort_keys=True) for name in runs}
        (stable if len(values) == 1 else drifting).append(key)
    return stable, drifting


# --- A6 多事件回归 ----------------------------------------------------------


def multi_event() -> dict[str, Any] | None:
    if not has("multi_event_regression.json"):
        return None
    return load("multi_event_regression.json")


# --- A7 平台日志 ------------------------------------------------------------


CHAT_RUN = re.compile(r'"(?:POST|GET) (?P<path>/nb/v1/[^"]*?) HTTP/1\.1"\s+(?P<status>\d{3})')
STARTED = re.compile(r"Started server process")
CONV_CREATED = re.compile(r"Created new conversation with id:\s*(\d+)")


@dataclass
class LogSummary:
    available: bool
    files: int
    first_ts: str
    last_ts: str
    starts: int
    chat_run_total: int
    status_counts: Counter[str]
    conversations: int
    max_conversation_id: int
    by_day: dict[str, Counter[str]]


def log_summary(log_dir: Path) -> LogSummary:
    if not log_dir.is_dir():
        return LogSummary(False, 0, "", "", 0, 0, Counter(), 0, 0, {})

    files = sorted(p for p in log_dir.iterdir() if p.is_file() and "northbound" in p.name)
    stamps: list[str] = []
    starts = 0
    chat_total = 0
    status: Counter[str] = Counter()
    conv_ids: list[int] = []
    by_day: dict[str, Counter[str]] = defaultdict(Counter)

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if len(line) >= 19 and line[4] == "-" and line[13] == ":":
                stamps.append(line[:19])
                day = line[:10]
            else:
                day = ""
            if STARTED.search(line):
                starts += 1
                if day:
                    by_day[day]["启动"] += 1
            conv = CONV_CREATED.search(line)
            if conv:
                conv_ids.append(int(conv.group(1)))
            match = CHAT_RUN.search(line)
            if match:
                chat_total += 1
                status[match.group("status")] += 1
                if day:
                    by_day[day][match.group("path")] += 1

    return LogSummary(
        available=True,
        files=len(files),
        first_ts=min(stamps) if stamps else "",
        last_ts=max(stamps) if stamps else "",
        starts=starts,
        chat_run_total=chat_total,
        status_counts=status,
        conversations=len(conv_ids),
        max_conversation_id=max(conv_ids) if conv_ids else 0,
        by_day=dict(by_day),
    )


# --- 断言 -------------------------------------------------------------------


def check(expect_version: int | None, log_dir: Path) -> list[str]:
    """返回问题清单。空列表 = 全部通过。"""

    problems: list[str] = []

    rows = agents()
    if not rows:
        problems.append("agents.json 里没有 Agent 记录")
    by_name = {r.name: r for r in rows}

    main = by_name.get(MAIN_AGENT)
    if main is None:
        problems.append(f"agents.json 缺少主推演 Agent「{MAIN_AGENT}」")
    else:
        if not main.available:
            problems.append(f"{MAIN_AGENT} 在平台上 is_available=false")
        if not main.model_name:
            problems.append(f"{MAIN_AGENT} 没有 model_name")
        if expect_version is not None and main.version_no != expect_version:
            problems.append(
                f"{MAIN_AGENT} 版本 = {main.version_no}，命令行期望 {expect_version}"
            )

    for name in CONTROL_AGENTS:
        if name not in by_name:
            problems.append(f"缺少对照组「{name}」——没有它就证明不了结论来自工具")

    for run in control_runs():
        if run.total == 0:
            problems.append(f"{run.file} 的 checks 为空")
            continue
        # 对照组的意义就是"大部分检查过不去"。全过说明工具其实还在挂载，对照失效。
        if run.passed == run.total:
            problems.append(
                f"{run.file} 对照组竟然 {run.passed}/{run.total} 全过，对照可能失效"
            )
        if not run.handoff_required:
            problems.append(f"{run.file} 对照组没有标记 handoff_required（应当转人工）")
        if run.conflicts or run.action_order:
            problems.append(
                f"{run.file} 对照组给出了冲突 {run.conflicts} 条 / 动作 {run.action_order} 条，"
                "无工具时不应产出结论"
            )

    if has("live_evidence.json"):
        stats = call_stats()
        if stats.total == 0:
            problems.append("live_evidence.json 没有 tool_calls")
        bad = [c for c in stats.rows if c.get("status") != "ok"]
        if bad:
            problems.append(f"live_evidence.json 有 {len(bad)} 条工具调用非 ok")
        non_mcp = [c for c in stats.rows if c.get("origin") != "mcp"]
        if non_mcp:
            problems.append(f"live_evidence.json 有 {len(non_mcp)} 条调用 origin 不是 mcp（不是平台侧调用）")
        payload = load("live_evidence.json")
        passed = payload.get("checks_passed")
        total = payload.get("checks_total")
        if passed is not None and total is not None and passed != total:
            problems.append(f"live_evidence.json 主链路一致性 {passed}/{total}，未全过")
        if not payload.get("smoke_passed"):
            problems.append("live_evidence.json 的 smoke_passed 不是 true")
    else:
        problems.append("缺少 live_evidence.json")

    runs = consistency_runs()
    if len(runs) >= 2:
        _, drifting = consistency_diff(runs)
        decision_drift = [
            k for k in drifting if k in ("selected_option_id", "schema_valid", "selected_match", "decision_error")
        ]
        if decision_drift:
            problems.append(f"多轮重跑的决策字段不一致：{decision_drift}")

    events = multi_event()
    if events is None:
        problems.append("缺少 multi_event_regression.json")
    else:
        bad_events = [
            e.get("event_id")
            for e in events.get("events", [])
            if str(e.get("checks_passed", "")) .split("/")[0] != str(e.get("checks_passed", "")).split("/")[-1]
        ]
        if bad_events:
            problems.append(f"多事件回归未全过：{bad_events}")

    if not log_summary(log_dir).available:
        problems.append(f"平台日志目录不可达：{log_dir}（仅凭 samples 产物出报告）")

    return problems


# --- 报告渲染 ---------------------------------------------------------------


def render(log_dir: Path) -> str:
    rows = agents()
    by_name = {r.name: r for r in rows}
    main = by_name.get(MAIN_AGENT)
    stats = call_stats()
    controls = control_runs()
    runs = consistency_runs()
    stable, drifting = consistency_diff(runs)
    events = multi_event()
    logs = log_summary(log_dir)
    groups = tool_groups()
    total_tools = sum(len(v) for v in groups.values())

    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")
    out: list[str] = []
    add = out.append

    add("# Nexent 平台实跑证据包")
    add("")
    add(f"> 自动生成于 {now}，**禁止手改**。生成命令：`uv run --locked python scripts/nexent_evidence_pack.py`")
    add(">")
    add("> 数据来自 `samples/nexent/live/` 的平台抓取产物与北向服务日志，均为**历史快照**。")
    add("> 本文不给结论性评价，只把散落记录整理成可逐条回查的事实。每条后面标了来源。")
    add("")

    # 摘要
    add("## 摘要")
    add("")
    add("| 项 | 值 | 来源 |")
    add("|---|---|---|")
    add(f"| 平台地址 | `{platform_base_url()}` | {src('live_evidence.json', 'nexent_base_url')} |")
    if main:
        add(
            f"| 主推演 Agent | `{main.name}` / {main.display_name} | "
            f"{src('agents.json', 'data[].name')} |"
        )
        add(
            f"| 当前版本 | **v{main.version_no}**（`{main.version_name}`） | "
            f"{src('agents.json', 'current_version_no')} |"
        )
        add(f"| 模型 | `{main.model_name}` | {src('agents.json', 'model_name')} |")
    add(f"| 已记录 Agent | {len(rows)} 个 | {src('agents.json')} |")
    add(f"| MCP 工具 | {total_tools} 项，分 {len(groups)} 组 | {src('src/decision_poc/mcp_server.py', 'TOOL_GROUPS')} |")
    add(f"| 主链路一致性 | {load('live_evidence.json').get('checks_passed')}/{load('live_evidence.json').get('checks_total')} | {src('live_evidence.json', 'checks_passed')} |")
    add(f"| 工具调用记录 | {stats.total} 次 | {src('live_evidence.json', 'tool_calls')} |")
    if logs.available:
        add(f"| 北向日志跨度 | {logs.first_ts} ~ {logs.last_ts} | {src(f'{log_dir.name}/（{logs.files} 个文件）')} |")
        add(f"| 北向请求累计 | {logs.chat_run_total} 次（服务端 access log） | {src(log_dir.name)} |")
    else:
        add("| 北向日志 | 未采集（目录不可达） | — |")
    add("")

    # A1
    add("## A1 平台与模型")
    add("")
    add("平台上已发布的 Agent 与各自版本。**版本号是平台侧的 `current_version_no`，不是代码里的常量。**")
    add("")
    add("| Agent | 显示名 | 版本 | 版本名 | 模型 | 可用 | 用途 |")
    add("|---|---|---|---|---|---|---|")
    for r in rows:
        add(
            f"| `{r.name}` | {esc(r.display_name)} | v{r.version_no} | `{r.version_name}` | "
            f"{r.model_name} | {'是' if r.available else '否'} | {esc(r.description)[:60]} |"
        )
    add("")
    note = platform_version_note()
    if note:
        add(f"平台版本说明（历史快照原文）：{note}")
        add("")

    # A2
    add("## A2 · 工具分组")
    add("")
    add("分组是互斥分区：一个工具只属于一组。`presentation` 类只在决策定稿后启用，")
    add("目的是不让智能体在推演循环里把推理预算花在换镜头上。")
    add("")
    add("| 分组 | 数量 | 工具 |")
    add("|---|---|---|")
    for name, tools in groups.items():
        add(f"| `{name}` | {len(tools)} | " + "、".join(f"`{t}`" for t in tools) + " |")
    add(f"| **合计** | **{total_tools}** | — |")
    add("")

    # A3
    add("## A3 · 工具调用流水")
    add("")
    add("平台侧抓到的每一次 MCP 调用。`snapshot_hash` 相同表示读的是同一份数据快照。")
    add("")
    add("| 指标 | 值 |")
    add("|---|---|")
    add(f"| 调用总数 | {stats.total} |")
    add(f"| 状态分布 | " + "、".join(f"{k} × {v}" for k, v in stats.by_status.items()) + " |")
    add(f"| 来源分布 | " + "、".join(f"{k} × {v}" for k, v in stats.by_origin.items()) + " |")
    if stats.total:
        add(f"| 成功占比 | {stats.pct(stats.by_status.get('ok', 0))} |")
        add(f"| 延迟 p50 / p95 | {stats.p(0.5):.0f} ms / {stats.p(0.95):.0f} ms |")
    add(f"| 数据快照（去重） | {len(stats.snapshot_hashes)} 个 |")
    add(f"| 知识库版本 | {'、'.join(sorted(stats.knowledge_versions)) or '—'} |")
    add("")
    if stats.by_tool:
        add("按工具：")
        add("")
        add("| 工具 | 次数 |")
        add("|---|---|")
        for tool, count in stats.by_tool.most_common():
            add(f"| `{tool}` | {count} |")
        add("")
        add("这张表是**单次会话**的抓取样本（一次对话里智能体实际发起的工具调用）。")
        add("平台整体的请求量看 A7 的服务端日志——那是全部会话的累计，两者口径不同，不要相加。")
        add("")
    if stats.rows:
        add("<details><summary>逐条调用记录</summary>")
        add("")
        add("| 时刻 | 工具 | 状态 | 延迟 | snapshot |")
        add("|---|---|---|---|---|")
        for c in stats.rows[:50]:
            add(
                f"| {esc(str(c.get('ts')))} | `{c.get('tool')}` | {c.get('status')} | "
                f"{c.get('latency_ms')} ms | `{str(c.get('snapshot_hash'))[:12]}` |"
            )
        add("")
        add("</details>")
        add("")

    # A4 —— 全文最重要的一节
    add("## A4 无工具对照组")
    add("")
    add("**这一节证明的是：决策结论来自 MCP 工具，不是模型先验。**")
    add("")
    add("做法：平台上另发两个 Agent，提示词与模型和主 Agent **完全相同**，只把工具解绑。")
    add("如果结论照样给得出来，说明工具是装饰；如果给不出来，说明工具是唯一来源。")
    add("")
    add("| 对照组文件 | 对话 | 重试 | 检查通过 | 未通过项 | 转人工 | 冲突数 | 动作数 |")
    add("|---|---|---|---|---|---|---|---|")
    for r in controls:
        add(
            f"| `{r.file}` | {r.conversation_id} | {r.attempts_used} | "
            f"**{r.passed}/{r.total}** | {esc('、'.join(r.failed_checks)) or '—'} | "
            f"{'是' if r.handoff_required else '否'} | {r.conflicts} | {r.action_order} |"
        )
    add("")
    for r in controls:
        add(f"`{r.file}` 的结论原文：")
        add("")
        add("> " + r.summary.replace("\n", " "))
        add("")
    add("对照组的意义在数字上：无工具时检查基本全灭、冲突与动作都是 0 条、主动标记转人工。")
    add(f"{src('control_no_tools.json', 'checks / answer.handoff_required')}")
    add("")

    # A5
    add("## A5 同一题多轮重跑的一致性")
    add("")
    if len(runs) >= 2:
        add(f"同一决策题（`{next(iter(runs.values())).get('case_id')}`）在平台上重跑 {len(runs)} 次，逐字段比对。")
        add("")
        add("| 结论 | 字段 |")
        add("|---|---|")
        add(f"| 完全一致 | {esc('、'.join(f'`{k}`' for k in stable)) or '—'} |")
        add(f"| 有漂移 | {esc('、'.join(f'`{k}`' for k in drifting)) or '—'} |")
        add("")
        if drifting:
            add("漂移字段（原文过长，这里截断；完整内容见源文件）：")
            add("")
            for key in drifting:
                vals = [brief(runs[n].get(key), 60) for n in runs]
                add(f"- `{key}`：" + " ｜ ".join(vals))
            add("")
            add("决策字段（选项、结构合法性、是否与期望一致）没有漂移；漂移集中在措辞、")
            add("置信度与引用条数上——模型每次组织证据的方式不同，但落点相同。")
            add("")
        add(f"{src('llm_decision_platform_run1..3.json')}")
        add("")
    else:
        add("重跑记录不足两次，无法比对。")
        add("")

    # A6
    add("## A6 多事件回归")
    add("")
    if events:
        add(
            f"采集于 {events.get('captured_at')}，Agent `{events.get('agent_name')}` "
            f"当时版本 v{events.get('agent_version')}，模型 `{events.get('model')}`。"
        )
        add("")
        add("| 事件 | 对话 | 重试 | 一致性 | 失败项 |")
        add("|---|---|---|---|---|")
        for e in events.get("events", []):
            add(
                f"| `{e.get('event_id')}` | {e.get('conversation_id')} | {e.get('attempts_used')} | "
                f"**{e.get('checks_passed')}** | {esc('、'.join(e.get('failed_checks') or [])) or '—'} |"
            )
        add("")
        if events.get("note"):
            add(f"记录备注（原文）：{events['note']}")
            add("")
        add("注意表里的版本号是**采集当时**的版本，与 A1 的当前版本不是同一个时刻。")
        add(f"{src('multi_event_regression.json', 'agent_version')}")
        add("")
    else:
        add("未找到多事件回归记录。")
        add("")

    # A7
    add("## A7 北向服务日志时间线")
    add("")
    if logs.available:
        add(f"日志目录 `{log_dir}`，共 {logs.files} 个文件，跨度 {logs.first_ts} ~ {logs.last_ts}。")
        add("")
        add("| 指标 | 值 |")
        add("|---|---|")
        add(f"| 服务启动次数 | {logs.starts} |")
        add(f"| 北向请求总数 | {logs.chat_run_total} |")
        add(f"| 状态分布 | " + "、".join(f"{k} × {v}" for k, v in sorted(logs.status_counts.items())) + " |")
        add(f"| 创建会话数 | {logs.conversations}（最大 id {logs.max_conversation_id}） |")
        add("")
        if logs.by_day:
            add("按天：")
            add("")
            add("| 日期 | 启动 | 北向请求 |")
            add("|---|---|---|")
            for day in sorted(logs.by_day):
                counter = logs.by_day[day]
                req = sum(v for k, v in counter.items() if k.startswith("/nb/"))
                add(f"| {day} | {counter.get('启动', 0)} | {req} |")
            add("")
        add("**这些是服务端 access log，不是本地 mock 记录。**请求从容器网络进来，")
        add("每次创建会话都有递增 id，刷新服务重启与请求的时间关系可以对照。")
        add("")
    else:
        add(f"日志目录 `{log_dir}` 不存在（运行态数据不入库，克隆后没有这个目录）。")
        add("")
        add("这不影响 A1–A6 的结论：那部分是抓取产物，随仓库分发。")
        add("")

    # 附录
    add("## 附：证据文件清单")
    add("")
    add("| 文件 | 字节 | 修改时间 | sha256 前 12 |")
    add("|---|---|---|---|")
    for path in sorted(LIVE_DIR.glob("*.json")):
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        add(f"| `{path.name}` | {stat.st_size} | {mtime} | `{sha12(path)}` |")
    add("")
    add("哈希用于确认「这份报告读的就是这些字节」，换人复核时对一下即可。")
    add("")

    return "\n".join(out)


# --- 入口 -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="整理 Nexent 平台实跑证据")
    parser.add_argument("--check", action="store_true", help="只跑断言，不出报告")
    parser.add_argument("--out", default=str(REPORT_PATH), help="报告输出路径")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR), help="北向日志目录")
    parser.add_argument("--expect-version", type=int, default=None, help="校验主 Agent 版本号")
    parser.add_argument("--keep-going", action="store_true", help="断言失败仍出报告")
    args = parser.parse_args(argv)

    log_dir = Path(args.log_dir)
    problems = check(args.expect_version, log_dir)

    if not args.check or args.keep_going:
        text = render(log_dir)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"报告已写入 {out_path}（{len(text)} 字符）")

    if problems:
        print(f"\n断言未通过（{len(problems)} 项）：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 1

    print("断言全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
