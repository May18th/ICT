"""图论封控优化：用最小割选"代价最小的封控组合"，而不是逐条按阈值判。

问题形式化：

- 路网是无向图，边权 = 该路段被封闭的代价（用时延增量或长度，由调用方给）；
- 已知哪些节点/路段已经进水（`sources`）；
- 要切断"进水区域"与"干区"之间的所有通路，且让被封闭边的总代价最小 → **无向图最小边割**。

实现用 Edmonds-Karp（BFS 增广）求最大流，再按残量图取最小割。
无向边按两条反向弧建模（各给 `capacity = weight`），这是无向最小割的标准做法。

**边界**：只做拓扑与代价，不含实时流量、信号配时、排队与转向限制；
边权口径由调用方决定，本模块不猜"封一条路值多少钱"。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence

from pydantic import Field, model_validator

from decision_poc.contracts import StrictModel

_INF = float("inf")

_ASSUMPTIONS = [
    "无向图：一条路段封闭即两个方向都不通，与『全幅封闭』语义一致",
    "边权由调用方给出（时延增量或长度），本模块不自行估计封控代价",
    "只做拓扑最小割，不含实时流量、信号配时、排队与转向限制",
    "已进水但完全孤立的区域无需封控，会被显式排除而不是计 0 条边",
]

SOURCE_MIN_CUT = "无向图最小边割（Edmonds-Karp 最大流 + 残量图取割）"


class ClosurePlan(StrictModel):
    cut_edge_ids: list[str]
    total_weight: float = Field(ge=0.0)
    isolated_sources: list[str] = Field(default_factory=list)
    dry_nodes: list[str] = Field(default_factory=list)
    formula_source: str = SOURCE_MIN_CUT
    assumptions: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicate_edges(self) -> ClosurePlan:
        if len(set(self.cut_edge_ids)) != len(self.cut_edge_ids):
            raise ValueError("割边集合不应重复")
        return self


def minimum_closure_set(
    nodes: Sequence[str],
    edges: Sequence[Mapping[str, object]],
    sources: Sequence[str],
) -> ClosurePlan:
    """求把 `sources` 与其余节点切断的最小代价边集。

    `edges` 每项至少含 `edge_id` / `u` / `v` / `weight`；缺字段直接报错，
    不按默认值猜——封控方案里的每一条边都要能追溯到台账。
    """

    node_list = [str(node) for node in nodes]
    if not node_list:
        raise ValueError("nodes 不能为空")
    if len(set(node_list)) != len(node_list):
        raise ValueError("nodes 不应重复")
    source_set = {str(node) for node in sources}
    unknown = source_set - set(node_list)
    if unknown:
        raise ValueError(f"sources 含未声明的节点: {sorted(unknown)}")
    dry = [node for node in node_list if node not in source_set]

    parsed: list[tuple[str, str, str, float]] = []
    for edge in edges:
        for key in ("edge_id", "u", "v", "weight"):
            if key not in edge:
                raise ValueError(f"边缺少字段 {key}: {edge!r}")
        edge_id, u, v = str(edge["edge_id"]), str(edge["u"]), str(edge["v"])
        weight = float(edge["weight"])
        if weight <= 0:
            raise ValueError(f"边权必须为正: {edge_id}")
        if u not in node_list or v not in node_list:
            raise ValueError(f"边 {edge_id} 指向未声明的节点")
        parsed.append((edge_id, u, v, weight))

    if not source_set or not dry:
        # 全网进水或全网干区：没有"隔离"这件事可做，明确返回空方案
        return ClosurePlan(
            cut_edge_ids=[],
            total_weight=0.0,
            isolated_sources=sorted(source_set) if not dry else [],
            dry_nodes=dry,
            assumptions=[*_ASSUMPTIONS, "水源与干区之间没有可隔离的边，返回空方案而不是编一条"],
        )

    # 节点编号
    index = {node: i for i, node in enumerate(node_list)}
    super_source, super_sink = len(node_list), len(node_list) + 1
    size = len(node_list) + 2
    capacity: list[list[float]] = [[0.0] * size for _ in range(size)]

    def add_arc(u: int, v: int, cap: float) -> None:
        capacity[u][v] += cap

    for _, u, v, weight in parsed:
        iu, iv = index[u], index[v]
        add_arc(iu, iv, weight)
        add_arc(iv, iu, weight)
    infinite = float(sum(weight for _, _, _, weight in parsed) + 1.0)
    for node in source_set:
        add_arc(super_source, index[node], infinite)
    for node in dry:
        add_arc(index[node], super_sink, infinite)

    # Edmonds-Karp
    def bfs() -> list[int] | None:
        parent = [-1] * size
        parent[super_source] = super_source
        queue: deque[int] = deque([super_source])
        while queue:
            u = queue.popleft()
            for v in range(size):
                if capacity[u][v] > 1e-12 and parent[v] == -1:
                    parent[v] = u
                    if v == super_sink:
                        return parent
                    queue.append(v)
        return None

    while True:
        parent = bfs()
        if parent is None:
            break
        bottleneck = _INF
        v = super_sink
        while v != super_source:
            u = parent[v]
            bottleneck = min(bottleneck, capacity[u][v])
            v = u
        v = super_sink
        while v != super_source:
            u = parent[v]
            capacity[u][v] -= bottleneck
            capacity[v][u] += bottleneck
            v = u

    # 残量图可达集 = 割的源侧
    reachable = {super_source}
    queue: deque[int] = deque([super_source])
    while queue:
        u = queue.popleft()
        for v in range(size):
            if capacity[u][v] > 1e-12 and v not in reachable:
                reachable.add(v)
                queue.append(v)

    cut: list[str] = []
    total = 0.0
    for edge_id, u, v, weight in parsed:
        iu, iv = index[u], index[v]
        if (iu in reachable) != (iv in reachable):
            cut.append(edge_id)
            total += weight

    isolated = [
        node
        for node in sorted(source_set)
        if not any(index[node] == index[u] or index[node] == index[v] for _, u, v, _ in parsed)
    ]
    return ClosurePlan(
        cut_edge_ids=sorted(cut),
        total_weight=total,
        isolated_sources=isolated,
        dry_nodes=sorted(dry),
        assumptions=list(_ASSUMPTIONS),
    )
