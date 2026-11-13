"""Dependency-free demo payload and browser server for the urban twin.

除推演与进化对比两个页面外，这里还承担两件事：

1. **UE 侧的取数端点**：`/api/scene/<snapshot_id>` 返回渲染作业，`/ack` 让引擎写回认领
   状态——`scene_render` 工具返回的 `delivery` 读的就是这个 ack 文件；
2. **工具调用日志**：`/api/tool-log` 输出 MCP 的 jsonl 调用记录，页面上并排滚动，评委
   要看得出"智能体调了渲染"还是"人点了按钮"，所以每条带 `origin`（mcp / manual）。
"""
from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from . import baseline_compare
from . import chain as scheduler
from . import review_collect as reviews
from . import selfcheck
from . import visualize
from . import web_llm
from .collab import orchestrate
from .counterfactual import Intervention, InterventionKind, rerun_with_interventions
from .deduction import DeductionEngine, DistrictSignal
from .evolution_demo import build_evolution_html, build_evolution_payload
from .flood_feedback import FloodFeedback, plan as feedback_plan, run_evolution, to_feedback_record
from .road_graph import RoadGraph, RoadGraphUnavailable
from .skills import SkillPipeline, SkillRuntime
from .semantic import build_urban_semantic_store
from .timeline import build_event_stream, build_timeline, compare_branches
from .twin_links import MAX_JUMP, build_twin_links, resolve_deeplink
from . import execution_ledger

EVENT_ID = "WO-20261029-007"
AS_OF = "2026-10-29T09:40:00+08:00"

# 情景白名单：键=URL 参数值，值=数据根目录。默认口径（assets/urban）不在表内——
# 只有显式传 ?scenario=<key> 才换根，冻结口径一个字节不动。
# real_20260801 由 scripts/build_real_scenario_dataset.py 生成（真实暴雨映射，见其 README）。
SCENARIO_ROOTS: dict[str, Path] = {
    "real_20260801": Path(__file__).resolve().parents[2]
    / "samples" / "scenarios" / "real_20260801",
}

SCENARIO_INFO: dict[str, dict[str, str]] = {
    "real_20260801": {
        "id": "real_20260801",
        "label": "真实暴雨 · 2026-08-01（成都）",
        "weather_source": "Open-Meteo / ERA5（CC BY 4.0）",
        "weather_provenance": "real",
        "note": "真实雨量映射到演示事件时间窗；等级由工单实测深度驱动，预警佐证降为黄色",
    },
}
DEFAULT_SCENARIO_INFO = {
    "id": "synthetic_v1",
    "label": "合成情景 · 橙色预警（默认口径）",
    "weather_source": "合成（weather_alerts.csv WA-20261029-01）",
    "weather_provenance": "synthetic",
    "note": "冻结口径：六跳/冲突/缺口以本情景为准",
}

# P9 报告里进化后的最终策略（weight_closure=1 / weight_hazard=0.25 / transfer_penalty=0.1）。
# 固化成预设，是为了让"策略变了 → 分配变了"在页面上可点、可复现。
EVOLVED_POLICY: dict[str, Any] = {
    "policy_version": "v4",
    "weight_closure": 1.0,
    "weight_hazard": 0.25,
    "transfer_penalty": 0.1,
}

# 门禁结果落盘目录（追加式历史 + latest 快照）；测试用环境变量指到临时目录。
EVOLUTION_DIR_ENV = "URBAN_TWIN_EVOLUTION_DIR"


def evolution_dir() -> Path:
    override = os.environ.get(EVOLUTION_DIR_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "artifacts" / "evolution"


def save_evolution_gate(result: Mapping[str, Any]) -> Path:
    """把一次门禁结果写 latest.json + 追加 history.jsonl，便于复盘"谁的打分改了哪版"。"""
    directory = evolution_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = dict(result)
    payload["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    latest = directory / "latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (directory / "history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return latest


def evolution_gate_history(limit: int = 10) -> dict[str, Any]:
    directory = evolution_dir()
    latest_path = directory / "latest.json"
    history_path = directory / "history.jsonl"
    latest = None
    if latest_path.is_file():
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = []
    if history_path.is_file():
        for line in history_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return {
        "latest": latest,
        "history": entries[-limit:],
        "total_entries": len(entries),
        "path": str(directory),
    }


def build_demo_payload(
    *,
    event_id: str = EVENT_ID,
    as_of: str = AS_OF,
    scenario: str = "",
) -> dict[str, Any]:
    scenario = str(scenario or "").strip()
    if scenario and scenario not in SCENARIO_ROOTS:
        raise KeyError(scenario)
    if scenario:
        runtime = SkillRuntime(SCENARIO_ROOTS[scenario])
        info = dict(SCENARIO_INFO[scenario])
    else:
        runtime = SkillRuntime()
        info = dict(DEFAULT_SCENARIO_INFO)
    report = SkillPipeline(runtime).run(
        event_id, as_of, caller_labels=["ORG-EMERG"], persist=False
    )
    payload = report.model_dump(mode="json")
    payload["evidence"] = payload.get("audit", {}).get("claims", [])
    # 情景元信息：前端 provenance 徽章与「结论稳健性」演示都从这里取，不硬编码
    facts = payload.get("facts") or {}
    payload["scenario"] = {
        **info,
        "warning_level": facts.get("warning_level") or "",
        "response_level": facts.get("response_level") or "",
        "depth_cm": facts.get("depth_cm"),
    }
    return payload


def weather_replay() -> dict[str, Any]:
    """2026-08-01 真实逐时降雨（动画回放数据源）。

    从气象数据集 CSV 现算，不硬编码：改数据重跑即可，页面自动跟随。
    """
    csv_path = (
        Path(__file__).resolve().parents[2]
        / "samples" / "weather" / "chengdu_hourly_2026-06-01_2026-09-30.csv"
    )
    if not csv_path.is_file():
        # 数据文件名可能随 prepare_weather 输出调整：退回目录内唯一的逐时 CSV
        candidates = sorted(
            (Path(__file__).resolve().parents[2] / "samples" / "weather").glob(
                "chengdu_hourly_*.csv"
            )
        )
        if not candidates:
            return {"error": "气象数据集缺失，先运行 scripts/prepare_weather.py"}
        csv_path = candidates[0]

    points: list[dict[str, Any]] = []
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        cols = {c.lower(): c for c in (reader.fieldnames or [])}
        time_col = cols.get("time") or next(iter(cols))
        rain_col = cols.get("rain_mm") or cols.get("rain") or cols.get("precipitation_mm")
        for row in reader:
            day = str(row.get(time_col) or "")
            if day.startswith("2026-08-01"):
                rain = row.get(rain_col) if rain_col else None
                points.append({"time": day[-5:], "rain_mm": float(rain) if rain not in (None, "") else 0.0})

    points.sort(key=lambda p: p["time"])
    cum = 0.0
    total = sum(p["rain_mm"] for p in points) or 1.0
    for p in points:
        cum += p["rain_mm"]
        p["cum_mm"] = round(cum, 2)
        p["cum_frac"] = round(cum / total, 4)
    peak = max(points, key=lambda p: p["rain_mm"], default=None)
    return {
        "date": "2026-08-01",
        "source": "Open-Meteo / ERA5（CC BY 4.0）· 成都 30.66N/104.07E",
        "total_mm": round(total, 1),
        "peak_mm": peak["rain_mm"] if peak else 0.0,
        "peak_time": peak["time"] if peak else "",
        "note": (
            "真实降雨回放（非推演输入）：动画水位由累计雨量占比驱动，事件推演仍读工单实测深度。"
            "total_mm 为 08-01 自然日累计（77.5mm）；材料中 77.7mm 为最大 24h 滑动窗口口径"
            "（跨自然日），两者出处一致、定义不同"
        ),
        "points": points,
    }


def quant_references() -> dict[str, Any]:
    """定量方法交叉参照（SWMM 对照）供前端展示；数字全部来自同源 JSON，不硬编码。"""
    data_path = (
        Path(__file__).resolve().parents[2]
        / "docs" / "reports" / "swmm-bypass-comparison.json"
    )
    if not data_path.is_file():
        return {"error": "SWMM 对照数据缺失，先运行 scripts/swmm_bypass.py"}
    return json.loads(data_path.read_text(encoding="utf-8"))


def collab_review(*, event_id: str = EVENT_ID, as_of: str = AS_OF) -> dict[str, Any]:
    """协同层复核：把同一次推演交给「1 总控 + 5 部门」编一遍动作卡。

    为什么单独一个端点而不是塞进时间线
    ----------------------------------
    `timeline` 的事件条数与顺序是测试钉死的形状（六跳 6 条 + 冲突 3 条 + 缺口 + 动作 + 审计），
    往里加一类事件会让"固定形状"这条不变量失效。复核是**并联**环节：它读同一份推演，
    产出的是角色动作卡与交接，不是推演的第 7 跳。

    `orchestrate()` 是纯函数且只读 `DeductionResult`，因此这个端点**不改变任何冻结数字**——
    六跳、三类冲突、缺口 2 台都由 `/api/demo` 说了算，这里只回答"这些结论落到部门头上长什么样"。
    复核用的是和推演同源的 `DeductionEngine`（同一 event_id / as_of，确定性重跑），
    拿的是原始 `DeductionResult` 而不是页面投影 `DeductionView`——后者没有 `steps`，编排层不认。
    """
    deduction = DeductionEngine.load().deduce(event_id, as_of=as_of)
    result = orchestrate(deduction)
    cards = [
        {
            "card_id": card.card_id,
            "agent_id": card.agent_id,
            "stage": card.stage,
            "action": card.action,
            "status": str(card.status),
            "targets": list(card.targets),
            "co_sign_required": list(card.co_sign_required),
            "evidence_count": len(card.evidence_refs),
            "claim_count": len(card.claim_ids),
            # 原始引用必须给出去：沙盘靠里面的 `work_order#row:N` 才能把动作卡落到具体对象上，
            # 只给条数就只能"知道有 5 条证据"却指不出是哪张工单（twin_links 依赖这两个字段）。
            "evidence_refs": list(card.evidence_refs),
            "claim_ids": list(card.claim_ids),
            "depends_on": list(card.depends_on),
            "quantity": card.quantity,
            "no_action_reason": card.no_action_reason,
        }
        for card in result.cards
    ]
    return {
        "status": "ok",
        "event_id": result.event_id,
        "counts": {
            "cards": len(result.cards),
            "agents": len(result.covered_agents),
            "handoffs": len(result.handoffs),
            "co_sign_pairs": len(result.co_sign_pairs),
            "violations": len(result.constraint_violations),
        },
        "agents": list(result.covered_agents),
        "cards": cards,
        "handoffs": [item.model_dump(mode="json") for item in result.handoffs],
        "co_sign_pairs": [list(pair) for pair in result.co_sign_pairs],
        "merge_order": list(result.merge_order),
        "violations": list(result.constraint_violations),
        "kernel_fingerprint": result.kernel_fingerprint,
        "rules_version": result.rules_version,
        "source": "deduction → collab.orchestrate（只读复核，不改六跳与缺口）",
    }


# --- 微调度台：人工干预 → 重推演 -------------------------------------------
#
# 干预只走**白名单**：前端只能提交预设 id，不能提交任意补丁。补丁机制会复制数据集并改写
# CSV，若允许外部传 target/field/value，这个端点就成了任意文件改写入口。
INTERVENTIONS: dict[str, dict[str, Any]] = {
    "pump-minus-one": {
        "label": "少调一台移动泵",
        "hint": "A-03 台账 4 台只放行 3 台（真减 1 台），缺口 2 → 3，看方案怎么重排",
        "plain": "把 A-03 可用移动泵从 4 台改为 3 台",
        "kind": "resource",
        "interventions": [
            Intervention(
                kind=InterventionKind.DATA_PATCH,
                target="resource_inventory.csv",
                match={"resource_id": "RES-PUMP-M"},
                field="qty_available",
                value="3",
                note="现场只放行 3 台移动泵",
            )
        ],
    },
    "close-road": {
        "label": "关闭民生街（R-103）",
        "hint": "真把 R-103 置为封闭：到场时间变长，但缺口由库存决定、不会变",
        "plain": "把民生街（R-103）状态改为「封闭」",
        "kind": "road",
        "road_id": "R-103",
        "interventions": [
            Intervention(
                kind=InterventionKind.DATA_PATCH,
                target="road_network.csv",
                match={"road_id": "R-103"},
                field="status",
                value="closed",
                note="现场封闭民生街",
            )
        ],
    },
    "pump-b01-off": {
        "label": "B-01 跨区泵全调不过来",
        "hint": "B-01 的 2 台库存置 0（跨区 40 分钟且车队被堵），看缺口怎么变",
        "plain": "把 B-01 跨区备用移动泵从 2 台改为 0 台",
        "kind": "resource",
        "interventions": [
            Intervention(
                kind=InterventionKind.DATA_PATCH,
                target="resource_inventory.csv",
                match={"resource_id": "RES-PUMP-M-B01"},
                field="qty_available",
                value="0",
                note="跨区移动泵未到位",
            )
        ],
    },
}


def intervention_catalog() -> list[dict[str, Any]]:
    """给页面列可点的干预项。补丁描述直接取 `Intervention.describe()`，不另写一套文案。"""
    return [
        {
            "id": key,
            "label": entry["label"],
            "hint": entry["hint"],
            "plain": entry.get("plain", ""),
            "kind": entry["kind"],
            "patches": [item.describe() for item in entry["interventions"]],
        }
        for key, entry in INTERVENTIONS.items()
    ]


def road_travel(road_id: str) -> dict[str, Any]:
    """封路后的到场时间对照（只算路口到路口，门到门仍然算不出）。

    单独一个函数是为了让"封路到底影响了什么"有据可查：它**不改**缺口，
    只改路网上的距离与时间；页面必须把这两件事分开说，不能合成一句"封路后更严重"。
    """
    try:
        impact = RoadGraph.load().impact(road_id)
    except RoadGraphUnavailable as exc:
        return {"available": False, "road_id": road_id, "reason": str(exc)}
    return {"available": True, **impact.model_dump(mode="json")}


def run_intervention(intervention_id: str) -> dict[str, Any]:
    """跑一次「人工改条件 → 重推演」，返回干预后的时间线与前后对照。

    两侧都现跑（临时数据集副本 + 独立 SkillRuntime），不是对基线结果做加减。
    """
    entry = INTERVENTIONS.get(intervention_id)
    if entry is None:
        raise KeyError(f"未登记的干预项: {intervention_id}")
    result = rerun_with_interventions(
        entry["interventions"],
        event_id=EVENT_ID,
        as_of=AS_OF,
        caller_labels=["ORG-EMERG"],
    )
    payload = {
        "id": intervention_id,
        "label": entry["label"],
        "plain": entry.get("plain", ""),
        "kind": entry["kind"],
        "patches": result["interventions"],
        "mechanism": result["mechanism"],
        "timeline": build_timeline(result["intervened"]),
        "comparison": compare_branches(
            result["baseline"], result["intervened"], interventions=result["interventions"]
        ),
    }
    road_id = entry.get("road_id")
    if road_id:
        payload["travel"] = road_travel(str(road_id))
    return payload


# 注意：下面是普通 Python 字符串，不是 raw 字符串。写 JS 时**不要**用 "\n"——Python 会把它
# 变成真实换行，从而截断 JS 的字符串字面量，整个 <script> 直接解析失败（页面看起来"按钮没反应"）。
# 需要换行请用 String.fromCharCode(10)。`tests/test_web_demo.py` 里有 node --check 兜底。
IMPACT_STYLE = '''<style>.impact-card{--impact-ink:#173238;--impact-muted:#667a7d;--impact-line:#d7e1df;--impact-teal:#0f766e;--impact-teal-soft:#e4f2ef;--impact-coral:#c56d45;position:relative;border:1px solid #d6e1df;background:#f7faf8;color:var(--impact-ink);padding:20px;box-shadow:0 16px 36px rgba(22,49,54,.08)}.impact-card h2{color:var(--impact-ink);font-size:22px;margin:5px 0 10px}.impact-kicker{font-size:11px;font-weight:700;letter-spacing:.08em;color:var(--impact-teal);text-transform:uppercase}.impact-hint{font-weight:400;font-size:12px;color:var(--impact-muted)}.scene-summary{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:2px 0 14px;padding:14px 15px;border-radius:10px;background:#fff;border:1px solid var(--impact-line)}.scene-summary b{font-size:18px;color:var(--impact-ink)}.scene-summary span{color:var(--impact-muted);font-size:13px}.scene-summary small{width:100%;color:#74898a;font-size:12px}.impact-flow{display:grid;grid-template-columns:1fr auto 1fr auto 1fr;align-items:center;gap:8px;margin:0 0 14px;color:var(--impact-muted);font-size:12px}.impact-flow span{padding:10px 8px;border:1px solid #c8dbd7;border-radius:8px;background:var(--impact-teal-soft);color:var(--impact-ink);text-align:center;font-weight:700}.impact-flow b{color:var(--impact-coral);font-size:16px}.impact-card svg{height:330px;border:1px solid #27464b;box-shadow:0 8px 20px rgba(22,49,54,.12)}.impact-card .lgd{border-top:1px solid var(--impact-line);padding-top:11px;color:var(--impact-muted)}.impact-card .lgd i{border:1px solid rgba(23,50,56,.2)}.scene-detail{margin-top:14px;padding:15px;border-radius:10px;background:#fff;border:1px solid var(--impact-line);min-height:104px}.scene-empty{color:var(--impact-muted);font-size:13px;line-height:1.7}.scene-detail-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}.scene-detail-head b{font-size:16px;color:var(--impact-ink)}.scene-kind,.scene-state{font-size:11px;padding:4px 8px;border-radius:999px;background:var(--impact-teal-soft);color:var(--impact-teal)}.scene-state{background:#fff0e8;color:#9a4d2e}.scene-detail-grid{display:grid;grid-template-columns:1.3fr 1fr 1fr;gap:12px}.scene-detail-grid>div{min-width:0}.scene-detail-grid small{display:block;color:#78908f;font-size:11px;margin-bottom:4px}.scene-detail-grid strong{display:block;color:var(--impact-ink);font-size:13px;font-weight:500;line-height:1.55;word-break:break-word}.scene-evidence{margin-top:12px;border-top:1px solid var(--impact-line);padding-top:10px}.scene-evidence summary{cursor:pointer;color:var(--impact-teal);font-size:12px}.scene-evidence summary:focus-visible{outline:2px solid var(--impact-coral);outline-offset:3px}.scene-evidence .evidence{margin-top:7px;line-height:1.5;color:var(--impact-muted)}.impact-card #assets{margin-top:14px;padding-top:12px;border-top:1px solid var(--impact-line);color:var(--impact-muted)}.impact-card #assets b{color:var(--impact-ink);font-size:12px}.impact-card #assets .pill{background:#edf5f3;color:var(--impact-ink);border:1px solid #d5e4e0}.impact-card .ent{transition:filter 180ms ease,opacity 180ms ease}.impact-card .ent:hover,.impact-card .ent.selected{filter:brightness(1.18);stroke:#fff;stroke-width:3}.impact-card .ent:focus-visible{outline:none;filter:brightness(1.2)}.entlist{display:grid;gap:8px}
.ent-item{display:flex;align-items:center;gap:10px;padding:9px 12px;border:1px solid var(--impact-line);border-radius:9px;background:#fff;cursor:pointer;transition:border-color .15s,box-shadow .15s}
.ent-item:hover,.ent-item.selected{border-color:var(--impact-teal);box-shadow:0 2px 8px rgba(15,118,110,.15)}
.ent-item .no{flex:0 0 22px;height:22px;border-radius:50%;background:var(--impact-teal-soft);color:var(--impact-teal);font:700 12px/22px ui-monospace,monospace;text-align:center}
.ent-item b{font-size:13.5px;color:var(--impact-ink)}
.ent-item small{display:block;font-size:12px;color:var(--impact-muted);margin-top:2px}.ent-item .ent-kind{margin-left:auto;flex:0 0 auto;font-size:11px;padding:3px 8px;border-radius:999px;background:#eef2f4;color:#5a6b6d}
.ent-item:focus-visible{outline:2px solid var(--impact-coral);outline-offset:2px}
@media(max-width:800px){.scene-detail-grid{grid-template-columns:1fr}.impact-flow{display:flex;flex-wrap:wrap}.impact-flow span{flex:1 1 28%}.impact-flow b{flex:0 0 auto}}
.deep{color:var(--impact-teal);font-weight:600;text-decoration:none;border-bottom:1px dashed rgba(15,118,110,.45);white-space:nowrap}
.deep:hover{border-bottom-style:solid}
.tl .mt .deep{font-size:11.5px}
.exrow{display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px dashed #e2e8e6;font-size:13px;color:#3f5357}
.exrow .who{flex:0 0 130px;color:#0f766e;font-weight:600}
.exrow .act{flex:1 1 auto;min-width:0;color:#42585c;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.exrow button{padding:4px 10px;font-size:12px;flex:0 0 auto}
.st{font-size:11px;padding:1px 6px;border-radius:4px;background:#eef2f1;color:#5b6d70;flex:0 0 auto}
.st.done{background:#e4f2ef;color:#0f766e}.st.running{background:#e6f0fa;color:#1d5c8f}
.st.needs_confirm{background:#fdf1dc;color:#9a5b1f}.st.cancelled{background:#fbeaea;color:#a03a3a}
.exmsg{margin-top:8px;font-size:12px;color:#9a5b1f;min-height:16px}
.exclose{margin-top:10px;padding:8px 10px;border:1px solid #d6e1df;border-radius:8px;background:#f7faf8;font-size:12px;color:#42585c}
.exclose b{color:#9a5b1f}</style>'''
PAGE_STYLE = '''<style>
:root{--page-bg:#eef2ef;--ink:#173238;--muted:#6c7e7f;--line:#d9e3e0;--surface:#fff;--accent:#0f766e;--accent-soft:#e2f1ed;--warning:#c56d45;--shadow:0 10px 28px rgba(23,50,56,.07)}
*{box-sizing:border-box}body{font-family:"Inter","Noto Sans SC","Microsoft YaHei",system-ui,sans-serif;background:var(--page-bg);color:var(--ink);line-height:1.55}main{max-width:1360px;padding:36px 40px 72px}.page-hero{display:flex;align-items:flex-end;justify-content:space-between;gap:28px;padding:22px 0 30px;border-bottom:1px solid var(--line)}.page-hero .eyebrow{font-size:11px;font-weight:800;letter-spacing:.12em;color:var(--accent);text-transform:uppercase}.page-hero h1{margin:7px 0 8px;color:var(--ink);font-size:clamp(28px,3vw,42px);line-height:1.12;font-weight:800}.page-hero .sub{max-width:760px;color:var(--muted);font-size:15px}.guide{grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:20px 0 0;border-top:0}.guide>div{min-height:112px;padding:17px 18px;background:rgba(255,255,255,.72);border:1px solid var(--line);border-radius:14px;color:var(--muted);line-height:1.55}.guide>div:first-child{padding-left:18px;border-left:1px solid var(--line)}.guide b{color:var(--accent);font:800 11px ui-monospace,monospace;letter-spacing:.08em}.guide span{color:var(--ink);font-size:16px;margin:6px 0 5px}.toolbar{gap:10px;margin:18px 0}.toolbar button,button{min-height:42px;padding:10px 16px;border:1px solid transparent;border-radius:10px;background:var(--accent);color:#fff;font-weight:750;box-shadow:0 5px 12px rgba(15,118,110,.16);transition:background 180ms ease,transform 180ms ease,box-shadow 180ms ease}.toolbar button:hover,button:hover{background:#0b625d;transform:translateY(-1px);box-shadow:0 8px 16px rgba(15,118,110,.22)}button:focus-visible,a:focus-visible{outline:3px solid rgba(197,109,69,.45);outline-offset:3px}.navlink{color:var(--accent);font-weight:750}.card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:22px;box-shadow:var(--shadow)}section.card{margin-top:20px!important}.card h2{margin:0 0 16px;color:var(--ink);font-size:19px;line-height:1.25}.card h2 span{color:var(--muted)!important;font-size:12px!important;font-weight:500!important}.sub{color:var(--muted)}#summary{padding:24px 26px;background:#183b3f;border-color:#183b3f;color:#fff;box-shadow:0 16px 32px rgba(24,59,63,.18)}#summary h2{color:#fff}#summary .sub{color:#c7dcd8}#metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.metric{display:block;margin:0;padding:14px 15px;background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.16);border-radius:12px;color:#c7dcd8}.metric b{margin-bottom:4px;color:#fff;font-size:24px;line-height:1.1}.primary-grid{grid-template-columns:minmax(0,1.35fr) minmax(300px,.65fr);gap:20px;align-items:start}.primary-grid>.card{height:100%}.data-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.data-grid>.card{min-height:220px}.process-card{background:#f8fbfa}.chain{gap:2px}.lnk{grid-template-columns:32px 150px minmax(0,1fr) minmax(0,220px);padding:12px 0 12px 14px;border-left-color:#c9d9d6}.lnk.on{border-left-color:var(--accent);opacity:1}.lnk .no{color:#78908f}.lnk .who{color:#68807f}.lnk .what b{color:var(--ink)}.lnk .what div{color:var(--muted)}.lnk .tool{color:var(--accent)}.row,.ord{border-bottom-color:#e3ebe8}.row b{color:var(--accent)}.pill,.tag{background:#edf5f3;color:var(--accent);border:1px solid #d5e4e0}.warn{color:var(--warning)}.ok{color:#247a58}.trace{border-left-color:var(--accent)}.plan{gap:12px}.plan .col{background:#f8fbfa;border-color:var(--line);border-radius:12px}.plan .col h3{color:var(--ink)}.plan .col.now{border-color:#78b7a9;background:#f0f9f5}.plan .col.alt{border-color:#e2b493;background:#fff8f2}.ivbtn,.ghost,.cat{background:#edf5f3!important;color:var(--accent)!important;border:1px solid #cfe1dc!important;box-shadow:none!important}.ivbtn:hover,.ghost:hover,.cat:hover{background:#dceee9!important}.cat.sel{background:var(--accent)!important;color:#fff!important}.tl li{border-left-color:#d4e1de}.tl li.on{border-left-color:var(--accent)}.tl .ttl{color:var(--ink)}.tl .dsc{color:var(--muted)}.tl .mt{color:#78908f}.tl ul.basis{color:var(--muted)}.tech{margin-top:28px;border:1px solid var(--line);border-radius:16px;background:rgba(255,255,255,.62)}.tech>summary{padding:17px 20px;color:var(--ink);font-size:14px}.tech>summary::before{color:var(--accent)}.tech>div{padding:0 20px 22px}.tech .card{box-shadow:none;background:#fbfdfc}.tech #panel,.tech #comparebody,.tech #llmbody{color:var(--muted)}#log{border-color:var(--line)}.impact-card{box-shadow:var(--shadow)}@media(max-width:960px){main{padding:26px 24px 56px}.page-hero{display:block}.page-hero .navlink{display:inline-block;margin-top:14px}.primary-grid,.data-grid{grid-template-columns:1fr}.primary-grid>.card{height:auto}#metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.lnk{grid-template-columns:32px minmax(0,1fr)}.lnk .who,.lnk .what,.lnk .tool{grid-column:2;text-align:left}}@media(max-width:640px){main{padding:20px 16px 44px}.page-hero{padding:12px 0 22px}.page-hero h1{font-size:30px}.guide{grid-template-columns:1fr}.guide>div{min-height:auto}.toolbar{align-items:stretch}.toolbar button{width:100%}#metrics{grid-template-columns:1fr 1fr}.metric b{font-size:20px}.card{padding:18px}.data-grid{gap:14px}}
/* P0 评委可读性：字号上调 / 主线默认可见 / 按钮分组 / 数据源徽章 / 投屏模式 */
body{font-size:15px;line-height:1.6}
.summary-head{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:16px;flex-wrap:wrap}
.summary-head h2{margin:0}
.src-chip{display:inline-flex;align-items:center;gap:7px;max-width:100%;padding:6px 12px;border-radius:999px;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.24);color:#d8ece8;font-size:12.5px;font-weight:600;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.src-chip::before{content:"";flex:0 0 auto;width:7px;height:7px;border-radius:50%;background:#7dd3fc}
.src-chip.real{background:rgba(52,211,153,.16);border-color:rgba(52,211,153,.5);color:#e3fbf1}
.src-chip.real::before{background:#34d399}
.flood-toolbar{gap:12px;align-items:center}
.tool-grp{display:flex;align-items:center;gap:8px;padding-right:14px;border-right:1px solid #c8d6d3}
.flood-toolbar .tool-grp:last-of-type{border-right:0;padding-right:0}
.grp-lbl{font-size:12px;font-weight:700;color:var(--muted);letter-spacing:.04em}
.flood-toolbar button{min-height:36px;padding:7px 13px;font-size:14px}
.scn.on{background:var(--accent)!important;color:#fff!important;border-color:var(--accent)!important}
.lnk{opacity:.62;grid-template-columns:30px 128px minmax(0,1fr) minmax(0,252px);align-items:start}
.lnk .no,.lnk .who,.lnk .tool{font-size:12px}
.lnk .who{line-height:1.5}
.lnk .what b{font-size:15px}
.lnk .what div{font-size:13px;line-height:1.58}
.lnk .tool{line-height:1.45;text-align:right;word-break:keep-all;overflow-wrap:break-word}
.row{font-size:14px}
.tl .ttl{font-size:15px}
.tl .dsc{font-size:14px}
.tl .mt{font-size:12px}
.tl ul.basis{font-size:13px}
.ord{font-size:14px}
.ord .ds{font-size:13px}
.cvrow{font-size:14px}
.plan .kv{font-size:14px}
.guide>div{font-size:13.5px}
.scene-detail-grid strong{font-size:14px}
.scene-detail-grid small{font-size:12px}
.row.inv{color:var(--ink)}
.dp{color:var(--muted);font-size:12.5px}
.cvwho{color:var(--accent);font-size:14px}
.cvact{color:var(--ink);font-size:14px}
.ord .id{color:var(--muted)}
.sev.high{color:#a8321f;background:#fdecea}
.sev.medium{color:#8a5510;background:#fdf1de}
.afflist{display:flex;flex-wrap:wrap;gap:6px;margin-top:5px}
.afflist .pill{font-size:12px;padding:3px 9px}
.flood-wrap{display:grid;grid-template-columns:minmax(0,1fr) 292px;gap:18px;align-items:stretch}
#flood-scene{border-radius:12px}
#flood-map{min-height:280px}
.flood-side{display:grid;gap:12px;align-content:start;min-width:0}
.flood-story{grid-template-columns:1fr}
.flood-readout{display:grid;gap:10px;margin:0;align-content:start;font-size:14px}
.flood-readout .fs{min-width:0;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:#f8fbfa}
.flood-readout .fs small{display:block;margin-bottom:5px;font-size:12px;font-weight:600;letter-spacing:.04em;color:var(--muted)}
.flood-readout b{display:block;font-size:22px;line-height:1.2;color:var(--accent)}
.flood-readout .fs span{display:block;font-size:13.5px;line-height:1.5;color:var(--ink);overflow-wrap:anywhere}
.flood-caption{position:absolute;left:12px;top:12px;z-index:3;max-width:76%;padding:9px 13px;border-radius:9px;background:rgba(7,18,28,.62);pointer-events:none}
.flood-caption b{display:block;font-size:15px;color:#e0f2fe;line-height:1.35}
.flood-caption span{display:block;margin-top:3px;font-size:12.5px;line-height:1.5;color:#b9cbd9}
.flood-gap-value{font-size:28px!important;color:var(--accent)!important}
#quant-refs{display:grid;gap:8px;margin-top:2px}
.qc-row{display:grid;grid-template-columns:112px auto minmax(0,1fr);gap:12px;align-items:baseline;padding:9px 12px;background:#f6faf8;border:1px solid #e1eceb;border-radius:10px}
.qc-k{font-size:13px;font-weight:700;color:var(--ink)}
.qc-v{font-size:13px;font-weight:700;color:var(--accent);white-space:nowrap}
.qc-n{font-size:13px;color:var(--muted);line-height:1.5}
.proj-btn-on{background:var(--ink)!important;border-color:var(--ink)!important}
html.projection body{font-size:17px}
html.projection .card h2{font-size:22px}
html.projection .metric{font-size:14px}
html.projection .metric b{font-size:28px}
html.projection .guide span{font-size:18px}
html.projection .guide>div{font-size:14.5px}
html.projection .lnk .what b{font-size:17px}
html.projection .lnk .what div{font-size:15px}
html.projection .lnk{grid-template-columns:30px 152px minmax(0,1fr) minmax(0,268px)}
html.projection .lnk .who,html.projection .lnk .no,html.projection .lnk .tool{font-size:13px}
html.projection .row{font-size:16px}
html.projection .tl .ttl{font-size:17px}
html.projection .tl .dsc{font-size:15.5px}
html.projection .qc-k,html.projection .qc-v,html.projection .qc-n{font-size:14.5px}
html.projection .scene-detail-grid strong{font-size:15.5px}
@media(max-width:960px){.flood-toolbar{display:block}.tool-grp{border-right:0;padding:0 0 8px;flex-wrap:wrap;min-width:0}.qc-row{grid-template-columns:1fr;gap:3px}.src-chip{white-space:normal}.flood-wrap{grid-template-columns:1fr}.flood-readout{grid-template-columns:repeat(3,minmax(0,1fr))}.lnk{grid-template-columns:30px minmax(0,1fr);row-gap:3px}.lnk .who,.lnk .what,.lnk .tool{grid-column:2;text-align:left}}
@media(max-width:640px){.flood-readout{grid-template-columns:1fr;grid-auto-rows:auto}}
</style>'''
def build_html() -> str:
    return """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Urban Twin</title>
<style>body{font:15px system-ui;margin:0;background:#0b1220;color:#e8eef7}main{max-width:1180px;margin:auto;padding:28px}h1{color:#7dd3fc;margin-bottom:4px}.sub{color:#9fb1c8}.toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:22px 0}button{background:#38bdf8;border:0;border-radius:6px;padding:10px 18px;font-weight:700;cursor:pointer;white-space:nowrap;flex:0 0 auto}.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:16px}.card{background:#152238;border:1px solid #263a55;border-radius:10px;padding:16px}.card h2{font-size:17px;margin:0 0 12px;color:#bae6fd}.metric{display:inline-block;background:#20344d;border-radius:7px;padding:10px;margin:0 8px 8px 0}.metric b{display:block;font-size:22px;color:#7dd3fc}.pill{display:inline-block;background:#334155;padding:3px 8px;border-radius:99px;margin:3px;font-size:12px}.warn{color:#fbbf24}.ok{color:#86efac}.trace{border-left:2px solid #38bdf8;padding-left:12px;margin:8px 0}.evidence{font-size:12px;color:#b8c7d9;word-break:break-all}svg{width:100%;height:330px;background:#0e1a2c;border-radius:8px}.road{stroke:#64748b;stroke-width:8}.flood{fill:#38bdf8;opacity:.35}.zone{fill:#1d3252;fill-opacity:.25;stroke:#2f4a6b;stroke-width:1.5;stroke-dasharray:6 6}.node{fill:#fbbf24;stroke:#fff;stroke-width:2}.navlink{color:#7dd3fc;text-decoration:none;margin-left:auto;font-weight:600}.guide{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:20px 0 0;border-top:1px solid #263a55}.guide>div{padding:14px 20px 0;border-left:1px solid #263a55;font-size:13px;color:#b8c7d9;line-height:1.65}.guide>div:first-child{border-left:0;padding-left:0}.guide b{display:block;font:700 12px ui-monospace,monospace;color:#38bdf8;letter-spacing:.1em}.guide span{display:block;font-size:16px;font-weight:700;color:#e8eef7;margin:2px 0 5px}.lgd{display:flex;gap:16px;flex-wrap:wrap;margin:8px 0 0;font-size:12px;color:#8fa4bd}.lgd i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:5px;vertical-align:-1px}.cap{font-size:12px;color:#8fa4bd;margin:10px 0 4px}.cvwho{margin:12px 0 4px;color:#7dd3fc;font-weight:700;font-size:14px}.cvwho .tag{margin-left:8px;font-weight:400}.cvrow{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:8px;align-items:start;padding:5px 0;border-bottom:1px dashed #1e3250;font-size:13px}.cvact{color:#cfe3f7;line-height:1.5;overflow-wrap:anywhere}.dp{font-size:11px;color:#7f93ab;line-height:1.5;margin:-2px 0 7px}.row.inv{color:#e8eef7}.tech{margin-top:18px;border:1px solid #263a55;border-radius:10px;background:#111d30}.tech>summary{cursor:pointer;padding:13px 16px;color:#8fa4bd;font-size:13px;font-weight:600;list-style:none}.tech>summary::-webkit-details-marker{display:none}.tech>summary::before{content:"▸ ";color:#38bdf8}.tech[open]>summary::before{content:"▾ "}.tech>div{padding:0 16px 16px}@media(max-width:800px){.grid{grid-template-columns:1fr}.guide{grid-template-columns:1fr}.guide>div{border-left:0;padding:9px 0;font-size:12px}.guide b{display:inline;margin-right:6px}.guide span{display:inline;font-size:14px;margin:0 6px 0 0}}</style>
<style>#flood-scene{position:relative;border:1px solid #263a55;border-radius:12px;background:#0d1a2a;overflow:hidden;min-height:300px}#flood-map{display:block;width:100%;height:100%;min-height:300px;background:#0d1a2a}#flood-rain{position:absolute;inset:0;z-index:2;pointer-events:none;opacity:0;transition:opacity .5s;background-image:repeating-linear-gradient(115deg,rgba(125,211,252,.32) 0 1px,transparent 1px 30px);animation:fmRain .45s linear infinite}#flood-rain.on{opacity:1}@keyframes fmRain{to{background-position:30px 52px}}.fm-zone{fill:rgba(47,74,107,.20);stroke:#3f5a75;stroke-dasharray:6 5;stroke-width:1.5}.fm-road{stroke:#54708a;stroke-width:5;stroke-linecap:round;fill:none;transition:stroke .3s}.fm-road-closed{stroke:#8a6534}.fm-node{stroke:#f4f8fb;stroke-width:1.5;transition:fill .2s}.fm-node-in{fill:#d9a53a}.fm-node-ps{fill:#38bdf8}.fm-water{fill:#38bdf8;fill-opacity:.32;stroke:#7dd3fc;stroke-width:1.2;transition:fill-opacity .5s}.fm-lbl{fill:#c3d3e2;font-size:10.5px;pointer-events:none;paint-order:stroke;stroke:#0d1a2a;stroke-width:3px;stroke-linejoin:round}.fm-evlbl{fill:#e0f2fe;font-size:11.5px;font-weight:700;pointer-events:none;paint-order:stroke;stroke:#0d1a2a;stroke-width:3.5px;stroke-linejoin:round}.fx-event{animation:fmPulse 1.1s ease-in-out infinite}.fx-affected{stroke:#7dd3fc;stroke-width:2.5;filter:drop-shadow(0 0 5px rgba(125,211,252,.9))}.fx-owner{stroke:#34d399;stroke-width:3;filter:drop-shadow(0 0 4px rgba(52,211,153,.8))}.fx-conflict{stroke:#fbbf24;stroke-width:3;animation:fmPulse .85s ease-in-out infinite}.fx-fault{fill:#ef4444;stroke:#fecaca;animation:fmFault .65s steps(2,end) infinite}.fx-closed{stroke:#fb923c;stroke-width:7;stroke-dasharray:14 8;animation:fmPulse 1.2s ease-in-out infinite}@keyframes fmPulse{50%{opacity:.4}}@keyframes fmFault{50%{opacity:.25}}.flood-caption{position:absolute;left:12px;top:12px;z-index:3;max-width:76%;padding:9px 13px;border-radius:9px;background:rgba(7,18,28,.68);pointer-events:none}.flood-caption b{display:block;font-size:15px;color:#e0f2fe;line-height:1.35}.flood-caption span{display:block;margin-top:3px;font-size:12.5px;line-height:1.5;color:#b9cbd9}.flood-readout{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px;font-size:13px;color:#b8c7d9}.flood-readout b{color:#7dd3fc;font-size:18px}.flood-story{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:10px 0 0}.flood-cause{padding:9px 10px;border:1px solid #29425d;border-radius:8px;background:#101d30;color:#b8c7d9;font-size:12px;line-height:1.45}.flood-cause b{display:block;color:#bae6fd;font-size:13px}.flood-cause.on{border-color:#38bdf8;background:#14334a;box-shadow:inset 3px 0 #38bdf8}@media(max-width:700px){.flood-story{grid-template-columns:1fr}}</style><main><header class=page-hero><div><div class=eyebrow>URBAN TWIN · 成都内涝推演</div><h1>城市内涝跨部门事件推演</h1><div class=sub>从一张积水工单，到一套可执行的跨部门调度方案。</div></div><a class=navlink href='/evolution'>查看决策进化对比 →</a><a class=navlink href='/feedback'>提交内涝反馈 →</a><a class=navlink href='/review'>参与方案评审 →</a><a class=navlink href='/status'>系统自检 →</a><a class=navlink href='/workbench'>证据工作台 →</a></header>
<section class=guide><div><b>01</b><span>先看结论</span>出什么事、要几台泵、实际有几台、怎么分 —— 见“事件概览 / 工单 / 资源 / 方案”</div><div><b>02</b><span>再看依据</span>这结论是谁算的、引用了台账哪一行 —— 见“演示链路 / 决策事件流”</div><div><b>03</b><span>最后改条件</span>少一台泵或封一条路，看结论怎么重算 —— 见“人工干预”</div></section>
<div class=toolbar><button onclick="run()">运行演示</button><button class=ghost id=btn-proj onclick="toggleProjection()">投屏模式</button><span id=status>等待运行</span></div><section id=summary class='card outcome-card'><div class=summary-head><h2>事件结论</h2><span id=data-source class=src-chip>合成情景 · 合成气象</span></div><div id=metrics>点击“运行演示”加载推演结果</div></section>
<section class='card process-card'><h2>推演主线 <span>事件输入 → 证据核验 → 协同复核 → 方案复算</span></h2><div class=toolbar style='margin:0 0 8px'><button onclick="runChain()">跑一遍链路</button><span id=chainstatus class=sub>未加载</span></div><div id=chain class=chain></div><div id=chainfoot class=sub style='margin-top:8px'></div></section>
<section class='card' id='dynamic-scene-card'><h2>动态内涝推演 <span>画面为示意动画，节奏与文案由事件流 stage 事件驱动</span></h2><div class="toolbar flood-toolbar" style='margin:0 0 10px'><div class=tool-grp><button onclick="playFloodScenario()">播放推演</button><button class=ghost onclick="resetFloodScenario()">重置</button></div><div class=tool-grp><b class=grp-lbl>情景</b><button class='ghost scn on' id=btn-scn-syn onclick="switchScenario('')">合成（默认）</button><button class='ghost scn' id=btn-scn-real onclick="switchScenario('real_20260801')">真实暴雨 08-01</button></div><div class=tool-grp><button class=ghost onclick="playRealRain()">真实降雨回放</button></div><span id=flood-status class=sub>待播放</span></div><div class=flood-wrap><div id=flood-scene><div class=flood-caption><b id=flood-cap-title>点「播放推演」开始</b><span id=flood-cap-detail>六跳推演逐步驱动水位与设施状态</span></div><div id=flood-rain></div><svg id=flood-map viewBox='0 0 700 330' aria-label='内涝事件场景图：实体与坐标来自 scene.json 台账投影'></svg></div><div class=flood-side><div class=flood-story aria-label='推演因果链'><div class='flood-cause' id=flood-cause-input><b>① 输入</b><span>雨量进入排水系统</span></div><div class='flood-cause' id=flood-cause-impact><b>② 影响</b><span>积水扩散、设施受压</span></div><div class='flood-cause' id=flood-cause-action><b>③ 处置</b><span>封路、调泵、部门复核</span></div></div><div class=flood-readout><div class=fs><small>积水深度</small><b id=flood-depth>0 cm</b></div><div class=fs><small>当前阶段</small><span id=flood-phase>降雨尚未开始</span></div><div class=fs><small>协同状态</small><span id=flood-dept>协同：等待事件</span></div><div class=fs><small>移动泵缺口</small><b class=flood-gap-value id=flood-gap>—</b><span id=flood-gap-note>运行演示后由缺口核验给出</span></div></div></div></div></section><section class='card' id='quant-card'><h2>定量方法交叉参照 <span>与 EPA SWMM 同口径对拍：简化假设的偏差方向与幅度都已量化</span></h2><div id=quant-refs class=sub>加载中…</div></section><div class='grid primary-grid'><section class='card impact-card'><div class=impact-kicker>02 · 影响与处置</div><h2>受影响对象 <span class='impact-hint'>清单与上方推演图同源 · 点对象看台账关联</span></h2><div id=scene-summary class=scene-summary>正在读取影响范围…</div><div class=impact-flow aria-label='影响链路'><span>事件</span><b>→</b><span>受影响对象</span><b>→</b><span>处置动作</span></div><div class=twin-reading-guide style="display:grid;grid-template-columns:auto auto 1fr;gap:10px;align-items:baseline;padding:11px 13px;margin:-4px 0 12px;border:1px solid #d7e1df;border-radius:8px;background:#eef7f5"><b>数字孪生怎么读</b><span>对象 → 影响 → 动作</span><small>在下方清单点任一对象，看工单、资源与证据；空间位置看上方动态推演图。</small></div><div id=scenemap class=entlist aria-label='事件对象清单（与上方动态推演图同源，不再重复画第二张地图）'></div><div id=scene-detail class="scene-detail scene-action"><div class=scene-empty>点击上方清单里的对象，查看它受到的影响、关联工单和所需资源。</div></div><div id=assets></div></section><section class='card coordination-card'><h2>跨部门协同</h2><div id=departments></div><h2>资源与风险</h2><div id=risks></div></section></div>
<div class='grid data-grid'>
<section class=card><h2>工单列表 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（台账全量，演示事件置顶）</span></h2><div id=orders class=sub>加载中</div></section>
<section class=card><h2>资源状态 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（台账可用量 vs 本次缺口）</span></h2><div id=resboard class=sub>加载中</div></section>
</div>
<section class=card style='margin-top:16px'><h2>当前方案 vs 备选方案 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（同一份决策输入，只换策略参数）</span></h2><div id=plans class=sub>加载中</div><div id=planfoot class=sub style='margin-top:10px'></div></section>
<section class=card style='margin-top:16px'><h2>人工协同 vs 智能体辅助 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（只比算得出来的项；人工耗时未实测就留空，不填估算值）</span></h2><div id=baseline class=sub>加载中</div><div id=basefoot class=sub style='margin-top:10px'></div></section>
<section class=card style='margin-top:16px'><h2>推理链路与证据</h2><div id=trace></div></section>
<section class=card style='margin-top:16px'><h2>微调度台 · 决策事件流 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（六类事件同源派生，每条都能对回推演字段）</span></h2><div class=toolbar style='margin:0 0 10px'><button onclick="playTimeline()">回放事件流</button><span id=tlstatus class=sub>未加载</span></div><div id=cats class=cats></div><ol id=timeline class=tl></ol><div id=tlfoot class=sub style='margin-top:10px'></div></section>
<section class=card style='margin-top:16px'><h2>人工干预 · 重新推演 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（改一个条件，系统重算并说明改了什么）</span></h2><div id=ivbtns class=toolbar style='margin:0 0 10px'></div><div id=ivout class=sub>点上面任一项，观察推演结果怎么变</div></section>
<section class=card style='margin-top:16px'><h2>协同复核 · 谁接哪张卡 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（1 总控 + 5 部门，只读复核、不改推演数字）</span></h2><div id=cvsum class=sub>未加载</div><div id=cvcards style='margin-top:10px'></div><div id=cvhand class=sub style='margin-top:10px'></div></section>
<section class=card id=exec-section style='margin-top:16px'><h2>执行账本 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（计划 vs 实际 · 演示执行账本：无真实身份系统，actor 由服务端固定）</span></h2><div id=exec-summary class=sub>加载中</div><div id=exec-cards style='margin-top:10px'></div><div id=exec-close></div><div id=exec-msg class=exmsg></div><details style='margin-top:10px'><summary class=sub style='cursor:pointer'>最近事件（账本原始记录，只追加不改写）</summary><div id=exec-events class=sub style='margin-top:8px'></div></details></section>
<style>.ghost{background:#1e3a55;color:#bae6fd}#log{max-height:360px;overflow:auto;font:12px ui-monospace,monospace}.logrow{padding:6px 8px;border-bottom:1px solid #1e3250;white-space:nowrap}.logrow .mcp{color:#86efac}.logrow .manual{color:#fbbf24}#panel{font:12px ui-monospace,monospace;white-space:pre-wrap;color:#cfe3f7}#comparebody{font:12px ui-monospace,monospace;white-space:pre-wrap;color:#cfe3f7}.tl{list-style:none;padding:0;margin:0}.tl li{border-left:2px solid #263a55;padding:8px 0 8px 14px;opacity:.2;transition:opacity .35s,border-color .35s}.tl li.on{opacity:1;border-left-color:#38bdf8}.tl .hd{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}.tl .seq{color:#64748b;font:12px ui-monospace,monospace}.tl .ttl{font-weight:700;color:#bae6fd}.tl .dsc{color:#cfe3f7;font-size:13px;margin-top:3px}.tl .mt{color:#7f93ab;font:11px ui-monospace,monospace;margin-top:5px;word-break:break-all}.tl ul.basis{margin:7px 0 0;padding-left:16px;color:#b8c7d9;font-size:12px}.tl ul.basis li{margin:2px 0;border:0;padding:0;opacity:1}.tag{font-size:11px;padding:1px 6px;border-radius:4px;background:#20344d;color:#9fb1c8}.k-conflict{color:#fbbf24}.k-gap{color:#fca5a5}.k-audit{color:#86efac}.ivbtn{background:#1e3a55;color:#bae6fd;border:0;border-radius:6px;padding:10px 14px;font-weight:700;cursor:pointer;text-align:left;max-width:330px;min-width:0;white-space:normal;overflow-wrap:anywhere;flex:1 1 280px}.ivbtn small{display:block;font-weight:400;color:#8fa4bd;font-size:11px;margin-top:4px;line-height:1.4}.row{display:flex;gap:12px;padding:5px 0;border-bottom:1px dashed #1e3250;font-size:13px}.row>span:first-child{flex:1 1 auto;min-width:0}.row b{margin-left:auto;color:#7dd3fc;flex:0 0 auto;padding-left:12px;white-space:nowrap}.chain{display:flex;flex-direction:column;gap:0}.lnk{display:grid;grid-template-columns:26px 140px minmax(0,1fr) minmax(0,200px);gap:12px;padding:9px 0 9px 14px;border-left:2px solid #263a55;opacity:.32;transition:opacity .3s,border-color .3s}.lnk.on{opacity:1;border-left-color:#38bdf8}.lnk .no{color:#64748b;font:12px ui-monospace,monospace}.lnk .who{font:11px ui-monospace,monospace;color:#8fa4bd;line-height:1.55}.lnk .what{min-width:0}.lnk .what b{color:#bae6fd}.lnk .what div{font-size:12px;color:#b8c7d9;margin-top:3px}.lnk .tool{font:11px ui-monospace,monospace;color:#86efac;text-align:right;word-break:break-all}.ord{display:grid;grid-template-columns:1fr auto;gap:4px 10px;padding:7px 0;border-bottom:1px dashed #1e3250;font-size:13px}.ord .id{font:11px ui-monospace,monospace;color:#8fa4bd}.ord .ds{grid-column:1/3;color:#b8c7d9;font-size:12px}.ord.ev{background:#14283d;border-left:2px solid #38bdf8;padding-left:8px}.ord .sev{padding:1px 6px;border-radius:4px;font-size:11px;background:#20344d}.sev.high{color:#fca5a5}.sev.medium{color:#fbbf24}.plan{display:grid;grid-template-columns:1fr 1fr;gap:14px}.plan .col{background:#101d30;border:1px solid #22344d;border-radius:8px;padding:12px}.plan .col h3{margin:0 0 8px;font-size:14px;color:#bae6fd}.plan .col.now{border-color:#2b6f8f}.plan .col.alt{border-color:#7c5a1f}.plan .kv{display:flex;font-size:13px;padding:3px 0}.plan .kv b{margin-left:auto;color:#7dd3fc}.ent{cursor:pointer}.ent:hover{filter:brightness(1.35)}.ent.selected{filter:brightness(1.45);stroke:#fff;stroke-width:3}.cats{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}.cat{background:#1e3a55;color:#bae6fd;border:0;border-radius:99px;padding:5px 11px;font-size:12px;cursor:pointer}.cat.sel{background:#38bdf8;color:#0b1220;font-weight:700}@media(max-width:800px){.grid{grid-template-columns:1fr}.plan{grid-template-columns:1fr}.lnk{grid-template-columns:26px minmax(0,1fr);row-gap:3px}.lnk .who{grid-column:2}.lnk .what{grid-column:2}.lnk .tool{grid-column:2;text-align:left}}</style>
<details class=tech><summary>技术附录 · 开发调试用，评委可跳过（渲染快照、工具原始日志、模型对照）</summary><div>
<div class=toolbar style='margin:0 0 12px'><button class=ghost onclick="renderScene('v1')">渲染方案一快照</button><button class=ghost onclick="renderScene('v4')">渲染方案二快照</button><button class=ghost onclick="diffScenes()">对比两版快照</button><button class=ghost onclick="runLLM()">大模型独立决策对照</button></div>
<div class=grid>
<section class=card><h2>当前渲染作业（UE 拉取的就是它）</h2><div id=panel>尚未渲染</div></section>
<section class=card><h2>工具调用日志（谁触发了渲染）</h2><div id=log><span class=warn>暂无调用记录</span></div></section>
</div>
<section id=llmcard class=card style='margin-top:16px;display:none'><h2>LLM 决策对照（DeepSeek，temperature=0）</h2><div id=llmbody style='font:13px ui-monospace,monospace;color:#cfe3f7'></div></section>
<section id=comparecard class=card style='margin-top:16px;display:none'><h2>策略对比</h2><div id=comparebody></div></section>
</div></details></main>
<script>const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function list(v){return (v||[]).map(x=>'<span class=pill>'+esc(typeof x==='string'?x:(x.name||x.id||JSON.stringify(x)))+'</span>').join('')||'<span class=warn>无</span>'}
function pct(v){const n=Number(v);return (v==null||isNaN(n))?'-':(Math.round(n*1000)/10)+'%'}
// 页面给评委看，凡是后端枚举值一律在这里翻成中文；后端字段不动，只做显示层映射。
const LV={'red':'红色预警 · Ⅰ级','orange':'橙色预警 · Ⅱ级','yellow':'黄色预警 · Ⅲ级','blue':'蓝色预警 · Ⅳ级'};
const SEV={'high':'高','medium':'中','low':'低'};
const STT={'open':'进行中','closed':'已结案'};
const POL={'v1':'方案一 · 各片区自理','v4':'方案二 · 允许跨区调拨'};
const ROLE={'urban_deduction_agent':'主 Agent · 分析','urban_collab_agent':'协同 Agent · 复核','值班室':'值班室 · 接单','推演引擎':'推演引擎 · 规则计算','证据审计':'证据审计 · 复算'};
const TOOLCN={'event_deduce':'事件推演','conflict_check':'冲突检查','evidence_locate':'证据定位','collab_run':'协同复核','allocation_plan':'调度方案'};
const CFT={'C1':'职责重叠','C2':'职责空白','C3':'资源争用'};
const EVST={'conflict':'有冲突','detected':'已识别','ok':'正常','publishable':'可发布','residual':'遗留风险','shortfall':'有缺口','supported':'有据'};
const CSTAGE={'observed':'观察','affected':'受影响对象','owner':'责任归属','conflict':'冲突','resource':'资源','action':'处置动作','audit':'复算','handoff':'交接'};
const CSTAT={'accepted':'已接受','pending':'待确认','rejected':'不采纳'};
function clipText(s,n){s=String(s||'');return s.length>n?s.slice(0,n)+'…':s}
function orgName(id){const d=((SCHED&&SCHED.departments)||[]).find(x=>x.org_id===id);return d?d.org_name:({['EMERG-COORD']:'应急总控'}[id]||id)}
function catLabel(id){const c=((STREAM&&STREAM.categories)||[]).find(x=>x.id===id);return c?c.label:id}
function zh(m,v){return (v==null||v==='')?'—':(m[v]||String(v))}
function tools(v){return String(v||'').split('、').filter(Boolean).map(t=>TOOLCN[t]||t).join('、')||'—'}
// 证据引用形如 AS-A-03-work_order-6a1282a1#A-03/work_order/v1#row:2，评委读不懂，翻成「A-03 工单台账 v1 第 2 行」。
const TBL={'construction_plan':'施工方案','drainage_facility':'排水设施台账','duty_list':'部门职责表','emergency_plan':'应急预案','pipeline_ledger':'管线台账','procedure':'处置规程','resource_inventory':'资源台账','road_network':'路网台账','underpass':'下穿隧道台账','work_order':'工单台账'};
const CLAIM={'CL-S1-observed':'发生了什么','CL-S2-affected':'影响了哪些对象','CL-S3-owner':'该谁负责','CL-S4-conflict':'暴露了哪些冲突','CL-S5-resource':'资源缺多少'};
function claimName(id){const s=String(id||'');if(CLAIM[s])return CLAIM[s];const m=s.match(/^CL-S6-action-(\\d+)$/);return m?'处置动作第 '+(+m[1])+' 条':s}
function refCn(x){const parts=String(x||'').split('#');if(parts.length<2)return x;
 const p=parts[1].split('/');const m=String(parts[2]||'').match(/(?:row|line):(\\d+)/);
 return (p[0]||'')+' '+(TBL[p[1]]||p[1])+(p[2]?' '+p[2]:'')+(m?' 第 '+m[1]+' 行':'')}
function drawFloodMap(){const scene=SCHED&&SCHED.scene;if(!scene||!scene.entities)return;const fm=document.getElementById('flood-map');if(!fm)return;const parts=[];(scene.entities||[]).filter(e=>e.kind==='district').forEach(e=>{const r=Math.min(e.r,96);parts.push('<circle class="fm-zone" id="fm-'+esc(e.id)+'" cx="'+e.cx+'" cy="'+e.cy+'" r="'+r+'"/>');parts.push('<text class="fm-lbl" x="'+e.cx+'" y="'+(e.cy-r-6)+'" text-anchor="middle">'+esc(e.label)+' 片区</text>')});(scene.entities||[]).filter(e=>e.kind==='road').forEach(e=>{parts.push('<polyline class="fm-road'+(/已封/.test(e.label||'')?' fm-road-closed':'')+'" id="fm-'+esc(e.id)+'" points="'+e.points+'" fill="none"/>')});(scene.entities||[]).filter(e=>e.kind==='facility').forEach(e=>{const ps=e.id.indexOf('DF-PS')===0;
  if(ps){parts.push('<rect class="fm-node fm-node-ps" id="fm-'+esc(e.id)+'" x="'+(Number(e.cx)-7)+'" y="'+(Number(e.cy)-7)+'" width="14" height="14" rx="3"/>')}
  else{parts.push('<circle class="fm-node fm-node-in" id="fm-'+esc(e.id)+'" cx="'+e.cx+'" cy="'+e.cy+'" r="6"/>')}
  parts.push('<text class="fm-lbl"'+(Number(e.cx)>640?' text-anchor="end" x="'+(Number(e.cx)-11)+'"':' x="'+(Number(e.cx)+11)+'"')+' y="'+(Number(e.cy)+4)+'">'+esc(e.id)+'</text>')});
 (scene.entities||[]).filter(e=>e.kind==='water').forEach(e=>{const pts=(e.points||'').split(' ').map(function(p){return p.split(',').map(Number)});
  const wx=Math.round(pts.reduce(function(a,p){return a+p[0]},0)/pts.length),wy=Math.round(pts.reduce(function(a,p){return a+p[1]},0)/pts.length);
  parts.push('<polygon class="fm-water" id="fm-'+esc(e.id)+'" points="'+e.points+'"/>');
  parts.push('<text class="fm-evlbl" x="'+wx+'" y="'+(wy+26)+'" text-anchor="middle">'+esc(e.label)+'</text>')});
 [['片区',12,'<circle r="5" fill="none" stroke="#3f5a75" stroke-dasharray="3 2"/>'],['道路',76,'<line x1="-7" y1="0" x2="7" y2="0" stroke="#54708a" stroke-width="4" stroke-linecap="round"/>'],['已封路',136,'<line x1="-7" y1="0" x2="7" y2="0" stroke="#a5793a" stroke-width="4" stroke-linecap="round"/>'],['积水面',210,'<rect x="-5" y="-5" width="10" height="10" fill="#38bdf8" fill-opacity=".5"/>'],['监测井',278,'<circle r="5" fill="#d9a53a"/>'],['泵站',344,'<rect x="-5" y="-5" width="10" height="10" rx="2" fill="#38bdf8"/>']].forEach(function(it){parts.push('<g transform="translate('+it[1]+',314)" pointer-events="none">'+it[2]+'<text x="9" y="3.5" font-size="10" fill="#8fa4bd">'+it[0]+'</text></g>')});
 fm.innerHTML=parts.join('')}
function fmSet(id,cls){const el=document.getElementById('fm-'+id);if(el)el.classList.add(cls)}
function fmClearFx(){['fx-event','fx-affected','fx-owner','fx-conflict','fx-fault','fx-closed'].forEach(function(c){document.querySelectorAll('.'+c).forEach(function(el){el.classList.remove(c)})})}
function rainLevel(v){const r=document.getElementById('flood-rain');if(!r)return;if(v>0){r.classList.add('on');r.style.opacity=String(0.3+0.55*v)}else{r.classList.remove('on');r.style.opacity='0'}}
function hitIds(detail){const d=String(detail||'');return(((SCHED&&SCHED.scene&&SCHED.scene.entities)||[]).filter(function(e){return d.indexOf(e.id)>=0}).map(function(e){return e.id}))}
let floodTimer=null,floodStep=0; function resetFloodScenario(){clearInterval(floodTimer);floodStep=0;['input','impact','action'].forEach(function(k){const el=document.getElementById('flood-cause-'+k);if(el)el.classList.remove('on')});rainLevel(0);fmClearFx();document.querySelectorAll('.fm-water').forEach(function(el){el.style.fillOpacity=''});document.getElementById('flood-depth').textContent='0 cm';document.getElementById('flood-phase').textContent='降雨尚未开始';document.getElementById('flood-dept').textContent='协同：等待事件';document.getElementById('flood-status').textContent='待播放';const ct=document.getElementById('flood-cap-title'),cd=document.getElementById('flood-cap-detail');if(ct)ct.textContent='点「播放推演」开始';if(cd)cd.textContent='六跳推演逐步驱动水位与设施状态'} async function playFloodScenario(){clearInterval(floodTimer);floodStep=0;const live=await Promise.all([fetch('/api/event-stream').then(r=>r.json()).catch(()=>null),fetch('/api/collab-review').then(r=>r.json()).catch(()=>null),fetch('/api/demo').then(r=>r.json()).catch(()=>null)]);const stream=live[0]||{},collab=live[1]||{},demo=live[2]||{};const statusEl=document.getElementById('flood-status');const stages=(stream.events||[]).filter(e=>e.kind==='stage');if(!stages.length){statusEl.textContent='事件流无 stage 事件，无法推演';return}if(!SCHED)await loadScheduler();drawFloodMap();const wEnt=((SCHED.scene||{}).entities||[]).find(x=>x.kind==='water'&&/A-03/.test(x.label||''));let maxDepth=NaN;const md=String((stages[0]||{}).detail||'').match(/积水\\s*([0-9.]+)\\s*cm/);if(md)maxDepth=parseFloat(md[1]);if(!isFinite(maxDepth))maxDepth=Number((demo.facts||{}).depth_cm)||0;if(!maxDepth)maxDepth=30;const phaseEl=document.getElementById('flood-phase'),deptEl=document.getElementById('flood-dept'),depthEl=document.getElementById('flood-depth');const visual={observed:function(){if(wEnt)fmSet(wEnt.id,'fx-event');fmSet('A-03','fx-affected');rainLevel(.7)},affected:function(e){((demo.impact||{}).affected||[]).forEach(function(o){fmSet(o.name,'fx-affected')});rainLevel(1)},owner:function(e){hitIds(e.detail).forEach(function(id){fmSet(id,'fx-owner')})},conflict:function(e){hitIds(e.detail).forEach(function(id){fmSet(id,'fx-conflict')});if(wEnt)fmSet(wEnt.id,'fx-conflict')},resource:function(e){hitIds(e.detail).forEach(function(id){fmSet(id,'fx-fault')})},action:function(e){((SCHED.scene||{}).entities||[]).filter(function(x){return x.kind==='road'&&/已封/.test(x.label||'')}).forEach(function(x){fmSet(x.id,'fx-closed')});rainLevel(.45)}};const total=stages.length;floodTimer=setInterval(function(){floodStep++;if(floodStep>total){clearInterval(floodTimer);statusEl.textContent='推演完成 · 事件流 '+((stream.counts||{}).events||'-')+' 条 · '+total+' 拍 stage 驱动';const dfc=(demo.deduction||{}).conflicts||[],dgp=((demo.deduction||{}).resource_gaps||[])[0]||{};document.getElementById('flood-cap-title').textContent='推演完成 · 结论可发布';document.getElementById('flood-cap-detail').textContent='三类冲突 '+dfc.length+' 类 · 移动泵缺口 '+(dgp.gap??'-')+' 台 · 动作卡 '+((collab.cards||[]).length)+' 张 → 部门复核';return}const e=stages[floodStep-1];const wEl=wEnt?document.getElementById('fm-'+wEnt.id):null;if(wEl)wEl.style.fillOpacity=String(0.18+0.5*floodStep/total);depthEl.textContent=Math.round(maxDepth*floodStep/total)+' cm';depthEl.title='终值取事件实测 '+maxDepth+' cm';phaseEl.textContent=e.title||e.phase;deptEl.textContent=String(e.detail||'').replace(/ORG-[A-Z0-9_-]+/g,function(m){return orgName(m)||m})+((collab.cards||[]).length&&e.phase==='action'?' · 动作卡 '+collab.cards.length+' 张':'');document.getElementById('flood-cap-title').textContent=floodStep+'/'+total+' · '+(e.title||e.phase||'');document.getElementById('flood-cap-detail').textContent=deptEl.textContent;const _fx=visual[e.phase];if(_fx)_fx(e);['input','impact','action'].forEach(function(k){const el=document.getElementById('flood-cause-'+k);if(el)el.classList.toggle('on',(k==='input'&&floodStep>=1)||(k==='impact'&&floodStep>=2)||(k==='action'&&floodStep>=total))});statusEl.textContent='推演 '+Math.round(floodStep/total*100)+'% · 第 '+floodStep+'/'+total+' 拍（stage 事件驱动）'},950)} let currentScenario=''; function switchScenario(s){currentScenario=s;const a=document.getElementById('btn-scn-syn'),b=document.getElementById('btn-scn-real');if(a)a.classList.toggle('on',!s);if(b)b.classList.toggle('on',!!s);run(true)} function toggleProjection(){const on=document.documentElement.classList.toggle('projection');const btn=document.getElementById('btn-proj');if(btn){btn.textContent=on?'退出投屏':'投屏模式';btn.classList.toggle('proj-btn-on',on)}try{localStorage.setItem('urban-proj',on?'1':'0')}catch(e){}} async function playRealRain(){clearInterval(floodTimer);resetFloodScenario();const statusEl=document.getElementById('flood-status');statusEl.textContent='读取真实降雨…';const r=await fetch('/api/weather-replay').then(x=>x.json()).catch(()=>null);if(!r||r.error){statusEl.textContent='真实降雨数据不可用';return}if(!SCHED)await loadScheduler();drawFloodMap();const wA=((SCHED.scene||{}).entities||[]).find(x=>x.kind==='water'&&/A-03/.test(x.label||''));const wAEl=wA?document.getElementById('fm-'+wA.id):null;const depthEl=document.getElementById('flood-depth'),phaseEl=document.getElementById('flood-phase'),deptEl=document.getElementById('flood-dept');const pts=r.points||[];if(!pts.length){statusEl.textContent='无 2026-08-01 数据';return}let i=0;floodTimer=setInterval(function(){if(i>=pts.length){clearInterval(floodTimer);statusEl.textContent='回放完成 · '+r.date+' 自然日累计 '+r.total_mm+' mm（峰值 '+r.peak_mm+' mm/h @ '+r.peak_time+'）· 77.7mm 为最大24h滑动窗口口径';return}const p=pts[i++];if(wAEl)wAEl.style.fillOpacity=String(0.15+0.55*(p.cum_frac||0));rainLevel(Math.min(1,(p.rain_mm||0)/(r.peak_mm||1)));depthEl.textContent='累计 '+p.cum_mm+' mm（非积水）';phaseEl.textContent=p.time+' · 时段降雨 '+p.rain_mm+' mm/h';deptEl.textContent='真实降雨回放 · '+r.source;statusEl.textContent='真实降雨回放 '+Math.round(i/pts.length*100)+'%（非推演输入）';const ct2=document.getElementById('flood-cap-title'),cd2=document.getElementById('flood-cap-detail');if(ct2)ct2.textContent='真实降雨回放 · '+r.date;if(cd2)cd2.textContent=p.time+' · 时段 '+p.rain_mm+' mm/h · 累计 '+p.cum_mm+' mm（非推演输入）'},420)} async function loadQuantRefs(){const el=document.getElementById('quant-refs');if(!el||el.dataset.loaded)return;const q=await fetch('/api/quant-references').then(x=>x.json()).catch(()=>null);if(!q||q.error){el.textContent='定量参照数据不可用';return}el.dataset.loaded='1';const rb=q.runoff||{},tk=q.trunk||{},fb=q.forebay||{},tw=fb.time_to_warning_h||{no_pump:{},pump_avail:{}};const qrow=(k,v,n)=>'<div class=qc-row><span class=qc-k>'+k+'</span><span class=qc-v>'+v+'</span><span class=qc-n>'+n+'</span></div>';el.innerHTML=qrow('干管是否约束',esc(tk.verdict||'-'),'Manning 满流 '+esc(tk.ours_capacity_m3h)+' m³/h vs SWMM 实测洪峰 '+esc(tk.swmm_peak_m3h)+' m³/h')+qrow('产流洪峰',esc(rb.ratio)+' 倍（SWMM / 我方）','推理公式 '+esc(rb.ours_m3s)+' m³/s vs SWMM '+esc(rb.swmm_m3s)+' m³/s；Horton 入渗衰减 vs 定值 ψ，属方法假设差异而非对拍误差')+qrow('前池到警 '+esc(fb.warning_stage_m)+' m','我方偏早（保守侧）','泵全停 '+esc(tw.no_pump.ours)+' h vs SWMM '+esc(tw.no_pump.swmm)+' h；泵可用 '+esc(tw.pump_avail.ours)+' h vs SWMM '+esc(tw.pump_avail.swmm)+' h；我方结果可作上界')} async function run(isSwitch){status.textContent=isSwitch?'切换情景推演中…':'推演中…';const r=await fetch('/api/demo'+(currentScenario?'?scenario='+encodeURIComponent(currentScenario):''));const p=await r.json();const f=p.facts||{},d=p.deduction||{};metrics.innerHTML='<span class=metric><b>'+esc(zh(LV,f.response_level))+'</b>预警等级</span><span class=metric><b>'+esc(f.depth_cm||'-')+' cm</b>积水深度</span><span class=metric><b>'+esc(String(p.knowledge_version||'-').replace(/^kb-/,'预案 '))+'</b>所用预案版本</span><span class=metric><b class=ok>'+esc(pct(p.audit?.citation_completeness))+'</b>引用完整率</span>';const scn=p.scenario||{};const dsEl=document.getElementById('data-source');if(dsEl){const real=scn.weather_provenance==='real';dsEl.textContent=real?'真实气象 · ERA5':'合成气象 · 冻结口径';dsEl.title=(scn.label||'')+' ｜ '+(scn.weather_source||'');dsEl.className='src-chip'+(real?' real':'');}const aff=(p.impact||{}).affected||[];const KIND_CN={drainage:'排水',gas:'燃气',incident:'事件',power:'供电',pump_station:'泵站',road:'道路',work_site:'工地'};assets.innerHTML='<div class=cap>对象台账（推演关联 '+aff.length+' 个；地图只画需点击的）</div><div class=afflist>'+aff.map(o=>'<span class=pill title="'+esc(o.twin_id||'')+'">'+esc(KIND_CN[o.kind]||o.kind)+' · '+esc(o.name||'')+'</span>').join('')+'</div>';renderDepartments();risks.innerHTML='<div>'+list((d.conflicts||[]).map(c=>String(c.conflict_id||'')+' '+(CFT[c.conflict_id]||'')+'（'+(c.subject||'')+'）'))+'</div><div class=cap>资源缺口（要多少 vs 有多少）</div><div>'+list((d.resource_gaps||[]).map(g=>String(g.resource_id||'')+' 缺 '+g.gap+' '+String(g.unit||'')))+'</div><div class=cap>需要人工盯住的风险</div><div class=warn>'+list(d.residual_risks||[])+'</div>';const acts=(p.evidence||[]).filter(c=>/^CL-S6-action/.test(String(c.claim_id||'')));trace.innerHTML=((p.evidence||[]).filter(c=>!/^CL-S6-action/.test(String(c.claim_id||''))).map(c=>{const rs=c.evidence_refs||[];return '<div class=trace><b>'+esc(c.text||claimName(c.claim_id))+'</b><div class=evidence>依据：'+esc(rs.slice(0,4).map(refCn).join(' · '))+(rs.length>4?' 等 '+rs.length+' 条':'')+'</div></div>'}).join('')||'<span class=warn>暂无可展示证据</span>')+(acts.length?'<div class=sub style="font-size:12px;margin-top:8px">另有 '+acts.length+' 条处置动作，逐条见下方「决策事件流」</div>':'');status.textContent=p.publishable?'可发布结论':'需要人工复核';loadLog();resetFloodScenario();loadQuantRefs();const gp=(d.resource_gaps||[])[0]||{};const gEl=document.getElementById('flood-gap');if(gEl){gEl.textContent=(gp.gap??'-')+' '+(gp.unit||'');const gn=document.getElementById('flood-gap-note');if(gn)gn.textContent='需 '+(gp.required??'-')+' · 可用 '+(gp.available??'-')+' · 核验规则 '+(gp.rule_ids||[]).length+' 条'}}
let lastV1=null,lastV4=null;
async function renderScene(v){status.textContent='渲染 '+v+' …';
 const r=await fetch('/api/render?policy='+v);const j=await r.json();
 if(j.status==='error'){status.textContent='失败: '+j.error;return}
 if(v==='v1'){lastV1=j.snapshot_id}else{lastV4=j.snapshot_id}
 panel.textContent=j.panel.text;status.textContent='已渲染 '+v+'（'+j.scenario+'，snapshot '+j.snapshot_id+'）';loadLog()}
async function diffScenes(){if(!lastV1||!lastV4){status.textContent='先各渲染一次 v1 和 v4';return}
 const r=await fetch('/api/compare?baseline='+lastV1+'&candidate='+lastV4);const c=await r.json();
 if(c.status==='error'){status.textContent='对比失败: '+c.error;return}
 comparecard.style.display='';
 const NL=String.fromCharCode(10);
 const rows=(c.allocation.per_district||[]).map(x=>'  '+x.district+': '+x.baseline+' → '+x.candidate+' 台（'+(x.delta>0?'+':'')+x.delta+'）').join(NL);
 comparebody.textContent=c.verdict+NL+NL+'分配差异：'+NL+rows+NL+NL+'高亮新增: '+((c.scene.highlights_added||[]).join(', ')||'无');loadLog()}
async function loadLog(){const r=await fetch('/api/tool-log?limit=30');const d=await r.json();
 log.innerHTML=(d.entries||[]).slice().reverse().map(e=>'<div class=logrow><span class="'+esc(e.origin||'mcp')+'">['+esc(e.origin||'mcp')+']</span> '+esc(e.tool)+' <span class=ok>'+esc(e.status)+'</span>'+(e.policy_version?' · '+esc(e.policy_version):'')+(e.snapshot_id?' · '+esc(String(e.snapshot_id).slice(0,14)):'')+'</div>').join('')||'<span class=warn>暂无调用记录</span>'}
async function runLLM(){status.textContent='LLM 决策中…（约 4-8 秒）';
 const r=await fetch('/api/llm-decision');const d=await r.json();
 llmcard.style.display='';
 if(!d.configured){llmbody.innerHTML='<span class=warn>未配置真实模型：'+esc(d.reason)+'</span><br>'+esc(d.hint);status.textContent='LLM 未配置';return}
 if(d.status==='error'){llmbody.innerHTML='<span class=warn>调用失败: '+esc(d.error)+'</span>';status.textContent='LLM 调用失败';return}
 const NL=String.fromCharCode(10);
 const mark=d.selected_match?'<span class=ok>与确定性参照一致</span>':'<span class=warn>与确定性参照不一致（参照 '+esc(d.expected_option_id)+'）</span>';
 llmbody.innerHTML='<b>'+esc(d.selected_option_id)+'</b> '+esc(d.selected_description)+'<br>'+mark+' · 置信度 '+esc(d.confidence)+' · 风险 '+esc(d.risk_level)+' · 时延 '+esc(d.latency_ms)+'ms · 模型 '+esc(d.model)+'（t='+esc(d.temperature)+'）<br><b>摘要：</b>'+esc(d.summary)+'<br><b>风险：</b>'+esc(d.risks.join('；'))+'<br><b>引用证据：</b>'+esc(d.evidence_cited)+' 条';
 status.textContent='LLM 决策完成';loadLog()}
async function loadPlatform(){try{const r=await fetch('/api/platform-evidence');const d=await r.json();
 if(!d.available){return}
 const p=d.record;const NL=String.fromCharCode(10);
 const agree=p.selected_match?'<span class=ok>与参照一致</span>':'<span class=warn>与参照不一致（真机温度未钉 0 会漂移，印证推演定优先级）</span>';
 llmcard.style.display='';
 llmbody.innerHTML='<b>Nexent 平台真机验证</b>（Agent '+esc(p.agent_name)+' · '+esc(p.case_id)+' · schema '+(p.schema_valid?'<span class=ok>合法</span>':'<span class=warn>失败</span>')+'）<br>选中 <b>'+esc(p.selected_option_id||'-')+'</b>（参照 '+esc(p.expected_option_id)+'）· '+agree+' · 时延 '+esc(p.latency_ms)+'ms<br><span class=evidence>'+esc(p.method||'')+'</span>';}catch(e){}}
let STREAM=null,STREAMCAT='all';
function streamEvents(){if(!STREAM)return[];return (STREAM.events||[]).filter(e=>STREAMCAT==='all'||e.category===STREAMCAT)}
function renderCats(){if(!STREAM)return;cats.innerHTML='<button class="cat'+(STREAMCAT==='all'?' sel':'')+'" onclick="filterCat(\\'all\\')">全部 '+STREAM.counts.events+'</button>'+(STREAM.categories||[]).map(c=>'<button class="cat'+(STREAMCAT===c.id?' sel':'')+'" onclick="filterCat(\\''+c.id+'\\')">'+esc(c.label)+' '+c.count+'</button>').join('')}
function renderStream(){timeline.innerHTML='';const evs=streamEvents();evs.forEach(e=>{const li=document.createElement('li');li.classList.add('on');
  const basis=(e.basis&&e.basis.length)?'<ul class=basis>'+e.basis.map(b=>'<li>'+esc(b)+'</li>').join('')+'</ul>':'';
  li.innerHTML='<div class=hd><span class=seq>'+String(e.seq).padStart(2,'0')+'</span><span class="ttl k-'+esc(e.kind)+'">'+esc(e.title)+'</span><span class=tag>'+esc(catLabel(e.category))+'</span><span class=tag>'+esc(zh(EVST,e.status))+'</span></div><div class=dsc>'+esc(e.detail)+'</div><div class=mt>取自推演输出 '+esc(e.source)+' · 证据 '+esc(e.ref_count)+' 条'+jumpLink(e)+'</div>'+basis;
  timeline.appendChild(li)})}
// 六跳事件直接给出沙盘深链接：跳次号由 /api/twin-links 的 jumps 给出，
// 前端不自己按下标记数——事件流里还混着冲突明细，数错一位就跳到别的跳。
function jumpLink(e){
  if(!TLINKS||e.category!=='timeline'||e.kind!=='stage'||!e.phase)return '';
  const j=(TLINKS.jumps||[]).find(x=>x.phase===e.phase);
  if(!j)return '';
  const ent=(j.entity_ids||[])[0]||'';
  const href='/twin3d?jump='+j.jump+(ent?'&entity='+encodeURIComponent(ent):'');
  return ' · <a class=deep href="'+esc(href)+'" target=_blank rel=noopener>在沙盘查看 →</a>';
}
function entityJump(id){
  if(!TLINKS)return 1;
  const hits=(TLINKS.cards||[]).filter(c=>(c.entity_ids||[]).indexOf(id)>=0).map(c=>c.jump||1);
  return hits.length?Math.min.apply(null,hits):1;
}
function filterCat(c){STREAMCAT=c;renderCats();renderStream();tlstatus.textContent='显示 '+streamEvents().length+' 条 · '+catLabel(c)}
async function loadStream(){const r=await fetch('/api/event-stream');STREAM=await r.json();renderCats();renderStream();tlstatus.textContent='已加载 '+STREAM.counts.events+' 条 · 点「回放事件流」看先后顺序';tlfoot.textContent='六类计数：'+(STREAM.categories||[]).map(c=>c.label+' '+c.count).join(' · ')+' ｜ '+STREAM.source}
async function playTimeline(){tlstatus.textContent='读取事件流…';const r=await fetch('/api/event-stream');STREAM=await r.json();renderCats();renderStream();
 const lis=[...timeline.children];for(let i=0;i<lis.length;i++){lis[i].classList.remove('on');await new Promise(done=>setTimeout(done,45));lis[i].classList.add('on')}
 tlstatus.textContent='回放完成：'+STREAM.counts.events+' 条事件';
 tlfoot.textContent='六类计数：'+(STREAM.categories||[]).map(c=>c.label+' '+c.count).join(' · ')+' ｜ '+STREAM.source}
async function loadInterventions(){const r=await fetch('/api/interventions');const d=await r.json();ivbtns.innerHTML='';(d.entries||[]).forEach(e=>{const b=document.createElement('button');b.className='ivbtn';b.innerHTML=esc(e.label)+'<small>'+esc(e.hint)+'</small>';b.onclick=()=>doIntervene(e.id);ivbtns.appendChild(b)})}
async function doIntervene(id){ivout.innerHTML='<span class=warn>重推演中：复制数据集、打补丁、重跑整条流水线…</span>';
 const r=await fetch('/api/interventions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});
 const d=await r.json();
 if(d.status==='error'){ivout.innerHTML='<span class=warn>'+esc(d.error)+'</span>';return}
 const c=d.comparison;
 const rows=[['可用移动泵',c.before.available+' → '+c.after.available+' 台'],['资源缺口',c.before.gap+' → '+c.after.gap+' 台'],['冲突集合',(c.before.conflicts.join('、')||'无')+' → '+(c.after.conflicts.join('、')||'无')],['处置动作',c.before.actions+' 条 · '+c.moved_actions.length+' 条挪位']];
 const tv=d.travel;
 const travelHtml=(tv&&tv.available)?'<div class=row><span>封路后到场时间（'+esc(tv.origin)+' → '+esc(tv.target)+'）</span><b>'+esc(tv.baseline.minutes)+' → '+esc(tv.after_closure.minutes)+' 分钟</b></div><div class=row><span>绕行距离增量</span><b>+'+esc(tv.delta_meters)+' m</b></div><div style="margin-top:6px;font-size:11px;color:#8fa4bd">封路只改路网到场时间，不改移动泵缺口——缺口由库存与需求折算决定，两件事必须分开说</div>':'';
 const stmts='<ul style="margin:10px 0 0;padding-left:18px;font-size:13px;color:#cfe3f7">'+c.statements.map(s=>'<li>'+esc(s)+'</li>').join('')+'</ul>';
 const moved=c.moved_actions.length?'<div style="margin-top:10px;font-size:12px;color:#8fa4bd">挪位：'+c.moved_actions.map(m=>'#'+m.order).join('、')+'</div>':'';
 ivout.innerHTML='<b style="color:#7dd3fc">'+esc(d.label)+'</b><div style="color:#b8c7d9;font-size:13px;margin:6px 0 10px">改了什么：'+esc(d.plain||d.patches.join(' · '))+'</div>'+rows.map(x=>'<div class=row><span>'+esc(x[0])+'</span><b>'+esc(x[1])+'</b></div>').join('')+travelHtml+stmts+moved+'<div style="margin-top:10px;font-size:11px;color:#7f93ab">'+esc(d.mechanism)+'</div>'}
async function loadCollab(){const r=await fetch('/api/collab-review');const d=await r.json();
 if(d.status!=='ok'){cvsum.innerHTML='<span class=warn>'+esc(d.error||'复核不可用')+'</span>';return}
 const c=d.counts;
 cvsum.innerHTML='动作卡 <b>'+c.cards+'</b> 张 · 部门 <b>'+c.agents+'</b> 个 · 交接 <b>'+c.handoffs+'</b> 次 · 需会签 <b>'+c.co_sign_pairs+'</b> 对 · 规则冲突 <b class="'+(c.violations?'warn':'ok')+'">'+c.violations+'</b> 处';
 const byAgent={};(d.cards||[]).forEach(x=>{(byAgent[x.agent_id]=byAgent[x.agent_id]||[]).push(x)});
 cvcards.innerHTML=Object.keys(byAgent).map(a=>'<div class=cvwho>'+esc(orgName(a))+'<span class=tag>'+byAgent[a].length+' 张动作卡</span></div>'+byAgent[a].map(x=>'<div class=cvrow><span class=tag>'+esc(zh(CSTAGE,x.stage))+'</span><span class=cvact title="'+esc(x.action)+'">'+esc(clipText(x.action,80))+'</span><b class="'+(x.status==='accepted'?'ok':'warn')+'">'+esc(zh(CSTAT,x.status))+'</b></div>').join('')).join('');
 cvhand.textContent=(d.handoffs||[]).length?'交接关系：'+d.handoffs.map(h=>orgName(h.from_agent)+' → '+orgName(h.to_agent)+'（'+h.card_ids.length+' 张）').join(' ｜ '):'本次无需角色交接；每个部门只复核自己职责范围内的动作卡'}
let SCHED=null,TLINKS=null;
// ---- 执行账本：计划（推演派生） vs 实际（账本事件） ----
let EXEC=null,PENDING_CLOSE='';
async function loadExecution(){
  try{const r=await fetch('/api/execution');EXEC=await r.json();renderExecution()}
  catch(e){EXEC=null;const s=document.getElementById('exec-summary');if(s)s.innerHTML='<span class=warn>执行账本不可用（/api/execution 未能加载）</span>'}
}
function execMsg(text,ok){
  const el=document.getElementById('exec-msg');if(!el)return;
  el.innerHTML=text?'<span class="'+(ok?'ok':'warn')+'">'+esc(text)+'</span>':'';
}
function renderExecution(){
  const s=document.getElementById('exec-summary');if(!s)return;
  if(!EXEC){s.innerHTML='<span class=warn>执行账本不可用</span>';return}
  const p=EXEC.plan;
  s.innerHTML='计划完成 <b>'+p.planned_done+'</b> 张 · 账本实际完成 <b>'+p.actual_done+'</b> 张 · 偏差 '
    +'<b class="'+(p.delta<0?'warn':'ok')+'">'+(p.delta>0?'+':'')+p.delta+'</b>'
    +(p.behind.length?' · 未落地 '+p.behind.length+' 张':'')
    +'<div style="margin-top:6px;font-size:11.5px;color:#7f93ab">身份：'+esc(EXEC.actor.actor_org)+' / '+esc(EXEC.actor.actor_role)
    +' · 账本目录：'+esc(EXEC.ledger)+' · 工单台账不动，关闭只记在账本</div>';

  const box=document.getElementById('exec-cards');
  if(!box)return;
  box.innerHTML=(EXEC.cards||[]).map(function(c){
    let label='',act='',to='';
    if(c.can_start){label='开始执行';act='transition';to='running'}
    else if(c.can_finish&&c.co_sign_required.length){label='申请会签';act='transition';to='needs_confirm'}
    else if(c.can_finish){label='提交完成';act='transition';to='done'}
    else if(c.can_confirm){label='会签确认';act='sign';to=''}
    const btn=label?'<button data-card="'+esc(c.card_id)+'" data-act="'+act+'" data-to="'+to+'" data-ver="'+c.version+'">'+label+'</button>':'';
    const sign=c.co_sign_required.length?' · 需会签 '+esc(c.co_sign_required.join('、')):'';
    return '<div class=exrow><span class="st '+esc(c.state)+'">'+esc(c.state_label)+'</span>'
      +'<span class=who>'+esc(c.org_name)+'</span>'
      +'<span class=act title="'+esc(c.action)+sign+'">'+esc(c.action)+'</span>'+btn+'</div>';
  }).join('')||'<span class=sub>本次事件没有可执行的处置动作卡</span>';
  box.querySelectorAll('button[data-card]').forEach(function(b){
    b.onclick=function(){execAct(b.dataset.card,b.dataset.act,b.dataset.to,Number(b.dataset.ver))};
  });

  const cb=document.getElementById('exec-close');
  if(cb){
    if(PENDING_CLOSE){
      cb.innerHTML='<div class=exclose><b>确认关闭工单 '+esc(PENDING_CLOSE)+'？</b>'
        +' 这是人工确认动作：会写入执行账本并留痕，但**不改**冻结的 work_orders.csv。'
        +'<div style="margin-top:8px"><button data-close="'+esc(PENDING_CLOSE)+'">确认关闭</button> '
        +'<button class=ghost data-cancel="1">取消</button></div></div>';
    }else if((EXEC.closable_orders||[]).length){
      cb.innerHTML='<div class=exclose>动作卡已全部完成，可申请关闭：'
        +EXEC.closable_orders.map(function(o){return '<b>'+esc(o)+'</b> <button data-request="'+esc(o)+'">申请关闭</button>'}).join(' ')
        +'<div style="margin-top:6px;color:#7f93ab">关闭需人工确认，不会自动发生。</div></div>';
    }else{
      // 没到可关闭条件时也要说清差多少，否则用户以为按钮坏了
      const prog=(EXEC.order_progress||[]).map(function(p){
        return esc(p.order_id)+' '+p.done+'/'+p.total+(p.closed?'（账本已关闭）':'');
      }).join(' · ');
      cb.innerHTML=prog?'<div class=exclose>工单完成进度：'+prog+'<div style="margin-top:6px;color:#7f93ab">全部动作卡完成后才出现「申请关闭」；关闭不会自动发生。</div></div>':'';
    }
    cb.querySelectorAll('button[data-close]').forEach(function(b){b.onclick=function(){execClose(b.dataset.close)}});
    cb.querySelectorAll('button[data-request]').forEach(function(b){b.onclick=function(){PENDING_CLOSE=b.dataset.request;renderExecution()}});
    cb.querySelectorAll('button[data-cancel]').forEach(function(b){b.onclick=function(){PENDING_CLOSE='';renderExecution()}});
  }

  const ev=document.getElementById('exec-events');
  if(ev){
    ev.innerHTML=(EXEC.events||[]).slice().reverse().map(function(e){
      const what=e.action==='sign'?'会签（'+esc(e.actor_org)+'）':(e.to_state==='order_closed'?'关闭工单 '+esc(e.order_id):esc(e.from_state||'')+' → '+esc(e.to_state));
      return '<div class=row><span>'+esc(e.event_id)+' · '+esc(e.card_id)+'</span><b>'+what+'</b></div>';
    }).join('')||'<span class=sub>暂无事件：账本为空时所有卡都是「待执行」</span>';
  }
}
async function execAct(cardId,action,toState,version){
  const body={event_id:'UI-'+Date.now()+'-'+cardId,card_id:cardId,action:action,expected_version:version};
  if(toState)body.to_state=toState;
  execMsg('提交中…',true);
  const r=await fetch('/api/execution/events',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(d.status==='error'){execMsg('被拒绝（'+d.code+'）：'+d.error,false);return}
  if(d.status==='duplicate'){execMsg(d.reason==='already_in_state'?'已忽略：该卡已处于这个状态（重复提交）':'已忽略：同一张卡同一状态已写过（幂等）',true);await loadExecution();return}
  execMsg('已写入账本：'+d.event.event_id+' · '+cardId,true);
  await loadExecution();
}
async function execClose(orderId){
  // 关闭事件仍要挂在一张卡上（审计要求"谁的动作触发了关单"），取该工单关联的第一张卡
  const card=(EXEC.cards||[]).find(function(c){return (c.order_ids||[]).indexOf(orderId)>=0})||(EXEC.cards||[])[0];
  if(!card){execMsg('账本里找不到与该工单关联的动作卡',false);return}
  execMsg('提交关闭…',true);
  const r=await fetch('/api/execution/events',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:'UI-CLOSE-'+Date.now(),card_id:card.card_id,action:'close_order',order_id:orderId,expected_version:0,note:'人工确认关闭（演示）'})});
  const d=await r.json();
  PENDING_CLOSE='';
  if(d.status==='error'){execMsg('关闭被拒绝（'+d.code+'）：'+d.error,false);await loadExecution();return}
  execMsg('工单 '+orderId+' 已在账本中标记关闭（台账未改动）',true);
  await loadExecution();
}
// 沙盘关联层：主页面只借两样东西——六跳的跳次号与"这一跳挂了哪些对象"。
// 跳次号必须来自后端，前端自己数会把冲突明细算成第 5、6 跳。
async function loadTwinLinks(){try{const r=await fetch('/api/twin-links?jump=7');TLINKS=await r.json()}catch(e){TLINKS=null}return TLINKS}
const SCENE_KIND={'water':'积水 / 事件','road':'道路','facility':'设施','district':'片区'};const SCENE_STATE={'incident':'当前事件','water':'积水影响','closed':'已封闭','under_construction':'施工中','clogged':'堵塞','fault_pending_repair':'待维修','in_service':'正常','normal':'正常','active':'处置中'};let SCENE_SELECTED_ID='';
/* DOM 引用必须显式声明。此前靠「id 与变量同名时自动挂到 window」的浏览器遗留行为跑，
   名字对得上（orders/resboard/plans/...）就侥幸能跑，对不上（id 是 scene-summary 而代码写
   sceneSummary）就 ReferenceError —— 而且是在 drawScene 第一行抛，async 链整条中断，
   于是地图空白、后面六个面板全不渲染。静态断言看不出这个，只有真浏览器能暴露。 */
let sceneSummary=document.getElementById('scene-summary'),sceneDetail=document.getElementById('scene-detail'),
 scenemap=document.getElementById('scenemap'),orders=document.getElementById('orders'),
 resboard=document.getElementById('resboard'),plans=document.getElementById('plans'),
 chain=document.getElementById('chain'),baseline=document.getElementById('baseline'),
 chainstatus=document.getElementById('chainstatus'),planfoot=document.getElementById('planfoot'),
 chainfoot=document.getElementById('chainfoot'),basefoot=document.getElementById('basefoot');
function renderSceneSummary(scene){const entities=(scene&&scene.entities)||[];const active=entities.filter(e=>e.kind!=='district'&&((e.links&&e.links.work_orders)||[]).length);const counts=['water','road','facility'].map(kind=>{const n=active.filter(e=>e.kind===kind).length;return n?(SCENE_KIND[kind]+' '+n+' 个'):''}).filter(Boolean);if(active.length){sceneSummary.innerHTML='<b>本次事件关联 '+active.length+' 个对象</b><span>'+esc(counts.join(' · '))+'</span><small>点对象条目查看台账关联</small>'}else{sceneSummary.innerHTML='<b>暂无关联对象</b><small>请重新运行演示</small>'}}
function drawScene(scene){if(!scene||!scene.entities)return;renderSceneSummary(scene);
 // 上方「动态内涝推演」已有同一份实体的动画地图，这里不再画第二张雷同图（评委看着像复制了两遍）。
 // 改为对象清单：一行一个对象，点条目看台账关联；空间位置只看上面那张图。
 const ents=(scene.entities||[]).filter(e=>e.kind!=='district');
 scenemap.innerHTML=ents.map(function(e,i){
  const cls='ent-item k-'+esc(e.kind)+(SCENE_SELECTED_ID===e.id?' selected':'');
  // role=button + tabindex 只让人"能聚焦"，不按 Enter 就等于没键盘支持。
  // 实测：焦点能落到实体上，但 before=0 / after=0，详情面板纹丝不动。
  const attrs='data-id="'+esc(e.id)+'" role="button" focusable="true" tabindex="0" aria-label="'+esc(e.label)+'" onclick="pickEntity(\\''+esc(e.id)+'\\')" onkeydown="entKey(event,\\''+esc(e.id)+'\\')"';
  return '<div class="'+cls+'" '+attrs+'><span class=no>'+(i+1)+'</span><div class=ent-main><b>'+esc(e.label)+'</b><small>'+esc(SCENE_STATE[e.state]||e.state||'待确认')+'</small></div><span class=ent-kind>'+esc(SCENE_KIND[e.kind]||e.kind)+'</span></div>'}).join('')}
function pickEntity(id){if(!SCHED)return;const e=(SCHED.scene.entities||[]).find(x=>x.id===id);if(!e)return;SCENE_SELECTED_ID=id;drawScene(SCHED.scene);
 const ords=(SCHED.work_orders||[]).filter(o=>(e.links.work_orders||[]).includes(o.order_id));
 const ress=(SCHED.resources.items||[]).filter(r=>(e.links.resources||[]).includes(r.resource_id));
 const orders=ords.length?ords.map(o=>esc(o.order_id)+'（'+esc(o.order_type_label||'工单')+'，'+esc(zh(STT,o.status))+'）').join('；'):'暂无关联工单';
 const resources=ress.length?ress.map(r=>esc(r.name)+' · 可用 '+esc(r.qty_available)+esc(r.unit)+(r.required!=null?' · 本次需 '+esc(r.required):'')).join('；'):'暂无资源占用';
 const evidence=(e.links.evidence||[]).length?(e.links.evidence||[]).map(refCn).join(' · '):'暂无台账依据';
 sceneDetail.innerHTML='<div class=scene-detail-head><span class=scene-kind>'+esc(SCENE_KIND[e.kind]||e.kind)+'</span><b>'+esc(e.label)+'</b><span class=scene-state>'+esc(SCENE_STATE[e.state]||e.state||'待确认')+'</span></div>'+
  '<div class=scene-detail-grid><div><small>影响说明</small><strong>'+esc(e.detail||'暂无说明')+'</strong></div><div><small>关联工单</small><strong>'+orders+'</strong></div><div><small>所需资源</small><strong>'+resources+'</strong></div></div>'+
  '<details class=scene-evidence><summary>查看判断依据</summary><div class=evidence>'+esc(evidence)+'</div></details>'+
  '<div style="margin-top:10px"><a class=deep href="/twin3d?jump='+entityJump(id)+'&entity='+encodeURIComponent(id)+'" target=_blank rel=noopener>在三维沙盘中查看该对象 →</a></div>'}
function entKey(ev,id){const k=ev.key;if(k==='Enter'||k===' '||k==='Spacebar'||k==='Space'){ev.preventDefault();pickEntity(id)}}
function renderDepartments(){const el=document.getElementById('departments');
 if(!SCHED||!SCHED.departments||!SCHED.departments.length){el.innerHTML='<span class=sub>部门台账加载中…</span>';return}
 const inv=new Set(SCHED.involved_orgs||[]);
 el.innerHTML='<div class=sub style="font-size:12px;margin-bottom:6px">台账共 '+SCHED.departments.length+' 个部门，其中 '+inv.size+' 个涉及本次事件（亮色标“参与本次”）</div>'+
  SCHED.departments.map(d=>'<div class="row dp'+(inv.has(d.org_id)?' inv':'')+'"><span>'+esc(d.org_name)+' <span class=tag>'+esc(d.org_id)+'</span></span><b class="'+(inv.has(d.org_id)?'ok':'sub')+'">'+(inv.has(d.org_id)?'参与本次':'未涉及')+'</b></div><div class=dp>' + esc(d.authority||'（台账未写职责）') + '</div>').join('')}
function renderOrders(){(orders=document.getElementById('orders')).className='';
 orders.innerHTML=(SCHED.work_orders||[]).map(o=>'<div class="ord'+(o.is_event?' ev':'')+'"><span>'+esc(o.order_type_label)+' · '+esc(o.road_id||o.facility_id||o.pipeline_id||'-')+'</span><span class="sev '+esc(o.severity)+'">风险'+esc(zh(SEV,o.severity))+'</span><div class=ds>'+esc(o.order_id)+' · '+esc(o.reporter_org)+' · '+esc(zh(STT,o.status))+' · '+esc(o.description)+'</div></div>').join('')}
function renderResources(){const r=SCHED.resources||{};const s=r.summary||{};
 (resboard=document.getElementById('resboard')).className='';
 resboard.innerHTML='<div class=row><span>台账资源项 / 本次引用</span><b>'+esc(s.total_rows)+' / '+esc(s.used_in_event)+'</b></div><div class=row><span>需求 / 已到位 / 缺口</span><b>'+esc(s.required)+' / '+esc(s.committed)+' / <span class="'+(s.gap>0?'warn':'ok')+'">'+esc(s.gap)+'</span> '+esc(s.unit)+'</b></div>'+
  (r.items||[]).map(i=>'<div class=row><span>'+esc(i.name)+' <span class=tag>'+esc(i.district)+'</span></span><b>'+(i.used_in_event?(esc(i.required)+' / '+esc(i.committed)+' / '+(i.gap>0?'<span class=warn>'+esc(i.gap)+'</span>':esc(i.gap))):('<span class=sub>'+esc(i.qty_available)+esc(i.unit)+'</span>'))+'</b></div>').join('')}
function renderPlan(){const p=SCHED.plan||{};
 (plans=document.getElementById('plans')).className='';
 if(!p||p.error||!p.current||!p.current.per_district){plans.innerHTML='<span class=warn>'+esc((p&&p.error)||'方案不可用')+'</span>';return}
 const col=(title,view,cls)=>'<div class="col '+cls+'"><h3>'+esc(title)+'</h3>'+(view.per_district||[]).map(d=>'<div class=kv><span>'+esc(d.district)+'</span><b>'+esc(d.allocated)+' / '+esc(d.demand)+' 台</b></div>').join('')+'<div class=kv><span>总缺口</span><b>'+esc(view.total_unmet)+' 台</b></div><div class=kv><span>跨区调拨</span><b>'+((view.transfers||[]).length?(view.transfers||[]).length+' 次':'无')+'</b></div></div>';
 plans.innerHTML='<div class=plan>'+col('当前方案：'+zh(POL,p.current.policy_version),p.current,'now')+col('备选方案：'+zh(POL,p.alternative.policy_version),p.alternative,'alt')+'</div>';
 planfoot.textContent=(p.statements||[]).join(' ｜ ')}
function renderChain(){const c=SCHED.chain||{};
 (chain=document.getElementById('chain')).innerHTML=(c.stages||[]).map(s=>'<div class=lnk id="lk-'+esc(s.id)+'"><span class=no>'+esc(s.seq)+'</span><span class=who>'+esc(zh(ROLE,s.actor))+'</span><span class=what><b>'+esc(s.title)+'</b><div>'+esc(s.summary)+'</div></span><span class=tool>'+esc(tools(s.tool))+'</span></div>').join('');
 chainfoot.textContent='实际调用的 MCP 工具（括号内为接口原名）：'+((c.tools||[]).join(' → ')||'—')+' ｜ '+esc(c.source||'')}
function renderBaseline(){const b=SCHED.baseline||{};const m=b.manual||{};const a=b.agent||{};
 (baseline=document.getElementById('baseline')).className='';
 const rows=[
  ['参与部门 / 角色',m.organizations+' 个部门',a.review_agents+' 个部门角色'],
  ['跨部门确认次数（下限）',m.confirm_calls_min+' 次',a.tool_calls+' 次工具调用'],
  ['需翻阅台账 / 证据引用',m.ledger_rows+' 行台账',a.evidence_refs+' 条带行号证据'],
  ['需读规程预案',m.procedure_chars+' 字','按 event_id 检索'],
  ['结论可追溯',m.traceability,'引用完整率 '+pct(a.citation_completeness)],
  ['耗时',(m.duration_minutes==null?'未实测':m.duration_minutes+' 分钟'),a.compute_ms+' ms（计算耗时，非协同耗时）']
 ];
 baseline.innerHTML=rows.map(r=>'<div class=row><span>'+esc(r[0])+'</span><span class=sub style="margin-left:12px">人工 '+esc(r[1])+'</span><b>智能 '+esc(r[2])+'</b></div>').join('');
 basefoot.textContent=(b.statements||[]).join(' ｜ ')+((b.unmeasured||[]).length?' ｜ 未测：'+(b.unmeasured||[]).join('、'):'')}
async function loadScheduler(){const r=await fetch('/api/scheduler');SCHED=await r.json();drawScene(SCHED.scene);renderDepartments();renderOrders();renderResources();renderPlan();renderChain();renderBaseline()}
function chainAll(on){document.querySelectorAll('#chain .lnk').forEach(function(el){el.classList.toggle('on',on)});if(on&&chainstatus)chainstatus.textContent='已就绪 · 点「跑一遍链路」看逐步执行';if(on)drawFloodMap()}
async function runChain(){if(!SCHED){chainstatus.textContent='加载中…';await loadScheduler()}
 const ls=[...document.getElementById('chain').children];chainstatus.textContent='执行中 0/'+ls.length;
 for(let i=0;i<ls.length;i++){ls[i].classList.remove('on');await new Promise(done=>setTimeout(done,200));ls[i].classList.add('on');chainstatus.textContent='第 '+(i+1)+'/'+ls.length+' 环：'+(((SCHED.chain||{}).stages||[])[i]||{}).title}
 chainstatus.textContent='链路完成：'+((SCHED.chain||{}).counts||{}).stages+' 环'}
loadPlatform();loadLog();loadInterventions();loadCollab();run();try{if(localStorage.getItem('urban-proj')==='1'||new URLSearchParams(location.search).get('proj')==='1')toggleProjection()}catch(e){}loadTwinLinks().then(loadStream);loadScheduler().then(function(){chainAll(true)});loadExecution();</script></html>""".replace("</style>\n<main>", "</style>"+IMPACT_STYLE+"\n<main>").replace("</style><main>", "</style>"+IMPACT_STYLE+"<main>").replace("</div></details></main>\n<script>", "</div></details>"+PAGE_STYLE+"</main>\n<script>")


# --- 可视化端点 -----------------------------------------------------------


def render_snapshot(policy_version: str = "v1", *, origin: str = "manual") -> dict[str, Any]:
    """跑一次决策并落盘渲染作业。origin=manual 表示人工点按钮（与 mcp 区分）。"""
    policy = EVOLVED_POLICY if policy_version.lower() not in {"", "v1"} else None
    snapshot = visualize.build_visual_snapshot(EVENT_ID, AS_OF, policy=policy)
    visualize.save_snapshot(snapshot)
    job = visualize.build_render_job(snapshot)
    visualize.save_job(job)
    visualize.append_tool_log(
        {
            "tool": "scene_render",
            "origin": origin,
            "status": "ok",
            "snapshot_id": snapshot.snapshot_id,
            "policy_version": snapshot.policy_version,
            "scenario": snapshot.scenario,
        }
    )
    return {
        "snapshot_id": snapshot.snapshot_id,
        "policy_version": snapshot.policy_version,
        "scenario": snapshot.scenario,
        "panel": job.panel,
        "metrics": job.metrics,
        "delivery": visualize.delivery_state(snapshot.snapshot_id),
    }


def scene_job(snapshot_id: str | None = None) -> dict[str, Any]:
    resolved = snapshot_id or visualize.latest_snapshot_id()
    if not resolved:
        raise KeyError("还没有任何渲染作业")
    return visualize.load_job(resolved).model_dump(mode="json")


def compare_scenes(baseline_id: str, candidate_id: str) -> dict[str, Any]:
    result = visualize.compare_snapshots(
        visualize.load_snapshot(baseline_id), visualize.load_snapshot(candidate_id)
    )
    return result.model_dump(mode="json")


# --- 真实用户评分采集 ------------------------------------------------------
#
# 评分通道只汇总（不进门禁）；只有"带修正建议"的提交会转成评审用例进 P9 门禁。
# 提交记录落盘为 JSONL（追加式），口径与 CLI 一致（decision_poc.review_collect）。


def review_signals(policy_label: str = "v1") -> tuple[list[DistrictSignal], dict[str, int]]:
    """取当前决策的输入快照与分配方案，让评分提交自包含（评审用例要能复算）。"""
    policy = EVOLVED_POLICY if policy_label.lower() not in {"", "v1"} else None
    snapshot = visualize.build_visual_snapshot(EVENT_ID, AS_OF, policy=policy)
    lines = (snapshot.allocation or {}).get("lines", [])
    signals = [
        DistrictSignal(
            district=str(item["district"]),
            depth_cm=item.get("depth_cm"),
            closure_required=item.get("closure_required"),
            has_hazard=bool(item.get("has_hazard")),
            demand=int(item.get("demand", 0)),
            local_available=int(item.get("local_available", 0)),
        )
        for item in lines
    ]
    current = {str(item["district"]): int(item.get("allocated", 0)) for item in lines}
    return signals, current


def evolution_after_review() -> dict[str, Any]:
    """收到「进化后」评分时重跑一次进化门禁，把候选采纳/拒绝结果带回页面。

    只跑一次 evolve（不写盘、不碰记忆），让"用户打了分 → 策略门禁 → 采纳/拒绝"
    在同一屏里闭环；失败不影响评分落盘。
    """
    from . import policy_evolution

    records = reviews.to_review_records(reviews.load_submissions())
    report = policy_evolution.evolve(
        reviews=records, include_real_event=False, use_memory=False
    )
    accepted = sum(1 for item in report.rounds if item.action == "promoted")
    rejected = sum(1 for item in report.rounds if item.action == "rejected")
    result: dict[str, Any] = {
        "eligible": True,
        "final_label": report.releases[-1].label if report.releases else "v1",
        "rounds": len(report.rounds),
        "accepted": accepted,
        "rejected": rejected,
        "review_ids": [item.review_id for item in report.reviews],
        "train_deviation": [report.initial_train.deviation, report.final_train.deviation],
        "holdout_deviation": [report.initial_holdout.deviation, report.final_holdout.deviation],
        "trace": [
            {"round": item.round, "action": item.action, "reason": item.reason}
            for item in report.rounds
        ],
    }
    try:
        result["saved_to"] = str(save_evolution_gate(result))
    except OSError as exc:  # 落盘失败不影响返回门禁结果
        result["saved_to"] = None
        result["save_error"] = f"{type(exc).__name__}: {exc}"
    return result


def submit_review(payload: Any, *, source: str = "web") -> dict[str, Any]:
    """校验并落盘一次真实用户评分。""" 
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    reviewer_id = str(payload.get("reviewer_id") or "").strip()
    if not reviewer_id:
        raise ValueError("reviewer_id 不能为空")
    phase = str(payload.get("phase") or "").strip()
    if phase not in reviews.PHASES:
        raise ValueError("phase 只能是 before / after")
    scores = payload.get("scores")
    if not isinstance(scores, dict):
        raise ValueError("scores 必须是对象（accuracy/timeliness/safety/explainability）")
    policy_label = str(payload.get("policy_version") or "v1")
    suggested = payload.get("suggested_allocation") or {}
    if isinstance(suggested, str):
        suggested = reviews.parse_allocation(suggested) if suggested.strip() else {}
    if not isinstance(suggested, dict):
        raise ValueError("suggested_allocation 必须是对象或 'A-03=5,B-01=1' 形式")
    signals, current = review_signals(policy_label) if suggested else ([], {})
    submission = reviews.make_submission(
        reviewer_id=reviewer_id,
        reviewer_role=str(payload.get("reviewer_role") or "未填写"),
        phase=phase,
        scores=scores,
        policy_version=policy_label,
        comment=str(payload.get("comment") or ""),
        suggested_allocation={str(key): int(value) for key, value in suggested.items()},
        signals=signals,
        current_allocation=current,
        source=source,
    )
    target, created = reviews.append_submission(submission)
    result: dict[str, Any] = {
        "status": "created" if created else "duplicate",
        "submission_id": submission.submission_id,
        "created": created,
        "path": str(target),
        "overall": submission.overall,
        "gate_candidate": bool(submission.suggested_allocation),
        "summary": reviews.summarize(reviews.load_submissions()),
    }
    if submission.phase == "after":
        try:
            result["evolution"] = evolution_after_review()
        except Exception as exc:  # 门禁失败不能吞掉评分本身
            result["evolution"] = {"eligible": False, "error": f"{type(exc).__name__}: {exc}"}
    return result


def review_payload(limit: int = 10) -> dict[str, Any]:
    items = reviews.load_submissions()
    return {
        "summary": reviews.summarize(items),
        "recent": [item.model_dump(mode="json") for item in items[-limit:]],
    }


def _json(payload: Any, status: int = 200) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(
        payload, ensure_ascii=False
    ).encode("utf-8")


# --- 微调度台视图：一次算好，多个端点共享 ---------------------------------
#
# 页面一进来要工单、资源、方案、场景、链路五份数据。若每个端点各自跑一遍推演，
# 首屏要十几秒——演示现场没人愿意等。这里按 (event_id, as_of) 缓存一份；
# 演示前点一下"刷新"（`?refresh=1`）强制重算，看得到耗时字段的变化。
_VIEW_CACHE: dict[str, dict[str, Any]] = {}


def scheduler_view(*, refresh: bool = False) -> dict[str, Any]:
    key = f"{EVENT_ID}|{AS_OF}"
    if not refresh and key in _VIEW_CACHE:
        return _VIEW_CACHE[key]

    payload = build_demo_payload()
    collab = collab_review()
    orders = scheduler.load_work_orders(event_id=payload.get("event_id") or EVENT_ID)
    try:
        plan: dict[str, Any] | None = scheduler.build_plan_alternatives(payload)
    except Exception as exc:  # 方案层失败不能连累推演与场景展示
        plan = {"error": f"{type(exc).__name__}: {exc}", "current": {}, "alternative": {}, "diff": [], "statements": []}
    _VIEW_CACHE[key] = {
        "payload": payload,
        "collab": collab,
        "orders": orders,
        "plan": plan,
        "chain": scheduler.build_chain(payload, collab=collab, plan=plan, orders=orders),
        "resources": scheduler.build_resource_board(payload),
        "departments": scheduler.load_departments(),
        "involved_orgs": list((payload.get("impact") or {}).get("departments") or []),
        "scene": scheduler.build_scene_entities(payload),
        "stream": build_event_stream(payload, collab=collab),
        "baseline": baseline_compare.compare(payload, collab=collab),
    }
    return _VIEW_CACHE[key]


def execution_cards() -> dict[str, dict[str, Any]]:
    """执行账本认的动作卡清单。

    卡片来自关联层（与沙盘同一份），账本只认这里有的 card_id——
    否则前端可以凭空提交一张不存在的卡，把账本写脏。
    """
    links = build_twin_links(scheduler_view(), current_jump=MAX_JUMP)
    cards: dict[str, dict[str, Any]] = {}
    for card in links["cards"]:
        # 执行账本只收 **action 阶段** 的卡：观测/影响/责任/冲突/资源这五跳是推演的
        # 分析产物，不是谁去现场执行的动作。把它们也纳入账本，会逼人给"观测"点开工。
        if card["stage"] != "action":
            continue
        cards[str(card["card_id"])] = {
            "card_id": card["card_id"],
            "order_ids": [str(item) for item in card.get("work_order_ids") or []],
            "co_sign_required": [str(item) for item in card.get("co_sign_required") or []],
            "claim_ids": [str(item) for item in card.get("claim_ids") or []],
            "evidence_refs": [str(item) for item in card.get("evidence_refs") or []],
            "org_name": card["org_name"],
            "stage": card["stage"],
            "jump": card["jump"],
            "action": card["action"],
            "planned_state": card["state"],
        }
    return cards


def execution_view() -> dict[str, Any]:
    """计划（推演派生） vs 实际（账本）对照。"""
    cards = execution_cards()
    projected = execution_ledger.current_state(cards)
    plan = execution_ledger.compare_plan(list(cards.values()), projected)
    rows = []
    for card_id, row in projected["cards"].items():
        source = cards[card_id]
        rows.append(
            {
                **row,
                "org_name": source["org_name"],
                "stage": source["stage"],
                "jump": source["jump"],
                "action": source["action"],
                "planned_state": source["planned_state"],
                "planned_label": execution_ledger.STATE_LABEL.get(source["planned_state"], ""),
                "state_label": execution_ledger.STATE_LABEL.get(row["state"], row["state"]),
                "can_start": row["state"] == "pending",
                "can_finish": row["state"] == "running",
                "can_confirm": row["state"] == "needs_confirm",
            }
        )
    rows.sort(key=lambda item: (item["jump"], item["card_id"]))

    # 哪些工单已经"卡全完成、可以申请关闭"：由后端算，页面不重复一遍状态机
    by_order: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for order_id in row["order_ids"]:
            by_order.setdefault(order_id, []).append(row)
    closed_ids = set(projected["closed_orders"])
    closable = [
        order_id
        for order_id, items in by_order.items()
        if items and order_id not in closed_ids and all(item["state"] == "done" for item in items)
    ]
    order_progress = [
        {
            "order_id": order_id,
            "total": len(items),
            "done": len([item for item in items if item["state"] == "done"]),
            "closed": order_id in closed_ids,
        }
        for order_id, items in sorted(by_order.items())
    ]

    return {
        "status": "ok",
        "plan": plan,
        "counts": projected["counts"],
        "cards": rows,
        "closable_orders": sorted(closable),
        "order_progress": order_progress,
        "closed_orders": list(projected["closed_orders"].values()),
        "events": execution_ledger.read_events()[-20:],
        "actor": dict(execution_ledger.DEFAULT_ACTOR),
        "ledger": str(execution_ledger.execution_dir()),
        "source": "计划=推演按跳次派生；实际=执行账本 events.jsonl（演示，无真实身份系统）",
    }


def _error(message: str, status: int = 404) -> tuple[int, str, bytes]:
    return _json({"status": "error", "error": message}, status)


def _assets_dir():
    """静态资产目录（three.js 内嵌分发，断网可用）。"""
    return Path(__file__).resolve().parents[2] / "assets"


def build_twin3d_html() -> str:
    """三维数字孪生写实沙盘（独立路由 /twin3d，主页面不动）。

    两个片区全景：混凝土立面楼栋、沥青/人行道 PBR 材质、行道树、路灯、
    路侧车辆、积水段(镜面反射+涟漪)、雨丝粒子、阴雨 HDR 天空。
    场景坐标来自 /api/scheduler 的 scene.entities；材质为 CC0(Poly Haven)，
    全部本地分发，断网可用。
    """
    return """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>城市内涝三维数字孪生 · 写实沙盘</title>
<style>html,body{margin:0;height:100%;background:#0b0e13;overflow:hidden;font:14px system-ui;color:#d7e4f0}
/* 标题区与顶部横幅原来都钉在 top:16px（一个左对齐、一个居中），副标题横跨到屏幕中部，
   必然被横幅压住；而且标题本身没有底色，浅灰文字压到浅色楼体上同样读不清。
   解法：标题加「左浓右淡」的渐变托底（不是硬边黑卡，右侧自然淡出到场景），
   横幅改挂右上角——那一带是空的，不与任何固定面板冲突。 */
#hud{position:fixed;left:18px;top:16px;z-index:5;pointer-events:none;max-width:min(620px,calc(100vw - 430px));padding:8px 26px 10px 12px;border-radius:10px;background:linear-gradient(100deg,rgba(7,18,28,.88) 0%,rgba(7,18,28,.78) 72%,rgba(7,18,28,0) 100%)}
#hud b{display:block;font-size:18px;color:#e8f3fb;letter-spacing:.5px}
#hud span{display:block;margin-top:5px;font-size:12.5px;color:#a3b6c8}
#event-bar{position:fixed;right:18px;top:16px;z-index:5;pointer-events:none;text-align:right;padding:8px 18px;border-radius:10px;background:rgba(7,18,28,.72);border:1px solid rgba(122,162,198,.35)}
#event-bar b{display:block;font-size:15px;color:#e0f2fe;letter-spacing:.5px}
#event-bar span{display:block;margin-top:3px;font-size:12px;color:#8fb7d6}
#legend{position:fixed;left:18px;bottom:16px;z-index:5;pointer-events:none;font-size:12px;color:#93a7bb;line-height:1.9}
#legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
#legend i.ring{background:transparent;border:1px solid #7ed4ff;border-radius:50%}
#tip{position:fixed;right:18px;bottom:16px;z-index:5;font-size:12px;color:#64788c;pointer-events:none}#evidence-card{position:fixed;left:18px;top:102px;width:236px;z-index:7;padding:11px;border-radius:10px;background:rgba(7,18,28,.78);backdrop-filter:blur(8px);border:1px solid rgba(122,162,198,.4);font-size:11.5px;color:#a9bfd2}#evidence-card b{color:#e0f2fe;display:block;margin-bottom:6px}#entity-card{position:fixed;right:18px;top:102px;width:250px;z-index:7;padding:12px;border-radius:10px;background:rgba(7,18,28,.82);backdrop-filter:blur(8px);border:1px solid rgba(122,162,198,.4);display:none}#entity-card b{color:#e0f2fe}#entity-card span{display:block;margin-top:6px;color:#a9bfd2;font-size:12px}#timeline{position:fixed;left:18px;right:18px;bottom:48px;z-index:6;display:flex;gap:6px;align-items:center;background:rgba(7,18,28,.84);padding:8px 10px;border-radius:10px;flex-wrap:wrap}#timeline button{background:#1a3042;color:#d7e4f0;border:1px solid #496579;border-radius:5px;padding:4px 8px;cursor:pointer}#timeline button.active{background:#1976a8}#timeline button.layer{background:#0f3a4a;color:#9fd8e8;border-color:#2b7f9f}#timeline button.layer.off{background:#1a3042;color:#5a7088;border-color:#3a4a58;text-decoration:line-through}#timeline #jump-buttons{display:flex;gap:4px;flex-wrap:wrap}#progress{flex:1;min-width:140px;accent-color:#4fc3f7;height:20px;cursor:pointer}#progress-time{font-size:11px;color:#8fb7d6;font-family:ui-monospace,monospace;min-width:44px;text-align:center}
canvas{display:block}
/* 322px 宽 + 内部滚动是对的，但预留量按 190px 算少了：卡片从 top:102 起，
   底边会撞上右下的「处置前后结果」面板（那里约占用屏幕底部 160px）。
   实拍确认泵站卡片有工单 + 4 条断言 + 责任部门 + 动作卡，滚动是常态，不是偶发。
   预留改 300px，卡片最高到 100vh-300，与底部面板留出空隙。 */
#entity-card{width:322px;max-height:calc(100vh - 300px);overflow:auto}
#entity-card::-webkit-scrollbar{width:6px}#entity-card::-webkit-scrollbar-thumb{background:#2c4459;border-radius:3px}
.ec-sec{margin-top:9px;padding-top:8px;border-top:1px solid rgba(122,162,198,.25)}
.ec-sec>small{display:block;color:#7b93aa;font-size:11px;margin-bottom:4px;letter-spacing:.4px}
.ec-row{display:flex;gap:8px;align-items:baseline;font-size:12px;color:#b6c9da;padding:2px 0}
.ec-row b{margin-left:auto;color:#dbe9f5;font-weight:600;white-space:nowrap}
.ec-card{font-size:11.5px;line-height:1.5;color:#a9bfd2;padding:5px 0;border-bottom:1px dashed rgba(122,162,198,.18)}
.ec-card em{color:#7b93aa;font-style:normal}
.badge{display:inline-block;font-size:10.5px;padding:1px 6px;border-radius:99px;border:1px solid #3d5568;color:#8fa3b5;background:rgba(60,80,100,.3);white-space:nowrap}
/* #entity-card span 是 display:block（详情正文要竖排），徽章必须压回去，
   否则"有据""待执行"这些标签会各占一整行 */
#entity-card .badge{display:inline-block;margin-top:0;width:auto;font-size:10.5px;color:#8fa3b5}
#entity-card .badge.b-active{color:#4fc3f7}#entity-card .badge.b-done{color:#6ee7a8}
#entity-card .badge.b-alert{color:#f5c063}
.badge.b-active{color:#4fc3f7;border-color:#2b7fa8;background:rgba(20,90,130,.35)}
.badge.b-done{color:#6ee7a8;border-color:#2f7a56;background:rgba(20,80,55,.35)}
.badge.b-alert{color:#f5c063;border-color:#8a6a2a;background:rgba(110,80,20,.35)}
.ec-empty{color:#64788c;font-size:11.5px}
#results-card{position:fixed;right:18px;bottom:92px;z-index:7;width:272px;max-height:30vh;overflow:auto;padding:10px 12px;border-radius:10px;background:rgba(7,18,28,.78);backdrop-filter:blur(8px);border:1px solid rgba(122,162,198,.4);font-size:11.5px;color:#a9bfd2}
#results-card>b{color:#e0f2fe;display:block;margin-bottom:6px;font-size:12px}
#results-card summary{cursor:pointer;color:#7ed4ff;font-size:12px;margin-bottom:4px}
.rc-row{padding:5px 0;border-bottom:1px dashed rgba(122,162,198,.18)}
.rc-row .v{color:#dbe9f5;font-weight:600}
.rc-row .v i{color:#6f8499;font-style:normal;font-weight:400}
.rc-row .n{display:block;color:#6f8499;font-size:10.5px;margin-top:2px;line-height:1.4}
.rc-basis{display:block;margin-top:3px;font-size:10.5px;color:#7b93aa}
.rc-basis .badge{display:inline-block;margin:2px 3px 0 0;font-size:10px;padding:0 5px}
.rc-basis.rc-none{color:#5d7085}
#jump-buttons button.settle{background:#1d4a3a;border-color:#3f8a6a}
#jump-buttons button.settle.active{background:#2f8a63}
#timeline .tl-sep{width:1px;height:22px;background:#2c4459;margin:0 2px}
#timeline button.final{background:#1d4a3a;color:#bfe8d2;border:1px solid #3f8a6a}
#timeline button.final.active{background:#2f8a63;color:#e8fff2}
#exec-card{position:fixed;left:18px;bottom:92px;z-index:7;width:272px;padding:10px 12px;border-radius:10px;background:rgba(7,18,28,.78);backdrop-filter:blur(8px);border:1px solid rgba(122,162,198,.4);font-size:11.5px;color:#a9bfd2}
#exec-card>b{color:#e0f2fe;display:block;margin-bottom:6px;font-size:12px}
#exec-card .delta-behind{color:#f5c063}
#hud small{display:block;margin-top:4px;font-size:11px;color:#8497a8}
@media(max-width:900px){#hud{max-width:calc(100vw - 36px);padding-right:12px;background:rgba(7,18,28,.82)}#event-bar{top:auto;bottom:84px;right:14px;padding:6px 12px}#event-bar b{font-size:13px}#event-bar span{font-size:11px}}</style>
<div id=hud><b>城市内涝三维数字孪生 · 写实沙盘</b><span>兴华路片区(A-03) 与 建设大道片区(B-01) · 坐标来自 scene.json 台账实时投影</span><small id=hud-src>正在加载关联层（动作卡 / 对象 / 证据）…</small></div>
<div id=event-bar><b>观测：事件识别</b><span>事件推演 · 第 1/6 跳</span></div>
<div id=legend><span><i style=background:#4a6fa5></i>积水段</span> &nbsp; <span><i style=background:#e0a965></i>已封路</span> &nbsp; <span><i style=background:#5a6a7a></i>泵站</span> &nbsp; <span><i style=background:#3a8fb0></i>排水管</span> &nbsp; <span><i style=background:#8bd8ff></i>地表汇流</span> &nbsp; <span><i style=background:#e0a33a></i>土壤下渗</span> &nbsp; <span><i style=background:#ff5a1f></i>瓶颈管段</span> &nbsp; <span><i style=background:#ff2d2d></i>故障告警</span> &nbsp; <span><i class=ring></i>本步落点</span></div>
<div id=evidence-card><b>当前跳次证据</b><span id=evidence-body>正在读取六跳证据…</span><br><a href="/" style="color:#7ed4ff">返回主页面事件台</a></div><div id=entity-card><b id=entity-title>对象详情</b><span id=entity-body>点击积水或泵站查看</span><div id=entity-links></div></div><div id=exec-card><b>执行账本（计划 vs 实际）</b><span id=exec-body>正在读取执行账本…</span></div><div id=results-card><b>处置前后结果</b><details id=results-details><summary id=results-summary>展开对照</summary><div id=results-body>正在读取…</div><div id=results-note style='margin-top:6px;color:#64788c;font-size:10.5px'></div></details></div><div id=timeline><button id=prev>‹</button><button id=play>暂停</button><button id=next>›</button><button id=restart>重播</button><button id=pipe-toggle class="layer off">管网：关</button><span id=jump-buttons></span><span class=tl-sep></span><button id=settle class=final>最终状态</button><input id=progress type=range min=0 max=1000 value=0 title="拖动到事件线任意时刻"><span id=progress-time>00:00</span></div><div id=tip>拖动旋转 · 滚轮缩放 · 右键平移</div>
<script src="/three.min.js"></script>
<script src="/RGBELoader.js"></script>
<script src="/OrbitControls.js"></script>
<script src="/GLTFLoader.js"></script>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let renderer,scene3,camera,controls,envTex=null;
let rain=null,rainGeo=null,rainSpeed=null;
const K=0.16,CX=350,CY=165;   // 投影缩放：1 场景单位(米) → 0.16 世界单位，即 1 世界单位 = 6.25 m
function W(x,y){return[(x-CX)*K,(y-CY)*K]}
// ---- 材质加载：一次加载、repeat 平铺、缓存复用 ----
const texCache={};
function loadTex(name){if(texCache[name])return texCache[name];
  const t=new THREE.TextureLoader().load('/assets/tex/'+name+'.jpg');
  t.wrapS=t.wrapT=THREE.RepeatWrapping;t.encoding=THREE.sRGBEncoding;return texCache[name]=t}
function stdMat(name,extra){const m=new THREE.MeshStandardMaterial(Object.assign({map:loadTex(name+'_diffuse'),roughnessMap:loadTex(name+'_rough'),normalMap:loadTex(name+'_normal'),roughness:.95,metalness:.02},extra||{}));return m}
// BoxGeometry 的每个面都把 UV 归一化到 0..1：一段 100 世界单位长的路面只有一次贴图，
// 纹理被沿路方向拉成一条条平行丝——远景看不出来，相机一拉近就是"地毯拉丝"。
// 这里只改顶面/底面的 UV，让贴图按世界尺寸平铺；tile 是一个纹理周期的世界长度。
// r128 的 BoxGeometry 顶点顺序固定为 px,nx,py,ny,pz,nz，每面 4 个顶点，py/ny 是 8..15。
function tileBoxTop(geo,su,sv){
  const uv=geo.attributes.uv;
  for(let i=8;i<16;i++){uv.setXY(i,uv.getX(i)*su,uv.getY(i)*sv);}
  uv.needsUpdate=true;
  return geo;
}
// three r128 没有颜色管理：color 的十六进制被当作线性值直接进光照，输出时再编码一次 sRGB，
// 等效把颜色整体提亮近一倍（131 的中灰会渲成 224 的白）。所有纯色材质都应经这里转线性。
function srgbHex(h){return new THREE.Color(h).convertSRGBToLinear()}
// 写实楼栋：混凝土立面，UV 按楼面大小平铺让窗户肌理自然
// 程序化楼栋立面：Canvas 画窗户格子，生成一张可平铺的立面贴图
let __facadeTex=null;
function facadeTex(){if(__facadeTex)return __facadeTex;
  const cv=document.createElement('canvas');cv.width=256;cv.height=512;const g=cv.getContext('2d');
  // 墙体底色
  g.fillStyle='#8a9096';g.fillRect(0,0,256,512);
  // 横向楼层分隔
  for(let y=0;y<512;y+=64){g.fillStyle='#6d7379';g.fillRect(0,y,256,3)}
  // 竖向柱
  for(let x=0;x<256;x+=64){g.fillStyle='#7a8086';g.fillRect(x,0,3,512)}
  // 窗户（每格一个，深色玻璃带微反光）
  for(let y=16;y<512;y+=64){for(let x=16;x<256;x+=64){
    g.fillStyle='#2a3a4a';g.fillRect(x,y,40,40);       // 玻璃
    g.fillStyle='#1d2834';g.fillRect(x,y,40,6);        // 上沿阴影
    g.fillStyle='rgba(180,210,230,.35)';g.fillRect(x+6,y+8,12,24); // 反光
  }}
  const t=new THREE.CanvasTexture(cv);t.wrapS=t.wrapT=THREE.RepeatWrapping;t.encoding=THREE.sRGBEncoding;t.anisotropy=4;
  return __facadeTex=t}
function makeBuilding(w,h,d,x,z,rotY){
  const grp=new THREE.Group();
  // 主体
  const ft=facadeTex().clone();ft.needsUpdate=true;
  // 统一现代城市立面材质，但用确定性的冷灰色阶区分楼栋，避免整片楼体像复制的积木
  const facadeColors=[0xd8dde0,0xc8d0d4,0xb9c3c8,0xe0e1dc];
  const facadeColor=facadeColors[Math.abs(Math.round(x*0.07+z*0.11))%facadeColors.length];
  const mat=new THREE.MeshStandardMaterial({color:facadeColor,map:ft,roughness:.82,metalness:.06});
  const b=new THREE.Mesh(new THREE.BoxGeometry(w,h,d),mat);b.castShadow=true;b.receiveShadow=true;
  b.position.y=h/2;grp.add(b);
  ft.repeat.set(Math.max(1,Math.round(w/1.6)),Math.max(1,Math.round(h/1.5)));
  // 裙楼基座：底部一圈深色体量，打破"纯积木"感
  const base=new THREE.Mesh(new THREE.BoxGeometry(w*1.06,0.7,d*1.06),
    new THREE.MeshStandardMaterial({color:0x5c646c,roughness:.75,metalness:.1}));
  base.position.y=0.35;base.castShadow=true;base.receiveShadow=true;grp.add(base);
  // 楼层腰线：每隔约 1.5 单位高加一圈浅色横条，增强立面横向节奏
  const nLines=Math.max(1,Math.floor(h/1.5));
  for(let i=1;i<nLines;i++){
    const line=new THREE.Mesh(new THREE.BoxGeometry(w+0.02,0.06,d+0.02),
      new THREE.MeshStandardMaterial({color:0xc4ccd2,roughness:.7}));
    line.position.y=i*1.5;grp.add(line);
  }
  // 屋顶女儿墙（收在顶部，深色）
  const cap=new THREE.Mesh(new THREE.BoxGeometry(w*1.02,0.16,d*1.02),
    new THREE.MeshStandardMaterial({color:0x3a424a,roughness:.9}));
  cap.position.y=h+0.08;cap.castShadow=true;grp.add(cap);
  // 屋顶设备：水箱/空调外机几个小方块，让屋顶不死板
  const roofMat=new THREE.MeshStandardMaterial({color:0x8a929a,roughness:.6,metalness:.3});
  for(let k=0;k<2;k++){
    const rw=0.8+Math.abs(Math.sin(w*7+k*3))*0.6, rd=0.6, rh=0.5;
    const box=new THREE.Mesh(new THREE.BoxGeometry(rw,rh,rd),roofMat);
    box.position.set((k===0?-1:1)*w*0.22,h+0.4,k===0?-d*0.1:d*0.12);box.castShadow=true;grp.add(box);
  }
  grp.position.set(x,0.37,z);if(rotY)grp.rotation.y=rotY;
  scene3.add(grp);return grp}
// 行道树：分叉树干 + 双层树冠
function makeTree(x,z,s){
  const g=new THREE.Group();
  const trunkMat=new THREE.MeshStandardMaterial({color:0x5a4632,roughness:.95});
  const trunk=new THREE.Mesh(new THREE.CylinderGeometry(0.22*s,0.34*s,1.6*s,8),trunkMat);
  trunk.position.y=0.8*s;trunk.castShadow=true;g.add(trunk);
  const leafMat=new THREE.MeshStandardMaterial({color:0x2e5b36,roughness:.9});
  const leafMat2=new THREE.MeshStandardMaterial({color:0x3a6b42,roughness:.9});
  [[0,1.7*s,0.9*s,leafMat2],[0.4*s,2.2*s,0.65*s,leafMat],[-0.3*s,2.3*s,0.55*s,leafMat2]].forEach(c=>{
    const b=new THREE.Mesh(new THREE.SphereGeometry(c[2],12,10),c[3]);b.position.set(c[0],c[1],0);b.castShadow=true;g.add(b)});
  g.position.set(x,0.37,z);scene3.add(g);return g}
// 路灯：杆 + 弯臂 + 暖光
function makeLamp(x,z,rotY){
  const g=new THREE.Group();
  const poleMat=new THREE.MeshStandardMaterial({color:0x2a3038,roughness:.6,metalness:.6});
  const pole=new THREE.Mesh(new THREE.CylinderGeometry(0.09,0.12,4.6,10),poleMat);pole.position.y=2.3;pole.castShadow=true;g.add(pole);
  const arm=new THREE.Mesh(new THREE.BoxGeometry(0.06,0.06,1.0),poleMat);arm.position.set(0,4.4,0.5);g.add(arm);
  const head=new THREE.Mesh(new THREE.SphereGeometry(0.22,10,8),new THREE.MeshStandardMaterial({color:0xfff2cc,emissive:0xffc766,emissiveIntensity:1.4}));
  head.position.set(0,4.4,1.0);g.add(head);
  g.position.set(x,0.37,z);if(rotY)g.rotation.y=rotY;scene3.add(g);return g}
// 车辆：车身+车窗+轮
function makeCar(x,z,rotY,color){
  const g=new THREE.Group();
  const body=new THREE.Mesh(new THREE.BoxGeometry(1.7,0.55,0.85),new THREE.MeshStandardMaterial({color:color||0x8899aa,roughness:.35,metalness:.7}));
  body.position.y=0.55;body.castShadow=true;g.add(body);
  const cabin=new THREE.Mesh(new THREE.BoxGeometry(0.9,0.42,0.78),new THREE.MeshStandardMaterial({color:0x2a3642,roughness:.2,metalness:.9}));
  cabin.position.set(-0.1,0.98,0);cabin.castShadow=true;g.add(cabin);
  [[-0.55,0.3],[0.55,0.3],[-0.55,-0.3],[0.55,-0.3]].forEach(p=>{
    const w=new THREE.Mesh(new THREE.CylinderGeometry(0.26,0.26,0.22,12),new THREE.MeshStandardMaterial({color:0x111,roughness:.8}));
    w.rotation.z=Math.PI/2;w.position.set(p[0],0.26,p[1]);g.add(w)});
  g.position.set(x,0.37,z);if(rotY)g.rotation.y=rotY;scene3.add(g);return g}
function init(){
  renderer=new THREE.WebGLRenderer({antialias:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));renderer.setSize(innerWidth,innerHeight);
  renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;renderer.outputEncoding=THREE.sRGBEncoding;renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=0.82;
  document.body.appendChild(renderer.domElement);
  scene3=new THREE.Scene();window.__scene3=scene3;   // 挂全局，便于在控制台/自动化里核查场景对象
  scene3.background=new THREE.Color(0x1a2028);
  // 雾只做极轻的空气感，不吞掉街区；起止距离拉到场景尺寸之外，远景保持通透可读。
  scene3.fog=new THREE.Fog(0x6a7684,150,430);
  camera=new THREE.PerspectiveCamera(46,innerWidth/innerHeight,0.1,500);
  // 镜头聚焦主事件区（A-03 兴华路积水 + 故障泵站 + 封路点），让一次事件的所有对象集中在同一画面内
  // 镜头对准 A-03 主事件区（积水最深处 + 故障泵站），而不是只对着泵站：
  // 原来 target 在场景 x≈294（泵站位置），积水面在 x≈200，水被挤到画面左下角，
  // 读起来像「事故在别处」。现在取两者之间（x≈235），并拉近到 28 世界单位，
  // 从东南上空看西北，避开西南街区的高楼遮挡。
  camera.position.set(-6,15.6,20.6);
  // minDistance 必须小于特写距离：OrbitControls 的 update() 会把相机距离夹在
  // [minDistance,maxDistance] 里，写 12 时任何 12 以内的特写都被顶回 12 世界单位——
  // 表现就是"特写参数怎么改画面都不变"。特写降到 1.75 后这里必须同步放到 1.0，
  // 否则上面改多少都会被夹回 3.0。
  controls=new THREE.OrbitControls(camera,renderer.domElement);controls.target.set(-18,0.6,0.6);controls.maxPolarAngle=1.42;controls.minDistance=1.0;controls.maxDistance=160;controls.enableDamping=true;controls.dampingFactor=0.08;
  // 调试/核查用俯视视角：地址加 #top 直接看平面投影（检查水面范围、楼栋退线是否压路）
  if(location.hash==='#top')camera.position.set(controls.target.x,46,controls.target.z+1);
  controls.update();
  // 阴雨天空 HDR + 环境反射
  new THREE.RGBELoader().setDataType(THREE.UnsignedByteType).load('/assets/hdr/cloudy.hdr',function(hdr){
    hdr.mapping=THREE.EquirectangularReflectionMapping;
    const pmrem=new THREE.PMREMGenerator(renderer);envTex=pmrem.fromEquirectangular(hdr).texture;
    // environment 只做反射；背景不能直接用这张 HDR ——
    // r128 里没有 scene.backgroundIntensity（r152 才加），设了是空操作，
    // 天空会以 HDR 全亮度铺满画面上缘，一片亮灰白，后处理一叠 bloom 整屏泛雾。
    // 所以背景改用暮色蓝灰纯色，云层纹理留在 environment 里参与水面/金属反射。
    scene3.environment=envTex;
    scene3.background=new THREE.Color(0x27313c);
    pmrem.dispose();
    // 天空原图留着不 dispose：水面 shader 要拿它做 equirect 反射采样。
    // 之前只有 fresnel 混一个常数天空色，不管怎么调都像"地上摊了一层灰雾"——
    // 水面之所以像水，靠的是把天光/楼影按视角反射出来，不是刷个蓝灰色。
    window.__skyTex=hdr;
    (window.__waterMeshes||[]).forEach(function(w){
      const u=w.surface&&w.surface.material&&w.surface.material.uniforms;
      if(u&&u.uSky){u.uSky.value=hdr;u.uHasSky.value=1;w.surface.material.needsUpdate=true;}
    });
  });
  // 阴天柔和光照：半球光 + 弱方向光
  // 天空压暗后必须同步降光照，否则地面比天空亮一档，看起来像"夜里打了探照灯"。
  // 降幅比天空小（0.55→0.44）：地面仍要读得清，只是不再有正午的亮度。
  scene3.add(new THREE.HemisphereLight(0x8796a8,0x242c1e,0.44));
  const sun=new THREE.DirectionalLight(0xc6cfd8,0.28);sun.position.set(40,60,25);sun.castShadow=true;sun.shadow.mapSize.set(2048,2048);sun.shadow.bias=-0.00035;sun.shadow.normalBias=0.025;
  const sc=sun.shadow.camera;sc.left=-70;sc.right=70;sc.top=70;sc.bottom=-70;sc.far=200;scene3.add(sun);
  // 城市基底：铺装大平面。纯色一律经 srgbHex() 转线性，见该函数上方说明。
  const baseMat=new THREE.MeshStandardMaterial({color:srgbHex(0x787c7f),map:loadTex('paving_diffuse'),roughnessMap:loadTex('paving_rough'),normalMap:loadTex('paving_normal'),roughness:.94,metalness:.02,envMapIntensity:.35});
  const base=new THREE.Mesh(new THREE.PlaneGeometry(500,500),baseMat);window.__base3=base;
  base.rotation.x=-Math.PI/2;base.position.y=-0.02;base.receiveShadow=true;
  // 地面贴图按世界尺寸平铺（一块纹理 ≈2 世界单位 ≈12.5m）。
  // 原来靠 texture.repeat 设 60，而 texture 是和人行道共用的——一改就把人行道的 UV 也带偏，
  // 所以这里改几何 UV；贴图 repeat 留给各处自己算，不再全局写。
  (function(){const uv=base.geometry.attributes.uv;
    for(let i=0;i<uv.count;i++)uv.setXY(i,uv.getX(i)*250,uv.getY(i)*250);
    uv.needsUpdate=true;})();
  scene3.add(base);
  fetch('/api/scheduler').then(r=>r.json()).then(build);
  fetch('/api/execution').then(r=>r.json()).then(renderExec).catch(()=>{const el=document.getElementById('exec-body');if(el)el.textContent='执行账本不可用（/api/execution 未能加载）';});
  addEventListener('resize',()=>{camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight);resizePost()});
  // 雨丝粒子
  makeRain();
  initReflection();
  initPost();
  renderer.setAnimationLoop(tick);
  bindUI();
  // ---- 场景自检入口：地址加 #audit 或 ?audit=1，每 1.5 秒刷新一次 ----
  // 输出水面 blob / 水面平面 / 道路实际宽度，排查几何问题时看数，不靠截图目测。
  if(location.hash==='#audit'||/[?&]audit=1/.test(location.search)){
    const pre=document.createElement('pre');pre.id='audit';
    pre.style.cssText='position:fixed;left:0;top:0;z-index:9999;background:#fff;color:#000;font:11px/1.4 monospace;max-height:100%;overflow:auto';
    document.body.appendChild(pre);
    const collect=function(){
      const L=[];
      (window.__waterMeshes||[]).forEach(function(w){
        const s=w.surface,g=s.geometry.parameters,u=s.material.uniforms;
        L.push('WATER|'+w.label+'|plane='+g.width.toFixed(2)+'x'+g.height.toFixed(2)
          +'|pos='+s.position.x.toFixed(2)+','+s.position.z.toFixed(2)+'|y='+s.position.y.toFixed(2));
        for(let i=0;i<8;i++){const v=u['uB'+i].value;
          if(v.z>0.01)L.push('BLOB|'+w.label+'|x='+v.x.toFixed(2)+' z='+v.y.toFixed(2)+' rx='+v.z.toFixed(2)+' rz='+v.w.toFixed(2));}
      });
      if(window.__scene3)window.__scene3.traverse(function(o){
        if(o.userData&&o.userData.kind==='road'&&o.geometry&&o.geometry.parameters)
          L.push('ROAD|'+o.userData.entityId+'|len='+o.geometry.parameters.width.toFixed(2)+'|wide='+o.geometry.parameters.depth.toFixed(2)+'|y='+o.position.y.toFixed(2));
        // 泵站/设施的实际占地：用来核对它有没有压到车行道（比看图可靠）
        if(o.userData&&o.userData.kind==='pump'&&o.children.length){
          const bb=new THREE.Box3().setFromObject(o);
          L.push('PUMP|'+o.userData.entityId
            +'|pos='+o.position.x.toFixed(2)+','+o.position.z.toFixed(2)
            +'|bboxX='+bb.min.x.toFixed(2)+'..'+bb.max.x.toFixed(2)
            +'|bboxZ='+bb.min.z.toFixed(2)+'..'+bb.max.z.toFixed(2));
        }
      });
      // 光环：正常情况下同时只该有 1~2 个（本步落点 + 选中对象），多了就是又满屏淡圈了
      // 同时输出投影后的屏幕像素坐标，用来在截图上定点核对"这个环到底画在哪儿、画没画出来"
      Object.keys(RINGS||{}).forEach(function(id){
        const fx=RINGS[id];if(!fx||!fx.ring.visible)return;
        const pv=fx.ring.position.clone().project(camera);
        const sx=Math.round((pv.x*0.5+0.5)*window.innerWidth),sy=Math.round((-pv.y*0.5+0.5)*window.innerHeight);
        L.push('RING|'+id+'|pos='+fx.ring.position.x.toFixed(2)+','+fx.ring.position.z.toFixed(2)
          +'|r='+fx.ring.geometry.parameters.outerRadius.toFixed(2)
          +'|op='+fx.ring.material.opacity.toFixed(2)
          +'|screen='+sx+','+sy+'|order='+fx.ring.renderOrder);
      });
      // 时序状态：堵塞标记/封路/车道线/水位，都是"该不该在场"的问题，用数值核对最快
      (function(){
        const cm=window.__clogMarks||[],ld=window.__laneDashes||[],rl=window.__roadLabels||[];
        L.push('SEQ|clogShown='+cm.filter(m=>m.group.visible).length+'/'+cm.length
          +'|lanesHidden='+ld.filter(g=>!g.visible).length+'/'+ld.length
          +'|labelBlocked='+rl.filter(r=>r.sprite.material.map===r.blocked).length+'/'+rl.length);
        // 现场处置到哪一步：车是否到场、人是否下车、锥桶摆了几个、泵是否接上
        const cw=window.__roadCrew;
        if(cw)L.push('CREW|truck='+((cw.truck&&cw.truck.visible)?1:0)
          +'|crew='+(cw.group.visible?1:0)
          +'|cones='+(cw.cones||[]).filter(g=>g.visible).length+'/4'
          +'|pump='+((cw.pump&&cw.pump.visible)?1:0)
          +'|pos='+cw.group.position.x.toFixed(2)+','+cw.group.position.z.toFixed(2));
        (window.__waterMeshes||[]).forEach(function(w){
          const u=w.surface&&w.surface.material&&w.surface.material.uniforms;
          if(u&&u.uLevel)L.push('LEVEL|'+w.label+'|lvl='+u.uLevel.value.toFixed(2)
            +'|grow='+(0.42+0.58*u.uLevel.value).toFixed(2)+'|vis='+w.surface.visible);
        });
        // 场景构成统计：按「几何类型 + 尺寸 + 材质色」分组计数。
        // 排查「路面上那排暗纹是什么」这类问题时，肉眼和射线都容易误判
        // （raycaster 命中的是穿透路径上最近的物体，不代表最终渲染在上层的那个），
        // 直接看哪一类 mesh 的数量级能对上纹路条数，比猜快得多。
        const grp={};
        scene3.traverse(function(o){
          if(!o.isMesh||!o.geometry||!o.visible)return;
          const g=o.geometry,p=g.parameters||{};
          let dim='';
          if(p.width!=null)dim=Math.round(p.width*100)/100+'x'+Math.round((p.height||0)*100)/100+'x'+Math.round((p.depth||0)*100)/100;
          else if(p.radiusTop!=null)dim='cyl'+p.radiusTop+'x'+Math.round((p.height||0)*100)/100;
          else if(p.radius!=null)dim='sph'+p.radius;
          const col=(o.material&&o.material.color)?o.material.color.getHexString():'-';
          const k=g.type+'['+dim+'|#'+col+'|ro'+(o.renderOrder||0)+']';
          grp[k]=(grp[k]||0)+1;
        });
        Object.entries(grp).sort(function(a,b){return b[1]-a[1]}).slice(0,12)
          .forEach(function(e){L.push('MESH|'+e[1]+'x '+e[0])});
        // ?paint=1：把所有「深色材质」一次性染成品红。
        // 排查「画面上那排暗纹是谁」时，逐个排除太慢（每轮要重启截图一分钟），
        // 一次染色能把所有候选同时点亮，看哪块变色就知道上层是谁。
        if(new URLSearchParams(location.search).get('paint')==='1'){
          const seen=new Set();let cnt=0;
          scene3.traverse(function(o){
            if(!o.isMesh||!o.material||!o.material.color)return;
            if(seen.has(o.material))return;seen.add(o.material);
            const c=o.material.color;
            // 线性空间亮度阈值：0x242a30 这类近黑约 0.07，路面沥青约 0.33
            if(c.r+c.g+c.b<0.22){c.setHex(0xff00ff);o.material.needsUpdate=true;cnt++}
          });
          L.push('PAINT|深色材质已染品红 '+cnt+' 个');
        }
        if(new URLSearchParams(location.search).get('hide')==='base'&&window.__base3){
          window.__base3.visible=false;L.push('HIDE|基底平面已隐藏');
        }
        // ?water=1：强制积水面不透明并染成品红，用来确认「水面到底画在哪、有没有画出来」。
        // 水面是自定义 ShaderMaterial，没有 material.color，paint=1 覆盖不到它。
        if(new URLSearchParams(location.search).get('water')==='1'){
          let nw=0;
          (window.__waterMeshes||[]).forEach(function(w){
            const m=w.surface&&w.surface.material,u=m&&m.uniforms;
            if(!u)return;
            if(u.uOpacity)u.uOpacity.value=1;
            if(u.uWaterColor)u.uWaterColor.value.setHex(0xff00ff);
            if(u.uDeepColor)u.uDeepColor.value.setHex(0xff00ff);
            w.surface.visible=true;w.surface.renderOrder=260;nw++;
            L.push('WATERDBG|'+w.label+'|pos='+w.surface.position.x.toFixed(1)+','
              +w.surface.position.y.toFixed(2)+','+w.surface.position.z.toFixed(1)
              +'|vis='+w.surface.visible+'|op='+u.uOpacity.value
              +'|hasSky='+(u.uHasSky?u.uHasSky.value:'-'));
          });
          L.push('WATERDBG|共 '+nw+' 片');
        }
      })();
      // 命中测试：给屏幕相对坐标，返回命中的对象，用来定位「屏幕上那块斑到底是什么」
      // 用法：?audit=1&probe=0.40,0.28 （相对视口宽高的比例，可多组用 ; 分隔）
      (function(){
        const q=new URLSearchParams(location.search);
        const spec=q.get('probe');if(!spec)return;
        spec.split(';').forEach(function(pair){
          const uv=pair.split(',').map(Number);if(uv.length<2)return;
          const rc=new THREE.Raycaster();
          rc.setFromCamera(new THREE.Vector2(uv[0]*2-1,-uv[1]*2+1),camera);
          const hits=rc.intersectObjects(scene3.children,true).slice(0,4);
          L.push('PROBE|'+uv[0]+','+uv[1]+'|'+hits.map(function(x){
            const u=x.object.userData||{};
            // 带上几何参数与材质色：只给 kind/type 时，「没有 userData 的装饰性 Box」
            // 会全部显示成 Mesh:BoxGeometry，定位不到是哪一个（踩过：路面暗纹查了三轮）。
            const g=x.object.geometry||{},pr=g.parameters||{};
            const dim=(pr.width!=null)?(pr.width+'x'+pr.height+'x'+(pr.depth!=null?pr.depth:'')):'';
            const col=(x.object.material&&x.object.material.color)?x.object.material.color.getHexString():'';
            return (u.kind||u.name||x.object.type)+':'+g.type+'['+dim+'|#'+col+']@'+x.distance.toFixed(1);
          }).join(' , '));
        });
      })();
      return L.join(String.fromCharCode(10));
    };
    setInterval(function(){pre.textContent=collect()},1500);
  }
}
// 执行账本摘要：计划（推演派生）vs 实际（账本），补到沙盘左下角，不再只存在于主页面
function renderExec(d){
  const el=document.getElementById('exec-body');if(!el)return;
  if(!d||d.status!=='ok'){el.textContent='执行账本不可用';return;}
  const p=d.plan||{},c=d.counts||{};
  const cards=d.cards||[];                    // 接口返回的是数组，不是字典
  const byId={};(cards.forEach?cards:[]).forEach(function(x){if(x&&x.card_id)byId[x.card_id]=x});
  window.__execBehind=p.behind||[];           // 差额卡 id，结算按钮要用
  const delta=p.delta==null?'—':(p.delta>0?'+'+p.delta:p.delta);
  const deltaCls=p.delta<0?'delta-behind':(p.delta>0?'':'');
  // 差额归因：从 cards 的 state / co_sign_required / signatures 里读，不猜。
  // 演示环境只有一个身份（服务端固定 ORG-EMERG），别的部门会签签不上，
  // 所以"实际完成 0"是**会签未完成**，不是账本坏了——这句话不写出来，看画面的人会以为系统出错。
  let running=0,needSign={};
  (p.behind||[]).forEach(function(id){
    const x=byId[id];if(!x)return;
    if(x.state==='running')running++;
    (x.co_sign_required||[]).forEach(function(o){
      if(!(x.signatures||{})[o])needSign[o]=(needSign[o]||0)+1;
    });
  });
  const signTxt=Object.keys(needSign).map(function(o){return o+' ×'+needSign[o]}).join(' · ');
  el.innerHTML='<div>计划完成 <b>'+p.planned_done+'</b> 张 · 账本实际完成 <b>'+p.actual_done+'</b> 张 · 偏差 <b class="'+deltaCls+'">'+delta+'</b></div>'
    +'<div style="margin-top:4px;color:#7b93aa;font-size:10.5px">账本状态：待执行 '+(c.pending==null?'-':c.pending)
      +' · 执行中 '+(c.running||0)+' · 已完成 '+(c.done||0)+' · 待会签 '+(c.needs_confirm||0)+'</div>'
    +(signTxt?'<div style="margin-top:3px;color:#c9a86a;font-size:10px">差额卡待会签：'+signTxt+'（演示环境只有 ORG-EMERG 一个身份，会签签不上）</div>':'')
    +'<div style="margin-top:3px;color:#64788c;font-size:10px">'+(d.source||'账本只记动作卡执行，不改冻结的 work_orders.csv')+'</div>'
    +((p.actual_done<p.planned_done&&window.__execBehind.length)
      ? '<button id="exec-settle" style="margin-top:5px;padding:3px 8px;font-size:10px;cursor:pointer;'
        +'background:#20303f;color:#9fd0ee;border:1px solid #35516a;border-radius:3px">'
        +'演示：按接口推进 '+window.__execBehind.length+' 张差额卡</button>'
      : '');
  const btn=document.getElementById('exec-settle');
  if(btn)btn.onclick=settleExecDemo;
}
// 演示推进：逐张读最新卡片状态，按状态机走一步（pending→running，
// running 且会签已齐则可到 done，否则停在 running）。
// 每一步都用接口刚返回的 version 做乐观锁——写死 0 会从第二张开始就 409（第一版踩过）。
async function settleExecDemo(){
  const ids=(window.__execBehind||[]).slice();
  const btn=document.getElementById('exec-settle');
  if(!ids.length)return;
  if(btn){btn.disabled=true;btn.textContent='推进中…';}
  let ok=0,err=0,lastErr='';
  const pull=async function(){const r=await fetch('/api/execution');return await r.json()};
  for(const id of ids){
    for(let step=0;step<4;step++){
      let d0;
      try{d0=await pull()}catch(e){err++;lastErr=String(e);break;}
      const arr=d0.cards||[];
      const card=(arr.forEach?arr:[]).filter(function(x){return x&&x.card_id===id})[0];
      if(!card||card.state==='done'||card.state==='cancelled')break;
      let body;
      if(card.state==='pending'){
        body={to_state:'running'};
      }else if(card.state==='running'||card.state==='needs_confirm'){
        const miss=(card.co_sign_required||[]).filter(function(o){return !((card.signatures||{})[o])});
        if(miss.length)break;               // 会签缺人：停在当前状态，交给人去签
        body={to_state:'done'};
      }else break;
      try{
        const r=await fetch('/api/execution/events',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({event_id:'DEMO-'+id+'-'+card.state+'-'+Date.now(),
            card_id:id,action:'transition',to_state:body.to_state,
            expected_version:card.version||0,
            note:'演示：沙盘按接口推进动作卡（非现场回传）'})});
        const j=await r.json();
        if(j&&j.status==='error'){err++;lastErr=j.error||'';break;}
        ok++;
      }catch(e){err++;lastErr=String(e);break;}
    }
  }
  try{
    const r=await fetch('/api/execution');const d=await r.json();renderExec(d);
    const line=document.createElement('div');
    line.style.cssText='margin-top:4px;font-size:10px;color:'+(err?'#e0a06a':'#7fc99a');
    line.textContent='演示推进：成功 '+ok+' 步'+(err?(' · 失败 '+err+' 步'+(lastErr?('（'+lastErr+'）'):'')):'');
    const el=document.getElementById('exec-body');
    if(el)el.appendChild(line);
  }catch(e){}
}
function makeRain(){
  // 雨丝画成斜线段（LineSegments），不用 Points：Points 是"一颗颗圆点"，
  // 在这个机位下只是一层灰噪点，读不出在下雨。
  // 雨滴长度 0.20~0.54 世界单位（≈1.3~3.4m 的运动模糊），下落带轻微西斜。
  const N=3000;
  const pos=new Float32Array(N*6);
  const speed=new Float32Array(N);
  for(let i=0;i<N;i++){
    const x=(Math.random()-0.5)*150, y=Math.random()*48-2, z=(Math.random()-0.5)*150;
    const L=0.20+Math.random()*0.34;
    pos[i*6]  =x;          pos[i*6+1]=y;    pos[i*6+2]=z;
    pos[i*6+3]=x+0.05*L;   pos[i*6+4]=y-L;  pos[i*6+5]=z;
    speed[i]=14+Math.random()*8;      // 世界单位/秒（≈0.34s 穿过 1 层楼高，比真实雨滴快，属视觉惯例）
  }
  rainGeo=new THREE.BufferGeometry();
  rainGeo.setAttribute('position',new THREE.BufferAttribute(pos,3));
  rainSpeed=speed;
  const m=new THREE.LineBasicMaterial({color:0xdcebf7,transparent:true,opacity:0.55,depthWrite:false});
  rain=new THREE.LineSegments(rainGeo,m);rain.frustumCulled=false;scene3.add(rain)}
function build(sched){
  twinState.scheduler=sched;
  const ents=(sched.scene&&sched.scene.entities)||[];
  const roads=ents.filter(e=>e.kind==='road'),water=ents.filter(e=>e.kind==='water'),fac=ents.filter(e=>e.kind==='facility'),dist=ents.filter(e=>e.kind==='district');
  // 规划路网：以两个片区中心为锚的正交网格（主干贯穿、次干连接、支路切分街区）
  // 坐标统一为「场景坐标」，A-03 中心(199,161)、B-01 中心(591,161)，东西并列
  const TWIN3D_ROADS={
    // 东西向主干（贯穿两片区）：环城北路(北) + 兴华路-工业大道走廊(中) + 民生街(南)
    'R-104':[[70,70],[720,70]],   // 环城北路(主干)
    'R-101':[[70,160],[525,160]], // 兴华路(次干,已封)
    'R-202':[[525,160],[720,160]],// 工业大道(主干)
    'R-103':[[100,250],[700,250]],// 民生街(支路)
    // 南北向：建设大道(主干,贯穿A-03) + 滨河路(次干,贯穿B-01) + 支路
    'R-102':[[200,40],[200,280]], // 建设大道(主干)
    'R-201':[[590,40],[590,280]], // 滨河路(次干,已封)
    'R-105':[[360,160],[360,250]],// 兴华支路(支路)
  };
  const TWIN3D_FAC={'DF-PS-01':[285,174],'DF-PS-02':[228,231],'DF-IN-101':[272,155],'DF-IN-103':[120,146],'DF-IN-201':[633,146],'DF-IN-202':[526,176],'DF-PS-03':[560,105]};
  // DF-IN-101 位置说明：台账只给了"兴华路雨水口组"没有实测坐标，原来取的 (258,78) 落在环城北路，
  // 离 A-03 积水点近 90 个场景单位——"堵了雨水口导致这个路口积水"在画面上根本对不上。
  // 按就近原则摆到兴华路南侧路缘 (272,155)，与积水点、封路点在同一处。
  // 积水范围：沿道路方向加宽，让远景下是「一片积水」而非「一条细线」。
  // 深度 cm 是台账真实值（标注数字不改），面积/边界是示意范围（台账无精确多边形）。
  const TWIN3D_WATER={'WO-20261029-007':[[240,150],[340,150],[340,172],[240,172]],'WO-20261029-011':[[580,96],[610,96],[610,226],[580,226]]};
  // ---- 地下排水管网：台账真实字段（pipeline_ledger.csv），几何沿道路中线「示意」布设 ----
  // 台账只登记 road_id + 埋深 + 管径，无实测管线坐标 → 空间位置按绑定道路中线派生，非测绘。
  // pts 为场景坐标折线（沿道路中线），端点尽量贴到该路对应雨水口/泵站节点，形成「雨水口→管段→泵站」汇流路径。
  const TWIN3D_PIPES=[
    {id:'PL-DRN-101',road:'R-101',label:'兴华路排水管',depth_m:2.8,diameter_mm:600,material:'concrete',install_year:2009,status:'in_service',note:'按现行标准重现期不足，管径偏小',pts:[[120,160],[285,160],[525,160]]},
    {id:'PL-DRN-102',road:'R-102',label:'建设大道排水干管',depth_m:3.0,diameter_mm:1000,material:'concrete',install_year:2015,status:'in_service',note:'主干管',pts:[[200,160],[228,231]]},
    {id:'PL-DRN-201',road:'R-201',label:'滨河路排水管',depth_m:2.6,diameter_mm:800,material:'concrete',install_year:2018,status:'in_service',note:'重现期达标',pts:[[590,160],[590,40]]},
  ];
  window.TWIN3D_PIPES=TWIN3D_PIPES;   // 供全局 renderPipeCard 读取（管段不在 twin-links 投影里）
  window.PIPE_MAT_CN={concrete:'混凝土',steel:'钢管',pvc:'PVC',pe:'PE'};
  const PIPE_MAT_CN=window.PIPE_MAT_CN;
  const netRoads=roads.map(function(e){return{id:e.id,label:e.label,detail:e.detail||'',pts:(TWIN3D_ROADS[e.id]||String(e.points||'').split(' ').map(function(p){return p.split(',').map(Number)}))}});
  const seeded=(function(){let s=20260801;return function(){s=(s*1664525+1013904223)>>>0;return s/4294967296}})();
  // ---- 道路：沥青 PBR + 车道标线 + 路缘 + 斑马线 ----
  // 车行道压暗一档（0x6a7076 亮 20%）：暮色场景里浅灰沥青会让整张画面发白，
  // 而且和积水拉不开——水面只能靠"比路面暗"来表达，路面越亮，水就得越黑，最后读成阴影。
  const roadMat=stdMat('asphalt',{roughness:.88});roadMat.color.set(0x565c64);
  const laneMat=new THREE.MeshBasicMaterial({color:0xe8e4d8});
  // 封路是第 5 跳才发生的事件，不是场景固有状态：车道线一律先画，封路时再隐藏；
  // 路名标注同样平时用常态色，封路后才换成橙色（换成橙色 = 图例里的「已封路」）。
  window.__laneDashes=[];window.__roadLabels=[];
  const curbMat=new THREE.MeshStandardMaterial({color:srgbHex(0x6f767c),roughness:.7,envMapIntensity:.35});
  // 人行道同按 srgbHex 转换（原 0xb7b9b6 不转会渲成白条，把街区切成网格线）
  const sidewalkMat=stdMat('paving',{roughness:.9,envMapIntensity:.35});sidewalkMat.color.copy(srgbHex(0x898d90));
  // 每条路的车道宽度（世界单位）单独记一份：设施退线、楼栋退线都要按真实路宽算，
  // 不能再用「最大路宽」一刀切。
  const ROAD_W={};
  netRoads.forEach(e=>{const ps=e.pts;const closed=/已封/.test(e.label||'');
    const wid=/主干路/.test(e.detail)?3.4:(/次干路/.test(e.detail)?2.8:2.2);
    ROAD_W[e.id]=wid;
    for(let i=0;i<ps.length-1;i++){const[ax,ay]=W(ps[i][0],ps[i][1]),[bx,by]=W(ps[i+1][0],ps[i+1][1]);
      const len=Math.hypot(bx-ax,by-ay);if(len<0.05)continue;const ang=-Math.atan2(by-ay,bx-ax);
      // 沥青纹理按 0.5 世界单位（≈3.1m）一个周期平铺：近景看得到骨料颗粒，远景自动 mip 成平均色。
      const roadGeo=tileBoxTop(new THREE.BoxGeometry(len+wid,0.05,wid),(len+wid)/0.5,wid/0.5);
      const road=new THREE.Mesh(roadGeo,roadMat);
      road.position.set((ax+bx)/2,0.36,(ay+by)/2);road.rotation.y=ang;road.userData={entityId:e.id,name:e.label||e.id,kind:"road",detail:e.detail||""};road.receiveShadow=true;scene3.add(road);
      // 连续人行道：把道路从“黑色条带”提升为城市街道断面
      const nx=Math.sin(ang),nz=Math.cos(ang);
      [-1,1].forEach(function(side){const swLen=len+wid+0.18,swWid=0.42;
        const sw=new THREE.Mesh(tileBoxTop(new THREE.BoxGeometry(swLen,0.035,swWid),swLen/0.35,swWid/0.35),sidewalkMat);
        sw.position.set((ax+bx)/2+nx*side*(wid/2+0.31),0.34,(ay+by)/2+nz*side*(wid/2+0.31));sw.rotation.y=ang;sw.receiveShadow=true;scene3.add(sw)});
      // 路缘石
      [-1,1].forEach(function(side){const eg=new THREE.Mesh(new THREE.BoxGeometry(len+wid,0.12,0.16),curbMat);
        eg.position.set((ax+bx)/2+nx*side*(wid/2+0.08),0.36,(ay+by)/2+nz*side*(wid/2+0.08));eg.rotation.y=ang;eg.receiveShadow=true;scene3.add(eg)});
      // 中央车道线（已封路段在封路后隐藏，见 applyTimeline）
      const laneGrp=new THREE.Group();scene3.add(laneGrp);
      if(closed)window.__laneDashes.push(laneGrp);
      let t=2.4;while(t<len-2.4){const d=new THREE.Mesh(new THREE.BoxGeometry(1.1,0.015,0.11),laneMat);
        d.position.set(ax+(bx-ax)*t/len,0.395,ay+(by-ay)*t/len);d.rotation.y=ang;laneGrp.add(d);t+=2.6}
    }
    // 路名标签
    const mid=ps[Math.floor(ps.length/2)];const[mx,mz]=W(mid[0],mid[1]);
    const baseName=String(e.label||'').split('（')[0];
    const lb=makeLabel(baseName,'#cfe0ef');lb.position.set(mx,3.2,mz);scene3.add(lb);
    if(closed){
      // 预先做好橙色版本，封路时只换纹理，不用每帧重绘 canvas
      const alt=makeLabel(baseName,'#e0a965');
      window.__roadLabels.push({sprite:lb,normal:lb.material.map,blocked:alt.material.map});
    }});
  // 路口斑马线
  const zebraMat=new THREE.MeshBasicMaterial({color:0xd5dce2});
  [[200,70],[200,160],[200,250],[360,160],[590,70],[590,160],[590,250]].forEach(function(j){const[jx,jz]=W(j[0],j[1]);
    for(let k=-2;k<=2;k++){const a=new THREE.Mesh(new THREE.BoxGeometry(1.5,0.015,0.2),zebraMat);a.position.set(jx+k*0.32,0.4,jz);scene3.add(a);
      const b=new THREE.Mesh(new THREE.BoxGeometry(0.2,0.015,1.5),zebraMat);b.position.set(jx,0.4,jz+k*0.32);scene3.add(b)}});
  // ---- 楼栋：严格贴街区网格，固定退线、固定朝向、绝不随机穿插 ----
  // 由路网围成的规则街区（场景坐标），每街区放多栋，位置 = 街区内的固定锚点（确定性，无随机偏移）
  // 街区定义：[x0,x1,z0,z1]，锚点按街区尺寸比例分布
  const blocks=[
    [ 70,200, 70,160],   // A-03 西北
    [200,360, 70,160],   // A-03 北中
    [ 70,200,160,250],   // A-03 西南
    [200,360,160,250],   // A-03 南中
    [360,590, 70,160],   // 中部北
    [360,590,160,250],   // 中部南
    [590,720, 70,160],   // B-01 北
    [590,720,160,250],   // B-01 南
  ];
  const gltfLoader=new THREE.GLTFLoader();
  // 街区内的固定锚点（相对可建范围中心的比例偏移）：每街区 2 栋
  const SUB_POS=[[-0.24,0.18],[0.24,-0.18]];
  // 建筑退线（场景单位）：街区边界＝道路中线，内缩「最大道路半宽(主干1.7世界) + 人行道退线」
  // 1 世界单位 = 1/0.16 = 6.25 场景单位 → 约 4.8 世界单位 ≈ 30 场景单位（含人行道）
  const INSET=30;
  // 设施占位：楼栋生成时避开泵站/雨水口周边空地，否则排水设施会被楼群夹在中间、分不清是设施还是楼
  const FAC_GUARD=Object.keys(TWIN3D_FAC).map(k=>TWIN3D_FAC[k]);
  const nearFac=(px,pz)=>FAC_GUARD.some(p=>Math.hypot(px-p[0],pz-p[1])<40);
  blocks.forEach((b,i)=>{
    const bx=(b[0]+b[1])/2, bz=(b[2]+b[3])/2;   // 街区中心（场景坐标）
    const bw=b[1]-b[0], bd=b[3]-b[2];           // 街区宽、深
    // 高度梯度：x 越大（东侧）离相机越远 → 楼越矮，制造「近高远低」纵深序列
    const heightK=1.30-(bx/720)*0.55;            // 近(135)≈1.20、远(655)≈0.80
    // 强制退线：街区边界就是道路中线，按「最大道路半宽 + 人行道退线」内缩出可建范围，
    // 楼中心再夹到可建范围内，任何参数下都不会压到道路。
    const x0=b[0]+INSET, x1=b[1]-INSET, z0=b[2]+INSET, z1=b[3]-INSET;
    if(x1-x0<34||z1-z0<30)return;               // 可建范围太小，该街区不放楼
    for(let si=0; si<2; si++){
      const sp=SUB_POS[si];
      // 楼体尺寸：「路要大、建筑不必大」——底面积按可建范围取比例并封顶
      const fw=Math.min((x1-x0)*0.13, 4.2), fd=Math.min((z1-z0)*0.22, 3.6);
      const halfMax=(Math.max(fw,fd)*1.06)*3.125;   // 世界→场景半边长，含 6% 基座外扩
      let px=(x0+x1)/2 + sp[0]*(x1-x0);
      let pz=(z0+z1)/2 + sp[1]*(z1-z0);
      px=Math.min(Math.max(px,x0+halfMax),x1-halfMax);   // 夹到可建范围，楼体不越界
      pz=Math.min(Math.max(pz,z0+halfMax),z1-halfMax);
      if(nearFac(px,pz))continue;               // 该位置让给排水设施
      const[wx,wz]=W(px,pz);
      const fh=(4.6+((i*7+si*5)%4)*0.9)*heightK;   // 高度 4.6~7.3 × 纵深系数
      makeBuilding(fw,fh,fd,wx,wz,(i+si)%2?0:Math.PI/2);
    }
  });
  // 地标建筑（KayKit 模型）：A-03 北中 + B-01 北，做视觉锚点，其余用程序化楼栋
  // 第四项是对应街区索引。地标同样要退线——用真实包围盒夹回街区可建范围，
  // 否则尺度一放大就贴到路缘，视觉上就是"楼压路"。
  [[285,115,'building_A',1],[655,115,'building_E',6]].forEach(function(lm){
    const[wx,wz]=W(lm[0],lm[1]);
    const blk=blocks[lm[3]];
    const[wx0,wz0]=W(blk[0]+14,blk[2]+14),[wx1,wz1]=W(blk[1]-14,blk[3]-14);
    const bx0=Math.min(wx0,wx1),bx1=Math.max(wx0,wx1),bz0=Math.min(wz0,wz1),bz1=Math.max(wz0,wz1);
    gltfLoader.load('/assets/kaykit/'+lm[2]+'.gltf',function(gltf){
      const model=gltf.scene;
      model.scale.set(2.4,2.4,2.4);   // 3.2 时占地约 60m×60m，整块街区被吃掉、糊在路缘
      model.position.set(wx,0.37,wz);
      model.updateMatrixWorld(true);
      const bb=new THREE.Box3().setFromObject(model);
      const hw=(bb.max.x-bb.min.x)/2+0.3, hd=(bb.max.z-bb.min.z)/2+0.3;   // 世界单位，含余量
      const cx=(bx1-bx0)>hw*2?Math.min(Math.max(wx,bx0+hw),bx1-hw):(bx0+bx1)/2;
      const cz=(bz1-bz0)>hd*2?Math.min(Math.max(wz,bz0+hd),bz1-hd):(bz0+bz1)/2;
      model.position.set(cx,0.37,cz);
      model.traverse(o=>{if(o.isMesh){o.castShadow=true;o.receiveShadow=true;}});
      scene3.add(model);
      (window.__buildings=window.__buildings||[]).push({model:model,index:lm[2],name:lm[2]});
    },undefined,function(err){console.warn('地标建筑加载失败',lm[2],err);});
  });
  // ---- 天际线远景楼：场景东侧 + 南北两侧边缘，低矮楼群剪影填补地平线，强化纵深与围合感 ----
  // 不挂台账、纯背景，复用 makeBuilding（自带窗户贴图/裙楼/腰线），与近景楼风格统一
  (function buildSkyline(){
    // 东侧边缘（x≈748，最远处）一排稀疏楼群剪影，沿 z 疏朗排布，高度起伏形成天际线
    for(let k=0;k<10;k++){
      const[wx,wz]=W(748, 45+k*24);
      makeBuilding(5.5, 3.2+(k%5)*1.1, 4.8, wx, wz, 0);
    }
    // 北侧边缘（z≈28）一列
    for(let k=0;k<8;k++){
      const[wx,wz]=W(50+k*90, 28);
      makeBuilding(5.5, 2.6+(k%3)*0.9, 4.2, wx, wz, 0);
    }
    // 南侧边缘（z≈305）一列
    for(let k=0;k<8;k++){
      const[wx,wz]=W(50+k*90, 305);
      makeBuilding(5.5, 2.6+((k+1)%3)*0.9, 4.2, wx, wz, 0);
    }
  })();
  // ---- 街区地块色块：每个街区铺一块铺装色块，分散地面大片纯色，形成地块纹理层次 ----
  // 城市街区以铺装/混凝土为主，只保留少量冷灰绿变化，避免整片地块读成村落草地
  // three r128 没有颜色管理：十六进制被当作线性值直接进光照，输出时又编码一次 sRGB，
  // 等效把整片地面提亮近一倍（131 的中灰会渲成 224 的白）。转换见上方 srgbHex()。
  // envMapIntensity 压到 0.35：阴天 HDR 的漫反射 IBL 很强，不做衰减时地面会比材质本色亮一倍
  // 色差控制在 5% 以内：四种铺装色只是让街区不完全雷同，不能读成「深一块浅一块」的补丁
  const lotMats=[0x878c8e,0x8c9092,0x83888a,0x898d8a].map(c=>new THREE.MeshStandardMaterial({color:srgbHex(c),roughness:.94,metalness:.02,envMapIntensity:.35}));
  blocks.forEach((b,i)=>{
    const[wx0,wz0]=W(b[0],b[2]),[wx1,wz1]=W(b[1],b[3]);
    const cw=Math.abs(wx1-wx0), cd=Math.abs(wz1-wz0);
    const lot=new THREE.Mesh(new THREE.PlaneGeometry(cw,cd),lotMats[i%4]);
    lot.rotation.x=-Math.PI/2;
    // 地块抬到 0.35，与人行道齐平（路面 0.385）。
    // 原来贴在 0.02 的基底上，等于比车行道低 2 米多，斜视时会看到一道深沟，
    // 漫出路面的积水也会变成悬在半空的板。
    lot.position.set((wx0+wx1)/2,0.35,(wz0+wz1)/2);
    lot.receiveShadow=true;
    scene3.add(lot);
  });
  // B-01 东侧空地 + 南北外围大地块（绿地/空地），填补远景纯色地面
  const extraLots=[[745,120,60,120],[745,220,60,90],[120,35,560,60],[360,255,360,45]];
  extraLots.forEach((r,i)=>{
    const[wx0,wz0]=W(r[0],r[1]),[wx1,wz1]=W(r[0]+r[2],r[1]+r[3]);
    const lot=new THREE.Mesh(new THREE.PlaneGeometry(Math.abs(wx1-wx0),Math.abs(wz1-wz0)),lotMats[(i+2)%4]);
    lot.rotation.x=-Math.PI/2;
    lot.position.set((wx0+wx1)/2,0.35,(wz0+wz1)/2);
    lot.receiveShadow=true;
    scene3.add(lot);
  });
  // 东南角低层建筑群：填补画面右下方空地（相机视角右前方），低层厂房/商业楼，不高不抢主体
  (function buildSELowRise(){
    // 民生街在 y=250（红线即道路中线），这几栋必须退到 y≥272，否则楼体压到民生街
    const rows=[[500,278],[590,282],[680,276]];
    rows.forEach((r,ri)=>{
      const[wx,wz]=W(r[0],r[1]);
      makeBuilding(5+(ri%2), 3.0+(ri%3)*0.8, 4.5, wx, wz, ri%2?0:Math.PI/2);
    });
  })();
  // ---- 行道树（沿主干路两侧）+ 片区内部点缀 ----
  // 路口 45 场景内不种树：路口是积水核心，行道树种进去必然泡在水里，
  // 视觉上就是「树长在水中间」。真实路口转角本来也不种乔木。
  const TREE_KEEP_OUT=[[200,160],[590,160],[200,70],[200,250],[360,160],[590,70],[590,250]];
  netRoads.forEach(rd=>{if(rd.pts.length<2)return;
    const main=/主干路|次干路/.test(rd.detail);
    // 偏移按实际路宽算：路半宽 + 人行道 + 树穴退让 1.15 世界单位（约 7m）。
    // 原来写死 8 场景（≈1.3 世界），比主干路半宽 1.7 世界还小 → 树直接种在车行道里；
    // 只退到 3.7m 时树冠又会被漫溢的积水泡住。
    const off=(ROAD_W[rd.id]/2+1.15)/K;
    for(let i=0;i<rd.pts.length-1;i++){const p0=rd.pts[i],p1=rd.pts[i+1];
      const dx=p1[0]-p0[0],dy=p1[1]-p0[1];const len=Math.hypot(dx,dy);if(len<5)continue;
      const ux=dx/len,uy=dy/len;const nx=-uy,ny=ux;
      let t=8;while(t<len-8){const bx=p0[0]+ux*t,by=p0[1]+uy*t;
        if(main){[-1,1].forEach(function(side){const px=bx+nx*side*off,py=by+ny*side*off;
          if(TREE_KEEP_OUT.some(function(c){return Math.hypot(px-c[0],py-c[1])<45}))return;
          if(seeded()>0.82){const[wx,wz]=W(px,py);makeTree(wx,wz,0.7+seeded()*0.5)}})}
        t+=20+seeded()*8}}
  });
  // 路灯光锥（体积光的廉价近似，纯 shader、零贴图）：
  // 阴雨天能见度低，路灯是亮的；雨幕把光散成一束，锥体顶部最亮、向下淡出。
  // 只在雨势起来之后才显示（tick 里按 v.rain 控制），白天大晴天挂着会假。
  window.__lampBeams=[];
  function addLampBeam(wx,wz){
    const mat=new THREE.ShaderMaterial({
      uniforms:{uAmt:{value:0.0}},
      vertexShader:['varying float vH;varying vec3 vN;varying vec3 vView;',
        'void main(){vH=uv.y;',
        '  vN=normalize(normalMatrix*normal);',
        '  vec4 mv=modelViewMatrix*vec4(position,1.0);',
        '  vView=normalize(-mv.xyz);',
        '  gl_Position=projectionMatrix*mv;}'].join(' '),
      fragmentShader:['uniform float uAmt;varying float vH;varying vec3 vN;varying vec3 vView;',
        'void main(){',
        // 边缘比中心亮：真实体积光的轮廓在锥面掠射处更明显，中心反而是空的
        '  float edge=pow(1.0-abs(dot(normalize(vN),normalize(vView))),1.6);',
        '  float a=(0.25+0.75*edge)*pow(vH,1.4)*uAmt;',
        '  gl_FragColor=vec4(vec3(1.0,0.88,0.66)*a,a);}'].join(' '),
      transparent:true,depthWrite:false,side:THREE.DoubleSide,
      blending:THREE.AdditiveBlending});
    const cone=new THREE.Mesh(new THREE.ConeGeometry(0.85,1.0,14,1,true),mat);
    cone.position.set(wx,0.95,wz);       // 灯头(1.45)到地面(0.45)，锥尖朝上
    cone.visible=false;scene3.add(cone);
    window.__lampBeams.push(cone);
  }
  // ---- 路灯（主干路两侧）：KayKit streetlight 模型 ----
  netRoads.forEach(rd=>{if(rd.pts.length<2)return;
    if(!/主干路/.test(rd.detail))return;
    // 偏移原来写死 5 场景（≈0.8 世界），比主干路半宽 1.7 世界还小 → 路灯直接立在车行道中间。
    // 按路宽算到路缘外 0.45 世界（约 2.8m），并且两侧都装。
    const lampOff=(ROAD_W[rd.id]/2+0.45)/K;
    for(let i=0;i<rd.pts.length-1;i++){const p0=rd.pts[i],p1=rd.pts[i+1];
      const dx=p1[0]-p0[0],dy=p1[1]-p0[1];const len=Math.hypot(dx,dy);if(len<5)continue;
      const ux=dx/len,uy=dy/len;const nx=-uy,ny=ux;
      let t=12;while(t<len-12){const bx=p0[0]+ux*t,by=p0[1]+uy*t;
        [-1,1].forEach(function(side){
          const[wx,wz]=W(bx+nx*side*lampOff,by+ny*side*lampOff);
          gltfLoader.load('/assets/kaykit/streetlight.gltf',function(g){
            const m=g.scene;const s=9.0*K/0.96;m.scale.set(s,s,s);   // 路灯高 9m：9*K/0.96 ≈ 1.5
            m.position.set(wx,0.37,wz);m.traverse(o=>{if(o.isMesh)o.castShadow=true;});
            scene3.add(m);
            // 只给主事件区附近的灯挂光锥：满城灯柱都发光会变成"满地路障"
            if(Math.hypot(wx+16,wz+1)<16)addLampBeam(wx,wz);
          });
        });
        t+=34
      }
    }
  });
  // ---- 路侧车辆：KayKit 车辆模型（轿车/出租车/掀背随机） ----
  const KAYKIT_CARS=['car_sedan','car_taxi','car_hatchback','car_stationwagon'];
  netRoads.forEach(rd=>{if(rd.pts.length<2)return;
    for(let i=0;i<rd.pts.length-1;i++){const p0=rd.pts[i],p1=rd.pts[i+1];
      const dx=p1[0]-p0[0],dy=p1[1]-p0[1];const len=Math.hypot(dx,dy);if(len<5)continue;
      const ux=dx/len,uy=dy/len;const nx=-uy,ny=ux;const ang=-Math.atan2(ny,nx);
      let t=16;while(t<len-16){const bx=p0[0]+ux*t,by=p0[1]+uy*t;
        if(seeded()>0.90){const[wx,wz]=W(bx+nx*10,by+ny*10);   // 偏移加大，车在车道内
          const carName=KAYKIT_CARS[Math.floor(seeded()*KAYKIT_CARS.length)];
          gltfLoader.load('/assets/kaykit/'+carName+'.gltf',function(g){
            // KayKit 车模长 0.94（模型单位），按真实车长 4.5m 换算：4.5*K/0.94 ≈ 0.77。
            // 原来写 4.3（把世界单位当成米了），车长 4.03 世界单位 ≈ 25m，比整条车行道还长。
            const m=g.scene;const s=4.5*K/0.94;m.scale.set(s,s,s);
            m.position.set(wx,0.37,wz);m.rotation.y=ang;
            m.traverse(o=>{if(o.isMesh)o.castShadow=true;});
            scene3.add(m);
          });
        }
        t+=30}}   // 步进加大到 30 场景单位(≈4.8 世界单位)，避免车长 4 米重叠
  });
  // ---- 积水段：贴地 shader 水面（薄薄一层，随水位轻微抬升 + 面积扩散 + 颜色加深）----
  // 真实内涝积水 = 路面上一薄层水。不用 ExtrudeGeometry 立体柱（会像悬浮蓝板），
  // 也不用多个矩形水面拼接（会出现"一段一段"）；整片水面用「多个水洼场融合」的 shader
  // 生成连续水体 + 不规则水岸线 + 边缘羽化，水位越高水洼越外扩、越连片。
  window.__waterMeshes=[];   // 供事件动画引用：{surface, label, depthMax, baseW, baseD, cx, cz}
  // 水洼场定义（场景坐标 + 半径世界单位）：沿低洼路网排布并按距离重叠，
  // 相邻水洼重叠才能融合成一条连续水体，否则会被羽化切断成"一段一段"。
  // 台账只有 2 处实测积水（A-03 30cm / B-01 18cm），这里不新增测点，
  // 只是把同一场积水在低洼路网上的漫流范围画出来，与主水面融合为一片。
  // 半径按「满水位时的有效半宽」给出：先按目标水面半宽反推，再让 grow 只做小幅收缩。
  // 兴华路(次干)半宽 1.4 世界单位、建设大道(主干)半宽 1.7 → blob 有效半宽要略大于路半宽，
  // 否则水面只占路面 60%，读起来像「路边一条水沟」而不是整条路积水。
  const WATER_BLOBS={
    // A-03：以路口为水体核心向四周摊开，不是沿路排一串长条。
    // 半径给得比「路半宽」大一圈（路口 4.6 世界单位≈29m 半宽），水才会漫过路缘、
    // 盖住路口四角和人行道，读成一片内涝；半径刚好等于路宽时只会读成一条沿街水沟。
    // 垂直路方向的半宽按「路半宽 + 约 5m 漫溢」给（兴华路半宽 1.4 世界，取 2.2）：
    // 给到 2.9 时水会漫到行道树的位置，树看起来像泡在水里；行道树在距中线 2.55 世界处。
    'WO-20261029-007':[
      [200,160,3.5,3.2],    // 建设大道 × 兴华路 路口主水体（最深，路口不种树）
      [250,161,8.0,2.6],    // 兴华路东段：拉长成一条 100m 级水带，不是一滩水（南缘贴到路缘石）
      [300,161,5.0,2.0],    // 兴华路东延（尾端收窄）
      [200,128,2.2,4.6],    // 建设大道北段
      [200,194,2.1,4.2],    // 建设大道南段
      // 作业点（DF-IN-101 雨水口，台账 272,155）自带一汪洼地。
      // 没有它的话，水位退到 0.44 时主水洼就缩到作业点外面了——
      // 画面变成"三个人对着干地抽水"，抽排这一段直接不成立。
      // 这汪水的到达阈值在下面被单独压到 0.02，全场最后消失。
      [271,155,1.7,1.3],
    ],
    // B-01：18cm 较浅，范围相应小一圈
    'WO-20261029-011':[
      [590,160,3.2,2.9],
      [590,126,2.0,4.2],
      [590,196,1.9,3.8],
      [638,160,5.0,2.2],
    ],
  };
  water.forEach(e=>{
    const ps=(TWIN3D_WATER[e.id]||String(e.points||'').split(' ').map(p=>p.split(',').map(Number)));
    const cen=ps.reduce((a,p)=>[a[0]+p[0],a[1]+p[1]],[0,0]).map(v=>v/ps.length);
    const pts=ps.map(p=>{const[wx,wz]=W(p[0],p[1]);return[wx,wz]});
    const[cx,cz]=W(cen[0],cen[1]);
    const xs=pts.map(p=>p[0]),zs=pts.map(p=>p[1]);
    const ww=Math.max(...xs)-Math.min(...xs), wd=Math.max(...zs)-Math.min(...zs);   // 主积水基准尺寸（世界单位）
    // 水洼场：优先用按低洼路网配置的 blob 序列（沿路相邻重叠 → 融合成连续水体）；
    // 未配置的积水退回「以台账范围生成单个水洼」
    let blobs=[];
    (WATER_BLOBS[e.id]||[]).forEach(s=>{
      const[bx,bz]=W(s[0],s[1]);
      blobs.push([bx,bz,s[2],s[3]]);
    });
    if(!blobs.length)blobs=[[cx,cz,Math.max(ww*0.5,5.0),Math.max(wd*0.5,2.6)]];
    // 水面平面铺满所有水洼外包范围 + 余量，保证连续水体不被平面边界切断
    let minX=cx,maxX=cx,minZ=cz,maxZ=cz;
    blobs.forEach(b=>{
      minX=Math.min(minX,b[0]-b[2]);maxX=Math.max(maxX,b[0]+b[2]);
      minZ=Math.min(minZ,b[1]-b[3]);maxZ=Math.max(maxZ,b[1]+b[3]);
    });
    const pad=3.5;   // 余量给足：水岸噪声会把水面推出 blob 半径，pad 太小会被平面边界切成直边
    const planeW=(maxX-minX)+pad*2, planeD=(maxZ-minZ)+pad*2;
    const pcx=(minX+maxX)/2, pcz=(minZ+maxZ)/2;
    const localBlobs=blobs.map(b=>[b[0]-pcx,b[1]-pcz,b[2],b[3]]);
    // 蔓延顺序：按离「积水核心」（第一个水洼：路口/低洼点）的距离给到达阈值。
    // 核心最早（水位 0.06 就开始积水），远端最晚（到 0.56 才开始），
    // 于是水面是顺着低洼路网一段段漫出去的。
    const bd=blobs.map(b=>Math.hypot(b[0]-blobs[0][0],b[1]-blobs[0][1]));
    const bmax=Math.max.apply(null,bd.concat([1e-6]));
    const blobA=bd.map(d=>0.06+0.50*(d/bmax));
    // 作业点那汪水（A-03 的最后一个水洼）单独压到 0.02：
    // 它离积水核心最远，按距离算会拿到 0.56 的阈值——最后一个出现、第一个消失，
    // 正好和"工人正在这里抽水"相反。抽排全程它都该在。
    if(/A-03/.test(e.label||'')&&blobA.length)blobA[blobA.length-1]=0.02;
    const surf=makeWaterSurface(planeW,planeD,localBlobs);
    // 水面高度：道路顶面 0.385，水面 0.45 → 约 0.065 世界单位≈40cm 水层，
    // 高于路缘石顶面 0.42，积水才不会被路缘从两边"切"成一条窄带。
    surf.position.set(pcx,0.45,pcz);
    surf.userData={entityId:e.id,name:e.label||"积水段",kind:"water",
      detail:(e.detail||'')+'（水面含漫流水洼，属同一场积水的低洼漫流，非独立测点）',isWaterSurface:true};
    surf.receiveShadow=true;
    scene3.add(surf);
    // depthMax：峰值水位（世界单位，真实积水深度，A-03 约 30cm→0.30，B-01 约 18cm→0.20）
    const depthMax=/A-03/.test(e.label||'')?0.30:0.20;
    window.__waterMeshes.push({surface:surf,label:(e.label||'积水'),depthMax:depthMax,cx:cx,cz:cz,baseW:ww,baseD:wd,blobA:blobA});
    // 积水标注：只留深度数字，紧贴水面（不飘在高空）。
    // 位置取主水洼中心，不是台账范围的外接矩形中心——台账范围跨整条路，
    // 矩形中心常落在没有水的路段上，标注就会飘在离水面几十米外。
    const uB0s=surf.material.uniforms.uB0.value;
    const lbx=pcx+uB0s.x, lbz=pcz+uB0s.y;
    const lb=makeLabel('','#eaf6ff');lb.position.set(lbx,0.9,lbz);lb.userData={waterLabel:true};
    surf.userData.waterLabel=lb;scene3.add(lb);window.__waterMeshes[window.__waterMeshes.length-1].labelSprite=lb;
  });
  // ---- 土壤下渗：积水区正下方一层土黄色晕染，表达「部分积水下渗进入浅层土壤」 ----
  // 数据没有土壤渗透系数/地质层，因此下渗分流用可配置占位参数 INFIL_RATE 表达，
  // 只做「地表径流 → 土壤下渗」的分流示意，不伪装成真实物理计算结果。
  // 晕染层贴路面（正常深度测试，不被遮挡、不投影错位），随水深变深、扩散，暖土黄区别于积水蓝。
  window.__infilMarkers=[];
  window.INFIL_RATE=0.18;   // 下渗分流比例（占位，可调；真实值需土壤渗透系数数据）
  // 晕染用径向渐变贴图，不是纯色平面：纯色平面是一块硬边矩形，
  // 铺在路面上像刷了一块土黄色油漆；径向渐变才是"从水底往外渗"的软边。
  const glowCv=document.createElement('canvas');glowCv.width=glowCv.height=128;
  const gctx=glowCv.getContext('2d');
  const grad=gctx.createRadialGradient(64,64,0,64,64,64);
  grad.addColorStop(0,'rgba(222,164,66,0.85)');
  grad.addColorStop(0.5,'rgba(214,156,70,0.38)');
  grad.addColorStop(1,'rgba(210,150,70,0)');
  gctx.fillStyle=grad;gctx.fillRect(0,0,128,128);
  const infilMat=new THREE.MeshBasicMaterial({map:new THREE.CanvasTexture(glowCv),transparent:true,opacity:0,depthWrite:false});
  (window.__waterMeshes||[]).forEach(w=>{
    // 下渗晕染：叠在积水水面之上（y 略高 + renderOrder 更高），橙黄色罩层表达「水正在往下渗」，
    // 随水深变深、变明显，一眼能区分于蓝色水面与灰黄地面。
    // 位置取主水洼（uB0）中心，不是台账范围的外接矩形中心：矩形中心常落在没有水的空地上，
    // 晕染就会像一块"沙地"贴在干路面上，和水面错开。
    const u0=w.surface&&w.surface.material&&w.surface.material.uniforms&&w.surface.material.uniforms.uB0;
    const gx=(w.surface&&u0&&u0.value.z>0.01)?w.surface.position.x+u0.value.x:w.cx;
    const gz=(w.surface&&u0&&u0.value.z>0.01)?w.surface.position.z+u0.value.y:w.cz;
    // 尺寸按主水洼的实际半径给（直径 ≈ 2×半径 + 一点余量），不用台账外接矩形：
    // 台账范围跨整条路（30+ 世界单位），按它做罩层就像在干路上铺了两块黄土。
    const gr=(u0&&u0.value.z>0.01)?u0.value.z:3.0;    // 主水洼 rx
    const gr2=(u0&&u0.value.w>0.01)?u0.value.w:3.0;   // 主水洼 rz
    const glow=new THREE.Mesh(new THREE.PlaneGeometry(gr*2.1,gr2*2.1),infilMat.clone());
    glow.rotation.x=-Math.PI/2;
    glow.position.set(gx,0.47,gz);
    glow.renderOrder=110;   // 高于积水水面(100)，确保不被水面盖住
    glow.visible=false;
    scene3.add(glow);
    window.__infilMarkers.push({mesh:glow,cx:gx,cz:gz,blobA:(w.blobA?w.blobA[0]:null)});
  });
  // 取设施最近道路的方向角，让雨水篦子顺路铺设（不靠人工猜朝向）
  function nearestRoadAngle(px,py){
    let bd=1e9,ang=0;
    for(const k in TWIN3D_ROADS){
      const pts=TWIN3D_ROADS[k];
      for(let i=0;i<pts.length-1;i++){
        const a=pts[i],b=pts[i+1],dx=b[0]-a[0],dy=b[1]-a[1];
        const len2=dx*dx+dy*dy||1;
        let t=((px-a[0])*dx+(py-a[1])*dy)/len2;t=Math.max(0,Math.min(1,t));
        const d=Math.hypot(px-(a[0]+t*dx),py-(a[1]+t*dy));
        if(d<bd){bd=d;ang=-Math.atan2(dy,dx);}
      }
    }
    return ang;
  }
  // 设施退线：把泵站中心沿「离开最近道路」的方向推到路缘之外。
  // 台账只给设施点位、不给占地，直接按点位摆泵站，泵房+围栏会压到车行道上
  // （DF-PS-01 在 (285,174)，兴华路中线 z=160、半宽 8.75 场景，泵站北缘到 159.6 —— 压进去 9 米）。
  // halfX/halfZ 是设施包围盒半宽、半深（世界单位）。
  // 用矩形的支撑函数算它在道路法向上的投影半径，比外接圆松弛，不会把设施推得离路太远。
  function pushStationOffRoads(px,py,halfX,halfZ){
    let cx=px, cy=py;
    for(let it=0; it<4; it++){          // 迭代几次：路口附近可能同时贴着两条路
      let moved=false;
      for(const k in TWIN3D_ROADS){
        const halfW=((ROAD_W[k]||2.2)/2)/K;   // 场景单位
        const pts=TWIN3D_ROADS[k];
        for(let i=0;i<pts.length-1;i++){
          const a=pts[i],b=pts[i+1],dx=b[0]-a[0],dy=b[1]-a[1];
          const len2=dx*dx+dy*dy||1;
          let t=((cx-a[0])*dx+(cy-a[1])*dy)/len2; t=Math.max(0,Math.min(1,t));
          const qx=a[0]+t*dx, qy=a[1]+t*dy;
          let ddx=cx-qx, ddy=cy-qy;
          let d=Math.hypot(ddx,ddy);
          if(d<0.001){ddx=0;ddy=1;d=1}
          const nx=ddx/d, ny=ddy/d;
          const support=(halfX*Math.abs(nx)+halfZ*Math.abs(ny))/K;
          const need=halfW+support+0.8/K;    // 留 0.8 世界单位（约 5m）余量：矩形近似有偏差，宁可多留
          if(d<need){const push=(need-d)/d; cx+=ddx*push; cy+=ddy*push; moved=true;}
        }
      }
      if(!moved)break;
    }
    return [cx,cy];
  }
  // ---- 设施：泵站(写实小楼) + 雨水口(路面雨水篦子) ----
  window.__pumpAlarms=[];   // 供事件动画引用：故障泵站的告警灯 mesh
  window.__clogMarks=[];    // 堵塞标记：现场核查后才出现，处置完成后消失（不该从第一帧就在）
  fac.forEach(e=>{
    let fpos=TWIN3D_FAC[e.id]||[Number(e.cx),Number(e.cy)];
    const ps=e.id.indexOf('DF-PS')===0;
    // 泵站先退线到路缘外，再落几何。雨水口本来就该在路上（篦子嵌在路面），不推。
    // 半宽/半深按缩放 0.68 后的实测包围盒给（含围栏与进水井）。
    if(ps)fpos=pushStationOffRoads(fpos[0],fpos[1],2.01,1.81);
    const[wx,wz]=W(fpos[0],fpos[1]);
    if(ps){
      // 排水泵站：低矮平顶泵房 + 格栅进水井 + 立式出水管 + 电控柜 + 围栏。
      // 刻意区别于住宅楼——泵房宽而矮、平顶无窗、灰水泥色，工业设施形态一眼可辨。
      const grp=new THREE.Group();grp.position.set(wx,0.37,wz);
      // 整体缩到 0.68：原尺寸泵房 4.4×3.2 世界单位（约 27m×20m）对街边泵站偏大，
      // 加围栏后占地近 40m，视觉上就是「一栋楼杵在路口」。缩放同时把子件间距一起收，比例不变。
      grp.scale.setScalar(0.68);
      grp.rotation.y=nearestRoadAngle(fpos[0],fpos[1]);
      const wallMat=new THREE.MeshStandardMaterial({color:0x828b92,roughness:.9,metalness:.04});
      const darkMat=new THREE.MeshStandardMaterial({color:0x454f59,roughness:.72,metalness:.22});
      const steelMat=new THREE.MeshStandardMaterial({color:0x5d6873,roughness:.42,metalness:.72});
      // 泵房主体：宽 4.4 × 高 1.9 × 深 3.2（低矮单层工业体量，不是多层住宅）
      const house=new THREE.Mesh(new THREE.BoxGeometry(4.4,1.9,3.2),wallMat);
      house.position.y=0.95;house.castShadow=true;house.receiveShadow=true;grp.add(house);
      // 平顶压顶檐口（略外挑，工业厂房感）
      const eave=new THREE.Mesh(new THREE.BoxGeometry(4.7,0.16,3.5),darkMat);
      eave.position.y=1.96;eave.castShadow=true;grp.add(eave);
      // 泵房正面卷帘门（深色，区别于住宅的窗）
      const door=new THREE.Mesh(new THREE.BoxGeometry(1.5,1.3,0.06),darkMat);
      door.position.set(-1.1,0.66,1.62);grp.add(door);
      // 市政排水识别色带：环泵房一圈的蓝色带，让设施脱离「普通小楼」的观感
      const band=new THREE.Mesh(new THREE.BoxGeometry(4.44,0.22,3.24),
        new THREE.MeshStandardMaterial({color:0x2f7d9e,roughness:.58,metalness:.12}));
      band.position.y=1.62;grp.add(band);
      // 格栅进水井：泵房侧前方的沉井 + 格栅条，水从这里进泵
      const well=new THREE.Mesh(new THREE.BoxGeometry(2.4,0.5,1.7),
        new THREE.MeshStandardMaterial({color:0x161d25,roughness:.86,metalness:.12}));
      well.position.set(1.1,0.25,2.0);well.receiveShadow=true;grp.add(well);
      for(let k=0;k<6;k++){
        const bar=new THREE.Mesh(new THREE.BoxGeometry(0.08,0.5,0.08),steelMat);
        bar.position.set(0.12+k*0.38,0.3,2.0);grp.add(bar);
      }
      const wellRim=new THREE.Mesh(new THREE.BoxGeometry(2.6,0.1,1.9),wallMat);
      wellRim.position.set(1.1,0.5,2.0);grp.add(wellRim);
      // 立式出水管：泵房后侧露出的竖管 + 顶部弯头，泵站最直观的识别特征
      const riser=new THREE.Mesh(new THREE.CylinderGeometry(0.17,0.17,2.9,14),
        new THREE.MeshStandardMaterial({color:0x6d8fa5,roughness:.4,metalness:.6}));
      riser.position.set(-1.9,1.45,-1.5);riser.castShadow=true;grp.add(riser);
      const flange=new THREE.Mesh(new THREE.CylinderGeometry(0.3,0.3,0.14,14),steelMat);
      flange.position.set(-1.9,0.07,-1.5);grp.add(flange);
      const elbow=new THREE.Mesh(new THREE.CylinderGeometry(0.17,0.17,1.1,14),riser.material);
      elbow.rotation.z=Math.PI/2;elbow.position.set(-1.35,2.9,-1.5);grp.add(elbow);
      // 电控柜
      const cab=new THREE.Mesh(new THREE.BoxGeometry(0.75,1.15,0.5),darkMat);
      cab.position.set(1.9,0.58,1.0);cab.castShadow=true;grp.add(cab);
      // 泵站围栏：四角立杆 + 一道横杆
      for(let k=0;k<4;k++){
        const px2=(k%2?1:-1)*2.9, pz2=(k<2?-1:1)*2.3;
        const post=new THREE.Mesh(new THREE.CylinderGeometry(0.05,0.05,0.85,8),steelMat);
        post.position.set(px2,0.42,pz2);grp.add(post);
      }
      const rail=new THREE.Mesh(new THREE.BoxGeometry(5.8,0.05,0.05),steelMat);
      rail.position.set(0,0.8,-2.3);grp.add(rail);
      const rail2=new THREE.Mesh(new THREE.BoxGeometry(5.8,0.05,0.05),steelMat);
      rail2.position.set(0,0.8,2.3);grp.add(rail2);
      grp.userData={entityId:e.id,name:e.label||"泵站",kind:"pump",detail:e.detail||""};
      scene3.add(grp);
      const sign=makeLabel(String(e.label||e.id).replace('雨水泵站','泵站')+' · 排水设施','#cfe0ef');sign.position.set(wx,2.15,wz);scene3.add(sign);
      // 故障泵站(DF-PS-01)挂红色警示光柱（向上投射的半透明红光柱 + 顶部闪烁灯），比小球醒目
      if(/fault|待修|故障/.test(e.detail||'')){
        // 光柱高度：原 6.5 世界单位＝约 40 m，比泵房（约 4 m）高十倍，在 69 m 机位下
        // 占了画面三分之一，读起来像「楼里着火」而不是「这台泵故障」。压到 2.6 时又被泵房
        // 完全挡住、只在屋顶露一点红，起不到远景定位作用。3.8 世界（≈24 m）是两头都成立的档位：
        // 高出泵房一截、远看找得到，又不再喧宾夺主。
        const beam=new THREE.Mesh(new THREE.CylinderGeometry(0.55,0.85,3.8,16,1,true),
          new THREE.MeshBasicMaterial({color:0xff2d2d,transparent:true,opacity:.35,side:THREE.DoubleSide,depthWrite:false}));
        beam.position.set(wx,2.5,wz);scene3.add(beam);
        const lamp=new THREE.Mesh(new THREE.SphereGeometry(0.34,12,10),new THREE.MeshBasicMaterial({color:0xff3b30}));
        lamp.position.set(wx,4.5,wz);scene3.add(lamp);
        window.__pumpAlarms.push({lamp:lamp,beam:beam});
      }}
    else{
      // 雨水口（inlet）：路面雨水篦子形态——混凝土框 + 深色井口 + 铸铁格栅条 + 路缘侧向进水口
      // 真实雨水篦子约 0.7×0.4 m。这里建成 1.2×0.72 m（真实的 1.7 倍）：
      // 再按真实尺寸做在这个相机距离下只有 3 px，且它是"堵了才积水"的因果对象，需要能辨认。
      // 堵塞是「现场核查后才确认」的状态，不是场景固有属性：篦子一律按正常态建出来，
      // 橙色篦子/警示锥/反涌/标注由 applyTimeline 按时间轴开合（第 2 跳出现、处置完成后消失）。
      const clogged=/clogged|堵塞/.test(e.detail||'');
      const grp=new THREE.Group();
      grp.position.set(wx,0.39,wz);
      grp.rotation.y=nearestRoadAngle(fpos[0],fpos[1]);   // 长边顺路
      grp.scale.setScalar(0.10);                          // 内部按 12m 级建模，缩到 1.2m 级
      const frame=new THREE.Mesh(new THREE.BoxGeometry(1.95,0.13,1.18),
        new THREE.MeshStandardMaterial({color:0x6c747a,roughness:.94,metalness:.02}));
      frame.receiveShadow=true;grp.add(frame);
      const pit=new THREE.Mesh(new THREE.BoxGeometry(1.64,0.10,0.9),
        new THREE.MeshStandardMaterial({color:0x0b0f13,roughness:.9,metalness:.12}));
      pit.position.y=0.06;grp.add(pit);
      // 铸铁篦条：深色高金属度，靠明暗对比在浅灰路面上显出「一排格栅」
      const grateMat=new THREE.MeshStandardMaterial({color:0x242a30,roughness:.36,metalness:.85});
      const nBar=7;
      for(let k=0;k<nBar;k++){
        const bar=new THREE.Mesh(new THREE.BoxGeometry(0.115,0.09,0.88),grateMat);
        bar.position.set(-0.78+k*(1.56/(nBar-1)),0.13,0);
        bar.castShadow=true;grp.add(bar);
      }
      // 路缘侧向进水口：靠路面一侧的竖直开口（真实雨水口的侧向汇入口）
      const curb=new THREE.Mesh(new THREE.BoxGeometry(1.7,0.28,0.14),
        new THREE.MeshStandardMaterial({color:0x0b0f13,roughness:.85,metalness:.1}));
      curb.position.set(0,0.22,-0.55);grp.add(curb);
      grp.userData={entityId:e.id,name:e.label||'雨水口',kind:'intake',detail:e.detail||''};
      scene3.add(grp);
      // 堵塞标记整体收进一个 Group（位置随雨水口），显隐由时间轴控制
      if(clogged){
        const cg=new THREE.Group();cg.position.set(wx,0,wz);cg.visible=false;
        // 警示锥 0.7m、反涌水球 0.35m 半径、标注贴地 0.6m——原来分别是 1.2 世界单位(7.5m)、
        // 0.8 半径(10m 大球)和 2.6m 高，整组比泵房还大。
        const cone=new THREE.Mesh(new THREE.ConeGeometry(0.18*K,0.70*K,10),new THREE.MeshStandardMaterial({color:0xff8c2d,roughness:.4}));
        cone.position.set(0.18,0.40,0);cone.castShadow=true;cg.add(cone);
        const gush=new THREE.Mesh(new THREE.SphereGeometry(0.35*K,14,12),
          new THREE.MeshStandardMaterial({color:0x3a9be0,roughness:.1,metalness:.3,transparent:true,opacity:.85}));
        gush.position.set(0,0.42,0);cg.add(gush);
        const clogLabel=makeLabel('堵塞','#ffb066');clogLabel.position.set(0,1.0,0);cg.add(clogLabel);
        scene3.add(cg);
        window.__clogMarks.push({group:cg,frameMat:frame.material,grateMat:grateMat});
      }
    }});
  // ---- 地下排水管网：半透明管段 + 接口节点（可点击查台账，图层可开关）----
  // 埋深换算：depth_m 米 → 场景 y 下沉（道路顶面约 0.41，管心埋到地下对应深度）
  // 管段保持地下标高；演示模式用轻微 X 光透视让地下层可读，建筑本体仍处于更高的地面层。
  // 管径换算：diameter_mm → 场景半径（放大 0.0016 倍，600mm≈0.96、1000mm≈1.6，粗细差异可辨）
  window.__pipeSegments=[];   // 供事件动画引用：{mesh, id, node, isBottleneck}
  // renderOrder 必须 > 水面(100)：透明层按 renderOrder 升序绘制，谁大谁后画、谁在上。
  // **注意 renderOrder 是 Object3D 的属性，不是 Material 的**——写在材质上完全不生效，
  // 管道就会按默认 0 排序、画在水面之前被盖死。这是本文件踩过的坑：
  // 一度以为是颜色对比度问题（把管道染成 0xff0066 立刻可见，正因为那次顺带把
  // transparent 关掉了，走 opaque 阶段不参与透明排序）。renderOrder 一律设在 mesh 上。
  // 材质用 Basic 而不是 Standard：埋深示意层不受阴雨光照影响，亮度稳定、也省光照计算。
  // 颜色要偏深：r128 把 hex 当线性值、输出再编码 sRGB，等效提亮约 1.9 倍，
  // 0x2ad8ff 那样的亮青会被提成接近白的浅青，和灰白积水同明度直接糊掉。
  const pipeMatBase=new THREE.MeshBasicMaterial({color:0x0a6f92,transparent:true,opacity:.88,depthTest:false,depthWrite:false});
  // 瓶颈管段（PL-DRN-101）：初始同普通管段蓝色，水位过半时 applyTimeline 切成橙色，一眼锁定
  const pipeBottleneckMat=new THREE.MeshBasicMaterial({color:0x0a6f92,transparent:true,opacity:.88,depthTest:false,depthWrite:false});
  // 接口节点/连接桩：同色系但更亮一档，读成"井口比管身更实"
  const nodeMat=new THREE.MeshBasicMaterial({color:0x0f8fb8,transparent:true,opacity:.95,depthTest:false,depthWrite:false});
  TWIN3D_PIPES.forEach(p=>{
    const seg=new THREE.Group();seg.name='pipe-'+p.id;
    const isBottleneck=/管径偏小|重现期不足/.test(p.note||'');
    // 埋深下沉：路面顶约 0.41。这是「地下管网」的示意层，不是实测标高——
    // 原来按 depth_m*0.42 把管心埋到 y≈-0.7~-0.85（相当于地下 7 米），又开了
    // depthTest:false 想靠 X 光透视浮出来，结果被积水水面（renderOrder 100、后画）
    // 整个盖死，开了「管网」开关也什么都看不见。
    // 现在浅埋到路面下 0.03~0.08（按台账深度微调，1.2m 埋深→0.348，2.8m→0.318）：
    // 仍在路面(0.385)以下、读得出"埋在地下"，半透明 + 自发光能透出来；
    // 三根管深浅略有差别，俯视时不会读成"一条平铺的彩带"。
    const depthY=0.36-Math.min(0.05,(p.depth_m||1.5)*0.018);
    // 管径收细一档：原来 600mm→0.24 半径，在 7 米近距离掠视时会糊成一条宽色带。
    const baseOpacity=0.82;
    for(let i=0;i<p.pts.length-1;i++){
      const[ax,az]=W(p.pts[i][0],p.pts[i][1]),[bx,bz]=W(p.pts[i+1][0],p.pts[i+1][1]);
      const len=Math.hypot(bx-ax,bz-az);if(len<0.05)continue;
      const ang=-Math.atan2(bz-az,bx-ax);
      // 管径→半径：按 1 世界单位≈6 m 折算后放大，保证远景可辨（600mm≈0.16、1000mm≈0.26），
      // 不做等比真实尺寸——真实 0.6 m 直径在此场景只有 0.1 单位，肉眼不可见。
      const radius=Math.max(0.15,p.diameter_mm*0.00026);
      // 圆柱 24 分段 + 两端封盖：立体圆管，不是扁平片
      const cyl=new THREE.Mesh(new THREE.CylinderGeometry(radius,radius,len,24,1,false),
        isBottleneck?pipeBottleneckMat:pipeMatBase);
      cyl.position.set((ax+bx)/2,depthY,(az+bz)/2);cyl.rotation.z=Math.PI/2;cyl.rotation.y=ang;
      cyl.renderOrder=105;   // 必须设在 mesh 上（材质上的 renderOrder 是无效属性）
      cyl.castShadow=false;cyl.receiveShadow=false;
      cyl.userData={entityId:p.id,name:p.label,kind:'pipe',detail:'',pipeData:p,__radial:true};
      seg.add(cyl);
    }
    // 接口节点（检查井/接管点）：节点与管段保持同一地下标高；地面只保留短竖向连接桩，避免节点漂在建筑层。
    const riserMat=new THREE.MeshBasicMaterial({color:0x0f8fb8,transparent:true,opacity:.9,depthTest:false,depthWrite:false});
    p.pts.forEach((pt,idx)=>{
      const[wx,wz]=W(pt[0],pt[1]);
      const node=new THREE.Mesh(new THREE.CylinderGeometry(0.34,0.42,0.22,14),nodeMat);
      node.position.set(wx,depthY,wz);
      node.renderOrder=105;
      node.userData={entityId:p.id,name:p.label,kind:'pipe',detail:'',pipeData:p,isPipeNode:true,__radial:true};
      seg.add(node);
      const riser=new THREE.Mesh(new THREE.CylinderGeometry(0.09,0.09,Math.max(0.35,0.44-depthY),10),riserMat);
      riser.position.set(wx,(0.44+depthY)/2,wz);
      riser.renderOrder=105;
      riser.userData={entityId:p.id,name:p.label,kind:'pipe',detail:'',pipeData:p,isPipeRiser:true,__radial:true};
      seg.add(riser);
    });
    seg.userData={entityId:p.id,name:p.label,kind:'pipe',detail:'',pipeData:p};
    seg.visible=false;   // 默认隐藏，点「管网」开关再显示
    scene3.add(seg);
    window.__pipeSegments.push({mesh:seg,id:p.id,isBottleneck:isBottleneck,mat:isBottleneck?pipeBottleneckMat:pipeMatBase,baseOpacity:baseOpacity});
  });
  // 地表汇流方向：用低矮箭头把“路面积水 → 雨水口/管网 → 泵站”画出来。
  // 箭头只表达台账已有管段的方向示意，不把它当成测绘流速。
  window.__flowMarkers=[];
  const flowMat=new THREE.MeshBasicMaterial({color:0x8bd8ff,transparent:true,opacity:.72,depthWrite:false});
  TWIN3D_PIPES.forEach(p=>{for(let i=0;i<p.pts.length-1;i++){
    const a=W(p.pts[i][0],p.pts[i][1]),b=W(p.pts[i+1][0],p.pts[i+1][1]);
    const dx=b[0]-a[0],dz=b[1]-a[1],len=Math.hypot(dx,dz);if(len<2)continue;
    const arrow=new THREE.Mesh(new THREE.ConeGeometry(.22,.9,8),flowMat.clone());
    arrow.rotation.z=Math.PI/2;arrow.rotation.y=-Math.atan2(dz,dx);
    arrow.position.set((a[0]+b[0])/2,.58,(a[1]+b[1])/2);scene3.add(arrow);
    window.__flowMarkers.push({mesh:arrow,pipeId:p.id});
  }});
  // ---- 封路路障：台账里「已封/施工」路段是持久状态，护栏始终显示（不是跳5才封）----
  window.__roadBlocks=[];   // 供事件动画引用：封路护栏组（每组一个 Group）
  const blockMat=new THREE.MeshStandardMaterial({color:0xffc21a,roughness:.5,metalness:.2});
  const coneMat=new THREE.MeshStandardMaterial({color:0xff5a2d,roughness:.4});
  // 台账里"已封"的路段有 4 条，其中 3 条在作业点 25~325m 外。
  // 原来给每条都建两块 20~23.75m 的封路板，全景里同时出现 8 块黄板横穿路面
  // （实测 t=1.0 画面里 3 条黄色长龙），既不是本次事件的内容，也把路面切碎。
  // 只给**作业点所在那条路**建，其余路段用图例 + 路名变色表达"已封"状态即可。
  const WP0={x:-12.48,z:-1.6};
  let _pickRoad=null,_pickD=1e9;
  netRoads.forEach(rd=>{
    if(!/已封|under_construction|closed/.test(rd.label||'')&&!/已封|closed/.test(rd.detail||''))return;
    const ps=rd.pts;if(!ps||ps.length<2)return;
    for(let i=0;i<ps.length-1;i++){
      const a=W(ps[i][0],ps[i][1]),c=W(ps[i+1][0],ps[i+1][1]);
      const vx=c[0]-a[0],vz=c[1]-a[1],L2=vx*vx+vz*vz||1e-6;
      let tt=((WP0.x-a[0])*vx+(WP0.z-a[1])*vz)/L2;tt=Math.max(0,Math.min(1,tt));
      const d=Math.hypot(a[0]+vx*tt-WP0.x,a[1]+vz*tt-WP0.z);
      if(d<_pickD){_pickD=d;_pickRoad=rd;}
    }
  });
  const bkRoads=_pickRoad?[_pickRoad]:[];
  bkRoads.forEach(rd=>{if(!/已封|under_construction|closed/.test(rd.label||'')&&!/已封|closed/.test(rd.detail||''))return;
    const ps=rd.pts;if(!ps||ps.length<2)return;
    const wid=ROAD_W[rd.id]||2.8;
    // 路段方向角（世界 = 场景方向，W 是等比例缩放）
    const ang=-Math.atan2(ps[1][1]-ps[0][1],ps[1][0]-ps[0][0]);
    const grp=new THREE.Group();
    // 封控点：取路段上离作业点最近的位置，向上下游各放 1.45 世界单位（约 9m）。
    // 原来放在整段路的 7%/93% 处——R-101 全长 480m，护栏因此跑到 156m 外，
    // 而特写机位画幅只有 ±11m，封路这个动作永远拍不到（实测护栏 x=-37.6，作业点 x=-12.5）。
    // 现场本来就是"把作业区两头封住"，两头各 9m 正好落在特写画幅里。
    // 最近点必须**投影到线段上**，不能只取折线顶点：R-101 只有两个端点，
    // 取顶点的结果还是 156m 外的路端（实测 x=-46.3），封路照样在画外。
    const WP={x:-12.48,z:-1.6};
    let bx=0,bz=0,bD=1e9;
    for(let i=0;i<ps.length-1;i++){
      const a=W(ps[i][0],ps[i][1]),c=W(ps[i+1][0],ps[i+1][1]);
      const vx=c[0]-a[0],vz=c[1]-a[1];
      const L2=vx*vx+vz*vz||1e-6;
      let tt=((WP.x-a[0])*vx+(WP.z-a[1])*vz)/L2;
      tt=Math.max(0,Math.min(1,tt));
      const qx=a[0]+vx*tt,qz=a[1]+vz*tt;
      const dd=Math.hypot(qx-WP.x,qz-WP.z);
      if(dd<bD){bD=dd;bx=qx;bz=qz;}
    }
    const bp=[bx,bz];
    const dx=Math.cos(ang),dz=-Math.sin(ang);
    // 路面法向单位向量：从路中线沿它往外走就是路缘。
    const nx=Math.sin(ang), nz=Math.cos(ang);
    const barW=wid+1.0;
    // 真实施工围栏：板高 1.10m、厚 60mm、腿高同板高。K=0.16 → 板高 0.176 世界。
    // 旧值 0.8 世界 = 5.0 米高、0.26 厚 = 1.6 米厚，这尺寸是围墙不是围栏，
    // 特写里一块黄板横过去就把人和车全挡住了。
    const H=1.10*K, TH=0.06*K;
    [-1.45,1.45].forEach(function(off,k){
      const wx=bp[0]+dx*off, wz=bp[1]+dz*off;
      // 封路前：整块围栏顺着路停在路缘内侧（还没挡道）。封路时由工人把它推到路中并转正。
      // 两块板从对角推入（k=0 走南缘、k=1 走北缘），避免两块 27m 长的板在同一侧街沿上重叠。
      const sgn=(k===0?-1:1);
      // 停放点取在**路缘**（半路宽 + 0.2 世界 ≈ 1.5m 让开人行道），封路时推到路中。
      // 推入行程 ≈ 半路宽 = 8.6m，两个人各推一侧，画面上是"两块板从两边合拢把路封死"。
      const offN=wid*0.5+0.20;
      const fx=wx+nx*sgn*offN, fz=wz+nz*sgn*offN;
      const seg=new THREE.Group();
      // 组基准抬到车行道顶面 0.385：原来 seg 原点在地面基底(0)，腿长 0.9 有一半插进路面，
      // 而横板中心写 0.9 等于悬空 0.5 世界单位（3 米高），整组读起来像一堵黄墙。
      seg.position.set(fx,0.385,fz);
      seg.rotation.y=ang+Math.PI/2;
      const bar=new THREE.Mesh(new THREE.BoxGeometry(barW,H,TH),blockMat);
      bar.position.y=H/2+0.04;bar.castShadow=true;seg.add(bar);
      // 底脚改成 4 个轮子（施工围挡本来就有脚轮）：一块 17m 长的板由一个人推着横移，
      // 没有轮子在任何角度看都"推不动"；有轮子才读成"把围挡推到位"，而且不需要拆成 10 节。
      const wheelGeo=new THREE.CylinderGeometry(0.020,0.020,0.022,10);
      const wheelMat=new THREE.MeshStandardMaterial({color:0x14181c,roughness:.92});
      [-0.40,-0.14,0.14,0.40].forEach(function(f){
        const w=new THREE.Mesh(wheelGeo,wheelMat);
        w.rotation.x=Math.PI/2;
        w.position.set(barW*f,0.020,0);seg.add(w);
      });
      // 围栏脚边的锥桶：0.7m 高、底径 0.36m，摆在板前 0.9m（原来 0.95 世界 = 5.9 米外，
      // 跑到路中间去了，且 0.45/0.9 的锥底是 2.8m 宽 5.6m 高的一个巨型锥，占掉半屏）
      const cone=new THREE.Mesh(new THREE.ConeGeometry(0.18*K,0.70*K,10),coneMat);
      cone.position.set(barW*0.3,0.35*K,0.15);seg.add(cone);
      // 停放位 / 封路位：updateCollab 每帧按「推入进度 push」在两者之间插值，
      // 而 push 由推这块板的那个工人的走位驱动（见 moveCrew 里的 sealW/sealE）。
      // hand0/hand1 是工人的站位。站位不能放在板的**端头**——一块 27m 长的板被一个站在
      // 端头的人横着推，读起来是"板自己在滑"。正确姿势是站在板面外侧、手扶板面、侧身推：
      //   · 沿板方向偏 barW*0.20（在板的上游 1/5 处，不在正中，正中像在推一扇大门）
      //   · 沿路方向偏 0.14 世界（0.9m，人贴着板面站，手才够得着；0.34 时人离板 2.1m，
      //     画面里就是"一个人站在黄板旁边发呆"）
      //   · 方向取 **-路向**：机位在西南，人必须站在板的西侧（镜头这一侧），
      //     站到东侧会被 17m 长的板整个挡死——实测只露出一个黄帽子尖。
      // pat 是路向单位向量，moveCrew 用它算"人面朝板"的朝向。
      const aLong=barW*0.20, bSide=-0.14;
      seg.userData={
        fix:{x:fx,z:fz},park:{x:wx,z:wz},side:k,pat:{x:dx,z:dz},
        hand0:{x:fx+nx*sgn*aLong+dx*bSide,z:fz+nz*sgn*aLong+dz*bSide},
        hand1:{x:wx+nx*sgn*aLong+dx*bSide,z:wz+nz*sgn*aLong+dz*bSide}
      };
      grp.add(seg);
    });
    // 封路标牌
    const sign=makeLabel('封路','#ffc21a');sign.position.set(grp.children.length?grp.children[0].position.x:W(ps[0][0],ps[0][1])[0],1.30,grp.children.length?grp.children[0].position.z:W(ps[0][0],ps[0][1])[1]);
    grp.add(sign);
    grp.visible=false;   // 封路是事件动作：施工队到场后才出现，不在一开始就摆着
    grp.userData={entityId:rd.id,name:rd.label||'封路',kind:'road',detail:rd.detail||''};
    scene3.add(grp);window.__roadBlocks.push({mesh:grp});
  });
  // ---- 抢险班组：工程车驶入 → 人员下车 → 走到作业点 → 布设警示/接移动泵 ----
  // 全过程都在兴华路中线上（场景 z=160）：车从东侧驶入，停在积水东缘外侧，
  // 人员步行进入作业点。之所以不让人"凭空出现在路口"，是因为现场处置本身是有到达过程的。
  (function buildCrew(){
    const drive=W(430,160);   // 驶入起点（东侧远处，默认机位画外）
    // 停车点必须贴着作业点：原来停在 W(334,159)，离雨水口 62m，
    // 而"到场"只有 1 秒多的窗口 → 人只能以 200km/h 的速度"飞"过去。
    // 现在停在作业点西侧 1.05 世界单位（≈6.6m），下车走 7.4m 到井口，正常步速即可。
    const park =W(279,158);
    const work =W(272,155);   // 作业点：堵塞的雨水口（DF-IN-101）所在的路缘

    // 排水抢险车：驾驶室 + 车厢 + 反光条 + 顶部黄色警灯 + 四轮。
    // 尺寸按真实小型抢险车：车长 4.5m、宽 1.9m、高 1.85m（旧版 1.3 倍缩放后长 6.8m、高 4m）。
    const truck=new THREE.Group();
    const bodyMat=new THREE.MeshStandardMaterial({color:0xe9ecee,roughness:.55,metalness:.15});
    const trimMat=new THREE.MeshStandardMaterial({color:0xd2601a,roughness:.62});
    const cab=new THREE.Mesh(new THREE.BoxGeometry(1.8*K,1.2*K,1.9*K),bodyMat);cab.position.set(1.6*K,0.95*K,0);
    const bed=new THREE.Mesh(new THREE.BoxGeometry(2.6*K,1.5*K,1.9*K),bodyMat);bed.position.set(-0.7*K,1.10*K,0);
    const stripe=new THREE.Mesh(new THREE.BoxGeometry(4.4*K,0.26*K,1.94*K),trimMat);stripe.position.set(0.2*K,0.55*K,0);
    const lamp=new THREE.Mesh(new THREE.BoxGeometry(0.5*K,0.16*K,0.66*K),new THREE.MeshBasicMaterial({color:0xffb020}));
    lamp.position.set(1.6*K,1.63*K,0);
    truck.add(cab);truck.add(bed);truck.add(stripe);truck.add(lamp);
    [[1.6,0.92],[1.6,-0.92],[-1.5,0.92],[-1.5,-0.92]].forEach(function(w){
      const wh=new THREE.Mesh(new THREE.CylinderGeometry(0.38*K,0.38*K,0.24*K,12),
        new THREE.MeshStandardMaterial({color:0x1b2127,roughness:.85}));
      wh.rotation.x=Math.PI/2;wh.position.set(w[0]*K,0.38*K,w[1]*K);truck.add(wh);
    });
    truck.rotation.y=Math.PI;                 // 车头朝西（行驶方向）
    truck.position.set(drive[0],0.39,drive[1]);
    truck.visible=false;scene3.add(truck);

    // 班组 3 人（排水工程师 ×2 + 道路养护 ×1）
    // 每人放在一个 slot Group 里：这样不管是几何小人还是外部 glb，都能按阶段挪到自己的工位，
    // 让"人走到井口、蹲下开篦子、在泵边接管"这些动作能被看见，而不是三个人站在一个点上不动。
    const grp=new THREE.Group();
    const slots=[];
    for(let i=0;i<3;i++){
      const s=new THREE.Group();grp.add(s);slots.push(s);
      // 几何小人兜底（worker_rig.glb 加载失败时才用到）：缩到真实身高 1.75m，脚底贴路面
      const m=makeCrewMember();m.scale.setScalar(1.75*K/1.9);m.position.set(0,0,0);s.add(m);
    }
    grp.position.set(park[0],0,park[1]);grp.visible=false;
    scene3.add(grp);

    // 人物模型：assets/models/worker_rig.glb
    // 用户给的 Tripo T-pose 人形（5,275 verts / 10,283 tris，无贴图无骨骼），
    // 离线自动绑了 21 根骨（生成脚本带注释：D:/Users/34984/Temp/rig_worker.py），
    // 并按身高分带写入顶点色（安全帽/反光背心/反光条/工装裤/靴子）。
    //
    // 关键差别：前两版模型都是**静态扫描件**，"弯腰"只能整个 slot 前倾，手臂永远垂着；
    // 这一版躯干、肩、肘、膝都有骨，能真的摆出"猫腰对着井口、双手下探"的姿势，
    // 而且可以逐帧微动。这是这一版唯一的意义，别再退回静态模型。
    //
    // 骨骼朝向约定（绑定时无旋转，局部轴=模型轴）：+X 是正前，+Y 上，+Z 是左。
    // 因此：左臂沿 +Z 伸出，绕 X 正角=垂下；右臂沿 −Z，绕 X 负角=垂下。
    // 绕 Y 让手臂前摆：左臂正角、右臂负角。
    const RIG_POSES=[
      // 0 号：撬井盖——弯腰最深，双手下探
      {spine:[0.52,0,0],chest:[0.30,0,0],head:[-0.58,0,0],
       upperArm_L:[1.32,0.40,0],foreArm_L:[0.20,0.10,0],
       upperArm_R:[-1.32,-0.40,0],foreArm_R:[-0.20,-0.10,0],
       thigh_L:[0.16,0,0],thigh_R:[0.16,0,0],shin_L:[-0.28,0,0],shin_R:[-0.28,0,0]},
      // 1 号：扶软管——半蹲，双臂前伸
      {spine:[0.34,0,0],chest:[0.18,0,0],head:[-0.34,0,0],
       upperArm_L:[1.05,0.72,0],foreArm_L:[0.34,0.12,0],
       upperArm_R:[-1.05,-0.72,0],foreArm_R:[-0.34,-0.12,0],
       thigh_L:[0.12,0,0],thigh_R:[0.12,0,0],shin_L:[-0.22,0,0],shin_R:[-0.22,0,0]},
      // 2 号：摆锥/警戒——基本直立，手在体侧
      {spine:[0.10,0,0],chest:[0.06,0,0],head:[-0.04,0,0],
       upperArm_L:[1.18,0.14,0],foreArm_L:[0.12,0.03,0],
       upperArm_R:[-1.18,-0.14,0],foreArm_R:[-0.12,-0.03,0]}
    ];
    function poseBones(bones,pose){
      for(const k in bones){bones[k].rotation.set(0,0,0);}
      for(const k in pose){const b=bones[k];if(b)b.rotation.set(pose[k][0],pose[k][1],pose[k][2],'YXZ');}
    }

    // ---- 关键帧动作 ----
    // 素材：Quaternius Universal Animation Library（CC0 1.0）自带的 Fixing_Kneeling /
    // Crouch_Idle_Loop / Idle_Loop / Walk_Loop / Push_Loop。
    // 关键点：动画和网格来自同一个文件、同一套骨架，重定向误差恒为 0。之前把动作
    // 重定向到用户那个 Tripo 扫描件上，两套骨架手性相反（源 +X 左 / 目标 +Z 左），
    // 逐骨对齐会让滚转误差沿骨链累积，脊柱拧 18°、大腿前倾 49°——那就是"丧尸"的真因。
    // 挂 window：tick 在另一个作用域，用闭包里的 const 会直接 ReferenceError，
    // 而 tick 一抛异常整个渲染循环就停了（画面卡在上一帧，看着像没有生效）。
    window.__kneelAt=1.40;   // Fixing_Kneeling 里「跪稳、手在井口上」的时刻
    const crewAnim={clips:null,mixers:[]};
    window.__crewAnim=crewAnim;
    function buildCrewClips(anims){
      const out={};
      anims.forEach(function(c){
        if(c.name==='Fixing_Kneeling'){
          // 5.2s 是「起→跪→撬→起身」的完整叙事。这里保留整段，由 KNEEL_AT 定格在
          // 跪地相位。早先用 AnimationUtils.subclip 切 34~57 帧，切出来是
          // 「站姿 + 上身前倾」（下半身没折起来），说明该区间并不是跪地段。
          out.kneel=c;
        }
        else if(c.name==='Idle_Loop')out.idle=c;
        else if(c.name==='Walk_Loop')out.walk=c;
        else if(c.name==='Crouch_Idle_Loop')out.crouch=c;
        else if(c.name==='Push_Loop')out.push=c;
      });
      return out;
    }
    function attachCrewAnim(ci){
      const rig=rigs[ci];
      if(!rig||!rig.model||!rig.clips||rig.acts)return;
      const mixer=new THREE.AnimationMixer(rig.model);
      const acts={};
      for(const n in rig.clips){
        const a=mixer.clipAction(rig.clips[n]);
        a.setLoop(THREE.LoopRepeat,Infinity);a.enabled=true;a.setEffectiveWeight(0);a.play();
        acts[n]=a;
      }
      rig.mixer=mixer;rig.acts=acts;rig.cur=null;
      rig.phase=ci*0.46;   // 同一个片段多人播时错开相位，否则动作同拍像复制粘贴
      crewAnim.mixers.push(mixer);
      window.__crewMixers=crewAnim.mixers;   // tick 里逐帧 update
    }
    // 换片段：交叉淡入，避免姿势"啪"地跳过去
    function setCrewClip(rig,name,fade){
      if(!rig||!rig.acts||rig.cur===name)return;
      const next=rig.acts[name];if(!next)return;
      const prev=rig.cur?rig.acts[rig.cur]:null;
      const first=(rig.cur===null);
      next.reset().setEffectiveTimeScale(1).setEffectiveWeight(1).play();
      // kneel 是一次性叙事片（5.2s：起→跪→撬→起身），要定格在 KNEEL_AT 那一帧。
      // 试过两种都不行：LoopOnce+clampWhenFinished 会停在末尾（末尾是站起来）；
      // timeScale=0 在 r128 里不保证姿势被累加，渲染出来还是旧姿态。
      // 真正可靠的做法是在 tick 里每帧把 action.time 钉回去（见 __kneelHold）。
      // kneel 从头播（起→跪→撬）：到 KNEEL_AT 由 tick 钉住。
      // 原来一进来就 next.time=KNEEL_AT，等于第一帧就是"跪稳"，看不到蹲下去的过程。
      if(name==='kneel'){next.setLoop(THREE.LoopOnce,1);next.time=0;}
      // 相位错开只对循环片生效；kneel 是叙事片，跳时间会把"蹲下去"跳掉
      if(first&&rig.phase&&name!=='kneel'){
        const d=next.getClip().duration;
        if(d>0)next.time=(rig.phase*1.7)%d;
      }
      if(prev)prev.crossFadeTo(next,fade,false);else next.fadeIn(fade);
      rig.cur=name;
    }
    window.__setCrewClip=setCrewClip;

    // 人物模型：assets/models/worker_char.glb（2.3MB，9.3k 顶点，5 段动画，顶点色 + 几何五官）
    // 由 scripts/build_worker_char.py 从上面那个 CC0 动作库里提取人形网格、按部位刷
    // 工人配色（橙背心 / 两道反光带 / 深色袖 / 工装裤 / 黑靴 / 手套），
    // 另用几何补出安全帽与眼鼻嘴——源模型是拿光球当脑袋的白模，没有脸。
    // 模型面朝 +Z，与场景约定一致，不需要再转。
    // 路径带 mtime 戳：改完模型浏览器还拿缓存里的旧 glb，会看到"改了没生效"。
    const CHAR_URL='/assets/models/worker_char.glb?v=__CHAR_VER__';
    const rigs=[];
    for(let ci=0;ci<3;ci++){
      // 必须分开加载：SkinnedMesh 的骨架不能 clone 共享（改一个人的手臂会带动全部），
      // 而 SkeletonUtils 不在 r128 的最小构建里。2.3MB，三份都命中浏览器缓存。
      gltfLoader.load(CHAR_URL,function(g){
        const src=g.scene;
        const box0=new THREE.Box3().setFromObject(src);
        const h=Math.max(1e-6,box0.max.y-box0.min.y);
        src.traverse(function(o){
          if(!o.isMesh)return;
          o.castShadow=true;o.frustumCulled=false;   // 蒙皮后包围盒不再准确，关掉剔除防止特写时整人消失
          const mats=Array.isArray(o.material)?o.material:[o.material];
          mats.forEach(function(m0){
            if(!m0)return;
            // 顶点色必须在首帧编译前打开：编译后改 vertexColors 不置 needsUpdate，
            // shader 不会重编，模型就一直是纯白/纯黄的白模——"脸和裤子都没了"。
            m0.vertexColors=true;m0.roughness=.88;m0.metalness=.02;
            m0.needsUpdate=true;
          });
        });
        const sc=1.75*K/h;                       // 真实身高 1.75m
        src.scale.setScalar(sc);
        src.position.y=-box0.min.y*sc;
        const bones={};
        src.traverse(function(o){if(o.isBone)bones[o.name]=o;});
        rigs[ci]={bones:bones,model:src,clips:null};
        const slot=slots[ci];
        while(slot.children.length)slot.remove(slot.children[0]);
        slot.add(src);
        rigs[ci].clips=buildCrewClips(g.animations||[]);
        attachCrewAnim(ci);
      },undefined,function(){/* 模型缺失：保留几何小人 */});
    }
    window.__crewRigs=rigs;

    // 警示锥：到场后沿路北侧逐个摆放（4 个），圈出作业区
    const cones=[];
    const coneMat=new THREE.MeshStandardMaterial({color:0xff8c2d,roughness:.5});
    const baseMat2=new THREE.MeshStandardMaterial({color:0x2a2f35,roughness:.7});
    for(let i=0;i<4;i++){
      const g=new THREE.Group();
      // 真实警示锥：底宽 0.36m、高 0.7m；旧版 0.30 世界单位＝1.9m 高，比人还高
      const cb=new THREE.Mesh(new THREE.ConeGeometry(0.18*K,0.70*K,10),coneMat);cb.position.y=0.37*K;
      const bs=new THREE.Mesh(new THREE.BoxGeometry(0.40*K,0.03,0.40*K),baseMat2);bs.position.y=0.015;
      g.add(cb);g.add(bs);
      // 从泵东侧沿路一字排开（间距 1.6m），把作业区上游圈出来；
      // 起点让开泵体，锥桶不压在机器和人身上
      g.position.set(work[0]+0.60+i*0.26,0.40,work[1]-0.22);
      g.visible=false;scene3.add(g);cones.push(g);
    }

    // 移动泵机组（自吸式污水泵 + 两轮小车）：到场后从车上卸下，摆在**井口东侧**。
    // 上一版只有一个 1.2×0.9×0.8 的蓝色方块 + 一个黄盖子，特写里就是一个"蓝箱子"，
    // 不像机器。这里用纯几何补到能读出来：底盘、轮胎、泵壳、发动机、进出水口、
    // 排气管、控制面板、压力表、扶手 —— 全部 Box/Cylinder，零资产。
    const pump=new THREE.Group();
    const matSteel=new THREE.MeshStandardMaterial({color:0x7d848c,roughness:.55,metalness:.6});
    const matDark=new THREE.MeshStandardMaterial({color:0x24282d,roughness:.8,metalness:.25});
    const matBody=new THREE.MeshStandardMaterial({color:0x2f6d8c,roughness:.45,metalness:.35});
    const matOrange=new THREE.MeshStandardMaterial({color:0xd9a13a,roughness:.45,metalness:.4});
    // 底盘 + 轮子（可推行的小车）
    const skid=new THREE.Mesh(new THREE.BoxGeometry(1.30*K,0.07*K,0.86*K),matSteel);
    skid.position.y=0.10*K;pump.add(skid);
    [-0.45,0.45].forEach(function(x){
      const wh=new THREE.Mesh(new THREE.CylinderGeometry(0.17*K,0.17*K,0.09*K,14),matDark);
      wh.rotation.x=Math.PI/2;wh.position.set(x*K,0.17*K,0.40*K);pump.add(wh);
    });
    // 泵壳（蜗壳外形用圆柱近似）+ 发动机
    const volute=new THREE.Mesh(new THREE.CylinderGeometry(0.30*K,0.30*K,0.34*K,18),matBody);
    volute.rotation.z=Math.PI/2;volute.position.set(-0.26*K,0.44*K,0);pump.add(volute);
    const eng=new THREE.Mesh(new THREE.BoxGeometry(0.52*K,0.40*K,0.62*K),
      new THREE.MeshStandardMaterial({color:0x363c44,roughness:.6,metalness:.35}));
    eng.position.set(0.30*K,0.50*K,0);pump.add(eng);
    const engTop=new THREE.Mesh(new THREE.BoxGeometry(0.40*K,0.10*K,0.44*K),matSteel);
    engTop.position.set(0.30*K,0.74*K,0);pump.add(engTop);
    // 油箱盖
    const pcap=new THREE.Mesh(new THREE.CylinderGeometry(0.07*K,0.07*K,0.05*K,12),matOrange);
    pcap.position.set(0.30*K,0.81*K,0.12*K);pump.add(pcap);
    // 进/出水口：进水朝西（接软管到井口），出水朝东（接排水带）
    const inlet=new THREE.Mesh(new THREE.CylinderGeometry(0.085*K,0.085*K,0.16*K,12),matSteel);
    inlet.rotation.z=Math.PI/2;inlet.position.set(-0.58*K,0.40*K,0);pump.add(inlet);
    const outlet=new THREE.Mesh(new THREE.CylinderGeometry(0.075*K,0.075*K,0.16*K,12),matSteel);
    outlet.rotation.z=Math.PI/2;outlet.position.set(0.58*K,0.44*K,0);pump.add(outlet);
    // 排气管 + 控制面板 + 压力表
    const exh=new THREE.Mesh(new THREE.CylinderGeometry(0.042*K,0.042*K,0.26*K,10),matDark);
    exh.position.set(0.52*K,0.86*K,-0.20*K);pump.add(exh);
    const panel=new THREE.Mesh(new THREE.BoxGeometry(0.20*K,0.26*K,0.05*K),matDark);
    panel.position.set(0.10*K,0.52*K,0.32*K);pump.add(panel);
    const gauge=new THREE.Mesh(new THREE.CylinderGeometry(0.055*K,0.055*K,0.02*K,14),
      new THREE.MeshStandardMaterial({color:0xe8eef2,roughness:.3,metalness:.1}));
    gauge.rotation.x=Math.PI/2;gauge.position.set(0.10*K,0.58*K,0.35*K);pump.add(gauge);
    // 扶手：两根细钢杆
    [-0.3,0.3].forEach(function(z){
      const bar=new THREE.Mesh(new THREE.CylinderGeometry(0.018*K,0.018*K,0.34*K,8),matSteel);
      bar.rotation.z=-0.5;bar.position.set(0.66*K,0.62*K,z*K);pump.add(bar);
    });
    pump.position.set(work[0]+0.38,0.40,work[1]-0.05);
    pump.visible=false;scene3.add(pump);

    // 进水管：从泵体西侧接到掀开的雨水口，贴地走，中间压扁一段。
    // "泵在抽水"这件事必须有物理连接才成立——上一版软管是泵身上一根朝西伸出去的短圆柱，
    // 端头离井口还有 2m，看着像一根没插上的棍子。
    const hoseGrp=new THREE.Group();hoseGrp.position.set(work[0],0.40,work[1]);
    const hoseCurve=new THREE.CatmullRomCurve3([
      new THREE.Vector3(0.30,0.09,-0.05),
      new THREE.Vector3(0.22,0.020,-0.02),
      new THREE.Vector3(0.13,0.014,0.01),
      new THREE.Vector3(0.05,0.026,0.03)
    ]);
    const hoseMesh=new THREE.Mesh(new THREE.TubeGeometry(hoseCurve,26,0.013,8,false),
      new THREE.MeshStandardMaterial({color:0x1a1e22,roughness:.85,metalness:.05}));
    hoseMesh.castShadow=true;hoseGrp.add(hoseMesh);
    hoseGrp.visible=false;scene3.add(hoseGrp);

    // 被掀开的铸铁篦子：斜靠在井口东侧地面上。井盖掀开 = "人在动井口"的直接证据。
    const lidGrp=new THREE.Group();lidGrp.position.set(work[0]-0.01,0.40,work[1]-0.11);
    const lid=new THREE.Mesh(new THREE.BoxGeometry(0.20,0.016,0.12),
      new THREE.MeshStandardMaterial({color:0x2b3238,roughness:.42,metalness:.8}));
    lid.position.set(0,0.045,0);lid.rotation.z=-1.15;lid.castShadow=true;lidGrp.add(lid);
    lidGrp.visible=false;scene3.add(lidGrp);

    // 掏出/冲出的淤泥：井口南侧一小片深色湿泥，别做成整齐的土堆
    const mudGrp=new THREE.Group();mudGrp.position.set(work[0]-0.02,0.40,work[1]-0.12);
    const mudMat=new THREE.MeshStandardMaterial({color:0x3b332a,roughness:.98,metalness:0});
    [[0,0,0,0.055],[0.06,0,0.02,0.038],[-0.05,0,0.03,0.032]].forEach(function(m){
      const b=new THREE.Mesh(new THREE.SphereGeometry(m[3],10,7),mudMat);
      b.scale.y=0.34;b.position.set(m[0],0.008,m[2]);mudGrp.add(b);
    });
    mudGrp.visible=false;scene3.add(mudGrp);

    // 撬棍：插在井口盖缝里的细钢杆，跟着 0 号工人的弯腰节奏轻晃
    const pryGrp=new THREE.Group();pryGrp.position.set(work[0]-0.01,0.40,work[1]-0.09);
    // 直径按实物：撬棍杆径 2.5cm → 0.004 世界单位。原来写 0.008/0.010（直径 10cm、长 1m）
    // 在这个机位下读成一根白色 PVC 管，是画面里最出戏的东西。
    // 杆径按实物 2.5cm 在 11m 外只有 2 个像素，读不出来；这里放到 3.5cm 并把
    // 颜色提到浅镀锌灰，靠对比度而不是靠尺寸去"被看见"。
    const pry=new THREE.Mesh(new THREE.CylinderGeometry(0.0050,0.0060,0.16,8),
      new THREE.MeshStandardMaterial({color:0x9aa3ac,roughness:.38,metalness:.75}));
    pry.rotation.z=0.95;pry.position.y=0.07;pry.castShadow=true;pryGrp.add(pry);
    pryGrp.visible=false;scene3.add(pryGrp);

    // 抽水出水口：泵启动后朝路侧喷一股水，落点有溅起的水花。
    // 这是"工作过程"最关键的可见证据——水位之所以开始下降，是因为泵在抽水。
    // 原来是一根直圆柱斜插在地上，读成"一根白色 PVC 管"。改成抛物线水舌 + 落点水花：
    // 有弧度、越远越细、半透明，才像喷出来的水而不是插着的东西。
    const jet=new THREE.Group();
    const jetMat=new THREE.MeshBasicMaterial({color:0xd6ecf7,transparent:true,opacity:.40,depthWrite:false});
    const arcCurve=new THREE.CatmullRomCurve3([
      new THREE.Vector3(0.00,0.09,0),
      new THREE.Vector3(0.13,0.085,0.01),
      new THREE.Vector3(0.26,0.045,0.02),
      new THREE.Vector3(0.36,0.012,0.03),
      new THREE.Vector3(0.44,0.006,0.03)
    ]);
    const stream=new THREE.Mesh(new THREE.TubeGeometry(arcCurve,22,0.011,8,false),jetMat);
    jet.add(stream);
    // 落点水花：两圈同心环 + 一层溅起的水雾，全部贴地、depthWrite 关闭
    const splash=new THREE.Mesh(new THREE.RingGeometry(0.05,0.13,20),jetMat.clone());
    splash.rotation.x=-Math.PI/2;splash.position.set(0.44,0.006,0.03);jet.add(splash);
    const splash2=new THREE.Mesh(new THREE.RingGeometry(0.13,0.20,20),jetMat.clone());
    splash2.material.opacity=0.18;
    splash2.rotation.x=-Math.PI/2;splash2.position.set(0.44,0.005,0.03);jet.add(splash2);
    jet.position.set(work[0]+0.52,0.40,work[1]-0.05);   // 泵体东侧出水管口
    jet.visible=false;scene3.add(jet);

    // 现场状态文字：只保留一条，随阶段换文案（"他们此刻在干什么"）。
    // 原来这里是 1 个角色标签 + 3 个动作标签 + 1 个状态，五条贴在同一个点上，
    // 远景直接糊成一坨——一条滚动文案反而读得清。
    const status=makeLabel('','#ffe0a6');
    status.scale.set(LABEL_W*0.5,LABEL_H*0.62,1);   // 小一号：现场文案不能压住人和车
    status.position.set(work[0]-0.06,0.92,work[1]+0.34);status.visible=false;scene3.add(status);

    // 到场标记光柱：只做远景指示，特写里那根半透明圆柱会挡住整片作业面，所以不挂到场景上。
    const marker=new THREE.Mesh(new THREE.CylinderGeometry(0.8,1.4,2.2,14,1,true),
      new THREE.MeshBasicMaterial({color:0xff8c2d,transparent:true,opacity:.32,side:THREE.DoubleSide,depthWrite:false}));
    marker.position.set(work[0],1.5,work[1]);marker.visible=false;
    window.__roadCrew={group:grp,truck:truck,pump:pump,jet:jet,cones:cones,status:status,
      hose:hoseGrp,lid:lidGrp,mud:mudGrp,pry:pryGrp,pryBar:pry,
      slots:slots,marker:marker,label:null,actions:[],
      drive:{x:drive[0],z:drive[1]},park:{x:park[0],z:park[1]},
      from:{x:park[0],z:park[1]},to:{x:work[0],z:work[1]},
      // 特写机位对准"人 + 井口 + 泵"这一小片，并往水侧（兴华路北半幅）偏 0.55 世界单位：
      // 对着 to（路缘上的雨水口）时，画面下缘正好压在南侧路缘石上，整片积水被切在画面外，
      // 读起来像"一群人在干燥路面上围着箱子"，看不出是在排内涝。
      focus:{x:work[0]-0.02,z:work[1]+0.20}};
  })();
  // ---- 派单气泡：部门落位→派活（跨部门协作的可视化）----
  (function buildDispatch(){
    const pos=W(330,120);   // 调度中心附近
    const bub=makeLabel('派单·排水→养护','#ffd36b');
    bub.position.set(pos[0],5.5,pos[1]);bub.visible=false;
    scene3.add(bub);
    const flow=[
      ['应急调度中心·接警',pos[0]-5,pos[1]+2.2],
      ['排水部门·接收工单',pos[0]+2,pos[1]+2.2],
      ['道路养护·确认封路',pos[0]+8,pos[1]+2.2]
    ].map((x,i)=>{const lab=makeLabel(x[0],i===2?'#ffe0a6':'#bde8ff');lab.position.set(x[1],4.6,x[2]);lab.visible=false;scene3.add(lab);return lab;});
    window.__dispatchBubble=bub;
    window.__dispatchFlow=flow;
  })();
  // ---- 跨部门协作层：让"谁指挥、卡派给谁、封路为什么必须两个部门一起签"在画面里发生 ----
  // 不是侧栏文字，是四件能看见的事：
  //   ① 每个部门一块站牌，牌面写「部门｜这一跳在干什么」，颜色 = 该部门动作卡在**这一跳**的状态
  //      （灰=待接单 / 蓝=执行中 / 橙=待会签·待确认 / 绿=已完成）
  //   ② 第 3 跳「部门落位」：三张任务卡从应急局飞到排水/道路/交警的站点＝派单
  //   ③ 第 6 跳「处置顺序」：道路和交警两张卡分别从封路段两端飞到路中央会合，
  //      出现「联合会签 ✓」之后护栏才升起——这就是"单一部门发布无效"的可视化
  //   ④ 同跳：一张「移动泵 400m³/h」从泵站飞到作业点，随后泵启动出水
  // 数据全部来自 /api/twin-links 的动作卡（归口部门 + 7 跳状态 + 会签要求）
  // 与 /api/scheduler 的部门台账，不写死"谁在第几跳做什么"。
  (function buildCollab(){
    const work=W(272,155), emerg=W(330,120);
    const segs=(window.__roadBlocks&&window.__roadBlocks[0]&&window.__roadBlocks[0].mesh.children)||[];
    const blk=segs[0]?{x:segs[0].position.x,z:segs[0].position.z}:{x:work[0]+1.4,z:work[1]};
    const ps=W(285,151.1);            // 兴华路雨水泵站 DF-PS-01（台账坐标，与泵房用同一套投影）
    const SITES=[
      // 动作卡里的总控 agent_id 是 EMERG-COORD，不是部门台账的 ORG-EMERG——
      // 用错键的结果是"应急局"站牌永远不显示（实测 site[0] 恒为 0）。
      {org:'EMERG-COORD',x:emerg[0],          y:5.9,  z:emerg[1]},
      {org:'ORG-DRAIN',  x:work[0]+0.56,      y:1.42, z:work[1]+0.34},
      {org:'ORG-ROAD',   x:blk.x,             y:2.10, z:blk.z},
      {org:'ORG-TRAFFIC',x:blk.x+1.55,        y:2.70, z:blk.z-0.75},
      {org:'ORG-POWER',  x:ps[0],             y:2.55, z:ps[1]}
    ];
    function siteSprite(){
      const sp=makeLabel(' ','#cfe6f5');
      sp.material.depthTest=false;sp.renderOrder=130;   // 协作层是"管理层"，不被楼体挡住
      sp.scale.set(LABEL_W*0.78,LABEL_H*0.78,1);
      sp.visible=false;scene3.add(sp);return sp;
    }
    SITES.forEach(function(s){s.sp=siteSprite();s.sp.position.set(s.x,s.y,s.z);s.key='';});
    // 每个工人头顶挂一块"我是哪个部门、在干什么"的牌子。
    // 没有这块牌子，画面里就只是"三个橙衣人在动"，观众没法把人和部门对上——
    // 这是"看不到怎么跨部门"最直接的一处。
    const CREW_TAGS=[['排水·开篦子','#5cc8f5'],['排水·接吸水管','#5cc8f5'],['道路·布锥警戒','#ffc21a']];
    ((window.__roadCrew||{}).slots||[]).forEach(function(sl,i){
      if(!CREW_TAGS[i])return;
      const tag=makeLabel(CREW_TAGS[i][0],CREW_TAGS[i][1]);
      tag.material.depthTest=false;tag.renderOrder=133;
      tag.scale.set(LABEL_W*0.58,LABEL_H*0.58,1);
      tag.position.set(0,0.60,0);
      sl.add(tag);
    });
    // 飞行卡：一块小牌子在两站之间划一道弧线，读起来就是"这张卡从 A 交到 B"
    function flySprite(text,col){
      const sp=makeLabel(text,col||'#ffe6a8');
      sp.material.depthTest=false;sp.renderOrder=131;
      sp.scale.set(LABEL_W*0.86,LABEL_H*0.86,1);
      sp.visible=false;scene3.add(sp);return sp;
    }
    const A={x:emerg[0],y:4.6,z:emerg[1]};
    const D={x:work[0]+0.56,y:1.3,z:work[1]+0.34};
    const R={x:blk.x,y:1.9,z:blk.z};
    const T={x:blk.x+1.55,y:2.4,z:blk.z-0.75};
    const P={x:ps[0],y:1.6,z:ps[1]};
    const MID={x:blk.x,y:1.55,z:blk.z};
    window.__collab={sites:SITES,flights:[],badge:null,built:true};
    // 派单弧线：起点略抬高，避免从楼里穿出来
    window.__collab.flights.push(
      {sp:flySprite('派单 → 排水公司','#9fe6ff'),t0:0.336,t1:0.392,a:A,b:D,lift:1.2},
      {sp:flySprite('派单 → 道路中心','#ffd98a'),t0:0.350,t1:0.406,a:A,b:R,lift:1.4},
      {sp:flySprite('派单 → 交警一大队','#ffb08a'),t0:0.364,t1:0.420,a:A,b:T,lift:1.6},
      // 会签：两张卡从封路段两端飞到路中央会合
      {sp:flySprite('道路中心 · 拟封','#ffd98a'),t0:0.700,t1:0.748,a:{x:blk.x+1.1,y:1.9,z:blk.z},b:MID,lift:0.5},
      {sp:flySprite('交警 · 会签','#ffb08a'),t0:0.706,t1:0.752,a:{x:blk.x-1.1,y:1.9,z:blk.z},b:MID,lift:0.5},
      // 调泵：泵站把移动泵交到作业点
      {sp:flySprite('移动泵 400m³/h → 作业点','#a8e6a0'),t0:0.792,t1:0.846,a:P,b:D,lift:0.7}
    );
    // 会签结果徽标：合流之后才出现，紧跟着才是护栏升起
    const badge=makeLabel('道路＋交警 · 联合会签 ✓','#8ef0b8');
    badge.material.depthTest=false;badge.renderOrder=132;
    badge.scale.set(LABEL_W*0.92,LABEL_H*0.92,1);
    badge.position.set(blk.x,2.35,blk.z);badge.visible=false;
    scene3.add(badge);window.__collab.badge=badge;
    // 封路动作的位姿**已经在 build 阶段算好了**（见 __roadBlocks 构建处的 userData：
    // fix / park / hand0 / hand1 / pat / side）。这里绝不能再按公式重算一遍——
    // 原来这段用 `seg.position + sin(ry)*(w*0.5+0.55)` 覆盖了 park，那是个"沿路方向平移
    // 半个板长"的旧公式；而工人站位 hand0/hand1 是按"沿法向平移 0.30·路宽"算的。
    // 两份数据方向差 90°：围栏横着往东滑、人却往北走，画面上就是"人推着空气"。
  })();
  // 场景摆完才建光环与锚点：此时每个实体的 mesh 已经挂在 scene 上
  buildRings();
  loadTwinLinks().then(()=>{
    // 默认从「降雨开始」（t=0）起：先下雨、再积水、再处置。
    // 只有深链接带了 jump/entity 参数时才直接定位到指定跳次。
    const q=new URLSearchParams(location.search);
    // ?t=0.73 直接定位到时间轴任意时刻（0~1），用于核对"某一刻现场到底有什么"
    const qt=q.get('t');
    if(qt!==null&&isFinite(Number(qt))){
      __evtClock=Math.max(0,Math.min(1,Number(qt)))*EVT_DURATION;
      applyTimeline(__evtClock/EVT_DURATION);paintHighlights();renderJumpControls();updateEvidence();renderResults();
      syncJumpFromClock();driveCamera(__evtClock/EVT_DURATION);return;
    }
    if(q.get('jump')||REQUESTED.entity){setJump(twinState.currentJump);return}
    twinState.currentJump=0;__evtClock=0;__evtPlaying=false;
    applyTimeline(0);paintHighlights();renderJumpControls();updateEvidence();renderResults();
    renderEventBar(0);syncProgress();
  });
}
// 标签用「屏幕恒定尺寸」：sizeAttenuation=false，缩放按 1 单位距离下的世界尺寸给。
// 原来 scale 是 7.2×1.35 世界单位（≈45m 宽的广告牌），远景刚好，一旦相机拉近
// 到现场特写就变成铺满半个屏幕的大字。恒定尺寸下远近一样大，这才是标注该有的行为。
const LABEL_W=0.27, LABEL_H=0.0505;   // ≈ 默认机位下 7.2×1.35 世界单位的观感
function makeLabel(text,color){const cv=document.createElement('canvas');cv.width=512;cv.height=96;const g=cv.getContext('2d');g.font='bold 26px system-ui';g.textAlign='center';g.textBaseline='middle';g.lineWidth=5;g.strokeStyle='rgba(10,16,22,.88)';g.strokeText(text,256,48);g.fillStyle=color||'#d5e4f0';g.fillText(text,256,48);const tex=new THREE.CanvasTexture(cv);tex.minFilter=THREE.LinearFilter;const mat=new THREE.SpriteMaterial({map:tex,transparent:true,depthTest:true,depthWrite:false,sizeAttenuation:false});const sp=new THREE.Sprite(mat);sp.scale.set(LABEL_W,LABEL_H,1);return sp}
// 只生成 CanvasTexture（供积水深度标注动态刷新文字用），不包 Sprite
function makeLabelTex(text,color){const cv=document.createElement('canvas');cv.width=256;cv.height=64;const g=cv.getContext('2d');g.font='bold 30px system-ui';g.textAlign='center';g.textBaseline='middle';g.lineWidth=6;g.strokeStyle='rgba(10,16,22,.9)';g.strokeText(text,128,32);g.fillStyle=color||'#d5e4f0';g.fillText(text,128,32);const tex=new THREE.CanvasTexture(cv);tex.minFilter=THREE.LinearFilter;return tex}
// ---- 水面 shader：连续水体（多水洼场融合）+ 真实波纹 + Fresnel + 边缘羽化 ----
// 关键：不是若干个矩形水面拼接（会"一段一段"），而是一整片水面，用若干个椭圆水洼场叠加出连续形状，
// 片元里按场值裁出水面、边界再叠加噪声与羽化，得到不规则、自然的水岸线。
// blobs = [[局部x, 局部z, 半径x, 半径z], ...]，局部坐标相对水面平面中心。
function makeWaterSurface(w,d,blobs){
  // 8 个水洼用 8 个独立 vec4 uniform（x, z, 半径x, 半径z）传入。
  // 不用 uniform 数组 + 循环索引：WebGL1 对动态数组索引支持不一致，会直接编译失败。
  const mk=(b)=>new THREE.Vector4(b?b[0]:99999,b?b[1]:99999,b?Math.max(0.05,b[2]):0.001,b?Math.max(0.05,b[3]):0.001);
  const uB={};
  for(let i=0;i<8;i++)uB['uB'+i]={value:mk((blobs||[])[i])};
  // 每个水洼的「到场程度」0~1：按离积水核心的距离排队，近的先长、远的后漫。
  // 分开传是为了让积水按顺序蔓延，而不是整片同时放大（那一版看着像水面在缩放的动画）。
  const uG={
    uG0:{value:new THREE.Vector4(0,0,0,0)},
    uG1:{value:new THREE.Vector4(0,0,0,0)},
  };
  const geo=new THREE.PlaneGeometry(w,d,96,96);geo.rotateX(-Math.PI/2);
  const mat=new THREE.ShaderMaterial({
    uniforms:Object.assign({
      uTime:{value:0},
      uLevel:{value:0.5},                                  // 水位系数：越高水洼越外扩、越连片
      uOpacity:{value:0.86},
      // 这三个颜色**不要** convertSRGBToLinear()：ShaderMaterial 不像内置材质那样
      // 在片元末尾插 encodings_fragment，它把值直接写进 sRGB 画布。
      // 转了线性等于把颜色压暗一次再原样输出：uDeepColor 转完是 (0.7,9,22)，
      // 深水区直接变成近黑的"黑洞"，这也是之前水面怎么调都发黑的原因。
      // 注意：uWaterColor 与 uOpacity 这两个初值每帧会被 applyTimeline 覆盖
      // （颜色来自 WATER_COLORS、透明度按水位算），改这里的数没有用。
      // 真正生效的是 WATER_COLORS 和 applyTimeline 里那两行；这里只保证首帧不为空。
      uWaterColor:{value:new THREE.Color(0x566051)},
      uDeepColor:{value:new THREE.Color(0x2f4148)},     // 深水区更深，青灰而不是藏蓝，也不回到绿
      uSkyColor:{value:new THREE.Color(0x8fa0b0)},
      uWaveAmp:{value:0.022},   // 波幅压到 2cm 级：原来 0.04 的波谷会沉到路缘石以下，水面被切成两半
      // 天空原图（equirect）+ 是否已加载。加载前保持 0，shader 走常数天空色兜底，
      // 避免 sampler 绑到 null 纹理导致整片水面不渲染。
      uSky:{value:window.__skyTex||null},
      uHasSky:{value:window.__skyTex?1:0},
      // 平面反射（纯代码，不依赖任何额外资产）：镜像相机渲染一遍场景到 RT，
      // 这里按反射相机的投影坐标采样。uHasRefl=0 时 shader 走原来的天空反射兜底。
      uRefl:{value:new THREE.Texture()},
      uReflVP:{value:new THREE.Matrix4()},
      uHasRefl:{value:0},
    },uB,uG),
    vertexShader:[
      'uniform float uTime;',
      'uniform float uWaveAmp;',
      'varying vec2 vUv;varying vec2 vLocal;varying vec3 vPos;varying vec3 vNormal;',
      'varying vec3 vWorld;varying vec3 vNormalW;',
      'void main(){',
      '  vUv=uv;',
      '  vec3 p=position;',
      '  vLocal=p.xz;',
      // 顶点真实波浪位移：让水面有几何起伏，不是纯平面贴片
      '  float w1=sin(p.x*0.9+uTime*1.6)*0.55;',
      '  float w2=cos(p.z*0.8+uTime*1.2)*0.45;',
      '  float w3=sin((p.x+p.z)*0.6+uTime*2.0)*0.3;',
      '  p.y+=(w1+w2+w3)*uWaveAmp;',
      '  vec4 mv=modelViewMatrix*vec4(p,1.0);',
      '  vPos=mv.xyz;',
      '  vNormal=normalize(normalMatrix*vec3(0.0,1.0,0.0));',
      // 世界空间的片元位置与法线：反射采样要用世界方向查天空图，
      // view 空间算出来的反射方向查 equirect 会整体偏转（水面倒影和楼的位置对不上）。
      '  vWorld=(modelMatrix*vec4(p,1.0)).xyz;',
      '  vNormalW=normalize(mat3(modelMatrix)*vec3(0.0,1.0,0.0));',
      '  gl_Position=projectionMatrix*mv;',
      '}',
    ].join(' '),
    fragmentShader:[
      'uniform float uTime;',
      'uniform float uLevel;',
      'uniform float uOpacity;',
      'uniform vec3 uWaterColor;uniform vec3 uDeepColor;uniform vec3 uSkyColor;',
      'uniform vec4 uB0;uniform vec4 uB1;uniform vec4 uB2;uniform vec4 uB3;',
      'uniform vec4 uB4;uniform vec4 uB5;uniform vec4 uB6;uniform vec4 uB7;',
      // uG0/uG1：每个水洼的到场程度 0~1（近的先长、远的后漫）。
      // 注意：shader 的每一行是拼成一整行再编译的，字符串里不能出现 // 行注释，
      // 否则会把后面所有代码一起注释掉（报错是 "Missing main()"）。
      'uniform vec4 uG0;uniform vec4 uG1;',
      'uniform sampler2D uSky;uniform float uHasSky;',
      'uniform sampler2D uRefl;uniform mat4 uReflVP;uniform float uHasRefl;',
      'varying vec2 vUv;varying vec2 vLocal;varying vec3 vPos;varying vec3 vNormal;',
      'varying vec3 vWorld;varying vec3 vNormalW;',
      // 单个水洼场：椭圆高斯，中心 1.0 向外衰减。g 是该水洼自己的生长系数：
      // g=0 完全不出现，g=1 长到满幅，中间按 0.32 起步的半径 + 幅度淡入，
      // 所以积水是「从低洼点一段段淌出去」，不是整片同时缩放。
      'float blob(vec4 b,vec2 p,float g){',
      '  if(g<=0.002)return 0.0;',
      '  vec2 rad=b.zw*(0.32+0.68*g);',
      '  vec2 dv=(p-b.xy)/max(rad,vec2(0.001));',
      '  return exp(-dot(dv,dv)*1.55)*smoothstep(0.0,0.30,g);',
      '}',
      'void main(){',
      // 水面形状不再由统一 grow 缩放，而是每个水洼有自己的生长曲线（CPU 每帧按水位算）。
      // 原来所有水洼共用一个 grow，结果是「整片水一起放大/缩小」，看不到漫延过程。
      '  float field=blob(uB0,vLocal,uG0.x)+blob(uB1,vLocal,uG0.y)+blob(uB2,vLocal,uG0.z)+blob(uB3,vLocal,uG0.w)',
      '             +blob(uB4,vLocal,uG1.x)+blob(uB5,vLocal,uG1.y)+blob(uB6,vLocal,uG1.z)+blob(uB7,vLocal,uG1.w);',
      // 场值裁到 1.0 再用于着色：多个水洼重叠处场值会累加到 2~3，
      // 直接拿去算深度会在水里留下几块「黑斑」；裁顶后整片水同深度、只在岸边变浅。
      '  float f=clamp(field,0.0,1.0);',
      // 水岸线噪声：只保留中低频起伏（波长 2.5~4 世界单位≈15~25m），
      // 原来叠了一项频率 5.3/4.1 且不带时间项的高频正弦，边界会长出一圈静态尖刺，
      // 远看像锯齿而不是水岸。
      '  float n=sin(vLocal.x*1.6+uTime*0.30)*0.055',
      '         +sin(vLocal.y*2.2-uTime*0.24)*0.045',
      '         +sin((vLocal.x+vLocal.y)*0.9+uTime*0.10)*0.035',
      '         +sin(vLocal.x*3.1-vLocal.y*2.6)*0.012;',
      // 水岸线过渡带收窄到 0.30~0.36：远景宽过渡带能藏住"纸片边"，
      // 但特写镜头下这十几米宽的羽化会读成一片雾，看不出水到哪儿为止。
      // 过渡带 0.30~0.36 → 0.31~0.345：0.06 的场值宽度在近景要铺开好几米，
      // 水线读成"一片雾"。收窄到 0.035，边界才立得住，但仍是渐变不是剪纸。
      '  float body=smoothstep(0.31+n,0.345+n,f);',
      // 湿痕外缘：水退过/正在漫的低洼处是"湿沥青"，不是水。
      // 加上这一圈的意义有两层——特写里水边不再是一条羽化的软边，而是"水→湿→干"的过渡；
      // 远景俯视时水面本来就不反光（45° 入射反射率仅 2%），全靠"这一片被浸湿变暗"才读得出积过水。
      // 湿痕带放宽到 0.19~0.315：水线收硬之后，如果湿痕也一起收窄，
      // 积水就变成"贴在地上的一条绿纸"，边缘没有"水→湿→干"的过渡。
      // 水线锐利 + 外围湿痕宽，这两件事必须一起做。
      '  float damp=smoothstep(0.19+n,0.315+n,f);',
      '  if(damp<0.01) discard;',
      // 中频波纹：波长按**世界单位**算（≈0.25~0.4 世界单位 = 1.5~2.5m 的风纹），不是按 uv。
      // 原来用 vUv*26：水面平面有几十世界单位宽，26 次重复摊下来波长 7m 级别，
      // 远景看不出问题，特写镜头一推近就是一整块死平的塑料板。
      '  float a=sin(vLocal.x*22.0+uTime*3.4);',
      '  float b=sin(vLocal.y*26.0-uTime*2.9);',
      '  float c=sin((vLocal.x+vLocal.y)*16.0+uTime*2.2);',
      '  float d=sin((vLocal.x-vLocal.y*1.3)*36.0-uTime*3.8);',
      '  float ripple=a*0.28+b*0.28+c*0.24+d*0.20;',
      '  float light=0.90+0.22*ripple;',
      // 法线扰动：把波纹的解析梯度当法线斜率用。
      // 这是"水面有没有质感"的关键一步——之前法线恒为 (0,1,0)，反射和镜面高光是一整片
      // 均匀的色块，特写里就是"铺了一层带噪点的塑料膜"。让法线随波起伏，
      // 高光才会碎成一条条闪动的水光。
      '  float ca=cos(vLocal.x*22.0+uTime*3.4);',
      '  float cb=cos(vLocal.y*26.0-uTime*2.9);',
      '  float cc=cos((vLocal.x+vLocal.y)*16.0+uTime*2.2);',
      '  float cd=cos((vLocal.x-vLocal.y*1.3)*36.0-uTime*3.8);',
      '  float gx=0.28*22.0*ca+0.24*16.0*cc+0.20*36.0*cd;',
      '  float gz=0.28*26.0*cb-0.24*16.0*cc-0.20*1.3*36.0*cd;',
      // 雨滴涟漪：暴雨积水与"静止水坑"最本质的区别，就是水面上永远有不断扩散的圈。
      // 用格点哈希给每个格子一个独立的落点相位，环从中心向外扩散、同时衰减。
      // 它同时扰动法线（让倒影碎开）和亮度（让水面有明暗闪动），一举两得。
      '  vec2 rc=vLocal*10.0;',
      '  vec2 rci=floor(rc);',
      '  vec2 rcf=fract(rc)-0.5;',
      '  float rh=fract(sin(dot(rci,vec2(127.1,311.7)))*43758.5453);',
      '  float rph=fract(uTime*0.62+rh*3.7);',
      '  float rd=length(rcf);',
      '  float rr=rph*0.56;',
      '  float rw=exp(-pow((rd-rr)/0.07,2.0))*(1.0-rph);',
      '  vec2 rdir=normalize(rcf+vec2(0.0001,0.0002));',
      '  gx+=rdir.x*rw*15.0;',
      '  gz+=rdir.y*rw*15.0;',
      '  light+=rw*0.34;',

      '  vec3 N=normalize(vec3(-gx*0.0022,1.0,-gz*0.0022));',
      '  vec3 Vw=normalize(cameraPosition-vWorld);',
      '  float fres=pow(1.0-max(dot(N,Vw),0.0),3.0);',
      '  vec3 H=normalize(Vw+vec3(0.0,1.0,0.0));',
      '  float spec=pow(max(dot(N,H),0.0),60.0);',
      // 颜色深浅由水位（uLevel）统一决定，不再用场值 f 做映射：
      // 用 f 会让每个水洼中心都比岸边深，几个水洼连起来就成了「一条条深沟」，
      // 而真实路面积水是薄薄一层、颜色基本均匀，只有边缘因水浅而变淡。
      '  float deep=0.35+0.65*uLevel;',
      // 深浅不再只吃全局 uLevel：用 f（该点到水洼中心的场值）当深度代理，
      // 中心深、边缘薄。整片均匀的一层灰是"水像油漆"的主因——
      // 真实积水一定是中间发暗、往岸边越来越薄、越来越接近湿沥青。
      '  float shallow=clamp(f*1.7,0.0,1.0);',
      '  vec3 col=mix(uWaterColor,uDeepColor,deep*(0.20+0.34*shallow))*light;',
      // 天空反射限幅：低视角掠射角下 fresnel 接近 1，不限幅水面会被刷成灰白一片。
      // 远景收到 0.12 是对的，但特写镜头（11m 看 30cm 积水）里这点反射完全不够，
      // 水面读成"没有水"——所以给到 0.26 上限，近处能看到天光，远处仍不会爆白。
      '  col=mix(col,uSkyColor,clamp(fres*0.18,0.0,0.26));',
      // 天空反射：用世界空间反射方向查 equirect HDR 天空图。
      // 这一步是"像不像水"的分水岭——只混一个常数天空色时，整片水就是一块
      // 均匀的灰绿渐变，特写里读成地面积了层雾。采到天的明暗和楼影才会读成水面。
      '  if(uHasSky>0.5){',
      '    vec3 R=reflect(-Vw,N);',
      '    float su=atan(R.z,R.x)*0.15915494+0.5;',
      '    float sv=asin(clamp(R.y,-1.0,1.0))*0.31830989+0.5;',
      // 天空采样亮度 0.50 → 0.78。0.50 时 HDR 采样被压到比水色还暗，
      // 反馈混越亮越暗——"加反射"反而把水拖黑（这一轮实测）。
      // 物理上暮色天空的亮度仍远高于沥青（沥青反射率约 10%），水面本来就该比路面亮。
      // 0.78 让水面比周围路面高 10~15% 明度，这是"积水"和"湿地"的分界线。
      // 上限仍用 min(...,1.0) 兜住，避免朝天的镜面在低角度爆成白带。
      '    vec3 sky=min(texture2D(uSky,vec2(su,sv)).rgb,vec3(1.0))*0.78;',
      // 0.30 → 0.45 保底：0.30 时水面只比路面亮 30 阶，27 米外读成"浅灰水泥"。
      // 45% 的天空混入让水面明度再抬一档（真实积水映的是整片天，不是一条缝里的光）。
      // 上限 0.62 不动，避免朝天的水在低角度爆成白带。
      '    col=mix(col,sky,clamp(0.45+fres*0.35,0.0,0.62));',
      '  }',
      '  col+=spec*vec3(0.82,0.90,1.0)*0.60;',
      // 雨滴涟漪：两组圆环随时间向外扩散，每 1.8 秒在随机位置起一组新的。
      // 俯视时水面再亮也只读成"一块浅色地面"——有扩散的圆环才认得出是液面。
      // 半径 1.15 世界单位（≈7m）在 26m 外约 15px 的环，勉强可辨；再小就淹没在路纹里。
      // 中心用 floor(rt) 取整后跳位置，同一个周期内不动，所以是"一处处泛开"而不是"整片抖"。
      '  float rt=uTime*0.55;',
      '  float rf=floor(rt);',
      '  float rp=fract(rt);',
      '  vec2 rc1=vec2(sin(rf*2.399)*5.0,cos(rf*1.873)*3.4);',
      '  vec2 rc2=vec2(sin(rf*1.117+2.1)*5.6,cos(rf*2.711+1.3)*3.0);',
      '  float rq=fract(rt+0.5);',
      '  float r1=length(vLocal-rc1);',
      '  float r2=length(vLocal-rc2);',
      // 半径 1.15→2.2 世界单位、环宽 0.045→0.10：1.15 的环在 26m 外只有 1px 宽，
      // 等于没画（实测完全看不见）。2.2 投影约 60px，才认得出是水波。
      '  float rip=smoothstep(0.10,0.0,abs(r1-rp*2.2))*(1.0-rp)*0.95',
      '           +smoothstep(0.09,0.0,abs(r2-rq*2.2))*(1.0-rq)*0.75;',
      '  col+=rip*vec3(0.26,0.31,0.36);',
      // 平面反射：用"反射相机的投影坐标"采样镜像画面，而不是屏幕 uv——
      // 屏幕 uv 采到的是"当前视角的画面"，倒影不会随视角正确移动。
      // 水面和湿路面都吃这张图，只是权重不同：水面镜面感强，湿路面只是"隐隐有个亮块"。
      '  vec3 refl=vec3(0.0);float hasR=0.0;',
      '  if(uHasRefl>0.5){',
      '    vec4 rc=uReflVP*vec4(vWorld,1.0);',
      '    vec2 ruv=rc.xy/max(rc.w,0.0001)*0.5+0.5;',
      // 法线扰动偏移：倒影跟着波纹一起抖，才是"水面"，否则是一张贴纸
      // 扰动 0.05→0.085：机位压到 15° 后倒影拉得很长，0.05 的偏移几乎看不出来，
      // 水面读成一块玻璃。真实积水的倒影是被风和水滴打碎的。
      '    ruv+=vec2(N.x,N.z)*0.085;',
      // 5-tap 模糊：真水/湿沥青的倒影都不锐利，模糊核同时掩盖 RT 半分辨率的锯齿
      // 模糊半径 0.0032→0.0016：5-tap 模糊是为了掩盖半分辨率 RT 的锯齿，
      // 但半径给到 0.006 时倒影被完全揉散，水面读成"磨砂地面"——
      // 真实积水（尤其雨停瞬间）是能看到楼影和锥桶轮廓的，模糊只该抹掉锯齿。
      '    float bl=0.0015+0.0014*body;',
      '    refl=texture2D(uRefl,ruv).rgb*0.36',
      '        +texture2D(uRefl,ruv+vec2(bl,0.0)).rgb*0.16',
      '        +texture2D(uRefl,ruv-vec2(bl,0.0)).rgb*0.16',
      '        +texture2D(uRefl,ruv+vec2(0.0,bl)).rgb*0.16',
      '        +texture2D(uRefl,ruv-vec2(0.0,bl)).rgb*0.16;',
      // RT 里是线性值，这里保持线性（见文件末尾 outc 的 pow(2.2)）：
      // 走了后处理链之后整条管线都是线性的，谁先转 sRGB 谁就会在合成时被 ACES 再压一次。
      '    refl=max(refl,vec3(0.0));',
      // 去饱和 + 压亮度：天空本身偏蓝，原样反射会让积水变成"蓝色亚克力板"。
      // 雨天积水的倒影是"灰白的、被水面搅散的"，不是镜面蓝天。
      // 去饱和 0.34→0.18：天空压暗之后倒影本身已经不蓝了，
      // 再去 34% 只会把倒影变成一块没有信息的灰斑。
      '    refl=mix(refl,vec3(dot(refl,vec3(0.3333))),0.18)*0.92;',
      '    hasR=1.0;',
      '  }',
      // 边缘 alpha 羽化：水面不是硬边贴片，是逐渐变薄的水。
      // body=1 是水，body=0 是湿痕：湿痕用压暗的浊水色（≈湿沥青），透明度也降到 0.34，
      // 两层之间按 body 混合，于是"水—湿—干"是一条连续过渡而不是一道软边。
      // 湿痕系数 0.52→0.92：0.52 是按"亮天空"定的，暗天空下湿沥青直接黑成一片。
      // 真实湿沥青是"明显的深色"而不是"没开灯的沥青"——保持在干路面的 55~65%。
      '  vec3 dampCol=mix(uWaterColor,uDeepColor,0.75)*0.92;',
      // 程序化污渍（不引入任何贴图）：湿痕不是均匀的一层灰，
      // 真实路面的水迹是"脏的"——车辙里的泥、油渍、排水口的锈水，
      // 用两层低频正弦交叉调制明暗，再在水线内侧压一条更深的"污垢线"。
      '  float dirt=0.86+0.26*sin(vLocal.x*7.3+vLocal.y*11.1)*sin(vLocal.y*5.7-vLocal.x*3.1);',
      '  dampCol*=dirt;',
      // 污垢线贴在水线外侧（0.26~0.32）：真实积水边缘积的是泥和油，比水更深。
      // 它是"这条线是水岸"最强的视觉证据，比调水色有效得多。
      '  float rim=smoothstep(0.255+n,0.295+n,f)*(1.0-smoothstep(0.315+n,0.35+n,f));',
      '  dampCol=mix(dampCol,dampCol*0.72,rim);',
      '  if(hasR>0.5){',
      // 反射权重：上一轮把常数项从 0.10 提到 0.30 想让水面亮一点，结果更暗了——
      // 平面反射的 RT 里是**镜像场景**（暗天空 + 暗楼体），混进来只会把水拖黑。
      // 真正让水变亮的是上面那一行的天空项。这里回到 0.10 保底，
      // 只在掠射角（fres 大、能看到楼影倒影）时才加权，这是"像水"的另一半证据。
      '    col=mix(col,refl,clamp(0.10+0.55*fres,0.0,0.68));',
      // 湿路面吃反射更少：它只是"隐隐发亮"，不是镜子；
      // 而且现在倒影本身是暗的，权重给高了只会把湿痕一起拖黑。
      '    dampCol=mix(dampCol,refl,clamp(0.06+0.18*fres,0.0,0.28));',
      '  }',
      // 天顶光：低机位看水面时，最显眼的不是楼影，而是"整片水面铺着一层亮天光"。
      // 这一项不来自 background（那是地平线附近的暗云），而是天顶散射——
      // 物理上成立，观感上正是它把"一片湿沥青"变成"一汪水"。
      // 必须放在平面反射混合**之后**：放前面会被 mix(col,refl,…) 再削掉大半。
      // 常数项 0.10：俯视全景时 fres 只有 0.1，纯靠 fres 的天光项等于没有，
      // 水面就只剩"比路面暗"这一条线索，读成阴影。真实水面即使正上方看，
      // 也有约 2% 镜面 + 大量漫反射天光，通体是"比沥青亮一点的灰"。
      '  float zenith=clamp(0.16+fres*0.46,0.0,0.44);',
      '  col+=vec3(0.52,0.57,0.60)*zenith*(0.72+0.28*ripple);',
      '  vec3 outc=mix(dampCol,col,body);',
      // 这里**不要**再做 sRGB→线性转换。
      // 后处理链现在的口径是：sceneRT.texture.encoding=sRGBEncoding（渲染到 RT 时由 r128 编码），
      // 合成 pass 的 uGamma=1.0 直接输出。内置材质走到 RT 时已被编码，而 ShaderMaterial
      // 不吃 encodings_fragment、原样写值——所以这里输出的**必须已经是 sRGB**。
      // 之前留下的 pow(2.2) 等于把水面又压暗一次（0.22→0.038），实测干路面 133 而水面 26，
      // 整片积水读成墨黑，就是这个原因。
      '  outc=max(outc,vec3(0.0));',
      '  gl_FragColor=vec4(outc,mix(0.34,uOpacity,body)*damp);',
      '}',
    ].join(' '),
    transparent:true,
    side:THREE.DoubleSide,
    depthWrite:false,
  });
  const surf=new THREE.Mesh(geo,mat);
  surf.renderOrder=100;   // 排在积水主体(透明)之后渲染，确保水面不被主体顶面盖住
  return surf;
}// ---- 六跳推演：连续时间轴驱动（不再是离散跳变） ----
// 整条事件线是一条连续时间轴 t∈[0,1]：暴雨→积水上涨→部门落位→派单→施工队到场封路→调泵回落。
// 跳次按钮/进度条只是把 t 定位到某个值，所有视觉量（水位/雨势/告警/封路/施工队/派单气泡）都由 t 连续驱动。
let __evtClock=0,__evtPlaying=false,__lastFrame=performance.now();
// 总时长 26→34：26 秒里「涨水 12s + 峰值 1s + 回落 6s」，峰值只闪一下，
// 观感像「水一戳就破」，和真实内涝（雨停后还要高位维持）不符。
// 拉长到 34 秒后：涨水 15s、高位平台 6.8s、回落 8.2s，三段都站得住。
const EVT_DURATION=44;         // 整条事件线总时长（秒），播放/拖动都映射到这个区间
// 34→38→44：施工段要装下「下车 → 走到围栏边 → 两人各推一侧围栏 → 封路完成 →
// 回井口 → 撬盖 → 接管 → 开泵」八个动作。按 34 秒算这段只有 10.9 秒，
// 而 0 号从车走到西侧围栏就是 15.6m，按压到 2.7 秒等于 20km/h——这就是"人是飞过来的"。
// 44 秒时走位窗口 5.9 秒（15.6m ⇒ 2.7m/s 快走）、推板 4.6 秒（6.6m ⇒ 1.4m/s），都落在常识范围内。
// 所有节点都是 0~1 的比例值，改总时长只拉长绝对时间，阶段顺序和相对节奏不变。
// 时间轴结构：EVT_JUMPS[0] 之前是「降雨前奏」——只有雨，地面还没有积水；
// 第 1 跳「事件识别」时水才刚在低洼处积起来。事件链是
// 降雨 → 积水上涨 → 事件识别 → 影响扩散 → 部门落位 → 冲突暴露 → 资源缺口 → 动作/疏通 → 处置后回落。
// 一上来就把整滩水摆好，等于把结果当起点，不符合客观过程。
const EVT_JUMPS=[              // 7 个节点在时间轴上的位置（0~1）：跳次→时间刻度
  // 0.56→0.76 是「水已经漫满、但还没退」的平台期：第 5 跳 1.00、第 6 跳 0.80，
  // 6.8 秒只降 20%，观感是「雨停了，水还在」。若把 1.00 直接连到 0.08，
  // 水位就是一路直线下跌，像有人在放水，不像内涝。
  0.12, 0.22, 0.33, 0.44, 0.56, 0.76, 1.00,
];
// 前奏：雨由小转大，地面积水仍为 0
const PRELUDE={water:0, rain:0.20, alarm:false, block:false, crew:false, dispatch:false, clog:false};
// 每个"跳"的视觉目标值（water 水位系数 0~1、rain 雨势、alarm 告警、block 封路、crew 施工队到场、
// dispatch 派单气泡、clog 雨水口堵塞标记）。
// water=1.0 对应台账峰值深度（A-03 30cm / B-01 18cm），所以事件识别那跳只到 0.18（约 5cm），
// 水是随雨一直涨上来的，到第 5 跳才到 30cm。
// clog 不是"场景自带的属性"：刚识别时只知道路口有水，还不知道是雨水口堵了；
// 现场核查（第 2 跳 影响）才确认堵塞，施工队到场疏通后（封路那跳）标记消失。
const JUMP_VISUAL=[
  {water:0.18, rain:0.55, alarm:false, block:false, crew:false, dispatch:false, clog:false},
  {water:0.42, rain:0.72, alarm:false, block:false, crew:false, dispatch:false, clog:true },
  {water:0.60, rain:0.82, alarm:false, block:false, crew:false, dispatch:true , clog:true },  // 部门落位→派单
  {water:0.76, rain:0.90, alarm:true,  block:false, crew:false, dispatch:true , clog:true },  // 资源缺口→告警
  {water:1.00, rain:0.95, alarm:true,  block:false, crew:true,  dispatch:true , clog:true },  // 峰值：施工队到场
  {water:0.80, rain:0.60, alarm:false, block:true,  crew:true,  dispatch:true , clog:false},  // 疏通完成→封路→水刚开始退（真正的回落在第 7 段）
  {water:0.08, rain:0.12, alarm:false, block:true,  crew:true,  dispatch:false, clog:false},  // 处置后：排水回落
];
const RING_COLOR={pending:0x5a6b7a,running:0x4fc3f7,needs_confirm:0xf5c063,done:0x34d399};
// 随深度：浅浑浊 → 深浊 → 回落。内涝水是裹着泥沙的浑水，不能给清亮的天蓝。
// 这组值在 applyTimeline 里 setHex 后还会再转一次线性（见那处注释）。
// 水体主色：内涝是浑水（灰褐带绿），不是游泳池的湖蓝，也不是池塘的墨绿。
// 随水位：浅时略清 → 涨水后发浊发暗 → 回落再变浅。
// 色值取舍：既要特写里读得出"水"，又要远景（俯角 45°、反射几乎为零）里
// 和沥青路面拉开差距。纯墨绿在近景像油污，纯浅灰在远景看不出来，取中间。
// 天空压暗到 0.30 之后，水面的天光反射整体降了一半，原色值直接塌成"墨黑一片"
// （实测干路面 133 vs 水面 26）。水体本身有散射，不靠反射也有底色，
// 所以这里把基色整体提亮约 20%，作为"天空变暗后的漫反射补偿"。
// 色相往泥绿偏一档：俯视全景时 fresnel 很小（掠射角只有 10°），天光反射起不来，
// 水面只能靠"比沥青更暗 + 更绿"识别。纯灰调会和冷灰沥青混成一片阴影。
// 逐跳的积水色（第 1~7 跳）。这七个值在 applyTimeline 里按跳次直接写进 uWaterColor，
// 改 makeWaterSurface 的初值无效。
// 2026-11-13 实拍验收：原值渲染出来比干燥沥青只暗约 14/255（水 119,126,129 vs 路 133,142,152），
// 在暮色光照下几乎读不出「这里有积水」。整套压暗约 15%，色相保持黄绿不变：
// 水面稳定低于路面一个档，又不至于暗成黑洞（此前压太狠被读成阴影的那次教训）。
// 积水颜色：按水位插值（0=浅水，6=最深）。
// 原来是一组灰绿（0x6a7664→0x4d5846），和阴雨光下的沥青色相几乎一样，
// 俯视 26m 外只能靠明度差区分——实测水面比路面亮 30 阶，但读起来就是"一块浅灰水泥地"。
// 改成偏青的浊色（绿通道压低、蓝通道抬起）：真实积水映的是天空，本来就偏冷；
// 青灰和沥青的暖灰在色相上直接分开，不用靠亮度硬撑。饱和度压住，仍是浑水不是泳池。
const WATER_COLORS=[0x6f8790,0x67808a,0x5f7882,0x557078,0x4b666e,0x557078,0x6f8790];
let LINKS=null,CARD_INDEX={},RINGS={},ANCHORS={};
const MAX_JUMP=7;
function requestedParams(){
  const q=new URLSearchParams(location.search);
  let jump=Math.round(Number(q.get('jump')||1));
  if(!isFinite(jump)||jump<1)jump=1; if(jump>MAX_JUMP)jump=MAX_JUMP;
  return {jump:jump,entity:(q.get('entity')||'').trim()};
}
const REQUESTED=requestedParams();
const twinState={jumps:[],currentJump:REQUESTED.jump-1,selectedEntity:REQUESTED.entity||null,scheduler:null};
function jumpTitle(i){
  if(i>=6)return '最终状态 · 六跳动作执行完毕';
  const j=LINKS&&LINKS.jumps&&LINKS.jumps[i];
  return j?j.title:'第 '+(i+1)+' 跳';
}
// 按钮位窄，标题只留冒号后半（"观测：事件识别" → "事件识别"），完整标题在顶栏
function shortTitle(text){
  const parts=String(text||'').split('：');
  return parts.length>1?parts.slice(1).join('：'):String(text||'');
}
function jumpRealIndex(i){return Math.min(i,5)}
function renderJumpControls(){
  const box=document.getElementById('jump-buttons');if(!box)return;
  let html='';
  for(let i=0;i<6;i++)html+='<button class="'+(i===twinState.currentJump?'active':'')+'" data-jump="'+i+'" title="'+esc(jumpTitle(i))+'">'+(i+1)+'. '+esc(shortTitle(jumpTitle(i)))+'</button>';
  box.innerHTML=html;
  box.querySelectorAll('button').forEach(b=>b.onclick=()=>setJump(Number(b.dataset.jump)));
  // 「最终状态」按钮单独分组在 jump-buttons 之外，这里只同步它的高亮
  const settle=document.getElementById('settle');
  if(settle)settle.classList.toggle('active',twinState.currentJump===6);
  document.getElementById('play').textContent=__evtPlaying?'暂停':'播放';
}
function updateEvidence(){
  const body=document.getElementById('evidence-body');if(!body)return;
  if(!LINKS){body.textContent='正在读取六跳证据…';return}
  // 降雨前奏阶段还没有事件，硬把第 1 跳的证据挂上来等于"证据先于事件"
  if((__evtClock/EVT_DURATION)<EVT_JUMPS[0]){
    body.innerHTML='<b style="color:#cfe3f7">降雨：雨势增强</b><br>'
      +'路面积水尚未形成，事件链从第 1 跳「事件识别」开始。<br>'
      +'<span style="color:#7f9bb3">本阶段只有降雨过程，没有可采信的事件证据。</span>';
    return;
  }
  const j=LINKS.jumps[jumpRealIndex(twinState.currentJump)]||{};
  const atSettle=twinState.currentJump===6;
  body.innerHTML='<b style="color:#cfe3f7">'+(atSettle?'最终状态':esc(j.title||''))+'</b><br>'
    +(atSettle?'六跳动作全部执行完毕，按会签要求待人工确认':esc(j.detail||'—'))
    +'<br>关联对象 '+(atSettle?(LINKS.jumps[5]||{}).entity_ids||[]:j.entity_ids||[]).length
    +' 个 · 动作卡 '+(atSettle?LINKS.cards.length:(j.card_ids||[]).length)+' 张 · 证据 '+(atSettle?0:j.ref_count||0)+' 条'
    +(atSettle?'':'<br><span style="color:#7f9bb3">事件序号 '+(j.seq||'-')+' · 来源 '+esc(j.source||'event-stream')+'</span>'
      +'<br><span style="color:#9dc3d8">规则 '+esc((j.rules||[]).slice(0,3).join(' · ')||'—')+'</span>'
      +'<br><span style="color:#7897ad">依据 '+esc((j.refs||[]).slice(0,2).map(refCn).join(' · ')||'—')+(j.ref_count>2?' 等 '+j.ref_count+' 条':'')+'</span>');
}
function renderEventBar(idx){
  const bar=document.getElementById('event-bar');if(!bar)return;
  const pre=(__evtClock/EVT_DURATION)<EVT_JUMPS[0];   // 降雨前奏：只有雨，还没形成积水
  const j=LINKS&&LINKS.jumps&&LINKS.jumps[Math.min(idx,5)];
  const title=pre?'降雨：雨势增强':(idx===6?'最终状态':(j&&j.title)||jumpTitle(idx));
  const seq=idx===6?'—':(j&&j.seq)||idx+1;
  const s=Math.round(__evtClock),mm=String(Math.floor(s/60)).padStart(2,'0'),ss=String(s%60).padStart(2,'0');
  bar.innerHTML='<b>'+esc(title)+'</b><span>模拟事件时钟 T+'+mm+':'+ss+' · '
    +(pre?'路面积水尚未形成 · 第 1 跳之前':'stage 序号 '+seq+' · '+(idx===6?'处置后状态':('第 '+(idx+1)+'/6 跳')))+'</span>';
}
function setJump(i){
  twinState.currentJump=Math.max(0,Math.min(6,i));
  __evtPlaying=false;                       // 手动跳步时暂停自动播放，避免两者抢时间轴
  __evtClock=EVT_JUMPS[twinState.currentJump]*EVT_DURATION;
  applyTimeline(__evtClock/EVT_DURATION);
  paintHighlights();
  renderJumpControls();updateEvidence();renderResults();
  if(twinState.selectedEntity)renderEntityCard(twinState.selectedEntity);
  renderEventBar(twinState.currentJump);
  syncProgress();
  // 相机按跳次自己的时刻取景：全景跳 → 全景机位，封路/作业跳 → 推到作业点特写。
  // 这样静止在某一跳时看到的画面，和播放到该时刻一致（原来只有播放时才运镜，
  // 手动跳到第 5 跳是全景，人在画面里只有几个像素）。
  window.__camUser=false;      // 跳次是系统取景，收回"用户手动视角"的让位
  driveCamera(__evtClock/EVT_DURATION);
}
// 在两个相邻跳次之间做连续线性插值：让水位/雨势/颜色平滑过渡，而不是瞬间跳变
function lerpJump(t){
  const n=EVT_JUMPS.length;
  // 降雨前奏：第一个跳次之前只有雨，地面积水从 0 涨到第 1 跳的水位（约 5cm）
  if(t<EVT_JUMPS[0]){
    const f=Math.max(0,t)/EVT_JUMPS[0],a=PRELUDE,b=JUMP_VISUAL[0];
    return {v:{water:a.water+(b.water-a.water)*f, rain:a.rain+(b.rain-a.rain)*f,
      alarm:false,block:false,crew:false,dispatch:false,clog:false},i:0,frac:f,pre:true};
  }
  if(t>=EVT_JUMPS[n-1])return {v:JUMP_VISUAL[n-1],i:n-1,frac:0};
  let i=0;while(i<n-1&&EVT_JUMPS[i+1]<t)i++;
  const a=EVT_JUMPS[i],b=EVT_JUMPS[i+1],frac=(t-a)/(b-a);
  const va=JUMP_VISUAL[i],vb=JUMP_VISUAL[i+1];
  return {v:{
    water:va.water+(vb.water-va.water)*frac,
    rain:va.rain+(vb.rain-va.rain)*frac,
    alarm:frac<0.5?va.alarm:vb.alarm,      // 告警/派单/施工队是离散事件，用阈值切换
    // 封路排在布锥之后（布锥 f≈0.62~0.78），疏通排在接泵之后（f≈0.82），
    // 所以这两个阈值不能都取 0.5：否则"封路"和"疏通完成"会在同一瞬间发生，工序就乱了。
    block:frac<0.78?va.block:vb.block,
    // crew 阈值单独给 0.15：第 5 跳（0.64）的标题写的是"施工队到场"，
    // 但用 0.5 的话人要等到 0.73 才出现，比文案晚 2 秒多——
    // 观众看到的是"标题说到场了，画面里没人"。
    crew:frac<0.15?va.crew:vb.crew,
    dispatch:frac<0.5?va.dispatch:vb.dispatch,
    clog:frac<0.90?va.clog:vb.clog,
  },i:i,frac:frac};
}
// 连续时间轴驱动所有视觉量：t∈[0,1]
function applyTimeline(t){
  t=Math.max(0,Math.min(1,t));
  const L=lerpJump(t);
  const v=L.v;
  // 把这一帧的离散事件状态挂出去：封路护栏的淡入要跟车道线/路名用同一根开关，不能各写各的
  const _fl=window.__flags||(window.__flags={});
  _fl.block=!!v.block;_fl.clog=!!v.clog;
  // 水体主色：随水位连续插值（浅清→深浊→回落）
  const colorIdx=Math.min(WATER_COLORS.length-1,L.i);
  const colorA=WATER_COLORS[colorIdx],colorB=WATER_COLORS[Math.min(WATER_COLORS.length-1,colorIdx+1)];
  const waterColor=lerpColor(colorA,colorB,L.frac);
  // 贴地积水水面：水位连续涨落（y 轻微升高）+ 面积随水位扩散 + 颜色加深
  (window.__waterMeshes||[]).forEach(w=>{
    const depth=v.water*w.depthMax;                 // 当前水深（真实比例，几厘米到几十厘米）
    if(w.surface){
      // 这三个值每帧都会被这里重写，改 makeWaterSurface 里的初值没用——
      // 水面高度、透明度、波形振幅、水色都必须在 applyTimeline 里改。
      // 0.46 起：高于路缘石顶面 0.42，波谷也不会沉到路缘以下被切成窄带。
      // 街区里的漫流水洼底下是地块(0.35)，标高给 0.395：波谷 0.363 仍在 0.35 之上，
      // 给 0.372 时波谷会穿到地块下面，被地块切出一块块黑斑。
      w.surface.position.y=(w.pond?0.395:0.46)+v.water*0.012;
      w.surface.visible=v.water>0.05;
      const um=w.surface.material.uniforms;
      if(um){
        um.uLevel.value=v.water;                              // 水位越高，水洼越外扩、越连片
        // 逐水洼生长：每个水洼有自己的到达阈值（离核心越远越晚），
        // 水位过阈值后才开始从小半径淡入 → 积水顺着低洼路网一段段蔓延。
        // 阈值来自 blobA（构建时按距离算好），不是每帧现推。
        const ba=w.blobA||[];
        // 退水阶段给每个水洼一个「退水滞后」，越远（a 越大）越慢：
        // 等效水位加 recF*a*0.10，远端在退水时能多拖一会儿，
        // 不会整片水从边界同步往里缩。
        // 必须用 recF 渐变（0.10 的时间窗 ≈3.4s）：直接切 rec=true 会让
        // 所有水洼的 g 在一帧内跳变，水面会「啪」地膨胀一下。
        const recF=clamp01((t-EVT_JUMPS[5])/0.10);
        for(let i=0;i<8;i++){
          const a=(ba[i]==null)?1:ba[i];
          const we=v.water+recF*a*0.10;
          const g=clamp01((we-a)/Math.max(0.001,1-a));
          const comp=['x','y','z','w'][i<4?i:i-4];
          if(i<4)um.uG0.value[comp]=g;else um.uG1.value[comp]=g;
        }
        // 透明度压在 0.67 附近：原来 0.80~0.86 太实，俯视特写时水面把站在积水里
        // 的工人的腿整个"吃掉"，人看着像"只露上半身漂在水上"（实际水深才 53cm，
        // 只到膝盖）。降到 0.67 让腿从水面下透出来，又不至于透出沥青颗粒读成"湿地"。
        // 0.62+0.05 满水只有 0.67：水面颜色被下面的深色沥青稀释三成，
        // 实测水与路面的像素色差只有 30/765（≈4%），远看就是"湿了一点"。
        // 提到 0.72+0.06（满水 0.78）——仍然是半透明（腿和车道线能透出来），
        // 但水色不再被路面吃掉，明度差拉到 60 阶左右才读得出"这是一层水"。
        um.uOpacity.value=0.72+0.06*v.water;
        // 这里不能 convertSRGBToLinear：水面是自定义 ShaderMaterial，r128 只给内置材质
        // 插入 sRGB 输出编码（encodings_fragment），ShaderMaterial 没有这段。
        // 之前做了 sRGB→线性转换，等于把颜色压暗一档后直接写进 sRGB 画布，
        // 0x2f7f9e 的浑水被渲成 (20,41,58) 的近黑——小水洼看着像地上的黑洞。
        um.uWaterColor.value.setHex(waterColor);
        // 波幅上限仍是 3.2cm（再高波谷会沉到路缘石 0.42 以下，水面被切成窄带），
        // 但满水位要顶到上限：0.012+0.02 只到 3.2 的 63%，
        // 俯视时这点起伏被法线抹平，水面读成一块塑料板。雨天水面一定有细碎涟漪。
        um.uWaveAmp.value=0.014+v.water*0.018;
      }
    }
    if(w.labelSprite){
      const baseCm=/A-03/.test(w.label)?30:18;
      const cm=Math.max(1,Math.round(v.water*baseCm));
      // 还没积水时不显示「积水 X cm」：降雨前奏阶段路面是干的，挂个"积水 0cm"反而自相矛盾
      w.labelSprite.visible=v.water>0.06;
      w.labelSprite.material.map=makeLabelTex('积水 '+cm+' cm','#eaf6ff');
      w.labelSprite.material.needsUpdate=true;
      w.labelSprite.position.y=0.9;   // 标注紧贴水面，不飘高空
    }
  });
  // 地表汇流示意：箭头沿已有雨水管线方向流向下游节点，随积水深度显隐/增强。
  // 当前数据没有土壤渗透系数，因此不把这段动画解释成可计算的“内渗”过程。
  (window.__flowMarkers||[]).forEach((f,idx)=>{
    const active=v.water>0.08;
    f.mesh.visible=active;
    f.mesh.material.opacity=active ? 0.18+0.62*v.water : 0;
    f.mesh.position.y=0.52+0.025*Math.sin(__evtClock*4+idx*0.9);
    f.mesh.scale.setScalar(0.8+0.35*v.water);
  });
  // 土壤下渗：积水越深，土黄色下渗晕染越明显、越向外扩散（示意下渗量随水深增大）。
  // 无土壤渗透系数实测数据，INFIL_RATE 为分流比例占位，不伪装成物理计算结果。
  (window.__infilMarkers||[]).forEach((f)=>{
    const active=v.water>0.12;
    f.mesh.visible=active;
    // 尺寸按主水洼半径建（最大值），这里跟着主水洼自己的生长曲线一起缩放，
    // 否则积水只有路口一小片时，晕染还是满幅，像在干路上铺了块黄土。
    // 平面已 rotation.x=-90°，局部 x/y 才对应地面 x/z。
    const a0=(f.blobA!=null)?f.blobA:0.06;
    const g0=clamp01((v.water-a0)/Math.max(0.001,1-a0));
    const g=0.32+0.68*g0;
    f.mesh.material.opacity=active ? 0.08+0.22*v.water : 0;
    f.mesh.scale.set(g,g,1);
  });
  // 告警：光柱 + 顶灯（顶灯在 tick 里闪烁）
  (window.__pumpAlarms||[]).forEach(p=>{p.on=!!v.alarm;if(p.beam)p.beam.visible=!!v.alarm;});
  // 雨水口堵塞：橙色篦子 + 警示锥 + 反涌水花 + 「堵塞」标注，只在核查确认之后出现、疏通后消失。
  // 直接按 clogged 静态建出来的话，事件第一帧就挂着"堵塞"，等于把结论画到了起因之前。
  (window.__clogMarks||[]).forEach(m=>{
    const on=!!v.clog;
    m.group.visible=on;
    m.frameMat.color.setHex(on?0xa8702f:0x6c747a);
    m.grateMat.color.setHex(on?0xd9822b:0x242a30);
  });
  // 封路同理：封路前车道线正常、路名是常态色；封路后才隐藏车道线、把路名转橙（图例「已封路」）
  (window.__laneDashes||[]).forEach(g=>{g.visible=!v.block;});
  (window.__roadLabels||[]).forEach(r=>{
    const want=v.block?r.blocked:r.normal;
    if(r.sprite.material.map!==want){r.sprite.material.map=want;r.sprite.material.needsUpdate=true;}
  });
  // 瓶颈管段（PL-DRN-101 管径偏小）：积水上涨时橙色高亮 + 自发光，水位回落时恢复——把「管径偏小」这条台账因果画出来
  (window.__pipeSegments||[]).forEach(s=>{
    if(!s.isBottleneck)return;
    const hot=v.water>0.5;   // 水位过半视为「重现期不足」开始显现
    // 冷态也要能看见：和普通管段同色，否则这根管在图上直接消失，
    // 只留两根别的管，读起来像"管网断了一截"。
    // 材质是 MeshBasicMaterial（无 emissive），亮度靠 color 本身，橙色也要挑深一点，
    // 否则 0xff5a1f 经线性→sRGB 提亮后是接近纯白的橙黄，在浅色路面上同样糊掉。
    s.mat.color.setHex(hot?0xd23c00:0x0a6f92);
    s.mat.opacity=hot?0.95:0.88;
  });
  // 封路护栏/派单气泡/施工队 = 离散事件，显隐+淡入由 fadeInOut() 统一处理（tick 每帧驱动）
  // 雨势：不只用透明度表达，雨丝的"条数"也跟着变——小雨稀、暴雨密，
  // 这才是量感差异。透明度只做辅助（0.30→0.90，避免暴雨把整个画面糊成白纱）。
  if(rain&&rain.material){
    rain.material.opacity=0.30+v.rain*0.60;
    if(rainGeo&&rainGeo.attributes.position){
      const total=rainGeo.attributes.position.count;      // 顶点数 = 雨滴数 × 2
      rainGeo.setDrawRange(0,Math.max(4,Math.floor(total*(0.20+0.80*v.rain))));
    }
  }
  // 氛围：暴雨时天空与地面光照变暗，但雾只轻微加深，不糊掉远处街区
  if(scene3.fog){
    scene3.fog.near=140-v.rain*45;scene3.fog.far=420-v.rain*110;
    scene3.fog.color.setHex(lerpColor(0x6a7680,0x54606c,v.rain));
  }
  if(scene3.background&&scene3.background.isTexture){scene3.backgroundIntensity=0.30-v.rain*0.13;}
  // 光环要跟着时间轴实时走：syncJumpFromClock 只在"跳次变化"时刷新，
  // 播放时从降雨前奏进入第 1 跳（idx 一直是 0）就不会触发，环会一直不出现。
  paintHighlights();
}
function lerpColor(a,b,f){const ar=(a>>16)&255,ag=(a>>8)&255,ab=a&255;const br=(b>>16)&255,bg=(b>>8)&255,bb=b&255;
  return (Math.round(ar+(br-ar)*f)<<16)|(Math.round(ag+(bg-ag)*f)<<8)|Math.round(ab+(bb-ab)*f);}
// ---- 关联对象高亮：光环颜色 = 该对象当前跳的动作卡状态 ----
function buildRings(){
  const box=new THREE.Box3(),center=new THREE.Vector3(),size=new THREE.Vector3();
  const HALF={};   // 每个对象自身的水平半对角线：环要套在物体外面，不能被墙体吃掉一半
  scene3.traverse(o=>{
    const id=o.userData&&o.userData.entityId;if(!id)return;
    // 楼体现在是 Group（含主体/裙楼/腰线/屋顶），锚点按整个 Group 的包围盒中心算
    if(!(o.isMesh||o.isGroup))return;
    box.setFromObject(o);box.getCenter(center);box.getSize(size);
    // 水面平面是「铺满所有水洼的外包矩形」，尺寸不代表实体，跳过
    if(!o.userData.isWaterSurface)
      HALF[id]=Math.max(HALF[id]||0,Math.hypot(size.x,size.z)/2);
    if(!ANCHORS[id])ANCHORS[id]=center.clone();
    else ANCHORS[id].lerp(center,0.5);
  });
  // 片区没有几何体（只是范围），按两个片区中心补锚点，否则"事件落在 A-03"这跳高亮不出来
  const DISTRICT={'A-03':[199,161],'B-01':[591,161]};
  Object.keys(DISTRICT).forEach(id=>{
    if(ANCHORS[id])return;
    const wxz=W(DISTRICT[id][0],DISTRICT[id][1]);
    ANCHORS[id]=new THREE.Vector3(wxz[0],0.42,wxz[1]);
  });
  // 水面锚点：水面平面是「铺满所有水洼的外包矩形」，几何中心往往落在积水之外的空地/地块上。
  // 直接用包围盒中心，光环就会挂在一块根本没水的路面上——看着就是个莫名其妙的圆。
  // 改用第一块水洼（路口主水体）的中心：那里才是这场积水真正的位置。
  (window.__waterMeshes||[]).forEach(function(w){
    const s=w.surface,um=s&&s.material&&s.material.uniforms;
    if(!s||!um||!um.uB0)return;
    const b=um.uB0.value;
    if(!(b.z>0.01))return;   // 没配 blob（退回单水洼）就不动，保留包围盒中心
    ANCHORS[s.userData.entityId]=new THREE.Vector3(s.position.x+b.x,0.5,s.position.z+b.y);
  });
  Object.keys(ANCHORS).forEach(id=>{
    const p=ANCHORS[id];
    const big=Object.prototype.hasOwnProperty.call(DISTRICT,id);
    // 地面环 + 竖直光柱：只画环的话，贴着楼的环有一半被楼体挡住，远看根本发现不了
    // 半径收窄：片区环原来 8.6（直径约 100m），在俯瞰视角下横跨半个屏幕，比积水本身更抢眼
    // 再收一档到 3.4~4.0：光环只标「事件落点」，不是「影响范围」，画得比积水还大就本末倒置了。
    // 设施（泵房/进水井）按自身半对角线撑开，否则环会埋在墙体里只露出几段弧。
    const half=Math.min(HALF[id]||0,3.4);
    const rIn=Math.max(big?3.55:2.28,half+0.30),rOut=rIn+0.28;
    const ring=new THREE.Mesh(
      new THREE.RingGeometry(rIn,rOut,56),
      new THREE.MeshBasicMaterial({color:0x7ed4ff,transparent:true,opacity:.9,side:THREE.DoubleSide,depthWrite:false})
    );
    ring.rotation.x=-Math.PI/2;ring.position.set(p.x,big?0.5:0.56,p.z);ring.visible=false;
    // 光环正好压在积水上，而水面是 transparent + depthWrite:false 的半透明材质：
    // 两者都关闭了深度写入，谁后画谁在上，靠的是 renderOrder。
    // 水面自己设了 renderOrder=100、下渗晕染 110，所以这里必须排在它们之后，否则：
    // 光环被水面整个盖掉，只露出落在积水外的那半圈——这就是之前那个"没有意义的缺口圆"。
    ring.renderOrder=120;
    scene3.add(ring);
    const beam=new THREE.Mesh(
      new THREE.CylinderGeometry(0.4,0.4,2.6,14,1,true),
      new THREE.MeshBasicMaterial({color:0x7ed4ff,transparent:true,opacity:.12,side:THREE.DoubleSide,depthWrite:false})
    );
    beam.position.set(p.x,1.4,p.z);beam.visible=false;beam.renderOrder=121;
    scene3.add(beam);
    RINGS[id]={ring:ring,beam:beam};
  });
}
// 光环只标「本步落点」这一个对象。
// 原来给本步涉及的每个对象都套一个环，问题很明显：
//   ① 路段的锚点是整条折线的包围盒中心，落在路中间的空地上，环下面什么都没有，看着就是一个圆；
//   ② 第 2 跳涉及 13 个对象，一口气亮 13 个淡色圆盘，比场景本身还乱；
//   ③ 泵站/雨水口本来就有标签、告警光柱、台账卡片在表达状态，再套环是重复信息。
// 现在：本步按「片区 → 积水 → 设施」取一个主落点套环，另外当前选中的对象套一个（选中即"你在这儿"）。
// 路段不套环——道路是线不是点，环放哪儿都不对，封路状态交给护栏和标签表达。
// 片区在台账里没有逐跳状态（states 全是空串），所以「优先片区」这条实际不会命中，
// 落点会落到片区内的积水点上，正好也是我们要的"一个具体的点"。
function primaryOf(active,jumpIdx){
  const ents=(LINKS&&LINKS.entities)||{};
  const rank=id=>{const k=(ents[id]||{}).kind;return k==='district'?0:(k==='water'?1:(k==='facility'?2:9))};
  let best=null,bestR=9;
  active.forEach(id=>{
    const row=ents[id]||{};
    const st=row.states?row.states[jumpIdx]:'';
    if(!st)return;                 // 这一跳没有状态的对象不参与，否则会出现"亮着但没颜色依据"的环
    const r=rank(id);
    if(r<bestR){bestR=r;best=id}
  });
  return bestR<9?best:null;
}
function paintHighlights(){
  if(!LINKS)return;
  // 降雨前奏还没有事件，不该有任何"当前处置对象"的光环
  const pre=(__evtClock/EVT_DURATION)<EVT_JUMPS[0];
  if(pre){Object.keys(RINGS).forEach(id=>{const fx=RINGS[id];if(!fx)return;fx.ring.visible=false;fx.beam.visible=false;});return;}
  const jump=jumpRealIndex(twinState.currentJump);
  const active=new Set(((LINKS.jumps[jump]||{}).entity_ids)||[]);
  const primary=primaryOf(active,twinState.currentJump);
  const sel=twinState.selectedEntity;
  Object.keys(RINGS).forEach(id=>{
    const fx=RINGS[id];const row=LINKS.entities[id];
    if(!fx)return;
    const kind=(row||{}).kind;
    const show=id===primary||(id===sel&&kind!=='road');
    const state=row&&row.states?row.states[twinState.currentJump]:'';
    if(!show||!state){fx.ring.visible=false;fx.beam.visible=false;return}
    const color=RING_COLOR[state]||0x7ed4ff;
    fx.ring.material.color.setHex(color);fx.beam.material.color.setHex(color);
    fx.ring.visible=true;fx.beam.visible=true;
  });
}
// ---- 对象详情：工单状态 · 责任部门 · 动作卡 · 证据引用 ----
const KIND_CN={water:'积水 / 事件',road:'道路',facility:'设施',district:'片区'};
const ORDER_STATE={open:'进行中',closed:'已结案'};
const SEV_CN={high:'高',medium:'中',low:'低'};
const STATE_CN={pending:'待执行',running:'执行中',done:'已完成',needs_confirm:'待人工确认'};
// 场景状态枚举来自台账（fault_pending_repair 之类），与主页面同一套说法
const SCENE_STATE_CN={incident:'当前事件',water:'积水影响',closed:'已封闭',under_construction:'施工中',clogged:'堵塞',fault_pending_repair:'待维修',in_service:'正常',normal:'正常',active:'处置中',area:'片区'};
function orgCn(id){
  const map=(LINKS&&LINKS.organizations)||{};
  return map[id]||({['EMERG-COORD']:'应急总指挥',['ORG-EMERG']:'区应急管理局'}[id])||id;
}
const TBL_CN={construction_plan:'施工方案',drainage_facility:'排水设施台账',duty_list:'部门职责表',emergency_plan:'应急预案',pipeline_ledger:'管线台账',procedure:'处置规程',resource_inventory:'资源台账',road_network:'路网台账',underpass:'下穿隧道台账',work_order:'工单台账'};
function refCn(ref){
  // 引用有两种形态：带资产前缀的 `AS-A-03-work_order-6a12#A-03/work_order/v1#row:2`
  // 和对象自带的短形态 `A-03/work_order/v1#row:2`。取含斜杠的那一段当路径，两种都能读。
  const parts=String(ref||'').split('#');
  const path=parts.find(p=>p.indexOf('/')>=0)||'';
  const segs=path.split('/').filter(Boolean);
  const m=String(ref||'').match(/(?:row|line):([0-9]+)/);
  let out=[segs[0]||'',TBL_CN[segs[1]]||segs[1]||''].filter(Boolean).join(' ');
  if(segs[2])out+=' '+segs[2];
  if(m)out+=' 第 '+m[1]+' 行';
  return out||String(ref||'');
}
function badge(state,label){
  if(!state)return '<span class="badge">无动作</span>';
  return '<span class="badge b-'+esc(state)+'">'+esc(label)+'</span>';
}
function renderEntityCard(id){
  const card=document.getElementById('entity-card'),title=document.getElementById('entity-title'),body=document.getElementById('entity-body'),box=document.getElementById('entity-links');
  card.style.display='block';
  const row=LINKS&&LINKS.entities[id];
  if(!row){
    title.textContent='对象详情';body.textContent='';
    box.innerHTML='<div class="ec-sec"><small>未在场景台账中找到</small><div class="ec-empty">深链接里的 entity='+esc(id)+' 不在 scene.json 投影结果中。请从主页面重新选择对象。</div></div>';
    return;
  }
  const jump=twinState.currentJump;
  const state=row.states?row.states[jump]:'';
  title.textContent=row.label;
  body.textContent='';
  let html='<div class="ec-sec"><small>基本信息</small>'
    +'<div class="ec-row"><span>类型</span><b>'+esc(KIND_CN[row.kind]||row.kind)+'</b></div>'
    +'<div class="ec-row"><span>对象编号</span><b>'+esc(row.id)+'</b></div>'
    +'<div class="ec-row"><span>当前跳次状态</span><b>'+badge(state,STATE_CN[state]||'—')+'</b></div>'
    +'<div class="ec-row"><span>说明</span></div><div class="ec-empty">'+esc(detailCn(row.detail,row.state))+'</div></div>';

  // 工单
  const orders=row.work_orders||[];
  html+='<div class="ec-sec"><small>关联工单（'+orders.length+'）</small>';
  html+=orders.length?orders.map(o=>'<div class="ec-card">'+esc(o.order_id)+' · '+esc(o.order_type_label)
    +'<br><em>状态 '+esc(ORDER_STATE[o.status]||o.status)+' · 风险 '+esc(SEV_CN[o.severity]||o.severity||'—')+' · 上报 '+esc(orgCn(o.reporter_org))+'</em></div>').join('')
    :'<div class="ec-empty">台账未登记该对象的工单</div>';
  html+='</div>';

  // 判断依据：这个对象为什么被判成当前状态，挂在哪条断言上
  const claims=row.claims||[];
  html+='<div class="ec-sec"><small>判断依据（'+claims.length+' 条断言）</small>';
  html+=claims.length?claims.map(c=>'<div class="ec-card">'+esc(c.label)
    +' <span class="badge'+(c.supported?' b-done':' b-alert')+'">'+(c.supported?'有据':'待核')+'</span>'
    +'<br><em>'+esc(c.claim_id)+' · 证据 '+c.evidence_count+' 条</em></div>').join('')
    :'<div class="ec-empty">该对象暂无断言支撑</div>';
  html+='</div>';

  // 责任部门
  const depts=row.departments||[];
  html+='<div class="ec-sec"><small>责任部门（'+depts.length+'）</small>'
    +(depts.length?depts.map(d=>'<span class="badge" style="margin:2px 3px 2px 0">'+esc(d)+'</span>').join(''):'<div class="ec-empty">尚未落位到具体部门</div>')+'</div>';

  // 动作卡
  const cards=(row.card_ids||[]).map(cid=>CARD_INDEX[cid]).filter(Boolean);
  const live=cards.filter(c=>{const s=c.states?c.states[jump]:'';return s==='running'||s==='needs_confirm'});
  const shown=(live.length?live:cards).slice(0,4);
  html+='<div class="ec-sec"><small>动作卡（'+cards.length+' 张）</small>';
  html+=shown.length?shown.map(c=>{
    const s=c.states?c.states[jump]:'';
    const text=c.stage==='action'?c.action:(c.stage_label||c.action);
    return '<div class="ec-card"><span class="badge b-'+esc(s)+'">'+esc(STATE_CN[s]||'')+'</span> '
      +esc(c.org_name)+' · '+esc(clip(text,72))
      +'<br><em>'+esc(c.card_id)+' · 证据 '+c.evidence_count+' 条</em></div>';
  }).join('')+(cards.length>shown.length?'<div class="ec-empty">另有 '+(cards.length-shown.length)+' 张未展开</div>':'')
    :'<div class="ec-empty">该对象暂无动作卡</div>';
  html+='</div>';

  // 证据
  const refs=row.evidence||[];
  html+='<div class="ec-sec"><small>证据引用（'+refs.length+' 条）</small>'
    +(refs.length?refs.map(r=>'<div class="ec-card"><em>'+esc(refCn(r))+'</em></div>').join(''):'<div class="ec-empty">暂无台账依据</div>')+'</div>';
  box.innerHTML=html;
}
// 管段台账详情：管线台账（pipeline_ledger.csv）真实字段，点击半透明地下管段时展示。
// 管段不在 /api/twin-links 的实体投影里（投影只保留 road/facility/water/district），
// 所以这里直接读 TWIN3D_PIPES 内嵌台账，不走 renderEntityCard 的 LINKS 路径。
function renderPipeCard(id){
  const card=document.getElementById('entity-card'),title=document.getElementById('entity-title'),body=document.getElementById('entity-body'),box=document.getElementById('entity-links');
  const p=(window.TWIN3D_PIPES||[]).find(x=>x.id===id);
  card.style.display='block';
  if(!p){
    title.textContent='管段详情';body.textContent='';
    box.innerHTML='<div class="ec-sec"><small>未在管线台账中找到</small><div class="ec-empty">管段 '+esc(id)+' 不在内嵌管线台账中。</div></div>';
    return;
  }
  title.textContent=p.label+'（'+p.id+'）';
  body.textContent='';
  const isBottleneck=/管径偏小|重现期不足/.test(p.note||'');
  const roadLabel='沿道路 '+p.road;
  let html='<div class="ec-sec"><small>管线台账</small>'
    +'<div class="ec-row"><span>管径</span><b>'+esc(p.diameter_mm)+' mm</b></div>'
    +'<div class="ec-row"><span>埋深</span><b>'+esc(p.depth_m)+' m</b></div>'
    +'<div class="ec-row"><span>材质</span><b>'+esc((window.PIPE_MAT_CN||{})[p.material]||p.material)+'</b></div>'
    +'<div class="ec-row"><span>敷设年份</span><b>'+esc(p.install_year)+'</b></div>'
    +'<div class="ec-row"><span>状态</span><b>'+esc(p.status==='in_service'?'在役':p.status)+'</b></div>'
    +'<div class="ec-row"><span>所在道路</span><b>'+esc(roadLabel)+'</b></div>'
    +'<div class="ec-empty">'+esc(p.note||'')+'</div></div>';
  if(isBottleneck){
    html+='<div class="ec-sec"><small>事件关联</small><div class="ec-empty" style="color:#f5c063">'
      +esc(p.note||'')+'——该管段是本次积水主因之一（重现期不足，管径偏小），推演中已高亮。</div></div>';
  }
  html+='<div class="ec-sec"><small>空间说明</small><div class="ec-empty">台账无实测管线坐标，几何沿'+esc(roadLabel)+'中线示意布设，非测绘定位。</div></div>';
  box.innerHTML=html;
}
function clip(s,n){s=String(s||'');return s.length>n?s.slice(0,n)+'…':s}
// 台账原文里夹着枚举（pump_station / fault_pending_repair），只把枚举换成中文，
// 其余原样保留——改写了就不是台账原文了。
const TYPE_CN={pump_station:'泵站',inlet:'雨水口',manhole:'检查井',pipe:'管段'};
function detailCn(detail,state){
  let s=String(detail||'暂无说明');
  Object.keys(TYPE_CN).forEach(k=>{s=s.split(k).join(TYPE_CN[k])});
  if(state&&SCENE_STATE_CN[state])s=s.split(state).join(SCENE_STATE_CN[state]);
  return s;
}
// ---- 处置前后对照：只比算得出来的项，没有实测就写没有 ----
function renderResults(){
  const box=document.getElementById('results-body'),note=document.getElementById('results-note');
  if(!box)return;
  if(!LINKS){box.textContent='加载中…';return}
  const r=LINKS.results;
  box.innerHTML=(r.rows||[]).map(row=>{
    const before=row.before==null?'—':row.before;
    const after=row.after==null?'无实测':row.after;
    // 结果行回指断言：能挂几条就挂几条，超过 3 条折叠成 +N，避免把对照表撑散
    const cl=(row.claims||[]);
    let basis='';
    if(cl.length){
      const shown=cl.slice(0,3).map(c=>'<span class="badge">'+esc(c.label)+'</span>').join(' ');
      basis='<span class="rc-basis">依据 '+shown+(cl.length>3?' <span class="badge">+'+(cl.length-3)+'</span>':'')+'</span>';
    }else{
      basis='<span class="rc-basis rc-none">无断言支撑（见来源注解）</span>';
    }
    return '<div class="rc-row"><span>'+esc(row.label)+'</span> <span class="v">'+esc(before)+' → '+esc(after)+' <i>'+esc(row.unit||'')+'</i></span><span class="n">'+esc(row.note||'')+'</span>'+basis+'</div>';
  }).join('');
  if(note)note.textContent=r.source+' · 当前停在第 '+(twinState.currentJump===6?'最终状态':(twinState.currentJump+1)+' 跳')+'，对照口径为六跳全部执行完';
}
function loadTwinLinks(){
  return fetch('/api/twin-links?jump='+MAX_JUMP).then(r=>r.json()).then(data=>{
    LINKS=data;
    (data.cards||[]).forEach(c=>{CARD_INDEX[c.card_id]=c});
    buildJumpTargets(data);
    renderJumpControls();updateEvidence();renderResults();
    const hud=document.getElementById('hud-src');
    if(hud)hud.textContent='关联层：'+data.counts.cards+' 张动作卡 · '+data.counts.linked_entities+' 个对象已挂动作 · 状态为推演推进，非现场回传';
    if(twinState.selectedEntity)renderEntityCard(twinState.selectedEntity);
    return data;
  }).catch(()=>{
    const hud=document.getElementById('hud-src');if(hud)hud.textContent='关联层不可用（/api/twin-links 未能加载）';
    return null;
  });
}
// ---- 每跳的取景目标：两个接口拼出来的，不写死坐标 ----
// /api/twin-links 的 jumps[k].entity_ids 给出"这一跳该看哪些对象"，
// /api/scheduler 的 scene.entities 给出这些对象的台账坐标（circle 用 cx/cy，
// line/polygon 取点集重心）。两边都不含 3D 世界坐标，统一用 W() 投影，
// 和管道、积水用的是同一套换算，不会出现"镜头指向另一条街"。
// 优先级 水面>设施>道路>片区：先看"水在哪"，再看是哪个设施，最后才是片区大框。
window.__jumpTargets=null;
function buildJumpTargets(links){
  fetch('/api/scheduler').then(function(r){return r.json()}).then(function(sc){
    const pos={};
    (((sc||{}).scene||{}).entities||[]).forEach(function(e){
      let x=null,z=null;
      if(e.shape==='circle'){x=e.cx;z=e.cy;}
      else if(e.shape==='line'||e.shape==='polygon'){
        // 注意：这段在 Python 三引号字符串里，正则里的反斜杠必须成对写（\\s），
        // 否则 Python 会报 invalid escape sequence（能跑，但每次启动都刷警告）。
        const pts=String(e.points||'').trim().split(/\\s+/).map(function(s){
          const p=s.split(',');return [Number(p[0]),Number(p[1])];
        }).filter(function(p){return isFinite(p[0])&&isFinite(p[1])});
        if(pts.length){
          x=pts.reduce(function(a,p){return a+p[0]},0)/pts.length;
          z=pts.reduce(function(a,p){return a+p[1]},0)/pts.length;
        }
      }
      if(x==null||!isFinite(x)||!isFinite(z))return;
      const w=W(x,z);
      pos[e.id]={x:w[0],z:w[1],kind:e.kind};
    });
    const RANK={water:0,facility:1,road:2,district:3};
    // 积水基准点：取离全景机位最近的那块水面。
    // 场景里有东西两块积水（WO-20261029-007 在作业区、WO-20261029-011 在东侧 60 世界单位外），
    // 取平均会被东侧那块拽走，所以只认最近的一块。
    const waters=Object.keys(pos).map(function(k){return pos[k]}).filter(function(p){return p.kind==='water'});
    let wc=null,best=1e9;
    waters.forEach(function(p){
      const dd=Math.hypot(p.x-CAM_HOME_T.x,p.z-CAM_HOME_T.z);
      if(dd<best){best=dd;wc=p;}
    });
    window.__jumpTargets=((links||{}).jumps||[]).map(function(j){
      const cands=(j.entity_ids||[]).map(function(id){return pos[id]}).filter(Boolean);
      cands.sort(function(a,b){
        const ra=RANK[a.kind]===undefined?9:RANK[a.kind];
        const rb=RANK[b.kind]===undefined?9:RANK[b.kind];
        return ra-rb;
      });
      const pick=cands[0]||null;
      if(!pick)return wc;
      if(!wc||pick.kind==='water')return pick;
      // 关联对象不是水面时（第 4 跳只挂 R-101 + 泵站 DF-PS-01），取景必须由积水主导：
      // 泵站在积水南侧 13 世界单位外，直接对过去整片积水会被甩出画面，
      // 画面只剩一个泵站和红色光柱、"积水 23cm"标签被挤到左边裁掉（实测）。
      // 0.6 积水 + 0.4 关联对象：水始终在画面里，同时镜头朝本跳关注的对象偏。
      return {x:wc.x*0.6+pick.x*0.4,z:wc.z*0.6+pick.z*0.4,kind:pick.kind};
    });
  }).catch(function(){window.__jumpTargets=null});
}
// 按时间取当前取景目标：在相邻两跳的目标之间做 smoothstep 插值。
// 不做插值的话每跨一跳镜头就瞬移一次，"缓慢扫过街区"会变成"跳切"。
function jumpTargetAt(t){
  const T=window.__jumpTargets;
  if(!T||!T.length)return null;
  if(t<=EVT_JUMPS[0])return T[0]||null;
  for(let k=0;k<EVT_JUMPS.length-1;k++){
    if(t<=EVT_JUMPS[k+1]){
      const A=T[k],B=T[k+1];
      if(!A||!B)return A||B||null;
      const f=(t-EVT_JUMPS[k])/Math.max(1e-6,EVT_JUMPS[k+1]-EVT_JUMPS[k]);
      const e=f*f*(3-2*f);
      return {x:A.x+(B.x-A.x)*e,z:A.z+(B.z-A.z)*e};
    }
  }
  return T[T.length-1]||null;
}
// ---- 平面反射（纯代码实现，不依赖任何额外资产）----
// 目的：让积水和湿路面映出建筑与天光。游戏里湿街面的质感，一半来自这一步。
// 做法：把相机关于水面 y=REFL_Y 镜像，用"斜近裁剪面"只渲染水面以上的世界，
// 输出到半分辨率 RT；水面 shader 用反射相机的投影坐标采样。
// 三个必须知道的坑：
//   1) 不做斜近裁剪：反射相机在水下，会先撞上地面/道路，整张图全是路面；
//   2) up 向量要关于法线反射（变成 (0,-1,0)），否则倒影上下颠倒；
//   3) 采样必须用反射相机的 VP 矩阵，不能用屏幕 uv——屏幕 uv 采到的是当前画面，
//      相机一动倒影就"贴"在屏幕上不跟着走。
let reflRT=null,reflCam=null,reflOn=false,reflBound=false;
const REFL_Y=0.46;                       // 主水面高度，与 applyTimeline 里 surface.position.y 一致
const reflVP=new THREE.Matrix4();
const _reflPos=new THREE.Vector3(),_reflAt=new THREE.Vector3(),_reflN=new THREE.Vector3(0,1,0);
const _reflPlane=new THREE.Plane(),_reflQ=new THREE.Vector4();
function initReflection(){
  if(!renderer)return;
  reflRT=new THREE.WebGLRenderTarget(
    Math.max(320,Math.floor(innerWidth*0.55)),
    Math.max(200,Math.floor(innerHeight*0.55)),
    {minFilter:THREE.LinearFilter,magFilter:THREE.LinearFilter,format:THREE.RGBAFormat,stencilBuffer:false});
  reflCam=new THREE.PerspectiveCamera(46,innerWidth/innerHeight,0.1,500);
  reflOn=true;
}
function collectReflExclude(){
  const arr=[];
  (window.__waterMeshes||[]).forEach(function(w){if(w.surface)arr.push(w.surface);});
  if(rain)arr.push(rain);
  (window.__lampBeams||[]).forEach(function(c){arr.push(c);});
  scene3.traverse(function(o){if(o.isSprite)arr.push(o);});        // 所有文字标签
  Object.keys(RINGS||{}).forEach(function(id){
    const fx=RINGS[id];if(!fx)return;
    if(fx.ring)arr.push(fx.ring);if(fx.beam)arr.push(fx.beam);
  });
  (window.__pumpAlarms||[]).forEach(function(p){if(p.beam)arr.push(p.beam);if(p.lamp)arr.push(p.lamp);});
  window.__reflExclude=arr;
}
function updateReflection(){
  if(!reflOn||!camera||!window.__waterMeshes||!window.__waterMeshes.length)return;
  // 没有一块水面可见时直接跳过（省一整套场景渲染）
  let any=false;
  for(let i=0;i<window.__waterMeshes.length;i++){
    const s=window.__waterMeshes[i].surface;if(s&&s.visible){any=true;break;}
  }
  if(!any)return;
  if(!window.__reflCollected){window.__reflCollected=true;collectReflExclude();}
  if(!reflBound){
    window.__waterMeshes.forEach(function(w){
      const u=w.surface&&w.surface.material&&w.surface.material.uniforms;
      if(u&&u.uRefl){u.uRefl.value=reflRT.texture;u.uHasRefl.value=1;}
    });
    reflBound=true;
  }
  // 1) 镜像相机
  _reflPos.copy(camera.position);_reflPos.y=2*REFL_Y-_reflPos.y;
  camera.getWorldDirection(_reflAt);_reflAt.multiplyScalar(20).add(camera.position);
  _reflAt.y=2*REFL_Y-_reflAt.y;
  reflCam.position.copy(_reflPos);
  reflCam.up.set(0,1,0).reflect(_reflN);
  reflCam.lookAt(_reflAt);
  reflCam.fov=camera.fov;reflCam.aspect=camera.aspect;reflCam.near=camera.near;reflCam.far=camera.far;
  reflCam.updateProjectionMatrix();
  reflCam.updateMatrixWorld();
  // 2) 斜近裁剪面：把近平面压到水面上，只保留 y>=REFL_Y
  _reflPlane.setFromNormalAndCoplanarPoint(_reflN,new THREE.Vector3(0,REFL_Y,0));
  _reflPlane.applyMatrix4(reflCam.matrixWorldInverse);
  _reflQ.set(_reflPlane.normal.x,_reflPlane.normal.y,_reflPlane.normal.z,_reflPlane.constant);
  const pe=reflCam.projectionMatrix.elements;
  const cq=new THREE.Vector4(
    (Math.sign(_reflQ.x)+pe[8])/pe[0],
    (Math.sign(_reflQ.y)+pe[9])/pe[5],
    -1.0,
    (1.0+pe[10])/pe[14]);
  const cdot=_reflQ.x*cq.x+_reflQ.y*cq.y+_reflQ.z*cq.z+_reflQ.w*cq.w;
  const cs=2.0/cdot;
  pe[2]=_reflQ.x*cs;pe[6]=_reflQ.y*cs;pe[10]=_reflQ.z*cs+1.0;pe[14]=_reflQ.w*cs;
  reflVP.multiplyMatrices(reflCam.projectionMatrix,reflCam.matrixWorldInverse);
  // 3) 渲一遍镜像场景
  const excl=window.__reflExclude||[],saved=[];
  for(let i=0;i<excl.length;i++){const o=excl[i];if(o&&o.visible){o.visible=false;saved.push(o);}}
  const prevRT=renderer.getRenderTarget();
  renderer.setRenderTarget(reflRT);
  renderer.clear();
  renderer.render(scene3,reflCam);
  renderer.setRenderTarget(prevRT);
  for(let i=0;i<saved.length;i++)saved[i].visible=true;
  // 4) 把矩阵交给水面 shader
  window.__waterMeshes.forEach(function(w){
    const u=w.surface&&w.surface.material&&w.surface.material.uniforms;
    if(u&&u.uReflVP)u.uReflVP.value.copy(reflVP);
  });
}
// ---- 后处理链：bloom + 色差 + 暗角 + 胶片颗粒（纯代码，不引外部依赖）----
// three r128 有个关键行为：只有渲染到**屏幕**时才应用 renderer.toneMapping / outputEncoding，
// 渲染到 RT 时是纯线性值。所以一旦走后期，ACES 和 sRGB 编码必须自己在这条链的最后一棒补回来，
// 否则整个画面会发灰发暗。这也顺带带来一个好处：bloom 作用在 HDR 线性值上，亮的更亮、不糊。
let postOK=false,sceneRT=null,bloomA=null,bloomB=null;
let postScene=null,postCam=null,postQuad=null;
const postMat={bright:null,blur:null,comp:null};
const POST_VS=['varying vec2 vUv;','void main(){vUv=uv;gl_Position=vec4(position.xy,0.0,1.0);}'].join(' ');
function initPost(){
  if(!renderer)return;
  // 调试开关：?nopost=1 回到直出，用来区分"后处理出问题"和"场景本身出问题"
  if(/[?&]nopost=1/.test(location.search))return;
  const sz=renderer.getDrawingBufferSize(new THREE.Vector2());
  const w=Math.max(2,sz.x),h=Math.max(2,sz.y);
  // 用 UnsignedByte 而不是 HalfFloat：swiftshader / 移动端对浮点 RT 支持不稳，
  // 建 HalfFloat RT 会直接报 ERR_INSUFFICIENT_RESOURCES 并整页白屏（踩过）。
  // 代价是高光被 clamp 到 1.0，bloom 只对"接近纯白"的像素起作用——亮度阈值相应降低即可。
  const hf=THREE.UnsignedByteType;
  const opt={minFilter:THREE.LinearFilter,magFilter:THREE.LinearFilter,format:THREE.RGBAFormat,type:hf,stencilBuffer:false};
  sceneRT=new THREE.WebGLRenderTarget(w,h,Object.assign({depthBuffer:true},opt));
  // 关键：让 RT 自己带 sRGB 编码。8bit 线性存储时暗部只有几十级，
  // 再在 comp 里做 pow(0.4545) 会把量化误差放大——实测暗部从 17 抬到 50，整张图发灰。
  // 标成 sRGB 后 r128 在渲染到 RT 时就编码好，comp 直接输出即可，暗部层级保住。
  sceneRT.texture.encoding=THREE.sRGBEncoding;
  const bw=Math.max(2,Math.floor(w/4)),bh=Math.max(2,Math.floor(h/4));
  bloomA=new THREE.WebGLRenderTarget(bw,bh,Object.assign({depthBuffer:false},opt));
  bloomB=new THREE.WebGLRenderTarget(bw,bh,Object.assign({depthBuffer:false},opt));
  postScene=new THREE.Scene();
  postCam=new THREE.OrthographicCamera(-1,1,1,-1,0,1);
  postQuad=new THREE.Mesh(new THREE.PlaneGeometry(2,2),
    new THREE.MeshBasicMaterial({depthTest:false,depthWrite:false}));
  postQuad.frustumCulled=false;postScene.add(postQuad);
  const base={depthTest:false,depthWrite:false,vertexShader:POST_VS};
  // 1) 亮部提取：只留超过阈值的部分，并把能量归一化，避免大面积亮天把整个画面糊成糖水
  postMat.bright=new THREE.ShaderMaterial(Object.assign({},base,{
    uniforms:{tSrc:{value:null},uTh:{value:0.88}},
    fragmentShader:['uniform sampler2D tSrc;uniform float uTh;varying vec2 vUv;',
      'void main(){',
      '  vec3 c=texture2D(tSrc,vUv).rgb;',
      '  float l=dot(c,vec3(0.2126,0.7152,0.0722));',
      '  float k=max(l-uTh,0.0)/max(l,0.0001);',
      '  gl_FragColor=vec4(c*k,1.0);',
      '}'].join(' ')}));
  // 2) 可分离高斯（5 tap 近似 9 tap）：一次水平一次垂直，比一次大核便宜一半
  postMat.blur=new THREE.ShaderMaterial(Object.assign({},base,{
    uniforms:{tSrc:{value:null},uDir:{value:new THREE.Vector2(1,0)},uTexel:{value:new THREE.Vector2(1/bw,1/bh)}},
    fragmentShader:['uniform sampler2D tSrc;uniform vec2 uDir;uniform vec2 uTexel;varying vec2 vUv;',
      'void main(){',
      '  vec2 o=uDir*uTexel;',
      '  vec3 s=texture2D(tSrc,vUv).rgb*0.2270270;',
      '  s+=texture2D(tSrc,vUv+o*1.3846154).rgb*0.3162162;',
      '  s+=texture2D(tSrc,vUv-o*1.3846154).rgb*0.3162162;',
      '  s+=texture2D(tSrc,vUv+o*3.2307692).rgb*0.0702703;',
      '  s+=texture2D(tSrc,vUv-o*3.2307692).rgb*0.0702703;',
      '  gl_FragColor=vec4(s,1.0);',
      '}'].join(' ')}));
  // 3) 合成：色差 → 叠 bloom → 曝光 → ACES → sRGB → 暗角 → 颗粒
  postMat.comp=new THREE.ShaderMaterial(Object.assign({},base,{
    uniforms:{tScene:{value:null},tBloom:{value:null},uBloom:{value:0.10},
      uContrast:{value:0.14},uGamma:{value:1.0},uTime:{value:0},uRes:{value:new THREE.Vector2(w,h)},
      uGrain:{value:0.015},uVig:{value:0.20},uAberr:{value:0.0014}},
    fragmentShader:['uniform sampler2D tScene;uniform sampler2D tBloom;',
      'uniform float uBloom;uniform float uContrast;uniform float uGamma;uniform float uTime;uniform float uGrain;',
      'uniform float uVig;uniform float uAberr;uniform vec2 uRes;varying vec2 vUv;',
      // 这里刻意**不做** ACES tonemap：主场景里大量颜色是经 srgbHex() 转过的线性值，
      // 但水面/标签这类 ShaderMaterial 是自己输出的 sRGB 值，两条管线混在一起。
      // 在这个"混合空间"上套 ACES 会把画面整体压灰（实测整屏发白、对比掉一半）。
      // 所以后处理只做风格化：S 曲线提对比 + 轻微 gamma + bloom + 暗角 + 颗粒，不动颜色空间。
      'void main(){',
      '  vec2 d=vUv-0.5;',
      // 色差：越靠画面边缘偏移越大，模拟镜头边缘的色散；中心几乎为 0，不影响读字
      '  vec2 ab=d*uAberr*2.0;',
      '  vec3 c;c.r=texture2D(tScene,vUv+ab).r;c.g=texture2D(tScene,vUv).g;c.b=texture2D(tScene,vUv-ab).b;',
      '  c+=texture2D(tBloom,vUv).rgb*uBloom;',
      '  c=clamp(c,0.0,1.0);',
      '  c=mix(c,c*c*(3.0-2.0*c),uContrast);',
      // r128 渲染到 RT 时不应用 outputEncoding，RT 里是线性值；直接写到画布会整体发暗。
      // 所以这一棒必须自己做 sRGB 编码（指数 0.4545），亮度才和"直出"对得上。
      '  c=pow(max(c,vec3(0.0)),vec3(uGamma));',
      '  float v=1.0-uVig*pow(length(d)*1.55,2.2);',
      '  c*=clamp(v,0.0,1.0);',
      '  float g=fract(sin(dot(vUv*uRes+uTime*37.0,vec2(12.9898,78.233)))*43758.5453);',
      '  c+=(g-0.5)*uGrain;',
      '  gl_FragColor=vec4(c,1.0);',
      '}'].join(' ')}));
  postOK=true;
}
function resizePost(){
  if(!postOK)return;
  const sz=renderer.getDrawingBufferSize(new THREE.Vector2());
  const w=Math.max(2,sz.x),h=Math.max(2,sz.y);
  sceneRT.setSize(w,h);
  const bw=Math.max(2,Math.floor(w/4)),bh=Math.max(2,Math.floor(h/4));
  bloomA.setSize(bw,bh);bloomB.setSize(bw,bh);
  postMat.blur.uniforms.uTexel.value.set(1/bw,1/bh);
  postMat.comp.uniforms.uRes.value.set(w,h);
}
function renderPost(now){
  postMat.bright.uniforms.tSrc.value=sceneRT.texture;
  postQuad.material=postMat.bright;
  renderer.setRenderTarget(bloomA);renderer.render(postScene,postCam);
  postMat.blur.uniforms.tSrc.value=bloomA.texture;
  postMat.blur.uniforms.uDir.value.set(1,0);
  postQuad.material=postMat.blur;
  renderer.setRenderTarget(bloomB);renderer.render(postScene,postCam);
  postMat.blur.uniforms.tSrc.value=bloomB.texture;
  postMat.blur.uniforms.uDir.value.set(0,1);
  renderer.setRenderTarget(bloomA);renderer.render(postScene,postCam);
  postMat.comp.uniforms.tScene.value=sceneRT.texture;
  postMat.comp.uniforms.tBloom.value=bloomA.texture;
  postMat.comp.uniforms.uTime.value=(now||0)/1000;
  postQuad.material=postMat.comp;
  renderer.setRenderTarget(null);renderer.render(postScene,postCam);
}
function tick(){
  const now=performance.now();const dt=Math.min(0.1,Math.max(0,now-__lastFrame)/1000);__lastFrame=now;
  if(controls)controls.update();
  if(__evtPlaying){__evtClock+=dt;if(__evtClock>=EVT_DURATION){__evtClock=EVT_DURATION;__evtPlaying=false;renderJumpControls();}}
  // 人物的骨骼动画（worker_rig.glb）：整体跟着画面推进，暂停时也保持播放
  (window.__crewMixers||[]).forEach(function(mx){
    // kneel 的定格：只在**超过** KNEEL_AT 之后才钉住，之前让它自然播。
    // 原来是无条件钉在 KNEEL_AT，于是切到 kneel 的那一帧就已经是"跪稳"的姿势，
    // 观众看不到人从站着蹲下去的过程——"直接就是最终动作"就是这么来的。
    (window.__crewRigs||[]).forEach(function(r){
      if(r&&r.cur==='kneel'&&r.acts&&r.acts.kneel&&r.acts.kneel.time>window.__kneelAt){
        r.acts.kneel.time=window.__kneelAt;
      }
    });
    mx.update(dt);
  });
  // 播放中：时间轴连续驱动画面；暂停/手动跳步后：applyTimeline 已把状态定住，这里只做呼吸/闪烁
  const t=__evtClock/EVT_DURATION;
  if(__evtPlaying)applyTimeline(t);
  // 播放中同步当前跳次（供证据面板/按钮高亮跟随时间轴）
  if(__evtPlaying){syncJumpFromClock();}
  // 关键动作相机运镜：封路阶段（施工队到场→放下栏杆）自动拉近到封路点，给"现场镜头"
  // 暂停时也要算一次：暂停后画面必须停在同一构图上，而围栏位置是由 __seal 驱动的动态值。
  // 只在播放时算的话，用 ?t= 直接打开某一帧会拍到"围栏还在路缘、人已经站到路中"的错位画面。
  // __camUser：用户手动拖过视角就让位，直到再点播放/跳次（那时会清掉这个标志）。
  if(camera&&window.__roadCrew&&(__evtPlaying||!window.__camUser)){driveCamera(t);}
  // 雨丝：每滴是 line 的两个端点，一起下移；落到地面以下就回到顶部重新落。
  // 不用"暂停就冻住"——雨是环境，不是被推演驱动的对象，停下来反而像画面卡住。
  if(rain&&rain.material&&rain.material.opacity>0.03){
    const p=rainGeo.attributes.position,a=p.array;
    for(let i=0;i<p.count;i+=2){
      const v=(rainSpeed?rainSpeed[i/2]:16)*dt;
      a[i*3+1]-=v; a[i*3+4]-=v;
      a[i*3]+=v*0.05; a[i*3+3]+=v*0.05;
      if(a[i*3+1]<-2){
        const L=Math.max(0.12,a[i*3+1]-a[i*3+4]);
        const ny=46+Math.random()*6;
        a[i*3+1]=ny; a[i*3+4]=ny-L;
      }
    }
    p.needsUpdate=true;
  }
  // 路灯光锥跟着雨势走：小雨不亮、暴雨才散出光柱。
  // 用雨丝的透明度当雨势代理，省得再从时间轴里取一遍状态。
  if(window.__lampBeams){
    const ro=(rain&&rain.material)?rain.material.opacity:0;
    const amt=Math.max(0,Math.min(1,(ro-0.12)/0.42))*0.55;
    for(let i=0;i<window.__lampBeams.length;i++){
      const c=window.__lampBeams[i];
      c.visible=amt>0.02;
      if(c.visible)c.material.uniforms.uAmt.value=amt;
    }
  }
  const beat=Math.sin(now/380)*0.5+0.5;
  (window.__pumpAlarms||[]).forEach(p=>{
    // 光柱常亮（告警时），顶灯闪烁
    if(p.beam)p.beam.visible=!!p.on;
    if(p.lamp)p.lamp.visible=!!p.on&&(Math.floor(now/320)%2===0);
  });
  // 瓶颈管段：水位高时橙色脉动（透明度随 beat 起伏），让「管径偏小」在播放中一眼可见
  (window.__pipeSegments||[]).forEach(s=>{
    if(!s.isBottleneck||!s.mat)return;
    // Basic 材质没有 emissiveIntensity，脉动改用透明度（0.76→0.98），
    // 颜色不变——色相一抖就不像"同一根管在报警"，像闪灯故障。
    const hot=s.mat.color.getHex()===0xd23c00;
    if(hot){s.mat.opacity=0.76+0.22*beat;}
  });
  // 管径按相机距离自适应：屏幕上恒定约 6px 粗。
  // 固定管径在两种机位下必然一头不对——0.31 世界单位（≈1.9m）在俯视 57m 处只有 4.7px、
  // 细如发丝；在特写 7m 处却有 38px、直接横贯画面糊成两条色带（实测）。
  // 缩放量与距离成正比 ⇒ 屏幕尺寸恒定。只缩横截面（局部 XZ），长度（局部 Y）不动。
  (window.__pipeSegments||[]).forEach(s=>{
    if(!s.mesh||!s.mesh.visible)return;
    s.mesh.children.forEach(o=>{
      if(!o.isMesh||!o.userData||!o.userData.__radial)return;
      const dx=o.position.x-camera.position.x,dy=o.position.y-camera.position.y,dz=o.position.z-camera.position.z;
      const k=Math.min(3.2,Math.max(0.12,Math.sqrt(dx*dx+dy*dy+dz*dz)/45));
      o.scale.set(k,1,k);
    });
  });
  // 施工队到场后：从路边走到路口（连续移动插值，不是瞬移）
  moveCrew();
  // 跨部门协作层：部门站牌 + 派单/会签/调泵的卡片飞行 + 护栏推进到位
  updateCollab(__evtClock/EVT_DURATION);
  // 事件淡入：派单气泡/封路护栏出现时透明度从 0→1 平滑过渡（不是瞬间冒出）
  fadeInOut();
  // shader 水面：推进波纹时间（顶点波浪随时间流动）
  (window.__waterMeshes||[]).forEach(w=>{
    if(w.surface&&w.surface.material.uniforms){w.surface.material.uniforms.uTime.value=now/1000;}
  });
  Object.keys(RINGS).forEach(id=>{const fx=RINGS[id];if(!fx||!fx.ring.visible)return;
    const dim=fx.ring.material.color.getHex()===0x5a6b7a;
    // 只剩 1~2 个环之后可以画实一点：0.16 是为了压住「满屏淡圈」，现在没有满屏了。
    // 环带宽 0.28 世界（≈1.7m）已经收细，透明度再收到 0.30~0.40，避免读成一块盘子。
    fx.ring.material.opacity=dim?0.24:(0.30+0.10*beat);
    fx.beam.material.opacity=dim?0.06:(0.08+0.05*beat);
    const s=1+0.05*beat;fx.ring.scale.set(s,s,1)});
  // 平面反射必须画在主画面之前：它要先用镜像相机渲一遍到 RT
  updateReflection();
  if(postOK){
    renderer.setRenderTarget(sceneRT);
    renderer.render(scene3,camera);
    renderPost(now);
  }else{
    renderer.render(scene3,camera);
  }
}
// 根据时间轴时钟反推当前跳次，让证据面板/按钮高亮/事件栏跟随播放
function syncJumpFromClock(){
  const t=__evtClock/EVT_DURATION;
  let idx=0;for(let i=0;i<EVT_JUMPS.length;i++){if(t>=EVT_JUMPS[i]-0.0001)idx=i;}
  if(idx!==twinState.currentJump){
    twinState.currentJump=idx;
    paintHighlights();renderJumpControls();updateEvidence();renderResults();
    if(twinState.selectedEntity)renderEntityCard(twinState.selectedEntity);
    renderEventBar(idx);
  }
  syncProgress();
}
// 进度条同步：把当前时钟映射到进度条位置 + 时间文字
function syncProgress(){
  const bar=document.getElementById('progress'),timeEl=document.getElementById('progress-time');
  if(bar){bar.value=Math.round(__evtClock/EVT_DURATION*1000);}
  if(timeEl){const s=Math.round(__evtClock);const mm=String(Math.floor(s/60)).padStart(2,'0'),ss=String(s%60).padStart(2,'0');timeEl.textContent='T+'+mm+':'+ss;}
  if(LINKS)renderEventBar(twinState.currentJump);
}
// 施工队：到场阶段，从路口一侧走到封路点（连续移动，不是瞬移）
// 现场处置的到达过程：工程车驶入 → 人员下车 → 步行到作业点 → 布锥桶 → 接移动泵
// 全部挂在同一条时间轴上（第 5 跳 施工队到场 → 第 6 跳 封路完成），不让人凭空出现在路口
// ---- 协作层的每帧更新：站牌文案随跳次换、任务卡在两点之间飞、护栏从路缘推进到位 ----
const ORG_SHORT={'EMERG-COORD':'应急局','ORG-EMERG':'应急局','ORG-DRAIN':'排水公司','ORG-ROAD':'道路中心','ORG-TRAFFIC':'交警','ORG-POWER':'供电'};
const CARD_STATE_COLOR={pending:'#6b7f92',running:'#5cc8f5',done:'#4fdb95',needs_confirm:'#ffb347',cancelled:'#6b7f92'};
// 站牌/飞行卡的文字纹理。640×96 的宽画布 + 自动缩字号：
// 「道路中心｜封积水路段·待交警会签」这种长句在 512 画布上会溢出成半个字。
function collabTex(text,color){
  const cv=document.createElement('canvas');cv.width=640;cv.height=96;
  const g=cv.getContext('2d');
  let fs=28;
  do{g.font='bold '+fs+'px system-ui';if(g.measureText(text).width<=606)break;fs-=2;}while(fs>=16);
  g.textAlign='center';g.textBaseline='middle';
  g.lineWidth=7;g.strokeStyle='rgba(8,16,24,.92)';g.strokeText(text,320,48);
  g.fillStyle=color||'#d5e4f0';g.fillText(text,320,48);
  const tex=new THREE.CanvasTexture(cv);tex.minFilter=THREE.LinearFilter;return tex;
}
// 动作卡原文常常是一整句话（"封闭积水路段全幅（深度≥25cm…）"），站牌上放不下，
// 按关键词压成 4~6 字。压不出来就截断，不做猜测性改写。
function shortAction(c){
  const a=String((c&&c.action)||'');
  const M={observed:'识别事件',affected:'圈定影响',owner:'派单·责任落位',conflict:'上报冲突',resource:'核验缺口',dispatch_pump:'调移动泵'};
  if(M[a])return M[a];
  if(/联合发布|单一部门/.test(a))return '联合发布封路';
  if(/封闭下穿隧道/.test(a))return '封下穿隧道';
  if(/封闭积水路段/.test(a))return '封积水路段';
  if(/绕行方案/.test(a))return '组织绕行';
  if(/先清掏/.test(a))return '清掏后抽排';
  if(/隔离风险源/.test(a))return '隔离风险源';
  if(/启动泵站/.test(a))return '启动泵站';
  if(/复检管道/.test(a))return '复检燃气';
  if(/停止基坑/.test(a))return '停止开挖';
  if(/交通管制/.test(a))return '交通管制';
  return a.slice(0,8);
}
// 一个部门在同一跳可能挂着好几张卡（道路中心在处置跳有 3 张封路卡），
// 取"最需要被看见"的那张：待会签 > 执行中 > 已完成，都pending就当本跳没它的事。
function pickOrgCard(org,jump){
  const cards=(typeof LINKS!=='undefined'&&LINKS&&LINKS.cards)||[];
  const RANK={needs_confirm:3,running:2,done:1,pending:0,cancelled:0};
  let best=null,br=-1;
  for(let i=0;i<cards.length;i++){
    const c=cards[i];
    if(c.agent_id!==org)continue;
    const st=(c.states||[])[jump];
    if(!st||st==='pending')continue;
    const r=RANK[st]||0;
    if(r>br){br=r;best=c;best.__st=st;}
  }
  return best;
}
function updateCollab(t){
  const C=window.__collab;if(!C||!C.built)return;
  const jump=Math.max(0,Math.min(6,twinState.currentJump|0));
  // ① 部门站牌：写"这一跳它在干什么"，颜色是这张卡在这一跳的状态
  C.sites.forEach(function(s){
    const c=pickOrgCard(s.org,jump);
    if(!c){s.sp.visible=false;s.rv=-1;return;}
    const st=c.__st||'pending';
    let txt=ORG_SHORT[s.org]+'｜'+shortAction(c);
    const cs=(c.co_sign_required||[]);
    if(st!=='done'&&cs.length)txt+='·待'+(ORG_SHORT[cs[0]]||'')+'会签';
    if(st==='done')txt+=' ✓';
    const key=txt+'|'+st;
    if(key!==s.key){
      s.key=key;
      if(s.sp.material.map)s.sp.material.map.dispose();
      s.sp.material.map=collabTex(txt,CARD_STATE_COLOR[st]||'#cfe6f5');
      s.sp.material.needsUpdate=true;
    }
    s.sp.visible=true;
    // 站牌也是淡入，不硬弹（0.68 秒）
    if(!s.rv||s.rv<0)s.rv=t;
    s.sp.material.opacity=Math.min(1,(t-s.rv)/0.020);
  });
  // ② 任务卡飞行：从 A 站划一道弧线交到 B 站
  C.flights.forEach(function(f){
    const p=(t-f.t0)/(f.t1-f.t0);
    if(p<0||p>1){f.sp.visible=false;return;}
    const e=p*p*(3-2*p);
    f.sp.visible=true;
    f.sp.position.set(
      f.a.x+(f.b.x-f.a.x)*e,
      f.a.y+(f.b.y-f.a.y)*e+Math.sin(Math.PI*e)*(f.lift||1),
      f.a.z+(f.b.z-f.a.z)*e
    );
    f.sp.material.opacity=Math.min(1,Math.min(p,1-p)*8);
  });
  // ③ 会签徽标：两张卡在路中央合流之后才亮，护栏随即到位
  if(C.badge){
    const on=t>=0.750&&t<=0.968;
    C.badge.visible=on;
    if(on)C.badge.material.opacity=Math.min(1,(t-0.750)/0.018);
  }
  // ④ 封路这个动作：两块围栏由 0 号/1 号分别从南北路缘推入路中。
  //    **进度不再由时间函数驱动**，而是由 moveCrew 里推板那个人的走位写出（__seal.w/.e），
  //    所以「谁推的、推到哪」和画面上那个人的位置是同一份数据，不会出现"人还没走到、牌已经封好"。
  //    旧实现是一个全局 be=(t-0.742)/0.058，两块板同时凭空滑进来，与工人无关——这是
  //    "封路不是工人封的"的根子。
  const seal=window.__seal||(window.__seal={w:0,e:0});
  (window.__roadBlocks||[]).forEach(function(b){
    b.mesh.children.forEach(function(seg){
      const f=seg.userData&&seg.userData.fix,k=seg.userData&&seg.userData.park;
      if(!f||!k)return;
      let p=(seg.userData.side===0)?seal.w:seal.e;
      p=Math.max(0,Math.min(1,p));
      const be=p*p*(3-2*p);
      seg.position.x=f.x+(k.x-f.x)*be;
      seg.position.z=f.z+(k.z-f.z)*be;
    });
  });
}
// 道具出场：从 0 长到 1（dur 是时间轴比例，0.016≈0.55 秒）。
// 所有道具 Group 的原点都落在地面/落点上，缩放读起来就是"就地立起来 / 放下去"。
// 之前一律用 visible 开关，观感是硬弹出来——这是"事件不连贯"最主要的来源。
function popIn(obj,t0,t,dur){
  if(!obj)return false;
  const p=Math.max(0,Math.min(1,(t-t0)/(dur||0.016)));
  obj.visible=p>0;
  if(p>0){const e=1-Math.pow(1-p,3);obj.scale.setScalar(e);}
  return p>=1;
}
function moveCrew(){
  const c=window.__roadCrew;if(!c)return;
  const t=__evtClock/EVT_DURATION;
  // 施工段的时间窗不能直接用 EVT_JUMPS[4]~[5]（0.64~0.82）：动作序列占的是 f 的
  // 0.42~0.86，映射回时间轴只剩 2 秒——车刚停人就下车、锥还没摆完泵就出水，
  // 观感是"快进"。往前放到 0.54（镜头推近时车已在路上）、往后延到 0.92，
  // 整段拉到 8.8 秒，撬盖→接管→泵启动才读得出先后。
  // t0 定在 0.62（第 5 跳 0.56 之后 0.06）：车开进来要 4 秒多，
  // 到约 0.759 才停稳放人下车——正好卡在第 6 跳（0.76，文案切到「处置顺序」）那一刻。
  // 之前为让特写里有人把 t0 提前到 0.54，结果人在「缺口核验」阶段就下车干活，文案和画面对不上。
  // ---- 到场时间轴（t 为 0~1，×44 秒）----
  //   0.300 工程车驶入 → 0.500 停稳在作业点东侧 6.6m
  //   0.505 三人下车
  //   0.505~0.640 0 号往西走 15.6m 到西侧围栏、1 号往东 2.2m 到东侧围栏（2 号开始沿锥线下锥）
  //   0.640~0.745 两人把围栏从路缘推到路中 → 封路完成（「封路」牌子此时才亮）
  //   0.745~0.800 两人退回井口就位 → 0.800 撬棍 → 0.835 掀盖
  //   0.860 卸泵 → 0.872 接管 → 0.895 泵启动出水
  // **顺序是硬的**：封路没完成不可能撬盖——撬棍的出场面卡在 T_SEAL_DONE 上（见下面的 gate），
  // 不是"时间上碰巧排在前面"。
  // 原来这段的顺序是反的：0.700 就跪下撬盖，而围栏 0.742~0.800 才由时间函数自动推进来，
  // 变成"人已经在掏井，栏杆自己飘过来"。用户指出"封路应该是工人封的、先封路再工作"就是这件事。
  const T_TRUCK0=0.300, T_PARK=0.500, T_WALK0=0.505;
  const T_SEAL_AT=0.640, T_SEAL_DONE=0.745, T_BACK=0.800, T_DONE=0.950;
  const on=t>=T_TRUCK0;
  if(c.truck)c.truck.visible=on;
  if(!on){
    c.group.visible=false;
    (c.cones||[]).forEach(g=>{g.visible=false;});
    ['pump','jet','hose','lid','mud','pry','status','marker'].forEach(k=>{if(c[k])c[k].visible=false;});
    return;
  }
  const sm=function(x){x=Math.max(0,Math.min(1,x));return x*x*(3-2*x);};
  // 1) 工程车驶入并停稳：ease-out，越接近终点越慢，读起来是"刹停"而不是匀速滑过
  const dp=Math.max(0,Math.min(1,(t-T_TRUCK0)/(T_PARK-T_TRUCK0)));
  const drv=1-Math.pow(1-dp,2.0);
  if(c.truck){
    c.truck.position.x=c.drive.x+(c.park.x-c.drive.x)*drv;
    c.truck.position.z=c.drive.z+(c.park.z-c.drive.z)*drv;
  }
  // 2) 下车 + 走位：每人从车门走到自己工位，各错 0.55 秒
  if(t<T_WALK0){c.group.visible=false;return;}
  c.group.visible=true;
  if(c.label)c.label.visible=false;
  if(c.marker)c.marker.visible=false;
  const P=c.park,TO=c.to;
  const L=function(wx,wz){return [wx-P.x,wz-P.z];};   // 世界 → slot 局部（group 钉在停车点）
  // 工位（相对作业点）：0 号跪在井口西端撬盖，1 号蹲在井口东端插吸水管
  // （两个人分别管"开盖"和"接管"，这是现场真实分工；早先 1 号站在泵边，
  //   身边没有任何能碰到的东西，看着就是蹲在那儿发呆）
  const ST=[[-0.105,0.015],[0.150,-0.105],[0,0]];
  // 2 号的行走路径：车门 → 沿锥线逐个下锥 → 锥线末端站定警戒
  const CS=(c.cones&&c.cones.length)?c.cones.map(g=>[g.position.x-TO.x,g.position.z-TO.z]):[[0.60,-0.22],[0.86,-0.22],[1.12,-0.22],[1.38,-0.22]];
  const P2=[L(TO.x+0.24,TO.z+0.30)].concat(CS.map(cn=>L(TO.x+cn[0],TO.z+cn[1]))).concat([L(TO.x+1.52,TO.z-0.30)]);
  const segL=[];let total2=0;
  for(let k=1;k<P2.length;k++){const d=Math.hypot(P2[k][0]-P2[k-1][0],P2[k][1]-P2[k-1][1]);segL.push(d);total2+=d;}
  const frac2=[];{let acc=0;for(let k=0;k<segL.length;k++){acc+=segL[k];frac2.push(total2>0?acc/total2:1);}}
  const at2=function(u){
    let want=Math.max(0,Math.min(1,u))*total2;
    for(let k=0;k<segL.length;k++){
      if(want<=segL[k]||k===segL.length-1){
        const q=segL[k]>0?Math.min(1,want/segL[k]):1;
        return [P2[k][0]+(P2[k+1][0]-P2[k][0])*q,P2[k][1]+(P2[k+1][1]-P2[k][1])*q];
      }
      want-=segL[k];
    }
    return P2[P2.length-1];
  };
  const pull=Math.sin(__evtClock*2.6);
  // 封路进度：**由 t 直接反推**，不依赖"这一帧是否落在推板窗口内"。
  // 时间轴可以任意跳（点跳次、拖进度条），如果只在推板那几帧里写 __seal，
  // 直接跳到 0.75 时 seal 会停在初始 0，围栏退回路缘——验收时就踩到过。
  // 人在这段窗口里的位置也用同一个 p 算，所以"手在板上、板跟着人走"是同一份参数。
  const seal=window.__seal||(window.__seal={w:0,e:0});
  const sealP=Math.max(0,Math.min(1,(t-T_SEAL_AT)/Math.max(0.001,T_SEAL_DONE-T_SEAL_AT)));
  seal.w=sealP;seal.e=sealP;
  // 取围栏的停放位/封路位：和 updateCollab 用的是同一份 userData
  const RB=(window.__roadBlocks&&window.__roadBlocks[0]&&window.__roadBlocks[0].mesh&&window.__roadBlocks[0].mesh.children)||[];
  const boardOf=function(k){
    for(let q=0;q<RB.length;q++){const u=RB[q].userData;if(u&&u.side===k&&u.fix&&u.park)return u;}
    return null;
  };
  (c.slots||[]).forEach(function(s,i){
    const st0=T_WALK0+i*0.010;
    s.visible=t>=st0;
    if(!s.visible)return;
    let x=0,z=0,yaw=0,want='idle',arrived=false;
    if(i===2){
      // 2 号：车门 → 沿锥线逐个下锥 → 锥线末端站定警戒
      const u2=Math.max(0,Math.min(1,(t-(T_WALK0+0.022))/(T_DONE-0.02-(T_WALK0+0.022))));
      const p2=at2(u2);x=p2[0];z=p2[1];
      if(u2<0.995){const a=at2(u2),b=at2(Math.min(1,u2+0.012));yaw=Math.atan2(b[0]-a[0],b[1]-a[1]);}
      else yaw=Math.PI*0.5;
      want=(t<T_DONE-0.010)?'walk':'idle';
    }else{
      // 0/1 号：下车 → 走到自己那侧围栏外端 → 推板入位 → 回井口工位
      const bd=boardOf(i);
      const sx=0.30-i*0.10, sz=0.42+i*0.05;                // 车门（车厢右后侧）
      const h0=bd?L(bd.hand0.x,bd.hand0.z):[sx,sz];         // 围栏停放时人站的位置
      const h1=bd?L(bd.hand1.x,bd.hand1.z):[sx,sz];         // 围栏推到路中时人站的位置
      const ex=TO.x+ST[i][0]-P.x, ez=TO.z+ST[i][1]-P.z;     // 井口工位
      if(t<T_SEAL_AT){
        const q=sm((t-st0)/Math.max(0.001,T_SEAL_AT-st0));
        x=sx+(h0[0]-sx)*q; z=sz+(h0[1]-sz)*q;
        yaw=Math.atan2(h0[0]-sx,h0[1]-sz);
        want='walk';
      }else if(t<T_SEAL_DONE){
        // 推围栏：位移用与板完全相同的 smoothstep（p→p²(3-2p)），
        // 人的手和板的端头是同一份参数推出来的，不可能错位。
        const p=Math.max(0,Math.min(1,(t-T_SEAL_AT)/(T_SEAL_DONE-T_SEAL_AT)));
        const be=p*p*(3-2*p);
        x=h0[0]+(h1[0]-h0[0])*be; z=h0[1]+(h1[1]-h0[1])*be;
        // 面朝板面（板横跨路面，法向＝路向），侧身把它推入——不是背对板追着它跑。
        const pt=(bd&&bd.pat)?bd.pat:{x:0,z:1};
        yaw=Math.atan2(pt.x,pt.z);
        want='push';
      }else{
        const q=sm((t-T_SEAL_DONE)/Math.max(0.001,T_BACK-T_SEAL_DONE));
        x=h1[0]+(ex-h1[0])*q; z=h1[1]+(ez-h1[1])*q;
        arrived=q>0.985;
        yaw=arrived?Math.atan2(-ST[i][0],-ST[i][1]):Math.atan2(ex-h1[0],ez-h1[1]);
        want=arrived?(i===0?'kneel':'crouch'):'walk';
      }
    }
    // 到位后的微动作：0 号跟着撬棍沉肩、1 号扶管小幅前后；2 号走锥线时不吃这套
    let lift=0,px=0,pz=0;
    if(arrived){
      lift=Math.sin(__evtClock*5+i*2)*0.008;
      if(i===0){lift-=0.004+0.004*pull;px=0.0055*Math.sin(__evtClock*2.6);pz+=0.0038*Math.sin(__evtClock*2.6+1.1);}
      if(i===1){pz+=0.0045*Math.sin(__evtClock*1.7);}
    }
    s.position.set(x+px,0.40+lift,z+pz);
    s.rotation.set(0,yaw,0);
    // 动作片段：走路 / 推围栏 / 到位后按分工（0 跪撬 · 1 半蹲接管 · 2 站定警戒）
    const rg=(window.__crewRigs||[])[i];
    if(rg&&rg.acts&&window.__setCrewClip)window.__setCrewClip(rg,want,0.24);
  });
  // 撬棍：底端插在井盖缝里不动，杆身每帧指向 0 号真正握杆的那只手。
  // 之前是"人摆一个角度、棍子自己来回摆"，两者没有共同约束，特写里手和杆必然错位；
  // 反过来以手为准，杆子就一定被"握着"——这比重算 IK 便宜，也不会穿帮。
  if(c.pry&&c.pryBar){
    const rig0=(window.__crewRigs||[])[0];
    const bones=rig0&&rig0.bones;
    if(bones&&(bones['DEF-handL']||bones['DEF-handR'])){
      const pv=new THREE.Vector3();
      c.pry.getWorldPosition(pv);
      let aim=null,best=1e9;
      ['DEF-handL','DEF-handR'].forEach(function(k){
        if(!bones[k])return;
        const w=bones[k].getWorldPosition(new THREE.Vector3());
        const d=w.distanceTo(pv);
        if(d<best){best=d;aim=w;}
      });
      if(aim&&best>0.02){
        const dir=aim.clone().sub(pv).normalize();
        // 杆长按"手到井口"的距离 ×1.45，夹在 0.11~0.20 世界单位（0.7~1.25m，接近实物撬棍），
        // 手自然落在杆身的 70~80% 处——低于这个比例像插了根牙签，高了手就悬在杆头外
        const len=Math.min(0.20,Math.max(0.11,best*1.45));
        c.pryBar.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),dir);
        c.pryBar.position.copy(dir).multiplyScalar(len*0.5);
        c.pryBar.scale.set(1,len/0.16,1);                    // 几何体原始长度 0.16
      }
    }
  }
  // 3) 警示锥：2 号走到哪个锥位，哪个锥就地"放下去"（从 0 长到 1），不是整排同时弹出
  (c.cones||[]).forEach(function(g,i){
    const at=T_WALK0+0.030+(T_DONE-(T_WALK0+0.030))*(frac2[i]||0);
    popIn(g,at,t,0.012);
  });
  // 作业起点 = 回位时刻 + 跪稳时间。0 号 0.800 到位才开始下场跪，
  // kneel 片段要 1.4 秒才跪稳（__kneelAt），撬棍必须等这 1.4 秒——
  // 早出场会跟着还在下落的手一起飘，"棍子先于人到位"就是穿帮。
  const T_WORK=T_BACK+0.032;   // ≈0.832
  popIn(c.pry, T_WORK,        t,0.016);
  popIn(c.lid, T_WORK+0.033,  t,0.016);
  popIn(c.mud, T_WORK+0.037,  t,0.016);
  popIn(c.pump,T_WORK+0.053,  t,0.020);
  popIn(c.hose,T_WORK+0.065,  t,0.016);
  popIn(c.jet, T_WORK+0.083,  t,0.014);   // ≈0.915，hold 到 0.96 留 1.8 秒"出水"特写
  if(c.jet&&c.jet.visible&&c.jet.children[1]){
    const p=1+0.10*Math.sin(__evtClock*9);c.jet.children[1].scale.set(p,p,1);
  }
  // 5) 现场状态文字：说明"此刻在干什么"
  if(c.status){
    let txt='工程车驶入';
    // 文案跟着动作走，不许出现"人还在推围栏、字已经写接泵疏通"
    [[T_TRUCK0,'工程车驶入'],[T_WALK0,'班组下车'],[T_WALK0+0.020,'布设警示锥 · 推围栏封路'],
     [T_SEAL_DONE,'封路完成 · 开始作业'],[T_WORK+0.053,'接泵疏通'],[T_WORK+0.083,'泵启动 · 抽排中']]
      .forEach(s=>{if(t>=s[0])txt=s[1];});
    c.status.visible=true;
    if(!c.status.userData.cache||c.status.userData.cache!==txt){
      c.status.material.map=makeLabelTex(txt,'#ffe0a6');
      c.status.material.needsUpdate=true;
      c.status.userData.cache=txt;
    }
  }
}// 事件淡入淡出：派单气泡、封路护栏、施工队在"该出现"的时间点附近做透明度过渡，避免瞬间冒出
function fadeInOut(){
  const t=__evtClock/EVT_DURATION;
  const FADE=0.04;   // 淡入窗口（占时间轴比例）
  // 派单气泡：dispatch 从 EVT_JUMPS[2]（部门落位）开始
  if(window.__dispatchBubble){
    const a=EVT_JUMPS[2];
    const f=clamp01((t-a)/FADE);
    const on=t>=a&&t<EVT_JUMPS[6];
    window.__dispatchBubble.visible=on;
    if(on)window.__dispatchBubble.material.opacity=f;
  }
  // 部门信息流按事件过程逐步传递：接警 → 排水接单 → 养护确认。
  (window.__dispatchFlow||[]).forEach((lab,i)=>{
    const at=[EVT_JUMPS[1],EVT_JUMPS[2],EVT_JUMPS[3]][i];
    lab.visible=t>=at;
    if(lab.visible)lab.material.opacity=Math.min(1,Math.max(0,(t-at)/FADE));
  });
  // 施工队（工程车/人员/锥桶/移动泵）的显隐与位移全由 moveCrew() 按时间轴驱动，
  // 这里不再插手，否则两处开关会互相覆盖（fadeInOut 在 tick 里后执行，会把 moveCrew 的结果盖掉）
  // 封路护栏：跟 v.block 同一根开关（而不是另写一个 t>=EVT_JUMPS[5]）。
  // 两处各写各的会出现"车道线已经没了、护栏还没摆上"的空档。
  const flags=window.__flags||(window.__flags={});
  const blkAt=(flags.blockAt===undefined)?-1:flags.blockAt;
  if(flags.block&&blkAt<0)flags.blockAt=t;
  if(!flags.block)flags.blockAt=-1;
  const f5=clamp01((t-Math.max(0,flags.blockAt))/FADE);
  // 封路 = 工人把围栏推入（0.640 走到位 → 0.745 推完）：
  //   围栏在工人走到它旁边那一刻才出现（BLK_SHOW），
  //   位移由 __seal 驱动（见 updateCollab），「封路」牌子在**工人推完**那一刻亮。
  // 原来围栏 0.700 就凭空立在路缘、0.742~0.800 自己飘到路中央，
  // 与任何工人动作都没有关系——观感就是"栏杆自己长出来、自己挪过去"。
  const BLK_SHOW=0.640;
  const sealNow=window.__seal||{w:0,e:0};
  const sealed=(sealNow.w>=0.999&&sealNow.e>=0.999);
  (window.__roadBlocks||[]).forEach(b=>{
    // 牌子：工人推到位就算封了；flags.block（推演数据）作兜底，
    // 万一人物模型没加载、封路支路没跑，围栏也要在该封的时候亮牌。
    const on=sealed||!!flags.block;
    const show=on||t>=BLK_SHOW;
    b.mesh.visible=show;
    if(!show)return;
    b.mesh.children.forEach(child=>{
      if(child.isSprite){child.visible=on;return;}          // 「封路」牌子等真封了才亮
      if(child.isMesh&&child.material){child.material.transparent=true;child.material.opacity=1;}
      if(child.isGroup)child.traverse(function(o){
        if(o.isMesh&&o.material){o.material.transparent=true;o.material.opacity=1;}
      });
    });
  });
}
function clamp01(x){return Math.max(0,Math.min(1,x));}
// 相机：全景机位必须和初始视角一致（原来 CAM_HOME 写的是另一个坐标，
// 一到封路阶段相机就整个跳到别处，像换了个场景）。
// 特写只做一件事：沿同一条视线方向推近到 CAM_CLOSE_R，不做大幅转向。
const CAM_HOME={x:-6,y:15.6,z:20.6}, CAM_HOME_T={x:-18,y:0.6,z:0.6};   // 必须与 init 里的初始视角一致
// 特写距离 2.9 世界单位 ≈ 18m。这个数是被"这一帧要讲清什么"倒推出来的：
// 相机 fov 46°，距离 d 处画面高 ≈ 0.85d、宽 ≈ 1.5d。
// 作业区里必须同框的东西横跨约 20m：抢险车(作业点东 6.75m)、井口/泵/三个人、
// 两端封路围栏(各 9m)。2.2(13.8m) 时画幅只有 21×12m——车出右边界、两端围栏全在画外，
// 观众看到的是"三个人趴在水里"，这就是"看不出在干什么"的直接原因。
// 3.4(21m) 能全框住，但人只剩 9.6%（543px 图上 52px），跪/蹲分不出。
// 2.9(18m) 画幅 27×15m：车和两端围栏都在内，人占 11.4%（约 62px），姿势与道具仍可辨。
const CAM_CLOSE_R=2.9;
// 机位方位（单位向量，相机相对目标点的方向）。**从西南上方斜 45°、俯角 33°**。
// 三个约束卡住这个角度（都是在 4 个候选机位上截图比出来的，不是估的）：
// ① 围栏横跨路面（沿南北延伸），正南正对着看只有端头，画面上是一条线；
//    斜 45° 才看得到板面（1.1m 高 × 17m 宽），才读得出"有一道围栏"。
// ② 俯角必须 ≥30°：25° 时围栏被工程车和路面反光吃掉，只剩一条黄边；
//    33° 时围栏立在车两侧、三道东西（车/人/围栏）在同一画面里分得开。
// ③ 再陡（>38°）就变平面图，人物立不起来、水面反光全丢。
// 东南机位（+x）围栏更清楚，但车会被挤到画面下边缘，所以取西南。
const CAM_CLOSE_DIR={x:-0.45,y:0.545,z:0.710};
// 第三机位「封路中景」：作业特写里看不到护栏，而封路恰恰是"推演结论落到现场"的证据，
// 必须给一个能同时装下护栏与退水路面的机位。7 世界单位 ≈ 44m，画面高约 37m，
// 一段横向护栏（宽 2~3 世界）占画面 6~8%，路面与两端的车道线一起留白，读得出"封了"。
// 不写死坐标：护栏实际落点由 pushStationOffRoads / ROAD_W 算出来，硬编码会在改路宽时错位。
// 收尾机位 7→10.5 世界单位（44→66m）：7 的时候画面里只剩一块黄护栏和一个锥桶，
// 没有路口、没有退水后的路面、没有收工的泵——结尾等于没交代结论。
// 退到 10.5 才装得下"封路 + 退水 + 作业点"三件事。
// 第三个机位「封路特写」：0.63~0.76 摇到西侧围栏旁，专门拍"两人把围栏推进路里"。
// 为什么必须单独给一个镜头：作业中景在 18m 外，1.75m 的人只有 50px，手臂动作 5px——
// "手扶着板推"这个细节在那个尺度下物理上不可见，加标签也读不出来。
// 距离 1.55 世界（9.7m）、俯角 30°：围栏高占画面 14%、人占 22%（715px 图上约 160px），
// 手臂前伸的动作有 23px，"推"这个细节看得见。
// 俯角从 20° 提到 30° 是被穿模逼的：机位在 20°/1.35 时只有 5.7m 高，从作业特写摇过来的
// 那段路上正好从工人头顶擦过，实测 t=0.61 整屏是一个肉色的头（相机钻进模型里）。
// 抬到 30°/1.55 后相机在人头顶上方 3.5m，路径怎么走都不会穿。
const CAM_BLOCK_R=1.55;
const CAM_BLOCK_DIR={x:-0.47,y:0.50,z:0.73};
// 第四机位「收尾中景」：t 过 0.96 后镜头不回全景，而是停在 8.5 世界单位（≈53m）、
// 俯角 25° 的中景上，取景中心是收工后的作业点。回全景（26 世界单位外）三个人只有 15px，
// 结局镜头等于没交代结论。
const CAM_FINAL_R=8.5;
const CAM_FINAL_DIR={x:-0.46,y:0.30,z:0.44};
function blockAnchor(){
  const arr=window.__roadBlocks||[];
  const g=arr[0]&&arr[0].mesh;
  if(!g||!g.children||!g.children.length)return null;
  // 取西侧那块板（0 号推的那块）的**实时位置**：板在动，锚点必须跟着它走，
  // 否则镜头对着空白路面，人推着板从画外滑过去。
  // 旧版返回的是外层 Group 的 position——那个 Group 一直在原点 (0,0,0)，
  // 所以这个机位从来没对上过封路点（等于死代码）。
  const s=g.children[0];
  return {x:s.position.x,y:0.46,z:s.position.z};
}
function driveCamera(t){
  if(!camera||!controls)return;
  if(location.hash==='#top')return;   // 俯视调试视角优先
  // 推近起点跟着「车停稳」走（0.548），到位点卡在「两人扶住围栏、开始推」之前（0.625）：
  // 满特写要覆盖的因果链是 走到围栏边 → 推围栏封路 → 回井口 → 撬盖 → 接管 → 开泵。
  // 原来 a=0.62/b=0.68，镜头到位时推围栏已经进行了一半，观众只看到"栏杆自己在动"，
  // 看不到"人去推它"这一步——这正是用户指出的"封路不是工人封的"。
  const a=EVT_JUMPS[4];        // 0.56：车刹停、人推门，镜头开始推近
  const b=a+0.065;             // 0.625：到作业特写，正好赶在人扶上围栏那一刻
  const hold=0.96;             // 满特写保持到 0.96：覆盖推围栏→撬盖→接管→泵启动→出水
  const release=EVT_JUMPS[6];  // 1.00 回到全景
  let w1=0,w2=0;
  if(t>=a&&t<=b)w1=(t-a)/(b-a);
  else if(t>b&&t<hold)w1=1;
  // 结尾从特写**直接**平滑拉回全景，中间不再经过封路中景。
  // 那一站只扫过 1 秒，价值不大，而且插值路径从作业点（东南）绕到封路点（东北）时
  // 会贴着行道树的树冠擦过去——实测 t=0.90 整屏被一棵绿树糊住。
  // 全景才是真正的结论画面：封路、退水、作业点、泵站四件事只有它装得下。
  else if(t>=hold)w1=1-(t-hold)/Math.max(0.001,release-hold);
  w1=clamp01(w1);
  // ---- 封路特写：0.628~0.762 摇到西侧围栏旁，只拍"两人把围栏推进路里" ----
  // 这不是炫技切镜头：作业中景里 1.75m 的人只有 50px、手臂 5px，
  // "手扶着板推"在这个尺度下**物理上不可见**。要让人看见"封路是工人封的"，
  // 只能给这个动作一个近景。两端各 0.032（≈1.4 秒）过渡，读起来是摇镜不是硬切。
  const SB0=0.628,SB1=0.762,SBF=0.032;
  if(t>SB0&&t<SB1){
    w2=Math.min(1,(t-SB0)/SBF)*Math.min(1,(SB1-t)/SBF);
    w2=Math.max(0,w2);
    w1=w1*(1-w2);          // 让位：这段画面给围栏，井口退到次要位置
  }
  w2=clamp01(w2);
  if(w1+w2>1){const s=w1+w2;w1/=s;w2/=s;}
  const w0=clamp01(1-w1-w2);
  const e1=w1*w1*(3-2*w1);      // 平滑只在各自权重上做
  const e2=w2*w2*(3-2*w2);
  // 对准的是 focus（人+井口+泵，并往水侧偏一点），不是 to（人群走过的终点）：
  // 两者差 0.3 世界单位，但对着 to 时画面下缘正好是路缘，整片积水被切在画面外。
  const to=(window.__roadCrew&&(window.__roadCrew.focus||window.__roadCrew.to))||{x:CAM_HOME_T.x,z:CAM_HOME_T.z};
  const blk=blockAnchor();
  // 封路点拿不到时（护栏还没摆上）退化成作业特写，绝不让镜头跳一下
  const bx=blk?blk.x:to.x, bz=blk?blk.z:to.z, by=blk?blk.y:0.26;
  // ---- 收尾混合：t 过 hold 后，全景项的位置从 CAM_HOME 渐变到「收尾中景」 ----
  // 只改终点不改路径：插值仍在「特写机位 → 终点」这条直线上，不会绕去封路点擦树冠。
  const wf=(t>=hold)?clamp01((t-hold)/Math.max(0.001,release-hold)):0;
  // 取景中心就是作业点，**不是**「作业点↔封路点」的中点：
  // 两点相距 78 世界单位（≈490m），取中点等于对着中间的空地——实测画面里只剩一块护栏和空路，
  // 作业点被挤出画外。聚焦作业点，靠 8.5 的距离和 25° 俯角把附近的护栏一起带进来。
  const finT={x:to.x,y:0.52,z:to.z};
  const fdLen=Math.hypot(CAM_FINAL_DIR.x,CAM_FINAL_DIR.y,CAM_FINAL_DIR.z)||1;
  const fpx=finT.x+CAM_FINAL_DIR.x/fdLen*CAM_FINAL_R;
  const fpy=finT.y+CAM_FINAL_DIR.y/fdLen*CAM_FINAL_R;
  const fpz=finT.z+CAM_FINAL_DIR.z/fdLen*CAM_FINAL_R;
  const HX=CAM_HOME.x+(fpx-CAM_HOME.x)*wf, HY=CAM_HOME.y+(fpy-CAM_HOME.y)*wf, HZ=CAM_HOME.z+(fpz-CAM_HOME.z)*wf;
  const HTX=CAM_HOME_T.x+(finT.x-CAM_HOME_T.x)*wf;
  const HTY=CAM_HOME_T.y+(finT.y-CAM_HOME_T.y)*wf;
  const HTZ=CAM_HOME_T.z+(finT.z-CAM_HOME_T.z)*wf;
  // ---- 跳次取景偏移：1~5 跳各看各的对象，镜头随推演缓慢平移 ----
  // 目标点来自 /api/twin-links 的 entity_ids × /api/scheduler 的台账坐标（见 buildJumpTargets）。
  // 只平移不转向：相机与 target 同步移动，构图不变，变的是"看的是哪一片街区"。
  // 系数 0.55→0.9：各跳目标只相差 3~10 世界单位，取一半后位移不到 4m，
  // 26m 外看等于没动（实测 J0 与 J4 的机位只差 0.7 世界单位）。
  // 权重必须带 (1-wf)：最后一跳的目标是东侧另一个积水点（x≈+44），
  // 收尾时若还全额生效，镜头会被整个拉到东边去，作业点直接出画（实测 cam.x=36）。
  const wj=w0*(1-wf);
  let jx=0,jz=0;
  if(wj>0.02&&window.__jumpTargets){
    const jt=jumpTargetAt(t);
    if(jt){jx=(jt.x-CAM_HOME_T.x)*0.9*wj;jz=(jt.z-CAM_HOME_T.z)*0.9*wj;}
  }
  const tx=HTX*w0+to.x*e1+bx*e2+jx;
  const tz=HTZ*w0+to.z*e1+bz*e2+jz;
  // 特写对准人的胸腹（0.52 世界）。人高 0.28 世界（1.75m）：脚底 0.40、头顶 0.68，
  // 胸口就在 0.58。原来写 0.42，注释自称"对准胸口"，实际只比脚底高 0.02 —— 等于对着脚看，
  // 构图整体下沉，画面下半 55% 全是空水面（验收时量出来的）。
  // 也不宜给到 0.58 顶到胸口：那样脚和井口会被压到画外，看不出"跪在井口边"。
  const ty=HTY*w0+0.52*e1+by*e2;
  const ox=HX-HTX, oy=HY-HTY, oz=HZ-HTZ;
  const len=Math.hypot(ox,oy,oz)||1;
  // 前 5 跳缓慢推近：26 → 19.5 世界单位（162m → 122m）。
  // 机位只平移不推近时，1~5 跳的画面"只是换了个位置"，没有"事情在变严重"的压迫感；
  // 距离单调收紧才读得出推演在往现场走，也让第 5 跳接特写时不算突兀。
  // 乘 (1-wf)：收尾段的距离由 CAM_FINAL_R 单独定，再乘 0.75 会把 8.5 压成 6.5（实测过）。
  const lenScale=1-0.25*clamp01(t/Math.max(0.001,EVT_JUMPS[4]))*(1-wf);
  const d=len*lenScale*w0+CAM_CLOSE_R*e1+CAM_BLOCK_R*e2;
  // 方位也插值：全景看向作业点，特写换到西南上方（避开泵房遮挡），中景从东南上方俯看封路段
  const cdLen=Math.hypot(CAM_CLOSE_DIR.x,CAM_CLOSE_DIR.y,CAM_CLOSE_DIR.z)||1;
  const bdLen=Math.hypot(CAM_BLOCK_DIR.x,CAM_BLOCK_DIR.y,CAM_BLOCK_DIR.z)||1;
  const dx=ox/len*w0+CAM_CLOSE_DIR.x/cdLen*e1+CAM_BLOCK_DIR.x/bdLen*e2;
  const dy=oy/len*w0+CAM_CLOSE_DIR.y/cdLen*e1+CAM_BLOCK_DIR.y/bdLen*e2;
  const dz=oz/len*w0+CAM_CLOSE_DIR.z/cdLen*e1+CAM_BLOCK_DIR.z/bdLen*e2;
  const dl=Math.hypot(dx,dy,dz)||1;
  camera.position.set(tx+dx/dl*d,ty+dy/dl*d,tz+dz/dl*d);
  // 推近过程中走一条略微抬起的弧线（中段 +0.45 世界 ≈ 2.8m，两端自然归零）：
  // 全景→特写是直线插值，中段正好从南侧人行道上空、2 号工人的走位线掠过，
  // 实测有一帧相机撞进人物模型里，整屏是一颗肉色的头。抬弧避开这段，不影响两端机位。
  const liftPath=Math.sin(Math.PI*clamp01(w1))*(1-w2)*0.45;
  camera.position.y+=liftPath;
  controls.target.set(tx,ty,tz);
  controls.update();
}
// 低多边形施工队员：橙色反光背心 + 头盔，用几何体拼（KayKit 无人物模型）
// 尺寸放大到约 1.8m 高（远景可见），躯干/四肢按真人比例
function makeCrewMember(){
  const g=new THREE.Group();
  const vest=new THREE.MeshStandardMaterial({color:0xff7b2d,roughness:.6});
  const pants=new THREE.MeshStandardMaterial({color:0x2f3f4f,roughness:.8});
  const skin=new THREE.MeshStandardMaterial({color:0xd9a57c,roughness:.7});
  const helmet=new THREE.MeshStandardMaterial({color:0xffc21a,roughness:.5});
  // 躯干（橙色反光背心）
  const body=new THREE.Mesh(new THREE.BoxGeometry(0.55,0.75,0.32),vest);body.position.y=0.92;g.add(body);
  // 反光条（亮银，突出"反光背心"）
  const strip=new THREE.Mesh(new THREE.BoxGeometry(0.58,0.16,0.34),new THREE.MeshStandardMaterial({color:0xe8f0f8,roughness:.2,metalness:.4,emissive:0x334455,emissiveIntensity:.5}));
  strip.position.y=0.98;g.add(strip);
  const strip2=new THREE.Mesh(new THREE.BoxGeometry(0.58,0.16,0.34),new THREE.MeshStandardMaterial({color:0xe8f0f8,roughness:.2,metalness:.4,emissive:0x334455,emissiveIntensity:.5}));
  strip2.position.y=0.82;g.add(strip2);
  // 头 + 安全头盔
  const head=new THREE.Mesh(new THREE.SphereGeometry(0.24,12,10),skin);head.position.y=1.62;g.add(head);
  const hm=new THREE.Mesh(new THREE.SphereGeometry(0.28,12,8,0,Math.PI*2,0,Math.PI/2),helmet);hm.position.y=1.66;g.add(hm);
  // 双腿
  const legL=new THREE.Mesh(new THREE.BoxGeometry(0.18,0.65,0.2),pants);legL.position.set(-0.14,0.32,0);g.add(legL);
  const legR=new THREE.Mesh(new THREE.BoxGeometry(0.18,0.65,0.2),pants);legR.position.set(0.14,0.32,0);g.add(legR);
  // 双臂（橙色袖）
  const armL=new THREE.Mesh(new THREE.BoxGeometry(0.16,0.55,0.18),vest);armL.position.set(-0.4,1.0,0);armL.rotation.z=0.1;g.add(armL);
  const armR=new THREE.Mesh(new THREE.BoxGeometry(0.16,0.55,0.18),vest);armR.position.set(0.4,1.0,0);armR.rotation.z=-0.1;g.add(armR);
  g.traverse(o=>{if(o.isMesh)o.castShadow=true;});
  return g;
}
const raycaster=new THREE.Raycaster(),pointer=new THREE.Vector2();
// 点击与按钮绑定必须等 renderer 建好再挂：顶层直接取 renderer.domElement
// 会在 init() 之前抛 TypeError，把后面的按钮绑定和 init() 一起带走——
// 表现就是页面全黑且毫无提示（真浏览器 console 才看得到）。
function bindUI(){
  // 用户一旦自己拖/滚了视角，就让出运镜权（tick 里据此跳过 driveCamera），
  // 否则自动运镜每帧把 target 抢回去，鼠标刚拖动就被拉回原位。
  // 点播放/跳次/拖进度条会把它清掉，重新交回系统取景。
  // 这两行必须在 bindUI 里：顶层 init 阶段直接取 renderer.domElement 会抛错并
  // 连累整个 init（历史回归，见 test_twin_page_binds_click_after_renderer_exists）。
  renderer.domElement.addEventListener('pointerdown',function(){window.__camUser=true;});
  renderer.domElement.addEventListener('wheel',function(){window.__camUser=true;},{passive:true});
  renderer.domElement.addEventListener('click',ev=>{
    pointer.x=(ev.clientX/innerWidth)*2-1;pointer.y=-(ev.clientY/innerHeight)*2+1;
    raycaster.setFromCamera(pointer,camera);
    const hits=raycaster.intersectObjects(scene3.children,true);
    // 命中的可能是楼体 Group 内的子 mesh（主体/裙楼/腰线），entityId 挂在 Group 上，
    // 需要沿 parent 链向上找带 entityId 的对象
    let hit=null,id=null,kind=null;
    for(const h of hits){let o=h.object;while(o){if(o.userData&&o.userData.entityId){hit=h;id=o.userData.entityId;kind=o.userData.kind;break;}o=o.parent;}if(id)break;}
    if(!id)return;
    twinState.selectedEntity=id;
    paintHighlights();   // 选中就把光环挪到该对象上（只保留一个"你在这儿"的环）
    // 管段是内嵌台账对象（不在 twin-links 实体投影里），走独立详情；其余走统一实体卡
    if(kind==='pipe'){renderPipeCard(id);}else{renderEntityCard(twinState.selectedEntity);}
  });
  document.getElementById('prev').onclick=()=>setJump(twinState.currentJump-1);
  document.getElementById('next').onclick=()=>setJump(twinState.currentJump+1);
  // 重播 = 从降雨前奏（t=0）开始播，而不是直接跳到第 1 跳：
  // 用户要看到的是"先下雨、再积水"，重播如果不给前奏就等于又跳过了起因。
  document.getElementById('restart').onclick=()=>{
    twinState.currentJump=0;__evtClock=0;__evtPlaying=true;window.__camUser=false;
    applyTimeline(0);paintHighlights();renderJumpControls();updateEvidence();renderResults();
    if(twinState.selectedEntity)renderEntityCard(twinState.selectedEntity);
    renderEventBar(0);syncProgress();
  };
  // 管网图层开关：切换地下排水管段显隐
  document.getElementById('pipe-toggle').onclick=()=>{
    const on=!window.__pipesVisible;
    window.__pipesVisible=on;
    (window.__pipeSegments||[]).forEach(s=>{s.mesh.visible=on;});
    const btn=document.getElementById('pipe-toggle');
    btn.textContent='管网：'+(on?'开':'关');
    btn.classList.toggle('off',!on);
  };
  window.__pipesVisible=false;   // 管网默认关闭：地下管段是 X 光透视层，常开会穿透建筑、显得满屏管线
  document.getElementById('settle').onclick=()=>setJump(6);
  document.getElementById('play').onclick=()=>{
    if(__evtClock>=EVT_DURATION)__evtClock=0;   // 播到末尾再点播放 = 从头重播
    __evtPlaying=!__evtPlaying;window.__camUser=false;renderJumpControls();
  };
  // 进度条拖动 = seek 到任意时刻
  const prog=document.getElementById('progress');
  prog.addEventListener('input',()=>{
    __evtPlaying=false;
    __evtClock=Number(prog.value)/1000*EVT_DURATION;
    window.__camUser=false;              // 拖进度条 = 要系统取景，不是手动视角
    applyTimeline(__evtClock/EVT_DURATION);
    syncJumpFromClock();
  });
}
init();
</script></html>""".replace("__CHAR_VER__", _char_ver())


def _char_ver() -> str:
    """人物模型的 mtime 戳，用来打破浏览器缓存。

    改完 glb 直接刷新，浏览器仍可能拿旧文件，表现是"改了没生效"——
    这一版正是踩过一次才加上的。
    """
    p = Path(__file__).resolve().parents[2] / "assets" / "models" / "worker_char.glb"
    try:
        return str(int(p.stat().st_mtime))
    except OSError:
        return "0"

def _feedback_history_path(create: bool = False) -> Path:
    """反馈流水落盘位置。放 artifacts/ 下：属运行时产物，已被 .gitignore 排除。

    `create=False` 时不建目录——`route()` 被当作纯函数用，读操作不该有副作用。
    """
    directory = Path(__file__).resolve().parents[2] / "artifacts" / "feedback"
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory / "history.jsonl"


def _append_feedback_run(record: dict) -> None:
    """追加一条流水。用单行 JSONL：便于 tail、便于按行 diff，不受半截写入破坏。"""
    path = _feedback_history_path(create=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_feedback_runs(limit: int = 20) -> list[dict]:
    """读最近 limit 条流水（新→旧）。文件不存在返回空表，不报错。"""
    path = _feedback_history_path()
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # 半截行直接跳过，不让一条坏记录拖垮整页
    return list(reversed(rows[-limit:]))


def build_feedback_html() -> str:
    """反馈注入页：人工把现场/仿真结果填进来，后端跑一遍闭环。

    为什么不给 Agent 一个 MCP 工具让它自己提交：那等于让模型自己注入反馈、自己触发策略调整，
    与"反馈必须手动注入"的约束直接冲突。人填写、机器算、门禁判，三层各就各位。
    """
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>反馈注入 · 城市内涝决策进化</title>
<style>
body{margin:0;background:#0b1622;color:#dce8f2;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:860px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 6px}h1 span{color:#5eead4}
.sub{color:#89a2b8;margin-bottom:22px}
form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 16px;
     border:1px solid #1e3348;border-radius:12px;padding:18px;background:#0f1e2e}
label{display:flex;flex-direction:column;gap:5px;font-size:12.5px;color:#9db4c8}
input,select{background:#0a1622;border:1px solid #24384e;border-radius:7px;color:#e6f1fa;padding:7px 9px;font:inherit}
.full{grid-column:1/-1}
button{grid-column:1/-1;justify-self:start;background:#1d7f6f;border:0;color:#eafff9;padding:9px 20px;
       border-radius:8px;font:inherit;cursor:pointer}button:hover{background:#229584}
#out{margin-top:20px;white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;
     background:#08131d;border:1px solid #1e3348;border-radius:10px;padding:14px;min-height:40px;color:#b7cadb}
.k{color:#7dd3fc}.ok{color:#34d399}.warn{color:#fbbf24}
a{color:#5eead4}
</style></head><body><main>
<h1>反馈注入 · <span>决策进化闭环</span></h1>
<div class="sub">填一次内涝结果 → 判定处置是否有效 → 若有效性问题成立，跑一轮经 12 例门禁的策略演化。
12 例评测集只读不动；演进结果写入运行期记忆（<code>artifacts/memory/</code>），与冻结的 <code>samples/eval/decision_memory.json</code> 无关。
专家评审取 <code>samples/eval/decision_reviews.json</code> 的 REV-001（角色扮演，非真实部门批复）——评审偏差 &gt; 0 会把整轮候选全部拒掉。<a href="/">← 回推演页</a> · <a href="/evolution">进化对比 →</a></div>
<form id="f">
  <label>场景 <input name="scenario_id" value="vd02"></label>
  <label>分钟 <input name="t_min" type="number" value="45" min="0"></label>
  <label>片区 <input name="district" value="A-03"></label>
  <label>实测水深 cm <input name="water_depth_cm" type="number" step="0.1" value="34"></label>
  <label>受淹道路（逗号分隔）<input name="flooded_road_ids" value="RD-A03-02,RD-A03-05"></label>
  <label>泵站状态
    <select name="pump_state">
      <option value="running">running</option><option value="idle">idle</option>
      <option value="fault">fault</option><option value="unknown">unknown</option>
    </select></label>
  <label>来源
    <select name="source">
      <option value="synthetic">synthetic（仿真）</option><option value="assumption">assumption（假设）</option>
      <option value="derived">derived（推算）</option><option value="real">real（实测）</option>
    </select></label>
  <label>预期水深 cm（可空）<input name="expected_depth_cm" type="number" step="0.1"></label>
  <label class="full">备注 <input name="note" placeholder="例如：封路后仍超临界，泵站单台运行"></label>
  <button type="submit">提交反馈并跑闭环</button>
</form>
<div id="out">等待提交…</div>
<h2 style="font-size:16px;margin:26px 0 8px">反馈流水 <span style="color:#7d95ab;font-weight:400;font-size:12.5px">（最近 30 条，倒序）</span></h2>
<div id="hist" style="color:#7d95ab;font-size:12.5px">加载中…</div>
</main><script>
const f=document.getElementById('f'),out=document.getElementById('out'),hist=document.getElementById('hist');
const NL=String.fromCharCode(10);
const esc=s=>String(s==null?'':s).replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
function cell(v,cls){return '<td'+(cls?' class="'+cls+'"':'')+'>'+esc(v)+'</td>';}
async function loadHist(){
  try{
    const r=await fetch('/feedback/history');
    const d=await r.json();
    const rows=(d.runs||[]);
    if(!rows.length){hist.textContent='还没有反馈记录。提交一次就会出现。';return;}
    let html='<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:12.5px"><thead><tr>'
      +['时间','片区·水深','判定','维度','触发','最终标签','降幅 训练·留出','记忆','条目','评审偏差']
        .map(h=>'<th style="text-align:left;padding:6px 8px;border-bottom:1px solid #24384e;color:#9db4c8;font-weight:500;white-space:nowrap">'+h+'</th>').join('')
      +'</tr></thead><tbody>';
    for(const x of rows){
      const rv=(x.review_before==null&&x.review_after==null)?'—'
        :(Number(x.review_before||0).toFixed(4)+' → '+Number(x.review_after||0).toFixed(4));
      const drop=(x.train_drop==null)?'—'
        :(Number(x.train_drop).toFixed(4)+' · '+Number(x.holdout_drop||0).toFixed(4));
      html+='<tr style="white-space:nowrap">'
        +cell(x.ts?new Date(x.ts).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'')
        +cell(x.district+' · '+x.depth_cm+'cm')+cell(x.outcome)
        +cell(x.dimension||'—')
        +cell(x.triggered?'是':'否',x.triggered?'ok':'')
        +cell(x.final_label||'—')
        +cell(drop)
        +cell(x.triggered?(x.memory_hit?('命中 '+(x.memory_label||'')):'从 v1.0 起步'):'—',x.memory_hit?'ok':'')
        +cell(x.memory_entries==null?'—':x.memory_entries)
        +cell(rv)
        +'</tr>';
    }
    hist.innerHTML=html+'</tbody></table></div>';
  }catch(err){hist.textContent='流水加载失败：'+err;}
}
loadHist();
f.addEventListener('submit',async ev=>{
  ev.preventDefault();
  const fd=new FormData(f),p={};
  for(const [k,v] of fd.entries()){
    if(v===''){continue;}
    if(k==='t_min'||k==='seed'){p[k]=parseInt(v,10);}
    else if(k==='water_depth_cm'||k==='expected_depth_cm'){p[k]=parseFloat(v);}
    else if(k==='flooded_road_ids'){p[k]=String(v).split(',').map(s=>s.trim()).filter(Boolean);}
    else {p[k]=v;}
  }
  out.textContent='计算中（演化会跑一遍 12 例门禁，约 1-2 秒）…';
  try{
    const r=await fetch('/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});
    const d=await r.json();
    let tip='';
    if(d.ledger&&d.ledger.state==='stale'){tip=NL+NL+'[台账待同步] '+d.ledger.hint;}
    else if(d.ledger&&d.ledger.state==='fresh'){tip=NL+NL+'[台账已同步] '+d.ledger.hint;}
    else if(d.ledger&&d.ledger.hint){tip=NL+NL+'[台账状态未知] '+d.ledger.hint;}
    out.textContent=JSON.stringify(d,null,2)+tip;
    loadHist();
  }catch(err){out.textContent='请求失败：'+err;}
});
</script></body></html>"""


_REVIEW_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>方案评审 · 城市内涝决策进化</title>
<style>
body{margin:0;background:#0b1622;color:#dce8f2;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:900px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 6px}h1 span{color:#5eead4}
h2{font-size:16px;margin:26px 0 8px}
.sub{color:#89a2b8;margin-bottom:20px}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:20px}
.stat{border:1px solid #1e3348;border-radius:10px;padding:10px 14px;background:#0f1e2e;min-width:112px}
.stat small{display:block;color:#8aa3b8;font-size:11.5px}
.stat b{font-size:19px;color:#e6f1fa;font-weight:600}
.stat.ok b{color:#34d399}.stat.warn b{color:#fbbf24}
form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 16px;
     border:1px solid #1e3348;border-radius:12px;padding:18px;background:#0f1e2e}
label{display:flex;flex-direction:column;gap:5px;font-size:12.5px;color:#9db4c8}
input,select,textarea{background:#0a1622;border:1px solid #24384e;border-radius:7px;color:#e6f1fa;padding:7px 9px;font:inherit}
.full{grid-column:1/-1}
button{grid-column:1/-1;justify-self:start;background:#1d7f6f;border:0;color:#eafff9;padding:9px 20px;
       border-radius:8px;font:inherit;cursor:pointer}button:hover{background:#229584}
#out{margin-top:18px;white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;
     background:#08131d;border:1px solid #1e3348;border-radius:10px;padding:14px;min-height:40px;color:#b7cadb}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;padding:6px 8px;border-bottom:1px solid #24384e;color:#9db4c8;font-weight:500;white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid #16273a;vertical-align:top}
.muted{color:#7d95ab;font-size:12.5px}
a{color:#5eead4}
</style></head><body><main>
<h1>方案评审 · <span>人工评分与修正建议</span></h1>
<div class="sub">四维评分只做样本记录，不进决策门禁；只有填了「建议调拨」的提交才会转成评审用例，
和 12 例一起送进候选评估。评分口径：__NOTE__。
<a href="/">← 回推演页</a> · <a href="/feedback">反馈注入 →</a> · <a href="/evolution">进化对比 →</a></div>
<div class="stats">__STATS__</div>
<form id="f">
  <label>评审人 <input name="reviewer_id" placeholder="如 zhang.water" required></label>
  <label>角色 <input name="reviewer_role" placeholder="如 水务调度负责人（角色扮演）"></label>
  <label>阶段
    <select name="phase">
      <option value="before">before（看方案前）</option>
      <option value="after">after（看方案后）</option>
    </select></label>
  <label>建议调拨 <input name="suggested_allocation" placeholder="A-03=5,B-01=1（留空则不提候选）"></label>
  <label>结论准确性 1-5 <input name="accuracy" type="number" min="1" max="5" step="1" value="4"></label>
  <label>时效性 1-5 <input name="timeliness" type="number" min="1" max="5" step="1" value="4"></label>
  <label>安全性/风险提示 1-5 <input name="safety" type="number" min="1" max="5" step="1" value="4"></label>
  <label>可解释性/可溯源 1-5 <input name="explainability" type="number" min="1" max="5" step="1" value="4"></label>
  <label class="full">备注 <textarea name="comment" rows="3" placeholder="写清为什么这样评；要提修正意见就填上面的建议调拨"></textarea></label>
  <button type="submit">提交评审</button>
</form>
<div id="out">等待提交…</div>
<h2>最近提交 <span class="muted">（最新 8 条）</span></h2>
__RECENT__
</main><script>
const f=document.getElementById('f'),out=document.getElementById('out');
const DIMS=['accuracy','timeliness','safety','explainability'];
f.addEventListener('submit',async ev=>{
  ev.preventDefault();
  const fd=new FormData(f),p={scores:{}};
  for(const [k,v] of fd.entries()){
    if(v===''){continue;}
    if(DIMS.indexOf(k)>=0){p.scores[k]=parseInt(v,10);}
    else{p[k]=v;}
  }
  out.textContent='提交中…';
  try{
    const r=await fetch('/api/reviews',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});
    const d=await r.json();
    out.textContent=JSON.stringify(d,null,2);
    setTimeout(()=>location.reload(),1800);
  }catch(err){out.textContent='请求失败：'+err;}
});
</script></body></html>"""


def build_review_html() -> str:
    """评审页：人工四维评分 + 可选的修正调拨。

    边界和 `review_collect.summarize()` 保持一致——评分本身只回答"样本多少、前后差多少"，
    **不进决策门禁**；只有带 `suggested_allocation` 的提交才被 `to_review_records()`
    转成评审用例，和 12 例一起参与候选评估。页面不重复解释口径，只在提交区提示一句。
    """
    from html import escape as _esc

    submissions = reviews.load_submissions()
    summary = reviews.summarize(submissions)

    def card(label: str, value: str, cls: str = "") -> str:
        return f'<div class="stat {cls}"><small>{_esc(label)}</small><b>{_esc(value)}</b></div>'

    before = summary["phases"]["before"]["overall"]
    after = summary["phases"]["after"]["overall"]
    delta = summary["delta"]
    reviewers = summary["reviewer_count"]
    stats = [
        card("样本数", str(summary["total"])),
        card("评审人", f"{reviewers}/{reviews.MIN_REVIEWERS}", "ok" if reviewers >= reviews.MIN_REVIEWERS else "warn"),
        card("before 均分", "—" if before is None else f"{before:.2f}"),
        card("after 均分", "—" if after is None else f"{after:.2f}"),
        card("前后差", "—" if delta is None else f"{delta:+.2f}"),
        card("门禁候选", str(summary["gate_candidates"])),
    ]

    rows: list[str] = []
    for item in list(submissions)[-8:][::-1]:
        allot = "，".join(f"{k}={v}" for k, v in item.suggested_allocation.items()) or "—"
        rows.append(
            "<tr>"
            f"<td>{_esc(item.created_at[:16].replace('T', ' '))}</td>"
            f'<td>{_esc(item.reviewer_id)}<br><span class="muted">{_esc(item.reviewer_role)}</span></td>'
            f"<td>{_esc(item.phase)}</td>"
            f"<td>{item.overall:.2f}</td>"
            f"<td>{item.scores.accuracy}/{item.scores.timeliness}/{item.scores.safety}/{item.scores.explainability}</td>"
            f"<td>{_esc(allot)}</td>"
            f"<td>{_esc(item.comment or '—')}</td>"
            "</tr>"
        )
    head = "".join(
        f"<th>{h}</th>" for h in ("时间", "评审人", "阶段", "总分", "准/时/安/释", "建议调拨", "备注")
    )
    body = "".join(rows) if rows else '<tr><td colspan="7" class="muted">还没有提交记录。</td></tr>'
    recent = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    return (
        _REVIEW_PAGE.replace("__STATS__", "".join(stats))
        .replace("__RECENT__", recent)
        .replace("__NOTE__", _esc(summary["note"]))
    )


def platform_versions_payload() -> dict[str, Any]:
    """平台 Agent 版本快照（只读）。判据与命令行共用 `selfcheck`。"""
    return selfcheck.platform_versions_payload()


def ledger_sync_payload() -> dict[str, Any]:
    """知识库台账同步状态（只读）。判据与命令行共用 `selfcheck`。"""
    return selfcheck.ledger_sync_payload()


def _ledger_freshness() -> dict[str, Any]:
    """本地最新反馈是否已进平台台账（只读，判据见 `selfcheck`）。"""
    return selfcheck.ledger_freshness()


_STATUS_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>系统自检 · 城市内涝决策进化</title>
<style>
body{margin:0;background:#0b1622;color:#dce8f2;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:880px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 6px}h1 span{color:#5eead4}
h2{font-size:16px;margin:26px 0 8px}
.sub{color:#89a2b8;margin-bottom:18px}
.verdict{border:1px solid #1e3348;border-radius:10px;padding:12px 16px;background:#0f1e2e;margin-bottom:18px;font-size:14px}
.verdict b{font-size:17px;margin-right:8px}
.ok{color:#34d399}.warn{color:#fbbf24}.bad{color:#f87171}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:7px 9px;border-bottom:1px solid #24384e;color:#9db4c8;font-weight:500;white-space:nowrap}
td{padding:7px 9px;border-bottom:1px solid #16273a;vertical-align:top}
td.name{white-space:nowrap;color:#cfe0ee}
td.detail{color:#9db4c8}
.pill{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11.5px;border:1px solid currentColor}
code{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#7dd3fc}
a{color:#5eead4}
.hint{margin-top:20px;color:#7d95ab;font-size:12.5px;line-height:1.9}
</style></head><body><main>
<h1>系统自检 · <span>谁在跑、跑的是哪版、对不对得上</span></h1>
<div class="sub">每项都是只读检查，不写任何状态。参数逐项比对与全量测试在命令行完成
（<code>scripts/policy_sensitivity.py</code>、<code>pytest</code>），这页只回答"当前这四类事实是否自洽"。
<a href="/">← 回推演页</a> · <a href="/feedback">反馈注入 →</a> · <a href="/review">方案评审 →</a> · <a href="/evolution">进化对比 →</a> · <a href="/workbench">证据工作台 →</a></div>
<div class="verdict">__VERDICT__</div>
<table><thead><tr><th>检查项</th><th>状态</th><th>详情</th></tr></thead><tbody>__ROWS__</tbody></table>
<div class="hint">检查时间：__WHEN__<br>
发现告警时的处理入口：台账落后 → <code>deploy/nexent/sync_policy_ledger.py --apply</code>；
平台快照缺失 → <code>deploy/nexent/snapshot_platform_versions.py</code>；
同步状态未知 → <code>deploy/nexent/check_ledger_sync.py</code>。</div>
</main></body></html>"""



def status_checks() -> list[dict[str, Any]]:
    """系统自洽检查。判据在 `selfcheck`，页面与命令行共用同一份。"""
    return selfcheck.status_checks()


def build_status_html() -> str:
    from html import escape as _esc

    checks = status_checks()
    mood = {"ok": "ok", "warn": "warn", "bad": "bad"}
    labels = {"ok": "通过", "warn": "待注意", "bad": "不通过"}
    ok_count = sum(1 for item in checks if item["state"] == "ok")
    problem = [item for item in checks if item["state"] != "ok"]
    if problem:
        verdict = (
            f'<b class="{"bad" if any(i["state"] == "bad" for i in problem) else "warn"}">'
            f'{len(problem)} 项需注意</b>'
            f"共 {len(checks)} 项检查，{ok_count} 项通过；下面标黄/红的条目就是该处理的。"
        )
    else:
        verdict = f'<b class="ok">全部通过</b>共 {len(checks)} 项检查，四类事实自洽。'

    rows = "".join(
        f'<tr><td class="name">{_esc(item["name"])}</td>'
        f'<td><span class="pill {mood[item["state"]]}">{labels[item["state"]]}</span></td>'
        f'<td class="detail">{_esc(item["detail"])}</td></tr>'
        for item in checks
    )
    from datetime import datetime, timezone

    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return (
        _STATUS_PAGE.replace("__VERDICT__", verdict)
        .replace("__ROWS__", rows)
        .replace("__WHEN__", _esc(when))
    )


# --- 证据工作台（对话 + 核查） ------------------------------------------------
#
# 为什么不是"再做一个聊天页面"：平台自带的对话界面已经能问，缺的是**问完之后能不能核**——
# 结论出自哪份文件的哪一行、这次到底调了哪些工具、系统自身是否自洽。所以这里只做
# 左对话 + 右核查，「证据 / 工具轨迹 / 自检」三块都是现成的数据，拼到一屏即可。
#
# 刻意不做：文件树与编辑器（业务方不改文件）、独立的聊天壳（会和平台对话界面分裂）。

WORKBENCH_AGENTS = {
    # 键是前端下拉值，值是平台上的 Agent 名。
    # 默认推演：工作台这一屏本来就是某个事件，用户问的多半是"谁负责/先做什么"。
    # 推演 Agent 是两步式协议，所以 `workbench_chat` 会把自由提问包成协议问法
    # （`_deduction_query`）——不包的话问一句"这个事件谁负责"整轮拿不到结构化结论，
    # 那才是早先把默认设成台账问答的真正原因。
    "deduction": "urban_deduction_agent",
    "asset_qa": "urban_asset_qa",
}

#: 工作台取证时假定的调用方身份（应急指挥）。
#: 只是**一个身份**，不是越权开关：受限依据（部门规程的可见性标签里写着 ORG-GAS /
#: ORG-DRAIN / ORG-EMERG）若不包含它，取证仍然会被 PERMISSION_DENIED 挡住，
#: 页面会如实说"权限受限"，不会把它写成"引用造假"。
WORKBENCH_CALLER = "ORG-EMERG"

#: 问题里出现事件编号就以它为准，否则用默认事件——现场换事件不必改代码。
_EVENT_PATTERN = re.compile(r"WO-\d{8}-\d{3}")

#: evidence_ref 的形状：`AS-<id>#<locator>#(line|row|region):<n>`。
#: 用固定前缀 + 两段 locator，避免把正文里普通的 `#` 也当成引用。
_REF_PATTERN = re.compile(r"AS-[A-Za-z0-9_\-]+#[A-Za-z0-9_\-/\.]+#(?:line|row|region):[0-9\-]+")

#: 台账问答（平台知识库）给的引用形如「（出处：underpasses.csv · UP-XH-101）」，
#: 它不是 MCP 的 asset_id，取不到原文；但可以拿它当检索词反查切片。
_CITATION_PATTERN = re.compile(r"（出处：([^）]{2,120})）")

#: 工作台页面。左半边**直接嵌平台的对话页**（`/zh/workbench`），不重写聊天；
#: 右半边是本项目自己的核查面板：证据原文 / 工具轨迹 / 自检。
#: 平台页面没有 X-Frame-Options，可以内嵌；嵌入后是跨源 iframe，不能脚本读内容，
#: 所以"点结论跳到原文"只在下面的「站内对话」模式里生效——两种模式各司其职。
_WORKBENCH_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>证据工作台 · 城市内涝决策</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f5f6f8;color:#1f2328;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{display:flex;align-items:baseline;gap:12px;padding:14px 20px;background:#fff;border-bottom:1px solid #e5e7eb}
h1{font-size:17px;margin:0;font-weight:600}
.tag{font-size:12px;color:#6b7280}
.spacer{flex:1}
select,button,input,textarea{font:inherit;color:inherit}
select{background:#fff;border:1px solid #d8dbe0;border-radius:6px;padding:4px 8px;font-size:12.5px}
.link{color:#0f766e;text-decoration:none;font-size:12.5px;border-bottom:1px dashed #9fd3cb}
main{display:grid;grid-template-columns:minmax(380px,48%) 1fr;gap:14px;padding:14px 18px 20px;height:calc(100vh - 57px)}
.panel{background:#fff;border:1px solid #e5e7eb;border-radius:10px;display:flex;flex-direction:column;min-height:0}
.panel>h2{font-size:12px;font-weight:600;color:#6b7280;letter-spacing:.4px;margin:0;padding:9px 14px;border-bottom:1px solid #eef0f3}
.switch{display:flex;align-items:center;gap:4px;padding:8px 12px 0;border-bottom:1px solid #eef0f3}
.switch select{margin-bottom:6px}
.sw{background:none;border:0;padding:6px 10px;font-size:13px;color:#6b7280;cursor:pointer;border-bottom:2px solid transparent}
.sw.on{color:#0f766e;border-bottom-color:#0f766e}
#frame{flex:1;border:0;width:100%;min-height:0;background:#fff;border-radius:0 0 10px 10px}
#chatpane{flex:1;display:none;flex-direction:column;min-height:0}
.hint{padding:8px 14px 11px;font-size:12px;line-height:1.6;color:#9aa1aa;border-top:1px solid #eef0f3}
#feed{flex:1;overflow:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:93%;font-size:13.5px;line-height:1.65;white-space:pre-wrap;word-break:break-word}
.me{align-self:flex-end;background:#e8f4f1;border:1px solid #cde7e1;border-radius:10px 10px 2px 10px;padding:8px 11px}
.ai{align-self:flex-start;background:#fbfbfc;border:1px solid #eceef1;border-radius:10px 10px 10px 2px;padding:9px 11px}
.meta{align-self:flex-start;font-size:11.5px;color:#8a919b}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{font-family:ui-monospace,Consolas,monospace;font-size:11px;background:#f3f4f6;border:1px solid #e1e4e8;border-radius:5px;padding:2px 6px;cursor:pointer;color:#0f766e}
.chip:hover{background:#e8f4f1}
form{display:flex;gap:8px;padding:10px 12px;border-top:1px solid #eef0f3}
textarea{flex:1;resize:none;height:42px;border:1px solid #d8dbe0;border-radius:8px;padding:9px 10px;font-size:13px}
textarea:focus{outline:2px solid #cde7e1}
.send{background:#0f766e;color:#fff;border:0;border-radius:8px;padding:0 16px;cursor:pointer;font-size:13px}
.send:disabled{opacity:.5;cursor:default}
.tabs{display:flex;gap:4px;padding:8px 12px 0;border-bottom:1px solid #eef0f3}
.tab{background:none;border:0;padding:6px 10px;font-size:13px;color:#6b7280;cursor:pointer;border-bottom:2px solid transparent}
.tab.on{color:#0f766e;border-bottom-color:#0f766e}
.body{flex:1;overflow:auto;padding:14px}
.empty{color:#9aa1aa;font-size:13px}
.kv{display:grid;grid-template-columns:80px 1fr;gap:5px 10px;font-size:12.5px;margin:0 0 12px}
.kv dt{color:#8a919b}
.kv dd{margin:0;word-break:break-all}
pre.excerpt{margin:0;background:#fbfbfc;border:1px solid #eceef1;border-radius:8px;padding:12px;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;line-height:1.7;white-space:pre-wrap;word-break:break-word}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:#8a919b;font-weight:500;padding:5px 8px;border-bottom:1px solid #eceef1;white-space:nowrap}
td{padding:5px 8px;border-bottom:1px solid #f3f4f6;vertical-align:top}
.ok{color:#0f766e}.warn{color:#b45309}.bad{color:#b91c1c}
.mono{font-family:ui-monospace,Consolas,monospace}
.row{padding:7px 0;border-bottom:1px solid #f3f4f6;font-size:13px}
@media(max-width:900px){main{grid-template-columns:1fr;height:auto}#feed{max-height:42vh}#frame{height:60vh}}
</style></head><body>
<header>
  <h1>证据工作台</h1>
  <span class="tag">左边问、右边核：每条结论都能落到原文某一行</span>
  <span class="spacer"></span>
  <a class="link" href="__WEB_BASE__/zh/workbench" target="_blank" rel="noopener">新窗口打开平台对话 ↗</a>
  <span class="tag" id="stat">就绪</span>
</header>
<main>
  <section class="panel">
    <div class="switch">
      <button class="sw" data-mode="platform">平台对话</button>
      <button class="sw on" data-mode="local">站内对话（可点引用）</button>
      <span class="spacer"></span>
      <select id="agentSel" title="站内对话使用哪个 Agent"><option value="deduction">推演（事件事实）</option><option value="asset_qa">台账问答</option></select>
    </div>
    <iframe id="frame" data-src="__WEB_BASE__/zh/workbench" title="平台对话" style="display:none"></iframe>
    <div class="hint">左边默认是「站内对话」——它不经过平台前端，页面打开就能问，回答里带定位的结论可以直接点开原文。
    「平台对话」直接嵌入平台原生对话页，需要平台已启动且当前浏览器已登录；一直转圈就用
    <a class="link" href="__WEB_BASE__/zh/workbench" target="_blank" rel="noopener">新窗口打开</a>。
    内嵌页的内容本页面读不到，所以「点结论跳到原文」只在站内对话下生效。</div>
    <div id="chatpane">
      <div id="feed"><div class="msg ai">直接用一句话问就行：「谁负责」「先做什么」「依据哪条」。推演模式下，问题会被包成固定问法发给平台助手，事实全部由「事件推演」这一步算出来，模型只负责把这些事实讲成中文。想换事件，把事件编号写进问题里即可（例如 WO-20261029-009）。回答里带「原文定位」的结论可以点，点开右边就是原始台账那一行。</div></div>
      <form id="f"><textarea id="q" placeholder="Enter 发送 · Shift+Enter 换行"></textarea><button class="send" id="go">发送</button></form>
    </div>
  </section>
  <section class="panel">
    <div class="tabs">
      <button class="tab on" data-tab="event">事件</button>
      <button class="tab" data-tab="evidence">证据</button>
      <button class="tab" data-tab="tools">工具轨迹</button>
      <button class="tab" data-tab="selfcheck">自检</button>
      <button class="tab" data-tab="replay">复跑</button>
    </div>
    <div class="body" id="pane"></div>
  </section>
</main>
<script>
var feed=document.getElementById('feed'), pane=document.getElementById('pane'), q=document.getElementById('q');
var go=document.getElementById('go'), stat=document.getElementById('stat');
var conv=null, current='event', agentSel=null, curEvent=null;

function el(tag, cls, text){var n=document.createElement(tag); if(cls){n.className=cls;} if(text!==null&&text!==undefined){n.textContent=String(text);} return n;}
function bubble(kind, text){var n=el('div','msg '+kind, text); feed.appendChild(n); feed.scrollTop=feed.scrollHeight; return n;}
function note(text){var n=el('div','meta',text); feed.appendChild(n); return n;}

function switchMode(mode){
  var frame=document.getElementById('frame'), chatpane=document.getElementById('chatpane');
  var btns=document.querySelectorAll('.sw');
  for(var i=0;i<btns.length;i++){btns[i].className='sw'+(btns[i].dataset.mode===mode?' on':'');}
  frame.style.display = mode==='platform' ? 'block' : 'none';
  chatpane.style.display = mode==='local' ? 'flex' : 'none';
  // 平台对话是跨源内嵌，要在平台已登录时才有内容。默认不加载：首屏给站内对话，
  // 点了「平台对话」才真正去拉，省得打开就是一片空白转圈。
  if(mode==='platform' && !frame.getAttribute('src')){
    frame.setAttribute('src', frame.getAttribute('data-src')||'');
    stat.textContent='正在连平台…';
    frame.onload=function(){stat.textContent='就绪';};
    setTimeout(function(){
      if(stat.textContent==='正在连平台…'){stat.textContent='平台没连上';}
    },8000);
  }
}

function send(){
  var text=q.value.trim(); if(!text||go.disabled){return;}
  q.value=''; bubble('me',text); go.disabled=true; stat.textContent='调用中…';
  var agent=(document.getElementById('agentSel')||{}).value||'asset_qa';
  var payload={message:text, agent:agent};
  if(conv){payload.conversation_id=conv;}
  fetch('/api/workbench/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
   .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});})
   .then(function(res){
      go.disabled=false;
      if(!res.ok){bubble('ai','（失败）'+(res.d.error||'未知错误')); stat.textContent='未连接'; return;}
      conv=res.d.conversation_id||conv;
      var body=res.d.answer_text||res.d.answer||'（空回答）';
      var n=bubble('ai',body);
      if(res.d.structured){ renderStructured(res.d.structured,n,res.d.labels||{}); }
      var refs=res.d.evidence_refs||[];
      if(refs.length){
        var box=el('div','chips');
        refs.forEach(function(ref){var c=el('span','chip',ref); c.title='点击查看原文'; c.onclick=function(){locate(ref);}; box.appendChild(c);});
        n.appendChild(box);
      }
      var cites=res.d.citations||[];
      if(cites.length){
        var cbox=el('div','chips');
        cites.forEach(function(txt){
          var c=el('span','chip',humanWith(txt,res.d.labels));
          c.title=txt+'（点击反查这条引用对应的台账切片）';
          c.onclick=function(){searchRef(txt);};
          cbox.appendChild(c);
        });
        n.appendChild(cbox);
      }
      note(res.d.agent+' · 会话 '+(conv||'-')+(refs.length?(' · '+refs.length+' 条可定位引用'):'')+(cites.length?(' · '+cites.length+' 条知识库引用'):''));
      if(refs.length||cites.length){ verifyAll(refs,cites,n); }
      stat.textContent='已连接';
    })
   .catch(function(e){go.disabled=false; bubble('ai','（网络错误）'+e); stat.textContent='未连接';});
}

var lastVerify=null;
function verifyAll(refs,cites,host){
  var badge=el('span','chip','证据链核验中…');
  badge.style.marginLeft='8px'; badge.style.cursor='pointer';
  badge.onclick=function(){ if(lastVerify){ renderVerify(lastVerify); } };
  host.appendChild(badge);
  fetch('/api/workbench/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({refs:refs,citations:cites})})
   .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});})
   .then(function(res){
      if(!res.ok){ badge.textContent='证据链核验失败'; badge.title=res.d.error||''; return; }
      lastVerify=res.d;
      badge.textContent='证据链 '+res.d.ok+'/'+res.d.total+' 可取证';
      badge.title='点击查看逐条核验结果';
      if(res.d.missing){
        badge.textContent+='（'+res.d.missing+' 条悬空）';
        badge.style.borderColor='#e8b4a0'; badge.style.color='#b4532a';
      }
    })
   .catch(function(e){ badge.textContent='证据链核验失败'; badge.title=String(e); });
}

function renderVerify(d){
  setTab('evidence'); pane.innerHTML='';
  var head=el('div','empty',d.verdict);
  head.style.color = d.missing ? '#b4532a' : '#0f766e';
  head.style.fontWeight='600';
  pane.appendChild(head);
  pane.appendChild(el('div','empty','悬空 = 这条引用回不到原文，要么原文里没有，要么定位信息不全；两种都要人工核对，不能当作已证实。'));
  (d.items||[]).forEach(function(it){
    var box=el('div');
    box.style.margin='12px 0'; box.style.borderTop='1px solid #f3f4f6'; box.style.paddingTop='10px';
    var tag=el('span','chip',it.ok?'可取证':'悬空');
    if(!it.ok){ tag.style.borderColor='#e8b4a0'; tag.style.color='#b4532a'; }
    box.appendChild(tag);
    var kind=el('span','mono',it.kind==='citation'?'知识库引用':'直接引用');
    kind.style.fontSize='12px'; kind.style.color='#6b7280'; kind.style.marginLeft='8px';
    box.appendChild(kind);
    var src=el('div','mono',it.input);
    src.style.fontSize='12px'; src.style.color='#374151'; src.style.marginTop='6px'; src.style.wordBreak='break-all';
    box.appendChild(src);
    if(it.ok){
      var meta=el('div','mono',(it.source_path||'')+(it.span?(' · 第 '+it.span.start+'–'+it.span.end+' 行'):(it.row?(' · 第 '+it.row+' 行'):'')));
      meta.style.fontSize='12px'; meta.style.color='#6b7280'; meta.style.marginTop='4px';
      box.appendChild(meta);
      box.appendChild(el('pre','excerpt',it.excerpt||''));
      var target=it.resolved_ref||it.input;
      var c=el('span','chip',target); c.style.marginTop='6px'; c.style.display='inline-block'; c.title='点击查看原文';
      c.onclick=function(){ locate(target); };
      box.appendChild(c);
    }else{
      var why=el('div','empty','原因：'+(it.reason||'未说明'));
      why.style.marginTop='4px';
      box.appendChild(why);
      if(it.resolved_ref){
        var r=el('span','chip',it.resolved_ref); r.style.marginTop='6px'; r.style.display='inline-block'; r.title='这条切片本身可定位，点开看原文';
        r.onclick=function(){ locate(it.resolved_ref); };
        box.appendChild(r);
      }
    }
    pane.appendChild(box);
  });
}

function locate(ref){
  setTab('evidence'); pane.innerHTML=''; pane.appendChild(el('div','empty','取原文中…'));
  fetch('/api/workbench/locate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({evidence_ref:ref})})
   .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});})
   .then(function(res){
      pane.innerHTML='';
      if(!res.ok){pane.appendChild(el('div','empty','取不到原文：'+(res.d.error||'')));return;}
      var d=res.d, a=d.asset||{};
      // 只留三行给人看：这是哪份文件、指向第几行、哪一版。
      // 文档类型与文件名本来是同义词（都叫"下穿隧道台账"），拆成两行等于重复。
      // 用 String.fromCharCode(10) 拼换行：这整段是 Python 字符串，JS 里写反斜杠加 n
      // 会被 Python 先变成真换行，整个 <script> 立刻语法错误、页面只剩布局。
      var rows=[['出处',humanFile(a.file_name)+(d.span?('（该文件共 '+d.span.line_count+' 行）'):''),a.file_name],
                ['原文位置',d.span?('第 '+d.span.start+(d.span.end!==d.span.start?('–'+d.span.end):'')+' 行'):'-',ref],
                ['版本',a.version,'']];
      var dl=el('dl','kv');
      rows.forEach(function(pair){
        dl.appendChild(el('dt',null,pair[0]));
        var dd=el('dd',null,pair[1]||'-');
        if(pair[2]){dd.title=pair[2];}
        dl.appendChild(dd);
      });
      pane.appendChild(dl);
      var det=document.createElement('details');
      var sum=document.createElement('summary');
      sum.style.cssText='cursor:pointer;font-size:12px;color:#6b7280;margin:6px 0';
      sum.textContent='技术信息（原文引用串、资产编号与文件校验值）';
      det.appendChild(sum);
      var tech=['引用串 '+ref,'资产编号 '+((a.asset_id||'-')),
                '文档类型 '+(a.doc_type||'-'),'文件 '+((a.file_name||'-')),
                'sha256 '+String(a.checksum_sha256||'-').slice(0,32)+'…'].join(String.fromCharCode(10));
      var inner=el('div','mono',tech);
      inner.style.cssText='font-size:11.5px;color:#9aa1aa;line-height:1.8;word-break:break-all;white-space:pre-wrap';
      det.appendChild(inner);
      pane.appendChild(det);
      pane.appendChild(el('pre','excerpt',d.excerpt||'（空）'));
    })
   .catch(function(e){pane.textContent='网络错误：'+e;});
}

function searchRef(text){
  setTab('evidence'); pane.innerHTML=''; pane.appendChild(el('div','empty','反查中…'));
  fetch('/api/workbench/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:text})})
   .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});})
   .then(function(res){
      pane.innerHTML='';
      if(!res.ok){pane.appendChild(el('div','empty','反查失败：'+(res.d.error||'')));return;}
      pane.appendChild(el('div','empty','引用：'+text));
      var hits=res.d.hits||[];
      if(!hits.length){pane.appendChild(el('div','empty','索引里没有匹配切片。'));return;}
      hits.forEach(function(h){
        var box=el('div'); box.style.margin='12px 0'; box.style.borderTop='1px solid #f3f4f6'; box.style.paddingTop='10px';
        var head=el('div',null,humanFile(h.source_path)
          +(h.heading_path&&h.heading_path.length?(' · '+h.heading_path.join(' / ')):''));
        head.style.fontSize='12px'; head.style.color='#6b7280'; head.style.marginBottom='6px';
        head.title=h.source_path||'';
        box.appendChild(head);
        box.appendChild(el('pre','excerpt',h.excerpt||''));
        if(h.evidence_ref){
          var c=el('span','chip',humanRef(h.evidence_ref)); c.style.marginTop='8px'; c.style.display='inline-block';
          c.title=h.evidence_ref+'（点击查看原文）';
          c.onclick=function(){locate(h.evidence_ref);};
          box.appendChild(c);
        }
        pane.appendChild(box);
      });
    })
   .catch(function(e){pane.textContent='网络错误：'+e;});
}

var KIND_CN={overlap:'权限重叠',gap:'职责空白',contention:'资源争用'};
// 六跳阶段名 -> 人话。英文阶段名是内核的字段名，不该直接摆在界面上给值班员看。
var STAGE_CN={observed:'雨情与定级',affected:'受影响对象',owner:'责任部门',
  conflict:'跨部门冲突',resource:'资源缺口',action:'处置动作'};
// 证据 ref 形如 AS-A-03-work_order-6a1282a1#A-03/work_order/v1#row:5，
// 界面上只显示「工单 · 第 5 行」，完整串留在 title 与点击行为里——可追溯不等于要给人看哈希。
var DOC_CN={work_order:'工单台账',construction_plan:'施工组织方案',drainage_facility:'排水设施台账',
  duty_list:'部门职责清单',emergency_plan:'防汛应急预案',emergency_response_case:'政府定级案例',
  underpass:'下穿隧道台账',pipeline_ledger:'管线台账',resource_inventory:'应急物资清单',
  weather_alert:'气象预警',road_network:'路网台账',procedure:'处置规程',
  public_notice:'对外通告',schematic_map:'管线示意图',forebay_curve:'泵站前池曲线'};
// 文件名同样是内部标识（underpasses.csv）。给人看中文名，文件名放 title。
var FILE_CN={'road_network.csv':'路网台账','pipeline_ledger.csv':'管线台账',
  'drainage_facilities.csv':'排水设施台账','construction_plans.csv':'施工组织方案',
  'work_orders.csv':'工单台账','weather_alerts.csv':'气象预警',
  'department_duties.csv':'部门职责清单','resource_inventory.csv':'应急物资清单',
  'plan_flood_emergency_v2.md':'防汛应急预案 v2','plan_flood_emergency_v1.md':'防汛应急预案 v1',
  'plan_flood_emergency_chengdu_2020.md':'成都市防汛应急预案（2020）',
  'plan_flood_emergency_cd_2026.md':'防汛应急预案（2026）',
  'procedure_waterlogging.md':'内涝处置规程','procedure_gas_protection.md':'燃气保护规程',
  'notice_road_closure.md':'封路通告','notice_gas_construction.md':'燃气施工通告',
  'pipeline_map.png':'管线示意图','underpasses.csv':'下穿隧道台账',
  'pump_forebay_curves.csv':'泵站前池曲线',
  'response_events_chengdu_20200811.csv':'成都 2020-08-11 定级记录',
  'mapping_report.md':'ERA5 雨量映射说明',
  // 自检栏会报文件名（decision_memory.json 命中 1 条），这四个是自检用到的台账，
  // 名字同样要能读——"命中 1 条"对评委没有信息量，"决策依据台账"才有。
  'decision_memory.json':'决策依据台账','allocation_cases.json':'调拨评测集',
  'decision_reviews.json':'专家评审样例','history.jsonl':'反馈闭环记录'};
// MCP 工具名是给开发看的（event_deduce）。工具轨迹栏要能让人读，逐个给中文动作名。
var TOOL_CN={semantic_query:'语义检索',twin_neighbors:'孪生邻接查询',doc_search:'知识检索',
  evidence_locate:'原文取证',claim_trace:'断言溯源',provenance_verify:'数据来源核验',
  event_deduce:'事件推演',counterfactual_run:'反事实推演',conflict_check:'冲突识别',
  allocation_plan:'资源调拨',scene_render:'场景渲染',scene_status:'场景状态',
  scene_compare:'场景对比',asset_lookup:'台账查询',collab_run:'跨部门协同编排'};
// 工具日志里的 ts 是 UTC（…+0000），直接截字符串会差 8 小时。按本机时区格式化。
function humanClock(iso){
  var d=new Date(iso);
  if(isNaN(d.getTime())){return String(iso||'-');}
  var p=function(n){return (n<10?'0':'')+n;};
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '
    +p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
}
function humanFile(name){
  var s=String(name||'');
  return FILE_CN[s]||s;
}
// 统一的人话化：代号 -> 中文名，文件名 -> 中文名，折断替换后多余的空格。
// 聊天回答、引用芯片、结构化段落都走它，避免三处各写一份规则。
function humanWith(text, labels){
  var out=String(text==null?'':text);
  var K=Object.keys(labels||{}).sort(function(a,b){return b.length-a.length;});
  for(var i=0;i<K.length;i++){ if(out.indexOf(K[i])>=0){out=out.split(K[i]).join(labels[K[i]]);} }
  var F=Object.keys(FILE_CN).sort(function(a,b){return b.length-a.length;});
  for(var j=0;j<F.length;j++){ if(out.indexOf(F[j])>=0){out=out.split(F[j]).join(FILE_CN[F[j]]);} }
  // 模型常把代号和名字一起写，换完会成口吃（"区排水公司（区排水公司）"）
  var V=[]; for(var m=0;m<K.length;m++){ if(labels[K[m]]){V.push(labels[K[m]]);} }
  for(var n=0;n<V.length;n++){
    out=out.split(V[n]+'（'+V[n]+'）').join(V[n]);
    out=out.split(V[n]+' '+V[n]).join(V[n]);
  }
  out=out.replace(/([一-龥])[ ]+(?=[一-龥])/g,'$1');
  // 内核字段名转述：「（order 1）」-> 「第 1 步」
  out=out.replace(/order[ ]*[=:]?[ ]*(\\d+)/g,'第 $1 步');
  // 与后端 _humanize 同一套收尾：先吃掉叠词（"兴华路雨水泵站泵站"），
  // 再把「区应急（区应急管理局）」这类"简称（全称）"折成全称。
  ['泵站','雨水口组','雨水口','下穿隧道','管线','管道','道路','路段'].forEach(function(s){
    out=out.split(s+s).join(s);
  });
  out=out.replace(/([一-龥]{2,6})（([一-龥]{2,12})）/g,function(all,a,b){
    return (b.indexOf(a)===0&&b.length>a.length)?b:all;
  });
  return out;
}
function humanRef(ref){
  var s=String(ref||'');
  var m=s.match(/#([^#]+)#(?:row|line|region):([0-9-]+)/);
  if(!m){return s;}
  var parts=m[1].split('/');
  var type=parts.length>1?parts[1]:parts[0];
  var doc=DOC_CN[type]||type;
  return doc+' · 第 '+m[2].split('-')[0]+' 行';
}
// 2026-10-20T11:10:00+08:00 -> 2026-10-20 11:10
function humanTime(v){
  var s=String(v||'');
  var m=s.match(/^([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}:[0-9]{2})/);
  return m?(m[1]+' '+m[2]):s;
}
var LEVEL_CN={blue:'蓝色响应',yellow:'黄色响应',orange:'橙色响应',red:'红色响应'};

function fetchEvent(ev){
  var host=document.getElementById('eventBody');
  if(!host){return;}
  host.innerHTML=''; host.appendChild(el('div','empty','推演中（本地引擎，不经模型）…'));
  fetch('/api/workbench/event',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({event_id:ev.id, as_of:ev.asOf})})
   .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});})
   .then(function(res){
      host.innerHTML='';
      if(!res.ok){host.appendChild(el('div','empty','推演失败：'+(res.d.error||'')));return;}
      renderEvent(res.d, host);
    })
   .catch(function(e){host.textContent='网络错误：'+e;});
}

function loadEvent(){
  pane.innerHTML='';
  var bar=el('div');
  bar.style.display='flex'; bar.style.gap='8px'; bar.style.alignItems='center';
  bar.style.marginBottom='10px'; bar.style.flexWrap='wrap';
  var sel=document.createElement('select');
  sel.id='eventSel'; sel.style.maxWidth='340px';
  sel.onchange=function(){
    var o=this.options[this.selectedIndex];
    curEvent={id:o.value, asOf:o.dataset.asof||''};
    fetchEvent(curEvent);
  };
  bar.appendChild(sel);
  var tag=el('span','empty','按工单上报时间推演');
  tag.style.fontSize='11.5px';
  bar.appendChild(tag);
  pane.appendChild(bar);
  var host=el('div'); host.id='eventBody'; pane.appendChild(host);
  host.appendChild(el('div','empty','读取事件清单…'));

  fetch('/api/workbench/events').then(function(r){return r.json();}).then(function(d){
    var events=d.events||[];
    if(!events.length){host.innerHTML=''; host.appendChild(el('div','empty','工单台账里没有事件。'));return;}
    events.forEach(function(ev){
      var opt=document.createElement('option');
      opt.value=ev.event_id;
      opt.dataset.asof=ev.as_of;
      var desc=(ev.description||'').slice(0,26);
      // 工单描述本身以路名开头（"兴华路 K0+320 积水深约…"），再拼一次路名会重复
      var road=ev.road_name||ev.road_id||'';
      if(road && desc.indexOf(road)===0){desc=desc.slice(road.length).replace(/^[ ·]+/,'');}
      opt.textContent=ev.event_id+' · '+road+(desc?(' · '+desc):'');
      sel.appendChild(opt);
    });
    var want=(curEvent&&curEvent.id)||d.default||events[0].event_id;
    for(var i=0;i<sel.options.length;i++){ if(sel.options[i].value===want){ sel.selectedIndex=i; break; } }
    var picked=sel.options[sel.selectedIndex];
    curEvent={id:picked.value, asOf:picked.dataset.asof||''};
    fetchEvent(curEvent);
  }).catch(function(e){host.textContent='读取事件清单失败：'+e;});
}

function blockTitle(text, tail){
  var h=el('div');
  h.style.fontSize='12px'; h.style.fontWeight='600'; h.style.color='#6b7280';
  h.style.letterSpacing='.4px'; h.style.margin='14px 0 6px';
  h.textContent=text;
  if(tail){
    var t=el('span',null,'　'+tail);
    t.style.fontWeight='400'; t.style.color='#9aa1aa';
    h.appendChild(t);
  }
  return h;
}

function refChips(refs, limit){
  refs=refs||[];
  var box=el('div','chips');
  var shown=(limit&&refs.length>limit)?refs.slice(0,limit):refs;
  shown.forEach(function(ref){
    var c=el('span','chip',humanRef(ref)); c.title=ref+'（点击查看原文）';
    c.onclick=function(){locate(ref);};
    box.appendChild(c);
  });
  // 47 条全铺开会把下面内容顶走；先给前 12 条，其余点开再补。
  if(limit&&refs.length>limit){
    var more=el('span','chip','展开其余 '+(refs.length-limit)+' 条');
    more.style.borderStyle='dashed'; more.style.color='#6b7280';
    more.onclick=function(){
      var rest=el('div','chips');
      refs.slice(limit).forEach(function(ref){
        var c=el('span','chip',humanRef(ref)); c.title=ref+'（点击查看原文）';
        c.onclick=function(){locate(ref);};
        rest.appendChild(c);
      });
      box.parentNode.insertBefore(rest, box.nextSibling);
      more.remove();
    };
    box.appendChild(more);
  }
  return box;
}

function fold(host, title, lines){
  var det=document.createElement('details');
  det.style.margin='0 0 10px';
  var sum=document.createElement('summary');
  sum.style.cursor='pointer'; sum.style.fontSize='12.5px'; sum.style.color='#374151';
  sum.textContent=title;
  det.appendChild(sum);
  var inner=el('div');
  inner.style.padding='6px 0 0 12px'; inner.style.fontSize='12.5px'; inner.style.lineHeight='1.8';
  inner.style.color='#4b5563'; inner.style.wordBreak='break-word';
  inner.textContent=lines;
  det.appendChild(inner);
  host.appendChild(det);
  return inner;
}

function renderEvent(d, target){
  var host=target||pane;
  var ob=d.observed||{};
  // 台账里的「代号 -> 中文名」。内核产出的文本带 ORG-ROAD / DF-PS-01 / RES-PUMP-M 这类内部编号，
  // 展示前换成中文名，原文留在 title 里——给值班员看人话，给核对的人留原串。
  var LABELS=d.labels||{};
  // 统一走 humanWith：代号、文件名、叠词、括号全称、order 编号这一整套规则只在这里写一份。
  // 原来事件栏和结构化段落各写一份简化版，改一处忘一处就会漏出 ORDER 1 这种串。
  function H(s){ return humanWith(s, LABELS); }
  var line=el('div');
  line.style.fontSize='15px'; line.style.fontWeight='600';
  // 已闭环事件引擎会直接拒推（conclusion.refused），不硬编结论——这一屏要把这件事说出来，
  // 而不是显示一排 0。判据就是 observed 里有没有 refused。
  if(ob.refused){
    line.textContent=d.event_id+' · 工单已结案，不给出处置方案';
    line.className='warn';
  }else{
    line.textContent=(ob.district||'—')+' 片区 · '+(LEVEL_CN[ob.response_level]||ob.response_level||'未定级')
      +' · '+(ob.depth_cm?('积水 '+ob.depth_cm+' cm'):'无水深记录');
  }
  host.appendChild(line);
  if(ob.trigger){
    var sub=el('div','empty','触发：'+ob.trigger);
    sub.style.fontSize='12px';
    host.appendChild(sub);
  }

  if(ob.refused){
    var why=el('div','warn','这张工单在处理流程里已经结案。系统不会为它重新编排处置动作，也不去推断受影响的设施——'
      +'宁可说"这次不推"，也不硬凑一份看着完整的方案。');
    why.style.fontSize='13px'; why.style.lineHeight='1.7'; why.style.margin='10px 0 4px';
    host.appendChild(why);
    var unc=d.uncovered_stages||[];
    if(unc.length){
      var names=unc.map(function(x){return STAGE_CN[x]||x;});
      host.appendChild(el('div','empty','本次没有推断：'+names.join('、')));
    }
    var refs0=d.evidence_refs||[];
    host.appendChild(blockTitle('依据','共 '+refs0.length+' 条，点开看原文'));
    if(!refs0.length){host.appendChild(el('div','empty','无'));}
    host.appendChild(refChips(refs0, 12));
    var foot0=el('div','empty');
    foot0.style.marginTop='14px'; foot0.style.fontSize='11.5px';
    foot0.textContent='工单上报时间 '+(humanTime(d.as_of)||'-');
    host.appendChild(foot0);
    fold(host,'技术信息（这份结论由哪一版规则、哪一份数据算出）',
      '规则版本 '+(d.rules_version||'-')+'；数据快照 '+String(d.snapshot_hash||'-')
      +'。内核原话：'+(ob.refused||'-')+'。核对时可拿这几项回查同一次推演。');
    return;
  }

  var actions=d.actions||[];
  host.appendChild(blockTitle('处置顺序','共 '+actions.length+' 步'));
  if(!actions.length){host.appendChild(el('div','empty','无'));}
  var ol=el('ol');
  ol.style.margin='0'; ol.style.paddingLeft='20px'; ol.style.fontSize='13px'; ol.style.lineHeight='1.75';
  actions.forEach(function(a){
    var li=el('li',null,H(a.action||''));
    li.style.marginBottom='2px';
    li.title=(a.action||'');  // 悬停看内核原文，代号与措辞都没丢
    ol.appendChild(li);
  });
  host.appendChild(ol);
  // 部门单列一行：动作被换成人话后代号就看不见了，这里仍要能一眼看出"这件事要惊动几个部门"。
  var joined=actions.map(function(a){return a.action||'';}).join(' ');
  var used=[];
  Object.keys(LABELS).forEach(function(code){
    if(code.indexOf('ORG-')===0 && joined.indexOf(code)>=0){used.push(LABELS[code]);}
  });
  if(used.length){
    var legend=el('div','empty','涉及部门：'+used.join('、'));
    legend.style.fontSize='11.5px'; legend.style.marginTop='6px';
    host.appendChild(legend);
  }

  var cf=d.conflicts||[];
  host.appendChild(blockTitle('跨部门冲突','共 '+cf.length+' 条'));
  if(!cf.length){host.appendChild(el('div','empty','无'));}
  cf.forEach(function(c){
    var row=el('div');
    row.style.fontSize='13px'; row.style.lineHeight='1.7'; row.style.padding='3px 0';
    row.appendChild(el('b',null,(c.conflict_id||'')+' '));
    row.appendChild(el('span','warn',(KIND_CN[c.kind]||c.kind||'')+'　'));
    var raw=((c.subject||'')+(c.detail?('：'+c.detail):''));
    var body=el('span',null,H(raw));
    body.title=raw;
    row.appendChild(body);
    var orgs=c.orgs||[];
    if(orgs.length){
      var names=orgs.map(function(x){return LABELS[x]||x;});
      // 正文里已经点名了部门就不在右边再列一遍（C1 会重复成两遍同样两个部门）
      var allIn=names.every(function(n){return H(raw).indexOf(n)>=0;});
      if(!allIn){
        var oc=el('span',null,'　'+names.join(' / '));
        oc.style.fontSize='11.5px'; oc.style.color='#6b7280';
        oc.title=orgs.join(' / ');
        row.appendChild(oc);
      }
    }
    host.appendChild(row);
  });

  var gaps=d.resource_gaps||[];
  host.appendChild(blockTitle('资源缺口','共 '+gaps.length+' 项'));
  if(!gaps.length){host.appendChild(el('div','empty','无'));}
  gaps.forEach(function(g){
    var row=el('div');
    row.style.fontSize='13px'; row.style.padding='2px 0';
    var nm=LABELS[g.resource_id];
    row.textContent=(nm||(g.resource_id||''))
      +'：需 '+g.required+' '+g.unit+'，可用 '+g.available+'，缺 '+g.gap+' '+g.unit;
    row.title=(g.resource_id||'');
    if(Number(g.gap)>0){row.className='bad';}
    host.appendChild(row);
  });

  var own=d.owner||{};
  host.appendChild(blockTitle('职责','协调方 '+(LABELS[own.coordinator]||own.coordinator||'-')));
  if(own.unassigned){
    var ua=el('div','bad','无明确责任主体：'+H(own.unassigned));
    ua.style.fontSize='12.5px'; ua.style.marginBottom='6px';
    ua.title=String(own.unassigned||'');
    host.appendChild(ua);
  }
  if(own.owners){ fold(host,'各对象责任部门', H(String(own.owners).split(';').join('；'))); }

  var af=d.affected||{};
  if(af.affected){
    var items=String(af.affected).split(',').filter(function(x){return x.trim();});
    var human=items.map(H);
    var box=fold(host,'受影响对象 '+items.length+' 个', human.join('、'));
    if(box){box.title=items.join('、');}
    if(af.reason){
      // 原因串里两种内部记号混着：对象代号（DF-IN-101）和 kind 值（waterlogging）。
      // 代号走 H() 的台账对照表，kind 值只有几个，直接列词表换掉。
      var reasonTxt=H(String(af.reason)).split(';').join('；').replace(/:/g,'：')
        .replace(/waterlogging/g,'积水').replace(/gas_leak/g,'燃气泄漏')
        .replace(/power_outage/g,'停电').replace(/road_closure/g,'封路');
      fold(host,'受影响原因', reasonTxt);
    }
  }

  var refs=d.evidence_refs||[];
  host.appendChild(blockTitle('依据','共 '+refs.length+' 条，点开看原文'));
  if(!refs.length){host.appendChild(el('div','empty','无'));}
  host.appendChild(refChips(refs, 12));

  var foot=el('div','empty');
  foot.style.marginTop='14px'; foot.style.fontSize='11.5px';
  foot.textContent='推演时间 '+(humanTime(d.as_of)||'-');
  host.appendChild(foot);
  fold(host,'技术信息（这份结论由哪一版规则、哪一份数据算出）',
    '规则版本 '+(d.rules_version||'-')+'；数据快照 '+String(d.snapshot_hash||'-')
    +'。核对时可拿这两项回查同一次推演。');
}

function loadReplay(){
  pane.innerHTML='';
  var bar=el('div');
  bar.style.display='flex'; bar.style.alignItems='center'; bar.style.gap='10px';
  bar.style.marginBottom='12px'; bar.style.flexWrap='wrap';
  var btn=el('button','send','复跑本事件');
  btn.id='replayBtn'; btn.style.height='32px'; btn.style.borderRadius='8px';
  btn.onclick=function(){runReplay(this);};
  bar.appendChild(btn);
  bar.appendChild(el('span','empty','只跑 WO-20261029-007（八项判据的标准答案只对这一个事件定过）：点一下，真机跑一遍主智能体，八项判据当场重算，并与上一轮逐项比。约 10–30 秒。'));
  pane.appendChild(bar);
  var host=el('div'); host.id='replayResult'; pane.appendChild(host);
  host.appendChild(el('div','empty','读取历史…'));
  fetch('/api/workbench/replay').then(function(r){return r.json();}).then(function(d){
    host.innerHTML='';
    var runs=d.runs||[];
    if(!runs.length){host.appendChild(el('div','empty','还没有复跑记录。点上面的按钮跑第一次。'));return;}
    if(d.latest){ renderReplay({current:d.latest, diff:d.diff||{}, labels:d.labels||{}}); }
    var sep=el('div','empty','最近 '+runs.length+' 轮');
    sep.style.margin='16px 0 6px'; sep.style.paddingTop='10px'; sep.style.borderTop='1px solid #f3f4f6';
    host.appendChild(sep);
    var t=el('table'), head=el('tr');
    ['时间','主智能体第几版','判据通过','耗时','平台会话'].forEach(function(h){head.appendChild(el('th',null,h));});
    t.appendChild(head);
    runs.forEach(function(x){
      var tr=el('tr');
      tr.appendChild(el('td','mono',(x.ts||'').replace('T',' ').slice(0,19)));
      tr.appendChild(el('td','mono',(x.agent_version===null||x.agent_version===undefined)?'-':('第 '+x.agent_version+' 版')));
      tr.appendChild(el('td',x.passed?'ok':'bad',(x.checks_passed||0)+'/'+(x.checks_total||0)));
      tr.appendChild(el('td','mono',(x.elapsed_s===null||x.elapsed_s===undefined)?'-':(x.elapsed_s+' 秒')));
      tr.appendChild(el('td','mono',x.conversation_id||'-'));
      t.appendChild(tr);
    });
    host.appendChild(t);
  }).catch(function(e){host.textContent='读取历史失败：'+e;});
}

function runReplay(btn){
  btn.disabled=true; btn.textContent='复跑中…';
  stat.textContent='复跑中…';
  var host=document.getElementById('replayResult');
  if(host){host.innerHTML=''; host.appendChild(el('div','empty','正在调用平台，八项判据重算中…'));}
  fetch('/api/workbench/replay',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
   .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});})
   .then(function(res){
      btn.disabled=false; btn.textContent='复跑本事件'; stat.textContent='就绪';
      var box=document.getElementById('replayResult');
      if(!box){return;}
      box.innerHTML='';
      if(!res.ok){box.appendChild(el('div','empty','复跑失败：'+(res.d.error||'')));return;}
      // 跑完直接重载历史：新记录进日志，明细与列表都从同一份数据渲染，避免两处口径不一致
      loadReplay();
    })
   .catch(function(e){btn.disabled=false; btn.textContent='复跑本事件'; stat.textContent='未连接';
      var box=document.getElementById('replayResult'); if(box){box.textContent='网络错误：'+e;}});
}

function renderStructured(s, host, labels){
  var wrap=el('div');
  wrap.style.marginTop='8px';
  // 模型复述的处置动作/冲突里也带 ORG-ROAD 这类代号，用后端回的对照表换成人话；
  // 原文留在 title 与下面的原始 JSON 里。
  function H(t){ return humanWith(t, labels||{}); }
  var acts=s.action_order||[];
  if(acts.length){
    wrap.appendChild(blockTitle('处置顺序','共 '+acts.length+' 步'));
    var ol=el('ol');
    ol.style.margin='0'; ol.style.paddingLeft='20px'; ol.style.fontSize='12.5px'; ol.style.lineHeight='1.65';
    acts.forEach(function(x){var li=el('li',null,H(x)); li.title=String(x||''); ol.appendChild(li);});
    wrap.appendChild(ol);
  }
  var cf=s.conflicts||[];
  if(cf.length){
    wrap.appendChild(blockTitle('冲突','共 '+cf.length+' 条'));
    cf.forEach(function(c){
      var row=el('div');
      row.style.fontSize='12.5px'; row.style.lineHeight='1.7';
      row.appendChild(el('b',null,(c.conflict_id||'')+' '));
      row.appendChild(el('span','warn',(KIND_CN[c.kind]||c.kind||'')+' '));
      // 模型有时只交 conflict_id 与 kind，不给正文（实测过一次），别留一片空白
      var bodyText=H(c.subject||c.detail||'');
      var body=el('span',bodyText?'':'empty',bodyText||'（这条只给了编号与类型，正文见右侧「事件」栏）');
      body.title=String(c.subject||c.detail||'');
      row.appendChild(body);
      wrap.appendChild(row);
    });
  }
  if(typeof s.resource_gap==='number'||typeof s.resource_gap==='string'){
    wrap.appendChild(blockTitle('资源缺口'));
    var g=el('div',null,'缺口 '+s.resource_gap+'（明细见右侧「事件」栏）');
    g.style.fontSize='12.5px';
    wrap.appendChild(g);
  }
  var raw=document.createElement('details');
  raw.style.marginTop='8px';
  var sum=document.createElement('summary');
  sum.style.cursor='pointer'; sum.style.fontSize='12px'; sum.style.color='#6b7280';
  sum.textContent='原始 JSON（模型交卷内容）';
  raw.appendChild(sum);
  var pre=el('pre','excerpt',JSON.stringify(s,null,1));
  pre.style.fontSize='11.5px'; pre.style.maxHeight='240px'; pre.style.overflow='auto'; pre.style.marginTop='6px';
  raw.appendChild(pre);
  wrap.appendChild(raw);
  host.appendChild(wrap);
}

// 复跑表里的值来自内核：布尔、阶段名、冲突类型三种。三层都要翻，漏一层就会在
// 「期望 / 实际」列里留下 contention 这种英文（复跑栏曾经整列都是英文冲突类型）。
function cnTerm(x){
  return KIND_CN[x]||STAGE_CN[x]||DOC_CN[x]||FILE_CN[x]||x;
}
function fmtCell(v, labels){
  if(v===null||v===undefined){return '-';}
  if(typeof v==='boolean'){return v?'是':'否';}
  // 实际值是模型原话，带 ORG-ROAD / DF-PS-01 这类台账代号，走同一份对照表换成人话。
  if(Array.isArray(v)){return v.map(function(x){return humanWith(cnTerm(x),labels);}).join('、');}
  if(typeof v==='string'){return humanWith(cnTerm(v),labels);}
  return JSON.stringify(v);
}

function maybeNum(v){return (v===null||v===undefined)?'?':v;}

function renderReplay(d){
  var box=document.getElementById('replayResult'); if(!box){return;}
  var c=d.current||{};
  var verdict=el('div');
  verdict.style.fontWeight='600'; verdict.style.marginBottom='6px';
  verdict.className = c.passed?'ok':'bad';
  verdict.textContent = c.passed
    ? ('与本机规则引擎的结论一致 '+c.checks_passed+'/'+c.checks_total)
    : ('与本机规则引擎不一致：'+c.checks_passed+'/'+c.checks_total+' 项通过');
  box.appendChild(verdict);

  box.appendChild(el('div','empty',
    '主智能体第 '+maybeNum(c.agent_version)+' 版 · 平台会话 #'+(c.conversation_id||'-')
    +' · 耗时 '+c.elapsed_s+' 秒 · 取到依据 '+(c.evidence_count||0)+' 条 · '+(c.ts||'').replace('T',' ').slice(0,19)));

  var legend=el('div','empty','「期望」是本机规则引擎算出的标准答案（不经过大模型），「实际」这次真机跑出来的；两项对不上就是不符。');
  legend.style.cssText='font-size:11.5px;line-height:1.7;margin:8px 0 4px';
  box.appendChild(legend);
  var LB=d.labels||{};
  var t=el('table');
  // 固定列宽：不固定时「实际」列的长段落会把「期望」列挤成竖条（一个字一行），
  // 复跑栏最该读的八项期望值反而看不清。
  t.style.tableLayout='fixed';
  var cg=document.createElement('colgroup');
  // 只钉「判据」和「结果」两头，中间两列均分剩余宽度——给 31% 这种固定比例时，
  // 「期望」列在窄侧栏里还是会被压到只剩两三个字一行。
  [['108px'],[],[],['56px']].forEach(function(w){
    var col=document.createElement('col'); if(w[0]){col.style.width=w[0];} cg.appendChild(col);
  });
  t.appendChild(cg);
  var head=el('tr');
  ['判据','期望','实际','结果'].forEach(function(h){head.appendChild(el('th',null,h));});
  t.appendChild(head);
  (c.checks||[]).forEach(function(item){
    var tr=el('tr');
    var nameCell=el('td',null,item.name);
    nameCell.style.width='110px';
    tr.appendChild(nameCell);
    tr.appendChild(el('td','mono',fmtCell(item.expected,LB)));
    tr.appendChild(el('td','mono',fmtCell(item.actual,LB)));
    tr.appendChild(el('td',item.passed?'ok':'bad',item.passed?'通过':'不符'));
    t.appendChild(tr);
  });
  box.appendChild(t);

  var diff=d.diff||{};
  var dbox=el('div');
  dbox.style.marginTop='14px'; dbox.style.paddingTop='10px'; dbox.style.borderTop='1px solid #f3f4f6';
  if(!diff.available){
    dbox.appendChild(el('div','empty',diff.reason||'没有上一轮可比。'));
  }else{
    dbox.appendChild(el('div','empty',
      '与上一轮（'+(diff.previous_ts||'').replace('T',' ').slice(0,19)+' · 第 '+maybeNum(diff.previous_version)
      +' 版 · '+maybeNum(diff.previous_checks_passed)+' 项通过）相比：'
      +(diff.same_checks?'八项判定完全未变':('恢复 '+diff.fixed.length+' 项，新增失败 '+diff.broken.length+' 项'))));
    if(diff.version_changed){
      dbox.appendChild(el('div','warn','注意：这一轮换了智能体版本，差异可能来自版本而不是模型波动。'));
    }
    if((diff.fixed||[]).length){dbox.appendChild(el('div','ok','恢复：'+diff.fixed.join('、')));}
    if((diff.broken||[]).length){dbox.appendChild(el('div','bad','新增失败：'+diff.broken.join('、')));}
  }
  box.appendChild(dbox);

  if((c.errors||[]).length){
    var err=el('div','bad','平台报错：'+c.errors.join(' / '));
    err.style.marginTop='8px'; box.appendChild(err);
  }
}

function setTab(name){
  current=name;
  var tabs=document.querySelectorAll('.tab');
  for(var i=0;i<tabs.length;i++){tabs[i].className='tab'+(tabs[i].dataset.tab===name?' on':'');}
  if(name==='event'){loadEvent();}
  else if(name==='tools'){loadTools();}
  else if(name==='selfcheck'){loadStatus();}
  else if(name==='replay'){loadReplay();}
  else if(!pane.querySelector('dl')){pane.innerHTML='<div class="empty">点左侧站内对话里的 ref 芯片，这里显示原文。</div>';}
}

function loadTools(){
  pane.innerHTML=''; pane.appendChild(el('div','empty','读取中…'));
  fetch('/api/tool-log?limit=20').then(function(r){return r.json();}).then(function(d){
    pane.innerHTML='';
    var rows=(d.entries||[]).slice().reverse();
    if(!rows.length){pane.appendChild(el('div','empty','还没有工具调用记录。'));return;}
    var t=el('table'), head=el('tr');
    ['时间','做了什么','结果','耗时','数据快照'].forEach(function(h){head.appendChild(el('th',null,h));});
    t.appendChild(head);
    rows.forEach(function(r){
      var tr=el('tr');
      tr.appendChild(el('td','mono',humanClock(r.ts)));
      // 工具名换成动作名（event_deduce -> 事件推演），原名放 title
      var td=el('td',null,TOOL_CN[r.tool]||r.tool||'-');
      td.title=r.tool||'';
      if(r.knowledge_version){td.title=(r.tool||'')+' · 知识库 '+(r.knowledge_version);}
      tr.appendChild(td);
      var ok=r.status==='ok';
      var st=el('td',ok?'ok':'bad',ok?'正常':'失败');
      if(!ok&&r.error_code){st.title='错误码 '+r.error_code;}
      tr.appendChild(st);
      tr.appendChild(el('td','mono',(r.latency_ms!==null&&r.latency_ms!==undefined)?(r.latency_ms+' ms'):'-'));
      var snap=el('td','mono',(r.snapshot_hash||'').slice(0,10)||'-');
      if(r.snapshot_hash){snap.title='完整快照哈希 '+r.snapshot_hash;}
      tr.appendChild(snap);
      t.appendChild(tr);
    });
    pane.appendChild(t);
    var note=el('div','empty','共 '+rows.length+' 条 · 时间为本机时区（日志原始记录是 UTC）·「数据快照」是同一份数据的指纹，'
      +'同一指纹表示几次调用读的是同一批台账。');
    note.style.cssText='font-size:11.5px;line-height:1.7;margin-top:8px';
    pane.appendChild(note);
  }).catch(function(e){pane.textContent='读取失败：'+e;});
}

function loadStatus(){
  pane.innerHTML=''; pane.appendChild(el('div','empty','读取中…'));
  // 自检明细里的少量英文标识（PRODUCTION_LABEL、northbound）是内核常量与取数口径，
  // 在展示层换成人话；这层不改 selfcheck 的输出，/status 页与自检指纹都不受影响。
  function humanStatus(text){
    var s=String(text==null?'':text);
    // 自检明细原本直接抛文件名（decision_memory.json）。文件名叫什么与"自洽不自洽"无关，
    // 换成人读的名字，原始文件名留在这一行的 title 里，要核对回查照样看得见。
    Object.keys(FILE_CN).forEach(function(k){s=s.replace(k,FILE_CN[k]);});
    // 「唯一」这条的明细是 `PRODUCTION_LABEL = flood-response-v1.2`。把等号左边那个常量名
    // 摊成人话，并把 label 里的版本号读出来——判断依据是"只有这一份"，说出来才算数。
    var m=/^PRODUCTION_LABEL\\s*=\\s*(\\S+)\\s*$/.exec(s);
    if(m){
      var ver=/-v([0-9.]+)$/.exec(m[1]);
      return '当前生效的是 '+m[1]+(ver?('（洪涝调拨策略第 '+ver[1]+' 版）'):'')
        +'，平台上只发布这一份';
    }
    return s
      // source 说的是"这份快照当初怎么来的"，不是"平台现在活着"。别翻成"实时取数"。
      .replace('取数来源 northbound','快照原由平台接口抓取')
      .replace('取数来源 snapshot','取自本地快照')
      .replace('取数来源 db','快照取自平台数据库');
  }
  fetch('/api/workbench/status').then(function(r){return r.json();}).then(function(d){
    pane.innerHTML='';
    var checks=d.checks||[];
    if(!checks.length){pane.appendChild(el('div','empty','没有自检项。'));return;}
    checks.forEach(function(c){
      var row=el('div','row');
      var label=c.state==='ok'?'通过':(c.state==='warn'?'待注意':'不通过');
      var cls=c.state==='ok'?'ok':(c.state==='warn'?'warn':'bad');
      row.appendChild(el('b',null,c.name+'　'));
      row.appendChild(el('span',cls,label));
      row.appendChild(el('div','empty',humanStatus(c.detail)));
      pane.appendChild(row);
    });
    var okCount=checks.filter(function(c){return c.state==='ok';}).length;
    var note=el('div','empty','共 '+checks.length+' 项，通过 '+okCount+' 项。这些检查只读本地台账与快照，'
      +'不发外部请求——平台挂不挂，这一栏都能打开。');
    note.style.cssText='font-size:11.5px;line-height:1.7;margin-top:10px';
    pane.appendChild(note);

    var snap=d.platform_snapshot||{};
    if(snap.available){
      var when=humanClock(snap.generated_at);
      // 不复述项名：项名「平台 Agent 可读」是 selfcheck.py 里的字面值，会进冻结
      // 期望文件，改名就破 108/108。改用「这一项」指代，读起来也不别扭。
      var tip=el('div','empty','这一项读的是 '+when+' 生成的版本快照（'+snap.agents
        +' 个智能体），不是此刻探测。要看平台此刻在不在，去问一句或看左侧对话。');
      tip.style.cssText='font-size:11.5px;line-height:1.7;margin-top:6px;color:#9aa1aa';
      pane.appendChild(tip);
    }
  }).catch(function(e){pane.textContent='读取失败：'+e;});
}

document.getElementById('f').addEventListener('submit',function(e){e.preventDefault();send();});
q.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
var sws=document.querySelectorAll('.sw'); for(var i=0;i<sws.length;i++){sws[i].onclick=function(){switchMode(this.dataset.mode);};}
// 首屏进站内对话：它不依赖平台前端，打开就能问；平台页要等点了才加载。
switchMode('local');
var tabs=document.querySelectorAll('.tab'); for(var j=0;j<tabs.length;j++){tabs[j].onclick=function(){setTab(this.dataset.tab);};}
// 允许直接链接到某一栏（演示时把 #replay 发出去，打开就是复跑面板）
var __h=location.hash||'';
if(__h.indexOf('#replay')===0){setTab('replay');}
else if(__h.indexOf('#tools')===0){setTab('tools');}
else if(__h.indexOf('#selfcheck')===0){setTab('selfcheck');}
// #ref=<evidence_ref>：直接打开某一条原文，演示时可以把链接发出去
else if(__h.indexOf('#ref=')===0){setTab('evidence'); locate(decodeURIComponent(__h.slice(5)));}
else{
  // #event=<事件编号>：演示时可以直接链到某个事件，不用手点下拉框
  if(__h.indexOf('#event=')===0){curEvent={id:decodeURIComponent(__h.slice(7)), asOf:''};}
  setTab('event');
}
// #ask=<问题>&agent=deduction：打开就自动问一句，演示时把链接发出去即可。
// 只在显式带这个 hash 时才触发，正常打开不会偷偷调平台。
(function(){
  if(__h.indexOf('#ask=')!==0){return;}
  var rest=__h.slice(5), agentKey='asset_qa';
  var idx=rest.indexOf('&agent=');
  if(idx>=0){agentKey=decodeURIComponent(rest.slice(idx+7)); rest=rest.slice(0,idx);}
  switchMode('local');
  var sel=document.getElementById('agentSel');
  if(sel){sel.value=agentKey;}
  q.value=decodeURIComponent(rest);
  send();
})();
</script></body></html>
"""

_WORKBENCH_TOOLKIT: Any = None


def _workbench_toolkit() -> Any:
    """进程内复用一个工具链实例：构造要读台账与语义库，每次请求都建一遍太亏。"""
    global _WORKBENCH_TOOLKIT
    if _WORKBENCH_TOOLKIT is None:
        from .mcp_server import UrbanToolkit

        _WORKBENCH_TOOLKIT = UrbanToolkit()
    return _WORKBENCH_TOOLKIT


def clean_agent_answer(answer: str) -> str:
    """去掉运行时的交卷外壳，只留人话。

    平台要求模型用 `<code> final_answer("...")</code>` 交卷；北向原样返回这段文本，
    直接贴到聊天框里会像代码。这里只剥壳，不改内容。
    """
    text = re.sub(r"</?code>", "", answer or "").strip()
    match = re.match(r"final_answer\(\s*(.*)\)\s*$", text, re.S)
    if match:
        text = match.group(1).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    return text


#: 复跑用的默认事件：必须与主链路验收同一个事件，否则"跟上次比"没有可比性。
WORKBENCH_REPLAY_EVENT = "WO-20261029-007"
WORKBENCH_REPLAY_AS_OF = "2026-10-29T09:40:00+08:00"


def _replay_log_path() -> Path:
    """运行历史落在 `artifacts/` 下（已 gitignore）：它是运行产物，不是证据样本。

    为什么不写进 `samples/`：每点一次就追加一行，进版本库等于把仓库当日志用。
    要留档的结论另外落到 `samples/nexent/live/`。
    """
    directory = Path(__file__).resolve().parents[2] / "artifacts" / "workbench"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "replay_runs.jsonl"


def read_replay_runs(*, event_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """读复跑历史，新的在前。坏行跳过——日志被手工编辑过也不该让页面崩。"""
    path = _replay_log_path()
    if not path.is_file():
        return []
    runs: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event_id and record.get("event_id") != event_id:
            continue
        runs.append(record)
    return list(reversed(runs))[:limit]


def _live_agent_version(agent_name: str) -> int | None:
    """查一次平台当前版本号，用于判断两次复跑之间换没换版本。

    失败返回 None，不抛：版本查不到只影响"这次变化能否归因到版本"，
    不该让复跑本身失败。
    """
    import urllib.request

    base = os.getenv("NEXENT_BASE_URL", "http://localhost:5013").rstrip("/")
    key = os.getenv("NEXENT_API_KEY", "").strip()
    request = urllib.request.Request(
        f"{base}/nb/v1/agents",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - 版本号是附加信息
        return None
    for item in payload.get("data") or []:
        if item.get("name") == agent_name:
            return item.get("current_version_no")
    return None


def _replay_diff(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """逐项比两次复跑。只比 passed，不比 expected/actual 文本——
    期望值来自同一份离线基线，实际值措辞会变，逐字比会把改写误判成结论变化。
    """
    if not previous:
        return {"available": False, "reason": "这是第一次复跑，没有可比的上一轮"}
    before = {item["name"]: item.get("passed") for item in previous.get("checks", [])}
    after = {item["name"]: item.get("passed") for item in current.get("checks", [])}
    return {
        "available": True,
        "previous_ts": previous.get("ts"),
        "previous_version": previous.get("agent_version"),
        "previous_checks_passed": previous.get("checks_passed"),
        "version_changed": previous.get("agent_version") != current.get("agent_version"),
        # 上轮不过、这轮过；上轮过、这轮不过。两个都空 = 判定没动。
        "fixed": [name for name, ok in after.items() if ok and before.get(name) is False],
        "broken": [name for name, ok in after.items() if (not ok) and before.get(name) is True],
        "same_checks": bool(before) and before == after,
    }


def workbench_replay(body: bytes | None) -> tuple[int, str, bytes]:
    """POST /api/workbench/replay：同一事件真机复跑一次，并与上一次逐项比对。

    为什么值得单独做一个端点：`8/8` 目前只是一份 JSON 里的数字，谁也当场复现不了。
    点一下，八项判据实时重算、和上一轮摊在一起比，"可进化"才有可验证的形式。
    """
    try:
        payload = json.loads((body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error("请求体不是合法 JSON", 400)
    event_id = str(payload.get("event_id") or WORKBENCH_REPLAY_EVENT).strip()
    as_of = str(payload.get("as_of") or WORKBENCH_REPLAY_AS_OF).strip()

    key = os.getenv("NEXENT_API_KEY", "").strip()
    if not key:
        return _error("未配置 NEXENT_API_KEY（北向访问凭据），无法复跑", 503)

    from .nexent_transport import NexentSettings
    from .nexent_urban import DEFAULT_LABELS
    from .nexent_urban import run as urban_run

    settings = NexentSettings(
        base_url=os.getenv("NEXENT_BASE_URL", "http://localhost:5013").rstrip("/"),
        api_key=key,
        agent_name=WORKBENCH_AGENTS["deduction"],
    )
    previous_runs = read_replay_runs(event_id=event_id, limit=1)
    started = time.time()
    try:
        report = urban_run(
            settings=settings,
            event_id=event_id,
            as_of=as_of,
            labels=list(DEFAULT_LABELS),
            attempts=1,
            out=io.StringIO(),
        )
    except Exception as exc:  # noqa: BLE001 - 平台不通要给可读原因，不能抛 500 堆栈
        return _error(f"复跑失败：{type(exc).__name__}: {exc}"[:300], 502)

    answer = report.answer
    current: dict[str, Any] = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event_id": event_id,
        "as_of": as_of,
        "agent": settings.agent_name,
        "agent_version": _live_agent_version(settings.agent_name),
        "conversation_id": report.conversation_id,
        "elapsed_s": round(time.time() - started, 1),
        "checks_passed": sum(1 for item in report.checks if item.passed),
        "checks_total": len(report.checks),
        "passed": not report.errors
        and bool(report.checks)
        and all(item.passed for item in report.checks),
        "checks": [
            {"name": item.name, "expected": item.expected, "actual": item.actual, "passed": item.passed}
            for item in report.checks
        ],
        "summary": (answer.summary if answer else "")[:300],
        "evidence_count": len(answer.evidence_refs) if answer else 0,
        "errors": list(report.errors)[:3],
    }

    with _replay_log_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(current, ensure_ascii=False, default=str) + "\n")

    previous = previous_runs[0] if previous_runs else None
    return _json(
        {
            "current": current,
            "previous": {
                "ts": previous.get("ts"),
                "agent_version": previous.get("agent_version"),
                "conversation_id": previous.get("conversation_id"),
                "checks_passed": previous.get("checks_passed"),
                "checks_total": previous.get("checks_total"),
            }
            if previous
            else None,
            "diff": _replay_diff(current, previous),
            "labels": _label_maps(),
        }
    )


def workbench_replay_history(limit: int = 10) -> tuple[int, str, bytes]:
    """GET /api/workbench/replay：最近几轮复跑。

    除了摘要列表，还回最近一轮的**完整明细**与它跟上一轮的差异：页面打开就该有东西看，
    不必先花 10 秒跑一次才知道上次结果如何。
    """
    fields = (
        "ts",
        "event_id",
        "agent_version",
        "conversation_id",
        "elapsed_s",
        "checks_passed",
        "checks_total",
        "passed",
    )
    runs = read_replay_runs(limit=limit)
    latest = runs[0] if runs else None
    previous = runs[1] if len(runs) > 1 else None
    return _json(
        {
            "runs": [{key: run.get(key) for key in fields} for run in runs],
            "latest": latest,
            "diff": _replay_diff(latest, previous) if latest else {"available": False, "reason": "还没有复跑记录"},
            # 判据的「实际」值是模型原话，里面带 ORG-ROAD / DF-PS-01 这类台账代号。
            # 页面拿这张对照表换成人名，否则复跑栏会出现"街道中心与交警"以外的英文串。
            "labels": _label_maps(),
        }
    )


def _ledger_rows(file_name: str) -> list[dict[str, str]]:
    """读台账 CSV。工单、部门、物资三处都用它，免得每个接口各读一遍。"""
    from .urban_dataset import DEFAULT_DATASET_ROOT

    path = DEFAULT_DATASET_ROOT / file_name
    if not path.is_file():
        return []
    # utf-8-sig：这几张台账首列表头带 BOM（﻿org_id），按 utf-8 读会得到空键，
    # 于是所有行挤成 {"": "最后一行"}——查部门名时静默失效，症状很像"数据没加载"。
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _work_order_rows() -> list[dict[str, str]]:
    return _ledger_rows("work_orders.csv")


def workbench_events() -> tuple[int, str, bytes]:
    """GET /api/workbench/events：可切换的事件清单，直接读工单台账。

    为什么不做成"输入框随便填"：填一个不存在的事件只会拿到 NOT_FOUND，
    演示时像个坏掉的系统。这里把台账里真实存在的工单列出来，
    选哪个就推哪个——顺带说明这套东西不是一个事件写死的。
    """
    rows = _work_order_rows()
    if not rows:
        return _error("工单台账缺失或为空", 500)
    roads = {row.get("road_id", ""): row.get("name", "") for row in _ledger_rows("road_network.csv")}
    events = [
        {
            "event_id": row.get("order_id") or "",
            "as_of": row.get("reported_at") or "",
            "order_type": row.get("order_type") or "",
            "road_id": row.get("road_id") or "",
            # 下拉里给人看的是路名，"R-101" 这种编号留在 road_id 里
            "road_name": roads.get(row.get("road_id") or "", "") or (row.get("road_id") or ""),
            "facility_id": row.get("facility_id") or "",
            "severity": row.get("severity") or "",
            "description": (row.get("description") or "")[:80],
        }
        for row in rows
    ]
    return _json({"events": events, "default": WORKBENCH_REPLAY_EVENT})


def workbench_event(body: bytes | None = None) -> tuple[int, str, bytes]:
    """GET/POST /api/workbench/event：把事件的六跳事实摆出来，**不经过 LLM**。

    为什么要有这一屏：工作台打开是一句"点 ref 看原文"，评委得先知道该问什么。
    这一栏给的是事件本身——定级、受影响对象、职责、冲突、资源缺口、处置顺序，
    全部来自本地引擎（确定性、秒出、不消耗模型），每条依据都能点回原文。
    它也是"复核"的参照物：下面几栏核的是模型答得对不对，这一栏是标准答案的事实层。
    """
    payload: dict[str, Any] = {}
    if body:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error("请求体不是合法 JSON", 400)
    event_id = str(payload.get("event_id") or WORKBENCH_REPLAY_EVENT).strip()
    as_of = str(payload.get("as_of") or "").strip()
    if not as_of:
        # 没给时点就取该工单自己的上报时间；用别的事件的时点会推出错误定级。
        as_of = next(
            (row.get("reported_at") or "" for row in _work_order_rows() if row.get("order_id") == event_id),
            "",
        ) or WORKBENCH_REPLAY_AS_OF

    try:
        result = _workbench_toolkit().call(
            "event_deduce",
            {"event_id": event_id, "as_of": as_of, "caller_labels": ["ORG-EMERG"]},
        )
    except Exception as exc:  # noqa: BLE001 - 事件不存在/规则校验失败都给可读原因
        return _error(f"推演失败：{type(exc).__name__}: {exc}"[:300], 500)
    if result.status != "ok":
        message = getattr(result.error, "message", None) or "该事件推演不可用"
        return _error(str(message), 404)

    data = result.data
    stages: dict[str, Any] = {}
    actions: list[dict[str, Any]] = []
    for step in data.get("steps") or []:
        if step.get("stage") == "action":
            actions.append(step.get("conclusion") or {})
        else:
            stages[str(step.get("stage"))] = step.get("conclusion") or {}
    # order 在结论里是字符串（"1"），按数值排，别按字典序——10 会排到 2 前面
    actions.sort(key=lambda item: int(str(item.get("order") or 0)))

    refs = sorted({ref for step in data.get("steps") or [] for ref in (step.get("evidence_refs") or [])})
    return _json(
        {
            "event_id": event_id,
            "as_of": as_of,
            "rules_version": data.get("rules_version"),
            "snapshot_hash": data.get("snapshot_hash"),
            "observed": stages.get("observed") or {},
            "affected": stages.get("affected") or {},
            "owner": stages.get("owner") or {},
            "conflicts": data.get("conflicts") or [],
            "resource_gaps": data.get("resource_gaps") or [],
            "uncovered_stages": data.get("uncovered_stages") or [],
            "actions": actions,
            "evidence_refs": refs,
            # 代号 -> 中文名。界面上不该出现 ORG-ROAD / RES-PUMP-M / DF-PS-01 这种内部编号，
            # 但代号仍是证据与判据里的原值，所以原样带回，由页面决定展示哪一种
            # （页面把代号换成人话，原始串放 title 与折叠的技术信息里，可追溯性不丢）。
            "labels": _label_maps(),
        }
    )


def _label_maps() -> dict[str, str]:
    """把四张台账里的「代号 -> 中文名」合成一张替换表。

    为什么在展示层替换而不是改内核输出：内核产出的 `action` / `conflict.subject` 是
    判据比对与证据引用的原值，改写它等于改结论。所以对照表只用于渲染，
    原文保留在 title 与技术信息里。
    """
    labels: dict[str, str] = {}
    for row in _ledger_rows("department_duties.csv"):
        if row.get("org_id"):
            labels[row["org_id"]] = row.get("org_name") or row["org_id"]
    for row in _ledger_rows("resource_inventory.csv"):
        if row.get("resource_id"):
            labels[row["resource_id"]] = row.get("name") or row["resource_id"]
    for file_name, key, field in (
        ("drainage_facilities.csv", "facility_id", "name"),
        ("underpasses.csv", "underpass_id", "name"),
        ("road_network.csv", "road_id", "name"),
    ):
        for row in _ledger_rows(file_name):
            if row.get(key):
                labels[row[key]] = row.get(field) or row[key]
    # 管线台账没有 name 列，但「受影响原因」和「各对象责任部门」里满屏 PL-DRN-101。
    # 用「路名 + 口径 + 管种」拼一个能读的名字，比留代号强，也比编一个不存在的
    # 官方名老实——拼出来的每一段都来自台账本身。
    roads = {row.get("road_id"): row.get("name") for row in _ledger_rows("road_network.csv")}
    kind_cn = {"drainage": "雨水", "gas": "燃气", "power": "电力", "tel": "通信", "water": "给水"}
    for row in _ledger_rows("pipeline_ledger.csv"):
        pid = row.get("pipeline_id")
        if not pid:
            continue
        pieces = [
            roads.get(row.get("road_id") or "") or "",
            f"DN{row['diameter_mm']}" if row.get("diameter_mm") else "",
            (kind_cn.get(row.get("kind") or "", "") or "") + "管",
        ]
        labels[pid] = " ".join(piece for piece in pieces if piece).strip() or pid
    # 施工计划在正文里就是项目名（CP-2026-041 = 兴华路燃气主干管更换），直接用。
    for row in _ledger_rows("construction_plans.csv"):
        if row.get("plan_id"):
            labels[row["plan_id"]] = row.get("project_name") or row["plan_id"]
    return labels


def _humanize(text: str) -> str:
    """把台账代号换成人话，仅供展示。

    用在两处：事件栏（内核产出的动作/冲突文本）与聊天回答（模型自己写的代号）。
    顺序有讲究——先把代号全换成中文名，再折叠替换造成的重复，最后收空格；
    反过来的话 "ORG-DRAIN（区排水公司）" 会变成 "区排水公司（区排水公司）"。
    """
    labels = _label_maps()
    out = str(text or "")
    for code in sorted(labels, key=len, reverse=True):
        if code in out:
            out = out.replace(code, labels[code])

    # 模型常把代号和名字一起写（"兴华路下穿隧道东端（UP-XH-101）"），换完就成了口吃
    for name in sorted(set(labels.values()), key=len, reverse=True):
        out = out.replace(f"{name}（{name}）", name)
        out = re.sub(re.escape(name) + r"[ ]{1,3}" + re.escape(name), name, out)

    # 字段名与枚举值：close_threshold_m=0.25 -> 封控阈值 0.25 m
    for field, (cn, unit) in _FIELD_CN.items():
        out = re.sub(
            re.escape(field) + r"=([0-9]+(?:\.[0-9]+)?)",
            lambda m, cn=cn, unit=unit: f"{cn} {m.group(1)}{unit}",
            out,
        )
        out = out.replace(field + "=", f"{cn}：")
        # 裸字段名（"台账 owner_org 未填"）没有等号，单独收一遍
        out = out.replace(field, cn)
    for token, cn in _VALUE_CN.items():
        out = out.replace(f"（{token}）", "")          # "淤积（clogged）" 括号里是重复信息
        # "状态为 clogged" / "状态 clogged" / "状态：clogged" 都要换
        out = re.sub(r"状态[为是]?[ ]*" + re.escape(token), f"状态：{cn}", out)

    # "挡水槛 entry_sill_height_m=0.15" 换完是"挡水槛 挡水槛高度 0.15 m"——字段名的中文
    # 和前面那个词重了。空格要一起吃掉，所以重复折叠放在最后收完空格之后再跑一遍。
    # 文件名换中文名：underpasses.csv -> 下穿隧道台账
    for name, cn in _FILE_CN.items():
        out = out.replace(name, cn)

    # 模型常把字段名塞在括号里（"0.25 m（close_threshold_m）"），中文句子里多余
    out = re.sub(r"[（(][a-z][a-z0-9_]{3,}[）)]", "", out)
    # 平台内部的引用角标 [[a1]]：正文里已有「出处：…」，展示时去掉
    out = re.sub(r"\[\[[A-Za-z0-9_\-]+\]\]", "", out)
    # 模型爱写 markdown 加粗，聊天框是纯文本，星号会原样露出来
    out = out.replace("**", "")

    # 收掉代号替换留下的汉字间空格，再折一次重复术语（"挡水槛 挡水槛高度"）
    out = re.sub(r"(?<=[一-龥])[ ]+(?=[一-龥])", "", out)
    for term in ("挡水槛", "封控阈值", "运维主体", "最低点标高"):
        out = out.replace(term + term, term)

    # 换成中文名之后会叠词：「DF-PS-01 泵站无书面运维主体」-> 「兴华路雨水泵站泵站无…」。
    # 名字本身已经以「泵站」结尾，原文后面那个「泵站」就多余了。
    for suffix in ("泵站", "雨水口组", "雨水口", "下穿隧道", "管线", "管道", "道路", "路段"):
        out = out.replace(suffix + suffix, suffix)

    # 「区应急（区应急管理局）」：括号里其实是同一个主体的全称，模型写作"简称（全称）"。
    # 只在括号内是括号外的延长时才换成全称，并列关系（A（B））不动。
    def _fold_alias(match: "re.Match[str]") -> str:
        short, full = match.group(1), match.group(2)
        return full if full.startswith(short) and len(full) > len(short) else match.group(0)

    out = re.sub(r"([一-龥]{2,6})（([一-龥]{2,12})）", _fold_alias, out)

    # 模型按内核字段名转述步骤号：「先停基坑开挖（order 1）」。翻成「第 1 步」。
    out = re.sub(r"order[ ]*[=:]?[ ]*(\d+)", r"第 \1 步", out)
    # 事件类别在台账与内核里是英文枚举（waterlogging）。这几个词不会和中文撞车，
    # 直接全局换掉即可——留在正文里就是"工单直接报出：waterlogging"。
    for token, cn in (
        ("waterlogging", "积水"),
        ("gas_leak", "燃气泄漏"),
        ("power_outage", "停电"),
        ("road_closure", "封路"),
    ):
        out = out.replace(token, cn)
    return out


#: 台账字段名 -> (中文名, 数值单位)。只收界面上真会出现的，别贪多。
_FIELD_CN: dict[str, tuple[str, str]] = {
    "close_threshold_m": ("封控阈值", " m"),
    "entry_sill_height_m": ("挡水槛高度", " m"),
    "lowest_point_z_m": ("最低点标高", " m"),
    "capacity_m3h": ("抽排能力", " m³/h"),
    "owner_org": ("运维主体", ""),
    "status": ("状态", ""),
    "qty_available": ("可用数量", ""),
}

#: 台账枚举值 -> 人话。括号里只写英文枚举的，展示时直接去掉括号（中文句子里已经说了）。
_VALUE_CN: dict[str, str] = {
    "clogged": "淤积堵塞",
    "open": "进行中",
    "closed": "已结案",
    "normal": "正常",
    "fault_pending_repair": "待检修",
}

#: 文件名 -> 中文名。与页面里的 FILE_CN 同源，两边都要用（后端换聊天回答，前端换卡片）。
_FILE_CN: dict[str, str] = {
    "road_network.csv": "路网台账",
    "pipeline_ledger.csv": "管线台账",
    "drainage_facilities.csv": "排水设施台账",
    "construction_plans.csv": "施工组织方案",
    "work_orders.csv": "工单台账",
    "weather_alerts.csv": "气象预警",
    "department_duties.csv": "部门职责清单",
    "resource_inventory.csv": "应急物资清单",
    "plan_flood_emergency_v2.md": "防汛应急预案 v2",
    "plan_flood_emergency_v1.md": "防汛应急预案 v1",
    "plan_flood_emergency_chengdu_2020.md": "成都市防汛应急预案（2020）",
    "plan_flood_emergency_cd_2026.md": "防汛应急预案（2026）",
    "procedure_waterlogging.md": "内涝处置规程",
    "procedure_gas_protection.md": "燃气保护规程",
    "notice_road_closure.md": "封路通告",
    "notice_gas_construction.md": "燃气施工通告",
    "underpasses.csv": "下穿隧道台账",
    "pump_forebay_curves.csv": "泵站前池曲线",
    "response_events_chengdu_20200811.csv": "成都 2020-08-11 定级记录",
    "mapping_report.md": "ERA5 雨量映射说明",
}


def _deduction_query(question: str) -> tuple[str, str]:
    """把一句自由提问包成推演 Agent 能接的协议问法，返回 (事件编号, 完整问法)。

    推演 Agent 是两步式协议：第一步必须调 `event_deduce`，之后按固定字段交卷。
    直接把"这个事件谁负责"丢过去，它要么当闲聊、要么走偏，整轮拿不到结构化结论。
    包一层之后自由提问与协议可以同时成立：**用户问什么不改变事实来源，只影响模型
    在 summary 里强调哪一句**。
    """
    match = _EVENT_PATTERN.search(question)
    event_id = match.group(0) if match else WORKBENCH_REPLAY_EVENT
    text = "\n".join(
        [
            f"城市内涝跨部门事件：{event_id}，时点 {WORKBENCH_REPLAY_AS_OF}，调用方身份 ORG-EMERG。",
            "请使用已挂载的 MCP 工具完成六跳推演：",
            "1) event_deduce 一次返回六跳全部事实、三类冲突、资源缺口与处置顺序；",
            "2) 需要核对依据时用 evidence_locate 取原文，不得凭记忆编造。",
            f"用户这次具体问的是：{question.strip()}",
            "按约定字段输出 JSON，用 final_answer 交卷。",
        ]
    )
    return event_id, text


def _balanced_objects(body: str) -> list[dict[str, Any]]:
    """列出文本里所有平衡的 JSON 对象，按出现顺序。"""
    found: list[dict[str, Any]] = []
    start = body.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for index in range(start, len(body)):
            char = body[index]
            if in_str:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_str = False
                continue
            if char == '"':
                in_str = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        found.append(json.loads(body[start : index + 1]))
                    except json.JSONDecodeError:
                        pass
                    break
        start = body.find("{", start + 1)
    return found


def _unescape_string_literal(text: str) -> str:
    """`final_answer("<json>")` 里给的是字符串字面量，先解一层引号转义。

    不解这一层，扫到的 `{` 全在字符串里，平衡扫描拿不到对象——症状是"明明答了 JSON，
    页面上却只有一段带反斜杠的文字"。
    """
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text
    return value if isinstance(value, str) else text


def extract_agent_json(answer: str) -> dict[str, Any] | None:
    """从回答里找结构化结论（推演 JSON）。找不到返回 None，不抛。

    平台有两种外壳：裸 JSON，以及 `final_answer("...")` 包装（内容还是 JSON 字符串）。
    认两种，且只认带 `six_hops`/`action_order` 的那个，避免把动作历史里的片段当结论。
    """
    raw = answer or ""
    texts: list[str] = []
    stripped = re.sub(r"</?code>", "", raw).strip()
    match = re.search(r"final_answer\((.*)\)\s*$", stripped, re.S)
    if match:
        inner = match.group(1).strip()
        texts.append(inner)
        texts.append(_unescape_string_literal(inner))
    texts.append(stripped)

    def is_result(obj: Any) -> bool:
        return isinstance(obj, dict) and ("six_hops" in obj or "action_order" in obj)

    for text in texts:
        for obj in _balanced_objects(text):
            if is_result(obj):
                return obj
            args = obj.get("arguments") if isinstance(obj, dict) else None
            if isinstance(args, dict) and isinstance(args.get("content"), str):
                for inner_obj in _balanced_objects(args["content"]):
                    if is_result(inner_obj):
                        return inner_obj
    return None


def workbench_chat(body: bytes | None) -> tuple[int, str, bytes]:
    """POST /api/workbench/chat：把一句话转给平台 Agent，回答案与引用清单。"""
    try:
        payload = json.loads((body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error("请求体不是合法 JSON", 400)
    message = str(payload.get("message") or "").strip()
    if not message:
        return _error("message 不能为空", 400)

    agent_key = str(payload.get("agent") or "deduction")
    agent = WORKBENCH_AGENTS.get(agent_key)
    if agent is None:
        return _error(f"未知 agent，可选：{'、'.join(WORKBENCH_AGENTS)}", 400)

    # 推演走协议问法：自由提问包进固定骨架，事实一律由 event_deduce 给。
    # 台账问答保持原话直传（它本来就是自由问答）。
    event_id: str | None = None
    if agent_key == "deduction":
        event_id, message = _deduction_query(message)

    key = os.getenv("NEXENT_API_KEY", "").strip()
    if not key:
        # 不静默降级成"本地假回答"：那会让人以为平台通了，实际没通。
        return _error("未配置 NEXENT_API_KEY（北向访问凭据），工作台无法对话", 503)

    from .nexent_transport import NexentHttpTransport, NexentSettings

    settings = NexentSettings(
        base_url=os.getenv("NEXENT_BASE_URL", "http://localhost:5013").rstrip("/"),
        api_key=key,
        agent_name=agent,
    )
    request: dict[str, Any] = {
        "request_id": f"workbench-{int(time.time() * 1000)}",
    }
    conversation_id = payload.get("conversation_id")
    if conversation_id:
        request["conversation_id"] = int(conversation_id)

    transport = NexentHttpTransport(settings=settings, query_builder=lambda _p: message)
    try:
        result = transport(request)
    except Exception as exc:  # noqa: BLE001 - 平台不通要给可读原因，不能抛 500 堆栈
        return _error(f"平台调用失败：{type(exc).__name__}: {exc}"[:300], 502)

    answer = str(result.get("answer") or "")
    structured = extract_agent_json(answer)
    display = clean_agent_answer(answer)
    if structured and structured.get("summary"):
        # 结构化结论在时，聊天框先显示人话摘要；原始 JSON 由前端折叠，别把一屏 JSON 糊上去
        display = str(structured["summary"])
    # 模型自己也会写 ORG-ROAD / DF-PS-01 这类台账代号，同样换成人话。
    # 只动展示串，`answer` 保持原文——要核对的人仍能拿到模型逐字输出。
    return _json(
        {
            "agent": agent,
            "event_id": event_id,
            "conversation_id": result.get("conversation_id"),
            "answer": answer,
            "answer_text": _humanize(display),
            "structured": structured,
            "labels": _label_maps(),
            "evidence_refs": sorted(set(_REF_PATTERN.findall(answer))),
            "citations": sorted({item.strip() for item in _CITATION_PATTERN.findall(answer)})[:8],
        }
    )


def workbench_locate(body: bytes | None) -> tuple[int, str, bytes]:
    """POST /api/workbench/locate：把一个 evidence_ref 还原成原文。"""
    try:
        payload = json.loads((body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error("请求体不是合法 JSON", 400)
    ref = str(payload.get("evidence_ref") or "").strip()
    if not ref:
        return _error("evidence_ref 不能为空", 400)

    try:
        result = _workbench_toolkit().call(
            "evidence_locate", {"evidence_ref": ref, "caller_labels": [WORKBENCH_CALLER]}
        )
    except Exception as exc:  # noqa: BLE001 - 资产缺失/解析异常都转成可读错误
        return _error(f"取证失败：{type(exc).__name__}: {exc}"[:300], 500)
    if result.status != "ok":
        message = getattr(result.error, "message", None) or "该引用取不到原文"
        return _error(str(message), 404)

    data = result.data
    return _json(
        {
            "evidence_ref": ref,
            "asset": data.get("asset"),
            "locator_kind": data.get("locator_kind"),
            "span": data.get("span"),
            "row": data.get("row"),
            "excerpt": data.get("excerpt"),
        }
    )


def workbench_search(body: bytes | None) -> tuple[int, str, bytes]:
    """POST /api/workbench/search：拿知识库引用当检索词，反查原始切片。

    为什么需要它：台账问答的引用是「文件 · 编号」，不是 asset_id，`evidence_locate`
    取不到；但反查一次就能拿到带 locator 的切片，再点就能看原文，链路不至于断在这。
    """
    try:
        payload = json.loads((body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error("请求体不是合法 JSON", 400)
    query = str(payload.get("query") or "").strip()
    if not query:
        return _error("query 不能为空", 400)

    try:
        result = _workbench_toolkit().call("doc_search", {"query": query, "top_k": 3})
    except Exception as exc:  # noqa: BLE001 - 索引缺失等都要给可读原因
        return _error(f"检索失败：{type(exc).__name__}: {exc}"[:300], 500)
    if result.status != "ok":
        message = getattr(result.error, "message", None) or "检索不可用"
        return _error(str(message), 503)

    hits = [
        {
            "evidence_ref": item.get("evidence_ref"),
            "source_path": item.get("source_path"),
            "heading_path": item.get("heading_path"),
            "score": item.get("score"),
            "excerpt": (item.get("excerpt") or "")[:600],
        }
        for item in (result.data.get("hits") or [])
    ]
    return _json({"query": query, "hits": hits})


def _verify_one(kind: str, text: str) -> dict[str, Any]:
    """核验一条引用：能不能落回原文。取不到就记原因，不吞掉。

    两种引用走两条路：
    - `ref`（`AS-…#locator#line:n`）直接 `evidence_locate`；
    - `citation`（「文件 · 编号」）先 `doc_search` 反查切片拿 locator，再 locate。

    **反查必须校验文件名与锚点，不能直接取 top-1**：`doc_search` 永远会返回最近的切片，
    拿它当"找到原文"会把编造的引用也判成通过（实测「完全不存在的引用 · ZZ-999」被
    匹配到了防汛预案）。所以要求命中的 `source_path` 与引用里写的文件名一致，
    且引用后半段的编号/条款要真的出现在原文里，缺一条就判悬空。
    """
    item: dict[str, Any] = {"kind": kind, "input": text, "ok": False}
    toolkit = _workbench_toolkit()
    try:
        if kind == "citation":
            head, _, anchor = text.partition("·")
            file_hint = head.strip()
            anchor = anchor.strip()

            # 台账类（.csv）不走向量检索，直接查文件：
            # 实测「DF-IN-101」在 drainage_facilities.csv 里明明有这一行，但向量排序把
            # 别的行顶上来，top-5 都挤不进去，于是真引用被判悬空。台账是**结构化底账**，
            # 逐行扫一遍比向量近邻可靠，也不受切片粒度影响。
            if file_hint.endswith(".csv"):
                rows = _ledger_rows(file_hint)
                if not rows:
                    item["reason"] = f"台账文件 {file_hint} 不存在"
                    return item
                row = next(
                    (row for row in rows if anchor and any(anchor in str(v) for v in row.values())),
                    None,
                )
                if row is None:
                    item["reason"] = f"台账 {file_hint} 里没有「{anchor}」，这条引用指错了位置"
                    return item
                item.update(
                    {
                        "ok": True,
                        "source_path": file_hint,
                        "verified_by": "台账直查",
                        "excerpt": "；".join(f"{k}={v}" for k, v in row.items() if v)[:300],
                    }
                )
                return item

            def matches(hit: dict[str, Any]) -> bool:
                source = str(hit.get("source_path") or "")
                return bool(file_hint) and (
                    source == file_hint or source.endswith("/" + file_hint) or file_hint in source
                )

            # 检索词先只给「章节主题」，不带文件名：实测带 `procedure_waterlogging.md`
            # 反而把 work_orders.csv 顶到第一（词袋里扩展名和字段名互相干扰），
            # 而「3 雨水口清掏」能一击命中。找不到再退回到带文件名的查法。
            hits: list[dict[str, Any]] = []
            for query in ([anchor, f"{anchor} {file_hint}"] if anchor else [file_hint]):
                found = toolkit.call("doc_search", {"query": query, "top_k": 5})
                if found.status != "ok":
                    item["reason"] = str(getattr(found.error, "message", None) or "检索不可用")
                    return item
                hits = found.data.get("hits") or []
                if any(matches(hit) for hit in hits):
                    break
            if not hits:
                item["reason"] = "索引里没有匹配切片"
                return item
            chosen = next((hit for hit in hits if matches(hit)), None)
            if chosen is None:
                item["reason"] = f"检索到的切片都不是「{file_hint}」，这条引用与原文对不上"
                return item
            ref = chosen.get("evidence_ref")
            item["resolved_ref"] = ref
            item["score"] = chosen.get("score")
            heading = " ".join(str(part) for part in (chosen.get("heading_path") or []))
            if not ref:
                item["reason"] = "命中的切片没有 locator，回不到原文"
                return item
        else:
            ref = text
            heading = ""

        located = toolkit.call(
            "evidence_locate", {"evidence_ref": ref, "caller_labels": [WORKBENCH_CALLER]}
        )
        if located.status != "ok":
            # 受限资产（部门规程）不越权取正文。这不是"引用造假"，要说清区别。
            error_code = str(getattr(located.error, "code", ""))
            if "PERMISSION" in error_code:
                item["blocked"] = True
                details = getattr(located.error, "details", None) or {}
                needed = details.get("required_labels") or []
                item["reason"] = (
                    f"权限受限：该依据只对 {'、'.join(needed) or '特定身份'} 开放，"
                    f"工作台身份是 {WORKBENCH_CALLER}"
                )
                return item
            item["reason"] = str(getattr(located.error, "message", None) or "取不到原文")
            return item
        data = located.data
        if kind == "citation" and anchor:
            excerpt = str(data.get("excerpt") or "")
            # 锚点有两种合法形态：条目编号（原文里有这个字符串）与章节标题
            # （`plan_flood… · 3 封路判定标准`）。后者不会在正文里重复标题，
            # 只能靠命中切片的 heading_path 认，否则真引用会被误判成悬空（踩过）。
            if anchor not in excerpt and anchor not in heading:
                item["reason"] = f"原文里没有「{anchor}」，这条引用指错了位置"
                return item
        asset = data.get("asset") or {}
        item.update(
            {
                "ok": True,
                "asset_id": asset.get("asset_id"),
                "source_path": asset.get("file_name"),
                "span": data.get("span"),
                "row": data.get("row"),
                "excerpt": (data.get("excerpt") or "")[:300],
            }
        )
    except Exception as exc:  # noqa: BLE001 - 单条失败不能让整次体检中断
        item["reason"] = f"{type(exc).__name__}: {exc}"[:200]
    return item


def workbench_verify(body: bytes | None) -> tuple[int, str, bytes]:
    """POST /api/workbench/verify：把一次回答的引用全部核一遍，给出可取证率。

    这条接口存在的原因：`/locate` 是"点一条看一条"，而"证据链完整"是一句**可证伪的
    断言**——它得能报出"12 条引用 12 条可取证"或"11/12，1 条悬空"。没有这个总数，
    完整性就只是形容词。
    """
    try:
        payload = json.loads((body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error("请求体不是合法 JSON", 400)

    def _clean(values: Any) -> list[str]:
        seen: dict[str, None] = {}
        for value in values or []:
            text = str(value).strip()
            if text:
                seen.setdefault(text, None)
        return list(seen)

    refs = _clean(payload.get("refs"))[:24]
    citations = _clean(payload.get("citations"))[:24]
    if not refs and not citations:
        return _error("没有可核验的引用", 400)

    items = [_verify_one("ref", ref) for ref in refs] + [_verify_one("citation", text) for text in citations]
    passed = sum(1 for item in items if item["ok"])
    blocked = sum(1 for item in items if item.get("blocked"))
    missing = len(items) - passed - blocked
    parts = [f"{passed}/{len(items)} 条引用可取证"]
    if blocked:
        # 权限受限与"引用对不上"是两件事：前者说明依据存在但当前身份看不到，
        # 后者说明这条引用是编的或指错了。混成一个"悬空"会冤枉真引用。
        parts.append(f"{blocked} 条权限受限（依据存在，当前身份看不到）")
    if missing:
        parts.append(f"{missing} 条悬空")
    return _json(
        {
            "total": len(items),
            "ok": passed,
            "blocked": blocked,
            "missing": missing,
            "items": items,
            "verdict": "，".join(parts),
        }
    )


def build_workbench_html() -> str:
    """注入平台对话页地址。平台 Web 与北向不是同一个端口，所以单独取 env。"""
    web = os.getenv("NEXENT_WEB_BASE", "http://localhost:3000").rstrip("/")
    return _WORKBENCH_PAGE.replace("__WEB_BASE__", web)


def build_map2d_html() -> tuple[int, str, bytes]:
    """GET /map2d：2.5D 俯视大屏。

    页面本身由 `scripts/build_map2d.py` 生成（单文件、无依赖、无 three.js），
    产物在 `dist/map2d/`，按仓库规矩不入库。所以这里做两件事：
    产物不存在、或脚本比产物新，就现场跑一遍生成；再读出来返回。
    演示时不必先记得敲命令，改完脚本刷新页面即可。

    脚本在 `scripts/` 下不在包里，按路径加载——不要为了一个生成器把它塞进 src。
    """
    root = Path(__file__).resolve().parents[2]  # src/decision_poc/web_demo.py -> agent_poc
    script = root / "scripts" / "build_map2d.py"
    artifact = root / "dist" / "map2d" / "index.html"
    if not script.is_file():
        return _error("缺少 scripts/build_map2d.py，无法生成 2.5D 大屏", 500)
    stale = (not artifact.is_file()) or artifact.stat().st_mtime < script.stat().st_mtime
    if stale:
        try:
            spec = importlib.util.spec_from_file_location("_build_map2d", script)
            if spec is None or spec.loader is None:
                raise RuntimeError("无法加载生成脚本")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.main()
        except Exception as exc:  # noqa: BLE001 - 生成失败要给可读原因，不能抛 500 堆栈
            # 生成脚本正在被人改到一半时（语法错、数据文件缺失），刷新页面不该 500。
            # 上一版产物还在就照旧返回，顶上挂一条黄条说明"这是旧图"——
            # 悄悄给旧图比报错更糟：看着正常，其实不是最新结论。
            if artifact.is_file():
                note = f"{type(exc).__name__}: {exc}"[:200]
                return 200, "text/html; charset=utf-8", _map2d_with_banner(artifact.read_bytes(), note)
            return _error(f"2.5D 大屏生成失败：{type(exc).__name__}: {exc}"[:300], 500)
    if not artifact.is_file():
        return _error("2.5D 大屏产物缺失", 500)
    return 200, "text/html; charset=utf-8", artifact.read_bytes()


def _map2d_with_banner(html: bytes, note: str) -> bytes:
    """在旧产物顶上插一条"这次没生成成功"的提示。只做展示，不碰图表本身。"""
    safe = (
        str(note)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    style = (
        "position:fixed;left:0;right:0;top:0;z-index:99;background:#fff7e6;"
        "border-bottom:1px solid #f0d9a8;color:#8a5a00;padding:6px 14px;"
        "font:12px/1.6 'Microsoft YaHei',system-ui,sans-serif"
    )
    banner = (
        '<div style="' + style + '">'
        + "这一屏是上一版产物：本次重新生成失败（" + safe + "），图表可能不是最新。"
        + "生成脚本改好后刷新即可。</div>"
    )
    marker = b"<body>"
    if marker in html:
        return html.replace(marker, marker + banner.encode("utf-8"), 1)
    return html


#: 主 Agent 挂的 MCP 工具。真源在 `deploy/nexent/setup_nexent.py` 的 `MAIN_TOOLS`，
#: 那份是发布用的；这里只用于门户展示，改工具集时两处一起改，别让门户写着旧清单。
PORTAL_MAIN_TOOLS: tuple[tuple[str, str], ...] = (
    ("event_deduce", "事件推演"),
    ("evidence_locate", "证据定位"),
    ("doc_search", "预案检索"),
)

#: 门户上的角色名。平台里的 `urban_deduction_agent` 对评委没有意义，
#: 但核对时要能对上平台，所以中文角色与平台 Agent 名同时出现。
PORTAL_AGENT_ROLES: tuple[tuple[str, str], ...] = (
    ("urban_deduction_agent", "主智能体 · 事件推演"),
    ("urban_collab_agent", "协同智能体 · 跨部门编排"),
    ("urban_asset_qa", "台账问答 · 多跳检索"),
)
PORTAL_CONTROL_ROLES: tuple[tuple[str, str], ...] = (
    ("urban_control_no_tools", "对照组 · 主智能体不用工具"),
    ("urban_collab_control_no_tools", "对照组 · 协同不用工具"),
)


def portal_payload() -> dict[str, Any]:
    """GET /api/portal：门户首页要的平台事实。

    刻意**不跑** evidence_check：它要 2 秒（要真跑规则、比对 108 项），首页等不起。
    这里只读快照与期望文件的元信息——测试数与指纹都是文件里的字面值，改一处就变，
    够硬，且毫秒级。108 项的实时比对留给工作台的自检栏。
    """
    snap = selfcheck.platform_versions_payload()
    by_name = {str(item.get("name")): item for item in (snap.get("platform_agents") or [])}

    def row(name: str, role: str, kind: str) -> dict[str, Any]:
        item = by_name.get(name) or {}
        return {
            "role": role,
            "name": name,
            "kind": kind,
            "version": item.get("current_version"),
            "version_name": item.get("version_name"),
            "enabled": bool(item.get("enabled")),
            "found": bool(item),
        }

    audit: dict[str, Any] = {}
    try:
        from .evidence_check import expectations_digest, load_expectations

        expectations = load_expectations()
        audit = {
            "tests": (expectations.get("test_baseline") or {}).get("collected"),
            "fingerprint": expectations_digest(expectations)[:16],
        }
    except Exception:  # noqa: BLE001 - 门户缺这块也要能打开，不能因为自检文件缺失白屏
        audit = {}

    return {
        "platform": {
            "available": bool(snap.get("available")),
            "generated_at": snap.get("generated_at"),
            "source": snap.get("source"),
            "source_detail": snap.get("source_detail"),
            "hint": snap.get("hint"),
        },
        "agents": [row(name, role, "main") for name, role in PORTAL_AGENT_ROLES],
        "controls": [row(name, role, "control") for name, role in PORTAL_CONTROL_ROLES],
        "tools": [{"name": name, "cn": cn} for name, cn in PORTAL_MAIN_TOOLS],
        "audit": audit,
    }


#: 门户首页。**这是作品的入口页**，不是决策演示页（那页在 /demo）。
#: 第一屏要回答评委的三个问题：这是什么、凭什么可信、建在什么平台上。
#: 所以顺序是：一句话定位 → 三条动线 → 在平台上交付了什么（Agent/工具/验收/部署）。
#: 注意：整段是普通 Python 字符串，JS 里别写裸反斜杠转义（会被 Python 先吃掉）。
_PORTAL_PAGE = """<!DOCTYPE html>
<html lang=zh-CN><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>城市内涝跨部门决策 · 可核查推演</title>
<style>
:root{
  --bg:#f5f7fa; --panel:#fff; --line:#e5e9ef; --line2:#d8dee7;
  --ink:#1b2330; --sub:#5b6472; --dim:#8b95a3;
  --blue:#2f6fb5; --teal:#0f766e; --amber:#b45309; --red:#b91c1c; --green:#15803d;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
body{padding:0 0 28px}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
header{display:flex;align-items:flex-start;gap:18px;padding:26px 34px 20px;background:var(--panel);
  border-bottom:1px solid var(--line)}
header .grow{flex:1;min-width:0}
header h1{margin:0;font-size:21px;font-weight:650;letter-spacing:.2px}
header .lead{margin:7px 0 0;color:var(--sub);font-size:13px}
header .lead b{color:var(--ink);font-weight:600}
.plat{flex-shrink:0;text-align:right;font-size:12px;color:var(--dim);line-height:1.5}
.plat .tag{display:inline-block;background:#eef4fb;color:var(--blue);border:1px solid #d5e4f5;
  border-radius:6px;padding:3px 9px;font-size:12px;font-weight:600;margin-bottom:5px}

#statusbar{display:flex;flex-wrap:wrap;align-items:center;gap:10px 22px;padding:11px 34px;
  background:#fbfcfd;border-bottom:1px solid var(--line);font-size:12.5px;color:var(--sub)}
.dot{width:8px;height:8px;border-radius:50%;background:#c3cad3;display:inline-block;margin-right:7px;
  vertical-align:1px}
.dot.on{background:#22a06b;box-shadow:0 0 0 3px rgba(34,160,107,.14)}
.dot.warn{background:#e0a02e;box-shadow:0 0 0 3px rgba(224,160,46,.16)}
#statusbar .ag{font-variant-numeric:tabular-nums}
#statusbar .sep{width:1px;height:12px;background:var(--line2)}
#statusbar .right{margin-left:auto;color:var(--dim);font-family:ui-monospace,Consolas,monospace;font-size:11.5px}

main{padding:24px 34px 0;max-width:1360px}
.cards{display:grid;grid-template-columns:1.5fr 1fr 1fr;gap:16px}
.card{display:flex;flex-direction:column;background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:18px 20px 16px;position:relative;transition:.16s;min-height:200px}
.card:hover{border-color:var(--line2);box-shadow:0 3px 14px rgba(27,35,48,.06);transform:translateY(-1px)}
.card .kicker{font-size:11.5px;letter-spacing:.6px;color:var(--dim);margin-bottom:9px}
.card h2{margin:0 0 8px;font-size:16.5px;font-weight:650}
.card p{margin:0;color:var(--sub);font-size:13px;line-height:1.65}
.card .points{margin:13px 0 0;padding:0;list-style:none;font-size:12.5px;color:var(--sub)}
.card .points li{padding:3px 0 3px 15px;position:relative}
.card .points li::before{content:'';position:absolute;left:0;top:11px;width:5px;height:5px;
  border-radius:50%;background:var(--line2)}
.card .go{margin-top:auto;padding-top:16px;font-size:13px;font-weight:600}
.card.primary{background:linear-gradient(180deg,#fbfdff,#fff);border-left:3px solid var(--teal)}
.card.primary h2{font-size:18px}

.strip{margin-top:14px;background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:16px 20px}
.strip .hd{display:flex;align-items:baseline;gap:10px;margin-bottom:12px}
.strip h3{margin:0;font-size:14px;font-weight:650}
.strip .hd span{font-size:12px;color:var(--dim)}
.jumps{display:flex;flex-wrap:wrap;gap:8px}
.jump{display:inline-flex;align-items:center;gap:7px;background:#f7f9fb;border:1px solid var(--line);
  border-radius:8px;padding:6px 11px;font-size:12.5px;color:var(--ink);cursor:pointer;transition:.15s}
.jump:hover{border-color:#bcd3ea;background:#f2f7fc;color:var(--blue)}
.jump .n{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--dim)}
.objs{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:10px;
  padding-top:10px;border-top:1px dashed #eef1f5}
.objs .lbl{font-size:12.5px;color:var(--sub);margin-right:4px;white-space:nowrap;flex-shrink:0}
.jump.obj{background:#fff}

.facts{margin-top:14px;display:grid;grid-template-columns:1fr 1fr;gap:16px}
.block{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px}
.block h3{margin:0 0 12px;font-size:14px;font-weight:650}
.frow{display:flex;align-items:baseline;gap:10px;font-size:12.5px;padding:7px 0;
  border-bottom:1px dashed #eef1f5}
.frow:last-child{border-bottom:0}
.frow .frole{flex:1;min-width:0}
.frow .fver{font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums;text-align:right}
.frow .fname{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--dim);white-space:nowrap}
.ftag{font-size:10.5px;color:var(--dim);border:1px solid var(--line2);border-radius:4px;
  padding:0 4px;flex-shrink:0}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
.chip{background:#f2f6fa;border:1px solid #e1eaf3;border-radius:6px;padding:2px 8px;font-size:11.5px;color:#3c5a78}
.chip .en{font-family:ui-monospace,Consolas,monospace;font-size:10px;color:#9aa5b1;margin-left:5px}
footer{padding:22px 34px 0;color:var(--dim);font-size:12px;display:flex;flex-wrap:wrap;gap:16px}
@media(max-width:1080px){.cards{grid-template-columns:1fr}.facts{grid-template-columns:1fr}
  header{flex-direction:column}.plat{text-align:left;margin-top:6px}}
</style>
<body>
<header>
  <div class=grow>
    <h1>城市内涝跨部门决策 · 可核查推演</h1>
    <p class=lead>成都兴华路片区（A-03）暴雨内涝。场景按成都真实路网简化，路名与设施编号为示意。
      从工单到处置顺序，每一步的结论都能回到台账的某一行。三个智能体在平台内编排，
      事实由本地规则引擎算出，模型只负责把事实讲成人话。</p>
  </div>
  <div class=plat>
    <span class=tag>Nexent v2.7.0</span><br>
    3 个智能体 · 3 个 MCP 工具<br>
    配置可整包导出 · 离线可运行
  </div>
</header>

<section id=statusbar>
  <span><span id=s-dot class=dot></span><span id=s-state>读取平台状态…</span></span>
  <span class=sep></span>
  <span id=s-agents class=ag>—</span>
  <span class=sep></span>
  <span id=s-tools>—</span>
  <span class=right id=s-audit></span>
</section>

<main>
  <section class=cards>
    <article class="card primary">
      <div class=kicker>主入口 · 一句话问</div>
      <h2>证据工作台</h2>
      <p>问「谁负责」「先做什么」，答案里的每条结论都带原文定位，点开就是台账里那一行。
        事实由本地规则引擎算出，模型只负责复述，所以说的和算的是同一件事。</p>
      <ul class=points>
        <li>六跳推演：雨情定级 → 影响对象 → 责任部门 → 跨部门冲突 → 资源缺口 → 处置动作</li>
        <li>工具轨迹：每次调用的入参、耗时、数据快照指纹</li>
        <li>自检与复跑：八项判据当场重算，与上一轮逐项比</li>
      </ul>
      <div class=go><a href="/workbench">进入工作台 →</a></div>
    </article>

    <article class=card>
      <div class=kicker>态势</div>
      <h2>2.5D 俯视大屏</h2>
      <p>一屏看现场：积水范围与深度、封路、泵站状态、跨区调泵。七拍推演可播放。</p>
      <ul class=points>
        <li>台账尺寸按真实米数，平面布局为示意坐标</li>
        <li>示意路贴合真实道路走向，偏差中位 42 米</li>
        <li>抽排资源分配按冻结案例</li>
      </ul>
      <div class=go><a href="/map2d">打开大屏 →</a></div>
    </article>

    <article class=card>
      <div class=kicker>空间</div>
      <h2>三维沙盘</h2>
      <p>看空间关系：谁挨着谁、封路板挡在哪、施工队从哪进场。可与上面两屏对同一事件交叉核对。</p>
      <ul class=points>
        <li>六跳逐步驱动场景状态</li>
        <li>可深链到具体跳次与对象</li>
      </ul>
      <div class=go><a id=go-twin href="/twin3d">在沙盘查看 →</a></div>
    </article>
  </section>

  <section class=strip>
    <div class=hd><h3>沙盘关键画面</h3><span id=strip-note>读取跳次…</span></div>
    <div class=jumps id=jumps></div>
    <div class=objs id=objs></div>
  </section>

  <section class=facts>
    <div class=block>
      <h3>在平台上交付了什么</h3>
      <div id=f-agents></div>
    </div>
    <div class=block>
      <h3>工具与部署</h3>
      <div id=f-tools></div>
      <div class=chips id=f-chips></div>
    </div>
  </section>
</main>

<footer>
  <span>决策推理面板 <a href="/demo">/demo</a></span>
  <span>自检明细 <a href="/status">/status</a></span>
  <span>期望文件与台账：<span class=mono>samples/eval/</span></span>
  <span style="margin-left:auto">示意图布局，非测绘坐标；路名与设施编号为示意</span>
</footer>

<script>
var LINKS = {jumps: [], event_id: '', as_of: ''};

function esc(s){
  return String(s==null?'':s).replace(/[&<>"']/g, function(c){
    return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
  });
}
// 本机时区。快照里的时间是 UTC（+0000），直接截串会差 8 小时。
function clockText(iso){
  if(!iso){ return '—'; }
  var d = new Date(iso);
  if(isNaN(d.getTime())){ return String(iso); }
  var p = function(n){ return (n<10?'0':'')+n; };
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+
         p(d.getHours())+':'+p(d.getMinutes());
}

// 沙盘深链：跳次与对象都从 /api/twin-links 来，前端不自己数。
function jumpLink(jump, entity){
  var href = '/twin3d?jump=' + encodeURIComponent(jump);
  if(entity){ href += '&entity=' + encodeURIComponent(entity); }
  return href;
}
function entityJump(id){
  var list = LINKS.jumps || [];
  for(var i=0;i<list.length;i++){
    if((list[i].entity_ids||[]).indexOf(id) >= 0){ return jumpLink(list[i].jump, id); }
  }
  return jumpLink(1, id);
}

function renderStatus(d){
  var p = d.platform || {};
  var dot = document.getElementById('s-dot');
  var state = document.getElementById('s-state');
  if(p.available){
    dot.className = 'dot on';
    state.textContent = '平台快照可用 · ' + clockText(p.generated_at) + ' 生成';
  }else{
    dot.className = 'dot warn';
    state.textContent = '暂无平台快照（智能体版本读不到）';
  }
  var agents = d.agents || [];
  var found = agents.filter(function(a){ return a.found; }).length;
  document.getElementById('s-agents').textContent =
    '智能体 ' + found + '/' + agents.length + ' 已发布' +
    (agents.length ? '（主智能体第 ' + (agents[0].version==null?'?':agents[0].version) + ' 版）' : '');
  var tools = d.tools || [];
  document.getElementById('s-tools').textContent = 'MCP 工具 ' + tools.length + ' 个';
  var a = d.audit || {};
  var bits = [];
  if(a.tests){ bits.push('冻结测试 ' + a.tests + ' 项'); }
  if(a.fingerprint){ bits.push('期望指纹 ' + a.fingerprint); }
  document.getElementById('s-audit').textContent = bits.join(' · ');

  // 一条一行：角色（左，可伸缩） / 第几版 / 平台版本名 / 对照标记。
  // 原来用 dt/dd 两列，左边反复写"Agent"三个字，既占地方又没信息。
  function factsRow(host, role, ver, name, isControl, missing){
    var row = document.createElement('div');
    row.className = 'frow';
    var a = document.createElement('span');
    a.className = 'frole';
    a.textContent = role;
    if(missing){
      var warn = document.createElement('span');
      warn.style.color = '#b45309';
      warn.textContent = '（快照里没有这个智能体）';
      a.appendChild(warn);
    }
    row.appendChild(a);
    if(isControl){
      var tag = document.createElement('span');
      tag.className = 'ftag';
      tag.textContent = '对照';
      row.appendChild(tag);
    }
    var b = document.createElement('span');
    b.className = 'fver';
    b.textContent = ver;
    row.appendChild(b);
    if(name){
      var c = document.createElement('span');
      c.className = 'fname';
      c.textContent = name;
      row.appendChild(c);
    }
    host.appendChild(row);
  }

  var host = document.getElementById('f-agents');
  host.innerHTML = '';
  agents.concat(d.controls||[]).forEach(function(x){
    var ver = (x.version==null) ? '未读到' : ('第 ' + x.version + ' 版');
    factsRow(host, x.role, ver, x.version_name || '', x.kind === 'control', !x.found);
  });

  var th = document.getElementById('f-tools');
  th.innerHTML = '';
  factsRow(th, 'MCP 工具',
    (tools.length ? tools.length + ' 个，主智能体全挂载' : '未读到'), '', false, false);
  factsRow(th, '推理分工', '规则引擎算事实，模型只复述', '', false, false);
  factsRow(th, '离线运行', '12 个容器，不依赖外网', '', false, false);
  factsRow(th, '配置导出', 'agent.json + skills 整包', '', false, false);

  var chips = document.getElementById('f-chips');
  chips.innerHTML = '';
  tools.forEach(function(t){
    var c = document.createElement('span');
    c.className = 'chip';
    c.title = '平台上的工具名：' + t.name;
    c.innerHTML = esc(t.cn) + '<span class="en">' + esc(t.name) + '</span>';
    chips.appendChild(c);
  });
}

function renderJumps(d){
  LINKS = d || {jumps: []};
  var list = (d && d.jumps) || [];
  var host = document.getElementById('jumps');
  host.innerHTML = '';
  document.getElementById('strip-note').textContent =
    (d && d.event_id ? (d.event_id + ' · ' + list.length + ' 跳') : '未读到跳次');

  list.forEach(function(j){
    var b = document.createElement('button');
    b.className = 'jump';
    b.innerHTML = '<span class=n>' + j.jump + '</span>' + esc(j.title || j.phase || '');
    b.title = (j.detail || '') + '（' + (j.ref_count||0) + ' 条依据）';
    b.onclick = function(){ window.open(jumpLink(j.jump), '_blank', 'noopener'); };
    host.appendChild(b);
  });

  // 三个常被追问的对象：泵站、下穿隧道、道路。跳次由 entityJump 反查（谁先出现就跳到那一跳），
  // 不在这里写死跳次号——否则跳次定义一改，链接就全指错地方。
  var objs = document.getElementById('objs');
  objs.innerHTML = '';
  var lbl = document.createElement('span');
  lbl.className = 'lbl';
  lbl.textContent = '常用对象';
  objs.appendChild(lbl);
  [['DF-PS-01','兴华路泵站'], ['UP-XH-101','兴华路下穿隧道'], ['R-101','兴华路']].forEach(function(pair){
    var b = document.createElement('button');
    b.className = 'jump obj';
    b.textContent = pair[1];
    b.title = '打开三维沙盘并定位到 ' + pair[1] + '（' + pair[0] + '）';
    b.onclick = function(){ window.open(entityJump(pair[0]), '_blank', 'noopener'); };
    objs.appendChild(b);
  });

  var goTwin = document.getElementById('go-twin');
  if(goTwin){
    var first = list.length ? list[0].jump : 1;
    goTwin.setAttribute('href', jumpLink(first));
  }
}

fetch('/api/portal').then(function(r){ return r.json(); }).then(renderStatus)
  .catch(function(){ document.getElementById('s-state').textContent = '平台状态读取失败'; });
fetch('/api/twin-links').then(function(r){ return r.json(); }).then(renderJumps)
  .catch(function(){ document.getElementById('strip-note').textContent = '跳次读取失败'; });
</script>
</body></html>
"""


def build_portal_html() -> str:
    """门户首页。纯静态外壳，状态与跳次都由接口填——页面本身不碰数据。"""
    return _PORTAL_PAGE


def workbench_status() -> tuple[int, str, bytes]:
    """GET /api/workbench/status：复用 /status 的判据，不另写一套。

    额外回带平台版本快照的生成时间：那几项是**读本地快照**，不是实时探活。
    页面必须把这件事说出来，否则"平台 Agent 可读 通过"会被读成"平台现在活着"。
    """
    from .selfcheck import platform_versions_payload

    snapshot = platform_versions_payload()
    return _json(
        {
            "checks": status_checks(),
            "platform_snapshot": {
                "available": bool(snapshot.get("available")),
                "generated_at": snapshot.get("generated_at"),
                "source": snapshot.get("source"),
                "agents": len(snapshot.get("platform_agents") or []),
            },
        }
    )


def _feedback_submit(body: bytes | None) -> tuple[int, str, bytes]:
    """POST /feedback：校验 → 判定 → 建议动作 →（必要时）跑一轮演化 → 记一条流水。"""
    from datetime import datetime, timezone

    try:
        payload = json.loads((body or b"{}").decode("utf-8"))
        feedback = FloodFeedback.model_validate(payload)
        record = to_feedback_record(feedback)
        suggestion = feedback_plan(feedback)
        evolution = run_evolution(feedback)
        memory = evolution.get("memory") or {}
        review_block = evolution.get("reviews") or {}
        review_items = review_block.get("items") or []
        first_review = review_items[0] if review_items else {}
        run = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "feedback_id": record.request_id,
            "scenario_id": feedback.scenario_id,
            "district": feedback.district,
            "depth_cm": feedback.water_depth_cm,
            "source": str(feedback.source),
            "outcome": str(record.outcome),
            "dimension": suggestion["dimension"],
            "triggered": bool(evolution.get("triggered")),
            "final_label": evolution.get("final_label"),
            "train_drop": (evolution.get("train") or {}).get("drop"),
            "holdout_drop": (evolution.get("holdout") or {}).get("drop"),
            "promoted_rounds": evolution.get("promoted_rounds"),
            # 记忆命中：这一轮是从"上次学到的策略"起步，还是从 v1.0 重跑
            "memory_hit": bool(memory.get("hit")),
            "memory_label": memory.get("hit_label"),
            "memory_entries": memory.get("entries_after"),
            # 专家评审（第二进化信号）：v1.0 时违反专家判断的程度 → 最终策略与专家的一致程度
            "review_cases": review_block.get("gate_cases"),
            "review_before": first_review.get("deviation_before"),
            "review_after": first_review.get("deviation_after"),
        }
        _append_feedback_run(run)
        result = {
            "feedback_id": record.request_id,
            "outcome": str(record.outcome),
            "note": record.note,
            "plan": suggestion,
            "evolution": evolution,
            "record": run,
            # 这条反馈进没进平台台账——只给判定与提示，同步动作仍要人工执行
            "ledger": _ledger_freshness(),
        }
        return 200, "application/json; charset=utf-8", json.dumps(
            result, ensure_ascii=False
        ).encode("utf-8")
    except Exception as exc:  # 校验失败也返回 JSON，前端只负责显示
        return 400, "application/json; charset=utf-8", json.dumps(
            {"error": f"{type(exc).__name__}: {exc}"[:500]}, ensure_ascii=False
        ).encode("utf-8")


def route(path: str, method: str = "GET", body: bytes | None = None) -> tuple[int, str, bytes]:
    """把一次 HTTP 请求映射成 (status, content_type, body)。

    纯函数：测试直接调它，不起 socket、不占端口。
    """
    parsed = urlparse(path)
    segments = [item for item in parsed.path.split("/") if item]
    query = parse_qs(parsed.query)

    if parsed.path in {"", "/"}:
        # 首页是门户：一屏说清"这是什么、凭什么可信、建在什么平台上"。
        # 旧的面板页留在 /demo —— 页面本身没删，只是不再是第一眼看到的东西。
        return 200, "text/html; charset=utf-8", build_portal_html().encode("utf-8")
    if parsed.path == "/demo":
        return 200, "text/html; charset=utf-8", build_html().encode("utf-8")
    if parsed.path == "/api/portal":
        return _json(portal_payload())
    if parsed.path == "/twin3d":
        return 200, "text/html; charset=utf-8", build_twin3d_html().encode("utf-8")
    if parsed.path == "/feedback":
        if method == "POST":
            return _feedback_submit(body)
        return 200, "text/html; charset=utf-8", build_feedback_html().encode("utf-8")
    if parsed.path == "/review":
        return 200, "text/html; charset=utf-8", build_review_html().encode("utf-8")
    if parsed.path == "/status":
        return 200, "text/html; charset=utf-8", build_status_html().encode("utf-8")
    if parsed.path == "/workbench":
        return 200, "text/html; charset=utf-8", build_workbench_html().encode("utf-8")
    if parsed.path == "/map2d":
        return build_map2d_html()
    if segments[:3] == ["api", "workbench", "events"]:
        return workbench_events()
    if segments[:3] == ["api", "workbench", "event"]:
        return workbench_event(body)
    if segments[:3] == ["api", "workbench", "replay"]:
        if method.upper() == "POST":
            return workbench_replay(body)
        return workbench_replay_history()
    if segments[:3] == ["api", "workbench", "chat"]:
        if method.upper() != "POST":
            return _error("只接受 POST", 405)
        return workbench_chat(body)
    if segments[:3] == ["api", "workbench", "locate"]:
        if method.upper() != "POST":
            return _error("只接受 POST", 405)
        return workbench_locate(body)
    if segments[:3] == ["api", "workbench", "status"]:
        return workbench_status()
    if segments[:3] == ["api", "workbench", "search"]:
        if method.upper() != "POST":
            return _error("只接受 POST", 400)
        return workbench_search(body)
    if segments[:3] == ["api", "workbench", "verify"]:
        if method.upper() != "POST":
            return _error("只接受 POST", 400)
        return workbench_verify(body)
    if parsed.path == "/feedback/history":
        return 200, "application/json; charset=utf-8", json.dumps(
            {"runs": read_feedback_runs(limit=30)}, ensure_ascii=False
        ).encode("utf-8")
    if parsed.path == "/three.min.js":
        three = _assets_dir() / "three.min.js"
        if not three.exists():
            return _error("three.min.js missing (assets/three.min.js)", 404)
        return 200, "application/javascript; charset=utf-8", three.read_bytes()
    # 三维写实沙盘的静态依赖（RGBELoader/OrbitControls）与 PBR 贴图/HDR，全部本地分发、断网可用
    if parsed.path in {"/RGBELoader.js", "/OrbitControls.js", "/GLTFLoader.js"}:
        f = _assets_dir() / parsed.path[1:]
        if not f.exists():
            return _error(f"{parsed.path} missing", 404)
        return 200, "application/javascript; charset=utf-8", f.read_bytes()
    if parsed.path.startswith("/assets/models/"):
        # 外部导入的模型（如 Tripo 导出的作业人员 worker_rig.glb）。文件不存在就 404，
        # 前端 GLTFLoader 的 error 回调会保留几何小人兜底，功能不受影响。
        name = parsed.path.rsplit("/", 1)[1]
        f = _assets_dir() / "models" / name
        if not f.exists():
            return _error("model asset missing", 404)
        if name.endswith(".glb"):
            return 200, "model/gltf-binary", f.read_bytes()
        if name.endswith(".gltf"):
            return 200, "model/gltf+json; charset=utf-8", f.read_bytes()
        if name.endswith(".bin"):
            return 200, "application/octet-stream", f.read_bytes()
        if name.endswith(".png"):
            return 200, "image/png", f.read_bytes()
        if name.endswith(".json"):
            # 关键帧动画数据（worker_anim.json）：UAL 重定向后烘出的四元数轨道
            return 200, "application/json; charset=utf-8", f.read_bytes()
        return 200, "application/octet-stream", f.read_bytes()
    if parsed.path.startswith("/assets/kaykit/"):
        # KayKit 城市资产：gltf(JSON) / bin(几何) / png(纹理图集)，CC0，本地分发
        name = parsed.path.rsplit("/", 1)[1]
        f = _assets_dir() / "kaykit" / name
        if not f.exists():
            return _error("kaykit asset missing", 404)
        if name.endswith(".gltf"):
            return 200, "model/gltf+json; charset=utf-8", f.read_bytes()
        if name.endswith(".bin"):
            return 200, "application/octet-stream", f.read_bytes()
        if name.endswith(".png"):
            return 200, "image/png", f.read_bytes()
        return 200, "application/octet-stream", f.read_bytes()
    if parsed.path.startswith("/assets/tex/") and parsed.path.endswith(".jpg"):
        f = _assets_dir() / "tex" / parsed.path.rsplit("/", 1)[1]
        if not f.exists():
            return _error("texture missing", 404)
        return 200, "image/jpeg", f.read_bytes()
    if parsed.path.startswith("/assets/hdr/") and parsed.path.endswith(".hdr"):
        f = _assets_dir() / "hdr" / parsed.path.rsplit("/", 1)[1]
        if not f.exists():
            return _error("hdr missing", 404)
        return 200, "application/octet-stream", f.read_bytes()
    if parsed.path == "/evolution":
        return 200, "text/html; charset=utf-8", build_evolution_html().encode("utf-8")
    if segments[:3] == ["api", "platform", "versions"]:
        return _json(platform_versions_payload())
    if segments[:3] == ["api", "ledger", "sync"]:
        return _json(ledger_sync_payload())
    if segments[:3] == ["api", "evolution", "gate"]:
        limit = int((query.get("limit") or ["10"])[0])
        return _json(evolution_gate_history(limit))
    if segments[:2] == ["api", "evolution"]:
        return _json(build_evolution_payload())
    if segments[:2] == ["api", "reviews"]:
        if method.upper() == "POST":
            try:
                payload = json.loads((body or b"{}").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _error("请求体不是合法 JSON", 400)
            try:
                return _json(submit_review(payload), 201)
            except (ValueError, TypeError) as exc:
                return _error(f"评分提交被拒：{exc}", 400)
        return _json(review_payload())
    if segments[:2] == ["api", "demo"]:
        try:
            return _json(build_demo_payload(scenario=(query.get("scenario") or [""])[0]))
        except KeyError:
            known = "、".join(sorted(SCENARIO_ROOTS)) or "（无）"
            return _error(f"未知情景，可选：{known}", 400)

    if segments[:2] == ["api", "weather-replay"]:
        payload = weather_replay()
        return _json(payload, 200 if "error" not in payload else 503)

    if segments[:2] == ["api", "quant-references"]:
        payload = quant_references()
        return _json(payload, 200 if "error" not in payload else 503)

    if segments[:2] == ["api", "timeline"]:
        # 决策事件流：从 /api/demo 同一份 payload 派生，只排顺序不重算。
        return _json(build_timeline(build_demo_payload()))

    if segments[:2] == ["api", "scheduler"]:        # 微调度台总入口：一次返回链路 / 六类事件流 / 工单 / 资源 / 场景 / 方案。
        view = scheduler_view(refresh=(query.get("refresh") or ["0"])[0] in {"1", "true"})
        return _json(
            {
                "event_id": view["payload"].get("event_id") or EVENT_ID,
                "as_of": view["payload"].get("as_of") or AS_OF,
                "chain": view["chain"],
                "event_stream": view["stream"],
                "work_orders": view["orders"],
                "resources": view["resources"],
                "departments": view["departments"],
                "involved_orgs": view["involved_orgs"],
                "scene": view["scene"],
                "plan": view["plan"],
                "baseline": view["baseline"],
                "publishable": bool(view["payload"].get("publishable")),
            }
        )

    if segments[:2] == ["api", "twin-links"]:
        # 沙盘的关联层：对象 ↔ 工单 ↔ 动作卡 ↔ 证据，外加六跳索引与处置前后对照。
        # `jump` 决定动作卡的推演状态（1..6 六跳，7=处置后）；`entity` 只是回显，
        # 由页面判断是否命中——404 会把"参数写错"和"对象不存在"搅成同一个现象。
        jump, entity = resolve_deeplink(query)
        links = build_twin_links(scheduler_view(), current_jump=jump)
        links["requested_entity"] = (
            {"id": entity, "found": entity in links["entities"]} if entity else None
        )
        return _json(links)

    if segments[:2] == ["api", "execution"]:
        # 执行账本：GET 看计划/实际对照，POST 才写。写入口没有鉴权（PoC 无登录系统），
        # 但 actor 由服务端固定，前端传了也不认。
        if segments[2:3] == ["events"]:
            if method.upper() != "POST":
                return _error("执行事件只接受 POST", 405)
            try:
                payload = json.loads((body or b"{}").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _error("请求体不是合法 JSON", 400)
            if not isinstance(payload, dict):
                return _error("请求体必须是 JSON 对象", 400)
            try:
                result = execution_ledger.append_event(payload, cards=execution_cards())
            except execution_ledger.LedgerError as exc:
                return _json({"status": "error", "code": exc.code, "error": str(exc), "detail": exc.detail}, exc.status)
            return _json(result, 201 if result.get("status") != "duplicate" else 200)
        if segments[2:3] == ["export"]:
            if method.upper() != "POST":
                return _error("导出快照请用 POST", 405)
            path = execution_ledger.export_snapshot(cards=execution_cards())
            return _json({"status": "ok", "path": str(path)})
        if method.upper() != "GET":
            return _error("执行账本读接口只接受 GET", 405)
        return _json(execution_view())

    # 下面四个是"只取一块"的细粒度端点：外部脚本与测试用，省一次整包传输。
    if segments[:2] == ["api", "work-orders"]:
        return _json({"entries": scheduler_view()["orders"]})

    if segments[:2] == ["api", "resources"]:
        return _json(scheduler_view()["resources"])

    if segments[:2] == ["api", "plan-alternatives"]:
        return _json(scheduler_view()["plan"])

    if segments[:2] == ["api", "scene-entities"]:
        return _json(scheduler_view()["scene"])

    if segments[:2] == ["api", "event-stream"]:
        return _json(scheduler_view()["stream"])

    if segments[:2] == ["api", "baseline-compare"]:
        return _json(scheduler_view()["baseline"])

    if segments[:2] == ["api", "interventions"]:
        if method.upper() == "POST":
            try:
                payload = json.loads((body or b"{}").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _error("请求体不是合法 JSON", 400)
            if not isinstance(payload, dict):
                return _error("请求体必须是 JSON 对象", 400)
            intervention_id = str(payload.get("id") or "").strip()
            if not intervention_id:
                return _error("缺少 id：先 GET 本端点取可选干预清单", 400)
            try:
                return _json(run_intervention(intervention_id))
            except KeyError as exc:
                # KeyError 的 str() 自带引号，直接拼进文案会出现两层引号
                message = exc.args[0] if exc.args else str(exc)
                return _error(f"干预项不可用：{message}", 404)
        return _json({"entries": intervention_catalog()})

    if segments[:2] == ["api", "collab-review"]:
        # 多智能体协同复核：只读同一份推演，产出部门动作卡与交接，不动冻结数字。
        return _json(collab_review())

    if segments[:2] == ["api", "llm-decision"]:
        # 可选端点：配置了 DeepSeek 才会真调模型，否则返回 configured=false 优雅降级。
        return _json(web_llm.llm_decision_payload())

    if segments[:2] == ["api", "platform-evidence"]:
        # Nexent 平台真机验证证据（发布时固化的 JSON，只读）。
        evidence_path = (
            Path(__file__).resolve().parents[2]
            / "samples" / "nexent" / "live" / "llm_decision_platform.json"
        )
        if not evidence_path.is_file():
            return _json({"available": False})
        try:
            return _json({"available": True, "record": json.loads(evidence_path.read_text(encoding="utf-8"))})
        except (OSError, json.JSONDecodeError) as exc:
            return _error(f"平台证据读取失败: {exc}", 500)

    if segments[:2] == ["api", "tool-log"]:
        limit = int((query.get("limit") or ["30"])[0])
        return _json({"entries": visualize.read_tool_log(limit)})

    if segments[:2] == ["api", "render"]:
        return _json(render_snapshot((query.get("policy") or ["v1"])[0]))

    if segments[:2] == ["api", "scene"]:
        tail = segments[2:]
        if not tail:
            return _error("缺少 snapshot_id")
        if tail[0] == "latest":
            try:
                return _json(scene_job(None))
            except KeyError as exc:
                return _error(str(exc))
        snapshot_id = tail[0]
        if len(tail) >= 2 and tail[1] == "ack":
            if method.upper() not in {"GET", "POST"}:
                return _error("ack 只接受 GET/POST", 405)
            try:
                visualize.write_ack(snapshot_id, engine="unreal")
            except OSError as exc:
                return _error(f"ack 写入失败: {exc}", 500)
            return _json({"status": "acked", "snapshot_id": snapshot_id})
        try:
            return _json(scene_job(snapshot_id))
        except KeyError:
            return _error(f"未知渲染作业: {snapshot_id}")

    if segments[:2] == ["api", "compare"]:
        baseline_id = (query.get("baseline") or [""])[0]
        candidate_id = (query.get("candidate") or [""])[0]
        if not baseline_id or not candidate_id:
            return _error("compare 需要 baseline 与 candidate 两个 id", 400)
        try:
            return _json(compare_scenes(baseline_id, candidate_id))
        except KeyError as exc:
            return _error(str(exc))

    return _error(f"未知路由: {parsed.path}")


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            status, content_type, payload = route(self.path, method, body)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            self._respond("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._respond("POST")

        def log_message(self, *_args: Any) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()



if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="urban twin browser demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    serve(args.host, args.port)


__all__ = [
    "AS_OF",
    "EVENT_ID",
    "EVOLVED_POLICY",
    "build_demo_payload",
    "build_html",
    "compare_scenes",
    "evolution_after_review",
    "evolution_dir",
    "evolution_gate_history",
    "render_snapshot",
    "review_payload",
    "review_signals",
    "route",
    "save_evolution_gate",
    "scene_job",
    "serve",
    "submit_review",
]
