"""Nexent 城市内涝主链路验证的测试：不触网，用 MockTransport 回放 SSE。

钉住三件事：
1. 查询里必须带事件编号/时点/工具名，Agent 才知道要调什么；
2. 回答必须能被解析成六跳结构，脏格式（think 标签、代码围栏）不影响；
3. 与离线结论不一致时**必须失败**——只看"返回了 JSON"不算过。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from decision_poc.nexent_transport import NexentHttpTransport, NexentSettings
from decision_poc.nexent_urban import (
    DEFAULT_AS_OF,
    DEFAULT_EVENT_ID,
    UrbanAgentAnswer,
    build_urban_query,
    offline_baseline,
    parse_urban_answer,
    run,
)

SETTINGS = NexentSettings(
    base_url="https://nexent.example.com",
    api_key="key-1",
    agent_name="urban-agent",
)


def _answer(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_id": DEFAULT_EVENT_ID,
        "as_of": DEFAULT_AS_OF,
        "summary": "A-03 积水叠加燃气基坑，需跨部门协同并跨区调 1 台泵",
        "six_hops": ["observed", "affected", "owner", "conflict", "resource", "action"],
        "conflicts": [
            {"conflict_id": "C1", "kind": "overlap", "subject": "封路权限"},
            {"conflict_id": "C2", "kind": "gap", "subject": "DF-PS-01"},
            {"conflict_id": "C3", "kind": "contention", "subject": "RES-PUMP-M"},
        ],
        "resource_gap": 2,
        "action_order": [
            "停止基坑开挖并覆盖或围堰",
            "隔离风险源：确认无泄漏后关阀或断电",
            "封闭积水路段全幅",
            "先清掏雨水口再抽排",
        ],
        "residual_risks": ["燃气管悬空或泄漏"],
        "evidence_refs": ["AS-07-work_orders-1234#row:2", "AS-01-pipeline_ledger-5678#row:5"],
        "handoff_required": True,
    }
    payload.update(overrides)
    return payload


def _sse(*events: dict[str, Any]) -> bytes:
    return "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events).encode("utf-8")


def _transport(handler) -> NexentHttpTransport:
    return NexentHttpTransport(
        settings=SETTINGS,
        max_attempts=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
        query_builder=build_urban_query,
    )


def _handler_with_answer(answer_text: str, captured: dict[str, Any] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "conversation_id": "77",
                "X-Request-Id": "srv-urban",
            },
            content=_sse(
                {"type": "conversation_created", "content": {"conversation_id": 77}},
                {"type": "final_answer", "content": answer_text},
            ),
        )

    return handler


def test_query_names_event_time_and_tools() -> None:
    query = build_urban_query({"event_id": DEFAULT_EVENT_ID, "as_of": DEFAULT_AS_OF})
    assert DEFAULT_EVENT_ID in query
    assert DEFAULT_AS_OF in query
    for tool in ("event_deduce", "twin_neighbors", "conflict_check", "allocation_plan"):
        assert tool in query
    assert "只输出 JSON" in query


def test_parse_tolerates_think_tags_and_fences() -> None:
    noisy = "分析完成。</think>\n```json\n" + json.dumps(_answer(), ensure_ascii=False) + "\n```"
    answer = parse_urban_answer(noisy)
    assert answer.event_id == DEFAULT_EVENT_ID
    assert answer.resource_gap == 2


def test_parse_rejects_natural_language() -> None:
    from decision_poc.nexent_urban import UrbanAnswerError

    with pytest.raises(UrbanAnswerError):
        parse_urban_answer("抱歉，我无法完成这个推演。")


def test_offline_baseline_matches_ground_truth() -> None:
    baseline = offline_baseline(DEFAULT_EVENT_ID, DEFAULT_AS_OF)
    assert baseline["conflict_ids"] == ["C1", "C2", "C3"]
    assert baseline["resource_gap"] == 2
    assert baseline["isolate_before_drain"] is True
    assert baseline["action_count"] >= 6


def test_consistent_answer_passes_all_checks() -> None:
    captured: dict[str, Any] = {}
    transport = _transport(_handler_with_answer(json.dumps(_answer(), ensure_ascii=False), captured))
    report = run(settings=SETTINGS, transport=transport)
    assert report.errors == []
    assert report.passed is True, [c.model_dump() for c in report.failures]
    assert report.conversation_id == 77
    assert len(report.checks) == 8
    body = captured["body"]
    assert body["agent_name"] == "urban-agent"
    assert DEFAULT_EVENT_ID in body["query"]
    assert body["query"].count("allocation_plan") >= 1


def test_inconsistent_answer_must_fail() -> None:
    """返回了合法 JSON 但缺口编成 0——这正是"看起来过了"的假阳性，必须拦。"""
    transport = _transport(
        _handler_with_answer(json.dumps(_answer(resource_gap=0), ensure_ascii=False))
    )
    report = run(settings=SETTINGS, transport=transport)
    assert report.passed is False
    names = [item.name for item in report.failures]
    assert "资源缺口" in names


def test_wrong_conflict_ids_must_fail() -> None:
    transport = _transport(
        _handler_with_answer(json.dumps(_answer(conflicts=[{"conflict_id": "C1", "kind": "overlap"}]), ensure_ascii=False))
    )
    report = run(settings=SETTINGS, transport=transport)
    assert report.passed is False
    assert "冲突编号" in [item.name for item in report.failures]


def test_action_order_violation_must_fail() -> None:
    answer = _answer(
        action_order=["抽排：先投入移动泵", "隔离风险源：确认无泄漏后关阀"]
    )
    transport = _transport(_handler_with_answer(json.dumps(answer, ensure_ascii=False)))
    report = run(settings=SETTINGS, transport=transport)
    assert report.passed is False
    assert "隔离先于抽排" in [item.name for item in report.failures]


def test_missing_final_answer_is_an_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse({"type": "conversation_created", "content": {"conversation_id": 1}}),
        )

    report = run(settings=SETTINGS, transport=_transport(handler))
    assert report.passed is False
    assert report.errors and "final_answer" in report.errors[0]


def test_missing_env_is_reported_without_network() -> None:
    report = run(environ={"NEXENT_BASE_URL": "http://localhost:5013"})
    assert report.passed is False
    assert any("缺少环境变量" in item for item in report.errors)


def test_answer_model_ignores_extra_fields() -> None:
    payload = _answer()
    payload["unexpected"] = {"nested": True}
    answer = UrbanAgentAnswer.model_validate(payload)
    assert answer.event_id == DEFAULT_EVENT_ID
