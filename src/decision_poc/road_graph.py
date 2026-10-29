"""路段拓扑：封路之后车还走不走得通、要绕多远、慢多少。

CF1 问的是"若先封路，会影响哪些抢修路径"。在拿到 `from_node/to_node/speed_kmh`
之前，这个问题只能答成一句话——"数据集没有拓扑，无法计算"——因为 `road_network`
原本只有单条长度，没有任何两条路共用一个路口的信息。本模块把那句话换成可复算的
数字：路段连成图，封路就是删一条边，绕行就是图上另一条路径。

口径（写清楚了才有资格被核对）：

- 时间是**自由流通行时间** Σ(长度 ÷ 有效速度)，不含路口延误、不含排队。
  有效速度 = 设计速度 × 占道降速系数（系数写在 `road_network.csv` 里，
  因为"该路段占道施工"是数据里的事实，不是代码里的常数）。
- 封路的语义是**该路段整条不可通行**，与 `notice_road_closure.md` §2「全幅封闭」一致。
- 基线同样排除 `status != normal` 的路段（施工全封闭的路本来就开不进去），所以
  基线与封路后两个数之差只来自"新增封的那一条路"。
- 门到门到场时间仍然算不出：没有抢修驻地坐标、没有实时路况。这里给的是
  "从封路路段一端到另一端"的通行时间增量，不是"车从车库到现场"的时间。

三件事刻意不做：不引入网络库（Dijkstra 二十行就够，且要能解释每一步）；不猜
路口延误系数（猜了就是编数）；不把"没有路径"降级成"时间很长"（`cut=True`，
调用方必须显式处理断网）。
"""

from __future__ import annotations

import csv
import heapq
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import Field

from .contracts import StrictModel
from .urban_dataset import DEFAULT_DATASET_ROOT

ROAD_FILE = "road_network.csv"
ROAD_LOCATOR = "A-03/road_network/v1.1"

# 缺这些列就只能说算不出：不从别的列反推速度，也不给默认限速。
REQUIRED_COLUMNS: tuple[str, ...] = ("road_id", "from_node", "to_node", "length_m", "speed_kmh")

PASSABLE_STATUS = "normal"


class RoadGraphUnavailable(RuntimeError):
    """路网缺拓扑字段时抛这个，而不是返回一个"看起来像 0 分钟"的结果。"""


class RoadEdge(StrictModel):
    road_id: str
    name: str
    from_node: str
    to_node: str
    meters: float
    speed_kmh: float
    work_zone_factor: float = 1.0
    status: str = PASSABLE_STATUS
    district: str = ""
    line_no: int = 0

    @property
    def passable(self) -> bool:
        return self.status == PASSABLE_STATUS

    @property
    def effective_speed_kmh(self) -> float:
        return self.speed_kmh * self.work_zone_factor

    @property
    def minutes(self) -> float:
        # m ÷ (km/h × 1000 ÷ 60) = m × 0.06 ÷ km/h
        speed = self.effective_speed_kmh
        if speed <= 0:
            return float("inf")
        return self.meters * 0.06 / speed

    @property
    def evidence_ref(self) -> str:
        return f"{ROAD_LOCATOR}#row:{self.line_no}"


class Route(StrictModel):
    origin: str
    target: str
    nodes: list[str] = Field(default_factory=list)
    roads: list[str] = Field(default_factory=list)
    meters: float = 0.0
    minutes: float = 0.0
    evidence_refs: list[str] = Field(default_factory=list)

    def describe(self) -> str:
        if not self.roads:
            return f"{self.origin} 与 {self.target} 是同一点"
        return (
            f"{self.origin}→{self.target} 经 {'/'.join(self.roads)}，"
            f"{self.meters:.0f} m，{self.minutes:.1f} 分钟"
        )


class TravelImpact(StrictModel):
    """封一条路前后，同一对节点之间的通行代价差。"""

    origin: str
    target: str
    closed_road: str
    baseline: Route | None = None
    after_closure: Route | None = None
    delta_minutes: float | None = None
    delta_meters: float | None = None
    cut: bool = False
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)

    @property
    def computable(self) -> bool:
        return self.baseline is not None and self.after_closure is not None

    def describe(self) -> str:
        if not self.computable:
            if self.baseline is not None and self.after_closure is None:
                return (
                    f"封 {self.closed_road} 后 {self.origin}→{self.target} 无可行路径（切断）"
                )
            return f"通行影响无法计算：{self.reason}"
        before, after = self.baseline, self.after_closure
        assert before is not None and after is not None
        if abs(after.minutes - before.minutes) < 1e-9:
            return f"封 {self.closed_road} 未改变 {self.origin}→{self.target} 的通行时间"
        return (
            f"封 {self.closed_road} 使 {self.origin}→{self.target} 从 "
            f"{before.meters:.0f} m / {before.minutes:.1f} 分钟 变为 "
            f"{after.meters:.0f} m / {after.minutes:.1f} 分钟"
            f"（绕行 {after.meters - before.meters:+.0f} m，"
            f"{after.minutes - before.minutes:+.1f} 分钟，经 {'/'.join(after.roads)}）"
        )


