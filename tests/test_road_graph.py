"""路网拓扑测试：封路之后能不能绕、绕多远、慢多少，以及缺字段时怎么如实降级。

这些断言全部盯"数字从哪来"：路线是图上跑出来的，速度是表里的，降速系数写在
同一行数据上。任何一条被改成常数，测试就应该红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_poc.road_graph import (
    RoadGraph,
    RoadGraphUnavailable,
    describe_route,
)
from decision_poc.urban_dataset import DEFAULT_DATASET_ROOT

ROADS = DEFAULT_DATASET_ROOT / "road_network.csv"


@pytest.fixture(scope="module")
def graph() -> RoadGraph:
    return RoadGraph.load()


def test_graph_reads_every_road_as_an_edge(graph: RoadGraph) -> None:
    assert len(graph.edges) == 7
    assert graph.nodes() == ["J-01", "J-02", "J-03", "J-04", "J-05", "K-01", "K-02", "K-03"]
    r101 = graph.road("R-101")
    assert r101 is not None
    assert (r101.from_node, r101.to_node) == ("J-01", "J-02")
    assert r101.minutes == pytest.approx(820 * 0.06 / 40)
    assert r101.evidence_ref.endswith("#row:2"), "证据要指到源文件那一行，不是行号偏移"


def test_baseline_route_uses_the_direct_road(graph: RoadGraph) -> None:
    route = graph.route("J-01", "J-02")
    assert route is not None
    assert route.roads == ["R-101"]
    assert route.meters == 820
    assert route.minutes == pytest.approx(1.23, abs=0.01)


def test_closure_forces_a_detour_around_the_ring(graph: RoadGraph) -> None:
    impact = graph.impact("R-101")
    assert impact.computable and not impact.cut
    assert impact.baseline is not None and impact.after_closure is not None
    assert impact.after_closure.roads == ["R-102", "R-103", "R-104"]
    assert impact.after_closure.meters == 3860
    assert impact.delta_meters == 3040
    assert impact.delta_minutes == pytest.approx(5.11, abs=0.02)
    assert "绕行 +3040 m" in impact.describe()


def test_work_zone_factor_slows_the_designated_detour(graph: RoadGraph) -> None:
    """R-103 是公告指定的绕行路线，但它自己在占道施工：降速系数写在数据里。"""
    r103 = graph.road("R-103")
    assert r103 is not None
    assert r103.work_zone_factor == 0.5
    assert r103.effective_speed_kmh == 15
    slowed = r103.minutes
    unslowed = r103.meters * 0.06 / r103.speed_kmh
    assert slowed == pytest.approx(unslowed * 2)


def test_cut_is_reported_as_cut_not_as_a_big_number(graph: RoadGraph) -> None:
    """三条路同时断了就是断了：返回 None 并标 cut，不返回 inf 冒充"很慢"。"""
    assert graph.route("J-01", "J-02", blocked={"R-101", "R-102", "R-104"}) is None
    impact = graph.impact("R-101")
    assert impact.cut is False
    assert graph.route("J-01", "J-09") is None, "未知节点不该被当成连通"


def test_districts_are_not_connected_so_cross_district_eta_stays_unknown(graph: RoadGraph) -> None:
    """A-03 与 B-01 之间没有路段连接数据，跨区路径就只能说算不出。"""
    assert graph.route("J-01", "K-01") is None


def test_missing_topology_columns_raise_instead_of_defaulting(tmp_path: Path) -> None:
    """缺 speed_kmh 时不许给个默认限速把数字凑出来。"""
    broken = tmp_path / "road_network.csv"
    broken.write_text(
        "road_id,name,from_node,to_node,length_m\nR-101,兴华路,J-01,J-02,820\n",
        encoding="utf-8",
    )
    with pytest.raises(RoadGraphUnavailable) as excinfo:
        RoadGraph.from_csv(broken)
    assert "speed_kmh" in str(excinfo.value)


def test_dataclass_describe_is_stable(graph: RoadGraph) -> None:
    route = graph.route("J-01", "J-02", blocked={"R-101"})
    assert route is not None
    first = route.describe()
    second = graph.route("J-01", "J-02", blocked={"R-101"})
    assert second is not None and second.describe() == first
    assert describe_route([edge for edge in graph.edges if edge.road_id in set(route.roads)])
