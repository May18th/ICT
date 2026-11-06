"""决策事件流与干预对照的回归测试。

两条纪律在这里变成可执行断言：

1. **只派生、不重算**——把 payload 里的缺口数字改掉，事件必须跟着变；
2. **不写算不出来的因果**——对照说明句里不许出现「更严重 / 危险」这类没有字段支撑的判断。

顺带钉住一个已修的真 bug：`affected` 是逗号分隔的 14 个对象，早先只按 `;` 与 `|` 计数，
把影响范围算成了 1 个。
"""

from __future__ import annotations

import json

import pytest

from decision_poc.timeline import build_timeline, compare_branches

EVENT_ID = "WO-20261029-007"
AS_OF = "2026-10-29T09:40:00+08:00"


@pytest.fixture(scope="module")
def payload() -> dict:
    from decision_poc.web_demo import build_demo_payload

    return build_demo_payload()


@pytest.fixture(scope="module")
def timeline(payload: dict) -> dict:
    return build_timeline(payload)


# --- 事件流结构 -----------------------------------------------------------


def test_six_hops_then_conflicts_gaps_audit(timeline: dict) -> None:
    kinds = [event["kind"] for event in timeline["events"]]
    assert kinds.count("stage") == 6, "六跳各一条，不能多也不能少"
    assert kinds.count("conflict") == 3, "C1/C2/C3 必须逐条出现"
    assert kinds.count("gap") == 1
    assert kinds[-1] == "audit", "审计永远收尾"
    assert [event["seq"] for event in timeline["events"]] == list(range(1, len(kinds) + 1))


def test_conflict_before_resource_gap_but_contention_after(timeline: dict) -> None:
    """职责重叠/空白挂在冲突跳后，资源争用挂在资源跳后——否则缺口数字连说三遍。"""
    phases = [(event["kind"], event["phase"]) for event in timeline["events"]]
    resource_index = phases.index(("stage", "resource"))
    contention = [index for index, event in enumerate(timeline["events"]) if event["kind"] == "conflict"]
    titles = [timeline["events"][index]["title"] for index in contention]
    assert any("资源争用" in title for title in titles)
    assert all(index > resource_index for index, event in enumerate(timeline["events"]) if "资源争用" in event["title"])


def test_affected_count_handles_comma_separated_objects(timeline: dict, payload: dict) -> None:
    """`affected` 是 `a,b,c` 拼串，14 个对象不能算成 1 个（旧实现的分隔符 bug）。"""
    stage = next(event for event in timeline["events"] if event["phase"] == "affected")
    conclusion = next(
        step for step in payload["deduction"]["stages"] if step["stage"] == "affected"
    )["conclusion"]
    expected = len([item for item in str(conclusion["affected"]).split(",") if item.strip()])
    assert expected == 14
    assert f"{expected} 个对象受影响" in stage["detail"]


def test_gap_event_carries_calculation_chain(timeline: dict, payload: dict) -> None:
    """缺口 2 台怎么来的：测算链原样透出，答辩被追问时的答案就在这。"""
    gap_event = next(event for event in timeline["events"] if event["kind"] == "gap")
    source = payload["deduction"]["resource_gaps"][0]
    assert str(source["gap"]) in gap_event["detail"]
    assert gap_event["basis"] == source["basis"]
    assert len(gap_event["basis"]) >= 5


def test_numbers_follow_payload_and_are_not_recomputed(payload: dict) -> None:
    """改 payload 里的缺口，事件跟着变——证明展示层没有第二套算法。"""
    tampered = json.loads(json.dumps(payload))
    tampered["deduction"]["resource_gaps"][0]["gap"] = 7
    gap_event = next(
        event for event in build_timeline(tampered)["events"] if event["kind"] == "gap"
    )
    assert "缺口 7" in gap_event["detail"]
    assert "缺口 2" not in gap_event["detail"]


def test_every_event_points_back_to_a_payload_field(timeline: dict) -> None:
    for event in timeline["events"]:
        assert event["source"], event["title"]
        root = event["source"].replace(" / ", ".").split(".")[0]
        assert root in {"deduction", "audit", "publishable"}, event["source"]


def test_audit_event_reflects_publishable(timeline: dict, payload: dict) -> None:
    audit = timeline["events"][-1]
    assert audit["status"] == ("publishable" if payload["publishable"] else "needs_review")


