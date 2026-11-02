"""MCP over SSE：把已有的城市孪生 MCP 工具挂到 Nexent 上。

为什么需要它
------------
Nexent 的「MCP 服务接入」只认 HTTP/SSE 端点（`http://<host>:<port>/sse`），
而本仓库的 MCP 服务此前只有 stdio 传输。于是出现一个尴尬事实：

    智能体在 Nexent 上，工具在本地 stdio 进程里，两边从未真正连上。

本模块只做传输层，不复制任何业务能力：工具清单、参数 schema、错误信封全部来自
`mcp_server.UrbanToolkit` / `mcp_server.McpServer`（P10 之后是 12 个工具）。业务
改动只要发生在原模块，SSE 侧自动同步，不存在第二套工具定义。

协议实现（MCP SSE transport，2024-11-05）：

    客户端 GET /sse
      ← event: endpoint
        data: /messages?session_id=<id>
    客户端 POST /messages?session_id=<id>  (JSON-RPC，可能是数组)
      → 202 Accepted（响应不在这里回，走 SSE）
      ← event: message
        data: {"jsonrpc":"2.0","id":1,"result":{...}}

三条边界：

1. **只读演示用途**：无鉴权、无 TLS，默认绑定 127.0.0.1（Docker 里的 Nexent 用
   `host.docker.internal` 访问宿主机）。上公网必须换反代 + 鉴权，不在本模块职责内。
2. **不引入依赖**：用标准库 `http.server` 手写 SSE，不装 FastMCP。
3. **不碰共享文件**：不改 `mcp_server.py`，避免与并行开发冲突；入口是独立 CLI。
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from .mcp_server import McpServer, UrbanToolkit

SSE_ENDPOINT = "/sse"
POST_ENDPOINT = "/messages"
HEALTH_ENDPOINT = "/health"
KEEPALIVE_SECONDS = 15


class SseMcpServer:
    """把 `McpServer` 挂到 HTTP/SSE 上；一个实例对应一个端口。"""

    def __init__(
        self,
        toolkit: UrbanToolkit | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> None:
        self.toolkit = toolkit if toolkit is not None else UrbanToolkit()
        self.mcp = McpServer(self.toolkit)
        self.sessions: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._httpd = ThreadingHTTPServer((host, port), _handler_class())
        # 端口传 0 时由系统分配，测试与并行演示都用它拿真实端口
        self._httpd.toolkit = self.toolkit  # type: ignore[attr-defined]
        self._httpd.mcp = self.mcp  # type: ignore[attr-defined]
        self._httpd.sessions = self.sessions  # type: ignore[attr-defined]
        bound_host, bound_port = self._httpd.server_address[:2]
        self.host = str(bound_host)
        self.port = int(bound_port)
        self._thread: threading.Thread | None = None

    @property
    def sse_url(self) -> str:
        """给 Nexent「MCP 配置」填的地址；Docker 部署时把 127.0.0.1 换成 host.docker.internal。"""
        return f"http://{self.host}:{self.port}{SSE_ENDPOINT}"

    def start(self) -> "SseMcpServer":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._httpd.serve_forever, name="mcp-sse", daemon=True
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def __enter__(self) -> "SseMcpServer":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def _handler_class() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server: ThreadingHTTPServer

        # --- GET --------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == SSE_ENDPOINT:
                self._stream()
            elif path == HEALTH_ENDPOINT:
                tools = self.server.toolkit.tools()  # type: ignore[attr-defined]
                self._json(
                    200,
                    {
                        "status": "ok",
                        "transport": "sse",
                        "server": self.server.mcp.__class__.__module__,  # type: ignore[attr-defined]
                        "tools_count": len(tools),
                        "tools": [item["name"] for item in tools],
                    },
                )
            elif path == POST_ENDPOINT:
                self._json(405, {"error": "messages 端点只接受 POST"})
            else:
                self._json(404, {"error": f"未知路径: {path}"})

        # --- POST -------------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != POST_ENDPOINT:
                self._json(404, {"error": f"未知路径: {parsed.path}"})
                return
            session_id = (parse_qs(parsed.query).get("session_id") or [""])[0]
            sink = self.server.sessions.get(session_id)  # type: ignore[attr-defined]
            if sink is None:
                self._json(404, {"error": f"未知会话: {session_id}"})
                return

            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "null")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"解析失败: {exc}"}})
                return

            messages = payload if isinstance(payload, list) else [payload]
            for message in messages:
                if not isinstance(message, Mapping):
                    self._json(
                        400,
                        {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "请求必须是 JSON 对象"}},
                    )
                    return
                response = self.server.mcp.handle(message)  # type: ignore[attr-defined]
                if response is not None:
                    sink.put(response)
            self._empty(202)

        # --- 输出 -------------------------------------------------------
        def _event(self, name: str, data: str) -> None:
            body = f"event: {name}\ndata: {data}\n\n".encode("utf-8")
            self.wfile.write(body)
            self.wfile.flush()

        def _comment(self, text: str) -> None:
            self.wfile.write(f": {text}\n\n".encode("utf-8"))
            self.wfile.flush()

        def _json(self, status: int, payload: Mapping[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _empty(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            session_id = uuid4().hex
            sink: queue.Queue[dict[str, Any]] = queue.Queue()
            self.server.sessions[session_id] = sink  # type: ignore[attr-defined]
            try:
                self._event("endpoint", f"{POST_ENDPOINT}?session_id={session_id}")
                while True:
                    try:
                        response = sink.get(timeout=KEEPALIVE_SECONDS)
                    except queue.Empty:
                        self._comment("keepalive")
                        continue
                    self._event("message", json.dumps(response, ensure_ascii=False))
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.server.sessions.pop(session_id, None)  # type: ignore[attr-defined]
                self.close_connection = True

        def log_message(self, *_args: Any) -> None:  # 静默：SSE 长连接不刷屏
            return

    return Handler


def _build_toolkit(dataset: str | None, ledger: str | None, version: str, groups: list[str] | None, visual_root: str | None) -> UrbanToolkit:
    try:
        from .visualize import tool_log_path

        default_log = str(tool_log_path())
    except Exception:  # 可视化层可选；缺了也不影响决策工具
        default_log = None
    return UrbanToolkit(
        dataset,
        knowledge_version=version,
        ledger_dir=ledger,
        log_path=default_log,
        visual_root=visual_root,
        groups=groups,
    )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    import argparse

    parser = argparse.ArgumentParser(
        description="MCP server over SSE for Nexent integration (Nexent 只认 HTTP/SSE)"
    )
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址；Docker 内 Nexent 用 host.docker.internal 访问宿主机")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--version", default="kb-v1.0.0")
    parser.add_argument("--visual-root", default=None)
    parser.add_argument("--groups", default=None, help="只暴露指定工具分组，逗号分隔")
    args = parser.parse_args(argv)

    toolkit = _build_toolkit(
        args.dataset,
        args.ledger,
        args.version,
        [item for item in (args.groups or "").split(",") if item] or None,
        args.visual_root,
    )
    server = SseMcpServer(toolkit, host=args.host, port=args.port)
    print(f"MCP SSE 已启动：{server.sse_url}", flush=True)
    print(f"健康检查：http://{server.host}:{server.port}{HEALTH_ENDPOINT}", flush=True)
    print("Nexent 配置：智能体开发 → 选择Agent的工具 → MCP配置 → 填上面的 SSE 地址", flush=True)
    print("Docker 部署的 Nexent 访问宿主机时，把 127.0.0.1 换成 host.docker.internal", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


__all__ = ["SSE_ENDPOINT", "POST_ENDPOINT", "HEALTH_ENDPOINT", "SseMcpServer", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
