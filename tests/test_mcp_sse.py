"""MCP over SSE 测试：Nexent 接入前先把传输层钉死。

不依赖 Docker、不触网：本地起一个真 HTTP 服务，用原始 SSE 客户端走一遍
「endpoint 事件 → POST JSON-RPC → SSE 回传响应」，覆盖握手、列工具、调工具、
批量消息、错误路径。
"""

from __future__ import annotations

import http.client
import json
from urllib.parse import parse_qs, urlparse

import pytest

from decision_poc.mcp_server import UrbanToolkit
from decision_poc.mcp_sse import HEALTH_ENDPOINT, SSE_ENDPOINT, SseMcpServer

EVENT = "WO-20261029-007"
AS_OF = "2026-10-29T09:40:00+08:00"


class SseClient:
    """最小 SSE 客户端：只做 MCP 需要的那点事，不引第三方库。"""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.connection = http.client.HTTPConnection(host, port, timeout=10)
        self.response = None
        self.session = ""

    def open(self) -> str:
        self.connection.request("GET", SSE_ENDPOINT)
        self.response = self.connection.getresponse()
        assert self.response.status == 200
        assert "text/event-stream" in self.response.getheader("Content-Type", "")
        event, data = self.next_event()
        assert event == "endpoint", (event, data)
        parsed = urlparse(data)
        self.session = parse_qs(parsed.query)["session_id"][0]
        return data

    def next_event(self) -> tuple[str, str]:
        event = ""
        data = ""
        while True:
            line = (self.response.readline() or b"").decode("utf-8").rstrip("\r\n")
            if line == "":
                return event, data
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]

    def next_message(self) -> dict:
        event, data = self.next_event()
        assert event == "message", (event, data)
        return json.loads(data)

    def post(self, payload) -> int:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        connection.request(
            "POST",
            f"/messages?session_id={self.session}",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        return response.status

    def close(self) -> None:
        try:
            self.connection.close()
        finally:
            self.session = ""

    def __enter__(self) -> "SseClient":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@pytest.fixture(scope="module")
def server() -> SseMcpServer:
    running = SseMcpServer(UrbanToolkit(), host="127.0.0.1", port=0).start()
    yield running
    running.stop()


def _get(host: str, port: int, path: str) -> tuple[int, dict]:
    connection = http.client.HTTPConnection(host, port, timeout=10)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, json.loads(body) if body else {}


def test_health_reports_the_live_tool_count(server: SseMcpServer) -> None:
    status, payload = _get(server.host, server.port, HEALTH_ENDPOINT)
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["transport"] == "sse"
    assert payload["tools_count"] == len(server.toolkit.tools())
    assert "allocation_plan" in payload["tools"]


def test_sse_endpoint_shape_matches_nexent_config(server: SseMcpServer) -> None:
    assert server.sse_url.endswith("/sse")
    assert server.port > 0


def test_handshake_returns_initialize_result_over_sse(server: SseMcpServer) -> None:
    with SseClient(server.host, server.port) as client:
        assert client.session
        assert client.post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        ) == 202
        message = client.next_message()
        assert message["id"] == 1
        assert message["result"]["protocolVersion"]
        assert message["result"]["capabilities"]["tools"]


def test_tools_list_exposes_every_tool(server: SseMcpServer) -> None:
    with SseClient(server.host, server.port) as client:
        assert client.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) == 202
        message = client.next_message()
        names = [item["name"] for item in message["result"]["tools"]]
        assert names == [item["name"] for item in server.toolkit.tools()]
        assert "event_deduce" in names and "allocation_plan" in names


def test_tool_call_returns_the_same_tool_result_envelope(server: SseMcpServer) -> None:
    with SseClient(server.host, server.port) as client:
        request = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "event_deduce",
                "arguments": {"event_id": EVENT, "as_of": AS_OF, "caller_labels": ["ORG-EMERG"]},
            },
        }
        assert client.post(request) == 202
        message = client.next_message()
        content = message["result"]["content"][0]
        assert content["type"] == "text"
        envelope = json.loads(content["text"])
        # 与 stdio / 进程内调用完全同一份 ToolResult 信封
        assert envelope["status"] == "ok"
        payload = envelope["data"]
        assert payload["event_id"] == EVENT
        stages = [step["stage"] for step in payload["steps"]]
        assert {"observed", "affected", "owner", "conflict", "resource", "action"} <= set(stages)
        assert [item["conflict_id"] for item in payload["conflicts"]] == ["C1", "C2", "C3"]
        assert message["result"]["isError"] is False


def test_allocation_plan_is_callable_from_sse(server: SseMcpServer) -> None:
    with SseClient(server.host, server.port) as client:
        request = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "allocation_plan",
                "arguments": {
                    "event_id": EVENT,
                    "as_of": AS_OF,
                    "caller_labels": ["ORG-EMERG"],
                    "policy": {"weight_closure": 1.0, "weight_hazard": 0.25, "transfer_penalty": 0.1},
                },
            },
        }
        assert client.post(request) == 202
        envelope = json.loads(client.next_message()["result"]["content"][0]["text"])
        assert envelope["status"] == "ok"
        allocation = envelope["data"]["allocation"]
        assert allocation["allocation_by_district"] == {"A-03": 5, "B-01": 1}
        assert allocation["transfers"][0]["qty"] == 1


def test_batch_messages_share_one_sse_stream(server: SseMcpServer) -> None:
    with SseClient(server.host, server.port) as client:
        batch = [
            {"jsonrpc": "2.0", "id": 5, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]
        assert client.post(batch) == 202
        message = client.next_message()
        assert message["id"] == 5
        assert message["result"] == {}


def test_unknown_session_is_rejected(server: SseMcpServer) -> None:
    connection = http.client.HTTPConnection(server.host, server.port, timeout=10)
    body = json.dumps({"jsonrpc": "2.0", "id": 6, "method": "ping"}).encode("utf-8")
    connection.request(
        "POST",
        "/messages?session_id=not-a-session",
        body=body,
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    response.read()
    connection.close()
    assert response.status == 404


def test_malformed_json_is_rejected(server: SseMcpServer) -> None:
    with SseClient(server.host, server.port) as client:
        connection = http.client.HTTPConnection(server.host, server.port, timeout=10)
        connection.request(
            "POST",
            f"/messages?session_id={client.session}",
            body=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        assert response.status == 400
        assert payload["error"]["code"] == -32700


def test_unknown_path_is_rejected(server: SseMcpServer) -> None:
    status, payload = _get(server.host, server.port, "/nope")
    assert status == 404
    assert "error" in payload
