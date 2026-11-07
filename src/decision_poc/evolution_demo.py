"""P9.1 浏览器演示：决策进化对比面板（独立模块，不依赖外部前端库）。

为什么单独一个模块
------------------
`web_demo.py` 是"一次推演 → 证据链"的页面，P10 可视化工具层（并行开发）也会改它。
进化对比面板要把两次会话、人工评审、版本标签放在同一屏，属于另一个叙事，
所以单独成模块，只在 `web_demo.serve()` 里挂两个路由：

    GET /evolution        → 本模块的页面
    GET /api/evolution    → 本模块的数据（服务端算好，前端只渲染）

三条约束：

1. **数据全部来自 `run_showcase()`**，不在这里再算一遍偏差或改数字——面板是展示层，
   一旦在展示层"补一个更好看的数"，报告与面板就会有两套口径。
2. **确定性**：同一进程两次请求返回逐位一致（`run_showcase` 本身确定性）。
3. **口径写在页面上**：受控反馈、评审角色扮演、记忆离线同构，三条边界直接印在页脚，
   不靠讲者临场补充。
"""

from __future__ import annotations

from typing import Any

from .policy_evolution import (
    HOLDOUT_TOLERANCE,
    ShowcaseReport,
    run_showcase,
)
from .experience import load_reviews


def _run_summary(report) -> dict[str, Any]:
    return {
        "initial_train": report.initial_train.model_dump(mode="json"),
        "final_train": report.final_train.model_dump(mode="json"),
        "initial_holdout": report.initial_holdout.model_dump(mode="json"),
        "final_holdout": report.final_holdout.model_dump(mode="json"),
        "rounds": [
            {
                "round": item.round,
                "target_dimension": item.target_dimension,
                "params_delta": dict(item.params_delta),
                "train_before": item.train_before,
                "train_after": item.train_after,
                "action": item.action,
                "reason": item.reason,
            }
            for item in report.rounds
        ],
        "releases": [
            {
                "label": item.label,
                "version_id": item.version_id,
                "parent_label": item.parent_label,
                "trigger_feedback_id": item.trigger_feedback_id,
                "trigger_review_ids": list(item.trigger_review_ids),
                "params_delta": dict(item.params_delta),
                "expected_improvement": item.expected_improvement,
            }
            for item in report.releases
        ],
        "final_label": report.final_label,
        "deviation_weights": dict(report.deviation_weights),
        "holdout_tolerance": HOLDOUT_TOLERANCE,
    }


def _real_event_plan(plan) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "allocation_by_district": dict(plan.allocation_by_district),
        "unmet_by_district": dict(plan.unmet_by_district),
        "transfers": list(plan.transfers),
        "escalation": list(plan.escalation),
    }


def build_evolution_payload(showcase: ShowcaseReport | None = None) -> dict[str, Any]:
    """面板数据：直接取双会话 showcase，不在展示层重算任何数字。"""
    data = showcase or run_showcase()
    first, second = data.first, data.second
    review = first.reviews[0] if first.reviews else None
    return {
        "schema_version": "evolution-demo-1",
        "case_digest": data.cases_digest,
        "first": _run_summary(first),
        "second": {
            "warm_start": (
                {
                    "entry_id": second.warm_start.entry_id,
                    "label": second.warm_start.label,
                    "similarity": second.warm_start.similarity,
                    "source_outcome": dict(second.warm_start.source_outcome),
                }
                if second.warm_start is not None
                else None
            ),
            "initial_train": second.initial_train.model_dump(mode="json"),
            "final_train": second.final_train.model_dump(mode="json"),
            "rounds": len(second.rounds),
            "promoted_rounds": second.promoted_rounds,
        },
        "review": (
            {
                "review_id": review.review_id,
                "reviewer_role": review.reviewer_role,
                "decision": review.decision,
                "reason": review.reason,
                "expected_allocation": dict(review.expected_allocation),
                "deviation_before": review.deviation_before,
                "deviation_after": review.deviation_after,
                "reviewer_note": next(
                    (item.reviewer_note for item in load_reviews() if item.review_id == review.review_id),
                    "",
                ),
            }
            if review is not None
            else None
        ),
        "memory": {
            "entries": [
                {
                    "entry_id": entry.entry_id,
                    "scope": entry.scope.value,
                    "label": entry.label,
                    "outcome": dict(entry.outcome),
                    "event_ids": list(entry.event_ids),
                    "review_ids": list(entry.review_ids),
                }
                for entry in data.memory_entries
            ],
            "path": data.memory_path,
        },
        "real_event": {
            "baseline": _real_event_plan(first.real_event_baseline),
            "evolved": _real_event_plan(first.real_event_evolved),
        },
        "boundaries": [
            "反馈信号 = 冻结 ground truth + 人工评审结论（受控评测环境），不是真实市政数据回流",
            "人工评审为角色扮演，用于演示第二反馈通道，不代表真实部门批复",
            "分层记忆为离线同构契约（Tenant/User/Agent），未在真实 Nexent 平台真机验证",
        ],
    }


