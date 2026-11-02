"""不确定性传播：把"点估计"换成"区间"，让决策能回答"预报改了就变吗"。

两种做法，用途不同：

- `range_from_bounds`：确定性角点枚举。输入只有上下界、公式单调时它给的是**真极值**，
  结果是可复算的（同一输入永远同一输出），适合进自检；
- `monte_carlo`：固定种子的均匀抽样。用于非单调或需要分布形状的量级判断，
  **种子必须显式传入**，否则"同一输入两次结果一致"这条口径就废了。

两条都不做：不拟合输入分布（没有率定数据，拟合出来也是假的）、不报置信度
（均匀抽样没有概率意义，只能报区间）。
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Callable, Mapping

from decision_poc.quant.contracts import SpreadResult

Number = float
Bounds = Mapping[str, tuple[float, float]]

SOURCE_RANGE = "确定性角点枚举：对每个输入取上下界组合，取函数值极值"
SOURCE_MONTE_CARLO = "固定种子均匀抽样：在输入上下界内均匀取样，取样本极值与均值"

_ASSUMPTION_RANGE = [
    "输入只给上下界，不给分布；区间是极值，不是置信区间",
    "公式在各输入上单调或近似单调时结果才有意义",
]
_ASSUMPTION_MONTE_CARLO = [
    "输入在上下界内按均匀分布抽样，不代表真实分布",
    "结果的低/高值是样本极值，不是置信区间",
]

_MAX_CORNER_DIMENSIONS = 8


def _check_bounds(bounds: Bounds) -> None:
    if not bounds:
        raise ValueError("bounds 不能为空")
    for name, (low, high) in bounds.items():
        if low > high:
            raise ValueError(f"输入 {name} 的下界不能大于上界")


def _round_trip_point(bounds: Bounds) -> dict[str, float]:
    return {name: (float(low) + float(high)) / 2.0 for name, (low, high) in bounds.items()}


def range_from_bounds(
    func: Callable[[dict[str, float]], float],
    bounds: Bounds,
    *,
    formula_id: str,
    formula_source: str = SOURCE_RANGE,
    assumptions: list[str] | None = None,
    unit: str,
) -> SpreadResult:
    """角点枚举求区间；维度上限 8（2^8 = 256 个角点）。"""

    _check_bounds(bounds)
    if len(bounds) > _MAX_CORNER_DIMENSIONS:
        raise ValueError(f"角点枚举最多支持 {_MAX_CORNER_DIMENSIONS} 个输入，实际 {len(bounds)} 个")
    names = sorted(bounds)
    corners = list(itertools.product(*[bounds[name] for name in names]))
    values = [float(func(dict(zip(names, corner, strict=True)))) for corner in corners]
    nominal = float(func(_round_trip_point(bounds)))
    return SpreadResult(
        low=min(values),
        nominal=nominal,
        high=max(values),
        unit=unit,
        formula_id=formula_id,
        formula_source=formula_source,
        assumptions=list(assumptions or _ASSUMPTION_RANGE),
        samples=len(values),
        seed=None,
        inputs={name: (float(bounds[name][0]), float(bounds[name][1])) for name in names},
    )


def monte_carlo(
    func: Callable[[dict[str, float]], float],
    bounds: Bounds,
    *,
    seed: int,
    samples: int,
    formula_id: str,
    formula_source: str = SOURCE_MONTE_CARLO,
    assumptions: list[str] | None = None,
    unit: str,
) -> SpreadResult:
    """固定种子均匀抽样；同 seed + 同 samples 两次调用结果逐位一致。"""

    _check_bounds(bounds)
    if samples < 1:
        raise ValueError("samples 必须大于等于 1")
    rng = random.Random(seed)
    names = sorted(bounds)
    values: list[float] = []
    for _ in range(samples):
        point = {name: rng.uniform(float(bounds[name][0]), float(bounds[name][1])) for name in names}
        values.append(float(func(point)))
    return SpreadResult(
        low=min(values),
        nominal=sum(values) / len(values),
        high=max(values),
        unit=unit,
        formula_id=formula_id,
        formula_source=formula_source,
        assumptions=list(assumptions or _ASSUMPTION_MONTE_CARLO),
        samples=samples,
        seed=seed,
        inputs={name: (float(bounds[name][0]), float(bounds[name][1])) for name in names},
    )
