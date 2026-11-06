"""Dependency-free demo payload and browser server for the urban twin.

除推演与进化对比两个页面外，这里还承担两件事：

1. **UE 侧的取数端点**：`/api/scene/<snapshot_id>` 返回渲染作业，`/ack` 让引擎写回认领
   状态——`scene_render` 工具返回的 `delivery` 读的就是这个 ack 文件；
2. **工具调用日志**：`/api/tool-log` 输出 MCP 的 jsonl 调用记录，页面上并排滚动，评委
   要看得出"智能体调了渲染"还是"人点了按钮"，所以每条带 `origin`（mcp / manual）。
"""
from __future__ import annotations

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


def build_demo_payload(*, event_id: str = EVENT_ID, as_of: str = AS_OF) -> dict[str, Any]:
    report = SkillPipeline(SkillRuntime()).run(
        event_id, as_of, caller_labels=["ORG-EMERG"], persist=False
    )
    payload = report.model_dump(mode="json")
    payload["evidence"] = payload.get("audit", {}).get("claims", [])
    return payload


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
def build_html() -> str:
    return """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Urban Twin</title>
<style>body{font:15px system-ui;margin:0;background:#0b1220;color:#e8eef7}main{max-width:1180px;margin:auto;padding:28px}h1{color:#7dd3fc;margin-bottom:4px}.sub{color:#9fb1c8}.toolbar{display:flex;gap:12px;align-items:center;margin:22px 0}button{background:#38bdf8;border:0;border-radius:6px;padding:10px 18px;font-weight:700;cursor:pointer}.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:16px}.card{background:#152238;border:1px solid #263a55;border-radius:10px;padding:16px}.card h2{font-size:17px;margin:0 0 12px;color:#bae6fd}.metric{display:inline-block;background:#20344d;border-radius:7px;padding:10px;margin:0 8px 8px 0}.metric b{display:block;font-size:22px;color:#7dd3fc}.pill{display:inline-block;background:#334155;padding:3px 8px;border-radius:99px;margin:3px;font-size:12px}.warn{color:#fbbf24}.ok{color:#86efac}.trace{border-left:2px solid #38bdf8;padding-left:12px;margin:8px 0}.evidence{font-size:12px;color:#b8c7d9;word-break:break-all}svg{width:100%;height:330px;background:#0e1a2c;border-radius:8px}.road{stroke:#64748b;stroke-width:8}.pipe{stroke:#f97316;stroke-width:3;stroke-dasharray:7 5}.flood{fill:#38bdf8;opacity:.35}.node{fill:#fbbf24;stroke:#fff;stroke-width:2}.navlink{color:#7dd3fc;text-decoration:none;margin-left:auto;font-weight:600}@media(max-width:800px){.grid{grid-template-columns:1fr}}</style>
<main><h1>城市内涝跨部门事件推演</h1><div class=sub>暴雨 + 道路封闭 + 地下管线施工冲突 · 证据链可追溯</div><div class=toolbar><button onclick="run()">运行演示</button><button class=ghost onclick="renderScene('v1')">渲染基线 v1</button><button class=ghost onclick="renderScene('v4')">渲染进化 v4</button><button class=ghost onclick="diffScenes()">对比两版</button><button class=ghost onclick="runLLM()">LLM 决策（DeepSeek）</button><span id=status>等待运行</span><a class=navlink href='/evolution'>决策进化对比 →</a></div><section id=summary class=card><h2>事件概览</h2><div id=metrics>点击“运行演示”加载推演结果</div></section>
<section class=card style='margin-top:16px'><h2>演示链路 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（事件输入 → 主 Agent → MCP 调用 → 协同复核 → 冲突 → 方案 → 复算）</span></h2><div class=toolbar style='margin:0 0 8px'><button onclick="runChain()">跑一遍链路</button><span id=chainstatus class=sub>未加载</span></div><div id=chain class=chain></div><div id=chainfoot class=sub style='margin-top:8px'></div></section>
<div class=grid><section class=card><h2>数字孪生影响范围</h2><svg id=scenemap viewBox='0 0 700 330' aria-label='可点场景图'></svg><div id=sceneinfo class=sub>加载场景实体后，点积水区、泵站或道路，看它对应哪张工单、占用哪些资源</div><div id=assets></div></section><section class=card><h2>跨部门协同</h2><div id=departments></div><h2>资源与风险</h2><div id=risks></div></section></div>
<div class=grid style='margin-top:16px'>
<section class=card><h2>工单列表 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（台账全量，演示事件置顶）</span></h2><div id=orders class=sub>加载中</div></section>
<section class=card><h2>资源状态 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（台账可用量 vs 本次缺口）</span></h2><div id=resboard class=sub>加载中</div></section>
</div>
<section class=card style='margin-top:16px'><h2>当前方案 vs 备选方案 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（同一份决策输入，只换策略参数）</span></h2><div id=plans class=sub>加载中</div><div id=planfoot class=sub style='margin-top:10px'></div></section>
<section class=card style='margin-top:16px'><h2>人工协同 vs 智能体辅助 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（只比算得出来的项；人工耗时未实测就留空，不填估算值）</span></h2><div id=baseline class=sub>加载中</div><div id=basefoot class=sub style='margin-top:10px'></div></section>
<section class=card style='margin-top:16px'><h2>推理链路与证据</h2><div id=trace></div></section>
<section class=card style='margin-top:16px'><h2>微调度台 · 决策事件流 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（六类事件同源派生，每条都能对回推演字段）</span></h2><div class=toolbar style='margin:0 0 10px'><button onclick="playTimeline()">回放事件流</button><span id=tlstatus class=sub>未加载</span></div><div id=cats class=cats></div><ol id=timeline class=tl></ol><div id=tlfoot class=sub style='margin-top:10px'></div></section>
<section class=card style='margin-top:16px'><h2>人工干预 · 重新推演 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（改一个条件，系统重算并说明改了什么）</span></h2><div id=ivbtns class=toolbar style='margin:0 0 10px'></div><div id=ivout class=sub>点上面任一项，观察推演结果怎么变</div></section>
<section class=card style='margin-top:16px'><h2>协同复核 · 谁接哪张卡 <span style='font-weight:400;font-size:12px;color:#9fb1c8'>（1 总控 + 5 部门，只读复核、不改推演数字）</span></h2><div id=cvsum class=sub>未加载</div><div id=cvcards style='margin-top:10px'></div><div id=cvhand class=sub style='margin-top:10px'></div></section>
<style>.ghost{background:#1e3a55;color:#bae6fd}#log{max-height:360px;overflow:auto;font:12px ui-monospace,monospace}.logrow{padding:6px 8px;border-bottom:1px solid #1e3250;white-space:nowrap}.logrow .mcp{color:#86efac}.logrow .manual{color:#fbbf24}#panel{font:12px ui-monospace,monospace;white-space:pre-wrap;color:#cfe3f7}#comparebody{font:12px ui-monospace,monospace;white-space:pre-wrap;color:#cfe3f7}.tl{list-style:none;padding:0;margin:0}.tl li{border-left:2px solid #263a55;padding:8px 0 8px 14px;opacity:.2;transition:opacity .35s,border-color .35s}.tl li.on{opacity:1;border-left-color:#38bdf8}.tl .hd{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}.tl .seq{color:#64748b;font:12px ui-monospace,monospace}.tl .ttl{font-weight:700;color:#bae6fd}.tl .dsc{color:#cfe3f7;font-size:13px;margin-top:3px}.tl .mt{color:#7f93ab;font:11px ui-monospace,monospace;margin-top:5px;word-break:break-all}.tl ul.basis{margin:7px 0 0;padding-left:16px;color:#b8c7d9;font-size:12px}.tl ul.basis li{margin:2px 0;border:0;padding:0;opacity:1}.tag{font-size:11px;padding:1px 6px;border-radius:4px;background:#20344d;color:#9fb1c8}.k-conflict{color:#fbbf24}.k-gap{color:#fca5a5}.k-audit{color:#86efac}.ivbtn{background:#1e3a55;color:#bae6fd;border:0;border-radius:6px;padding:10px 14px;font-weight:700;cursor:pointer;text-align:left;max-width:330px}.ivbtn small{display:block;font-weight:400;color:#8fa4bd;font-size:11px;margin-top:4px;line-height:1.4}.row{display:flex;gap:12px;padding:5px 0;border-bottom:1px dashed #1e3250;font-size:13px}.row b{margin-left:auto;color:#7dd3fc}.chain{display:flex;flex-direction:column;gap:0}.lnk{display:flex;gap:12px;padding:9px 0 9px 14px;border-left:2px solid #263a55;opacity:.32;transition:opacity .3s,border-color .3s}.lnk.on{opacity:1;border-left-color:#38bdf8}.lnk .no{width:22px;color:#64748b;font:12px ui-monospace,monospace}.lnk .who{font:11px ui-monospace,monospace;color:#8fa4bd;min-width:150px}.lnk .what{flex:1}.lnk .what b{color:#bae6fd}.lnk .what div{font-size:12px;color:#b8c7d9;margin-top:3px}.lnk .tool{font:11px ui-monospace,monospace;color:#86efac;white-space:nowrap}.ord{display:grid;grid-template-columns:1fr auto;gap:4px 10px;padding:7px 0;border-bottom:1px dashed #1e3250;font-size:13px}.ord .id{font:11px ui-monospace,monospace;color:#8fa4bd}.ord .ds{grid-column:1/3;color:#b8c7d9;font-size:12px}.ord.ev{background:#14283d;border-left:2px solid #38bdf8;padding-left:8px}.ord .sev{padding:1px 6px;border-radius:4px;font-size:11px;background:#20344d}.sev.high{color:#fca5a5}.sev.medium{color:#fbbf24}.plan{display:grid;grid-template-columns:1fr 1fr;gap:14px}.plan .col{background:#101d30;border:1px solid #22344d;border-radius:8px;padding:12px}.plan .col h3{margin:0 0 8px;font-size:14px;color:#bae6fd}.plan .col.now{border-color:#2b6f8f}.plan .col.alt{border-color:#7c5a1f}.plan .kv{display:flex;font-size:13px;padding:3px 0}.plan .kv b{margin-left:auto;color:#7dd3fc}.ent{cursor:pointer}.ent:hover{filter:brightness(1.35)}#sceneinfo{margin-top:8px;font-size:13px;color:#cfe3f7}#sceneinfo b{color:#7dd3fc}#sceneinfo .lk{font:12px ui-monospace,monospace;color:#8fa4bd;word-break:break-all;margin-top:4px}.cats{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}.cat{background:#1e3a55;color:#bae6fd;border:0;border-radius:99px;padding:5px 11px;font-size:12px;cursor:pointer}.cat.sel{background:#38bdf8;color:#0b1220;font-weight:700}@media(max-width:800px){.grid{grid-template-columns:1fr}.plan{grid-template-columns:1fr}.lnk .who{min-width:0}}</style>
<div class=grid style='margin-top:16px'>
<section class=card><h2>当前渲染作业（UE 拉取的就是它）</h2><div id=panel>尚未渲染</div></section>
<section class=card><h2>工具调用日志（谁触发了渲染）</h2><div id=log><span class=warn>暂无调用记录</span></div></section>
</div>
<section id=llmcard class=card style='margin-top:16px;display:none'><h2>LLM 决策对照（DeepSeek，temperature=0）</h2><div id=llmbody style='font:13px ui-monospace,monospace;color:#cfe3f7'></div></section>
<section id=comparecard class=card style='margin-top:16px;display:none'><h2>策略对比</h2><div id=comparebody></div></section></main>
<script>const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function list(v){return (v||[]).map(x=>'<span class=pill>'+esc(typeof x==='string'?x:(x.name||x.id||JSON.stringify(x)))+'</span>').join('')||'<span class=warn>无</span>'}
async function run(){status.textContent='推演中…';const r=await fetch('/api/demo');const p=await r.json();const f=p.facts||{},d=p.deduction||{};metrics.innerHTML='<span class=metric><b>'+esc(f.response_level||'-')+'</b>响应等级</span><span class=metric><b>'+esc(f.depth_cm||'-')+' cm</b>积水深度</span><span class=metric><b>'+esc(p.knowledge_version)+'</b>知识版本</span><span class=metric><b class=ok>'+esc(p.audit?.citation_completeness??'-')+'</b>引用完整率</span>';assets.innerHTML='<b>受影响对象</b><div>'+list(p.impact?.affected_objects||d.affected_assets||[])+'</div>';departments.innerHTML=list(d.departments||p.deduction?.departments||p.facts?.departments||[]);risks.innerHTML='<div>'+list(d.conflicts||[])+'</div><div>'+list(d.resource_gaps||[])+'</div><div class=warn>'+list(d.residual_risks||[])+'</div>';trace.innerHTML=(p.evidence||[]).slice(0,12).map(c=>'<div class=trace><b>'+esc(c.text||c.claim_id)+'</b><div class=evidence>'+esc((c.evidence_refs||[]).join(' · '))+'</div></div>').join('')||'<span class=warn>暂无可展示证据</span>';status.textContent=p.publishable?'可发布结论':'需要人工复核';loadLog()}
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
  li.innerHTML='<div class=hd><span class=seq>'+String(e.seq).padStart(2,'0')+'</span><span class="ttl k-'+esc(e.kind)+'">'+esc(e.title)+'</span><span class=tag>'+esc(e.category)+'</span><span class=tag>'+esc(e.status)+'</span></div><div class=dsc>'+esc(e.detail)+'</div><div class=mt>'+esc(e.source)+' · 证据 '+esc(e.ref_count)+' 条</div>'+basis;
  timeline.appendChild(li)})}
function filterCat(c){STREAMCAT=c;renderCats();renderStream();tlstatus.textContent='显示 '+streamEvents().length+' 条（'+c+'）'}
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
 ivout.innerHTML='<b style="color:#7dd3fc">'+esc(d.label)+'</b><div style="color:#7f93ab;font:11px ui-monospace,monospace;margin:6px 0 10px;word-break:break-all">'+esc(d.patches.join(' · '))+'</div>'+rows.map(x=>'<div class=row><span>'+esc(x[0])+'</span><b>'+esc(x[1])+'</b></div>').join('')+travelHtml+stmts+moved+'<div style="margin-top:10px;font-size:11px;color:#7f93ab">'+esc(d.mechanism)+'</div>'}
async function loadCollab(){const r=await fetch('/api/collab-review');const d=await r.json();
 if(d.status!=='ok'){cvsum.innerHTML='<span class=warn>'+esc(d.error||'复核不可用')+'</span>';return}
 const c=d.counts;
 cvsum.innerHTML='动作卡 <b>'+c.cards+'</b> 张 · 部门 <b>'+c.agents+'</b> 个 · 交接 <b>'+c.handoffs+'</b> 次 · 需会签 <b>'+c.co_sign_pairs+'</b> 对 · 规则冲突 <b class="'+(c.violations?'warn':'ok')+'">'+c.violations+'</b> 处';
 const byAgent={};(d.cards||[]).forEach(x=>{(byAgent[x.agent_id]=byAgent[x.agent_id]||[]).push(x)});
 cvcards.innerHTML=Object.keys(byAgent).map(a=>'<div style="margin:8px 0 4px;color:#7dd3fc;font-weight:700">'+esc(a)+'<span class=tag style="margin-left:8px">'+byAgent[a].length+' 张</span></div>'+byAgent[a].map(x=>'<div class=row><span>'+esc(x.action)+'</span><span class=tag>'+esc(x.stage)+'</span><b>'+esc(x.status)+'</b></div>').join('')).join('');
 cvhand.textContent=(d.handoffs||[]).length?'交接：'+d.handoffs.map(h=>h.from_agent+' → '+h.to_agent+'（'+h.card_ids.length+' 张）').join(' ｜ ')+' ｜ 指纹 '+d.kernel_fingerprint.slice(0,12):'无角色交接 ｜ 指纹 '+d.kernel_fingerprint.slice(0,12)}
let SCHED=null;
function drawScene(scene){if(!scene||!scene.entities)return;const parts=[];
 (scene.entities||[]).forEach(e=>{const cls='ent k-'+esc(e.kind);const attrs='data-id="'+esc(e.id)+'" onclick="pickEntity(\\''+esc(e.id)+'\\')"';
  if(e.shape==='line'){parts.push('<polyline class="'+cls+' road" points="'+e.points+'" fill="none" '+attrs+'/>')}
  else if(e.shape==='polygon'){parts.push('<polygon class="'+cls+' flood" points="'+e.points+'" '+attrs+'/>')}
  else{parts.push('<circle class="'+cls+' node" cx="'+e.cx+'" cy="'+e.cy+'" r="'+e.r+'" '+attrs+'/>')}});
 const labels=(scene.entities||[]).filter(e=>e.kind==='water').map(e=>{const p=(e.points||'').split(' ')[0].split(',');return '<text x="'+esc(p[0])+'" y="'+esc(p[1])+'" fill="#e8eef7" font-size="11">'+esc(e.label)+'</text>'}).join('');
 scenemap.innerHTML=parts.join('')+labels}
function pickEntity(id){if(!SCHED)return;const e=(SCHED.scene.entities||[]).find(x=>x.id===id);if(!e)return;
 const ords=(SCHED.work_orders||[]).filter(o=>(e.links.work_orders||[]).includes(o.order_id));
 const ress=(SCHED.resources.items||[]).filter(r=>(e.links.resources||[]).includes(r.resource_id));
 sceneinfo.innerHTML='<b>'+esc(e.label)+'</b><div style="margin-top:4px;font-size:13px">'+esc(e.detail)+'</div>'+
  '<div class=lk>关联工单：'+(ords.length?ords.map(o=>esc(o.order_id)+'（'+esc(o.order_type_label)+'，'+esc(o.status)+'）').join('；'):'无')+'</div>'+
  '<div class=lk>占用资源：'+(ress.length?ress.map(r=>esc(r.name)+' 可用 '+esc(r.qty_available)+esc(r.unit)+(r.required!=null?' / 本次需 '+esc(r.required):'')).join('；'):'无')+'</div>'+
  '<div class=lk>证据：'+((e.links.evidence||[]).length?esc(e.links.evidence.join(' · ')):'无')+'</div>'}
function renderOrders(){(orders=document.getElementById('orders')).className='';
 orders.innerHTML=(SCHED.work_orders||[]).map(o=>'<div class="ord'+(o.is_event?' ev':'')+'"><span>'+esc(o.order_type_label)+' · '+esc(o.road_id||o.facility_id||o.pipeline_id||'-')+'</span><span class="sev '+esc(o.severity)+'">'+esc(o.severity)+'</span><div class=ds>'+esc(o.order_id)+' · '+esc(o.reporter_org)+' · '+esc(o.status)+' · '+esc(o.description)+'</div></div>').join('')}
function renderResources(){const r=SCHED.resources||{};const s=r.summary||{};
 (resboard=document.getElementById('resboard')).className='';
 resboard.innerHTML='<div class=row><span>台账资源项 / 本次引用</span><b>'+esc(s.total_rows)+' / '+esc(s.used_in_event)+'</b></div><div class=row><span>需求 / 已到位 / 缺口</span><b>'+esc(s.required)+' / '+esc(s.committed)+' / <span class="'+(s.gap>0?'warn':'ok')+'">'+esc(s.gap)+'</span> '+esc(s.unit)+'</b></div>'+
  (r.items||[]).map(i=>'<div class=row><span>'+esc(i.name)+' <span class=tag>'+esc(i.district)+'</span></span><b>'+(i.used_in_event?(esc(i.required)+' / '+esc(i.committed)+' / '+(i.gap>0?'<span class=warn>'+esc(i.gap)+'</span>':esc(i.gap))):('<span class=sub>'+esc(i.qty_available)+esc(i.unit)+'</span>'))+'</b></div>').join('')}
function renderPlan(){const p=SCHED.plan||{};
 (plans=document.getElementById('plans')).className='';
 if(!p||p.error||!p.current||!p.current.per_district){plans.innerHTML='<span class=warn>'+esc((p&&p.error)||'方案不可用')+'</span>';return}
 const col=(title,view,cls)=>'<div class="col '+cls+'"><h3>'+esc(title)+'</h3>'+(view.per_district||[]).map(d=>'<div class=kv><span>'+esc(d.district)+'</span><b>'+esc(d.allocated)+' / '+esc(d.demand)+' 台</b></div>').join('')+'<div class=kv><span>总缺口</span><b>'+esc(view.total_unmet)+' 台</b></div><div class=kv><span>跨区调拨</span><b>'+((view.transfers||[]).length?(view.transfers||[]).length+' 次':'无')+'</b></div></div>';
 plans.innerHTML='<div class=plan>'+col('当前方案（'+p.current.policy_version+'）',p.current,'now')+col('备选方案（'+p.alternative.policy_version+'）',p.alternative,'alt')+'</div>';
 planfoot.textContent=(p.statements||[]).join(' ｜ ')}
function renderChain(){const c=SCHED.chain||{};
 (chain=document.getElementById('chain')).innerHTML=(c.stages||[]).map(s=>'<div class=lnk id="lk-'+esc(s.id)+'"><span class=no>'+esc(s.seq)+'</span><span class=who>'+esc(s.actor)+'</span><span class=what><b>'+esc(s.title)+'</b><div>'+esc(s.summary)+'</div></span><span class=tool>'+esc(s.tool||'—')+'</span></div>').join('');
 chainfoot.textContent='工具链：'+((c.tools||[]).join(' → ')||'—')+' ｜ '+esc(c.source||'')}
function renderBaseline(){const b=SCHED.baseline||{};const m=b.manual||{};const a=b.agent||{};
 (baseline=document.getElementById('baseline')).className='';
 const rows=[
  ['参与部门 / 角色',m.organizations+' 个部门',a.review_agents+' 个部门角色'],
  ['跨部门确认次数（下限）',m.confirm_calls_min+' 次',a.tool_calls+' 次工具调用'],
  ['需翻阅台账 / 证据引用',m.ledger_rows+' 行台账',a.evidence_refs+' 条带行号证据'],
  ['需读规程预案',m.procedure_chars+' 字','按 event_id 检索'],
  ['结论可追溯',m.traceability,'引用完整率 '+a.citation_completeness],
  ['耗时',(m.duration_minutes==null?'未实测':m.duration_minutes+' 分钟'),a.compute_ms+' ms（计算耗时，非协同耗时）']
 ];
 baseline.innerHTML=rows.map(r=>'<div class=row><span>'+esc(r[0])+'</span><span class=sub style="margin-left:12px">人工 '+esc(r[1])+'</span><b>智能 '+esc(r[2])+'</b></div>').join('');
 basefoot.textContent=(b.statements||[]).join(' ｜ ')+((b.unmeasured||[]).length?' ｜ 未测：'+(b.unmeasured||[]).join('、'):'')}
async function loadScheduler(){const r=await fetch('/api/scheduler');SCHED=await r.json();drawScene(SCHED.scene);renderOrders();renderResources();renderPlan();renderChain();renderBaseline()}
async function runChain(){if(!SCHED){chainstatus.textContent='加载中…';await loadScheduler()}
 const ls=[...document.getElementById('chain').children];chainstatus.textContent='执行中 0/'+ls.length;
 for(let i=0;i<ls.length;i++){ls[i].classList.remove('on');await new Promise(done=>setTimeout(done,200));ls[i].classList.add('on');chainstatus.textContent='第 '+(i+1)+'/'+ls.length+' 环：'+(((SCHED.chain||{}).stages||[])[i]||{}).title}
 chainstatus.textContent='链路完成：'+((SCHED.chain||{}).counts||{}).stages+' 环'}
loadPlatform();loadLog();loadInterventions();loadCollab();loadScheduler();</script></html>"""


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
        "scene": scheduler.build_scene_entities(payload),
        "stream": build_event_stream(payload, collab=collab),
        "baseline": baseline_compare.compare(payload, collab=collab),
    }
    return _VIEW_CACHE[key]


def _error(message: str, status: int = 404) -> tuple[int, str, bytes]:
    return _json({"status": "error", "error": message}, status)


def route(path: str, method: str = "GET", body: bytes | None = None) -> tuple[int, str, bytes]:
    """把一次 HTTP 请求映射成 (status, content_type, body)。

    纯函数：测试直接调它，不起 socket、不占端口。
    """
    parsed = urlparse(path)
    segments = [item for item in parsed.path.split("/") if item]
    query = parse_qs(parsed.query)

    if parsed.path in {"", "/"}:
        return 200, "text/html; charset=utf-8", build_html().encode("utf-8")
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
        return _json(build_demo_payload())

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
