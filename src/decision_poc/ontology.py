"""Semi-automatic ontology bootstrap for the equipment-maintenance domain.

The contest brief asks for an *ontology半自动化构建方案* that works in a
low-resource setting, keeps updating, and aligns with external standards. This
module implements that as a three-stage pipeline with no network dependency:

1. **Seed** — a ten-type schema with a few lexical seeds (`SEED_ONTOLOGY`).
   Everything else is induced from data, so a new domain only needs this dict.
2. **Induce** — deterministic extractors read the registered data assets
   (table headers, procedure text, declared standard references) and emit
   candidate entities / relations, each carrying an `Evidence` locator that
   points at the exact file and row/line it came from.
3. **Align** — candidates are merged by (type, normalised name), checked
   against relation domain/range, scored by an explainable confidence model,
   and split into `auto_accept` / `review` / `drop`. `plan_merge()` then diffs
   the result against an existing KG snapshot so the delta can be replayed.

Cross-document hop: threshold rules parsed from the maintenance procedure are
matched against numeric observations parsed from the inspection log, producing
an `Equipment -exhibits-> FaultMode` relation whose evidence list contains
locators from *two different assets*. That is the multi-hop trace the brief
asks for, produced without an LLM.
"""

import csv
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator

from .contracts import StrictModel
from .data_asset import ENCODINGS, AssetModality, DataAsset

SCHEMA_VERSION = "1"
SEED_VERSION = "2026-10-29"

# --- confidence model -------------------------------------------------------
# Each extractor declares a base confidence; the value is explainable on stage:
# a typed table column is trusted more than a prose pattern, and an inferred
# fault mode is trusted less than a value read straight from a cell.
CONF_DECLARED_REF = 0.95  # standard reference declared on the asset itself
CONF_TABLE_TYPED = 0.92  # entity read from an identified table column
CONF_TABLE_INFERRED = 0.86  # relation inferred from two table columns
CONF_RULE_TABLE = 0.90  # threshold rule parsed from a markdown table
CONF_PROSE_PATTERN = 0.84  # value captured by a regex over prose
CONF_STEP_PATTERN = 0.88  # numbered procedure step
CONF_CROSS_DOC = 0.80  # fault mode inferred across two documents

AUTO_ACCEPT_AT = 0.80
REVIEW_FLOOR_AT = 0.55


class ReviewDecision(StrEnum):
    AUTO_ACCEPT = "auto_accept"
    REVIEW = "review"
    DROP = "drop"


class Evidence(StrictModel):
    """A citable pointer: which asset, which position, which quote."""

    asset_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    quote: str = Field(min_length=1)

    @field_validator("asset_id", "locator", "quote")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text


class EntityCandidate(StrictModel):
    candidate_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(min_length=1)
    decision: ReviewDecision
    extractor: str = Field(min_length=1)


class RelationCandidate(StrictModel):
    candidate_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(min_length=1)
    decision: ReviewDecision
    extractor: str = Field(min_length=1)


class OntologyType(StrictModel):
    name: str = Field(min_length=1)
    parent: str | None = None
    description: str = Field(min_length=1)
    seeds: list[str] = Field(default_factory=list)


class RelationSchema(StrictModel):
    name: str = Field(min_length=1)
    domain: list[str] = Field(min_length=1)
    range: list[str] = Field(min_length=1)
    description: str = Field(min_length=1)


class OntologySchema(StrictModel):
    version: str = Field(min_length=1)
    types: list[OntologyType] = Field(min_length=1)
    relations: list[RelationSchema] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_duplicate_names(self) -> "OntologySchema":
        type_names = [item.name for item in self.types]
        if len(set(type_names)) != len(type_names):
            raise ValueError("duplicate type name in schema")
        relation_names = [item.name for item in self.relations]
        if len(set(relation_names)) != len(relation_names):
            raise ValueError("duplicate relation name in schema")
        known = set(type_names)
        for relation in self.relations:
            unknown = (set(relation.domain) | set(relation.range)) - known
            if unknown:
                raise ValueError(
                    f"relation {relation.name} references unknown types: {sorted(unknown)}"
                )
        return self

    def type_names(self) -> set[str]:
        return {item.name for item in self.types}

    def relation(self, name: str) -> RelationSchema | None:
        return next((item for item in self.relations if item.name == name), None)


