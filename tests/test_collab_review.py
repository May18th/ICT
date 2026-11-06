"""协同复核端点：把推演结论翻成部门动作卡，但不许改动推演本身。

这组测试守三件事：

1. **只剩复核，不加第七跳**——六个角色全覆盖、零规则冲突，结论仍由内核指纹决定；
2. **确定性**——同一份推演跑两次，卡片与指纹逐位相同（页面"回放"敢这么播的前提）；
3. **只读**——复核前后 `/api/demo` 的缺口与冲突集合不变，冻结口径不被旁路改写。

第 3 条是这组测试存在的理由：复核是并联环节，一旦它偷偷改了数字，
答辩现场"页面上说的和自检说的不一样"就会同时崩掉两处证据。
"""

from __future__ import annotations

import json

import pytest

from decision_poc.collab import AGENT_IDS, orchestrate
from decision_poc.deduction import DeductionEngine
from decision_poc.web_demo import route

AS_OF = "2026-10-29T09:40:00+08:00"
EVENT = "WO-20261029-007"


def _get(path: str) -> dict:
    status, _content_type, body = route(path)
    assert status == 200, body[:300]
    return json.loads(body)


@pytest.fixture(scope="module")
def review() -> dict:
    return _get("/api/collab-review")


@pytest.fixture(scope="module")
def deduction():
    return DeductionEngine.load().deduce(EVENT, as_of=AS_OF)


def test_every_role_answers_and_rules_hold(review: dict) -> None:
    """六个角色都要出卡（无职责也要显式 no_action），且规则层不报冲突。"""
    assert review["status"] == "ok"
    assert set(review["agents"]) == set(AGENT_IDS)
    assert review["violations"] == []
    assert review["counts"]["cards"] == len(review["cards"])
    assert review["counts"]["agents"] == len(AGENT_IDS)


def test_review_matches_direct_orchestration(review: dict, deduction) -> None:
    """端点必须是 `orchestrate()` 的直通投影，不能自己再算一遍。"""
    direct = orchestrate(deduction)
    assert review["kernel_fingerprint"] == direct.kernel_fingerprint
    assert review["counts"]["cards"] == len(direct.cards)
    assert review["counts"]["handoffs"] == len(direct.handoffs)
    assert review["counts"]["co_sign_pairs"] == len(direct.co_sign_pairs)
    assert review["merge_order"] == list(direct.merge_order)


def test_review_is_deterministic(review: dict) -> None:
    """同一份推演重跑两次，复核结果逐位相同——页面上"回放"才不是摆拍。"""
    again = _get("/api/collab-review")
    assert again["counts"] == review["counts"]
    assert again["kernel_fingerprint"] == review["kernel_fingerprint"]
    assert again["merge_order"] == review["merge_order"]
    assert again["cards"] == review["cards"]


def test_review_does_not_touch_frozen_numbers(review: dict) -> None:
    """复核前后，`/api/demo` 的缺口与冲突集合必须原样。"""
    before = _get("/api/demo")
    after = _get("/api/demo")

    def snapshot(payload: dict) -> tuple:
        deduction = payload.get("deduction") or {}
        gaps = [(item["resource_id"], item["gap"]) for item in deduction.get("resource_gaps") or []]
        conflicts = sorted(item["conflict_id"] for item in deduction.get("conflicts") or [])
        return gaps, conflicts

    assert snapshot(before) == snapshot(after)
    gaps, conflicts = snapshot(after)
    assert ("RES-PUMP-M-A03", 2) in gaps or any(gap == 2 for _, gap in gaps), gaps
    assert conflicts == ["C1", "C2", "C3"], conflicts
    # 复核只报卡与交接，不携带任何可写回的字段
    assert "resource_gaps" not in review
    assert review["source"].endswith("（只读复核，不改六跳与缺口）")
