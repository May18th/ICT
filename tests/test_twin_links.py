"""沙盘关联层：对象 ↔ 工单 ↔ 动作卡 ↔ 证据，以及跳次派生的执行状态。

这组测试守四件事：

1. **六跳就是六跳**——跳次表必须且只能来自 `event_stream` 的 6 条 `stage` 事件。
   之前沙盘取的是 `category == 'timeline'` 的前 6 条，把第 5、6 条冲突事件
   （C1 职责重叠、C2 职责空白）当成了第三、第四跳，动作跳反而被挤掉；
2. **每条关联都能回溯**——动作卡挂在某个对象上，必须能说出"卡 → 哪张工单 → 哪行台账"，
   不接受"看着像就关联"；
3. **状态是推演状态**——四态由跳次推进派生，台账里根本没有现场进度字段，
   测试要钉死"不读环境、不看时间"的纯函数行为；
4. **不编数字**——处置后积水深度没有实测回传，就必须是 null 并写明原因。
"""

from __future__ import annotations

import json

import pytest

from decision_poc.web_demo import route, scheduler_view
from decision_poc.twin_links import (
    CARD_STATE_LABEL,
    MAX_JUMP,
    STAGE_JUMPS,
    build_twin_links,
    card_state,
    resolve_deeplink,
    work_order_rows,
)


@pytest.fixture(scope="module")
def view() -> dict:
    return scheduler_view()


@pytest.fixture(scope="module")
def links(view: dict) -> dict:
    return build_twin_links(view, current_jump=1)


# --- 六跳 -----------------------------------------------------------------


def test_jumps_are_exactly_the_six_stage_events(links: dict, view: dict) -> None:
    """6 跳且顺序固定；不能混进冲突明细事件。"""
    assert [item["phase"] for item in links["jumps"]] == list(STAGE_JUMPS)
    assert [item["jump"] for item in links["jumps"]] == [1, 2, 3, 4, 5, 6]
    assert all(item["status"] != "missing" for item in links["jumps"])

    stage_events = [
        event
        for event in view["stream"]["events"]
        if event.get("category") == "timeline" and event.get("kind") == "stage"
    ]
    assert len(stage_events) == 6
    assert {item["title"] for item in links["jumps"]} == {event["title"] for event in stage_events}


def test_jump_titles_are_stages_not_conflicts(links: dict) -> None:
    """回归：冲突明细（C1/C2）不得占据跳次位。"""
    titles = " ".join(item["title"] for item in links["jumps"])
    assert "职责重叠" not in titles
    assert "职责空白" not in titles
    assert links["jumps"][-1]["phase"] == "action"


# --- 关联可回溯 -----------------------------------------------------------


def test_every_card_order_link_points_at_a_real_ledger_row(links: dict) -> None:
    """卡片 → 工单：工单号必须在台账里，且行号引用能对上。"""
    rows = work_order_rows()
    order_ids = {order_id for order_id in rows.values()}
    for card in links["cards"]:
        for order_id in card["work_order_ids"]:
            assert order_id in order_ids, f"{card['card_id']} 关联到台账里不存在的工单 {order_id}"


def test_cards_with_evidence_produce_links_and_others_stay_empty(links: dict) -> None:
    """有工单证据的卡必须落到对象上；没证据的卡不许凭空挂对象。"""
    for card in links["cards"]:
        if "work_order" in " ".join(card["evidence_refs"]):
            assert card["entity_ids"], f"{card['card_id']} 引用了工单行却没能落到任何对象"
        if not card["work_order_ids"]:
            # 只能靠动作文本里点名的 id 关联，没有就不许有
            assert not any(
                card["card_id"] in row["card_ids"] and not row["work_order_ids"]
                for row in links["entities"].values()
                if card["entity_ids"]
            )


def test_entity_links_are_consistent_with_work_orders(links: dict) -> None:
    """对象的 card_ids 里每张卡，都必须真的通过工单或文本指向该对象。"""
    for entity_id, row in links["entities"].items():
        by_id = {card["card_id"]: card for card in links["cards"]}
        for card_id in row["card_ids"]:
            card = by_id[card_id]
            assert entity_id in card["entity_ids"]