class RoadGraph(StrictModel):
    edges: list[RoadEdge] = Field(default_factory=list)

    # --- 构造 -----------------------------------------------------------
    @classmethod
    def from_csv(cls, path: Path | str) -> RoadGraph:
        path = Path(path)
        if not path.exists():
            raise RoadGraphUnavailable(f"路网文件不存在: {path}")
        text = path.read_text(encoding="utf-8-sig")
        reader = csv.DictReader(text.splitlines())
        header = [name.strip() for name in (reader.fieldnames or [])]
        missing = [name for name in REQUIRED_COLUMNS if name not in header]
        if missing:
            raise RoadGraphUnavailable(
                f"{path.name} 缺拓扑字段 {','.join(missing)}，只能给出行程无法计算"
            )
        edges: list[RoadEdge] = []
        for offset, row in enumerate(reader, start=2):
            road_id = (row.get("road_id") or "").strip()
            if not road_id:
                continue
            edges.append(
                RoadEdge(
                    road_id=road_id,
                    name=(row.get("name") or road_id).strip(),
                    from_node=(row.get("from_node") or "").strip(),
                    to_node=(row.get("to_node") or "").strip(),
                    meters=_num(row.get("length_m")),
                    speed_kmh=_num(row.get("speed_kmh")),
                    work_zone_factor=_num(row.get("work_zone_factor"), default=1.0),
                    status=(row.get("status") or PASSABLE_STATUS).strip() or PASSABLE_STATUS,
                    district=(row.get("district") or "").strip(),
                    line_no=offset,
                )
            )
        dangling = [edge.road_id for edge in edges if not edge.from_node or not edge.to_node]
        if dangling:
            raise RoadGraphUnavailable(f"路段缺端点: {','.join(dangling)}")
        return cls(edges=edges)

    @classmethod
    def load(cls, root: Path | str | None = None) -> RoadGraph:
        dataset_root = Path(root) if root is not None else DEFAULT_DATASET_ROOT
        return cls.from_csv(dataset_root / ROAD_FILE)

    # --- 查询 -----------------------------------------------------------
    def road(self, road_id: str) -> RoadEdge | None:
        return next((edge for edge in self.edges if edge.road_id == road_id), None)

    def nodes(self) -> list[str]:
        return sorted({edge.from_node for edge in self.edges} | {edge.to_node for edge in self.edges})

    def neighbors(self, node: str) -> list[RoadEdge]:
        return sorted(
            (edge for edge in self.edges if node in (edge.from_node, edge.to_node)),
            key=lambda edge: (edge.road_id, edge.from_node, edge.to_node),
        )

    def permanently_blocked(self) -> set[str]:
        """施工全封闭等既有阻断，基线也要排除，否则"封路影响"会混进别的变量。"""
        return {edge.road_id for edge in self.edges if not edge.passable}

    def route(
        self,
        origin: str,
        target: str,
        blocked: Iterable[str] = (),
    ) -> Route | None:
        """最短通行时间路径；图不连通时返回 None（不返回 inf，让调用方显式处理）。"""
        if origin == target:
            return Route(origin=origin, target=target, nodes=[origin])
        closed = set(blocked)
        known = set(self.nodes())
        if origin not in known or target not in known:
            return None
        best: dict[str, tuple[float, float, tuple[str, ...], tuple[str, ...]]] = {}
        start = (0.0, 0.0, (origin,), ())
        heap: list[tuple[float, float, tuple[str, ...], tuple[str, ...]]] = [start]
        best[origin] = start
        while heap:
            minutes, meters, node_path, road_path = heapq.heappop(heap)
            node = node_path[-1]
            if node == target:
                edges = [self.road(road_id) for road_id in road_path]
                return Route(
                    origin=origin,
                    target=target,
                    nodes=list(node_path),
                    roads=list(road_path),
                    meters=meters,
                    minutes=minutes,
                    evidence_refs=sorted(
                        {edge.evidence_ref for edge in edges if edge is not None}
                    ),
                )
            if best.get(node, (float("inf"),))[0] < minutes:
                continue
            for edge in self.neighbors(node):
                if edge.road_id in closed or not edge.passable:
                    continue
                nxt = edge.to_node if edge.from_node == node else edge.from_node
                candidate = (
                    minutes + edge.minutes,
                    meters + edge.meters,
                    node_path + (nxt,),
                    road_path + (edge.road_id,),
                )
                if nxt not in best or candidate[:2] < best[nxt][:2]:
                    best[nxt] = candidate
                    heapq.heappush(heap, candidate)
        return None

    def impact(self, closed_road: str, origin: str | None = None, target: str | None = None) -> TravelImpact:
        edge = self.road(closed_road)
        if edge is None:
            return TravelImpact(
                origin=origin or "-",
                target=target or "-",
                closed_road=closed_road,
                reason=f"路网里没有路段 {closed_road}",
            )
        origin = origin or edge.from_node
        target = target or edge.to_node
        blocked = self.permanently_blocked()
        baseline = self.route(origin, target, blocked)
        after = self.route(origin, target, blocked | {closed_road})
        refs = {edge.evidence_ref}
        if baseline is not None:
            refs.update(baseline.evidence_refs)
        if after is not None:
            refs.update(after.evidence_refs)
        reason = ""
        if baseline is None:
            reason = f"{origin}→{target} 在不封路时就不连通，无法比较"
        return TravelImpact(
            origin=origin,
            target=target,
            closed_road=closed_road,
            baseline=baseline,
            after_closure=after,
            delta_minutes=(
                None
                if baseline is None or after is None
                else round(after.minutes - baseline.minutes, 2)
            ),
            delta_meters=(
                None
                if baseline is None or after is None
                else round(after.meters - baseline.meters, 1)
            ),
            cut=baseline is not None and after is None,
            reason=reason,
            evidence_refs=sorted(refs),
        )


def _num(value: object, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def describe_route(route: Sequence[RoadEdge]) -> str:
    """给调用方一个统一的路径文字，避免各处自己拼。"""
    if not route:
        return "（空路径）"
    return "/".join(edge.road_id for edge in route)
