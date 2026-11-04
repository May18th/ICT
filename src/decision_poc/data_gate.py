"""数据门禁：行级来源分级 + 高程基准一致性 + 跨字段一致性。

与既有模块的分工（刻意不重复）
------------------------------
- `deduction.verify_rules` 校验**规则**的 `source_ref` 能否定位到真实行、正文是否含关键词；
- `provenance.ProvenanceLedger` 校验**运行时 claim** 是否挂在已注册资产上，并落哈希链；
- `evidence_check` 校验**对外数字**是否漂移。

三者都不回答一个问题：这份数据里，哪一条是实测、哪一条是推导、哪一条是假设。
现状只有 `urban_dataset.py` 一句 docstring「The demo dataset is 15 synthetic assets」——
而 manifest 已有 18 个资产，这句话本身已经漂了；假设值只写在 `underpasses.csv` 的 note 里，
机器读不到。本模块把来源分级变成逐条、可校验、可统计的事实。

三条检查
--------
1. **来源分级**：每条事实必须落一个级别。`real` 必须能定位到真实行且正文含关键值；
   `derived` / `assumption` 必须写明依据。数据里没有逐条标注的，落到默认级别并**如实计数**，
   不允许悄悄当成实测值。
2. **基准一致性**：高程类字段必须声明基准。同一字段混用「场地相对」与「城市绝对」直接判冲突——
   两套基准差 500 m 量级，混用会让管底与水深计算整体错位，而且不会有任何报错。
3. **跨字段一致性**：同一事件里互相矛盾的数字组合直接报错。典型三种：
   工单水深超过「路面高程 − 管底高程」（水面已低于管底，与"雨水口反涌"矛盾）；
   隧道封控阈值不高于入口挡水槛（永远不会封控）；故障泵组数超过泵组总数。

口径约定
--------
- 单位不另立表，一律取 `city_profile.UNITS`（项目唯一的「字段 → 单位」来源）；
- 错误码一律取 `governance.ErrorCode` 闭合枚举，不新增；`DG-*` 是**检查项编号**，
  与规则集里的 `RULE-*` 同类，不是错误码；
- 只读不写：`samples/assets/urban/` 下的 CSV 一个字节都不改。改 CSV 会让 manifest
  指纹与 `evidence_check` 的冻结口径一起漂，那属于另一批变更（见 AGENTS.md「对外数字」）。
- 缺字段就跳过，不拿默认值硬算——假阴性比假阳性更难发现。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Sequence

from pydantic import Field

from .city_profile import UNITS
from .contracts import StrictModel
from .governance import ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET_ROOT = REPO_ROOT / "samples" / "assets" / "urban"
DEFAULT_ANNOTATIONS_PATH = REPO_ROOT / "samples" / "gate" / "data_provenance.json"

#: 需要声明高程基准的字段。只认"绝对标高"意义上的字段；
#: `water_depth_cm` 是相对深度，不应被要求标基准。
ELEVATION_FIELDS = frozenset({"elevation_m", "lowest_point_z_m", "invert_elevation_m"})

#: 场地相对基准的合理量级上界。超出即视为与城市绝对高程（约 500 m）混用。
SITE_RELATIVE_CEILING_M = 100.0

#: 物理/工程意义上的取值上界。命中通常意味着单位或列错位。
VALUE_CEILING = {
    "water_depth_cm": 300.0,
    "rainfall_mm_3h": 300.0,
}

#: 必须为正的字段（面积、流量、长度、埋深）。
POSITIVE_FIELDS = frozenset(
    {
        "capacity_m3h",
        "open_water_area_m2",
        "ramp_length_m",
        "opening_width_m",
        "length_m",
        "depth_m",
    }
)

_DEPTH_IN_DESCRIPTION = re.compile(r"积水(?:深度|深)?\s*约?\s*(\d+(?:\.\d+)?)\s*cm")

#: 引用阈值（真实口径）：《成都市防汛抗旱应急预案（2020 年）》的响应启动雨量。
#: 该文件在项目里标为公开原文转载（`source_org=成都市人民政府办公厅`），与「区级属地合成预案」
#: `plan_flood_emergency_v2.md` 不是一回事——后者自己声明封路阈值 25cm 属合成设定，
#: 因此那条只能记 synthetic，不能混进 real 里充数。
_REAL_THRESHOLD_SOURCE = (
    ("response_blue_6h_mm", 50.0, "mm/6h", "plan_flood_emergency_chengdu_2020.md#line:86", "50毫米",
     "Ⅳ级（蓝色）：主城区 6 小时普降 50 毫米以上"),
    ("response_yellow_12h_mm", 100.0, "mm/12h", "plan_flood_emergency_chengdu_2020.md#line:71", "100毫米",
     "Ⅲ级（黄色）：主城区 12 小时普降 100 毫米以上"),
    ("response_orange_24h_mm", 150.0, "mm/24h", "plan_flood_emergency_chengdu_2020.md#line:56", "150毫米",
     "Ⅱ级（橙色）：主城区 24 小时普降 150 毫米以上"),
    ("response_red_24h_mm", 250.0, "mm/24h", "plan_flood_emergency_chengdu_2020.md#line:41", "250毫米",
     "Ⅰ级（红色）：主城区 24 小时普降 250 毫米以上"),
)

#: 响应级别由低到高。阈值必须随级别严格递增，不允许倒挂。
_RESPONSE_ORDER = (
    "response_blue_6h_mm",
    "response_yellow_12h_mm",
    "response_orange_24h_mm",
    "response_red_24h_mm",
)

#: 区级合成预案的封路阈值：项目自己在市级预案第 107 行声明它不属于公开条款，
#: 所以这里显式记 synthetic，让报告能直接读出"这个数不能对外当公开口径"。
_SYNTHETIC_CLOSURE = (
    "closure_depth_cm",
    25.0,
    "cm",
    "plan_flood_emergency_v2.md#line:29",
    "25cm",
    "区级属地合成预案（YJ-FX-2025-002）的设定值；"
    "plan_flood_emergency_chengdu_2020.md 第 107 行明确声明该阈值「来源于合成设定，不属于本文件的真实条款」，"
    "对外不得当作公开口径引用",
)


class ProvenanceLevel(StrEnum):
    REAL = "real"                    # 原文可定位（预案、公告、台账行）
    DERIVED = "derived"              # 由真实值按明确公式算出
    ASSUMPTION = "assumption"        # 无公开口径，按量级假设
    SYNTHETIC = "synthetic"          # 演示/占位数据


class Datum(StrEnum):
    SITE_RELATIVE = "site_relative"
    CITY_ABSOLUTE = "city_absolute"


class Severity(StrEnum):
    ERROR = "error"
    WARN = "warn"


class DataFact(StrictModel):
    """一条可核对的数据事实。值从数据里读出来，来源从标注文件里给。"""

    fact_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    value: float
    unit: str = ""
    datum: Datum | None = None
    provenance: ProvenanceLevel
    parent_id: str = ""              # 所属上级实体（工单所属道路、管线所属道路）
    dataset_ref: str = ""            # 该数值在数据集里的位置
    source_ref: str = ""             # 外部原文位置；real 必填
    keyword: str = ""                # 原文必须出现的串，用于反查
    basis: str = ""                  # derived / assumption 的依据
    derived_from: list[str] = Field(default_factory=list)


class Finding(StrictModel):
    check_id: str = Field(min_length=1)
    code: ErrorCode
    severity: Severity
    subject: str = Field(min_length=1)
    message: str = Field(min_length=1)


class GateReport(StrictModel):
    dataset: str
    annotations: str = ""
    facts_total: int = 0
    by_provenance: dict[str, int] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [item for item in self.findings if item.severity is Severity.ERROR]

    @property
    def warns(self) -> list[Finding]:
        return [item for item in self.findings if item.severity is Severity.WARN]

    @property
    def ok(self) -> bool:
        return not self.errors


class _Annotation(StrictModel):
    entity_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    level: ProvenanceLevel | None = None
    datum: Datum | None = None
    basis: str = ""
    source_ref: str = ""
    keyword: str = ""


class _AnnotationFile(StrictModel):
    schema_version: str = Field(min_length=1)
    dataset: str = ""
    default_level: ProvenanceLevel = ProvenanceLevel.SYNTHETIC
    default_basis: str = ""
    note: str = ""
    entries: list[_Annotation] = Field(default_factory=list)

    def find(self, entity_id: str, field: str) -> _Annotation | None:
        for entry in self.entries:
            if entry.entity_id == entity_id and entry.field == field:
                return entry
        return None


class _SourceError(ValueError):
    """定位失败，带上对应的检查项编号，便于报告里区分「文件没了」与「行号越界」。"""

    def __init__(self, check_id: str, message: str) -> None:
        super().__init__(message)
        self.check_id = check_id


def _finding(check_id: str, code: ErrorCode, severity: Severity, subject: str, message: str) -> Finding:
    return Finding(check_id=check_id, code=code, severity=severity, subject=subject, message=message)


def _num(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _read_rows(root: Path, file_name: str) -> list[tuple[int, dict[str, str]]]:
    """按行读 CSV，返回 (文件行号, 行字典)。行号含表头，与 `line:N` 定位口径一致。"""
    path = root / file_name
    if not path.is_file():
        raise FileNotFoundError(f"缺少数据集文件：{path}")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return [
        (position + 2, row)
        for position, row in enumerate(csv.DictReader(lines))
        if any((value or "").strip() for value in row.values())
    ]


def _locate(root: Path, source_ref: str) -> str:
    """按 `file#line:N` / `file#line:N-M` / `file#row:N` 取原文。"""
    if "#" not in source_ref:
        raise _SourceError("DG-SRC-002", f"source_ref 必须是 <文件>#<定位>：{source_ref}")
    file_name, locator = source_ref.split("#", 1)
    if "#" in locator:
        locator = locator.rsplit("#", 1)[1]
    path = Path(root) / file_name
    if not path.is_file():
        raise _SourceError("DG-SRC-002", f"来源文件不存在：{file_name}")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    kind, _, span = locator.partition(":")
    if kind == "line":
        start_text, _, end_text = span.partition("-")
        try:
            start, end = int(start_text), int(end_text or start_text)
        except ValueError:
            raise _SourceError("DG-SRC-002", f"行号写法无法解析：{source_ref}") from None
        if start < 1 or end > len(lines):
            raise _SourceError(
                "DG-SRC-003", f"{file_name} 共 {len(lines)} 行，定位要求 {start}-{end}，越界"
            )
        return "\n".join(lines[start - 1 : end])
    if kind == "row":
        try:
            position = int(span)
        except ValueError:
            raise _SourceError("DG-SRC-002", f"行号写法无法解析：{source_ref}") from None
        if position < 1 or position > len(lines):
            raise _SourceError("DG-SRC-003", f"{file_name} 共 {len(lines)} 行，row {position} 越界")
        return lines[position - 1]
    raise _SourceError("DG-SRC-002", f"不支持的定位写法：{locator}")


def load_annotations(path: Path | str | None = None) -> _AnnotationFile:
    target = Path(path) if path is not None else DEFAULT_ANNOTATIONS_PATH
    import json

    return _AnnotationFile.model_validate(json.loads(target.read_text(encoding="utf-8")))


def _make(
    entity_id: str,
    field: str,
    value: float,
    *,
    dataset_ref: str,
    annotation: _Annotation | None,
    defaults: _AnnotationFile,
    parent_id: str = "",
    unit: str | None = None,
) -> tuple[DataFact, bool]:
    """按标注生成事实，返回 (事实, 是否用了默认级别)。"""
    if annotation is not None and annotation.level is not None:
        level = annotation.level
        basis = annotation.basis
        used_default = False
    else:
        level = defaults.default_level
        basis = defaults.default_basis
        used_default = True
    fact = DataFact(
        fact_id=f"{entity_id}/{field}",
        entity_id=entity_id,
        field=field,
        value=value,
        unit=UNITS.get(field, "") if unit is None else unit,
        datum=annotation.datum if annotation else None,
        provenance=level,
        parent_id=parent_id,
        dataset_ref=dataset_ref,
        source_ref=annotation.source_ref if annotation else "",
        keyword=annotation.keyword if annotation else "",
        basis=basis,
    )
    return fact, used_default


def build_facts(
    root: Path | str | None = None,
    annotations: _AnnotationFile | None = None,
) -> tuple[list[DataFact], list[str]]:
    """从数据集读出全部事实；返回 (事实表, 使用默认级别的 fact_id 列表)。"""
    base = Path(root) if root is not None else DEFAULT_DATASET_ROOT
    defaults = annotations or load_annotations()
    facts: list[DataFact] = []
    defaulted: list[str] = []

    def add(fact: DataFact, used_default: bool) -> None:
        facts.append(fact)
        if used_default:
            defaulted.append(fact.fact_id)

    # 道路：高程与长度
    road_elevation: dict[str, DataFact] = {}
    for line, row in _read_rows(base, "road_network.csv"):
        road_id = (row.get("road_id") or "").strip()
        if not road_id:
            continue
        entity = f"road:{road_id}"
        for field in ("elevation_m", "length_m"):
            value = _num(row.get(field))
            if value is None:
                continue
            fact, used = _make(
                entity,
                field,
                value,
                dataset_ref=f"road_network.csv#line:{line}",
                annotation=defaults.find(entity, field),
                defaults=defaults,
            )
            add(fact, used)
            if field == "elevation_m":
                road_elevation[road_id] = fact

    # 排水设施：泵站与雨水口分开成实体
    for line, row in _read_rows(base, "drainage_facilities.csv"):
        facility_id = (row.get("facility_id") or "").strip()
        if not facility_id:
            continue
        kind = (row.get("facility_type") or "").strip()
        entity = f"{'pump' if kind == 'pump_station' else 'intake'}:{facility_id}"
        for field in ("capacity_m3h", "pump_units", "failed_units"):
            value = _num(row.get(field))
            if value is None:
                continue
            fact, used = _make(
                entity,
                field,
                value,
                dataset_ref=f"drainage_facilities.csv#line:{line}",
                annotation=defaults.find(entity, field),
                defaults=defaults,
            )
            add(fact, used)

    # 管线：埋深与管径；埋深用于反算管底高程
    for line, row in _read_rows(base, "pipeline_ledger.csv"):
        pipeline_id = (row.get("pipeline_id") or "").strip()
        if not pipeline_id:
            continue
        entity = f"pipeline:{pipeline_id}"
        road_id = (row.get("road_id") or "").strip()
        parent = f"road:{road_id}" if road_id else ""
        pipe_depth: float | None = None
        for field in ("depth_m", "diameter_mm"):
            value = _num(row.get(field))
            if value is None:
                continue
            fact, used = _make(
                entity,
                field,
                value,
                dataset_ref=f"pipeline_ledger.csv#line:{line}",
                annotation=defaults.find(entity, field),
                defaults=defaults,
                parent_id=parent,
            )
            add(fact, used)
            if field == "depth_m":
                pipe_depth = value
        # 管底高程 = 路面高程 − 埋深：有路才有高程，否则跳过（不猜）
        source = road_elevation.get(road_id)
        if pipe_depth is not None and source is not None and (row.get("kind") or "").strip() == "drainage":
            annotation = defaults.find(entity, "invert_elevation_m")
            facts.append(
                DataFact(
                    fact_id=f"{entity}/invert_elevation_m",
                    entity_id=entity,
                    field="invert_elevation_m",
                    value=round(source.value - pipe_depth, 3),
                    unit=UNITS.get("depth_m", "m"),
                    datum=annotation.datum if annotation and annotation.datum else source.datum,
                    provenance=ProvenanceLevel.DERIVED,
                    parent_id=parent,
                    dataset_ref=f"pipeline_ledger.csv#line:{line}",
                    basis=f"管底高程 = 路面高程 − 埋深 = {source.value:g} − {pipe_depth:g}",
                    derived_from=[source.fact_id, f"{entity}/depth_m"],
                )
            )

    # 工单：水深只在 description 文本里，必须解析，不能取列
    for line, row in _read_rows(base, "work_orders.csv"):
        order_id = (row.get("order_id") or "").strip()
        if not order_id:
            continue
        match = _DEPTH_IN_DESCRIPTION.search(row.get("description") or "")
        if match is None:
            continue
        entity = f"work_order:{order_id}"
        road_id = (row.get("road_id") or "").strip()
        fact, used = _make(
            entity,
            "water_depth_cm",
            float(match.group(1)),
            dataset_ref=f"work_orders.csv#line:{line}",
            annotation=defaults.find(entity, "water_depth_cm"),
            defaults=defaults,
            parent_id=f"road:{road_id}" if road_id else "",
            unit="cm",
        )
        add(fact, used)

    # 下穿隧道
    for line, row in _read_rows(base, "underpasses.csv"):
        underpass_id = (row.get("underpass_id") or "").strip()
        if not underpass_id:
            continue
        entity = f"underpass:{underpass_id}"
        road_id = (row.get("road_id") or "").strip()
        for field in ("entry_sill_height_m", "lowest_point_z_m", "close_threshold_m", "open_water_area_m2"):
            value = _num(row.get(field))
            if value is None:
                continue
            fact, used = _make(
                entity,
                field,
                value,
                dataset_ref=f"underpasses.csv#line:{line}",
                annotation=defaults.find(entity, field),
                defaults=defaults,
                parent_id=f"road:{road_id}" if road_id else "",
            )
            add(fact, used)

    # 气象预警
    for line, row in _read_rows(base, "weather_alerts.csv"):
        alert_id = (row.get("alert_id") or "").strip()
        if not alert_id:
            continue
        value = _num(row.get("rainfall_mm_3h"))
        if value is None:
            continue
        entity = f"alert:{alert_id}"
        fact, used = _make(
            entity,
            "rainfall_mm_3h",
            value,
            dataset_ref=f"weather_alerts.csv#line:{line}",
            annotation=defaults.find(entity, "rainfall_mm_3h"),
            defaults=defaults,
        )
        add(fact, used)

    facts.extend(_threshold_facts())
    return facts, defaulted


def _threshold_facts() -> list[DataFact]:
    """引用阈值：真实口径 4 条带原文定位，合成口径 1 条显式标 synthetic。"""
    facts = [
        DataFact(
            fact_id=f"threshold:chengdu-2020/{field}",
            entity_id="threshold:chengdu-2020",
            field=field,
            value=value,
            unit=unit,
            provenance=ProvenanceLevel.REAL,
            source_ref=source_ref,
            keyword=keyword,
            basis=label,
        )
        for field, value, unit, source_ref, keyword, label in _REAL_THRESHOLD_SOURCE
    ]
    field, value, unit, source_ref, keyword, basis = _SYNTHETIC_CLOSURE
    facts.append(
        DataFact(
            fact_id=f"threshold:district-synthetic/{field}",
            entity_id="threshold:district-synthetic",
            field=field,
            value=value,
            unit=unit,
            provenance=ProvenanceLevel.SYNTHETIC,
            source_ref=source_ref,
            keyword=keyword,
            basis=basis,
        )
    )
    return facts


def check_provenance(facts: Sequence[DataFact], root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for fact in facts:
        if fact.provenance is ProvenanceLevel.REAL:
            if not fact.source_ref:
                findings.append(
                    _finding(
                        "DG-SRC-001",
                        ErrorCode.EVIDENCE_INSUFFICIENT,
                        Severity.ERROR,
                        fact.fact_id,
                        "标为 real 却没有 source_ref：实测值必须能指回原文",
                    )
                )
                continue
            try:
                text = _locate(root, fact.source_ref)
            except _SourceError as exc:
                findings.append(
                    _finding(exc.check_id, ErrorCode.EVIDENCE_INSUFFICIENT, Severity.ERROR, fact.fact_id, str(exc))
                )
                continue
            if fact.keyword and fact.keyword not in text:
                findings.append(
                    _finding(
                        "DG-SRC-004",
                        ErrorCode.EVIDENCE_INSUFFICIENT,
                        Severity.ERROR,
                        fact.fact_id,
                        f"来源行不含 {fact.keyword!r}：{fact.source_ref}",
                    )
                )
        elif fact.provenance is ProvenanceLevel.DERIVED:
            if not fact.basis.strip() or not fact.derived_from:
                findings.append(
                    _finding(
                        "DG-SRC-005",
                        ErrorCode.VALIDATION_FAILED,
                        Severity.WARN,
                        fact.fact_id,
                        "标为 derived 但没写公式或上游事实",
                    )
                )
        elif not fact.basis.strip():
            findings.append(
                _finding(
                    "DG-SRC-005",
                    ErrorCode.VALIDATION_FAILED,
                    Severity.WARN,
                    fact.fact_id,
                    f"标为 {fact.provenance.value} 但没写依据",
                )
            )
    return findings


def check_unannotated(defaulted: Sequence[str], level: ProvenanceLevel) -> list[Finding]:
    if not defaulted:
        return []
    sample = "、".join(defaulted[:5])
    more = f" 等 {len(defaulted)} 条" if len(defaulted) > 5 else ""
    return [
        _finding(
            "DG-SRC-006",
            ErrorCode.VALIDATION_FAILED,
            Severity.WARN,
            f"{len(defaulted)} 条",
            f"未逐条标注来源，按数据集自述记为 {level.value}：{sample}{more}；"
            "对外表述不得把这一批说成实测值",
        )
    ]


def check_datum(facts: Sequence[DataFact]) -> list[Finding]:
    findings: list[Finding] = []
    grouped: dict[str, list[DataFact]] = {}
    for fact in facts:
        if fact.field in ELEVATION_FIELDS:
            grouped.setdefault(fact.field, []).append(fact)
    for field, items in sorted(grouped.items()):
        declared = {item.datum for item in items if item.datum is not None}
        if len(declared) > 1:
            findings.append(
                _finding(
                    "DG-DAT-001",
                    ErrorCode.EVIDENCE_CONFLICT,
                    Severity.ERROR,
                    field,
                    f"{field} 在同一份数据里混用两套基准："
                    f"{'、'.join(sorted(item.value for item in declared))}；"
                    "两套相差约 500 m，管底与水深计算会整体错位而不会报错",
                )
            )
        for item in items:
            if item.datum is None:
                findings.append(
                    _finding(
                        "DG-DAT-002",
                        ErrorCode.VALIDATION_FAILED,
                        Severity.WARN,
                        item.fact_id,
                        f"{field} 未标注高程基准（场地相对 / 城市绝对）",
                    )
                )
            elif item.datum is Datum.SITE_RELATIVE and abs(item.value) >= SITE_RELATIVE_CEILING_M:
                findings.append(
                    _finding(
                        "DG-DAT-003",
                        ErrorCode.EVIDENCE_CONFLICT,
                        Severity.ERROR,
                        item.fact_id,
                        f"标注为场地相对基准但取值 {item.value:g} m，落在城市绝对高程量级（约 500 m）："
                        "基准标错，或数值把 m/cm 填反了",
                    )
                )
    return findings


def check_magnitude(facts: Sequence[DataFact]) -> list[Finding]:
    findings: list[Finding] = []
    for fact in facts:
        ceiling = VALUE_CEILING.get(fact.field)
        if ceiling is not None and fact.value > ceiling:
            findings.append(
                _finding(
                    "DG-MAG-001",
                    ErrorCode.VALIDATION_FAILED,
                    Severity.ERROR,
                    fact.fact_id,
                    f"{fact.field} = {fact.value:g}{fact.unit}，超过工程上界 {ceiling:g}{fact.unit}："
                    "多半是单位或列错位",
                )
            )
        if fact.field in POSITIVE_FIELDS and fact.value <= 0:
            findings.append(
                _finding(
                    "DG-MAG-002",
                    ErrorCode.VALIDATION_FAILED,
                    Severity.ERROR,
                    fact.fact_id,
                    f"{fact.field} = {fact.value:g}，该字段物理上必须为正",
                )
            )
    return findings


def _at(facts: Iterable[DataFact], entity_id: str, field: str) -> DataFact | None:
    for fact in facts:
        if fact.entity_id == entity_id and fact.field == field:
            return fact
    return None


def check_cross_field(facts: Sequence[DataFact]) -> list[Finding]:
    findings: list[Finding] = []

    # 工单水深 vs 路面−管底：水面低于管底时，管道不可能反涌
    for fact in facts:
        if fact.field != "water_depth_cm" or not fact.parent_id:
            continue
        road_elevation = _at(facts, fact.parent_id, "elevation_m")
        pipes = [item for item in facts if item.field == "depth_m" and item.parent_id == fact.parent_id]
        if road_elevation is None or not pipes:
            continue  # 缺字段跳过，不猜
        deepest = max(pipes, key=lambda item: item.value)
        invert = road_elevation.value - deepest.value
        limit_cm = deepest.value * 100.0
        if fact.value > limit_cm:
            findings.append(
                _finding(
                    "DG-XF-001",
                    ErrorCode.EVIDENCE_CONFLICT,
                    Severity.ERROR,
                    fact.fact_id,
                    f"工单水深 {fact.value:g}cm 超过 {fact.parent_id.split(':', 1)[1]} 的路面−管底高差 "
                    f"{limit_cm:g}cm（路面 {road_elevation.value:g} m − 管底 {invert:.2f} m，"
                    f"管 {deepest.entity_id.split(':', 1)[1]}）：水面已低于管底，与雨水口反涌的记载矛盾，"
                    "多半是 cm/m 混填或高程取错",
                )
            )

    # 隧道封控阈值必须高于入口挡水槛，否则永远不会封控
    for fact in facts:
        if fact.field != "close_threshold_m":
            continue
        sill = _at(facts, fact.entity_id, "entry_sill_height_m")
        if sill is None:
            continue
        if fact.value <= sill.value:
            findings.append(
                _finding(
                    "DG-XF-002",
                    ErrorCode.EVIDENCE_CONFLICT,
                    Severity.ERROR,
                    fact.fact_id,
                    f"封控阈值 {fact.value:g} m 不高于入口挡水槛 {sill.value:g} m："
                    "水先漫过挡水槛才会触发封控，按此阈值永远不会封控",
                )
            )

    # 故障泵组数不得超过泵组总数
    for fact in facts:
        if fact.field != "failed_units":
            continue
        total = _at(facts, fact.entity_id, "pump_units")
        if total is None:
            continue
        if fact.value > total.value:
            findings.append(
                _finding(
                    "DG-XF-003",
                    ErrorCode.EVIDENCE_CONFLICT,
                    Severity.ERROR,
                    fact.fact_id,
                    f"故障泵组 {fact.value:g} 台超过泵组总数 {total.value:g} 台："
                    "可用能力会被算成负数",
                )
            )

    # 四级响应阈值必须随级别严格递增，否则更高级别反而更容易触发
    response = {item.field: item for item in facts if item.entity_id == "threshold:chengdu-2020"}
    chain = [response[name] for name in _RESPONSE_ORDER if name in response]
    for lower, higher in zip(chain, chain[1:]):
        if higher.value <= lower.value:
            findings.append(
                _finding(
                    "DG-XF-004",
                    ErrorCode.EVIDENCE_CONFLICT,
                    Severity.ERROR,
                    higher.fact_id,
                    f"{higher.field} = {higher.value:g}{higher.unit} 不高于低一级的 "
                    f"{lower.field} = {lower.value:g}{lower.unit}："
                    "更高级别的响应反而更容易触发，阈值倒挂",
                )
            )

    return findings


def run_gate(
    root: Path | str | None = None,
    annotations_path: Path | str | None = None,
) -> GateReport:
    base = Path(root) if root is not None else DEFAULT_DATASET_ROOT
    defaults = load_annotations(annotations_path)
    facts, defaulted = build_facts(base, defaults)

    findings: list[Finding] = []
    findings.extend(check_provenance(facts, base))
    findings.extend(check_unannotated(defaulted, defaults.default_level))
    findings.extend(check_datum(facts))
    findings.extend(check_magnitude(facts))
    findings.extend(check_cross_field(facts))
    findings.sort(key=lambda item: (0 if item.severity is Severity.ERROR else 1, item.check_id, item.subject))

    counts: dict[str, int] = {}
    for fact in facts:
        counts[fact.provenance.value] = counts.get(fact.provenance.value, 0) + 1
    return GateReport(
        dataset=str(base),
        annotations=str(annotations_path or DEFAULT_ANNOTATIONS_PATH),
        facts_total=len(facts),
        by_provenance=counts,
        findings=findings,
    )


_LEVEL_MEANING = {
    "real": "可定位到预案/台账原文",
    "derived": "由真实值按明确公式算出",
    "assumption": "无公开口径，按量级假设，待核实",
    "synthetic": "演示数据，不得当作实测",
}


def render_markdown(report: GateReport) -> str:
    lines: list[str] = [
        "# 数据门禁报告",
        "",
        f"- 数据集：`{report.dataset}`",
        f"- 来源标注：`{report.annotations}`",
        f"- 事实总数：**{report.facts_total}**",
        "",
        "## 一、来源分级",
        "",
        "| 级别 | 条数 | 含义 |",
        "| --- | --- | --- |",
    ]
    for level in ("real", "derived", "assumption", "synthetic"):
        lines.append(f"| {level} | {report.by_provenance.get(level, 0)} | {_LEVEL_MEANING[level]} |")

    pending = [item for item in report.findings if item.check_id.startswith("DG-SRC")]
    lines += ["", "## 二、检查结果", ""]
    if not report.findings:
        lines.append("全部检查通过，无待确认项。")
    else:
        lines.append("| 检查项 | 级别 | 对象 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for item in report.findings:
            message = item.message.replace("|", "\\|")
            lines.append(f"| {item.check_id} | {item.severity.value} | `{item.subject}` | {message} |")

    lines += [
        "",
        "## 三、声明边界",
        "",
        f"- 本次结论建立在 **{report.facts_total}** 条事实上，其中 "
        f"real {report.by_provenance.get('real', 0)} 条、derived {report.by_provenance.get('derived', 0)} 条、"
        f"assumption {report.by_provenance.get('assumption', 0)} 条、"
        f"synthetic {report.by_provenance.get('synthetic', 0)} 条。",
        "- assumption 与 synthetic 两类**不得对外表述为实测值**；"
        "阈值与假设的口径需甲方书面确认后才能在对外材料中引用。",
    ]
    if pending:
        lines.append(
            f"- 来源层面有 **{len(pending)}** 项待确认（见上表）。"
            "确认前，相关数值只能在内部联调材料里出现，不得写进对外材料当作实测。"
        )
    lines += ["", f"结论：**{'通过' if report.ok else '不通过'}**（{len(report.errors)} 项 error / {len(report.warns)} 项 warn）", ""]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m decision_poc.data_gate", description="数据门禁：来源分级 + 基准 + 跨字段")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET_ROOT), help="数据集目录")
    parser.add_argument("--annotations", default=str(DEFAULT_ANNOTATIONS_PATH), help="来源标注文件")
    parser.add_argument("--out", default="", help="把 Markdown 报告写到该路径")
    args = parser.parse_args(argv)

    report = run_gate(args.dataset, args.annotations)
    text = render_markdown(report)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[data_gate] 报告已写入 {args.out}")
    else:
        print(text)
    print(f"[data_gate] {'PASS' if report.ok else 'FAIL'} facts={report.facts_total} "
          f"errors={len(report.errors)} warns={len(report.warns)}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DataFact",
    "Datum",
    "Finding",
    "GateReport",
    "ProvenanceLevel",
    "Severity",
    "build_facts",
    "check_cross_field",
    "check_datum",
    "check_magnitude",
    "check_provenance",
    "load_annotations",
    "main",
    "render_markdown",
    "run_gate",
]
