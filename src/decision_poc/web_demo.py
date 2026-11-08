"""Dependency-free demo payload and browser server for the urban twin.

除推演与进化对比两个页面外，这里还承担两件事：

1. **UE 侧的取数端点**：`/api/scene/<snapshot_id>` 返回渲染作业，`/ack` 让引擎写回认领
   状态——`scene_render` 工具返回的 `delivery` 读的就是这个 ack 文件；
2. **工具调用日志**：`/api/tool-log` 输出 MCP 的 jsonl 调用记录，页面上并排滚动，评委
   要看得出"智能体调了渲染"还是"人点了按钮"，所以每条带 `origin`（mcp / manual）。
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from . import baseline_compare
from . import chain as scheduler
from . import review_collect as reviews
from . import visualize
from . import web_llm
from .collab import orchestrate
from .counterfactual import Intervention, InterventionKind, rerun_with_interventions
from .deduction import DeductionEngine, DistrictSignal
from .evolution_demo import build_evolution_html, build_evolution_payload
from .road_graph import RoadGraph, RoadGraphUnavailable
from .skills import SkillPipeline, SkillRuntime
from .semantic import build_urban_semantic_store
from .timeline import build_event_stream, build_timeline, compare_branches

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
.ent-item small{display:block;font-size:12px;color:var(--impact-muted);margin-top:2px}
.ent-item .ent-kind{margin-left:auto;flex:0 0 auto;font-size:11px;padding:3px 8px;border-radius:999px;background:#eef2f4;color:#5a6b6d}
.ent-item:focus-visible{outline:2px solid var(--impact-coral);outline-offset:2px}
@media(max-width:800px){.scene-detail-grid{grid-template-columns:1fr}.impact-flow{display:flex;flex-wrap:wrap}.impact-flow span{flex:1 1 28%}.impact-flow b{flex:0 0 auto}}</style>'''
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
<style>#flood-scene{position:relative;border:1px solid #263a55;border-radius:12px;background:#0d1a2a;overflow:hidden;min-height:300px}#flood-map{display:block;width:100%;height:100%;min-height:300px;background:#0d1a2a}#flood-rain{position:absolute;inset:0;z-index:2;pointer-events:none;opacity:0;transition:opacity .5s;background-image:repeating-linear-gradient(115deg,rgba(125,211,252,.32) 0 1px,transparent 1px 30px);animation:fmRain .45s linear infinite}#flood-rain.on{opacity:1}@keyframes fmRain{to{background-position:30px 52px}}.fm-zone{fill:rgba(47,74,107,.20);stroke:#3f5a75;stroke-dasharray:6 5;stroke-width:1.5}.fm-road{stroke:#54708a;stroke-width:5;stroke-linecap:round;fill:none;transition:stroke .3s}.fm-road-closed{stroke:#8a6534}.fm-node{stroke:#f4f8fb;stroke-width:1.5;transition:fill .2s}.fm-node-in{fill:#d9a53a}.fm-node-ps{fill:#38bdf8}.fm-water{fill:#38bdf8;fill-opacity:.32;stroke:#7dd3fc;stroke-width:1.2;transition:fill-opacity .5s}.fm-lbl{fill:#c3d3e2;font-size:10.5px;pointer-events:none;paint-order:stroke;stroke:#0d1a2a;stroke-width:3px;stroke-linejoin:round}.fm-evlbl{fill:#e0f2fe;font-size:11.5px;font-weight:700;pointer-events:none;paint-order:stroke;stroke:#0d1a2a;stroke-width:3.5px;stroke-linejoin:round}.fx-event{animation:fmPulse 1.1s ease-in-out infinite}.fx-affected{stroke:#7dd3fc;stroke-width:2.5;filter:drop-shadow(0 0 5px rgba(125,211,252,.9))}.fx-owner{stroke:#34d399;stroke-width:3;filter:drop-shadow(0 0 4px rgba(52,211,153,.8))}.fx-conflict{stroke:#fbbf24;stroke-width:3;animation:fmPulse .85s ease-in-out infinite}.fx-fault{fill:#ef4444;stroke:#fecaca;animation:fmFault .65s steps(2,end) infinite}.fx-closed{stroke:#fb923c;stroke-width:7;stroke-dasharray:14 8;animation:fmPulse 1.2s ease-in-out infinite}@keyframes fmPulse{50%{opacity:.4}}@keyframes fmFault{50%{opacity:.25}}.flood-caption{position:absolute;left:12px;top:12px;z-index:3;max-width:76%;padding:9px 13px;border-radius:9px;background:rgba(7,18,28,.68);pointer-events:none}.flood-caption b{display:block;font-size:15px;color:#e0f2fe;line-height:1.35}.flood-caption span{display:block;margin-top:3px;font-size:12.5px;line-height:1.5;color:#b9cbd9}.flood-readout{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px;font-size:13px;color:#b8c7d9}.flood-readout b{color:#7dd3fc;font-size:18px}.flood-story{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:10px 0 0}.flood-cause{padding:9px 10px;border:1px solid #29425d;border-radius:8px;background:#101d30;color:#b8c7d9;font-size:12px;line-height:1.45}.flood-cause b{display:block;color:#bae6fd;font-size:13px}.flood-cause.on{border-color:#38bdf8;background:#14334a;box-shadow:inset 3px 0 #38bdf8}@media(max-width:700px){.flood-story{grid-template-columns:1fr}}</style><main><header class=page-hero><div><div class=eyebrow>URBAN TWIN · 成都内涝推演</div><h1>城市内涝跨部门事件推演</h1><div class=sub>从一张积水工单，到一套可执行的跨部门调度方案。</div></div><a class=navlink href='/evolution'>查看决策进化对比 →</a></header>
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
  li.innerHTML='<div class=hd><span class=seq>'+String(e.seq).padStart(2,'0')+'</span><span class="ttl k-'+esc(e.kind)+'">'+esc(e.title)+'</span><span class=tag>'+esc(catLabel(e.category))+'</span><span class=tag>'+esc(zh(EVST,e.status))+'</span></div><div class=dsc>'+esc(e.detail)+'</div><div class=mt>取自推演输出 '+esc(e.source)+' · 证据 '+esc(e.ref_count)+' 条</div>'+basis;
  timeline.appendChild(li)})}
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
let SCHED=null;
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
  '<details class=scene-evidence><summary>查看判断依据</summary><div class=evidence>'+esc(evidence)+'</div></details>'}
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
loadPlatform();loadLog();loadStream();loadInterventions();loadCollab();run();try{if(localStorage.getItem('urban-proj')==='1'||new URLSearchParams(location.search).get('proj')==='1')toggleProjection()}catch(e){}loadScheduler().then(function(){chainAll(true)});</script></html>""".replace("</style>\n<main>", "</style>"+IMPACT_STYLE+"\n<main>").replace("</style><main>", "</style>"+IMPACT_STYLE+"<main>").replace("</div></details></main>\n<script>", "</div></details>"+PAGE_STYLE+"</main>\n<script>")


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
#hud{position:fixed;left:18px;top:16px;z-index:5;pointer-events:none}
#hud b{display:block;font-size:18px;color:#e8f3fb;letter-spacing:.5px}
#hud span{display:block;margin-top:5px;font-size:12.5px;color:#93a7bb}
#legend{position:fixed;left:18px;bottom:16px;z-index:5;pointer-events:none;font-size:12px;color:#93a7bb;line-height:1.9}
#legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
#tip{position:fixed;right:18px;bottom:16px;z-index:5;font-size:12px;color:#64788c;pointer-events:none}
canvas{display:block}</style>
<div id=hud><b>城市内涝三维数字孪生 · 写实沙盘</b><span>兴华路片区(A-03) 与 建设大道片区(B-01) · 坐标来自 scene.json 台账实时投影</span></div>
<div id=legend><span><i style=background:#4a6fa5></i>积水段</span> &nbsp; <span><i style=background:#e0a965></i>已封路</span> &nbsp; <span><i style=background:#5a6a7a></i>泵站</span></div>
<div id=tip>拖动旋转 · 滚轮缩放 · 右键平移</div>
<script src="/three.min.js"></script>
<script src="/RGBELoader.js"></script>
<script src="/OrbitControls.js"></script>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let renderer,scene3,camera,controls,envTex=null;
let rain=null,rainGeo=null;
const K=0.10,CX=350,CY=165;
function W(x,y){return[(x-CX)*K,(y-CY)*K]}
// ---- 材质加载：一次加载、repeat 平铺、缓存复用 ----
const texCache={};
function loadTex(name){if(texCache[name])return texCache[name];
  const t=new THREE.TextureLoader().load('/assets/tex/'+name+'.jpg');
  t.wrapS=t.wrapT=THREE.RepeatWrapping;t.encoding=THREE.sRGBEncoding;return texCache[name]=t}
