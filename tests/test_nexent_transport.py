import json
from typing import Any

import httpx
import pytest

from decision_poc.contracts import DecisionRequest
from decision_poc.nexent_adapter import NexentAdapter
from decision_poc.nexent_transport import (
    NexentAuthError,
    NexentConfigError,
    NexentConflictError,
    NexentHttpTransport,
    NexentSettings,
    NexentStreamError,
    NexentTransportError,
    build_decision_query,
    parse_decision_output,
)

SETTINGS = NexentSettings(
    base_url="https://nexent.example.com",
    api_key="key-1",
    agent_name="decision-agent",
)


def _decision(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "request_id": "req-1",
        "summary": "choose option a",
        "options": [
            {"option_id": "a", "description": "first", "expected_impact": "high"}
        ],
        "selected_option_id": "a",
        "confidence": 0.8,
        "risk_level": "low",
        "risks": ["scope creep"],
        "evidence": [{"source": "runbook", "content": "step 1"}],
    }
    payload.update(overrides)
    return payload


def _sse(*events: dict[str, Any]) -> bytes:
    return "".join(
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events
    ).encode("utf-8")


def _transport(handler, *, max_attempts: int = 2) -> NexentHttpTransport:
    return NexentHttpTransport(
        settings=SETTINGS,
        max_attempts=max_attempts,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )


def _request_payload() -> dict[str, Any]:
    return DecisionRequest(
        request_id="req-1",
        goal="choose",
        context={"team": "ict"},
        constraints=["safe"],
    ).model_dump(mode="json")


def test_transport_aggregates_stream_into_decision():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "conversation_id": "42",
                "X-Request-Id": "srv-1",
            },
            content=_sse(
                {"type": "conversation_created", "content": {"conversation_id": 42}},
                {"type": "model_output_thinking", "content": "分析", "unit_index": 1},
                {
                    "type": "final_answer",
                    "content": json.dumps(_decision(), ensure_ascii=False),
                    "unit_index": 2,
                },
            ),
        )

    result = _transport(handler)(_request_payload())

    assert result["status"] == "accepted"
    assert result["request_id"] == "srv-1"
    assert result["conversation_id"] == 42
    assert result["decision"]["selected_option_id"] == "a"
    assert "decision_error" not in result
    assert result["events"]["final_answer"] == 1


def test_transport_sends_contract_headers_and_body():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse({"type": "final_answer", "content": "done"}),
        )

    _transport(handler)(_request_payload())

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://nexent.example.com/nb/v1/chat/run"
    assert request.headers["Authorization"] == "Bearer key-1"
    assert request.headers["Idempotency-Key"] == "req-1"
    body = json.loads(request.content)
    assert body["agent_name"] == "decision-agent"
    assert "决策目标：choose" in body["query"]
    assert "- safe" in body["query"]
    assert body["meta_data"]["decision_request_id"] == "req-1"
    # `metadata` is gated by the agent's allow_chat_metadata flag; sending it
    # against an agent with the flag off returns 400 on the live platform.
    assert "metadata" not in body


def test_auth_failure_is_not_retried():
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={"detail": "Unauthorized: invalid API key"})

    with pytest.raises(NexentAuthError):
        _transport(handler)(_request_payload())

    assert len(attempts) == 1


def test_rate_limit_retries_with_same_idempotency_key():
    keys: list[str] = []
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        keys.append(request.headers["Idempotency-Key"])
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse({"type": "final_answer", "content": "ok"}),
        )

    result = _transport(handler)(_request_payload())

    assert result["status"] == "accepted"
    assert len(attempts) == 2
    assert keys == ["req-1", "req-1"]


def test_retries_are_bounded():
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503)

    with pytest.raises(NexentTransportError):
        _transport(handler, max_attempts=2)(_request_payload())

    assert len(attempts) == 2


def test_stream_without_final_answer_is_a_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse({"type": "model_output_thinking", "content": "thinking"}),
        )

    with pytest.raises(NexentStreamError):
        _transport(handler)(_request_payload())


def test_error_event_is_a_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"type": "error", "content": "tool crashed"},
                {"type": "final_answer", "content": "ignored"},
            ),
        )

    with pytest.raises(NexentStreamError):
        _transport(handler)(_request_payload())


def test_conflict_is_not_retried():
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "X-Stream-Status": "conflict",
            },
            content=_sse({"type": "final_answer", "content": "ignored"}),
        )

    with pytest.raises(NexentConflictError):
        _transport(handler)(_request_payload())

    assert len(attempts) == 1


def test_non_json_answer_is_reported_as_structured_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse({"type": "final_answer", "content": "选 A，因为更稳"}),
        )

    result = _transport(handler)(_request_payload())

    assert result["status"] == "accepted"
    assert result["decision"] is None
    assert "decision_error" in result


def test_adapter_uses_transport_as_injected_invoke():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {
                    "type": "final_answer",
                    "content": "```json\n"
                    + json.dumps(_decision(), ensure_ascii=False)
                    + "\n```",
                }
            ),
        )

    adapter = NexentAdapter(invoke=_transport(handler))
    result = adapter.run(
        DecisionRequest(
            request_id="req-1", goal="choose", context={}, constraints=[]
        )
    )

    assert result["decision"]["request_id"] == "req-1"


def test_settings_require_credentials(monkeypatch):
    monkeypatch.delenv("NEXENT_API_KEY", raising=False)
    monkeypatch.setenv("NEXENT_AGENT_NAME", "decision-agent")

    with pytest.raises(NexentConfigError):
        NexentSettings.from_env()

    monkeypatch.setenv("NEXENT_API_KEY", "key-1")
    monkeypatch.delenv("NEXENT_AGENT_NAME", raising=False)

    with pytest.raises(NexentConfigError):
        NexentSettings.from_env()


def test_query_and_parser_helpers():
    query = build_decision_query(
        {"goal": "choose", "context": {"team": "ict"}, "constraints": []}
    )
    assert "无" in query
    assert "严格" in query or "只输出" in query
    assert '"selected_option_id"' in query
    assert "req-" in query or "request_id" in query

    parsed = parse_decision_output(json.dumps(_decision()))
    assert parsed.selected_option_id == "a"


def test_parser_extracts_json_from_noisy_agent_answer():
    noisy = (
        "我注意到这是一个决策任务。<|tool_call_start|>\n"
        '[{"name": "knowledge_base_search"}]\n<|tool_call_end|>\n'
        "结论如下：\n"
        + json.dumps(_decision(), ensure_ascii=False)
        + "\n以上。"
    )

    parsed = parse_decision_output(noisy)

    assert parsed.request_id == "req-1"


def test_parser_handles_nested_braces_inside_evidence():
    payload = _decision()
    payload["evidence"] = [
        {"source": "ticket", "content": '{"nested": {"deep": "value"}}'}
    ]

    parsed = parse_decision_output("说明文本 " + json.dumps(payload, ensure_ascii=False))

    assert parsed.evidence[0].content == '{"nested": {"deep": "value"}}'


def test_parser_strips_leaked_reasoning_tags():
    answer = "lysis.</think>" + json.dumps(_decision(), ensure_ascii=False)

    parsed = parse_decision_output(answer)

    assert parsed.request_id == "req-1"


def test_parser_rejects_answer_without_json():
    with pytest.raises(ValueError):
        parse_decision_output("选 A，因为更稳")
