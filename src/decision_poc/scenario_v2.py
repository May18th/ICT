"""下穿隧道倒灌与泵站负荷的 v2 口径实现（尚未接入六跳）。

本模块是 P12 场景 v2 的**可测内核**，刻意与现有数据集分离：

- 新对象与新台账放在 `samples/scenarios/v2/`，不混进 `samples/assets/urban/`，
  因为一改资产目录的 CSV，资产指纹与"15 个资产/未识别列 0"这些冻结口径就会变，
  必须与对外数字重算（T3）同一批改，不能分两次漂移；
- 函数全部是纯计算，不读环境、不写文件，接入六跳时只做编排，不再改口径。

两个口径的公式与出处见 `docs/superpowers/specs/2026-11-02-p12-...-design.md` §5/§6。
"""

from __future__ import annotations

import csv
from pathlib import Path

from pydantic import Field, model_validator

from decision_poc.contracts import StrictModel
from decision_poc.quant import QuantResult, weir_inflow
from decision_poc.urban_dataset import DEFAULT_DATASET_ROOT

V2_SCENARIO_DIR = DEFAULT_DATASET_ROOT.parent.parent / "scenarios" / "v2"

#: 倒灌判定门槛（P12 §5.2）：外部积水达到挡水槛即高，达到 60% 为中。
BACKFLOW_MEDIUM_RATIO = 0.6

#: 泵站负荷分档（P12 §6.2）。
LOAD_NORMAL_MAX = 0.85
LOAD_HIGH_MAX = 1.0

#: 通道抽排的量级假设：目标排空时间与楔形积水体积系数。
UNDERPASS_DRAIN_HOURS = 10.0
UNDERPASS_PROFILE_FACTOR = 0.5

SOURCE_BACKFLOW = "P12 设计规格 §5.2：入口挡水槛倒灌判定"
SOURCE_LOAD = "P12 设计规格 §6.2：可用能力 = 额定 − 故障泵组能力"


class UnderpassSpec(StrictModel):
    """一个下穿隧道/地下通道对象。几何参数用于判定，不做水动力计算。"""

    underpass_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    road_id: str = Field(min_length=1)
    district: str = ""
    entry_sill_height_m: float = Field(ge=0.0)
    lowest_point_z_m: float
    ramp_length_m: float = Field(gt=0.0)
    opening_width_m: float = Field(gt=0.0)
    open_water_area_m2: float = Field(gt=0.0)
    has_pump_room: bool = False
    close_threshold_m: float = Field(gt=0.0)
    owner_org: str = ""
    note: str = ""

    @model_validator(mode="after")
    def _geometry_is_consistent(self) -> UnderpassSpec:
        # 最低点必须低于入口地面；否则"倒灌"这个物理量不存在
        if self.lowest_point_z_m >= 0.0:
            raise ValueError("最低点标高必须低于入口地面（< 0）")
        if self.close_threshold_m <= self.entry_sill_height_m:
            raise ValueError("封控水深阈值应高于入口挡水槛，否则永远不会封控")
        return self


class BackflowAssessment(StrictModel):
    level: str  # none / medium / high
    sill_height_m: float
    external_depth_m: float
    overtopping_head_m: float
    inflow: QuantResult | None = None
    formula_source: str = SOURCE_BACKFLOW

    @model_validator(mode="after")
    def _level_is_known(self) -> BackflowAssessment:
        if self.level not in {"none", "medium", "high"}:
            raise ValueError(f"unknown backflow level: {self.level}")
        if self.level == "high" and self.inflow is None:
            raise ValueError("high 倒灌必须给出进水量，不能只说'有风险'")
        if self.level != "high" and self.inflow is not None:
            raise ValueError("未漫过挡水槛时不应给出倒灌流量")
        return self


class PumpLoad(StrictModel):
    """泵站负荷事实：可用能力、负荷率与分档。"""

    station_id: str = Field(min_length=1)
    rated_m3h: float = Field(gt=0.0)
    failed_unit_m3h: float = Field(ge=0.0)
    design_inflow_m3h: float = Field(ge=0.0)
    capacity_avail_m3h: float
    load_ratio: float
    state: str
    formula_source: str = SOURCE_LOAD

    @model_validator(mode="after")
    def _load_is_consistent(self) -> PumpLoad:
        if self.failed_unit_m3h > self.rated_m3h:
            raise ValueError("故障泵组能力不能超过额定能力")
        if self.capacity_avail_m3h != self.rated_m3h - self.failed_unit_m3h:
            raise ValueError("capacity_avail 必须等于 额定 − 故障泵组能力")
        if self.capacity_avail_m3h <= 0:
            raise ValueError("可用能力不为正时不能用负荷率表达，应直接判停机")
        if self.state not in {"normal", "high_load", "overload"}:
            raise ValueError(f"unknown load state: {self.state}")
        return self