function stdMat(name,extra){const m=new THREE.MeshStandardMaterial(Object.assign({map:loadTex(name+'_diffuse'),roughnessMap:loadTex(name+'_rough'),normalMap:loadTex(name+'_normal'),roughness:.95,metalness:.02},extra||{}));return m}
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
  const g=new THREE.BoxGeometry(w,h,d);
  // 程序化立面：每栋楼独立材质，按楼面尺寸平铺窗户格子（写实窗户肌理）
  const ft=facadeTex().clone();ft.needsUpdate=true;
  const mat=new THREE.MeshStandardMaterial({color:0xd8dde0,map:ft,roughness:.82,metalness:.06});
  const b=new THREE.Mesh(g,mat);b.castShadow=true;b.receiveShadow=true;
  b.position.set(x,h/2+0.37,z);if(rotY)b.rotation.y=rotY;
  // 按楼面尺寸平铺：宽约 1 格/1.6 单位，高约 1 层/1.5 单位
  ft.repeat.set(Math.max(1,Math.round(w/1.6)),Math.max(1,Math.round(h/1.5)));
  scene3.add(b);
  // 楼顶女儿墙 + 底面稍内收，增强真实感
  const cap=new THREE.Mesh(new THREE.BoxGeometry(w*1.02,0.14,d*1.02),new THREE.MeshStandardMaterial({color:0x3a424a,roughness:.9}));
  cap.position.set(x,h+0.44,z);cap.castShadow=true;scene3.add(cap);
  return b}
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
  scene3=new THREE.Scene();scene3.background=new THREE.Color(0x1a2028);scene3.fog=new THREE.Fog(0x5a646e,70,200);
  camera=new THREE.PerspectiveCamera(40,innerWidth/innerHeight,0.1,500);
  camera.position.set(30,44,58);
  controls=new THREE.OrbitControls(camera,renderer.domElement);controls.target.set(0,0.5,0);controls.maxPolarAngle=1.42;controls.minDistance=18;controls.maxDistance=120;controls.enableDamping=true;controls.dampingFactor=0.08;controls.update();
  // 阴雨天空 HDR + 环境反射
  new THREE.RGBELoader().setDataType(THREE.UnsignedByteType).load('/assets/hdr/cloudy.hdr',function(hdr){
    hdr.mapping=THREE.EquirectangularReflectionMapping;
    const pmrem=new THREE.PMREMGenerator(renderer);envTex=pmrem.fromEquirectangular(hdr).texture;
    scene3.environment=envTex;scene3.background=envTex;scene3.backgroundIntensity=0.6;hdr.dispose();pmrem.dispose()});
  // 阴天柔和光照：半球光 + 弱方向光
  scene3.add(new THREE.HemisphereLight(0x8fa4b8,0x2a3322,0.55));
  const sun=new THREE.DirectionalLight(0xcfd8e0,0.38);sun.position.set(40,60,25);sun.castShadow=true;sun.shadow.mapSize.set(2048,2048);
  const sc=sun.shadow.camera;sc.left=-70;sc.right=70;sc.top=70;sc.bottom=-70;sc.far=200;scene3.add(sun);
  // 城市基底：自然地面(草/土)大平面
  const base=new THREE.Mesh(new THREE.PlaneGeometry(500,500),new THREE.MeshStandardMaterial({color:0x6f767c,roughness:.96,metalness:.02}));
  base.rotation.x=-Math.PI/2;base.position.y=-0.02;base.receiveShadow=true;
  // 地面贴图大范围平铺
  ['ground_diffuse','ground_rough','ground_normal'].forEach(n=>{const t=loadTex(n);t.repeat.set(60,60)});
  scene3.add(base);
  fetch('/api/scheduler').then(r=>r.json()).then(build);
  addEventListener('resize',()=>{camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight)});
  // 雨丝粒子
  makeRain();
  renderer.setAnimationLoop(tick);
}
function makeRain(){
  const N=900;const pos=new Float32Array(N*3);
  for(let i=0;i<N;i++){pos[i*3]=(Math.random()-0.5)*140;pos[i*3+1]=Math.random()*55;pos[i*3+2]=(Math.random()-0.5)*140}
  rainGeo=new THREE.BufferGeometry();rainGeo.setAttribute('position',new THREE.BufferAttribute(pos,3));
  const m=new THREE.PointsMaterial({color:0xbfd4e6,size:0.12,transparent:true,opacity:0.45,depthWrite:false});
  rain=new THREE.Points(rainGeo,m);rain.frustumCulled=false;scene3.add(rain)}