SEED_ONTOLOGY = OntologySchema(
    version=SEED_VERSION,
    types=[
        OntologyType(
            name="Equipment",
            parent="Asset",
            description="可独立检修、有台账编号的物理设备",
            seeds=["减速机", "油泵", "风机", "空压机", "电机", "水泵"],
        ),
        OntologyType(
            name="EquipmentClass",
            parent="Concept",
            description="设备类型（分类节点），用于同类故障复用",
            seeds=["减速机", "液压泵", "风机", "压缩机", "电机"],
        ),
        OntologyType(
            name="Line",
            parent="Location",
            description="产线或机组，停机决策的最小影响域",
            seeds=["L2", "L3", "C1", "D1"],
        ),
        OntologyType(
            name="Metric",
            parent="Concept",
            description="可测量指标及其单位",
            seeds=["振动", "轴承温度", "噪声"],
        ),
        OntologyType(
            name="FaultMode",
            parent="Concept",
            description="故障模式，由指标越限或人工判定触发",
            seeds=["振动超限", "轴承过热"],
        ),
        OntologyType(
            name="ThresholdRule",
            parent="Rule",
            description="指标判定区间与处置要求，可跨文档匹配观测值",
        ),
        OntologyType(
            name="Process",
            parent="Concept",
            description="一份可执行的处置流程",
            seeds=["异常振动处置"],
        ),
        OntologyType(
            name="ProcedureStep",
            parent="Concept",
            description="流程中的单步动作，可被引用与排序",
        ),
        OntologyType(
            name="Standard",
            parent="Reference",
            description="外部或厂内标准，如 ISO 10816-3:2009",
        ),
        OntologyType(
            name="StandardClause",
            parent="Reference",
            description="标准条款，溯源的最小锚点",
        ),
    ],
    relations=[
        RelationSchema(
            name="located_in",
            domain=["Equipment"],
            range=["Line"],
            description="设备所属产线",
        ),
        RelationSchema(
            name="classified_as",
            domain=["Equipment"],
            range=["EquipmentClass"],
            description="设备归属类型",
        ),
        RelationSchema(
            name="measured_by",
            domain=["Equipment"],
            range=["Metric"],
            description="设备被该指标持续观测",
        ),
        RelationSchema(
            name="exhibits",
            domain=["Equipment"],
            range=["FaultMode"],
            description="设备出现该故障模式（可跨文档证据）",
        ),
        RelationSchema(
            name="triggered_by",
            domain=["FaultMode"],
            range=["ThresholdRule"],
            description="故障模式由该判定规则触发",
        ),
        RelationSchema(
            name="defined_in",
            domain=["ThresholdRule"],
            range=["StandardClause"],
            description="判定规则的出处条款",
        ),
        RelationSchema(
            name="part_of",
            domain=["StandardClause", "ProcedureStep"],
            range=["Standard", "Process"],
            description="条款归属标准、步骤归属流程",
        ),
        RelationSchema(
            name="applies_to",
            domain=["Process"],
            range=["Equipment"],
            description="流程适用的设备",
        ),
        RelationSchema(
            name="has_step",
            domain=["Process"],
            range=["ProcedureStep"],
            description="流程包含的步骤",
        ),
        RelationSchema(
            name="cites",
            domain=["ProcedureStep"],
            range=["StandardClause"],
            description="步骤引用的条款",
        ),
        RelationSchema(
            name="precedes",
            domain=["ProcedureStep"],
            range=["ProcedureStep"],
            description="步骤先后顺序",
        ),
    ],
)


# --- text helpers -----------------------------------------------------------
STANDARD_PATTERN = re.compile(r"(ISO\s*\d{4,5}(?:-\d+)?(?::\d{4})?|Q/[A-Z]{2,4}-\d{4}-\d+)")
CLAUSE_PATTERN = re.compile(r"第\s*(\d+(?:\.\d+)*)\s*[节条]")
UNIT_PATTERN = re.compile(r"^([^\x00-\x7F]+)(.*)$")
NUMBER = r"(\d+(?:\.\d+)?)"
RANGE_PATTERNS = (
    (re.compile(rf"≤\s*{NUMBER}"), "le"),
    (re.compile(rf"<\s*{NUMBER}"), "lt"),
    (re.compile(rf"≥\s*{NUMBER}"), "ge"),
    (re.compile(rf">\s*{NUMBER}"), "gt"),
    (re.compile(rf"{NUMBER}\s*[~～\-]\s*{NUMBER}"), "between"),
)
THRESHOLD_PROSE = re.compile(
    rf"{NUMBER}\s*°?C\s*为\s*(报警|停机|跳机|预警)值", re.IGNORECASE
)
STEP_PATTERN = re.compile(r"^(\d+)[.、]\s*(.+)$")
HEADING_PATTERN = re.compile(r"^(#{1,6})\s*(.+)$")

# Column aliases are the only hand-written domain knowledge per table type:
# without them a Chinese CSV is just strings. Unknown numeric columns still
# become metrics, so a new sheet is not silently dropped.
LEDGER_ALIASES = {
    "设备编号": "equipment_id",
    "设备名称": "name",
    "所属产线": "line",
    "设备类型": "equipment_class",
    "投运日期": "commissioned_on",
    "额定功率kW": "rated_power_kw",
    "关键等级": "criticality",
    "最近大修": "last_overhaul",
}
OBSERVATION_ALIASES = {
    "巡检时间": "observed_at",
    "设备编号": "equipment_id",
    "巡检结论": "conclusion",
    "巡检人": "inspector",
}
METRIC_KEYWORDS = (("振动", "振动"), ("温度", "轴承温度"), ("噪声", "噪声"))


def _decode(data: bytes) -> str:
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def normalize_name(value: str) -> str:
    """Case/space/unit-insensitive key for dedupe and snapshot matching."""
    text = str(value).strip()
    text = text.replace("（", "(").replace("）", ")").replace("，", ",")
    text = re.sub(r"\s+", "", text)
    return text.casefold()


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fa5-]+", "-", str(value).strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-_") or "unk"


def decide(confidence: float) -> ReviewDecision:
    if confidence >= AUTO_ACCEPT_AT:
        return ReviewDecision.AUTO_ACCEPT
    if confidence >= REVIEW_FLOOR_AT:
        return ReviewDecision.REVIEW
    return ReviewDecision.DROP


def metric_key(text: str) -> str | None:
    for keyword, canonical in METRIC_KEYWORDS:
        if keyword in str(text):
            return canonical
    return None


def split_metric_header(header: str) -> tuple[str, str]:
    """`振动mm/s` -> (`振动`, `mm/s`); `轴承温度C` -> (`轴承温度`, `C`)."""
    match = UNIT_PATTERN.match(header.strip())
    if not match:
        return header.strip(), ""
    name, unit = match.group(1).strip(), match.group(2).strip()
    return name or header.strip(), unit


@dataclass(frozen=True)
class TableCell:
    line_no: int
    values: dict[str, str]


def _read_table(path: Path) -> tuple[list[str], list[TableCell]]:
    """Parse a delimited file, keeping the *file* line number of every row."""
    text = _decode(path.read_bytes())
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    reader = csv.reader(text.splitlines(), delimiter=delimiter)
    header: list[str] = []
    cells: list[TableCell] = []
    for line_no, row in enumerate(reader, start=1):
        if not any(cell.strip() for cell in row):
            continue
        if not header:
            header = [cell.strip() for cell in row]
            continue
        values = {
            header[index]: (row[index].strip() if index < len(row) else "")
            for index in range(len(header))
        }
        cells.append(TableCell(line_no=line_no, values=values))
    return header, cells