class PumpRequirementComparison(StrictModel):
    """v1（固定 0.8 折算）与 v2（负荷缺口）两种口径的移动泵需求对比。"""

    station_id: str = Field(min_length=1)
    unit_capacity_m3h: float = Field(gt=0.0)
    v1_pumps: int = Field(ge=0)
    v2_pumps: int = Field(ge=0)
    delta: int
    note: str = ""

    @model_validator(mode="after")
    def _delta_is_consistent(self) -> PumpRequirementComparison:
        if self.delta != self.v2_pumps - self.v1_pumps:
            raise ValueError("delta 必须等于 v2 − v1")
        return self


class UnderpassPumpDemand(StrictModel):
    """下穿通道抽排需求：按"积水体积 ÷ 目标排空时间"反算需要几台移动泵。"""

    underpass_id: str = Field(min_length=1)
    drain_volume_m3: float = Field(ge=0.0)
    drain_hours: float = Field(gt=0.0)
    demand_flow_m3h: float
    unit_capacity_m3h: float = Field(gt=0.0)
    pumps: int = Field(ge=0)
    basis: list[str] = Field(min_length=1)
    assumption_note: str = Field(min_length=1)

    @model_validator(mode="after")
    def _pumps_follow_from_demand(self) -> UnderpassPumpDemand:
        expected = _ceil_div(self.demand_flow_m3h, self.unit_capacity_m3h)
        if self.pumps != expected:
            raise ValueError(f"pumps 必须等于 ceil(需求流量 / 单台能力) = {expected}")
        return self


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes", "y"}


def load_underpasses(root: Path | str | None = None) -> dict[str, UnderpassSpec]:
    """读 v2 台账；同一 id 重复出现直接报错，不静默覆盖。"""

    base = Path(root) if root is not None else V2_SCENARIO_DIR
    path = base / "underpasses.csv"
    specs: dict[str, UnderpassSpec] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if not row.get("underpass_id"):
                continue
            spec = UnderpassSpec(
                underpass_id=row["underpass_id"],
                name=row["name"],
                road_id=row["road_id"],
                district=row.get("district", ""),
                entry_sill_height_m=float(row["entry_sill_height_m"]),
                lowest_point_z_m=float(row["lowest_point_z_m"]),
                ramp_length_m=float(row["ramp_length_m"]),
                opening_width_m=float(row["opening_width_m"]),
                open_water_area_m2=float(row["open_water_area_m2"]),
                has_pump_room=_as_bool(row.get("has_pump_room", "false")),
                close_threshold_m=float(row["close_threshold_m"]),
                owner_org=row.get("owner_org", ""),
                note=row.get("note", ""),
            )
            if spec.underpass_id in specs:
                raise ValueError(f"重复的下穿隧道 id: {spec.underpass_id}")
            specs[spec.underpass_id] = spec
    return specs


