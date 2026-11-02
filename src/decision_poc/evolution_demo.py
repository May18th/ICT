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


def build_evolution_html() -> str:
    return """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>决策进化对比</title>
<style>
body{font:15px/1.6 system-ui;margin:0;background:#0b1220;color:#e8eef7}
main{max-width:1180px;margin:auto;padding:28px}
h1{color:#7dd3fc;margin:0 0 4px}.sub{color:#9fb1c8}
a.back{color:#7dd3fc;text-decoration:none;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}
.metric{background:#152238;border:1px solid #263a55;border-radius:10px;padding:14px}
.metric b{display:block;font-size:22px;color:#7dd3fc}.metric span{font-size:12px;color:#9fb1c8}
.card{background:#152238;border:1px solid #263a55;border-radius:10px;padding:16px;margin:16px 0}
.card h2{font-size:16px;margin:0 0 12px;color:#bae6fd}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{border-bottom:1px solid #263a55;padding:7px 8px;text-align:left}th{color:#9fb1c8;font-weight:600}
.pill{display:inline-block;background:#20344d;border:1px solid #334155;border-radius:99px;padding:2px 9px;margin:2px;font-size:12px}
.ok{color:#86efac}.warn{color:#fbbf24}.dim{color:#9fb1c8}
.bar{height:8px;border-radius:4px;background:#263a55;overflow:hidden;margin:3px 0 8px}
.bar i{display:block;height:100%;background:#38bdf8}
.bad{background:#f97316!important}
.bound{font-size:12px;color:#9fb1c8;border-top:1px solid #263a55;padding-top:12px;margin-top:18px}
.rv{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:10px 0}
.rv input,.rv select{background:#0e1a2c;border:1px solid #334155;color:#e8eef7;border-radius:8px;padding:8px;font:13px system-ui}
.rv input.wide,.rv button{grid-column:1/3}
.rv button{background:#38bdf8;border:0;color:#04121f;font-weight:700;border-radius:8px;padding:9px;cursor:pointer}
.rvstat{font-size:13px;color:#9fb1c8}
code{background:#0e1a2c;padding:1px 5px;border-radius:4px;font-size:12px}
@media(max-width:800px){.grid{grid-template-columns:1fr 1fr}}
</style>
<main>
<div><a class=back href='/'>← 返回推演面板</a></div>
<h1>决策进化对比</h1><div class=sub>同一条推演链：初始决策 → 反馈信号 → 策略调整 → 偏差缩小 → 记忆沉淀</div>
<div class=grid id=metrics><div class=metric><span>加载中…</span></div></div>
<section class=card><h2>第一会话：进化前 vs 进化后</h2><table id=firstTable></table></section>
<section class=card><h2>第二会话：分层记忆 warm start</h2><div id=memory></div></section>
<section class=card><h2>人工协同反馈（第二信号）</h2><div id=review></div></section>
<section class=card><h2>真实用户评分（3–5 人 · 进化前后各一次）</h2>
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
<section class=card><h2>策略版本标签（可审计、可回滚）</h2><table id=releases></table></section>
<section class=card><h2>真实事件分配对比：WO-20261029-007</h2><div id=realEvent></div></section>
<div class=bound id=boundaries></div>
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
    '<div class=metric><span>训练集综合偏差</span><b>'+f4(a.deviation)+' → '+f4(b.deviation)+'</b><span class=dim>进化前 → 后</span></div>'+
    '<div class=metric><span>留出集综合偏差</span><b>'+f4(c.deviation)+' → '+f4(d.deviation)+'</b><span class=dim>不退化门禁</span></div>'+
    '<div class=metric><span>第二会话起点偏差</span><b class=ok>'+f4(second.initial_train.deviation)+'</b><span class=dim>记忆 warm start</span></div>'+
    '<div class=metric><span>人工评审偏差</span><b>'+(review?f4(review.deviation_before)+' → '+f4(review.deviation_after):'-')+'</b><span class=dim>第二反馈信号</span></div>';
  firstTable.innerHTML='<tr><th>指标</th><th>训练集 前</th><th>训练集 后</th><th>留出集 前</th><th>留出集 后</th></tr>'+
    row('综合决策偏差',a.deviation,b.deviation,c.deviation,d.deviation)+
    row('分配向量 L1 差（台）',a.allocation_l1_units,b.allocation_l1_units,c.allocation_l1_units,d.allocation_l1_units)+
    row('关键片区未满足（台）',a.critical_unmet_units,b.critical_unmet_units,c.critical_unmet_units,d.critical_unmet_units)+
    row('应调拨未调拨（次）',a.missed_transfers,b.missed_transfers,c.missed_transfers,d.missed_transfers)+
    row('优先序命中',a.order_hits,b.order_hits,c.order_hits,d.order_hits)+
    '<tr><td>最终策略标签</td><td colspan=4>'+label(first.final_label)+' 轮次 '+first.rounds.length+'（采纳 '+first.rounds.filter(x=>x.action==='promoted').length+' / 拒绝 '+first.rounds.filter(x=>x.action==='rejected').length+'）</td></tr>';
  const w=second.warm_start;
  memory.innerHTML=w?
    '<p>命中经验条目 '+label(w.entry_id)+' 标签 '+label(w.label)+' 场景相似度 <b>'+f4(w.similarity)+'</b></p>'+
    '<p class=dim>该条目历史结果：偏差 '+f4(w.source_outcome.deviation)+'、应调拨未调拨 '+esc(w.source_outcome.missed_transfers)+'、关键片区未满足 '+esc(w.source_outcome.critical_unmet_units)+' 台</p>'+
    '<p>第二次会话：起点偏差 <b class=ok>'+f4(second.initial_train.deviation)+'</b>，只需 <b>'+second.rounds+'</b> 轮收敛探针（采纳 '+second.promoted_rounds+' 次）——记忆里存的是已验证有效的策略。</p>'+
    (p.memory.path?'<p class=dim>记忆落盘：<code>'+esc(p.memory.path)+'</code>（追加式，跨进程可复用）</p>':'')
    :'<p class=warn>未命中记忆</p>';
  review.innerHTML=review?
    '<p>'+label(review.review_id)+' '+esc(review.reviewer_role)+' → <b>'+esc(review.decision)+'</b></p>'+
    '<p>'+esc(review.reason)+'</p>'+
    '<p>修正后分配：<code>'+esc(JSON.stringify(review.expected_allocation))+'</code>；评审用例偏差 <b>'+f4(review.deviation_before)+' → <span class=ok>'+f4(review.deviation_after)+'</span></b></p>'+
    '<p class=dim>口径：'+esc(review.reviewer_note)+'；评审结论已转成门禁硬约束，不满足的候选一律被拒。</p>'
    :'<p class=dim>本次无人工评审</p>';
  releases.innerHTML='<tr><th>策略标签</th><th>版本</th><th>父标签</th><th>触发反馈</th><th>触发评审</th><th>参数变更</th><th>预期改善</th></tr>'+
    (first.releases.length?first.releases.map(r=>'<tr><td>'+label(r.label)+'</td><td>'+esc(r.version_id)+'</td><td>'+esc(r.parent_label)+'</td><td>'+esc(r.trigger_feedback_id)+'</td><td>'+esc((r.trigger_review_ids||[]).join('，')||'-')+'</td><td><code>'+esc(JSON.stringify(r.params_delta))+'</code></td><td class=ok>'+f4(r.expected_improvement)+'</td></tr>').join(''):'<tr><td colspan=7 class=dim>本轮无采纳</td></tr>');
  if(real.baseline&&real.evolved){
    const names=Object.keys(real.evolved.allocation_by_district);
    realEvent.innerHTML='<table><tr><th>片区</th><th>进化前分配</th><th>进化后分配</th><th>进化前未满足</th><th>进化后未满足</th></tr>'+
      names.map(n=>'<tr><td>'+esc(n)+'</td><td>'+esc(real.baseline.allocation_by_district[n]??0)+'</td><td class=ok>'+esc(real.evolved.allocation_by_district[n]??0)+'</td><td>'+esc(real.baseline.unmet_by_district[n]??0)+'</td><td>'+esc(real.evolved.unmet_by_district[n]??0)+'</td></tr>').join('')+'</table>'+
      '<p>调拨：'+esc(real.evolved.transfers.join('；')||'无')+'</p>'+
      '<p class=dim>需上报增援：'+esc(real.evolved.escalation.join('；')||'无')+'</p>';
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