def _read_text_lines(path: Path) -> list[str]:
    return _decode(path.read_bytes()).splitlines()


def _evidence(asset: DataAsset, suffix: str, quote: str) -> Evidence:
    return Evidence(
        asset_id=asset.asset_id,
        locator=f"{asset.locator}{suffix}",
        quote=quote.strip()[:160],
    )


def _merge_evidence(items: Iterable[Evidence]) -> list[Evidence]:
    seen: dict[tuple[str, str], Evidence] = {}
    for item in items:
        seen.setdefault((item.asset_id, item.locator), item)
    return sorted(seen.values(), key=lambda it: (it.asset_id, it.locator))


# --- extractors -------------------------------------------------------------
@dataclass
class Draft:
    """Mutable accumulator; frozen into pydantic candidates at the end."""

    name: str
    type: str
    confidence: float
    extractor: str
    attributes: dict[str, Any]
    evidence: list[Evidence]


def _draft_entity(
    *,
    name: str,
    type_name: str,
    confidence: float,
    extractor: str,
    evidence: list[Evidence],
    attributes: Mapping[str, Any] | None = None,
) -> Draft:
    return Draft(
        name=name.strip(),
        type=type_name,
        confidence=confidence,
        extractor=extractor,
        attributes=dict(attributes or {}),
        evidence=_merge_evidence(evidence),
    )


def _draft_relation(
    *,
    subject: str,
    predicate: str,
    object_: str,
    confidence: float,
    extractor: str,
    evidence: list[Evidence],
) -> tuple[str, str, str, float, str, list[Evidence]]:
    return (subject.strip(), predicate, object_.strip(), confidence, extractor, evidence)


def extract_standard_refs(asset: DataAsset) -> tuple[list[Draft], list[tuple]]:
    """Standard/Clause entities from the refs declared on the asset record."""
    entities: list[Draft] = []
    relations: list[tuple] = []
    for ref in asset.standard_refs:
        match = re.match(r"^(?P<std>.+?)\s*§\s*(?P<clause>[\d.]+)$", ref.strip())
        if match:
            standard, clause = match.group("std").strip(), match.group("clause")
        else:
            standard, clause = ref.strip(), ""
        evidence = [_evidence(asset, "#standard_refs", ref)]
        entities.append(
            _draft_entity(
                name=standard,
                type_name="Standard",
                confidence=CONF_DECLARED_REF,
                extractor="declared_standard_ref",
                evidence=evidence,
            )
        )
        if not clause:
            continue
        clause_name = f"{standard} §{clause}"
        entities.append(
            _draft_entity(
                name=clause_name,
                type_name="StandardClause",
                confidence=CONF_DECLARED_REF,
                extractor="declared_standard_ref",
                evidence=evidence,
                attributes={"standard": standard, "clause": clause},
            )
        )
        relations.append(
            _draft_relation(
                subject=clause_name,
                predicate="part_of",
                object_=standard,
                confidence=CONF_DECLARED_REF,
                extractor="declared_standard_ref",
                evidence=evidence,
            )
        )
    return entities, relations


def extract_ledger(asset: DataAsset, path: Path) -> tuple[list[Draft], list[tuple]]:
    header, cells = _read_table(path)
    mapped = {LEDGER_ALIASES.get(name): name for name in header if name in LEDGER_ALIASES}
    if "equipment_id" not in mapped:
        return [], []

    entities: list[Draft] = []
    relations: list[tuple] = []
    for cell in cells:
        raw = {key: cell.values.get(column, "") for key, column in mapped.items()}
        equipment_id = raw.get("equipment_id", "").strip()
        if not equipment_id:
            continue
        name = raw.get("name", "").strip() or equipment_id
        attributes: dict[str, Any] = {"equipment_id": equipment_id}
        for key in ("commissioned_on", "criticality", "last_overhaul"):
            if raw.get(key):
                attributes[key] = raw[key]
        if raw.get("rated_power_kw"):
            try:
                attributes["rated_power_kw"] = float(raw["rated_power_kw"])
            except ValueError:
                attributes["rated_power_kw_raw"] = raw["rated_power_kw"]
        quote = "、".join(f"{key}={cell.values.get(col, '')}" for key, col in mapped.items())
        evidence = [_evidence(asset, f"#row:{cell.line_no}", quote)]
        entities.append(
            _draft_entity(
                name=name,
                type_name="Equipment",
                confidence=CONF_TABLE_TYPED,
                extractor="ledger_table",
                evidence=evidence,
                attributes=attributes,
            )
        )
        if raw.get("line"):
            line = raw["line"].strip()
            entities.append(
                _draft_entity(
                    name=line,
                    type_name="Line",
                    confidence=CONF_TABLE_TYPED,
                    extractor="ledger_table",
                    evidence=evidence,
                )
            )
            relations.append(
                _draft_relation(
                    subject=name,
                    predicate="located_in",
                    object_=line,
                    confidence=CONF_TABLE_INFERRED,
                    extractor="ledger_table",
                    evidence=evidence,
                )
            )
        if raw.get("equipment_class"):
            klass = raw["equipment_class"].strip()
            entities.append(
                _draft_entity(
                    name=klass,
                    type_name="EquipmentClass",
                    confidence=CONF_TABLE_TYPED,
                    extractor="ledger_table",
                    evidence=evidence,
                )
            )
            relations.append(
                _draft_relation(
                    subject=name,
                    predicate="classified_as",
                    object_=klass,
                    confidence=CONF_TABLE_INFERRED,
                    extractor="ledger_table",
                    evidence=evidence,
                )
            )
    return entities, relations