function build(sched){
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
  const TWIN3D_FAC={'DF-PS-01':[285,174],'DF-PS-02':[228,231],'DF-IN-101':[258,78],'DF-IN-103':[120,146],'DF-IN-201':[633,146],'DF-IN-202':[526,176],'DF-PS-03':[560,105]};
  const TWIN3D_WATER={'WO-20261029-007':[[250,150],[330,150],[330,172],[250,172]],'WO-20261029-011':[[545,150],[635,150],[635,172],[545,172]]};
  const netRoads=roads.map(function(e){return{id:e.id,label:e.label,detail:e.detail||'',pts:(TWIN3D_ROADS[e.id]||String(e.points||'').split(' ').map(function(p){return p.split(',').map(Number)}))}});
  const seeded=(function(){let s=20260801;return function(){s=(s*1664525+1013904223)>>>0;return s/4294967296}})();
  // ---- 道路：沥青 PBR + 车道标线 + 路缘 + 斑马线 ----
  const roadMat=stdMat('asphalt',{roughness:.88});roadMat.color.set(0x6a7076);
  const laneMat=new THREE.MeshBasicMaterial({color:0xe8e4d8});
  const curbMat=new THREE.MeshStandardMaterial({color:0x8a9299,roughness:.7});
  netRoads.forEach(e=>{const ps=e.pts;const closed=/已封/.test(e.label||'');
    const wid=/主干路/.test(e.detail)?1.6:(/次干路/.test(e.detail)?1.3:1.05);
    for(let i=0;i<ps.length-1;i++){const[ax,ay]=W(ps[i][0],ps[i][1]),[bx,by]=W(ps[i+1][0],ps[i+1][1]);
      const len=Math.hypot(bx-ax,by-ay);if(len<0.05)continue;const ang=-Math.atan2(by-ay,bx-ax);
      const road=new THREE.Mesh(new THREE.BoxGeometry(len+wid,0.05,wid),roadMat);
      road.position.set((ax+bx)/2,0.36,(ay+by)/2);road.rotation.y=ang;road.receiveShadow=true;scene3.add(road);
      // 路缘石
      const nx=Math.sin(ang),nz=Math.cos(ang);
      [-1,1].forEach(function(side){const eg=new THREE.Mesh(new THREE.BoxGeometry(len+wid,0.12,0.16),curbMat);
        eg.position.set((ax+bx)/2+nx*side*(wid/2+0.08),0.36,(ay+by)/2+nz*side*(wid/2+0.08));eg.rotation.y=ang;eg.receiveShadow=true;scene3.add(eg)});
      // 中央车道线（已封路画实线黄）
      if(!closed){let t=2.4;while(t<len-2.4){const d=new THREE.Mesh(new THREE.BoxGeometry(1.1,0.015,0.11),laneMat);
        d.position.set(ax+(bx-ax)*t/len,0.395,ay+(by-ay)*t/len);d.rotation.y=ang;scene3.add(d);t+=2.6}}
    }
    // 路名标签
    const mid=ps[Math.floor(ps.length/2)];const[mx,mz]=W(mid[0],mid[1]);
    const lb=makeLabel(String(e.label||'').split('（')[0],closed?'#e0a965':'#cfe0ef');lb.position.set(mx,3.2,mz);scene3.add(lb)});
  // 路口斑马线
  const zebraMat=new THREE.MeshBasicMaterial({color:0xd5dce2});
  [[200,70],[200,160],[200,250],[360,160],[590,70],[590,160],[590,250]].forEach(function(j){const[jx,jz]=W(j[0],j[1]);
    for(let k=-2;k<=2;k++){const a=new THREE.Mesh(new THREE.BoxGeometry(1.5,0.015,0.2),zebraMat);a.position.set(jx+k*0.32,0.4,jz);scene3.add(a);
      const b=new THREE.Mesh(new THREE.BoxGeometry(0.2,0.015,1.5),zebraMat);b.position.set(jx,0.4,jz+k*0.32);scene3.add(b)}});
  // ---- 楼栋：落在街区内部（正交网格围成的街区），每街区 1-3 栋，疏朗有规划感 ----
  // 街区中心点：由主次干道网格围成的空地中心
  const blocks=[[135,115],[280,115],[135,205],[280,205],[475,115],[655,115],[475,205],[655,205]];
  dist.forEach(e=>{
    const r=Math.min(e.r,96);
    blocks.forEach(bc=>{
      // 只在该片区范围内布楼
      if(Math.hypot(bc[0]-e.cx,bc[1]-e.cy)>r*0.9)return;
      // 该街区放 1-3 栋
      const n=1+Math.floor(seeded()*3);
      for(let k=0;k<n;k++){
        const px=bc[0]+(seeded()-0.5)*22,py=bc[1]+(seeded()-0.5)*22;
        // 避开积水面
        let nearWater=false;
        Object.values(TWIN3D_WATER).forEach(wp=>{const wc=wp.reduce((a,p)=>[a[0]+p[0],a[1]+p[1]],[0,0]).map(v=>v/wp.length);
          if(Math.hypot(px-wc[0],py-wc[1])<20)nearWater=true});
        if(nearWater)continue;
        const w=5+seeded()*3,d=5+seeded()*3,h=3.5+seeded()*seeded()*9;
        const[wx,wz]=W(px,py);
        makeBuilding(w,h,d,wx,wz,Math.floor(seeded()*4)*Math.PI/2);
      }
    });
  });
  // ---- 行道树（沿主干路两侧）+ 片区内部点缀 ----
  netRoads.forEach(rd=>{if(rd.pts.length<2)return;
    const main=/主干路|次干路/.test(rd.detail);
    for(let i=0;i<rd.pts.length-1;i++){const p0=rd.pts[i],p1=rd.pts[i+1];
      const dx=p1[0]-p0[0],dy=p1[1]-p0[1];const len=Math.hypot(dx,dy);if(len<5)continue;
      const ux=dx/len,uy=dy/len;const nx=-uy,ny=ux;
      let t=8;while(t<len-8){const bx=p0[0]+ux*t,by=p0[1]+uy*t;
        if(main){[-1,1].forEach(function(side){const px=bx+nx*side*8,py=by+ny*side*8;
          if(seeded()>0.82){const[wx,wz]=W(px,py);makeTree(wx,wz,0.7+seeded()*0.5)}})}
        t+=20+seeded()*8}}
  });
  // ---- 路灯（主干路两侧） ----
  netRoads.forEach(rd=>{if(rd.pts.length<2)return;
    if(!/主干路/.test(rd.detail))return;
    for(let i=0;i<rd.pts.length-1;i++){const p0=rd.pts[i],p1=rd.pts[i+1];
      const dx=p1[0]-p0[0],dy=p1[1]-p0[1];const len=Math.hypot(dx,dy);if(len<5)continue;
      const ux=dx/len,uy=dy/len;const nx=-uy,ny=ux;
      let t=12;while(t<len-12){const bx=p0[0]+ux*t,by=p0[1]+uy*t;
        const[wx,wz]=W(bx+nx*5,by+ny*5);makeLamp(wx,wz);t+=34}}
  });
  // ---- 路侧车辆 ----
  const carColors=[0x8899aa,0x6b7b8a,0x9aa7b2,0x5a6b7a,0x77828d];
  netRoads.forEach(rd=>{if(rd.pts.length<2)return;
    for(let i=0;i<rd.pts.length-1;i++){const p0=rd.pts[i],p1=rd.pts[i+1];
      const dx=p1[0]-p0[0],dy=p1[1]-p0[1];const len=Math.hypot(dx,dy);if(len<5)continue;
      const ux=dx/len,uy=dy/len;const nx=-uy,ny=ux;const ang=-Math.atan2(ny,nx);
      let t=16;while(t<len-16){const bx=p0[0]+ux*t,by=p0[1]+uy*t;
        if(seeded()>0.88){const[wx,wz]=W(bx+nx*8,by+ny*8);
          makeCar(wx,wz,ang,carColors[Math.floor(seeded()*carColors.length)])}t+=22}}
  });
  // ---- 积水段：高饱和蓝水面 + 白色描边，从灰地面强对比跳出 ----
  const waterMat=new THREE.MeshBasicMaterial({color:0x0a8cff,depthTest:false,side:THREE.DoubleSide});
  const strokeMat=new THREE.LineBasicMaterial({color:0xffffff,linewidth:2,depthTest:false});
  water.forEach(e=>{const ps=(TWIN3D_WATER[e.id]||String(e.points||'').split(' ').map(p=>p.split(',').map(Number)));
    const cen=ps.reduce((a,p)=>[a[0]+p[0],a[1]+p[1]],[0,0]).map(v=>v/ps.length);
    const sh=new THREE.Shape();
    ps.forEach((p,i)=>{const[wx,wz]=W(p[0],p[1]);if(i===0)sh.moveTo(wx,-wz);else sh.lineTo(wx,-wz)});
    const g=new THREE.ShapeGeometry(sh);const m=new THREE.Mesh(g,waterMat);
    m.rotation.x=Math.PI/2;m.position.y=0.55;m.renderOrder=999;scene3.add(m);
    // 白色描边（沿积水轮廓）
    const edges=new THREE.EdgesGeometry(g);
    const line=new THREE.LineSegments(edges,strokeMat);
    line.rotation.x=Math.PI/2;line.position.y=0.56;line.renderOrder=1000;scene3.add(line);
    // 积水标注（白色衬底蓝字）
    const[wx,wz]=W(cen[0],cen[1]);
    const lb=makeLabel((e.label||'积水')+' '+((e.detail||'').match(/\\d+[.]?\\d*\\s*cm/)||[''])[0],'#ffffff');lb.position.set(wx,3.0,wz);scene3.add(lb)});
  // ---- 设施：泵站(写实小楼) + 雨水口(井盖) ----
  fac.forEach(e=>{const fpos=TWIN3D_FAC[e.id]||[Number(e.cx),Number(e.cy)];const[wx,wz]=W(fpos[0],fpos[1]);const ps=e.id.indexOf('DF-PS')===0;
    if(ps){makeBuilding(2.6,2.6,2.6,wx,wz,0);
      const sign=makeLabel(String(e.label||e.id).replace('雨水泵站','泵站'),'#cfe0ef');sign.position.set(wx,4.6,wz);scene3.add(sign)}
    else{const cap=new THREE.Mesh(new THREE.CylinderGeometry(0.5,0.5,0.12,16),new THREE.MeshStandardMaterial({color:0x5a6a7a,roughness:.5,metalness:.6}));
      cap.position.set(wx,0.4,wz);cap.receiveShadow=true;scene3.add(cap)}});
}
function makeLabel(text,color){const cv=document.createElement('canvas');cv.width=256;cv.height=64;const g=cv.getContext('2d');g.font='bold 30px system-ui';g.textAlign='center';g.textBaseline='middle';g.lineWidth=6;g.strokeStyle='rgba(10,16,22,.9)';g.strokeText(text,128,32);g.fillStyle=color||'#d5e4f0';g.fillText(text,128,32);const tex=new THREE.CanvasTexture(cv);tex.minFilter=THREE.LinearFilter;const mat=new THREE.SpriteMaterial({map:tex,transparent:true,depthTest:false});const sp=new THREE.Sprite(mat);sp.scale.set(9,2.25,1);return sp}
function tick(){
  if(controls)controls.update();
  if(rain){const p=rainGeo.attributes.position;for(let i=0;i<p.count;i++){p.array[i*3+1]-=0.22;if(p.array[i*3+1]<-2)p.array[i*3+1]=54;p.array[i*3]+=(Math.random()-0.5)*0.04;p.array[i*3+2]+=(Math.random()-0.5)*0.04}p.needsUpdate=true}
  renderer.render(scene3,camera);
}
init();
</script></html>"""

def route(path: str, method: str = "GET", body: bytes | None = None) -> tuple[int, str, bytes]:
    """把一次 HTTP 请求映射成 (status, content_type, body)。

    纯函数：测试直接调它，不起 socket、不占端口。
    """
    parsed = urlparse(path)
    segments = [item for item in parsed.path.split("/") if item]
    query = parse_qs(parsed.query)

    if parsed.path in {"", "/"}:
        return 200, "text/html; charset=utf-8", build_html().encode("utf-8")
    if parsed.path == "/twin3d":
        return 200, "text/html; charset=utf-8", build_twin3d_html().encode("utf-8")
    if parsed.path == "/three.min.js":
        three = _assets_dir() / "three.min.js"
        if not three.exists():
            return _error("three.min.js missing (assets/three.min.js)", 404)
        return 200, "application/javascript; charset=utf-8", three.read_bytes()
    # 三维写实沙盘的静态依赖（RGBELoader/OrbitControls）与 PBR 贴图/HDR，全部本地分发、断网可用
    if parsed.path in {"/RGBELoader.js", "/OrbitControls.js"}:
        f = _assets_dir() / parsed.path[1:]
        if not f.exists():
            return _error(f"{parsed.path} missing", 404)
        return 200, "application/javascript; charset=utf-8", f.read_bytes()
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

    if segments[:2] == ["api", "scheduler"]:
        # 微调度台总入口：一次返回链路 / 六类事件流 / 工单 / 资源 / 场景 / 方案。
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
