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

from . import review_collect as reviews
from . import visualize
from . import web_llm
from .deduction import DistrictSignal
from .evolution_demo import build_evolution_html, build_evolution_payload
from .skills import SkillPipeline, SkillRuntime
from .semantic import build_urban_semantic_store

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


# 注意：下面是普通 Python 字符串，不是 raw 字符串。写 JS 时**不要**用 "\n"——Python 会把它
# 变成真实换行，从而截断 JS 的字符串字面量，整个 <script> 直接解析失败（页面看起来"按钮没反应"）。
# 需要换行请用 String.fromCharCode(10)。`tests/test_web_demo.py` 里有 node --check 兜底。
def build_html() -> str:
    return """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Urban Twin</title>
<style>body{font:15px system-ui;margin:0;background:#0b1220;color:#e8eef7}main{max-width:1180px;margin:auto;padding:28px}h1{color:#7dd3fc;margin-bottom:4px}.sub{color:#9fb1c8}.toolbar{display:flex;gap:12px;align-items:center;margin:22px 0}button{background:#38bdf8;border:0;border-radius:6px;padding:10px 18px;font-weight:700;cursor:pointer}.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:16px}.card{background:#152238;border:1px solid #263a55;border-radius:10px;padding:16px}.card h2{font-size:17px;margin:0 0 12px;color:#bae6fd}.metric{display:inline-block;background:#20344d;border-radius:7px;padding:10px;margin:0 8px 8px 0}.metric b{display:block;font-size:22px;color:#7dd3fc}.pill{display:inline-block;background:#334155;padding:3px 8px;border-radius:99px;margin:3px;font-size:12px}.warn{color:#fbbf24}.ok{color:#86efac}.trace{border-left:2px solid #38bdf8;padding-left:12px;margin:8px 0}.evidence{font-size:12px;color:#b8c7d9;word-break:break-all}svg{width:100%;height:330px;background:#0e1a2c;border-radius:8px}.road{stroke:#64748b;stroke-width:8}.pipe{stroke:#f97316;stroke-width:3;stroke-dasharray:7 5}.flood{fill:#38bdf8;opacity:.35}.node{fill:#fbbf24;stroke:#fff;stroke-width:2}.navlink{color:#7dd3fc;text-decoration:none;margin-left:auto;font-weight:600}@media(max-width:800px){.grid{grid-template-columns:1fr}}</style>
<main><h1>城市内涝跨部门事件推演</h1><div class=sub>暴雨 + 道路封闭 + 地下管线施工冲突 · 证据链可追溯</div><div class=toolbar><button onclick="run()">运行演示</button><button class=ghost onclick="renderScene('v1')">渲染基线 v1</button><button class=ghost onclick="renderScene('v4')">渲染进化 v4</button><button class=ghost onclick="diffScenes()">对比两版</button><button class=ghost onclick="runLLM()">LLM 决策（DeepSeek）</button><span id=status>等待运行</span><a class=navlink href='/evolution'>决策进化对比 →</a></div><section id=summary class=card><h2>事件概览</h2><div id=metrics>点击“运行演示”加载推演结果</div></section><div class=grid><section class=card><h2>数字孪生影响范围</h2><svg viewBox='0 0 700 330' aria-label='影响范围示意图'><path class=road d='M40 260 L660 70 M40 80 L660 260'/><path class=pipe d='M80 250 L400 160 L630 150'/><ellipse class=flood cx=350 cy=165 rx=130 ry=80/><circle class=node cx=350 cy=165 r=8/><text x=365 y=160 fill='#e8eef7'>A-03 积水区</text></svg><div id=assets></div></section><section class=card><h2>跨部门协同</h2><div id=departments></div><h2>资源与风险</h2><div id=risks></div></section></div><section class=card style='margin-top:16px'><h2>推理链路与证据</h2><div id=trace></div></section>
<style>.ghost{background:#1e3a55;color:#bae6fd}#log{max-height:360px;overflow:auto;font:12px ui-monospace,monospace}.logrow{padding:6px 8px;border-bottom:1px solid #1e3250;white-space:nowrap}.logrow .mcp{color:#86efac}.logrow .manual{color:#fbbf24}#panel{font:12px ui-monospace,monospace;white-space:pre-wrap;color:#cfe3f7}#comparebody{font:12px ui-monospace,monospace;white-space:pre-wrap;color:#cfe3f7}</style>
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
loadPlatform();loadLog();</script></html>"""


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