def test_trace_lists_the_five_skills(timeline: dict) -> None:
    assert [step["skill"] for step in timeline["trace"]] == [
        "event_understand",
        "impact_analysis",
        "resource_check",
        "cross_department_deduction",
        "evidence_audit",
    ]


# --- 干预对照 -------------------------------------------------------------


def test_data_patch_moves_gap_and_keeps_action_order() -> None:
    """跨区泵调不过来：缺口 2 → 4，但处置顺序不变（顺序由规则定，不由库存定）。"""
    from decision_poc.counterfactual import (
        Intervention,
        InterventionKind,
        rerun_with_interventions,
    )

    patch = Intervention(
        kind=InterventionKind.DATA_PATCH,
        target="resource_inventory.csv",
        match={"resource_id": "RES-PUMP-M-B01"},
        field="qty_available",
        value="0",
        note="跨区移动泵未到位",
    )
    result = rerun_with_interventions(
        [patch], event_id=EVENT_ID, as_of=AS_OF, caller_labels=["ORG-EMERG"]
    )
    comparison = compare_branches(
        result["baseline"], result["intervened"], interventions=result["interventions"]
    )
    assert comparison["before"]["gap"] == 2
    assert comparison["after"]["gap"] == 4
    assert comparison["delta"]["available"] == -2
    assert comparison["moved_actions"] == []

    joined = "".join(comparison["statements"])
    assert "缺口 2 → 4 台" in joined
    # 纪律：说明句里不许出现算不出来的因果判断
    for banned in ("更严重", "危险", "影响更大", "显著", "可能造成"):
        assert banned not in joined, f"对照说明出现了无字段支撑的判断：{banned}"


def test_rule_patch_moves_action_order_and_keeps_gap() -> None:
    """先封路后隔离：缺口不变，处置顺序重排——这是"改条件→改方案"的另一半。"""
    from decision_poc.counterfactual import CASES, rerun_with_interventions

    result = rerun_with_interventions(
        list(CASES["CF1"].interventions),
        event_id=EVENT_ID,
        as_of=AS_OF,
        caller_labels=["ORG-EMERG"],
    )
    comparison = compare_branches(
        result["baseline"], result["intervened"], interventions=result["interventions"]
    )
    assert comparison["delta"]["gap"] == 0
    assert comparison["moved_actions"], "规则顺序对调必须体现在处置顺序上"
    assert any("挪位" in statement for statement in comparison["statements"])


# --- 路由（纯函数，不起 socket） -----------------------------------------


def test_timeline_route_serves_the_same_events() -> None:
    from decision_poc.web_demo import route

    status, content_type, body = route("/api/timeline")
    assert status == 200
    assert content_type.startswith("application/json")
    payload = json.loads(body)
    assert payload["counts"]["events"] == 11
    assert payload["counts"]["evidence_refs"] > 0


def test_interventions_route_lists_and_runs() -> None:
    from decision_poc.web_demo import INTERVENTIONS, route

    status, _, body = route("/api/interventions")
    assert status == 200
    entries = json.loads(body)["entries"]
    assert {entry["id"] for entry in entries} == set(INTERVENTIONS)

    status, _, body = route(
        "/api/interventions", "POST", json.dumps({"id": "pump-b01-off"}).encode("utf-8")
    )
    assert status == 200
    result = json.loads(body)
    assert result["comparison"]["after"]["gap"] == 4
    assert result["timeline"]["counts"]["events"] == 11, "干预后仍应产出同样形状的时间线"


def test_intervention_route_rejects_unknown_id() -> None:
    from decision_poc.web_demo import route

    status, _, body = route(
        "/api/interventions", "POST", json.dumps({"id": "delete-everything"}).encode("utf-8")
    )
    assert status == 404
    assert json.loads(body)["status"] == "error"


def test_intervention_route_rejects_non_object_body() -> None:
    """端点只吃 {id}，别的形状一律拒——补丁目标不能由外部指定。"""
    from decision_poc.web_demo import route

    status, _, _ = route("/api/interventions", "POST", b"['a']")
    assert status == 400
    status, _, _ = route("/api/interventions", "POST", b"{\"target\":\"../../etc/passwd\"}")
    assert status == 400