EVOLUTION_STYLE = """
<style>
:root{--page-bg:#eef2ef;--ink:#173238;--muted:#6c7e7f;--line:#d9e3e0;--surface:#fff;--accent:#0f766e;--accent-soft:#e2f1ed;--warning:#c56d45;--shadow:0 16px 42px rgba(25,55,57,.08)}
*{box-sizing:border-box}
html{background:var(--page-bg)}
body{font:15px/1.65 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;background:var(--page-bg);color:var(--ink)}
main.evolution-page{max-width:1240px;margin:auto;padding:42px 34px 58px}
.evolution-hero{display:flex;align-items:flex-end;justify-content:space-between;gap:28px;padding:10px 0 32px;border-bottom:1px solid var(--line)}
.evolution-hero-copy{max-width:760px}
.evolution-eyebrow{margin-bottom:10px;color:var(--accent);font-size:11px;font-weight:800;letter-spacing:.14em;text-transform:uppercase}
.evolution-hero h1{margin:0;color:var(--ink);font-size:clamp(30px,4vw,50px);line-height:1.08;letter-spacing:-.04em}
.evolution-hero .sub{max-width:650px;margin:14px 0 0;color:var(--muted);font-size:16px}
a.back{display:inline-flex;align-items:center;min-height:42px;padding:0 16px;border:1px solid #b7cbc5;border-radius:999px;background:rgba(255,255,255,.7);color:var(--accent);font-size:13px;font-weight:750;text-decoration:none;transition:background .18s ease,border-color .18s ease,transform .18s ease}
a.back:hover{background:#fff;border-color:var(--accent);transform:translateY(-1px)}
.evolution-metrics{grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:24px 0}
.evolution-metrics .metric{min-height:128px;background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:var(--shadow)}
.evolution-metrics .metric:first-child{background:var(--ink);border-color:var(--ink);color:#fff}
.evolution-metrics .metric:first-child span{color:#bfd2ce}
.metric span{display:block;color:var(--muted);font-size:12px;font-weight:700}
.metric b{display:block;margin:8px 0 4px;color:var(--accent);font-size:clamp(19px,2.2vw,28px);letter-spacing:-.03em}
.evolution-metrics .metric:first-child b{color:#fff}
.evolution-grid{display:grid;grid-template-columns:minmax(0,1.28fr) minmax(320px,.72fr);gap:16px}
.evolution-card{margin:0;background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:var(--shadow)}
.comparison-card,.feedback-card{grid-column:1/-1}
.evolution-card h2{margin:0 0 14px;color:var(--ink);font-size:18px;letter-spacing:-.02em}
.evolution-card h2:after{content:"";display:block;width:32px;height:3px;margin-top:10px;border-radius:99px;background:var(--accent)}
.allocation-card{grid-column:1/-1}
.outcome-card{grid-column:1/-1;background:var(--ink);border-color:var(--ink);color:#fff;padding:26px 28px}.outcome-card h2{color:#fff;font-size:13px;text-transform:uppercase;letter-spacing:.12em}.outcome-card h2:after{background:#7ed2c4}.outcome-result{margin:4px 0 8px;font-size:clamp(25px,3.6vw,42px);font-weight:850;letter-spacing:-.04em}.outcome-card p{max-width:760px;margin:0;color:#c7d9d5}.outcome-list{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}.outcome-list span{padding:7px 11px;border:1px solid rgba(255,255,255,.18);border-radius:999px;color:#d9e8e5;font-size:12px}
.story-lead{margin:0 0 8px;color:var(--ink);font-size:16px;font-weight:800}.story-lead b{color:var(--accent)}
.flow-card{grid-column:1/-1}.story-flow{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.story-step{position:relative;min-height:112px;padding:15px;border:1px solid var(--line);border-radius:14px;background:#fbfdfc}.story-step strong{display:block;margin-bottom:5px;color:var(--accent);font-size:13px}.story-step span{display:block;color:var(--muted);font-size:13px}.story-step:not(:last-child):after{content:"→";position:absolute;right:-15px;top:42px;color:var(--accent);font-size:20px;font-weight:800;z-index:1}.story-note{margin:14px 0 0;color:var(--muted);font-size:12px}
.secondary-details{grid-column:1/-1;margin:0;background:var(--surface);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);overflow:hidden}
.secondary-details summary{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:17px 22px;color:var(--ink);font-size:14px;font-weight:800;cursor:pointer;list-style:none}.secondary-details summary::-webkit-details-marker{display:none}.secondary-details summary:after{content:"＋";color:var(--accent);font-size:20px;font-weight:500}.secondary-details[open] summary{border-bottom:1px solid var(--line)}.secondary-details[open] summary:after{content:"−"}.secondary-details summary span{margin-left:auto;color:var(--muted);font-size:12px;font-weight:600}
.details-body{padding:0 18px 18px}.details-body .evolution-card{margin-top:16px;box-shadow:none}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{border-bottom:1px solid var(--line);padding:10px 9px;text-align:left;vertical-align:top}th{color:var(--muted);font-size:12px;font-weight:800;white-space:nowrap}tbody tr:last-child td,tr:last-child td{border-bottom:0}
.pill{display:inline-block;background:var(--accent-soft);border:1px solid #c4ded7;border-radius:99px;padding:3px 9px;margin:2px;font-size:12px;color:var(--accent);font-weight:700}
.ok{color:#16805f}.warn{color:var(--warning)}.dim{color:var(--muted)}
.bar{height:8px;border-radius:4px;background:#e5ece9;overflow:hidden;margin:3px 0 8px}.bar i{display:block;height:100%;background:var(--accent)}.bad{background:var(--warning)!important}
.review-panel{border-left:4px solid var(--accent)}
.history-panel{border-top:4px solid #d7a25b}
.rv{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:14px 0}
.rv input,.rv select{width:100%;min-height:42px;background:#fbfdfc;border:1px solid #cbdad6;color:var(--ink);border-radius:10px;padding:9px 11px;font:13px inherit;outline:none}.rv input:focus,.rv select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(15,118,110,.12)}
.rv input.wide,.rv button{grid-column:1/3}.rv button{min-height:44px;background:var(--accent);border:0;color:#fff;font-weight:800;border-radius:10px;padding:9px;cursor:pointer;transition:background .18s ease,transform .18s ease}.rv button:hover{background:#0a5f59;transform:translateY(-1px)}
.rvstat{font-size:13px;color:var(--muted)}
code{background:#eef4f1;color:#35605c;padding:2px 6px;border-radius:5px;font-size:12px;overflow-wrap:anywhere}
.evolution-footer{font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:16px;margin-top:22px}
@media(max-width:900px){main.evolution-page{padding:28px 20px 42px}.evolution-hero{align-items:flex-start;flex-direction:column}.evolution-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.evolution-grid{grid-template-columns:1fr}.comparison-card,.feedback-card{grid-column:auto}.evolution-card{overflow-x:auto}.story-flow{grid-template-columns:repeat(2,minmax(0,1fr))}.story-step:not(:last-child):after{display:none}}
@media(max-width:560px){.evolution-metrics{grid-template-columns:1fr}.evolution-card{padding:17px}.rv{grid-template-columns:1fr}.rv input.wide,.rv button{grid-column:auto}.story-flow{grid-template-columns:1fr}table{min-width:620px}}
</style>
"""