def extract_observations(
    asset: DataAsset, path: Path
) -> tuple[list[Draft], list[tuple], list[tuple]]:
    """Return (entities, relations, observations) — observations feed rule matching."""
    header, cells = _read_table(path)
    mapped = {
        OBSERVATION_ALIASES.get(name): name
        for name in header
        if name in OBSERVATION_ALIASES
    }
    if "equipment_id" not in mapped:
        return [], [], []

    metric_columns: dict[str, tuple[str, str]] = {}
    for name in header:
        if name in OBSERVATION_ALIASES:
            continue
        metric_name, unit = split_metric_header(name)
        if metric_name and any(
            _is_number(cell.values.get(name, "")) for cell in cells
        ):
            metric_columns[metric_name] = (name, unit)

    entities: list[Draft] = []
    relations: list[tuple] = []
    observations: list[tuple] = []

    for metric_name, (column, unit) in metric_columns.items():
        entities.append(
            _draft_entity(
                name=metric_name,
                type_name="Metric",
                confidence=CONF_TABLE_TYPED,
                extractor="observation_table",
                evidence=[
                    _evidence(
                        asset,
                        "#header",
                        f"列名 {column}" + (f"（单位 {unit}）" if unit else ""),
                    )
                ],
                attributes={"unit": unit} if unit else {},
            )
        )

    for cell in cells:
        equipment_id = cell.values.get(mapped["equipment_id"], "").strip()
        if not equipment_id:
            continue
        for metric_name, (column, unit) in metric_columns.items():
            raw_value = cell.values.get(column, "").strip()
            value = _parse_number(raw_value)
            if value is None:
                continue
            quote_parts = [f"{key}={cell.values.get(col, '')}" for key, col in mapped.items()]
            quote_parts.append(f"{column}={raw_value}")
            evidence = [_evidence(asset, f"#row:{cell.line_no}", "、".join(quote_parts))]
            relations.append(
                _draft_relation(
                    subject=equipment_id,
                    predicate="measured_by",
                    object_=metric_name,
                    confidence=CONF_TABLE_INFERRED,
                    extractor="observation_table",
                    evidence=evidence,
                )
            )
            observations.append(
                (
                    equipment_id,
                    metric_name,
                    value,
                    unit,
                    str(cell.values.get(mapped.get("observed_at", ""), "")).strip(),
                    cell.line_no,
                    asset,
                )
            )
    return entities, relations, observations


def _is_number(text: str) -> bool:
    return _parse_number(text) is not None


def _parse_number(text: str) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def extract_procedure(
    asset: DataAsset, path: Path
) -> tuple[list[Draft], list[tuple], list[tuple]]:
    """Parse threshold rules, procedure steps and standard clauses from markdown."""
    lines = _read_text_lines(path)
    entities: list[Draft] = []
    relations: list[tuple] = []
    rules: list[tuple] = []
    step_order: list[tuple[str, list[Evidence]]] = []

    current_heading = ""
    doc_title = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        heading = HEADING_PATTERN.match(line.strip())
        if heading:
            current_heading = heading.group(2).strip()
            if not doc_title and heading.group(1) == "#":
                doc_title = current_heading
            index += 1
            continue

        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            block, index = _consume_table(lines, index)
            block_entities, block_relations, block_rules = _parse_rule_table(
                asset, block, standard_hint=asset.standard_refs, heading=current_heading
            )
            entities.extend(block_entities)
            relations.extend(block_relations)
            rules.extend(block_rules)
            continue

        if STEP_PATTERN.match(stripped) and "步骤" in current_heading:
            step_entities, step_relations = _parse_step(
                asset, stripped, lines_index=index + 1, heading=current_heading
            )
            entities.extend(step_entities)
            relations.extend(step_relations)
            for item in step_entities:
                step_order.append((item.name, item.evidence))
            index += 1
            continue

        prose_entities, prose_relations, prose_rules = _parse_threshold_prose(
            asset, stripped, lines_index=index + 1
        )
        entities.extend(prose_entities)
        relations.extend(prose_relations)
        rules.extend(prose_rules)

        for match in STANDARD_PATTERN.finditer(stripped):
            standard = re.sub(r"\s+", " ", match.group(1)).strip()
            clause_match = CLAUSE_PATTERN.search(stripped)
            evidence = [_evidence(asset, f"#line:{index + 1}", stripped)]
            entities.append(
                _draft_entity(
                    name=standard,
                    type_name="Standard",
                    confidence=CONF_PROSE_PATTERN,
                    extractor="procedure_prose",
                    evidence=evidence,
                )
            )
            if clause_match:
                clause = clause_match.group(1)
                clause_name = f"{standard} §{clause}"
                entities.append(
                    _draft_entity(
                        name=clause_name,
                        type_name="StandardClause",
                        confidence=CONF_PROSE_PATTERN,
                        extractor="procedure_prose",
                        evidence=evidence,
                        attributes={"standard": standard, "clause": clause},
                    )
                )
                relations.append(
                    _draft_relation(
                        subject=clause_name,
                        predicate="part_of",
                        object_=standard,
                        confidence=CONF_PROSE_PATTERN,
                        extractor="procedure_prose",
                        evidence=evidence,
                    )
                )
        index += 1

    if not rules and not entities:
        return [], [], []
    process_name = _process_name(asset, doc_title)
    process_evidence = [_evidence(asset, "#heading", process_name)]
    entities.append(
        _draft_entity(
            name=process_name,
            type_name="Process",
            confidence=CONF_PROSE_PATTERN,
            extractor="procedure_prose",
            evidence=process_evidence,
        )
    )
    if asset.equipment_id:
        relations.append(
            _draft_relation(
                subject=process_name,
                predicate="applies_to",
                object_=asset.equipment_id,
                confidence=CONF_PROSE_PATTERN,
                extractor="procedure_prose",
                evidence=process_evidence,
            )
        )
    for position, (step_name, step_evidence) in enumerate(step_order):
        relations.append(
            _draft_relation(
                subject=process_name,
                predicate="has_step",
                object_=step_name,
                confidence=CONF_STEP_PATTERN,
                extractor="procedure_step",
                evidence=step_evidence,
            )
        )
        if position == 0:
            continue
        previous_name = step_order[position - 1][0]
        relations.append(
            _draft_relation(
                subject=previous_name,
                predicate="precedes",
                object_=step_name,
                confidence=CONF_STEP_PATTERN,
                extractor="procedure_step",
                evidence=step_evidence,
            )
        )
    return entities, relations, rules


