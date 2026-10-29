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
    return """<!doctype html><meta charset=utf-8><title>Urban Twin</title>
<style>body{font:16px system-ui;margin:2rem;background:#101827;color:#e8eef7}button{padding:.6rem 1rem}pre{white-space:pre-wrap;background:#18253a;padding:1rem;border-radius:8px}h1{color:#7dd3fc}</style>
<h1>城市内涝跨部门事件推演</h1><p>暴雨 + 道路封闭 + 地下管线施工冲突</p><button onclick="run()">运行演示</button><pre id=o>点击运行</pre>
<script>async function run(){o.textContent='推演中…';const r=await fetch('/api/demo');o.textContent=JSON.stringify(await r.json(),null,2)}</script>"""


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
