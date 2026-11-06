"""库容曲线来源标注：声明、继承、缓存隔离、默认路径形状。

为什么单独一组
--------------
"12 分钟到警戒"这类数字一旦进对外材料，出处就不能只活在文档里。这组测试锁四件事：

1. 声明了来源就同时进契约（`storage_provenance`）和 `assumptions` 的固定前缀行，
   不是只写在注释里；
2. 下游查询（到达时刻、超阈时长）继承同一行，不允许中途掉链；
3. 同一组数值、不同来源不共享缓存对象——否则先跑的假设值会替后来的实测值"背书"，
   这正是本项目最怕的洗白方向；
4. 不声明来源的历史调用路径，dump 只多 `storage_provenance` 一个键，其余键序与值原样。
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from decision_poc.contracts import ProvenanceLevel
from decision_poc.hydro_contracts import CurveProvenance, HydrographResult
from decision_poc.quant import level_exceedance_duration, level_pool_route, time_to_level
from decision_poc.quant.hydrodynamics import PROVENANCE_ASSUMPTION_PREFIX
from decision_poc.skills import SkillRuntime

EVENT = "WO-20261029-007"
AS_OF = datetime.fromisoformat("2026-10-29T09:40:00+08:00")
CURVE_PATH = Path(__file__).resolve().parents[1] / "samples" / "assets" / "urban" / "pump_forebay_curves.csv"
FOREBAY_REF = "待补：DF-PS-01 前池竣工图/设计图"

# 加来源字段之前的键序；新增键必须排在最后，旧键相对顺序不允许变
LEGACY_DUMP_KEYS = [
    "times_h",
    "values",
    "unit",
    "formula_id",
    "formula_source",
    "assumptions",
    "inputs",
    "summary",
    "error_bound",
    "peak",
    "peak_time_h",
    "computable",
    "uncomputable_reason",
]


def _curve() -> tuple[list[float], list[float]]:
    with CURVE_PATH.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [float(row["stage_m"]) for row in rows], [float(row["storage_m3"]) for row in rows]


def _route(
    provenance: CurveProvenance | None = None,
    *,
    steps: int = 21,
):
    stages, storages = _curve()
    return level_pool_route(
        inflow_m3h=[1200.0] * steps,
        dt_h=0.01,
        stage_m=stages,
        storage_m3=storages,
        initial_stage_m=0.0,
        pump_outflow_m3h=0.0,
        storage_provenance=provenance,
    )


def test_level_pool_route_carries_storage_provenance() -> None:
    """声明来源 → 契约字段与 assumptions 前缀行同时出现，且只出现一次。"""

    hydrograph = _route(CurveProvenance.assumption(FOREBAY_REF))
    assert hydrograph.storage_provenance is not None
    assert hydrograph.storage_provenance.basis is ProvenanceLevel.ASSUMPTION
    assert hydrograph.storage_provenance.ref == FOREBAY_REF
    assert hydrograph.assumptions[-1] == f"{PROVENANCE_ASSUMPTION_PREFIX}assumption｜{FOREBAY_REF}"
    assert sum(line.startswith(PROVENANCE_ASSUMPTION_PREFIX) for line in hydrograph.assumptions) == 1


def test_downstream_queries_inherit_storage_provenance() -> None:
    """到达时刻与超阈时长都继承上游来源；未声明来源时两处都不该凭空多出一行。"""

    hydrograph = _route(CurveProvenance.real("竣工图 JS-2023-014"))
    reached = time_to_level(hydrograph=hydrograph, target_level_m=2.4)
    exceeded = level_exceedance_duration(hydrograph=hydrograph, threshold_m=2.4)
    for result in (reached, exceeded):
        # 位置不固定：超阈时长会在末尾追加"未回落"备注，因此约定是"有且只有一行带前缀"
        marked = [line for line in result.assumptions if line.startswith(PROVENANCE_ASSUMPTION_PREFIX)]
        assert len(marked) == 1
        assert "real" in marked[0]
        assert "JS-2023-014" in marked[0]

    bare = _route()
    for result in (
        time_to_level(hydrograph=bare, target_level_m=2.4),
        level_exceedance_duration(hydrograph=bare, threshold_m=2.4),
    ):
        assert all(
            not line.startswith(PROVENANCE_ASSUMPTION_PREFIX) for line in result.assumptions
        )


def test_default_path_adds_only_one_dump_key() -> None:
    """不声明来源时：旧键序与旧值不动，只在末尾多一个 storage_provenance=None。"""

    hydrograph = _route()
    payload = hydrograph.model_dump()
    assert list(payload) == [*LEGACY_DUMP_KEYS, "storage_provenance"]
    assert payload["storage_provenance"] is None
    assert len(hydrograph.assumptions) == 5, "默认路径的 assumptions 条数不允许变"
    assert len(payload) == len(LEGACY_DUMP_KEYS) + 1, "只允许新增一个键，不准夹带别的字段"


def test_cache_key_separates_storage_provenance() -> None:
    """同数值不同来源必须落两个缓存键；同来源重复调用仍命中同一个对象。"""

    runtime = SkillRuntime()
    assumed = _route(CurveProvenance.assumption(FOREBAY_REF))
    measured = _route(CurveProvenance.real("竣工图 JS-2023-014"))

    first = runtime.deduce(EVENT, as_of=AS_OF, hydrograph=assumed)
    second = runtime.deduce(EVENT, as_of=AS_OF, hydrograph=measured)
    again = runtime.deduce(EVENT, as_of=AS_OF, hydrograph=assumed)

    assert len(runtime._deductions) == 2
    assert second is not first
    assert again is first
    assert second.hydrograph is not None
    assert second.hydrograph.storage_provenance is not None
    assert second.hydrograph.storage_provenance.basis is ProvenanceLevel.REAL


def test_curve_provenance_requires_ref_only_when_real() -> None:
    """契约只管形状：real 必须有出处，assumption/derived 允许只有级别。"""

    with pytest.raises(ValidationError):
        CurveProvenance(basis=ProvenanceLevel.REAL, ref="   ")
    assert CurveProvenance(basis=ProvenanceLevel.ASSUMPTION).ref == ""
    assert CurveProvenance(basis=ProvenanceLevel.REAL, ref="竣工图 JS-2023-014").ref
    assert CurveProvenance.assumption().basis is ProvenanceLevel.ASSUMPTION
    assert CurveProvenance.derived("storage = area × stage").basis is ProvenanceLevel.DERIVED


def test_two_kwarg_form_is_unrepresentable() -> None:
    """旧的「级别 + 出处」两参数写法必须完全不可传，而不是传了才报错。

    静默丢来源的根因是"两个松散参数可以只传一个"。修法不是加校验，而是让这种组合
    在签名上不存在——本测试锁住这一点：任何人把 `storage_basis` 加回签名，这里立刻红。
    """

    stages, storages = _curve()
    common = dict(
        inflow_m3h=[1200.0] * 21,
        dt_h=0.01,
        stage_m=stages,
        storage_m3=storages,
        initial_stage_m=0.0,
        pump_outflow_m3h=0.0,
    )
    with pytest.raises(TypeError):
        level_pool_route(**common, storage_basis=ProvenanceLevel.ASSUMPTION)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        level_pool_route(**common, storage_ref="竣工图 JS-2023-014")  # type: ignore[call-arg]


def test_public_exports_expose_provenance_types() -> None:
    """公开契约的导出面要一致：根包与 quant 都能直接拿到来源类型。"""

    import decision_poc
    import decision_poc.quant as quant

    assert decision_poc.CurveProvenance is CurveProvenance
    assert decision_poc.ProvenanceLevel is ProvenanceLevel
    assert quant.CurveProvenance is CurveProvenance
    for module in (decision_poc, quant):
        assert "CurveProvenance" in module.__all__, f"{module.__name__}.__all__ 缺 CurveProvenance"
    assert "ProvenanceLevel" in decision_poc.__all__