def load_pump_rows(root: Path | str | None = None) -> dict[str, dict[str, float | str]]:
    """读 v2 泵站台账（额定 / 故障泵组能力 / 设计来水）。"""

    base = Path(root) if root is not None else V2_SCENARIO_DIR
    path = base / "pump_load_v2.csv"
    rows: dict[str, dict[str, float | str]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if not row.get("station_id"):
                continue
            rows[row["station_id"]] = {
                "rated_m3h": float(row["rated_m3h"]),
                "failed_unit_m3h": float(row["failed_unit_m3h"]),
                "design_inflow_m3h": float(row["design_inflow_m3h"]),
                "note": row.get("note", ""),
            }
    return rows


def backflow_state(
    *,
    external_depth_m: float,
    sill_height_m: float,
    opening_width_m: float,
) -> BackflowAssessment:
    """外部积水对下穿通道的倒灌判定；漫槛时才给流量，用堰流公式算。"""

    if external_depth_m < 0 or sill_height_m < 0:
        raise ValueError("水深与挡水槛高度不能为负")
    if opening_width_m <= 0:
        raise ValueError("洞口宽必须为正")
    medium_line = sill_height_m * BACKFLOW_MEDIUM_RATIO
    if external_depth_m >= sill_height_m:
        head = external_depth_m - sill_height_m
        return BackflowAssessment(
            level="high",
            sill_height_m=sill_height_m,
            external_depth_m=external_depth_m,
            overtopping_head_m=head,
            inflow=weir_inflow(width_m=opening_width_m, head_m=head),
        )
    if external_depth_m >= medium_line:
        return BackflowAssessment(
            level="medium",
            sill_height_m=sill_height_m,
            external_depth_m=external_depth_m,
            overtopping_head_m=0.0,
        )
    return BackflowAssessment(
        level="none",
        sill_height_m=sill_height_m,
        external_depth_m=external_depth_m,
        overtopping_head_m=0.0,
    )


def underpass_closure_required(*, water_depth_m: float, close_threshold_m: float) -> bool:
    """通道内积水达到封控阈值即需封闭；阈值未达不封，避免过度管制。"""

    if close_threshold_m <= 0:
        raise ValueError("封控阈值必须为正")
    return water_depth_m >= close_threshold_m


def pump_load(*, station_id: str, rated_m3h: float, failed_unit_m3h: float, design_inflow_m3h: float) -> PumpLoad:
    """泵站负荷：可用能力扣除故障泵组，负荷率 = 设计来水 / 可用能力。"""

    capacity = rated_m3h - failed_unit_m3h
    ratio = design_inflow_m3h / capacity if capacity > 0 else float("inf")
    if ratio <= LOAD_NORMAL_MAX:
        state = "normal"
    elif ratio <= LOAD_HIGH_MAX:
        state = "high_load"
    else:
        state = "overload"
    return PumpLoad(
        station_id=station_id,
        rated_m3h=rated_m3h,
        failed_unit_m3h=failed_unit_m3h,
        design_inflow_m3h=design_inflow_m3h,
        capacity_avail_m3h=capacity,
        load_ratio=ratio,
        state=state,
    )


def compare_pump_requirement(
    *,
    station_id: str,
    rated_m3h: float,
    failed_unit_m3h: float,
    design_inflow_m3h: float,
    derate: float,
    unit_capacity_m3h: float,
) -> PumpRequirementComparison:
    """v1 与 v2 两种口径的移动泵台数对比——用来事先看清"换口径会动多少数字"。

    v1：故障泵站按 `ceil(额定 × 折算系数 / 单台能力)`，把打折后的能力整体当需求；
    正常泵站 v1 需求为 0（与引擎一致：只有 `under_maintenance` 才补位）；
    v2：`ceil(max(0, 设计来水 − 可用能力) / 单台能力)`，只补真实缺口。
    """

    if derate <= 0:
        raise ValueError("折算系数必须为正")
    v1 = 0 if failed_unit_m3h <= 0 else _ceil_div(rated_m3h * derate, unit_capacity_m3h)
    capacity = rated_m3h - failed_unit_m3h
    v2 = _ceil_div(max(0.0, design_inflow_m3h - capacity), unit_capacity_m3h)
    note = (
        "v2 只补'来水超过可用能力'的部分，因此必然不大于 v1；"
        "换成 v2 会让故障泵站的移动泵需求下降，需同步重算缺口与分配案例"
    )
    return PumpRequirementComparison(
        station_id=station_id,
        unit_capacity_m3h=unit_capacity_m3h,
        v1_pumps=v1,
        v2_pumps=v2,
        delta=v2 - v1,
        note=note,
    )


def _ceil_div(value: float, divisor: float) -> int:
    if value <= 0:
        return 0
    import math

    return int(math.ceil(value / divisor - 1e-12))


def underpass_pump_demand(
    spec: UnderpassSpec,
    *,
    unit_capacity_m3h: float = 400.0,
    drain_hours: float = UNDERPASS_DRAIN_HOURS,
) -> UnderpassPumpDemand:
    """通道抽排需求：先估积水体积，再按目标排空时间反算台数。

    体积口径：`敞口汇水面积 × 平均水深`，平均水深取"最低点到入口地面高差"的
    `UNDERPASS_PROFILE_FACTOR`（楔形积水近似半深）。两个系数都是**量级假设**，
    台账没有通道汇水过程线与泵房参数，因此全部写进 `assumption_note`，
    接入六跳后按 `assumption` 规则对待，不得当成实测值。
    """

    if drain_hours <= 0:
        raise ValueError("目标排空时间必须为正")
    if spec.has_pump_room:
        return UnderpassPumpDemand(
            underpass_id=spec.underpass_id,
            drain_volume_m3=0.0,
            drain_hours=drain_hours,
            demand_flow_m3h=0.0,
            unit_capacity_m3h=unit_capacity_m3h,
            pumps=0,
            basis=[f"{spec.name} 自带泵房，不占移动泵"],
            assumption_note="自带泵房时按不占用移动泵处理；泵房能力未在台账给出，需现场核实",
        )
    depth_m = abs(spec.lowest_point_z_m) + spec.entry_sill_height_m
    mean_depth_m = depth_m * UNDERPASS_PROFILE_FACTOR
    volume_m3 = spec.open_water_area_m2 * mean_depth_m
    flow_m3h = volume_m3 / drain_hours
    pumps = _ceil_div(flow_m3h, unit_capacity_m3h)
    basis = [
        f"{spec.name} 敞口 {spec.open_water_area_m2:g} m² × 平均水深 {mean_depth_m:.2f} m "
        f"≈ 积水 {volume_m3:.0f} m³",
        f"按 {drain_hours:g} h 排空 → 需 {flow_m3h:.0f} m³/h，÷ 单台 {unit_capacity_m3h:g} m³/h "
        f"向上取整 = {pumps} 台",
    ]
    return UnderpassPumpDemand(
        underpass_id=spec.underpass_id,
        drain_volume_m3=volume_m3,
        drain_hours=drain_hours,
        demand_flow_m3h=flow_m3h,
        unit_capacity_m3h=unit_capacity_m3h,
        pumps=pumps,
        basis=basis,
        assumption_note=(
            "平均水深取'最低点到入口地面高差 × 0.5'（楔形积水近似），排空时间 10 h 为量级假设；"
            "台账无通道汇水过程线与泵房参数，接入推演时按假设规则显式标注"
        ),
    )
