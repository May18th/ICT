"""Dependency-free demo payload and browser server for the urban twin."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .skills import SkillPipeline, SkillRuntime
from .semantic import build_urban_semantic_store

EVENT_ID = "WO-20261029-007"
AS_OF = "2026-10-29T09:40:00+08:00"


def build_demo_payload(*, event_id: str = EVENT_ID, as_of: str = AS_OF) -> dict[str, Any]:
    report = SkillPipeline(SkillRuntime()).run(
        event_id, as_of, caller_labels=["ORG-EMERG"], persist=False
    )
    payload = report.model_dump(mode="json")
    payload["evidence"] = payload.get("audit", {}).get("claims", [])
    return payload


def build_html() -> str:
    return """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Urban Twin</title>
<style>body{font:15px system-ui;margin:0;background:#0b1220;color:#e8eef7}main{max-width:1180px;margin:auto;padding:28px}h1{color:#7dd3fc;margin-bottom:4px}.sub{color:#9fb1c8}.toolbar{display:flex;gap:12px;align-items:center;margin:22px 0}button{background:#38bdf8;border:0;border-radius:6px;padding:10px 18px;font-weight:700;cursor:pointer}.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:16px}.card{background:#152238;border:1px solid #263a55;border-radius:10px;padding:16px}.card h2{font-size:17px;margin:0 0 12px;color:#bae6fd}.metric{display:inline-block;background:#20344d;border-radius:7px;padding:10px;margin:0 8px 8px 0}.metric b{display:block;font-size:22px;color:#7dd3fc}.pill{display:inline-block;background:#334155;padding:3px 8px;border-radius:99px;margin:3px;font-size:12px}.warn{color:#fbbf24}.ok{color:#86efac}.trace{border-left:2px solid #38bdf8;padding-left:12px;margin:8px 0}.evidence{font-size:12px;color:#b8c7d9;word-break:break-all}svg{width:100%;height:330px;background:#0e1a2c;border-radius:8px}.road{stroke:#64748b;stroke-width:8}.pipe{stroke:#f97316;stroke-width:3;stroke-dasharray:7 5}.flood{fill:#38bdf8;opacity:.35}.node{fill:#fbbf24;stroke:#fff;stroke-width:2}@media(max-width:800px){.grid{grid-template-columns:1fr}}</style>
<main><h1>城市内涝跨部门事件推演</h1><div class=sub>暴雨 + 道路封闭 + 地下管线施工冲突 · 证据链可追溯</div><div class=toolbar><button onclick="run()">运行演示</button><span id=status>等待运行</span></div><section id=summary class=card><h2>事件概览</h2><div id=metrics>点击“运行演示”加载推演结果</div></section><div class=grid><section class=card><h2>数字孪生影响范围</h2><svg viewBox='0 0 700 330' aria-label='影响范围示意图'><path class=road d='M40 260 L660 70 M40 80 L660 260'/><path class=pipe d='M80 250 L400 160 L630 150'/><ellipse class=flood cx=350 cy=165 rx=130 ry=80/><circle class=node cx=350 cy=165 r=8/><text x=365 y=160 fill='#e8eef7'>A-03 积水区</text></svg><div id=assets></div></section><section class=card><h2>跨部门协同</h2><div id=departments></div><h2>资源与风险</h2><div id=risks></div></section></div><section class=card style='margin-top:16px'><h2>推理链路与证据</h2><div id=trace></div></section></main>
<script>const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function list(v){return (v||[]).map(x=>'<span class=pill>'+esc(typeof x==='string'?x:(x.name||x.id||JSON.stringify(x)))+'</span>').join('')||'<span class=warn>无</span>'}
async function run(){status.textContent='推演中…';const r=await fetch('/api/demo');const p=await r.json();const f=p.facts||{},d=p.deduction||{};metrics.innerHTML='<span class=metric><b>'+esc(f.response_level||'-')+'</b>响应等级</span><span class=metric><b>'+esc(f.depth_cm||'-')+' cm</b>积水深度</span><span class=metric><b>'+esc(p.knowledge_version)+'</b>知识版本</span><span class=metric><b class=ok>'+esc(p.audit?.citation_completeness??'-')+'</b>引用完整率</span>';assets.innerHTML='<b>受影响对象</b><div>'+list(p.impact?.affected_objects||d.affected_assets||[])+'</div>';departments.innerHTML=list(d.departments||p.deduction?.departments||p.facts?.departments||[]);risks.innerHTML='<div>'+list(d.conflicts||[])+'</div><div>'+list(d.resource_gaps||[])+'</div><div class=warn>'+list(d.residual_risks||[])+'</div>';trace.innerHTML=(p.evidence||[]).slice(0,12).map(c=>'<div class=trace><b>'+esc(c.text||c.claim_id)+'</b><div class=evidence>'+esc((c.evidence_refs||[]).join(' · '))+'</div></div>').join('')||'<span class=warn>暂无可展示证据</span>';status.textContent=p.publishable?'可发布结论':'需要人工复核'}</script></html>"""


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/demo":
                body = json.dumps(build_demo_payload(), ensure_ascii=False).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            else:
                body = build_html().encode("utf-8")
                content_type = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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


__all__ = ["AS_OF", "EVENT_ID", "build_demo_payload", "build_html", "serve"]
