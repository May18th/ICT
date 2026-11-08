"""沙盘业务闭环端到端：事件 → 判断 → 动作 → 状态 → 结果 → 证据。

为什么要单开一组
----------------
关联层单测只能证明"某一环算对了"，证明不了六环之间接得上。
这组按评委的阅读顺序走一遍：从一张工单出发，能不能一路点到"谁在办、办到哪一步、
凭什么"；反过来，从一条证据能不能回到台账第几行。

另一类断言是**页面级**的：JS 里 `renderer.domElement` 曾在顶层执行过，
那时 `renderer` 还是 undefined，脚本从那一行整体中断，页面全黑却接口全 200。
静态结构断言与接口测试都发现不了，只有浏览器 console 能看见，所以这里把
"必须等 renderer 建好再绑定"钉成不变量。
"""

from __future__ import annotations

import json
import re

import pytest

from decision_poc.web_demo import build_demo_payload, route, scheduler_view
from decision_poc.twin_links import (
    CARD_STATE_LABEL,
    STAGE_JUMPS,
    build_twin_links,
    work_order_rows,
)


@pytest.fixture(scope="module")
def view() -> dict:
    return scheduler_view()


@pytest.fixture(scope="module")
def links(view: dict) -> dict:
    return build_twin_links(view, current_jump=7)


# --- 六环串联 -------------------------------------------------------------


def test_event_to_evidence_walks_the_whole_chain(view: dict, links: dict) -> None:
    """一张工单 → 对象 → 动作卡 → 证据 → 台账行，每一跳都能落地。"""
    event_id = links["event_id"]
    assert event_id == build_demo_payload()["event_id"]

    # ① 事件落在一个沙盘对象上
    entity = links["entities"][event_id]
    assert entity["work_order_ids"], "事件对象没有工单"

    # ② 判断有据：claims 带引用
    claims = (view["payload"].get("audit") or {}).get("claims") or []
    assert claims
    assert all(claim["claim_id"] and claim["evidence_refs"] for claim in claims)

    # ③ 动作：对象上挂着动作卡
    assert entity["card_ids"], "事件对象没有动作卡"
    cards = {card["card_id"]: card for card in links["cards"]}

    # ④ 动作卡带证据，且证据能解析出台账行号
    row_map = work_order_rows()
    for card_id in entity["card_ids"]:
        card = cards[card_id]
        assert card["evidence_refs"], f"{card_id} 没有证据引用"
        for order_id in card["work_order_ids"]:
            assert order_id in row_map.values()

    # ⑤ 状态可读
    for card_id in entity["card_ids"]:
        assert cards[card_id]["state"] in CARD_STATE_LABEL

    # ⑥ 判断依据：对象的 claim 必须能在 audit.claims 里找到，且带证据
    assert entity["claim_ids"], "事件对象没有断言支撑"
    audit_ids = {claim["claim_id"] for claim in claims}
    for claim_id in entity["claim_ids"]:
        assert claim_id in audit_ids, f"{claim_id} 不在 audit.claims 里"
        assert claim_id in links["claims"]
        assert links["claims"][claim_id]["evidence_refs"]


def test_states_only_move_forward(links: dict, view: dict) -> None:
    """跳次递增时，卡的状态只准往前走，不准从已完成退回待执行。"""
    rank = {"pending": 0, "running": 1, "needs_confirm": 2, "done": 2}
    for card in links["cards"]:
        previous = -1
        for state in card["states"]:
            assert rank[state] >= previous, f"{card['card_id']} 状态回退：{card['states']}"
            previous = rank[state]

    # 逐跳重算一遍，结果必须与 states 数组一致
    for jump in range(1, 8):
        built = build_twin_links(view, current_jump=jump)
        by_id = {card["card_id"]: card for card in built["cards"]}
        for card in links["cards"]:
            assert by_id[card["card_id"]]["state"] == card["states"][jump - 1]


def test_results_agree_with_card_states(links: dict) -> None:
    """对照表里的"已完成动作卡"必须等于 cards 里真数得出来的张数。"""
    rows = {row["key"]: row for row in links["results"]["rows"]}
    done = [card for card in links["cards"] if card["state"] == "done"]
    action_done = [card for card in done if card["stage"] == "action"]
    assert rows["action_cards"]["after"] == len(action_done)
    assert rows["linked_objects"]["after"] == len([row for row in links["entities"].values() if row["card_ids"]])


def test_every_jump_highlights_at_least_one_object(links: dict) -> None:
    """每一跳都要能在沙盘上指出"看哪儿"，否则跳次按钮就是摆设。"""
    for jump in links["jumps"]:
        assert jump["entity_ids"], f"第 {jump['jump']} 跳没有关联对象"


def test_jump_entities_exist_in_scene(links: dict) -> None:
    for jump in links["jumps"]:
        for entity_id in jump["entity_ids"]:
            assert entity_id in links["entities"]


# --- 页面级 ---------------------------------------------------------------


def _twin_html() -> str:
    status, content_type, body = route("/twin3d")
    assert status == 200 and "html" in content_type
    return body.decode("utf-8")


def test_twin_page_wires_every_panel() -> None:
    html = _twin_html()
    for anchor in ("id=entity-links", "id=results-body", "id=jump-buttons", "id=hud-src"):
        assert anchor in html, f"缺少挂载点 {anchor}"
    assert "/api/twin-links" in html
    # 深链接两个参数都要被读取
    assert "location.search" in html
    assert "jump" in html and "entity" in html


def test_twin_page_binds_click_after_renderer_exists() -> None:
    """回归：顶层取 renderer.domElement 会抛错并连累 init()，页面全黑。"""
    html = _twin_html()
    bind = html.index("function bindUI(")
    hit = html.index("renderer.domElement.addEventListener")
    assert hit > bind, "点击绑定必须写在 bindUI 里，不能在顶层执行"
    # bindUI 必须在 init 末尾被调用，否则绑定了也没人触发
    assert re.search(r"setAnimationLoop\(tick\);\s*bindUI\(\);", html), "init 结束前没有调用 bindUI()"


def test_home_page_offers_twin_deeplinks() -> None:
    """主页面必须能跳到沙盘，且跳次号来自关联层而不是前端自己数。"""
    html = route("/")[2].decode("utf-8")
    assert "在沙盘查看" in html
    assert "/twin3d?jump=" in html
    assert "/api/twin-links" in html
    assert "function jumpLink(" in html
    assert "function entityJump(" in html


def test_deeplink_round_trip() -> None:
    """页面给的链接，交给接口要能原样认出跳次与对象。"""
    status, _, body = route("/api/twin-links?jump=4&entity=DF-PS-01")
    assert status == 200
    payload = json.loads(body)
    assert payload["current_jump"] == 4
    assert payload["requested_entity"] == {"id": "DF-PS-01", "found": True}
    assert payload["jumps"][3]["phase"] == STAGE_JUMPS[3]


def test_links_repeatability(view: dict) -> None:
    """同一份视图连算两次必须逐字节一致，页面回放才不是摆拍。"""
    first = json.dumps(build_twin_links(view, current_jump=5), ensure_ascii=False, sort_keys=True)
    second = json.dumps(build_twin_links(view, current_jump=5), ensure_ascii=False, sort_keys=True)
    assert first == second