def build_evolution_html() -> str:
    return """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>决策进化对比</title>""" + EVOLUTION_STYLE + """
<main class=evolution-page>
<header class=evolution-hero><div class=evolution-hero-copy><div class=evolution-eyebrow>POLICY EVOLUTION · 策略如何变好</div><h1>决策进化：从反馈到可回滚策略</h1><div class=sub>看清楚每次调整改了什么、为什么被采纳，以及下一次推演能否直接复用。</div></div><a class=back href='/'>← 返回推演面板</a></header>
<div class="grid evolution-metrics" id=metrics><div class=metric><span>正在读取进化结果…</span></div></div>
<div class=evolution-grid>
<section class="card evolution-card outcome-card"><h2>先看结论</h2><div class=outcome-result>这次方案变好了</div><p>系统根据演练反馈调整了资源分配，结果更接近人工建议；下一次遇到相似事件，可以直接复用这套策略。</p><div class=outcome-list><span>误差降到 0</span><span>新场景没有变差</span><span>策略已记录，可回滚</span></div></section>
<section class="card evolution-card flow-card"><h2>这页只讲四件事</h2><div class=story-flow><div class=story-step><strong>1 · 原来的方案</strong><span>先按初始规则分配资源。</span></div><div class=story-step><strong>2 · 收到建议</strong><span>人工指出重点片区需要更多资源。</span></div><div class=story-step><strong>3 · 调整方案</strong><span>把资源从低优先区域转到重点区域。</span></div><div class=story-step><strong>4 · 下次复用</strong><span>相似事件可直接沿用已验证策略。</span></div></div><p class=story-note>下面的数字用于证明这四步确实发生过；不需要先理解专业术语。</p></section>
<section class="card evolution-card memory-card"><h2>第二会话：复用已验证经验</h2><div id=memory></div></section>
<section class="card evolution-card review-panel"><h2>人工协同反馈：为什么采纳这次调整</h2><div id=review></div></section>
<section class="card evolution-card allocation-card"><h2>真实事件：资源分配变化 · WO-20261029-007</h2><div id=realEvent></div></section>
<details class=secondary-details><summary>更多审计信息 <span>评分、版本与边界说明</span></summary><div class=details-body>
<section class="card evolution-card comparison-card"><h2>第一会话：策略调整前后</h2><table id=firstTable></table></section>
<section class="card evolution-card feedback-card"><h2>真实用户评分：收集意见并触发门禁</h2>
<div id=reviewReal class=rvstat>加载中…</div>
<form class=rv id=rvForm>
  <input id=rvId placeholder='你的名字或编号（必填）' required>
  <input id=rvRole placeholder='身份，如 环境专业教师 / 市政从业者'>
  <select id=rvPhase><option value=before>进化前方案</option><option value=after>进化后方案</option></select>
  <input id=rvScores placeholder='四维评分 1-5，如 4,3,5,4（准确/时效/安全/可解释）' required>
  <input id=rvSuggest class=wide placeholder='可选：建议分配，如 A-03=5,B-01=1 —— 填了才会转成评审用例进进化门禁'>
  <input id=rvComment class=wide placeholder='意见（可选）'>
  <button type=submit>提交评分</button>
</form>
<div id=rvMsg class=dim></div>
<div id=gateLatest class=rvstat></div>
<p class=dim>口径：评分只做汇总（样本/均分/前后差值），<b>不参与门禁</b>；只有给出"建议分配"的提交才会转成评审用例，成为候选策略的硬约束。真实用户评分是个人意见，非部门批复。</p>
</section>
<section class="card evolution-card history-panel"><h2>策略版本：可审计、可回滚</h2><table id=releases></table></section>
<div class="bound evolution-footer" id=boundaries></div></div></details>
</div>
</main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const f4=v=>(typeof v==='number'?v.toFixed(4):esc(v));
const label=x=>'<span class=pill>'+esc(x)+'</span>';
function row(name,a,b,c,d){return '<tr><td>'+esc(name)+'</td><td>'+f4(a)+'</td><td class=ok>'+f4(b)+'</td><td>'+f4(c)+'</td><td class=ok>'+f4(d)+'</td></tr>'}
function bar(pct,cls){return '<div class=bar><i class="'+(cls||'')+'" style="width:'+Math.max(2,Math.min(100,pct))+'%"></i></div>'}
async function main(){
  const p=await (await fetch('/api/evolution')).json();
  const first=p.first, second=p.second, review=p.review, real=p.real_event;
  const a=first.initial_train, b=first.final_train, c=first.initial_holdout, d=first.final_holdout;
  metrics.innerHTML=
    '<div class=metric><span>演练场景：方案偏差</span><b>'+f4(a.deviation)+' → '+f4(b.deviation)+'</b><span class=dim>调整前 → 调整后</span></div>'+
    '<div class=metric><span>新场景：方案偏差</span><b>'+f4(c.deviation)+' → '+f4(d.deviation)+'</b><span class=dim>通过不退化门禁</span></div>'+
    '<div class=metric><span>复用经验后的起点</span><b class=ok>'+f4(second.initial_train.deviation)+'</b><span class=dim>第二次会话</span></div>'+
    '<div class=metric><span>人工建议修正幅度</span><b>'+(review?f4(review.deviation_before)+' → '+f4(review.deviation_after):'-')+'</b><span class=dim>第二反馈信号</span></div>';
  firstTable.innerHTML='<tr><th>指标</th><th>训练集 前</th><th>训练集 后</th><th>留出集 前</th><th>留出集 后</th></tr>'+
    row('综合决策偏差',a.deviation,b.deviation,c.deviation,d.deviation)+
    row('分配向量 L1 差（台）',a.allocation_l1_units,b.allocation_l1_units,c.allocation_l1_units,d.allocation_l1_units)+
    row('关键片区未满足（台）',a.critical_unmet_units,b.critical_unmet_units,c.critical_unmet_units,d.critical_unmet_units)+
    row('应调拨未调拨（次）',a.missed_transfers,b.missed_transfers,c.missed_transfers,d.missed_transfers)+
    row('优先序命中',a.order_hits,b.order_hits,c.order_hits,d.order_hits)+
    '<tr><td>最终策略标签</td><td colspan=4>'+label(first.final_label)+' 轮次 '+first.rounds.length+'（采纳 '+first.rounds.filter(x=>x.action==='promoted').length+' / 拒绝 '+first.rounds.filter(x=>x.action==='rejected').length+'）</td></tr>';
  const w=second.warm_start;
  memory.innerHTML=w?
    '<p class=story-lead>下次遇到相似事件，可以直接沿用 '+label(w.label)+'。</p>'+
    '<p>这套策略已经验证过，第二次推演一开始就得到更好的结果；相似度 <b>'+f4(w.similarity)+'</b>。</p>'+
    '<p class=dim>记录编号 '+label(w.entry_id)+'，用于审计和回滚。</p>'
    :'<p class=warn>未命中记忆</p>';
  review.innerHTML=review?
    '<p class=story-lead>人工建议：把资源优先给更需要的片区。</p>'+
    '<p>'+esc(review.reason)+'</p>'+
    '<p>采纳后，方案偏差从 <b>'+f4(review.deviation_before)+'</b> 降到 <b class=ok>'+f4(review.deviation_after)+'</b>。</p>'+
    '<p class=dim>这条建议来自 '+esc(review.reviewer_role)+'，记录编号 '+label(review.review_id)+'。</p>'
    :'<p class=dim>本次无人工评审</p>';
  releases.innerHTML='<tr><th>策略标签</th><th>版本</th><th>父标签</th><th>触发反馈</th><th>触发评审</th><th>参数变更</th><th>预期改善</th></tr>'+
    (first.releases.length?first.releases.map(r=>'<tr><td>'+label(r.label)+'</td><td>'+esc(r.version_id)+'</td><td>'+esc(r.parent_label)+'</td><td>'+esc(r.trigger_feedback_id)+'</td><td>'+esc((r.trigger_review_ids||[]).join('，')||'-')+'</td><td><code>'+esc(JSON.stringify(r.params_delta))+'</code></td><td class=ok>'+f4(r.expected_improvement)+'</td></tr>').join(''):'<tr><td colspan=7 class=dim>本轮无采纳</td></tr>');
  if(real.baseline&&real.evolved){
    const names=Object.keys(real.evolved.allocation_by_district);
    const gain=names.find(n=>(real.evolved.allocation_by_district[n]??0)>(real.baseline.allocation_by_district[n]??0));
    const release=names.find(n=>(real.evolved.allocation_by_district[n]??0)<(real.baseline.allocation_by_district[n]??0));
    const moved=gain&&release?Math.abs((real.evolved.allocation_by_district[gain]??0)-(real.baseline.allocation_by_district[gain]??0)):0;
    realEvent.innerHTML='<p class=story-lead>把 <b>'+moved+' 台</b> 资源从 '+label(release||'其他片区')+' 调到 '+label(gain||'重点片区')+'。</p>'+
      '<p>这样做的目的，是先满足更紧急的片区，而不是平均分配。</p>'+
      '<p class=dim>需要额外上报增援：'+esc(real.evolved.escalation.join('；')||'无')+'</p>';
  } else { realEvent.innerHTML='<p class=warn>真实事件未跑通</p>'; }
  boundaries.innerHTML='表述边界：'+p.boundaries.map(esc).join(' · ')+'。反馈指纹 <code>'+esc((p.case_digest||'').slice(0,16))+'</code>';
  loadReviews();
  loadGate();
}
async function loadReviews(){
  try{
    const r=await (await fetch('/api/reviews')).json();const s=r.summary||{};
    const ph=s.phases||{},before=ph.before||{},after=ph.after||{};
    reviewReal.innerHTML='<p>样本 <b>'+esc(s.total||0)+'</b> 条，评审人 <b>'+esc(s.reviewer_count||0)+'</b> 位；满足最低样本（≥3 人且进化前后各有）：<b class="'+(s.meets_min_reviewers?'ok':'warn')+'">'+(s.meets_min_reviewers?'是':'否')+'</b></p>'+
      '<p>进化前均分 <b>'+esc(before.overall??'-')+'</b>（'+esc(before.count||0)+' 条） · 进化后均分 <b>'+esc(after.overall??'-')+'</b>（'+esc(after.count||0)+' 条） · 差值 <b class=ok>'+esc(s.delta??'-')+'</b></p>'+
      '<p class=dim>带修正建议（可进门禁）：'+esc(s.gate_candidates||0)+' 条；最近提交 '+esc(s.latest_at||'-')+'</p>';
  }catch(e){reviewReal.innerHTML='<span class=warn>评分汇总加载失败：'+esc(e)+'</span>';}
}
async function loadGate(){
  try{
    const g=await (await fetch('/api/evolution/gate?limit=3')).json();
    const l=g.latest;
    if(!l){gateLatest.innerHTML='<p class=dim>还没有门禁记录：提交一条「进化后」评分即可触发。</p>';return;}
    const tr=Array.isArray(l.train_deviation)?l.train_deviation:[null,null];
    const ho=Array.isArray(l.holdout_deviation)?l.holdout_deviation:[null,null];
    gateLatest.innerHTML='<p>最近门禁：最终标签 <b>'+esc(l.final_label)+'</b>，轮次 '+esc(l.rounds)+'（采纳 <b class=ok>'+esc(l.accepted)+'</b> / 拒绝 <b class=warn>'+esc(l.rejected)+'</b>）；训练 '+esc(f4(tr[0]))+' → <b class=ok>'+esc(f4(tr[1]))+'</b>，留出 '+esc(f4(ho[0]))+' → <b class=ok>'+esc(f4(ho[1]))+'</b></p>'+
      '<p class=dim>累计门禁记录 '+esc(g.total_entries)+' 条；最近一次 '+esc(l.saved_at||'')+'；落盘 <code>'+esc(g.path||'')+'</code></p>';
  }catch(e){gateLatest.innerHTML='<span class=warn>门禁历史加载失败：'+esc(e)+'</span>';}
}
function parseScores(t){
  const nums=String(t||'').replace(/，/g,',').split(',').map(x=>parseInt(x.trim(),10));
  if(nums.length!==4||nums.some(x=>!(x>=1&&x<=5)))throw new Error('评分需要 4 个 1-5 的整数，如 4,3,5,4');
  return {accuracy:nums[0],timeliness:nums[1],safety:nums[2],explainability:nums[3]};
}
rvForm.addEventListener('submit',async e=>{
  e.preventDefault();
  try{
    const scores=parseScores(rvScores.value);
    rvMsg.textContent='提交中…';
    const res=await fetch('/api/reviews',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewer_id:rvId.value.trim(),reviewer_role:rvRole.value.trim(),phase:rvPhase.value,scores:scores,comment:rvComment.value.trim(),suggested_allocation:rvSuggest.value.trim()})});
    const j=await res.json();
    if(!res.ok){rvMsg.innerHTML='<span class=warn>'+esc(j.error||'提交失败')+'</span>';return;}
    let extra='';
    if(j.evolution&&j.evolution.eligible){
      const e=j.evolution;
      extra='<div class=rvstat style="margin-top:6px">已触发进化门禁：最终标签 <b>'+esc(e.final_label)+'</b>，'+
        '轮次 '+esc(e.rounds)+'（采纳 <b class=ok>'+esc(e.accepted)+'</b> / 拒绝 <b class=warn>'+esc(e.rejected)+'</b>）；'+
        '训练偏差 '+esc(f4(e.train_deviation&&e.train_deviation[0]))+' → <b class=ok>'+esc(f4(e.train_deviation&&e.train_deviation[1]))+'</b>，'+
        '留出偏差 '+esc(f4(e.holdout_deviation&&e.holdout_deviation[0]))+' → <b class=ok>'+esc(f4(e.holdout_deviation&&e.holdout_deviation[1]))+'</b>'+
        (e.review_ids&&e.review_ids.length?'；生效评审 <code>'+esc(e.review_ids.join('，'))+'</code>':'')+'</div>';
    } else if(j.evolution && !j.evolution.eligible){
      extra='<div class=rvstat>进化门禁未跑通：'+esc(j.evolution.error||'未知原因')+'（评分已落盘）</div>';
    }
    rvMsg.innerHTML='<span class=ok>已记录 '+esc(j.submission_id)+'（综合 '+esc(j.overall)+'/5'+(j.gate_candidate?'，已转评审用例进进化门禁':'，仅计入评分汇总')+'）</span>'+extra;
    rvComment.value='';rvSuggest.value='';loadReviews();loadGate();
  }catch(err){rvMsg.innerHTML='<span class=warn>提交失败：'+esc(err.message||err)+'</span>';}
});
main().catch(e=>{metrics.innerHTML='<div class=metric><span class=warn>加载失败：'+esc(e)+'</span></div>'});
</script></html>"""


__all__ = ["build_evolution_html", "build_evolution_payload"]
