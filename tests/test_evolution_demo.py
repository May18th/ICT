"""P9.1 进化对比面板测试：数据一致性、确定性、页面锚点。"""

from __future__ import annotations

import pytest

from decision_poc.evolution_demo import build_evolution_html, build_evolution_payload


@pytest.fixture(scope="module")
def payload() -> dict:
    return build_evolution_payload()


def test_payload_mirrors_the_showcase_numbers(payload: dict) -> None:
    first = payload["first"]
    assert payload["schema_version"] == "evolution-demo-1"
    assert first["initial_train"]["deviation"] == pytest.approx(0.226428)
    assert first["final_train"]["deviation"] == 0.0
    assert first["initial_holdout"]["deviation"] == pytest.approx(0.218155)
    assert first["final_holdout"]["deviation"] == 0.0
    assert first["final_label"] == "flood-response-v1.2"
    assert [item["label"] for item in first["releases"]] == [
        "flood-response-v1.1",
        "flood-response-v1.2",
    ]
    assert [item["action"] for item in first["rounds"]] == ["promoted", "promoted", "rejected"]

    # V1/V2 对照：缺口必须能拆成「题面不可消除 + 策略自造」，成本必须能从台账推回来。
    # 页面上的「15 → 6」只是一个差值；拆开才是可辩护的结论——6 台是参考答案自己也
    # 填不平的下限，9 台是 V1 自己造出来的，这两者混在一起说就会被问穿。
    contrast = payload["contrast"]
    train = contrast["train"]
    assert train["critical_unmet"] == [15, 6]
    # 恒等式：总缺口 = 题面下限 + 策略自造。两侧同时成立才算账算平了。
    for total, floor, self_made in zip(
        train["critical_unmet"], [train["floor"]] * 2, train["self_inflicted"]
    ):
        assert total == floor + self_made, f"缺口拆账不平：{total} ≠ {floor} + {self_made}"
    assert train["self_inflicted"][1] == 0, "V2 不该再有自己造出来的缺口"
    # 留出集同一条恒等式：策略收敛后剩下的缺口应当正好等于题面下限。
    holdout = contrast["holdout"]
    assert holdout["critical_unmet"][1] == holdout["floor"]
    # 成本只折算泵台时，且必须能从「自造缺口 × 台账在途」推回来，不能是手写常数。
    assert contrast["delay_min"] == pytest.approx(40.0)
    for hours, self_made in zip(train["extra_hours"], train["self_inflicted"]):
        assert hours == pytest.approx(self_made * contrast["delay_min"] / 60, abs=0.05)


def test_payload_shows_memory_warm_start(payload: dict) -> None:
    second = payload["second"]
    assert second["warm_start"] is not None
    assert second["warm_start"]["label"] == "flood-response-v1.2"
    assert second["warm_start"]["similarity"] == 1.0
    assert second["initial_train"]["deviation"] == 0.0
    assert second["rounds"] == 1
    assert second["promoted_rounds"] == 0
    assert payload["memory"]["entries"][0]["label"] == "flood-response-v1.2"
    assert payload["memory"]["entries"][0]["review_ids"] == ["REV-001"]


def test_payload_shows_human_review_signal(payload: dict) -> None:
    review = payload["review"]
    assert review is not None
    assert review["review_id"] == "REV-001"
    assert review["decision"] == "modify"
    assert review["deviation_before"] == pytest.approx(0.117143)
    assert review["deviation_after"] == 0.0
    assert review["expected_allocation"] == {"A-03": 5, "B-01": 1}
    assert "角色扮演" in review["reviewer_note"]


def test_payload_shows_real_event_before_and_after(payload: dict) -> None:
    baseline = payload["real_event"]["baseline"]
    evolved = payload["real_event"]["evolved"]
    assert baseline is not None and evolved is not None
    assert baseline["allocation_by_district"] == {"A-03": 4, "B-01": 2}
    assert evolved["allocation_by_district"] == {"A-03": 6, "B-01": 0}
    assert evolved["unmet_by_district"] == {"A-03": 0, "B-01": 2}
    assert any("B-01->A-03" in item and "40" in item for item in evolved["transfers"])


def test_payload_declares_honesty_boundaries(payload: dict) -> None:
    text = " ".join(payload["boundaries"])
    assert "受控评测环境" in text
    assert "角色扮演" in text
    assert "未在真实 Nexent 平台验证" in text


def test_payload_is_deterministic() -> None:
    first = build_evolution_payload()
    second = build_evolution_payload()
    assert first == second


def test_html_contains_panels_and_api_route() -> None:
    html = build_evolution_html()
    for anchor in [
        "evolution-hero",
        "evolution-metrics",
        "evolution-grid",
        "review-panel",
        "history-panel",
        "evolution-footer",
        "secondary-details",
        "summary>更多审计信息",
        "outcome-card",
        "story-flow",
        "资源分配与人工建议一致",
        "该策略已记录版本，可回滚",
        "决策进化对比",
        "策略调整前后",
        "复用已验证经验",
        "人工协同反馈：本次调整的采纳依据",
        "策略版本：可审计、可回滚",
        "场景事件：资源分配变化",
        "fetch('/api/evolution')",
        "返回推演面板",
    ]:
        assert anchor in html, anchor


def test_web_demo_links_to_the_evolution_panel() -> None:
    from decision_poc.web_demo import build_html

    html = build_html()
    assert "href='/evolution'" in html
    assert "决策进化对比" in html
