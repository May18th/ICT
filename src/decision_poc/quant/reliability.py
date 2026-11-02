"""可靠性层：把"泵站无备用电源"从形容词变成可比较的数字。

模型是一个最小故障树（两条并联失效路径，任一条发生即"泵站不可用"）：

1. **泵组全失效**：`P = p_pump_failure ** 运行台数`（各泵独立失效）；
2. **电源不可用**：无备电时 `P = p_grid_failure`；有备电时 `P = p_grid_failure × p_backup_failure`。

两条路径并联：`P(不可用) = 1 − (1 − P泵组)(1 − P电源)`。

**参数全部由调用方传入，没有任何"实测默认值"**：默认数字只是量级占位，
必须在输出的 `assumptions` 里如实声明；台账里的 MTBF 一旦补齐，替换输入即可，模型不用改。
"""

from __future__ import annotations

from pydantic import Field, model_validator

from decision_poc.contracts import StrictModel

SOURCE_FAULT_TREE = "最小故障树（并联）：泵组全失效 OR 电源不可用"
DEFAULT_P_PUMP_FAILURE = 0.05
DEFAULT_P_GRID_FAILURE = 0.20
DEFAULT_P_BACKUP_FAILURE = 0.10


class FaultTreeResult(StrictModel):
    station_id: str = Field(min_length=1)
    running_units: int = Field(ge=0)
    backup_power: bool
    p_pumps_all_fail: float = Field(ge=0.0, le=1.0)
    p_power_unavailable: float = Field(ge=0.0, le=1.0)
    p_unavailable: float = Field(ge=0.0, le=1.0)
    cut_sets: list[list[str]] = Field(min_length=1)
    formula_source: str = SOURCE_FAULT_TREE
    assumptions: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _probability_is_consistent(self) -> FaultTreeResult:
        expected = 1.0 - (1.0 - self.p_pumps_all_fail) * (1.0 - self.p_power_unavailable)
        if abs(expected - self.p_unavailable) > 1e-9:
            raise ValueError("p_unavailable 必须等于并联失效概率")
        if any(not item.strip() for item in self.assumptions):
            raise ValueError("assumptions 不允许出现空条目")
        return self


def _check_probability(name: str, value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} 必须在 [0, 1] 内")


def pump_station_unavailable(
    *,
    station_id: str,
    running_units: int,
    backup_power: bool,
    p_pump_failure: float = DEFAULT_P_PUMP_FAILURE,
    p_grid_failure: float = DEFAULT_P_GRID_FAILURE,
    p_backup_failure: float = DEFAULT_P_BACKUP_FAILURE,
) -> FaultTreeResult:
    """一次暴雨情景下泵站"无法提供额定能力"的概率。"""

    if running_units < 0:
        raise ValueError("运行台数不能为负")
    _check_probability("p_pump_failure", p_pump_failure)
    _check_probability("p_grid_failure", p_grid_failure)
    _check_probability("p_backup_failure", p_backup_failure)

    p_pumps = p_pump_failure**running_units if running_units > 0 else 1.0
    if backup_power:
        p_power = p_grid_failure * p_backup_failure
        cut_sets = [["泵组全失效"], ["上级电源失电", "备用电源启动失败"]]
        backup_note = "有备用电源：按'市电失电 × 备电启动失败'串联计算"
    else:
        p_power = p_grid_failure
        cut_sets = [["泵组全失效"], ["上级电源失电（无备用电源）"]]
        backup_note = "无备用电源：市电失电即不可用，这是本台账 DF-PS-01 的现状"

    p_unavailable = 1.0 - (1.0 - p_pumps) * (1.0 - p_power)
    assumptions = [
        "各泵组独立同分布失效，泵组之间无共因失效",
        "失效概率为量级占位值，台账未提供 MTBF/检修记录，接入真实数据前不得当成实测值",
        backup_note,
        "只判'能否提供额定能力'，不推算抽排能力随扬程的衰减（缺泵特性曲线）",
    ]
    return FaultTreeResult(
        station_id=station_id,
        running_units=running_units,
        backup_power=backup_power,
        p_pumps_all_fail=p_pumps,
        p_power_unavailable=p_power,
        p_unavailable=p_unavailable,
        cut_sets=cut_sets,
        assumptions=assumptions,
    )