def test_event_entity_carries_the_incident_work_order(links: dict) -> None:
    """事件本体（WO-20261029-007）必须有工单、有部门、有证据。"""
    row = links["entities"]["WO-20261029-007"]
    assert "WO-20261029-007" in row["work_order_ids"]
    assert row["departments"], "事件对象没落到任何部门"
    assert row["card_ids"]


def test_departments_are_chinese_names(links: dict) -> None:
    """总控 EMERG-COORD 要翻成台账里的中文单位名，不能直接吐 agent id。"""
    names = {card["org_name"] for card in links["cards"]}
    assert "EMERG-COORD" not in names
    assert "区应急管理局" in names


# --- 断言（claim）落到对象 -------------------------------------------------


def test_objects_carry_their_claims(links: dict) -> None:
    """对象要有判断依据：claim 来自挂在它上面的动作卡，不是另算一遍。"""
    assert links["claims"], "顶层断言表不能为空"
    for entity_id, row in links["entities"].items():
        by_id = {card["card_id"]: card for card in links["cards"]}
        expected: list[str] = []
        for card_id in row["card_ids"]:
            for claim_id in by_id[card_id]["claim_ids"]:
                if claim_id not in expected:
                    expected.append(claim_id)
        assert row["claim_ids"] == expected, f"{entity_id} 的 claim 与卡片对不上"

        for claim in row["claims"]:
            assert claim["claim_id"] in links["claims"]
            assert claim["label"] and not claim["label"].startswith("CL-")


def test_claims_are_labelled_in_chinese(links: dict) -> None:
    """断言名要给中文：页面上不能出现 `CL-S4-conflict` 这种机器编号当标题。"""
    for claim in links["claims"].values():
        assert claim["label"]
        assert claim["stage"] in STAGE_JUMPS
        assert claim["evidence_refs"], f"{claim['claim_id']} 没有证据引用"
        assert claim["evidence_count"] == len(claim["evidence_refs"])


def test_claim_label_rules() -> None:
    from decision_poc.twin_links import claim_label, claim_stage

    assert claim_label("CL-S1-observed") == "发生了什么"
    assert claim_label("CL-S6-action-07") == "处置动作第 7 条"
    assert claim_label("CL-UNKNOWN") == "CL-UNKNOWN"  # 认不出就原样返回，不猜
    assert claim_stage("CL-S4-conflict") == "conflict"
    assert claim_stage("CL-UNKNOWN") == ""


def test_event_object_has_observation_claim(links: dict) -> None:
    """事件本体的"发生了什么"必须挂得上——这是整条链的起点。"""
    row = links["entities"]["WO-20261029-007"]
    assert "CL-S1-observed" in row["claim_ids"]
    observed = next(item for item in row["claims"] if item["claim_id"] == "CL-S1-observed")
    assert observed["supported"] is True
    assert observed["evidence_count"] >= 1


# --- 执行状态 -------------------------------------------------------------


def test_card_state_is_pure_and_ordered() -> None:
    """四态规则：没轮到 pending、正赶上 running、过去了 done、要会签 needs_confirm。"""
    plain = {"stage": "action", "co_sign_required": []}
    signed = {"stage": "action", "co_sign_required": ["ORG-ROAD"]}

    assert card_state(plain, 1) == "pending"
    assert card_state(plain, 5) == "pending"
    assert card_state(plain, 6) == "running"
    assert card_state(plain, 7) == "done"
    assert card_state(signed, 7) == "needs_confirm"
    # 会签卡在"当前跳"仍然是执行中，只有过去了才等签字
    assert card_state(signed, 6) == "running"


def test_state_labels_and_visual_keys_are_complete(links: dict) -> None:
    for card in links["cards"]:
        assert card["state"] in CARD_STATE_LABEL
        assert card["state_label"] == CARD_STATE_LABEL[card["state"]]
        assert card["visual"]


def test_first_jump_only_observes(links: dict) -> None:
    """停在第一跳时，除观测卡外不准有完成的卡。"""
    done = [card for card in links["cards"] if card["state"] == "done"]
    assert done == []
    running = [card for card in links["cards"] if card["state"] == "running"]
    assert [card["stage"] for card in running] == ["observed"]


def test_after_dispatch_all_cards_leave_pending(view: dict) -> None:
    """跳到"处置后"（7）时不应该还有待执行的卡。"""
    after = build_twin_links(view, current_jump=MAX_JUMP)
    assert after["counts"]["states"]["pending"] == 0
    assert after["counts"]["states"]["running"] == 0


