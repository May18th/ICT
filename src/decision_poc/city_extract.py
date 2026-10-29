"""Deterministic candidate extraction for the city-infrastructure domain.

This is an *explainable baseline*, not a model: every candidate comes from a
typed table cell, a foreign-key join, a declared duty scope, or a numbered
procedure step, and always carries an `Evidence` locator.

Deliberate non-behaviour: `DF-PS-01` has no `owner_org`, so **no**
`responsible_for` relation is produced for it. Filling that gap automatically
would destroy the 职责空白 conflict the demo exists to show.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

from .adapters import ParsedDocument, parse_assets
from .city_profile import ASSET_ENTITY_TYPE, CITY_SCHEMA, ID_COLUMN
from .contracts import StrictModel
from .data_asset import DataAsset
from .ontology import (
    CONF_PROSE_PATTERN,
    CONF_STEP_PATTERN,
    CONF_TABLE_INFERRED,
    CONF_TABLE_TYPED,
    Draft,
    Evidence,
    OntologyBundle,
    OntologySchema,
    bundle_stats,
    freeze_candidates,
    normalize_name,
)

STEP_PATTERN = re.compile(r"^(?P<no>\d+\.\d+)\s*(?P<text>.+)$")
ACTION_NAME_LIMIT = 24
QUOTE_LIMIT = 120
DEPRECATED_FACTOR = 0.7


class CityExtractionStats(StrictModel):
    assets: int
    entities: int
    relations: int
    auto_accept: int
    review: int
    drop: int
    warnings: list[str]


def _evidence(asset: DataAsset, suffix: str, quote: str) -> Evidence:
    text = quote.strip().replace("\n", " ")[:QUOTE_LIMIT]
    return Evidence(asset_id=asset.asset_id, locator=f"{asset.locator}{suffix}", quote=text or "-")


def _entity(
    *,
    name: str,
    type_name: str,
    confidence: float,
    extractor: str,
    evidence: list[Evidence],
    attributes: dict[str, Any] | None = None,
) -> Draft:
    return Draft(
        name=name.strip(),
        type=type_name,
        confidence=confidence,
        extractor=extractor,
        attributes=dict(attributes or {}),
        evidence=evidence,
    )


def _relation(
    subject: str,
    predicate: str,
    object_: str,
    *,
    confidence: float,
    extractor: str,
    evidence: list[Evidence],
) -> tuple[str, str, str, float, str, list[Evidence]]:
    return (subject.strip(), predicate, object_.strip(), confidence, extractor, evidence)


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _quote_from_row(row: dict[str, Any], columns: Sequence[str]) -> str:
    parts = [f"{column}={row[column]}" for column in columns if row.get(column) is not None]
    return "；".join(parts) if parts else "-"


def _plan_entities(
    documents: Sequence[ParsedDocument], assets: dict[str, DataAsset]
) -> tuple[list[Draft], dict[str, str]]:
    """EmergencyPlan nodes named by their document title, keyed by asset id."""
    drafts: list[Draft] = []
    names: dict[str, str] = {}
    for document in documents:
        asset = assets[document.asset_id]
        if ASSET_ENTITY_TYPE.get(asset.doc_type) != "EmergencyPlan":
            continue
        title = ""
        for chunk in document.chunks:
            if chunk.heading_path:
                title = chunk.heading_path[0]
                break
        name = title or (asset.standard_refs[0] if asset.standard_refs else Path(asset.file_name).stem)
        names[asset.asset_id] = name
        anchor = (
            document.chunks[0].locators[0].split("#line:")[-1] if document.chunks else "1-1"
        )
        evidence = [_evidence(asset, f"#line:{anchor}", title or asset.summary)]
        drafts.append(
            _entity(
                name=name,
                type_name="EmergencyPlan",
                confidence=CONF_TABLE_TYPED,
                extractor="city_plan_title",
                evidence=evidence,
                attributes={
                    "file_name": asset.file_name,
                    "version": asset.version,
                    "standard_refs": list(asset.standard_refs),
                },
            )
        )
    return drafts, names


def _action_entities(
    documents: Sequence[ParsedDocument],
    assets: dict[str, DataAsset],
    plan_names: dict[str, str],
) -> tuple[list[Draft], list[tuple]]:
    """Numbered procedure steps become Action nodes linked by `precedes`."""
    drafts: list[Draft] = []
    relations: list[tuple] = []
    used_names: set[str] = set()
    for document in documents:
        asset = assets[document.asset_id]
        plan_name = plan_names.get(asset.asset_id)
        if plan_name is None:
            continue
        previous: str | None = None
        for chunk in document.chunks:
            if not any("处置顺序" in heading or "步骤" in heading for heading in chunk.heading_path):
                continue
            for raw_line in chunk.text.splitlines():
                match = STEP_PATTERN.match(raw_line.strip())
                if not match:
                    continue
                text = match.group("text").strip()
                name = text[:ACTION_NAME_LIMIT].rstrip("，,。；;")
                if name in used_names:
                    # 不同预案的同序步骤会被截断成同名；带上步骤号保证可区分
                    name = f"{match.group('no')} {name}"
                used_names.add(name)
                line_span = chunk.locators[0].split("#line:")[-1]
                evidence = [_evidence(asset, f"#line:{line_span}", text)]
                drafts.append(
                    _entity(
                        name=name,
                        type_name="Action",
                        confidence=CONF_STEP_PATTERN,
                        extractor="city_procedure_step",
                        evidence=evidence,
                        attributes={"step_no": match.group("no"), "text": text, "plan": plan_name},
                    )
                )
                relations.append(
                    _relation(
                        name,
                        "covered_by_plan",
                        plan_name,
                        confidence=CONF_STEP_PATTERN,
                        extractor="city_procedure_step",
                        evidence=evidence,
                    )
                )
                if previous is not None:
                    relations.append(
                        _relation(
                            previous,
                            "precedes",
                            name,
                            confidence=CONF_TABLE_INFERRED,
                            extractor="city_step_order",
                            evidence=evidence,
                        )
                    )
                previous = name
    return drafts, relations


def _department_relations(
    entity_attrs: dict[str, dict[str, Any]],
) -> list[tuple]:
    """Duty scopes -> responsible_for; overlapping authorities -> conflicts_with."""
    relations: list[tuple] = []
    departments = {
        name: attrs for name, attrs in entity_attrs.items() if attrs.get("__type") == "Department"
    }
    for name, attrs in departments.items():
        duties = attrs.get("_evidence", [])
        scope_type = str(attrs.get("scope_type") or "")
        scope_ref = str(attrs.get("scope_ref") or "")
        refs = [item.strip() for item in re.split(r"[;；,，]", scope_ref) if item.strip()]
        for target_name, target_attrs in entity_attrs.items():
            target_type = target_attrs.get("__type")
            if target_type == "Pipeline" and scope_type == "pipeline_kind":
                if str(target_attrs.get("kind") or "") in refs:
                    relations.append(
                        _relation(
                            name,
                            "responsible_for",
                            target_name,
                            confidence=CONF_TABLE_INFERRED,
                            extractor="city_duty_scope",
                            evidence=duties,
                        )
                    )
            elif target_type == "Road" and scope_type == "road_class":
                if str(target_attrs.get("road_class") or "") in refs:
                    relations.append(
                        _relation(
                            name,
                            "responsible_for",
                            target_name,
                            confidence=CONF_TABLE_INFERRED,
                            extractor="city_duty_scope",
                            evidence=duties,
                        )
                    )

    # 封路权限交叉：两个部门都声明了封路相关权限，属显式冲突而非主责不明
    CLOSURE_KEYWORDS = ("封路", "封闭", "交通管制")
    road_like = [
        (name, attrs)
        for name, attrs in departments.items()
        if any(keyword in str(attrs.get("authority") or "") for keyword in CLOSURE_KEYWORDS)
    ]
    for index, (left, left_attrs) in enumerate(road_like):
        for right, right_attrs in road_like[index + 1 :]:
            relations.append(
                _relation(
                    left,
                    "conflicts_with",
                    right,
                    confidence=CONF_PROSE_PATTERN,
                    extractor="city_authority_overlap",
                    evidence=[*left_attrs.get("_evidence", []), *right_attrs.get("_evidence", [])],
                )
            )
    return relations


def extract_city_candidates(
    documents: Sequence[ParsedDocument],
    assets: Sequence[DataAsset],
    *,
    schema: OntologySchema = CITY_SCHEMA,
    asset_status: dict[str, str] | None = None,
) -> OntologyBundle:
    asset_map = {asset.asset_id: asset for asset in assets}
    entity_drafts: list[Draft] = []
    relation_drafts: list[tuple] = []
    entity_attrs: dict[str, dict[str, Any]] = {}
    name_by_id_column: dict[str, str] = {}

    # --- 1. table rows -> entities -----------------------------------------
    for document in documents:
        asset = asset_map[document.asset_id]
        entity_type = ASSET_ENTITY_TYPE.get(asset.doc_type)
        id_column = ID_COLUMN.get(asset.doc_type)
        if entity_type is None or id_column is None:
            continue
        for table in document.tables:
            if id_column not in table.column_names():
                continue
            columns = [column.canonical for column in table.columns][:4]
            for position, row in enumerate(table.rows):
                raw_name = row.get(id_column)
                if not isinstance(raw_name, str) or not raw_name.strip():
                    continue
                name = raw_name.strip()
                attributes = {
                    column: _json_safe(value)
                    for column, value in row.items()
                    if value is not None and column != id_column
                }
                evidence = [
                    _evidence(asset, table.row_locators[position], _quote_from_row(row, columns))
                ]
                entity_drafts.append(
                    _entity(
                        name=name,
                        type_name=entity_type,
                        confidence=CONF_TABLE_TYPED,
                        extractor=f"city_table_{asset.doc_type}",
                        evidence=evidence,
                        attributes=attributes,
                    )
                )
                entity_attrs.setdefault(
                    name, {"__type": entity_type, **attributes, "_evidence": evidence}
                )
                name_by_id_column[name] = entity_type

    # --- 2. district -> Area, and located_in ------------------------------
    districts: dict[str, list[Evidence]] = {}
    for document in documents:
        asset = asset_map[document.asset_id]
        for table in document.tables:
            if "district" not in table.column_names():
                continue
            for position, row in enumerate(table.rows):
                value = row.get("district")
                if not isinstance(value, str) or not value.strip():
                    continue
                districts.setdefault(value.strip(), []).append(
                    _evidence(asset, table.row_locators[position], f"district={value}")
                )
    for district, evidence in districts.items():
        entity_drafts.append(
            _entity(
                name=district,
                type_name="Area",
                confidence=CONF_TABLE_TYPED,
                extractor="city_table_district",
                evidence=evidence,
            )
        )
        entity_attrs.setdefault(district, {"__type": "Area", "_evidence": evidence})

    # facilities inherit the district of the road they sit on
    road_district = {
        name: str(attrs.get("district"))
        for name, attrs in entity_attrs.items()
        if attrs.get("__type") == "Road" and attrs.get("district")
    }
    for name, attrs in list(entity_attrs.items()):
        district = attrs.get("district")
        if isinstance(district, str) and district in entity_attrs:
            relation_drafts.append(
                _relation(
                    name,
                    "located_in",
                    district,
                    confidence=CONF_TABLE_INFERRED,
                    extractor="city_row_district",
                    evidence=attrs.get("_evidence", []),
                )
            )
        road_id = attrs.get("road_id")
        if isinstance(road_id, str) and road_id in entity_attrs:
            host_district = road_district.get(road_id)
            if host_district:
                relation_drafts.append(
                    _relation(
                        name,
                        "located_in",
                        host_district,
                        confidence=CONF_TABLE_INFERRED,
                        extractor="city_road_district",
                        evidence=attrs.get("_evidence", []),
                    )
                )
            if attrs.get("__type") == "Pipeline":
                relation_drafts.append(
                    _relation(
                        name,
                        "connected_to",
                        road_id,
                        confidence=CONF_TABLE_INFERRED,
                        extractor="city_pipeline_road",
                        evidence=attrs.get("_evidence", []),
                    )
                )

    # --- 3. construction / incident links ---------------------------------
    for name, attrs in entity_attrs.items():
        entity_type = attrs.get("__type")
        evidence = attrs.get("_evidence", [])
        if entity_type == "ConstructionProject":
            pipeline_id = attrs.get("pipeline_id")
            if isinstance(pipeline_id, str) and pipeline_id in entity_attrs:
                relation_drafts.append(
                    _relation(
                        pipeline_id,
                        "under_construction",
                        name,
                        confidence=CONF_TABLE_INFERRED,
                        extractor="city_construction_pipeline",
                        evidence=evidence,
                    )
                )
            road_id = attrs.get("road_id")
            if isinstance(road_id, str) and road_id in entity_attrs:
                relation_drafts.append(
                    _relation(
                        road_id,
                        "under_construction",
                        name,
                        confidence=CONF_TABLE_INFERRED,
                        extractor="city_construction_road",
                        evidence=evidence,
                    )
                )
                host_district = road_district.get(road_id)
                if host_district:
                    relation_drafts.append(
                        _relation(
                            name,
                            "located_in",
                            host_district,
                            confidence=CONF_TABLE_INFERRED,
                            extractor="city_construction_road",
                            evidence=evidence,
                        )
                    )
        elif entity_type == "Incident":
            for key, target_type in (("road_id", "Road"), ("facility_id", "DrainageFacility")):
                target = attrs.get(key)
                if isinstance(target, str) and entity_attrs.get(target, {}).get("__type") == target_type:
                    relation_drafts.append(
                        _relation(
                            target,
                            "affected_by",
                            name,
                            confidence=CONF_TABLE_INFERRED,
                            extractor="city_incident_target",
                            evidence=evidence,
                        )
                    )

    # --- 4. 权属单位 -> responsible_for（空值绝不补全）----------------------
    for name, attrs in entity_attrs.items():
        owner = attrs.get("owner_org")
        if not isinstance(owner, str) or not owner.strip():
            continue
        if entity_attrs.get(owner, {}).get("__type") != "Department":
            continue
        if attrs.get("__type") not in {"Pipeline", "DrainageFacility", "Resource"}:
            continue
        relation_drafts.append(
            _relation(
                owner,
                "responsible_for",
                name,
                confidence=CONF_TABLE_INFERRED,
                extractor="city_owner_org",
                evidence=attrs.get("_evidence", []),
            )
        )

    relation_drafts.extend(_department_relations(entity_attrs))

    # --- 5. plans and procedure steps -------------------------------------
    plan_drafts, plan_names = _plan_entities(documents, asset_map)
    entity_drafts.extend(plan_drafts)
    action_drafts, action_relations = _action_entities(documents, asset_map, plan_names)
    entity_drafts.extend(action_drafts)
    relation_drafts.extend(action_relations)

    if asset_status:
        relation_drafts = _penalise_deprecated(entity_drafts, relation_drafts, asset_status)

    entities, relations, warnings = freeze_candidates(
        entity_drafts, relation_drafts, schema=schema
    )
    if asset_status and any(status == "deprecated" for status in asset_status.values()):
        warnings.append("存在已废止资产：其候选已降权至人工复核区间，不得自动采纳")
    return OntologyBundle(
        seed=schema,
        entities=entities,
        relations=relations,
        warnings=sorted(set(warnings)),
    )


def _penalise_deprecated(
    entity_drafts: list[Draft],
    relation_drafts: list[tuple],
    asset_status: dict[str, str],
) -> list[tuple]:
    """Deprecated sources must not produce auto-accepted knowledge.

    The knowledge still enters the graph (it is needed to show 冲突 and 过期),
    but at a confidence that forces human review.
    """
    for draft in entity_drafts:
        if any(asset_status.get(item.asset_id) == "deprecated" for item in draft.evidence):
            draft.confidence = round(draft.confidence * DEPRECATED_FACTOR, 3)
    penalised: list[tuple] = []
    for subject, predicate, object_, confidence, extractor, evidence in relation_drafts:
        if any(asset_status.get(item.asset_id) == "deprecated" for item in evidence):
            confidence = round(confidence * DEPRECATED_FACTOR, 3)
        penalised.append((subject, predicate, object_, confidence, extractor, evidence))
    return penalised


def extraction_stats(bundle: OntologyBundle, *, asset_count: int = 0) -> CityExtractionStats:
    stats = bundle_stats(bundle, asset_count=asset_count)
    decisions = [entity.decision.value for entity in bundle.entities]
    return CityExtractionStats(
        assets=asset_count,
        entities=len(bundle.entities),
        relations=len(bundle.relations),
        auto_accept=decisions.count("auto_accept"),
        review=decisions.count("review"),
        drop=decisions.count("drop"),
        warnings=list(bundle.warnings),
    )


def build_city_bundle(
    dataset_root: Path | str | None = None,
) -> OntologyBundle:
    """End-to-end: register -> parse -> extract, all offline."""
    from .urban_dataset import DEFAULT_DATASET_ROOT, load_urban_assets

    root = Path(dataset_root) if dataset_root is not None else DEFAULT_DATASET_ROOT
    rows = load_urban_assets(root)
    assets = [asset for asset, _ in rows]
    status = {asset.asset_id: governance.status.value for asset, governance in rows}
    documents = parse_assets(assets, root, annotations_path=root / "annotations.json")
    return extract_city_candidates(documents, assets, asset_status=status)
