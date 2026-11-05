"""定量层的结果契约。

设计取舍（与 P12 规格 §6.4 对齐）：

- 单个 `value` 而不是把中间量塞进结果：中间量放 `inputs`，让"这个数怎么来的"可复核；
- `error_bound` 是字符串而不是数值区间：多数输入未率定，硬写成 ±x 会变成假精确；
  需要区间时用 `SpreadResult`，它是真算出来的区间，不是估的。
"""

from __future__ import annotations

from pydantic import Field, model_validator

from decision_poc.contracts import StrictModel
from decision_poc.hydro_contracts import HydrographResult


class QuantResult(StrictModel):
    """一个可复核的定量结果。"""

    value: float
    unit: str = Field(min_length=1)
    formula_id: str = Field(min_length=1)
    formula_source: str = Field(min_length=1)
    assumptions: list[str] = Field(min_length=1)
    inputs: dict[str, float]
    error_bound: str = Field(min_length=1)
    computable: bool = True
    uncomputable_reason: str = ""

    @model_validator(mode="after")
    def _check_invariants(self) -> QuantResult:
        if any(not item.strip() for item in self.assumptions):
            raise ValueError("assumptions 不允许出现空条目")
        if self.computable:
            if self.uncomputable_reason.strip():
                raise ValueError("computable=True 时不应带 uncomputable_reason")
            return self
        if self.value != 0:
            raise ValueError("computable=False 时 value 必须为 0，不得给出近似值")
        if not self.uncomputable_reason.strip():
            raise ValueError("computable=False 时必须给出 uncomputable_reason")
        return self


class SpreadResult(StrictModel):
    """带区间的结果：低/标称/高三点，且三点顺序必须成立。"""

    low: float
    nominal: float
    high: float
    unit: str = Field(min_length=1)
    formula_id: str = Field(min_length=1)
    formula_source: str = Field(min_length=1)
    assumptions: list[str] = Field(min_length=1)
    samples: int = Field(ge=1)
    seed: int | None = None
    inputs: dict[str, tuple[float, float]] = {}

    @model_validator(mode="after")
    def _check_order(self) -> SpreadResult:
        if not (self.low <= self.nominal <= self.high):
            raise ValueError("区间必须满足 low <= nominal <= high")
        if any(not item.strip() for item in self.assumptions):
            raise ValueError("assumptions 不允许出现空条目")
        return self


def uncomputable(
    *,
    formula_id: str,
    formula_source: str,
    assumptions: list[str],
    inputs: dict[str, float],
    reason: str,
    unit: str = "n/a",
    error_bound: str = "不可计算",
) -> QuantResult:
    """构造一个"明确算不出来"的结果，而不是编一个近似值。"""

    return QuantResult(
        value=0.0,
        unit=unit,
        formula_id=formula_id,
        formula_source=formula_source,
        assumptions=assumptions,
        inputs=inputs,
        error_bound=error_bound,
        computable=False,
        uncomputable_reason=reason,
    )


def uncomputable_hydrograph(
    *,
    formula_id: str,
    formula_source: str,
    assumptions: list[str],
    inputs: dict[str, float],
    reason: str,
    unit: str = "n/a",
    error_bound: str = "不可计算",
) -> HydrographResult:
    """构造一个"明确给不出过程线"的结果，而不是截断/外推一段假过程。"""

    return HydrographResult(
        unit=unit,
        formula_id=formula_id,
        formula_source=formula_source,
        assumptions=assumptions,
        inputs=inputs,
        error_bound=error_bound,
        computable=False,
        uncomputable_reason=reason,
    )