def test_jump_is_clamped(view: dict) -> None:
    assert build_twin_links(view, current_jump=99)["current_jump"] == MAX_JUMP
    assert build_twin_links(view, current_jump=0)["current_jump"] == 1


# --- 深链接 ---------------------------------------------------------------


def test_deeplink_parsing() -> None:
    assert resolve_deeplink({"jump": ["4"], "entity": ["DF-PS-01"]}) == (4, "DF-PS-01")
    assert resolve_deeplink({}) == (1, "")
    assert resolve_deeplink({"jump": ["abc"]}) == (1, "")
    assert resolve_deeplink({"jump": ["0"]}) == (1, "")
    assert resolve_deeplink({"jump": ["99"]}) == (MAX_JUMP, "")


def test_deeplink_endpoint_reports_missing_entity() -> None:
    status, content_type, body = route("/api/twin-links?jump=3&entity=NOT-A-THING")
    assert status == 200
    assert "json" in content_type
    payload = json.loads(body)
    assert payload["requested_entity"] == {"id": "NOT-A-THING", "found": False}
    assert payload["current_jump"] == 3

    status, _, body = route("/api/twin-links?jump=3&entity=DF-PS-01")
    assert json.loads(body)["requested_entity"] == {"id": "DF-PS-01", "found": True}


# --- 前后对比 -------------------------------------------------------------


def test_results_do_not_invent_measurements(view: dict) -> None:
    """积水深度的"处置后"必须是 null：没有实测回传就不许给数。"""
    after = build_twin_links(view, current_jump=MAX_JUMP)
    rows = {row["key"]: row for row in after["results"]["rows"]}
    assert rows["depth_cm"]["before"] == 30.0
    assert rows["depth_cm"]["after"] is None
    assert "无实测" in rows["depth_cm"]["note"]

    # 缺口由库存决定，处置动作不该让它变——前后必须相同，否则就是编的
    assert rows["gap"]["before"] == rows["gap"]["after"] == 2
    assert after["results"]["measured"] is False

    # 结果行回指断言：能挂的挂上，挂不上的留空并写明来源
    by_key = {row["key"]: row for row in after["results"]["rows"]}
    coverage = after["results"]["claim_coverage"]
    assert coverage["rows"] == len(by_key)
    assert coverage["bound"] + coverage["unbound"] == coverage["rows"]
    assert coverage["bound"] >= 5, "至少五行的数字要能说清依据"
    for row in after["results"]["rows"]:
        assert row["claim_ids"] == [ref["claim_id"] for ref in row["claims"]]
        for claim_id in row["claim_ids"]:
            assert claim_id in after["claims"], f"{row['key']} 指向不存在的断言 {claim_id}"
    # 缺口这种纯推演数字必须挂在资源断言上
    assert by_key["gap"]["claim_ids"] == ["CL-S5-resource"]
    # 台账计数没有对应断言，不许硬编一条
    assert by_key["open_orders"]["claim_ids"] == []
    assert "台账" in by_key["open_orders"]["basis"]
    assert by_key["residual_risks"]["claim_ids"] == []
    assert "未单独成断言" in by_key["residual_risks"]["basis"]


def test_results_progress_with_jump(view: dict) -> None:
    first = {row["key"]: row for row in build_twin_links(view, current_jump=1)["results"]["rows"]}
    last = {row["key"]: row for row in build_twin_links(view, current_jump=MAX_JUMP)["results"]["rows"]}
    assert first["action_cards"]["after"] == 0
    assert last["action_cards"]["after"] > 0
    assert first["linked_objects"]["after"] == last["linked_objects"]["after"] > 0


# --- 只读 -----------------------------------------------------------------


def test_links_do_not_touch_frozen_numbers(links: dict) -> None:
    """关联层是读出来的视图，不许反过来改推演结果。"""
    status, _, body = route("/api/demo")
    assert status == 200
    payload = json.loads(body)
    deduction = payload["deduction"]
    gaps = [(item["resource_id"], item["gap"]) for item in deduction["resource_gaps"]]
    conflicts = sorted(item["conflict_id"] for item in deduction["conflicts"])
    assert any(gap == 2 for _, gap in gaps)
    assert conflicts == ["C1", "C2", "C3"]
    assert links["event_id"] == payload["event_id"]
