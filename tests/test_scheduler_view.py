"""微调度台取数层：链路、工单、资源、方案、可点场景、六类事件流。

这组测试钉的是同一条不变量——**页面上任何一个数字都能对回推演**：

- 工单与资源读的就是推演读的那份台账（不是页面另写一份）；
- 资源缺口与 `deduction.resource_gaps` 完全一致；
- 两套方案只差策略参数，`decision_inputs` 是同一份；
- 场景实体坐标来自 `scene_layout.json`，页面不硬编码；
- 六类事件流的条数等于各自来源的条数，不多不少。
"""
from __future__ import annotations

import pytest

from decision_poc import chain
from decision_poc.timeline import build_event_stream
from decision_poc.web_demo import (
    INTERVENTIONS,
    build_demo_payload,
    collab_review,
    intervention_catalog,
    route,
    run_intervention,
    scheduler_view,
)


@pytest.fixture(scope="module")
def payload():
    return build_demo_payload()


@pytest.fixture(scope="module")
def view():
    return scheduler_view(refresh=True)


def test_work_orders_come_from_the_same_ledger():
    orders = chain.load_work_orders()
    assert len(orders) == 8
    # 演示事件必须置顶：评委第一眼看到的就是本次事件
    assert orders[0]["order_id"] == "WO-20261029-007"
    assert orders[0]["is_event"] is True
    assert sum(1 for item in orders if item["status"] == "open") == 7
    # 工单要能挂到资源上，否则"工单列表"和"资源状态"是两张互不相干的表
    assert orders[0]["resource_id"] == "RES-PUMP-M"
    assert all(item["order_type_label"] for item in orders)


def test_resource_board_matches_deduction(payload):
    board = chain.build_resource_board(payload)
    lines = payload["resources"]["lines"]
    assert board["summary"]["required"] == sum(int(item["required"]) for item in lines)
    assert board["summary"]["committed"] == sum(int(item["available"]) for item in lines)
    assert board["summary"]["gap"] == sum(int(item["gap"]) for item in lines)
    assert board["summary"]["gap"] == 2
    used = [item for item in board["items"] if item["used_in_event"]]
    assert [item["resource_id"] for item in used] == [item["resource_id"] for item in lines]
    # 台账里 B-01 那 2 台也必须出现（跨区可调是方案 B 的事实基础）
    assert any(item["resource_id"] == "RES-PUMP-M-B01" for item in board["items"])


def test_plan_alternatives_differ_only_by_policy(payload):
    plan = chain.build_plan_alternatives(payload)
    current = {item["district"]: item for item in plan["current"]["per_district"]}
    alternative = {item["district"]: item for item in plan["alternative"]["per_district"]}
    # 同一份决策输入 → 需求总量必须一致，差的只能是分配
    assert plan["current"]["total_demand"] == plan["alternative"]["total_demand"]
    assert current["A-03"]["allocated"] == 4
    assert alternative["A-03"]["allocated"] == 6
    assert alternative["B-01"]["allocated"] == 0
    # 方案 B 是跨区调拨 2 台，不是凭空多出 2 台
    assert plan["alternative"]["transfers"]
    assert plan["alternative"]["total_unmet"] == plan["current"]["total_unmet"] == 2
    assert plan["statements"]


def test_scene_entities_are_clickable_with_links(payload):
    scene = chain.build_scene_entities(payload)
    assert scene["counts"]["water"] == 2
    assert scene["counts"]["road"] == 7
    assert scene["counts"]["facility"] == 7
    assert scene["counts"]["district"] == 2

    incident = next(item for item in scene["entities"] if item["id"] == "WO-20261029-007")
    assert incident["kind"] == "water"
    assert incident["state"] == "incident"
    # 点积水区必须能看到它对应哪张工单、占哪项资源——否则"可点"没有意义
    assert incident["links"]["work_orders"] == ["WO-20261029-007"]
    assert incident["links"]["resources"] == ["RES-PUMP-M"]
    assert incident["links"]["evidence"]

    closed_road = next(item for item in scene["entities"] if item["id"] == "R-101")
    assert closed_road["state"] == "closed"
    assert "已封" in closed_road["label"]

    # 坐标必须落在画布内（投影公式错了会让整张图跑到画布外）
    for entity in scene["entities"]:
        if entity["shape"] == "circle":
            assert 0 <= entity["cx"] <= scene["view"]["width"]
            assert 0 <= entity["cy"] <= scene["view"]["height"]


