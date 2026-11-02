"""图论封控：最小割必须比"逐条按阈值判"更省，且算得出来。"""

from __future__ import annotations

import pytest

from decision_poc.quant.graphopt import ClosurePlan, minimum_closure_set


def _edge(edge_id: str, u: str, v: str, weight: float) -> dict[str, object]:
    return {"edge_id": edge_id, "u": u, "v": v, "weight": weight}


def test_single_path_needs_one_cut() -> None:
    plan = minimum_closure_set(
        nodes=["A", "B", "C"],
        edges=[_edge("E-AB", "A", "B", 10.0), _edge("E-BC", "B", "C", 10.0)],
        sources=["A"],
    )
    assert plan.cut_edge_ids == ["E-AB"]
    assert plan.total_weight == pytest.approx(10.0)
    assert plan.assumptions


def test_parallel_paths_need_both_cut() -> None:
    plan = minimum_closure_set(
        nodes=["A", "B1", "B2", "C"],
        edges=[
            _edge("E-A-B1", "A", "B1", 5.0),
            _edge("E-B1-C", "B1", "C", 5.0),
            _edge("E-A-B2", "A", "B2", 5.0),
            _edge("E-B2-C", "B2", "C", 5.0),
        ],
        sources=["A"],
    )
    assert plan.cut_edge_ids == ["E-A-B1", "E-A-B2"]
    assert plan.total_weight == pytest.approx(10.0)


def test_cheapest_edge_on_a_corridor_is_chosen() -> None:
    # 单条走廊 A-x-y-B：应在最便宜的那一段切（A-x=1），而不是切 50 的段
    plan = minimum_closure_set(
        nodes=["A", "x", "y", "B"],
        edges=[
            _edge("E-Ax", "A", "x", 1.0),
            _edge("E-xy", "x", "y", 50.0),
            _edge("E-yB", "y", "B", 50.0),
        ],
        sources=["A"],
    )
    assert plan.cut_edge_ids == ["E-Ax"]
    assert plan.total_weight == pytest.approx(1.0)


def test_all_corridors_must_be_cut() -> None:
    # 两条并行走廊：轻走廊切 1，重走廊切 100 → 总代价 101（不能只切一条就算隔离）
    plan = minimum_closure_set(
        nodes=["A", "p", "q", "r", "B"],
        edges=[
            _edge("E-Ap", "A", "p", 1.0),
            _edge("E-pq", "p", "q", 50.0),
            _edge("E-qB", "q", "B", 50.0),
            _edge("E-Ar", "A", "r", 100.0),
            _edge("E-rB", "r", "B", 100.0),
        ],
        sources=["A"],
    )
    assert plan.cut_edge_ids == ["E-Ap", "E-Ar"]
    assert plan.total_weight == pytest.approx(101.0)


def test_isolated_source_needs_no_closure() -> None:
    plan = minimum_closure_set(
        nodes=["A", "B"],
        edges=[_edge("E-AB", "A", "B", 10.0)],
        sources=["A"],
        # B 是干区，但 A 与 B 之间的割就是这条边；孤立场景改用独立节点图验证
    )
    assert plan.cut_edge_ids == ["E-AB"]
    isolated = minimum_closure_set(
        nodes=["A", "B", "C"],
        edges=[_edge("E-BC", "B", "C", 10.0)],
        sources=["A"],
    )
    assert isolated.cut_edge_ids == []
    assert isolated.isolated_sources == ["A"]


def test_whole_network_flooded_returns_empty_plan() -> None:
    plan = minimum_closure_set(
        nodes=["A", "B"],
        edges=[_edge("E-AB", "A", "B", 10.0)],
        sources=["A", "B"],
    )
    assert plan.cut_edge_ids == []
    assert any("没有可隔离的边" in item for item in plan.assumptions)


def test_multi_source_region() -> None:
    plan = minimum_closure_set(
        nodes=["A1", "A2", "B"],
        edges=[
            _edge("E-1", "A1", "A2", 3.0),
            _edge("E-2", "A2", "B", 7.0),
        ],
        sources=["A1", "A2"],
    )
    assert plan.cut_edge_ids == ["E-2"]
    assert plan.total_weight == pytest.approx(7.0)


def test_unknown_source_is_rejected() -> None:
    with pytest.raises(ValueError):
        minimum_closure_set(nodes=["A"], edges=[], sources=["Z"])


def test_edge_must_carry_its_ledger_fields() -> None:
    with pytest.raises(ValueError):
        minimum_closure_set(nodes=["A", "B"], edges=[{"edge_id": "E", "u": "A", "v": "B"}], sources=["A"])


def test_non_positive_weight_is_rejected() -> None:
    with pytest.raises(ValueError):
        minimum_closure_set(
            nodes=["A", "B"], edges=[_edge("E-AB", "A", "B", 0.0)], sources=["A"]
        )


def test_result_is_deterministic() -> None:
    nodes = ["A", "B", "C", "D"]
    edges = [_edge("E-AB", "A", "B", 2.0), _edge("E-AD", "A", "D", 2.0), _edge("E-BC", "B", "C", 2.0)]
    first = minimum_closure_set(nodes=nodes, edges=edges, sources=["A"])
    second = minimum_closure_set(nodes=nodes, edges=edges, sources=["A"])
    assert first.model_dump() == second.model_dump()