def _process_name(asset: DataAsset, title: str = "") -> str:
    cleaned = re.sub(r"[（(][^）)]*[）)]\s*$", "", title.strip())
    return cleaned.strip() or asset.doc_type.strip() or "处置流程"


def _consume_table(lines: Sequence[str], start: int) -> tuple[list[tuple[int, str]], int]:
    """Collect a markdown table, tagging each row with its real 1-based line."""
    block: list[tuple[int, str]] = []
    index = start
    while index < len(lines) and lines[index].strip().startswith("|"):
        block.append((index + 1, lines[index].strip()))
        index += 1
    return block, index


def _split_row(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _parse_rule_table(
    asset: DataAsset,
    block: Sequence[tuple[int, str]],
    *,
    standard_hint: Sequence[str],
    heading: str,
) -> tuple[list[Draft], list[tuple], list[tuple]]:
    if len(block) < 3:
        return [], [], []
    header = _split_row(block[0][1])
    if not any("判定" in cell or "处置" in cell for cell in header):
        return [], [], []
    metric_col = next((i for i, cell in enumerate(header) if metric_key(cell)), None)
    if metric_col is None:
        return [], [], []
    metric_name, metric_unit = split_metric_header(header[metric_col])
    std_col = next((i for i, cell in enumerate(header) if "依据" in cell), None)
    action_col = next((i for i, cell in enumerate(header) if "处置" in cell), None)
    verdict_col = next((i for i, cell in enumerate(header) if "判定" in cell), None)

    entities: list[Draft] = []
    relations: list[tuple] = []
    rules: list[tuple] = []
    clause_ref = _default_clause(standard_hint)

    for position, (line_no, row) in enumerate(block[2:], start=1):
        cells = _split_row(row)
        if len(cells) <= metric_col:
            continue
        bounds = parse_range(cells[metric_col])
        if bounds is None:
            continue
        zone = cells[0] if cells else ""
        verdict = cells[verdict_col] if verdict_col is not None and verdict_col < len(cells) else ""
        action = cells[action_col] if action_col is not None and action_col < len(cells) else ""
        ref = cells[std_col] if std_col is not None and std_col < len(cells) else ""
        if not ref:
            ref = clause_ref
        rule_name = f"{metric_name}{zone}区判定" if zone else f"{metric_name}判定{position}"
        quote = " | ".join(cells)
        evidence = [_evidence(asset, f"#line:{line_no}", quote)]
        attributes: dict[str, Any] = {
            "metric": metric_name,
            "unit": metric_unit,
            "zone": zone,
            "verdict": verdict,
            "action": action,
            "on": heading,
        }
        attributes.update(bounds)
        entities.append(
            _draft_entity(
                name=rule_name,
                type_name="ThresholdRule",
                confidence=CONF_RULE_TABLE,
                extractor="rule_table",
                evidence=evidence,
                attributes=attributes,
            )
        )
        if ref:
            clause_name = str(ref).strip()
            if "§" not in clause_name:
                clause_name = f"{clause_name} §{zone or ''}".strip()
            relations.append(
                _draft_relation(
                    subject=rule_name,
                    predicate="defined_in",
                    object_=clause_name,
                    confidence=CONF_RULE_TABLE,
                    extractor="rule_table",
                    evidence=evidence,
                )
            )
        rules.append((rule_name, metric_name, bounds, verdict, action, evidence, attributes))
    return entities, relations, rules


def _default_clause(standard_refs: Sequence[str]) -> str:
    for ref in standard_refs:
        if "§" in ref:
            return ref.strip()
    return ""


def parse_range(text: str) -> dict[str, Any] | None:
    text = str(text).strip()
    for pattern, kind in RANGE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        if kind == "between":
            lower, upper = float(match.group(1)), float(match.group(2))
            return {
                "lower": lower,
                "upper": upper,
                "lower_inclusive": True,
                "upper_inclusive": True,
            }
        value = float(match.group(1))
        if kind in {"le", "lt"}:
            return {"upper": value, "upper_inclusive": kind == "le"}
        return {"lower": value, "lower_inclusive": kind == "ge"}
    return None


def rule_matches(bounds: Mapping[str, Any], value: float) -> bool:
    lower = bounds.get("lower")
    upper = bounds.get("upper")
    if lower is not None:
        if value < lower or (value == lower and not bounds.get("lower_inclusive", True)):
            return False
    if upper is not None:
        if value > upper or (value == upper and not bounds.get("upper_inclusive", True)):
            return False
    return True


def _parse_step(
    asset: DataAsset, line: str, *, lines_index: int, heading: str
) -> tuple[list[Draft], list[tuple]]:
    match = STEP_PATTERN.match(line)
    if not match:
        return [], []
    order, body = int(match.group(1)), match.group(2).strip()
    name = body if len(body) <= 40 else body[:40] + "…"
    evidence = [_evidence(asset, f"#line:{lines_index}", f"{order}. {body}")]
    entities = [
        _draft_entity(
            name=name,
            type_name="ProcedureStep",
            confidence=CONF_STEP_PATTERN,
            extractor="procedure_step",
            evidence=evidence,
            attributes={"order": order, "section": heading},
        )
    ]
    relations: list[tuple] = []
    for ref_match in STANDARD_PATTERN.finditer(body):
        standard = re.sub(r"\s+", " ", ref_match.group(1)).strip()
        clause_match = CLAUSE_PATTERN.search(body)
        target = (
            f"{standard} §{clause_match.group(1)}" if clause_match else standard
        )
        relations.append(
            _draft_relation(
                subject=name,
                predicate="cites",
                object_=target,
                confidence=CONF_STEP_PATTERN,
                extractor="procedure_step",
                evidence=evidence,
            )
        )
    return entities, relations


def _parse_threshold_prose(
    asset: DataAsset, line: str, *, lines_index: int
) -> tuple[list[Draft], list[tuple], list[tuple]]:
    entities: list[Draft] = []
    relations: list[tuple] = []
    rules: list[tuple] = []
    for match in THRESHOLD_PROSE.finditer(line):
        value, kind = float(match.group(1)), match.group(2)
        metric_name = metric_key(line)
        if metric_name is None:
            continue
        rule_name = f"{metric_name}{kind}值"
        evidence = [_evidence(asset, f"#line:{lines_index}", line)]
        attributes: dict[str, Any] = {
            "metric": metric_name,
            "kind": kind,
            "threshold": value,
            "lower": value,
            "lower_inclusive": True,
        }
        entities.append(
            _draft_entity(
                name=rule_name,
                type_name="ThresholdRule",
                confidence=CONF_PROSE_PATTERN,
                extractor="threshold_prose",
                evidence=evidence,
                attributes=attributes,
            )
        )
        relations.append(
            _draft_relation(
                subject=rule_name,
                predicate="defined_in",
                object_=_default_clause(asset.standard_refs),
                confidence=CONF_PROSE_PATTERN,
                extractor="threshold_prose",
                evidence=evidence,
            )
        )
        rules.append((rule_name, metric_name, attributes, kind, "", evidence, attributes))
    return entities, relations, rules


# --- bundling ---------------------------------------------------------------
class OntologyBundle(StrictModel):
    schema_version: str = SCHEMA_VERSION
    seed: OntologySchema
    entities: list[EntityCandidate]
    relations: list[RelationCandidate]
    warnings: list[str] = Field(default_factory=list)


def _candidate_id(prefix: str, type_name: str, name: str) -> str:
    return f"{prefix}-{_slug(type_name)}-{_slug(name)}"


NORMAL_HINTS = ("正常", "无需", "可长期")
FAULT_HINTS = ("危险", "异常", "不宜", "报警", "停机", "降负荷", "检修")


def _is_fault_verdict(verdict: str) -> bool:
    text = str(verdict)
    if any(hint in text for hint in NORMAL_HINTS):
        return False
    return any(hint in text for hint in FAULT_HINTS)


def _fault_mode_from_rule(
    observation: tuple, rule: tuple
) -> tuple[Draft, tuple, tuple]:
    rule_name, _, _, rule_verdict, action, rule_evidence, attributes = rule
    equipment_id, metric_name, value, unit, observed_at, line_no, asset = observation
    zone = str(attributes.get("zone", "")).strip()
    zone_label = f"{zone}区" if len(zone) == 1 else zone
    mode_name = f"{metric_name}{zone_label}超限" if zone_label else f"{metric_name}超限"
    observation_evidence = _evidence(
        asset,
        f"#row:{line_no}",
        f"{observed_at} {equipment_id} {metric_name}={value}{unit}",
    )
    evidence = _merge_evidence([observation_evidence, *rule_evidence])
    entity = _draft_entity(
        name=mode_name,
        type_name="FaultMode",
        confidence=CONF_CROSS_DOC,
        extractor="cross_document_rule_match",
        evidence=evidence,
        attributes={
            "metric": metric_name,
            "observed_value": value,
            "unit": unit,
            "observed_at": observed_at,
            "zone": zone,
            "verdict": rule_verdict,
            "recommended_action": action,
            "trigger_asset": asset.asset_id,
        },
    )
    relation = _draft_relation(
        subject=equipment_id,
        predicate="exhibits",
        object_=mode_name,
        confidence=CONF_CROSS_DOC,
        extractor="cross_document_rule_match",
        evidence=evidence,
    )
    trigger = _draft_relation(
        subject=mode_name,
        predicate="triggered_by",
        object_=rule_name,
        confidence=CONF_CROSS_DOC,
        extractor="cross_document_rule_match",
        evidence=evidence,
    )
    return entity, relation, trigger


def _pick_type(
    types: set[str] | None, spec: RelationSchema | None, side: str
) -> str | None:
    """Resolve a name that may denote several types (`干燥机` is equipment *and* class)."""
    if not types:
        return None
    if spec is not None:
        allowed = set(spec.domain if side == "domain" else spec.range)
        for item in sorted(types):
            if item in allowed:
                return item
    return sorted(types)[0]


def _conform(
    schema: OntologySchema,
    predicate: str,
    subject_type: str | None,
    object_type: str | None,
) -> tuple[bool, str]:
    spec = schema.relation(predicate)
    if spec is None:
        return False, f"未声明的谓词 {predicate}"
    if subject_type is None or object_type is None:
        return False, f"{predicate} 端点类型未解析"
    if subject_type not in spec.domain:
        return False, f"{predicate} 主语类型 {subject_type} 不在 {spec.domain}"
    if object_type not in spec.range:
        return False, f"{predicate} 宾语类型 {object_type} 不在 {spec.range}"
    return True, ""


def build_bundle(
    assets: Sequence[DataAsset],
    root: Path | str = Path("."),
    *,
    assets_dir: Path | str | None = None,
    schema: OntologySchema | None = None,
) -> OntologyBundle:
    """Run the full seed -> induce -> align pipeline over the registered assets.

    `assets_dir` and `schema` are injectable so a new domain can reuse the
    pipeline instead of forking it; the defaults reproduce the original
    equipment-maintenance behaviour exactly.

    Note: table routing below still only recognises the maintenance ledger and
    inspection-log headers. Other domains route their tables through
    `freeze_candidates` directly (see `city_extract.py`).
    """
    base = Path(root)
    directory = Path(assets_dir) if assets_dir is not None else base / "samples" / "assets"
    active_schema = schema or SEED_ONTOLOGY
    warnings: list[str] = []
    entity_drafts: list[Draft] = []
    relation_drafts: list[tuple] = []
    observations: list[tuple] = []
    rules: list[tuple] = []

    for asset in assets:
        path = directory / asset.file_name
        if not path.is_file():
            path = base / asset.file_name
        if not path.is_file():
            warnings.append(f"资产文件缺失，跳过抽取：{asset.file_name}")
            continue

        std_entities, std_relations = extract_standard_refs(asset)
        entity_drafts.extend(std_entities)
        relation_drafts.extend(std_relations)

        if asset.modality is AssetModality.TABLE:
            header, _ = _read_table(path)
            if "设备名称" in header and "设备编号" in header:
                e, r = extract_ledger(asset, path)
                entity_drafts.extend(e)
                relation_drafts.extend(r)
            elif "巡检时间" in header and "设备编号" in header:
                e, r, obs = extract_observations(asset, path)
                entity_drafts.extend(e)
                relation_drafts.extend(r)
                observations.extend(obs)
            else:
                warnings.append(f"未识别的表格结构，仅登记资产：{asset.file_name}")
        elif asset.modality is AssetModality.TEXT:
            e, r, parsed_rules = extract_procedure(asset, path)
            entity_drafts.extend(e)
            relation_drafts.extend(r)
            rules.extend(parsed_rules)

    # Cross-document hop: numeric observations matched against rules parsed
    # from another document. Evidence keeps both locators, so the resulting
    # FaultMode is traceable back to two separate assets.
    for observation in observations:
        _, metric_name, value = observation[0], observation[1], observation[2]
        for rule in rules:
            if metric_key(rule[1]) != metric_key(metric_name):
                continue
            if not _is_fault_verdict(rule[3]):
                continue
            if not rule_matches(rule[2], value):
                continue
            entity, relation, trigger = _fault_mode_from_rule(observation, rule)
            entity_drafts.append(entity)
            relation_drafts.extend([relation, trigger])
            break

    entities, relations, merge_warnings = freeze_candidates(
        entity_drafts, relation_drafts, schema=active_schema
    )
    warnings.extend(merge_warnings)
    return OntologyBundle(
        seed=active_schema,
        entities=entities,
        relations=relations,
        warnings=sorted(set(warnings)),
    )


def freeze_candidates(
    entity_drafts: Sequence[Draft],
    relation_drafts: Sequence[tuple],
    *,
    schema: OntologySchema,
) -> tuple[list[EntityCandidate], list[RelationCandidate], list[str]]:
    merged: dict[tuple[str, str], Draft] = {}
    warnings: list[str] = []
    for draft in entity_drafts:
        if not draft.name:
            continue
        key = (draft.type, normalize_name(draft.name))
        existing = merged.get(key)
        if existing is None:
            merged[key] = Draft(
                name=draft.name,
                type=draft.type,
                confidence=draft.confidence,
                extractor=draft.extractor,
                attributes=dict(draft.attributes),
                evidence=list(draft.evidence),
            )
            continue
        existing.evidence = _merge_evidence([*existing.evidence, *draft.evidence])
        existing.confidence = min(existing.confidence, draft.confidence)
        for name, value in draft.attributes.items():
            if name not in existing.attributes:
                existing.attributes[name] = value
            elif existing.attributes[name] != value and name not in {"unit"}:
                warnings.append(
                    f"属性冲突 {existing.name}.{name}: 保留 {existing.attributes[name]!r}，"
                    f"忽略 {value!r}"
                )

    entities = [
        EntityCandidate(
            candidate_id=_candidate_id("EN", draft.type, draft.name),
            name=draft.name,
            type=draft.type,
            confidence=round(draft.confidence, 2),
            attributes=draft.attributes,
            evidence=draft.evidence,
            decision=decide(draft.confidence),
            extractor=draft.extractor,
        )
        for draft in merged.values()
        if draft.type in schema.type_names()
    ]

    # A name that denotes more than one type is a genuine ontology ambiguity
    # (`干燥机` is both an Equipment and an EquipmentClass), so it must not be
    # auto-accepted — a human has to pick the intended node.
    types_by_name: dict[str, set[str]] = {}
    for item in entities:
        types_by_name.setdefault(normalize_name(item.name), set()).add(item.type)
    ambiguous = {
        normalize_name(item.name): sorted(types_by_name[normalize_name(item.name)])
        for item in entities
        if len(types_by_name[normalize_name(item.name)]) > 1
    }
    if ambiguous:
        for item in entities:
            key = normalize_name(item.name)
            if key in ambiguous and item.decision is ReviewDecision.AUTO_ACCEPT:
                item.decision = ReviewDecision.REVIEW
        for key, names in sorted(ambiguous.items()):
            warnings.append(f"同名多类型，需人工确认：{names}")
    entity_types: dict[str, set[str]] = {}
    for item in entities:
        entity_types.setdefault(normalize_name(item.name), set()).add(item.type)
    # Equipment is referred to by business code (`A-101`) in logs and by name
    # (`主传动减速机`) in the ledger; both must resolve to the same node.
    for item in entities:
        business_id = item.attributes.get("equipment_id")
        if business_id:
            entity_types.setdefault(normalize_name(str(business_id)), set()).add(item.type)

    relations: list[RelationCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for subject, predicate, object_, confidence, extractor, evidence in relation_drafts:
        if normalize_name(subject) == normalize_name(object_):
            # A node pointing at itself carries no graph information; in this
            # catalogue it always means a name collision (equipment named the
            # same as its class), which the entity stage already flags.
            warnings.append(f"自指关系已跳过：{subject} -{predicate}-> {object_}")
            continue
        spec = schema.relation(predicate)
        subject_type = _pick_type(entity_types.get(normalize_name(subject)), spec, "domain")
        object_type = _pick_type(entity_types.get(normalize_name(object_)), spec, "range")
        ok, reason = _conform(schema, predicate, subject_type, object_type)
        if not ok:
            warnings.append(f"关系未通过 schema 校验（{reason}）：{subject} -{predicate}-> {object_}")
            confidence = min(confidence, REVIEW_FLOOR_AT - 0.01)
        key = (normalize_name(subject), predicate, normalize_name(object_))
        if key in seen:
            continue
        seen.add(key)
        relations.append(
            RelationCandidate(
                candidate_id=_candidate_id("REL", predicate, f"{subject}-{object_}"),
                subject=subject,
                predicate=predicate,
                object=object_,
                confidence=round(confidence, 2),
                evidence=_merge_evidence(evidence),
                decision=decide(confidence),
                extractor=extractor,
            )
        )

    entities.sort(key=lambda item: item.candidate_id)
    relations.sort(key=lambda item: item.candidate_id)
    return entities, relations, warnings


# --- reporting --------------------------------------------------------------
# Backwards-compatible alias: the maintenance pipeline and its tests were written
# against the private name; new domains should call `freeze_candidates`.
_freeze = freeze_candidates


def bundle_stats(bundle: OntologyBundle, *, asset_count: int) -> dict[str, Any]:
    all_candidates = [*bundle.entities, *bundle.relations]
    total = len(all_candidates)
    counts: dict[str, int] = {decision.value: 0 for decision in ReviewDecision}
    for item in all_candidates:
        counts[item.decision.value] += 1
    with_evidence = sum(1 for item in all_candidates if item.evidence)
    cross_asset = sum(
        1 for item in all_candidates if len({ev.asset_id for ev in item.evidence}) > 1
    )
    covered_types = {item.type for item in bundle.entities}
    seed_types = bundle.seed.type_names()
    review_total = counts[ReviewDecision.REVIEW.value] + counts[
        ReviewDecision.AUTO_ACCEPT.value
    ]
    return {
        "asset_count": asset_count,
        "entity_count": len(bundle.entities),
        "relation_count": len(bundle.relations),
        "candidate_count": total,
        "decision_counts": counts,
        "provenance_rate": round(with_evidence / total, 4) if total else 0.0,
        "cross_asset_candidates": cross_asset,
        "type_coverage": round(len(covered_types & seed_types) / len(seed_types), 4),
        "types_covered": sorted(covered_types & seed_types),
        "review_load_per_asset": round(
            counts[ReviewDecision.REVIEW.value] / asset_count, 2
        )
        if asset_count
        else 0.0,
        "auto_accept_rate": round(counts[ReviewDecision.AUTO_ACCEPT.value] / total, 4)
        if total
        else 0.0,
        "seed_type_count": len(seed_types),
        "warning_count": len(bundle.warnings),
        "review_hint": f"每份资产需人工确认 {round(counts[ReviewDecision.REVIEW.value] / asset_count, 2)} 条"
        if asset_count
        else "",
    }


def bundle_payload(bundle: OntologyBundle, *, asset_count: int) -> dict[str, Any]:
    return {
        "schema_version": bundle.schema_version,
        "seed": bundle.seed.model_dump(mode="json"),
        "stats": bundle_stats(bundle, asset_count=asset_count),
        "entities": [item.model_dump(mode="json") for item in bundle.entities],
        "relations": [item.model_dump(mode="json") for item in bundle.relations],
        "warnings": list(bundle.warnings),
    }


class MergeOp(StrictModel):
    op: str = Field(pattern="^(add|merge|conflict|unchanged)$")
    candidate_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    detail: str = Field(min_length=1)


def plan_merge(
    bundle: OntologyBundle, snapshot: Sequence[Mapping[str, Any]]
) -> list[MergeOp]:
    """Diff candidates against an existing KG snapshot (`kg.kg_entity` export)."""
    index: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in snapshot:
        name = str(row.get("name", "")).strip()
        type_name = str(row.get("type", "")).strip()
        if name and type_name:
            index[(type_name, normalize_name(name))] = row
    ops: list[MergeOp] = []
    for entity in bundle.entities:
        key = (entity.type, normalize_name(entity.name))
        existing = index.get(key)
        if existing is None:
            ops.append(
                MergeOp(
                    op="add",
                    candidate_id=entity.candidate_id,
                    name=entity.name,
                    type=entity.type,
                    detail="快照中不存在，可直接新增",
                )
            )
            continue
        existing_attributes = existing.get("attributes") or {}
        conflicts = [
            name
            for name, value in entity.attributes.items()
            if name in existing_attributes and existing_attributes[name] != value
        ]
        if conflicts:
            ops.append(
                MergeOp(
                    op="conflict",
                    candidate_id=entity.candidate_id,
                    name=entity.name,
                    type=entity.type,
                    detail="属性冲突：" + ", ".join(sorted(conflicts)),
                )
            )
        else:
            ops.append(
                MergeOp(
                    op="merge",
                    candidate_id=entity.candidate_id,
                    name=entity.name,
                    type=entity.type,
                    detail=f"补充 {len(entity.attributes)} 项属性与 {len(entity.evidence)} 条证据",
                )
            )
    ops.sort(key=lambda item: (item.op, item.candidate_id))
    return ops


def merge_summary(ops: Sequence[MergeOp]) -> dict[str, int]:
    summary = {"add": 0, "merge": 0, "conflict": 0, "unchanged": 0}
    for op in ops:
        summary[op.op] = summary.get(op.op, 0) + 1
    return summary