def test_chain_has_seven_stages_and_runs_collab(view):
    stages = view["chain"]["stages"]
    assert [item["id"] for item in stages] == [
        "event_input",
        "main_agent",
        "mcp_call",
        "collab_review",
        "conflict",
        "plan",
        "recalc",
    ]
    # 协同复核必须在链路里，而且挂的是 collab_run 工具——"两个 Agent 各跑各的"那个口子就堵在这
    collab_stage = next(item for item in stages if item["id"] == "collab_review")
    assert collab_stage["tool"] == "collab_run"
    assert collab_stage["actor"] == "urban_collab_agent"
    assert "collab_run" in view["chain"]["tools"]
    assert collab_stage["summary"].startswith("动作卡")
    # 只有真的有工具调用的环节才写 tool，事件输入与复算不硬填
    assert next(item for item in stages if item["id"] == "event_input")["tool"] is None


def test_event_stream_has_six_categories(payload):
    stream = build_event_stream(payload, collab=collab_review())
    assert [item["id"] for item in stream["categories"]] == [
        "timeline",
        "agent_message",
        "tool_call",
        "resource_change",
        "risk_change",
        "evidence",
    ]
    for item in stream["categories"]:
        assert item["count"] > 0, f"{item['id']} 类为空，六类事件流不算补齐"
    assert stream["counts"]["events"] == len(stream["events"])
    # Agent 消息必须两条：主 Agent 结论 + 协同 Agent 复核
    agents = [item for item in stream["events"] if item["category"] == "agent_message"]
    assert {item["actor"] for item in agents} == {"urban_deduction_agent", "urban_collab_agent"}
    # 工具调用里必须有 collab_run
    tools = [item.get("tool") for item in stream["events"] if item["category"] == "tool_call"]
    assert "collab_run" in tools


def test_intervention_catalog_matches_the_checklist():
    kinds = {key: entry["kind"] for key, entry in INTERVENTIONS.items()}
    assert kinds["pump-minus-one"] == "resource"
    assert kinds["close-road"] == "road"
    catalog = {item["id"]: item for item in intervention_catalog()}
    # 减一台泵：补的是 A-03 台账的 4 台（不是把 B-01 两台清零）
    assert any("RES-PUMP-M]" in patch for patch in catalog["pump-minus-one"]["patches"])
    assert any("R-103" in patch for patch in catalog["close-road"]["patches"])


def test_pump_minus_one_raises_the_gap():
    result = run_intervention("pump-minus-one")
    before, after = result["comparison"]["before"], result["comparison"]["after"]
    assert before["available"] == 6 and after["available"] == 5
    assert before["gap"] == 2 and after["gap"] == 3
    assert "travel" not in result


def test_close_road_keeps_gap_but_changes_travel():
    result = run_intervention("close-road")
    before, after = result["comparison"]["before"], result["comparison"]["after"]
    # 封路不改缺口：缺口由库存与需求折算决定。这条断言是"不许包装"的技术保证。
    assert before["gap"] == after["gap"] == 2
    travel = result["travel"]
    assert travel["available"] is True
    assert travel["closed_road"] == "R-103"
    assert travel["delta_meters"] > 0
    assert travel["after_closure"]["minutes"] > travel["baseline"]["minutes"]


def test_baseline_compare_keeps_duration_unmeasured(payload):
    """人工耗时没实测就必须是 None。这一条是"不许拿估算值填表"的技术保证。"""
    from decision_poc.baseline_compare import compare

    result = compare(payload, collab=collab_review())
    manual, agent = result["manual"], result["agent"]
    assert manual["organizations"] == 8
    assert manual["confirm_calls_min"] == manual["organizations"]
    assert manual["ledger_rows"] == sum(manual["ledger_detail"].values()) > 0
    assert manual["procedure_chars"] > 0
    # 未实测项必须是 None，并且如实列进 unmeasured
    assert manual["duration_minutes"] is None
    assert any("人工协同耗时" in item for item in result["unmeasured"])
    # 智能体侧只报"计算耗时"，不许被当成协同耗时用
    assert "compute_ms" in agent and "compute_note" in agent
    assert agent["tools_registered"] == 15
    assert agent["evidence_refs"] > 0
    assert result["statements"]


@pytest.mark.parametrize(
    "path", ["/api/scheduler", "/api/work-orders", "/api/resources", "/api/plan-alternatives", "/api/scene-entities", "/api/event-stream", "/api/baseline-compare"]
)
def test_scheduler_routes_serve(path):
    status, content_type, body = route(path)
    assert status == 200
    assert "json" in content_type
    assert body
