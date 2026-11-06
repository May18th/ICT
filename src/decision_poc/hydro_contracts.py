"""跨层共享的过程线契约。

定量层负责生产 `HydrographResult`，推演、协同和回放层只消费这份带边界的
结果。契约放在根包，避免上层导入 `decision_poc.quant` 时触发定量层的聚合
导出，从而形成与推演规则的循环依赖。
"""

from __future__ import annotations

from pydantic import model_validator

from .contracts import ProvenanceLevel, StrictModel


class CurveProvenance(StrictModel):
    """过程线输入曲线（如库容曲线）的来源标注。

    只约束形状：`basis` 取统一枚举，`real` 必须给出可定位的出处。
    具体内容规则（"待补：…"、同一曲线不许混用级别）由数据层守卫负责，
    契约层不认业务字面。
    """

    basis: ProvenanceLevel
    ref: str = ""

    @model_validator(mode="after")
    def _check_shape(self) -> "CurveProvenance":
        if self.basis is ProvenanceLevel.REAL and not self.ref.strip():
            raise ValueError("basis=real 时必须给出可定位的出处")
        return self

    @classmethod
    def assumption(cls, ref: str = "") -> "CurveProvenance":
        """无公开口径的假设曲线；`ref` 建议写「待补：…」指明缺哪份资料。"""

        return cls(basis=ProvenanceLevel.ASSUMPTION, ref=ref)

    @classmethod
    def derived(cls, ref: str) -> "CurveProvenance":
        """由真实值按明确公式算出；`ref` 写公式或推导出处。"""

        return cls(basis=ProvenanceLevel.DERIVED, ref=ref)

    @classmethod
    def real(cls, ref: str) -> "CurveProvenance":
        """原文可定位（图纸、台账行）；`ref` 必填。"""

        return cls(basis=ProvenanceLevel.REAL, ref=ref)


class HydrographResult(StrictModel):
    """一条可复核的过程线（水位或流量随时间的离散序列）。"""

    times_h: list[float] = []
    values: list[float] = []
    unit: str
    formula_id: str
    formula_source: str
    assumptions: list[str]
    inputs: dict[str, float] = {}
    summary: dict[str, float] = {}
    error_bound: str
    peak: float = 0.0
    peak_time_h: float = 0.0
    computable: bool = True
    uncomputable_reason: str = ""
    # 输入曲线的来源；None 表示调用方没声明（历史调用路径默认如此，dump 形状只多这一个键）
    storage_provenance: CurveProvenance | None = None

    @model_validator(mode="after")
    def _check_invariants(self) -> "HydrographResult":
        if not self.unit.strip() or not self.formula_id.strip() or not self.formula_source.strip():
            raise ValueError("unit、formula_id、formula_source 不允许为空")
        if not self.assumptions or any(not item.strip() for item in self.assumptions):
            raise ValueError("assumptions 不允许为空或出现空条目")
        if not self.error_bound.strip():
            raise ValueError("error_bound 不允许为空")
        if not self.computable:
            if self.times_h or self.values:
                raise ValueError("computable=False 时不得给出过程线")
            if not self.uncomputable_reason.strip():
                raise ValueError("computable=False 时必须给出 uncomputable_reason")
            return self
        if self.uncomputable_reason.strip():
            raise ValueError("computable=True 时不应带 uncomputable_reason")
        if len(self.times_h) != len(self.values):
            raise ValueError("times_h 与 values 长度必须一致")
        if len(self.times_h) < 2:
            raise ValueError("过程线至少需要两个时段")
        if any(later <= earlier for earlier, later in zip(self.times_h, self.times_h[1:])):
            raise ValueError("times_h 必须严格递增")
        expected_peak = max(self.values)
        expected_index = self.values.index(expected_peak)
        if abs(expected_peak - self.peak) > 1e-9:
            raise ValueError("peak 必须等于 values 的最大值")
        if abs(self.times_h[expected_index] - self.peak_time_h) > 1e-9:
            raise ValueError("peak_time_h 必须等于峰值出现的时刻")
        return self
