"""定量层：把"台账里的数"换成"有公式、有出处、有假设、有误差界"的数。

本包只做确定性计算：不调模型、不读环境变量、不写副作用文件。上层（推演规则、
MCP 工具、Agent）只能调用这里的结果，不能自己推导公式——这是 P12 规格 §4
「断言权在内核」在数值层面的落点。

三条硬规则：

1. 每个结果必须带 `formula_id` 与 `formula_source`，来源要能被人工回查；
2. `assumptions` 不能为空：没有假设的公式等于没写清适用条件；
3. 算不出来时返回 `computable=False` 且 `value=0`，并给原因——不靠推测补全。
"""

from decision_poc.quant.contracts import (
    HydrographResult,
    QuantResult,
    SpreadResult,
    uncomputable,
    uncomputable_hydrograph,
)
from decision_poc.quant.hydraulics import (
    manning_full_flow,
    manning_velocity,
    orifice_inflow,
    ponding_forecast,
    weir_inflow,
)
from decision_poc.quant.hydrodynamics import (
    MuskingumCoefficients,
    level_exceedance_duration,
    level_pool_route,
    muskingum_coefficients,
    muskingum_route,
    time_to_level,
)
from decision_poc.quant.hydrology import (
    MM_PER_H_TO_LPS_HA,
    RAINFALL_SCENARIOS_MM_2H,
    intensity_from_depth,
    rational_runoff,
    runoff_from_rainfall,
)
from decision_poc.quant.uncertainty import monte_carlo, range_from_bounds
from decision_poc.quant.optimize import (
    OptimalAllocation,
    OptimalityGap,
    TransferPlan,
    compare_with_policy,
    optimal_allocation,
)
from decision_poc.quant.graphopt import ClosurePlan, minimum_closure_set
from decision_poc.quant.reliability import FaultTreeResult, pump_station_unavailable

__all__ = [
    "MM_PER_H_TO_LPS_HA",
    "RAINFALL_SCENARIOS_MM_2H",
    "ClosurePlan",
    "FaultTreeResult",
    "HydrographResult",
    "MuskingumCoefficients",
    "OptimalAllocation",
    "OptimalityGap",
    "QuantResult",
    "SpreadResult",
    "TransferPlan",
    "compare_with_policy",
    "intensity_from_depth",
    "level_exceedance_duration",
    "level_pool_route",
    "manning_full_flow",
    "manning_velocity",
    "minimum_closure_set",
    "monte_carlo",
    "muskingum_coefficients",
    "muskingum_route",
    "optimal_allocation",
    "orifice_inflow",
    "ponding_forecast",
    "pump_station_unavailable",
    "range_from_bounds",
    "rational_runoff",
    "runoff_from_rainfall",
    "time_to_level",
    "uncomputable",
    "uncomputable_hydrograph",
    "weir_inflow",
]
